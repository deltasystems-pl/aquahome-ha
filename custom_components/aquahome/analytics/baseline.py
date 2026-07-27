"""Learned usage baselines for the AquaHome analytics tier.

Every detector in this tier compares "now" against "normal"; this module is
where "normal" is defined. Two independent baselines are kept, because neither
one alone is trustworthy:

* The **device's own** per-weekday averages (``avg_daily_use_day_N_gals``) — an
  authoritative, zero-warm-up figure the softener derives from the same meter.
  Its timestamp is a *change*-stamp, though, so a slot whose value stops moving
  simply ages: the reference device's Friday slot was 43 days old and reported
  10 gal against a ~160 L/day household, which unguarded would have declared an
  EXCESS anomaly every single Friday. Hence :func:`slot_fresh` — beyond
  :data:`~..const.WEEKDAY_SLOT_FRESHNESS_DAYS` a slot is not a baseline, it is a
  souvenir, and the learned statistics take over.
* **Locally learned** statistics over the imported meter history — two weekly
  cycles slower to warm up, but never stale and never subject to the device's
  own accounting choices.

:func:`expected_daily_liters` resolves the two in a fixed order (fresh device
slot, learned weekday, fresh overall average), and every step must produce both
a centre *and* a spread: an expectation without a band cannot be turned into a
verdict, only into a guess.

All statistics here are robust — median and ``1.4826 * MAD`` rather than mean
and standard deviation — because household water use is event-driven and
heavy-tailed. One irrigation afternoon or one filled bathtub moves a mean and
inflates its standard deviation enough to hide the next real anomaly, while the
median and the MAD ignore it entirely. The 1.4826 factor makes the MAD a
consistent estimator of sigma for normally distributed data, so the
:data:`~..const.ANALYTICS_K` band keeps its familiar meaning.

The hour-of-week grid holds :data:`GRID_BUCKETS` buckets indexed
``weekday(Mon=0) * 24 + local hour``. Only hours whose usage is *certain* (the
output of :func:`~.series.hour_knowledge`) are fed in, and an hour the device
never pushed a reading for is absent rather than zero — so an empty bucket stays
``nan`` with ``n = 0``: unknown, never imputed. ``nan`` is the grid's honest
"unknown" and it never leaves this module; every expectation and forecast is
finiteness-guarded before it is returned.

Units: device slots are GALLONS, the whole analytics tier is LITERS. The
conversion happens exactly once, here, at the slot boundary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final

import numpy as np

from custom_components.aquahome.const import (
    ANALYTICS_K,
    LEARNED_DAILY_MIN_DAYS,
    MIN_BUCKET_SAMPLES,
    OCCUPANCY_LITERS_PER_PERSON,
    WEEKDAY_SLOT_FRESHNESS_DAYS,
    WEEKDAY_SLOTS,
)
from custom_components.aquahome.salt import LITERS_PER_GALLON

from .model import (
    SOURCE_DEVICE_AVERAGE,
    SOURCE_LEARNED_WEEKDAY,
    SOURCE_OVERALL_AVERAGE,
    ForecastState,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import date, datetime

    import numpy.typing as npt

    from .model import AnalyticsInputs, WeekdaySlot

#: Buckets in the hour-of-week grid: 7 local days x 24 local hours.
GRID_BUCKETS: Final = 168

#: Days in a week — the width of every per-weekday statistic below.
_WEEK_DAYS: Final = 7

#: MAD -> sigma consistency factor for normally distributed data.
MAD_SCALE: Final = 1.4826

#: Share of a bucket's samples that must have carried water before its hour
#: counts as an active hour of the household's week.
_ACTIVE_NONZERO_FRACTION: Final = 0.5

#: Map B (live-verified, see :data:`~..const.WEEKDAY_SLOTS`): device slot 1 is
#: Saturday, so python ``weekday()`` (Mon=0) maps onto the slot index through
#: this rotation. Indexed by ``weekday()``, valued by slot index.
_SLOT_FOR_WEEKDAY: Final = (2, 3, 4, 5, 6, 0, 1)


def _robust_center_spread(values: npt.NDArray[np.float64]) -> tuple[float, float]:
    """Return the median and scaled MAD of a non-empty sample array."""
    center = float(np.median(values))
    return center, MAD_SCALE * float(np.median(np.abs(values - center)))


def _resolved(
    expected_liters: float, spread_liters: float, source: str
) -> tuple[float, float, str] | None:
    """Return the expectation triple, or ``None`` when it is unusable.

    The single exit gate of the resolution chain: a non-finite number (a nan
    median of a corrupt series, an infinite cloud value) and a non-positive
    expectation are rejected here rather than propagated, because a zero
    expectation makes every ratio a division by zero and a nan makes every
    comparison silently false. A zero spread is legitimate — a household with
    four identical days genuinely has no spread yet.
    """
    if not math.isfinite(expected_liters) or expected_liters <= 0.0:
        return None
    if not math.isfinite(spread_liters) or spread_liters < 0.0:
        return None
    return expected_liters, spread_liters, source


def _slot_spread_liters(slot: WeekdaySlot) -> float | None:
    """Return a device slot's deviation in liters, or ``None`` if unusable."""
    deviation = slot.deviation_gal
    if deviation is None or not math.isfinite(deviation) or deviation < 0.0:
        return None
    return deviation * LITERS_PER_GALLON


