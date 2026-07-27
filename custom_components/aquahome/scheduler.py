"""The per-device regeneration scheduler: the automation tier's decision maker.

One :class:`AquaHomeRegenScheduler` per device owns every piece of automation
state the integration keeps — the three default-off opt-in flags, the vacation
deferral bookkeeping, and the scheduler's own verdict trail — and is the only
place that turns an analytics verdict into a device command. The switch
platform, the service layer and the repair flows all mutate that state through
this coordinator's small public API, so there is exactly one code path that
persists a flag and exactly one that talks to the cloud.

Everything here is built on the *live-verified* ``regenerate`` command surface
(``schedule`` / ``cancel``). The iQua app's vacation tile has its own
``/command`` payloads which remain unverified (gap-analysis ledger P1), so
"vacation" in this module means *deferral*: while it is active a regeneration
the device schedules for itself is cancelled again, and a resin-hygiene cap
(:data:`~.const.REGEN_DEFERRAL_MAX_DAYS`) eventually lets one through rather
than leaving the resin bed unregenerated indefinitely.

Two evaluators drive it, both subscribed as plain listeners:

* the **engine evaluator** runs on every analytics pass (startup, the nightly
  run, a service-triggered refresh). It follows the vacation verdict when the
  user asked it to, then — with smart regeneration on — takes the nightly
  decision: schedule a recharge when the remaining treated-water capacity is
  below tomorrow's forecast plus a :data:`~.const.FORECAST_RESERVE_FACTOR`
  reserve, at most once per device-local day.
* the **fast evaluator** runs on every telemetry poll and enforces an active
  deferral, capped at :data:`~.const.REGEN_CANCEL_DAILY_BUDGET` cancels per
  device-local day so a disagreement with the device's own scheduling logic can
  never become a command fight on a throttled cloud.

The scheduler is a pure *consumer* of the analytics tier: it never triggers an
engine refresh and never touches the recorder, so an automation decision costs
nothing beyond the single command it sends. Every condition it checks is
recorded — the ordered ``skipped_*`` literals below say exactly why a night
passed without action, which is the difference between an automation the owner
can trust and one they have to reverse-engineer.

Data-source discipline: the remaining capacity is read from the RAW
``treated_water_avail_gals`` property (gallons, unscaled) rather than the
enriched tile, which lags a poll behind.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, timedelta
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import scaled_value
from .automation_state import AutomationState, options_with_state, state_from_options
from .command import async_execute_command
from .const import (
    DEFERRAL_SOURCE_AUTO,
    DOMAIN,
    EVENT_AQUAHOME,
    EVENT_TYPE_REGEN_DEFERRAL_EXPIRED,
    EVENT_TYPE_REGEN_DEFERRED,
    EVENT_TYPE_REGEN_SCHEDULED,
    FORECAST_RESERVE_FACTOR,
    RECHARGE_STATE_READY,
    RECHARGE_STATE_SCHEDULED,
    REGEN_CANCEL_DAILY_BUDGET,
    REGEN_DEFERRAL_MAX_DAYS,
    REGEN_REASON_CATCH_UP,
    REGEN_REASON_LOW_CAPACITY,
)

if TYPE_CHECKING:
    from datetime import date, datetime, tzinfo

    from homeassistant.core import HomeAssistant

    from .analytics.engine import AquaHomeAnalyticsEngine
    from .analytics.model import AnalyticsResult
    from .api import AquaHomeClient, Device
    from .coordinator import (
        AquaHomeConfigEntry,
        AquaHomeCoordinator,
        AquaHomeSettingsCoordinator,
    )

_LOGGER = logging.getLogger(__name__)

#: The only device function the automation tier commands. Its three actions are
#: the ones proven live; nothing here depends on an unverified payload.
_COMMAND_FUNCTION: Final = "regenerate"
_ACTION_SCHEDULE: Final = "schedule"
_ACTION_CANCEL: Final = "cancel"

#: Raw property carrying the remaining treated-water capacity (gallons, x1).
_CAPACITY_PROPERTY: Final = "treated_water_avail_gals"

#: Raw property carrying the device's own IANA timezone.
_TIMEZONE_PROPERTY: Final = "tz_id"

#: ``regeneration_status`` value meaning a recharge is running right now.
_REGENERATING: Final = "regenerating"

# Verdict literals published as ``AutomationState.last_decision`` — the
# scheduler's whole observability surface, exposed as an attribute of the
# smart-regeneration switch. The ``skipped_*`` values are recorded in the exact
# order the preconditions are checked, so the first unmet one is always the one
# reported.
DECISION_SCHEDULED: Final = "scheduled"
DECISION_DEFERRED: Final = "deferred"
DECISION_NOT_NEEDED: Final = "not_needed"
DECISION_CATCH_UP: Final = "catch_up"
DECISION_DEFERRAL_EXPIRED: Final = "deferral_expired"
DECISION_SKIPPED_OFF: Final = "skipped_off"
DECISION_SKIPPED_DEFERRAL: Final = "skipped_deferral"
DECISION_SKIPPED_VACATION: Final = "skipped_vacation"
DECISION_SKIPPED_OFFLINE: Final = "skipped_offline"
DECISION_SKIPPED_NOT_ALLOWED: Final = "skipped_not_allowed"
DECISION_SKIPPED_NOT_READY: Final = "skipped_not_ready"
DECISION_SKIPPED_NO_FORECAST: Final = "skipped_no_forecast"
DECISION_SKIPPED_NO_CAPACITY: Final = "skipped_no_capacity"
DECISION_SKIPPED_ALREADY_TODAY: Final = "skipped_already_today"
DECISION_SKIPPED_COMMAND_FAILED: Final = "skipped_command_failed"

#: Verdict recorded for each reason a schedule command is sent for.
_DECISION_FOR_REASON: Final = {
    REGEN_REASON_LOW_CAPACITY: DECISION_SCHEDULED,
    REGEN_REASON_CATCH_UP: DECISION_CATCH_UP,
}


# ---------------------------------------------------------------------------
# None-safe payload accessors
#
# Deliberately local copies of the button platform's tiny recharge accessors
# (the same replication rule the setting platforms follow for their shared
# classification): the scheduler must not import an entity platform to reach a
# three-line payload read.
# ---------------------------------------------------------------------------


def _can_schedule(device: Device | None) -> bool | None:
    """Return the device's ``can_schedule`` hint, ``recharge_ui`` taking priority.

    The offline-capable ``recharge_ui`` tile is authoritative when present; only
    when it is absent (an ``iqua2`` host) does the value come from the
    ``regeneration`` block. ``None`` when neither carries the hint — the caller
    then treats scheduling as allowed rather than guessing.
    """
    enriched = device.enriched_data if device is not None else None
    if enriched is None:
        return None
    if enriched.recharge_ui is not None:
        return enriched.recharge_ui.can_schedule
    regeneration = enriched.regeneration
    return regeneration.can_schedule if regeneration is not None else None


def _recharge_state(device: Device | None) -> tuple[str | None, bool]:
    """Return the device's recharge state and whether the tile reported it.

    The ``recharge_ui`` tile's ``state`` wins when present; otherwise the
    enriched ``regeneration`` block's ``regeneration_status`` stands in. The
    flag distinguishes the two vocabularies: the tile names the ready state
    explicitly, while the fallback only names the busy ones.
    """
    enriched = device.enriched_data if device is not None else None
    if enriched is None:
        return None, False
    recharge_ui = enriched.recharge_ui
    if recharge_ui is not None and recharge_ui.state is not None:
        return recharge_ui.state, True
    regeneration = enriched.regeneration
    if regeneration is not None:
        return regeneration.regeneration_status, False
    return None, False


def _recharge_ready(device: Device | None) -> bool:
    """Return whether the device is ready to accept a scheduled recharge.

    From the tile that means the explicit ``ready`` state. From the
    ``regeneration`` fallback it means anything other than the two busy values
    (``scheduled`` / ``regenerating``), since that block reports no ready state
    of its own — it reads ``none`` on an idle device. Without either source the
    answer is ``False``: an unknown state is never assumed schedulable.
    """
    state, from_tile = _recharge_state(device)
    if state is None:
        return False
    if from_tile:
        return state == RECHARGE_STATE_READY
    return state not in (RECHARGE_STATE_SCHEDULED, _REGENERATING)


def _recharge_scheduled(device: Device | None) -> bool:
    """Return whether a regeneration is currently scheduled on the device.

    Both vocabularies name this state identically, so one comparison covers the
    tile and the ``regeneration`` fallback; an absent state is not scheduled.
    """
    state, _ = _recharge_state(device)
    return state == RECHARGE_STATE_SCHEDULED


def _capacity_gallons(device: Device | None) -> float | None:
    """Return the remaining treated-water capacity in gallons, or ``None``.

    Read from the raw property rather than the enriched tile (raw first: the
    enriched block lags a poll behind), and ``None`` whenever the device does
    not report it — which honestly skips the scheduling decision instead of
    treating an unknown capacity as empty.
    """
    if device is None:
        return None
    prop = device.properties.get(_CAPACITY_PROPERTY)
    return scaled_value(prop) if prop is not None else None


def _forecast_gallons(result: AnalyticsResult | None) -> float | None:
    """Return tomorrow's forecast usage in gallons from an analytics pass."""
    return result.forecast.gallons if result is not None else None


