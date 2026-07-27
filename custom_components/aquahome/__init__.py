"""The AquaHome integration for iQua-cloud water treatment devices.

Sets up, per cloud device, a fast :class:`~.coordinator.AquaHomeCoordinator`
(telemetry), a gentle :class:`~.coordinator.AquaHomeActivityCoordinator`
(alert + regeneration history), a gentle
:class:`~.coordinator.AquaHomeSettingsCoordinator` (the rule-driven settings
document), and a slow :class:`~.statistics.AquaHomeStatisticsCoordinator`
(external water-usage statistics backfilled from the cloud datapoint history,
first run as a background task) behind a shared authenticated
:class:`~.api.AquaHomeClient`, stores
them on ``entry.runtime_data`` (:class:`~.coordinator.AquaHomeRuntimeData`), and
forwards the sensor / binary-sensor / event platforms. A rising alert badge or a
regeneration transition seen by the fast coordinator triggers an early activity
refresh, and both the activity feed and the settings document are refreshed
tolerantly at setup so neither ever blocks core telemetry. Rotated tokens are
persisted back onto the config entry so the entry survives long Home Assistant
downtime without forcing interactive reauthentication.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.const import (
    CONF_ACCESS_TOKEN,
    CONF_EMAIL,
    CONF_HOST,
    CONF_PASSWORD,
)
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    ApiError,
    AquaHomeClient,
    AquaHomeConnectionError,
    AuthError,
    AuthManager,
    Device,
    RateLimitError,
)
from .const import CONF_REFRESH_TOKEN, CONFIG_VERSION, DOMAIN, PLATFORMS
from .coordinator import (
    AquaHomeActivityCoordinator,
    AquaHomeConfigEntry,
    AquaHomeCoordinator,
    AquaHomeRuntimeData,
    AquaHomeSettingsCoordinator,
)
from .statistics import (
    AquaHomeStatisticsCoordinator,
    async_clear_device_statistics,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

#: ``recharge_ui.state`` / ``regeneration_status`` value meaning a live recharge.
_REGENERATING = "regenerating"


async def async_setup_entry(hass: HomeAssistant, entry: AquaHomeConfigEntry) -> bool:
    """Set up AquaHome from a config entry.

    Builds the auth manager and client from the stored host and tokens, lists the
    account's devices (healing a stale refresh token with one re-login), then
    creates and first-refreshes one coordinator per device sequentially — gently,
    since the cloud is throttled — before forwarding the platforms.
    """
    session = async_get_clientsession(hass)
    host: str = entry.data[CONF_HOST]

    def _persist_tokens(access_token: str, refresh_token: str) -> None:
        """Persist a rotated token pair back onto the config entry."""
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_ACCESS_TOKEN: access_token,
                CONF_REFRESH_TOKEN: refresh_token,
            },
        )

    auth = AuthManager(session, base_url=host, on_token_update=_persist_tokens)
    auth.set_tokens(entry.data[CONF_ACCESS_TOKEN], entry.data[CONF_REFRESH_TOKEN])
    client = AquaHomeClient(session, auth, base_url=host, language=hass.config.language)

    devices = await _async_list_devices(entry, client, auth)
    if not devices:
        _LOGGER.warning(
            "AquaHome account %s reports no devices; setting up with no entities",
            entry.title,
        )

    coordinators: dict[str, AquaHomeCoordinator] = {}
    activity_coordinators: dict[str, AquaHomeActivityCoordinator] = {}
    settings_coordinators: dict[str, AquaHomeSettingsCoordinator] = {}
    statistics_coordinators: dict[str, AquaHomeStatisticsCoordinator] = {}
    for device in devices:
        coordinator = AquaHomeCoordinator(hass, entry, client, device)
        await coordinator.async_config_entry_first_refresh()
        coordinators[device.id] = coordinator

        activity = AquaHomeActivityCoordinator(
            hass,
            entry,
            client,
            device_id=device.id,
            device_slug=coordinator.device_slug,
        )
        await _async_first_activity_refresh(activity)
        activity_coordinators[device.id] = activity

        settings = AquaHomeSettingsCoordinator(
            hass,
            entry,
            client,
            device_id=device.id,
            device_slug=coordinator.device_slug,
        )
        await _async_first_settings_refresh(settings)
        settings_coordinators[device.id] = settings

        statistics_coordinators[device.id] = AquaHomeStatisticsCoordinator(
            hass,
            entry,
            client,
            device_id=device.id,
            device_slug=coordinator.device_slug,
            device_name=_device_display_name(device),
            tz_id=_device_tz_id(device),
        )

        _async_wire_activity_triggers(hass, entry, coordinator, activity)

    entry.runtime_data = AquaHomeRuntimeData(
        client=client,
        auth=auth,
        coordinators=coordinators,
        activity_coordinators=activity_coordinators,
        settings_coordinators=settings_coordinators,
        statistics_coordinators=statistics_coordinators,
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # The history backfill talks to a throttled cloud and the recorder; it must
    # never delay entity setup, so its first run happens as a background task
    # (which also arms the 12-hour cadence). Failures log via the coordinator.
    for statistics in statistics_coordinators.values():
        entry.async_create_background_task(
            hass,
            statistics.async_refresh(),
            name=f"{DOMAIN} statistics backfill {statistics.device_slug}",
        )
    return True


async def _async_first_activity_refresh(
    activity: AquaHomeActivityCoordinator,
) -> None:
    """Refresh the activity feed once at setup, tolerating a failure.

    The alert / regeneration feed must never block core telemetry, so — unlike
    the fast coordinator's ``async_config_entry_first_refresh`` — a failed first
    activity refresh does not abort setup: it is logged and swallowed, and the
    activity entities show unavailable until the next 30-minute refresh succeeds.
    ``async_refresh`` never raises, so setup always continues past this point.
    """
    await activity.async_refresh()
    if not activity.last_update_success:
        _LOGGER.warning(
            "AquaHome activity feed for %s did not load during setup; its entities "
            "will be unavailable until the next refresh",
            activity.device_slug,
        )


async def _async_first_settings_refresh(
    settings: AquaHomeSettingsCoordinator,
) -> None:
    """Refresh the settings document once at setup, tolerating a failure.

    The rule-driven settings document must never block core telemetry, so — like
    the activity feed and unlike the fast coordinator's
    ``async_config_entry_first_refresh`` — a failed first settings refresh does
    not abort setup: it is logged and swallowed, and the settings entities stay
    unavailable until the next 6-hour refresh (or a write) succeeds.
    ``async_refresh`` never raises, so setup always continues past this point.
    """
    await settings.async_refresh()
    if not settings.last_update_success:
        _LOGGER.warning(
            "AquaHome settings for %s did not load during setup; its entities "
            "will be unavailable until the next refresh",
            settings.device_slug,
        )


def _async_wire_activity_triggers(
    hass: HomeAssistant,
    entry: AquaHomeConfigEntry,
    fast: AquaHomeCoordinator,
    activity: AquaHomeActivityCoordinator,
) -> None:
    """Trigger an early activity refresh on a fast-coordinator activity change.

    The activity feed polls gently (30 min). To surface a fresh alert or a
    regeneration transition promptly, this watches every fast-coordinator update
    for a rise in the enriched alert badge count or a flip of the
    "regeneration active" flag, and requests an out-of-band activity refresh when
    it sees one. The remembered values are seeded from the fast coordinator's
    first payload and updated on every callback, so the very first real change is
    detected while a fresh setup never fires spuriously; a value that starts (or
    becomes) unknown is never compared. The listener is sync, so the async
    refresh is scheduled as a task, and is removed automatically on unload.
    """
    previous_badge = _alert_badge_count(fast.data)
    previous_regen = _regen_active(fast.data)

    @callback
    def _handle_fast_update() -> None:
        """Request an activity refresh when the badge rises or regen flips."""
        nonlocal previous_badge, previous_regen
        device = fast.data
        badge = _alert_badge_count(device)
        regen_active = _regen_active(device)
        badge_increased = (
            badge is not None and previous_badge is not None and badge > previous_badge
        )
        regen_flipped = (
            regen_active is not None
            and previous_regen is not None
            and regen_active != previous_regen
        )
        previous_badge = badge
        previous_regen = regen_active
        if badge_increased or regen_flipped:
            hass.async_create_task(activity.async_request_refresh())

    entry.async_on_unload(fast.async_add_listener(_handle_fast_update))


def _device_display_name(device: Device) -> str:
    """Return the human-facing device name for statistics metadata.

    Mirrors the fallback chain of :func:`~.entity.build_device_info` so the
    external statistic is listed under the same name as the device card.
    """
    enriched = device.enriched_data
    model = enriched.model if enriched is not None else None
    return device.nickname or model or "AquaHome"


def _device_tz_id(device: Device) -> str | None:
    """Return the device's IANA timezone property value, if it carries one."""
    prop = device.properties.get("tz_id")
    if prop is not None and isinstance(prop.value, str) and prop.value:
        return prop.value
    return None


