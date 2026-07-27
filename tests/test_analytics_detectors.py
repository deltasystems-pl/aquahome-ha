"""Tests for :mod:`custom_components.aquahome.analytics.detectors`.

This file holds the analytics tier's **exit criteria**. Two of them are
replays of the reference household's real captured history:

1. Replaying eleven months of real meter readings against the real
   regeneration history must produce **zero** leak verdicts — every assessable
   night NO_LEAK, every regeneration night MASKED, the leak binary off, the
   anomaly binary off and the vacation binary off on the owner's genuine
   two-day absence. A detector that cries wolf on the only ground truth we have
   is worthless however elegant its statistics.
2. Overlaying a synthetic constant leak on that same history must fire, at the
   tier the implied daily volume earns — and must stay silent when a
   regeneration covers every injected night, because masking outranks evidence.

The remainder scores the detectors against seeded synthetic households with
known labels (Matthews correlation, the metric the analytics research mandates
for these heavily imbalanced classes) and pins the individual rules: night
classification precedence, the leak debounce and its tri-state, the 72-hour
persistent-flow rule, the drift charts' upward-only consensus, Hampel cleaning,
and the redesigned occupancy evidence paths the vacation verdict rests on.

Nothing here touches Home Assistant, and nothing here reads a clock: every
``now`` is an explicit literal handed to a pure function, so the file is
hermetic by construction rather than by freezing. All thresholds come from
``const``; the only bare numbers are the measured ground-truth pins.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from custom_components.aquahome.analytics import baseline, detectors, series
from custom_components.aquahome.analytics.detectors import compute_analytics
from custom_components.aquahome.analytics.model import (
    REASON_DAILY_HIGH,
    SOURCE_DEVICE_AVERAGE,
    TIER_INFO,
    TIER_URGENT,
    TIER_WARNING,
    AnalyticsInputs,
    AnalyticsResult,
    DayAssessment,
    LeakState,
    NightAssessment,
    NightVerdict,
    WeekdaySlot,
)
from custom_components.aquahome.const import (
    ANALYTICS_K,
    ASSESSABLE_BOUND_HOURS,
    DETECTOR_WINDOW_DAYS,
    LEAK_CONSECUTIVE_NIGHTS,
    LEAK_TIER_INFO_LITERS_PER_DAY,
    LEAK_TIER_WARNING_LITERS_PER_DAY,
    PERSISTENT_FLOW_HOURS,
    VACATION_MAX_EVENTS,
    VACATION_MIN_DAYS,
    VACATION_RATIO,
)
from custom_components.aquahome.salt import LITERS_PER_GALLON
from tests.analytics_traces import (
    ConfusionCounts,
    SyntheticHousehold,
    inject_leak,
    real_readings,
    real_regen_windows,
)
from tests.conftest import load_fixture

if TYPE_CHECKING:
    from collections.abc import Sequence

    from custom_components.aquahome.analytics.model import Reading

#: The reference device's zone, and the zone every fixture label carries.
TZ: Final = ZoneInfo("Europe/Warsaw")
#: IANA key of :data:`TZ`, as the engine resolves it into the inputs.
TZ_KEY: Final = "Europe/Warsaw"

#: The replay instant of the ground truth: 12:30 device-local on the day after
#: the capture's last reading window closed.
REPLAY_NOW: Final = datetime(2026, 7, 27, 10, 30, tzinfo=UTC)

#: An arbitrary quiet night used by the classifier unit tests, and an instant
#: safely after its window has closed.
UNIT_NIGHT: Final = date(2026, 6, 10)
UNIT_NOW: Final = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
#: Regeneration coverage reaching well before every unit-test night.
UNIT_COVERAGE: Final = datetime(2026, 1, 1, tzinfo=UTC)

#: Daylight-saving transition nights of :data:`TZ` in 2026: 02:00 local never
#: happens in spring, and 02:00 local happens twice in autumn.
SPRING_FORWARD_NIGHT: Final = date(2026, 3, 29)
AUTUMN_FALL_BACK_NIGHT: Final = date(2026, 10, 25)
#: Window hours those nights carry (a normal night carries six).
SPRING_WINDOW_HOURS: Final = 5
AUTUMN_WINDOW_HOURS: Final = 6

#: Per-hour draw of the synthetic classifier nights, in gallons.
UNIT_HOURLY_GALLONS: Final = 2.0

#: A deterministic seven-value wobble standing in for household noise. Every
#: value is distinct, so a seven-wide Hampel window has a defined scale (a
#: constant window has none and is deliberately left untouched).
_WOBBLE: Final = (-9.0, 14.0, -3.0, 8.0, -14.0, 5.0, -1.0)

#: Daily-total levels the drift-chart series are built from, in liters.
_DRIFT_LEVEL: Final = 170.0
_DRIFT_STEP_UP: Final = 255.0
_DRIFT_STEP_DOWN: Final = 85.0
_DRIFT_STABLE_DAYS: Final = 39
_DRIFT_SHIFTED_DAYS: Final = 21

#: Household size of the synthetic MCC scenarios.
_SYNTHETIC_WEEKS: Final = 10
#: Ten-day leak span of the leak-MCC households (day indices from day zero).
_MCC_LEAK_START: Final = 50
_MCC_LEAK_END: Final = 60
#: Ten-day absence span of the occupancy-MCC households.
_MCC_VACATION_START: Final = 45
_MCC_VACATION_END: Final = 55
#: A leak rate comfortably above the ~1 gal/h push-detection floor.
_MCC_LEAK_LPH: Final = 6.0
#: The MCC floor both classifiers must clear (the tier's exit criterion).
MCC_FLOOR: Final = 0.9

#: An empty device slot: the synthetic households have no cloud averages, so
#: their expectations come from the locally learned statistics alone.
EMPTY_SLOT: Final = WeekdaySlot(average_gal=None, deviation_gal=None, updated_at=None)
EMPTY_SLOTS: Final = (EMPTY_SLOT,) * 7


def _properties() -> dict[str, Any]:
    """Return the captured raw-property map of the reference device."""
    properties: dict[str, Any] = load_fixture("properties.json")["properties"]
    return properties


def _device_slot(average_name: str, deviation_name: str | None) -> WeekdaySlot:
    """Build one weekday slot from the real captured properties.

    Mirrors what the engine does with ``scaled_value`` — the averages carry no
    divisor, so the raw value is already gallons — and keeps the property's
    change-stamp, which is what the freshness guard turns on.
    """
    properties = _properties()
    average = properties[average_name]
    deviation = properties[deviation_name] if deviation_name is not None else None
    stamp = str(average["updated_at"]).replace("Z", "+00:00")
    return WeekdaySlot(
        average_gal=float(average["value"]),
        deviation_gal=float(deviation["value"]) if deviation is not None else None,
        updated_at=datetime.fromisoformat(stamp),
    )


def _real_weekday_slots() -> tuple[WeekdaySlot, ...]:
    """Return the device's seven captured weekday slots, slot 1 first."""
    return tuple(
        _device_slot(
            f"avg_daily_use_day_{index}_gals", f"avg_daily_dev_day_{index}_gals"
        )
        for index in range(1, 8)
    )


