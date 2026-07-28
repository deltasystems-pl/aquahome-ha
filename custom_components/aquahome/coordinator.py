"""Per-device data update coordinator for the AquaHome integration.

One :class:`AquaHomeCoordinator` polls a single iQua device on the fixed
:data:`~.const.UPDATE_INTERVAL` cadence via ``GET /devices/{id}?props=true``.
The cloud is aggressively throttled and occasionally blips, so the coordinator
serves last-good data for up to :data:`~.const.MAX_STALE_SECONDS` across rate
limits and transient 5xx failures instead of flapping every entity to
unavailable. Authentication failures bypass that grace period and route straight
to Home Assistant's reauth flow; a genuine 4xx contract failure is surfaced
honestly.

:data:`AquaHomeRuntimeData` is the object stored on ``entry.runtime_data`` and
:func:`resolve_device_online` is the single, host-neutral availability signal
shared with the binary-sensor platform.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    Alert,
    ApiError,
    AquaHomeClient,
    AquaHomeConnectionError,
    AquaHomeError,
    AuthError,
    AuthManager,
    Device,
    DeviceSettingsDocument,
    RateLimitError,
    RegenerationEvent,
)
from .const import (
    ACTIVITY_MAX_STALE_SECONDS,
    ACTIVITY_PAGE_SIZE,
    ACTIVITY_UPDATE_INTERVAL,
    DOMAIN,
    EVENT_AQUAHOME,
    MAX_STALE_SECONDS,
    SETTINGS_MAX_STALE_SECONDS,
    SETTINGS_UPDATE_INTERVAL,
    UPDATE_INTERVAL,
)
from .entity import device_slug

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .analytics.engine import AquaHomeAnalyticsEngine
    from .live import AquaHomeLiveManager
    from .scheduler import AquaHomeRegenScheduler
    from .statistics import AquaHomeStatisticsCoordinator

_LOGGER = logging.getLogger(__name__)

type AquaHomeConfigEntry = ConfigEntry[AquaHomeRuntimeData]

#: Aware sentinel used to sort alerts lacking a timestamp after every dated one.
_UNDATED_ALERT_SORT_KEY = datetime.min.replace(tzinfo=UTC)


@dataclass
class AquaHomeRuntimeData:
    """Runtime objects stored on the config entry."""

    client: AquaHomeClient
    auth: AuthManager
    coordinators: dict[str, AquaHomeCoordinator]
    activity_coordinators: dict[str, AquaHomeActivityCoordinator]
    settings_coordinators: dict[str, AquaHomeSettingsCoordinator]
    statistics_coordinators: dict[str, AquaHomeStatisticsCoordinator]
    analytics_engines: dict[str, AquaHomeAnalyticsEngine]
    schedulers: dict[str, AquaHomeRegenScheduler]
    live_managers: dict[str, AquaHomeLiveManager]


def resolve_device_online(device: Device) -> bool:
    """Return the host-neutral device-online signal.

    Primary source is the device-root ``is_online`` flag, which both API hosts
    populate. When it is absent (a legacy host that omits it) the raw
    ``_internal_is_online`` property is used as a fallback. When neither is
    present the device is assumed online, so a host that reports neither never
    spuriously kills every entity.

    This deliberately differs from :attr:`~.api.Device.online`, which prefers the
    property first; device-root precedence is deliberate — it is the signal
    both hosts are known to populate.
    """
    if device.is_online is not None:
        return device.is_online
    prop = device.properties.get("_internal_is_online")
    if prop is not None:
        return prop.value is True
    return True


class AquaHomeCoordinator(DataUpdateCoordinator[Device]):
    """Poll one iQua device, serving cached data across transient failures."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: AquaHomeConfigEntry,
        client: AquaHomeClient,
        device: Device,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the coordinator to a device and its account client.

        ``monotonic`` is injected so the serve-stale time-to-live can be driven
        deterministically in tests.
        """
        self.device_id = device.id
        self.device_slug = device_slug(device)
        self.client = client
        self._monotonic = monotonic
        self._last_good: float | None = None
        self._last_attempt: float | None = None
        self._serving_stale = False
        self._updating_from_push = False
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {self.device_slug}",
            update_interval=UPDATE_INTERVAL,
        )

    @property
    def device_online(self) -> bool:
        """Return whether the polled device reports itself online.

        ``False`` before the first successful refresh (no data yet), otherwise
        the :func:`resolve_device_online` signal from the latest device view.
        """
        data: Device | None = self.data
        if data is None:
            return False
        return resolve_device_online(data)

    @property
    def serving_stale(self) -> bool:
        """Return whether the latest refresh re-served cached data.

        A stale re-serve repeats the previous payload verbatim, so consumers
        that count *observations* — the capability debounce in
        :func:`~.dynamic.async_setup_dynamic_entities` — must ignore it.
        """
        return self._serving_stale

    @property
    def updating_from_push(self) -> bool:
        """Return whether the update being dispatched is a live-stream push.

        ``True`` only while :meth:`async_apply_live_update` is notifying
        listeners. A push refreshes a handful of raw properties and carries the
        rest of the device view — the enriched block above all — verbatim, so
        synchronous listeners that react to *polled* facts (the automation
        tier's enforcement, the capability debounce, the activity triggers)
        must treat it as no new observation and return immediately.
        """
        return self._updating_from_push

    @callback
    def async_apply_live_update(self, device: Device) -> None:
        """Publish a live-streamed device view without starving the REST poll.

        :meth:`~homeassistant.helpers.update_coordinator.DataUpdateCoordinator.async_set_updated_data`
        reschedules the next poll a full interval away, so a stream that pushes
        at least once per interval — one gallon every ten minutes is enough —
        would postpone polling indefinitely, freezing the enriched block that
        only genuine polls refresh (regeneration state, salt level, feature
        gating, ``is_online``). Whenever the last poll *attempt* is older than
        the update interval, a refresh is requested alongside the publish, so
        streaming can only ever make data fresher, never staler.

        The floor is keyed to attempts, not successes, and the slot is claimed
        synchronously before the refresh task is created. Both matter:
        ``async_set_updated_data`` cancels the request-refresh debouncer, so
        each push would otherwise execute its refresh immediately — and while
        the poll is *failing* (``_last_good`` frozen), a busy stream would turn
        every coalesced frame into a REST request against an already-unhealthy
        cloud instead of one request per interval.
        """
        self._updating_from_push = True
        try:
            self.async_set_updated_data(device)
        finally:
            self._updating_from_push = False
        now = self._monotonic()
        attempt = self._last_attempt
        if attempt is not None and now - attempt >= UPDATE_INTERVAL.total_seconds():
            self._last_attempt = now
            self.hass.async_create_task(self.async_request_refresh())

    async def _async_update_data(self) -> Device:
        """Fetch the full device view, serving cached data on transient errors.

        Authentication failures raise :class:`ConfigEntryAuthFailed` (bypassing
        the stale-serving grace period, straight to reauth). Rate limits and
        transient 5xx errors take the serve-stale path; a 4xx is a real,
        non-transient contract failure raised as :class:`UpdateFailed`.
        """
        self._last_attempt = self._monotonic()
        try:
            device = await self.client.async_get_device(self.device_id)
        except AuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (RateLimitError, AquaHomeConnectionError) as err:
            return self._serve_stale(err)
        except ApiError as err:
            if (
                err.status is not None
                and err.status >= HTTPStatus.INTERNAL_SERVER_ERROR
            ):
                return self._serve_stale(err)
            raise UpdateFailed(str(err)) from err
        self._last_good = self._monotonic()
        if self._serving_stale:
            _LOGGER.info(
                "AquaHome device %s recovered; serving fresh data again",
                self.device_slug,
            )
            self._serving_stale = False
        return device

    def _serve_stale(self, err: AquaHomeError) -> Device:
        """Return the last-good device across a transient failure, or fail honestly.

        Within :data:`~.const.MAX_STALE_SECONDS` of the last successful poll the
        cached device is returned so entities keep their last-known state and
        stay available; the first stale poll logs one warning and subsequent ones
        stay quiet (guarded by ``_serving_stale``). Past the grace period, or with
        no cached data yet, the failure is surfaced as :class:`UpdateFailed` so
        entities go honestly unavailable.
        """
        cached: Device | None = self.data
        if (
            cached is not None
            and self._last_good is not None
            and self._monotonic() - self._last_good < MAX_STALE_SECONDS
        ):
            if not self._serving_stale:
                _LOGGER.warning(
                    "AquaHome device %s poll failed (%s); serving cached data",
                    self.device_slug,
                    err,
                )
                self._serving_stale = True
            return cached
        raise UpdateFailed(str(err)) from err


def _sort_alerts_newest_first(alerts: tuple[Alert, ...]) -> tuple[Alert, ...]:
    """Return the alerts sorted newest-first, with undated alerts last.

    The sort is stable and ``reverse``-stable, so alerts that share a timestamp
    (or all lack one) keep their original API order; an alert whose timestamp
    could not be parsed sorts after every dated alert instead of raising.
    """
    return tuple(
        sorted(
            alerts,
            key=lambda alert: alert.timestamp or _UNDATED_ALERT_SORT_KEY,
            reverse=True,
        )
    )


@dataclass(frozen=True, slots=True)
class DeviceActivity:
    """Parsed activity feed for one device: alerts and regeneration history.

    ``alerts`` is sorted newest-first by timestamp (undated alerts last);
    ``regeneration_events`` preserves the API's own newest-first order.
    ``new_alerts`` holds the alerts observed for the first time on the most
    recent refresh, ordered oldest-to-newest, and is empty (``()``) on the first
    successful refresh so a fresh setup never replays the backlog.
    """

    alerts: tuple[Alert, ...]
    regeneration_events: tuple[RegenerationEvent, ...]
    new_alerts: tuple[Alert, ...]


class AquaHomeActivityCoordinator(DataUpdateCoordinator[DeviceActivity]):
    """Poll one device's slow-moving activity feed (alerts + regenerations).

    Runs on the gentle :data:`~.const.ACTIVITY_UPDATE_INTERVAL`; the fast
    telemetry coordinator asks for an early refresh when it sees a rising alert
    badge or a regeneration transition. Alerts new since the previous refresh are
    diffed against a watermark of seen ids, fired on the Home Assistant event bus
    as :data:`~.const.EVENT_AQUAHOME`, and exposed on :class:`DeviceActivity` for
    the event and activity-sensor platforms.

    The serve-stale behaviour mirrors :class:`AquaHomeCoordinator` in spirit but
    is implemented standalone: history records stay valid far longer than live
    telemetry, so the grace window is the wider
    :data:`~.const.ACTIVITY_MAX_STALE_SECONDS`.
    """

    def __init__(  # noqa: PLR0913 - deliberate dependency-injection signature
        self,
        hass: HomeAssistant,
        entry: AquaHomeConfigEntry,
        client: AquaHomeClient,
        *,
        device_id: str,
        device_slug: str,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the coordinator to one device's activity feed.

        ``monotonic`` is injected so the serve-stale time-to-live can be driven
        deterministically in tests.
        """
        self.device_id = device_id
        self.device_slug = device_slug
        self.client = client
        self._monotonic = monotonic
        self._last_good: float | None = None
        self._serving_stale = False
        #: Alert ids seen on the previous successful refresh. ``None`` until the
        #: first success, which establishes the watermark without firing events.
        self._seen_alert_ids: frozenset[str] | None = None
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {device_slug} activity",
            update_interval=ACTIVITY_UPDATE_INTERVAL,
        )

    async def _async_update_data(self) -> DeviceActivity:
        """Fetch one page of alerts and regenerations, firing events for new alerts.

        Authentication failures raise :class:`ConfigEntryAuthFailed` (straight to
        reauth). Rate limits and transient 5xx errors take the serve-stale path;
        a non-transient 4xx is raised as :class:`UpdateFailed`.
        """
        try:
            alerts_page = await self.client.async_get_alerts(
                self.device_id, page=1, per_page=ACTIVITY_PAGE_SIZE
            )
            events_page = await self.client.async_get_regeneration_events(
                self.device_id, page=1, per_page=ACTIVITY_PAGE_SIZE
            )
        except AuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (RateLimitError, AquaHomeConnectionError) as err:
            return self._serve_stale(err)
        except ApiError as err:
            if (
                err.status is not None
                and err.status >= HTTPStatus.INTERNAL_SERVER_ERROR
            ):
                return self._serve_stale(err)
            raise UpdateFailed(str(err)) from err

        alerts = _sort_alerts_newest_first(alerts_page.alerts)
        new_alerts = self._diff_new_alerts(alerts)
        self._last_good = self._monotonic()
        if self._serving_stale:
            _LOGGER.info(
                "AquaHome device %s activity recovered; serving fresh data again",
                self.device_slug,
            )
            self._serving_stale = False
        for alert in new_alerts:
            self._fire_alert_event(alert)
        return DeviceActivity(
            alerts=alerts,
            regeneration_events=events_page.events,
            new_alerts=new_alerts,
        )

    def _diff_new_alerts(self, alerts: tuple[Alert, ...]) -> tuple[Alert, ...]:
        """Return alerts unseen on any previous refresh, ordered oldest-to-newest.

        The first successful refresh only establishes the watermark (returns no
        new alerts). Afterwards any alert whose id has never been seen is new.
        Seen ids accumulate (union, not replacement) so even a glitched shrunk
        or empty page can never make old alerts look new again on the page
        after it; growth is bounded in practice by the account's alert rate
        (tens of ids per year) and resets on restart.
        """
        current_ids = frozenset(alert.id for alert in alerts if alert.id)
        seen = self._seen_alert_ids
        if seen is None:
            self._seen_alert_ids = current_ids
            return ()
        new = tuple(
            alert for alert in reversed(alerts) if alert.id and alert.id not in seen
        )
        self._seen_alert_ids = seen | current_ids
        return new

    def _fire_alert_event(self, alert: Alert) -> None:
        """Fire the ``aquahome_event`` bus event for one newly observed alert."""
        self.hass.bus.async_fire(
            EVENT_AQUAHOME,
            {
                "device_id": self.device_id,
                "device": self.device_slug,
                "type": "alert",
                "alert_id": alert.id,
                "alert_type": alert.type,
                "title": alert.title,
                "message": alert.message,
                "level": alert.level,
                "timestamp": alert.timestamp.isoformat()
                if alert.timestamp is not None
                else None,
            },
        )

    def _serve_stale(self, err: AquaHomeError) -> DeviceActivity:
        """Return the last-good activity across a transient failure, or fail.

        Within :data:`~.const.ACTIVITY_MAX_STALE_SECONDS` of the last success the
        cached activity is returned so the entities keep their state; the first
        stale poll logs one warning and subsequent ones stay quiet. Past the
        grace period, or with no cached data yet, the failure is surfaced as
        :class:`UpdateFailed`.

        The cached view is returned with ``new_alerts`` cleared: a failed poll
        observed nothing new, and the base coordinator notifies listeners even
        for stale data, so replaying the previous cycle's ``new_alerts`` would
        re-trigger the alert event entity for alerts it already fired.
        """
        cached: DeviceActivity | None = self.data
        if (
            cached is not None
            and self._last_good is not None
            and self._monotonic() - self._last_good < ACTIVITY_MAX_STALE_SECONDS
        ):
            if not self._serving_stale:
                _LOGGER.warning(
                    "AquaHome device %s activity poll failed (%s); serving cached data",
                    self.device_slug,
                    err,
                )
                self._serving_stale = True
            return replace(cached, new_alerts=())
        raise UpdateFailed(str(err)) from err


