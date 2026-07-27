"""Tests for :mod:`custom_components.aquahome.analytics.series`.

The series module is the analytics tier's arithmetic floor: every night verdict,
every baseline bucket and every daily total is derived from the primitives here,
so a silent regression in one of them would surface downstream as a plausible —
and wrong — leak, anomaly or vacation. It is also pure, so this file is
deliberately hass-free and clock-free: no config entry, no coordinator, no
recorder, no ``freezer``. Every instant is an explicit literal and every
expectation is derived from that literal or from a captured fixture, never from
wall-clock time.

Three kinds of case run here.

*Semantics.* Small hand-built series pin what the module considers *certain*: a
one-hour interval is knowledge, a zero-delta span proves zeros for every hour it
fully covers, a positive multi-hour span proves nothing about any single hour of
it, and a backwards step proves nothing at all (it is clamped for volume
purposes but never admitted as a proven zero — the one path by which a glitch
could talk a real leak out of existence).

*Calendars.* Real zones with real transitions, because the whole module works in
local wall clock. Europe/Warsaw is the reference device's own zone: spring
forward on 2026-03-29 (no 02:00 exists) and fall back on 2026-10-25 (02:00 runs
twice, and the two passes share one dictionary key by design). Asia/Kolkata
supplies the half-hour offset no European zone can produce, where the hourly
statistics grid never aligns with the local clock and only proven zeros survive.

*Replay.* The canonical merged real history from :mod:`tests.analytics_traces` —
405 readings spanning 2025-09-13 to 2026-07-27 — with the noon-day totals,
certain-hour counts and draw counts pinned to the numbers measured when the
analytics tier was frozen. Those pins are the regression net for the whole
analytics tier: the detector suites replay the same series and reason about
these exact values.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from statistics import median
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

import pytest

from custom_components.aquahome.analytics.series import (
    bounded,
    build_intervals,
    counter_at,
    day_total_liters,
    event_count,
    hour_knowledge,
    largest_event_liters,
    noon_days,
    reading_hours,
)
from tests.analytics_traces import real_readings, real_regen_windows

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import tzinfo

    from custom_components.aquahome.analytics.model import Reading

#: Litres in one US gallon, spelled out here so the expectations are independent
#: of the production constant they check.
LITRES_PER_GALLON: Final = 3.785411784

#: The reference device's zone — every fixture label carries its offsets.
WARSAW: Final = ZoneInfo("Europe/Warsaw")

#: A half-hour-offset zone: its local hour-starts never land on a UTC hour.
KOLKATA: Final = ZoneInfo("Asia/Kolkata")

#: The slack :func:`bounded` allows on each side of an assessed window.
BOUND_SLACK: Final = timedelta(hours=48)


def local(spec: str, tz: tzinfo) -> datetime:
    """Return the aware local instant ``"YYYY-MM-DD HH:MM"`` names in ``tz``."""
    return datetime.fromisoformat(spec).replace(tzinfo=tz)


def noon(day: date, tz: tzinfo) -> datetime:
    """Return local noon on ``day``, the instant a noon-day boundary falls on."""
    return datetime.combine(day, time(12), tzinfo=tz)


def rows(*pairs: tuple[datetime, float]) -> tuple[Reading, ...]:
    """Return explicit ``(instant, gallons)`` pairs as a UTC reading series."""
    return tuple((instant.astimezone(UTC), gallons) for instant, gallons in pairs)


def hourly(first: datetime, gallons: Sequence[float]) -> tuple[Reading, ...]:
    """Return one reading per absolute hour from ``first``, carrying ``gallons``.

    Stepping in absolute hours is what the imported statistics series does, so a
    daylight-saving transition shows up in the *local* labels of the result and
    nowhere else — which is exactly the situation the module has to survive.
    """
    start = first.astimezone(UTC)
    return tuple(
        (start + timedelta(hours=index), value) for index, value in enumerate(gallons)
    )


def litres(gallons: float) -> float:
    """Return ``gallons`` in litres."""
    return gallons * LITRES_PER_GALLON


def window_hours(
    knowledge: dict[datetime, float], day: date, tz: tzinfo
) -> set[datetime]:
    """Return the certain hours of ``day``'s 01-07 minimum-night-flow window."""
    opening = datetime.combine(day, time(1), tzinfo=tz)
    closing = datetime.combine(day, time(7), tzinfo=tz)
    return {hour for hour in knowledge if opening <= hour < closing}


