"""Cross-cutting Phase-7 smoke test — the analytics tier inside a real boot.

The dedicated Phase-7 suites drive the pure layers (series, baseline, detectors),
the engine and the five entities in isolation. This file, like
:mod:`tests.test_phase5_smoke` and :mod:`tests.test_phase6_smoke`, boots the
integration exactly as Home Assistant would — every platform forwarded, a real
recorder behind ``recorder_mock``, the captured cloud payloads behind
``aioresponses`` — and checks that the detection tier hangs together with
everything that came before it:

* the five analytics entities join the Phase-6 inventory rather than displacing
  any of it (37 sensors and 14 binary sensors on the dev device), and the whole
  startup pipeline really runs: the statistics backfill lands its 405 rows and
  the engine completes a pass over the series it reads back;
* the verdicts over the replayed real history are the quiet ones pinned here —
  no leak (zero consecutive nights, last verdict 2026-07-27), no anomaly,
  no vacation, a 35 gal forecast resting on the device's own fresh weekday
  average, and a night-flow sensor reporting a hard zero for that night;
* nothing shouts: no urgent-leak repair issue is filed, and not one analytics
  transition event reaches the bus during a boot — the first pass has no previous
  verdict to flip against, so an event here would be an invented alarm;
* the daily 07:35 device-local trigger is armed by the pipeline, so the next
  morning really produces fresh cloud traffic and a fresh verdict.

Time is frozen throughout — at 12:30 Europe/Warsaw, the device's own zone, so the
whole captured history lies in the past and the measured pins apply —
and the stored access token is re-minted against that frozen clock so the auth
manager never reaches for a refresh route.

Two mechanics are inherited from the Phase-5 smoke and the statistics-coordinator
suite. :data:`~custom_components.aquahome.const.BACKFILL_REQUEST_PACING_SECONDS`
is patched to zero module-wide because the event loop's clock is frozen with
everything else, so a genuine ``asyncio.sleep`` would never wake and any paced
backfill session — the boot's own *and* the scheduled overnight one — would
deadlock rather than merely be slow. And the recorder is always drained before
its rows are read back, because the import is a task queued into the recorder
thread rather than a completed write (see :func:`_settled_result`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, cast
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_ACCESS_TOKEN, STATE_OFF
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    statistics_during_period,
)

from custom_components.aquahome.analytics.engine import AquaHomeAnalyticsEngine
from custom_components.aquahome.const import (
    DOMAIN,
    EVENT_AQUAHOME,
    EVENT_TYPE_LEAK_CLEARED,
    EVENT_TYPE_LEAK_SUSPECTED,
    EVENT_TYPE_USAGE_ANOMALY,
    EVENT_TYPE_USAGE_ANOMALY_CLEARED,
    EVENT_TYPE_VACATION_ENDED,
    EVENT_TYPE_VACATION_STARTED,
)
from custom_components.aquahome.coordinator import AquaHomeRuntimeData
from tests.conftest import (
    TEST_DEVICE_ID,
    add_datapoint_graph_routes,
    add_device_routes,
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

    from custom_components.aquahome.analytics.model import AnalyticsResult

#: Slug derived from the fixture serial ``7384243-20203-1120`` (see entity.py).
SLUG: Final = "7384243_20203_1120"

#: The device's own zone, as reported by its captured ``tz_id`` property.
DEVICE_TZ: Final = ZoneInfo("Europe/Warsaw")

#: The instant every clock in this module reads: 12:30 Europe/Warsaw on the last
#: day of the captured history — the instant the pins below were measured at.
FROZEN_NOW: Final = datetime(2026, 7, 27, 10, 30, tzinfo=dt_util.UTC)

#: Entities the captured dev fixtures create, per platform domain. The sensor and
#: binary-sensor totals carry the analytics tier: 35 + ``usage_forecast`` +
#: ``night_flow``, and 11 + the three detection binaries. The switches are the
#: three Phase-8 automation opt-ins, created for every device.
EXPECTED_ENTITIES: Final[dict[str, int]] = {
    "sensor": 37,
    "binary_sensor": 14,
    "event": 1,
    "button": 6,
    "select": 15,
    "switch": 3,
}

#: The five entities the analytics tier itself contributes.
ANALYTICS_ENTITIES: Final[tuple[tuple[str, str], ...]] = (
    ("sensor", "usage_forecast"),
    ("sensor", "night_flow"),
    ("binary_sensor", "leak_suspected"),
    ("binary_sensor", "usage_anomaly"),
    ("binary_sensor", "vacation_detected"),
)

#: Rows the Phase-5 backfill imports from the captured fixtures — the very series
#: the engine reads back as its meter history.
EXPECTED_ROWS: Final = 405

#: Nights the detector window covers on the replayed history, and how they are
#: classified: every assessable night is dry, four are masked by a regeneration.
EXPECTED_NIGHTS: Final = 35
EXPECTED_NO_LEAK_NIGHTS: Final = 31
EXPECTED_MASKED_NIGHTS: Final = 4

#: The freshest night the classifier reaches a verdict on, ISO-rendered.
LAST_VERDICT_NIGHT: Final = "2026-07-27"

#: Forecast for Tuesday 2026-07-28 off the device's own (fresh) weekday slot:
#: 35 gal, 132 L, with a 3-sigma band of 91 L and a one-person household.
FORECAST_GALLONS: Final = 35.0
FORECAST_LITERS: Final = 132
FORECAST_BAND_LITERS: Final = 91

#: Trailing unoccupied streak on the replayed history: zero — the newest
#: noon-day (the return morning, four draws) is unjudgeable without its
#: closing bound and breaks the streak before the two genuinely-away days.
EXPECTED_VACATION_DAYS: Final = 0

#: Repair issue id the urgent-leak nudge would use for the fixture device.
LEAK_ISSUE_ID: Final = f"leak_urgent_{SLUG}"

#: Every bus event type the analytics engine can fire.
ANALYTICS_EVENT_TYPES: Final = frozenset(
    {
        EVENT_TYPE_LEAK_SUSPECTED,
        EVENT_TYPE_LEAK_CLEARED,
        EVENT_TYPE_USAGE_ANOMALY,
        EVENT_TYPE_USAGE_ANOMALY_CLEARED,
        EVENT_TYPE_VACATION_STARTED,
        EVENT_TYPE_VACATION_ENDED,
    }
)

#: The next device-local analytics run after :data:`FROZEN_NOW` — 07:35 the
#: following morning in the device's zone, which is 05:35 UTC. Hard-coded so
#: a mistimed arming (HA-local, mid-MNF-window) cannot re-derive itself green.
NEXT_RUN: Final = datetime(2026, 7, 28, 5, 35, tzinfo=UTC)


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

    Mirrors the reference device's ~130-day hourly retention: a window inside the
    captured July serves real readings, anything older serves the zero-filled
    shape the cloud really returns past the retention floor (which is what stops
    the coordinator walking backwards for ever).
    """
    if query.get("start", "").startswith("2026-07"):
        return load_fixture("graph-meter-hourly.json")
    return load_fixture("graph-meter-hourly-empty.json")


