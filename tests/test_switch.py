"""Tests for the AquaHome switch platform.

Two unrelated switch families share :mod:`custom_components.aquahome.switch`,
each on its own coordinator, and both are exercised end-to-end here against the
``aioresponses`` HTTP fakes with only the switch platform forwarded so every
assertion runs through the real coordinator-first-refresh path.

Setting switches (family 1) are dynamic entities on the *settings* coordinator:
no boolean setting exists on the dev device, so they are synthesised with the
``make_switch_setting`` builder. Their writes PATCH ``{"settings": {name: value}}``
and reconcile from the document the server echoes back — asserted here to happen
without any follow-up settings GET.

The leak-detector scan switch (family 2) is a momentary control on the *fast*
telemetry coordinator, present once the ``leak_detector`` feature or a
``leak_detectors`` block appears. Its ``is_on`` follows the polled
``is_scanning`` flag with an optimistic override after a start/stop command that
decays after :data:`~const.OPTIMISTIC_STATE_TTL_SECONDS` or as soon as the cloud
reports a real scanning state. A rejected command surfaces as a
``HomeAssistantError`` and must never flip the switch optimistically.
"""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from aioresponses.core import RequestCall
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    EntityCategory,
    Platform,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.aquahome.const import DOMAIN, OPTIMISTIC_STATE_TTL_SECONDS
from tests.conftest import (
    TEST_DEVICE_ID,
    add_activity_routes,
    add_device_routes,
    add_settings_routes,
    command_url,
    device_url,
    devices_url,
    load_fixture,
    make_switch_setting,
    patch_settings_route,
    setup_integration,
    with_extra_setting,
    with_leak_detectors,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aioresponses import aioresponses
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

#: The switch platform domain (``homeassistant.components.switch`` does not
#: re-export ``DOMAIN`` for typing, so derive it from the platform enum).
SWITCH_DOMAIN = Platform.SWITCH
#: Slug derived from the fixture serial ``7384243-20203-1120``.
SLUG = "7384243_20203_1120"
#: Unique-id suffixes of the two switches this file drives.
SETTING_UNIQUE_ID = f"{SLUG}_setting_night_mode"
LEAK_SCAN_UNIQUE_ID = f"{SLUG}_leak_detector_scan"
#: Fixed instant the timer-driven tests freeze to (2026-07-21T12:00:00Z).
FROZEN_INSTANT = "2026-07-21T12:00:00+00:00"


@pytest.fixture(autouse=True)
def _only_switch_platform() -> Iterator[None]:
    """Forward only the switch platform for the duration of a test."""
    with patch("custom_components.aquahome.PLATFORMS", [Platform.SWITCH]):
        yield


# ---------------------------------------------------------------------------
# Route + payload helpers (never mutate the on-disk fixtures in place)
# ---------------------------------------------------------------------------


def _settings_doc(*, value: bool) -> dict[str, Any]:
    """Return the real settings document with one boolean ``night_mode`` added."""
    return with_extra_setting(
        load_fixture("settings.json"), make_switch_setting(current_value=value)
    )


def _leak_detail(*, scanning: bool = False, add_feature: bool = True) -> dict[str, Any]:
    """Return the dev device-detail payload carrying a leak-detector block."""
    return with_leak_detectors(
        load_fixture("device-detail.json"),
        scanning=scanning,
        add_feature=add_feature,
    )


def _feature_only_detail() -> dict[str, Any]:
    """Return a device detail advertising ``leak_detector`` but with no block.

    Exercises the "feature OR block" gate on its feature-only leg, which no
    conftest builder produces (``with_leak_detectors`` always adds the block).
    """
    detail = copy.deepcopy(load_fixture("device-detail.json"))
    treatment = detail["enriched_data"]["water_treatment"]
    features = treatment.setdefault("features", [])
    if "leak_detector" not in features:
        features.append("leak_detector")
    return detail


def _register_leak_routes(
    mock: aioresponses,
    *,
    first: dict[str, Any],
    rest: dict[str, Any] | None = None,
) -> None:
    """Register the read routes a switch-only setup and its refreshes hit.

    ``first`` answers the setup device poll; ``rest`` (defaulting to ``first``)
    answers every later poll, so a distinct payload models the cloud reporting a
    changed scanning state on the next refresh.
    """
    mock.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock.get(device_url(), payload=first)
    mock.get(device_url(), payload=rest or first, repeat=True)
    add_activity_routes(mock)
    add_settings_routes(mock)


def _requests(mock: aioresponses, method: str, suffix: str) -> list[RequestCall]:
    """Return every recorded request whose method and URL-path suffix match."""
    return [
        call
        for (call_method, url), calls in mock.requests.items()
        if call_method == method and url.path.endswith(suffix)
        for call in calls
    ]


def _command_bodies(mock: aioresponses) -> list[dict[str, Any]]:
    """Return the JSON bodies of every recorded command PUT, in order."""
    return [call.kwargs["json"] for call in _requests(mock, "PUT", "/command")]


def _entity_id(registry: er.EntityRegistry, unique_id: str) -> str | None:
    """Resolve a switch entity id from its unique-id suffix."""
    return registry.async_get_entity_id(SWITCH_DOMAIN, DOMAIN, unique_id)


def _state(hass: HomeAssistant, entity_id: str) -> str:
    """Return the current state string of ``entity_id`` (asserting it exists)."""
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} has no state"
    return state.state


