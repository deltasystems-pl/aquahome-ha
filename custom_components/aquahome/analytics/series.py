"""Meter-series primitives shared by the whole AquaHome analytics tier.

Night classification, the hour-of-week baseline and every daily total read the
same imported long-term-statistics series, so it is turned into usage here and
only here. A :data:`~.model.Reading` is ``(UTC instant, cumulative gallons)`` —
the recorder's ``sum`` column of ``aquahome:<slug>_water``, which
:mod:`~.statistics` fills with **meter reads, not usage reads**. Consecutive
readings therefore diff to the water that flowed between them, and nothing
downstream has to care which cloud bucket a datapoint came from.

**Why an hour can be unknown.** The device pushes only when water moves. Over
the 27-day reference capture its 300 hourly readings produced 299 intervals with
*no* zero-delta push and *no* backwards step, only 198 of the ~637 hours were
covered by a 1-hour interval, and the live websocket emits
``total_outlet_water_gals`` on every whole-gallon increment. The series is thus
activity-driven and sparse, and the water inside a multi-hour interval cannot be
attributed to any one hour of it. :func:`hour_knowledge` consequently reports
*certain hours only*: the delta of an interval that spans exactly one local
clock hour, and the exact zeros a zero-delta interval proves. An hour it omits
is genuinely unknown, and spreading a multi-hour delta across its hours would be
a fabrication that poisons the baseline grid and invents night flow the meter
never saw.

The complementary evidence — that the device pushed *at all* — is
:func:`reading_hours`. An hour with no reading carried less than roughly one
gallon (the push granularity), while a continuous leak of >= 1 gal/h forces a
reading every single hour, which is exactly the signature the night classifier
looks for. Neither set is sufficient alone, and together they set the honest
sensitivity floor of this tier: about 1 gal/h, i.e. ~91 L/day. Sub-threshold
drips are invisible on a sparse-push device, by construction rather than by
oversight.

Days are cut **noon to noon**, never at midnight: the quiet part of a household
day is the middle of the night, so a midnight cut splits the evening-into-night
activity a daily total should hold together and lets a shifted bedtime smear one
day into the next. Day ``d`` spans ``[noon d-1, noon d]`` in local wall clock
and is labelled by the date it closes on.

Local arithmetic is wall clock (``datetime.combine(day, time(h), tzinfo=tz)``);
hour stepping is absolute, because a local hour boundary is worth exactly 3600 s
in every whole-hour zone. Daylight-saving transitions cost one window hour and
nothing else, in either direction and without a special case. A spring-forward
night never produces its nonexistent local hour, so no result mentions it. A
fall-back night runs its repeated wall-clock hour twice, and because Python
compares two datetimes of the *same* zone by their digits — ``fold`` is ignored
inside a zone, which is also how a caller's ``combine`` can address them at all —
the second pass lands on the first one's key and wins. Both are honest values
for a real hour; the night simply carries one fewer.

That same rule is why nothing here compares a local timestamp to a UTC one with
``==``: equality across zones is defined to fail for an ambiguous time, which
would silently drop every reading of a fall-back hour. Durations and orderings
are well defined throughout, and this module uses only those.

Readings must be sorted ascending by instant — the recorder returns them that
way and :class:`~.model.AnalyticsInputs` guarantees it. Every function here is
total: empty, short or uncovered input yields an empty result or ``None``, never
an exception.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from itertools import pairwise
from typing import TYPE_CHECKING, Final

from custom_components.aquahome.const import ASSESSABLE_BOUND_HOURS
from custom_components.aquahome.salt import LITERS_PER_GALLON

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from .model import Reading

#: Resolution of the imported statistics series and of the baseline grid.
_HOUR: Final = timedelta(hours=1)

#: Wall-clock instant every day boundary is cut at.
_NOON: Final = time(12)

#: One calendar day, for label arithmetic on noon-day dates.
_ONE_DAY: Final = timedelta(days=1)

#: Slack a reading may sit outside an assessed window and still bound it.
_BOUND_SLACK: Final = timedelta(hours=ASSESSABLE_BOUND_HOURS)


def build_intervals(
    readings: Sequence[Reading],
) -> list[tuple[datetime, datetime, float]]:
    """Return ``(start, end, gallons)`` for every consecutive pair of readings.

    Fewer than two readings span nothing, so the result is empty. A backwards
    step is clamped to ``0.0``: the import already absorbs counter resets by
    restarting its accumulation, so a residual negative is a glitch in the
    stored series and never water anybody used.
    """
    return [
        (start, end, max(after - before, 0.0))
        for (start, before), (end, after) in pairwise(readings)
    ]


def hour_knowledge(readings: Sequence[Reading], tz: tzinfo) -> dict[datetime, float]:
    """Map local hour-starts to the litres *certainly* used within that hour.

    Only two interval shapes carry certainty. One that starts on a local clock
    hour and lasts exactly an hour pins that hour to its delta. One with a zero
    delta proves every clock hour it fully contains carried nothing, however
    long it runs. Every other hour is absent from the mapping, and a missing key
    means *unknown*, never zero — see the module docstring for why the
    difference is the whole design.

    Keys are aware local hour-starts, so a caller reads weekday and hour off
    them directly and looks an hour up with the ``datetime.combine`` it already
    builds its windows from. In a zone whose UTC
    offset is not a whole number of hours the hourly statistics grid never
    aligns with the local clock, so no interval is ever exactly one local hour
    and only the proven zeros survive; the detectors then rest on push evidence
    alone, which is a graceful degradation rather than a wrong answer.

    A backwards step contributes nothing at all. :func:`build_intervals` clamps
    it to zero so it can never invent usage, but a glitched reading is not
    evidence that no water flowed either, and admitting it as a proven zero
    would be the one way this function could talk a real leak out of existence.
    The recorder's ``sum`` never goes backwards, so the distinction only ever
    matters for a hand-built series.
    """
    knowledge: dict[datetime, float] = {}
    for (start, before), (end, after) in pairwise(readings):
        gallons = after - before
        if gallons < 0.0:
            continue
        if gallons == 0.0:
            for covered in _covered_hours(start, end, tz):
                knowledge[covered] = 0.0
            continue
        hour = start.astimezone(tz)
        if end - start == _HOUR and _on_the_hour(hour):
            knowledge[hour] = gallons * LITERS_PER_GALLON
    return knowledge


def reading_hours(readings: Sequence[Reading], tz: tzinfo) -> set[datetime]:
    """Return the local hour-starts holding at least one reading.

    Membership is the push evidence the night classifier pairs with
    :func:`hour_knowledge`: an hour outside this set saw no push at all, which
    on an activity-driven device means it carried under about a gallon.
    """
    return {_hour_start(instant, tz) for instant, _ in readings}


def counter_at(readings: Sequence[Reading], instant: datetime) -> float | None:
    """Return the meter reading in effect at ``instant``, in gallons.

    That is the last reading at or before it, since the counter holds its value
    until the next push. ``None`` when the series begins after ``instant``: a
    meter cannot be honestly extrapolated backwards past its first reading.
    """
    index = bisect_right(readings, instant, key=_instant_of)
    if index == 0:
        return None
    return readings[index - 1][1]


def day_total_liters(
    readings: Sequence[Reading], day: date, tz: tzinfo
) -> float | None:
    """Return the litres used in the noon-day labelled ``day``.

    The span is ``[noon day-1, noon day]`` in local wall clock, and the total is
    the difference of the two counter readings in effect at those instants — so
    water that flowed while the device was silent is still counted, exactly as
    the meter recorded it. ``None`` when the series does not reach back to the
    opening boundary. Whether that difference is *trustworthy* is a separate
    question answered by :func:`bounded`: a long gap before the opening
    boundary makes this total honest arithmetic over dishonest coverage.
    """
    opening = counter_at(readings, _noon(day - _ONE_DAY, tz))
    closing = counter_at(readings, _noon(day, tz))
    if opening is None or closing is None:
        return None
    return max(closing - opening, 0.0) * LITERS_PER_GALLON


def largest_event_liters(
    readings: Sequence[Reading], day: date, tz: tzinfo
) -> float | None:
    """Return the biggest single draw inside the noon-day ``day``, in litres.

    A draw is one interval between consecutive readings, and only intervals
    lying entirely within the day count — a draw straddling a noon boundary is
    attributed to neither day rather than to both. The result discriminates a
    genuinely empty house (many tiny draws) from a quiet but occupied one (one
    shower-sized draw), which is what the vacation rule turns on.

    ``None`` when no interval fits inside the day: with the meter pushing only
    on flow, that means the day holds at most one push, so its largest draw is
    unmeasured rather than zero.
    """
    lower = bisect_left(readings, _noon(day - _ONE_DAY, tz), key=_instant_of)
    upper = bisect_right(readings, _noon(day, tz), key=_instant_of)
    inside = build_intervals(readings[lower:upper])
    if not inside:
        return None
    return max(gallons for _, _, gallons in inside) * LITERS_PER_GALLON


def event_count(readings: Sequence[Reading], day: date, tz: tzinfo) -> int:
    """Return how many distinct draws happened inside the noon-day ``day``.

    A draw is one positive interval between consecutive readings lying entirely
    within the day, exactly as :func:`largest_event_liters` counts them. On a
    push-on-flow meter each such interval is a moment somebody (or something)
    ran water, which makes the count the second occupancy feature of the
    vacation research alongside the volume ratio: an empty house shows *zero*
    draws, while even a frugal occupied morning shows several small ones the
    volume ratio alone would wave through.
    """
    lower = bisect_left(readings, _noon(day - _ONE_DAY, tz), key=_instant_of)
    upper = bisect_right(readings, _noon(day, tz), key=_instant_of)
    return sum(
        1 for _, _, gallons in build_intervals(readings[lower:upper]) if gallons > 0
    )


def bounded(readings: Sequence[Reading], start: datetime, end: datetime) -> bool:
    """Return whether readings bracket ``[start, end]`` closely enough to judge it.

    A window is assessable only when the meter was seen no longer than
    :data:`~.const.ASSESSABLE_BOUND_HOURS` before it opened *and* again no
    longer than that after it closed. Without both, silence inside the window is
    indistinguishable from missing data — device offline, cloud bucket aged out,
    backfill not caught up — and every verdict drawn from it would be fiction.
    """
    before = bisect_right(readings, start, key=_instant_of)
    if before == 0 or readings[before - 1][0] < start - _BOUND_SLACK:
        return False
    after = bisect_left(readings, end, key=_instant_of)
    return after < len(readings) and readings[after][0] <= end + _BOUND_SLACK


def noon_days(window_days: int, now: datetime, tz: tzinfo) -> list[date]:
    """Return the ``window_days`` most recent completed noon-days, oldest first.

    A noon-day is labelled by the date it closes on, so the newest completed one
    is today once local noon has passed and yesterday before that — the running
    day is never returned, because a partial day cannot be compared against a
    full day's expectation. A window of zero or less yields an empty list.
    """
    if window_days <= 0:
        return []
    local = now.astimezone(tz)
    newest = local.date()
    if local < _noon(newest, tz):
        newest -= _ONE_DAY
    return [newest - offset * _ONE_DAY for offset in reversed(range(window_days))]


def _instant_of(reading: Reading) -> datetime:
    """Return a reading's instant, the ordering key of the series."""
    return reading[0]


