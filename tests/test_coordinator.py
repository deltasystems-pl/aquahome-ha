"""Tests for :mod:`custom_components.aquahome.coordinator`.

The coordinator is exercised end-to-end wherever possible: a config entry is set
up through the shared ``setup_integration`` helper against the real captured
fixtures faked over ``aioresponses``, wall-clock/loop time is advanced with the
``freezer`` fixture plus ``async_fire_time_changed`` to drive the fixed poll
cadence, and the resulting entity *states* are asserted. That proves the whole
data path — HTTP response, tolerant parsing, coordinator refresh, entity
availability — not just an internal return value.

The serve-stale time-to-live is arithmetic over an injected monotonic clock, so
those two boundary cases construct the coordinator directly with a fake
monotonic and call :meth:`AquaHomeCoordinator._async_update_data` — the sanctioned
exception to the end-to-end rule. The ``freezer`` fixture patches ``time`` module
lookups but NOT the ``time.monotonic`` reference captured as the coordinator's
default argument at import time, so the integration-created coordinator reads the
real monotonic clock; the TTL therefore cannot be driven by the freezer and the
direct-construction path is used for the exact TTL boundary instead.
"""

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING, Any

import aiohttp
import pytest
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.aquahome.api import AquaHomeClient, AuthManager, Device
from custom_components.aquahome.api.const import API_BASE_URL
from custom_components.aquahome.const import DOMAIN, MAX_STALE_SECONDS, UPDATE_INTERVAL
from custom_components.aquahome.coordinator import (
    AquaHomeCoordinator,
    resolve_device_online,
)
from tests.conftest import (
    TEST_DEVICE_ID,
    device_url,
    devices_url,
    load_fixture,
    make_access_token,
    setup_integration,
)

if TYPE_CHECKING:
    from aioresponses import aioresponses
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

#: Entity ids the device nickname ``Dom`` yields under ``has_entity_name``.
SALT_LEVEL = "sensor.dom_salt_level"
TOTAL_RECHARGES = "sensor.dom_total_recharges"
ONLINE = "binary_sensor.dom_online"
SALT_LEVEL_ALERT = "binary_sensor.dom_salt_level_alert"

#: Coordinator logger used to scope the caplog assertions.
COORDINATOR_LOGGER = "custom_components.aquahome.coordinator"
#: Substrings identifying the two log lines the serve-stale path emits.
STALE_WARNING = "serving cached data"
RECOVERY_INFO = "recovered"


class _FakeMonotonic:
    """Advanceable monotonic clock for driving the serve-stale TTL directly."""

    def __init__(self, now: float = 1_000.0) -> None:
        """Start the clock at a fixed value."""
        self._now = now

    def __call__(self) -> float:
        """Return the current monotonic reading."""
        return self._now

    def advance(self, seconds: float) -> None:
        """Move the clock forward."""
        self._now += seconds


def _detail(
    *, salt_level_percent: float | None = None, total_recharges: int | None = None
) -> dict[str, Any]:
    """Return a deep-copied device-detail payload with selected fields overridden.

    Mutating a copy keeps the on-disk fixture pristine while letting a second
    poll return changed values so a state transition can be asserted.
    """
    payload = copy.deepcopy(load_fixture("device-detail.json"))
    water_treatment = payload["enriched_data"]["water_treatment"]
    if salt_level_percent is not None:
        water_treatment["salt_level"]["salt_level_percent"] = salt_level_percent
        water_treatment["salt_level_percent"] = salt_level_percent
    if total_recharges is not None:
        water_treatment["total_recharges"] = total_recharges
    return payload


def _standalone_client(hass: HomeAssistant) -> AquaHomeClient:
    """Build a client whose auth already holds a fresh (non-refreshing) token.

    Used by the direct-construction TTL tests, which drive
    :meth:`AquaHomeCoordinator._async_update_data` without going through
    integration setup.
    """
    session = async_get_clientsession(hass)
    auth = AuthManager(session, base_url=API_BASE_URL)
    auth.set_tokens(make_access_token(), "refresh-token-1")
    return AquaHomeClient(session, auth, base_url=API_BASE_URL, language="en")


