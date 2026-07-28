"""Tests for :mod:`custom_components.aquahome.analytics.baseline`.

The baseline module answers one question — "what is normal for this household?"
— from two disagreeing sources: the device's own per-weekday averages (fresh but
change-stamped, so silently ageing) and statistics learned locally from the
imported meter history (slow to warm up but never stale). Everything here tests
that arbitration, so every test either replays the real captured series or
builds the two sources by hand and watches which one wins.

Nothing in this file touches Home Assistant, and nothing in the module under
test reads a clock: ``now`` arrives inside :class:`AnalyticsInputs`, so the
whole file is hermetic by construction and needs no ``freezer``. Every instant
is an explicit literal and every expectation is derived from that literal.

The replayed series is ``tests.analytics_traces.real_readings()`` — the
production-merge shape of the captured graphs (405 readings, 2025-09-13 →
2026-07-27, gallons). Its numbers are pinned as literals rather than recomputed
from the fixtures, so a regression in the grid arithmetic cannot quietly move
the expectation along with the result.

Two pinned buckets deserve their own note, because the merged series is not the
hourly-only capture the first measurements were taken on:

* The ``(Saturday, 09:00)`` bucket holds **four** samples, not three. Ten days
  of unchanged daily meter rows in October 2025 (2025-10-19 → 2025-10-29) prove
  every hour they cover carried nothing, and one of those proven zeros lands in
  this bucket alongside the three July mornings (53 L, 8 L, 34 L) the hourly
  capture held. Median 21 L, not 34 L — see
  ``test_real_grid_saturday_nine_bucket``.
* ``(Tuesday, 20:00)`` holds three samples for the same reason (two proven
  zeros plus one July evening), which still leaves it short of
  ``MIN_BUCKET_SAMPLES`` and therefore immature.

The learned statistics are taken over the trailing **28** noon-days unless a
test says otherwise: that is the shortest window in which the reference
household's Friday reaches ``MIN_BUCKET_SAMPLES``, and it reproduces the
pinned ``learned_weekday`` resolution of 134.5 L / 20.0 L. The 21-
and 14-day windows are used deliberately to walk the chain one step further
down on each fallback test.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from custom_components.aquahome.analytics import series
from custom_components.aquahome.analytics.baseline import (
    GRID_BUCKETS,
    MAD_SCALE,
    LearnedDaily,
    activity_grid,
    bucket_index,
    build_grid,
    expected_daily_liters,
    forecast_for,
    peak_hours,
    slot_for_day,
    slot_fresh,
)
from custom_components.aquahome.analytics.model import (
    SOURCE_DEVICE_AVERAGE,
    SOURCE_LEARNED_WEEKDAY,
    SOURCE_OVERALL_AVERAGE,
    AnalyticsInputs,
    WeekdaySlot,
)
from custom_components.aquahome.const import (
    ANALYTICS_K,
    LEARNED_DAILY_MIN_DAYS,
    MIN_BUCKET_SAMPLES,
    OCCUPANCY_LITERS_PER_PERSON,
    PEAK_HOURS_PER_WEEKDAY,
    WEEKDAY_SLOT_FRESHNESS_DAYS,
    WEEKDAY_SLOTS,
)
from custom_components.aquahome.salt import LITERS_PER_GALLON
from tests.analytics_traces import real_readings

#: The reference device's zone, carried by every fixture label.
WARSAW: Final = ZoneInfo("Europe/Warsaw")

#: The instant every analytics pass in this file is computed at — the end of
#: the captured window, the instant the replayed ground truth was measured at.
NOW: Final = datetime(2026, 7, 27, 10, 30, tzinfo=UTC)

#: Wall-clock instant a noon-day boundary is cut at.
NOON: Final = time(12)

#: The replayed real meter series and its certain-hour knowledge, built once.
REAL: Final = real_readings()
REAL_KNOWLEDGE: Final = series.hour_knowledge(REAL, WARSAW)

#: Grid index of the two buckets called out in the module docstring.
SATURDAY_NINE: Final = 5 * 24 + 9
TUESDAY_EIGHT_PM: Final = 1 * 24 + 20

#: The device's own weekday slots exactly as ``properties.json`` reports them,
#: in Map B order (index 0 = slot 1 = Saturday). Slot 7 (Friday) carries the
#: 43-day-old change-stamp that makes the freshness guard load-bearing.
DEVICE_SLOTS: Final = (
    WeekdaySlot(41.0, 11.0, datetime(2026, 7, 19, 0, 1, 1, tzinfo=UTC)),
    WeekdaySlot(57.0, 25.0, datetime(2026, 7, 20, 0, 1, 2, tzinfo=UTC)),
    WeekdaySlot(43.0, 14.0, datetime(2026, 7, 21, 0, 1, 3, tzinfo=UTC)),
    WeekdaySlot(35.0, 8.0, datetime(2026, 7, 18, 0, 1, 1, tzinfo=UTC)),
    WeekdaySlot(46.0, 10.0, datetime(2026, 7, 16, 0, 1, 1, tzinfo=UTC)),
    WeekdaySlot(40.0, 5.0, datetime(2026, 7, 17, 0, 1, 2, tzinfo=UTC)),
    WeekdaySlot(10.0, 11.0, datetime(2026, 6, 14, 2, 53, 37, tzinfo=UTC)),
)

#: ``avg_daily_use_gals`` wrapped as a slot: an average with no deviation.
DEVICE_OVERALL: Final = WeekdaySlot(
    47.0, None, datetime(2026, 7, 20, 22, 1, 3, tzinfo=UTC)
)

#: A stamp comfortably inside the freshness window at :data:`NOW`.
FRESH_STAMP: Final = datetime(2026, 7, 25, tzinfo=UTC)

#: Days the chain is resolved for: a Monday whose device slot is fresh, and the
#: Friday whose slot is stale (both inside the captured history).
MONDAY: Final = date(2026, 7, 20)
FRIDAY: Final = date(2026, 7, 24)

#: The local day after the capture ends — the day the forecast describes.
FORECAST_DAY: Final = date(2026, 7, 28)

#: The Monday every crafted grid below is laid out from, one sample per week.
#: July carries no daylight-saving transition, so every crafted sample lands on
#: the local hour its bucket is named after.
GRID_MONDAY: Final = date(2026, 7, 6)


def make_inputs(
    slots: tuple[WeekdaySlot, ...] = DEVICE_SLOTS,
    overall: WeekdaySlot = DEVICE_OVERALL,
    *,
    now: datetime = NOW,
) -> AnalyticsInputs:
    """Return analytics inputs carrying only what the baseline module reads.

    ``readings`` stays empty on purpose: the baseline never touches the series
    itself, it consumes it pre-digested as a :class:`LearnedDaily`. A test that
    passed the real readings here would be asserting nothing about them.
    """
    return AnalyticsInputs(
        readings=(),
        regen_windows=(),
        regen_coverage_start=None,
        weekday_slots=slots,
        overall_average=overall,
        tz_key="Europe/Warsaw",
        now=now,
        device_online=True,
        statistics_fresh=True,
    )


def learned_over(window_days: int) -> LearnedDaily:
    """Return learned statistics over the trailing noon-days of the real series.

    Rebuilds the ``(day, total, assessable)`` triples the detector pipeline
    feeds :meth:`LearnedDaily.from_days`, so the numbers below are the ones the
    production chain would see for the same window.
    """
    triples: list[tuple[date, float | None, bool]] = []
    for day in series.noon_days(window_days, NOW, WARSAW):
        opening = datetime.combine(day - timedelta(days=1), NOON, tzinfo=WARSAW)
        closing = datetime.combine(day, NOON, tzinfo=WARSAW)
        total = series.day_total_liters(REAL, day, WARSAW)
        assessable = total is not None and series.bounded(REAL, opening, closing)
        triples.append((day, total, assessable))
    return LearnedDaily.from_days(triples)


def uniform_learned(
    median: float | None, spread: float | None, count: int
) -> LearnedDaily:
    """Return hand-built statistics identical on every weekday, overall empty.

    Callers that also need the overall distribution add it with
    :func:`dataclasses.replace`, which keeps each test's gate visible at the
    place it matters.
    """
    return LearnedDaily(
        weekday_median=(median,) * 7,
        weekday_spread=(spread,) * 7,
        weekday_count=(count,) * 7,
        overall_median=None,
        overall_spread=None,
        overall_count=0,
        overall_mean=None,
    )


def crafted_peaks(
    plan: Mapping[tuple[int, int], Sequence[float]],
) -> tuple[tuple[int, ...], ...]:
    """Return the peak hours of a grid built from crafted bucket samples.

    ``plan`` maps ``(python weekday, hour)`` to that bucket's samples, laid down
    one per week from :data:`GRID_MONDAY`, so a bucket's maturity is simply how
    many samples it was handed and its median is theirs. Buckets left out of the
    plan are never sampled and stay unknown.
    """
    knowledge: dict[datetime, float] = {}
    for (weekday, hour), samples in plan.items():
        first = datetime.combine(
            GRID_MONDAY + timedelta(days=weekday), time(hour), tzinfo=WARSAW
        )
        for week, liters in enumerate(samples):
            knowledge[first + timedelta(days=7 * week)] = liters
    median, _mad, counts = build_grid(knowledge)
    return peak_hours(median, counts)


def bucket_values(bucket: int) -> list[float]:
    """Return the certain-hour samples the real series puts in one bucket."""
    return sorted(
        liters
        for hour, liters in REAL_KNOWLEDGE.items()
        if bucket_index(hour) == bucket
    )


def test_real_grid_saturday_nine_bucket() -> None:
    """The Saturday-morning bucket holds three July mornings and one proven zero.

    The three hourly captures are 53 L / 8 L / 34 L; the fourth sample is an
    hour inside October's ten-day flat stretch of daily meter rows, which proves
    that hour carried nothing. Four samples, median 21 L — the earlier ``n=3,
    median 34 L`` was measured on the hourly capture alone, before the
    production merge shape was frozen.
    """
    median, scaled_mad, counts = build_grid(REAL_KNOWLEDGE)

    assert bucket_values(SATURDAY_NINE) == [
        pytest.approx(0.0),
        pytest.approx(8.0),
        pytest.approx(34.0),
        pytest.approx(53.0),
    ]
    assert int(counts[SATURDAY_NINE]) == 4
    assert float(median[SATURDAY_NINE]) == pytest.approx(21.0)
    # Absolute deviations from the median are 21, 13, 13 and 32 litres.
    assert float(scaled_mad[SATURDAY_NINE]) == pytest.approx(MAD_SCALE * 17.0)
    assert int(counts[SATURDAY_NINE]) >= MIN_BUCKET_SAMPLES


def test_real_grid_counts_every_certain_hour_exactly_once() -> None:
    """Every hour of certain knowledge lands in exactly one bucket.

    438 certain hours across 46 mature buckets — the measured grid summary for
    the replayed series.
    """
    _median, _mad, counts = build_grid(REAL_KNOWLEDGE)

    assert len(REAL_KNOWLEDGE) == 438
    assert int(np.sum(counts)) == 438
    assert counts.shape == (GRID_BUCKETS,)
    assert int(np.count_nonzero(counts >= MIN_BUCKET_SAMPLES)) == 46


def test_real_grid_immature_buckets_stay_below_the_gate() -> None:
    """Sparse buckets keep their statistics but never claim maturity.

    Take ``(Tuesday, 20:00)``: two of October's proven zeros plus one 42 L
    July evening, so a median of zero on three samples — short of
    ``MIN_BUCKET_SAMPLES`` and therefore never usable as an expectation. 33 of
    the grid's buckets rest on a single sample alone.
    """
    median, scaled_mad, counts = build_grid(REAL_KNOWLEDGE)

    assert bucket_values(TUESDAY_EIGHT_PM) == [
        pytest.approx(0.0),
        pytest.approx(0.0),
        pytest.approx(42.0),
    ]
    assert int(counts[TUESDAY_EIGHT_PM]) == 3
    assert int(counts[TUESDAY_EIGHT_PM]) < MIN_BUCKET_SAMPLES
    assert float(median[TUESDAY_EIGHT_PM]) == pytest.approx(0.0)
    assert float(scaled_mad[TUESDAY_EIGHT_PM]) == pytest.approx(0.0)

    single = [index for index in range(GRID_BUCKETS) if int(counts[index]) == 1]
    assert len(single) == 33
    for index in single:
        assert float(scaled_mad[index]) == pytest.approx(0.0)
        assert float(median[index]) == pytest.approx(bucket_values(index)[0])


def test_build_grid_leaves_unsampled_buckets_unknown() -> None:
    """A bucket nobody sampled stays ``nan``, never an imputed zero."""
    median, scaled_mad, counts = build_grid({})

    assert median.shape == scaled_mad.shape == counts.shape == (GRID_BUCKETS,)
    assert bool(np.isnan(median).all())
    assert bool(np.isnan(scaled_mad).all())
    assert int(np.sum(counts)) == 0


def test_build_grid_drops_non_finite_samples() -> None:
    """A corrupt reading neither poisons a bucket nor inflates its maturity."""
    hour = datetime(2026, 7, 6, 7, tzinfo=WARSAW)
    knowledge = {
        hour: float("nan"),
        hour + timedelta(days=7): float("inf"),
        hour + timedelta(days=14): 12.0,
    }

    median, scaled_mad, counts = build_grid(knowledge)

    bucket = bucket_index(hour)
    assert int(counts[bucket]) == 1
    assert float(median[bucket]) == pytest.approx(12.0)
    assert float(scaled_mad[bucket]) == pytest.approx(0.0)
    assert int(np.sum(counts)) == 1


def test_build_grid_uses_a_robust_centre_and_spread() -> None:
    """One irrigation afternoon moves neither the median nor the scaled MAD.

    Five Monday mornings of 10 L plus a 500 L outlier: a mean would land near
    91 L and a standard deviation near 200 L, which would hide every later
    anomaly in that hour. Median and MAD ignore the outlier entirely.
    """
    monday = datetime(2026, 7, 6, 7, tzinfo=WARSAW)
    knowledge = {
        monday + timedelta(days=7 * week): liters
        for week, liters in enumerate([10.0, 10.0, 10.0, 10.0, 10.0, 500.0])
    }

    median, scaled_mad, counts = build_grid(knowledge)

    bucket = bucket_index(monday)
    assert int(counts[bucket]) == 6
    assert float(median[bucket]) == pytest.approx(10.0)
    assert float(scaled_mad[bucket]) == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 7, 27, 0, tzinfo=WARSAW), 0),  # Monday midnight
        (datetime(2026, 7, 27, 23, tzinfo=WARSAW), 23),
        (datetime(2026, 7, 28, 0, tzinfo=WARSAW), 24),  # Tuesday
        (datetime(2026, 7, 25, 9, tzinfo=WARSAW), SATURDAY_NINE),
        (datetime(2026, 8, 2, 23, tzinfo=WARSAW), GRID_BUCKETS - 1),  # Sunday
    ],
)
def test_bucket_index_lays_the_week_out_monday_first(
    moment: datetime, expected: int
) -> None:
    """The grid runs ``weekday(Mon=0) * 24 + hour`` over 168 buckets."""
    assert bucket_index(moment) == expected


@pytest.mark.parametrize(
    ("samples", "active"),
    [
        pytest.param([5.0, 5.0, 5.0], False, id="three-wet-hours-immature"),
        pytest.param([5.0, 5.0, 5.0, 5.0], True, id="four-wet-hours-active"),
        pytest.param([5.0, 5.0, 0.0, 0.0], True, id="half-wet-is-active"),
        pytest.param([5.0, 0.0, 0.0, 0.0], False, id="mostly-dry-is-quiet"),
        pytest.param([0.0, 0.0, 0.0, 0.0], False, id="all-dry-is-quiet"),
    ],
)
def test_activity_grid_needs_maturity_and_a_water_majority(
    samples: list[float], active: bool
) -> None:
    """An hour is active only once it is both known and usually wet.

    The two conditions answer different questions — "do we know this hour?" and
    "is it a water hour?" — so a single late-night dishwasher run never turns
    01:00 into an active hour, and neither does a well-sampled but mostly dry
    one.
    """
    hour = datetime(2026, 7, 6, 1, tzinfo=WARSAW)
    knowledge = {
        hour + timedelta(days=7 * week): liters for week, liters in enumerate(samples)
    }

    median, mad, counts = build_grid(knowledge)

    assert activity_grid(median, mad, counts, knowledge)[bucket_index(hour)] is active


def test_activity_grid_matches_the_real_mature_buckets() -> None:
    """On the real series every mature bucket is also an active hour.

    46 mature buckets, 46 active hours, and the same 46 — the household's week
    is sparse enough that any hour it pushes four readings for is an hour it
    genuinely uses water in.
    """
    median, mad, counts = build_grid(REAL_KNOWLEDGE)

    active = activity_grid(median, mad, counts, REAL_KNOWLEDGE)

    assert len(active) == GRID_BUCKETS
    assert sum(active) == 46
    mature = {index for index in range(GRID_BUCKETS) if int(counts[index]) >= 4}
    assert {index for index, flag in enumerate(active) if flag} == mature


def test_activity_grid_never_promotes_an_immature_bucket() -> None:
    """No bucket below the maturity gate is ever reported as an active hour."""
    median, mad, counts = build_grid(REAL_KNOWLEDGE)

    active = activity_grid(median, mad, counts, REAL_KNOWLEDGE)

    immature = [
        index
        for index in range(GRID_BUCKETS)
        if int(counts[index]) < MIN_BUCKET_SAMPLES
    ]
    assert immature  # the real grid is sparse; the gate has work to do
    assert not any(active[index] for index in immature)


def test_peak_hours_rank_a_weekday_by_median_volume() -> None:
    """The busiest mature buckets of a weekday, published ascending by hour.

    Six mature Monday hours with distinct medians: the four biggest are the
    peaks, and they come back in clock order rather than ranking order — a
    consumer walks them forward through the day, it does not re-sort them.
    """
    peaks = crafted_peaks(
        {
            (0, 6): [4.0] * MIN_BUCKET_SAMPLES,
            (0, 7): [40.0] * MIN_BUCKET_SAMPLES,
            (0, 12): [25.0] * MIN_BUCKET_SAMPLES,
            (0, 15): [2.0] * MIN_BUCKET_SAMPLES,
            (0, 19): [30.0] * MIN_BUCKET_SAMPLES,
            (0, 21): [10.0] * MIN_BUCKET_SAMPLES,
        }
    )

    assert peaks[0] == (7, 12, 19, 21)
    assert peaks[1:] == ((),) * 6


def test_peak_hours_never_promote_an_immature_bucket() -> None:
    """One sample short of maturity, however large, is not a peak.

    An unknown hour must never out-rank a known one: the biggest median of the
    row rests on ``MIN_BUCKET_SAMPLES - 1`` samples and stays out, leaving a
    modest matured hour to carry the weekday.
    """
    peaks = crafted_peaks(
        {
            (2, 3): [500.0] * (MIN_BUCKET_SAMPLES - 1),
            (2, 8): [12.0] * MIN_BUCKET_SAMPLES,
        }
    )

    assert peaks[2] == (8,)


def test_peak_hours_ignore_a_matured_dry_bucket() -> None:
    """An hour proven dry four times over is a known quiet hour, not a peak.

    A multi-week absence matures buckets at median zero; ranking them would
    hand a household back four "peaks" it never draws a drop in.
    """
    peaks = crafted_peaks(
        {
            (4, 2): [0.0] * MIN_BUCKET_SAMPLES,
            (4, 18): [9.0] * MIN_BUCKET_SAMPLES,
        }
    )

    assert peaks[4] == (18,)


def test_peak_hours_ignore_an_unknown_median_on_a_matured_bucket() -> None:
    """A ``nan`` median never ranks, whatever its bucket's sample count says.

    ``nan`` is the grid's "unknown" and it compares false against everything,
    so an unguarded ranking would order it by sort stability alone and publish
    an hour nothing is known about.
    """
    median = np.full(GRID_BUCKETS, np.nan, dtype=np.float64)
    median[bucket_index(datetime(2026, 7, 6, 17, tzinfo=WARSAW))] = 14.0
    counts = np.full(GRID_BUCKETS, MIN_BUCKET_SAMPLES, dtype=np.int64)

    peaks = peak_hours(median, counts)

    assert peaks[0] == (17,)
    assert peaks[1:] == ((),) * 6


def test_peak_hours_break_ties_toward_the_earlier_hour() -> None:
    """Five identical medians resolve to the four earliest hours, every pass.

    Determinism is the point: the same grid has to publish the same peaks each
    time, or a consumer that arms on them re-arms on the tie-break's whim.
    """
    peaks = crafted_peaks(
        {(1, hour): [7.0] * MIN_BUCKET_SAMPLES for hour in (5, 9, 14, 18, 22)}
    )

    assert peaks[1] == (5, 9, 14, 18)


def test_peak_hours_publish_at_most_the_configured_count() -> None:
    """Ten qualifying hours still yield K of them, and they are the K largest."""
    peaks = crafted_peaks(
        {(6, hour): [float(hour)] * MIN_BUCKET_SAMPLES for hour in range(10, 20)}
    )

    assert len(peaks[6]) == PEAK_HOURS_PER_WEEKDAY
    assert peaks[6] == (16, 17, 18, 19)


def test_peak_hours_keep_the_weekdays_independent() -> None:
    """A heavy weekday never lends its hours to a quiet one.

    Wednesday's evening out-draws every Sunday hour by two orders of magnitude,
    yet Sunday keeps its own peak: the ranking is per row, so a household with
    one laundry day still gets usable peaks for the other six.
    """
    peaks = crafted_peaks(
        {
            (2, 19): [200.0] * MIN_BUCKET_SAMPLES,
            (2, 20): [180.0] * MIN_BUCKET_SAMPLES,
            (6, 9): [3.0] * MIN_BUCKET_SAMPLES,
        }
    )

    assert peaks[2] == (19, 20)
    assert peaks[6] == (9,)
    assert [row for index, row in enumerate(peaks) if index not in (2, 6)] == [()] * 5


def test_peak_hours_do_not_exclude_the_night() -> None:
    """A household that genuinely draws at 03:00 gets 03:00 as a peak.

    The night rule belongs to whoever acts on the peaks (the live tier skips
    01:00-07:00); the analytics has no business hiding water that moved.
    """
    peaks = crafted_peaks(
        {
            (3, 3): [60.0] * MIN_BUCKET_SAMPLES,
            (3, 18): [12.0] * MIN_BUCKET_SAMPLES,
        }
    )

    assert peaks[3] == (3, 18)


def test_peak_hours_of_an_empty_grid_are_seven_empty_rows() -> None:
    """A cold start publishes the shape consumers guard on, holding nothing."""
    median, _mad, counts = build_grid({})

    assert peak_hours(median, counts) == ((),) * 7


def test_peak_hours_of_a_short_grid_are_seven_empty_rows() -> None:
    """A grid narrower than a full week is unusable, not partly usable.

    Indexing a short grid by ``weekday * 24 + hour`` reads the wrong weekday
    long before it runs off the end, so the whole grid is refused instead.
    """
    median = np.full(GRID_BUCKETS - 1, 10.0, dtype=np.float64)
    counts = np.full(GRID_BUCKETS - 1, MIN_BUCKET_SAMPLES, dtype=np.int64)

    assert peak_hours(median, counts) == ((),) * 7


def test_real_grid_peaks_thin_each_weekday_to_its_busiest_hours() -> None:
    """The real household's 46 mature buckets thin to 26 peaks across the week.

    Monday and Sunday mature 13 and 12 buckets and still publish four hours
    each — the point of the top-K rule, since the binary grid marks all 46 as
    active. Saturday matured only two buckets, and a row shorter than K is the
    honest answer there rather than a padded one.
    """
    median, _mad, counts = build_grid(REAL_KNOWLEDGE)

    peaks = peak_hours(median, counts)

    assert peaks == (
        (9, 10, 20, 22),
        (10, 11, 17, 18),
        (10, 12, 19, 20),
        (10, 11, 12, 15),
        (7, 13, 19, 22),
        (8, 9),
        (12, 13, 17, 19),
    )
    assert sum(len(row) for row in peaks) == 26
    mature = {
        index
        for index in range(GRID_BUCKETS)
        if int(counts[index]) >= MIN_BUCKET_SAMPLES
    }
    assert {
        weekday * 24 + hour for weekday, row in enumerate(peaks) for hour in row
    } <= mature


def test_slot_freshness_splits_the_real_device_slots() -> None:
    """The 43-day-old Friday slot is stale; the Monday slot is fresh.

    Unguarded, that Friday slot (10 gal against a ~160 L/day household) would
    declare an EXCESS anomaly every single Friday — the guard is the reason the
    chain ever reaches its learned fallback.
    """
    friday_slot = DEVICE_SLOTS[slot_for_day(FRIDAY)]
    monday_slot = DEVICE_SLOTS[slot_for_day(MONDAY)]

    assert friday_slot.updated_at == datetime(2026, 6, 14, 2, 53, 37, tzinfo=UTC)
    assert slot_fresh(friday_slot, NOW) is False
    assert monday_slot.updated_at == datetime(2026, 7, 21, 0, 1, 3, tzinfo=UTC)
    assert slot_fresh(monday_slot, NOW) is True
    assert slot_fresh(DEVICE_OVERALL, NOW) is True


@pytest.mark.parametrize(
    ("stamp", "fresh"),
    [
        pytest.param(None, False, id="never-stamped"),
        pytest.param(
            NOW - timedelta(days=WEEKDAY_SLOT_FRESHNESS_DAYS),
            True,
            id="exactly-at-edge",
        ),
        pytest.param(
            NOW - timedelta(days=WEEKDAY_SLOT_FRESHNESS_DAYS, seconds=1),
            False,
            id="one-second-past-edge",
        ),
        pytest.param(NOW + timedelta(hours=6), True, id="clock-skew-into-the-future"),
    ],
)
def test_slot_freshness_boundaries(stamp: datetime | None, fresh: bool) -> None:
    """The window is closed at its edge and a skewed future stamp is not stale."""
    assert slot_fresh(WeekdaySlot(40.0, 5.0, stamp), NOW) is fresh


@pytest.mark.parametrize(
    ("day", "index", "name"),
    [
        pytest.param(date(2026, 7, 25), 0, "saturday", id="saturday-is-slot-1"),
        pytest.param(date(2026, 7, 26), 1, "sunday", id="sunday"),
        pytest.param(date(2026, 7, 27), 2, "monday", id="monday"),
        pytest.param(date(2026, 7, 28), 3, "tuesday", id="tuesday"),
        pytest.param(date(2026, 7, 29), 4, "wednesday", id="wednesday"),
        pytest.param(date(2026, 7, 30), 5, "thursday", id="thursday"),
        pytest.param(date(2026, 7, 31), 6, "friday", id="friday-is-slot-7"),
    ],
)
def test_slot_for_day_follows_map_b(day: date, index: int, name: str) -> None:
    """Map B rotates the device's slots so slot 1 carries Saturday."""
    assert slot_for_day(day) == index
    assert WEEKDAY_SLOTS[slot_for_day(day)] == name


