"""Tests for the command-button platform.

Every button is a fire-and-forget device command routed through
:func:`~custom_components.aquahome.command.async_execute_command`, so the platform
owns two decisions only — *which* buttons exist for a device and *when* one is
available — plus the exact ``/command`` payload each press emits. These tests set
the platform up end-to-end against the ``aioresponses`` HTTP fakes and the
captured device fixture (only the button platform forwarded, so each assertion
runs the real coordinator-first-refresh path), then exercise:

* the exact button set created on the dev fixture (``features=["regeneration"]``):
  the three regeneration controls plus refresh-data / advance-valve /
  reset-error-code — silence-alarm and the shutoff-valve reset feature-gated out,
  and the three recharge-mode buttons excluded while the const gate is ``False``;
* the CONFIG / registry-disabled and DIAGNOSTIC entity-category metadata;
* the literal ``{"function": ..., "action": ...}`` PUT body of each command;
* the refresh-data two-step: the ``get_all_data`` command fires immediately, but
  the follow-up coordinator poll only after ``REFRESH_BUTTON_POLL_DELAY_SECONDS``;
* the error taxonomy (422 -> ``command_rejected``, 429 -> ``rate_limited``,
  a transport failure -> ``cannot_connect``);
* availability (``can_recharge`` ``False`` disables only regenerate-now; an
  offline device disables every button);
* the gated recharge-mode buttons — their documented UNVERIFIED payload guesses,
  their absence while the const is ``False``, and that flipping the table in makes
  them appear only when ``recharge_ui`` advertises the matching action.

Fixture payloads are always deep-copied before mutation; the JSON files are never
edited in place.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

import aiohttp
import pytest
from homeassistant.components.button.const import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button.const import SERVICE_PRESS
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    Platform,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import RegistryEntryDisabler
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from yarl import URL

from custom_components.aquahome import button
from custom_components.aquahome.const import (
    DOMAIN,
    RECHARGE_ACTION_COMMANDS_VERIFIED,
    REFRESH_BUTTON_POLL_DELAY_SECONDS,
)
from tests.conftest import (
    TEST_DEVICE_ID,
    add_device_routes,
    command_url,
    load_fixture,
    setup_integration,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aioresponses import aioresponses
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

#: Slug derived from the fixture serial ``4213377-30105-2242``.
SLUG = "4213377_30105_2242"

#: The buttons created for the regeneration-only dev device.
DEV_BUTTON_KEYS = frozenset(
    {
        "regenerate_now",
        "schedule_regeneration",
        "cancel_regeneration",
        "refresh_data",
        "advance_valve",
        "reset_error_code",
    }
)

#: Buttons that are never created on the dev device (feature-gated or const-gated).
ABSENT_BUTTON_KEYS = frozenset(
    {
        "silence_alarm",
        "reset_wsov_error_code",
        "vacation_mode",
        "recharge_off",
        "enable_recharge",
    }
)


@pytest.fixture(autouse=True)
def _only_button_platform() -> Iterator[None]:
    """Forward only the button platform for the duration of a test."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("custom_components.aquahome.PLATFORMS", [Platform.BUTTON])
        yield


# ---------------------------------------------------------------------------
# Local fixture-payload helpers (never mutate the loaded fixture in place)
# ---------------------------------------------------------------------------


def _detail() -> dict[str, Any]:
    """Return a deep copy of the captured device-detail payload to mutate."""
    return copy.deepcopy(load_fixture("device-detail.json"))


def _detail_without_recharge_tile() -> dict[str, Any]:
    """Return a device detail whose enriched ``recharge_ui`` block is absent.

    Models an ``iqua2`` host: the offline-capable recharge tile is missing, so
    every recharge decision has to come from the ``regeneration`` block instead.
    """
    detail = _detail()
    detail["enriched_data"]["water_treatment"].pop("recharge_ui")
    return detail