async def _fire_next_poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Advance one poll interval and let the scheduled refresh run to completion."""
    freezer.tick(UPDATE_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def _state(hass: HomeAssistant, entity_id: str) -> str:
    """Return the state string of ``entity_id``, asserting the entity exists."""
    state = hass.states.get(entity_id)
    assert state is not None
    return state.state


def _stale_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return the coordinator serve-stale WARNING records captured so far."""
    return [
        record
        for record in caplog.records
        if record.name == COORDINATOR_LOGGER
        and record.levelno == logging.WARNING
        and STALE_WARNING in record.getMessage()
    ]


def _recovery_infos(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return the coordinator recovery INFO records captured so far."""
    return [
        record
        for record in caplog.records
        if record.name == COORDINATOR_LOGGER
        and record.levelno == logging.INFO
        and RECOVERY_INFO in record.getMessage()
    ]


# ---------------------------------------------------------------------------
# Successful poll cycle
# ---------------------------------------------------------------------------


async def test_successful_poll_cycle_updates_entity_states(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A poll cycle refreshes entity state from the newest device payload."""
    mock_api.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock_api.get(device_url(), payload=load_fixture("device-detail.json"))
    mock_api.get(
        device_url(),
        payload=_detail(salt_level_percent=20.0, total_recharges=150),
        repeat=True,
    )

    assert await setup_integration(hass, mock_config_entry)

    salt = hass.states.get(SALT_LEVEL)
    assert salt is not None
    assert salt.state == "37.5"
    assert salt.attributes["unit_of_measurement"] == "%"
    assert salt.attributes["state_class"] == "measurement"

    recharges = hass.states.get(TOTAL_RECHARGES)
    assert recharges is not None
    assert recharges.state == "149"
    assert recharges.attributes["state_class"] == "total_increasing"

    online = hass.states.get(ONLINE)
    assert online is not None
    assert online.state == STATE_ON
    assert online.attributes["device_class"] == "connectivity"

    await _fire_next_poll(hass, freezer)

    assert _state(hass, SALT_LEVEL) == "20.0"
    assert _state(hass, TOTAL_RECHARGES) == "150"


# ---------------------------------------------------------------------------
# Serve-stale across transient failures
# ---------------------------------------------------------------------------


async def test_serve_stale_on_rate_limit_keeps_state_and_warns_once(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Consecutive 429 polls keep last-good state, stay available, warn once."""
    caplog.set_level(logging.INFO, logger=COORDINATOR_LOGGER)
    mock_api.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock_api.get(device_url(), payload=load_fixture("device-detail.json"))
    mock_api.get(
        device_url(),
        status=429,
        payload={"code": "ThrottleLimitExceeded", "detail": "slow down"},
        repeat=True,
    )

    assert await setup_integration(hass, mock_config_entry)
    assert _state(hass, SALT_LEVEL) == "37.5"

    # First stale poll hits the network 429 and arms the client backoff; the
    # second is refused client-side by that backoff. Both take the serve-stale
    # path, so state is retained and the entity stays available throughout.
    await _fire_next_poll(hass, freezer)
    await _fire_next_poll(hass, freezer)

    salt = hass.states.get(SALT_LEVEL)
    assert salt is not None
    assert salt.state == "37.5"
    assert salt.state != STATE_UNAVAILABLE

    coordinator = mock_config_entry.runtime_data.coordinators[TEST_DEVICE_ID]
    assert coordinator.last_update_success is True

    assert len(_stale_warnings(caplog)) == 1


async def test_serve_stale_recovers_and_updates_state(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A recovery poll after a transient failure logs INFO and refreshes state.

    The transient failure is a connection error rather than a 429: a 429 arms the
    client's real-monotonic backoff window (60 s), which the freezer cannot wind
    forward, so the recovery poll would be refused before reaching the network.
    Both errors travel the identical serve-stale path in the coordinator.
    """
    caplog.set_level(logging.INFO, logger=COORDINATOR_LOGGER)
    mock_api.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock_api.get(device_url(), payload=load_fixture("device-detail.json"))
    mock_api.get(device_url(), exception=aiohttp.ClientError("network down"))
    mock_api.get(
        device_url(),
        payload=_detail(salt_level_percent=12.5),
        repeat=True,
    )

    assert await setup_integration(hass, mock_config_entry)
    assert _state(hass, SALT_LEVEL) == "37.5"

    # Transient failure: cached state retained, one warning, no recovery yet.
    await _fire_next_poll(hass, freezer)
    assert _state(hass, SALT_LEVEL) == "37.5"
    assert len(_stale_warnings(caplog)) == 1
    assert _recovery_infos(caplog) == []

    # Recovery poll: fresh data flows to entities and the recovery is logged once.
    await _fire_next_poll(hass, freezer)
    assert _state(hass, SALT_LEVEL) == "12.5"
    assert len(_recovery_infos(caplog)) == 1
    # Still exactly one warning: the recovery must not re-warn.
    assert len(_stale_warnings(caplog)) == 1


# ---------------------------------------------------------------------------
# Serve-stale time-to-live (direct construction with injected monotonic)
# ---------------------------------------------------------------------------


async def test_serve_stale_within_ttl_returns_cached_data(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """Just under MAX_STALE_SECONDS a failing poll returns the cached device."""
    mock_config_entry.add_to_hass(hass)
    clock = _FakeMonotonic()
    device = Device.from_dict(load_fixture("device-detail.json"))
    coordinator = AquaHomeCoordinator(
        hass, mock_config_entry, _standalone_client(hass), device, monotonic=clock
    )

    mock_api.get(device_url(), payload=load_fixture("device-detail.json"))
    await coordinator.async_refresh()
    assert coordinator.last_update_success is True
    assert coordinator.data is not None

    clock.advance(MAX_STALE_SECONDS - 1.0)
    mock_api.get(
        device_url(),
        status=429,
        payload={"code": "ThrottleLimitExceeded", "detail": "slow down"},
        repeat=True,
    )

    result = await coordinator._async_update_data()
    assert result is coordinator.data
    assert result.id == TEST_DEVICE_ID


async def test_stale_ttl_exceeded_raises_update_failed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """Past MAX_STALE_SECONDS a failing poll raises UpdateFailed, dropping stale."""
    mock_config_entry.add_to_hass(hass)
    clock = _FakeMonotonic()
    device = Device.from_dict(load_fixture("device-detail.json"))
    coordinator = AquaHomeCoordinator(
        hass, mock_config_entry, _standalone_client(hass), device, monotonic=clock
    )

    mock_api.get(device_url(), payload=load_fixture("device-detail.json"))
    await coordinator.async_refresh()
    assert coordinator.data is not None

    clock.advance(MAX_STALE_SECONDS)
    mock_api.get(
        device_url(),
        status=429,
        payload={"code": "ThrottleLimitExceeded", "detail": "slow down"},
        repeat=True,
    )

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_stale_ttl_exceeded_makes_entities_unavailable(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """When a transient failure outlives the TTL, entities go unavailable.

    The live coordinator reads the real monotonic clock, so the last-good marker
    is back-dated past the TTL to place the next poll deterministically beyond the
    grace period without a 30-minute real wait.
    """
    mock_api.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock_api.get(device_url(), payload=load_fixture("device-detail.json"))
    mock_api.get(
        device_url(),
        exception=aiohttp.ClientError("network down"),
        repeat=True,
    )

    assert await setup_integration(hass, mock_config_entry)
    assert _state(hass, SALT_LEVEL) == "37.5"

    coordinator = mock_config_entry.runtime_data.coordinators[TEST_DEVICE_ID]
    coordinator._last_good = coordinator._monotonic() - (MAX_STALE_SECONDS + 60.0)

    await _fire_next_poll(hass, freezer)

    assert coordinator.last_update_success is False
    assert _state(hass, SALT_LEVEL) == STATE_UNAVAILABLE


# ---------------------------------------------------------------------------
# 4xx contract failures (never served stale)
# ---------------------------------------------------------------------------


async def test_4xx_raises_update_failed_even_with_fresh_data(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """A 4xx never serves stale, even with fresh last-good data in hand."""
    mock_config_entry.add_to_hass(hass)
    clock = _FakeMonotonic()
    device = Device.from_dict(load_fixture("device-detail.json"))
    coordinator = AquaHomeCoordinator(
        hass, mock_config_entry, _standalone_client(hass), device, monotonic=clock
    )

    mock_api.get(device_url(), payload=load_fixture("device-detail.json"))
    await coordinator.async_refresh()
    assert coordinator.data is not None

    # Fresh last-good (clock barely moved), yet a 404 must fail immediately.
    mock_api.get(
        device_url(),
        status=404,
        payload={"code": "NotFound", "detail": "no such device"},
        repeat=True,
    )

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert coordinator._serving_stale is False


async def test_5xx_serves_stale_within_ttl(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """A transient 5xx (unlike a 4xx) takes the serve-stale path within the TTL."""
    mock_config_entry.add_to_hass(hass)
    clock = _FakeMonotonic()
    device = Device.from_dict(load_fixture("device-detail.json"))
    coordinator = AquaHomeCoordinator(
        hass, mock_config_entry, _standalone_client(hass), device, monotonic=clock
    )

    mock_api.get(device_url(), payload=load_fixture("device-detail.json"))
    await coordinator.async_refresh()
    assert coordinator.data is not None

    mock_api.get(
        device_url(),
        status=503,
        payload={"code": "ServiceUnavailable", "detail": "try later"},
        repeat=True,
    )

    result = await coordinator._async_update_data()
    assert result is coordinator.data
    assert coordinator._serving_stale is True


async def test_4xx_makes_entities_unavailable(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """End-to-end: a 4xx on a scheduled poll drives entities unavailable."""
    mock_api.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock_api.get(device_url(), payload=load_fixture("device-detail.json"))
    mock_api.get(
        device_url(),
        status=404,
        payload={"code": "NotFound", "detail": "no such device"},
        repeat=True,
    )

    assert await setup_integration(hass, mock_config_entry)
    assert _state(hass, SALT_LEVEL) == "37.5"

    await _fire_next_poll(hass, freezer)

    coordinator = mock_config_entry.runtime_data.coordinators[TEST_DEVICE_ID]
    assert coordinator.last_update_success is False
    assert _state(hass, SALT_LEVEL) == STATE_UNAVAILABLE


# ---------------------------------------------------------------------------
# Authentication failure mid-run
# ---------------------------------------------------------------------------


async def test_auth_error_midrun_starts_reauth_and_bypasses_stale(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A 401 whose refresh also 401s bypasses serve-stale and starts reauth."""
    mock_api.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock_api.get(device_url(), payload=load_fixture("device-detail.json"))
    mock_api.get(
        device_url(),
        status=401,
        payload={"code": "Unauthorized", "detail": "token expired"},
        repeat=True,
    )
    mock_api.post(
        f"{API_BASE_URL}/auth/refresh",
        status=401,
        payload={"code": "AuthCannotRefreshToken", "detail": "refresh rejected"},
        repeat=True,
    )

    assert await setup_integration(hass, mock_config_entry)
    assert _state(hass, SALT_LEVEL) == "37.5"

    await _fire_next_poll(hass, freezer)

    coordinator = mock_config_entry.runtime_data.coordinators[TEST_DEVICE_ID]
    assert coordinator.last_update_success is False
    # Serve-stale is bypassed: the fresh last-good state is dropped, not retained.
    assert _state(hass, SALT_LEVEL) == STATE_UNAVAILABLE

    flows = hass.config_entries.flow.async_progress()
    reauth_flows = [
        flow
        for flow in flows
        if flow["handler"] == DOMAIN and flow["context"].get("source") == SOURCE_REAUTH
    ]
    assert len(reauth_flows) == 1


# ---------------------------------------------------------------------------
# Device-online split
# ---------------------------------------------------------------------------


async def test_offline_device_splits_telemetry_from_alerts(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """An offline device hides telemetry but keeps the online/alert binaries.

    Telemetry sensors gate on ``device_online`` and go unavailable; the
    connectivity binary reports ``off`` and the cloud-side alert binaries — which
    stay meaningful during an outage — remain available.
    """
    offline_detail = copy.deepcopy(load_fixture("device-detail.json"))
    offline_detail["is_online"] = False
    mock_api.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock_api.get(device_url(), payload=offline_detail, repeat=True)

    assert await setup_integration(hass, mock_config_entry)

    salt = hass.states.get(SALT_LEVEL)
    assert salt is not None
    assert salt.state == STATE_UNAVAILABLE

    online = hass.states.get(ONLINE)
    assert online is not None
    assert online.state == STATE_OFF

    alert = hass.states.get(SALT_LEVEL_ALERT)
    assert alert is not None
    assert alert.state == STATE_OFF


# ---------------------------------------------------------------------------
# resolve_device_online unit coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("is_online", "internal_prop", "expected"),
    [
        # Device-root is_online wins over the legacy internal property.
        (False, True, False),
        (True, False, True),
        # Root absent: the internal property decides.
        (None, True, True),
        (None, False, False),
        # Neither present: assume online, never spuriously kill entities.
        (None, None, True),
    ],
)
def test_resolve_device_online_host_variants(
    is_online: bool | None, internal_prop: bool | None, expected: bool
) -> None:
    """resolve_device_online applies device-root-first precedence per host."""
    payload: dict[str, Any] = {"id": TEST_DEVICE_ID}
    if is_online is not None:
        payload["is_online"] = is_online
    if internal_prop is not None:
        payload["properties"] = {
            "_internal_is_online": {
                "name": "_internal_is_online",
                "value": internal_prop,
            }
        }
    device = Device.from_dict(payload)
    assert resolve_device_online(device) is expected


def test_resolve_device_online_real_fixture_is_true() -> None:
    """The captured online device resolves to True."""
    device = Device.from_dict(load_fixture("device-detail.json"))
    assert resolve_device_online(device) is True


async def test_device_online_false_before_first_refresh(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The coordinator reports offline until it has device data."""
    mock_config_entry.add_to_hass(hass)
    device = Device.from_dict(load_fixture("device-detail.json"))
    coordinator = AquaHomeCoordinator(
        hass, mock_config_entry, _standalone_client(hass), device
    )
    # No poll has run, so ``data`` is unset and the online signal is False even
    # though the (unfetched) device would report itself online.
    assert coordinator.device_online is False


# ---------------------------------------------------------------------------
# Live-stream applies (direct construction with injected monotonic)
# ---------------------------------------------------------------------------


async def test_live_apply_marks_the_push_for_synchronous_listeners(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """Listeners can tell a live push from a genuine poll while dispatching.

    The flag is up only for the synchronous span of the push dispatch, so the
    consumers that must ignore pushes — deferral enforcement, the capability
    debounce, the activity triggers — read it in their ``@callback`` before
    scheduling any work.
    """
    mock_config_entry.add_to_hass(hass)
    device = Device.from_dict(load_fixture("device-detail.json"))
    coordinator = AquaHomeCoordinator(
        hass, mock_config_entry, _standalone_client(hass), device
    )
    seen: list[bool] = []
    coordinator.async_add_listener(lambda: seen.append(coordinator.updating_from_push))

    coordinator.async_set_updated_data(device)
    coordinator.async_apply_live_update(device)
    coordinator.async_set_updated_data(device)

    assert seen == [False, True, False]
    assert coordinator.updating_from_push is False


async def test_live_apply_keeps_the_poll_floor(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """Sustained pushes cannot postpone the REST poll past its interval.

    ``async_set_updated_data`` reschedules the next poll a full interval away,
    so a stream pushing at least once per interval would starve the poll — and
    with it the enriched block only polls refresh. Once the last genuine poll
    is older than the interval, a push must bring a refresh with it.
    """
    mock_config_entry.add_to_hass(hass)
    clock = _FakeMonotonic()
    device = Device.from_dict(load_fixture("device-detail.json"))
    coordinator = AquaHomeCoordinator(
        hass, mock_config_entry, _standalone_client(hass), device, monotonic=clock
    )
    coordinator.async_add_listener(lambda: None)

    mock_api.get(device_url(), payload=load_fixture("device-detail.json"))
    await coordinator.async_refresh()
    assert coordinator.last_update_success is True

    # Fresh poll: a push is pure gravy and must not trigger anything.
    coordinator.async_apply_live_update(device)
    await hass.async_block_till_done()
    assert len(mock_api.requests) == 1  # the single GET route, called once

    # The last genuine poll ages past the interval while pushes keep landing:
    # the next push requests a refresh alongside the publish.
    clock.advance(UPDATE_INTERVAL.total_seconds() + 1.0)
    mock_api.get(device_url(), payload=load_fixture("device-detail.json"))
    coordinator.async_apply_live_update(device)
    await hass.async_block_till_done()

    calls = next(iter(mock_api.requests.values()))
    assert len(calls) == 2