def _analytics_inputs(  # noqa: PLR0913 - one keyword per scenario knob
    readings: tuple[Reading, ...],
    regen_windows: tuple[tuple[datetime, datetime], ...],
    *,
    now: datetime,
    weekday_slots: tuple[WeekdaySlot, ...] = EMPTY_SLOTS,
    overall_average: WeekdaySlot = EMPTY_SLOT,
    device_online: bool = True,
    statistics_fresh: bool = True,
) -> AnalyticsInputs:
    """Assemble one pass's inputs the way the engine assembles them."""
    return AnalyticsInputs(
        readings=readings,
        regen_windows=regen_windows,
        regen_coverage_start=regen_windows[0][0] if regen_windows else None,
        weekday_slots=weekday_slots,
        overall_average=overall_average,
        tz_key=TZ_KEY,
        now=now,
        device_online=device_online,
        statistics_fresh=statistics_fresh,
    )


def _replay(
    readings: tuple[Reading, ...] | None = None,
    *,
    regen_windows: tuple[tuple[datetime, datetime], ...] | None = None,
    now: datetime = REPLAY_NOW,
) -> AnalyticsResult:
    """Run one analytics pass over the replayed real history."""
    return compute_analytics(
        _analytics_inputs(
            real_readings() if readings is None else readings,
            real_regen_windows() if regen_windows is None else regen_windows,
            now=now,
            weekday_slots=_real_weekday_slots(),
            overall_average=_device_slot("avg_daily_use_gals", None),
        )
    )


def _household(  # noqa: PLR0913 - one keyword per scenario knob
    *,
    seed: int,
    leak_liters_per_hour: float = 0.0,
    leak_start_day: int | None = None,
    leak_end_day: int | None = None,
    vacation_start_day: int | None = None,
    vacation_end_day: int | None = None,
) -> SyntheticHousehold:
    """Generate a ten-week seeded synthetic household."""
    return SyntheticHousehold.generate(
        weeks=_SYNTHETIC_WEEKS,
        seed=seed,
        leak_liters_per_hour=leak_liters_per_hour,
        leak_start_day=leak_start_day,
        leak_end_day=leak_end_day,
        vacation_start_day=vacation_start_day,
        vacation_end_day=vacation_end_day,
    )


def _household_now(house: SyntheticHousehold) -> datetime:
    """Return local noon of a household's last day.

    Every night of the trace has closed and every noon-day is complete by then,
    so the detector window sees the whole scenario.
    """
    return datetime.combine(house.last_day, time(12), tzinfo=TZ).astimezone(UTC)


def _household_result(
    house: SyntheticHousehold,
    *,
    readings: tuple[Reading, ...] | None = None,
    device_online: bool = True,
    statistics_fresh: bool = True,
) -> AnalyticsResult:
    """Run one analytics pass over a synthetic household."""
    return compute_analytics(
        _analytics_inputs(
            house.readings if readings is None else readings,
            house.regen_windows,
            now=_household_now(house),
            device_online=device_online,
            statistics_fresh=statistics_fresh,
        )
    )


def _local(day: date, hour: int) -> datetime:
    """Return a local wall-clock instant in the device's zone."""
    return datetime.combine(day, time(hour), tzinfo=TZ)


def _chain(start: datetime, deltas: Sequence[float]) -> tuple[Reading, ...]:
    """Build readings one absolute hour apart, consuming ``deltas`` gallons.

    Each consecutive pair spans exactly one hour, which is the only interval
    shape :func:`~.series.hour_knowledge` can attribute to a single clock hour —
    so every hour of the chain carries certain usage.
    """
    readings: list[Reading] = [(start.astimezone(UTC), 1000.0)]
    counter = 1000.0
    cursor = start.astimezone(UTC)
    for delta in deltas:
        cursor += timedelta(hours=1)
        counter += delta
        readings.append((cursor, counter))
    return tuple(readings)


def _classify(
    readings: tuple[Reading, ...],
    night: date = UNIT_NIGHT,
    *,
    masked: frozenset[date] = frozenset(),
    coverage: datetime | None = UNIT_COVERAGE,
) -> NightAssessment:
    """Classify one night from a hand-built reading series."""
    return detectors.classify_night(
        night,
        series.hour_knowledge(readings, TZ),
        series.reading_hours(readings, TZ),
        readings,
        masked,
        coverage,
        TZ,
    )


