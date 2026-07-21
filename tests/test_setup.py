"""End-to-end setup, unload, token-healing, and migration-entry tests.

These exercise :mod:`custom_components.aquahome.__init__` against the real
captured fixtures with every socket faked by ``aioresponses``: a happy setup
that forwards both platforms and registers the device, the stale-token healing
that re-logs-in exactly once, the transient-error paths that defer setup for
retry, runtime token persistence, the empty-account case, and the
config-entry-version migration guard. No integration internals are patched.
"""

from __future__ import annotations

import copy
import logging
import time
from typing import TYPE_CHECKING, Any

import aiohttp
import pytest
from aioresponses import aioresponses
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_ACCESS_TOKEN, STATE_UNAVAILABLE
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aquahome import async_migrate_entry
from custom_components.aquahome.api.const import API_BASE_URL
from custom_components.aquahome.const import CONF_REFRESH_TOKEN, CONFIG_VERSION, DOMAIN
from custom_components.aquahome.coordinator import (
    AquaHomeCoordinator,
    AquaHomeRuntimeData,
)
from tests.api.conftest import make_jwt
from tests.conftest import (
    TEST_DEVICE_ID,
    TEST_USER_ID,
    add_device_routes,
    device_url,
    devices_url,
    load_fixture,
    make_access_token,
    setup_integration,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant

#: Slug derived from the fixture serial ``7384243-20203-1120`` (see entity.py).
DEVICE_SLUG = "7384243_20203_1120"
DEVICE_SERIAL = "7384243-20203-1120"
LOGIN_URL = f"{API_BASE_URL}/auth/login"
REFRESH_URL = f"{API_BASE_URL}/auth/refresh"


def _login_body() -> dict[str, Any]:
    """Return a ``POST /auth/login`` success body carrying a fresh access JWT."""
    return {
        "access_token": make_access_token(),
        "refresh_token": "refresh-token-relogin",
        "user_id": TEST_USER_ID,
        "is_verified": True,
    }


def _count_requests(mock: aioresponses, method: str, path_suffix: str) -> int:
    """Count intercepted requests whose method and URL path suffix match."""
    return sum(
        len(calls)
        for (call_method, url), calls in mock.requests.items()
        if call_method == method and url.path.endswith(path_suffix)
    )


def _runtime(entry: MockConfigEntry) -> AquaHomeRuntimeData:
    """Return the entry's runtime data, narrowed for the type checker."""
    runtime = entry.runtime_data
    assert isinstance(runtime, AquaHomeRuntimeData)
    return runtime


# ---------------------------------------------------------------------------
# Happy path: load, device registry, unload
# ---------------------------------------------------------------------------


async def test_setup_entry_loaded_and_wires_runtime_data(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """A clean setup reaches LOADED and stores per-device coordinators."""
    add_device_routes(mock_api)

    assert await setup_integration(hass, mock_config_entry) is True
    assert mock_config_entry.state is ConfigEntryState.LOADED

    runtime = _runtime(mock_config_entry)
    assert set(runtime.coordinators) == {TEST_DEVICE_ID}
    coordinator = runtime.coordinators[TEST_DEVICE_ID]
    assert isinstance(coordinator, AquaHomeCoordinator)
    assert coordinator.device_id == TEST_DEVICE_ID
    # The single account client is shared with every coordinator.
    assert coordinator.client is runtime.client

    # Both platforms were forwarded and produced entities.
    assert hass.states.async_entity_ids("sensor")
    assert hass.states.async_entity_ids("binary_sensor")


async def test_setup_registers_device(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """The device-registry entry carries the identity from the fixture payload."""
    add_device_routes(mock_api)

    assert await setup_integration(hass, mock_config_entry) is True

    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device(identifiers={(DOMAIN, DEVICE_SLUG)})
    assert device is not None
    assert device.serial_number == DEVICE_SERIAL
    assert device.manufacturer == "iQua"
    assert device.name == "Dom"
    assert device.model == "AquaHome 20 Smart"
    assert device.sw_version == "r4.5 MPC01154"


async def test_unload_entry_removes_platforms(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """Unloading returns to NOT_LOADED and every platform entity goes unavailable."""
    add_device_routes(mock_api)
    assert await setup_integration(hass, mock_config_entry) is True

    # Setup produced live entities on both platforms.
    live = hass.states.get("sensor.dom_salt_level")
    assert live is not None
    assert live.state == "37.5"
    assert hass.states.get("binary_sensor.dom_online") is not None

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id) is True
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    entity_ids = [
        *hass.states.async_entity_ids("sensor"),
        *hass.states.async_entity_ids("binary_sensor"),
    ]
    assert entity_ids
    for entity_id in entity_ids:
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == STATE_UNAVAILABLE


# ---------------------------------------------------------------------------
# Stale-token healing on the device-list call
# ---------------------------------------------------------------------------


async def test_stale_token_healed_with_single_relogin(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """A 401 list + 401 refresh heals via exactly one login, then setup loads."""
    # The stored access token is rejected, forcing a refresh...
    mock_api.get(devices_url(), status=401)
    # ...but the stored refresh token has rotated away during downtime...
    mock_api.post(REFRESH_URL, status=401)
    # ...so one fresh login with the stored credentials recovers a token pair...
    mock_api.post(LOGIN_URL, payload=_login_body())
    # ...and the retried list plus the coordinator poll now succeed.
    mock_api.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock_api.get(device_url(), payload=load_fixture("device-detail.json"), repeat=True)

    assert await setup_integration(hass, mock_config_entry) is True
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert _count_requests(mock_api, "POST", "/auth/login") == 1
    assert set(_runtime(mock_config_entry).coordinators) == {TEST_DEVICE_ID}


async def test_relogin_failure_starts_reauth(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """When the healing re-login also 401s, setup errors and reauth begins."""
    mock_api.get(devices_url(), status=401)
    mock_api.post(REFRESH_URL, status=401)
    mock_api.post(LOGIN_URL, status=401)

    assert await setup_integration(hass, mock_config_entry) is False
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR

    flows = [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["handler"] == DOMAIN
    ]
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == SOURCE_REAUTH


# ---------------------------------------------------------------------------
# Transient device-list failures defer setup for retry
# ---------------------------------------------------------------------------


def _fail_rate_limited(mock: aioresponses) -> None:
    """Register a 429 throttle response for the device-list endpoint."""
    mock.get(devices_url(), status=429, payload={"code": "ThrottleLimitExceeded"})


def _fail_connection(mock: aioresponses) -> None:
    """Register a network failure for the device-list endpoint."""
    mock.get(devices_url(), exception=aiohttp.ClientConnectionError("no route"))


def _fail_server_error(mock: aioresponses) -> None:
    """Register a 500 server error for the device-list endpoint."""
    mock.get(devices_url(), status=500, payload={"code": "InternalServerError"})


@pytest.mark.parametrize(
    "register_failure",
    [_fail_rate_limited, _fail_connection, _fail_server_error],
    ids=["rate_limited", "connection_error", "server_error"],
)
async def test_setup_retry_on_transient_device_list_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    register_failure: Callable[[aioresponses], None],
) -> None:
    """A throttled, unreachable, or 5xx device list defers setup for retry."""
    register_failure(mock_api)

    assert await setup_integration(hass, mock_config_entry) is False
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


# ---------------------------------------------------------------------------
# Runtime token rotation is persisted onto the entry
# ---------------------------------------------------------------------------


async def test_runtime_token_refresh_is_persisted(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """A refresh during a poll writes the rotated token pair back to the entry."""
    detail = load_fixture("device-detail.json")
    new_access = make_jwt(time.time() + 3600.0)
    new_refresh = "refresh-token-rotated"
    original_access = mock_config_entry.data[CONF_ACCESS_TOKEN]

    mock_api.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock_api.get(device_url(), payload=detail)  # setup first refresh
    mock_api.get(device_url(), status=401)  # runtime poll: token rejected
    mock_api.post(
        REFRESH_URL,
        payload={"access_token": new_access, "refresh_token": new_refresh},
    )
    mock_api.get(device_url(), payload=detail, repeat=True)  # retried poll

    assert await setup_integration(hass, mock_config_entry) is True
    assert mock_config_entry.data[CONF_ACCESS_TOKEN] == original_access

    coordinator = _runtime(mock_config_entry).coordinators[TEST_DEVICE_ID]
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    assert mock_config_entry.data[CONF_ACCESS_TOKEN] == new_access
    assert mock_config_entry.data[CONF_REFRESH_TOKEN] == new_refresh


# ---------------------------------------------------------------------------
# Empty account still sets up cleanly
# ---------------------------------------------------------------------------


async def test_setup_with_zero_devices_loads_and_warns(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An account with no devices loads with empty coordinators and a warning."""
    empty = copy.deepcopy(load_fixture("devices-list.json"))
    empty["data"] = []
    empty["total"] = 0
    mock_api.get(devices_url(), payload=empty, repeat=True)

    with caplog.at_level(logging.WARNING):
        assert await setup_integration(hass, mock_config_entry) is True

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert _runtime(mock_config_entry).coordinators == {}
    assert not hass.states.async_all()
    assert "reports no devices" in caplog.text


# ---------------------------------------------------------------------------
# Config-entry version migration guard
# ---------------------------------------------------------------------------


async def test_migrate_entry_current_version_returns_true(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The current schema version needs no migration and reports success."""
    assert await async_migrate_entry(hass, mock_config_entry) is True


async def test_migrate_entry_from_future_version_errors(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """An entry written by a newer version cannot be downgraded; setup migrates fail."""
    future_entry = MockConfigEntry(
        domain=DOMAIN,
        title=mock_config_entry.title,
        unique_id=TEST_USER_ID,
        version=CONFIG_VERSION + 1,
        data=dict(mock_config_entry.data),
    )

    assert await setup_integration(hass, future_entry) is False
    assert future_entry.state is ConfigEntryState.MIGRATION_ERROR