def _alert_badge_count(device: Device) -> int | None:
    """Return the enriched alert badge count, or ``None`` when unavailable."""
    enriched = device.enriched_data
    if enriched is None or enriched.water_treatment_status is None:
        return None
    return enriched.water_treatment_status.alert_badge_count


def _regen_active(device: Device) -> bool | None:
    """Return whether a regeneration is currently running, ``None`` if unknown.

    ``True`` when the ``recharge_ui`` tile reads ``regenerating`` or the enriched
    ``regeneration`` block reports ``regeneration_status == "regenerating"``;
    ``None`` only when neither source is present, so there is nothing to compare
    a transition against.
    """
    enriched = device.enriched_data
    if enriched is None:
        return None
    recharge_ui = enriched.recharge_ui
    regeneration = enriched.regeneration
    if recharge_ui is None and regeneration is None:
        return None
    if recharge_ui is not None and recharge_ui.state == _REGENERATING:
        return True
    return (
        regeneration is not None and regeneration.regeneration_status == _REGENERATING
    )


async def _async_list_devices(
    entry: AquaHomeConfigEntry, client: AquaHomeClient, auth: AuthManager
) -> list[Device]:
    """Return the account's devices, healing a stale token with one re-login.

    A stored refresh token can rotate away during long Home Assistant downtime;
    a single fresh login with the persisted credentials recovers it before we
    give up. A second authentication failure demands interactive reauth; any
    rate-limit or connection error means the cloud is momentarily unusable, so
    the entry is retried later rather than set up half-broken.
    """
    try:
        return await client.async_get_devices()
    except AuthError:
        _LOGGER.debug("Device list rejected authentication; attempting one re-login")
    except (RateLimitError, AquaHomeConnectionError, ApiError) as err:
        raise ConfigEntryNotReady(str(err)) from err

    try:
        await auth.async_login(entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
        return await client.async_get_devices()
    except AuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except (RateLimitError, AquaHomeConnectionError, ApiError) as err:
        raise ConfigEntryNotReady(str(err)) from err


async def async_unload_entry(hass: HomeAssistant, entry: AquaHomeConfigEntry) -> bool:
    """Unload a config entry and its forwarded platforms.

    The fast-coordinator trigger listeners (and the dynamic-platform capability
    listeners) were registered through ``entry.async_on_unload`` and are removed
    automatically. The activity and settings coordinators are shut down explicitly
    so their scheduled refresh and request debouncer stop cleanly once the
    platforms (and their entity listeners) are gone.
    """
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        for activity in entry.runtime_data.activity_coordinators.values():
            await activity.async_shutdown()
        for settings in entry.runtime_data.settings_coordinators.values():
            await settings.async_shutdown()
        for statistics in entry.runtime_data.statistics_coordinators.values():
            await statistics.async_shutdown()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: AquaHomeConfigEntry) -> None:
    """Clean up when a config entry is permanently removed.

    External statistics are not tied to entities, so Home Assistant does not
    delete them with the entry — without this hook every ``aquahome:*`` series
    would survive an uninstall as orphaned recorder data.
    """
    await async_clear_device_statistics(hass, entry)


async def async_migrate_entry(hass: HomeAssistant, entry: AquaHomeConfigEntry) -> bool:
    """Migrate a config entry to the current version.

    An entry written by a newer integration version cannot be handled safely, so
    a downgrade is refused. Version 1 is current and needs no data migration.
    A future version bump adds its version-gated step here — running
    :func:`~.migration.async_migrate_unique_ids` and updating the entry version —
    below this downgrade guard.
    """
    if entry.version > CONFIG_VERSION:
        _LOGGER.error(
            "Cannot downgrade AquaHome config entry from version %s to %s",
            entry.version,
            CONFIG_VERSION,
        )
        return False
    return True
