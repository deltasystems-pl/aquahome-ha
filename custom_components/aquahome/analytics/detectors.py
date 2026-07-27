"""Detection verdicts over a device's imported water-usage history.

Every verdict the analytics tier publishes is decided here, and every threshold
it applies is imported from the integration's ``const`` module, where each one
carries its research citation — no folklore constants live in this file. The
module is pure: no Home Assistant imports, no I/O and no clock read beyond the
``now`` its caller captured, so :func:`compute_analytics` is a deterministic
function of its inputs and safe to hand to an executor.

**Minimum night flow, adapted to meter-read push semantics.** The classic MNF
rule classifies a night by whether any hour of the local 01-07 window registered
exactly zero volume: a leak-free home always has such an hour, continuous
unintended flow never does. On this device class that rule cannot run as
written — the ESP32 pushes a datapoint only when water moves, so a quiet hour
has *no row* rather than a zero row (0 of 299 consecutive real intervals carry a
zero delta). Replayed against 27 days of real history the classic rule left
every single night unclassified. The completion used here is the **no-push
rule**: a window hour holding no reading is evidence *for* NO_LEAK, not missing
data. It is sound because the device streams the outlet counter on every gallon
increment, so an hour without a reading carried less than roughly one gallon,
whereas a genuine continuous leak of >= 1 gal/h forces a reading every single
hour — which is exactly the LEAK signature the classifier looks for. The honest
cost is a detection floor of about 1 gal/h (~91 L/day, between the INFO and
WARNING tiers): sub-threshold drips are invisible to this data source, and that
limitation is documented rather than papered over with a volume threshold.

**Masking is mandatory, and so is proving masking was possible.** The softener
regenerates around 02:00 local, inside the very window the classifier reads, so
a night whose window overlaps a known regeneration is MASKED outright. A night
older than the oldest known regeneration event cannot be *proven* regen-free, so
it is never declared a LEAK; it degrades to UNKNOWN instead.

**Robust statistics throughout.** Household water use is spiky, right-skewed and
bimodal, so every spread here is 1.4826 x MAD and every band is centred on a
median; a standard deviation would be inflated by the very outliers being
hunted. The drift charts are fed a Hampel-cleaned series, which keeps a single
holiday-scale spike the business of the point detector instead of tripping the
slow-drift detector.

Sparse input is normal, not exceptional: a fresh install has no imported
history, a device can be offline for days, and the recorder may hold nothing at
all. Every function here degrades to ``None`` ("honestly unknown") or to a
neutral verdict, and none of them raise.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from operator import itemgetter
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
from numpy.typing import NDArray

from custom_components.aquahome.const import (
    ANALYTICS_K,
    BASELINE_WINDOW_DAYS,
    CUSUM_H_SIGMA,
    CUSUM_K_SIGMA,
    DETECTOR_WINDOW_DAYS,
    DRIFT_WINDOW_DAYS,
    EWMA_L,
    EWMA_LAMBDA,
    HAMPEL_WINDOW,
    LEAK_CONSECUTIVE_NIGHTS,
    LEAK_TIER_INFO_LITERS_PER_DAY,
    LEAK_TIER_URGENT_LITERS_PER_DAY,
    LEAK_TIER_WARNING_LITERS_PER_DAY,
    LEARNED_DAILY_MIN_DAYS,
    MIN_BUCKET_SAMPLES,
    MNF_WINDOW_END_HOUR,
    MNF_WINDOW_START_HOUR,
    PERSISTENT_FLOW_HOURS,
    POINT_ANOMALY_MIN_HOURS,
    RATIO_EXCESS,
    RATIO_LOW,
    VACATION_LARGE_EVENT_GALLONS,
    VACATION_MAX_EVENTS,
    VACATION_MIN_DAYS,
    VACATION_RATIO,
)
from custom_components.aquahome.salt import LITERS_PER_GALLON

from . import baseline, series
from .model import (
    BUCKET_EXCESS,
    BUCKET_LOW,
    BUCKET_NORMAL,
    REASON_DAILY_HIGH,
    REASON_DRIFT,
    REASON_POINT,
    TIER_INFO,
    TIER_URGENT,
    TIER_WARNING,
    AnalyticsResult,
    AnomalyState,
    DayAssessment,
    GridSummary,
    LeakState,
    NightAssessment,
    NightVerdict,
    VacationState,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from collections.abc import Set as AbstractSet
    from datetime import date, tzinfo

    from .model import AnalyticsInputs, ForecastState, Reading

#: One-dimensional float array — the shape every statistic below travels in.
type FloatArray = NDArray[np.float64]

#: Anything the daily-series filters accept: a plain sequence or an array.
type FloatSeries = Sequence[float] | FloatArray

#: Consistency constant turning a MAD into a standard-deviation estimate for
#: Gaussian data; used for every spread in this module.
_MAD_SCALE: Final = 1.4826

#: Hour-of-week grid size, index = weekday(Mon=0) * 24 + local hour.
_GRID_HOURS: Final = 168

#: Local wall-clock anchor of the analytics day (noon-to-noon), which keeps a
#: single overnight absence inside one day instead of splitting it across two.
_NOON: Final = time(12)

#: Hours a leak rate is extrapolated over to state an implied daily volume.
_HOURS_PER_DAY: Final = 24.0

#: Trailing window the point detector judges, in local hours.
_POINT_LOOKBACK_HOURS: Final = 24

#: EWMA burn-in: the chart's variance is still inflating over the first weekly
#: cycle of daily values, so alarms there would be startup artefacts.
_EWMA_BURN_IN: Final = 7

#: Fewest values a drift chart needs before its scale estimate means anything.
_DRIFT_MIN_VALUES: Final = 2


def _all_finite(*values: float) -> bool:
    """Return whether every value is a finite number.

    Device-reported averages arrive from JSON, which admits bare ``NaN`` and
    ``Infinity`` literals, and NaN passes every ordinary ``<``/``>`` guard
    silently (all comparisons are ``False``). Detection verdicts must never be
    derived from such a value.
    """
    return all(math.isfinite(value) for value in values)


def _local_hour(instant: datetime, tz: tzinfo) -> datetime:
    """Return the local hour-start containing ``instant``.

    Truncation happens after the zone conversion, so zones at a half- or
    quarter-hour offset still key on their own hour boundaries.
    """
    return instant.astimezone(tz).replace(minute=0, second=0, microsecond=0)


def _resolve_zone(tz_key: str) -> tzinfo:
    """Return the device's zone, falling back to UTC on an unusable key.

    The engine resolves the key before building the inputs, so a failure here
    means corrupt configuration; shifting the windows by an offset degrades
    accuracy far less than a pass that raises and leaves every entity
    unavailable.
    """
    try:
        zone = ZoneInfo(tz_key)
    except (ValueError, ZoneInfoNotFoundError):
        return UTC
    return zone


def _night_window(night: date, tz: tzinfo) -> tuple[datetime, datetime]:
    """Return the local 01-07 minimum-night-flow window of one night."""
    return (
        datetime.combine(night, time(MNF_WINDOW_START_HOUR), tzinfo=tz),
        datetime.combine(night, time(MNF_WINDOW_END_HOUR), tzinfo=tz),
    )


def _window_hours(night: date, tz: tzinfo) -> list[datetime]:
    """Return the local hour-starts inside one night's window, oldest first.

    Stepping in absolute time and re-deriving the local hour keeps DST honest
    without special-casing it: a spring-forward night simply has one window hour
    fewer, and the repeated autumn hour collapses into a single entry because
    both passes name the same local hour.
    """
    start, end = _night_window(night, tz)
    return _local_hour_span(start.astimezone(UTC), end.astimezone(UTC), tz)


def _trailing_local_hours(now: datetime, count: int, tz: tzinfo) -> list[datetime]:
    """Return the ``count`` most recent completed local hours, oldest first.

    The hour containing ``now`` is excluded: it is still filling, so it can
    neither be proven silent nor be certain-zero, and judging it would make
    every verdict depend on when the pass happened to run.
    """
    current = _local_hour(now, tz).astimezone(UTC)
    return _local_hour_span(current - timedelta(hours=count), current, tz)


def _local_hour_span(start: datetime, end: datetime, tz: tzinfo) -> list[datetime]:
    """Return the distinct local hour-starts of ``[start, end)``, oldest first."""
    hours: list[datetime] = []
    seen: set[datetime] = set()
    cursor = start
    while cursor < end:
        local = _local_hour(cursor, tz)
        if local not in seen:
            seen.add(local)
            hours.append(local)
        cursor += timedelta(hours=1)
    return hours


def _bucket_of(local_hour: datetime) -> int:
    """Return the hour-of-week grid index of a local hour-start."""
    return local_hour.weekday() * 24 + local_hour.hour


def masked_nights(
    regen_windows: Sequence[tuple[datetime, datetime]], tz: tzinfo
) -> set[date]:
    """Return every night whose 01-07 window overlaps a regeneration.

    A regeneration draws water inside the detection window and would masquerade
    as continuous night flow, so the affected nights are removed from the
    evidence entirely rather than being corrected for. Each window is tested
    against the nights of the local dates it touches plus the following one,
    which covers a cycle that starts late in the evening and reaches into the
    next morning's window.
    """
    masked: set[date] = set()
    for start, end in regen_windows:
        first = start.astimezone(tz).date()
        last = end.astimezone(tz).date() + timedelta(days=1)
        night = first
        while night <= last:
            window_start, window_end = _night_window(night, tz)
            if start < window_end and end > window_start:
                masked.add(night)
            night += timedelta(days=1)
    return masked


def classify_night(  # noqa: PLR0913 - one verdict, five independent evidence sources
    night: date,
    hour_knowledge: Mapping[datetime, float],
    reading_hours: AbstractSet[datetime],
    readings: Sequence[Reading],
    masked: AbstractSet[date],
    regen_coverage_start: datetime | None,
    tz: tzinfo,
) -> NightAssessment:
    """Return the minimum-night-flow verdict for one local night.

    Precedence is deliberate and never reordered: a masked night is unusable
    evidence, an unbounded night is a data gap rather than a quiet house, and
    NO_LEAK outranks LEAK so that any single hour of proven quiet — an hour with
    a certain zero delta, or an hour the device stayed silent through (the
    no-push rule) — clears the night. Only a night where *every* window hour
    registered flow, and no certain hour was zero, can be a LEAK, and only when
    regeneration history reaches back far enough to prove the night was
    regen-free. ``min_hour_liters`` is the smallest certain positive hour on a
    LEAK night (the implied continuous rate), ``0.0`` on a NO_LEAK night, and
    ``None`` when nothing was decided.
    """
    if night in masked:
        return NightAssessment(
            night=night, verdict=NightVerdict.MASKED, min_hour_liters=None
        )

    window_start, window_end = _night_window(night, tz)
    if not series.bounded(readings, window_start, window_end):
        return NightAssessment(
            night=night, verdict=NightVerdict.UNASSESSED, min_hour_liters=None
        )

    window_hours = _window_hours(night, tz)
    if not window_hours:
        return NightAssessment(
            night=night, verdict=NightVerdict.UNKNOWN, min_hour_liters=None
        )

    certain = [hour_knowledge[hour] for hour in window_hours if hour in hour_knowledge]
    silent_hour = any(hour not in reading_hours for hour in window_hours)
    if silent_hour or any(usage <= 0.0 for usage in certain):
        return NightAssessment(
            night=night, verdict=NightVerdict.NO_LEAK, min_hour_liters=0.0
        )

    covered = (
        regen_coverage_start is not None
        and night >= regen_coverage_start.astimezone(tz).date()
    )
    if not covered:
        return NightAssessment(
            night=night, verdict=NightVerdict.UNKNOWN, min_hour_liters=None
        )

    positives = [usage for usage in certain if usage > 0.0]
    return NightAssessment(
        night=night,
        verdict=NightVerdict.LEAK,
        min_hour_liters=min(positives) if positives else None,
    )


def persistent_flow(
    hour_knowledge: Mapping[datetime, float],
    reading_hours: AbstractSet[datetime],
    now: datetime,
    tz: tzinfo,
) -> bool:
    """Return whether outlet flow never stopped over the trailing window.

    The secondary leak rule: if every one of the last ``PERSISTENT_FLOW_HOURS``
    completed local hours registered flow and none of them is a certain zero,
    the meter has not returned to rest for three days — a leak signature that
    needs no night window and survives a household with unusual hours.
    """
    hours = _trailing_local_hours(now, PERSISTENT_FLOW_HOURS, tz)
    if not hours:
        return False
    if any(hour not in reading_hours for hour in hours):
        return False
    return all(hour_knowledge[hour] > 0.0 for hour in hours if hour in hour_knowledge)


def _leak_tier(implied_liters_per_day: float | None) -> str | None:
    """Return the severity tier of an implied continuous rate.

    Tiers are anchored on the REU2016 leakage distribution, which is heavily
    right-skewed; below the INFO threshold there is no tier at all, because a
    rate under the mean household leakage is not worth naming.
    """
    if implied_liters_per_day is None or not _all_finite(implied_liters_per_day):
        return None
    if implied_liters_per_day >= LEAK_TIER_URGENT_LITERS_PER_DAY:
        return TIER_URGENT
    if implied_liters_per_day >= LEAK_TIER_WARNING_LITERS_PER_DAY:
        return TIER_WARNING
    if implied_liters_per_day >= LEAK_TIER_INFO_LITERS_PER_DAY:
        return TIER_INFO
    return None


def leak_state(  # noqa: PLR0913 - the aggregate verdict spans both leak rules
    nights: Sequence[NightAssessment],
    hour_knowledge: Mapping[datetime, float],
    reading_hours: AbstractSet[datetime],
    readings: Sequence[Reading],
    now: datetime,
    tz: tzinfo,
    masking_coverage: bool,
) -> LeakState:
    """Return the debounced leak verdict over a window of classified nights.

    The streak walks newest-first over *determinate* verdicts only: a MASKED,
    UNKNOWN or UNASSESSED night carries no evidence either way and must not
    break an otherwise continuous run, while a single NO_LEAK night ends it. The
    binary turns on after ``LEAK_CONSECUTIVE_NIGHTS`` such nights — the lower
    bound of the study's 2-3 night debounce — or immediately on the 72-hour
    persistent-flow rule. The reported rate is the *smallest* hourly
    volume seen across the streak, the most conservative reading of "the flow
    that never stops". ``active`` is ``False`` only on the strength of at least
    one determinate night's evidence, and ``None`` when the whole window was
    masked, unbounded or unknown — including the no-history case.
    """
    ordered = sorted(nights, key=lambda assessment: assessment.night)
    consecutive = 0
    streak_rates: list[float] = []
    for assessment in reversed(ordered):
        if assessment.verdict is NightVerdict.NO_LEAK:
            break
        if assessment.verdict is NightVerdict.LEAK:
            consecutive += 1
            if assessment.min_hour_liters is not None:
                streak_rates.append(assessment.min_hour_liters)

    last_verdict_night = next(
        (
            assessment.night
            for assessment in reversed(ordered)
            if assessment.verdict in (NightVerdict.LEAK, NightVerdict.NO_LEAK)
        ),
        None,
    )

    persistent = persistent_flow(hour_knowledge, reading_hours, now, tz)
    rate = min(streak_rates) if streak_rates else None
    implied = rate * _HOURS_PER_DAY if rate is not None else None
    # "No leak" may only be asserted from actual evidence: at least one
    # determinate night, or a live persistent-flow reading. A window whose
    # every night is masked, unbounded or unknown proves nothing, and turning
    # the binary off there would dress zero evidence up as an all-clear.
    determinate = any(
        assessment.verdict in (NightVerdict.LEAK, NightVerdict.NO_LEAK)
        for assessment in ordered
    )
    active: bool | None = None
    if consecutive >= LEAK_CONSECUTIVE_NIGHTS or persistent:
        active = True
    elif determinate:
        active = False

    return LeakState(
        active=active,
        consecutive_nights=consecutive,
        rate_liters_per_hour=rate,
        implied_liters_per_day=implied,
        tier=_leak_tier(implied),
        persistent_flow=persistent,
        last_verdict_night=last_verdict_night,
        masking_coverage=masking_coverage,
    )


def daily_anomaly(day: DayAssessment) -> bool:
    """Return whether a day's total broke out of its expectation band upward.

    Only the upward side is an anomaly: a day far below expectation is the
    vacation detector's business, and flagging it as a problem would fire on
    every weekend away.
    """
    total = day.total_liters
    expected = day.expected_liters
    spread = day.spread_liters
    if not day.assessable or total is None or expected is None or spread is None:
        return False
    if not _all_finite(total, expected, spread):
        return False
    return total > expected + ANALYTICS_K * spread


def point_anomaly_hours(  # noqa: PLR0913 - a grid lookup needs all three statistics
    hour_knowledge: Mapping[datetime, float],
    median: FloatArray,
    mad: FloatArray,
    n: NDArray[Any],
    now: datetime,
    tz: tzinfo,
) -> int:
    """Return how many of the last 24 local hours broke their hour-of-week band.

    Only hours with *certain* usage are judged (an hour inside a multi-hour
    interval has no attributable volume), and only against buckets holding at
    least ``MIN_BUCKET_SAMPLES`` samples — an immature bucket has no band, and
    inventing one is how contextual detectors earn their false positives. The
    count dtype of ``n`` is left open: the grid builder may tally in integers or
    floats.
    """
    if min(median.size, mad.size, n.size) < _GRID_HOURS:
        return 0
    anomalous = 0
    for hour in _trailing_local_hours(now, _POINT_LOOKBACK_HOURS, tz):
        usage = hour_knowledge.get(hour)
        if usage is None:
            continue
        bucket = _bucket_of(hour)
        if float(n[bucket]) < MIN_BUCKET_SAMPLES:
            continue
        centre = float(median[bucket])
        spread = float(mad[bucket])
        if not _all_finite(usage, centre, spread):
            continue
        if usage > centre + ANALYTICS_K * spread:
            anomalous += 1
    return anomalous


def hampel_clean(
    values: FloatSeries, window: int = HAMPEL_WINDOW, k: float = ANALYTICS_K
) -> FloatArray:
    """Return the series with local outliers replaced by their local median.

    A sliding window of ``window`` values (half-window on each side) gives each
    point its own median and scaled MAD, so a genuine level change survives
    while an isolated spike — a filled paddling pool, a burst-and-fixed pipe —
    is pulled back to the local level. Every window is taken from the original
    series, never from the partially cleaned one, so one outlier cannot drag its
    neighbours. A window with zero spread replaces nothing: constant data has no
    outliers, only an undefined scale.
    """
    data = np.asarray(values, dtype=np.float64).ravel()
    if data.size == 0:
        return data
    half = max(window // 2, 1)
    cleaned = data.copy()
    for index in range(data.size):
        local = data[max(index - half, 0) : index + half + 1]
        centre = float(np.median(local))
        spread = _MAD_SCALE * float(np.median(np.abs(local - centre)))
        if not _all_finite(centre, spread) or spread <= 0.0:
            continue
        if abs(float(data[index]) - centre) > k * spread:
            cleaned[index] = centre
    return cleaned


def _chart_baseline(data: FloatArray) -> tuple[float, float]:
    """Return the in-control target and sigma estimate of a cleaned series.

    The target is the *mean* of the already-cleaned series rather than its
    median: on a right-skewed distribution the mean of the positive deviations
    exceeds the mean of the negative ones, so a median target hands the upper
    cumulative sum a systematic positive drift and both charts would eventually
    alarm on a perfectly stationary household. Sigma is the standard deviation
    of the cleaned series, not a scaled MAD: on the same skewed daily totals
    the MAD estimate runs 25-30 % low, which silently turns a five-sigma
    decision interval into roughly 3.6 real sigma and was measured to false-
    alarm on 41 % of stationary 60-day synthetic households. Hampel cleaning
    has already removed the isolated outliers that would inflate a plain
    standard deviation.
    """
    return float(np.mean(data)), float(np.std(data))


def cusum_alarm(values: FloatSeries) -> bool:
    """Return whether an upward CUSUM chart alarms on the daily series.

    CUSUM is the detector for the failure the point rules miss: a small mean
    shift that never exceeds any single-day band but persists — a developing
    leak, or a household that gained a member. The chart runs on the
    Hampel-cleaned series with the standard design (slack ``k`` at half a sigma,
    decision interval ``h`` at five sigma). Only the upward side is watched: a
    sustained *drop* in usage is the vacation detector's domain, and reporting
    it as drift would raise a "problem" flag on every absence.

    The batch chart needs one guard the sequential original does not: its
    target is the in-sample mean, so a series that *dropped* mid-window leaves
    its earlier, higher prefix reading as a sustained positive excursion and
    the upper sum alarms on the mirror image of what it watches for. An alarm
    therefore only counts when the window's trailing level actually sits above
    the target — the shift is upward *and still in force*. A series without a
    usable scale estimate never alarms.
    """
    data = hampel_clean(values)
    if data.size < _DRIFT_MIN_VALUES:
        return False
    target, sigma = _chart_baseline(data)
    if not _all_finite(target, sigma) or sigma <= 0.0:
        return False
    slack = CUSUM_K_SIGMA * sigma
    limit = CUSUM_H_SIGMA * sigma
    high = 0.0
    crossed = False
    for raw in data:
        high = max(0.0, high + (float(raw) - target) - slack)
        if high > limit:
            crossed = True
            break
    if not crossed:
        return False
    trailing = data[-min(_EWMA_BURN_IN, data.size) :]
    return float(np.mean(trailing)) > target


def ewma_alarm(values: FloatSeries) -> bool:
    """Return whether an EWMA control chart alarms on the daily series.

    The lightweight complement to CUSUM: exponential smoothing reacts to small
    sustained shifts a day or two sooner on noisy series. Control limits use the
    exact time-varying variance, and no alarm is raised over the first weekly
    cycle, where those limits are still widening from the startup value and any
    crossing would be an artefact of the burn-in rather than a change in usage.
    Like the CUSUM chart, only the upward side alarms — a sustained drop belongs
    to the vacation detector.
    """
    data = hampel_clean(values)
    if data.size <= _EWMA_BURN_IN:
        return False
    target, sigma = _chart_baseline(data)
    if not _all_finite(target, sigma) or sigma <= 0.0:
        return False
    smoothed = target
    for index, raw in enumerate(data):
        smoothed = EWMA_LAMBDA * float(raw) + (1.0 - EWMA_LAMBDA) * smoothed
        if index < _EWMA_BURN_IN:
            continue
        variance = (
            EWMA_LAMBDA
            / (2.0 - EWMA_LAMBDA)
            * (1.0 - (1.0 - EWMA_LAMBDA) ** (2 * (index + 1)))
        )
        if smoothed - target > EWMA_L * sigma * math.sqrt(variance):
            return True
    return False


def _unoccupied(day: DayAssessment, readings: Sequence[Reading], tz: tzinfo) -> bool:
    """Return whether one bounded noon-day looks like nobody was home.

    Both stage-1 occupancy features of the vacation research apply: the day
    used less than ``VACATION_RATIO`` of its expectation, and it shows at most
    ``VACATION_MAX_EVENTS`` distinct draws — a genuinely empty house registers
    none, while even a frugal occupied morning registers several small ones the
    volume ratio alone would wave through (live-verified on the reference
    household's own return morning: three draws, 34 L, well under the ratio).
    No single draw may be shower-sized either — the fixture event that most
    reliably means a person. A day with no draw at all satisfies both event
    conditions: absence of any event is the vacation signature itself.
    """
    ratio = day.ratio
    if not day.assessable or ratio is None or not _all_finite(ratio):
        return False
    if ratio >= VACATION_RATIO:
        return False
    if series.event_count(readings, day.day, tz) > VACATION_MAX_EVENTS:
        return False
    largest = day.largest_event_liters
    return largest is None or largest < VACATION_LARGE_EVENT_GALLONS * LITERS_PER_GALLON


def _occupancy(  # noqa: PLR0911 - one verdict per distinct evidence situation
    day: DayAssessment,
    readings: Sequence[Reading],
    tz: tzinfo,
    *,
    device_online: bool,
    statistics_fresh: bool,
) -> bool | None:
    """Return one noon-day's occupancy verdict, ``None`` when unjudgeable.

    A genuinely empty house produces *no readings at all* on a push-on-flow
    device, so the strict 48-hour assessability bound can never hold during a
    real absence — the very days the vacation detector exists for. Three
    evidence paths cover it honestly:

    1. A bounded day is judged directly (:func:`_unoccupied`).
    2. A day lying wholly inside a reading gap is judged from the gap's
       *certain* counter delta, averaged over the gap's span: the meter proves
       how much water the whole silent period used, so a gap whose per-day
       average is below the vacation ratio was an absence even though no
       single day inside it can be totalled. (A device-offline-at-home gap
       fails this — the return delta carries days of normal usage.)
    3. A day lying wholly after the *last reading of the series* (trailing
       silence) is a certain zero — *provided* the device is currently online
       (an offline device pushes nothing regardless of usage) and the
       statistics import is known current (otherwise silence may just be
       import lag). A day that merely lacks its closing bound while holding
       readings — the still-settling newest noon-day of a live household — is
       NOT silence: it plainly had usage, and judging it would hand every
       afternoon run a free "unoccupied" day at the head of the streak
       (observed on the real replay: 49 L across four draws read as empty).

    Every path needs a positive resolved expectation to compare against.
    """
    if day.assessable:
        return _unoccupied(day, readings, tz)
    expected = day.expected_liters
    if expected is None or not _all_finite(expected) or expected <= 0.0:
        return None

    start = datetime.combine(day.day - timedelta(days=1), _NOON, tzinfo=tz)
    end = datetime.combine(day.day, _NOON, tzinfo=tz)
    before = bisect_right(readings, start, key=itemgetter(0))
    if before == 0:
        return None
    previous_instant, previous_value = readings[before - 1]
    after = bisect_left(readings, end, key=itemgetter(0))
    if after >= len(readings):
        if not device_online or not statistics_fresh:
            return None
        if readings and readings[-1][0] > start:
            return None
        allocated = 0.0
    else:
        next_instant, next_value = readings[after]
        span_days = (next_instant - previous_instant).total_seconds() / 86400.0
        if span_days <= 0.0:
            return None
        allocated = (next_value - previous_value) * LITERS_PER_GALLON / span_days
    if not _all_finite(allocated) or allocated < 0.0:
        return None
    return allocated < VACATION_RATIO * expected


def vacation_state(
    days: Sequence[DayAssessment],
    readings: Sequence[Reading],
    tz: tzinfo,
    *,
    device_online: bool,
    statistics_fresh: bool,
) -> VacationState:
    """Return the vacation verdict over a window of assessed noon-days.

    Water can only resolve absences of days, never hours, so the verdict needs
    a run of ``VACATION_MIN_DAYS`` consecutive unoccupied noon-days, judged by
    :func:`_occupancy` so that the reading silence of a genuinely empty house
    counts as evidence rather than as a gap. A day whose occupancy could not be
    judged breaks the run — an unjudgeable day is not evidence of an empty
    house. An offline device pushes nothing whatever the household does, so the
    verdict is additionally held inactive while the device is unreachable.
    ``active`` is ``None`` only when no day in the window could be judged at
    all.
    """
    ordered = sorted(days, key=lambda assessment: assessment.day)
    verdicts = [
        _occupancy(
            day,
            readings,
            tz,
            device_online=device_online,
            statistics_fresh=statistics_fresh,
        )
        for day in ordered
    ]
    if not any(verdict is not None for verdict in verdicts):
        return VacationState(active=None, consecutive_days=0, since=None)

    streak: list[date] = []
    for day, verdict in zip(reversed(ordered), reversed(verdicts), strict=True):
        if verdict is not True:
            break
        streak.append(day.day)

    consecutive = len(streak)
    active = device_online and consecutive >= VACATION_MIN_DAYS
    # ``since`` names the head of the current streak whenever one exists, not
    # only once the verdict fires — a building two-day streak with no start
    # date would leave the entity's attributes internally inconsistent.
    return VacationState(
        active=active,
        consecutive_days=consecutive,
        since=streak[-1] if streak else None,
    )


@dataclass(frozen=True, slots=True)
class _NoonDay:
    """Raw noon-to-noon facts for one day, before any expectation is applied."""

    day: date
    total_liters: float | None
    largest_event_liters: float | None
    assessable: bool


def _noon_days(
    readings: Sequence[Reading], window_days: int, now: datetime, tz: tzinfo
) -> list[_NoonDay]:
    """Return the completed noon-days of a window with their raw facts.

    A day counts as assessable only when readings bound both of its noon
    anchors: without them the counter difference is a guess about a gap, and
    every downstream verdict would inherit that guess.
    """
    assessed: list[_NoonDay] = []
    for day in series.noon_days(window_days, now, tz):
        start = datetime.combine(day - timedelta(days=1), _NOON, tzinfo=tz)
        end = datetime.combine(day, _NOON, tzinfo=tz)
        total = series.day_total_liters(readings, day, tz)
        assessed.append(
            _NoonDay(
                day=day,
                total_liters=total,
                largest_event_liters=series.largest_event_liters(readings, day, tz),
                assessable=series.bounded(readings, start, end) and total is not None,
            )
        )
    return assessed


def _ratio_bucket(ratio: float) -> str:
    """Return the REU application-ratio bucket label of a daily ratio."""
    if ratio < RATIO_LOW:
        return BUCKET_LOW
    if ratio > RATIO_EXCESS:
        return BUCKET_EXCESS
    return BUCKET_NORMAL


def _assess_day(
    entry: _NoonDay, inputs: AnalyticsInputs, learned: baseline.LearnedDaily
) -> DayAssessment:
    """Return one noon-day measured against its resolved expectation.

    The ratio is only formed for an assessable day with a positive expectation;
    anywhere else it stays ``None`` rather than becoming a number nothing
    supports.
    """
    expected: float | None = None
    spread: float | None = None
    ratio: float | None = None
    bucket: str | None = None

    expectation = baseline.expected_daily_liters(entry.day, inputs, learned)
    if expectation is not None:
        expected, spread, _source = expectation

    total = entry.total_liters
    if (
        entry.assessable
        and total is not None
        and expected is not None
        and _all_finite(total, expected)
        and expected > 0.0
    ):
        ratio = total / expected
        bucket = _ratio_bucket(ratio)

    return DayAssessment(
        day=entry.day,
        total_liters=total,
        expected_liters=expected,
        spread_liters=spread,
        ratio=ratio,
        bucket=bucket,
        largest_event_liters=entry.largest_event_liters,
        assessable=entry.assessable,
    )


def _latest_closed_night(now: datetime, tz: tzinfo) -> date:
    """Return the newest night whose detection window has already closed."""
    local = now.astimezone(tz)
    if local.hour < MNF_WINDOW_END_HOUR:
        return local.date() - timedelta(days=1)
    return local.date()


def _assess_nights(
    inputs: AnalyticsInputs,
    hour_knowledge: Mapping[datetime, float],
    reading_hours: AbstractSet[datetime],
    tz: tzinfo,
) -> tuple[NightAssessment, ...]:
    """Return the detector window's night verdicts, oldest first."""
    masked = masked_nights(inputs.regen_windows, tz)
    newest = _latest_closed_night(inputs.now, tz)
    return tuple(
        classify_night(
            newest - timedelta(days=offset),
            hour_knowledge,
            reading_hours,
            inputs.readings,
            masked,
            inputs.regen_coverage_start,
            tz,
        )
        for offset in range(DETECTOR_WINDOW_DAYS - 1, -1, -1)
    )


def _masking_coverage(
    regen_coverage_start: datetime | None,
    nights: Sequence[NightAssessment],
    tz: tzinfo,
) -> bool:
    """Return whether regeneration history reaches the assessed nights."""
    if regen_coverage_start is None:
        return False
    if not nights:
        return True
    return regen_coverage_start.astimezone(tz).date() <= nights[-1].night


def _anomaly_state(
    days: Sequence[DayAssessment],
    point_hours: int,
    drift: tuple[bool, bool, bool],
    mature_buckets: int,
) -> AnomalyState:
    """Return the combined usage-anomaly verdict.

    The three detectors are complementary rather than confirmatory — a daily
    total, a handful of anomalous hours and a slow drift each catch a different
    failure — so any one of them raises the flag and all of them are listed.
    With neither a daily expectation nor a mature grid there is nothing to be
    anomalous against, and the verdict is honestly unknown instead of "fine".
    """
    drift_alarm, drift_cusum, drift_ewma = drift
    latest = next(
        (day for day in reversed(days) if day.assessable),
        None,
    )
    reasons: list[str] = []
    if latest is not None and daily_anomaly(latest):
        reasons.append(REASON_DAILY_HIGH)
    if point_hours >= POINT_ANOMALY_MIN_HOURS:
        reasons.append(REASON_POINT)
    if drift_alarm:
        reasons.append(REASON_DRIFT)

    has_expectation = latest is not None and latest.expected_liters is not None
    active = bool(reasons) if has_expectation or mature_buckets > 0 else None
    return AnomalyState(
        active=active,
        reasons=tuple(reasons),
        day=latest,
        point_hours=point_hours,
        drift_alarm=drift_alarm,
        drift_cusum=drift_cusum,
        drift_ewma=drift_ewma,
    )


def _drift_alarm(totals: Sequence[float]) -> tuple[bool, bool, bool]:
    """Return the drift verdict and its two chart votes on the daily series.

    The charts run on the trailing :data:`~..const.DRIFT_WINDOW_DAYS` of
    assessable totals only — a batch chart re-evaluated over an ever-growing
    window crosses any finite decision interval eventually, purely by run
    length. CUSUM reacts to a steady creep, EWMA to a small step; the
    user-facing verdict requires BOTH to agree, which multiplies their
    individual false-alarm rates while a genuine sustained shift trips both.
    Each chart's own vote is returned so the anomaly attributes can show them.
    Below two weekly cycles of assessable days there is no in-control period
    to compare against, so no alarm is possible.
    """
    if len(totals) < LEARNED_DAILY_MIN_DAYS:
        return False, False, False
    values = np.asarray(totals[-DRIFT_WINDOW_DAYS:], dtype=np.float64)
    cusum = cusum_alarm(values)
    ewma = ewma_alarm(values)
    return cusum and ewma, cusum, ewma


def _forecast(
    inputs: AnalyticsInputs, learned: baseline.LearnedDaily, tz: tzinfo
) -> ForecastState:
    """Return the expectation for the local day following ``now``."""
    return baseline.forecast_for(
        inputs.now.astimezone(tz).date() + timedelta(days=1), inputs, learned
    )


def compute_analytics(inputs: AnalyticsInputs) -> AnalyticsResult:
    """Return one complete analytics pass over a device's usage history.

    This is the executor entry point and the only function the engine calls: it
    is pure, deterministic in its inputs (including ``now``, captured once by
    the caller) and total — sparse, empty or gap-riddled history produces
    ``None`` verdicts, never an exception. Order matters only where the data
    depends on it: the hour-of-week grid and the learned daily statistics are
    built over the full imported window, while the night, day and vacation
    verdicts are drawn from the shorter detector window on top of them.
    """
    tz = _resolve_zone(inputs.tz_key)
    readings = inputs.readings
    hour_knowledge = series.hour_knowledge(readings, tz)
    reading_hours = series.reading_hours(readings, tz)
    median, mad, n = baseline.build_grid(hour_knowledge)

    history = _noon_days(readings, BASELINE_WINDOW_DAYS, inputs.now, tz)
    learned = baseline.LearnedDaily.from_days(
        [(entry.day, entry.total_liters, entry.assessable) for entry in history]
    )
    observed = [
        entry.total_liters
        for entry in history
        if entry.assessable
        and entry.total_liters is not None
        and math.isfinite(entry.total_liters)
    ]

    nights = _assess_nights(inputs, hour_knowledge, reading_hours, tz)
    days = tuple(
        _assess_day(entry, inputs, learned) for entry in history[-DETECTOR_WINDOW_DAYS:]
    )

    mature_buckets = int(np.count_nonzero(n >= MIN_BUCKET_SAMPLES)) if n.size else 0
    grid = GridSummary(
        active_hours=baseline.activity_grid(median, mad, n, hour_knowledge),
        mature_buckets=mature_buckets,
        hourly_samples=int(np.sum(n)) if n.size else 0,
    )

    return AnalyticsResult(
        computed_at=inputs.now,
        nights=nights,
        days=days,
        leak=leak_state(
            nights,
            hour_knowledge,
            reading_hours,
            readings,
            inputs.now,
            tz,
            _masking_coverage(inputs.regen_coverage_start, nights, tz),
        ),
        anomaly=_anomaly_state(
            days,
            point_anomaly_hours(hour_knowledge, median, mad, n, inputs.now, tz),
            _drift_alarm(observed),
            mature_buckets,
        ),
        vacation=vacation_state(
            days,
            readings,
            tz,
            device_online=inputs.device_online,
            statistics_fresh=inputs.statistics_fresh,
        ),
        forecast=_forecast(inputs, learned, tz),
        grid=grid,
    )
