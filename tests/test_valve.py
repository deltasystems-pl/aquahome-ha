"""Tests for the water-shutoff-valve (WSOV) platform.

The valve is a single open/close actuator with no position feedback: its
``is_closed`` state is read straight from the enriched ``water_shutoff_valve``
block's ``status`` enum, and only the transient optimistic-motion hint
(``is_opening`` / ``is_closing``) is ever fabricated. No device in the developer
cohort carries a valve (every dev device advertises only ``["regeneration"]``),
so the platform is exercised solely by the synthetic ``with_wsov`` builder from
``tests/conftest.py`` — deep-copying the captured detail payload and attaching a
valve block, never mutating the fixture in place.

Only the valve platform is forwarded, so every assertion runs the real
coordinator-first-refresh path. Coverage: creation from the ``wsov`` feature and
from a bare block; the ``status`` -> open/closed/unknown mapping; the literal
``{function, action}`` PUT body of open and close; the confirmation-dialog guard
that refuses a disabled action before any I/O while leaving the sibling action
pressable; the optimistic-motion TTL and its early clear from a confirming poll;
availability (offline / not-installed / block-vanished); and the diagnostic extra
attributes.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.components.valve.const import DOMAIN as VALVE_DOMAIN
from homeassistant.components.valve.const import ValveState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_CLOSE_VALVE,
    SERVICE_OPEN_VALVE,
    STATE_CLOSED,
    STATE_OPEN,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from yarl import URL

from custom_components.aquahome.const import (
    DOMAIN,
    OPTIMISTIC_STATE_TTL_SECONDS,
    UPDATE_INTERVAL,
)
from tests.conftest import (
    TEST_DEVICE_ID,
    add_activity_routes,
    add_device_routes,
    add_settings_routes,
    command_url,
    device_url,
    devices_url,
    load_fixture,
    setup_integration,
    with_wsov,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aioresponses import aioresponses
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

#: Slug derived from the fixture serial ``4213377-30105-2242``.
SLUG = "4213377_30105_2242"

#: Unique-id suffix of the one valve a device carries.
VALVE_KEY = "water_shutoff_valve"

#: The request-record key of the one ``PUT /devices/{id}/command`` URL.
COMMAND_KEY = ("PUT", URL(command_url()))


@pytest.fixture(autouse=True)
def _only_valve_platform() -> Iterator[None]:
    """Forward only the valve platform for the duration of a test."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("custom_components.aquahome.PLATFORMS", [Platform.VALVE])
        yield


# ---------------------------------------------------------------------------
# Local payload / route helpers (never mutate the loaded fixture in place)
# ---------------------------------------------------------------------------


def _base_detail() -> dict[str, Any]:
    """Return a deep copy of the captured device-detail payload to mutate."""
    return copy.deepcopy(load_fixture("device-detail.json"))


def _valve_detail(**kwargs: Any) -> dict[str, Any]:
    """Return a device-detail payload carrying a synthetic valve block."""
    return with_wsov(_base_detail(), **kwargs)


def _register_sequence(
    mock: aioresponses,
    first: dict[str, Any],
    rest: dict[str, Any],
) -> None:
    """Register a detail payload consumed once, then a repeating replacement.

    The first (non-repeat) device-detail GET is consumed by the coordinator's
    setup refresh; every later poll reads ``rest``. The activity and settings
    routes are served from the real fixtures so the tolerant setup refreshes
    succeed cleanly instead of logging warnings.
    """
    mock.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock.get(device_url(), payload=first, repeat=False)
    mock.get(device_url(), payload=rest, repeat=True)
    add_activity_routes(mock)
    add_settings_routes(mock)


def _entity_id(entity_registry: er.EntityRegistry) -> str | None:
    """Resolve the valve's entity id from its unique-id suffix, or ``None``."""
    return entity_registry.async_get_entity_id(
        VALVE_DOMAIN, DOMAIN, f"{SLUG}_{VALVE_KEY}"
    )


def _state_of(hass: HomeAssistant, entity_id: str) -> str | None:
    """Return the current state string of ``entity_id``, or ``None``."""
    state = hass.states.get(entity_id)
    return state.state if state is not None else None


