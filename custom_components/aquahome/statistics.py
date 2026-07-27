"""External water-usage statistics for AquaHome devices.

Home Assistant's long-term statistics only start when an entity does, so the
history the iQua cloud already holds — years of it — would stay invisible to the
Energy dashboard. This module imports that history as an *external* statistic
series, ``aquahome:<device_slug>_water``, next to (never onto) the live
``total_water`` sensor's own statistics.

The import is a **meter read**, not a usage read. The datapoint graph is queried
with :data:`~.const.DATAPOINT_METER_VALUE_TYPE` (``max``), which returns the raw
lifetime counter reading inside each bucket, and consecutive readings are diffed
here. The alternative the phone app uses (``max_diff``) drops the water that
flows between two buckets and measurably under-counts (~4 % at hourly
resolution, live-verified 2026-07-27); diffing absolute readings cannot lose
water, because anything one bucket misses simply lands in the next bucket's
delta. That also makes gaps cheap: a throttled, mis-labelled or aged-out bucket
costs attribution detail, never volume.

A ``0`` value means "no reading in this bucket" — responses are always
zero-filled rather than empty, and a lifetime counter is never genuinely zero —
so zeros are dropped instead of being imported as a meter reset. Hourly readings
are retained for roughly 130 days and daily ones for years, so every run merges
both resolutions: a day with hourly coverage is imported hour by hour, an older
day contributes its single daily reading at local midnight.

Runs are idempotent. Rows are keyed by their bucket start and
:func:`~homeassistant.components.recorder.statistics.async_add_external_statistics`
upserts them, so re-running over the same readings regenerates identical rows.
Each run recomputes only the newest :data:`~.const.BACKFILL_OVERLAP_DAYS` days —
anchored on the last stored row before that cutoff, whose ``state`` and ``sum``
seed the running total — which absorbs readings the device uploaded late while
never rewriting history behind the anchor. That is what stops the aging-out of
hourly retention from degrading already-stored hourly rows into daily ones.

The response ``units`` string is both account-preference-driven and
server-localized, so every request pins ``accept-language`` to
:data:`~.const.BACKFILL_LANGUAGE` and an unrecognized unit aborts the whole run:
importing mis-scaled volumes into the recorder is far worse than importing none.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING, Final

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    clear_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfVolume
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr

# The recorder component re-exports this only implicitly, which strict typing
# rejects; homeassistant.helpers.recorder is where it is actually defined.
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import VolumeConverter

from .api import (
    ApiError,
    AquaHomeClient,
    AquaHomeConnectionError,
    AuthError,
    RateLimitError,
)
from .const import (
    BACKFILL_DAILY_CHUNK_DAYS,
    BACKFILL_DEPTH_PROBE_YEARS,
    BACKFILL_HOURLY_CHUNK_DAYS,
    BACKFILL_LANGUAGE,
    BACKFILL_OVERLAP_DAYS,
    BACKFILL_REQUEST_PACING_SECONDS,
    DATAPOINT_METER_VALUE_TYPE,
    DATAPOINT_WATER_PROPERTY,
    DOMAIN,
    STATISTICS_UPDATE_INTERVAL,
    TOTAL_WATER_CLAMP_TOLERANCE,
    WATER_STATISTIC_SUFFIX,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import AquaHomeConfigEntry

_LOGGER = logging.getLogger(__name__)

#: Gallons per unit of the response ``units`` string, matched case-insensitively
#: against a deliberately tiny allow-list: the field is free-form in the spec and
#: server-localized, so anything unrecognized must fail the run, not be guessed.
_UNIT_FACTORS: Final[dict[str, float]] = {
    "liters": 1 / 3.785411784,
    "gallons": 1.0,
}

#: Soft dependency guarding every recorder call in this module.
_RECORDER_DOMAIN: Final = "recorder"

#: Statistics fields read from the recorder for the resume anchor.
_ANCHOR_TYPES: Final[set[str]] = {"state", "sum"}

#: Lower bound of the anchor lookup window. Predates any possible AquaHome row
#: while staying far away from the epoch-timestamp edges.
_STATISTICS_EPOCH: Final = datetime(2000, 1, 1, tzinfo=UTC)

#: Cushion applied before snapping a bucket label onto its local calendar
#: period. Labels carry the fixed UTC offset of the request ``start``, so a
#: month or year boundary bucket can land up to an hour either side of the true
#: local boundary; half a day of slack snaps it back without reaching a
#: neighbouring period.
_LABEL_CUSHION: Final = timedelta(hours=12)

#: Days re-fetched before the anchor day on a resume run, so a bucket the cloud
#: re-stamped slightly earlier is still seen. Rows at or before the anchor are
#: dropped afterwards, so the margin only ever costs one extra bucket.
_RESUME_MARGIN_DAYS: Final = 2


def statistic_id_for(device_slug: str) -> str:
    """Return the external statistic id of a device's water-usage series."""
    return f"{DOMAIN}:{device_slug}{WATER_STATISTIC_SUFFIX}"


