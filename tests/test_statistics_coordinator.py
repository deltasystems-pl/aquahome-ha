"""Recorder-backed tests for :class:`AquaHomeStatisticsCoordinator`.

Unlike the pure builder suite, every test here boots the real integration
against an in-memory recorder (``recorder_mock``) and the captured datapoint
graph fixtures faked over ``aioresponses``, then reads the imported series back
out of the recorder with the very same ``statistics_during_period`` call the
coordinator uses for its resume anchor. That proves the whole path — HTTP
window, unit normalization, resolution merge, row algorithm, external-statistics
import — instead of an internal return value.

The expected rows are the independently computed ground truth for the full
fixtures (405 rows spanning 2025-09-14 to 2026-07-27, ending on the live
lifetime counter of 180 529 L = 47 690.7164 gal): the daily capture supplies the
older days at local midnight and the July hourly capture takes over hour by hour
where it has coverage.

Two mechanics recur in every test:

* the clock is frozen to :data:`FROZEN_INSTANT` — just after the newest fixture
  reading — *before* setup, and the stored access token is re-minted against the
  frozen clock so no test depends on the machine's wall clock;
* the first backfill runs as an entry background task, so setup is followed by
  ``async_block_till_done(wait_background_tasks=True)`` and then by
  ``async_wait_recording_done`` before anything is asserted.

The inter-request pacing is collapsed to zero for the whole module: it is real
``asyncio.sleep`` against the event loop clock, which freezegun does not move, so
leaving it in place would cost ~16 s of wall time per full backfill without
testing anything the pacing constant does not already state.
"""

from __future__ import annotations

import asyncio
import copy
import itertools
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import patch