def _capacity_low(capacity: float | None, forecast: float | None) -> bool:
    """Return whether the capacity falls short of the reserved forecast.

    Strictly below ``forecast x FORECAST_RESERVE_FACTOR``: a capacity exactly
    on the reserve line still covers tomorrow and needs no action. An unknown
    capacity or forecast is never "low" — the callers report that as their own
    honest skip.
    """
    if capacity is None or forecast is None:
        return False
    return capacity < forecast * FORECAST_RESERVE_FACTOR


def _deferral_age(state: AutomationState, now: datetime) -> timedelta | None:
    """Return how long the active deferral has been running, or ``None``.

    ``None`` when the state carries no start stamp (an option written by hand
    or by a future version): the resin-hygiene cap exists to protect the resin,
    and guessing an age could only cut a deferral the user asked for short. A
    naive stamp is interpreted in the Home Assistant zone rather than rejected.
    """
    started = state.deferral_started
    if started is None:
        return None
    return now - dt_util.as_utc(started)


def _device_timezone(device: Device | None, device_slug: str) -> tzinfo:
    """Return the zone the device dates its own days in, falling back to UTC.

    The scheduler's once-per-day latch and its cancel budget are keyed by the
    *device-local* day, which is the day the softener itself schedules against.
    Unlike the statistics coordinator the scheduler has no Home Assistant zone
    fallback to offer — a device that reports no (or an unusable) ``tz_id``
    falls back to UTC, which keeps the latches consistent even if their
    rollover is not local midnight. Zone lookups are cached by
    :class:`~zoneinfo.ZoneInfo` itself, so this stays cheap on every call.
    """
    prop = device.properties.get(_TIMEZONE_PROPERTY) if device is not None else None
    tz_id = prop.value if prop is not None else None
    if not isinstance(tz_id, str) or not tz_id:
        return UTC
    try:
        return ZoneInfo(tz_id)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        _LOGGER.debug(
            "Device %s reports unusable timezone %s; scheduling against UTC",
            device_slug,
            tz_id,
        )
        return UTC