def _leaking_night(night: date = UNIT_NIGHT, hours: int = 7) -> tuple[Reading, ...]:
    """Build a night whose every window hour registered continuous flow."""
    return _chain(_local(night, 0), [UNIT_HOURLY_GALLONS] * hours)


def _night_run(*verdicts: NightVerdict) -> tuple[NightAssessment, ...]:
    """Build consecutive night assessments, oldest first, ending on tonight.

    LEAK nights carry a descending minimum hour so the reported rate is
    unambiguously the smallest of the streak.
    """
    newest = UNIT_NIGHT
    rate = float(len(verdicts))
    assessments: list[NightAssessment] = []
    for offset, verdict in enumerate(reversed(verdicts)):
        assessments.append(
            NightAssessment(
                night=newest - timedelta(days=offset),
                verdict=verdict,
                min_hour_liters=rate + offset if verdict is NightVerdict.LEAK else None,
            )
        )
    return tuple(reversed(assessments))


def _leak_state(nights: tuple[NightAssessment, ...]) -> LeakState:
    """Aggregate a run of night verdicts with no live flow evidence."""
    return detectors.leak_state(nights, {}, frozenset(), (), UNIT_NOW, TZ, True)


def _wobbly(level: float, count: int, offset: int = 0) -> list[float]:
    """Return ``count`` daily totals around ``level`` with deterministic noise."""
    return [level + _WOBBLE[(index + offset) % len(_WOBBLE)] for index in range(count)]


def _gap_day(day: date, expected_liters: float | None) -> DayAssessment:
    """Build an unbounded noon-day assessment with a resolved expectation."""
    return DayAssessment(
        day=day,
        total_liters=None,
        expected_liters=expected_liters,
        spread_liters=40.0,
        ratio=None,
        bucket=None,
        largest_event_liters=None,
        assessable=False,
    )


# ---------------------------------------------------------------------------
# Exit criterion 1 — replaying the real history must stay completely quiet.
# ---------------------------------------------------------------------------


def test_real_history_replay_classifies_every_night_without_a_leak() -> None:
    """Eleven months of real readings yield NO_LEAK or MASKED, nothing else."""
    result = _replay()

    verdicts = [night.verdict for night in result.nights]
    assert len(verdicts) == DETECTOR_WINDOW_DAYS
    assert verdicts.count(NightVerdict.NO_LEAK) == 31
    assert verdicts.count(NightVerdict.MASKED) == 4
    assert verdicts.count(NightVerdict.LEAK) == 0
    assert verdicts.count(NightVerdict.UNKNOWN) == 0
    assert verdicts.count(NightVerdict.UNASSESSED) == 0
    assert [
        night.night for night in result.nights if night.verdict is NightVerdict.MASKED
    ] == [
        date(2026, 6, 25),
        date(2026, 7, 1),
        date(2026, 7, 9),
        date(2026, 7, 17),
    ]


def test_real_history_replay_leaves_the_leak_binary_off() -> None:
    """The leak verdict is a proven all-clear, not an absence of evidence."""
    leak = _replay().leak

    assert leak.active is False
    assert leak.consecutive_nights == 0
    assert leak.rate_liters_per_hour is None
    assert leak.implied_liters_per_day is None
    assert leak.tier is None
    assert leak.persistent_flow is False
    assert leak.masking_coverage is True
    assert leak.last_verdict_night == date(2026, 7, 27)


def test_real_history_replay_leaves_the_anomaly_binary_off() -> None:
    """No daily, point or drift anomaly survives the real July history.

    One anomalous hour is found, which is below ``POINT_ANOMALY_MIN_HOURS`` and
    therefore not a reason — the debounce doing exactly its job.
    """
    anomaly = _replay().anomaly

    assert anomaly.active is False
    assert anomaly.reasons == ()
    assert anomaly.point_hours == 1
    assert anomaly.drift_alarm is False
    assert anomaly.drift_cusum is False
    assert anomaly.drift_ewma is False


def test_real_history_replay_vacation_is_a_two_day_near_miss() -> None:
    """The owner's genuine two-day absence never reaches the three-day rule.

    The trailing streak is zero, not two: the newest noon-day (the return
    morning, 49 L across four draws) is missing only its closing bound, and
    the occupancy rule refuses to treat a day that plainly had usage as
    trailing silence — so it is unjudgeable and breaks the streak at its head
    before the two genuinely-away days are reached.
    """
    vacation = _replay().vacation

    assert vacation.active is False
    assert vacation.consecutive_days == 0
    assert vacation.consecutive_days < VACATION_MIN_DAYS
    assert vacation.since is None


def test_real_history_replay_forecasts_from_the_fresh_device_slot() -> None:
    """Tomorrow (Tuesday) resolves to the device's own fresh weekday average."""
    forecast = _replay().forecast

    assert forecast.source == SOURCE_DEVICE_AVERAGE
    assert forecast.gallons == pytest.approx(35.0)
    assert forecast.liters == pytest.approx(132.489, abs=1e-3)
    assert forecast.band_liters == pytest.approx(90.850, abs=1e-3)
    assert forecast.weekday == "tuesday"
    assert forecast.persons == 1


def test_real_history_replay_grid_matures_over_the_captured_hours() -> None:
    """The hour-of-week grid learns 46 mature buckets from 438 certain hours."""
    grid = _replay().grid

    assert grid.mature_buckets == 46
    assert grid.hourly_samples == 438
    assert sum(grid.active_hours) == 46
    assert len(grid.active_hours) == 168


