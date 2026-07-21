"""The AquaHome integration for iQua-cloud water treatment devices.

Sets up one :class:`~.coordinator.AquaHomeCoordinator` per cloud device behind a
shared authenticated :class:`~.api.AquaHomeClient`, stores them on
``entry.runtime_data`` (:class:`~.coordinator.AquaHomeRuntimeData`), and forwards
the sensor / binary-sensor platforms. Rotated tokens are persisted back onto the
config entry so the entry survives long Home Assistant downtime without forcing
interactive reauthentication.
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
from .const import CONF_REFRESH_TOKEN, CONFIG_VERSION, PLATFORMS
from .coordinator import (
    AquaHomeConfigEntry,
    AquaHomeCoordinator,
    AquaHomeRuntimeData,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


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
    for device in devices:
        coordinator = AquaHomeCoordinator(hass, entry, client, device)
        await coordinator.async_config_entry_first_refresh()
        coordinators[device.id] = coordinator

    entry.runtime_data = AquaHomeRuntimeData(
        client=client, auth=auth, coordinators=coordinators
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


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
    """Unload a config entry and its forwarded platforms."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


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
