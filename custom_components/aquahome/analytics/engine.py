"""The Home Assistant-facing analytics engine for AquaHome devices.

One :class:`AquaHomeAnalyticsEngine` per device gathers everything a detection
pass needs — the imported water-usage statistics (hourly LTS, never raw states),
the regeneration history for the mandatory night masking, and the device's own
weekday averages for the cold start — dispatches the pure numpy computation to
an executor, and publishes the :class:`~.model.AnalyticsResult` to the
detection entities. Verdict transitions additionally fire
:data:`~..const.EVENT_AQUAHOME` bus events so automations can react without
polling entity state.

Alongside the published pass the engine answers one on-demand question:
:meth:`AquaHomeAnalyticsEngine.async_compute_forecasts` resolves the coming
days' expectations for the forecast service. It gathers the same inputs and
dispatches to the same executor, but publishes nothing and fires no event — a
question about the future must never move a detector's verdict.

The engine is deliberately stateless across runs: every verdict, including the
multi-night leak debounce, is recomputed from the statistics window, so a Home
Assistant restart can never lose or fabricate detector state (owner decision
2026-07-27). It runs once at startup — sequenced after the statistics backfill
by ``__init__`` so detectors work from day one over replayed nights — and then
daily at :data:`~..const.ANALYTICS_RUN_LOCAL_TIME` device-local, just after the
minimum-night-flow window closes, so the freshest complete night is classified
the same morning. The daily trigger first refreshes the statistics coordinator
(the overnight readings are not in the recorder yet — its own cadence is
12-hourly) and only then recomputes.

The engine talks to no cloud endpoint itself and can therefore never raise an
authentication error; a recorder read failure is an honest ``UpdateFailed``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal

from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from custom_components.aquahome.api import scaled_value
from custom_components.aquahome.const import (
    ANALYTICS_RUN_LOCAL_TIME,
    BASELINE_WINDOW_DAYS,
    DOMAIN,
    EVENT_AQUAHOME,
    EVENT_TYPE_LEAK_CLEARED,
    EVENT_TYPE_LEAK_SUSPECTED,
    EVENT_TYPE_USAGE_ANOMALY,
    EVENT_TYPE_USAGE_ANOMALY_CLEARED,
    EVENT_TYPE_VACATION_ENDED,
    EVENT_TYPE_VACATION_STARTED,
    NOMINAL_REGEN_DURATION,
)

from .detectors import compute_analytics, compute_forecasts
from .model import (
    AnalyticsInputs,
    AnalyticsResult,
    ForecastState,
    Reading,
    WeekdaySlot,
)

if TYPE_CHECKING:
    from datetime import date

    from homeassistant.components.recorder.statistics import StatisticsRow
    from homeassistant.core import HomeAssistant

    from custom_components.aquahome.api import Device
    from custom_components.aquahome.coordinator import (
        AquaHomeActivityCoordinator,
        AquaHomeConfigEntry,
        AquaHomeCoordinator,
        DeviceActivity,
    )
    from custom_components.aquahome.statistics import AquaHomeStatisticsCoordinator

_LOGGER = logging.getLogger(__name__)

#: Soft dependency guarding the statistics read, mirroring the statistics module.
_RECORDER_DOMAIN: Final = "recorder"

#: Statistics column the meter series is rebuilt from. ``sum`` is the imported
#: cumulative usage — meter-read semantics survive the import, so consecutive
#: sums diff to the water used between readings with resets already absorbed.
#: (Typed with the recorder API's full literal vocabulary so the call to
#: ``statistics_during_period`` type-checks under strict mypy.)
_SUM_TYPES: Final[
    set[Literal["change", "last_reset", "max", "mean", "min", "state", "sum"]]
] = {"sum"}

#: Number of device weekday-average slots (slot 1 = Saturday, live-verified Map B).
_WEEKDAY_SLOT_COUNT: Final = 7


class AquaHomeAnalyticsEngine(DataUpdateCoordinator[AnalyticsResult]):
    """Compute one device's analytics verdicts from its imported statistics."""

    def __init__(  # noqa: PLR0913 - contract-fixed dependency-injection signature
        self,
        hass: HomeAssistant,
        entry: AquaHomeConfigEntry,
        *,
        device_id: str,
        device_slug: str,
        fast: AquaHomeCoordinator,
        activity: AquaHomeActivityCoordinator,
        statistics: AquaHomeStatisticsCoordinator,
    ) -> None:
        """Bind the engine to one device and its sibling coordinators.

        The fast coordinator supplies the weekday-average cold start and the
        device-online signal, the activity coordinator the regeneration windows
        for masking, and the statistics coordinator the statistic id, the
        timezone resolution, and the pre-run refresh of the imported series.
        """
        self.device_id = device_id
        self.device_slug = device_slug
        self._fast = fast
        self._activity = activity
        self._statistics = statistics
        self._unsub_schedule: CALLBACK_TYPE | None = None
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {device_slug} analytics",
            update_interval=None,
        )

        @callback
        def _keep_alive() -> None:
            """Keep the coordinator serviceable with zero entity listeners."""

        # Detection entities may all be disabled by the user; the engine must
        # keep running (events, repairs) regardless, so hold an inert listener
        # exactly like the statistics coordinator does.
        self.async_add_listener(_keep_alive)

    async def _async_update_data(self) -> AnalyticsResult:
        """Run one full analytics pass and fire transition events.

        Without a recorder there is no imported series to read: the pass still
        runs — the cold-start forecast needs only the device's own weekday
        averages — and every detector honestly reports "nothing to assess".
        """
        previous = self.data
        readings = await self._async_load_readings()
        tz = await self._statistics.async_resolve_timezone()
        tz_key = getattr(tz, "key", None) or "UTC"
        result = await self.hass.async_add_executor_job(
            compute_analytics, self._build_inputs(readings, tz_key)
        )
        self._fire_transitions(previous, result)
        return result

    async def async_compute_forecasts(
        self, days: int
    ) -> tuple[tuple[date, ForecastState], ...]:
        """Return the coming ``days`` device-local daily forecasts, newest last.

        A read-only sibling of :meth:`_async_update_data`: the same readings,
        the same timezone resolution and the same executor dispatch, but the
        result is handed straight back to the caller — no coordinator update,
        no transition events, and no effect whatsoever on the published
        verdicts. A recorder failure surfaces as :class:`UpdateFailed` exactly
        as it does for a full pass, which the service layer turns into a
        user-facing error.
        """
        readings = await self._async_load_readings()
        tz = await self._statistics.async_resolve_timezone()
        tz_key = getattr(tz, "key", None) or "UTC"
        return await self.hass.async_add_executor_job(
            compute_forecasts, self._build_inputs(readings, tz_key), days
        )

    async def async_schedule(self) -> None:
        """Arm the daily run at the next device-local run time.

        Re-armed after every firing (and re-resolved against the device zone
        each time, so a DST change or a device timezone change is honoured on
        the next cycle at the latest). The scheduled run refreshes the
        statistics coordinator first: the overnight meter readings the night
        verdict needs are not in the recorder yet on a 12-hour backfill cadence.
        """
        self.async_cancel_schedule()
        tz = await self._statistics.async_resolve_timezone()
        now_local = dt_util.utcnow().astimezone(tz)
        run_at = datetime.combine(now_local.date(), ANALYTICS_RUN_LOCAL_TIME, tzinfo=tz)
        if run_at <= now_local:
            run_at = datetime.combine(
                now_local.date() + timedelta(days=1),
                ANALYTICS_RUN_LOCAL_TIME,
                tzinfo=tz,
            )

        async def _run(_now: datetime) -> None:
            """Execute one scheduled pass and arm the next one."""
            self._unsub_schedule = None
            await self.async_schedule()
            await self._statistics.async_refresh()
            await self.async_refresh()

        self._unsub_schedule = async_track_point_in_time(self.hass, _run, run_at)
        _LOGGER.debug(
            "Scheduled the next %s analytics run at %s", self.device_slug, run_at
        )

    @callback
    def async_cancel_schedule(self) -> None:
        """Cancel a pending scheduled run, if one is armed."""
        if self._unsub_schedule is not None:
            self._unsub_schedule()
            self._unsub_schedule = None

    async def async_shutdown(self) -> None:
        """Cancel the daily schedule alongside the coordinator shutdown."""
        self.async_cancel_schedule()
        await super().async_shutdown()

    async def _async_load_readings(self) -> tuple[Reading, ...]:
        """Read the device's imported meter series back from the recorder.

        Returns the empty series when no recorder is configured (a deliberate
        installation choice the detectors must survive) and raises
        :class:`UpdateFailed` when the recorder exists but the read fails.
        """
        if _RECORDER_DOMAIN not in self.hass.config.components:
            _LOGGER.debug(
                "Recorder unavailable; %s analytics runs without history",
                self.device_slug,
            )
            return ()
        start = dt_util.utcnow() - timedelta(days=BASELINE_WINDOW_DAYS)
        try:
            stats = await get_instance(self.hass).async_add_executor_job(
                self._read_statistics, start
            )
        except Exception as err:
            msg = f"Reading imported statistics failed: {err}"
            raise UpdateFailed(msg) from err
        rows = stats.get(self._statistics.statistic_id, [])
        readings: list[Reading] = []
        for row in rows:
            total = row.get("sum")
            if total is None:
                continue
            readings.append((dt_util.utc_from_timestamp(row["start"]), total))
        return tuple(readings)

    def _read_statistics(self, start: datetime) -> dict[str, list[StatisticsRow]]:
        """Fetch the hourly statistics rows (runs on the recorder executor)."""
        return statistics_during_period(
            self.hass,
            start,
            None,
            {self._statistics.statistic_id},
            "hour",
            None,
            _SUM_TYPES,
        )

    def _build_inputs(
        self, readings: tuple[Reading, ...], tz_key: str
    ) -> AnalyticsInputs:
        """Assemble one pass's inputs from the sibling coordinators."""
        device: Device | None = self._fast.data
        regen_windows, coverage_start = _regen_windows(self._activity.data)
        return AnalyticsInputs(
            readings=readings,
            regen_windows=regen_windows,
            regen_coverage_start=coverage_start,
            weekday_slots=_weekday_slots(device),
            overall_average=_overall_average(device),
            tz_key=tz_key,
            now=dt_util.utcnow(),
            device_online=self._fast.device_online,
            statistics_fresh=self._statistics.last_update_success,
        )

    def _fire_transitions(
        self, previous: AnalyticsResult | None, result: AnalyticsResult
    ) -> None:
        """Fire one bus event per detector whose verdict flipped.

        Only genuine boolean flips fire; a transition from or to ``None``
        (nothing to assess) is silence, never an alarm or an all-clear.
        """
        if previous is None:
            return
        self._fire_flip(
            previous.leak.active,
            result.leak.active,
            EVENT_TYPE_LEAK_SUSPECTED,
            EVENT_TYPE_LEAK_CLEARED,
            {
                "rate_liters_per_hour": result.leak.rate_liters_per_hour,
                "tier": result.leak.tier,
            },
        )
        self._fire_flip(
            previous.anomaly.active,
            result.anomaly.active,
            EVENT_TYPE_USAGE_ANOMALY,
            EVENT_TYPE_USAGE_ANOMALY_CLEARED,
            {"reasons": list(result.anomaly.reasons)},
        )
        self._fire_flip(
            previous.vacation.active,
            result.vacation.active,
            EVENT_TYPE_VACATION_STARTED,
            EVENT_TYPE_VACATION_ENDED,
            {
                "since": result.vacation.since.isoformat()
                if result.vacation.since is not None
                else None,
                "consecutive_days": result.vacation.consecutive_days,
            },
        )

    def _fire_flip(
        self,
        was: bool | None,
        now: bool | None,
        on_type: str,
        off_type: str,
        detail: dict[str, object],
    ) -> None:
        """Fire the on/off event for one detector's boolean transition."""
        if was is None or now is None or was == now:
            return
        self.hass.bus.async_fire(
            EVENT_AQUAHOME,
            {
                "device_id": self.device_id,
                "device": self.device_slug,
                "type": on_type if now else off_type,
                **detail,
            },
        )