# ---------------------------------------------------------------------------
# Exit criterion 2 — an injected leak fires, and masking outranks it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rate_lph", "tier"),
    [(6.0, TIER_INFO), (40.0, TIER_WARNING), (50.0, TIER_URGENT)],
    ids=["info", "warning", "urgent"],
)
def test_injected_leak_fires_at_the_tier_its_volume_earns(
    rate_lph: float, tier: str
) -> None:
    """A constant leak over the last five real days is caught at its tier."""
    leaked = inject_leak(
        real_readings(), rate_lph, REPLAY_NOW - timedelta(days=5), REPLAY_NOW
    )

    leak = _replay(leaked).leak

    assert leak.active is True
    assert leak.consecutive_nights == 5
    assert leak.consecutive_nights >= LEAK_CONSECUTIVE_NIGHTS
    assert leak.rate_liters_per_hour == pytest.approx(rate_lph)
    assert leak.implied_liters_per_day == pytest.approx(rate_lph * 24)
    assert leak.tier == tier
    # Five days of unbroken flow is also a persistent-flow signature, and
    # correctly so: a genuine 72-hour continuous draw is a leak either way.
    assert leak.persistent_flow is True


def test_injected_leak_stays_silent_when_regeneration_masks_every_night() -> None:
    """Masking outranks injected evidence: no night, no verdict, no alarm."""
    start = REPLAY_NOW - timedelta(days=2, hours=12)
    leaked = inject_leak(real_readings(), 6.0, start, REPLAY_NOW)
    injected_nights = [
        date(2026, 7, 25),
        date(2026, 7, 26),
        date(2026, 7, 27),
    ]
    windows = list(real_regen_windows())
    for night in injected_nights:
        opened = _local(night, 2)
        windows.append(
            (opened.astimezone(UTC), (opened + timedelta(hours=2)).astimezone(UTC))
        )
    windows.sort()

    result = _replay(leaked, regen_windows=tuple(windows))

    masked = {
        night.night for night in result.nights if night.verdict is NightVerdict.MASKED
    }
    assert set(injected_nights) <= masked
    assert all(night.verdict is not NightVerdict.LEAK for night in result.nights)
    assert result.leak.active is False
    assert result.leak.consecutive_nights == 0
    assert result.leak.persistent_flow is False


# ---------------------------------------------------------------------------
# Synthetic ground truth — Matthews correlation on labelled households.
# ---------------------------------------------------------------------------


def _leak_prediction(verdict: NightVerdict) -> bool | None:
    """Map a night verdict onto the binary prediction MCC scores."""
    if verdict is NightVerdict.LEAK:
        return True
    if verdict is NightVerdict.NO_LEAK:
        return False
    return None


def test_leak_night_classification_scores_a_perfect_mcc() -> None:
    """Three seeded households with a known ten-day leak are classified exactly.

    Masked and indeterminate nights are skipped rather than scored — refusing
    to answer is not a wrong answer, and counting it as one would reward a
    classifier that guesses through a regeneration.
    """
    counts = ConfusionCounts()
    for seed in (11, 12, 13):
        house = _household(
            seed=seed,
            leak_liters_per_hour=_MCC_LEAK_LPH,
            leak_start_day=_MCC_LEAK_START,
            leak_end_day=_MCC_LEAK_END,
        )
        result = _household_result(house)
        for night in result.nights:
            counts.add(
                predicted=_leak_prediction(night.verdict),
                actual=night.night in house.leak_nights,
            )

    assert counts.mcc() >= MCC_FLOOR
    assert (counts.tp, counts.fp, counts.fn, counts.skipped) == (24, 0, 0, 15)


def test_daily_occupancy_scores_an_mcc_above_the_floor() -> None:
    """Five seeded households with a known ten-day absence are judged well.

    The occupancy verdict is the vacation detector's atom, so it is scored
    per day rather than through the three-day streak: a streak metric would
    hide which day the evidence actually failed on.
    """
    counts = ConfusionCounts()
    for seed in (1, 2, 3, 4, 5):
        house = _household(
            seed=seed,
            vacation_start_day=_MCC_VACATION_START,
            vacation_end_day=_MCC_VACATION_END,
        )
        result = _household_result(house)
        for day in result.days:
            counts.add(
                predicted=detectors._occupancy(
                    day,
                    house.readings,
                    TZ,
                    device_online=True,
                    statistics_fresh=True,
                ),
                actual=day.day in house.vacation_days,
            )

    assert counts.mcc() >= MCC_FLOOR
    assert counts.mcc() == pytest.approx(1.0, abs=1e-3)
    assert counts.fn == 0
    assert counts.fp == 0


# ---------------------------------------------------------------------------
# classify_night — the precedence ladder.
# ---------------------------------------------------------------------------


def test_masked_night_outranks_a_full_night_of_flow() -> None:
    """A regeneration night is unusable evidence, not a leak."""
    assessment = _classify(_leaking_night(), masked=frozenset({UNIT_NIGHT}))

    assert assessment.verdict is NightVerdict.MASKED
    assert assessment.min_hour_liters is None


def test_masked_night_outranks_an_unbounded_window() -> None:
    """Masking is checked before assessability — the ladder's first rung."""
    far = (
        (_local(UNIT_NIGHT - timedelta(days=5), 12).astimezone(UTC), 1000.0),
        (_local(UNIT_NIGHT + timedelta(days=5), 12).astimezone(UTC), 1200.0),
    )

    assert _classify(far, masked=frozenset({UNIT_NIGHT})).verdict is NightVerdict.MASKED
    assert _classify(far).verdict is NightVerdict.UNASSESSED


def test_unbounded_night_is_unassessed_rather_than_quiet() -> None:
    """Silence the meter never witnessed is missing data, not proof of quiet."""
    stale = timedelta(hours=ASSESSABLE_BOUND_HOURS + 1)
    window_start, window_end = detectors._night_window(UNIT_NIGHT, TZ)
    readings = (
        ((window_start - stale).astimezone(UTC), 1000.0),
        ((window_end + stale).astimezone(UTC), 1010.0),
    )

    assert _classify(readings).verdict is NightVerdict.UNASSESSED