@pytest.fixture(scope="module")
def real_series() -> tuple[Reading, ...]:
    """Return the replayed real meter history in the reading convention."""
    return real_readings()


# ---------------------------------------------------------------------------
# build_intervals — consecutive-reading deltas
# ---------------------------------------------------------------------------


def test_intervals_span_every_consecutive_pair_of_readings() -> None:
    """Each interval carries its two instants and the water between them."""
    readings = hourly(local("2026-07-10 08:00", WARSAW), [100.0, 102.5, 102.5, 110.0])

    intervals = build_intervals(readings)

    assert [(start, end) for start, end, _ in intervals] == [
        (before[0], after[0]) for before, after in pairwise(readings)
    ]
    assert [gallons for *_, gallons in intervals] == pytest.approx([2.5, 0.0, 7.5])


@pytest.mark.parametrize(
    "count",
    [0, 1],
    ids=["empty-series", "single-reading"],
)
def test_a_series_shorter_than_two_readings_spans_nothing(count: int) -> None:
    """One reading is a counter value, not a measurement of anything."""
    readings = hourly(local("2026-07-10 08:00", WARSAW), [100.0][:count])

    assert build_intervals(readings) == []


def test_a_backwards_step_is_clamped_to_zero_rather_than_negative_usage() -> None:
    """A residual counter glitch is never water somebody used.

    The statistics import already absorbs genuine meter resets by restarting its
    accumulation, so anything still negative in the stored series is corruption,
    and letting it through would subtract phantom water from a daily total.
    """
    readings = hourly(local("2026-07-10 08:00", WARSAW), [100.0, 90.0, 95.0])

    assert [gallons for *_, gallons in build_intervals(readings)] == pytest.approx(
        [0.0, 5.0]
    )


# ---------------------------------------------------------------------------
# hour_knowledge — what the meter actually proves about a single hour
# ---------------------------------------------------------------------------


def test_a_one_hour_interval_pins_that_hour_to_its_delta() -> None:
    """An interval spanning exactly one clock hour is knowledge about it."""
    readings = hourly(local("2026-07-10 08:00", WARSAW), [100.0, 102.5])

    knowledge = hour_knowledge(readings, WARSAW)

    assert set(knowledge) == {local("2026-07-10 08:00", WARSAW)}
    assert knowledge[local("2026-07-10 08:00", WARSAW)] == pytest.approx(litres(2.5))


def test_a_positive_multi_hour_interval_leaves_all_its_hours_unknown() -> None:
    """Water inside a gap cannot be attributed to any one hour of it.

    Spreading the delta over the span would fabricate night flow the meter never
    saw and poison the hour-of-week baseline with invented samples, so every
    hour of the span stays absent — and absent means unknown, never zero.
    """
    readings = rows(
        (local("2026-07-10 08:00", WARSAW), 100.0),
        (local("2026-07-10 11:00", WARSAW), 106.0),
    )

    assert hour_knowledge(readings, WARSAW) == {}


def test_an_hour_long_interval_off_the_clock_hour_proves_nothing() -> None:
    """An hour of water straddling two clock hours belongs to neither."""
    readings = rows(
        (local("2026-07-10 08:30", WARSAW), 100.0),
        (local("2026-07-10 09:30", WARSAW), 104.0),
    )

    assert hour_knowledge(readings, WARSAW) == {}


def test_a_zero_delta_span_proves_a_zero_for_every_hour_it_covers() -> None:
    """The counter standing still is certainty about the whole span."""
    readings = rows(
        (local("2026-07-10 08:00", WARSAW), 100.0),
        (local("2026-07-10 13:00", WARSAW), 100.0),
    )

    knowledge = hour_knowledge(readings, WARSAW)

    assert set(knowledge) == {
        local(f"2026-07-10 {hour:02d}:00", WARSAW) for hour in range(8, 13)
    }
    assert set(knowledge.values()) == {0.0}


def test_a_zero_delta_span_skips_the_hours_it_only_partly_covers() -> None:
    """A span proves nothing about an hour that started before it did."""
    readings = rows(
        (local("2026-07-10 08:20", WARSAW), 100.0),
        (local("2026-07-10 13:00", WARSAW), 100.0),
    )

    assert set(hour_knowledge(readings, WARSAW)) == {
        local(f"2026-07-10 {hour:02d}:00", WARSAW) for hour in range(9, 13)
    }


