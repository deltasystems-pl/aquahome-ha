"""Cross-cutting Phase-8 smoke test — the automation tier inside a real boot.

The dedicated Phase-8 suites drive the pieces in isolation: the scheduler's
decision matrix, the four actions, the three switches, the repair flows. This
file, like :mod:`tests.test_phase7_smoke` whose boot mechanics it copies,
starts the integration exactly as Home Assistant would — every platform
forwarded, a real recorder behind ``recorder_mock``, the captured cloud
payloads behind ``aioresponses`` — and checks that the opt-in automation tier
is *wired into* that boot rather than merely importable:

* the three automation switches join the Phase-7 inventory rather than
  displacing any of it (66 entities now, 63 + 3), all three registry-enabled
  and all three **off** on a fresh entry — the tier is opt-in by construction,
  so a boot that turns anything on is a breach of the opt-in guarantee;
* all four actions are registered on the domain, and *only* those four;
* one scheduler exists per device, its published state is the all-off default,
  and the analytics pass the boot runs really reaches it: the scheduler records
  its honest ``skipped_off`` verdict against the frozen clock, which is the one
  observable proof the engine listener was subscribed before the pipeline ran;
* a restart of an entry whose options already carry the flags brings the
  switches back **on** with their deferral bookkeeping intact, and the boot's
  own verdict-writing does not wipe the user's persisted opt-ins;
* nothing acts and nothing shouts: no command reaches the cloud during either
  boot, no automation event reaches the bus, and no ``aquahome`` logger emits
  at ERROR;
* the Phase-7 pipeline underneath is untouched — the statistics backfill still
  lands its 405 rows, and the nightly 07:35 device-local run still recomputes
  *and* drives a fresh scheduler verdict.

Time is frozen throughout at 12:30 Europe/Warsaw — the device's own zone, the
instant Phase 7's measured pins were taken at — and the stored access token is
re-minted against that frozen clock so the auth manager never reaches for a
refresh route.

The two inherited mechanics are unchanged from the Phase-7 smoke:
:data:`~custom_components.aquahome.const.BACKFILL_REQUEST_PACING_SECONDS` is
patched to zero module-wide (a genuine ``asyncio.sleep`` would never wake under
the freezer), and the recorder is drained before its rows are read back.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, cast
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_ACCESS_TOKEN, STATE_OFF, STATE_ON
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    statistics_during_period,
)
from yarl import URL

from custom_components.aquahome.analytics.engine import AquaHomeAnalyticsEngine
from custom_components.aquahome.automation_state import AutomationState
from custom_components.aquahome.const import (
    DEFERRAL_SOURCE_MANUAL,
    DOMAIN,
    EVENT_AQUAHOME,
    EVENT_TYPE_LEAK_WHILE_AWAY,
    EVENT_TYPE_REGEN_DEFERRAL_EXPIRED,
    EVENT_TYPE_REGEN_DEFERRED,
    EVENT_TYPE_REGEN_SCHEDULED,
    OPTION_AUTOMATION,
    SERVICE_ANALYZE_USAGE,
    SERVICE_GET_USAGE_FORECAST,
    SERVICE_SCHEDULE_REGENERATION,
    SERVICE_SET_VACATION_MODE,
)
from custom_components.aquahome.coordinator import AquaHomeRuntimeData
from custom_components.aquahome.scheduler import (
    DECISION_SKIPPED_DEFERRAL,
    DECISION_SKIPPED_OFF,
    AquaHomeRegenScheduler,
)
from tests.conftest import (
    TEST_DEVICE_ID,
    add_datapoint_graph_routes,
    add_device_routes,
    command_url,
    load_fixture,
    make_access_token,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from aioresponses import aioresponses
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.components.recorder.core import Recorder
    from homeassistant.core import Event, HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

#: Slug derived from the fixture serial ``7384243-20203-1120`` (see entity.py).
SLUG: Final = "7384243_20203_1120"

#: The instant every clock in this module reads: 12:30 Europe/Warsaw on the last
#: day of the captured history, matching the Phase-7 smoke exactly.
FROZEN_NOW: Final = datetime(2026, 7, 27, 10, 30, tzinfo=UTC)

#: The next device-local analytics run after :data:`FROZEN_NOW` — 07:35 the
#: following morning in Europe/Warsaw, which is 05:35 UTC.
NEXT_RUN: Final = datetime(2026, 7, 28, 5, 35, tzinfo=UTC)

#: Entities the captured dev fixtures create, per platform domain. Everything
#: but ``switch`` is the Phase-7 inventory verbatim; the three switches are the
#: Phase-8 automation opt-ins, created unconditionally for every device.
EXPECTED_ENTITIES: Final[dict[str, int]] = {
    # 37 + the water-flow and live-mode-status additions
    "sensor": 39,
    "binary_sensor": 14,
    "event": 1,
    "button": 6,
    "select": 15,
    # automation opt-ins + live-mode controls
    "switch": 6,
    # the two live-mode budget knobs
    "number": 2,
}

#: Total entities a full boot on the dev fixtures registers. The per-domain map
#: above is the authority; this is the same inventory summed, kept as its own
#: pin so a platform that silently stops registering anything is caught even if
#: another one grows by the same amount. (The earlier "63 + 3 = 66" estimate
#: understates the measured Phase-7 inventory of 73.)
# 76 pre-live entities + 3 live switches + 2 budget numbers + 2 live sensors.
EXPECTED_TOTAL: Final = 83

#: The three automation switches, by unique-id suffix, in creation order.
AUTOMATION_SWITCH_KEYS: Final[tuple[str, ...]] = (
    "vacation_deferral",
    "auto_vacation",
    "smart_regeneration",
)

#: Every action the integration registers on its domain — and nothing else.
EXPECTED_SERVICES: Final = frozenset(
    {
        SERVICE_ANALYZE_USAGE,
        SERVICE_GET_USAGE_FORECAST,
        SERVICE_SET_VACATION_MODE,
        SERVICE_SCHEDULE_REGENERATION,
    }
)

#: Every bus event type the automation tier can fire. None of them may reach the
#: bus during a boot: the switches are off, so the tier has nothing to announce.
AUTOMATION_EVENT_TYPES: Final = frozenset(
    {
        EVENT_TYPE_LEAK_WHILE_AWAY,
        EVENT_TYPE_REGEN_SCHEDULED,
        EVENT_TYPE_REGEN_DEFERRED,
        EVENT_TYPE_REGEN_DEFERRAL_EXPIRED,
    }
)

#: Rows the Phase-5 backfill imports from the captured fixtures.
EXPECTED_ROWS: Final = 405

#: How long the restored deferral of the second-boot test has been running when
#: the frozen clock reads :data:`FROZEN_NOW`.
RESTORED_DEFERRAL_DAYS: Final = 3


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_recorder_before_hass(recorder_db_url: str) -> None:
    """Prepare the recorder database before Home Assistant is created.

    ``recorder_db_url`` refuses to run once a ``hass`` instance exists, and the
    suite-wide autouse ``enable_custom_integrations`` fixture pulls ``hass`` in
    ahead of everything else. Overriding this hook — which the ``hass`` fixture
    itself depends on, exactly as its upstream docstring invites recorder tests
    to do — puts the database back in front of it.
    """


@pytest.fixture(autouse=True)
def _instant_pacing() -> Iterator[None]:
    """Collapse the backfill's inter-request pacing for the whole module.

    The pacing is a real ``asyncio.sleep`` on the event-loop clock, which the
    freezer never advances, so a paced session would idle for ever rather than
    merely be slow. It is patched for the whole module because the *scheduled*
    overnight run backfills again, long after the boot's own session finished.
    """
    with patch(
        "custom_components.aquahome.statistics.BACKFILL_REQUEST_PACING_SECONDS", 0
    ):
        yield


# ---------------------------------------------------------------------------
# Route + boot helpers
# ---------------------------------------------------------------------------


def _hourly_payload(query: Mapping[str, str]) -> dict[str, Any]:
    """Return the hourly graph payload for one requested window.

    Mirrors the reference device's ~130-day hourly retention: a window inside
    the captured July serves real readings, anything older serves the
    zero-filled shape the cloud really returns past the retention floor.
    """
    if query.get("start", "").startswith("2026-07"):
        return load_fixture("graph-meter-hourly.json")
    return load_fixture("graph-meter-hourly-empty.json")


def _add_routes(mock: aioresponses) -> None:
    """Register every cloud route a full Phase-8 boot may hit.

    The command route is registered even though a quiet boot must never use it:
    an unexpected command then shows up as a *counted call* the assertions can
    name, rather than as an opaque connection error deep in a background task.
    """
    add_device_routes(mock)
    add_datapoint_graph_routes(
        mock,
        by_period={
            "year": load_fixture("graph-meter-yearly.json"),
            "month": load_fixture("graph-meter-monthly.json"),
            "day": load_fixture("graph-meter-daily.json"),
            "hour": _hourly_payload,
        },
    )
    mock.put(command_url(), payload={"result": "ok"}, repeat=True)


async def _boot(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock: aioresponses,
    freezer: FrozenDateTimeFactory,
    options: dict[str, Any] | None = None,
) -> None:
    """Freeze the clock, boot every platform, and settle the startup pipeline.

    The stored access token is re-minted against the frozen clock so the auth
    manager never decides it is stale — which has to happen between adding the
    entry and setting it up, so the shared ``setup_integration`` helper is
    unrolled here rather than called, and ``options`` (the persisted automation
    flags a restart restores from) is written through the same call. The
    backfill-then-analytics pipeline is a background task deliberately kept off
    the setup path, so settling it needs the background-aware wait, the
    recorder needs its own, and the scheduler — whose evaluators the engine
    listener hands to fresh tasks — needs one more pass of the loop after both.
    """
    freezer.move_to(FROZEN_NOW)
    _add_routes(mock)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_ACCESS_TOKEN: make_access_token()},
        options=options if options is not None else entry.options,
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)
    await hass.async_block_till_done()


def _runtime(entry: MockConfigEntry) -> AquaHomeRuntimeData:
    """Return the entry's runtime data, narrowed to the integration's type."""
    runtime = entry.runtime_data
    assert isinstance(runtime, AquaHomeRuntimeData)
    return runtime


def _scheduler(entry: MockConfigEntry) -> AquaHomeRegenScheduler:
    """Return the fixture device's regeneration scheduler."""
    scheduler = _runtime(entry).schedulers[TEST_DEVICE_ID]
    assert isinstance(scheduler, AquaHomeRegenScheduler)
    return scheduler


