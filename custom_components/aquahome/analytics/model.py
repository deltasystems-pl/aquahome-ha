"""Shared types of the AquaHome analytics tier.

Every dataclass the analytics pipeline passes between modules lives here, so the
pure computation (:mod:`.series`, :mod:`.baseline`, :mod:`.detectors`) and the
Home Assistant-facing engine (:mod:`.engine`) agree on one frozen vocabulary.
Nothing in this module imports Home Assistant or numpy: the types must be
constructible from plain test code and safe to ship across the executor
boundary.

The meter series convention: a :data:`Reading` is ``(UTC instant, cumulative
usage in gallons)`` taken from the imported long-term statistics series' ``sum``
column — meter-read semantics, so consecutive readings diff to the water used
between them and an absent instant means the device pushed nothing (which, on
these devices, means essentially no water moved; see the detectors module).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date, datetime

type Reading = tuple["datetime", float]


class NightVerdict(enum.StrEnum):
    """Classification of one local night's 01-07 minimum-night-flow window."""

    LEAK = "leak"
    NO_LEAK = "no_leak"
    UNKNOWN = "unknown"
    MASKED = "masked"
    UNASSESSED = "unassessed"


@dataclass(frozen=True, slots=True)
class WeekdaySlot:
    """One device-reported weekday average with its change-stamp."""

    average_gal: float | None
    deviation_gal: float | None
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class AnalyticsInputs:
    """Everything one analytics pass consumes, gathered by the engine.

    ``weekday_slots`` is indexed by device slot (index 0 = slot 1 = Saturday,
    the live-verified Map B); ``overall_average`` is the device's single
    ``avg_daily_use_gals`` wrapped as a slot with no deviation. ``readings``
    are sorted ascending. ``regen_coverage_start`` is the start of the oldest
    known regeneration event — nights before it cannot be declared LEAK because
    masking cannot be guaranteed there.
    """

    readings: tuple[Reading, ...]
    regen_windows: tuple[tuple[datetime, datetime], ...]
    regen_coverage_start: datetime | None
    weekday_slots: tuple[WeekdaySlot, ...]
    overall_average: WeekdaySlot
    tz_key: str
    now: datetime
    device_online: bool
    #: Whether the statistics import succeeded on its most recent run. Trailing
    #: silence in the readings only counts as "no water moved" when the import
    #: is known current — otherwise it may simply be lag.
    statistics_fresh: bool


@dataclass(frozen=True, slots=True)
class NightAssessment:
    """Verdict for one local night, keyed by the date its window falls on."""

    night: date
    verdict: NightVerdict
    min_hour_liters: float | None


@dataclass(frozen=True, slots=True)
class DayAssessment:
    """One completed noon-to-noon day against its expectation."""

    day: date
    total_liters: float | None
    expected_liters: float | None
    spread_liters: float | None
    ratio: float | None
    bucket: str | None
    largest_event_liters: float | None
    assessable: bool


@dataclass(frozen=True, slots=True)
class LeakState:
    """The leak detector's aggregate verdict.

    ``active`` is ``None`` only when there is nothing to assess (no imported
    readings yet); ``masking_coverage`` records whether regeneration history
    covered the assessed window, without which no LEAK verdict is ever issued.
    """

    active: bool | None
    consecutive_nights: int
    rate_liters_per_hour: float | None
    implied_liters_per_day: float | None
    tier: str | None
    persistent_flow: bool
    last_verdict_night: date | None
    masking_coverage: bool


@dataclass(frozen=True, slots=True)
class AnomalyState:
    """The usage-anomaly detector's aggregate verdict.

    ``drift_alarm`` is the user-facing consensus verdict (both charts agree);
    ``drift_cusum`` / ``drift_ewma`` expose each chart's individual vote.
    """

    active: bool | None
    reasons: tuple[str, ...]
    day: DayAssessment | None
    point_hours: int
    drift_alarm: bool
    drift_cusum: bool
    drift_ewma: bool


@dataclass(frozen=True, slots=True)
class VacationState:
    """The vacation detector's aggregate verdict."""

    active: bool | None
    consecutive_days: int
    since: date | None


@dataclass(frozen=True, slots=True)
class ForecastState:
    """Tomorrow's expected usage and how it was derived."""

    gallons: float | None
    liters: float | None
    source: str | None
    band_liters: float | None
    weekday: str | None
    persons: int | None


@dataclass(frozen=True, slots=True)
class GridSummary:
    """Learned hour-of-week activity classification (internal Phase-8/9 signal).

    ``active_hours`` has 168 entries indexed ``weekday(Mon=0) * 24 + hour``.
    """

    active_hours: tuple[bool, ...]
    mature_buckets: int
    hourly_samples: int


@dataclass(frozen=True, slots=True)
class AnalyticsResult:
    """One complete analytics pass over a device's imported usage history."""

    computed_at: datetime
    nights: tuple[NightAssessment, ...]
    days: tuple[DayAssessment, ...]
    leak: LeakState
    anomaly: AnomalyState
    vacation: VacationState
    forecast: ForecastState
    grid: GridSummary


#: Expectation/forecast source labels (single source of the literals).
SOURCE_DEVICE_AVERAGE = "device_average"
SOURCE_LEARNED_WEEKDAY = "learned_weekday"
SOURCE_OVERALL_AVERAGE = "overall_average"

#: Daily-ratio bucket labels.
BUCKET_LOW = "low"
BUCKET_NORMAL = "normal"
BUCKET_EXCESS = "excess"

#: Leak tier labels (info < warning < urgent).
TIER_INFO = "info"
TIER_WARNING = "warning"
TIER_URGENT = "urgent"

#: Anomaly reason labels.
REASON_DAILY_HIGH = "daily_high"
REASON_POINT = "point"
REASON_DRIFT = "drift"