async def _call(hass: HomeAssistant, service: str, entity_id: str) -> None:
    """Invoke a switch turn_on/turn_off service call and settle the loop."""
    await hass.services.async_call(
        SWITCH_DOMAIN, service, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Setting switch — creation and current value
# ---------------------------------------------------------------------------


async def test_setting_switch_created_config_and_on(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """A boolean setting becomes a CONFIG switch whose ``is_on`` is its value."""
    add_device_routes(mock_api, settings=_settings_doc(value=True))

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry, SETTING_UNIQUE_ID)
    assert entity_id is not None
    entry = entity_registry.async_get(entity_id)
    assert entry is not None
    assert entry.entity_category == EntityCategory.CONFIG
    assert _state(hass, entity_id) == STATE_ON


# ---------------------------------------------------------------------------
# Setting switch — write path (PATCH body + reconcile without an extra GET)
# ---------------------------------------------------------------------------


async def test_setting_switch_turn_off_patches_false_and_reconciles(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """turn_off PATCHes ``night_mode: false`` and reconciles from the response.

    The switch starts ``on``; the PATCH echoes back a document with the value
    flipped to ``false`` and the entity must reflect it without issuing any
    follow-up settings GET.
    """
    add_device_routes(mock_api, settings=_settings_doc(value=True))
    patch_settings_route(mock_api, payload=_settings_doc(value=False))

    assert await setup_integration(hass, mock_config_entry)
    entity_id = _entity_id(entity_registry, SETTING_UNIQUE_ID)
    assert entity_id is not None
    assert _state(hass, entity_id) == STATE_ON
    # The setup refresh performed exactly one settings GET.
    assert len(_requests(mock_api, "GET", "/settings")) == 1

    await _call(hass, SERVICE_TURN_OFF, entity_id)

    patches = _requests(mock_api, "PATCH", "/settings")
    assert len(patches) == 1
    assert patches[0].kwargs["json"] == {"settings": {"night_mode": False}}
    # State reconciled from the PATCH response document …
    assert _state(hass, entity_id) == STATE_OFF
    # … with no additional settings GET (still just the one from setup).
    assert len(_requests(mock_api, "GET", "/settings")) == 1


async def test_setting_switch_turn_on_patches_true_and_reconciles(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """turn_on PATCHes ``night_mode: true`` and reconciles the state to ``on``."""
    add_device_routes(mock_api, settings=_settings_doc(value=False))
    patch_settings_route(mock_api, payload=_settings_doc(value=True))

    assert await setup_integration(hass, mock_config_entry)
    entity_id = _entity_id(entity_registry, SETTING_UNIQUE_ID)
    assert entity_id is not None
    assert _state(hass, entity_id) == STATE_OFF

    await _call(hass, SERVICE_TURN_ON, entity_id)

    patches = _requests(mock_api, "PATCH", "/settings")
    assert len(patches) == 1
    assert patches[0].kwargs["json"] == {"settings": {"night_mode": True}}
    assert _state(hass, entity_id) == STATE_ON
    assert len(_requests(mock_api, "GET", "/settings")) == 1


# ---------------------------------------------------------------------------
# Leak-detector scan switch — existence gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "variant",
    ["feature_and_block", "block_only", "feature_only"],
)
async def test_leak_scan_switch_created_when_capability_present(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    variant: str,
) -> None:
    """The scan switch exists whenever the feature OR the block is present.

    All three legs of the "require either" gate create the CONFIG switch: the
    feature and block together, the block alone (feature absent), and the
    feature alone (no block yet).
    """
    if variant == "feature_and_block":
        detail = _leak_detail(add_feature=True)
    elif variant == "block_only":
        detail = _leak_detail(add_feature=False)
    else:
        detail = _feature_only_detail()
    add_device_routes(mock_api, device_detail=detail)

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry, LEAK_SCAN_UNIQUE_ID)
    assert entity_id is not None
    entry = entity_registry.async_get(entity_id)
    assert entry is not None
    assert entry.entity_category == EntityCategory.CONFIG


async def test_leak_scan_switch_absent_on_plain_dev_fixture(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """The regeneration-only dev device advertises no leak capability, so no switch."""
    add_device_routes(mock_api)

    assert await setup_integration(hass, mock_config_entry)

    assert _entity_id(entity_registry, LEAK_SCAN_UNIQUE_ID) is None


@pytest.mark.parametrize(
    ("scanning", "expected"),
    [(True, STATE_ON), (False, STATE_OFF)],
)
async def test_leak_scan_is_on_follows_is_scanning(  # noqa: PLR0913
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    scanning: bool,
    expected: str,
) -> None:
    """With no optimistic override the switch mirrors the polled ``is_scanning``."""
    add_device_routes(mock_api, device_detail=_leak_detail(scanning=scanning))

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry, LEAK_SCAN_UNIQUE_ID)
    assert entity_id is not None
    assert _state(hass, entity_id) == expected


# ---------------------------------------------------------------------------
# Leak-detector scan switch — command PUT bodies
# ---------------------------------------------------------------------------


async def test_leak_scan_commands_send_start_and_end_scan_puts(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """turn_on issues ``start_scan`` and turn_off ``end_scan`` command PUTs."""
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(mock_api, device_detail=_leak_detail(scanning=False))
    mock_api.put(command_url(), payload={"status": "success"}, repeat=True)

    assert await setup_integration(hass, mock_config_entry)
    entity_id = _entity_id(entity_registry, LEAK_SCAN_UNIQUE_ID)
    assert entity_id is not None

    await _call(hass, SERVICE_TURN_ON, entity_id)
    await _call(hass, SERVICE_TURN_OFF, entity_id)

    assert _command_bodies(mock_api) == [
        {"function": "leak_detector", "action": "start_scan"},
        {"function": "leak_detector", "action": "end_scan"},
    ]

    # Let the final optimistic override decay so no timer lingers past the test.
    freezer.tick(timedelta(seconds=OPTIMISTIC_STATE_TTL_SECONDS + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Leak-detector scan switch — optimistic override lifecycle
# ---------------------------------------------------------------------------


async def test_leak_scan_optimistic_shows_on_then_decays_to_polled(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """turn_on shows ``on`` at once, then falls back to the polled ``off``.

    The cloud still reports ``is_scanning=False``; after the
    :data:`~const.OPTIMISTIC_STATE_TTL_SECONDS` window elapses the override
    decays and the switch reads the polled state again.
    """
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(mock_api, device_detail=_leak_detail(scanning=False))
    mock_api.put(command_url(), payload={"status": "success"}, repeat=True)

    assert await setup_integration(hass, mock_config_entry)
    entity_id = _entity_id(entity_registry, LEAK_SCAN_UNIQUE_ID)
    assert entity_id is not None
    assert _state(hass, entity_id) == STATE_OFF

    await _call(hass, SERVICE_TURN_ON, entity_id)
    # Optimistic: shown on immediately, before any poll confirms it.
    assert _state(hass, entity_id) == STATE_ON

    freezer.tick(timedelta(seconds=OPTIMISTIC_STATE_TTL_SECONDS + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    # The fast coordinator has not repolled (10 min cadence); the override simply
    # decayed back to the polled ``is_scanning=False``.
    assert _state(hass, entity_id) == STATE_OFF


async def test_leak_scan_coordinator_update_clears_optimistic_immediately(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """A poll carrying a non-None ``is_scanning`` drops the override at once.

    turn_off pins an optimistic ``off``; the very next coordinator refresh
    reports ``is_scanning=True`` and — without the TTL elapsing — the switch
    immediately abandons the override and shows the real polled ``on``.
    """
    _register_leak_routes(
        mock_api,
        first=_leak_detail(scanning=False),
        rest=_leak_detail(scanning=True),
    )
    mock_api.put(command_url(), payload={"status": "success"}, repeat=True)

    assert await setup_integration(hass, mock_config_entry)
    entity_id = _entity_id(entity_registry, LEAK_SCAN_UNIQUE_ID)
    assert entity_id is not None
    assert _state(hass, entity_id) == STATE_OFF

    await _call(hass, SERVICE_TURN_OFF, entity_id)
    assert _state(hass, entity_id) == STATE_OFF

    # Force a single fast-coordinator refresh (no clock advance, so the TTL timer
    # cannot be what clears the override) reporting a real scanning state.
    coordinator = mock_config_entry.runtime_data.coordinators[TEST_DEVICE_ID]
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert _state(hass, entity_id) == STATE_ON


# ---------------------------------------------------------------------------
# Leak-detector scan switch — rejected command
# ---------------------------------------------------------------------------


async def test_leak_scan_command_failure_raises_and_does_not_flip(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """A 422 rejection raises HomeAssistantError and leaves the state unchanged.

    The command is issued before any optimistic write, so a rejected start_scan
    surfaces the error and the switch stays at the polled ``off`` — it never
    shows an on state the device did not accept.
    """
    add_device_routes(mock_api, device_detail=_leak_detail(scanning=False))
    mock_api.put(
        command_url(),
        status=422,
        payload={"code": "CommandRejected", "detail": "scan unavailable"},
        repeat=True,
    )

    assert await setup_integration(hass, mock_config_entry)
    entity_id = _entity_id(entity_registry, LEAK_SCAN_UNIQUE_ID)
    assert entity_id is not None
    assert _state(hass, entity_id) == STATE_OFF

    with pytest.raises(HomeAssistantError):
        await _call(hass, SERVICE_TURN_ON, entity_id)

    # No optimistic flip: the failed command never set the override.
    assert _state(hass, entity_id) == STATE_OFF