def test_fresh_device_slot_wins_the_chain() -> None:
    """A fresh slot beats richer learned statistics: it is the newer evidence.

    Monday's slot reports 43 gal +/- 14 gal, which is what the expectation
    resolves to even though four weeks of learned Mondays are available.
    """
    resolved = expected_daily_liters(MONDAY, make_inputs(), learned_over(28))

    assert resolved is not None
    expected, spread, source = resolved
    assert source == SOURCE_DEVICE_AVERAGE
    assert expected == pytest.approx(162.772706712)
    assert spread == pytest.approx(52.995764976)
    assert expected == pytest.approx(43.0 * LITERS_PER_GALLON)


def test_stale_device_slot_falls_back_to_the_learned_weekday() -> None:
    """Friday's souvenir slot is skipped for four learned Fridays.

    The pinned resolution: 134.5 L with a 20.0 L spread, from the trailing 28
    noon-days — exactly ``MIN_BUCKET_SAMPLES`` Fridays, the point at which this
    branch first opens.
    """
    learned = learned_over(28)
    assert learned.count_for(FRIDAY) == MIN_BUCKET_SAMPLES

    resolved = expected_daily_liters(FRIDAY, make_inputs(), learned)

    assert resolved is not None
    expected, spread, source = resolved
    assert source == SOURCE_LEARNED_WEEKDAY
    assert expected == pytest.approx(134.5)
    assert spread == pytest.approx(20.0151, abs=1e-3)