def _engine(entry: MockConfigEntry) -> AquaHomeAnalyticsEngine:
    """Return the fixture device's analytics engine from the runtime data."""
    engine = _runtime(entry).analytics_engines[TEST_DEVICE_ID]
    assert isinstance(engine, AquaHomeAnalyticsEngine)
    return engine


def _entry_state(entry: MockConfigEntry) -> ConfigEntryState:
    """Return the entry's current lifecycle state.

    Read through a call so each assertion compares the state as it is at that
    point rather than as the type checker last narrowed it.
    """
    return cast("ConfigEntryState", entry.state)


def _entity_id(registry: er.EntityRegistry, domain: str, key: str) -> str:
    """Resolve an entity id from its platform domain and unique-id suffix."""
    entity_id = registry.async_get_entity_id(domain, DOMAIN, f"{SLUG}_{key}")
    assert entity_id is not None, f"{domain} entity {key} was not registered"
    return entity_id


def _state(
    hass: HomeAssistant, registry: er.EntityRegistry, domain: str, key: str
) -> str:
    """Return the state string of the entity with unique-id suffix ``key``."""
    state = hass.states.get(_entity_id(registry, domain, key))
    assert state is not None, f"{domain} entity {key} has no state"
    return state.state


def _attributes(
    hass: HomeAssistant, registry: er.EntityRegistry, domain: str, key: str
) -> dict[str, Any]:
    """Return the state attributes of the entity with unique-id suffix ``key``."""
    state = hass.states.get(_entity_id(registry, domain, key))
    assert state is not None, f"{domain} entity {key} has no state"
    return dict(state.attributes)