import pytest
from aioresponses import CallbackResult
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    list_statistic_ids,
    statistics_during_period,
)
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import CONF_ACCESS_TOKEN, Platform, UnitOfVolume
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import VolumeConverter
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.aquahome.api.const import API_BASE_URL
from custom_components.aquahome.const import (
    BACKFILL_LANGUAGE,
    DATAPOINT_METER_VALUE_TYPE,
    DATAPOINT_WATER_PROPERTY,
    DOMAIN,
    STATISTICS_UPDATE_INTERVAL,
)
from custom_components.aquahome.statistics import statistic_id_for
from tests.conftest import (
    TEST_DEVICE_ID,
    add_datapoint_graph_routes,
    add_device_routes,
    graph_url,
    load_fixture,
    make_access_token,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from aioresponses import aioresponses
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.components.recorder.core import Recorder
    from homeassistant.components.recorder.statistics import StatisticsRow
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from yarl import URL

    from custom_components.aquahome.statistics import AquaHomeStatisticsCoordinator

#: Slug derived from the fixture serial ``4213377-30105-2242`` (see other suites).
SLUG = "4213377_30105_2242"
#: External statistic id the backfill imports into.
STATISTIC_ID = statistic_id_for(SLUG)

#: Instant every test freezes to: 12:30 Europe/Warsaw on the capture day, just
#: past the newest fixture reading (09:00+02:00), so the whole capture is history.
FROZEN_INSTANT = "2026-07-27T10:30:00+00:00"

#: Lower bound of the read-back window — predates every possible imported row.
EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
#: Statistics fields read back, matching the coordinator's own anchor lookup.
STAT_TYPES = {"state", "sum"}

#: UTC window the captured hourly fixture covers (2026-07-01T00:00+02:00 through
#: 2026-07-27T12:00+02:00). Requests outside it are answered with the all-zero
#: shape, which is the signal that stops the walk-backward hourly fetch.
HOURLY_WINDOW_START = datetime(2026, 6, 30, 22, tzinfo=UTC)
HOURLY_WINDOW_END = datetime(2026, 7, 27, 10, tzinfo=UTC)

# Ground truth for the full fixtures (computed independently from the captures:
# daily readings from 2025-09-14, hourly readings across July 2026, liters
# converted at 3.785411784 L/gal, first reading a zero-delta baseline). The
# figures below are the native gallons the cloud reports; a series is stored in
# the unit the installation reads, so the assertions convert through
# :func:`stored` (the test instance is metric, hence liters).
EXPECTED_ROW_COUNT = 405
FIRST_START = datetime(2025, 9, 13, 22, tzinfo=UTC)
FIRST_STATE = 42122.7621
FIRST_SUM = 0.0
LAST_START = datetime(2026, 7, 27, 7, tzinfo=UTC)
LAST_STATE = 47690.7164
LAST_SUM = 5567.9543


def stored(gallons: float, unit: str = UnitOfVolume.LITERS) -> float:
    """Return a gallon ground-truth figure in the unit the series stores."""
    return VolumeConverter.convert(gallons, UnitOfVolume.GALLONS, unit)


#: Bucket starts of the two July-27 readings a late upload would be missing.
LATE_STARTS = (
    datetime(2026, 7, 27, 6, tzinfo=UTC),
    datetime(2026, 7, 27, 7, tzinfo=UTC),
)

#: 2026-07-27T00:00 Europe/Warsaw — the one bucket a day without hourly coverage
#: and a day with it both land on, so it carries the daily reading (180 526 L)
#: while July 27 is missing and the finer hourly one (180 511 L) once it arrives.
FALLBACK_START = datetime(2026, 7, 26, 22, tzinfo=UTC)
FALLBACK_DAILY_STATE = 47689.9239
FALLBACK_HOURLY_STATE = 47685.9613

#: Absolute tolerance for stored-volume comparisons — below a millilitre.
LITER_TOLERANCE = 1e-3

#: Knobs for the recorder-barrier test. The freezer stops the event loop's own
#: clock, so an asyncio deadline only expires once the frozen clock is stepped
#: past it — a handful of steps here, bounded so a lost deadline fails rather
#: than hangs.
DRAIN_TIMEOUT_SECONDS = 0.2
DRAIN_SPIN_STEP = timedelta(milliseconds=50)
DRAIN_SPIN_PASSES = 100


@pytest.fixture
def mock_recorder_before_hass(recorder_db_url: str) -> None:
    """Prepare the recorder database before Home Assistant is created.

    ``recorder_db_url`` refuses to run once ``hass`` exists, and the suite-wide
    autouse custom-integration fixture pulls ``hass`` in before any test
    argument is resolved. Home Assistant's test plugin provides this hook —
    a dependency of the ``hass`` fixture itself — for exactly that reason.
    """


@pytest.fixture(autouse=True)
def _instant_pacing() -> Iterator[None]:
    """Collapse the backfill's inter-request pacing for the whole module.

    The pacing is a real ``asyncio.sleep`` on the event-loop clock, which the
    freezer does not advance, so a full nine-request backfill would idle for
    sixteen real seconds per test without exercising anything.
    """
    with patch(
        "custom_components.aquahome.statistics.BACKFILL_REQUEST_PACING_SECONDS", 0
    ):
        yield


# ---------------------------------------------------------------------------
# Route helpers
# ---------------------------------------------------------------------------


class HourlyWindowRoute:
    """Answer hourly datapoint requests according to the window they ask for.

    The walk-backward fetch asks for successively older 92-day windows and stops
    at the first one without a single reading, mirroring the cloud's ~130-day
    hourly retention. Only the window overlapping the captured July 2026 fixture
    carries readings here; everything older gets the captured all-zero shape.

    ``payload`` stays swappable so a test can let a reading arrive late between
    two backfill runs.
    """

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        """Serve ``payload`` (the full hourly capture by default) for July 2026."""
        self.payload = payload if payload is not None else hourly_fixture()
        self.empty = load_fixture("graph-meter-hourly-empty.json")

    def __call__(self, query: Mapping[str, str]) -> dict[str, Any]:
        """Return the readings covering the requested window."""
        start = datetime.fromisoformat(query["start"])
        end = datetime.fromisoformat(query["end"])
        if end <= HOURLY_WINDOW_START or start >= HOURLY_WINDOW_END:
            return self.empty
        return self.payload


def hourly_fixture() -> dict[str, Any]:
    """Return a fresh copy of the captured hourly meter readings."""
    return load_fixture("graph-meter-hourly.json")


def meter_routes(
    mock: aioresponses,
    *,
    hourly: HourlyWindowRoute | None = None,
    day: dict[str, Any] | None = None,
    year: list[dict[str, Any]] | None = None,
    seen: list[tuple[dict[str, str], dict[str, str]]] | None = None,
) -> None:
    """Register the probe-shaped datapoint routes of one full backfill.

    The yearly and monthly sweeps answer the depth probe, the daily route covers
    every chunk of the import range (identical readings per chunk, deduplicated
    downstream), and the hourly route dispatches on the requested window. A list
    of yearly payloads is consumed one per request (the last one repeating),
    which is how a probe can answer one backfill run differently than the next.
    """
    add_datapoint_graph_routes(
        mock,
        by_period={
            "year": year
            if year is not None
            else load_fixture("graph-meter-yearly.json"),
            "month": load_fixture("graph-meter-monthly.json"),
            "day": day if day is not None else load_fixture("graph-meter-daily.json"),
            "hour": hourly if hourly is not None else HourlyWindowRoute(),
        },
        seen_requests=seen,
    )


class ArmableMeterRoutes:
    """Serve every backfill fixture until armed, then throttle every request.

    ``aioresponses`` matches routes in registration order and a matched route
    cannot decline, so one backfill run cannot succeed and the next fail through
    two separately registered routes. A single callback whose behaviour is
    flipped between runs can, which is what tests of the rebuild path need.
    """

    def __init__(self) -> None:
        """Start out serving the captured payloads of every period type."""
        self.throttled = False
        self.hourly = HourlyWindowRoute()
        self.by_period = {
            "year": load_fixture("graph-meter-yearly.json"),
            "month": load_fixture("graph-meter-monthly.json"),
            "day": load_fixture("graph-meter-daily.json"),
        }

    def __call__(self, url: URL, **kwargs: Any) -> CallbackResult:
        """Answer one datapoint-graph request."""
        if self.throttled:
            return CallbackResult(
                status=429,
                payload={"code": "ThrottleLimitExceeded", "detail": "slow down"},
            )
        query = dict(url.query.items())
        period = query["period_type"]
        if period == "hour":
            return CallbackResult(payload=self.hourly(query))
        return CallbackResult(payload=self.by_period[period])


class NeverConfirmingTask:
    """A synchronize marker the recorder dequeues and never resolves.

    Stands in for a recorder that stopped, or whose thread died, between the
    import being queued and the marker behind it being reached.
    """

    commit_before = True

    #: Every marker built while this stands in for the real one, so a test can
    #: inspect the future the barrier gave up on.
    built: ClassVar[list[NeverConfirmingTask]] = []

    def __init__(self, future: asyncio.Future[None]) -> None:
        """Keep the future the real marker would have resolved."""
        self.future = future
        NeverConfirmingTask.built.append(self)

    def run(self, instance: Recorder) -> None:
        """Drop the marker, leaving the future pending forever."""


def graph_url_for_period(period: str) -> re.Pattern[str]:
    """Match only the datapoint-graph requests of one period type.

    ``aioresponses`` sorts the query string before matching, so the period can be
    pinned without also pinning the percent-encoded window bounds.
    """
    return re.compile(
        rf"^{re.escape(API_BASE_URL)}/devices/{re.escape(TEST_DEVICE_ID)}"
        rf"/datapoints/{re.escape(DATAPOINT_WATER_PROPERTY)}/graph"
        rf"\?.*period_type={period}.*$"
    )


def zeroed(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a graph payload with every reading zero-filled.

    Zero is the API's "no reading in this bucket" placeholder — responses are
    never empty — so this is the shape a device with no retained history returns.
    """
    document = copy.deepcopy(payload)
    for point in document["data"]:
        point["value"] = 0
    return document


def without_july_27(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the hourly capture with the last day not yet uploaded.

    The bucket rows stay in place with a zero value, exactly as the cloud renders
    a window whose readings have not landed yet.
    """
    document = copy.deepcopy(payload)
    for point in document["data"]:
        if point["label"].startswith("2026-07-27"):
            point["value"] = 0
    return document


# ---------------------------------------------------------------------------
# Setup / read-back helpers
# ---------------------------------------------------------------------------


async def boot(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    *,
    platforms: list[Platform] | None = None,
) -> None:
    """Freeze the clock, set the entry up, and settle the background backfill.

    The stored access token is re-minted against the frozen clock so the auth
    manager never decides it is stale (which would demand a refresh route and
    couple the suite to the machine's wall clock). That has to happen between
    adding the entry and setting it up — ``async_update_entry`` is the only way
    to change entry data — which is why the shared ``setup_integration`` helper
    is unrolled here instead of called. Platforms default to none: only the
    removal test needs a device registry entry.
    """
    freezer.move_to(FROZEN_INSTANT)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_ACCESS_TOKEN: make_access_token()}
    )
    with patch("custom_components.aquahome.PLATFORMS", platforms or []):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)