def test_immature_learned_weekday_falls_back_to_the_overall_average() -> None:
    """Three Fridays are not a Friday baseline, so the household average stands.

    Over 21 noon-days the weekday branch is one sample short; the device's
    fresh 47 gal overall average pairs with the learned overall spread instead.
    """
    learned = learned_over(21)
    assert learned.count_for(FRIDAY) == MIN_BUCKET_SAMPLES - 1
    assert learned.overall_count >= LEARNED_DAILY_MIN_DAYS

    resolved = expected_daily_liters(FRIDAY, make_inputs(), learned)

    assert resolved is not None
    expected, spread, source = resolved
    assert source == SOURCE_OVERALL_AVERAGE
    assert expected == pytest.approx(47.0 * LITERS_PER_GALLON)
    assert expected == pytest.approx(177.914353848)
    assert spread == pytest.approx(58.5627, abs=1e-3)


def test_cold_start_resolves_nothing() -> None:
    """Two weeks in, with a stale slot, the honest answer is no expectation.

    13 assessable days is one short of ``LEARNED_DAILY_MIN_DAYS``, so nothing
    resolves and the daily detectors stay silent rather than inventing a
    baseline to compare against.
    """
    learned = learned_over(14)
    assert learned.overall_count == LEARNED_DAILY_MIN_DAYS - 1

    assert expected_daily_liters(FRIDAY, make_inputs(), learned) is None


