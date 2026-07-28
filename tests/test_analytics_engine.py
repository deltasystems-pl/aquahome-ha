"""Recorder-backed tests for :class:`AquaHomeAnalyticsEngine` (Phase 7).

Every test here boots the real integration and drives the engine the way Home
Assistant does — through the startup pipeline, the daily device-local trigger,
and the entities and bus events the verdicts surface on — rather than calling
``compute_analytics`` directly (that is the detector suite's job).

The imported long-term statistics are seeded straight into the recorder with
``async_add_external_statistics`` from the replayed real series
(:func:`tests.analytics_traces.real_readings`), one row per reading with
``state`` *and* ``sum`` set to the meter reading itself. The engine rebuilds its
series from the ``sum`` column alone, so this reproduces exactly the meter-read
semantics of the production import (whose ``sum`` is the same series shifted by
its first reading — consecutive sums diff to the water used between readings
either way). The statistics coordinator's own backfill still runs against the
captured datapoint fixtures: it finds the seeded rows as its resume anchor and
recomputes the 30-day overlap from them, which keeps ``sum == state`` because
the anchor's total *is* its reading.

Three mechanics are copied from ``test_statistics_coordinator.py``:

* the ``mock_recorder_before_hass`` hook, so ``recorder_db_url`` is resolved
  before Home Assistant exists;
* the clock frozen to :data:`FROZEN_INSTANT` — the instant the ground truth
  below was measured at — *before* setup, with the stored access token
  re-minted against that frozen clock (with enough headroom to survive the
  multi-day advances the daily-trigger test makes, so no test ever needs a
  refresh route or the machine's wall clock);
* the backfill's inter-request pacing collapsed to zero, since it is a real
  ``asyncio.sleep`` the freezer does not move.

The engine's first pass runs as an entry background task behind the backfill,
so every boot is followed by ``async_block_till_done(wait_background_tasks=True)``
and, when a recorder is present, ``async_wait_recording_done``.
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_ACCESS_TOKEN,
    STATE_OFF,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
    UnitOfVolume,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import VolumeConverter
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.aquahome.analytics.detectors import compute_analytics
from custom_components.aquahome.analytics.model import (
    AnalyticsInputs,
    AnalyticsResult,
    AnomalyState,
    ForecastState,
    GridSummary,
    LeakState,
    NightVerdict,
    VacationState,
)
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
from custom_components.aquahome.statistics import (
    AquaHomeStatisticsCoordinator,
    statistic_id_for,
)
from tests.analytics_traces import real_readings, real_regen_windows
from tests.conftest import (
    TEST_DEVICE_ID,
    add_datapoint_graph_routes,
    add_device_routes,
    load_fixture,
    make_access_token,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator, Mapping
    from contextlib import AbstractContextManager

    from aioresponses import aioresponses
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.components.recorder.core import Recorder
    from homeassistant.components.recorder.statistics import StatisticsRow
    from homeassistant.core import Event, HomeAssistant, State
    from homeassistant.helpers.entity_component import EntityComponent
    from homeassistant.helpers.typing import StateType
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.aquahome.analytics.engine import AquaHomeAnalyticsEngine

#: Slug derived from the fixture serial ``7384243-20203-1120``.
SLUG = "7384243_20203_1120"
#: External statistic id the meter series lives under.
STATISTIC_ID = statistic_id_for(SLUG)

#: The instant the ground truth below was measured at: 12:30 Warsaw on
#: the capture day, just past the newest fixture reading.
FROZEN_INSTANT = "2026-07-27T10:30:00+00:00"
FROZEN_NOW = datetime(2026, 7, 27, 10, 30, tzinfo=UTC)

#: 07:35 Europe/Warsaw (the integration's ``ANALYTICS_RUN_LOCAL_TIME``) on the two
#: mornings after the frozen instant, in UTC — the device is on CEST (UTC+2).
FIRST_DAILY_RUN = datetime(2026, 7, 28, 5, 35, tzinfo=UTC)
SECOND_DAILY_RUN = datetime(2026, 7, 29, 5, 35, tzinfo=UTC)

#: Extra token lifetime so the multi-day advances above never make the auth
#: manager consider the stored access token stale (it would then demand a
#: refresh route). Minted against the frozen clock, never the machine's.
TOKEN_HEADROOM_SECONDS = 3 * 24 * 3600

#: The recorder read window the engine uses (``BASELINE_WINDOW_DAYS``) starts
#: 182 days before "now", which trims the five oldest replayed readings.
ENGINE_WINDOW_START = FROZEN_NOW - timedelta(days=182)

#: UTC window covered by the captured hourly datapoint fixture; requests outside
#: it get the all-zero shape that stops the walk-backward fetch.
HOURLY_WINDOW_START = datetime(2026, 6, 30, 22, tzinfo=UTC)
HOURLY_WINDOW_END = datetime(2026, 7, 27, 10, tzinfo=UTC)

#: Lower bound of the read-back window — predates every imported row.
EPOCH = datetime(2000, 1, 1, tzinfo=UTC)

#: Absolute gallon tolerance for the stored-series comparison (sub-millilitre).
GALLON_TOLERANCE = 1e-3

#: The five analytics entities, by platform.
ANALYTICS_BINARIES = ("leak_suspected", "usage_anomaly", "vacation_detected")
ANALYTICS_SENSORS = ("usage_forecast", "night_flow")

#: Platforms carrying analytics entities (the only two these tests forward).
ANALYTICS_PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]

# --- Measured ground-truth pins, as the engine sees them --------------------
# Leak / anomaly / vacation / forecast reproduce the measured values exactly.
# The grid and point-hour figures differ (46 mature buckets, 438 hourly samples
# and point_hours 1 as first measured) because that measurement was taken on the
# *untrimmed* replay series while the engine reads only ``BASELINE_WINDOW_DAYS``
# back: the trimmed head carries a 241-hour zero-delta interval (2025-10-18 to
# 2025-10-28) whose 240 certain-zero hours are what mature those buckets. Both
# numbers below are the engine-window truth.
EXPECTED_LEAK = LeakState(
    active=False,
    consecutive_nights=0,
    rate_liters_per_hour=None,
    implied_liters_per_day=None,
    tier=None,
    persistent_flow=False,
    last_verdict_night=date(2026, 7, 27),
    masking_coverage=True,
)
EXPECTED_NIGHT_VERDICTS = {NightVerdict.NO_LEAK: 31, NightVerdict.MASKED: 4}
EXPECTED_FORECAST_GALLONS = 35.0
EXPECTED_FORECAST_LITERS = 132.48941244
#: The forecast's *state* string: 35 native gallons converted to the harness's
#: metric display unit. Unrounded — ``suggested_display_precision`` is a
#: frontend hint and never touches the state machine.
EXPECTED_FORECAST_STATE = "132.48941244"
EXPECTED_FORECAST_BAND_LITERS = 90.84988281599999
EXPECTED_ANOMALY_DAY = date(2026, 7, 26)
EXPECTED_EXPECTED_LITERS = 215.768471688
EXPECTED_VACATION_DAYS = 0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_recorder_before_hass(recorder_db_url: str) -> None:
    """Prepare the recorder database before Home Assistant is created.

    ``recorder_db_url`` refuses to run once ``hass`` exists, and the suite-wide
    autouse custom-integration fixture pulls ``hass`` in before any test
    argument is resolved. Home Assistant's test plugin provides this hook — a
    dependency of the ``hass`` fixture itself — for exactly that reason.
    """


@pytest.fixture(autouse=True)
def _instant_pacing() -> Iterator[None]:
    """Collapse the backfill's inter-request pacing for the whole module.

    The pacing is a real ``asyncio.sleep`` on the event-loop clock, which the
    freezer does not advance, so every backfill would idle for real seconds.
    """
    with patch(
        "custom_components.aquahome.statistics.BACKFILL_REQUEST_PACING_SECONDS", 0
    ):
        yield


@pytest.fixture(autouse=True)
def _analytics_platforms() -> Iterator[None]:
    """Forward only the two platforms the analytics entities live on."""
    with patch("custom_components.aquahome.PLATFORMS", ANALYTICS_PLATFORMS):
        yield


@pytest.fixture(autouse=True)
async def _unload_entries(
    hass: HomeAssistant,
    _analytics_platforms: None,
) -> AsyncIterator[None]:
    """Unload any entry a test left loaded, cancelling the daily trigger.

    The engine arms a real ``async_track_point_in_time`` for the next
    device-local run; nothing cancels it on a bare Home Assistant shutdown, so a
    test that boots and never unloads would leave a lingering timer behind (and
    the test plugin fails on those). Depending on the platform patch keeps that
    patch alive until after this teardown runs.
    """
    yield
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# HTTP route helpers
# ---------------------------------------------------------------------------


class HourlyWindowRoute:
    """Answer hourly datapoint requests according to the window they ask for.

    The walk-backward fetch asks for successively older windows and stops at the
    first one without a reading; only the window overlapping the captured July
    fixture carries readings, everything older gets the all-zero capture.
    """

    def __init__(self) -> None:
        """Load the captured hourly readings and the all-zero shape."""
        self.payload = load_fixture("graph-meter-hourly.json")
        self.empty = load_fixture("graph-meter-hourly-empty.json")

    def __call__(self, query: Mapping[str, str]) -> dict[str, Any]:
        """Return the readings covering the requested window."""
        start = datetime.fromisoformat(query["start"])
        end = datetime.fromisoformat(query["end"])
        if end <= HOURLY_WINDOW_START or start >= HOURLY_WINDOW_END:
            return self.empty
        return self.payload


def meter_routes(mock: aioresponses) -> None:
    """Register the datapoint-graph routes one full backfill pass needs."""
    add_datapoint_graph_routes(
        mock,
        by_period={
            "year": load_fixture("graph-meter-yearly.json"),
            "month": load_fixture("graph-meter-monthly.json"),
            "day": load_fixture("graph-meter-daily.json"),
            "hour": HourlyWindowRoute(),
        },
    )


# ---------------------------------------------------------------------------
# Seeding / boot helpers
# ---------------------------------------------------------------------------


def _stored(gallons: float) -> float:
    """Return a captured gallon reading in the unit the series stores."""
    return VolumeConverter.convert(gallons, UnitOfVolume.GALLONS, UnitOfVolume.LITERS)


def meter_metadata() -> StatisticMetaData:
    """Return the external-series metadata the statistics coordinator uses."""
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name="Dom water usage history",
        source=DOMAIN,
        statistic_id=STATISTIC_ID,
        unit_class=VolumeConverter.UNIT_CLASS,
        unit_of_measurement=UnitOfVolume.LITERS,
    )


async def seed_meter_series(hass: HomeAssistant) -> None:
    """Import the replayed real meter series as this device's LTS rows.

    ``sum`` carries the meter reading itself — the column the engine rebuilds
    its series from — and ``state`` the same value, which is what makes the
    coordinator's own overlap recompute reproduce these rows exactly.

    The captured readings are gallons, while a series is stored in the unit the
    installation reads (metric on a test instance), so the rows are seeded
    converted: the engine asks the recorder for gallons on the way out and gets
    the captured figures back.
    """
    rows = [
        StatisticData(start=start, state=_stored(reading), sum=_stored(reading))
        for start, reading in real_readings()
    ]
    async_add_external_statistics(hass, meter_metadata(), rows)
    await async_wait_recording_done(hass)


async def boot(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    *,
    seed: bool = True,
) -> None:
    """Freeze the clock, seed the series, set the entry up, and settle it.

    The stored access token is re-minted against the frozen clock so the auth
    manager never decides it is stale; that has to happen between adding the
    entry and setting it up, which is why the shared ``setup_integration``
    helper is unrolled here.
    """
    freezer.move_to(FROZEN_INSTANT)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_ACCESS_TOKEN: make_access_token(time.time() + TOKEN_HEADROOM_SECONDS),
        },
    )
    if seed:
        await seed_meter_series(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)


async def settle(hass: HomeAssistant) -> None:
    """Wait for the startup pipeline task and, with a recorder, for its writes."""
    await hass.async_block_till_done(wait_background_tasks=True)
    if "recorder" in hass.config.components:
        await async_wait_recording_done(hass)


def engine_of(entry: MockConfigEntry) -> AquaHomeAnalyticsEngine:
    """Return the analytics engine the entry built for the fixture device."""
    engine: AquaHomeAnalyticsEngine = entry.runtime_data.analytics_engines[
        TEST_DEVICE_ID
    ]
    return engine


def result_of(engine: AquaHomeAnalyticsEngine) -> AnalyticsResult:
    """Return the engine's latest result, asserting a pass has completed."""
    result = engine.data
    assert result is not None
    return result