@pytest.mark.parametrize(
    "closing_hour",
    ["09:00", "13:00"],
    ids=["one-hour-step", "multi-hour-step"],
)
def test_a_backwards_step_is_skipped_instead_of_proving_a_zero(
    closing_hour: str,
) -> None:
    """A glitched reading is not evidence that no water flowed.

    :func:`build_intervals` clamps the delta so it can never invent usage, but
    admitting the clamped zero here would let one corrupt row certify a quiet
    night and suppress a genuine leak verdict.
    """
    readings = rows(
        (local("2026-07-10 08:00", WARSAW), 100.0),
        (local(f"2026-07-10 {closing_hour}", WARSAW), 90.0),
    )

    assert hour_knowledge(readings, WARSAW) == {}


@pytest.mark.parametrize(
    "count",
    [0, 1],
    ids=["empty-series", "single-reading"],
)
def test_hour_knowledge_of_a_series_that_spans_nothing_is_empty(count: int) -> None:
    """Certainty needs two readings; anything less yields an empty mapping."""
    readings = hourly(local("2026-07-10 08:00", WARSAW), [100.0][:count])

    assert hour_knowledge(readings, WARSAW) == {}


# ---------------------------------------------------------------------------
# reading_hours — the complementary push evidence
# ---------------------------------------------------------------------------


def test_reading_hours_collapses_every_push_to_its_local_hour_start() -> None:
    """Several pushes inside one hour are one hour of evidence, not three."""
    readings = rows(
        (local("2026-07-10 08:00", WARSAW), 100.0),
        (local("2026-07-10 08:20", WARSAW), 101.0),
        (local("2026-07-10 08:59", WARSAW), 102.0),
        (local("2026-07-10 10:05", WARSAW), 103.0),
    )

    assert reading_hours(readings, WARSAW) == {
        local("2026-07-10 08:00", WARSAW),
        local("2026-07-10 10:00", WARSAW),
    }


def test_reading_hours_of_an_empty_series_is_empty() -> None:
    """No pushes, no evidence — and no exception."""
    assert reading_hours((), WARSAW) == set()


# ---------------------------------------------------------------------------
# counter_at — step interpolation of a meter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        ("2026-07-10 07:59", None),
        ("2026-07-10 08:00", 100.0),
        ("2026-07-10 09:30", 110.0),
        ("2026-07-10 10:00", 115.0),
        ("2026-07-12 06:00", 115.0),
    ],
    ids=[
        "before-the-first-reading",
        "exactly-on-a-reading",
        "between-two-readings",
        "exactly-on-the-last-reading",
        "long-after-the-last-reading",
    ],
)
def test_counter_at_holds_the_last_reading_in_effect(
    instant: str, expected: float | None
) -> None:
    """A meter keeps its value until the next push, and cannot be extrapolated back."""
    readings = hourly(local("2026-07-10 08:00", WARSAW), [100.0, 110.0, 115.0])

    assert counter_at(readings, local(instant, WARSAW)) == expected


# ---------------------------------------------------------------------------
# day_total_liters — the noon-to-noon cut
# ---------------------------------------------------------------------------


def test_a_day_is_cut_at_local_noon_not_at_midnight() -> None:
    """Evening usage stays with the day it belongs to.

    The 13:00 draw of the 9th opens the 10th's day, and the 13:00 draw of the
    10th opens the 11th's — a midnight cut would split the evening-into-night
    activity a daily total is supposed to hold together.
    """
    readings = rows(
        (local("2026-07-09 11:00", WARSAW), 1000.0),
        (local("2026-07-09 13:00", WARSAW), 1010.0),
        (local("2026-07-10 11:00", WARSAW), 1050.0),
        (local("2026-07-10 13:00", WARSAW), 1060.0),
    )

    assert day_total_liters(readings, date(2026, 7, 10), WARSAW) == pytest.approx(
        litres(50.0)
    )