def _button_entity_id(entity_registry: er.EntityRegistry, key: str) -> str | None:
    """Resolve a button's entity id from its unique-id suffix, or ``None``."""
    return entity_registry.async_get_entity_id(BUTTON_DOMAIN, DOMAIN, f"{SLUG}_{key}")


def _state_of(hass: HomeAssistant, entity_id: str) -> str | None:
    """Return the current state string of ``entity_id``, or ``None``."""
    state = hass.states.get(entity_id)
    return state.state if state is not None else None


def _device_detail_get_count(mock_api: aioresponses) -> int:
    """Return how many ``GET /devices/{id}`` detail polls have been recorded."""
    return sum(
        len(calls)
        for (method, url), calls in mock_api.requests.items()
        if method == "GET" and url.path.endswith(f"/devices/{TEST_DEVICE_ID}")
    )


async def _press(hass: HomeAssistant, entity_id: str) -> None:
    """Press a button through the ``button.press`` service (errors propagate)."""
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )


# ---------------------------------------------------------------------------
# Creation set on the dev fixture
# ---------------------------------------------------------------------------


async def test_dev_fixture_creates_exactly_six_buttons(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """The regeneration-only dev device yields exactly the six expected buttons.

    Silence-alarm and the shutoff-valve reset are feature-gated out (no
    ``audible_alarm`` / ``wsov`` feature, no ``alarm_is_beeping`` / valve block);
    the three recharge-mode buttons are excluded while the const gate is ``False``.
    """
    add_device_routes(mock_api)

    assert await setup_integration(hass, mock_config_entry)

    entries = er.async_entries_for_config_entry(
        entity_registry, mock_config_entry.entry_id
    )
    created = {
        entry.unique_id.removeprefix(f"{SLUG}_")
        for entry in entries
        if entry.domain == BUTTON_DOMAIN
    }
    assert created == set(DEV_BUTTON_KEYS)
    assert created.isdisjoint(ABSENT_BUTTON_KEYS)


# ---------------------------------------------------------------------------
# Entity-category / registry-enabled metadata
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "category", "disabled"),
    [
        ("advance_valve", EntityCategory.CONFIG, True),
        ("reset_error_code", EntityCategory.CONFIG, True),
        ("refresh_data", EntityCategory.DIAGNOSTIC, False),
        ("regenerate_now", None, False),
    ],
)
async def test_button_category_and_registry_default(  # noqa: PLR0913
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    key: str,
    category: EntityCategory | None,
    disabled: bool,
) -> None:
    """The advanced tools are CONFIG + registry-disabled; refresh-data is DIAGNOSTIC.

    ``advance_valve`` and ``reset_error_code`` are service tools registry-disabled
    by default; ``refresh_data`` is a DIAGNOSTIC button enabled by default; a plain
    command button (``regenerate_now``) carries no entity category.
    """
    add_device_routes(mock_api)

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _button_entity_id(entity_registry, key)
    assert entity_id is not None
    registry_entry = entity_registry.async_get(entity_id)
    assert registry_entry is not None
    assert registry_entry.entity_category == category
    if disabled:
        assert registry_entry.disabled_by == RegistryEntryDisabler.INTEGRATION
    else:
        assert registry_entry.disabled_by is None


# ---------------------------------------------------------------------------
# Command payloads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "function", "action"),
    [
        ("regenerate_now", "regenerate", "regenerate"),
        ("schedule_regeneration", "regenerate", "schedule"),
        ("cancel_regeneration", "regenerate", "cancel"),
    ],
)
async def test_press_sends_exact_command_body(  # noqa: PLR0913
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    key: str,
    function: str,
    action: str,
) -> None:
    """Each regeneration control PUTs the literal ``{function, action}`` body."""
    add_device_routes(mock_api)
    mock_api.put(command_url(), payload={"result": "ok"})

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _button_entity_id(entity_registry, key)
    assert entity_id is not None
    await _press(hass, entity_id)

    calls = mock_api.requests[("PUT", URL(command_url()))]
    assert len(calls) == 1
    assert calls[-1].kwargs["json"] == {"function": function, "action": action}