async def stored_sums(hass: HomeAssistant) -> dict[datetime, float]:
    """Return every stored row of the water series as ``{start: sum}``."""
    stored: dict[str, list[StatisticsRow]] = await get_instance(
        hass
    ).async_add_executor_job(
        statistics_during_period,
        hass,
        EPOCH,
        None,
        {STATISTIC_ID},
        "hour",
        None,
        {"sum"},
    )
    sums: dict[datetime, float] = {}
    for row in stored.get(STATISTIC_ID, []):
        total = row.get("sum")
        assert total is not None
        sums[dt_util.utc_from_timestamp(row["start"])] = total
    return sums


# ---------------------------------------------------------------------------
# Entity helpers
# ---------------------------------------------------------------------------


def entity_id_of(hass: HomeAssistant, domain: str, key: str) -> str:
    """Return the entity id of one analytics entity, asserting it exists."""
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{SLUG}_{key}")
    assert entity_id is not None, f"{domain}.{key} was never created"
    return entity_id


def state_of(hass: HomeAssistant, domain: str, key: str) -> State:
    """Return the live state object of one analytics entity."""
    state = hass.states.get(entity_id_of(hass, domain, key))
    assert state is not None
    return state


def native_value(hass: HomeAssistant, key: str) -> StateType:
    """Return an analytics sensor's native value (unit-system independent).

    The forecast is stored in native gallons and displayed in litres for this
    metric harness, so the pinned number is only visible here.
    """
    component: EntityComponent[Any] = hass.data[DATA_INSTANCES]["sensor"]
    entity = component.get_entity(entity_id_of(hass, "sensor", key))
    assert entity is not None
    value: StateType = entity.native_value
    return value