# ---------------------------------------------------------------------------
# The scheduler
# ---------------------------------------------------------------------------


class AquaHomeRegenScheduler(DataUpdateCoordinator[AutomationState]):
    """Own one device's automation state and act on the analytics verdicts.

    A coordinator without a poll cycle: its data is the device's
    :class:`~.automation_state.AutomationState`, published whenever a flag, a
    deferral or a decision changes, and consumed by the three automation
    switches. Every mutation goes through :meth:`_async_apply`, which persists
    the user-set subset into ``entry.options`` and then publishes — so a
    restart never forgets an opt-in and the switches never show a state that
    was not written.

    The two evaluators are serialised by one lock: an engine pass that starts a
    deferral and a telemetry poll that enforces one must not interleave, and
    the public setters take the same lock so a service call landing mid-pass
    cannot tear the state. The public setters delegate to unlocked internals
    the evaluators reuse, so the follower path never re-enters the lock.
    """

    def __init__(  # noqa: PLR0913 - deliberate dependency-injection signature
        self,
        hass: HomeAssistant,
        entry: AquaHomeConfigEntry,
        *,
        device_id: str,
        device_slug: str,
        client: AquaHomeClient,
        fast: AquaHomeCoordinator,
        settings: AquaHomeSettingsCoordinator,
        engine: AquaHomeAnalyticsEngine,
    ) -> None:
        """Bind the scheduler to one device and its sibling coordinators.

        The fast coordinator supplies the live device view (capacity, recharge
        state, online signal, timezone), the engine the daily verdicts, and the
        client the command channel. The settings coordinator is held for the
        automation tier's settings-writing repair flow, which resolves it
        through the scheduler rather than re-deriving the device mapping.
        """
        self.device_id = device_id
        self.device_slug = device_slug
        self.client = client
        self.fast = fast
        self.settings = settings
        self.engine = engine
        self._entry = entry
        self._state = state_from_options(entry, device_id)
        self._lock = asyncio.Lock()
        #: Device-local day a regeneration was last scheduled on (once-a-day latch).
        self._scheduled_day: date | None = None
        #: Device-local day the cancel budget below is counted for.
        self._cancel_day: date | None = None
        self._cancels_today = 0
        #: Refused cancel attempts today. Bounded separately from the sent
        #: cancels so a cloud flake neither eats a success slot nor lets the
        #: enforcement retry a refusing cloud on every ten-minute poll.
        self._cancel_failures_today = 0
        self._budget_logged = False
        #: Whether the current deferral already announced that it ran past the
        #: resin-hygiene cap. Reset when a new deferral starts.
        self._expiry_fired = False
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {device_slug} automation",
            update_interval=None,
        )

        @callback
        def _keep_alive() -> None:
            """Keep the coordinator serviceable with zero entity listeners."""

        # The three automation switches may all be disabled by the user, yet
        # the scheduler must keep evaluating (commands, events, repairs), so it
        # holds an inert listener exactly like the analytics engine does.
        self.async_add_listener(_keep_alive)

    @property
    def state(self) -> AutomationState:
        """Return the device's current automation state.

        The seeded state until :meth:`async_start` publishes it, the published
        data afterwards — the two are the same object in every state the
        outside world can observe.
        """
        data: AutomationState | None = self.data
        return self._state if data is None else data

    async def _async_update_data(self) -> AutomationState:
        """Return the current state.

        The scheduler fetches nothing: its data is local, so a refresh is a
        republish and can never fail.
        """
        return self.state

    async def async_start(self) -> None:
        """Publish the seeded state and subscribe to the two data sources.

        Both listeners are plain synchronous callbacks that hand the actual
        evaluation to a task — coordinator listeners must not block, and the
        evaluators await both the config-entry write and (at most) one cloud
        command. Both subscriptions are released with the config entry.
        """

        @callback
        def _handle_engine_update() -> None:
            """Re-evaluate the automation rules on a fresh analytics verdict."""
            self.hass.async_create_task(
                self._async_evaluate_engine(),
                name=f"{DOMAIN} {self.device_slug} automation engine pass",
            )

        @callback
        def _handle_fast_update() -> None:
            """Enforce an active deferral against the fresh device view."""
            self.hass.async_create_task(
                self._async_evaluate_fast(),
                name=f"{DOMAIN} {self.device_slug} automation deferral pass",
            )

        self._entry.async_on_unload(
            self.engine.async_add_listener(_handle_engine_update)
        )
        self._entry.async_on_unload(self.fast.async_add_listener(_handle_fast_update))
        self.async_set_updated_data(self._state)

    # -- public API -------------------------------------------------------

    async def async_set_vacation_deferral(self, active: bool, *, source: str) -> None:
        """Start or end the vacation deferral on behalf of ``source``.

        Starting one cancels a regeneration the device has already scheduled
        (that first cancel is free of the daily budget — it is the deferral
        taking effect, not a fight with the device); ending one schedules a
        catch-up recharge when the household comes back to a nearly exhausted
        resin bed. ``source`` records who asked, which decides whether the
        deferral may release itself again (see :meth:`_async_follow_vacation`).
        Setting the flag to the value it already has does nothing at all.
        """
        async with self._lock:
            await self._async_apply_deferral(active, source=source)

    async def async_set_auto_vacation(self, enabled: bool) -> None:
        """Enable or disable following the vacation detector automatically.

        Enabling it while the household is *already* detected away applies the
        deferral immediately, so the user does not have to wait for the next
        nightly verdict to see the switch they just flipped take effect.
        """
        async with self._lock:
            await self._async_apply(replace(self.state, auto_vacation=enabled))
            if not enabled:
                return
            result: AnalyticsResult | None = self.engine.data
            if (
                result is not None
                and result.vacation.active is True
                and not self.state.vacation_deferral
            ):
                await self._async_apply_deferral(True, source=DEFERRAL_SOURCE_AUTO)

    async def async_set_smart_regeneration(self, enabled: bool) -> None:
        """Enable or disable the nightly capacity-versus-forecast scheduler.

        Persists the flag and nothing else: the next engine pass acts on it,
        and the repair issues it gates clean themselves up through their own
        listeners on this coordinator.
        """
        async with self._lock:
            await self._async_apply(replace(self.state, smart_regeneration=enabled))

    # -- evaluators -------------------------------------------------------

    async def _async_evaluate_engine(self) -> None:
        """Run one automation pass over a fresh analytics result."""
        async with self._lock:
            result: AnalyticsResult | None = self.engine.data
            await self._async_follow_vacation(result)
            await self._async_decide(result)

    async def _async_evaluate_fast(self) -> None:
        """Enforce an active deferral against the freshest device view."""
        async with self._lock:
            await self._async_enforce_deferral()

    async def _async_follow_vacation(self, result: AnalyticsResult | None) -> None:
        """Mirror the vacation detector into the deferral flag when asked to.

        Arming is gated on the ``auto_vacation`` opt-in. Releasing is gated on
        the deferral's *source* instead: an ``auto`` deferral is one the system
        started on the household's behalf — by this follower or by a confirmed
        vacation-defer suggestion, which promises it resumes when water use
        returns — so it releases whatever the follower flag says now. A
        deferral the user (or a blueprint) started manually stays theirs to
        end. A verdict of ``None`` — nothing to assess — moves nothing in
        either direction, the same silence rule the detection tier follows.
        """
        state = self.state
        if result is None:
            return
        detected = result.vacation.active
        if detected is True and state.auto_vacation and not state.vacation_deferral:
            await self._async_apply_deferral(True, source=DEFERRAL_SOURCE_AUTO)
        elif (
            detected is False
            and state.vacation_deferral
            and state.deferral_source == DEFERRAL_SOURCE_AUTO
        ):
            await self._async_apply_deferral(False, source=DEFERRAL_SOURCE_AUTO)

    async def _async_decide(self, result: AnalyticsResult | None) -> None:
        """Take (or honestly skip) tonight's smart-regeneration decision."""
        now = dt_util.utcnow()
        device: Device | None = self.fast.data
        blocked = self._blocking_reason(device, result, now)
        if blocked is not None:
            await self._async_record(blocked, now)
            return
        capacity = _capacity_gallons(device)
        forecast = _forecast_gallons(result)
        if not _capacity_low(capacity, forecast):
            await self._async_record(DECISION_NOT_NEEDED, now)
            return
        await self._async_schedule(capacity, forecast, REGEN_REASON_LOW_CAPACITY, now)

    def _blocking_reason(
        self, device: Device | None, result: AnalyticsResult | None, now: datetime
    ) -> str | None:
        """Return the first unmet scheduling precondition, or ``None``.

        The conditions are listed in the order they are reported, cheapest and
        most user-visible first: the opt-in itself, the states that mean "not
        now" (deferral, detected vacation, an offline or unwilling device),
        then the data the decision needs, and finally the once-a-day latch. All
        of them are pure payload reads, so evaluating them together costs
        nothing and keeps the reporting order in one readable place.
        """
        state = self.state
        checks: tuple[tuple[bool, str], ...] = (
            (not state.smart_regeneration, DECISION_SKIPPED_OFF),
            (state.vacation_deferral, DECISION_SKIPPED_DEFERRAL),
            (
                result is not None and result.vacation.active is True,
                DECISION_SKIPPED_VACATION,
            ),
            (not self.fast.device_online, DECISION_SKIPPED_OFFLINE),
            (_can_schedule(device) is False, DECISION_SKIPPED_NOT_ALLOWED),
            (not _recharge_ready(device), DECISION_SKIPPED_NOT_READY),
            (_forecast_gallons(result) is None, DECISION_SKIPPED_NO_FORECAST),
            (_capacity_gallons(device) is None, DECISION_SKIPPED_NO_CAPACITY),
            (
                self._scheduled_day == self._local_date(now),
                DECISION_SKIPPED_ALREADY_TODAY,
            ),
        )
        for failed, reason in checks:
            if failed:
                return reason
        return None

    async def _async_enforce_deferral(self) -> None:
        """Cancel the device's scheduled regeneration while the deferral holds.

        Nothing to do unless a deferral is active *and* the device actually has
        a regeneration scheduled. Past the resin-hygiene cap the scheduled
        recharge is deliberately let through — one announcement per deferral,
        never a repeated nag — and within it the cancels are capped per
        device-local day.
        """
        state = self.state
        device: Device | None = self.fast.data
        if not state.vacation_deferral or not _recharge_scheduled(device):
            return
        now = dt_util.utcnow()
        age = _deferral_age(state, now)
        if age is not None and age > timedelta(days=REGEN_DEFERRAL_MAX_DAYS):
            if not self._expiry_fired:
                self._expiry_fired = True
                self._fire_event(
                    EVENT_TYPE_REGEN_DEFERRAL_EXPIRED,
                    {
                        "deferral_source": state.deferral_source,
                        "days_deferred": age.days,
                    },
                )
                await self._async_record(DECISION_DEFERRAL_EXPIRED, now)
            return
        if not self._claim_cancel_budget(now):
            return
        if not await self._async_command(_ACTION_CANCEL):
            self._cancel_failures_today += 1
            await self._async_record(DECISION_SKIPPED_COMMAND_FAILED, now)
            return
        self._cancels_today += 1
        # A sent cancel undoes the day's scheduled regeneration, so the
        # once-per-day schedule latch must re-open for a same-day catch-up.
        self._scheduled_day = None
        self._fire_event(
            EVENT_TYPE_REGEN_DEFERRED, {"deferral_source": state.deferral_source}
        )
        await self._async_record(DECISION_DEFERRED, now)

    def _claim_cancel_budget(self, now: datetime) -> bool:
        """Return whether another deferral cancel fits in today's budget.

        The budget is counted per device-local day and rolls over on its own;
        exhaustion is logged once per day, because the condition repeats on
        every telemetry poll until the device stops re-scheduling.
        """
        day = self._local_date(now)
        if self._cancel_day != day:
            self._cancel_day = day
            self._cancels_today = 0
            self._cancel_failures_today = 0
            self._budget_logged = False
        if (
            self._cancels_today < REGEN_CANCEL_DAILY_BUDGET
            and self._cancel_failures_today < REGEN_CANCEL_DAILY_BUDGET
        ):
            return True
        if not self._budget_logged:
            self._budget_logged = True
            _LOGGER.debug(
                "Deferral cancel budget for %s exhausted for %s; letting the device "
                "keep its scheduled regeneration",
                self.device_slug,
                day,
            )
        return False

    # -- state transitions ------------------------------------------------

    async def _async_apply_deferral(self, active: bool, *, source: str) -> None:
        """Apply a deferral transition and its side effect (lock already held)."""
        state = self.state
        if state.vacation_deferral == active:
            return
        now = dt_util.utcnow()
        if active:
            self._expiry_fired = False
            await self._async_apply(
                replace(
                    state,
                    vacation_deferral=True,
                    deferral_source=source,
                    deferral_started=now,
                ).with_decision(DECISION_DEFERRED, now)
            )
            await self._async_cancel_scheduled(source)
            return
        await self._async_apply(
            replace(
                state,
                vacation_deferral=False,
                deferral_source=None,
                deferral_started=None,
            )
        )
        await self._async_catch_up(now)

    async def _async_cancel_scheduled(self, source: str) -> None:
        """Cancel a regeneration already scheduled when a deferral starts.

        Deliberately free of the daily cancel budget: this is the deferral
        taking effect once, not the recurring enforcement the budget caps.
        """
        if not _recharge_scheduled(self.fast.data):
            return
        if await self._async_command(_ACTION_CANCEL):
            self._scheduled_day = None
            self._fire_event(EVENT_TYPE_REGEN_DEFERRED, {"deferral_source": source})

    async def _async_catch_up(self, now: datetime) -> None:
        """Schedule a make-up recharge when a deferral ends on low capacity.

        The device only regenerates on its own schedule, so a household coming
        home to a nearly exhausted resin bed would otherwise draw hard water
        until the next scheduled recharge. Nothing is sent when the capacity
        still covers the forecast, when the device refuses scheduling, or when
        it is not in a state that can accept one.
        """
        device: Device | None = self.fast.data
        capacity = _capacity_gallons(device)
        forecast = _forecast_gallons(self.engine.data)
        if not _capacity_low(capacity, forecast):
            return
        if _can_schedule(device) is False or not _recharge_ready(device):
            return
        # The same once-per-device-local-day bound the nightly path honours:
        # the device view only refreshes on the poll, so repeated deferral OFF
        # transitions inside one window would otherwise re-send the command.
        # A sent cancel re-opens the latch, so a genuine cancel-then-return
        # day still gets its catch-up.
        if self._scheduled_day == self._local_date(now):
            return
        await self._async_schedule(capacity, forecast, REGEN_REASON_CATCH_UP, now)

    async def _async_schedule(
        self,
        capacity: float | None,
        forecast: float | None,
        reason: str,
        now: datetime,
    ) -> None:
        """Send the schedule command, then announce and record the outcome.

        The once-a-day latch is set only after the cloud accepted the command,
        so a rejected attempt can be retried on the next pass.
        """
        if not await self._async_command(_ACTION_SCHEDULE):
            await self._async_record(DECISION_SKIPPED_COMMAND_FAILED, now)
            return
        self._scheduled_day = self._local_date(now)
        self._fire_event(
            EVENT_TYPE_REGEN_SCHEDULED,
            {
                "reason": reason,
                "capacity_gallons": capacity,
                "forecast_gallons": forecast,
            },
        )
        await self._async_record(_DECISION_FOR_REASON[reason], now)

    async def _async_record(self, decision: str, now: datetime) -> None:
        """Publish ``decision`` as the scheduler's latest verdict."""
        await self._async_apply(self.state.with_decision(decision, now))

    async def _async_apply(self, state: AutomationState) -> None:
        """Persist and publish one automation-state mutation.

        The single write path: the user-set subset goes into ``entry.options``
        (no update listener is registered for this entry, so writing options
        cannot trigger a reload storm) and the full state — including the
        runtime-only decision fields — is published to the switches.
        """
        self._state = state
        self.hass.config_entries.async_update_entry(
            self._entry,
            options=options_with_state(self._entry, self.device_id, state),
        )
        self.async_set_updated_data(state)

    # -- device dialogue --------------------------------------------------

    async def _async_command(self, action: str) -> bool:
        """Send one regeneration command, reporting whether the cloud took it.

        An automation must never surface a command failure as an exception:
        nobody is standing in front of the device at 07:35, so the failure is
        logged, the caller records it as a skipped decision, and the next pass
        simply tries again.
        """
        try:
            await async_execute_command(
                self.client, self.device_id, _COMMAND_FUNCTION, action
            )
        except HomeAssistantError as err:
            _LOGGER.warning(
                "AquaHome %s could not send the regeneration %s command: %s",
                self.device_slug,
                action,
                err,
            )
            return False
        return True

    @callback
    def _fire_event(self, event_type: str, detail: dict[str, object]) -> None:
        """Fire one automation-tier event on the Home Assistant bus."""
        self.hass.bus.async_fire(
            EVENT_AQUAHOME,
            {
                "device_id": self.device_id,
                "device": self.device_slug,
                "type": event_type,
                **detail,
            },
        )

    def _local_date(self, now: datetime) -> date:
        """Return the device-local calendar day ``now`` falls on."""
        return now.astimezone(_device_timezone(self.fast.data, self.device_slug)).date()