def test_device_slot_without_deviation_borrows_the_learned_weekday_spread() -> None:
    """A fresher centre is worth keeping even when the spread must be borrowed."""
    slots = (*DEVICE_SLOTS[:6], WeekdaySlot(30.0, None, FRESH_STAMP))

    resolved = expected_daily_liters(FRIDAY, make_inputs(slots), learned_over(28))

    assert resolved is not None
    expected, spread, source = resolved
    assert source == SOURCE_DEVICE_AVERAGE
    assert expected == pytest.approx(30.0 * LITERS_PER_GALLON)
    assert spread == pytest.approx(20.0151, abs=1e-3)


def test_device_slot_without_deviation_borrows_the_overall_spread() -> None:
    """An immature weekday sends the borrowed spread one level up, not away."""
    slots = (*DEVICE_SLOTS[:6], WeekdaySlot(30.0, None, FRESH_STAMP))

    resolved = expected_daily_liters(FRIDAY, make_inputs(slots), learned_over(21))

    assert resolved is not None
    expected, spread, source = resolved
    assert source == SOURCE_DEVICE_AVERAGE
    assert expected == pytest.approx(113.56235352)
    assert spread == pytest.approx(58.5627, abs=1e-3)


def test_device_slot_without_any_spread_is_forfeited() -> None:
    """An expectation without a band is a guess, so the whole step is dropped.

    The slot is fresh and finite, but nothing anywhere can supply a spread for
    it, and the chain below it is equally cold — the result is ``None``, not a
    centre with an invented band.
    """
    slots = (*DEVICE_SLOTS[:6], WeekdaySlot(30.0, None, FRESH_STAMP))

    assert expected_daily_liters(FRIDAY, make_inputs(slots), learned_over(14)) is None