def analytics_states(hass: HomeAssistant) -> dict[str, str]:
    """Return the raw state string of all five analytics entities."""
    states = {
        key: state_of(hass, "binary_sensor", key).state for key in ANALYTICS_BINARIES
    }
    states.update(
        {key: state_of(hass, "sensor", key).state for key in ANALYTICS_SENSORS}
    )
    return states


# ---------------------------------------------------------------------------
# Crafted results (for the transition-event tests)
# ---------------------------------------------------------------------------


def crafted_result(
    *,
    leak: bool | None,
    anomaly: bool | None,
    vacation: bool | None,
) -> AnalyticsResult:
    """Build a minimal result carrying only the three detector verdicts."""
    return AnalyticsResult(
        computed_at=FROZEN_NOW,
        nights=(),
        days=(),
        leak=LeakState(
            active=leak,
            consecutive_nights=2 if leak else 0,
            rate_liters_per_hour=18.5 if leak else None,
            implied_liters_per_day=444.0 if leak else None,
            tier="warning" if leak else None,
            persistent_flow=False,
            last_verdict_night=date(2026, 7, 27),
            masking_coverage=True,
        ),
        anomaly=AnomalyState(
            active=anomaly,
            reasons=("daily_high",) if anomaly else (),
            day=None,
            point_hours=0,
            drift_alarm=False,
            drift_cusum=False,
            drift_ewma=False,
        ),
        vacation=VacationState(
            active=vacation,
            consecutive_days=4 if vacation else 0,
            since=date(2026, 7, 23) if vacation else None,
        ),
        forecast=ForecastState(
            gallons=None,
            liters=None,
            source=None,
            band_liters=None,
            weekday=None,
            persons=None,
        ),
        grid=GridSummary(
            active_hours=(False,) * 168, mature_buckets=0, hourly_samples=0
        ),
    )