def _noon(day: date, tz: tzinfo) -> datetime:
    """Return local noon on ``day``, the instant a noon-day boundary falls on."""
    return datetime.combine(day, _NOON, tzinfo=tz)


def _hour_start(instant: datetime, tz: tzinfo) -> datetime:
    """Return the local hour-start containing ``instant``.

    ``replace`` carries the fold across, so the result keeps the offset of the
    pass it came from and still denotes the right instant — even though the two
    passes of a repeated fall-back hour share one dictionary key and one set
    member, same-zone equality being blind to fold.
    """
    return instant.astimezone(tz).replace(minute=0, second=0, microsecond=0)


def _on_the_hour(local: datetime) -> bool:
    """Return whether a local timestamp sits exactly on a clock hour."""
    return local.minute == 0 and local.second == 0 and local.microsecond == 0


def _next_hour(hour: datetime, tz: tzinfo) -> datetime:
    """Return the local hour-start one absolute hour after ``hour``.

    Stepping through UTC keeps the step a true hour across daylight-saving
    transitions, where wall-clock addition would land on a nonexistent or
    ambiguous local time.
    """
    return (hour.astimezone(UTC) + _HOUR).astimezone(tz)


def _covered_hours(start: datetime, end: datetime, tz: tzinfo) -> Iterator[datetime]:
    """Yield the local hour-starts whose whole hour lies inside ``[start, end]``.

    A partially covered hour at either edge is skipped: an interval only proves
    something about the hours it contains completely.
    """
    hour = _hour_start(start, tz)
    while True:
        following = _next_hour(hour, tz)
        if following > end:
            return
        if hour >= start:
            yield hour
        hour = following