def _attributes(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Return the current attribute dict of ``entity_id`` (empty if stateless)."""
    state = hass.states.get(entity_id)
    return dict(state.attributes) if state is not None else {}


async def _call(hass: HomeAssistant, service: str, entity_id: str) -> None:
    """Invoke a valve service, letting handler errors propagate."""
    await hass.services.async_call(
        VALVE_DOMAIN, service, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )


def _command_bodies(mock: aioresponses) -> list[dict[str, Any]]:
    """Return every JSON body PUT to the ``/command`` endpoint, in order."""
    return [call.kwargs["json"] for call in mock.requests.get(COMMAND_KEY, [])]


async def _flush_optimistic(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Let the optimistic-motion TTL timer fire so nothing lingers past a test.

    Advancing just past :data:`OPTIMISTIC_STATE_TTL_SECONDS` (well short of the
    10-minute poll cadence, so no coordinator refresh is triggered) fires only the
    ``async_call_later`` motion timer, which cancels itself on expiry.
    """
    freezer.tick(OPTIMISTIC_STATE_TTL_SECONDS + 1)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Creation / discovery
# ---------------------------------------------------------------------------


async def test_created_when_wsov_feature_present(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """A device advertising the ``wsov`` feature grows exactly one valve entity."""
    add_device_routes(mock_api, device_detail=_valve_detail(status="open"))

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry)
    assert entity_id is not None
    assert _state_of(hass, entity_id) == STATE_OPEN


async def test_created_when_only_block_present(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """A bare ``water_shutoff_valve`` block (feature list omitted) still creates it.

    The discovery rule requires *either* the ``wsov`` feature *or* the block, so a
    host that carries the valve without advertising the feature is still covered.
    """
    add_device_routes(
        mock_api, device_detail=_valve_detail(status="close", add_feature=False)
    )

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry)
    assert entity_id is not None
    assert _state_of(hass, entity_id) == STATE_CLOSED


async def test_not_created_without_feature_or_block(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """The regeneration-only dev fixture (no feature, no block) creates no valve."""
    add_device_routes(mock_api)

    assert await setup_integration(hass, mock_config_entry)

    assert _entity_id(entity_registry) is None


# ---------------------------------------------------------------------------
# is_closed mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("open", STATE_OPEN),
        ("close", STATE_CLOSED),
        ("manual", STATE_UNKNOWN),
        ("error", STATE_UNKNOWN),
        ("unknown", STATE_UNKNOWN),
    ],
)
async def test_is_closed_status_mapping(  # noqa: PLR0913
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    status: str,
    expected: str,
) -> None:
    """``close`` -> closed, ``open`` -> open; every other status -> unknown.

    ``manual`` / ``error`` / ``unknown`` are not a definite position, so the valve
    reports the genuine unknown state rather than fabricating open or closed.
    """
    add_device_routes(mock_api, device_detail=_valve_detail(status=status))

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry)
    assert entity_id is not None
    assert _state_of(hass, entity_id) == expected


# ---------------------------------------------------------------------------
# Command payloads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("service", "start_status", "action"),
    [
        (SERVICE_OPEN_VALVE, "close", "open"),
        (SERVICE_CLOSE_VALVE, "open", "close"),
    ],
)
async def test_command_sends_exact_put_body(  # noqa: PLR0913
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    service: str,
    start_status: str,
    action: str,
) -> None:
    """Opening / closing PUTs the literal ``{water_shutoff_valve, open|close}`` body."""
    add_device_routes(mock_api, device_detail=_valve_detail(status=start_status))
    mock_api.put(command_url(), payload={"status": "ok"}, repeat=True)

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry)
    assert entity_id is not None
    await _call(hass, service, entity_id)

    assert _command_bodies(mock_api) == [
        {"function": "water_shutoff_valve", "action": action}
    ]

    await _flush_optimistic(hass, freezer)


# ---------------------------------------------------------------------------
# Confirmation-dialog guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("blocked_service", "allowed_service", "buttons", "allowed_action"),
    [
        (SERVICE_OPEN_VALVE, SERVICE_CLOSE_VALVE, {"open": False}, "close"),
        (SERVICE_CLOSE_VALVE, SERVICE_OPEN_VALVE, {"close": False}, "open"),
    ],
)
async def test_dialog_guard_blocks_action_before_any_io(  # noqa: PLR0913
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    blocked_service: str,
    allowed_service: str,
    buttons: dict[str, bool],
    allowed_action: str,
) -> None:
    """A dialog button explicitly ``False`` refuses that action with no HTTP call.

    The guard raises ``valve_action_blocked`` before any I/O, so no command is
    recorded; the sibling action, left unset, stays pressable and PUTs normally.
    """
    add_device_routes(mock_api, device_detail=_valve_detail(dialog_buttons=buttons))
    mock_api.put(command_url(), payload={"status": "ok"}, repeat=True)

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry)
    assert entity_id is not None

    with pytest.raises(HomeAssistantError) as caught:
        await _call(hass, blocked_service, entity_id)
    assert caught.value.translation_key == "valve_action_blocked"
    assert caught.value.translation_domain == DOMAIN
    # The guard fired before any I/O: nothing reached the command endpoint.
    assert COMMAND_KEY not in mock_api.requests

    # The unblocked sibling action is unaffected and PUTs its command.
    await _call(hass, allowed_service, entity_id)
    assert _command_bodies(mock_api) == [
        {"function": "water_shutoff_valve", "action": allowed_action}
    ]

    await _flush_optimistic(hass, freezer)


async def test_absent_dialog_allows_both_actions(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """With no confirmation dialog present, open and close both send their command."""
    add_device_routes(mock_api, device_detail=_valve_detail(status="open"))
    mock_api.put(command_url(), payload={"status": "ok"}, repeat=True)

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry)
    assert entity_id is not None

    await _call(hass, SERVICE_CLOSE_VALVE, entity_id)
    await _call(hass, SERVICE_OPEN_VALVE, entity_id)

    assert _command_bodies(mock_api) == [
        {"function": "water_shutoff_valve", "action": "close"},
        {"function": "water_shutoff_valve", "action": "open"},
    ]

    await _flush_optimistic(hass, freezer)