def normalize_volume_unit(units: str | None) -> float | None:
    """Return the factor converting a reading in ``units`` to gallons.

    ``None`` for a missing or unrecognized unit — including a correctly spelled
    unit in the wrong language (``"Litry"``), which is exactly why every
    backfill request pins :data:`~.const.BACKFILL_LANGUAGE`. Callers must treat
    ``None`` as fatal for the run rather than assuming a default scale.
    """
    if units is None:
        return None
    return _UNIT_FACTORS.get(units.strip().lower())


def local_chunks(
    tz: tzinfo, start_local: datetime, end_local: datetime, max_days: int
) -> Iterator[tuple[datetime, datetime]]:
    """Split a local range into request windows with stable bucket labels.

    The server aligns every bucket of a response to the UTC offset carried by
    the request ``start``, for the whole response — so a window spanning a DST
    transition labels the days past it an hour off their true local midnight.
    Windows are therefore cut at every offset change and at ``max_days``, always
    on a true local midnight.

    Consecutive windows touch (one window's end is the next one's start) and the
    API's ``end`` is inclusive, so the shared boundary bucket is fetched twice
    and deduplicated by start. Where the offset changes, that duplicate falls
    inside the older window's last bucket instead, which is equally harmless.
    """
    start_local = start_local.astimezone(tz)
    end_local = end_local.astimezone(tz)
    if end_local <= start_local:
        return
    span = max(max_days, 1)
    chunk_start = start_local
    cursor = start_local
    days = 0
    while cursor < end_local:
        # Wall-clock arithmetic on an aware datetime lands on the same local
        # time the next day, which is the boundary a day bucket follows across a
        # DST change (the absolute step is 23, 24 or 25 hours).
        nxt = cursor + timedelta(days=1)
        days += 1
        if nxt >= end_local:
            yield chunk_start, end_local
            return
        if days >= span or nxt.utcoffset() != chunk_start.utcoffset():
            yield chunk_start, nxt
            chunk_start = nxt
            days = 0
        cursor = nxt


def merge_resolutions(
    hourly: Sequence[tuple[datetime, float]],
    daily: Sequence[tuple[datetime, float]],
    tz: tzinfo,
) -> list[tuple[datetime, float]]:
    """Merge hourly and daily meter readings into one hour-bucketed series.

    Both inputs are ``(label, reading)`` pairs with the zero placeholders
    already dropped, in whatever unit the response carried. A local day with at
    least one hourly reading is imported hour by hour; a day without one falls
    back to its daily reading, moved onto that day's true local midnight (the
    daily label itself can sit an hour off it, and the last reading of the day is
    the one that survives). Usage before a partial day's first hourly reading is
    not lost: it shows up in the diff against the previous day's reading.

    The result is sorted by instant and deduplicated by hour-floored UTC start,
    first reading wins — a second reading inside the same hour only shifts its
    water into the next hour's delta.
    """
    hourly_days: dict[date, list[tuple[datetime, float]]] = {}
    for label, value in hourly:
        day = dt_util.as_utc(label).astimezone(tz).date()
        hourly_days.setdefault(day, []).append((label, value))

    daily_days: dict[date, tuple[datetime, float]] = {}
    for label, value in daily:
        day = _snap_local(label, tz).date()
        latest = daily_days.get(day)
        if latest is None or label >= latest[0]:
            daily_days[day] = (label, value)

    merged: list[tuple[datetime, float]] = []
    for readings in hourly_days.values():
        merged.extend(readings)
    merged.extend(
        (_local_midnight(tz, day), value)
        for day, (_, value) in daily_days.items()
        if day not in hourly_days
    )

    seen: set[datetime] = set()
    series: list[tuple[datetime, float]] = []
    for label, value in sorted(merged, key=lambda reading: reading[0]):
        start = _floor_to_hour(label)
        if start in seen:
            continue
        seen.add(start)
        series.append((start, value))
    return series