def scripted_analytics(
    results: list[AnalyticsResult],
) -> AbstractContextManager[MagicMock]:
    """Patch ``compute_analytics`` to return ``results`` in order.

    The last entry repeats, so a test may drive more refreshes than it scripts.
    """
    queue = list(results)

    def _next(_inputs: AnalyticsInputs) -> AnalyticsResult:
        """Return the next scripted result for one engine pass."""
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return patch(
        "custom_components.aquahome.analytics.engine.compute_analytics",
        side_effect=_next,
    )


def captured_analytics(
    captured: list[AnalyticsInputs],
) -> AbstractContextManager[MagicMock]:
    """Record every pass's inputs while still computing the real result."""

    def _capture(inputs: AnalyticsInputs) -> AnalyticsResult:
        """Remember one pass's inputs, then compute it for real."""
        captured.append(inputs)
        return compute_analytics(inputs)

    return patch(
        "custom_components.aquahome.analytics.engine.compute_analytics",
        side_effect=_capture,
    )


def event_recorder(hass: HomeAssistant) -> list[Event]:
    """Collect every ``aquahome_event`` fired from now on."""
    events: list[Event] = []

    @callback
    def _collect(event: Event) -> None:
        """Append one bus event."""
        events.append(event)

    hass.bus.async_listen(EVENT_AQUAHOME, _collect)
    return events


def detection_events(events: list[Event]) -> list[dict[str, Any]]:
    """Return only the detector transition payloads, alert events dropped."""
    detection_types = {
        EVENT_TYPE_LEAK_SUSPECTED,
        EVENT_TYPE_LEAK_CLEARED,
        EVENT_TYPE_USAGE_ANOMALY,
        EVENT_TYPE_USAGE_ANOMALY_CLEARED,
        EVENT_TYPE_VACATION_STARTED,
        EVENT_TYPE_VACATION_ENDED,
    }
    return [
        dict(event.data)
        for event in events
        if event.data.get("type") in detection_types
    ]


# ---------------------------------------------------------------------------
# Full boot over the imported series
# ---------------------------------------------------------------------------


