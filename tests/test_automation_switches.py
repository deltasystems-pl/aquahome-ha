"""Tests for the three per-device automation switches (the Phase-8 opt-ins).

The automation tier is opt-in *by construction*: every device-affecting rule it
owns hangs off one of three switches — ``vacation_deferral``, ``auto_vacation``
and ``smart_regeneration`` — which are created unconditionally for every device
and start OFF on a fresh config entry. This module owns that projection layer:
what the switches are (unique ids, categories, icons, registry defaults), what
they render from the scheduler's published
:class:`~custom_components.aquahome.automation_state.AutomationState`, what a
turn on/off writes into ``entry.options``, and the fact that they stay operable
when the cloud and the analytics tier do not.

What they are *not* asked to prove is the scheduler's decision matrix — that is
the scheduler suite's job. Everything here drives the real integration end to
end (only the switch platform forwarded, so each assertion runs through the real
coordinator-first-refresh path) and then feeds the analytics engine crafted
:class:`~custom_components.aquahome.analytics.model.AnalyticsResult` values, or
publishes a crafted ``AutomationState`` straight onto the scheduler when the
point is purely how a state renders.

Two determinism rules run through the file. The clock is frozen before setup
and the stored access token re-minted against it, so every timestamp an
attribute renders is a fixed literal; and the engine is seeded with a crafted
result carrying *no* forecast, so a scheduler pass provoked by a switch write
can never take a scheduling decision (and therefore never sends an unmocked
command) unless a test explicitly asks for one.

Attribute convention asserted throughout, as across the analytics tier: the
deferral and decision attribute sets always emit *every* key, ``None`` when the
state has no value, so a template written against a running deferral keeps
evaluating once it ends.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_ICON,
    CONF_ACCESS_TOKEN,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    EntityCategory,
    Platform,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.helpers.update_coordinator import UpdateFailed
from yarl import URL

from custom_components.aquahome.analytics.model import (
    AnalyticsResult,
    AnomalyState,
    ForecastState,
    GridSummary,
    LeakState,
    VacationState,
)
from custom_components.aquahome.automation_state import AutomationState
from custom_components.aquahome.const import (
    DEFERRAL_SOURCE_AUTO,
    DEFERRAL_SOURCE_MANUAL,
    DOMAIN,
    OPTION_AUTOMATION,
)
from custom_components.aquahome.scheduler import (
    DECISION_DEFERRED,
    DECISION_SCHEDULED,
    DECISION_SKIPPED_DEFERRAL,
    DECISION_SKIPPED_NO_FORECAST,
    DECISION_SKIPPED_OFF,
)
from custom_components.aquahome.switch import (
    AquaHomeAutomationSwitch,
    AquaHomeVacationDeferralSwitch,
)
from tests.conftest import (
    TEST_DEVICE_ID,
    add_device_routes,
    command_url,
    make_access_token,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aioresponses import aioresponses
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from syrupy.assertion import SnapshotAssertion

    from custom_components.aquahome.analytics.engine import AquaHomeAnalyticsEngine
    from custom_components.aquahome.scheduler import AquaHomeRegenScheduler

#: The switch platform domain (``homeassistant.components.switch`` does not
#: re-export ``DOMAIN`` for typing, so derive it from the platform enum).
SWITCH_DOMAIN = Platform.SWITCH

#: Slug of the captured device's serial ``7384243-20203-1120``.
SLUG = "7384243_20203_1120"

#: Instant every test freezes to before setup, matching the sibling platform
#: suites: inside the fixtures' capture window, so nothing depends on wall time.
FROZEN_INSTANT = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
#: The same instant as an attribute renders it (``deferral_started`` etc.).
FROZEN_ISO = "2026-07-21T12:00:00+00:00"

#: The three automation switches: (description key, category, icon). Only the
#: deferral is a primary control; the other two configure how the automation
#: behaves and must therefore be CONFIG-categorised.
AUTOMATION_SWITCHES: tuple[tuple[str, EntityCategory | None, str], ...] = (
    ("vacation_deferral", None, "mdi:calendar-remove"),
    ("auto_vacation", EntityCategory.CONFIG, "mdi:home-export-outline"),
    ("smart_regeneration", EntityCategory.CONFIG, "mdi:auto-fix"),
)

#: Keys the vacation-deferral switch's attribute dict always carries.
DEFERRAL_ATTRIBUTE_KEYS = frozenset(
    {"deferral_source", "deferral_started", "days_deferred"}
)
#: Keys the smart-regeneration switch's attribute dict always carries.
DECISION_ATTRIBUTE_KEYS = frozenset({"last_decision", "last_decision_at"})

#: Remaining treated-water capacity the captured fixture reports, in gallons
#: (raw ``treated_water_avail_gals``) — the figure a forecast is measured
#: against when a test wants the scheduler to actually take a decision.
FIXTURE_CAPACITY_GALLONS = 185.0


# ---------------------------------------------------------------------------
# Crafted analytics results
#
# One neutral value per state dataclass — the shape a pass with nothing to
# assess produces. The default result carries no forecast, which is what makes
# every switch write in this file free of device commands.
# ---------------------------------------------------------------------------

NEUTRAL_LEAK = LeakState(
    active=None,
    consecutive_nights=0,
    rate_liters_per_hour=None,
    implied_liters_per_day=None,
    tier=None,
    persistent_flow=False,
    last_verdict_night=None,
    masking_coverage=True,
)
NEUTRAL_ANOMALY = AnomalyState(
    active=None,
    reasons=(),
    day=None,
    point_hours=0,
    drift_alarm=False,
    drift_cusum=False,
    drift_ewma=False,
)
NEUTRAL_VACATION = VacationState(active=None, consecutive_days=0, since=None)
NEUTRAL_FORECAST = ForecastState(
    gallons=None,
    liters=None,
    source=None,
    band_liters=None,
    weekday=None,
    persons=None,
)
NEUTRAL_GRID = GridSummary(
    active_hours=(False,) * 168, mature_buckets=0, hourly_samples=0
)


def _result(*, forecast: ForecastState = NEUTRAL_FORECAST) -> AnalyticsResult:
    """Assemble one crafted analytics result from the neutral defaults.

    Only the forecast is overridable: it is the single field the automation
    tier reads on the way to a decision, and leaving it ``None`` is what keeps a
    scheduler pass silent.
    """
    return AnalyticsResult(
        computed_at=FROZEN_INSTANT,
        nights=(),
        days=(),
        leak=NEUTRAL_LEAK,
        anomaly=NEUTRAL_ANOMALY,
        vacation=NEUTRAL_VACATION,
        forecast=forecast,
        grid=NEUTRAL_GRID,
    )


# ---------------------------------------------------------------------------
# Fixtures, boot and access helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _only_switch_platform() -> Iterator[None]:
    """Forward only the switch platform for the duration of a test."""
    with patch("custom_components.aquahome.PLATFORMS", [Platform.SWITCH]):
        yield


async def boot(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock: aioresponses,
    freezer: FrozenDateTimeFactory,
    *,
    seed: AnalyticsResult | None = None,
) -> None:
    """Freeze the clock, set the entry up, and publish a crafted first verdict.

    The stored access token is re-minted against the frozen clock so the auth
    manager never decides it is stale mid-test; that has to happen between
    adding the entry and setting it up, which is why the shared
    ``setup_integration`` helper is unrolled here. Settling with
    ``wait_background_tasks`` matters: the engine's own startup pass runs as an
    entry background task, and a result pushed before it finished would be
    overwritten by it.

    ``seed`` defaults to the neutral (forecast-free) crafted result so the
    engine's published data is this file's, not whatever the startup pass
    computed off the fixture's weekday averages — a test that never asks for a
    scheduling decision must not get one. Pass ``seed=None`` to leave the
    engine's own data in place.
    """
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(mock)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_ACCESS_TOKEN: make_access_token()}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    if seed is not None:
        await push(hass, entry, seed)


def engine_of(entry: MockConfigEntry) -> AquaHomeAnalyticsEngine:
    """Return the analytics engine the entry built for the fixture device."""
    engine: AquaHomeAnalyticsEngine = entry.runtime_data.analytics_engines[
        TEST_DEVICE_ID
    ]
    return engine


def scheduler_of(entry: MockConfigEntry) -> AquaHomeRegenScheduler:
    """Return the regeneration scheduler the entry built for the device."""
    scheduler: AquaHomeRegenScheduler = entry.runtime_data.schedulers[TEST_DEVICE_ID]
    return scheduler


async def push(
    hass: HomeAssistant, entry: MockConfigEntry, result: AnalyticsResult
) -> None:
    """Publish a crafted analytics result and settle every listener.

    The scheduler subscribes to the engine, so this is also how a scheduler
    pass is provoked deterministically.
    """
    engine_of(entry).async_set_updated_data(result)
    await hass.async_block_till_done()


async def publish_state(
    hass: HomeAssistant, entry: MockConfigEntry, state: AutomationState
) -> None:
    """Publish a crafted automation state straight onto the scheduler.

    Used where the point is purely how a state *renders* — the scheduler's own
    transitions are the scheduler suite's subject, and crafting the state keeps
    unreachable-by-transition shapes (a fresh state with no decision yet)
    testable.
    """
    scheduler_of(entry).async_set_updated_data(state)
    await hass.async_block_till_done()


def entity_id_of(registry: er.EntityRegistry, key: str) -> str:
    """Return the entity id registered for one automation-switch key."""
    entity_id = registry.async_get_entity_id(SWITCH_DOMAIN, DOMAIN, f"{SLUG}_{key}")
    assert entity_id is not None, f"automation switch {key} was never registered"
    return entity_id


def state_of(hass: HomeAssistant, registry: er.EntityRegistry, key: str) -> str:
    """Return the current state string of one automation switch."""
    state = hass.states.get(entity_id_of(registry, key))
    assert state is not None, f"automation switch {key} has no state"
    return state.state


def attributes_of(
    hass: HomeAssistant, registry: er.EntityRegistry, key: str
) -> dict[str, Any]:
    """Return the current state attributes of one automation switch."""
    state = hass.states.get(entity_id_of(registry, key))
    assert state is not None, f"automation switch {key} has no state"
    return dict(state.attributes)


def live_switch(
    hass: HomeAssistant, registry: er.EntityRegistry, key: str
) -> AquaHomeAutomationSwitch:
    """Return the live entity object behind one automation switch."""
    component = hass.data[DATA_INSTANCES][SWITCH_DOMAIN]
    entity = component.get_entity(entity_id_of(registry, key))
    assert isinstance(entity, AquaHomeAutomationSwitch)
    return entity


def stored_flags(entry: MockConfigEntry) -> dict[str, Any]:
    """Return the device's persisted automation options block."""
    devices = entry.options.get(OPTION_AUTOMATION)
    assert isinstance(devices, dict), "no automation options were ever persisted"
    stored = devices.get(TEST_DEVICE_ID)
    assert isinstance(stored, dict), "the device has no persisted automation options"
    return stored