def build_meter_rows(
    readings: Sequence[tuple[datetime, float]],
    anchor_state: float | None,
    anchor_sum: float | None,
) -> list[StatisticData]:
    """Turn meter readings into statistics rows by diffing consecutive readings.

    ``readings`` are ``(hour-floored UTC start, reading in gallons)``; they are
    sorted and deduplicated by start here so the caller cannot mis-order them.
    Each row carries the reading itself as ``state`` and the accumulated volume
    as ``sum``, continuing from ``anchor_state`` / ``anchor_sum`` when a previous
    run left rows behind. Without an anchor the first reading is a baseline: it
    contributes no water, because the water it counts was consumed before the
    series existed.

    Two kinds of backwards step are distinguished. A dip within
    :data:`~.const.TOTAL_WATER_CLAMP_TOLERANCE` of the previous reading is a
    cloud glitch — the bucket is skipped entirely, keeping the previous reading
    as the reference, so the glitch cannot invent a reset or a negative delta. A
    larger drop is a genuine counter reset (firmware or board replacement), and
    the new counter's whole value is the cycle's first delta.
    """
    previous = anchor_state
    total = anchor_sum if anchor_sum is not None else 0.0
    seen: set[datetime] = set()
    rows: list[StatisticData] = []
    for start, reading in sorted(readings, key=lambda item: item[0]):
        if start in seen:
            continue
        seen.add(start)
        if previous is None:
            delta = 0.0
        elif reading >= previous:
            delta = reading - previous
        elif reading >= previous * (1 - TOTAL_WATER_CLAMP_TOLERANCE):
            continue
        else:
            delta = reading
        total += delta
        previous = reading
        rows.append(StatisticData(start=start, state=reading, sum=total))
    return rows


@dataclass(frozen=True, slots=True)
class _Anchor:
    """The stored row a resuming backfill continues from."""

    start: datetime
    state: float
    total: float


@dataclass(slots=True)
class _RunState:
    """Per-run bookkeeping shared by every request of one backfill pass."""

    #: Reading-to-gallons factor of the first response; every later response
    #: must agree with it, or the account changed units mid-run.
    factor: float | None = None
    #: Requests already issued, driving the pacing between them.
    requests: int = 0