def test_a_silent_window_hour_clears_the_night() -> None:
    """The no-push rule: an hour without a reading is evidence *for* NO_LEAK."""
    silent = tuple(
        reading
        for reading in _leaking_night()
        if reading[0] != _local(UNIT_NIGHT, 4).astimezone(UTC)
    )

    assessment = _classify(silent)

    assert assessment.verdict is NightVerdict.NO_LEAK
    assert assessment.min_hour_liters == 0.0


def test_a_silent_window_hour_clears_the_night_without_regen_coverage() -> None:
    """NO_LEAK outranks the coverage gate: an all-clear needs no masking proof."""
    silent = tuple(
        reading
        for reading in _leaking_night()
        if reading[0] != _local(UNIT_NIGHT, 4).astimezone(UTC)
    )

    assert _classify(silent, coverage=None).verdict is NightVerdict.NO_LEAK


def test_a_certain_zero_hour_clears_the_night() -> None:
    """The classic minimum-night-flow rule still applies when it can run."""
    hourly = [UNIT_HOURLY_GALLONS] * 7
    hourly[3] = 0.0

    assessment = _classify(_chain(_local(UNIT_NIGHT, 0), hourly))

    assert assessment.verdict is NightVerdict.NO_LEAK
    assert assessment.min_hour_liters == 0.0


def test_a_night_of_unbroken_flow_is_a_leak() -> None:
    """Every window hour pushed, none of them zero: the leak signature."""
    assessment = _classify(_leaking_night())

    assert assessment.verdict is NightVerdict.LEAK
    assert assessment.min_hour_liters == pytest.approx(
        UNIT_HOURLY_GALLONS * LITERS_PER_GALLON
    )


@pytest.mark.parametrize(
    "coverage",
    [None, datetime(2026, 7, 1, tzinfo=UTC)],
    ids=["no-regen-history", "history-starts-after-the-night"],
)
def test_a_leak_needs_regeneration_coverage_or_degrades_to_unknown(
    coverage: datetime | None,
) -> None:
    """A night that cannot be proven regeneration-free is never a leak."""
    assert (
        _classify(_leaking_night(), coverage=coverage).verdict is NightVerdict.UNKNOWN
    )


def test_masked_nights_covers_the_night_a_regeneration_reaches_into() -> None:
    """A cycle starting before midnight masks the morning window it touches."""
    late = _local(UNIT_NIGHT, 23) + timedelta(minutes=30)
    windows = ((late.astimezone(UTC), (late + timedelta(hours=2)).astimezone(UTC)),)

    assert detectors.masked_nights(windows, TZ) == {UNIT_NIGHT + timedelta(days=1)}


# ---------------------------------------------------------------------------
# Daylight saving — the window simply carries fewer or repeated hours.
# ---------------------------------------------------------------------------


def test_spring_forward_night_carries_five_window_hours() -> None:
    """02:00 local never happens, so the window is one hour short."""
    hours = detectors._window_hours(SPRING_FORWARD_NIGHT, TZ)
    assert len(hours) == SPRING_WINDOW_HOURS
    assert [hour.hour for hour in hours] == [1, 3, 4, 5, 6]

    # Five hourly readings are therefore enough to make the night a LEAK …
    flowing = _leaking_night(SPRING_FORWARD_NIGHT)
    assert _classify(flowing, SPRING_FORWARD_NIGHT).verdict is NightVerdict.LEAK

    # … and dropping any one of them (here 05:00 local) clears it again.
    silent = tuple(
        reading for reading in flowing if reading[0].astimezone(TZ).hour != 5
    )
    assert _classify(silent, SPRING_FORWARD_NIGHT).verdict is NightVerdict.NO_LEAK


def test_autumn_fall_back_night_carries_six_window_hours_over_seven() -> None:
    """The repeated 02:00 collapses onto one key: six hours, seven real ones."""
    hours = detectors._window_hours(AUTUMN_FALL_BACK_NIGHT, TZ)
    assert len(hours) == AUTUMN_WINDOW_HOURS
    assert [hour.hour for hour in hours] == [1, 2, 3, 4, 5, 6]

    # Eight absolute hours of flow span the whole window twice over …
    flowing = _leaking_night(AUTUMN_FALL_BACK_NIGHT, hours=8)
    assert flowing[-1][0] - flowing[0][0] == timedelta(hours=8)
    assert _classify(flowing, AUTUMN_FALL_BACK_NIGHT).verdict is NightVerdict.LEAK

    # … and dropping the push that carries local 06:00 (05:00 UTC, the hour
    # after the clocks went back) leaves that window hour silent.
    silent = tuple(
        reading
        for reading in flowing
        if reading[0] != datetime(2026, 10, 25, 5, tzinfo=UTC)
    )
    assert _classify(silent, AUTUMN_FALL_BACK_NIGHT).verdict is NightVerdict.NO_LEAK


# ---------------------------------------------------------------------------
# leak_state — the debounce, the tri-state and the reported rate.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("verdicts", "consecutive", "active"),
    [
        ((), 0, None),
        ((NightVerdict.MASKED, NightVerdict.UNKNOWN, NightVerdict.UNASSESSED), 0, None),
        ((NightVerdict.NO_LEAK,), 0, False),
        ((NightVerdict.LEAK,), 1, False),
        ((NightVerdict.LEAK, NightVerdict.LEAK), 2, True),
        ((NightVerdict.LEAK, NightVerdict.MASKED, NightVerdict.LEAK), 2, True),
        ((NightVerdict.LEAK, NightVerdict.LEAK, NightVerdict.NO_LEAK), 0, False),
        ((NightVerdict.NO_LEAK, NightVerdict.LEAK, NightVerdict.LEAK), 2, True),
    ],
    ids=[
        "no-nights",
        "all-indeterminate",
        "one-clear-night",
        "single-leak-below-debounce",
        "debounce-reached",
        "masked-night-does-not-break-the-streak",
        "clear-night-resets",
        "streak-after-a-clear-night",
    ],
)
def test_leak_debounce_walks_determinate_nights_newest_first(
    verdicts: tuple[NightVerdict, ...], consecutive: int, active: bool | None
) -> None:
    """Indeterminate nights are skipped, a clear night resets, two fire."""
    state = _leak_state(_night_run(*verdicts))

    assert state.consecutive_nights == consecutive
    assert state.active is active
    assert (state.consecutive_nights >= LEAK_CONSECUTIVE_NIGHTS) is (active is True)