def test_water_that_flowed_while_the_meter_was_silent_still_lands_in_a_day() -> None:
    """A gap's whole delta is charged to the day its closing reading falls in.

    That is honest meter arithmetic, and it is why a total alone never justifies
    a verdict: :func:`bounded` is what decides whether the coverage underneath
    it was good enough to reason about.
    """
    readings = rows(
        (local("2026-07-09 10:00", WARSAW), 1000.0),
        (local("2026-07-10 11:00", WARSAW), 1100.0),
    )

    assert day_total_liters(readings, date(2026, 7, 10), WARSAW) == pytest.approx(
        litres(100.0)
    )
    assert day_total_liters(readings, date(2026, 7, 11), WARSAW) == 0.0


def test_a_day_opening_before_the_series_has_no_total() -> None:
    """A meter cannot be extrapolated backwards past its first reading."""
    readings = rows((local("2026-07-10 13:00", WARSAW), 1000.0))

    assert day_total_liters(readings, date(2026, 7, 10), WARSAW) is None


def test_a_backwards_counter_never_yields_a_negative_day() -> None:
    """Corruption reads as unjudgeable, never as a certainly quiet day."""
    readings = rows(
        (local("2026-07-09 11:00", WARSAW), 1100.0),
        (local("2026-07-10 11:00", WARSAW), 1000.0),
    )

    assert day_total_liters(readings, date(2026, 7, 10), WARSAW) is None


# ---------------------------------------------------------------------------
# largest_event_liters / event_count — the draw-shape features
# ---------------------------------------------------------------------------


def test_only_draws_lying_entirely_inside_the_day_are_events() -> None:
    """A draw straddling a noon boundary is attributed to neither day.

    Both boundary intervals here are far bigger than the interior ones, so the
    day would be misread as containing a shower if either leaked in.
    """
    readings = rows(
        (local("2026-07-09 11:30", WARSAW), 100.0),
        (local("2026-07-09 12:30", WARSAW), 120.0),
        (local("2026-07-09 18:00", WARSAW), 125.0),
        (local("2026-07-10 09:00", WARSAW), 130.0),
        (local("2026-07-10 12:30", WARSAW), 200.0),
    )

    assert largest_event_liters(readings, date(2026, 7, 10), WARSAW) == pytest.approx(
        litres(5.0)
    )
    assert event_count(readings, date(2026, 7, 10), WARSAW) == 2


def test_a_day_holding_at_most_one_push_has_no_measured_draw() -> None:
    """With no interval inside the day the largest draw is unknown, not zero."""
    readings = rows(
        (local("2026-07-09 10:00", WARSAW), 100.0),
        (local("2026-07-10 09:00", WARSAW), 130.0),
        (local("2026-07-11 09:00", WARSAW), 140.0),
    )

    assert largest_event_liters(readings, date(2026, 7, 10), WARSAW) is None
    assert event_count(readings, date(2026, 7, 10), WARSAW) == 0


def test_a_zero_delta_interval_is_not_a_draw() -> None:
    """Counting stationary intervals would invent occupancy out of silence."""
    readings = rows(
        (local("2026-07-09 12:30", WARSAW), 100.0),
        (local("2026-07-09 15:00", WARSAW), 100.0),
        (local("2026-07-09 18:00", WARSAW), 105.0),
    )

    assert event_count(readings, date(2026, 7, 10), WARSAW) == 1
    assert largest_event_liters(readings, date(2026, 7, 10), WARSAW) == pytest.approx(
        litres(5.0)
    )


# ---------------------------------------------------------------------------
# bounded — the assessability gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("before_slip", "after_slip", "expected"),
    [
        (0, 0, True),
        (1, 0, False),
        (0, 1, False),
        (1, 1, False),
    ],
    ids=[
        "both-exactly-48h-out",
        "opening-evidence-too-old",
        "closing-evidence-too-late",
        "no-evidence-either-side",
    ],
)
def test_a_window_is_assessable_only_within_48_hours_on_each_side(
    before_slip: int, after_slip: int, expected: bool
) -> None:
    """Both bounds are inclusive; a single second past either is fatal.

    Without evidence on both sides, silence inside the window is
    indistinguishable from missing data — device offline, cloud bucket aged out,
    backfill behind — and any verdict drawn from it would be fiction.
    """
    opening = local("2026-07-10 01:00", WARSAW)
    closing = local("2026-07-10 07:00", WARSAW)
    readings = rows(
        (opening - BOUND_SLACK - timedelta(seconds=before_slip), 100.0),
        (closing + BOUND_SLACK + timedelta(seconds=after_slip), 130.0),
    )

    assert bounded(readings, opening, closing) is expected