class AquaHomeStatisticsCoordinator(DataUpdateCoordinator[None]):
    """Backfill and extend one device's external water-usage statistics.

    Every refresh is a complete, self-contained backfill pass: find the resume
    anchor, discover how deep the retained history goes (first run only), fetch
    the daily and hourly meter readings that matter, and import the resulting
    rows in a single call. It owns no entity and produces no data — the recorder
    is the only consumer — so a failed pass has nothing to serve stale and is
    simply retried on the next :data:`~.const.STATISTICS_UPDATE_INTERVAL`.

    Requests are paced :data:`~.const.BACKFILL_REQUEST_PACING_SECONDS` apart. A
    full first run is under a dozen requests, which keeps even the deepest
    backfill inside a single refill window of the cloud's token bucket.
    """

    def __init__(  # noqa: PLR0913 - contract-fixed dependency-injection signature
        self,
        hass: HomeAssistant,
        entry: AquaHomeConfigEntry,
        client: AquaHomeClient,
        *,
        device_id: str,
        device_slug: str,
        device_name: str,
        tz_id: str | None,
    ) -> None:
        """Bind the coordinator to one device's water-usage history.

        ``tz_id`` is the device's own reported timezone; it decides which local
        days the cloud aligns its buckets to, and falls back to the Home
        Assistant zone when absent or unknown. It is resolved per run rather
        than at construction, so no wall-clock state is bound at setup time.
        """
        self.device_id = device_id
        self.device_slug = device_slug
        self.device_name = device_name
        self.statistic_id = statistic_id_for(device_slug)
        self.client = client
        self._tz_id = tz_id
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {device_slug} statistics",
            update_interval=STATISTICS_UPDATE_INTERVAL,
        )

        @callback
        def _keep_scheduled() -> None:
            """Hold the update interval open."""

        # Nothing subscribes to a statistics coordinator, and the base class
        # only keeps its interval armed while at least one listener exists, so
        # register an inert one (the same reason core's Opower integration
        # does). It lives until the coordinator is shut down with the entry.
        self.async_add_listener(_keep_scheduled)

    async def _async_update_data(self) -> None:
        """Run one backfill pass over this device's water-usage history.

        The recorder is a soft dependency: without it there is nowhere to import
        to, so the pass is skipped and reported successful instead of failing
        every 12 h on an installation that deliberately runs without one.

        Authentication failures raise :class:`ConfigEntryAuthFailed` (straight to
        reauth). Everything else — throttling, connection trouble, a 4xx or 5xx
        contract failure — is an honest :class:`UpdateFailed`: no data is served
        from here, and the next scheduled pass recomputes the very same rows
        from the very same immutable readings.
        """
        if _RECORDER_DOMAIN not in self.hass.config.components:
            _LOGGER.debug(
                "Recorder unavailable; skipping the %s backfill", self.statistic_id
            )
            return
        try:
            await self._async_backfill()
        except AuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (RateLimitError, AquaHomeConnectionError, ApiError) as err:
            raise UpdateFailed(str(err)) from err

    async def _async_backfill(self) -> None:
        """Fetch, merge and import the readings this run is responsible for."""
        tz = await self._async_resolve_timezone()
        now = dt_util.utcnow()
        now_local = now.astimezone(tz)
        run = _RunState()

        anchor = await self._async_load_anchor(now)
        if anchor is None:
            start_local = await self._async_probe_history_start(run, tz, now_local)
            if start_local is None:
                _LOGGER.info(
                    "No water datapoints retained for %s; nothing to import",
                    self.device_slug,
                )
                return
        else:
            resume_day = anchor.start.astimezone(tz) - timedelta(
                days=_RESUME_MARGIN_DAYS
            )
            start_local = _local_midnight(tz, resume_day.date())

        daily = await self._async_fetch_daily(run, tz, start_local, now_local)
        hourly = await self._async_fetch_hourly(run, tz, start_local, now_local)
        factor = run.factor
        if factor is None:
            _LOGGER.debug("No datapoint window to fetch for %s", self.statistic_id)
            return

        cutoff = anchor.start if anchor is not None else None
        readings = [
            (start, value * factor)
            for start, value in merge_resolutions(hourly, daily, tz)
            if cutoff is None or start > cutoff
        ]
        rows = build_meter_rows(
            readings,
            anchor.state if anchor is not None else None,
            anchor.total if anchor is not None else None,
        )
        if not rows:
            _LOGGER.debug("No new water statistics rows for %s", self.statistic_id)
            return
        async_add_external_statistics(self.hass, self._metadata(), rows)
        _LOGGER.debug(
            "Imported %s water statistics rows for %s (%s to %s)",
            len(rows),
            self.statistic_id,
            rows[0]["start"],
            rows[-1]["start"],
        )

    def _metadata(self) -> StatisticMetaData:
        """Describe the series to the recorder.

        The name is English on purpose: an external statistic has no entity and
        therefore no translation machinery behind it. Gallons is the fixed
        native unit — the account's own preference is a display setting and must
        never reach stored statistics — and the volume unit class lets Home
        Assistant convert the series for display.
        """
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{self.device_name} water usage history",
            source=DOMAIN,
            statistic_id=self.statistic_id,
            unit_class=VolumeConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfVolume.GALLONS,
        )

    async def _async_resolve_timezone(self) -> tzinfo:
        """Return the zone the cloud aligns this device's buckets to.

        The device's own reported zone wins; an absent or unknown one falls back
        to the Home Assistant zone, which every installation has.
        """
        if self._tz_id:
            zone = await dt_util.async_get_time_zone(self._tz_id)
            if zone is not None:
                return zone
            _LOGGER.debug(
                "Device %s reports unknown timezone %s; using the Home Assistant zone",
                self.device_slug,
                self._tz_id,
            )
        configured = self.hass.config.time_zone
        if configured:
            zone = await dt_util.async_get_time_zone(configured)
            if zone is not None:
                return zone
        return dt_util.get_default_time_zone()

    async def _async_load_anchor(self, now: datetime) -> _Anchor | None:
        """Return the stored row this run continues from, or ``None``.

        The newest row before the overlap cutoff seeds the running counter and
        total, so the run rewrites only the last
        :data:`~.const.BACKFILL_OVERLAP_DAYS`. A series younger than that window
        has no such row, and that is deliberately treated as a full recompute:
        the row algorithm is deterministic over immutable meter readings, so it
        reproduces the stored rows byte for byte.
        """
        instance = get_instance(self.hass)
        latest = await instance.async_add_executor_job(
            get_last_statistics, self.hass, 1, self.statistic_id, False, _ANCHOR_TYPES
        )
        if not latest.get(self.statistic_id):
            return None
        cutoff = now - timedelta(days=BACKFILL_OVERLAP_DAYS)
        stored = await instance.async_add_executor_job(
            statistics_during_period,
            self.hass,
            _STATISTICS_EPOCH,
            cutoff,
            {self.statistic_id},
            "hour",
            None,
            _ANCHOR_TYPES,
        )
        rows = stored.get(self.statistic_id)
        if not rows:
            return None
        row = rows[-1]
        state = row.get("state")
        total = row.get("sum")
        if state is None or total is None:
            return None
        return _Anchor(
            start=dt_util.utc_from_timestamp(row["start"]), state=state, total=total
        )

    async def _async_probe_history_start(
        self, run: _RunState, tz: tzinfo, now_local: datetime
    ) -> datetime | None:
        """Return the local day the retained readings begin at, or ``None``.

        Two coarse sweeps hold the probe at two requests however much history
        the account has: a yearly sweep over the last
        :data:`~.const.BACKFILL_DEPTH_PROBE_YEARS` finds the earliest year
        holding a reading, a monthly sweep over that year finds the earliest
        month. An all-zero yearly sweep means the cloud retains nothing for this
        device — a brand-new or long-offline softener — which is a successful
        run that imports nothing.
        """
        first_year = now_local.year - BACKFILL_DEPTH_PROBE_YEARS
        yearly = await self._async_fetch_series(
            run, "year", _local_midnight(tz, date(first_year, 1, 1)), now_local
        )
        if not yearly:
            return None
        earliest_year = _snap_local(min(start for start, _ in yearly), tz).year
        monthly = await self._async_fetch_series(
            run, "month", _local_midnight(tz, date(earliest_year, 1, 1)), now_local
        )
        if not monthly:
            return None
        earliest_month = _snap_local(min(start for start, _ in monthly), tz)
        return _local_midnight(tz, earliest_month.date().replace(day=1))

    async def _async_fetch_daily(
        self, run: _RunState, tz: tzinfo, start_local: datetime, end_local: datetime
    ) -> list[tuple[datetime, float]]:
        """Fetch the daily meter readings covering the whole import range."""
        readings: list[tuple[datetime, float]] = []
        for start, end in local_chunks(
            tz, start_local, end_local, BACKFILL_DAILY_CHUNK_DAYS
        ):
            readings.extend(await self._async_fetch_series(run, "day", start, end))
        return readings

    async def _async_fetch_hourly(
        self, run: _RunState, tz: tzinfo, floor_local: datetime, end_local: datetime
    ) -> list[tuple[datetime, float]]:
        """Fetch hourly readings backwards from now to the retention floor.

        Hourly retention is a rolling window (~130 days on the reference
        device), and past its edge the response zero-fills even where daily data
        exists. Rather than guessing where the edge is, windows are walked
        backwards until one comes back without a single reading, or until the
        daily range's own start is reached — so a resuming run costs one window
        and a first run stops one window past the real floor.
        """
        readings: list[tuple[datetime, float]] = []
        window_end = end_local.astimezone(tz)
        floor_local = floor_local.astimezone(tz)
        while window_end > floor_local:
            step_back = window_end - timedelta(days=BACKFILL_HOURLY_CHUNK_DAYS)
            window_start = max(_local_midnight(tz, step_back.date()), floor_local)
            if window_start >= window_end:
                break
            window: list[tuple[datetime, float]] = []
            for start, end in local_chunks(
                tz, window_start, window_end, BACKFILL_HOURLY_CHUNK_DAYS
            ):
                window.extend(await self._async_fetch_series(run, "hour", start, end))
            if not window:
                break
            readings.extend(window)
            window_end = window_start
        return readings

    async def _async_fetch_series(
        self, run: _RunState, period_type: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, float]]:
        """Fetch one datapoint window and return its readings, zeros dropped.

        Requests are paced apart, the language is pinned so the ``units`` string
        is parseable, and an unrecognized or changing unit aborts the run before
        anything reaches the recorder.
        """
        if run.requests:
            await asyncio.sleep(BACKFILL_REQUEST_PACING_SECONDS)
        run.requests += 1
        graph = await self.client.async_get_datapoint_graph(
            self.device_id,
            DATAPOINT_WATER_PROPERTY,
            period_type=period_type,
            start=start,
            end=end,
            value_type=DATAPOINT_METER_VALUE_TYPE,
            language=BACKFILL_LANGUAGE,
        )
        factor = normalize_volume_unit(graph.units)
        if factor is None:
            msg = (
                f"Unrecognized water datapoint units {graph.units!r} for "
                f"{self.device_slug}; refusing to import mis-scaled statistics"
            )
            raise UpdateFailed(msg)
        if run.factor is None:
            run.factor = factor
        elif run.factor != factor:
            msg = (
                f"Water datapoint units changed to {graph.units!r} mid-backfill for "
                f"{self.device_slug}; refusing to import mixed-scale statistics"
            )
            raise UpdateFailed(msg)

        readings: list[tuple[datetime, float]] = []
        for point in graph.data:
            label, value = point.label, point.value
            # A zero is the API's "no reading here" placeholder: the series is
            # zero-filled, and a lifetime counter never genuinely reads zero.
            if label is None or value is None or value <= 0:
                continue
            readings.append((dt_util.as_utc(label), value))
        return readings