def _entities_by_domain(
    registry: er.EntityRegistry, entry: MockConfigEntry
) -> dict[str, int]:
    """Return how many entities the entry registered, per platform domain."""
    counts: dict[str, int] = {}
    for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
        counts[registered.domain] = counts.get(registered.domain, 0) + 1
    return counts


def _stored_flags(entry: MockConfigEntry) -> dict[str, Any]:
    """Return the automation options the entry currently persists for the device."""
    devices = entry.options.get(OPTION_AUTOMATION)
    assert isinstance(devices, dict), "the entry persists no automation options"
    stored = devices.get(TEST_DEVICE_ID)
    assert isinstance(stored, dict), "the fixture device has no persisted flags"
    return dict(stored)


def _command_calls(mock: aioresponses) -> int:
    """Return how many device commands have been sent to the cloud."""
    return len(mock.requests.get(("PUT", URL(command_url())), []))


def _graph_request_count(mock: aioresponses) -> int:
    """Return how many datapoint-graph requests have been intercepted."""
    return sum(
        len(calls)
        for (method, url), calls in mock.requests.items()
        if method == "GET" and url.path.endswith("/graph")
    )


def _aquahome_errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return every ERROR-or-worse message the integration's loggers emitted."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.ERROR
        and record.name.startswith("custom_components.aquahome")
    ]