def test_a_reading_on_the_opening_boundary_alone_does_not_bound_a_window() -> None:
    """Evidence that the window closed cleanly is a separate requirement."""
    opening = local("2026-07-10 01:00", WARSAW)
    closing = local("2026-07-10 07:00", WARSAW)

    assert bounded(rows((opening, 100.0)), opening, closing) is False
    assert bounded(rows((opening, 100.0), (closing, 100.0)), opening, closing) is True


def test_an_empty_series_bounds_nothing() -> None:
    """No readings, no assessment — the honest answer, not a crash."""
    assert (
        bounded(
            (),
            local("2026-07-10 01:00", WARSAW),
            local("2026-07-10 07:00", WARSAW),
        )
        is False
    )


# ---------------------------------------------------------------------------
# noon_days — the assessed day window
# ---------------------------------------------------------------------------


def test_noon_days_returns_completed_days_oldest_first() -> None:
    """Once local noon has passed, today's noon-day is complete and included."""
    assert noon_days(3, local("2026-07-27 12:00", WARSAW), WARSAW) == [
        date(2026, 7, 25),
        date(2026, 7, 26),
        date(2026, 7, 27),
    ]


def test_the_running_day_is_excluded_before_local_noon() -> None:
    """A partial day cannot be compared against a full day's expectation."""
    assert noon_days(3, local("2026-07-27 11:59", WARSAW), WARSAW) == [
        date(2026, 7, 24),
        date(2026, 7, 25),
        date(2026, 7, 26),
    ]


@pytest.mark.parametrize("window", [0, -1], ids=["zero-window", "negative-window"])
def test_a_nonpositive_window_yields_no_days(window: int) -> None:
    """A degenerate window is empty rather than an error or a wrapped range."""
    assert noon_days(window, local("2026-07-27 12:00", WARSAW), WARSAW) == []


def test_noon_days_follows_the_device_zone_not_utc() -> None:
    """The same instant closes a different noon-day in a different zone.

    07:00 UTC is still morning in Warsaw but past noon in Kolkata, so the device
    zone — not the HA host's — decides which day is assessable.
    """
    now = datetime(2026, 7, 27, 7, 0, tzinfo=UTC)

    assert noon_days(1, now, WARSAW) == [date(2026, 7, 26)]
    assert noon_days(1, now, KOLKATA) == [date(2026, 7, 27)]


# ---------------------------------------------------------------------------
# Daylight-saving transitions (Europe/Warsaw, the reference device's zone)
# ---------------------------------------------------------------------------


def test_a_spring_forward_night_simply_carries_one_fewer_hour() -> None:
    """The nonexistent 02:00 never appears, and nothing else changes.

    2026-03-29 jumps 02:00 to 03:00 in Warsaw, so the 01-07 window holds five
    real hours instead of six. Every one of them is still a whole clock hour of
    the same absolute length, so the classifier sees five honest samples rather
    than a gap it has to special-case.
    """
    readings = hourly(
        local("2026-03-29 00:00", WARSAW), [100.0 + 2 * step for step in range(8)]
    )

    knowledge = hour_knowledge(readings, WARSAW)

    assert window_hours(knowledge, date(2026, 3, 29), WARSAW) == {
        local("2026-03-29 01:00", WARSAW),
        local("2026-03-29 03:00", WARSAW),
        local("2026-03-29 04:00", WARSAW),
        local("2026-03-29 05:00", WARSAW),
        local("2026-03-29 06:00", WARSAW),
    }
    assert local("2026-03-29 02:00", WARSAW) not in knowledge
    assert list(knowledge.values()) == pytest.approx([litres(2.0)] * 7)