async def async_clear_device_statistics(
    hass: HomeAssistant, entry: AquaHomeConfigEntry
) -> None:
    """Delete the external water statistics of every device in an entry.

    Called from ``async_remove_entry``, which runs on an entry that may never
    have been loaded, so the statistic ids are rebuilt from the device registry
    (each AquaHome device identifier is exactly the slug the ids are built from)
    rather than from runtime data. Ids the recorder does not know are a harmless
    no-op, so nothing here depends on statistics having been imported.
    """
    if _RECORDER_DOMAIN not in hass.config.components:
        _LOGGER.debug("Recorder unavailable; leaving AquaHome statistics untouched")
        return
    registry = dr.async_get(hass)
    statistic_ids = [
        statistic_id_for(identifier)
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
        for domain, identifier in device.identifiers
        if domain == DOMAIN
    ]
    if not statistic_ids:
        return
    instance = get_instance(hass)
    await instance.async_add_executor_job(clear_statistics, instance, statistic_ids)
    _LOGGER.debug("Cleared AquaHome statistics %s", ", ".join(statistic_ids))


def _local_midnight(tz: tzinfo, day: date) -> datetime:
    """Return the start of a local calendar day in ``tz``."""
    return datetime.combine(day, time.min, tzinfo=tz)


def _floor_to_hour(moment: datetime) -> datetime:
    """Return the UTC hour bucket containing ``moment``.

    Statistics rows must start on the top of a UTC hour, which the buckets of a
    half-hour-offset zone never do on their own.
    """
    return dt_util.as_utc(moment).replace(minute=0, second=0, microsecond=0)


def _snap_local(moment: datetime, tz: tzinfo) -> datetime:
    """Return ``moment`` as local time, snapped onto its calendar period.

    Bucket labels carry the UTC offset of their request's start, so a day, month
    or year boundary can be reported up to an hour off the true local one; the
    cushion pulls such a label back inside the period it belongs to.
    """
    return (dt_util.as_utc(moment) + _LABEL_CUSHION).astimezone(tz)