class AquaHomeSettingsCoordinator(DataUpdateCoordinator[DeviceSettingsDocument]):
    """Poll one device's rule-driven settings document (DeviceSettingsBody).

    Runs on the gentle :data:`~.const.SETTINGS_UPDATE_INTERVAL`: the document only
    changes when the owner reconfigures the device, so a slow cadence suffices.
    :meth:`async_write_setting` PATCHes a single value and reconciles from the
    fresh document the server echoes back in the same round-trip, independent of
    the poll cadence — a write is authoritative and immediately heals staleness.

    The serve-stale behaviour mirrors :class:`AquaHomeCoordinator` in spirit but
    is implemented standalone: device configuration stays valid across long cloud
    blips, so the grace window is the widest of the three,
    :data:`~.const.SETTINGS_MAX_STALE_SECONDS`.
    """

    def __init__(  # noqa: PLR0913 - deliberate dependency-injection signature
        self,
        hass: HomeAssistant,
        entry: AquaHomeConfigEntry,
        client: AquaHomeClient,
        *,
        device_id: str,
        device_slug: str,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the coordinator to one device's settings document.

        ``monotonic`` is injected so the serve-stale time-to-live can be driven
        deterministically in tests.
        """
        self.device_id = device_id
        self.device_slug = device_slug
        self.client = client
        self._monotonic = monotonic
        self._last_good: float | None = None
        self._serving_stale = False
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {device_slug} settings",
            update_interval=SETTINGS_UPDATE_INTERVAL,
        )

    @property
    def serving_stale(self) -> bool:
        """Return whether the latest refresh re-served cached data.

        Mirrors :attr:`AquaHomeCoordinator.serving_stale` — the dynamic-entity
        debounce must not treat a stale re-serve as a new observation.
        """
        return self._serving_stale

    async def _async_update_data(self) -> DeviceSettingsDocument:
        """Fetch the settings document, serving cached data on transient errors.

        Authentication failures raise :class:`ConfigEntryAuthFailed` (straight to
        reauth). Rate limits and transient 5xx errors take the serve-stale path;
        a non-transient 4xx is raised as :class:`UpdateFailed`.
        """
        try:
            document = await self.client.async_get_settings(self.device_id)
        except AuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (RateLimitError, AquaHomeConnectionError) as err:
            return self._serve_stale(err)
        except ApiError as err:
            if (
                err.status is not None
                and err.status >= HTTPStatus.INTERNAL_SERVER_ERROR
            ):
                return self._serve_stale(err)
            raise UpdateFailed(str(err)) from err
        self._last_good = self._monotonic()
        if self._serving_stale:
            _LOGGER.info(
                "AquaHome device %s settings recovered; serving fresh data again",
                self.device_slug,
            )
            self._serving_stale = False
        return document

    async def async_write_setting(
        self, name: str, value: bool | int | float | str
    ) -> None:
        """PATCH a single setting and reconcile from the returned document.

        Sends ``{name: value}`` and pushes the refreshed
        :class:`~.api.DeviceSettingsDocument` the server echoes back to listeners
        via :meth:`async_set_updated_data`. That document also counts as fresh
        data — ``_last_good`` is advanced and any serve-stale state cleared — so a
        successful write heals a coordinator that had gone stale, without waiting
        for the next scheduled poll. API exceptions propagate raw; the entity
        layer maps them onto user-facing :class:`HomeAssistantError` translations.
        """
        document = await self.client.async_update_settings(
            self.device_id, {name: value}
        )
        self._last_good = self._monotonic()
        self._serving_stale = False
        self.async_set_updated_data(document)

    def _serve_stale(self, err: AquaHomeError) -> DeviceSettingsDocument:
        """Return the last-good document across a transient failure, or fail.

        Within :data:`~.const.SETTINGS_MAX_STALE_SECONDS` of the last success the
        cached document is returned so the settings entities keep their state; the
        first stale poll logs one warning and subsequent ones stay quiet. Past the
        grace period, or with no cached data yet, the failure is surfaced as
        :class:`UpdateFailed`.
        """
        cached: DeviceSettingsDocument | None = self.data
        if (
            cached is not None
            and self._last_good is not None
            and self._monotonic() - self._last_good < SETTINGS_MAX_STALE_SECONDS
        ):
            if not self._serving_stale:
                _LOGGER.warning(
                    "AquaHome device %s settings poll failed (%s); serving cached data",
                    self.device_slug,
                    err,
                )
                self._serving_stale = True
            return cached
        raise UpdateFailed(str(err)) from err