@pytest.mark.parametrize(
    "average",
    [
        pytest.param(0.0, id="zero-would-divide-every-ratio-by-zero"),
        pytest.param(-12.0, id="negative"),
        pytest.param(float("inf"), id="infinite"),
        pytest.param(float("nan"), id="not-a-number"),
    ],
)
def test_unusable_slot_average_is_rejected(average: float) -> None:
    """A cloud value that cannot be an expectation never becomes one."""
    slots = (*DEVICE_SLOTS[:6], WeekdaySlot(average, 4.0, FRESH_STAMP))
    learned = uniform_learned(120.0, 15.0, MIN_BUCKET_SAMPLES)

    resolved = expected_daily_liters(FRIDAY, make_inputs(slots), learned)

    assert resolved is not None
    expected, spread, source = resolved
    assert source == SOURCE_LEARNED_WEEKDAY
    assert expected == pytest.approx(120.0)
    assert spread == pytest.approx(15.0)


@pytest.mark.parametrize(
    "deviation",
    [
        pytest.param(-3.0, id="negative"),
        pytest.param(float("nan"), id="not-a-number"),
        pytest.param(float("inf"), id="infinite"),
    ],
)
def test_unusable_slot_deviation_borrows_a_learned_spread(deviation: float) -> None:
    """A broken deviation costs the band, not the fresher centre."""
    slots = (*DEVICE_SLOTS[:6], WeekdaySlot(30.0, deviation, FRESH_STAMP))
    learned = uniform_learned(120.0, 15.0, MIN_BUCKET_SAMPLES)

    resolved = expected_daily_liters(FRIDAY, make_inputs(slots), learned)

    assert resolved is not None
    expected, spread, source = resolved
    assert source == SOURCE_DEVICE_AVERAGE
    assert expected == pytest.approx(30.0 * LITERS_PER_GALLON)
    assert spread == pytest.approx(15.0)


