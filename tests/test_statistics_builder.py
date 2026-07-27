"""Tests for the pure helpers of :mod:`custom_components.aquahome.statistics`.

Everything exercised here is deliberately hass-free: the unit allow-list, the
resolution merge, the DST-aware request chunker and the meter-row builder are
plain functions over explicit instants, so they are tested directly rather than
through a coordinator, a recorder or a faked cloud. Those layers are covered by
``test_statistics_coordinator.py``; this file is the algorithmic net underneath
them, which is where a silent arithmetic regression would otherwise hide.

None of these functions read a clock, so the file needs no ``freezer``: every
instant in it is an explicit literal and every expectation is derived from that
literal, never from wall-clock time. The zones are real ones with real
transitions — Europe/Warsaw for the DST cases (the reference device's own zone)
and Asia/Kolkata for the half-hour offset that no European zone can produce.

The final test is the integration of all four parts against the *real* captured
graph payloads: the July hourly window merged over the eleven-month daily
window, converted from the account's litres and reduced to statistics rows. Its
expected numbers were computed independently from the fixtures and cross-checked
against the yearly totals and the live lifetime counter, so they pin the whole
pipeline, not just its pieces.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any, Final
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.recorder.models import StatisticData

from custom_components.aquahome.const import TOTAL_WATER_CLAMP_TOLERANCE
from custom_components.aquahome.statistics import (
    build_meter_rows,
    local_chunks,
    merge_resolutions,
    normalize_volume_unit,
)
from tests.conftest import load_fixture

#: Litres in one US gallon — the exact factor the allow-list inverts.
LITRES_PER_GALLON: Final = 3.785411784
#: The factor a litre reading must be multiplied by to reach gallons.
LITRES_TO_GALLONS: Final = 1 / LITRES_PER_GALLON

#: The reference device's zone: a full DST zone, spring 2026 forward on the
#: 29th of March and autumn 2025 back on the 26th of October.
WARSAW: Final = ZoneInfo("Europe/Warsaw")
#: A half-hour-offset zone, whose local midnights never land on a UTC hour.
KOLKATA: Final = ZoneInfo("Asia/Kolkata")

#: Base UTC hour bucket the row-algorithm series are minted from.
BASE_HOUR: Final = datetime(2026, 7, 10, 6, 0, tzinfo=UTC)


def hour(offset: int) -> datetime:
    """Return the UTC hour bucket ``offset`` hours after :data:`BASE_HOUR`."""
    return BASE_HOUR + timedelta(hours=offset)


def local(text: str) -> datetime:
    """Return an aware datetime from an ISO instant literal.

    Bucket labels arrive from the cloud in exactly this shape — a local time
    carrying the fixed UTC offset of its request — so the tests spell them the
    same way the payloads do.
    """
    return datetime.fromisoformat(text)


def starts(rows: list[StatisticData]) -> list[datetime]:
    """Return the bucket start of every row."""
    return [row["start"] for row in rows]


def states(rows: list[StatisticData]) -> list[float]:
    """Return the meter reading of every row."""
    return [row["state"] for row in rows]


def sums(rows: list[StatisticData]) -> list[float]:
    """Return the accumulated volume of every row."""
    return [row["sum"] for row in rows]


def fixture_readings(name: str) -> list[tuple[datetime, float]]:
    """Return the nonzero ``(label, reading)`` pairs of a captured graph payload.

    Mirrors what the coordinator does with a response: parse the label, drop the
    ``0`` placeholders the always-zero-filled series uses for "no reading in
    this bucket".
    """
    payload: dict[str, Any] = load_fixture(name)
    points: list[dict[str, Any]] = payload["data"]
    readings: list[tuple[datetime, float]] = []
    for point in points:
        label, value = point.get("label"), point.get("value")
        if label is None or value is None or value <= 0:
            continue
        readings.append((local(label), float(value)))
    return readings


# ---------------------------------------------------------------------------
# normalize_volume_unit — the fail-closed unit allow-list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("units", "expected"),
    [
        ("Liters", LITRES_TO_GALLONS),
        ("liters", LITRES_TO_GALLONS),
        ("  LITERS\t", LITRES_TO_GALLONS),
        ("Gallons", 1.0),
        ("gallons", 1.0),
        (" GALLONS ", 1.0),
    ],
    ids=[
        "liters-as-sent",
        "liters-lowercase",
        "liters-padded-uppercase",
        "gallons-as-sent",
        "gallons-lowercase",
        "gallons-padded-uppercase",
    ],
)
def test_known_units_convert_to_gallons(units: str, expected: float) -> None:
    """Both English units are accepted regardless of case and padding."""
    assert normalize_volume_unit(units) == pytest.approx(expected)


@pytest.mark.parametrize(
    "units",
    [None, "Litry", "Liter", "m3", "", "   ", "cubic meters"],
    ids=[
        "missing",
        "polish-liters",
        "english-singular",
        "cubic-metres",
        "empty",
        "blank",
        "unknown-english",
    ],
)
def test_unknown_units_are_rejected(units: str | None) -> None:
    """Anything outside the allow-list is fatal, never guessed at.

    ``"Litry"`` is the important one: the server localizes this field, so a
    correctly spelled unit in the account's own language must still be refused
    rather than silently treated as gallons (community PAIN #5).
    """
    assert normalize_volume_unit(units) is None


def test_localized_units_of_the_captured_polish_payload_are_rejected() -> None:
    """The real ``accept-language: pl`` capture fails the allow-list."""
    payload: dict[str, Any] = load_fixture("graph-usage-daily-pl.json")
    assert payload["units"] == "Litry"
    assert normalize_volume_unit(payload["units"]) is None


def test_captured_english_payload_parses_as_litres() -> None:
    """The pinned-language captures carry a unit the allow-list knows."""
    payload: dict[str, Any] = load_fixture("graph-meter-daily.json")
    assert normalize_volume_unit(payload["units"]) == pytest.approx(LITRES_TO_GALLONS)


# ---------------------------------------------------------------------------
# build_meter_rows — the row algorithm
# ---------------------------------------------------------------------------


def test_first_row_without_an_anchor_is_a_baseline() -> None:
    """A fresh series opens on the reading itself, contributing no water.

    The water that reading counts was consumed before the series existed, so
    importing it as a delta would invent history.
    """
    rows = build_meter_rows([(hour(0), 100.0)], None, None)

    assert rows == [StatisticData(start=hour(0), state=100.0, sum=0.0)]


def test_consecutive_readings_are_diffed_into_the_running_total() -> None:
    """Each row carries its raw reading and the accumulated volume so far."""
    readings = [(hour(0), 100.0), (hour(1), 110.0), (hour(2), 115.5)]

    rows = build_meter_rows(readings, None, None)

    assert starts(rows) == [hour(0), hour(1), hour(2)]
    assert states(rows) == [100.0, 110.0, 115.5]
    assert sums(rows) == pytest.approx([0.0, 10.0, 15.5])


def test_anchor_continues_the_stored_series_without_a_baseline_row() -> None:
    """A resume run diffs its first reading against the anchor, losing nothing."""
    readings = [(hour(0), 100.0), (hour(1), 110.0), (hour(2), 115.5)]

    rows = build_meter_rows(readings, 90.0, 500.0)

    assert starts(rows) == [hour(0), hour(1), hour(2)]
    assert states(rows) == [100.0, 110.0, 115.5]
    # 500 + 10 for the very first row: no baseline is swallowed on a resume.
    assert sums(rows) == pytest.approx([510.0, 520.0, 525.5])


def test_anchor_state_without_a_stored_sum_starts_the_total_at_zero() -> None:
    """A known previous reading still diffs even when no total came with it."""
    rows = build_meter_rows([(hour(0), 100.0)], 90.0, None)

    assert sums(rows) == pytest.approx([10.0])


@pytest.mark.parametrize(
    ("dipped", "expected_starts", "expected_states", "expected_sums"),
    [
        (
            98.0,
            [hour(0), hour(2)],
            [100.0, 105.0],
            [0.0, 5.0],
        ),
        (
            100.0 * (1 - TOTAL_WATER_CLAMP_TOLERANCE),
            [hour(0), hour(2)],
            [100.0, 105.0],
            [0.0, 5.0],
        ),
    ],
    ids=["inside-tolerance", "exactly-on-tolerance"],
)
def test_small_dip_is_skipped_and_the_next_delta_spans_it(
    dipped: float,
    expected_starts: list[datetime],
    expected_states: list[float],
    expected_sums: list[float],
) -> None:
    """A glitch dip drops its bucket and leaves the pre-dip reading as reference.

    Skipping rather than clamping is what keeps the glitch from inventing either
    a negative delta or a counter reset: the next reading is diffed against the
    reading before the dip, so the water is attributed, just an hour late.
    """
    readings = [(hour(0), 100.0), (hour(1), dipped), (hour(2), 105.0)]

    rows = build_meter_rows(readings, None, None)

    assert starts(rows) == expected_starts
    assert states(rows) == expected_states
    assert sums(rows) == pytest.approx(expected_sums)


def test_drop_past_the_tolerance_is_a_genuine_counter_reset() -> None:
    """A replaced board restarts the counter, and its whole value is the delta."""
    readings = [(hour(0), 100.0), (hour(1), 10.0), (hour(2), 25.0)]

    rows = build_meter_rows(readings, None, None)

    assert starts(rows) == [hour(0), hour(1), hour(2)]
    assert states(rows) == [100.0, 10.0, 25.0]
    assert sums(rows) == pytest.approx([0.0, 10.0, 25.0])


def test_duplicate_starts_keep_the_first_reading() -> None:
    """Overlapping request windows repeat a bucket; the repeat is ignored."""
    readings = [(hour(0), 100.0), (hour(0), 999.0), (hour(1), 110.0)]

    rows = build_meter_rows(readings, None, None)

    assert starts(rows) == [hour(0), hour(1)]
    assert states(rows) == [100.0, 110.0]
    assert sums(rows) == pytest.approx([0.0, 10.0])


def test_readings_are_sorted_before_being_diffed() -> None:
    """Chunked fetches arrive out of order and must not corrupt the deltas."""
    ordered = [(hour(0), 100.0), (hour(1), 110.0), (hour(2), 115.5)]
    shuffled = [ordered[2], ordered[0], ordered[1]]

    assert build_meter_rows(shuffled, None, None) == build_meter_rows(
        ordered, None, None
    )


def test_no_readings_produce_no_rows() -> None:
    """An empty window imports nothing rather than a phantom baseline."""
    assert build_meter_rows([], None, None) == []


# ---------------------------------------------------------------------------
# merge_resolutions — hourly detail over daily depth
# ---------------------------------------------------------------------------


def test_hourly_day_replaces_its_own_daily_reading() -> None:
    """A day with hourly coverage is imported hour by hour, daily row dropped."""
    hourly = [
        (local("2026-07-10T08:00:00+02:00"), 200.0),
        (local("2026-07-10T09:00:00+02:00"), 210.0),
    ]
    daily = [
        (local("2026-07-09T00:00:00+02:00"), 150.0),
        (local("2026-07-10T00:00:00+02:00"), 999.0),
    ]

    merged = merge_resolutions(hourly, daily, WARSAW)

    assert merged == [
        # The daily-only day, moved onto its true local midnight.
        (datetime(2026, 7, 8, 22, 0, tzinfo=UTC), 150.0),
        (datetime(2026, 7, 10, 6, 0, tzinfo=UTC), 200.0),
        (datetime(2026, 7, 10, 7, 0, tzinfo=UTC), 210.0),
    ]


def test_daily_label_an_hour_off_midnight_lands_on_the_right_day() -> None:
    """Labels carry the request's offset, which a DST change makes stale.

    A window opened before the spring transition labels every later day at
    ``+01:00``, so the 31st of March is reported an hour before its true local
    midnight. It must still be imported as the 31st, at ``00:00+02:00``.
    """
    daily = [(local("2026-03-31T00:00:00+01:00"), 500.0)]

    merged = merge_resolutions([], daily, WARSAW)

    assert merged == [(datetime(2026, 3, 30, 22, 0, tzinfo=UTC), 500.0)]
    assert merged[0][0].astimezone(WARSAW) == local("2026-03-31T00:00:00+02:00")


def test_output_is_sorted_by_instant() -> None:
    """Readings from separate windows come back as one ascending series."""
    hourly = [
        (local("2026-07-10T09:00:00+02:00"), 210.0),
        (local("2026-07-10T08:00:00+02:00"), 200.0),
    ]
    daily = [
        (local("2026-07-08T00:00:00+02:00"), 100.0),
        (local("2026-07-09T00:00:00+02:00"), 150.0),
    ]

    merged = merge_resolutions(hourly, daily, WARSAW)

    assert [start for start, _ in merged] == sorted(start for start, _ in merged)
    assert [value for _, value in merged] == [100.0, 150.0, 200.0, 210.0]


def test_readings_inside_one_utc_hour_are_deduped_first_wins() -> None:
    """Statistics rows are hour-keyed, so a second reading in an hour is dropped.

    Nothing is lost by it: the water it counted simply lands in the next hour's
    delta, because the following reading is diffed against the surviving one.
    """
    hourly = [
        (local("2026-07-10T08:30:00+02:00"), 222.0),
        (local("2026-07-10T08:00:00+02:00"), 200.0),
    ]

    merged = merge_resolutions(hourly, [], WARSAW)

    assert merged == [(datetime(2026, 7, 10, 6, 0, tzinfo=UTC), 200.0)]


def test_half_hour_offset_zone_floors_onto_the_top_of_the_utc_hour() -> None:
    """A ``+05:30`` local midnight is not a UTC hour, so it is floored to one."""
    hourly = [(local("2026-07-10T08:00:00+05:30"), 300.0)]
    daily = [(local("2026-07-11T00:00:00+05:30"), 400.0)]

    merged = merge_resolutions(hourly, daily, KOLKATA)

    assert merged == [
        # 02:30Z floored back to 02:00Z.
        (datetime(2026, 7, 10, 2, 0, tzinfo=UTC), 300.0),
        # The 11th's local midnight is 18:30Z, floored back to 18:00Z.
        (datetime(2026, 7, 10, 18, 0, tzinfo=UTC), 400.0),
    ]
    assert all(start.minute == 0 for start, _ in merged)


def test_partial_hourly_day_keeps_the_usage_before_its_first_reading() -> None:
    """Hourly coverage starting mid-day loses no water, only attribution detail.

    The first hourly reading of the day is diffed against the previous day's
    daily reading, so everything drawn before it is carried by that one delta.
    """
    daily = [(local("2026-07-09T00:00:00+02:00"), 1000.0)]
    hourly = [
        (local("2026-07-10T10:00:00+02:00"), 1050.0),
        (local("2026-07-10T11:00:00+02:00"), 1060.0),
    ]

    rows = build_meter_rows(merge_resolutions(hourly, daily, WARSAW), 990.0, 100.0)

    assert starts(rows) == [
        datetime(2026, 7, 8, 22, 0, tzinfo=UTC),
        datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
        datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
    ]
    # 50 units of pre-10:00 usage land whole on the first hourly bucket.
    assert sums(rows) == pytest.approx([110.0, 160.0, 170.0])


def test_no_readings_merge_to_nothing() -> None:
    """Two empty windows are not an error, just an empty series."""
    assert merge_resolutions([], [], WARSAW) == []


# ---------------------------------------------------------------------------
# local_chunks — request windows that keep bucket labels honest
# ---------------------------------------------------------------------------


def assert_chunks_are_contiguous(
    chunks: list[tuple[datetime, datetime]],
    start_local: datetime,
    end_local: datetime,
) -> None:
    """Assert the windows tile ``[start_local, end_local]`` without a gap."""
    assert chunks
    assert chunks[0][0] == start_local
    assert chunks[-1][1] == end_local
    for earlier, later in pairwise(chunks):
        assert earlier[1] == later[0]


def test_spring_transition_splits_on_the_first_true_local_midnight() -> None:
    """The window is cut where the offset changes, not where the clocks do.

    Warsaw springs forward at 02:00 on 2026-03-29, so that day still *starts* at
    ``+01:00``; the first midnight carrying the new offset is the 30th, and that
    is where the next request must begin for its labels to be right.
    """
    start_local = datetime(2026, 3, 25, tzinfo=WARSAW)
    end_local = datetime(2026, 4, 5, tzinfo=WARSAW)

    chunks = list(local_chunks(WARSAW, start_local, end_local, 366))

    boundary = local("2026-03-30T00:00:00+02:00")
    assert chunks == [(start_local, boundary), (boundary, end_local)]
    assert boundary.utcoffset() == timedelta(hours=2)
    assert start_local.utcoffset() == timedelta(hours=1)
    assert_chunks_are_contiguous(chunks, start_local, end_local)


def test_autumn_transition_splits_on_the_first_true_local_midnight() -> None:
    """Warsaw falls back at 03:00 on 2025-10-26; the 27th opens at ``+01:00``."""
    start_local = datetime(2025, 10, 22, tzinfo=WARSAW)
    end_local = datetime(2025, 11, 2, tzinfo=WARSAW)

    chunks = list(local_chunks(WARSAW, start_local, end_local, 366))

    boundary = local("2025-10-27T00:00:00+01:00")
    assert chunks == [(start_local, boundary), (boundary, end_local)]
    assert boundary.utcoffset() == timedelta(hours=1)
    assert start_local.utcoffset() == timedelta(hours=2)
    assert_chunks_are_contiguous(chunks, start_local, end_local)


@pytest.mark.parametrize(
    ("max_days", "expected_days"),
    [(3, [3, 3, 3, 1]), (1, [1] * 10), (0, [1] * 10)],
    ids=["three-day-windows", "one-day-windows", "zero-clamped-to-one"],
)
def test_max_days_splits_a_range_without_a_transition(
    max_days: int, expected_days: list[int]
) -> None:
    """Inside one offset regime the only cut is the size ceiling."""
    start_local = datetime(2026, 5, 1, tzinfo=WARSAW)
    end_local = datetime(2026, 5, 11, tzinfo=WARSAW)

    chunks = list(local_chunks(WARSAW, start_local, end_local, max_days))

    assert [(end - start).days for start, end in chunks] == expected_days
    assert_chunks_are_contiguous(chunks, start_local, end_local)


@pytest.mark.parametrize(
    ("start_local", "end_local"),
    [
        (datetime(2026, 5, 1, tzinfo=WARSAW), datetime(2026, 5, 1, tzinfo=WARSAW)),
        (datetime(2026, 5, 2, tzinfo=WARSAW), datetime(2026, 5, 1, tzinfo=WARSAW)),
    ],
    ids=["empty-range", "inverted-range"],
)
def test_degenerate_range_yields_no_windows(
    start_local: datetime, end_local: datetime
) -> None:
    """A resume run with nothing to fetch must issue no request at all."""
    assert list(local_chunks(WARSAW, start_local, end_local, 366)) == []


def test_long_range_cuts_only_on_midnights_of_the_offset_in_force() -> None:
    """Every window of a multi-year sweep opens on a true local midnight.

    That is the whole point of the chunker: a boundary that is an hour off would
    label a whole response's day, month or year buckets an hour off with it.
    """
    start_local = datetime(2024, 1, 1, tzinfo=WARSAW)
    end_local = datetime(2026, 12, 1, tzinfo=WARSAW)

    chunks = list(local_chunks(WARSAW, start_local, end_local, 366))

    assert_chunks_are_contiguous(chunks, start_local, end_local)
    for start, end in chunks:
        assert (start.hour, start.minute, start.second) == (0, 0, 0)
        assert (end.hour, end.minute, end.second) == (0, 0, 0)
        # A window never straddles a transition, so its own offset is stable.
        assert start.utcoffset() == (end - timedelta(days=1)).utcoffset()
    # Every cut is the first midnight *after* a transition, which in 2024 falls
    # a day later than in 2026 because the transition itself lands on the 31st.
    boundaries = {start for start, _ in chunks}
    assert local("2024-04-01T00:00:00+02:00") in boundaries
    assert local("2024-10-28T00:00:00+01:00") in boundaries
    assert local("2026-03-30T00:00:00+02:00") in boundaries
    assert local("2026-10-26T00:00:00+01:00") in boundaries


# ---------------------------------------------------------------------------
# Integration of the parts against the captured payloads
# ---------------------------------------------------------------------------


def test_captured_hourly_and_daily_windows_build_the_expected_series() -> None:
    """Merge, convert and reduce the real captures into the ground-truth rows.

    The July hourly window covers 300 buckets over the last 27 days; the daily
    window reaches back to the first retained reading on 2025-09-14. Every day
    the hourly window covers drops its daily reading, so the merged series is
    300 hourly rows plus the 105 older daily-only ones, and the last state is
    the hourly 180529 L rather than the daily 180526 L of the same day.

    The expected values were computed independently from the fixtures and
    cross-checked two ways: the final reading equals the live lifetime counter
    the ``total_water`` sensor reports, and the total volume matches the yearly
    payload's own year-over-year difference to within daily rounding.
    """
    hourly = fixture_readings("graph-meter-hourly.json")
    daily = fixture_readings("graph-meter-daily.json")
    factor = normalize_volume_unit(load_fixture("graph-meter-hourly.json")["units"])
    assert factor is not None
    assert (len(hourly), len(daily)) == (300, 131)

    merged = merge_resolutions(hourly, daily, WARSAW)
    rows = build_meter_rows(
        [(start, value * factor) for start, value in merged], None, None
    )

    assert len(rows) == 405
    assert all(start.tzinfo is UTC and start.minute == 0 for start in starts(rows))
    assert states(rows) == sorted(states(rows))
    assert sums(rows) == sorted(sums(rows))

    assert rows[0]["start"] == datetime(2025, 9, 13, 22, 0, tzinfo=UTC)
    assert rows[0]["state"] == pytest.approx(42122.7621, abs=1e-4)
    assert rows[0]["sum"] == 0.0

    assert rows[-1]["start"] == datetime(2026, 7, 27, 7, 0, tzinfo=UTC)
    assert rows[-1]["state"] == pytest.approx(47690.7164, abs=1e-4)
    assert rows[-1]["sum"] == pytest.approx(5567.9543, abs=1e-4)
    # 21077 L of treated water over the whole retained history.
    assert rows[-1]["sum"] * LITRES_PER_GALLON == pytest.approx(21077.0, abs=1.0)


def test_captured_empty_hourly_window_yields_no_readings() -> None:
    """Past the hourly retention floor the payload is zero-filled, not empty."""
    payload: dict[str, Any] = load_fixture("graph-meter-hourly-empty.json")

    assert payload["data"]
    assert fixture_readings("graph-meter-hourly-empty.json") == []