async def _stored_row_count(hass: HomeAssistant, statistic_id: str) -> int:
    """Return how many rows of the imported series the recorder holds."""
    # A window that predates every captured reading, minted off the frozen clock
    # so nothing here depends on the wall clock.
    window_start = dt_util.utcnow() - timedelta(days=3 * 365)
    stored: dict[str, list[dict[str, Any]]] = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        window_start,
        None,
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    return len(stored.get(statistic_id, []))


def _restored_options() -> dict[str, Any]:
    """Return entry options with all three automation flags already switched on.

    A *manual* deferral three days old: manual because the auto-vacation
    follower is forbidden from releasing a deferral the user started, which is
    what keeps this boot's restored state stable while the household is home.
    Every timestamp is minted off the frozen clock rather than the wall clock.
    """
    return {
        OPTION_AUTOMATION: {
            TEST_DEVICE_ID: {
                "vacation_deferral": True,
                "auto_vacation": True,
                "smart_regeneration": True,
                "deferral_source": DEFERRAL_SOURCE_MANUAL,
                "deferral_started": (
                    FROZEN_NOW - timedelta(days=RESTORED_DEFERRAL_DAYS)
                ).isoformat(),
            }
        }
    }


# ---------------------------------------------------------------------------
# Full boot: the automation tier joins the inventory, switched off
# ---------------------------------------------------------------------------