def test_non_finite_learned_median_falls_through_to_the_overall_average() -> None:
    """A corrupt learned centre is rejected by the same gate as a cloud one."""
    learned = replace(
        uniform_learned(float("nan"), 5.0, 10),
        overall_median=150.0,
        overall_spread=30.0,
        overall_count=LEARNED_DAILY_MIN_DAYS,
        overall_mean=150.0,
    )

    resolved = expected_daily_liters(FRIDAY, make_inputs(()), learned)

    assert resolved is not None
    expected, spread, source = resolved
    assert source == SOURCE_OVERALL_AVERAGE
    assert expected == pytest.approx(47.0 * LITERS_PER_GALLON)
    assert spread == pytest.approx(30.0)


@pytest.mark.parametrize(
    ("overall_slot", "overall_count", "overall_spread", "resolves"),
    [
        pytest.param(DEVICE_OVERALL, LEARNED_DAILY_MIN_DAYS, 30.0, True, id="resolves"),
        pytest.param(
            WeekdaySlot(47.0, None, datetime(2026, 6, 1, tzinfo=UTC)),
            LEARNED_DAILY_MIN_DAYS,
            30.0,
            False,
            id="stale-overall-slot",
        ),
        pytest.param(
            DEVICE_OVERALL,
            LEARNED_DAILY_MIN_DAYS - 1,
            30.0,
            False,
            id="one-day-short-of-two-weekly-cycles",
        ),
        pytest.param(
            DEVICE_OVERALL, LEARNED_DAILY_MIN_DAYS, None, False, id="no-learned-spread"
        ),
        pytest.param(
            WeekdaySlot(None, None, FRESH_STAMP),
            LEARNED_DAILY_MIN_DAYS,
            30.0,
            False,
            id="device-reports-no-overall-average",
        ),
    ],
)
def test_overall_average_needs_freshness_and_two_weekly_cycles(
    overall_slot: WeekdaySlot,
    overall_count: int,
    overall_spread: float | None,
    *,
    resolves: bool,
) -> None:
    """The last step of the chain is gated on both of its ingredients."""
    learned = replace(
        uniform_learned(None, None, 0),
        overall_median=150.0,
        overall_spread=overall_spread,
        overall_count=overall_count,
        overall_mean=150.0,
    )

    resolved = expected_daily_liters(FRIDAY, make_inputs((), overall_slot), learned)

    assert (resolved is not None) is resolves
    if resolved is not None:
        assert resolved[2] == SOURCE_OVERALL_AVERAGE