def _learned_spread_liters(day: date, learned: LearnedDaily) -> float | None:
    """Return the learned spread standing in for a missing device deviation.

    The day's own weekday spread is preferred once that weekday has matured;
    otherwise the household's overall daily spread is used, which needs the
    full :data:`~..const.LEARNED_DAILY_MIN_DAYS` warm-up because it mixes
    weekdays and weekends into one distribution.
    """
    weekday_spread = learned.spread_for(day)
    if weekday_spread is not None and learned.count_for(day) >= MIN_BUCKET_SAMPLES:
        return weekday_spread
    if (
        learned.overall_spread is not None
        and learned.overall_count >= LEARNED_DAILY_MIN_DAYS
    ):
        return learned.overall_spread
    return None


def _persons_estimate(learned: LearnedDaily) -> int | None:
    """Return the coarse occupancy estimate, or ``None`` without any data.

    Mean (not median) daily use is the right input here: the REU per-capita
    reference is itself a mean, and occupancy is a volume question, so the
    heavy right tail belongs in the number.
    """
    mean = learned.overall_mean
    if mean is None or not math.isfinite(mean):
        return None
    return max(0, round(mean / OCCUPANCY_LITERS_PER_PERSON))


def bucket_index(hour: datetime) -> int:
    """Return the hour-of-week grid index of a local hour start.

    ``weekday(Mon=0) * 24 + hour`` — the single definition of the grid layout,
    shared by everything that reads a grid array.
    """
    return hour.weekday() * 24 + hour.hour


def build_grid(
    hour_knowledge: Mapping[datetime, float],
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64], npt.NDArray[np.int64]]:
    """Return the hour-of-week grid as ``(median, scaled MAD, sample count)``.

    Keys of ``hour_knowledge`` are LOCAL hour starts and its values are the
    certain usage in liters for that hour; hours of unknown usage are simply
    absent and contribute nothing. Buckets without a single sample keep
    ``nan``/``nan`` with a count of ``0`` — an unknown hour of the week must
    never read as a quiet one. Non-finite values are dropped on ingest so a
    corrupt reading cannot poison a bucket's statistics while still inflating
    its maturity count.
    """
    median = np.full(GRID_BUCKETS, np.nan, dtype=np.float64)
    scaled_mad = np.full(GRID_BUCKETS, np.nan, dtype=np.float64)
    buckets: list[int] = []
    samples: list[float] = []
    for hour, liters in hour_knowledge.items():
        if not math.isfinite(liters):
            continue
        buckets.append(bucket_index(hour))
        samples.append(liters)
    indices = np.asarray(buckets, dtype=np.int64)
    values = np.asarray(samples, dtype=np.float64)
    counts = np.bincount(indices, minlength=GRID_BUCKETS).astype(np.int64)
    for bucket in np.flatnonzero(counts):
        center, spread = _robust_center_spread(values[indices == bucket])
        median[bucket] = center
        scaled_mad[bucket] = spread
    return median, scaled_mad, counts