def _regen_windows(
    activity: DeviceActivity | None,
) -> tuple[tuple[tuple[datetime, datetime], ...], datetime | None]:
    """Return the known regeneration windows and the start of their coverage.

    An event still running at fetch time has no end yet and is padded with
    :data:`~..const.NOMINAL_REGEN_DURATION` so the night it is drawing water on
    is masked either way. Without any activity data there is no coverage — and
    the detectors then refuse to declare any LEAK night.
    """
    if activity is None:
        return (), None
    windows: list[tuple[datetime, datetime]] = []
    for event in activity.regeneration_events:
        if event.start_time is None:
            continue
        end = event.end_time or (event.start_time + NOMINAL_REGEN_DURATION)
        windows.append((event.start_time, end))
    if not windows:
        return (), None
    windows.sort(key=lambda window: window[0])
    return tuple(windows), windows[0][0]


def _weekday_slots(device: Device | None) -> tuple[WeekdaySlot, ...]:
    """Return the device's seven weekday-average slots (slot 1 first).

    Each slot pairs ``avg_daily_use_day_N_gals`` with its deviation twin and
    carries the property's change-stamp for the freshness guard. A missing
    property yields an empty slot rather than dropping the index.
    """
    slots: list[WeekdaySlot] = []
    for index in range(1, _WEEKDAY_SLOT_COUNT + 1):
        slots.append(
            _slot_from_properties(
                device,
                f"avg_daily_use_day_{index}_gals",
                f"avg_daily_dev_day_{index}_gals",
            )
        )
    return tuple(slots)


def _overall_average(device: Device | None) -> WeekdaySlot:
    """Return the device's overall daily average wrapped as a slot."""
    return _slot_from_properties(device, "avg_daily_use_gals", None)


def _slot_from_properties(
    device: Device | None, average_name: str, deviation_name: str | None
) -> WeekdaySlot:
    """Build one :class:`WeekdaySlot` from the raw property map."""
    if device is None:
        return WeekdaySlot(average_gal=None, deviation_gal=None, updated_at=None)
    average = device.properties.get(average_name)
    deviation = (
        device.properties.get(deviation_name) if deviation_name is not None else None
    )
    return WeekdaySlot(
        average_gal=scaled_value(average) if average is not None else None,
        deviation_gal=scaled_value(deviation) if deviation is not None else None,
        updated_at=average.updated_at if average is not None else None,
    )