def test_missing_weekday_slot_index_is_skipped() -> None:
    """A device that reports fewer than seven slots is not indexed off its end."""
    learned = uniform_learned(120.0, 15.0, MIN_BUCKET_SAMPLES)

    short = expected_daily_liters(FRIDAY, make_inputs(DEVICE_SLOTS[:3]), learned)
    empty = expected_daily_liters(FRIDAY, make_inputs(()), learned)

    assert short is not None
    assert short[2] == SOURCE_LEARNED_WEEKDAY
    assert empty == short


@pytest.mark.parametrize(
    ("count", "source"),
    [
        pytest.param(MIN_BUCKET_SAMPLES - 1, SOURCE_OVERALL_AVERAGE, id="immature"),
        pytest.param(MIN_BUCKET_SAMPLES, SOURCE_LEARNED_WEEKDAY, id="just-mature"),
    ],
)
def test_learned_weekday_gate_is_exact(count: int, source: str) -> None:
    """The weekday branch opens at exactly ``MIN_BUCKET_SAMPLES`` samples."""
    learned = replace(
        uniform_learned(120.0, 15.0, count),
        overall_median=150.0,
        overall_spread=30.0,
        overall_count=LEARNED_DAILY_MIN_DAYS,
        overall_mean=150.0,
    )

    resolved = expected_daily_liters(FRIDAY, make_inputs(()), learned)

    assert resolved is not None
    assert resolved[2] == source


def test_forecast_for_the_day_after_the_real_capture() -> None:
    """Tuesday's forecast comes from the device's own fresh Tuesday slot.

    The measured replay: 35.0 gal / 132.5 L, source ``device_average``, band
    90.85 L, weekday ``tuesday``, one person. The band is three scaled spreads,
    so it is stated in the forecast rather than left for a template to
    reconstruct.
    """
    forecast = forecast_for(FORECAST_DAY, make_inputs(), learned_over(28))

    assert forecast.gallons == pytest.approx(35.0)
    assert forecast.liters == pytest.approx(132.48941244)
    assert forecast.source == SOURCE_DEVICE_AVERAGE
    assert forecast.band_liters == pytest.approx(90.84988282, abs=1e-6)
    assert forecast.weekday == "tuesday"
    assert forecast.persons == 1