def test_a_fall_back_night_keeps_the_second_pass_of_its_repeated_hour() -> None:
    """The repeated 02:00 shares one key, and the later pass wins.

    2026-10-25 runs 02:00 twice in Warsaw. Same-zone datetimes compare and hash
    by their digits — which is exactly what lets a caller address either pass
    with the ``datetime.combine`` it builds its windows from — so both passes
    land on one key. Both are honest values for a real hour; the window carries
    six certain hours across seven absolute ones.
    """
    readings = hourly(
        local("2026-10-25 00:00", WARSAW),
        [0.0, 2.0, 4.0, 7.0, 12.0, 13.0, 14.0, 15.0, 16.0],
    )

    knowledge = hour_knowledge(readings, WARSAW)
    repeated = datetime.combine(date(2026, 10, 25), time(2), tzinfo=WARSAW)

    assert knowledge[repeated] == pytest.approx(litres(5.0))
    assert knowledge[repeated.replace(fold=1)] == pytest.approx(litres(5.0))
    assert window_hours(knowledge, date(2026, 10, 25), WARSAW) == {
        local("2026-10-25 01:00", WARSAW),
        repeated,
        local("2026-10-25 03:00", WARSAW),
        local("2026-10-25 04:00", WARSAW),
        local("2026-10-25 05:00", WARSAW),
        local("2026-10-25 06:00", WARSAW),
    }


def test_the_two_passes_of_a_repeated_hour_are_one_reading_hour() -> None:
    """Push evidence collapses with the clock, so the hour counts once."""
    readings = hourly(
        local("2026-10-25 00:00", WARSAW),
        [0.0, 2.0, 4.0, 7.0, 12.0, 13.0, 14.0, 15.0, 16.0],
    )

    hours = reading_hours(readings, WARSAW)

    assert len(readings) == 9
    assert len(hours) == 8
    assert datetime.combine(date(2026, 10, 25), time(2), tzinfo=WARSAW) in hours


def test_a_fall_back_day_totals_all_twenty_five_of_its_hours() -> None:
    """The noon-to-noon cut is wall clock, so the long day is simply longer."""
    readings = hourly(
        local("2026-10-24 12:00", WARSAW), [1000.0 + step for step in range(26)]
    )

    assert day_total_liters(readings, date(2026, 10, 25), WARSAW) == pytest.approx(
        litres(25.0)
    )


# ---------------------------------------------------------------------------
# Half-hour-offset zones (Asia/Kolkata)
# ---------------------------------------------------------------------------


def test_a_half_hour_offset_zone_yields_no_single_hour_certainty() -> None:
    """The hourly grid never aligns with the local clock there.

    Every statistics bucket lands at half past the local hour, so no interval is
    ever exactly one local clock hour and the detectors fall back on push
    evidence alone — a graceful degradation, not a wrong answer.
    """
    readings = hourly(
        datetime(2026, 6, 1, 0, 0, tzinfo=UTC), [500.0 + 2 * step for step in range(6)]
    )

    assert hour_knowledge(readings, KOLKATA) == {}
    assert reading_hours(readings, KOLKATA) == {
        local(f"2026-06-01 {hour:02d}:00", KOLKATA) for hour in range(5, 11)
    }


def test_a_half_hour_offset_zone_still_proves_its_zeros() -> None:
    """A stationary counter is certainty in any zone, edges excluded."""
    readings = rows(
        (datetime(2026, 6, 1, 0, 0, tzinfo=UTC), 500.0),
        (datetime(2026, 6, 1, 6, 0, tzinfo=UTC), 500.0),
    )

    knowledge = hour_knowledge(readings, KOLKATA)

    assert set(knowledge) == {
        local(f"2026-06-01 {hour:02d}:00", KOLKATA) for hour in range(6, 11)
    }
    assert set(knowledge.values()) == {0.0}


# ---------------------------------------------------------------------------
# Totality — every helper answers, none raises
# ---------------------------------------------------------------------------


def test_every_helper_is_total_on_an_empty_series() -> None:
    """An engine that runs before the first backfill must not explode."""
    empty: tuple[Reading, ...] = ()
    day = date(2026, 7, 10)

    assert build_intervals(empty) == []
    assert hour_knowledge(empty, WARSAW) == {}
    assert reading_hours(empty, WARSAW) == set()
    assert counter_at(empty, noon(day, WARSAW)) is None
    assert day_total_liters(empty, day, WARSAW) is None
    assert largest_event_liters(empty, day, WARSAW) is None
    assert event_count(empty, day, WARSAW) == 0
    assert bounded(empty, noon(day - timedelta(days=1), WARSAW), noon(day, WARSAW)) is (
        False
    )


