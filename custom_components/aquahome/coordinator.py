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
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    ApiError,
    AquaHomeClient,
    AquaHomeConnectionError,
    AquaHomeError,
    AuthError,
    AuthManager,
    Device,
    RateLimitError,
)
from .const import DOMAIN, MAX_STALE_SECONDS, UPDATE_INTERVAL
from .entity import device_slug

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

type AquaHomeConfigEntry = ConfigEntry[AquaHomeRuntimeData]


@dataclass
class AquaHomeRuntimeData:
    """Runtime objects stored on the config entry."""

    client: AquaHomeClient
    auth: AuthManager
    coordinators: dict[str, AquaHomeCoordinator]


def resolve_device_online(device: Device) -> bool:
    """Return the host-neutral device-online signal (gap-analysis §7 D6).

    Primary source is the device-root ``is_online`` flag, which both API hosts
    populate. When it is absent (a legacy host that omits it) the raw
    ``_internal_is_online`` property is used as a fallback. When neither is
    present the device is assumed online, so a host that reports neither never
    spuriously kills every entity.

    This deliberately differs from :attr:`~.api.Device.online`, which prefers the
    property first; the plan mandates device-root precedence.
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
        self._serving_stale = False
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

    async def _async_update_data(self) -> Device:
        """Fetch the full device view, serving cached data on transient errors.

        Authentication failures raise :class:`ConfigEntryAuthFailed` (bypassing
        the stale-serving grace period, straight to reauth). Rate limits and
        transient 5xx errors take the serve-stale path; a 4xx is a real,
        non-transient contract failure raised as :class:`UpdateFailed`.
        """
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