def test_forecast_band_is_three_scaled_spreads() -> None:
    """``band_liters`` is ``ANALYTICS_K`` spreads and the gallons match the litres."""
    slots = (*DEVICE_SLOTS[:6], WeekdaySlot(30.0, 4.0, FRESH_STAMP))

    forecast = forecast_for(FRIDAY, make_inputs(slots), uniform_learned(None, None, 0))

    assert forecast.liters is not None
    assert forecast.gallons is not None
    assert forecast.band_liters == pytest.approx(ANALYTICS_K * 4.0 * LITERS_PER_GALLON)
    assert forecast.gallons * LITERS_PER_GALLON == pytest.approx(forecast.liters)
    assert forecast.weekday == "friday"


def test_unresolved_forecast_still_describes_the_day() -> None:
    """With no expectation the forecast keeps what it does know.

    The weekday label and the occupancy estimate describe the day and the
    household rather than the expectation, so they survive a chain that
    resolves to nothing — the sensor goes unknown, its attributes do not.
    """
    learned = replace(uniform_learned(None, None, 0), overall_mean=444.0)

    forecast = forecast_for(FRIDAY, make_inputs(()), learned)

    assert forecast.gallons is None
    assert forecast.liters is None
    assert forecast.source is None
    assert forecast.band_liters is None
    assert forecast.weekday == "friday"
    assert forecast.persons == 2


@pytest.mark.parametrize(
    ("mean", "persons"),
    [
        pytest.param(None, None, id="no-history-at-all"),
        pytest.param(float("nan"), None, id="corrupt-mean"),
        pytest.param(0.0, 0, id="empty-house"),
        pytest.param(-500.0, 0, id="never-negative"),
        pytest.param(OCCUPANCY_LITERS_PER_PERSON * 0.75, 1, id="reference-household"),
        pytest.param(OCCUPANCY_LITERS_PER_PERSON * 3.4, 3, id="rounds-down"),
        pytest.param(OCCUPANCY_LITERS_PER_PERSON * 3.6, 4, id="rounds-up"),
    ],
)
def test_persons_estimate_rounds_and_never_goes_negative(
    mean: float | None, persons: int | None
) -> None:
    """Occupancy is the mean daily use over the REU per-capita reference.

    The mean, not the median: the reference figure is itself a mean and
    occupancy is a volume question, so the heavy right tail belongs in it.
    """
    learned = replace(uniform_learned(None, None, 0), overall_mean=mean)

    assert forecast_for(FRIDAY, make_inputs(()), learned).persons == persons


def test_learned_daily_ignores_unassessable_and_unusable_days() -> None:
    """Only assessable days with a finite total are learned from.

    A day the meter did not bound is a data gap, and importing gaps as low
    usage would drag every baseline down and mask the next quiet week.
    """
    monday = date(2026, 7, 6)
    days: list[tuple[date, float | None, bool]] = [
        (monday, 100.0, True),
        (monday + timedelta(days=7), 200.0, True),
        (monday + timedelta(days=14), 5.0, False),  # unbounded gap day
        (monday + timedelta(days=21), None, True),  # series does not reach back
        (monday + timedelta(days=28), float("nan"), True),  # corrupt total
    ]

    learned = LearnedDaily.from_days(days)

    assert learned.count_for(monday) == 2
    assert learned.median_for(monday) == pytest.approx(150.0)
    assert learned.spread_for(monday) == pytest.approx(MAD_SCALE * 50.0)
    assert learned.overall_count == 2
    assert learned.overall_median == pytest.approx(150.0)
    assert learned.overall_mean == pytest.approx(150.0)


def test_learned_daily_keys_weekdays_python_style() -> None:
    """Learned statistics are indexed by ``date.weekday()``, not by device slot.

    The device's Map B rotation applies to its own slots only; mixing the two
    orders up would silently compare Saturdays against Mondays.
    """
    week = [date(2026, 7, 6) + timedelta(days=offset) for offset in range(7)]
    learned = LearnedDaily.from_days(
        [(day, 10.0 * (day.weekday() + 1), True) for day in week]
    )

    assert learned.weekday_median[0] == pytest.approx(10.0)  # Monday
    for day in week:
        assert learned.median_for(day) == pytest.approx(10.0 * (day.weekday() + 1))
        assert learned.spread_for(day) == pytest.approx(0.0)
        assert learned.count_for(day) == 1
    assert learned.overall_count == 7
    assert learned.overall_median == pytest.approx(40.0)


def test_learned_daily_from_no_days_is_empty() -> None:
    """A device with no imported history learns nothing and claims nothing."""
    learned = LearnedDaily.from_days([])

    assert learned.weekday_median == (None,) * 7
    assert learned.weekday_spread == (None,) * 7
    assert learned.weekday_count == (0,) * 7
    assert learned.overall_median is None
    assert learned.overall_spread is None
    assert learned.overall_mean is None
    assert learned.overall_count == 0


def test_real_learned_statistics_stay_robust_across_windows() -> None:
    """The learned Friday tightens as the window shortens, and stays sane.

    A guard against a silent regression in :meth:`LearnedDaily.from_days`
    keying or filtering: over the full baseline window the reference household
    has 18 assessable Fridays and 125 assessable days overall, and its mean
    daily use (167 L) is the number the occupancy estimate rounds to one
    person.
    """
    learned = learned_over(182)

    assert learned.count_for(FRIDAY) == 18
    assert learned.overall_count == 125
    assert learned.overall_median == pytest.approx(163.0)
    assert learned.overall_mean == pytest.approx(167.496, abs=1e-3)
    assert forecast_for(FORECAST_DAY, make_inputs(), learned).persons == 1