def test_leak_state_is_none_when_nothing_was_determinate() -> None:
    """An all-masked window proves nothing and must not read as an all-clear."""
    state = _leak_state(_night_run(NightVerdict.MASKED, NightVerdict.UNASSESSED))

    assert state.active is None
    assert state.last_verdict_night is None
    assert state.tier is None


def test_leak_state_reports_the_smallest_hourly_volume_of_the_streak() -> None:
    """The conservative reading of "the flow that never stops"."""
    mid_rate = (
        (LEAK_TIER_INFO_LITERS_PER_DAY + LEAK_TIER_WARNING_LITERS_PER_DAY) / 2 / 24
    )
    nights = (
        NightAssessment(
            night=UNIT_NIGHT - timedelta(days=1),
            verdict=NightVerdict.LEAK,
            min_hour_liters=mid_rate * 2,
        ),
        NightAssessment(
            night=UNIT_NIGHT, verdict=NightVerdict.LEAK, min_hour_liters=mid_rate
        ),
    )

    state = _leak_state(nights)

    assert state.rate_liters_per_hour == pytest.approx(mid_rate)
    assert state.implied_liters_per_day == pytest.approx(mid_rate * 24)
    assert state.tier == TIER_INFO
    assert state.last_verdict_night == UNIT_NIGHT


def test_persistent_flow_alone_raises_the_leak_binary() -> None:
    """The 72-hour rule needs no night window and no debounce."""
    hours = PERSISTENT_FLOW_HOURS
    readings = _chain(
        (UNIT_NOW - timedelta(hours=hours)).astimezone(TZ),
        [UNIT_HOURLY_GALLONS] * hours,
    )

    state = detectors.leak_state(
        _night_run(NightVerdict.UNKNOWN),
        series.hour_knowledge(readings, TZ),
        series.reading_hours(readings, TZ),
        readings,
        UNIT_NOW,
        TZ,
        True,
    )

    assert state.persistent_flow is True
    assert state.active is True
    assert state.consecutive_nights == 0


@pytest.mark.parametrize(
    ("hours", "zero_at", "drop_at", "expected"),
    [
        (PERSISTENT_FLOW_HOURS, None, None, True),
        (PERSISTENT_FLOW_HOURS + 8, None, None, True),
        (PERSISTENT_FLOW_HOURS - 12, None, None, False),
        (PERSISTENT_FLOW_HOURS, 40, None, False),
        (PERSISTENT_FLOW_HOURS, None, 40, False),
    ],
    ids=[
        "exactly-the-window",
        "longer-than-the-window",
        "short-of-the-window",
        "a-certain-zero-hour",
        "a-silent-hour",
    ],
)
def test_persistent_flow_needs_every_trailing_hour(
    hours: int, zero_at: int | None, drop_at: int | None, expected: bool
) -> None:
    """Three days of unbroken flow, or nothing — one quiet hour is enough."""
    deltas = [UNIT_HOURLY_GALLONS] * hours
    if zero_at is not None:
        deltas[zero_at] = 0.0
    readings = _chain((UNIT_NOW - timedelta(hours=hours)).astimezone(TZ), deltas)
    if drop_at is not None:
        readings = tuple(
            reading for index, reading in enumerate(readings) if index != drop_at
        )

    assert (
        detectors.persistent_flow(
            series.hour_knowledge(readings, TZ),
            series.reading_hours(readings, TZ),
            UNIT_NOW,
            TZ,
        )
        is expected
    )


# ---------------------------------------------------------------------------
# Drift — Hampel cleaning and the two-chart upward-only consensus.
# ---------------------------------------------------------------------------


def test_hampel_replaces_an_isolated_spike_with_its_local_level() -> None:
    """One filled paddling pool is the point detector's business, not drift's."""
    values = _wobbly(_DRIFT_LEVEL, 21)
    spiked = list(values)
    spiked[10] = 900.0

    cleaned = detectors.hampel_clean(spiked)

    assert float(cleaned[10]) < _DRIFT_LEVEL + max(_WOBBLE)
    assert float(cleaned[10]) > _DRIFT_LEVEL + min(_WOBBLE)
    assert np.allclose(np.delete(cleaned, 10), np.delete(np.asarray(values), 10))


def test_hampel_keeps_a_genuine_level_shift() -> None:
    """A household that really doubled its usage must survive the filter."""
    shifted = _wobbly(_DRIFT_LEVEL, 15) + _wobbly(2 * _DRIFT_LEVEL, 15)

    cleaned = detectors.hampel_clean(shifted)

    assert np.allclose(cleaned, np.asarray(shifted))


