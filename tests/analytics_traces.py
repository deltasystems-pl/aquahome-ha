"""Shared trace material for the analytics-tier test suites.

Two kinds of series are produced here, both in the analytics tier's reading
convention — ``(UTC instant, cumulative gallons)``:

* **Replayed real history**: the captured datapoint fixtures re-expressed as the
  meter-read series the engine would read back from the imported long-term
  statistics. The merge is deliberately re-implemented here, independently of
  ``custom_components.aquahome.statistics``, so the exit-criteria replay tests
  do not inherit a production bug.
* **Synthetic households**: a seeded, fully deterministic hourly usage
  generator with the same *push semantics* the real device shows (a reading
  exists only for hours that moved at least one gallon), plus optional injected
  leaks and vacation spans with ground-truth labels for MCC scoring.

Nothing here imports Home Assistant or the integration.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

FIXTURES = Path(__file__).parent / "fixtures"

LITERS_PER_GALLON = 3.785411784

#: The reference device's zone — every fixture label carries its offsets.
DEVICE_TZ = ZoneInfo("Europe/Warsaw")

#: Push granularity of the reading model: an hour registers a reading only when
#: at least this many gallons moved (live-verified behaviour; 0 of 299 real
#: intervals carry a zero delta).
PUSH_THRESHOLD_GAL = 1.0


def _fixture_readings(name: str) -> list[tuple[datetime, float]]:
    """Load one datapoint-graph fixture as ``(UTC instant, gallons)`` rows.

    The fixtures carry liters (``units: "Liters"``); zero values are the API's
    "no reading in this bucket" placeholder and are dropped, exactly as the
    statistics import drops them.
    """
    payload = json.loads((FIXTURES / name).read_text())
    rows: list[tuple[datetime, float]] = []
    for row in payload["data"]:
        value = row.get("value")
        if not value or value <= 0:
            continue
        label = datetime.fromisoformat(row["label"])
        rows.append((label.astimezone(UTC), value / LITERS_PER_GALLON))
    rows.sort(key=lambda item: item[0])
    return rows


def real_readings() -> tuple[tuple[datetime, float], ...]:
    """Return the replayed real meter series (daily + hourly captures merged).

    A local day covered by at least one hourly reading contributes its hourly
    readings; any other day contributes its daily reading at local midnight —
    the same shape the statistics import stores, rebuilt independently.
    """
    hourly = _fixture_readings("graph-meter-hourly.json")
    daily = _fixture_readings("graph-meter-daily.json")
    hourly_days = {instant.astimezone(DEVICE_TZ).date() for instant, _ in hourly}
    merged = list(hourly)
    for instant, value in daily:
        local_day = instant.astimezone(DEVICE_TZ).date()
        if local_day in hourly_days:
            continue
        midnight = datetime.combine(local_day, time.min, tzinfo=DEVICE_TZ)
        merged.append((midnight.astimezone(UTC), value))
    merged.sort(key=lambda item: item[0])
    deduped: list[tuple[datetime, float]] = []
    seen: set[datetime] = set()
    for instant, value in merged:
        start = instant.replace(minute=0, second=0, microsecond=0)
        if start in seen:
            continue
        seen.add(start)
        deduped.append((start, value))
    return tuple(deduped)


def real_regen_windows() -> tuple[tuple[datetime, datetime], ...]:
    """Return the captured regeneration events as closed UTC windows.

    An event without an end (in progress at capture) is padded to three hours,
    matching the engine's ``NOMINAL_REGEN_DURATION`` fallback.
    """
    payload = json.loads((FIXTURES / "regeneration-events.json").read_text())
    windows: list[tuple[datetime, datetime]] = []
    for event in payload["data"]:
        raw_start = event.get("start_time")
        if not raw_start:
            continue
        start = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
        raw_end = event.get("end_time")
        end = (
            datetime.fromisoformat(raw_end.replace("Z", "+00:00"))
            if raw_end
            else start + timedelta(hours=3)
        )
        windows.append((start.astimezone(UTC), end.astimezone(UTC)))
    windows.sort(key=lambda item: item[0])
    return tuple(windows)


def inject_leak(
    readings: tuple[tuple[datetime, float], ...],
    rate_liters_per_hour: float,
    start: datetime,
    end: datetime,
) -> tuple[tuple[datetime, float], ...]:
    """Overlay a constant leak onto a reading series, with push semantics.

    A real leak turns the meter continuously, so the device pushes a reading
    every hour of it: the result carries a synthetic reading at every hour
    boundary inside ``[start, end)`` whose value is the base counter (step
    interpolation of the last real reading) plus the leak accumulated since
    ``start``. Real readings inside and after the span are shifted by the leak
    accumulated at their own instant, keeping the series monotonic.
    """
    rate_gal = rate_liters_per_hour / LITERS_PER_GALLON
    start = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    end = end.astimezone(UTC).replace(minute=0, second=0, microsecond=0)

    def leaked(instant: datetime) -> float:
        clipped = min(max(instant, start), end)
        return rate_gal * (clipped - start).total_seconds() / 3600

    def base(instant: datetime) -> float | None:
        prior = [value for when, value in readings if when <= instant]
        return prior[-1] if prior else None

    merged: dict[datetime, float] = {}
    cursor = start
    while cursor < end:
        base_value = base(cursor)
        if base_value is not None:
            merged[cursor] = base_value + leaked(cursor)
        cursor += timedelta(hours=1)
    for when, value in readings:
        merged[when] = max(value + leaked(when), merged.get(when, 0.0))
    return tuple(sorted(merged.items(), key=lambda item: item[0]))


#: Relative weight of each local hour in the synthetic diurnal pattern:
#: a two-peak weekday shape (morning + evening) and a flatter weekend shape.
_WEEKDAY_SHAPE = (
    0,
    0,
    0,
    0,
    0,
    1,
    6,
    10,
    6,
    2,
    1,
    1,
    2,
    2,
    1,
    1,
    2,
    5,
    8,
    9,
    6,
    4,
    2,
    1,
)
_WEEKEND_SHAPE = (
    0,
    0,
    0,
    0,
    0,
    0,
    1,
    4,
    8,
    9,
    6,
    4,
    3,
    3,
    3,
    2,
    3,
    4,
    6,
    7,
    6,
    4,
    2,
    1,
)


@dataclass(frozen=True, slots=True)
class SyntheticHousehold:
    """A deterministic synthetic household trace with ground-truth labels.

    ``readings`` follow the real device's push semantics. ``leak_nights`` holds
    every local date whose whole 01-07 window falls inside the leak span;
    ``vacation_days`` holds every local date fully inside the vacation span.
    ``regen_windows`` fire every seventh night at 02:00 local for two hours
    (the draw itself is sub-threshold, exactly like the real device).
    """

    readings: tuple[tuple[datetime, float], ...]
    regen_windows: tuple[tuple[datetime, datetime], ...]
    leak_nights: frozenset[date]
    vacation_days: frozenset[date]
    first_day: date
    last_day: date

    @classmethod
    def generate(  # noqa: PLR0913 - scenario knobs are the whole point here
        cls,
        *,
        weeks: int = 8,
        seed: int = 42,
        daily_target_liters: float = 170.0,
        leak_liters_per_hour: float = 0.0,
        leak_start_day: int | None = None,
        leak_end_day: int | None = None,
        vacation_start_day: int | None = None,
        vacation_end_day: int | None = None,
        tz: ZoneInfo = DEVICE_TZ,
    ) -> SyntheticHousehold:
        """Generate ``weeks`` of hourly usage starting Monday 2026-05-04.

        Day indices for the leak/vacation spans count from that first day;
        spans are half-open (``end`` day excluded). All randomness comes from
        one seeded generator, so identical arguments reproduce identical
        traces.
        """
        rng = random.Random(seed)  # noqa: S311 - deterministic test traces, not crypto
        first_day = date(2026, 5, 4)
        days = weeks * 7
        counter = 12000.0
        readings: list[tuple[datetime, float]] = []
        leak_rate_gal = leak_liters_per_hour / LITERS_PER_GALLON
        leak_span = (
            range(leak_start_day, leak_end_day)
            if leak_liters_per_hour > 0
            and leak_start_day is not None
            and leak_end_day is not None
            else range(0)
        )
        vacation_span = (
            range(vacation_start_day, vacation_end_day)
            if vacation_start_day is not None and vacation_end_day is not None
            else range(0)
        )

        regen_windows: list[tuple[datetime, datetime]] = []
        for day_index in range(days):
            day = first_day + timedelta(days=day_index)
            if day_index % 7 == 3:
                start = datetime.combine(day, time(2, 1), tzinfo=tz)
                regen_windows.append(
                    (
                        start.astimezone(UTC),
                        (start + timedelta(hours=2)).astimezone(UTC),
                    )
                )
            shape = _WEEKEND_SHAPE if day.weekday() >= 5 else _WEEKDAY_SHAPE
            scale = daily_target_liters / LITERS_PER_GALLON / sum(shape)
            for hour in range(24):
                usage = 0.0
                if day_index not in vacation_span and shape[hour]:
                    # Lumpy usage: most hours of the weight class see one or
                    # two draws, some see none — the real series is spiky.
                    draws = rng.randint(0, 2) if shape[hour] < 5 else rng.randint(1, 3)
                    for _ in range(draws):
                        usage += rng.uniform(0.4, 1.2) * shape[hour] * scale
                if day_index in leak_span:
                    usage += leak_rate_gal
                if usage <= 0:
                    continue
                counter += usage
                if usage >= PUSH_THRESHOLD_GAL:
                    instant = datetime.combine(day, time(hour), tzinfo=tz)
                    readings.append((instant.astimezone(UTC), counter))

        leak_nights = frozenset(
            first_day + timedelta(days=index)
            for index in leak_span
            # The 01-07 window of night N needs the leak active from night
            # start, i.e. day N inside the span (leak spans start at 00:00).
        )
        vacation_days = frozenset(
            first_day + timedelta(days=index) for index in vacation_span
        )
        return cls(
            readings=tuple(readings),
            regen_windows=tuple(regen_windows),
            leak_nights=leak_nights,
            vacation_days=vacation_days,
            first_day=first_day,
            last_day=first_day + timedelta(days=days - 1),
        )


@dataclass(slots=True)
class ConfusionCounts:
    """Binary confusion-matrix tally with an MCC accessor."""

    tp: int = 0
    fp: int = 0
    tn: int = 0
    fn: int = 0
    skipped: int = field(default=0)

    def add(self, *, predicted: bool | None, actual: bool) -> None:
        """Tally one prediction; ``None`` predictions count as skipped."""
        if predicted is None:
            self.skipped += 1
            return
        if predicted and actual:
            self.tp += 1
        elif predicted and not actual:
            self.fp += 1
        elif not predicted and actual:
            self.fn += 1
        else:
            self.tn += 1

    def mcc(self) -> float:
        """Return the Matthews correlation coefficient of the tally.

        The analytics research mandates MCC over accuracy for these heavily
        imbalanced classes; an empty or degenerate denominator yields 0.0.
        """
        denominator = math.sqrt(
            (self.tp + self.fp)
            * (self.tp + self.fn)
            * (self.tn + self.fp)
            * (self.tn + self.fn)
        )
        if denominator == 0:
            return 0.0
        return (self.tp * self.tn - self.fp * self.fn) / denominator