async def switch_to(
    hass: HomeAssistant, registry: er.EntityRegistry, key: str, *, on: bool
) -> None:
    """Drive one automation switch through the real turn_on/turn_off action."""
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON if on else SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: entity_id_of(registry, key)},
        blocking=True,
    )
    await hass.async_block_till_done()


def command_bodies(mock: aioresponses) -> list[dict[str, Any]]:
    """Return the JSON bodies of every recorded command PUT, in order."""
    calls = mock.requests.get(("PUT", URL(command_url())), [])
    return [call.kwargs["json"] for call in calls]


# ---------------------------------------------------------------------------
# Existence, identity and registry metadata
# ---------------------------------------------------------------------------


async def test_three_automation_switches_per_device(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Every device gets exactly the three opt-ins under ``{slug}_{key}`` ids.

    Nothing gates their existence — the scheduler runs for every device — so the
    set is fixed and complete even on the regeneration-only dev fixture, which
    has neither a boolean setting nor a leak detector to contribute a switch of
    its own. All three hang off the same device as the telemetry entities.
    """
    await boot(hass, mock_config_entry, mock_api, freezer, seed=_result())

    switches = [
        entity
        for entity in er.async_entries_for_config_entry(
            entity_registry, mock_config_entry.entry_id
        )
        if entity.domain == SWITCH_DOMAIN
    ]
    # The switch domain also hosts the live-mode controls; the automation trio
    # must be exactly present within it, all on the one telemetry device.
    automation_ids = {f"{SLUG}_{key}" for key, _category, _icon in AUTOMATION_SWITCHES}
    assert automation_ids <= {entity.unique_id for entity in switches}
    assert len({entity.device_id for entity in switches}) == 1


@pytest.mark.parametrize(
    ("key", "category", "icon"),
    [pytest.param(*row, id=row[0]) for row in AUTOMATION_SWITCHES],
)
async def test_registry_metadata_and_icon(  # noqa: PLR0913 - standard HA platform-test fixture set plus the parametrized row
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    key: str,
    category: EntityCategory | None,
    icon: str,
) -> None:
    """Each opt-in is registry-enabled, correctly categorised, and iconed.

    An opt-in the user cannot find is not an opt-in, so none of the three may
    ship registry-disabled or hidden; the deferral is a primary control while
    the two behaviour flags are CONFIG.
    """
    await boot(hass, mock_config_entry, mock_api, freezer, seed=_result())

    entity_id = entity_id_of(entity_registry, key)
    entity = entity_registry.async_get(entity_id)
    assert entity is not None
    assert entity.unique_id == f"{SLUG}_{key}"
    assert entity.disabled_by is None
    assert entity.hidden_by is None
    assert entity.entity_category == category
    assert entity.translation_key == key
    assert entity.original_icon == icon
    assert attributes_of(hass, entity_registry, key)[ATTR_ICON] == icon


async def test_default_state_is_off_on_a_fresh_entry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A fresh config entry starts every automation switch OFF.

    This is the exit criterion in entity form: with no persisted options, no
    device-affecting automation may be running, and the persisted block the
    scheduler writes on its first pass must say so too.
    """
    await boot(hass, mock_config_entry, mock_api, freezer, seed=_result())

    for key, _category, _icon in AUTOMATION_SWITCHES:
        assert state_of(hass, entity_registry, key) == STATE_OFF
    stored = stored_flags(mock_config_entry)
    assert stored["vacation_deferral"] is False
    assert stored["auto_vacation"] is False
    assert stored["smart_regeneration"] is False
    assert stored["deferral_source"] is None
    assert stored["deferral_started"] is None


async def test_only_the_deferral_switch_is_the_service_target_class(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """``set_vacation_mode`` identifies its target by class, so only one has it.

    The action layer rejects any other switch with a translated error rather
    than silently doing nothing, which only works while exactly one of the three
    is an :class:`AquaHomeVacationDeferralSwitch`.
    """
    await boot(hass, mock_config_entry, mock_api, freezer, seed=_result())

    deferral = live_switch(hass, entity_registry, "vacation_deferral")
    assert isinstance(deferral, AquaHomeVacationDeferralSwitch)
    for key in ("auto_vacation", "smart_regeneration"):
        other = live_switch(hass, entity_registry, key)
        assert not isinstance(other, AquaHomeVacationDeferralSwitch)


# ---------------------------------------------------------------------------
# Writes: options round-trip and re-render
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key", [pytest.param(row[0], id=row[0]) for row in AUTOMATION_SWITCHES]
)
async def test_turn_on_and_off_round_trips_through_the_entry_options(  # noqa: PLR0913 - standard HA platform-test fixture set plus the parametrized row
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    key: str,
) -> None:
    """Each flag persists into ``entry.options`` and re-renders on the switch.

    The switch writes nothing itself: it calls the scheduler, which persists the
    user-set subset and republishes the state the entity then renders. Both
    halves are asserted, because a flag that renders ON without persisting is
    forgotten on the next restart.
    """
    await boot(hass, mock_config_entry, mock_api, freezer, seed=_result())

    await switch_to(hass, entity_registry, key, on=True)
    assert state_of(hass, entity_registry, key) == STATE_ON
    assert stored_flags(mock_config_entry)[key] is True

    await switch_to(hass, entity_registry, key, on=False)
    assert state_of(hass, entity_registry, key) == STATE_OFF
    assert stored_flags(mock_config_entry)[key] is False


async def test_turning_a_flag_on_leaves_the_other_two_alone(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Flipping one opt-in never drags a sibling with it.

    The three flags share one persisted dict and one published state object, so
    a careless write path could easily reset a neighbour.
    """
    await boot(hass, mock_config_entry, mock_api, freezer, seed=_result())

    await switch_to(hass, entity_registry, "smart_regeneration", on=True)
    await switch_to(hass, entity_registry, "auto_vacation", on=True)

    assert state_of(hass, entity_registry, "smart_regeneration") == STATE_ON
    assert state_of(hass, entity_registry, "auto_vacation") == STATE_ON
    assert state_of(hass, entity_registry, "vacation_deferral") == STATE_OFF

    await switch_to(hass, entity_registry, "auto_vacation", on=False)
    assert state_of(hass, entity_registry, "smart_regeneration") == STATE_ON
    assert stored_flags(mock_config_entry)["smart_regeneration"] is True


async def test_deferral_switch_records_a_manual_source(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A deferral started from the switch is persisted as *manual*.

    The source decides whether the auto-vacation follower may release the
    deferral again on the household's behalf, so a tap on the switch has to be
    recorded as the user's own act — and the bookkeeping has to be cleared
    again when the deferral ends, never left behind as stale residue.
    """
    await boot(hass, mock_config_entry, mock_api, freezer, seed=_result())

    await switch_to(hass, entity_registry, "vacation_deferral", on=True)
    stored = stored_flags(mock_config_entry)
    assert stored["deferral_source"] == DEFERRAL_SOURCE_MANUAL
    assert stored["deferral_started"] == FROZEN_ISO

    await switch_to(hass, entity_registry, "vacation_deferral", on=False)
    stored = stored_flags(mock_config_entry)
    assert stored["deferral_source"] is None
    assert stored["deferral_started"] is None
    # Neither edge talks to the device here: the fixture reports a *ready*
    # softener with nothing scheduled to cancel, and the seeded verdict carries
    # no forecast to catch up against. No ``/command`` route is registered, so
    # an attempted call would be recorded (and refused) rather than pass.
    assert command_bodies(mock_api) == []


async def test_async_set_vacation_mode_drives_the_same_path(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The action hook on the deferral switch behaves exactly like a tap.

    ``set_vacation_mode`` reaches the entity through this method, and it must
    land on the same manual-deferral path the switch itself uses so an action, a
    blueprint and a tap are indistinguishable afterwards.
    """
    await boot(hass, mock_config_entry, mock_api, freezer, seed=_result())

    deferral = live_switch(hass, entity_registry, "vacation_deferral")
    assert isinstance(deferral, AquaHomeVacationDeferralSwitch)

    await deferral.async_set_vacation_mode(True)
    await hass.async_block_till_done()
    assert state_of(hass, entity_registry, "vacation_deferral") == STATE_ON
    assert stored_flags(mock_config_entry)["vacation_deferral"] is True
    assert stored_flags(mock_config_entry)["deferral_source"] == DEFERRAL_SOURCE_MANUAL

    await deferral.async_set_vacation_mode(False)
    await hass.async_block_till_done()
    assert state_of(hass, entity_registry, "vacation_deferral") == STATE_OFF
    assert stored_flags(mock_config_entry)["vacation_deferral"] is False


async def test_persisted_flags_survive_a_reload(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A deferral and its bookkeeping come back after a config-entry reload.

    The whole point of persisting into ``entry.options`` is that a restart never
    forgets an opt-in: the rebuilt scheduler seeds itself from the same options
    and the switch renders the running deferral — including who started it and
    when — without the user touching anything.
    """
    await boot(hass, mock_config_entry, mock_api, freezer, seed=_result())
    await switch_to(hass, entity_registry, "vacation_deferral", on=True)
    await switch_to(hass, entity_registry, "smart_regeneration", on=True)

    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert state_of(hass, entity_registry, "vacation_deferral") == STATE_ON
    assert state_of(hass, entity_registry, "smart_regeneration") == STATE_ON
    assert state_of(hass, entity_registry, "auto_vacation") == STATE_OFF
    attributes = attributes_of(hass, entity_registry, "vacation_deferral")
    assert attributes["deferral_source"] == DEFERRAL_SOURCE_MANUAL
    assert attributes["deferral_started"] == FROZEN_ISO
    # The rebuilt scheduler acts on the restored flags straight away: its first
    # pass reports the restored deferral as the reason it stood down, which it
    # can only know from the options it seeded itself from.
    assert (
        attributes_of(hass, entity_registry, "smart_regeneration")["last_decision"]
        == DECISION_SKIPPED_DEFERRAL
    )


# ---------------------------------------------------------------------------
# Attributes
# ---------------------------------------------------------------------------


async def test_deferral_attributes_track_the_running_deferral(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The deferral switch reports who deferred, since when, and for how long.

    ``days_deferred`` is the unit the resin-hygiene cap is measured in, so it
    has to keep counting as the deferral runs rather than freezing at the value
    it had when the flag was set.
    """
    await boot(hass, mock_config_entry, mock_api, freezer, seed=_result())

    idle = attributes_of(hass, entity_registry, "vacation_deferral")
    assert idle.keys() >= DEFERRAL_ATTRIBUTE_KEYS
    assert all(idle[name] is None for name in DEFERRAL_ATTRIBUTE_KEYS)

    await switch_to(hass, entity_registry, "vacation_deferral", on=True)
    started = attributes_of(hass, entity_registry, "vacation_deferral")
    assert started["deferral_source"] == DEFERRAL_SOURCE_MANUAL
    assert started["deferral_started"] == FROZEN_ISO
    assert started["days_deferred"] == 0

    # Three days on (plus an hour, so the boundary is not the assertion), a
    # fresh scheduler pass re-renders the switch against the same start stamp.
    freezer.move_to(FROZEN_INSTANT + timedelta(days=3, hours=1))
    await push(hass, mock_config_entry, _result())
    running = attributes_of(hass, entity_registry, "vacation_deferral")
    assert running["deferral_started"] == FROZEN_ISO
    assert running["days_deferred"] == 3


async def test_smart_regeneration_attributes_report_the_last_decision(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The smart switch explains every pass, acted on or not.

    A night that passed without a regeneration must say why on the entity
    rather than only in the debug log: with the opt-in off that is
    ``skipped_off``, and with it on but no forecast to compare against,
    ``skipped_no_forecast``.
    """
    await boot(hass, mock_config_entry, mock_api, freezer, seed=_result())

    off = attributes_of(hass, entity_registry, "smart_regeneration")
    assert off.keys() >= DECISION_ATTRIBUTE_KEYS
    assert off["last_decision"] == DECISION_SKIPPED_OFF
    assert off["last_decision_at"] == FROZEN_ISO

    await switch_to(hass, entity_registry, "smart_regeneration", on=True)
    await push(hass, mock_config_entry, _result())

    on = attributes_of(hass, entity_registry, "smart_regeneration")
    assert on["last_decision"] == DECISION_SKIPPED_NO_FORECAST
    assert on["last_decision_at"] == FROZEN_ISO
    assert command_bodies(mock_api) == []


async def test_smart_regeneration_attributes_report_a_taken_decision(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An acted-on pass renders as the action, not as a skip.

    With the opt-in on and tomorrow's forecast well past the fixture's
    remaining capacity the scheduler schedules a recharge; the switch is where
    the user sees that it did.
    """
    mock_api.put(command_url(), payload={"result": "ok"}, repeat=True)
    await boot(hass, mock_config_entry, mock_api, freezer, seed=_result())
    await switch_to(hass, entity_registry, "smart_regeneration", on=True)

    hungry = replace(NEUTRAL_FORECAST, gallons=FIXTURE_CAPACITY_GALLONS * 2)
    await push(hass, mock_config_entry, _result(forecast=hungry))

    attributes = attributes_of(hass, entity_registry, "smart_regeneration")
    assert attributes["last_decision"] == DECISION_SCHEDULED
    assert attributes["last_decision_at"] == FROZEN_ISO
    assert command_bodies(mock_api) == [
        {"function": "regenerate", "action": "schedule"}
    ]


async def test_attribute_keys_are_always_present(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Both attribute sets emit every key in every state, ``None`` when absent.

    Rendered from crafted states so the empty shape — a scheduler that has not
    taken a decision yet — is reachable, and so the ``auto`` deferral source
    (only the follower and a confirmed repair produce it) is covered without
    borrowing the scheduler's transitions.
    """
    await boot(hass, mock_config_entry, mock_api, freezer, seed=_result())

    await publish_state(hass, mock_config_entry, AutomationState())
    empty_deferral = attributes_of(hass, entity_registry, "vacation_deferral")
    empty_decision = attributes_of(hass, entity_registry, "smart_regeneration")
    assert empty_deferral.keys() >= DEFERRAL_ATTRIBUTE_KEYS
    assert empty_decision.keys() >= DECISION_ATTRIBUTE_KEYS
    assert all(empty_deferral[name] is None for name in DEFERRAL_ATTRIBUTE_KEYS)
    assert all(empty_decision[name] is None for name in DECISION_ATTRIBUTE_KEYS)

    await publish_state(
        hass,
        mock_config_entry,
        AutomationState(
            vacation_deferral=True,
            auto_vacation=True,
            smart_regeneration=True,
            deferral_source=DEFERRAL_SOURCE_AUTO,
            deferral_started=FROZEN_INSTANT - timedelta(days=2),
            last_decision=DECISION_DEFERRED,
            last_decision_at=FROZEN_INSTANT,
        ),
    )
    for key, _category, _icon in AUTOMATION_SWITCHES:
        assert state_of(hass, entity_registry, key) == STATE_ON
    full_deferral = attributes_of(hass, entity_registry, "vacation_deferral")
    assert full_deferral["deferral_source"] == DEFERRAL_SOURCE_AUTO
    assert full_deferral["deferral_started"] == "2026-07-19T12:00:00+00:00"
    assert full_deferral["days_deferred"] == 2
    full_decision = attributes_of(hass, entity_registry, "smart_regeneration")
    assert full_decision["last_decision"] == DECISION_DEFERRED
    assert full_decision["last_decision_at"] == FROZEN_ISO


async def test_auto_vacation_switch_carries_no_bookkeeping(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The follower flag exposes no attributes of its own.

    It is a plain preference — everything worth reporting about the deferral it
    starts belongs on the deferral switch, and duplicating it would give
    templates two sources of truth.
    """
    await boot(hass, mock_config_entry, mock_api, freezer, seed=_result())

    attributes = attributes_of(hass, entity_registry, "auto_vacation")
    assert not (DEFERRAL_ATTRIBUTE_KEYS | DECISION_ATTRIBUTE_KEYS) & attributes.keys()


# ---------------------------------------------------------------------------
# Availability: a local preference is never cloud-gated
# ---------------------------------------------------------------------------


async def test_switches_are_available_when_the_engine_never_produced_data(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """With the analytics tier dead on arrival the opt-ins still work.

    The engine's very first pass fails here, so it has published nothing at all
    and the scheduler has never seen a verdict. The switches render their
    persisted flags regardless — an install whose analytics never came up must
    still be able to arm and disarm its automations.
    """
    with patch(
        "custom_components.aquahome.analytics.engine.AquaHomeAnalyticsEngine"
        "._async_update_data",
        side_effect=UpdateFailed("recorder unavailable"),
    ):
        await boot(hass, mock_config_entry, mock_api, freezer, seed=None)

        assert engine_of(mock_config_entry).data is None
        for key, _category, _icon in AUTOMATION_SWITCHES:
            assert state_of(hass, entity_registry, key) == STATE_OFF

        await switch_to(hass, entity_registry, "auto_vacation", on=True)
        assert state_of(hass, entity_registry, "auto_vacation") == STATE_ON
        assert stored_flags(mock_config_entry)["auto_vacation"] is True


async def test_switches_are_available_when_the_last_refresh_failed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A failed engine or scheduler refresh never makes an opt-in unavailable.

    Every other entity in the integration is gated on its coordinator's last
    update, and the default coordinator-entity behaviour would drop these three
    to ``unavailable`` with it. They deliberately override that: the flags live
    in the config entry, not in the cloud, so an outage must never strand an
    automation the owner wants to switch off.
    """
    await boot(hass, mock_config_entry, mock_api, freezer, seed=_result())
    await switch_to(hass, entity_registry, "smart_regeneration", on=True)

    engine_of(mock_config_entry).async_set_update_error(UpdateFailed("no statistics"))
    await hass.async_block_till_done()
    scheduler = scheduler_of(mock_config_entry)
    scheduler.async_set_update_error(UpdateFailed("automation state unreadable"))
    await hass.async_block_till_done()

    assert scheduler.last_update_success is False
    assert state_of(hass, entity_registry, "smart_regeneration") == STATE_ON
    for key in ("vacation_deferral", "auto_vacation"):
        assert state_of(hass, entity_registry, key) == STATE_OFF

    # Still writable in that state: the flag is local, so nothing about a failed
    # refresh may block turning an automation off.
    await switch_to(hass, entity_registry, "smart_regeneration", on=False)
    assert state_of(hass, entity_registry, "smart_regeneration") == STATE_OFF
    assert stored_flags(mock_config_entry)["smart_regeneration"] is False


async def test_switches_are_available_while_the_device_is_offline(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An offline softener does not take the automation opt-ins with it."""
    await boot(hass, mock_config_entry, mock_api, freezer, seed=_result())

    coordinator = mock_config_entry.runtime_data.coordinators[TEST_DEVICE_ID]
    coordinator.async_set_updated_data(replace(coordinator.data, is_online=False))
    await hass.async_block_till_done()

    assert coordinator.device_online is False
    for key, _category, _icon in AUTOMATION_SWITCHES:
        assert state_of(hass, entity_registry, key) != STATE_UNAVAILABLE


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


async def test_automation_switches_snapshot(  # noqa: PLR0913 - standard HA snapshot-test fixture set
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The three automation switches match their registry + state snapshot.

    Restricted to this family on purpose: the peer platform suites already
    snapshot everything else. The engine is seeded first, so the captured
    baseline is the honest fresh-entry one — all three off, the scheduler
    reporting that it stood down because the opt-in is off.
    """
    await boot(hass, mock_config_entry, mock_api, freezer, seed=_result())

    for key, _category, _icon in AUTOMATION_SWITCHES:
        entity_id = entity_id_of(entity_registry, key)
        assert entity_registry.async_get(entity_id) == snapshot(
            name=f"{entity_id}-entry"
        )
        assert hass.states.get(entity_id) == snapshot(name=f"{entity_id}-state")