@pytest.mark.parametrize(
    ("values", "cusum", "ewma"),
    [
        (_wobbly(_DRIFT_LEVEL, 60), False, False),
        (
            _wobbly(_DRIFT_LEVEL, _DRIFT_STABLE_DAYS)
            + _wobbly(_DRIFT_STEP_UP, _DRIFT_SHIFTED_DAYS, _DRIFT_STABLE_DAYS),
            True,
            True,
        ),
        (
            _wobbly(_DRIFT_LEVEL, _DRIFT_STABLE_DAYS)
            + _wobbly(_DRIFT_STEP_DOWN, _DRIFT_SHIFTED_DAYS, _DRIFT_STABLE_DAYS),
            False,
            False,
        ),
    ],
    ids=["stationary", "fifty-percent-rise", "fifty-percent-drop"],
)
def test_drift_charts_only_agree_on_an_upward_shift(
    values: list[float], cusum: bool, ewma: bool
) -> None:
    """Every chart vote is upward-only; a sustained drop belongs to vacation.

    The EWMA chart reads the end of the series and is silent on the drop. The
    upper CUSUM's excursion check alone would fire there — a batch chart
    centred on the in-sample mean sees the *high prefix* of a downward step as
    a sustained excursion — so its vote additionally confirms the trailing
    level actually sits above the target, and the drop stays silent on both.
    """
    assert detectors.cusum_alarm(values) is cusum
    assert detectors.ewma_alarm(values) is ewma
    assert (detectors.cusum_alarm(values) and detectors.ewma_alarm(values)) is (
        cusum and ewma
    )


@pytest.mark.parametrize("seed", [21, 22, 23, 24, 25])
def test_stationary_households_never_raise_a_drift_alarm(seed: int) -> None:
    """Ten weeks of a stationary household must not creep into an alarm."""
    result = _household_result(_household(seed=seed))

    assert result.anomaly.drift_alarm is False
    assert result.anomaly.drift_cusum is False
    assert result.anomaly.drift_ewma is False


@pytest.mark.parametrize("seed", [21, 22])
def test_a_sustained_fifty_percent_step_alarms_both_charts(seed: int) -> None:
    """A developing leak no single day exceeds its band is caught by drift."""
    house = _household(seed=seed)
    now = _household_now(house)
    # +3.5 L/h is +84 L/day on a ~170 L/day household: half again as much,
    # spread over three weeks so no single day breaks its own band.
    stepped = inject_leak(house.readings, 3.5, now - timedelta(days=21), now)

    anomaly = _household_result(house, readings=stepped).anomaly

    assert anomaly.drift_cusum is True
    assert anomaly.drift_ewma is True
    assert anomaly.drift_alarm is True


def test_daily_anomaly_is_upward_only() -> None:
    """A day far below expectation is the vacation detector's business."""
    expected, spread = 170.0, 30.0
    high = DayAssessment(
        day=UNIT_NIGHT,
        total_liters=expected + ANALYTICS_K * spread + 1.0,
        expected_liters=expected,
        spread_liters=spread,
        ratio=None,
        bucket=None,
        largest_event_liters=None,
        assessable=True,
    )
    low = DayAssessment(
        day=UNIT_NIGHT,
        total_liters=0.0,
        expected_liters=expected,
        spread_liters=spread,
        ratio=None,
        bucket=None,
        largest_event_liters=None,
        assessable=True,
    )

    assert detectors.daily_anomaly(high) is True
    assert detectors.daily_anomaly(low) is False


# ---------------------------------------------------------------------------
# Occupancy and vacation — the three evidence paths of the redesign.
# ---------------------------------------------------------------------------


def test_an_in_progress_absence_fires_while_still_under_way() -> None:
    """A genuinely empty house pushes nothing; that silence is the evidence.

    Three complete silent days carry the streak past the rule; the newest,
    still-open noon-day sits partly before the last pre-departure reading's
    silence horizon and is deliberately not judged.
    """
    house = _household(seed=7, vacation_start_day=66, vacation_end_day=70)

    vacation = _household_result(house).vacation

    assert vacation.active is True
    assert vacation.consecutive_days == 3
    assert vacation.consecutive_days >= VACATION_MIN_DAYS
    assert vacation.since == date(2026, 7, 10)


def test_an_offline_device_never_reports_a_vacation() -> None:
    """An unreachable device pushes nothing whatever the household does."""
    house = _household(seed=7, vacation_start_day=66, vacation_end_day=70)

    vacation = _household_result(house, device_online=False).vacation

    assert vacation.active is False
    assert vacation.consecutive_days == 0
    assert vacation.since is None


def test_stale_statistics_gate_the_trailing_silence_path() -> None:
    """Silence may be import lag, and lag is not evidence of an empty house."""
    house = _household(seed=7, vacation_start_day=66, vacation_end_day=70)

    vacation = _household_result(house, statistics_fresh=False).vacation

    assert vacation.active is False
    assert vacation.consecutive_days == 0
    assert vacation.since is None


def test_the_event_count_feature_breaks_the_streak_on_a_frugal_day() -> None:
    """The owner's real 30 L return morning is occupancy, not absence.

    Its volume ratio alone (0.19) would wave it through as unoccupied; three
    distinct draws are what say somebody was home.
    """
    readings = real_readings()
    day = next(
        assessment
        for assessment in _replay().days
        if assessment.day == date(2026, 7, 25)
    )

    assert day.ratio is not None
    assert day.ratio < VACATION_RATIO
    assert series.event_count(readings, day.day, TZ) > VACATION_MAX_EVENTS
    assert (
        detectors._occupancy(
            day, readings, TZ, device_online=True, statistics_fresh=True
        )
        is False
    )


@pytest.mark.parametrize(
    ("returned_gallons", "expected"),
    [(5.0, True), (6 * 45.0, False)],
    ids=["empty-house", "device-offline-at-home"],
)
def test_occupancy_reads_a_reading_gap_from_its_closing_counter(
    returned_gallons: float, expected: bool
) -> None:
    """A day inside a gap is judged by what the meter says the gap used."""
    readings = (
        (_local(date(2026, 6, 1), 12).astimezone(UTC), 1000.0),
        (_local(date(2026, 6, 7), 12).astimezone(UTC), 1000.0 + returned_gallons),
    )

    assert (
        detectors._occupancy(
            _gap_day(date(2026, 6, 4), 170.0),
            readings,
            TZ,
            device_online=True,
            statistics_fresh=True,
        )
        is expected
    )