async def settle(hass: HomeAssistant) -> None:
    """Wait for the background backfill task and for the recorder to commit."""
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)


def coordinator_of(entry: MockConfigEntry) -> AquaHomeStatisticsCoordinator:
    """Return the statistics coordinator the entry created for the fixture device."""
    statistics: AquaHomeStatisticsCoordinator = (
        entry.runtime_data.statistics_coordinators[TEST_DEVICE_ID]
    )
    return statistics


async def stored_rows(hass: HomeAssistant) -> list[StatisticsRow]:
    """Return every stored row of the water series, oldest first."""
    stored = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        EPOCH,
        None,
        {STATISTIC_ID},
        "hour",
        None,
        STAT_TYPES,
    )
    return stored.get(STATISTIC_ID, [])


async def stored_metadata(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Return the recorder's metadata entries for the water series."""
    listed: list[dict[str, Any]] = await get_instance(hass).async_add_executor_job(
        list_statistic_ids, hass, {STATISTIC_ID}
    )
    return listed


def starts(rows: list[StatisticsRow]) -> list[datetime]:
    """Return the UTC bucket starts of ``rows``."""
    return [dt_util.utc_from_timestamp(row["start"]) for row in rows]


def digest(rows: list[StatisticsRow]) -> list[tuple[float, float, float]]:
    """Return ``(start, state, sum)`` triples for exact row-by-row comparison."""
    return [(row["start"], number(row["state"]), number(row["sum"])) for row in rows]


def number(value: float | None) -> float:
    """Return a stored statistics value, asserting the recorder kept one."""
    assert value is not None
    return value


def continuity_gaps(rows: list[StatisticsRow]) -> list[datetime]:
    """Return the bucket starts where the running sum leaves the meter behind.

    A meter series accumulates exactly what the counter advanced, so between any
    two consecutive stored rows the ``sum`` must grow by the same amount as the
    ``state`` (the fixtures contain no counter reset, which is the only case
    where the two legitimately part ways). A series that ends up mixing two
    volume units breaks that in one enormous step.
    """
    return [
        dt_util.utc_from_timestamp(row["start"])
        for previous, row in itertools.pairwise(rows)
        if abs(
            (number(row["sum"]) - number(previous["sum"]))
            - (number(row["state"]) - number(previous["state"]))
        )
        > LITER_TOLERANCE
    ]


# ---------------------------------------------------------------------------
# First run: full history import
# ---------------------------------------------------------------------------


async def test_first_run_imports_the_full_meter_history(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A first backfill imports every retained reading as one external series.

    Also pins the request contract: the depth probe is two coarse sweeps, the
    daily range is chunked at the two DST transitions it spans, the hourly fetch
    walks backwards until a window comes back empty, and every request pins
    ``accept-language: en`` and ``value_type=max`` — even though the account
    itself is Polish, because the response ``units`` string is server-localized.
    """
    seen: list[tuple[dict[str, str], dict[str, str]]] = []
    add_device_routes(mock_api)
    meter_routes(mock_api, seen=seen)
    # The client follows the Home Assistant locale; the backfill must not.
    hass.config.language = "pl"

    await boot(hass, mock_config_entry, freezer)

    coordinator = coordinator_of(mock_config_entry)
    assert coordinator.last_update_success is True
    assert coordinator.statistic_id == STATISTIC_ID

    rows = await stored_rows(hass)
    assert len(rows) == EXPECTED_ROW_COUNT
    assert starts(rows)[0] == FIRST_START
    assert rows[0]["state"] == pytest.approx(stored(FIRST_STATE), abs=LITER_TOLERANCE)
    assert rows[0]["sum"] == pytest.approx(stored(FIRST_SUM), abs=LITER_TOLERANCE)
    assert starts(rows)[-1] == LAST_START
    assert rows[-1]["state"] == pytest.approx(stored(LAST_STATE), abs=LITER_TOLERANCE)
    assert rows[-1]["sum"] == pytest.approx(stored(LAST_SUM), abs=LITER_TOLERANCE)

    # A lifetime counter only ever climbs, and so does its accumulated total.
    states = [number(row["state"]) for row in rows]
    sums = [number(row["sum"]) for row in rows]
    assert states == sorted(states)
    assert sums == sorted(sums)

    metadata = await stored_metadata(hass)
    assert len(metadata) == 1
    assert metadata[0]["source"] == DOMAIN
    assert metadata[0]["has_sum"] is True
    assert metadata[0]["mean_type"] is StatisticMeanType.NONE
    assert metadata[0]["unit_class"] == VolumeConverter.UNIT_CLASS
    assert metadata[0]["statistics_unit_of_measurement"] == UnitOfVolume.LITERS
    assert metadata[0]["statistic_id"] == STATISTIC_ID
    assert metadata[0]["name"] == "Demo water usage history"
    # An external statistic has no entity to convert it for display, so the
    # series is stored in the unit this installation reads — metric here — and
    # the volume unit class lets the user convert it later if they disagree.
    assert metadata[0]["display_unit_of_measurement"] == UnitOfVolume.LITERS

    # Two probe sweeps, the daily range split at both DST transitions, then the
    # hourly walk: the July window (two chunks) plus one older window that comes
    # back zero-filled and stops the walk.
    assert [query["period_type"] for query, _ in seen] == [
        "year",
        "month",
        "day",
        "day",
        "day",
        "hour",
        "hour",
        "hour",
        "hour",
    ]
    assert all(query["value_type"] == DATAPOINT_METER_VALUE_TYPE for query, _ in seen)
    assert all(headers["accept-language"] == BACKFILL_LANGUAGE for _, headers in seen)

    # The very same client keeps speaking Polish everywhere else.
    other_headers = [
        call.kwargs["headers"]
        for (_, url), calls in mock_api.requests.items()
        for call in calls
        if "/datapoints/" not in str(url)
    ]
    assert other_headers
    assert all(headers["accept-language"] == "pl" for headers in other_headers)


# ---------------------------------------------------------------------------
# Idempotency (the phase exit criterion)
# ---------------------------------------------------------------------------


async def test_reruns_add_no_duplicates_and_change_no_sums(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Re-running the backfill over the same readings changes nothing at all.

    Both re-run paths are exercised: the scheduled 12-hour refresh and a direct
    ``async_refresh``. Rows are keyed by bucket start and the readings behind
    them are immutable, so every row — including its running ``sum`` — must come
    back byte-identical, and the row count must not grow.
    """
    add_device_routes(mock_api)
    meter_routes(mock_api)

    await boot(hass, mock_config_entry, freezer)
    first = digest(await stored_rows(hass))
    assert len(first) == EXPECTED_ROW_COUNT

    # Scheduled re-run on the coordinator's own cadence.
    freezer.tick(STATISTICS_UPDATE_INTERVAL)
    async_fire_time_changed(hass)
    await settle(hass)

    coordinator = coordinator_of(mock_config_entry)
    assert coordinator.last_update_success is True
    assert digest(await stored_rows(hass)) == first

    # Direct re-run, same window, same readings.
    await coordinator.async_refresh()
    await settle(hass)
    assert coordinator.last_update_success is True
    assert digest(await stored_rows(hass)) == first


# ---------------------------------------------------------------------------
# Late-arriving readings inside the overlap window
# ---------------------------------------------------------------------------


async def test_late_reading_inside_the_overlap_is_picked_up(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Readings the device uploads late land on the next run, rewriting nothing.

    The first run sees a cloud that has not yet received 2026-07-27's hourly
    readings, so that day falls back to its daily reading. Once the hourly
    readings arrive, the next run — which recomputes the whole overlap window
    from its anchor — adds the two missing buckets and corrects the day's
    fallback row, while every row before the anchor keeps its exact ``sum``.
    """
    add_device_routes(mock_api)
    hourly = HourlyWindowRoute(without_july_27(hourly_fixture()))
    meter_routes(mock_api, hourly=hourly)

    await boot(hass, mock_config_entry, freezer)
    truncated = await stored_rows(hass)
    truncated_starts = starts(truncated)
    # Two hourly buckets short, the day represented by its daily reading only.
    assert len(truncated) == EXPECTED_ROW_COUNT - 2
    assert all(start not in truncated_starts for start in LATE_STARTS)
    fallback = truncated[truncated_starts.index(FALLBACK_START)]
    assert fallback["state"] == pytest.approx(
        stored(FALLBACK_DAILY_STATE), abs=LITER_TOLERANCE
    )

    # The readings land, and the next run picks them up.
    hourly.payload = hourly_fixture()
    coordinator = coordinator_of(mock_config_entry)
    await coordinator.async_refresh()
    await settle(hass)
    assert coordinator.last_update_success is True

    rows = await stored_rows(hass)
    assert len(rows) == EXPECTED_ROW_COUNT
    assert all(start in starts(rows) for start in LATE_STARTS)
    # The daily fallback row is not left behind either: it is replaced in place
    # by the hourly reading for the same bucket.
    corrected = rows[starts(rows).index(FALLBACK_START)]
    assert corrected["state"] == pytest.approx(
        stored(FALLBACK_HOURLY_STATE), abs=LITER_TOLERANCE
    )
    assert starts(rows)[-1] == LAST_START
    assert rows[-1]["state"] == pytest.approx(stored(LAST_STATE), abs=LITER_TOLERANCE)
    assert rows[-1]["sum"] == pytest.approx(stored(LAST_SUM), abs=LITER_TOLERANCE)

    # History behind the overlap window is never rewritten: every row the first
    # run wrote before the resume anchor survives with an identical sum.
    anchor_cutoff = datetime(2026, 6, 26, 22, tzinfo=UTC)
    before = {start for start in truncated_starts if start <= anchor_cutoff}
    assert before
    kept = {
        start: (row["state"], row["sum"])
        for start, row in zip(starts(rows), rows, strict=True)
        if start in before
    }
    original = {
        start: (row["state"], row["sum"])
        for start, row in zip(truncated_starts, truncated, strict=True)
        if start in before
    }
    assert kept == original


# ---------------------------------------------------------------------------
# Fail-closed guards
# ---------------------------------------------------------------------------


async def test_localized_units_abort_the_run_without_importing(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A ``units`` string in the wrong language aborts before anything is stored.

    The captured Polish response (``"Litry"``) is what a mis-pinned
    ``accept-language`` would return; importing it would silently scale every
    volume by 3.79, so the run must fail with nothing written — not even the
    windows that parsed cleanly before it.
    """
    add_device_routes(mock_api)
    meter_routes(mock_api, day=load_fixture("graph-usage-daily-pl.json"))

    await boot(hass, mock_config_entry, freezer)

    coordinator = coordinator_of(mock_config_entry)
    assert coordinator.last_update_success is False
    assert await stored_rows(hass) == []
    assert await stored_metadata(hass) == []


async def test_rate_limited_window_fails_the_run_without_importing(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A throttled window fails the pass cleanly, leaving the series untouched.

    The 429 lands on the hourly walk, after the probe and the daily windows have
    already been fetched — proving the import is a single all-or-nothing call at
    the end of the pass rather than a per-window write.
    """
    seen: list[tuple[dict[str, str], dict[str, str]]] = []
    add_device_routes(mock_api)
    mock_api.get(
        graph_url_for_period("hour"),
        status=429,
        payload={"code": "ThrottleLimitExceeded", "detail": "slow down"},
    )
    meter_routes(mock_api, seen=seen)

    await boot(hass, mock_config_entry, freezer)

    coordinator = coordinator_of(mock_config_entry)
    assert coordinator.last_update_success is False
    # The probe and every daily window were served normally (the throttled hourly
    # request never reaches the fixture route), and the run still stored nothing.
    assert [query["period_type"] for query, _ in seen] == [
        "year",
        "month",
        "day",
        "day",
        "day",
    ]
    assert await stored_rows(hass) == []
    assert await stored_metadata(hass) == []


async def test_auth_failure_starts_reauth(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A 401 whose refresh also fails sends the entry straight to reauth."""
    add_device_routes(mock_api)
    mock_api.get(
        graph_url_for_period("year"),
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
    meter_routes(mock_api)

    await boot(hass, mock_config_entry, freezer)

    assert coordinator_of(mock_config_entry).last_update_success is False
    assert await stored_rows(hass) == []

    reauth_flows = [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["handler"] == DOMAIN and flow["context"].get("source") == SOURCE_REAUTH
    ]
    assert len(reauth_flows) == 1


# ---------------------------------------------------------------------------
# Nothing to import
# ---------------------------------------------------------------------------


async def test_device_without_retained_datapoints_succeeds_with_no_rows(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An all-zero depth probe is a successful run that imports nothing.

    A brand-new or long-offline softener retains no datapoints, and the API says
    so by zero-filling rather than by returning an empty series. That must not be
    an error — and it must stop the pass at the very first sweep, so only the
    yearly route is ever asked for (the others would answer with a 500).
    """
    seen: list[tuple[dict[str, str], dict[str, str]]] = []
    add_device_routes(mock_api)
    add_datapoint_graph_routes(
        mock_api,
        by_period={"year": zeroed(load_fixture("graph-meter-yearly.json"))},
        seen_requests=seen,
    )

    await boot(hass, mock_config_entry, freezer)

    assert coordinator_of(mock_config_entry).last_update_success is True
    assert [query["period_type"] for query, _ in seen] == ["year"]
    assert await stored_rows(hass) == []
    assert await stored_metadata(hass) == []


# ---------------------------------------------------------------------------
# Cleanup on entry removal
# ---------------------------------------------------------------------------


async def test_removing_the_entry_clears_its_statistics(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Removing the config entry deletes the external series it created.

    External statistics outlive entities, so without the ``async_remove_entry``
    hook an uninstall would leave the whole ``aquahome:`` series orphaned in the
    recorder. The sensor platform is loaded here because the cleanup rebuilds the
    statistic ids from the device registry, which only exists once a platform has
    registered the device.
    """
    add_device_routes(mock_api)
    meter_routes(mock_api)

    await boot(hass, mock_config_entry, freezer, platforms=[Platform.SENSOR])
    assert len(await stored_rows(hass)) == EXPECTED_ROW_COUNT
    assert len(await stored_metadata(hass)) == 1

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await settle(hass)

    assert await stored_rows(hass) == []
    assert await stored_metadata(hass) == []


async def test_timezone_comes_from_the_props_detail_not_the_device_list(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Bucket alignment survives the property-less production device list.

    ``GET /devices`` is fetched without ``props``, so in production the list
    objects carry no property map at all — the composed fixture's full map is a
    test-only convenience. The device timezone must therefore come from the
    ``props=true`` detail payload the fast coordinator fetched (adversarial
    review finding, 2026-07-27): fed from the list object it would silently
    fall back to the Home Assistant zone (US/Pacific in this harness) and file
    every daily reading at the wrong local instant.
    """
    devices_list = load_fixture("devices-list.json")
    for item in devices_list["data"]:
        item["properties"] = {}
        item["enriched_data"] = None
    add_device_routes(mock_api, devices_list=devices_list)
    meter_routes(mock_api)

    await boot(hass, mock_config_entry, freezer)

    assert coordinator_of(mock_config_entry).last_update_success is True
    rows = await stored_rows(hass)
    assert len(rows) == EXPECTED_ROW_COUNT
    # Warsaw local midnight, not a US/Pacific one — the detail payload's tz won.
    assert starts(rows)[0] == FIRST_START


# ---------------------------------------------------------------------------
# The stored unit: an external statistic has no entity to convert it
# ---------------------------------------------------------------------------


async def test_a_us_customary_installation_stores_gallons(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A new series takes its unit from the installation's unit system.

    Nothing converts an external statistic on its way to a dashboard, so the
    series is stored in the unit the household reads — the device's native
    gallons here, unlike the metric default the other tests exercise.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    add_device_routes(mock_api)
    meter_routes(mock_api)

    await boot(hass, mock_config_entry, freezer)

    metadata = await stored_metadata(hass)
    assert metadata[0]["statistics_unit_of_measurement"] == UnitOfVolume.GALLONS
    rows = await stored_rows(hass)
    assert rows[0]["state"] == pytest.approx(FIRST_STATE, abs=LITER_TOLERANCE)
    assert rows[-1]["sum"] == pytest.approx(LAST_SUM, abs=LITER_TOLERANCE)


async def test_a_series_stored_in_another_unit_is_rebuilt(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Switching the installation's unit system rebuilds the whole series.

    Home Assistant converts stored statistics only for series the recorder
    itself owns, so an external series can never be converted in place: rows in
    one unit would keep accumulating a running total in another.

    Pins the rebuild branch. The series is really built in gallons — a US
    customary installation — and the installation then really flips to metric,
    so the rows the recorder holds are gallon-valued, not merely gallon-labelled
    (which is all ``async_update_statistics_metadata`` does, and why simulating
    the mismatch that way let the ordinary resume path pass this test). Without
    the rebuild, the resume path keeps every row behind the overlap window in
    gallons and steps the running total by ~130 000 when the first liter reading
    is diffed against the gallon anchor: the exact row values and
    :func:`continuity_gaps` both reject that.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    add_device_routes(mock_api)
    meter_routes(mock_api)

    await boot(hass, mock_config_entry, freezer)
    native = await stored_rows(hass)
    assert len(native) == EXPECTED_ROW_COUNT
    assert native[0]["state"] == pytest.approx(FIRST_STATE, abs=LITER_TOLERANCE)
    assert native[-1]["sum"] == pytest.approx(LAST_SUM, abs=LITER_TOLERANCE)
    before = await stored_metadata(hass)
    assert before[0]["statistics_unit_of_measurement"] == UnitOfVolume.GALLONS

    hass.config.units = METRIC_SYSTEM
    coordinator = coordinator_of(mock_config_entry)
    await coordinator.async_refresh()
    await settle(hass)

    assert coordinator.last_update_success is True
    metadata = await stored_metadata(hass)
    assert metadata[0]["statistics_unit_of_measurement"] == UnitOfVolume.LITERS
    assert metadata[0]["display_unit_of_measurement"] == UnitOfVolume.LITERS

    # The full history is back — not just the overlap window a resume would
    # have rewritten — and every row of it is liter-denominated.
    rows = await stored_rows(hass)
    assert len(rows) == EXPECTED_ROW_COUNT
    assert starts(rows)[0] == FIRST_START
    assert starts(rows)[-1] == LAST_START
    assert rows[0]["state"] == pytest.approx(stored(FIRST_STATE), abs=LITER_TOLERANCE)
    assert rows[0]["sum"] == pytest.approx(stored(FIRST_SUM), abs=LITER_TOLERANCE)
    assert rows[-1]["state"] == pytest.approx(stored(LAST_STATE), abs=LITER_TOLERANCE)
    assert rows[-1]["sum"] == pytest.approx(stored(LAST_SUM), abs=LITER_TOLERANCE)
    assert continuity_gaps(rows) == []


async def test_a_throttled_rebuild_keeps_the_mismatched_series(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A rebuild whose re-fetch fails leaves the old series untouched.

    Pins the fetch-before-clear ordering: the replacement rows must all exist
    in memory before the old series is deleted. Clearing first — and every
    fetch after it can throttle, drop the connection or return a contract
    failure — hands the user an empty Energy dashboard for at least the next
    twelve hours, and permanently once the cloud ages the readings out. A
    series stored in the wrong unit is strictly better than no series.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    add_device_routes(mock_api)
    routes = ArmableMeterRoutes()
    mock_api.get(graph_url(), callback=routes, repeat=True)

    await boot(hass, mock_config_entry, freezer)
    intact = digest(await stored_rows(hass))
    assert len(intact) == EXPECTED_ROW_COUNT

    hass.config.units = METRIC_SYSTEM
    routes.throttled = True
    coordinator = coordinator_of(mock_config_entry)
    await coordinator.async_refresh()
    await settle(hass)

    assert coordinator.last_update_success is False
    assert digest(await stored_rows(hass)) == intact
    metadata = await stored_metadata(hass)
    assert metadata[0]["statistics_unit_of_measurement"] == UnitOfVolume.GALLONS


async def test_a_rebuild_probing_empty_keeps_the_mismatched_series(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A rebuild whose depth probe finds nothing leaves the old series alone.

    Pins the same ordering against the failure mode that raises nothing at all:
    the cloud can legitimately answer the probe with an all-zero sweep — a
    softener offline long enough for its datapoints to age out — and that ends
    the pass successfully with no rows to import. Clearing before probing would
    turn exactly that case into a silent, permanent data loss, since the
    readings the rebuild needs are the ones the cloud no longer has.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    add_device_routes(mock_api)
    yearly = load_fixture("graph-meter-yearly.json")
    meter_routes(mock_api, year=[yearly, zeroed(yearly)])

    await boot(hass, mock_config_entry, freezer)
    intact = digest(await stored_rows(hass))
    assert len(intact) == EXPECTED_ROW_COUNT

    hass.config.units = METRIC_SYSTEM
    coordinator = coordinator_of(mock_config_entry)
    await coordinator.async_refresh()
    await settle(hass)

    # Nothing to import is a successful run; it just must not be a destructive
    # one, and the mismatch stays for the next run to retry.
    assert coordinator.last_update_success is True
    assert digest(await stored_rows(hass)) == intact
    metadata = await stored_metadata(hass)
    assert metadata[0]["statistics_unit_of_measurement"] == UnitOfVolume.GALLONS


# ---------------------------------------------------------------------------
# The recorder barrier: bounded, so a stopped recorder cannot pin the run
# ---------------------------------------------------------------------------


async def test_a_recorder_that_never_confirms_fails_instead_of_hanging(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A synchronize marker nobody resolves ends the wait on a deadline.

    Pins the bound on the post-import barrier. The marker queued behind an
    import is resolved from the recorder's own thread, so a recorder stopped by
    a shutdown landing mid-backfill — or one whose thread died — never resolves
    it at all. An unbounded await on that future pins the coordinator task for
    the lifetime of the process: shutdown waits on it, and no later import ever
    runs. The marker is left pending rather than cancelled, so a recorder that
    comes back and resolves it late cannot raise inside the event loop.

    The barrier is driven on its own instead of through a whole pass because
    the module-wide freezer stops the event loop's clock along with everything
    else: no asyncio deadline expires until the frozen clock is moved past it,
    which this test does pass by pass. The bounded number of passes is the
    regression guard — a wait that lost its deadline fails the assertion below
    instead of hanging the suite.
    """
    add_device_routes(mock_api)
    meter_routes(mock_api)

    await boot(hass, mock_config_entry, freezer)
    coordinator = coordinator_of(mock_config_entry)
    NeverConfirmingTask.built.clear()

    with (
        patch(
            "custom_components.aquahome.statistics.SynchronizeTask",
            NeverConfirmingTask,
        ),
        patch(
            "custom_components.aquahome.statistics._RECORDER_DRAIN_TIMEOUT_SECONDS",
            DRAIN_TIMEOUT_SECONDS,
        ),
    ):
        barrier = coordinator._async_wait_for_recorder()
        waiting = hass.async_create_task(barrier)
        for _ in range(DRAIN_SPIN_PASSES):
            if waiting.done():
                break
            freezer.tick(DRAIN_SPIN_STEP)
            await asyncio.sleep(0)

    assert waiting.done()
    with pytest.raises(UpdateFailed, match="did not confirm"):
        await waiting
    # Giving up must not cancel the marker: a recorder that comes back later
    # still resolves it, and resolving a cancelled future raises in the loop.
    assert [marker.future.cancelled() for marker in NeverConfirmingTask.built] == [
        False
    ]
    await settle(hass)