# ---------------------------------------------------------------------------
# Optimistic motion
# ---------------------------------------------------------------------------


async def test_optimistic_motion_clears_after_ttl(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """After a close the valve shows ``closing``, falling back to polled truth on TTL.

    The command fabricates only the motion hint; once
    :data:`OPTIMISTIC_STATE_TTL_SECONDS` elapses with no confirming poll the hint is
    dropped and the state reverts to the still-polled ``open`` status.
    """
    add_device_routes(mock_api, device_detail=_valve_detail(status="open"))
    mock_api.put(command_url(), payload={"status": "ok"}, repeat=True)

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry)
    assert entity_id is not None

    await _call(hass, SERVICE_CLOSE_VALVE, entity_id)
    await hass.async_block_till_done()
    assert _state_of(hass, entity_id) == ValveState.CLOSING

    # TTL elapses (< the 10-minute poll cadence, so no refresh intervenes).
    freezer.tick(OPTIMISTIC_STATE_TTL_SECONDS + 1)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    # Motion hint gone; state reverts to the unchanged polled status.
    assert _state_of(hass, entity_id) == STATE_OPEN


async def test_optimistic_motion_cleared_by_confirming_poll(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """A poll reporting the target ``close`` status clears the motion hint early.

    The refresh is driven directly (rather than by ticking the 10-minute cadence)
    so it lands *inside* the optimistic TTL window: this exercises the
    coordinator-update early-clear branch, which the TTL would otherwise fire
    first. The valve settles on ``closed`` — a state reachable only from the fresh
    poll, since the setup payload reported ``open``.
    """
    _register_sequence(
        mock_api,
        first=_valve_detail(status="open"),
        rest=_valve_detail(status="close"),
    )
    mock_api.put(command_url(), payload={"status": "ok"}, repeat=True)

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry)
    assert entity_id is not None

    await _call(hass, SERVICE_CLOSE_VALVE, entity_id)
    await hass.async_block_till_done()
    assert _state_of(hass, entity_id) == ValveState.CLOSING

    coordinator = mock_config_entry.runtime_data.coordinators[TEST_DEVICE_ID]
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert _state_of(hass, entity_id) == STATE_CLOSED


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


async def test_unavailable_when_device_offline(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """An offline device (root ``is_online`` ``False``) makes the valve unavailable."""
    detail = _valve_detail(status="open")
    detail["is_online"] = False
    add_device_routes(mock_api, device_detail=detail)

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry)
    assert entity_id is not None
    assert _state_of(hass, entity_id) == STATE_UNAVAILABLE


async def test_unavailable_when_not_installed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """A valve reporting ``is_installed`` ``False`` is present but unavailable.

    The device advertises the feature (so the entity exists) but the hardware is
    not installed, an unknowable state we surface as unavailable rather than a
    fabricated position.
    """
    add_device_routes(
        mock_api, device_detail=_valve_detail(status="open", is_installed=False)
    )

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry)
    assert entity_id is not None
    assert _state_of(hass, entity_id) == STATE_UNAVAILABLE


async def test_block_vanishing_on_later_poll_goes_unavailable_not_deleted(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """When the valve block disappears from a poll the entity stays, unavailable.

    Removed hardware goes unavailable through the entity's own availability rule;
    the dynamic adder never deletes a known entity, so the registry entry persists.
    """
    _register_sequence(
        mock_api,
        first=_valve_detail(status="open"),
        rest=_base_detail(),
    )

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry)
    assert entity_id is not None
    assert _state_of(hass, entity_id) == STATE_OPEN

    # A later poll no longer carries the valve block.
    freezer.tick(UPDATE_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    # Entity still registered, now unavailable rather than removed.
    assert _entity_id(entity_registry) == entity_id
    assert _state_of(hass, entity_id) == STATE_UNAVAILABLE


# ---------------------------------------------------------------------------
# Extra state attributes
# ---------------------------------------------------------------------------


async def test_extra_attributes_surface_when_present(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """``error_code`` and ``manual_override`` appear as attributes when reported."""
    add_device_routes(
        mock_api,
        device_detail=_valve_detail(
            status="open", error_code="E5", manual_override=True
        ),
    )

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry)
    assert entity_id is not None
    attributes = _attributes(hass, entity_id)
    assert attributes["error_code"] == "E5"
    assert attributes["manual_override"] is True


async def test_extra_attributes_omitted_when_absent(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """Absent diagnostic fields are omitted, not surfaced as ``None`` attributes."""
    add_device_routes(mock_api, device_detail=_valve_detail(status="open"))

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry)
    assert entity_id is not None
    attributes = _attributes(hass, entity_id)
    assert "error_code" not in attributes
    assert "manual_override" not in attributes