async def test_startup_pipeline_computes_the_replayed_verdicts(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A full boot turns the imported series into the pinned verdicts.

    This is the whole tier end to end: seeded long-term statistics, the real
    device and datapoint routes, and the backfill-then-analyze background
    pipeline. Every pinned number is the measured ground truth for
    the real July history — most importantly that a real, leak-free month
    produces a leak verdict that is *False* rather than merely unknown.
    """
    add_device_routes(mock_api)
    meter_routes(mock_api)
    events = event_recorder(hass)

    await boot(hass, mock_config_entry, freezer)

    engine = engine_of(mock_config_entry)
    assert engine.last_update_success is True
    result = result_of(engine)
    assert result.computed_at == FROZEN_NOW

    # Night classification over the 35-night detector window.
    assert Counter(night.verdict for night in result.nights) == EXPECTED_NIGHT_VERDICTS
    assert result.leak == EXPECTED_LEAK

    assert result.anomaly.active is False
    assert result.anomaly.reasons == ()
    assert result.anomaly.drift_alarm is False
    assert result.anomaly.day is not None
    assert result.anomaly.day.day == EXPECTED_ANOMALY_DAY

    assert result.vacation.active is False
    assert result.vacation.consecutive_days == EXPECTED_VACATION_DAYS

    assert result.forecast.gallons == EXPECTED_FORECAST_GALLONS
    assert result.forecast.source == "device_average"
    assert result.forecast.weekday == "tuesday"
    assert result.forecast.persons == 1

    # A leak-free replay is a silent one: the listener was attached before
    # setup and no detector event reached it.
    assert detection_events(events) == []


async def test_the_seeded_rows_are_read_back_as_a_meter_series(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Every stored row's ``sum`` is the meter reading the engine parses back.

    The statistics coordinator's own backfill runs over the same fixtures and
    recomputes its 30-day overlap from the seeded rows, so this also proves the
    two shapes agree. The engine then reads only ``BASELINE_WINDOW_DAYS`` back,
    which trims the five oldest replayed readings — pinned because the
    hour-of-week grid is built from whatever survives that window.
    """
    add_device_routes(mock_api)
    meter_routes(mock_api)

    await boot(hass, mock_config_entry, freezer)

    stored = await stored_sums(hass)
    for start, reading in real_readings():
        assert start in stored
        # Stored rows are in the installation's unit; the captures are gallons.
        assert stored[start] == pytest.approx(_stored(reading), abs=GALLON_TOLERANCE)

    result = result_of(engine_of(mock_config_entry))
    in_window = [
        reading for reading in real_readings() if reading[0] >= ENGINE_WINDOW_START
    ]
    assert len(in_window) == 400
    # 198 hours whose usage the windowed series makes certain.
    assert result.grid.hourly_samples == 198
    assert result.grid.mature_buckets == 1
    assert result.anomaly.point_hours == 0


async def test_analytics_entities_render_the_replayed_result(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The five entities project the very same verdicts, attributes included.

    The forecast is native gallons displayed as litres (this harness is
    metric), ``night_flow`` is native litres per hour, and every analytics
    attribute is always present — ``None`` when absent — so templates written
    against the populated case never break.
    """
    add_device_routes(mock_api)
    meter_routes(mock_api)

    await boot(hass, mock_config_entry, freezer)

    assert analytics_states(hass) == {
        "leak_suspected": STATE_OFF,
        "usage_anomaly": STATE_OFF,
        "vacation_detected": STATE_OFF,
        "usage_forecast": EXPECTED_FORECAST_STATE,
        "night_flow": "0.0",
    }
    assert native_value(hass, "usage_forecast") == EXPECTED_FORECAST_GALLONS
    forecast = state_of(hass, "sensor", "usage_forecast")
    assert forecast.attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfVolume.LITERS
    assert float(forecast.state) == pytest.approx(EXPECTED_FORECAST_LITERS, rel=1e-4)
    assert forecast.attributes["liters"] == round(EXPECTED_FORECAST_LITERS)
    assert forecast.attributes["band_liters"] == round(EXPECTED_FORECAST_BAND_LITERS)
    assert forecast.attributes["source"] == "device_average"
    assert forecast.attributes["weekday"] == "tuesday"
    assert forecast.attributes["persons"] == 1

    night_flow = state_of(hass, "sensor", "night_flow")
    assert (
        night_flow.attributes[ATTR_UNIT_OF_MEASUREMENT]
        == UnitOfVolumeFlowRate.LITERS_PER_HOUR
    )
    assert night_flow.attributes["night"] == "2026-07-27"
    assert night_flow.attributes["verdict"] == "no_leak"

    leak = state_of(hass, "binary_sensor", "leak_suspected")
    assert leak.attributes["consecutive_nights"] == 0
    assert leak.attributes["rate_liters_per_hour"] is None
    assert leak.attributes["tier"] is None
    assert leak.attributes["last_verdict_night"] == "2026-07-27"
    assert leak.attributes["persistent_flow"] is False
    assert leak.attributes["masking_coverage"] is True

    anomaly = state_of(hass, "binary_sensor", "usage_anomaly")
    assert anomaly.attributes["reasons"] == []
    assert anomaly.attributes["day"] == EXPECTED_ANOMALY_DAY.isoformat()
    assert anomaly.attributes["expected_liters"] == round(EXPECTED_EXPECTED_LITERS, 1)
    assert anomaly.attributes["ratio_bucket"] == "low"
    assert anomaly.attributes["drift_cusum"] is False
    assert anomaly.attributes["drift_ewma"] is False

    vacation = state_of(hass, "binary_sensor", "vacation_detected")
    assert vacation.attributes["consecutive_days"] == EXPECTED_VACATION_DAYS
    assert vacation.attributes["since"] is None


async def test_inputs_are_gathered_from_the_sibling_coordinators(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """One pass's inputs are the three siblings' data, verbatim.

    The engine owns no data of its own: the series comes from the recorder, the
    masking windows from the activity feed, the cold-start averages from the
    telemetry poll, and the zone plus the import's freshness from the statistics
    coordinator. The captured readings are also the direct proof that the seeded
    ``sum`` column is parsed back as the meter series it was written from.
    """
    add_device_routes(mock_api)
    meter_routes(mock_api)
    captured: list[AnalyticsInputs] = []

    with captured_analytics(captured):
        await boot(hass, mock_config_entry, freezer)

    assert len(captured) == 1
    inputs = captured[0]
    assert inputs.tz_key == "Europe/Warsaw"
    assert inputs.now == FROZEN_NOW
    assert inputs.device_online is True
    assert inputs.statistics_fresh is True

    # The parsed series is the seeded one, trimmed to the read window.
    in_window = tuple(
        reading for reading in real_readings() if reading[0] >= ENGINE_WINDOW_START
    )
    assert [instant for instant, _ in inputs.readings] == [
        instant for instant, _ in in_window
    ]
    for (_, parsed), (_, seeded) in zip(inputs.readings, in_window, strict=True):
        assert parsed == pytest.approx(seeded, abs=GALLON_TOLERANCE)

    # Regeneration history, closed windows, oldest start as the coverage bound.
    assert inputs.regen_windows == real_regen_windows()
    assert inputs.regen_coverage_start == inputs.regen_windows[0][0]

    # Weekday slots in device order (slot 1 = Saturday, Map B) with their
    # change-stamps, plus the overall average — the forecast's cold start.
    assert [slot.average_gal for slot in inputs.weekday_slots] == [
        41.0,
        57.0,
        43.0,
        35.0,
        46.0,
        40.0,
        10.0,
    ]
    assert inputs.weekday_slots[3].deviation_gal == 8.0
    assert inputs.weekday_slots[6].updated_at == datetime(
        2026, 6, 14, 2, 53, 37, tzinfo=UTC
    )
    assert inputs.overall_average.average_gal == 47.0
    assert inputs.overall_average.deviation_gal is None

    # A failed import is passed on honestly: trailing silence in the series may
    # then be import lag rather than an empty house.
    statistics = mock_config_entry.runtime_data.statistics_coordinators[TEST_DEVICE_ID]
    statistics.async_set_update_error(UpdateFailed("cloud unreachable"))
    with captured_analytics(captured):
        await engine_of(mock_config_entry).async_refresh()
        await settle(hass)

    assert captured[-1].statistics_fresh is False


# ---------------------------------------------------------------------------
# No recorder at all
# ---------------------------------------------------------------------------


async def test_analytics_run_without_a_recorder(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Without a recorder the engine still runs, and says so honestly.

    There is no imported series to read, so every detector reports "nothing to
    assess" (``unknown``, never a fabricated all-clear) — while the forecast,
    which needs only the device's own weekday averages, resolves exactly as it
    does with a full history.
    """
    add_device_routes(mock_api)

    await boot(hass, mock_config_entry, freezer, seed=False)

    assert "recorder" not in hass.config.components
    engine = engine_of(mock_config_entry)
    assert engine.last_update_success is True
    result = result_of(engine)

    assert result.leak.active is None
    assert result.anomaly.active is None
    assert result.vacation.active is None
    assert result.grid.hourly_samples == 0
    assert all(
        night.verdict in (NightVerdict.UNASSESSED, NightVerdict.MASKED)
        for night in result.nights
    )

    assert result.forecast.gallons == EXPECTED_FORECAST_GALLONS
    assert result.forecast.source == "device_average"
    # Occupancy needs assessable days, which an empty series cannot offer.
    assert result.forecast.persons is None

    assert analytics_states(hass) == {
        "leak_suspected": STATE_UNKNOWN,
        "usage_anomaly": STATE_UNKNOWN,
        "vacation_detected": STATE_UNKNOWN,
        "usage_forecast": EXPECTED_FORECAST_STATE,
        "night_flow": STATE_UNKNOWN,
    }
    assert native_value(hass, "usage_forecast") == EXPECTED_FORECAST_GALLONS


# ---------------------------------------------------------------------------
# Bus events on verdict transitions
# ---------------------------------------------------------------------------


async def test_verdict_flips_fire_bus_events(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Only genuine boolean flips fire, and they carry the verdict's detail.

    The engine is driven through four passes with scripted results: nothing to
    assess, then a clean all-clear (a ``None`` -> ``False`` transition, which
    must stay silent — an all-clear nobody was warned about is noise), then all
    three detectors tripping, then all three clearing.
    """
    add_device_routes(mock_api)
    passes = [
        crafted_result(leak=None, anomaly=None, vacation=None),
        crafted_result(leak=False, anomaly=False, vacation=False),
        crafted_result(leak=True, anomaly=True, vacation=True),
        crafted_result(leak=False, anomaly=False, vacation=False),
    ]

    with scripted_analytics(passes):
        await boot(hass, mock_config_entry, freezer, seed=False)
        engine = engine_of(mock_config_entry)
        assert result_of(engine).leak.active is None
        events = event_recorder(hass)

        # Pass two: every detector goes from "nothing to assess" to "all clear".
        await engine.async_refresh()
        assert result_of(engine).leak.active is False
        assert detection_events(events) == []

        # Pass three: all three trip.
        await engine.async_refresh()
        raised = detection_events(events)

        # Pass four: all three clear again.
        await engine.async_refresh()
        cleared = detection_events(events)[len(raised) :]

    assert [payload["type"] for payload in raised] == [
        EVENT_TYPE_LEAK_SUSPECTED,
        EVENT_TYPE_USAGE_ANOMALY,
        EVENT_TYPE_VACATION_STARTED,
    ]
    assert raised[0] == {
        "device_id": TEST_DEVICE_ID,
        "device": SLUG,
        "type": EVENT_TYPE_LEAK_SUSPECTED,
        "rate_liters_per_hour": 18.5,
        "tier": "warning",
    }
    assert raised[1]["reasons"] == ["daily_high"]
    assert raised[2]["since"] == "2026-07-23"
    assert raised[2]["consecutive_days"] == 4

    assert [payload["type"] for payload in cleared] == [
        EVENT_TYPE_LEAK_CLEARED,
        EVENT_TYPE_USAGE_ANOMALY_CLEARED,
        EVENT_TYPE_VACATION_ENDED,
    ]
    assert cleared[0]["device"] == SLUG
    assert cleared[0]["tier"] is None
    assert cleared[2]["since"] is None

    # The binaries followed the same flips.
    assert analytics_states(hass)["leak_suspected"] == STATE_OFF


async def test_a_verdict_lost_to_none_fires_nothing(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A verdict falling back to "nothing to assess" is not an all-clear.

    Losing the evidence (an emptied window, a failed import) must never fire a
    cleared event: the leak may well still be running.
    """
    add_device_routes(mock_api)
    passes = [
        crafted_result(leak=True, anomaly=True, vacation=True),
        crafted_result(leak=None, anomaly=None, vacation=None),
    ]

    with scripted_analytics(passes):
        await boot(hass, mock_config_entry, freezer, seed=False)
        engine = engine_of(mock_config_entry)
        events = event_recorder(hass)
        await engine.async_refresh()

    assert result_of(engine).leak.active is None
    assert detection_events(events) == []
    assert analytics_states(hass)["leak_suspected"] == STATE_UNKNOWN


# ---------------------------------------------------------------------------
# The daily device-local trigger
# ---------------------------------------------------------------------------


async def test_daily_trigger_refreshes_statistics_then_recomputes(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """At 07:35 device-local the import is refreshed first, then the verdicts.

    Ordering is the point: the overnight readings the night verdict needs are
    not in the recorder yet on the coordinator's own 12-hour cadence, so the
    engine must pull them in before recomputing. The spy therefore records the
    engine result that was current when the statistics refresh started — still
    the previous one — and the trigger must re-arm for the following morning.
    """
    add_device_routes(mock_api)
    meter_routes(mock_api)

    await boot(hass, mock_config_entry, freezer)
    engine = engine_of(mock_config_entry)
    assert result_of(engine).computed_at == FROZEN_NOW

    original = AquaHomeStatisticsCoordinator.async_refresh
    seen_before: list[datetime | None] = []

    async def _spy(self: AquaHomeStatisticsCoordinator) -> None:
        """Record the engine's state at refresh time, then refresh for real."""
        result = engine.data
        seen_before.append(result.computed_at if result is not None else None)
        await original(self)

    with patch.object(AquaHomeStatisticsCoordinator, "async_refresh", _spy):
        # Nothing may run before 07:35 device-local: a mid-MNF-window arming
        # (or one computed in HA-local time) would already have fired by one
        # second before the pinned instant — the lower bound of the bracket.
        freezer.move_to(FIRST_DAILY_RUN - timedelta(seconds=1))
        async_fire_time_changed(hass)
        await settle(hass)
        assert seen_before == []
        assert result_of(engine).computed_at == FROZEN_NOW

        freezer.move_to(FIRST_DAILY_RUN)
        async_fire_time_changed(hass)
        await settle(hass)

        assert seen_before == [FROZEN_NOW]
        assert result_of(engine).computed_at == FIRST_DAILY_RUN
        assert engine.last_update_success is True

        # Re-armed for the next device-local morning, not a one-shot —
        # and again not a second early.
        freezer.move_to(SECOND_DAILY_RUN - timedelta(seconds=1))
        async_fire_time_changed(hass)
        await settle(hass)
        assert seen_before == [FROZEN_NOW]

        freezer.move_to(SECOND_DAILY_RUN)
        async_fire_time_changed(hass)
        await settle(hass)

    assert seen_before == [FROZEN_NOW, FIRST_DAILY_RUN]
    assert result_of(engine).computed_at == SECOND_DAILY_RUN
    # Two more nights of replay change nothing about a leak-free house.
    assert result_of(engine).leak.active is False


async def test_unload_cancels_the_daily_trigger(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Unloading the entry stops the schedule dead, with no lingering timer.

    A surviving trigger would refresh a coordinator whose client is gone; the
    clock is pushed past the next two run times to prove nothing fires.
    """
    add_device_routes(mock_api)
    meter_routes(mock_api)

    await boot(hass, mock_config_entry, freezer)
    engine = engine_of(mock_config_entry)
    computed_at = result_of(engine).computed_at

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED

    original = AquaHomeStatisticsCoordinator.async_refresh
    calls: list[str] = []

    async def _spy(self: AquaHomeStatisticsCoordinator) -> None:
        """Record any refresh a leaked schedule would have triggered."""
        calls.append(self.device_slug)
        await original(self)

    with patch.object(AquaHomeStatisticsCoordinator, "async_refresh", _spy):
        for instant in (FIRST_DAILY_RUN, SECOND_DAILY_RUN):
            freezer.move_to(instant)
            async_fire_time_changed(hass)
            await settle(hass)

    assert calls == []
    assert result_of(engine).computed_at == computed_at


# ---------------------------------------------------------------------------
# Failure honesty
# ---------------------------------------------------------------------------


async def test_recorder_read_failure_makes_the_entities_unavailable(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A failed statistics read is an honest ``UpdateFailed``, and it recovers.

    The engine talks to no cloud, so the recorder is its only failure mode.
    Serving the previous verdicts as if they were current would be worse than
    going unavailable: a stale "no leak" is exactly the lie this tier must not
    tell.
    """
    add_device_routes(mock_api)
    meter_routes(mock_api)

    await boot(hass, mock_config_entry, freezer)
    engine = engine_of(mock_config_entry)
    healthy = result_of(engine)

    with patch(
        "custom_components.aquahome.analytics.engine.statistics_during_period",
        side_effect=RuntimeError("database is locked"),
    ):
        await engine.async_refresh()

    failed = engine.last_update_success
    assert failed is False
    assert analytics_states(hass) == dict.fromkeys(
        ANALYTICS_BINARIES + ANALYTICS_SENSORS, STATE_UNAVAILABLE
    )

    # The next pass reads the very same rows again and recovers completely.
    await engine.async_refresh()
    await settle(hass)

    recovered = engine.last_update_success
    assert recovered is True
    assert result_of(engine).leak == healthy.leak
    assert analytics_states(hass)["leak_suspected"] == STATE_OFF
    assert native_value(hass, "usage_forecast") == EXPECTED_FORECAST_GALLONS