def test_a_single_reading_reads_as_a_counter_and_nothing_else() -> None:
    """It positions the meter, but proves nothing about any window."""
    readings = rows((local("2026-07-10 08:00", WARSAW), 100.0))

    assert reading_hours(readings, WARSAW) == {local("2026-07-10 08:00", WARSAW)}
    assert counter_at(readings, local("2026-07-10 09:00", WARSAW)) == 100.0
    assert day_total_liters(readings, date(2026, 7, 10), WARSAW) is None
    assert day_total_liters(readings, date(2026, 7, 11), WARSAW) == 0.0
    assert largest_event_liters(readings, date(2026, 7, 11), WARSAW) is None
    assert event_count(readings, date(2026, 7, 11), WARSAW) == 0
    assert (
        bounded(
            readings,
            local("2026-07-11 01:00", WARSAW),
            local("2026-07-11 07:00", WARSAW),
        )
        is False
    )


# ---------------------------------------------------------------------------
# Replay of the real captured history (the analytics tier's regression net)
# ---------------------------------------------------------------------------


def test_the_replayed_series_is_the_canonical_production_shape(
    real_series: tuple[Reading, ...],
) -> None:
    """405 monotone readings, matching the Phase-5 recorder ground truth."""
    assert len(real_series) == 405
    assert real_series[0][0] == datetime(2025, 9, 13, 22, 0, tzinfo=UTC)
    assert real_series[0][1] == pytest.approx(42122.7621)
    assert real_series[-1][0] == datetime(2026, 7, 27, 7, 0, tzinfo=UTC)
    assert real_series[-1][1] == pytest.approx(47690.7164)
    assert all(after >= before for (_, before), (_, after) in pairwise(real_series))


def test_the_replayed_series_is_activity_driven(
    real_series: tuple[Reading, ...],
) -> None:
    """No backwards step anywhere, and exactly one stationary interval.

    The lone zero-delta interval is the 2025 autumn gap between two daily
    captures; every other push moved water, which is the premise the whole
    no-push rule rests on.
    """
    intervals = build_intervals(real_series)
    stationary = [interval for interval in intervals if interval[2] == 0.0]

    assert len(intervals) == 404
    assert not [interval for interval in intervals if interval[2] < 0.0]
    assert len(stationary) == 1
    assert stationary[0][0].astimezone(WARSAW) == local("2025-10-19 00:00", WARSAW)
    assert stationary[0][1].astimezone(WARSAW) == local("2025-10-29 00:00", WARSAW)


def test_the_replayed_certain_hours_split_into_usage_and_proven_zeros(
    real_series: tuple[Reading, ...],
) -> None:
    """198 measured hours plus 240 proven zeros — the baseline's whole diet.

    The zero run spans 241 absolute hours but contributes only 240 keys: it
    crosses the 2025-10-26 fall-back, whose repeated 02:00 collapses onto one
    key. The 198 positive hours are exactly the one-hour intervals of the
    series, so nothing was invented from a gap.
    """
    knowledge = hour_knowledge(real_series, WARSAW)
    positive = {hour for hour, value in knowledge.items() if value > 0.0}
    zeros = {hour for hour, value in knowledge.items() if value == 0.0}
    stationary = next(iv for iv in build_intervals(real_series) if iv[2] == 0.0)
    single_hour = [
        interval
        for interval in build_intervals(real_series)
        if interval[1] - interval[0] == timedelta(hours=1)
    ]

    assert len(knowledge) == 438
    assert len(positive) == len(single_hour) == 198
    assert (stationary[1] - stationary[0]) / timedelta(hours=1) == 241
    assert len(zeros) == 240
    assert datetime.combine(date(2025, 10, 26), time(2), tzinfo=WARSAW) in zeros


def test_the_replayed_pushes_each_own_a_local_hour(
    real_series: tuple[Reading, ...],
) -> None:
    """The imported series is hourly, so pushes and reading hours agree."""
    assert len(reading_hours(real_series, WARSAW)) == len(real_series) == 405


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 7, 20), 326.0),
        (date(2026, 7, 25), 30.0),
        (date(2026, 7, 26), 0.0),
        (date(2026, 7, 27), 49.0),
    ],
    ids=[
        "the-one-real-anomaly-day",
        "first-day-away",
        "second-day-away",
        "return-day",
    ],
)
def test_pinned_real_noon_day_totals(
    real_series: tuple[Reading, ...], day: date, expected: float
) -> None:
    """The re-pinned July totals, on the canonical merged series.

    07-26 is 0 L rather than 27 L because the merge drops that day's daily row
    in favour of the surrounding hourly coverage; the water it carried lands in
    the 07-27 noon-day together with the owner's return morning.
    """
    assert day_total_liters(real_series, day, WARSAW) == pytest.approx(expected)