async def test_full_boot_adds_the_three_opt_in_switches(  # noqa: PLR0913 - one fixture per faked subsystem
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The Phase-8 switches join the Phase-7 inventory, and all three are off."""
    await _boot(hass, mock_config_entry, mock_api, freezer)

    assert _entry_state(mock_config_entry) is ConfigEntryState.LOADED

    counts = _entities_by_domain(entity_registry, mock_config_entry)
    assert counts == EXPECTED_ENTITIES
    assert sum(counts.values()) == EXPECTED_TOTAL

    for key in AUTOMATION_SWITCH_KEYS:
        registered = entity_registry.async_get(
            _entity_id(entity_registry, "switch", key)
        )
        assert registered is not None
        # Registry-enabled by default: an opt-in the owner cannot see is not
        # an opt-in at all.
        assert registered.disabled_by is None
        assert _state(hass, entity_registry, "switch", key) == STATE_OFF

    # A fresh entry carries no bookkeeping behind the off flags ...
    deferral = _attributes(hass, entity_registry, "switch", "vacation_deferral")
    assert deferral["deferral_source"] is None
    assert deferral["deferral_started"] is None
    assert deferral["days_deferred"] is None

    # ... and the scheduler's own verdict is the honest "you never asked".
    decision = _attributes(hass, entity_registry, "switch", "smart_regeneration")
    assert decision["last_decision"] == DECISION_SKIPPED_OFF
    assert decision["last_decision_at"] == FROZEN_NOW.isoformat()


async def test_full_boot_registers_exactly_the_four_actions(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The domain exposes the integration's four actions and no others."""
    await _boot(hass, mock_config_entry, mock_api, freezer)

    assert set(hass.services.async_services_for_domain(DOMAIN)) == EXPECTED_SERVICES


async def test_full_boot_arms_one_scheduler_per_device(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Every device gets a started scheduler seeded with the all-off default."""
    await _boot(hass, mock_config_entry, mock_api, freezer)

    runtime = _runtime(mock_config_entry)
    assert set(runtime.schedulers) == set(runtime.coordinators)

    scheduler = _scheduler(mock_config_entry)
    assert scheduler.device_id == TEST_DEVICE_ID
    assert scheduler.device_slug == SLUG
    # No poll cycle: the scheduler reacts to its two sources and nothing else.
    assert scheduler.update_interval is None

    published = scheduler.data
    assert published is not None
    assert published is scheduler.state
    # Every persisted field is the default; only the runtime-only verdict pair
    # has moved, which is what proves the engine listener was subscribed before
    # the boot pipeline's analytics pass ran.
    assert (
        replace(published, last_decision=None, last_decision_at=None)
        == AutomationState()
    )
    assert published.last_decision == DECISION_SKIPPED_OFF
    assert published.last_decision_at == FROZEN_NOW

    # The verdict write persists the (still default) flags without inventing an
    # opt-in on the user's behalf.
    assert _stored_flags(mock_config_entry) == {
        "vacation_deferral": False,
        "auto_vacation": False,
        "smart_regeneration": False,
        "deferral_source": None,
        "deferral_started": None,
    }


async def test_full_boot_is_silent_and_sends_no_command(  # noqa: PLR0913 - one fixture per faked subsystem
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An opt-out boot touches no device and logs no error."""
    bus_events: list[Event[Any]] = []

    @callback
    def _capture(event: Event[Any]) -> None:
        """Collect fired bus events synchronously, preserving fire order."""
        bus_events.append(event)

    # Registered before setup: an automation event fired while every switch is
    # off would be the tier acting without an opt-in.
    hass.bus.async_listen(EVENT_AQUAHOME, _capture)

    await _boot(hass, mock_config_entry, mock_api, freezer)

    assert _command_calls(mock_api) == 0
    automation_events = [
        event
        for event in bus_events
        if event.data.get("type") in AUTOMATION_EVENT_TYPES
    ]
    assert automation_events == []
    assert _aquahome_errors(caplog) == []


# ---------------------------------------------------------------------------
# Second boot: the persisted opt-ins come back on
# ---------------------------------------------------------------------------


async def test_boot_restores_the_persisted_automation_flags(  # noqa: PLR0913 - one fixture per faked subsystem
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An entry whose options carry the flags boots with all three switches on."""
    await _boot(hass, mock_config_entry, mock_api, freezer, options=_restored_options())

    assert _entry_state(mock_config_entry) is ConfigEntryState.LOADED
    for key in AUTOMATION_SWITCH_KEYS:
        assert _state(hass, entity_registry, "switch", key) == STATE_ON

    # The deferral comes back with its history, so the resin-hygiene cap keeps
    # counting from when the user actually left rather than from the restart.
    deferral = _attributes(hass, entity_registry, "switch", "vacation_deferral")
    assert deferral["deferral_source"] == DEFERRAL_SOURCE_MANUAL
    assert (
        deferral["deferral_started"]
        == (FROZEN_NOW - timedelta(days=RESTORED_DEFERRAL_DAYS)).isoformat()
    )
    assert deferral["days_deferred"] == RESTORED_DEFERRAL_DAYS

    # An active deferral outranks the smart scheduler, and that is what the
    # boot's analytics pass records — not "off", which would mean the restored
    # smart-regeneration flag never reached the decision.
    assert (
        _attributes(hass, entity_registry, "switch", "smart_regeneration")[
            "last_decision"
        ]
        == DECISION_SKIPPED_DEFERRAL
    )

    scheduler = _scheduler(mock_config_entry)
    assert scheduler.state.vacation_deferral is True
    assert scheduler.state.auto_vacation is True
    assert scheduler.state.smart_regeneration is True

    # Writing that verdict must not quietly drop the opt-ins it was written
    # alongside: the persisted subset survives the boot unchanged.
    assert _stored_flags(mock_config_entry) == {
        "vacation_deferral": True,
        "auto_vacation": True,
        "smart_regeneration": True,
        "deferral_source": DEFERRAL_SOURCE_MANUAL,
        "deferral_started": (
            FROZEN_NOW - timedelta(days=RESTORED_DEFERRAL_DAYS)
        ).isoformat(),
    }

    # The captured device reports a *ready* recharge and 185 gal against a 35
    # gal forecast, so neither the deferral enforcement nor the nightly
    # decision has anything to do: a restart must not command the softener.
    assert _command_calls(mock_api) == 0
    assert _aquahome_errors(caplog) == []


# ---------------------------------------------------------------------------
# The Phase-7 pipeline underneath, and the nightly run that drives the tier
# ---------------------------------------------------------------------------


async def test_boot_keeps_the_analytics_pipeline_and_feeds_the_scheduler(  # noqa: PLR0913 - one fixture per faked subsystem
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The backfill still lands, and the nightly run still reaches the scheduler."""
    await _boot(hass, mock_config_entry, mock_api, freezer)

    # -- Phase 7 is intact underneath the new tier ---------------------------
    runtime = _runtime(mock_config_entry)
    statistics = runtime.statistics_coordinators[TEST_DEVICE_ID]
    assert statistics.last_update_success is True
    assert await _stored_row_count(hass, statistics.statistic_id) == EXPECTED_ROWS

    engine = _engine(mock_config_entry)
    assert engine.last_update_success is True
    booted_result = engine.data
    assert booted_result is not None
    assert _state(hass, entity_registry, "binary_sensor", "leak_suspected") == STATE_OFF

    requests_after_boot = _graph_request_count(mock_api)
    assert requests_after_boot > 0

    scheduler = _scheduler(mock_config_entry)
    assert scheduler.state.last_decision_at == FROZEN_NOW

    # -- Nothing fires early: one second short of the trigger changes nothing -
    freezer.move_to(NEXT_RUN - timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert engine.data is booted_result
    assert scheduler.state.last_decision_at == FROZEN_NOW

    # -- 07:35 Europe/Warsaw: the nightly pass recomputes and re-decides ------
    freezer.move_to(NEXT_RUN)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    await hass.async_block_till_done()

    assert _graph_request_count(mock_api) > requests_after_boot
    result = engine.data
    assert result is not None
    assert result is not booted_result
    assert result.computed_at == NEXT_RUN

    # The scheduler rode that pass: a fresh verdict stamped at the trigger is
    # the only way this timestamp can move.
    assert scheduler.state.last_decision == DECISION_SKIPPED_OFF
    assert scheduler.state.last_decision_at == NEXT_RUN