def test_occupancy_needs_an_expectation_to_compare_a_gap_against() -> None:
    """Without a resolved expectation a gap says nothing either way."""
    readings = (
        (_local(date(2026, 6, 1), 12).astimezone(UTC), 1000.0),
        (_local(date(2026, 6, 7), 12).astimezone(UTC), 1005.0),
    )

    assert (
        detectors._occupancy(
            _gap_day(date(2026, 6, 4), None),
            readings,
            TZ,
            device_online=True,
            statistics_fresh=True,
        )
        is None
    )


# ---------------------------------------------------------------------------
# Adversarial-review coverage additions (2026-07-27)
# ---------------------------------------------------------------------------


def test_the_one_real_july_anomaly_fires_on_its_own_day() -> None:
    """Replayed at the July-20 noon, the single genuine daily anomaly fires.

    326 L against the fresh Monday slot's 163 L expectation and 53 L deviation
    sits just past the three-deviation band — the only such day in the capture.
    Without this the suites never assert ``anomaly.active is True`` through
    ``compute_analytics`` at all, and a detector that could never fire would
    pass every replay pin.
    """
    anomaly = _replay(now=datetime(2026, 7, 20, 12, 30, tzinfo=UTC)).anomaly

    assert anomaly.active is True
    assert anomaly.reasons == (REASON_DAILY_HIGH,)
    assert anomaly.day is not None
    assert anomaly.day.day == date(2026, 7, 20)


def test_a_single_chart_vote_does_not_reach_the_drift_consensus() -> None:
    """A split vote yields no consensus; a genuine sustained shift trips both.

    The split series steps up mid-window and returns to its level: the EWMA
    chart alarms while crossing, but the CUSUM vote's trailing-level check
    sees the window end back at the target and withholds — exactly the
    disagreement the two-chart consensus exists for. Asserting through
    ``_drift_alarm`` (not the two chart functions separately) is what makes an
    ``and`` → ``or`` regression in the consensus observable.
    """
    generator = np.random.default_rng(5)
    split = (
        list(170 + generator.normal(0, 20, 20))
        + list(255 + generator.normal(0, 20, 15))
        + list(170 + generator.normal(0, 20, 25))
    )
    sustained = list(170 + generator.normal(0, 20, 39)) + list(
        255 + generator.normal(0, 20, 21)
    )

    assert detectors._drift_alarm(split) == (False, False, True)
    assert detectors._drift_alarm(sustained) == (True, True, True)


@pytest.mark.parametrize(
    ("start_day", "consecutive", "active"),
    [(66, 3, True), (67, 2, False)],
)
def test_vacation_needs_the_full_run_of_silent_days(
    start_day: int, consecutive: int, active: bool
) -> None:
    """One silent day fewer keeps the verdict off — the threshold bites both ways."""
    house = _household(seed=7, vacation_start_day=start_day, vacation_end_day=70)

    vacation = _household_result(house).vacation

    assert vacation.active is active
    assert vacation.consecutive_days == consecutive


def test_point_detector_refuses_a_zero_width_band() -> None:
    """A matured bucket of certain zeros must not flag ordinary use.

    A multi-week absence writes certain 0.0 into every covered hour; the
    resulting buckets mature at median 0 and scaled MAD 0, and without the
    zero-scale guard any positive draw in those hours would read as anomalous
    for weeks after the household returns.
    """
    tz = ZoneInfo(TZ_KEY)
    knowledge: dict[datetime, float] = {}
    # Five prior Mondays proven dry at 07:00 and 08:00 …
    for weeks_back in range(1, 6):
        for hour in (7, 8):
            instant = datetime(2026, 7, 20, hour, tzinfo=tz) - timedelta(
                days=7 * weeks_back
            )
            knowledge[instant] = 0.0
    # … and one ordinary shower-hour this Monday morning.
    now = datetime(2026, 7, 20, 9, 30, tzinfo=tz)
    knowledge[datetime(2026, 7, 20, 7, tzinfo=tz)] = 1.0 * LITERS_PER_GALLON
    knowledge[datetime(2026, 7, 20, 8, tzinfo=tz)] = 1.0 * LITERS_PER_GALLON

    median, mad, count = baseline.build_grid(knowledge)

    assert detectors.point_anomaly_hours(knowledge, median, mad, count, now, tz) == 0


def test_daily_anomaly_refuses_a_zero_spread_band() -> None:
    """A zero spread is an unusable scale, not a band of width zero."""
    day = DayAssessment(
        day=date(2026, 7, 20),
        total_liters=38.2,
        expected_liters=37.9,
        spread_liters=0.0,
        ratio=1.008,
        bucket="normal",
        largest_event_liters=None,
        assessable=True,
    )

    assert detectors.daily_anomaly(day) is False


def test_a_day_holding_readings_is_never_a_gap_interior() -> None:
    """A day with in-day readings but a missing bound is unjudgeable.

    Averaging the surrounding gap's delta over such a day would smear its own
    real usage into an "unoccupied" verdict — the review reproduced exactly
    that on a mid-vacation day the household briefly visited.
    """
    tz = ZoneInfo(TZ_KEY)

    def reading(day: int, hour: int, value: float) -> Reading:
        return (
            datetime(2026, 6, day, hour, tzinfo=tz).astimezone(UTC),
            value,
        )

    readings = (
        reading(1, 9, 1000.0),
        # Readings inside the noon-day labelled June 10 …
        reading(9, 18, 1010.0),
        reading(10, 8, 1030.0),
        # … whose closing bound only arrives days later.
        reading(20, 9, 1031.0),
    )
    day = DayAssessment(
        day=date(2026, 6, 10),
        total_liters=None,
        expected_liters=160.0,
        spread_liters=40.0,
        ratio=None,
        bucket=None,
        largest_event_liters=None,
        assessable=False,
    )

    verdict = detectors._occupancy(
        day, readings, tz, device_online=True, statistics_fresh=True
    )

    assert verdict is None