def _add_routes(mock: aioresponses) -> None:
    """Register every cloud route a full Phase-7 boot hits."""
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


async def _boot(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Freeze the clock, boot every platform, and settle the startup pipeline.

    The stored access token is re-minted against the frozen clock so the auth
    manager never decides it is stale — which has to happen between adding the
    entry and setting it up, so the shared ``setup_integration`` helper is
    unrolled here rather than called. The backfill-then-analytics pipeline is a
    background task deliberately kept off the setup path, so settling it needs
    the background-aware wait, and the recorder needs its own.
    """
    freezer.move_to(FROZEN_NOW)
    _add_routes(mock)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_ACCESS_TOKEN: make_access_token()}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)


async def _settled_result(
    hass: HomeAssistant, entry: MockConfigEntry
) -> AnalyticsResult:
    """Return the engine's verdict over the fully committed imported series.

    The statistics coordinator drains the recorder behind its own import, so
    the boot pipeline's pass already sees every row — the boot-pass assertions
    in the first smoke test prove exactly that. This helper only re-settles
    and recomputes so later tests stay independent of pass ordering: the
    engine is stateless by design, so the extra pass recomputes the same
    result.
    """
    await async_wait_recording_done(hass)
    engine = _engine(entry)
    await engine.async_refresh()
    await hass.async_block_till_done()
    result = engine.data
    assert result is not None
    assert engine.last_update_success is True
    return result


def _engine(entry: MockConfigEntry) -> AquaHomeAnalyticsEngine:
    """Return the fixture device's analytics engine from the runtime data."""
    runtime = entry.runtime_data
    assert isinstance(runtime, AquaHomeRuntimeData)
    engine = runtime.analytics_engines[TEST_DEVICE_ID]
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


def _verdict_counts(result: AnalyticsResult) -> dict[str, int]:
    """Return how many nights of a result carry each verdict."""
    counts: dict[str, int] = {}
    for night in result.nights:
        counts[str(night.verdict)] = counts.get(str(night.verdict), 0) + 1
    return counts


def _graph_request_count(mock: aioresponses) -> int:
    """Return how many datapoint-graph requests have been intercepted."""
    return sum(
        len(calls)
        for (method, url), calls in mock.requests.items()
        if method == "GET" and url.path.endswith("/graph")
    )


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


# ---------------------------------------------------------------------------
# Full boot: the startup pipeline end to end
# ---------------------------------------------------------------------------


async def test_full_boot_imports_history_then_computes_analytics(  # noqa: PLR0913 - one fixture per faked subsystem
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The five analytics entities join the inventory and the pipeline runs."""
    await _boot(hass, mock_config_entry, mock_api, freezer)

    assert _entry_state(mock_config_entry) is ConfigEntryState.LOADED

    # The analytics tier adds to the Phase-6 inventory; it displaces nothing.
    assert _entities_by_domain(entity_registry, mock_config_entry) == EXPECTED_ENTITIES
    for domain, key in ANALYTICS_ENTITIES:
        registered = entity_registry.async_get(_entity_id(entity_registry, domain, key))
        assert registered is not None
        assert registered.disabled_by is None

    # The pipeline's first half really landed the history in the recorder ...
    runtime = mock_config_entry.runtime_data
    assert isinstance(runtime, AquaHomeRuntimeData)
    statistics = runtime.statistics_coordinators[TEST_DEVICE_ID]
    assert statistics.last_update_success is True
    assert await _stored_row_count(hass, statistics.statistic_id) == EXPECTED_ROWS

    # ... and its second half completed a pass without an error — and that
    # BOOT pass itself already saw the imported rows (the statistics
    # coordinator drains the recorder behind its import; without that barrier
    # this pass raced the import and judged an empty series). Asserting on
    # engine.data here, before any re-refresh, is what makes the barrier's
    # absence observable.
    engine = _engine(mock_config_entry)
    assert engine.last_update_success is True
    booted = engine.data
    assert booted is not None
    assert _verdict_counts(booted) == {
        "no_leak": EXPECTED_NO_LEAK_NIGHTS,
        "masked": EXPECTED_MASKED_NIGHTS,
    }
    assert booted.leak.active is False
    assert _state(hass, entity_registry, "binary_sensor", "leak_suspected") == STATE_OFF

    # Over that imported series the classifier judges every night of its window.
    result = await _settled_result(hass, mock_config_entry)
    assert len(result.nights) == EXPECTED_NIGHTS
    assert _verdict_counts(result) == {
        "no_leak": EXPECTED_NO_LEAK_NIGHTS,
        "masked": EXPECTED_MASKED_NIGHTS,
    }
    assert result.grid.hourly_samples > 0


# ---------------------------------------------------------------------------
# Full boot: the verdicts a quiet real history deserves
# ---------------------------------------------------------------------------


async def test_full_boot_detects_nothing_on_the_real_history(  # noqa: PLR0913 - one fixture per faked subsystem
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    issue_registry: ir.IssueRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A quiet real history produces quiet detectors, no issue and no events."""
    bus_events: list[Event[Any]] = []

    @callback
    def _capture(event: Event[Any]) -> None:
        """Collect fired bus events synchronously, preserving fire order."""
        bus_events.append(event)

    # Registered before setup: an analytics event fired during a boot would be an
    # alarm invented out of an absent previous verdict.
    hass.bus.async_listen(EVENT_AQUAHOME, _capture)

    await _boot(hass, mock_config_entry, mock_api, freezer)
    result = await _settled_result(hass, mock_config_entry)

    # -- No leak: every classified night of the replayed history is dry -------
    assert _state(hass, entity_registry, "binary_sensor", "leak_suspected") == STATE_OFF
    leak = _attributes(hass, entity_registry, "binary_sensor", "leak_suspected")
    assert leak["consecutive_nights"] == 0
    assert leak["tier"] is None
    assert leak["rate_liters_per_hour"] is None
    assert leak["persistent_flow"] is False
    assert leak["last_verdict_night"] == LAST_VERDICT_NIGHT
    # The regeneration history covers the assessed window, so a permanent "off"
    # is a real all-clear rather than a detector that is unable to judge.
    assert leak["masking_coverage"] is True

    # -- No anomaly, and two low days is one short of a vacation --------------
    assert _state(hass, entity_registry, "binary_sensor", "usage_anomaly") == STATE_OFF
    anomaly = _attributes(hass, entity_registry, "binary_sensor", "usage_anomaly")
    assert anomaly["reasons"] == []
    assert anomaly["drift_alarm"] is False
    assert anomaly["drift_cusum"] is False
    assert anomaly["drift_ewma"] is False

    assert (
        _state(hass, entity_registry, "binary_sensor", "vacation_detected") == STATE_OFF
    )
    vacation = _attributes(hass, entity_registry, "binary_sensor", "vacation_detected")
    assert vacation["consecutive_days"] == EXPECTED_VACATION_DAYS

    # -- The forecast rests on the device's own fresh weekday average ---------
    assert result.forecast.gallons == pytest.approx(FORECAST_GALLONS)
    forecast_state = _state(hass, entity_registry, "sensor", "usage_forecast")
    assert float(forecast_state) > 0.0
    forecast = _attributes(hass, entity_registry, "sensor", "usage_forecast")
    assert forecast["source"] == "device_average"
    assert forecast["liters"] == FORECAST_LITERS
    assert forecast["band_liters"] == FORECAST_BAND_LITERS
    assert forecast["weekday"] == "tuesday"
    assert forecast["persons"] == 1

    # -- The night-flow evidence behind the leak verdict: a hard zero ---------
    assert float(_state(hass, entity_registry, "sensor", "night_flow")) == 0.0
    night_flow = _attributes(hass, entity_registry, "sensor", "night_flow")
    assert night_flow["verdict"] == "no_leak"
    assert night_flow["night"] == LAST_VERDICT_NIGHT

    # -- Nothing shouted: no repair issue, no transition event ---------------
    assert issue_registry.async_get_issue(DOMAIN, LEAK_ISSUE_ID) is None
    analytics_events = [
        event for event in bus_events if event.data.get("type") in ANALYTICS_EVENT_TYPES
    ]
    assert analytics_events == []


# ---------------------------------------------------------------------------
# Full boot: the daily trigger the pipeline arms behind it
# ---------------------------------------------------------------------------


async def test_full_boot_arms_the_daily_device_local_run(  # noqa: PLR0913 - one fixture per faked subsystem
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The next 07:35 in the device's zone refreshes statistics and recomputes."""
    await _boot(hass, mock_config_entry, mock_api, freezer)

    engine = _engine(mock_config_entry)
    booted_result = engine.data
    assert booted_result is not None
    requests_after_boot = _graph_request_count(mock_api)
    assert requests_after_boot > 0

    # 07:35 Europe/Warsaw the next morning — the device's zone, which is where a
    # Home-Assistant-local time trigger would have fired at the wrong hour.
    freezer.move_to(NEXT_RUN)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    # The scheduled run refreshes the statistics before recomputing, so both
    # halves are observable: fresh cloud traffic and a brand-new result object
    # (only the analytics trigger can produce the latter).
    assert _graph_request_count(mock_api) > requests_after_boot
    assert engine.last_update_success is True
    result = engine.data
    assert result is not None
    assert result is not booted_result
    assert result.computed_at == NEXT_RUN

    # And the recomputation is the real thing, over the imported history.
    assert _state(hass, entity_registry, "binary_sensor", "leak_suspected") == STATE_OFF
    assert float(_state(hass, entity_registry, "sensor", "night_flow")) == 0.0
