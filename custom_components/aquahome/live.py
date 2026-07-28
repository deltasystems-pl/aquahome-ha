"""Live mode: one on-demand websocket session per device, and everything into it.

The iQua cloud offers a per-device websocket that pushes property updates as
they happen. It is a scarce resource rather than a second poll: a session needs
a ticket from ``GET /devices/{id}/live``, and that endpoint runs its own token
bucket — 6 tickets per 600 s with a burst of 60, refilling roughly one token
every 100 s — entirely separate from (and far smaller than) the REST budget the
primary poll spends. Everything in this module exists to make live coverage
useful while staying comfortably inside that budget.

One :class:`AquaHomeLiveManager` per device therefore owns the *single*
websocket lifecycle and every path into it. The manual Live-view hold, the
continuous mode, the analytics-driven peak-hour windows, the event bursts (a
starting regeneration, a confirmed usage anomaly) and poll-detected active water
use all funnel through one ordered grant gate, so no combination of triggers can
open two sockets, exceed the per-day grant budget, or ignore the minimum gap
between sessions. Every denial is recorded as one of the ``denied_*`` literals
below, which is the difference between a budget the owner can reason about and
one they have to guess at.

A granted session is short by nature. The device fast-reports for about three
minutes from connect and then falls silent with the socket still open; roughly
five minutes in it publishes ``app_active=false``, which signals the reporting
window closing, not a disconnect. A hold therefore *renews* — close, fresh
ticket, reconnect — window after window, which re-arms fast reporting
immediately at a cost of one ticket per five minutes, well inside the refill
rate. A smart window holds the same way for as long as its learned peak hour
lasts — roughly twelve tickets an hour against a bucket that refills about
thirty-six — because per-gallon, timestamped usage events are the one thing
this API yields nowhere else (its history is hourly forever), and a household's
water moves in those peaks. Without a hold the session simply ends.

Live data upgrades the *existing* entities: streamed frames are merged into the
polling coordinator's device view, so live mode adds no telemetry entities of
its own and a device that never streams behaves exactly as before. Two rules
keep that merge cheap. Frames are applied coalesced, because the connect
snapshot and a running tap both arrive as bursts and each apply re-renders every
bound entity. And the two housekeeping properties never reach entity state:
``current_time_secs`` ticks every ten seconds purely as a liveness signal and
``app_active`` reports the window, so pushing either would rewrite entity state
every few seconds for no user-visible change.

Failures are never fatal. Polling continues untouched, reconnects back off from
one minute to thirty, and only a long run of failures against a device that
reports itself online raises a repair issue — which the next successful session
withdraws. The manager sends no commands, triggers no analytics refresh, touches
neither the recorder nor the event bus, and writes no logbook entries: a live
session costs exactly one ticket and one socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_point_in_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import (
    IQUA2_BASE_URL,
    AquaHomeConnectionError,
    LiveTicketExpiredError,
    PropertyValue,
    RateLimitError,
    scaled_value,
)
from .api.websocket import AquaHomeLiveSession, live_websocket_url
from .const import (
    DOMAIN,
    LIVE_ACTIVE_USE_COOLDOWN_SECONDS,
    LIVE_ACTIVE_USE_DELTA_GALLONS,
    LIVE_BACKOFF_INITIAL_SECONDS,
    LIVE_BACKOFF_MAX_SECONDS,
    LIVE_COALESCE_SECONDS,
    LIVE_FAILURES_FOR_ISSUE,
    LIVE_IQUA2_WINDOW_SECONDS,
    LIVE_PUSHED_PROPERTIES,
    LIVE_RENEWAL_MIN_SECONDS,
    LIVE_SMART_NO_FLOW_SUSPEND,
    LIVE_SOURCE_ACTIVE_USE,
    LIVE_SOURCE_ANOMALY,
    LIVE_SOURCE_CONTINUOUS,
    LIVE_SOURCE_MANUAL,
    LIVE_SOURCE_REGEN,
    LIVE_SOURCE_SMART,
    LIVE_STATUS_BACKOFF,
    LIVE_STATUS_IDLE,
    LIVE_STATUS_LIVE,
    LIVE_SUBSCRIBED_PROPERTIES,
    LIVE_VIEW_HOLD_MAX_SECONDS,
    LIVE_WINDOW_FALLBACK_SECONDS,
    LIVE_WINDOW_GRACE_SECONDS,
)
from .entity import device_display_name
from .live_state import (
    LiveConfig,
    LiveState,
    clamp_min_gap,
    clamp_sessions_per_day,
    config_from_options,
    options_with_config,
)

if TYPE_CHECKING:
    from datetime import date, datetime, tzinfo

    from homeassistant.core import HomeAssistant

    from .analytics.engine import AquaHomeAnalyticsEngine
    from .analytics.model import AnalyticsResult, GridSummary
    from .api import AquaHomeClient, Device
    from .api.websocket import LiveFrame
    from .coordinator import AquaHomeConfigEntry, AquaHomeCoordinator

_LOGGER = logging.getLogger(__name__)

# Deny reasons of the grant gate, listed in the exact order they are checked so
# the first unmet condition is always the one reported. They are the manager's
# whole observability surface for a trigger that did not open a session, and
# they are logged at debug level only — a denied trigger is normal operation.
DENIED_ACTIVE: Final = "denied_active"
DENIED_BACKOFF: Final = "denied_backoff"
DENIED_REST_BACKOFF: Final = "denied_rest_backoff"
DENIED_OFFLINE: Final = "denied_offline"
DENIED_BUDGET: Final = "denied_budget"
DENIED_GAP: Final = "denied_gap"
DENIED_COOLDOWN: Final = "denied_cooldown"
DENIED_SUSPENDED: Final = "denied_suspended"
DENIED_NIGHT: Final = "denied_night"

#: Prefix of the repair issue raised when live sessions keep failing.
LIVE_FAILING_ISSUE_PREFIX: Final = "live_mode_failing_"

# Why the current reporting window ended — the three cases the renewal decision
# distinguishes.
_WINDOW_TIMER: Final = "timer"
_WINDOW_APP_INACTIVE: Final = "app_inactive"
_WINDOW_STREAM_END: Final = "stream_end"

#: Streamed window signal: ``False`` means the fast-reporting window is over.
_APP_ACTIVE_PROPERTY: Final = "app_active"
#: Raw property advertising the reporting-window length, in minutes.
_APP_ACTIVE_TIMEOUT_PROPERTY: Final = "app_active_timeout"
#: Streamed liveness heartbeat; ticks every ~10 s and carries no user value.
_CLOCK_PROPERTY: Final = "current_time_secs"
#: Raw today-usage counter driving the poll-detected active-use trigger.
_USAGE_PROPERTY: Final = "gallons_used_today"
#: Raw property carrying the device's own IANA timezone.
_TIMEZONE_PROPERTY: Final = "tz_id"

#: Pushed properties whose movement proves water actually flowed. A smart
#: window that sees none of them move was a window spent on a quiet house.
_COUNTER_PROPERTIES: Final = frozenset(
    {
        "total_outlet_water_gals",
        "water_counter_gals",
        "gallons_used_today",
        "treated_water_avail_gals",
    }
)

#: ``recharge_ui`` / ``regeneration`` value meaning a recharge is running.
_REGENERATING: Final = "regenerating"

# Device-local hours smart windows never open in: a scheduled session between
# 01:00 and 07:00 would stream a sleeping household. Event bursts and
# poll-detected active use are deliberately night-allowed — an unexpected night
# flow is exactly the evidence a leak investigation needs.
_NIGHT_START_HOUR: Final = 1
_NIGHT_END_HOUR: Final = 7

_HOURS_PER_DAY: Final = 24
_WEEKDAYS: Final = 7
#: Length of the learned activity grid (hour of week).
_GRID_HOURS: Final = _WEEKDAYS * _HOURS_PER_DAY
_SECONDS_PER_MINUTE: Final = 60.0

#: Backoff doublings past which the maximum is reached anyway; bounding the
#: exponent keeps a long outage from overflowing the multiplication.
_BACKOFF_EXPONENT_CAP: Final = 16


# ---------------------------------------------------------------------------
# None-safe payload accessors
#
# Local copies rather than imports: the manager reads three small values out of
# the device payload and must not depend on an entity platform or the setup
# module to do it (the same replication rule the automation tier follows).
# ---------------------------------------------------------------------------


def _regen_active(device: Device | None) -> bool | None:
    """Return whether a regeneration is running now, ``None`` when unknown.

    ``True`` when the ``recharge_ui`` tile reads ``regenerating`` or the enriched
    ``regeneration`` block reports ``regeneration_status == "regenerating"``;
    ``None`` only when neither source is present, so there is nothing to compare
    a transition against.
    """
    enriched = device.enriched_data if device is not None else None
    if enriched is None:
        return None
    recharge_ui = enriched.recharge_ui
    regeneration = enriched.regeneration
    if recharge_ui is None and regeneration is None:
        return None
    if recharge_ui is not None and recharge_ui.state == _REGENERATING:
        return True
    return (
        regeneration is not None and regeneration.regeneration_status == _REGENERATING
    )


def _gallons_used_today(device: Device | None) -> float | None:
    """Return today's water usage in gallons from the raw counter, or ``None``.

    Read from the raw property rather than an enriched tile: the trigger
    compares two consecutive polls, so it needs the freshest of the two sources.
    """
    if device is None:
        return None
    prop = device.properties.get(_USAGE_PROPERTY)
    return scaled_value(prop) if prop is not None else None


def _window_timeout_minutes(device: Device | None) -> float | None:
    """Return the reporting-window length the device advertises, in minutes.

    ``None`` when the device does not report it (or reports a nonsensical zero),
    which leaves the caller with its own fallback rather than a window that ends
    the instant it opens.
    """
    if device is None:
        return None
    prop = device.properties.get(_APP_ACTIVE_TIMEOUT_PROPERTY)
    minutes = scaled_value(prop) if prop is not None else None
    return minutes if minutes is not None and minutes > 0 else None


def _device_timezone(device: Device | None, device_slug: str) -> tzinfo | None:
    """Return the zone the device dates its own days in, or ``None``.

    The daily grant budget, the smart-window hours and the night rule are all
    keyed to the *device-local* day, which is the day the softener itself works
    in. ``None`` when the device reports no (or an unusable) ``tz_id`` — the
    caller decides the fallback. Zone lookups are cached by
    :class:`~zoneinfo.ZoneInfo` itself, so this stays cheap on every call.
    """
    prop = device.properties.get(_TIMEZONE_PROPERTY) if device is not None else None
    tz_id = prop.value if prop is not None else None
    if not isinstance(tz_id, str) or not tz_id:
        return None
    try:
        return ZoneInfo(tz_id)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        _LOGGER.debug(
            "Device %s reports unusable timezone %s; falling back to the "
            "installation's zone",
            device_slug,
            tz_id,
        )
        return None


# ---------------------------------------------------------------------------
# The live-mode manager
# ---------------------------------------------------------------------------


class AquaHomeLiveManager(DataUpdateCoordinator[LiveState]):
    """Own one device's live-mode state and its single websocket lifecycle.

    A coordinator without a poll cycle: its data is the device's
    :class:`~.live_state.LiveState`, published whenever a configuration flag, a
    session or a failure changes, and consumed by the live switches, numbers and
    the status sensor. Exactly one session runs at a time — the background task
    holding it is the only place that opens a socket — and exactly one method
    (:meth:`_publish`) writes state, so the entities can never show a session
    that is not running.

    Trigger evaluation is synchronous by construction: the grant gate and the
    task hand-off run without an intervening ``await``, so two triggers landing
    in the same event-loop pass cannot both be granted, and no lock is needed.
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
        engine: AquaHomeAnalyticsEngine,
    ) -> None:
        """Bind the manager to one device and the data sources it reacts to.

        The fast coordinator supplies the device view live frames are merged
        into plus the online signal and the device timezone, the analytics
        engine the anomaly verdict and the learned activity grid, and the client
        the ticket endpoint (whose base URL also decides the host-specific
        session semantics).
        """
        self.device_id = device_id
        self.device_slug = device_slug
        self.client = client
        self.fast = fast
        self.engine = engine
        self._entry = entry
        self._state = LiveState(config=config_from_options(entry, device_id))
        #: The newer API host keeps a session open for about an hour and asks
        #: for a reconnect with ``app_active=false`` instead of closing it.
        self._iqua2 = client.base_url == IQUA2_BASE_URL
        self._session: AquaHomeLiveSession | None = None
        self._session_task: asyncio.Task[None] | None = None
        #: Set once shutdown begins. The fast/engine listeners stay subscribed
        #: until the config entry releases them — after every consumer's own
        #: shutdown — so an update landing mid-unload could otherwise pass the
        #: grant gate and open a fresh ticketed session against a manager that
        #: is already down.
        self._stopping = False
        self._window_reason = _WINDOW_STREAM_END
        #: Whether the current reporting window delivered at least one frame —
        #: the evidence that clears the failure trail and permits a renewal.
        self._window_saw_frames = False
        #: Reporting-window length last seen on the stream, in minutes.
        self._timeout_minutes: float | None = None
        #: Frames waiting for the next coalesced apply, newest per property.
        self._pending: dict[str, LiveFrame] = {}
        self._unsub_window: CALLBACK_TYPE | None = None
        self._unsub_coalesce: CALLBACK_TYPE | None = None
        self._unsub_backoff: CALLBACK_TYPE | None = None
        self._unsub_view_cap: CALLBACK_TYPE | None = None
        self._unsub_smart: CALLBACK_TYPE | None = None
        #: Device-local day the daily counters below are kept for.
        self._day: date | None = None
        #: Today-usage counter seen on the previous *fresh* poll.
        self._usage_baseline: float | None = None
        self._last_active_use: datetime | None = None
        #: Consecutive no-flow reporting windows across the tier's sessions
        #: today — the granularity the no-flow brake counts in.
        self._no_flow_windows = 0
        #: Whether the current reporting window streamed a counter movement.
        self._window_saw_flow = False
        self._regen_active: bool | None = None
        self._anomaly_active: bool | None = None
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {device_slug} live",
            update_interval=None,
        )

        @callback
        def _keep_alive() -> None:
            """Keep the coordinator serviceable with zero entity listeners."""

        # The live entities may all be disabled by the user, yet the manager
        # must keep evaluating its triggers, so it holds an inert listener
        # exactly like the analytics engine and the scheduler do.
        self.async_add_listener(_keep_alive)

    @property
    def state(self) -> LiveState:
        """Return the device's current live-mode state.

        The seeded state until :meth:`async_start` publishes it, the published
        data afterwards — the two are the same object in every state the outside
        world can observe.
        """
        data: LiveState | None = self.data
        return self._state if data is None else data

    async def _async_update_data(self) -> LiveState:
        """Return the current state.

        The manager fetches nothing on refresh: its data is local, so a refresh
        is a republish and can never fail.
        """
        return self.state

    # -- public API -------------------------------------------------------

    async def async_start(self) -> None:
        """Publish the seeded state and subscribe to the two data sources.

        Both listeners are plain synchronous callbacks that hand the evaluation
        to a task, because coordinator listeners must not block. The transition
        detectors are seeded from whatever the sources already hold, so a fresh
        setup never mistakes its first observation for a change. Both
        subscriptions are released with the config entry.
        """

        @callback
        def _handle_fast_update() -> None:
            """Re-run the poll-driven triggers against a fresh device view.

            A live push is not a fresh view: it carries the polled payload
            verbatim apart from the handful of raw properties this manager
            streamed into it, so reacting to one would advance the active-use
            baseline from the stream — hiding the very rise the next genuine
            poll must trigger on — and re-run the hold resume at streaming
            cadence. The push flag is read synchronously here, exactly as the
            scheduler and the capability debounce read it.
            """
            if self.fast.updating_from_push:
                return
            self.hass.async_create_task(
                self._async_evaluate_fast(),
                name=f"{DOMAIN} {self.device_slug} live poll pass",
            )

        @callback
        def _handle_engine_update() -> None:
            """Re-run the analytics-driven triggers on a fresh verdict."""
            self.hass.async_create_task(
                self._async_evaluate_engine(),
                name=f"{DOMAIN} {self.device_slug} live analytics pass",
            )

        # Seeding the day here is what makes the seeded usage baseline survive
        # the first evaluator pass: an unseeded day would read as a rollover and
        # clear the very baseline the first poll has to be compared against.
        self._day = self._local_date(dt_util.utcnow())
        self._regen_active = _regen_active(self.fast.data)
        self._usage_baseline = _gallons_used_today(self.fast.data)
        result: AnalyticsResult | None = self.engine.data
        self._anomaly_active = result.anomaly.active if result is not None else None
        self._entry.async_on_unload(self.fast.async_add_listener(_handle_fast_update))
        self._entry.async_on_unload(
            self.engine.async_add_listener(_handle_engine_update)
        )
        self.async_set_updated_data(self._state)

    async def async_shutdown(self) -> None:
        """Cancel every timer, close the session, then shut the coordinator down.

        Ordered so nothing can re-arm behind the teardown: the stop flag goes
        up first (the data-source listeners outlive this method and keep
        delivering updates until the config entry releases them), then the
        timers (a window or backoff timer firing mid-shutdown would try to
        reconnect), then the session task is cancelled and its socket closed,
        and only then does the base coordinator stand down.
        """
        self._stopping = True
        self._cancel_timers()
        await self._async_stop_session(publish_idle=False)
        await super().async_shutdown()

    async def async_set_live_view(self, on: bool) -> None:
        """Start or end the manual live hold. Idempotent.

        Switching it on requests a session and holds it open across window
        renewals, up to the auto-off cap that protects the cloud budget from a
        forgotten switch; switching it off ends the hold, and with it the
        session, at once. A request the grant gate refuses (the device is
        offline, the budget is spent, another session is already streaming)
        leaves the flag on: the hold is what the user asked for, and the next
        poll retries it as soon as the gate opens.
        """
        if self.state.live_view == on:
            return
        self._cancel_view_cap()
        self._publish(replace(self.state, live_view=on))
        if not on:
            await self._async_release_hold()
            return
        self._unsub_view_cap = async_call_later(
            self.hass, LIVE_VIEW_HOLD_MAX_SECONDS, self._handle_view_cap
        )
        self._request(LIVE_SOURCE_MANUAL)

    async def async_set_smart_windows(self, on: bool) -> None:
        """Enable or disable the analytics-driven peak-hour windows.

        Persists the flag and re-arms (or drops) the pending window
        immediately, so the switch takes effect without waiting for the next
        analytics pass. Switching it off also ends a peak-hour session that is
        streaming right now — the flag is the tier's off switch, and a
        full-block hold would otherwise keep the socket (and its per-window
        ticket spend) until the peak hour ran out. A manual or continuous hold
        that wants the same socket keeps it. Switching it on only arms the next
        window: a session is never opened mid-hour.
        """
        config = self.state.config
        if config.smart_windows == on:
            return
        self._apply_config(replace(config, smart_windows=on))
        self._arm_smart_window(dt_util.utcnow())
        if not on:
            await self._async_release_smart_hold()

    async def async_set_continuous(self, on: bool) -> None:
        """Enable or disable the continuous live hold.

        The advanced mode: an unlimited hold that renews window after window for
        as long as it is on. Enabling it requests a session at once; disabling
        it ends the hold, and with it the session, at once.
        """
        config = self.state.config
        if config.continuous == on:
            return
        self._apply_config(replace(config, continuous=on))
        if on:
            self._request(LIVE_SOURCE_CONTINUOUS)
            return
        await self._async_release_hold()

    async def async_set_sessions_per_day(self, value: int) -> None:
        """Set the daily grant budget, clamped to the supported range."""
        config = self.state.config
        clamped = clamp_sessions_per_day(value)
        if config.sessions_per_day != clamped:
            self._apply_config(replace(config, sessions_per_day=clamped))

    async def async_set_min_gap(self, value: float) -> None:
        """Set the minimum gap between grants in seconds, clamped to range."""
        config = self.state.config
        clamped = clamp_min_gap(value)
        if config.min_gap_seconds != clamped:
            self._apply_config(replace(config, min_gap_seconds=clamped))

    # -- trigger evaluators ------------------------------------------------

    async def _async_evaluate_fast(self) -> None:
        """Re-derive the poll-driven triggers from a fresh device view."""
        if self._stopping:
            return
        now = dt_util.utcnow()
        self._roll_day(now)
        device: Device | None = self.fast.data
        self._evaluate_regen(device)
        self._evaluate_active_use(device)
        self._resume_hold()

    async def _async_evaluate_engine(self) -> None:
        """Re-derive the analytics-driven triggers from a fresh verdict."""
        if self._stopping:
            return
        now = dt_util.utcnow()
        self._roll_day(now)
        result: AnalyticsResult | None = self.engine.data
        if result is not None:
            active = result.anomaly.active
            previous = self._anomaly_active
            self._anomaly_active = active
            if active is True and previous is not True:
                self._request(LIVE_SOURCE_ANOMALY)
        self._arm_smart_window(now)

    @callback
    def _evaluate_regen(self, device: Device | None) -> None:
        """Open a burst when a regeneration starts.

        Only the ``False`` -> ``True`` transition counts: a regeneration already
        running when the integration starts is not news, and a device that does
        not report the state at all (``None``) is never compared.
        """
        active = _regen_active(device)
        previous = self._regen_active
        self._regen_active = active
        if active is True and previous is False:
            self._request(LIVE_SOURCE_REGEN)

    @callback
    def _evaluate_active_use(self, device: Device | None) -> None:
        """Open a burst when the polled today-counter jumps.

        The comparison is only meaningful between two consecutive *fresh*
        polls, so a re-served stale payload is skipped entirely rather than
        diffed against itself. A counter that went backwards is a day rollover
        or a device reset, which re-seeds the baseline instead of counting as
        usage.
        """
        if self.fast.serving_stale:
            return
        gallons = _gallons_used_today(device)
        if gallons is None:
            return
        baseline = self._usage_baseline
        self._usage_baseline = gallons
        if baseline is None or gallons < baseline:
            return
        if gallons - baseline >= LIVE_ACTIVE_USE_DELTA_GALLONS:
            self._request(LIVE_SOURCE_ACTIVE_USE)

    @callback
    def _roll_day(self, now: datetime) -> None:
        """Reset the per-day budget and cooldowns when the device-local day turns."""
        day = self._local_date(now)
        if self._day == day:
            return
        self._day = day
        self._usage_baseline = None
        self._last_active_use = None
        self._no_flow_windows = 0
        state = self.state
        if state.sessions_today or state.smart_suspended_until is not None:
            self._publish(replace(state, sessions_today=0, smart_suspended_until=None))

    @callback
    def _arm_smart_window(self, now: datetime) -> None:
        """Arm the next analytics-driven window, replacing any pending one.

        Re-armed after every analytics pass because the learned activity grid
        moves: the window that was next an hour ago may no longer be an active
        hour. With the flag off, or without a usable grid, nothing is armed.
        """
        self._cancel_smart_window()
        if self._stopping or not self.state.config.smart_windows:
            return
        result: AnalyticsResult | None = self.engine.data
        if result is None:
            return
        run_at = self._next_smart_hour(result.grid, now)
        if run_at is None:
            return
        self._unsub_smart = async_track_point_in_time(
            self.hass, self._handle_smart_window, run_at
        )
        _LOGGER.debug(
            "Armed the next %s smart live window at %s", self.device_slug, run_at
        )

    def _next_smart_hour(self, grid: GridSummary, now: datetime) -> datetime | None:
        """Return the next device-local peak hour worth streaming, or ``None``.

        The first upcoming hour the learned grid ranks among that weekday's
        peak hours, skipping the night hours the smart tier never opens in.
        Peaks rather than the binary activity grid because on a real household
        that grid resolves to "awake from 07:00", which is no information at
        all: the peaks are where the water actually moves. A grid that does not
        carry all seven weekdays (the "not computed" default), or one with no
        peak hour at all, arms nothing rather than guessing an hour.

        The arithmetic is device-local wall-clock hour arithmetic, which is
        fold-tolerant but not DST-exact: on the two transition days a window
        armed across the change can land an hour off. Accepted — the cost is
        one mistimed window twice a year, against the complexity of resolving
        ambiguous local hours.
        """
        peak_hours = grid.peak_hours
        if len(peak_hours) != _WEEKDAYS:
            return None
        local = now.astimezone(self._timezone()).replace(
            minute=0, second=0, microsecond=0
        )
        for offset in range(1, _GRID_HOURS + 1):
            candidate = local + timedelta(hours=offset)
            hour = candidate.hour
            if _NIGHT_START_HOUR <= hour < _NIGHT_END_HOUR:
                continue
            if hour in peak_hours[candidate.weekday()]:
                return candidate
        return None

    def _in_peak_hour(self, now: datetime) -> bool:
        """Return whether ``now`` falls on a learned peak hour of the device.

        The predicate behind the full-block hold: the hour is read in the
        device's own zone, against the weekday that zone dates ``now`` to.
        ``False`` whenever the answer is not known — the engine has published
        no verdict yet, or the grid does not carry all seven weekdays — so an
        unknown grid can never hold a socket open.
        """
        result: AnalyticsResult | None = self.engine.data
        if result is None:
            return False
        peak_hours = result.grid.peak_hours
        if len(peak_hours) != _WEEKDAYS:
            return False
        local = now.astimezone(self._timezone())
        return local.hour in peak_hours[local.weekday()]

    @callback
    def _handle_smart_window(self, _now: datetime) -> None:
        """Request the due smart window and arm the one after it."""
        self._unsub_smart = None
        self._request(LIVE_SOURCE_SMART)
        self._arm_smart_window(dt_util.utcnow())

    @callback
    def _handle_view_cap(self, _now: datetime) -> None:
        """Auto-off the manual hold once it has run for its maximum."""
        self._unsub_view_cap = None
        if not self.state.live_view:
            return
        _LOGGER.debug(
            "Live view for %s reached its hold limit; switching it off",
            self.device_slug,
        )
        self.hass.async_create_task(
            self.async_set_live_view(False),
            name=f"{DOMAIN} {self.device_slug} live view auto-off",
        )

    # -- the grant gate ----------------------------------------------------

    @callback
    def _request(self, source: str) -> None:
        """Grant ``source`` a live session, or record why it was refused.

        Deliberately synchronous through to the task hand-off: nothing awaits
        between the gate and the session task taking ownership, so concurrent
        triggers cannot both pass. The stop flag is checked here too — the
        single choke point every trigger path funnels through — so a stray
        evaluator task or timer landing mid-unload cannot spend a ticket.
        """
        if self._stopping:
            return
        now = dt_util.utcnow()
        self._roll_day(now)
        denied = self._can_grant(source, now)
        if denied is not None:
            _LOGGER.debug(
                "Live session for %s not granted to %s: %s",
                self.device_slug,
                source,
                denied,
            )
            return
        self._session_task = self._entry.async_create_background_task(
            self.hass,
            self._async_run_session(source),
            name=f"{DOMAIN} {self.device_slug} live session",
        )

    def _can_grant(self, source: str, now: datetime) -> str | None:
        """Return the first unmet grant condition, or ``None`` when clear.

        The conditions are evaluated in the order they are reported: the states
        that mean "not now at all" (a session in progress, our own failure
        backoff, the account being throttled on the REST domain, an offline
        device), then the shared budget, and finally the per-source rules. All
        of them are cheap local reads, so evaluating them together keeps the
        reporting order in one readable place.
        """
        state = self.state
        backoff_until = state.backoff_until
        last_end = state.last_session_end
        checks: tuple[tuple[bool, str], ...] = (
            (self._session_running, DENIED_ACTIVE),
            (backoff_until is not None and now < backoff_until, DENIED_BACKOFF),
            (self.client.rest_backoff_active, DENIED_REST_BACKOFF),
            (not self.fast.device_online, DENIED_OFFLINE),
            (state.sessions_today >= state.config.sessions_per_day, DENIED_BUDGET),
            (
                last_end is not None
                and (now - last_end).total_seconds() < state.config.min_gap_seconds,
                DENIED_GAP,
            ),
            (
                source == LIVE_SOURCE_ACTIVE_USE and self._in_use_cooldown(now),
                DENIED_COOLDOWN,
            ),
            (
                source == LIVE_SOURCE_SMART and self._suspension_active(now),
                DENIED_SUSPENDED,
            ),
            (source == LIVE_SOURCE_SMART and self._is_night(now), DENIED_NIGHT),
        )
        for failed, reason in checks:
            if failed:
                return reason
        return None

    def _in_use_cooldown(self, now: datetime) -> bool:
        """Return whether the poll-detected active-use trigger is still cooling."""
        last = self._last_active_use
        if last is None:
            return False
        return (now - last).total_seconds() < LIVE_ACTIVE_USE_COOLDOWN_SECONDS

    @property
    def _session_running(self) -> bool:
        """Return whether a session task is currently owning the websocket."""
        task = self._session_task
        return task is not None and not task.done()

    def _hold_wanted(self) -> str | None:
        """Return the source of the hold the user is asking for, if any.

        Continuous mode outranks the manual hold: it is the deliberate always-on
        choice, and it is what should keep the session alive if both are on.
        """
        state = self.state
        if state.config.continuous:
            return LIVE_SOURCE_CONTINUOUS
        if state.live_view:
            return LIVE_SOURCE_MANUAL
        return None

    def _smart_block_wanted(self, now: datetime) -> bool:
        """Return whether the peak-hour tier wants the socket held right now.

        The peak block is a hold in its own right, and this is its single
        definition: the tier is on, not suspended for the day, ``now`` falls on
        a learned peak hour, and it is not night. Every lifecycle decision —
        renewing a window, releasing a user hold, resuming after a lost
        session — consults this rather than re-deriving the terms, so the
        block can never be torn down by one path while another still wants it.
        """
        state = self.state
        return (
            state.config.smart_windows
            and not self._suspension_active(now)
            and self._in_peak_hour(now)
            and not self._is_night(now)
        )

    def _suspension_active(self, now: datetime) -> bool:
        """Return whether the no-flow brake still stands the tier down."""
        until = self.state.smart_suspended_until
        return until is not None and now < until

    def _block_end(self, now: datetime) -> datetime:
        """Return when the peak block containing ``now`` runs out.

        The no-flow brake stands the tier down for the *rest of the current
        contiguous block* — quiet at 17:00 must not forfeit the evening block,
        which is a fresh hypothesis about a different hour (first production
        day proved exactly this: a quiet 17:00 skipped the household's real
        evening usage window under the old day-long latch). The walk
        follows the same rules the hold itself renews under: consecutive peak
        hours extend the block, and the night wall ends it outright.
        """
        local = now.astimezone(self._timezone()).replace(
            minute=0, second=0, microsecond=0
        )
        end = local + timedelta(hours=1)
        result: AnalyticsResult | None = self.engine.data
        peak_hours = result.grid.peak_hours if result is not None else ()
        if len(peak_hours) == _WEEKDAYS:
            for _ in range(_HOURS_PER_DAY):
                hour = end.hour
                if _NIGHT_START_HOUR <= hour < _NIGHT_END_HOUR:
                    break
                if hour not in peak_hours[end.weekday()]:
                    break
                end += timedelta(hours=1)
        return end

    @callback
    def _resume_hold(self) -> None:
        """Re-request a wanted hold that is not currently streaming.

        A hold can outlive the session that carries it. For the continuous
        flag that is unconditional: a refused grant, a failure backoff or an
        offline blink all end the session while the flag stays on, and this
        retry (from every poll and from the backoff expiry) is what brings it
        back. The manual hold is narrower by design — any *clean* session end
        also switches it off (deliberate: manual is ephemeral) — so it resumes
        only across failures, where the switch survives. The peak block
        resumes like the continuous flag: a block lost to an absorbed window,
        a ticket collision or a backoff picks up again mid-hour instead of
        forfeiting the rest of it.
        """
        source = self._hold_wanted()
        if source is None and self._smart_block_wanted(dt_util.utcnow()):
            source = LIVE_SOURCE_SMART
        if source is not None and not self._session_running:
            self._request(source)

    async def _async_release_hold(self) -> None:
        """End the running session once nothing is holding it any more.

        A released switch is not the only interest that can keep the socket:
        a peak block in progress holds the session too, whoever started it —
        the manual switch's auto-off cap (and the live-dashboard blueprint
        flipping it off) must not cost the tier the rest of its block.
        """
        if (
            self._hold_wanted() is None
            and not self._smart_block_wanted(dt_util.utcnow())
            and self._session_task is not None
        ):
            await self._async_stop_session()

    async def _async_release_smart_hold(self) -> None:
        """End a peak-hour session whose tier has just been switched off.

        The mirror of :meth:`_async_release_hold` for the source that holds its
        socket without a switch of its own: a manual or continuous hold on the
        same session outranks the flag and keeps it open.
        """
        if (
            self.state.source == LIVE_SOURCE_SMART
            and self._hold_wanted() is None
            and self._session_task is not None
        ):
            await self._async_stop_session()

    # -- session lifecycle -------------------------------------------------

    async def _async_run_session(self, source: str) -> None:
        """Run one granted session end to end, absorbing every failure.

        The background task that owns the websocket. Cancellation (a released
        hold, an unload) is the caller's business and propagates untouched;
        every other exception becomes a recorded failure, because a live session
        is an optional extra and must never take a task — or the polling tier —
        down with it.
        """
        try:
            await self._async_session(source)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            # Deliberately broad: anything the ticket call, the handshake or the
            # stream can raise is live-mode state, not a crash.
            self._handle_failure(err)
        finally:
            self._cancel_window_timer()
            await self._async_close_socket()

    async def _async_session(self, source: str) -> None:
        """Hold one session open for as many reporting windows as it is wanted."""
        self._session = await self._async_open()
        self._publish_live(source)
        while True:
            window_opened = self.hass.loop.time()
            reason = await self._async_stream()
            if not self._window_saw_frames:
                # A healthy window always delivers frames within seconds (the
                # connect snapshot at minimum). A stream that ends without a
                # single one is a sick cloud, and renewing into it would cycle
                # connect-and-die at the ticket floor forever — escalate into
                # the failure path so the backoff can grow instead.
                msg = "The live stream ended without delivering a single frame"
                raise AquaHomeConnectionError(msg)
            self._account_window()
            if not self._wants_renewal(reason):
                break
            blocked = self._renewal_blocked()
            if blocked is not None:
                _LOGGER.debug(
                    "Not renewing the %s live session: %s", self.device_slug, blocked
                )
                break
            await self._async_close_socket()
            await self._async_pace_renewal(window_opened)
            self._session = await self._async_renewal_open()
            self._publish_renewal()
        # Close before publishing the end so the state never claims idle while
        # the socket is still up.
        await self._async_close_socket()
        self._finish_session()

    async def _async_pace_renewal(self, window_opened: float) -> None:
        """Hold the renewal until the window has cost at most one refill token.

        The window length is the device's own ``app_active_timeout`` — a value
        the cloud could someday shrink. Renewal cadence is a ticket cadence, so
        it is floored here at the measured refill interval of the /live bucket:
        whatever the device advertises, a held session can never spend tickets
        faster than the bucket refills.
        """
        remaining = LIVE_RENEWAL_MIN_SECONDS - (self.hass.loop.time() - window_opened)
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def _async_renewal_open(self) -> AquaHomeLiveSession:
        """Open the next reporting window, riding out one ticket collision.

        The client's live-ticket floor is account-wide while holds are
        per-device: two devices renewing their blocks in the same minute
        collide on it. For a renewal that is not a failure — the block is
        healthy, the account is simply mid-floor — so one bounded wait clears
        the floor and retries once. A second refusal is a real throttle and
        takes the failure path.
        """
        try:
            return await self._async_open()
        except RateLimitError:
            _LOGGER.debug(
                "Live-ticket floor hit renewing the %s session; waiting it out",
                self.device_slug,
            )
            await asyncio.sleep(LIVE_RENEWAL_MIN_SECONDS)
            return await self._async_open()

    async def _async_open(self) -> AquaHomeLiveSession:
        """Fetch a ticket and open the websocket on it.

        Tickets expire a few minutes after issue and the server answers a stale
        one by rejecting the handshake, so exactly one fresh ticket is fetched
        and retried before the attempt counts as a failure — a second rejection
        is a real problem, not a race with the clock.

        The retry ticket bypasses the client's own request floor. It has to:
        the rejected ticket was issued seconds ago, so the floor would refuse
        every retry and the path would report a throttle error instead of
        reconnecting.
        """
        session = await self._async_ticketed_session()
        try:
            await session.connect()
        except LiveTicketExpiredError:
            _LOGGER.debug(
                "Live ticket for %s expired before the handshake; retrying once",
                self.device_slug,
            )
            session = await self._async_ticketed_session(ignore_throttle=True)
            await session.connect()
        return session

    async def _async_ticketed_session(
        self, *, ignore_throttle: bool = False
    ) -> AquaHomeLiveSession:
        """Return an unconnected session bound to a freshly issued ticket.

        ``ignore_throttle`` is set by the handshake retry alone; see
        :meth:`_async_open`.
        """
        ticket = await self.client.async_get_live_ticket(
            self.device_id, LIVE_SUBSCRIBED_PROPERTIES, ignore_throttle=ignore_throttle
        )
        uri = ticket.websocket_uri
        if not uri:
            msg = "The live ticket carried no websocket URI"
            raise AquaHomeConnectionError(msg)
        return AquaHomeLiveSession(
            async_get_clientsession(self.hass),
            live_websocket_url(self.client.base_url, uri),
        )

    async def _async_stream(self) -> str:
        """Consume one reporting window's frames and report how it ended.

        Runs until the window timer closes the socket, the device declares the
        window over, or the server ends the stream. The two housekeeping
        properties are consumed here and never leave this method: the window
        signal decides the loop, the advertised window length re-sizes the next
        timer, and the liveness heartbeat is dropped outright.
        """
        session = self._session
        if session is None:
            return _WINDOW_STREAM_END
        self._window_saw_frames = False
        self._window_saw_flow = False
        self._arm_window_timer()
        async for frame in session.frames():
            if not self._window_saw_frames:
                self._note_stream_alive()
            if frame.name == _APP_ACTIVE_PROPERTY:
                if frame.value is False:
                    self._window_reason = _WINDOW_APP_INACTIVE
                    break
            elif frame.name == _APP_ACTIVE_TIMEOUT_PROPERTY:
                self._note_window_timeout(frame.value)
            elif frame.name != _CLOCK_PROPERTY:
                self._buffer_frame(frame)
        self._cancel_window_timer()
        return self._window_reason

    def _wants_renewal(self, reason: str) -> bool:
        """Return whether the session should open another reporting window.

        A hold renews for as long as it is held, and a wanted peak block is a
        hold — the full-block capture is the whole point of arming on peaks:
        the sub-hour, per-gallon events this API yields nowhere else need the
        socket held across the probable-usage block, not for the five minutes
        one reporting window covers. :meth:`_smart_block_wanted` is re-read per
        window, so the hold ends within one window of the tier being switched
        off, suspended for the day, or the block running out; consecutive peak
        hours renew straight through as one contiguous block (deliberate —
        asking the grant gate for the second hour would lose it to the
        minimum-gap check).

        Without any hold only the newer API host renews: there
        ``app_active=false`` mid-session is the server asking for a reconnect,
        while on the legacy host it is simply the window closing and an
        on-demand session is done. A smart session is excluded from that
        host-side renewal — its block rule decides its end on both hosts.
        """
        if self._hold_wanted() is not None:
            return True
        if self._smart_block_wanted(dt_util.utcnow()):
            # Whoever opened the session: a burst or a manual grant streaming
            # when a peak block begins renews straight into it, because the
            # per-gallon capture is the same whichever trigger paid the grant.
            return True
        return (
            self._iqua2
            and reason == _WINDOW_APP_INACTIVE
            and self.state.source != LIVE_SOURCE_SMART
        )

    def _renewal_blocked(self) -> str | None:
        """Return the hard-off condition that forbids a renewal, or ``None``.

        Renewals skip the grant gate — they are ticket spends inside a session
        the budget already paid for — but never the conditions that mean live
        mode must stop right now.
        """
        state = self.state
        backoff_until = state.backoff_until
        if backoff_until is not None and dt_util.utcnow() < backoff_until:
            return DENIED_BACKOFF
        if self.client.rest_backoff_active:
            return DENIED_REST_BACKOFF
        if not self.fast.device_online:
            return DENIED_OFFLINE
        return None

    @callback
    def _publish_live(self, source: str) -> None:
        """Record the start of a granted session and spend one grant.

        The failure trail is deliberately NOT cleared here: a completed
        handshake against a sick cloud proves nothing (a server that accepts
        connections and instantly drops them would otherwise pin the backoff
        at its floor forever). The trail clears on stream evidence — the first
        frame of a window — in :meth:`_note_stream_alive`.
        """
        now = dt_util.utcnow()
        if source == LIVE_SOURCE_ACTIVE_USE:
            self._last_active_use = now
        state = self.state
        self._publish(
            replace(
                state,
                status=LIVE_STATUS_LIVE,
                source=source,
                session_started=now,
                windows_in_session=0,
                sessions_today=state.sessions_today + 1,
                backoff_until=None,
            )
        )

    @callback
    def _publish_renewal(self) -> None:
        """Record one more reporting window inside the running session.

        A renewal spends a ticket, never a grant. Like a fresh grant it earns
        no failure-trail clearing by connecting — only its first frame does.
        """
        state = self.state
        self._publish(replace(state, windows_in_session=state.windows_in_session + 1))

    @callback
    def _note_stream_alive(self) -> None:
        """Clear the failure trail on real stream evidence.

        Called for the first frame of every reporting window. Frames are the
        proof a session is worth something — the connect snapshot arrives
        within seconds on a healthy stream — so this is where a recovered
        cloud withdraws the repair issue and zeroes the failure count.
        """
        self._window_saw_frames = True
        state = self.state
        if state.consecutive_failures or state.last_error is not None:
            self._clear_failure_state()
            self._publish(
                replace(
                    state,
                    consecutive_failures=0,
                    backoff_until=None,
                    last_error=None,
                )
            )

    @callback
    def _finish_session(self) -> None:
        """Publish the clean end of a session and settle its book-keeping.

        The single end path, shared by a session that ran out of windows and one
        cut short by a released hold, so the no-flow accounting, the manual
        switch's auto-off and the minimum-gap stamp can never diverge.
        """
        state = self.state
        if state.status != LIVE_STATUS_LIVE:
            return
        self._cancel_view_cap()
        self._publish(
            replace(
                state,
                status=LIVE_STATUS_IDLE,
                source=None,
                session_started=None,
                windows_in_session=0,
                last_session_end=dt_util.utcnow(),
                live_view=False,
            )
        )

    @callback
    def _account_window(self) -> None:
        """Count one finished reporting window against the no-flow brake.

        Streaming a house where no water moves buys nothing, so a run of
        consecutive no-flow *windows* — the five-minute unit, not the whole
        held block, or an empty house would stream its blocks for hours before
        the brake could bite — suspends the tier until the next device-local
        day. Counted whenever the tier is what keeps the socket open: the
        session it granted itself, and any burst-opened session whose renewals
        the wanted block is paying for. A manual or continuous hold is the
        user's explicit choice, quiet house or not, and is never counted. The
        latch is published mid-session, which is exactly what lets the next
        renewal decision end the held block within one window.
        """
        if self._hold_wanted() is not None:
            return
        if self.state.source != LIVE_SOURCE_SMART and not self._smart_block_wanted(
            dt_util.utcnow()
        ):
            return
        if self._window_saw_flow:
            self._no_flow_windows = 0
            return
        self._no_flow_windows += 1
        if self._no_flow_windows < LIVE_SMART_NO_FLOW_SUSPEND:
            return
        now = dt_util.utcnow()
        if not self._suspension_active(now):
            until = self._block_end(now)
            _LOGGER.debug(
                "Standing smart live windows for %s down until %s: %s windows "
                "in a row saw no flow",
                self.device_slug,
                until,
                self._no_flow_windows,
            )
            self._publish(replace(self.state, smart_suspended_until=until))

    async def _async_stop_session(self, *, publish_idle: bool = True) -> None:
        """Cancel the running session task and close the socket it owns.

        The task reference is cleared only once the cancellation has been
        awaited, so the grant gate keeps refusing new sessions while the old one
        is still tearing down.
        """
        task = self._session_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._session_task = None
        self._cancel_window_timer()
        await self._async_close_socket()
        if publish_idle:
            self._finish_session()

    async def _async_close_socket(self) -> None:
        """Close the current websocket, if one is open. Safe to repeat."""
        session = self._session
        self._session = None
        if session is None:
            return
        try:
            await session.close()
        except Exception:
            # A close that fails has nothing left to report: the socket is being
            # abandoned either way, and raising here would mask why.
            _LOGGER.debug(
                "Closing the %s live websocket failed", self.device_slug, exc_info=True
            )

    # -- window timing -----------------------------------------------------

    @callback
    def _arm_window_timer(self) -> None:
        """Arm the client-side end of the current reporting window."""
        self._cancel_window_timer()
        self._window_reason = _WINDOW_STREAM_END
        self._unsub_window = async_call_later(
            self.hass, self._window_delay(), self._handle_window_timer
        )

    def _window_delay(self) -> float:
        """Return how long the current reporting window may run, in seconds.

        The device advertises its own window length and the grace keeps this
        timer from racing the server's own end-of-window frame. The newer API
        host runs much longer sessions and signals its reconnects explicitly, so
        there the timer is only a backstop.
        """
        if self._iqua2:
            return LIVE_IQUA2_WINDOW_SECONDS
        minutes = self._timeout_minutes
        if minutes is None:
            minutes = _window_timeout_minutes(self.fast.data)
        if minutes is None:
            return LIVE_WINDOW_FALLBACK_SECONDS + LIVE_WINDOW_GRACE_SECONDS
        return minutes * _SECONDS_PER_MINUTE + LIVE_WINDOW_GRACE_SECONDS

    @callback
    def _handle_window_timer(self, _now: datetime) -> None:
        """End the reporting window the device stopped reporting in.

        The stream falls silent well before the socket does, so the window ends
        client-side: closing the socket is what unblocks the frame loop, which
        then decides between a renewal and a clean end.
        """
        self._unsub_window = None
        self._window_reason = _WINDOW_TIMER
        session = self._session
        if session is not None:
            self.hass.async_create_task(
                session.close(), name=f"{DOMAIN} {self.device_slug} live window end"
            )

    @callback
    def _note_window_timeout(self, value: bool | int | float | str | None) -> None:
        """Remember the reporting-window length the device just published."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        if value > 0:
            self._timeout_minutes = float(value)

    # -- frame application -------------------------------------------------

    @callback
    def _buffer_frame(self, frame: LiveFrame) -> None:
        """Queue one streamed property update for the next coalesced apply.

        Only the properties entity value paths actually bind are merged, and
        only when the value really moved: the connect snapshot repeats values
        the poll already carries, and re-publishing those would churn every
        bound entity for nothing.
        """
        if frame.name not in LIVE_PUSHED_PROPERTIES:
            return
        device: Device | None = self.fast.data
        if device is None:
            return
        pending = self._pending.get(frame.name)
        existing = device.properties.get(frame.name)
        if pending is not None:
            # A property already buffered is compared against the value that
            # will actually be applied, not the older polled one.
            if pending.value == frame.value:
                return
        elif existing is not None and existing.value == frame.value:
            return
        self._pending[frame.name] = frame
        if frame.name in _COUNTER_PROPERTIES:
            self._window_saw_flow = True
        if self._unsub_coalesce is None:
            self._unsub_coalesce = async_call_later(
                self.hass, LIVE_COALESCE_SECONDS, self._handle_coalesce
            )

    @callback
    def _handle_coalesce(self, _now: datetime) -> None:
        """Apply the frames buffered during the coalescing window."""
        self._unsub_coalesce = None
        self._apply_pending()

    @callback
    def _apply_pending(self) -> None:
        """Merge the buffered frames into the polled device view.

        Live mode upgrades the existing entities rather than adding its own, so
        the streamed values are written into the polling coordinator's frozen
        device and republished through its live-apply path, which keeps the
        REST poll's floor cadence: the stream refreshes a handful of raw
        properties, but the enriched block — regeneration state, salt level,
        feature gating — only ever refreshes through a genuine poll.
        """
        pending = self._pending
        self._pending = {}
        device: Device | None = self.fast.data
        if device is None or not pending:
            return
        properties = dict(device.properties)
        for name, frame in pending.items():
            existing = properties.get(name)
            properties[name] = (
                replace(existing, value=frame.value, updated_at=frame.timestamp)
                if existing is not None
                else PropertyValue(
                    name=name, value=frame.value, updated_at=frame.timestamp
                )
            )
        self.fast.async_apply_live_update(replace(device, properties=properties))

    # -- failure handling --------------------------------------------------

    @callback
    def _handle_failure(self, err: Exception) -> None:
        """Record one failed session attempt and back off before the next.

        The backoff doubles from one minute to a half-hour cap, and only a run
        of failures against a device that reports itself online raises a repair
        issue — a device that is simply offline is not a live-mode problem. Only
        the error message is kept; the ticket that authenticates the socket is a
        credential and never reaches state.
        """
        state = self.state
        failures = state.consecutive_failures + 1
        message = str(err) or type(err).__name__
        delay = min(
            LIVE_BACKOFF_INITIAL_SECONDS
            * 2 ** min(failures - 1, _BACKOFF_EXPONENT_CAP),
            LIVE_BACKOFF_MAX_SECONDS,
        )
        now = dt_util.utcnow()
        _LOGGER.debug(
            "Live session for %s failed (%s); retrying in %s s",
            self.device_slug,
            message,
            delay,
        )
        self._cancel_backoff_timer()
        self._unsub_backoff = async_call_later(
            self.hass, delay, self._handle_backoff_expiry
        )
        self._publish(
            replace(
                state,
                status=LIVE_STATUS_BACKOFF,
                source=None,
                session_started=None,
                windows_in_session=0,
                consecutive_failures=failures,
                backoff_until=now + timedelta(seconds=delay),
                last_error=message,
            )
        )
        if failures >= LIVE_FAILURES_FOR_ISSUE and self.fast.device_online:
            self._file_issue(message)

    @callback
    def _handle_backoff_expiry(self, _now: datetime) -> None:
        """Return to idle when the backoff window closes, resuming any hold."""
        self._unsub_backoff = None
        if self.state.status != LIVE_STATUS_BACKOFF:
            return
        self._publish(replace(self.state, status=LIVE_STATUS_IDLE, backoff_until=None))
        self._resume_hold()

    @callback
    def _clear_failure_state(self) -> None:
        """Withdraw the failure trail after a successful connect."""
        self._cancel_backoff_timer()
        ir.async_delete_issue(
            self.hass, DOMAIN, f"{LIVE_FAILING_ISSUE_PREFIX}{self.device_slug}"
        )

    @callback
    def _file_issue(self, error: str) -> None:
        """Report that live mode keeps failing on an otherwise-online device."""
        device: Device | None = self.fast.data
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{LIVE_FAILING_ISSUE_PREFIX}{self.device_slug}",
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="live_mode_failing",
            translation_placeholders={
                "device": device_display_name(device) if device is not None else "?",
                "error": error,
            },
        )

    # -- state, timers, time -----------------------------------------------

    @callback
    def _publish(self, state: LiveState) -> None:
        """Publish one live-state mutation to the live entities."""
        self._state = state
        self.async_set_updated_data(state)

    @callback
    def _apply_config(self, config: LiveConfig) -> None:
        """Persist one configuration change and publish it.

        The user-set subset goes into ``entry.options`` (no update listener is
        registered for this entry, so writing options cannot trigger a reload
        storm) and the full state is published to the switches and numbers.
        """
        self.hass.config_entries.async_update_entry(
            self._entry,
            options=options_with_config(self._entry, self.device_id, config),
        )
        self._publish(self.state.with_config(config))

    @callback
    def _cancel_timers(self) -> None:
        """Cancel every armed timer and drop any frames waiting to be applied."""
        self._cancel_window_timer()
        self._cancel_backoff_timer()
        self._cancel_view_cap()
        self._cancel_smart_window()
        if self._unsub_coalesce is not None:
            self._unsub_coalesce()
            self._unsub_coalesce = None
        self._pending = {}

    @callback
    def _cancel_window_timer(self) -> None:
        """Cancel the pending window-end timer, if one is armed."""
        if self._unsub_window is not None:
            self._unsub_window()
            self._unsub_window = None

    @callback
    def _cancel_backoff_timer(self) -> None:
        """Cancel the pending backoff-expiry timer, if one is armed."""
        if self._unsub_backoff is not None:
            self._unsub_backoff()
            self._unsub_backoff = None

    @callback
    def _cancel_view_cap(self) -> None:
        """Cancel the manual hold's auto-off timer, if one is armed."""
        if self._unsub_view_cap is not None:
            self._unsub_view_cap()
            self._unsub_view_cap = None

    @callback
    def _cancel_smart_window(self) -> None:
        """Cancel the pending smart window, if one is armed."""
        if self._unsub_smart is not None:
            self._unsub_smart()
            self._unsub_smart = None

    def _timezone(self) -> tzinfo:
        """Return the zone the device dates its own days in.

        Falls back to the installation's configured zone rather than UTC: the
        peak grid this manager reads was learned in the analytics tier's zone,
        whose own missing-``tz_id`` fallback is the installation zone — so both
        tiers degrade to the *same* clock and four sharp peak hours cannot
        silently shift by the household's UTC offset.
        """
        zone = _device_timezone(self.fast.data, self.device_slug)
        return zone if zone is not None else dt_util.get_default_time_zone()

    def _local_date(self, now: datetime) -> date:
        """Return the device-local calendar day ``now`` falls on."""
        return now.astimezone(self._timezone()).date()

    def _is_night(self, now: datetime) -> bool:
        """Return whether ``now`` falls in the device-local night hours."""
        hour = now.astimezone(self._timezone()).hour
        return _NIGHT_START_HOUR <= hour < _NIGHT_END_HOUR


@callback
def async_remove_live_issues(hass: HomeAssistant, entry: AquaHomeConfigEntry) -> None:
    """Delete every device's live-mode issue when the entry is removed.

    Mirrors the other issue cleanups: ids are rebuilt from the device registry
    because ``async_remove_entry`` may run on an entry that was never loaded,
    and deleting an id that was never filed is a documented no-op.
    """
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        for domain, identifier in device.identifiers:
            if domain == DOMAIN:
                ir.async_delete_issue(
                    hass, DOMAIN, f"{LIVE_FAILING_ISSUE_PREFIX}{identifier}"
                )