def test_july_noon_day_statistics_of_the_real_series(
    real_series: tuple[Reading, ...],
) -> None:
    """The robust daily statistics the detectors' bands are built from.

    Derived here rather than copied, because the canonical merged series moved
    these numbers away from the raw-fixture ones they were first frozen
    against: 27 complete noon-days, median 159 L and a scaled MAD of about
    79 L, running from the 0 L second day away up to the 326 L peak of 07-20,
    which sits 2.1 scaled MADs above the median.
    """
    totals = [
        day_total_liters(real_series, date(2026, 7, day), WARSAW)
        for day in range(1, 28)
    ]
    values = [total for total in totals if total is not None]
    centre = median(values)
    scaled_mad = 1.4826 * median([abs(value - centre) for value in values])

    assert len(values) == len(totals) == 27
    assert centre == pytest.approx(159.0)
    assert scaled_mad == pytest.approx(78.5778, abs=1e-3)
    assert min(values) == 0.0
    assert max(values) == pytest.approx(326.0)
    assert (max(values) - centre) / scaled_mad == pytest.approx(2.126, abs=1e-3)


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 7, 20), 84.0),
        (date(2026, 7, 25), 12.0),
        (date(2026, 7, 26), None),
    ],
    ids=["shower-sized-draw", "quiet-day-draw", "day-without-a-push"],
)
def test_pinned_real_largest_events(
    real_series: tuple[Reading, ...], day: date, expected: float | None
) -> None:
    """The vacation rule's volume feature, including its absent case.

    07-26 holds no interval at all, so its largest draw is unmeasured — which
    the occupancy logic must read as "no event", never as a large one.
    """
    largest = largest_event_liters(real_series, day, WARSAW)

    if expected is None:
        assert largest is None
    else:
        assert largest == pytest.approx(expected)


def test_the_empty_day_and_the_return_morning_differ_in_draw_count(
    real_series: tuple[Reading, ...],
) -> None:
    """The draw counter separates an empty house from a frugal morning.

    07-26 shows no draws at all. The owner's return on 07-27 shows several,
    even though the day's volume is small — and since the occupancy rule allows
    at most one draw, that count alone is what breaks the vacation streak.
    """
    assert event_count(real_series, date(2026, 7, 26), WARSAW) == 0
    assert event_count(real_series, date(2026, 7, 27), WARSAW) >= 2


def test_the_real_july_nights_are_all_assessable(
    real_series: tuple[Reading, ...],
) -> None:
    """Daily rows keep every night bounded, even across the quiet days away."""
    nights = [date(2026, 7, day) for day in range(1, 28)]

    assert all(
        bounded(
            real_series,
            datetime.combine(night, time(1), tzinfo=WARSAW),
            datetime.combine(night, time(7), tzinfo=WARSAW),
        )
        for night in nights
    )


def test_a_window_before_the_real_series_opens_is_unassessable(
    real_series: tuple[Reading, ...],
) -> None:
    """History starts on 2025-09-13; nothing earlier can be judged."""
    assert (
        bounded(
            real_series,
            local("2025-09-10 01:00", WARSAW),
            local("2025-09-10 07:00", WARSAW),
        )
        is False
    )


def test_a_regeneration_never_moves_the_outlet_meter(
    real_series: tuple[Reading, ...],
) -> None:
    """The regeneration draw does not pass the outlet meter in bulk.

    Across all three July regenerations — the ones inside the hourly-covered
    window — the counter in effect at the end of the window equals the one at
    its start. Masking those nights is therefore about not reading brine-cycle
    activity as a leak signature, not about protecting daily totals from a
    phantom draw.
    """
    july = [
        window
        for window in real_regen_windows()
        if window[0].astimezone(WARSAW).date().month == 7
    ]

    assert len(july) == 3
    for start, end in july:
        opening = counter_at(real_series, start)
        assert opening is not None
        assert counter_at(real_series, end) == opening