async def test_refresh_data_delays_the_follow_up_poll(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Refresh-data sends ``get_all_data`` now, but re-polls only after the delay.

    The ``get_all_data`` command asks the device to push fresh state to the cloud;
    the coordinator re-poll is deferred ``REFRESH_BUTTON_POLL_DELAY_SECONDS`` so it
    reads the pushed values. The device-detail GET count must not rise until the
    timer elapses.
    """
    add_device_routes(mock_api)
    mock_api.put(command_url(), payload={"result": "ok"})

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _button_entity_id(entity_registry, "refresh_data")
    assert entity_id is not None
    baseline = _device_detail_get_count(mock_api)

    await _press(hass, entity_id)

    # The command fired; the deferred poll has not.
    calls = mock_api.requests[("PUT", URL(command_url()))]
    assert calls[-1].kwargs["json"] == {"function": "get_all_data", "action": "none"}
    assert _device_detail_get_count(mock_api) == baseline

    # Just short of the delay: still no extra poll.
    freezer.tick(REFRESH_BUTTON_POLL_DELAY_SECONDS - 1)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _device_detail_get_count(mock_api) == baseline

    # Past the delay: exactly one follow-up poll.
    freezer.tick(2)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _device_detail_get_count(mock_api) == baseline + 1


# ---------------------------------------------------------------------------
# Error surfacing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "exception", "translation_key"),
    [
        (422, None, "command_rejected"),
        (429, None, "rate_limited"),
        (None, aiohttp.ClientConnectionError("boom"), "cannot_connect"),
    ],
)
async def test_press_maps_failures_to_translations(  # noqa: PLR0913
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    status: int | None,
    exception: Exception | None,
    translation_key: str,
) -> None:
    """A rejected / throttled / unreachable command raises the mapped error key.

    A 400/422 rejection surfaces ``command_rejected``, a 429 ``rate_limited``, and
    a transport failure ``cannot_connect`` — the shared command taxonomy, seen
    through a button press.
    """
    add_device_routes(mock_api)
    if exception is not None:
        mock_api.put(command_url(), exception=exception)
    else:
        mock_api.put(command_url(), status=status, payload={"detail": "no"})

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _button_entity_id(entity_registry, "regenerate_now")
    assert entity_id is not None
    with pytest.raises(HomeAssistantError) as caught:
        await _press(hass, entity_id)
    assert caught.value.translation_key == translation_key
    assert caught.value.translation_domain == DOMAIN


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


async def test_regenerate_unavailable_when_cannot_recharge(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """``can_recharge`` ``False`` disables regenerate-now but not cancel.

    Availability layers the recharge-tile ``can_recharge`` guidance on top of the
    online gate for regenerate-now only; ``cancel_regeneration`` has no such gate
    and stays pressable so a running recharge can always be stopped.
    """
    detail = _detail()
    detail["enriched_data"]["water_treatment"]["recharge_ui"]["can_recharge"] = False
    add_device_routes(mock_api, device_detail=detail)

    assert await setup_integration(hass, mock_config_entry)

    regenerate_id = _button_entity_id(entity_registry, "regenerate_now")
    cancel_id = _button_entity_id(entity_registry, "cancel_regeneration")
    assert regenerate_id is not None
    assert cancel_id is not None
    assert _state_of(hass, regenerate_id) == STATE_UNAVAILABLE
    assert _state_of(hass, cancel_id) == STATE_UNKNOWN


async def test_silence_alarm_created_when_the_alarm_feature_is_advertised(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """A device advertising ``audible_alarm`` gets the silence-alarm button.

    The dev fixture has neither the feature nor an ``alarm_is_beeping`` flag, so
    only the feature leg of the gate proves a softener with an audible alarm can
    actually silence it; a gate collapsed onto the flag alone would leave those
    devices without the button.
    """
    detail = _detail()
    detail["enriched_data"]["water_treatment"]["features"].append("audible_alarm")
    add_device_routes(mock_api, device_detail=detail)

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _button_entity_id(entity_registry, "silence_alarm")
    assert entity_id is not None
    assert _state_of(hass, entity_id) == STATE_UNKNOWN


@pytest.mark.parametrize(
    ("flag", "blocked_key", "free_key"),
    [
        ("can_recharge", "regenerate_now", "schedule_regeneration"),
        ("can_schedule", "schedule_regeneration", "regenerate_now"),
    ],
)
async def test_recharge_guidance_falls_back_to_the_regeneration_block(  # noqa: PLR0913
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    flag: str,
    blocked_key: str,
    free_key: str,
) -> None:
    """Without a recharge tile the guidance comes from the ``regeneration`` block.

    On a host that omits ``recharge_ui`` the ``can_recharge`` / ``can_schedule``
    hints live in the ``regeneration`` block; losing that fallback would make
    both controls permanently pressable there and let the integration fire
    commands the device has already said it will refuse.
    """
    detail = _detail_without_recharge_tile()
    detail["enriched_data"]["water_treatment"]["regeneration"][flag] = False
    add_device_routes(mock_api, device_detail=detail)

    assert await setup_integration(hass, mock_config_entry)

    blocked_id = _button_entity_id(entity_registry, blocked_key)
    free_id = _button_entity_id(entity_registry, free_key)
    assert blocked_id is not None
    assert free_id is not None
    assert _state_of(hass, blocked_id) == STATE_UNAVAILABLE
    # The other control's hint is still True in the same block: only one is gated.
    assert _state_of(hass, free_id) == STATE_UNKNOWN


async def test_all_buttons_unavailable_when_device_offline(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """An offline device (``is_online`` ``False``) makes every button unavailable."""
    detail = _detail()
    detail["is_online"] = False
    add_device_routes(mock_api, device_detail=detail)

    assert await setup_integration(hass, mock_config_entry)

    for key in ("regenerate_now", "cancel_regeneration", "refresh_data"):
        entity_id = _button_entity_id(entity_registry, key)
        assert entity_id is not None
        assert _state_of(hass, entity_id) == STATE_UNAVAILABLE


# ---------------------------------------------------------------------------
# Gated recharge-mode buttons (UNVERIFIED payload guesses)
# ---------------------------------------------------------------------------


def test_recharge_action_buttons_are_gated_off() -> None:
    """While the const is ``False`` the three recharge buttons are not in the table.

    They carry documented UNVERIFIED ``regenerate`` payload guesses (ledger P1) but
    are excluded from :data:`~custom_components.aquahome.button.BUTTONS` — and so
    never created — until the supervised live test flips the gate.
    """
    assert RECHARGE_ACTION_COMMANDS_VERIFIED is False

    table_keys = {description.key for description in button.BUTTONS}
    assert table_keys.isdisjoint({"vacation_mode", "recharge_off", "enable_recharge"})

    guesses = {
        description.key: (description.function, description.action)
        for description in button._RECHARGE_ACTION_BUTTONS
    }
    assert guesses == {
        "vacation_mode": ("regenerate", "vacation_mode"),
        "recharge_off": ("regenerate", "recharge_off"),
        "enable_recharge": ("regenerate", "enable_recharge"),
    }


async def test_recharge_buttons_absent_even_when_advertised_while_gated(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """The captured device advertises the actions, yet no button is created.

    The fixture ``recharge_ui.actions`` already offer ``vacation_mode`` and
    ``recharge_off``, but the const gate keeps them out of the table, so neither
    button materialises.
    """
    add_device_routes(mock_api)

    assert await setup_integration(hass, mock_config_entry)

    assert _button_entity_id(entity_registry, "vacation_mode") is None
    assert _button_entity_id(entity_registry, "recharge_off") is None


async def test_recharge_buttons_appear_only_when_advertised_once_enabled(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """With the table flipped in, a recharge button exists only if advertised.

    Patches :data:`~custom_components.aquahome.button.BUTTONS` to include the gated
    descriptions (standing in for the const flip). The captured device advertises
    ``vacation_mode`` and ``recharge_off`` in ``recharge_ui.actions`` but not
    ``enable_recharge``, so the first two buttons are created and the third — whose
    action the tile never offers — stays absent.
    """
    add_device_routes(mock_api)

    enabled_table = (*button._ACTIVE_BUTTONS, *button._RECHARGE_ACTION_BUTTONS)
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(button, "BUTTONS", enabled_table)
        assert await setup_integration(hass, mock_config_entry)

    assert _button_entity_id(entity_registry, "vacation_mode") is not None
    assert _button_entity_id(entity_registry, "recharge_off") is not None
    assert _button_entity_id(entity_registry, "enable_recharge") is None


async def test_recharge_buttons_absent_when_no_tile_advertises_them(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """With the table flipped in, a device with no recharge tile gets none of them.

    An ``iqua2`` host carries no ``recharge_ui`` block at all, so there is
    nothing advertising the recharge-mode actions. The exists check must read
    that as "not offered" — without its ``None`` leg it would raise on the
    missing tile while the platform is being built, taking every other button
    down with it.
    """
    add_device_routes(mock_api, device_detail=_detail_without_recharge_tile())

    enabled_table = (*button._ACTIVE_BUTTONS, *button._RECHARGE_ACTION_BUTTONS)
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(button, "BUTTONS", enabled_table)
        assert await setup_integration(hass, mock_config_entry)

    for key in ("vacation_mode", "recharge_off", "enable_recharge"):
        assert _button_entity_id(entity_registry, key) is None
    # The rest of the platform still came up.
    assert _button_entity_id(entity_registry, "regenerate_now") is not None


# ---------------------------------------------------------------------------
# The refresh-data follow-up timer
# ---------------------------------------------------------------------------


async def test_second_refresh_press_restarts_the_pending_poll(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Pressing refresh-data again cancels the pending poll and re-arms it.

    Both presses ask the device to push fresh state, so only the last one's
    delay matters: the first timer must be cancelled, not left running. Without
    the cancel the entity would poll twice — once on the stale timer, a second
    time on the new one — spending two cloud reads per pair of presses.
    """
    add_device_routes(mock_api)
    mock_api.put(command_url(), payload={"result": "ok"}, repeat=True)

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _button_entity_id(entity_registry, "refresh_data")
    assert entity_id is not None
    baseline = _device_detail_get_count(mock_api)

    await _press(hass, entity_id)
    freezer.tick(REFRESH_BUTTON_POLL_DELAY_SECONDS - 1)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    # Second press one second before the first timer would have fired.
    await _press(hass, entity_id)
    freezer.tick(2)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    # The first timer was cancelled by the second press: nothing polled yet.
    assert _device_detail_get_count(mock_api) == baseline

    # The second press's own delay elapses: exactly one poll, not two.
    freezer.tick(REFRESH_BUTTON_POLL_DELAY_SECONDS)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _device_detail_get_count(mock_api) == baseline + 1


async def test_pending_refresh_poll_is_cancelled_when_the_entry_unloads(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Unloading the entry before the delay elapses drops the follow-up poll.

    The timer outlives the entity unless removal cancels it; a surviving
    callback would poll the cloud through a coordinator whose entry is already
    unloaded — the classic "lingering timer" teardown leak Home Assistant fails
    tests for.
    """
    add_device_routes(mock_api)
    mock_api.put(command_url(), payload={"result": "ok"})

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _button_entity_id(entity_registry, "refresh_data")
    assert entity_id is not None

    await _press(hass, entity_id)
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    baseline = _device_detail_get_count(mock_api)

    freezer.tick(REFRESH_BUTTON_POLL_DELAY_SECONDS + 1)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert _device_detail_get_count(mock_api) == baseline