def activity_grid(
    median: npt.NDArray[np.float64],
    mad: npt.NDArray[np.float64],
    n: npt.NDArray[np.int64],
    hour_knowledge: Mapping[datetime, float],
) -> tuple[bool, ...]:
    """Return, per hour of the week, whether the household is normally active.

    An hour is active when its bucket has matured
    (:data:`~..const.MIN_BUCKET_SAMPLES` samples) and at least half of those
    samples carried water. The two conditions answer different questions —
    "do we know this hour?" and "is it a water hour?" — and both must hold, so
    a single midnight dishwasher run never turns 01:00 into an active hour.

    ``median`` and ``mad`` complete the triple :func:`build_grid` returns; the
    classification itself is a question about samples, not about their central
    tendency, so only the counts are read.
    """
    nonzero = np.zeros(GRID_BUCKETS, dtype=np.float64)
    for hour, liters in hour_knowledge.items():
        if math.isfinite(liters) and liters > 0.0:
            nonzero[bucket_index(hour)] += 1.0
    counts = n.astype(np.float64)
    fraction = np.divide(
        nonzero,
        counts,
        out=np.zeros(GRID_BUCKETS, dtype=np.float64),
        where=counts > 0.0,
    )
    active = (counts >= MIN_BUCKET_SAMPLES) & (fraction >= _ACTIVE_NONZERO_FRACTION)
    return tuple(bool(flag) for flag in active)


def slot_fresh(slot: WeekdaySlot, now: datetime) -> bool:
    """Return whether a device weekday slot is recent enough to trust.

    ``updated_at`` is a change-stamp, not a computation-stamp, so a stable
    household can make a perfectly valid slot look stale. The guard is
    deliberately conservative anyway: falling back to learned statistics costs
    a little accuracy, while trusting a weeks-old average costs false alarms.
    A stamp in the future (cloud/Home Assistant clock skew) is never stale.
    """
    if slot.updated_at is None:
        return False
    return now - slot.updated_at <= timedelta(days=WEEKDAY_SLOT_FRESHNESS_DAYS)


def slot_for_day(day: date) -> int:
    """Return the device weekday-slot index carrying a calendar day's average.

    Map B: slot 1 (index 0) is Saturday, so the index runs Sat, Sun, Mon ...
    Fri. Verified by correlating 8 weeks of daily-usage history against the
    slot values; see :data:`~..const.WEEKDAY_SLOTS`.
    """
    return _SLOT_FOR_WEEKDAY[day.weekday()]


@dataclass(frozen=True, slots=True)
class LearnedDaily:
    """Robust daily-total statistics learned from the device's own history.

    Per python weekday (``Monday = 0``, matching :meth:`datetime.date.weekday`
    and *not* the device's slot order) plus one overall distribution, all in
    liters. Medians and spreads are ``None`` where no sample exists; the
    matching count is what callers gate maturity on, so this type stays a plain
    statistics carrier and every threshold decision remains visible at the
    point where it is made.
    """

    weekday_median: tuple[float | None, ...]
    weekday_spread: tuple[float | None, ...]
    weekday_count: tuple[int, ...]
    overall_median: float | None
    overall_spread: float | None
    overall_count: int
    overall_mean: float | None

    @classmethod
    def from_days(cls, days: Sequence[tuple[date, float | None, bool]]) -> LearnedDaily:
        """Build the statistics from ``(day, total_liters, assessable)`` triples.

        Only assessable days with a finite total contribute: a day whose meter
        readings do not bound it is a data gap, and importing gaps as low usage
        would drag every baseline down and mask the next quiet week. The input
        is a plain triple rather than a :class:`~.model.DayAssessment` because
        an assessment already carries the expectation these statistics produce —
        they must be computable before the first assessment exists.
        """
        by_weekday: list[list[float]] = [[] for _ in range(_WEEK_DAYS)]
        everything: list[float] = []
        for day, total_liters, assessable in days:
            if not assessable or total_liters is None:
                continue
            if not math.isfinite(total_liters):
                continue
            by_weekday[day.weekday()].append(total_liters)
            everything.append(total_liters)

        medians: list[float | None] = []
        spreads: list[float | None] = []
        counts: list[int] = []
        for samples in by_weekday:
            if samples:
                center, spread = _robust_center_spread(
                    np.asarray(samples, dtype=np.float64)
                )
                medians.append(center)
                spreads.append(spread)
            else:
                medians.append(None)
                spreads.append(None)
            counts.append(len(samples))

        overall_median: float | None = None
        overall_spread: float | None = None
        overall_mean: float | None = None
        if everything:
            values = np.asarray(everything, dtype=np.float64)
            overall_median, overall_spread = _robust_center_spread(values)
            overall_mean = float(np.mean(values))

        return cls(
            weekday_median=tuple(medians),
            weekday_spread=tuple(spreads),
            weekday_count=tuple(counts),
            overall_median=overall_median,
            overall_spread=overall_spread,
            overall_count=len(everything),
            overall_mean=overall_mean,
        )

    def median_for(self, day: date) -> float | None:
        """Return the learned median daily total for a day's weekday."""
        return self.weekday_median[day.weekday()]

    def spread_for(self, day: date) -> float | None:
        """Return the learned scaled MAD of daily totals for a day's weekday."""
        return self.weekday_spread[day.weekday()]

    def count_for(self, day: date) -> int:
        """Return how many assessable days fed a day's weekday statistics."""
        return self.weekday_count[day.weekday()]


def expected_daily_liters(
    day: date, inputs: AnalyticsInputs, learned: LearnedDaily
) -> tuple[float, float, str] | None:
    """Return ``(expected liters, spread liters, source)`` for a noon-day.

    The resolution chain, best source first:

    1. The device's own average for that weekday, when its stamp is fresh. Its
       reported deviation is the spread; a device that reports none borrows the
       learned spread rather than forfeiting the fresher centre.
    2. The learned median for that weekday, once the weekday has matured.
    3. The device's fresh overall daily average paired with the learned overall
       spread (the overall slot carries no deviation of its own).

    ``None`` when nothing resolves — a cold start with a stale device: no
    expectation is published and the daily detectors simply stay silent, which
    is the honest answer, not a fabricated average.
    """
    slots = inputs.weekday_slots
    index = slot_for_day(day)
    if index < len(slots):
        slot = slots[index]
        if slot.average_gal is not None and slot_fresh(slot, inputs.now):
            spread = _slot_spread_liters(slot)
            if spread is None:
                spread = _learned_spread_liters(day, learned)
            if spread is not None:
                candidate = _resolved(
                    slot.average_gal * LITERS_PER_GALLON, spread, SOURCE_DEVICE_AVERAGE
                )
                if candidate is not None:
                    return candidate

    median = learned.median_for(day)
    spread = learned.spread_for(day)
    if (
        median is not None
        and spread is not None
        and learned.count_for(day) >= MIN_BUCKET_SAMPLES
    ):
        candidate = _resolved(median, spread, SOURCE_LEARNED_WEEKDAY)
        if candidate is not None:
            return candidate

    overall = inputs.overall_average
    if (
        overall.average_gal is not None
        and slot_fresh(overall, inputs.now)
        and learned.overall_spread is not None
        and learned.overall_count >= LEARNED_DAILY_MIN_DAYS
    ):
        candidate = _resolved(
            overall.average_gal * LITERS_PER_GALLON,
            learned.overall_spread,
            SOURCE_OVERALL_AVERAGE,
        )
        if candidate is not None:
            return candidate

    return None


def forecast_for(
    day: date, inputs: AnalyticsInputs, learned: LearnedDaily
) -> ForecastState:
    """Return the published usage forecast for a day.

    Same resolution chain as :func:`expected_daily_liters`, presented for the
    user: gallons for the sensor's native unit, liters and the
    :data:`~..const.ANALYTICS_K` band for the attributes, and the source label
    so the forecast can always be traced back to the number it came from. The
    weekday label and the occupancy estimate describe the day and the household
    rather than the expectation, so they are published even when no expectation
    resolves.
    """
    weekday = WEEKDAY_SLOTS[slot_for_day(day)]
    persons = _persons_estimate(learned)
    resolved = expected_daily_liters(day, inputs, learned)
    if resolved is None:
        return ForecastState(
            gallons=None,
            liters=None,
            source=None,
            band_liters=None,
            weekday=weekday,
            persons=persons,
        )
    expected_liters, spread_liters, source = resolved
    return ForecastState(
        gallons=expected_liters / LITERS_PER_GALLON,
        liters=expected_liters,
        source=source,
        band_liters=ANALYTICS_K * spread_liters,
        weekday=weekday,
        persons=persons,
    )
