"""Behavioural tests for the live-mode manager (``live.py``).

The manager owns one device's websocket lifecycle, so everything here is driven
against a **real** local server: the shared ``FakeIquaLiveServer`` serves the
ticket endpoint and the ticketed websocket, and the production client, session
and manager run unmodified against it. That choice sets the two ground rules
this file obeys throughout.

*No frozen clock.* ``freezegun`` freezes ``time.monotonic``, which is the clock
the event loop schedules socket I/O on, so a frozen test that talks to the
server never completes. Home Assistant timers are therefore driven with
``async_fire_time_changed`` (which fires due timers without moving the clock),
and everything the manager keys to wall-clock time — the device-local day, the
night window, the learned peak hours — is steered through the device's own
reported ``tz_id`` instead.
Two rules genuinely need wall-clock movement and are therefore neutralised
everywhere except in the one test that owns each: the minimum gap between
grants (lowered per test through :func:`_allow_back_to_back_grants`, asserted
in :func:`test_minimum_gap_denies_the_next_trigger`) and the renewal pacing
floor (zeroed for the whole module by :func:`_instant_renewal_pacing`,
asserted in :func:`test_the_renewal_pacing_floors_a_windows_ticket_spend`).

*No ``aioresponses``.* ``ws_connect`` routes through the same request path
``aioresponses`` patches, so the coordinators here are built directly and seeded
with ``async_set_updated_data`` — the recorder-free harness idiom — and the
client is bound to the fake server's base URL.

Sessions run as background tasks, so assertions wait on an observable condition
(:func:`_settle_until`) rather than on ``async_block_till_done`` alone, and a
test that asserts *nothing* happened lets the loop run first (:func:`_quiesce`).

What the groups below pin: every ordered deny reason of the grant gate is
reachable and is the *first* unmet condition reported; the daily budget counts
grants while window renewals inside a held session cost only tickets; the
poll-, analytics- and event-driven triggers fire on exactly the transitions they
are specified for; holds renew across reporting windows and stand down on the
conditions that mean live mode must stop; a learned peak block is a hold in its
own right — held whichever trigger opened the session, surviving the release of
the user hold that did, resumed on the next poll when a session is lost inside
it, and ended within one reporting window of the block running out; the no-flow
brake counts consecutive quiet reporting *windows* (not sessions) and latches
mid-session; failures back off from one minute to the half-hour cap and raise
(then withdraw) a repair issue; and streamed frames reach the polled device view
coalesced, deduplicated, allow-listed and without the two housekeeping
properties, while never being mistaken for a poll.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from datetime import UTC, timedelta
from http import HTTPStatus
from itertools import count
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.aquahome.analytics.engine import AquaHomeAnalyticsEngine
from custom_components.aquahome.analytics.model import (
    AnalyticsResult,
    AnomalyState,
    ForecastState,
    GridSummary,
    LeakState,
    VacationState,
)
from custom_components.aquahome.api import Device, RateLimitError
from custom_components.aquahome.api.auth import AuthManager
from custom_components.aquahome.api.client import AquaHomeClient
from custom_components.aquahome.api.models import LiveTicket
from custom_components.aquahome.api.websocket import LiveFrame
from custom_components.aquahome.const import (
    DOMAIN,
    LIVE_ACTIVE_USE_DELTA_GALLONS,
    LIVE_BACKOFF_INITIAL_SECONDS,
    LIVE_BACKOFF_MAX_SECONDS,
    LIVE_COALESCE_SECONDS,
    LIVE_FAILURES_FOR_ISSUE,
    LIVE_MIN_GAP_SECONDS_MIN,
    LIVE_PUSHED_PROPERTIES,
    LIVE_RENEWAL_MIN_SECONDS,
    LIVE_SESSIONS_PER_DAY_MAX,
    LIVE_SESSIONS_PER_DAY_MIN,
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
    LIVE_VIEW_HOLD_MAX_SECONDS,
    LIVE_WINDOW_FALLBACK_SECONDS,
    LIVE_WINDOW_GRACE_SECONDS,
    RECHARGE_STATE_READY,
)
from custom_components.aquahome.coordinator import (
    AquaHomeActivityCoordinator,
    AquaHomeCoordinator,
)
from custom_components.aquahome.live import (
    _WINDOW_APP_INACTIVE as WINDOW_ENDED_ON_APP_INACTIVE,
)
from custom_components.aquahome.live import _WINDOW_TIMER as WINDOW_ENDED_ON_TIMER
from custom_components.aquahome.live import (
    DENIED_ACTIVE,
    DENIED_BACKOFF,
    DENIED_BUDGET,
    DENIED_COOLDOWN,
    DENIED_GAP,
    DENIED_NIGHT,
    DENIED_OFFLINE,
    DENIED_REST_BACKOFF,
    DENIED_SUSPENDED,
    LIVE_FAILING_ISSUE_PREFIX,
    AquaHomeLiveManager,
    _device_timezone,
    async_remove_live_issues,
)
from custom_components.aquahome.live_state import LiveConfig, config_from_options
from custom_components.aquahome.statistics import AquaHomeStatisticsCoordinator
from tests.api.conftest import FAKE_NOW, FakeClock, make_jwt
from tests.conftest import TEST_DEVICE_ID, load_fixture
from tests.live_server import FakeIquaLiveServer, frame

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterable

    from homeassistant.core import HomeAssistant

    from custom_components.aquahome.api.websocket import AquaHomeLiveSession

#: Slug derived from the fixture serial ``4213377-30105-2242`` (see entity.py).
SLUG = "4213377_30105_2242"

#: Logger the manager reports its grant decisions on.
LIVE_LOGGER = "custom_components.aquahome.live"

#: Debug template the manager logs one refused grant with. Every deny reason is
#: observable only here and through the session that did *not* start, so the two
#: are asserted together throughout.
DENIAL_MESSAGE = "Live session for %s not granted to %s: %s"

#: Ceiling for every wait on a background session, in seconds.
SETTLE_TIMEOUT = 5.0
#: One turn of a settle loop: long enough for local socket I/O to progress.
SETTLE_STEP = 0.005
#: Turns a "nothing happened" assertion lets the loop run before checking.
QUIET_TURNS = 20

#: Monotonic jump per client clock read. The client refuses a second live ticket
#: within a minute of the previous one on its own monotonic clock; renewals and
#: retries happen in milliseconds here, so the injected clock puts every read
#: well past that floor and leaves the pacing assertions to the manager's state.
CLOCK_STEP_SECONDS = 1_000.0

#: The captured payload's own values, which the triggers below move away from.
FIXTURE_GALLONS_TODAY = 3
FIXTURE_WATER_COUNTER = 47_479
FIXTURE_CLOCK_SECS = 30_234
FIXTURE_RF_DBM = -37
FIXTURE_FLOW_GPM = 0
FIXTURE_HARDNESS = 26
#: Rise comfortably above the active-use threshold.
ACTIVE_USE_RISE = int(LIVE_ACTIVE_USE_DELTA_GALLONS) + 3

#: A raw property the stream may name but no entity value path binds. It is not
#: even subscribed, which is exactly why a frame carrying it must be dropped.
UNBOUND_PROPERTY = "hardness_grains"

#: ``recharge_ui`` state meaning a recharge is running right now.
REGENERATING = "regenerating"

_HOURS_PER_DAY = 24
_WEEKDAYS = 7
#: The device-local night window the smart tier refuses to open in.
NIGHT_RANGE = range(1, 7)
#: Device-local hours a test may ask "now" to fall on, per class and in
#: preference order. Every candidate sits clear of its class's edges, so an
#: hour boundary crossing mid-test cannot move one into the other class; the
#: alternatives exist only so :func:`_zone_for_local_hour` can step off an hour
#: whose zone would be indistinguishable from the manager's own fallback.
NIGHT_HOUR_CANDIDATES = (3, 4, 2, 5)
DAY_HOUR_CANDIDATES = (12, 13, 11, 14, 10)
#: The hour each class asks for by default.
NIGHT_HOUR = NIGHT_HOUR_CANDIDATES[0]
DAY_HOUR = DAY_HOUR_CANDIDATES[0]
#: Two zones 26 h apart: whatever the instant, they date it to different days.
TZ_FAR_EAST = "Etc/GMT-14"
TZ_FAR_WEST = "Etc/GMT+12"

#: Learned binary activity grids. The live tier no longer reads them (peaks
#: replaced them in v1.1), which is itself asserted below.
GRID_HOURS = _WEEKDAYS * _HOURS_PER_DAY
NO_ACTIVE_HOURS = (False,) * GRID_HOURS
ALL_ACTIVE_HOURS = (True,) * GRID_HOURS


def _peaks_only(weekday: int, *hours: int) -> tuple[tuple[int, ...], ...]:
    """Return a peak grid whose only peak hours are ``hours`` on ``weekday``."""
    return tuple(hours if day == weekday else () for day in range(_WEEKDAYS))


def _peaks_except(*hours: int) -> tuple[tuple[int, ...], ...]:
    """Return a peak grid marking every hour of every weekday but ``hours``."""
    kept = tuple(hour for hour in range(_HOURS_PER_DAY) if hour not in hours)
    return (kept,) * _WEEKDAYS


def _peaks_away_from(hour: int) -> tuple[tuple[int, ...], ...]:
    """Return a peak grid ranking every hour but ``hour`` and its neighbours.

    The two neighbours go with it so an hour boundary crossing mid-test cannot
    flip the answer. A window still arms off this grid — the next peak hour is
    two hours out — while the session it opens is NOT inside a peak hour, so it
    ends at its first reporting window instead of holding a block.
    """
    return _peaks_except(hour - 1, hour, hour + 1)


#: Learned peak hours, per python weekday (Mon=0). A real grid ranks at most
#: PEAK_HOURS_PER_WEEKDAY hours per weekday, but the manager reads the tuple as
#: an allow-list rather than a length contract, so these craft what each case
#: needs: no peak at all, every hour a peak, and peaks that fall entirely
#: inside the night window the smart tier refuses to open in.
NO_PEAK_HOURS = ((),) * _WEEKDAYS
ALL_PEAK_HOURS = (tuple(range(_HOURS_PER_DAY)),) * _WEEKDAYS
NIGHT_ONLY_PEAKS = (tuple(NIGHT_RANGE),) * _WEEKDAYS
#: How far ahead the armed smart window is fired. The next peak hour of the
#: crafted grids above is at most three hours out, so this releases exactly the
#: one armed timer.
SMART_ARM_HORIZON_SECONDS = timedelta(hours=4).total_seconds()

#: Reporting-window length (minutes) that keeps the client-side window timer
#: beyond the manual hold's auto-off cap, so the cap can be fired on its own.
LONG_WINDOW_MINUTES = 60
#: Reporting-window length (minutes) a test streams, distinct from the polled
#: five minutes the fixture carries.
STREAMED_WINDOW_MINUTES = 9


# ---------------------------------------------------------------------------
# Crafted inputs
# ---------------------------------------------------------------------------


def _detail(  # noqa: PLR0913 - one keyword per payload field a trigger reads
    *,
    gallons: int = FIXTURE_GALLONS_TODAY,
    tile_state: str = RECHARGE_STATE_READY,
    enriched: bool = True,
    online: bool = True,
    tz_id: str | None = None,
    window_minutes: int | None = None,
) -> dict[str, Any]:
    """Return a device-detail payload with the manager's inputs set.

    ``load_fixture`` re-parses the JSON on every call, so each payload is an
    independent document and the fixture file is never mutated. The two enriched
    blocks that report a running recharge move together, exactly as the cloud
    reports them; ``enriched`` of ``False`` drops both, which is the "nothing to
    compare against" case.
    """
    detail = load_fixture("device-detail.json")
    treatment = detail["enriched_data"]["water_treatment"]
    if enriched:
        treatment["recharge_ui"]["state"] = tile_state
        treatment["regeneration"]["regeneration_status"] = (
            REGENERATING if tile_state == REGENERATING else "none"
        )
    else:
        treatment.pop("recharge_ui", None)
        treatment.pop("regeneration", None)
    detail["properties"]["gallons_used_today"]["value"] = gallons
    detail["is_online"] = online
    if tz_id is not None:
        detail["properties"]["tz_id"]["value"] = tz_id
    if window_minutes is not None:
        detail["properties"]["app_active_timeout"]["value"] = window_minutes
    return detail


_NEUTRAL_LEAK = LeakState(
    active=None,
    consecutive_nights=0,
    rate_liters_per_hour=None,
    implied_liters_per_day=None,
    tier=None,
    persistent_flow=False,
    last_verdict_night=None,
    masking_coverage=True,
)
_NEUTRAL_VACATION = VacationState(active=None, consecutive_days=0, since=None)
_NEUTRAL_FORECAST = ForecastState(
    gallons=None, liters=None, source=None, band_liters=None, weekday=None, persons=None
)


def _result(
    *,
    anomaly_active: bool | None = None,
    active_hours: tuple[bool, ...] = NO_ACTIVE_HOURS,
    peak_hours: tuple[tuple[int, ...], ...] = NO_PEAK_HOURS,
) -> AnalyticsResult:
    """Assemble one crafted analytics pass from neutral defaults.

    Only the blocks the manager consumes are parameterised — the anomaly
    verdict, the learned peak hours the smart tier arms and holds on, and the
    binary activity grid it deliberately stopped reading — so no assertion here
    depends on detector numerics.
    """
    return AnalyticsResult(
        computed_at=dt_util.utcnow(),
        nights=(),
        days=(),
        leak=_NEUTRAL_LEAK,
        anomaly=AnomalyState(
            active=anomaly_active,
            reasons=(),
            day=None,
            point_hours=0,
            drift_alarm=False,
            drift_cusum=False,
            drift_ewma=False,
        ),
        vacation=_NEUTRAL_VACATION,
        forecast=_NEUTRAL_FORECAST,
        grid=GridSummary(
            active_hours=active_hours,
            mature_buckets=0,
            hourly_samples=0,
            peak_hours=peak_hours,
        ),
    )


class _AdvancingMonotonic:
    """Monotonic clock that jumps :data:`CLOCK_STEP_SECONDS` on every read."""

    def __init__(self) -> None:
        """Start the clock at a fixed reading."""
        self._now = 10_000.0

    def __call__(self) -> float:
        """Return the next reading, well past the client's live-ticket floor."""
        self._now += CLOCK_STEP_SECONDS
        return self._now


class _FixedMonotonic:
    """Monotonic clock frozen at one reading — production's own worst case.

    Two ticket calls milliseconds apart read the same instant on a real clock
    too; freezing it makes that certain, which is what the client's ticket
    floor has to be measured against.
    """

    def __call__(self) -> float:
        """Return the one and only reading."""
        return 10_000.0


class _UnclosableSession:
    """Stand-in live session whose close always fails."""

    async def close(self) -> None:
        """Fail the way a socket already dead at the TCP level does."""
        msg = "the socket is already gone"
        raise OSError(msg)


def _zone_for_local_hour(hour: int) -> tuple[str, int]:
    """Return a device zone reading an hour of ``hour``'s class, and that hour.

    The night rule, the daily counters and the peak-hour predicate are all keyed
    to the *device-local* hour, which the device reports through ``tz_id``;
    real websocket traffic forbids freezing the clock, so tests steer them by
    choosing the device's zone instead.

    Steering only proves anything while the chosen zone is one the manager could
    not have arrived at by itself, and two of them are exactly that: UTC — which
    the naive arithmetic picks whenever the requested hour happens to be the
    current UTC hour, a twenty-fourth of the day on which every tz-steered
    assertion here would pass against a manager that ignored ``tz_id``
    altogether — and any zone whose current offset matches the installation's,
    which is what :meth:`~AquaHomeLiveManager._timezone` falls back to when the
    device reports no usable zone. Both are refused: the hour moves on to the
    next candidate of its own class (night stays inside [01, 07), a day hour
    stays a day hour) and is returned alongside the zone, so the grids and
    assertions built on it follow the hour actually chosen.
    """
    now = dt_util.utcnow()
    shadowed = {
        timedelta(0),
        now.astimezone(dt_util.get_default_time_zone()).utcoffset(),
    }
    candidates = NIGHT_HOUR_CANDIDATES if hour in NIGHT_RANGE else DAY_HOUR_CANDIDATES
    for candidate in (hour, *candidates):
        offset = (candidate - now.hour) % _HOURS_PER_DAY
        if offset > _HOURS_PER_DAY // 2:
            offset -= _HOURS_PER_DAY
        # POSIX-style zone names invert the sign: Etc/GMT-3 is UTC+3.
        zone = f"Etc/GMT{-offset:+d}"
        if now.astimezone(ZoneInfo(zone)).utcoffset() not in shadowed:
            return zone, candidate
    pytest.fail(f"no unshadowed zone puts the device-local hour in {hour}'s class")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@dataclass
class LiveHarness:
    """One device's live-mode stack, wired to a running fake iQua server."""

    hass: HomeAssistant
    entry: MockConfigEntry
    server: FakeIquaLiveServer
    client: AquaHomeClient
    fast: AquaHomeCoordinator
    engine: AquaHomeAnalyticsEngine
    manager: AquaHomeLiveManager
    activity: AquaHomeActivityCoordinator

    async def async_shutdown(self) -> None:
        """Stand the whole stack down, live manager first."""
        await self.manager.async_shutdown()
        await self.engine.async_shutdown()
        await self.activity.async_shutdown()
        await self.fast.async_shutdown()


@pytest.fixture(autouse=True)
def _real_sockets(socket_enabled: None) -> None:
    """Allow real TCP: every test here speaks to a live local server."""


@pytest.fixture(autouse=True)
def _capture_grant_decisions(caplog: pytest.LogCaptureFixture) -> None:
    """Record the manager's debug-level grant decisions in every test."""
    caplog.set_level(logging.DEBUG, logger=LIVE_LOGGER)


@pytest.fixture(autouse=True)
def _instant_renewal_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the renewal pacing floor so a held session renews at test speed.

    Every renewal sleeps out whatever is left of
    :data:`~custom_components.aquahome.const.LIVE_RENEWAL_MIN_SECONDS` (100 s)
    before it spends its ticket. That sleep is a real ``asyncio.sleep`` on the
    real clock — these tests talk to a real socket, so they cannot freeze it,
    and ``async_fire_time_changed`` does not release it — which would stall
    every multi-window test here for a minute and a half per window. The floor
    is asserted on its own, with the shipped value restored, in
    :func:`test_the_renewal_pacing_floors_a_windows_ticket_spend`; the tests
    that ride on it instead assert the ticket and window book-keeping the
    pacing is invisible to.
    """
    monkeypatch.setattr("custom_components.aquahome.live.LIVE_RENEWAL_MIN_SECONDS", 0.0)


@pytest.fixture
async def live_server() -> AsyncIterator[FakeIquaLiveServer]:
    """Run the fake iQua /live + /ws/ server for the duration of a test."""
    server = FakeIquaLiveServer()
    # A healthy stream always opens with a partial snapshot (observed live: the
    # app_active frame lands within seconds of every connect), and a window
    # that delivers no frame at all is treated as a sick stream and escalates
    # into the failure path. Default to the realistic minimum; tests that need
    # a richer snapshot (or a deliberately dead stream) overwrite `script`.
    server.script = [frame("app_active", True)]
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def build_live(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    live_server: FakeIquaLiveServer,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Callable[..., Awaitable[LiveHarness]]]:
    """Provide a builder for a started live manager and its data sources.

    The fast coordinator's polling interval is dropped before anything
    subscribes to it, so the only device views the manager ever sees are the
    ones a test publishes; the statistics coordinator — an engine dependency
    that is never refreshed here — is stood down for the same reason.
    """
    built: list[LiveHarness] = []

    async def _build(
        *,
        detail: dict[str, Any] | None = None,
        result: AnalyticsResult | None = None,
        iqua2: bool = False,
        monotonic: Callable[[], float] | None = None,
    ) -> LiveHarness:
        """Build and start one live manager over a seeded device and verdict.

        The client clock defaults to the self-advancing one described above;
        a test that has to face the ticket floor as production does injects
        :class:`_FixedMonotonic` instead.
        """
        mock_config_entry.add_to_hass(hass)
        session = async_get_clientsession(hass)
        auth = AuthManager(
            session, base_url=live_server.base_url, time_func=FakeClock()
        )
        auth.set_tokens(make_jwt(FAKE_NOW), "refresh-token")
        client = AquaHomeClient(
            session,
            auth,
            base_url=live_server.base_url,
            monotonic=monotonic if monotonic is not None else _AdvancingMonotonic(),
        )
        device = Device.from_dict(detail if detail is not None else _detail())
        fast = AquaHomeCoordinator(hass, mock_config_entry, client, device)
        fast.update_interval = None
        fast.async_set_updated_data(device)
        activity = AquaHomeActivityCoordinator(
            hass,
            mock_config_entry,
            client,
            device_id=TEST_DEVICE_ID,
            device_slug=SLUG,
        )
        statistics = AquaHomeStatisticsCoordinator(
            hass,
            mock_config_entry,
            client,
            device_id=TEST_DEVICE_ID,
            device_slug=SLUG,
            device_name="AquaHome",
            tz_id=None,
        )
        await statistics.async_shutdown()
        engine = AquaHomeAnalyticsEngine(
            hass,
            mock_config_entry,
            device_id=TEST_DEVICE_ID,
            device_slug=SLUG,
            fast=fast,
            activity=activity,
            statistics=statistics,
        )
        engine.async_set_updated_data(result if result is not None else _result())
        if iqua2:
            # The manager decides the host-specific session semantics by
            # comparing the client's base URL with the newer host's; pointing
            # that constant at the fake server is what puts it on that branch
            # while the traffic still lands on the local socket.
            monkeypatch.setattr(
                "custom_components.aquahome.live.IQUA2_BASE_URL", live_server.base_url
            )
        manager = AquaHomeLiveManager(
            hass,
            mock_config_entry,
            device_id=TEST_DEVICE_ID,
            device_slug=SLUG,
            client=client,
            fast=fast,
            engine=engine,
        )
        await manager.async_start()
        await hass.async_block_till_done()
        harness = LiveHarness(
            hass=hass,
            entry=mock_config_entry,
            server=live_server,
            client=client,
            fast=fast,
            engine=engine,
            manager=manager,
            activity=activity,
        )
        built.append(harness)
        return harness

    yield _build
    for harness in built:
        await harness.async_shutdown()
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Drive helpers
# ---------------------------------------------------------------------------


async def _settle_until(
    hass: HomeAssistant, condition: Callable[[], bool], description: str
) -> None:
    """Run the loop until ``condition`` holds, failing the test on timeout.

    A granted session runs as a background task, which ``async_block_till_done``
    deliberately does not wait for, so progress is observed rather than awaited.
    """
    deadline = time.monotonic() + SETTLE_TIMEOUT
    while True:
        await asyncio.sleep(SETTLE_STEP)
        await hass.async_block_till_done()
        if condition():
            return
        if time.monotonic() >= deadline:
            pytest.fail(f"timed out waiting for {description}")


async def _quiesce(hass: HomeAssistant) -> None:
    """Let the loop run long enough for anything pending to have happened."""
    for _ in range(QUIET_TURNS):
        await asyncio.sleep(SETTLE_STEP)
        await hass.async_block_till_done()


async def _fire_in(hass: HomeAssistant, seconds: float) -> None:
    """Fire every Home Assistant timer due within ``seconds`` and settle.

    The wall clock is left alone (see the module docstring); this only releases
    timers that would have come due.
    """
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def _push_device(harness: LiveHarness, detail: dict[str, Any]) -> None:
    """Publish a device view on the fast coordinator and settle its listeners."""
    harness.fast.async_set_updated_data(Device.from_dict(detail))
    await harness.hass.async_block_till_done()


async def _push_result(harness: LiveHarness, result: AnalyticsResult) -> None:
    """Publish an analytics verdict on the engine and settle its listeners."""
    harness.engine.async_set_updated_data(result)
    await harness.hass.async_block_till_done()


async def _wait_live(harness: LiveHarness) -> None:
    """Wait until a granted session is streaming."""
    await _settle_until(
        harness.hass,
        lambda: harness.manager.state.status == LIVE_STATUS_LIVE,
        "the live session to start",
    )


async def _wait_idle(harness: LiveHarness) -> None:
    """Wait until no session is running."""
    await _settle_until(
        harness.hass,
        lambda: harness.manager.state.status == LIVE_STATUS_IDLE,
        "the live session to end",
    )


async def _end_session(harness: LiveHarness) -> None:
    """Close the streaming socket server-side and wait for the session to end."""
    await harness.server.close_connections()
    await _wait_idle(harness)


async def _renew_window(harness: LiveHarness, window: int) -> None:
    """End the current reporting window and wait for renewal number ``window``.

    Closing the socket server-side is how a reporting window ends here: the
    frame loop returns, the window is accounted, and a hold that still wants
    the socket reconnects on a fresh ticket.
    """
    await harness.server.close_connections()
    await _settle_until(
        harness.hass,
        lambda: harness.manager.state.windows_in_session >= window,
        f"the hold to open reporting window {window + 1}",
    )


def _quiet_stream(harness: LiveHarness) -> None:
    """Script the next connection to talk without any water moving.

    The connect snapshot alone: enough for the window to count as healthy,
    with nothing in it the no-flow brake reads as flow.
    """
    harness.server.script = [frame("app_active", True)]


def _flowing_stream(harness: LiveHarness, step: int) -> None:
    """Script the next connection to stream a counter that really moved.

    ``step`` makes the value distinct per window: a frame repeating a value the
    device view (or the coalescing buffer) already carries is dropped before it
    can count as flow, exactly as a connect snapshot's repeats are.
    """
    harness.server.script = [
        frame("app_active", True),
        frame("water_counter_gals", FIXTURE_WATER_COUNTER + step),
    ]


async def _run_regen_burst(harness: LiveHarness) -> None:
    """Run one full event-burst session by toggling the recharge tile.

    The tile goes back to ready afterwards, so the next call is another
    ``False`` -> ``True`` transition and therefore another grant.
    """
    await _push_device(harness, _detail(tile_state=REGENERATING))
    await _wait_live(harness)
    await _end_session(harness)
    await _push_device(harness, _detail())


async def _allow_back_to_back_grants(
    monkeypatch: pytest.MonkeyPatch, harness: LiveHarness
) -> None:
    """Remove the minimum-gap floor so several grants fit in one test.

    The shipped floor is one minute of wall-clock time, which a test that must
    not freeze the clock cannot wait out. The gap rule itself is asserted
    separately in :func:`test_minimum_gap_denies_the_next_trigger`.
    """
    monkeypatch.setattr(
        "custom_components.aquahome.live_state.LIVE_MIN_GAP_SECONDS_MIN", 0.0
    )
    await harness.manager.async_set_min_gap(0.0)


def _refuse_renewal_tickets(
    monkeypatch: pytest.MonkeyPatch, harness: LiveHarness, refusals: int
) -> None:
    """Make the next ``refusals`` ticket requests after the grant's own fail.

    Models the client's account-wide live-ticket floor refusing a *renewal*,
    which is what two devices renewing their held blocks in the same minute do
    to each other. The session's first ticket is always served, so what the
    manager faces is a healthy session whose next window cannot get a ticket;
    every request past the refusals runs the real client call against the fake
    server, so a retry that succeeds really does reconnect.
    """
    real = harness.client.async_get_live_ticket
    requests = count()

    async def _maybe_refuse(
        device_id: str,
        properties: Iterable[str],
        *,
        type_: str = "property",
        ignore_throttle: bool = False,
    ) -> LiveTicket:
        """Raise the client's own throttle error, then defer to the real call."""
        if 0 < next(requests) <= refusals:
            msg = "Live-ticket requests are throttled; try again shortly"
            raise RateLimitError(msg)
        return await real(
            device_id, properties, type_=type_, ignore_throttle=ignore_throttle
        )

    monkeypatch.setattr(harness.client, "async_get_live_ticket", _maybe_refuse)


def _denials(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    """Return ``(source, reason)`` for every grant the manager refused."""
    refused: list[tuple[str, str]] = []
    for record in caplog.records:
        if record.msg != DENIAL_MESSAGE:
            continue
        args = record.args
        assert isinstance(args, tuple)
        refused.append((str(args[1]), str(args[2])))
    return refused


def _issue(hass: HomeAssistant) -> ir.IssueEntry | None:
    """Return the live-mode repair issue for the fixture device, if filed."""
    return ir.async_get(hass).issues.get((DOMAIN, f"{LIVE_FAILING_ISSUE_PREFIX}{SLUG}"))


async def _wait_for_failures(
    hass: HomeAssistant, manager: AquaHomeLiveManager, count: int
) -> None:
    """Wait until the manager has recorded ``count`` consecutive failures."""
    await _settle_until(
        hass,
        lambda: manager.state.consecutive_failures >= count,
        f"live session failure #{count}",
    )


async def _drive_ticket_failures(harness: LiveHarness, count: int) -> list[float]:
    """Fail ``count`` consecutive session attempts and return their backoffs.

    The continuous hold is what keeps re-requesting: every backoff expiry
    resumes it, so seeding the ticket endpoint with failures produces one
    attempt per fired timer. Each returned value is the announced backoff
    measured from just before the attempt, so it is the delay plus the
    milliseconds the attempt itself took.
    """
    hass = harness.hass
    manager = harness.manager
    delays: list[float] = []
    for number in range(1, count + 1):
        before = dt_util.utcnow()
        if number == 1:
            await manager.async_set_continuous(True)
        else:
            await _fire_in(hass, delays[-1] + 1.0)
        await _wait_for_failures(hass, manager, number)
        backoff_until = manager.state.backoff_until
        assert backoff_until is not None
        delays.append((backoff_until - before).total_seconds())
    return delays


# ---------------------------------------------------------------------------
# The grant gate: every deny reason, in the order it is reported
# ---------------------------------------------------------------------------


async def test_a_second_trigger_while_streaming_is_denied(
    build_live: Callable[..., Awaitable[LiveHarness]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One poll that trips two triggers opens exactly one socket."""
    harness = await build_live()

    # A regeneration start and a jump in the today-counter in the same payload:
    # the burst is granted, and the active-use trigger behind it finds a session
    # already running.
    await _push_device(
        harness,
        _detail(
            tile_state=REGENERATING,
            gallons=FIXTURE_GALLONS_TODAY + ACTIVE_USE_RISE,
        ),
    )
    await _wait_live(harness)
    await _quiesce(harness.hass)

    assert harness.manager.state.source == LIVE_SOURCE_REGEN
    assert harness.manager.state.sessions_today == 1
    assert len(harness.server.all_connections) == 1
    assert _denials(caplog) == [(LIVE_SOURCE_ACTIVE_USE, DENIED_ACTIVE)]


async def test_a_trigger_during_the_failure_backoff_is_denied(
    build_live: Callable[..., Awaitable[LiveHarness]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """While the manager is backing off, no trigger may reopen a socket."""
    harness = await build_live()
    harness.server.live_status_overrides.append(HTTPStatus.INTERNAL_SERVER_ERROR)

    await _push_device(harness, _detail(tile_state=REGENERATING))
    await _settle_until(
        harness.hass,
        lambda: harness.manager.state.status == LIVE_STATUS_BACKOFF,
        "the failed attempt to arm the backoff",
    )
    caplog.clear()
    await _push_device(
        harness,
        _detail(
            tile_state=REGENERATING,
            gallons=FIXTURE_GALLONS_TODAY + ACTIVE_USE_RISE,
        ),
    )
    await _quiesce(harness.hass)

    assert _denials(caplog) == [(LIVE_SOURCE_ACTIVE_USE, DENIED_BACKOFF)]
    assert harness.server.all_connections == []


async def test_a_trigger_while_the_rest_domain_is_throttled_is_denied(
    build_live: Callable[..., Awaitable[LiveHarness]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A throttled account gets no live sessions, separate budget or not."""
    harness = await build_live()

    with patch.object(AquaHomeClient, "rest_backoff_active", True):
        await _push_device(harness, _detail(tile_state=REGENERATING))
        await _quiesce(harness.hass)

    assert _denials(caplog) == [(LIVE_SOURCE_REGEN, DENIED_REST_BACKOFF)]
    assert harness.server.live_requests == []
    assert harness.server.all_connections == []


async def test_a_trigger_while_the_device_is_offline_is_denied(
    build_live: Callable[..., Awaitable[LiveHarness]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An offline device has nothing to stream."""
    harness = await build_live()

    await _push_device(harness, _detail(tile_state=REGENERATING, online=False))
    await _quiesce(harness.hass)

    assert _denials(caplog) == [(LIVE_SOURCE_REGEN, DENIED_OFFLINE)]
    assert harness.server.all_connections == []


async def test_the_daily_grant_budget_denies_further_triggers(
    build_live: Callable[..., Awaitable[LiveHarness]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Once the day's grants are spent, every trigger is refused until it turns."""
    harness = await build_live()
    await _allow_back_to_back_grants(monkeypatch, harness)
    await harness.manager.async_set_sessions_per_day(LIVE_SESSIONS_PER_DAY_MIN)

    for _ in range(LIVE_SESSIONS_PER_DAY_MIN):
        await _run_regen_burst(harness)

    assert harness.manager.state.sessions_today == LIVE_SESSIONS_PER_DAY_MIN
    caplog.clear()
    await _push_device(harness, _detail(tile_state=REGENERATING))
    await _quiesce(harness.hass)

    assert _denials(caplog) == [(LIVE_SOURCE_REGEN, DENIED_BUDGET)]
    assert len(harness.server.all_connections) == LIVE_SESSIONS_PER_DAY_MIN


async def test_minimum_gap_denies_the_next_trigger(
    build_live: Callable[..., Awaitable[LiveHarness]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A second grant inside the configured gap is refused after a clean end."""
    harness = await build_live()

    await _push_device(harness, _detail(tile_state=REGENERATING))
    await _wait_live(harness)
    await _end_session(harness)
    assert harness.manager.state.last_session_end is not None
    assert harness.manager.state.config.min_gap_seconds >= LIVE_MIN_GAP_SECONDS_MIN

    caplog.clear()
    await _push_device(
        harness,
        _detail(
            tile_state=REGENERATING,
            gallons=FIXTURE_GALLONS_TODAY + ACTIVE_USE_RISE,
        ),
    )
    await _quiesce(harness.hass)

    assert _denials(caplog) == [(LIVE_SOURCE_ACTIVE_USE, DENIED_GAP)]
    assert len(harness.server.all_connections) == 1


async def test_the_active_use_cooldown_denies_only_that_trigger(
    build_live: Callable[..., Awaitable[LiveHarness]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Routine household use cannot drain the budget, but a burst still passes."""
    harness = await build_live()
    await _allow_back_to_back_grants(monkeypatch, harness)

    used = FIXTURE_GALLONS_TODAY + ACTIVE_USE_RISE
    await _push_device(harness, _detail(gallons=used))
    await _wait_live(harness)
    assert harness.manager.state.source == LIVE_SOURCE_ACTIVE_USE
    await _end_session(harness)

    caplog.clear()
    used += ACTIVE_USE_RISE
    await _push_device(harness, _detail(gallons=used))
    await _quiesce(harness.hass)
    assert _denials(caplog) == [(LIVE_SOURCE_ACTIVE_USE, DENIED_COOLDOWN)]
    assert len(harness.server.all_connections) == 1

    # The gate is open for everything else: only the active-use trigger cools.
    await _push_device(harness, _detail(gallons=used, tile_state=REGENERATING))
    await _wait_live(harness)
    assert harness.manager.state.source == LIVE_SOURCE_REGEN
    assert len(harness.server.all_connections) == 2


async def test_the_grant_gate_reports_the_first_unmet_condition(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """Stacked denials report the earliest condition in the documented order.

    The deny reasons are live mode's whole observability surface, so their
    order is part of the contract rather than an accident of the code: a gate
    that answered "the device is offline" while the account was also being
    throttled would send the owner after the wrong problem, and one that
    answered "budget spent" during a failure backoff would have them raise the
    daily limit for nothing. Every condition below stays true while the ones
    above it are peeled away, so each assertion proves a *shadowing* and not
    merely that the reason is reachable.
    """
    night_zone, _ = _zone_for_local_hour(NIGHT_HOUR)
    harness = await build_live(detail=_detail(tz_id=night_zone, online=False))
    manager = harness.manager
    now = dt_util.utcnow()
    seeded = manager.state
    manager._publish(
        replace(
            seeded,
            backoff_until=now + timedelta(minutes=5),
            sessions_today=seeded.config.sessions_per_day,
            last_session_end=now,
            smart_suspended_until=dt_util.utcnow() + timedelta(hours=1),
        )
    )

    with patch.object(AquaHomeClient, "rest_backoff_active", True):
        with patch.object(AquaHomeLiveManager, "_session_running", True):
            assert manager._can_grant(LIVE_SOURCE_SMART, now) == DENIED_ACTIVE
        assert manager._can_grant(LIVE_SOURCE_SMART, now) == DENIED_BACKOFF
        manager._publish(replace(manager.state, backoff_until=None))
        assert manager._can_grant(LIVE_SOURCE_SMART, now) == DENIED_REST_BACKOFF
    assert manager._can_grant(LIVE_SOURCE_SMART, now) == DENIED_OFFLINE

    await _push_device(harness, _detail(tz_id=night_zone))
    assert manager._can_grant(LIVE_SOURCE_SMART, now) == DENIED_BUDGET
    manager._publish(replace(manager.state, sessions_today=0))
    assert manager._can_grant(LIVE_SOURCE_SMART, now) == DENIED_GAP

    # The gap shadows the active-use cooldown for as long as it lasts; past it
    # the cooldown governs that one trigger on its own.
    manager._last_active_use = now
    assert manager._can_grant(LIVE_SOURCE_ACTIVE_USE, now) == DENIED_GAP
    manager._publish(replace(manager.state, last_session_end=None))
    assert manager._can_grant(LIVE_SOURCE_ACTIVE_USE, now) == DENIED_COOLDOWN

    assert manager._can_grant(LIVE_SOURCE_SMART, now) == DENIED_SUSPENDED
    manager._publish(replace(manager.state, smart_suspended_until=None))
    assert manager._can_grant(LIVE_SOURCE_SMART, now) == DENIED_NIGHT

    # Every shared condition is clear, and the per-source rules do not touch a
    # burst: the night the smart tier is refused is when a burst matters most.
    assert manager._can_grant(LIVE_SOURCE_REGEN, now) is None


# ---------------------------------------------------------------------------
# Budget accounting and holds
# ---------------------------------------------------------------------------


async def test_renewals_spend_tickets_but_never_grants(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """A held session reconnects window after window on one grant."""
    harness = await build_live()

    await harness.manager.async_set_live_view(True)
    await _wait_live(harness)
    assert harness.manager.state.source == LIVE_SOURCE_MANUAL

    await harness.server.close_connections()
    await _settle_until(
        harness.hass,
        lambda: harness.manager.state.windows_in_session >= 1,
        "the hold to reconnect for a second window",
    )

    state = harness.manager.state
    assert state.status == LIVE_STATUS_LIVE
    assert state.windows_in_session == 1
    assert state.sessions_today == 1
    assert len(harness.server.live_requests) == 2

    await harness.manager.async_set_live_view(False)
    await _wait_idle(harness)
    end_state = harness.manager.state
    assert end_state.live_view is False
    assert end_state.sessions_today == 1
    assert end_state.last_session_end is not None
    assert harness.server.connections == []


async def test_the_manual_hold_switches_itself_off_at_its_cap(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """A forgotten Live view stops on its own half an hour in."""
    # A device advertising a long reporting window keeps the client-side window
    # timer out of the way, so firing the cap fires nothing else.
    harness = await build_live(detail=_detail(window_minutes=LONG_WINDOW_MINUTES))

    await harness.manager.async_set_live_view(True)
    await _wait_live(harness)

    await _fire_in(harness.hass, LIVE_VIEW_HOLD_MAX_SECONDS + 1.0)
    await _wait_idle(harness)

    state = harness.manager.state
    assert state.live_view is False
    assert state.sessions_today == 1
    assert len(harness.server.all_connections) == 1
    assert harness.server.connections == []


async def test_the_continuous_hold_renews_and_stops_when_the_device_drops(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """Continuous mode reconnects for as long as the device is reachable."""
    harness = await build_live()

    await harness.manager.async_set_continuous(True)
    await _wait_live(harness)
    assert harness.manager.state.source == LIVE_SOURCE_CONTINUOUS

    await harness.server.close_connections()
    await _settle_until(
        harness.hass,
        lambda: harness.manager.state.windows_in_session >= 1,
        "continuous mode to reconnect",
    )
    assert harness.manager.state.windows_in_session == 1

    # The device drops out; the next window end must not be renewed.
    await _push_device(harness, _detail(online=False))
    await _end_session(harness)

    state = harness.manager.state
    assert state.config.continuous is True
    assert state.sessions_today == 1
    assert len(harness.server.all_connections) == 2


async def test_a_hold_resumes_once_its_failure_backoff_expires(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """A hold outlives the attempt that failed and reconnects by itself."""
    harness = await build_live()
    harness.server.live_status_overrides.append(HTTPStatus.INTERNAL_SERVER_ERROR)

    await harness.manager.async_set_continuous(True)
    await _settle_until(
        harness.hass,
        lambda: harness.manager.state.status == LIVE_STATUS_BACKOFF,
        "the first attempt to fail",
    )
    assert harness.manager.state.consecutive_failures == 1
    assert harness.server.all_connections == []

    await _fire_in(harness.hass, LIVE_BACKOFF_INITIAL_SECONDS + 1.0)
    await _wait_live(harness)

    state = harness.manager.state
    assert state.source == LIVE_SOURCE_CONTINUOUS
    assert state.consecutive_failures == 0
    assert state.backoff_until is None
    assert state.last_error is None
    assert len(harness.server.all_connections) == 1


# ---------------------------------------------------------------------------
# Host-specific window semantics
# ---------------------------------------------------------------------------


async def test_the_legacy_host_ends_an_on_demand_session_at_the_window(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """Without a hold, app_active=false is simply the window closing."""
    harness = await build_live()

    await _push_device(harness, _detail(tile_state=REGENERATING))
    await _wait_live(harness)
    await harness.server.push(frame("app_active", value=False))
    await _wait_idle(harness)

    assert len(harness.server.all_connections) == 1
    assert len(harness.server.live_requests) == 1
    assert harness.manager.state.sessions_today == 1


async def test_the_newer_host_reconnects_when_the_window_closes(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """On the newer host app_active=false is a reconnect request."""
    harness = await build_live(iqua2=True)

    await _push_device(harness, _detail(tile_state=REGENERATING))
    await _wait_live(harness)
    await harness.server.push(frame("app_active", value=False))
    await _settle_until(
        harness.hass,
        lambda: harness.manager.state.windows_in_session >= 1,
        "the session to reconnect on the newer host",
    )

    state = harness.manager.state
    assert state.status == LIVE_STATUS_LIVE
    assert state.windows_in_session == 1
    assert state.sessions_today == 1
    assert len(harness.server.live_requests) == 2

    # A server-side close is not a reconnect request: the session ends there.
    await _end_session(harness)
    assert len(harness.server.all_connections) == 2


# ---------------------------------------------------------------------------
# Poll-driven triggers
# ---------------------------------------------------------------------------


async def test_active_use_opens_a_session_only_past_the_delta(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """A trickle is ignored; a real draw is streamed."""
    harness = await build_live()

    below = FIXTURE_GALLONS_TODAY + int(LIVE_ACTIVE_USE_DELTA_GALLONS) - 1
    await _push_device(harness, _detail(gallons=below))
    await _quiesce(harness.hass)
    assert harness.server.all_connections == []

    await _push_device(harness, _detail(gallons=below + ACTIVE_USE_RISE))
    await _wait_live(harness)

    assert harness.manager.state.source == LIVE_SOURCE_ACTIVE_USE
    assert len(harness.server.all_connections) == 1


async def test_a_stale_re_serve_is_never_diffed_against_itself(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """A repeated payload is skipped whole, baseline included."""
    harness = await build_live()

    used = FIXTURE_GALLONS_TODAY + ACTIVE_USE_RISE
    with patch.object(AquaHomeCoordinator, "serving_stale", True):
        await _push_device(harness, _detail(gallons=used))
        await _quiesce(harness.hass)
        assert harness.server.all_connections == []

    # The stale pass left the baseline untouched, so the very same reading now
    # reads as the rise it always was.
    await _push_device(harness, _detail(gallons=used))
    await _wait_live(harness)
    assert harness.manager.state.source == LIVE_SOURCE_ACTIVE_USE


async def test_a_regeneration_burst_opens_on_the_start_transition_only(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """A recharge already running is not news; the one that starts is."""
    harness = await build_live(detail=_detail(tile_state=REGENERATING))

    # Already regenerating at start-up, and still regenerating on the next poll.
    await _push_device(harness, _detail(tile_state=REGENERATING))
    await _quiesce(harness.hass)
    assert harness.server.all_connections == []

    await _push_device(harness, _detail())
    await _push_device(harness, _detail(tile_state=REGENERATING))
    await _wait_live(harness)

    assert harness.manager.state.source == LIVE_SOURCE_REGEN
    assert len(harness.server.all_connections) == 1


async def test_a_regeneration_burst_needs_a_previous_state_to_compare(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """A device that reports no recharge state at all is never compared."""
    harness = await build_live(detail=_detail(enriched=False))

    await _push_device(harness, _detail(tile_state=REGENERATING))
    await _quiesce(harness.hass)

    assert harness.server.all_connections == []
    assert harness.manager.state.sessions_today == 0


async def test_a_device_local_day_rollover_resets_the_daily_counters(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """The budget and its cooldowns are keyed to the device's own day."""
    harness = await build_live(detail=_detail(tz_id=TZ_FAR_EAST))

    await _push_device(harness, _detail(tz_id=TZ_FAR_EAST, tile_state=REGENERATING))
    await _wait_live(harness)
    await _end_session(harness)
    assert harness.manager.state.sessions_today == 1

    # The same instant, dated by a zone 26 h away, is a different local day.
    await _push_device(harness, _detail(tz_id=TZ_FAR_WEST, tile_state=REGENERATING))
    await _quiesce(harness.hass)

    assert harness.manager.state.sessions_today == 0


# ---------------------------------------------------------------------------
# Analytics-driven triggers
# ---------------------------------------------------------------------------


async def test_an_anomaly_verdict_turning_active_opens_a_burst(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """The confirmed-anomaly transition is streamed; staying active is not."""
    harness = await build_live(result=_result(anomaly_active=False))

    await _push_result(harness, _result(anomaly_active=True))
    await _wait_live(harness)
    assert harness.manager.state.source == LIVE_SOURCE_ANOMALY
    await _end_session(harness)

    await _push_result(harness, _result(anomaly_active=True))
    await _quiesce(harness.hass)
    assert len(harness.server.all_connections) == 1


async def test_smart_windows_are_armed_only_outside_the_night_hours(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """A grid whose only peak hours are night hours arms nothing."""
    zone, day_hour = _zone_for_local_hour(DAY_HOUR)
    harness = await build_live(
        detail=_detail(tz_id=zone), result=_result(peak_hours=NIGHT_ONLY_PEAKS)
    )

    await harness.manager.async_set_smart_windows(True)
    await _fire_in(harness.hass, timedelta(days=2).total_seconds())
    await _quiesce(harness.hass)
    assert harness.server.all_connections == []

    # The same grid with daytime peaks does arm the next one.
    await _push_result(harness, _result(peak_hours=_peaks_away_from(day_hour)))
    await _fire_in(harness.hass, SMART_ARM_HORIZON_SECONDS)
    await _wait_live(harness)

    assert harness.manager.state.source == LIVE_SOURCE_SMART


async def test_a_smart_window_due_at_night_is_denied(
    build_live: Callable[..., Awaitable[LiveHarness]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The smart tier never streams a sleeping household."""
    zone, _ = _zone_for_local_hour(NIGHT_HOUR)
    harness = await build_live(
        detail=_detail(tz_id=zone), result=_result(peak_hours=ALL_PEAK_HOURS)
    )

    await harness.manager.async_set_smart_windows(True)
    caplog.clear()
    await _fire_in(harness.hass, timedelta(hours=8).total_seconds())
    await _quiesce(harness.hass)

    assert _denials(caplog) == [(LIVE_SOURCE_SMART, DENIED_NIGHT)]
    assert harness.server.all_connections == []


async def test_three_no_flow_windows_suspend_the_tier_mid_block(
    build_live: Callable[..., Awaitable[LiveHarness]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A quiet house stops costing tickets after three windows, not three blocks.

    The no-flow brake counts consecutive quiet reporting *windows*, because a
    peak block holds its socket for the whole hour: counted per session — as it
    was before the block hold existed — an empty house would stream three
    entire blocks, some thirty-six tickets, before the tier stood down. The
    latch is published mid-session, which is exactly what lets the renewal
    decision taken one window later end the block it is holding, and it lasts
    until the device-local day turns.
    """
    zone, _ = _zone_for_local_hour(DAY_HOUR)
    harness = await build_live(
        detail=_detail(tz_id=zone), result=_result(peak_hours=ALL_PEAK_HOURS)
    )
    manager = harness.manager
    await _allow_back_to_back_grants(monkeypatch, harness)
    await manager.async_set_smart_windows(True)

    published: list[tuple[str, bool]] = []

    @callback
    def _record() -> None:
        """Record the session status each published state carried."""
        state = manager.data
        published.append((state.status, state.smart_suspended_until is not None))

    unsub = manager.async_add_listener(_record)
    try:
        await _fire_in(harness.hass, SMART_ARM_HORIZON_SECONDS)
        await _wait_live(harness)
        assert manager.state.source == LIVE_SOURCE_SMART

        # Every window talks — the connect snapshot arrives — but no counter
        # ever moves: nothing in this house is worth the socket.
        for window in range(1, LIVE_SMART_NO_FLOW_SUSPEND):
            await _renew_window(harness, window)
            assert manager.state.smart_suspended_until is None
        await _end_session(harness)
    finally:
        unsub()

    state = manager.state
    assert state.smart_suspended_until is not None
    assert manager._no_flow_windows == LIVE_SMART_NO_FLOW_SUSPEND
    assert state.consecutive_failures == 0
    assert state.sessions_today == 1
    # One grant, one window per connection, and the block let go on the third.
    assert len(harness.server.all_connections) == LIVE_SMART_NO_FLOW_SUSPEND
    # The latch reached the entities while the session was still streaming,
    # which is what the renewal decision on the next line reads.
    assert (LIVE_STATUS_LIVE, True) in published

    caplog.clear()
    await _fire_in(harness.hass, SMART_ARM_HORIZON_SECONDS)
    await _quiesce(harness.hass)
    assert _denials(caplog) == [(LIVE_SOURCE_SMART, DENIED_SUSPENDED)]
    assert len(harness.server.all_connections) == LIVE_SMART_NO_FLOW_SUSPEND

    # The suspension lasts exactly one device-local day. The two zones are 26 h
    # apart, so whatever day the streaming zone dated this instant to, at least
    # one of them dates it to another. The grid is emptied first so the day
    # turning is observed on its own rather than opening the next block.
    await _push_result(harness, _result(peak_hours=NO_PEAK_HOURS))
    await _push_device(harness, _detail(tz_id=TZ_FAR_WEST))
    await _push_device(harness, _detail(tz_id=TZ_FAR_EAST))
    await _quiesce(harness.hass)

    rolled = manager.state
    assert rolled.smart_suspended_until is None
    assert rolled.sessions_today == 0
    assert manager._no_flow_windows == 0


# ---------------------------------------------------------------------------
# Peak hours: what arms a smart window, and what holds it open
# ---------------------------------------------------------------------------


async def test_in_peak_hour_reads_the_learned_grid_in_the_devices_own_zone(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """Only the device's own local hour, on its own weekday, is a peak hour.

    The predicate the full-block hold renews on, so the three answers that must
    be ``False`` whatever the clock says matter most: a grid that does not
    carry all seven weekdays (the ``()`` "not computed" default), an engine
    that has published no verdict at all, and an hour some *other* weekday
    peaks in. Without any one of them an unknown grid would hold the socket
    open for as long as the tier is switched on — and arm windows off a grid
    that never ranked anything.
    """
    zone, _ = _zone_for_local_hour(DAY_HOUR)
    harness = await build_live(detail=_detail(tz_id=zone))
    manager = harness.manager
    now = dt_util.utcnow()
    local = now.astimezone(ZoneInfo(zone))

    # A computed grid that ranked no peaks anywhere.
    assert manager._in_peak_hour(now) is False

    await _push_result(
        harness, _result(peak_hours=_peaks_only(local.weekday(), local.hour))
    )
    assert manager._in_peak_hour(now) is True

    # The same hour of day, one weekday over.
    await _push_result(
        harness,
        _result(peak_hours=_peaks_only((local.weekday() + 1) % _WEEKDAYS, local.hour)),
    )
    assert manager._in_peak_hour(now) is False

    # The right weekday, the neighbouring hour.
    await _push_result(
        harness,
        _result(
            peak_hours=_peaks_only(local.weekday(), (local.hour + 1) % _HOURS_PER_DAY)
        ),
    )
    assert manager._in_peak_hour(now) is False

    # A short grid is not a grid: the shape guard refuses it, and with it any
    # attempt to arm a window off it.
    await _push_result(harness, _result(peak_hours=((local.hour,),) * (_WEEKDAYS - 1)))
    assert manager._in_peak_hour(now) is False
    await manager.async_set_smart_windows(True)
    assert manager._unsub_smart is None

    # An engine that has published nothing answers the same way.
    await _push_result(
        harness, _result(peak_hours=_peaks_only(local.weekday(), local.hour))
    )
    assert manager._in_peak_hour(now) is True
    with patch.object(harness.engine, "data", None):
        assert manager._in_peak_hour(now) is False
        manager._arm_smart_window(now)
        assert manager._unsub_smart is None


async def test_the_binary_activity_grid_alone_no_longer_arms_a_window(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """Arming keys on the learned peaks, not on "the household is awake".

    Pins the v1.1 switch away from ``active_hours``: on a real household that
    grid resolves to every hour from 07:00, so a tier armed on it fired all day
    for no information. A grid whose every hour is active but which ranks no
    peak must arm nothing at all; the peaks are what put a session where the
    water actually moves.
    """
    zone, day_hour = _zone_for_local_hour(DAY_HOUR)
    harness = await build_live(
        detail=_detail(tz_id=zone),
        result=_result(active_hours=ALL_ACTIVE_HOURS, peak_hours=NO_PEAK_HOURS),
    )

    await harness.manager.async_set_smart_windows(True)
    assert harness.manager._unsub_smart is None
    await _fire_in(harness.hass, timedelta(days=2).total_seconds())
    await _quiesce(harness.hass)
    assert harness.server.all_connections == []

    # The very same activity grid, now with peaks ranked, does arm one.
    await _push_result(
        harness,
        _result(active_hours=ALL_ACTIVE_HOURS, peak_hours=_peaks_away_from(day_hour)),
    )
    await _fire_in(harness.hass, SMART_ARM_HORIZON_SECONDS)
    await _wait_live(harness)

    assert harness.manager.state.source == LIVE_SOURCE_SMART


async def test_a_peak_hour_session_holds_across_its_whole_block(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """A smart session renews window after window while the peak hour lasts.

    The full-block hold. Before it, a smart window bought exactly one
    five-minute reporting window per grant, which is a twelfth of the block the
    tier arms on; the per-gallon, timestamped capture the whole feature exists
    for needs the socket held across the block. One grant, many windows, one
    ticket each — a renewal that spent a *grant* per window would exhaust the
    day's budget inside an hour. Every window here streams a counter that moved,
    so the per-window no-flow brake stays clear and the only thing that can end
    the hold is the hard-off pinned at the end: a hold that ignored it would
    reconnect against an unreachable device.
    """
    zone, _ = _zone_for_local_hour(DAY_HOUR)
    harness = await build_live(
        detail=_detail(tz_id=zone), result=_result(peak_hours=ALL_PEAK_HOURS)
    )
    manager = harness.manager
    _flowing_stream(harness, 1)
    await manager.async_set_smart_windows(True)
    await _fire_in(harness.hass, SMART_ARM_HORIZON_SECONDS)
    await _wait_live(harness)
    assert manager.state.source == LIVE_SOURCE_SMART

    _flowing_stream(harness, 2)
    await _renew_window(harness, 1)
    _flowing_stream(harness, 3)
    await _renew_window(harness, 2)

    state = manager.state
    assert state.status == LIVE_STATUS_LIVE
    assert state.windows_in_session >= 2
    assert state.sessions_today == 1
    assert len(harness.server.live_requests) == 3

    # The device drops out: the hold ends cleanly at the next window end.
    await _push_device(harness, _detail(tz_id=zone, online=False))
    await _end_session(harness)

    ended = manager.state
    assert ended.sessions_today == 1
    assert ended.last_session_end is not None
    assert ended.smart_suspended_until is None
    assert manager._no_flow_windows == 0


@pytest.mark.parametrize(
    "false_term", ["tier_off", "suspended_today", "outside_the_hour", "night"]
)
async def test_the_full_block_hold_requires_every_one_of_its_conditions(
    build_live: Callable[..., Awaitable[LiveHarness]],
    false_term: str,
) -> None:
    """Every term of the block predicate is load-bearing on its own.

    ``_smart_block_wanted`` is the single definition of "the peak tier wants
    this socket": the renewal decision, the release of a user hold and the
    resume after a lost session all consult it rather than re-deriving the
    terms, so a regression in any one of them surfaces only as a socket that
    will not let go — or one that lets go mid-block. Each case here leaves the
    other three conditions true and flips exactly one, including the in-hour
    term, which a table that only ever flips it together with the night rule (a
    night zone makes both false at once) would never test at all: an ``or``
    where the code means ``and`` survives every such table.
    """
    zone, day_hour = _zone_for_local_hour(DAY_HOUR)
    night_zone, _ = _zone_for_local_hour(NIGHT_HOUR)
    harness = await build_live(
        detail=_detail(tz_id=zone), result=_result(peak_hours=ALL_PEAK_HOURS)
    )
    manager = harness.manager
    await manager.async_set_smart_windows(True)
    manager._publish(
        replace(manager.state, status=LIVE_STATUS_LIVE, source=LIVE_SOURCE_SMART)
    )
    assert manager._smart_block_wanted(dt_util.utcnow()) is True
    assert manager._wants_renewal(WINDOW_ENDED_ON_TIMER) is True

    if false_term == "tier_off":
        # The tier's own switch, which is also its off switch mid-block.
        await manager.async_set_smart_windows(False)
    elif false_term == "suspended_today":
        # Suspended for the day by the no-flow brake.
        manager._publish(
            replace(
                manager.state,
                smart_suspended_until=dt_util.utcnow() + timedelta(hours=1),
            )
        )
    elif false_term == "outside_the_hour":
        # The block ran out: the grid still ranks peaks, just not this hour.
        await _push_result(harness, _result(peak_hours=_peaks_away_from(day_hour)))
    else:
        # The night rule outranks the grid: the same all-peak grid, read by a
        # device whose own clock says the small hours.
        await _push_device(harness, _detail(tz_id=night_zone))

    now = dt_util.utcnow()
    state = manager.state
    unmet = {
        "tier_off": not state.config.smart_windows,
        "suspended_today": manager._suspension_active(now),
        "outside_the_hour": not manager._in_peak_hour(now),
        "night": manager._is_night(now),
    }
    # The mutation isolated the term under test: the other three still hold,
    # so what follows can only be attributed to this one.
    assert [term for term, failed in unmet.items() if failed] == [false_term]
    assert manager._smart_block_wanted(now) is False
    # Nothing else holds this session, and the legacy host does not renew on
    # its own, so the block predicate alone decides the next window.
    assert manager._wants_renewal(WINDOW_ENDED_ON_TIMER) is False


@pytest.mark.parametrize(
    "source",
    [
        LIVE_SOURCE_SMART,
        LIVE_SOURCE_REGEN,
        LIVE_SOURCE_ANOMALY,
        LIVE_SOURCE_ACTIVE_USE,
    ],
)
async def test_a_wanted_block_renews_whatever_source_opened_the_session(
    build_live: Callable[..., Awaitable[LiveHarness]],
    source: str,
) -> None:
    """A burst streaming into a peak block is held for the rest of it.

    The block is a hold in its own right, so the renewal decision asks whether
    the *block* is wanted — not which trigger paid for the grant. A regeneration
    or active-use burst that happens to be streaming when the block begins is
    worth exactly as much per-gallon capture as a session the tier armed
    itself, and keyed to ``state.source`` instead the manager would drop it
    after one window and then be refused a fresh grant by the minimum-gap
    check, losing the block to the very trigger that noticed the water moving.
    """
    zone, day_hour = _zone_for_local_hour(DAY_HOUR)
    harness = await build_live(
        detail=_detail(tz_id=zone), result=_result(peak_hours=ALL_PEAK_HOURS)
    )
    manager = harness.manager
    await manager.async_set_smart_windows(True)
    manager._publish(replace(manager.state, status=LIVE_STATUS_LIVE, source=source))

    assert manager._wants_renewal(WINDOW_ENDED_ON_TIMER) is True

    # Outside the block every one of them is an on-demand session again.
    await _push_result(harness, _result(peak_hours=_peaks_away_from(day_hour)))
    assert manager._wants_renewal(WINDOW_ENDED_ON_TIMER) is False


async def test_the_newer_hosts_reconnect_request_never_holds_a_smart_session(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """On the newer host only a non-smart session renews on ``app_active=false``.

    That host asks for a reconnect by declaring the reporting window over
    instead of closing the socket, and an on-demand session obeys. A smart
    session must not: its block rule is the whole of its lifetime on both
    hosts, so honouring the host's request as well would keep a peak session
    reconnecting after its block ended — the one path by which a tier that is
    switched off, suspended or out of hour could still spend a ticket every
    reporting window.
    """
    harness = await build_live(iqua2=True, result=_result(peak_hours=NO_PEAK_HOURS))
    manager = harness.manager
    manager._publish(
        replace(manager.state, status=LIVE_STATUS_LIVE, source=LIVE_SOURCE_ANOMALY)
    )

    assert manager._wants_renewal(WINDOW_ENDED_ON_APP_INACTIVE) is True
    # Any other reason is the window simply ending, even on the newer host.
    assert manager._wants_renewal(WINDOW_ENDED_ON_TIMER) is False

    manager._publish(replace(manager.state, source=LIVE_SOURCE_SMART))
    assert manager._wants_renewal(WINDOW_ENDED_ON_APP_INACTIVE) is False


async def test_the_hard_offs_end_even_a_wanted_renewal(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """A held block still stands down on the conditions that stop live mode.

    Renewals skip the grant gate — the budget already paid for the session —
    so these three are the only thing between a wanted block and a reconnect
    loop against a sick cloud, a throttled account or a device that is not
    there. They are reported in their documented order.
    """
    zone, _ = _zone_for_local_hour(DAY_HOUR)
    harness = await build_live(
        detail=_detail(tz_id=zone), result=_result(peak_hours=ALL_PEAK_HOURS)
    )
    manager = harness.manager
    await manager.async_set_smart_windows(True)
    assert manager._wants_renewal(WINDOW_ENDED_ON_TIMER) is True

    assert manager._renewal_blocked() is None
    manager._publish(
        replace(manager.state, backoff_until=dt_util.utcnow() + timedelta(minutes=5))
    )
    assert manager._renewal_blocked() == DENIED_BACKOFF
    manager._publish(replace(manager.state, backoff_until=None))
    with patch.object(AquaHomeClient, "rest_backoff_active", True):
        assert manager._renewal_blocked() == DENIED_REST_BACKOFF
    await _push_device(harness, _detail(tz_id=zone, online=False))
    assert manager._renewal_blocked() == DENIED_OFFLINE


async def test_a_block_that_runs_out_declines_the_next_renewal(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """The held session ends cleanly within one window of its last peak hour.

    The renewal path's own end-of-block decision, driven through the real
    server rather than asserted on the predicate: the tier's entire ticket
    budget rests on a block that stops renewing once the learned grid stops
    ranking the hour it is in. A hold that read the grid once — when the window
    was armed — would keep the socket, and its ticket per reporting window,
    until the device dropped or the day rolled over, and nothing in the state
    the entities show would say so.
    """
    zone, day_hour = _zone_for_local_hour(DAY_HOUR)
    harness = await build_live(
        detail=_detail(tz_id=zone), result=_result(peak_hours=ALL_PEAK_HOURS)
    )
    manager = harness.manager
    await manager.async_set_smart_windows(True)
    await _fire_in(harness.hass, SMART_ARM_HORIZON_SECONDS)
    await _wait_live(harness)
    assert manager.state.source == LIVE_SOURCE_SMART

    _flowing_stream(harness, 1)
    await _renew_window(harness, 1)
    assert manager.state.status == LIVE_STATUS_LIVE

    # The block runs out. Nothing else changes: the socket is healthy, the
    # device is online and the tier is still switched on.
    await _push_result(harness, _result(peak_hours=_peaks_away_from(day_hour)))
    assert manager.state.status == LIVE_STATUS_LIVE
    assert len(harness.server.connections) == 1

    await _end_session(harness)
    await _quiesce(harness.hass)

    state = manager.state
    assert state.status == LIVE_STATUS_IDLE
    assert state.consecutive_failures == 0
    assert state.last_error is None
    assert state.last_session_end is not None
    # One grant, two windows, and no new session behind it.
    assert state.sessions_today == 1
    assert len(harness.server.live_requests) == 2
    assert len(harness.server.all_connections) == 2
    assert harness.server.connections == []


async def test_releasing_a_user_hold_mid_block_leaves_the_session_streaming(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """Live view off — by hand or at its cap — does not cost the tier its block.

    A peak block in progress holds the socket whoever opened it, so every
    release path has to ask whether the block still wants it before tearing a
    session down. The manual switch going off is the common case: the live
    dashboard flips it, and the auto-off cap flips it for a user who forgot —
    half an hour into a block the tier would then have to forfeit, because the
    grant gate's minimum gap refuses to reopen it. Continuous mode releases the
    same way.
    """
    # A device advertising a long reporting window keeps the client-side window
    # timer out of the way, so firing the manual hold's cap fires nothing else.
    zone, _ = _zone_for_local_hour(DAY_HOUR)
    harness = await build_live(
        detail=_detail(tz_id=zone, window_minutes=LONG_WINDOW_MINUTES),
        result=_result(peak_hours=ALL_PEAK_HOURS),
    )
    manager = harness.manager
    await manager.async_set_smart_windows(True)
    await manager.async_set_live_view(True)
    await _wait_live(harness)
    assert manager.state.source == LIVE_SOURCE_MANUAL

    # Switched off by hand, mid-block.
    await manager.async_set_live_view(False)
    await _quiesce(harness.hass)
    assert manager.state.status == LIVE_STATUS_LIVE
    assert len(harness.server.connections) == 1

    # Continuous mode taken back off the same socket.
    await manager.async_set_continuous(True)
    await manager.async_set_continuous(False)
    await _quiesce(harness.hass)
    assert manager.state.status == LIVE_STATUS_LIVE
    assert len(harness.server.connections) == 1

    # And the manual hold's auto-off cap, which flips the switch itself.
    await manager.async_set_live_view(True)
    await _fire_in(harness.hass, LIVE_VIEW_HOLD_MAX_SECONDS + 1.0)
    await _quiesce(harness.hass)

    state = manager.state
    assert state.live_view is False
    assert state.status == LIVE_STATUS_LIVE
    # Still the very first connection: nothing reconnected, nothing re-granted.
    assert state.sessions_today == 1
    assert len(harness.server.connections) == 1
    assert len(harness.server.all_connections) == 1


async def test_a_wanted_block_resumes_on_the_poll_after_losing_its_session(
    build_live: Callable[..., Awaitable[LiveHarness]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A block that lost its socket picks the rest of its hour up again.

    A peak block outlives the session carrying it exactly as a switched-on hold
    does: a device that blinks offline, a ticket collision or a failure backoff
    ends the session mid-block, and nothing re-arms the tier until the next
    hour. Without a resume the block is silently forfeited — the grid still
    says the water is moving, and the manager sits idle through it.
    """
    zone, _ = _zone_for_local_hour(DAY_HOUR)
    harness = await build_live(
        detail=_detail(tz_id=zone), result=_result(peak_hours=ALL_PEAK_HOURS)
    )
    manager = harness.manager
    await _allow_back_to_back_grants(monkeypatch, harness)
    await manager.async_set_smart_windows(True)
    await _fire_in(harness.hass, SMART_ARM_HORIZON_SECONDS)
    await _wait_live(harness)
    assert manager.state.source == LIVE_SOURCE_SMART

    # The device blinks offline: the hard-off ends the session at the window.
    await _push_device(harness, _detail(tz_id=zone, online=False))
    await _end_session(harness)
    assert manager.state.sessions_today == 1

    # The very next poll finds the block still wanted and no session running.
    await _push_device(harness, _detail(tz_id=zone))
    await _wait_live(harness)

    state = manager.state
    assert state.source == LIVE_SOURCE_SMART
    assert state.sessions_today == 2
    assert len(harness.server.all_connections) == 2


async def test_adjacent_peak_hours_renew_straight_through_the_boundary(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """A contiguous peak block is one hold, not one hold per hour.

    The renewal predicate is asked again every reporting window, so it has to
    answer ``True`` on both sides of a boundary the grid peaks either side of.
    A predicate keyed to the hour the window was armed in — or one that reset
    at :00 — would drop the socket at the boundary and lose the second hour to
    the grant gate's minimum-gap check, which is exactly why the hold is
    granted per block rather than per hour.
    """
    zone, _ = _zone_for_local_hour(DAY_HOUR)
    harness = await build_live(detail=_detail(tz_id=zone))
    manager = harness.manager
    now = dt_util.utcnow()
    local = now.astimezone(ZoneInfo(zone))
    next_hour = (local.hour + 1) % _HOURS_PER_DAY

    await _push_result(
        harness, _result(peak_hours=_peaks_only(local.weekday(), local.hour, next_hour))
    )
    assert manager._in_peak_hour(now) is True
    assert manager._in_peak_hour(now + timedelta(hours=1)) is True

    # With only this hour ranked, the same crossing ends the hold.
    await _push_result(
        harness, _result(peak_hours=_peaks_only(local.weekday(), local.hour))
    )
    assert manager._in_peak_hour(now) is True
    assert manager._in_peak_hour(now + timedelta(hours=1)) is False


async def test_switching_the_tier_off_ends_a_running_peak_hour_hold(
    build_live: Callable[..., Awaitable[LiveHarness]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The smart switch releases the socket its own tier is holding, mid-block.

    Both halves of the release. Without it a hold nothing else wants kept
    streaming — a ticket per reporting window — for the rest of the peak hour
    after the user switched the feature off, since the flag was only ever read
    when arming. With a manual or continuous hold on the same session, that
    hold outranks the flag and the socket stays: the switch releases the smart
    tier's claim, not everyone else's.
    """
    zone, _ = _zone_for_local_hour(DAY_HOUR)
    harness = await build_live(
        detail=_detail(tz_id=zone), result=_result(peak_hours=ALL_PEAK_HOURS)
    )
    manager = harness.manager
    await _allow_back_to_back_grants(monkeypatch, harness)

    await manager.async_set_smart_windows(True)
    await _fire_in(harness.hass, SMART_ARM_HORIZON_SECONDS)
    await _wait_live(harness)
    assert manager.state.source == LIVE_SOURCE_SMART

    await manager.async_set_smart_windows(False)
    await _wait_idle(harness)
    assert harness.server.connections == []
    assert manager.state.sessions_today == 1
    assert manager.state.last_session_end is not None

    # A second block, this time with continuous mode holding the same socket.
    await manager.async_set_smart_windows(True)
    await _fire_in(harness.hass, SMART_ARM_HORIZON_SECONDS)
    await _wait_live(harness)
    assert manager.state.source == LIVE_SOURCE_SMART
    await manager.async_set_continuous(True)

    await manager.async_set_smart_windows(False)
    await _quiesce(harness.hass)

    assert manager.state.status == LIVE_STATUS_LIVE
    assert len(harness.server.connections) == 1
    assert manager.state.sessions_today == 2


# ---------------------------------------------------------------------------
# Renewal pacing and ticket collisions
# ---------------------------------------------------------------------------


async def test_the_renewal_pacing_floors_a_windows_ticket_spend(
    build_live: Callable[..., Awaitable[LiveHarness]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A renewal waits out whatever is left of the /live bucket's refill.

    Renewal cadence *is* ticket cadence, and the reporting window it follows is
    the device's own advertised value — a number the cloud could shrink under
    us. The floor is what guarantees a held block can never spend tickets
    faster than the bucket refills, whatever the device says. Every other test
    in this module neutralises it (:func:`_instant_renewal_pacing`), which is
    precisely why it needs one that puts the shipped value back: a pacing call
    that slept the whole floor on every renewal — or none of it — would
    otherwise be invisible here, and only a live account would notice.
    """
    harness = await build_live()
    manager = harness.manager
    monkeypatch.setattr(
        "custom_components.aquahome.live.LIVE_RENEWAL_MIN_SECONDS",
        LIVE_RENEWAL_MIN_SECONDS,
    )
    slept: list[float] = []

    async def _record_sleep(delay: float) -> None:
        """Record the wait instead of performing it."""
        slept.append(delay)

    spent = 40.0
    with patch("custom_components.aquahome.live.asyncio.sleep", _record_sleep):
        await manager._async_pace_renewal(harness.hass.loop.time() - spent)
        short_window = list(slept)
        await manager._async_pace_renewal(
            harness.hass.loop.time() - LIVE_RENEWAL_MIN_SECONDS - 1.0
        )

    # Only the remainder is waited out: the window already served most of it.
    assert len(short_window) == 1
    assert short_window[0] == pytest.approx(LIVE_RENEWAL_MIN_SECONDS - spent, abs=1.0)
    # A window that already outran the floor waits for nothing at all.
    assert slept == short_window


async def test_a_renewal_rides_out_one_ticket_collision(
    build_live: Callable[..., Awaitable[LiveHarness]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A renewal refused a ticket by the floor waits it out and retries once.

    The client's live-ticket floor is account-wide while a block hold is
    per-device, so two devices renewing in the same minute refuse each other a
    ticket. That is not a failed session — the block is healthy and the socket
    was fine a second ago — and treating it as one would back the whole tier
    off for a minute and then lose the rest of the block to the minimum-gap
    check on the way back.
    """
    harness = await build_live()
    manager = harness.manager
    _refuse_renewal_tickets(monkeypatch, harness, 1)

    await manager.async_set_continuous(True)
    await _wait_live(harness)
    await _renew_window(harness, 1)

    state = manager.state
    assert state.status == LIVE_STATUS_LIVE
    assert state.consecutive_failures == 0
    assert state.last_error is None
    assert state.sessions_today == 1
    # The refused request never reached the cloud; the two that did are the
    # grant's own ticket and the retry's.
    assert len(harness.server.live_requests) == 2
    assert len(harness.server.all_connections) == 2


async def test_a_second_ticket_refusal_fails_the_renewal(
    build_live: Callable[..., Awaitable[LiveHarness]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A floor that will not clear is a real throttle and takes the failure path.

    One bounded wait is the whole allowance: a second refusal means the account
    really is throttled, and retrying past it would hammer the endpoint the
    floor exists to protect. It has to become a recorded failure with a
    backoff, from which the hold resumes, rather than a tight loop inside the
    session.
    """
    harness = await build_live()
    manager = harness.manager
    _refuse_renewal_tickets(monkeypatch, harness, 2)

    await manager.async_set_continuous(True)
    await _wait_live(harness)
    await harness.server.close_connections()
    await _settle_until(
        harness.hass,
        lambda: manager.state.status == LIVE_STATUS_BACKOFF,
        "the second ticket refusal to be recorded as a failure",
    )

    state = manager.state
    assert state.consecutive_failures == 1
    assert state.last_error is not None
    assert state.backoff_until is not None
    assert state.source is None
    # The grant's connection is the only one the cloud ever saw.
    assert len(harness.server.all_connections) == 1


# ---------------------------------------------------------------------------
# Handshake retry, backoff and the repair issue
# ---------------------------------------------------------------------------


async def test_an_expired_ticket_is_retried_once_with_a_fresh_one(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """A rejected handshake means a stale ticket, not a failed session."""
    harness = await build_live()
    harness.server.reject_next_handshakes = 1

    await _push_device(harness, _detail(tile_state=REGENERATING))
    await _wait_live(harness)

    state = harness.manager.state
    assert state.consecutive_failures == 0
    assert state.sessions_today == 1
    assert len(harness.server.live_requests) == 2
    assert len(harness.server.all_connections) == 1
    # The connection that succeeded presented the second, fresh ticket.
    assert harness.server.all_connections[0].ticket == "ticket-2"


async def test_the_expired_ticket_retry_ignores_the_client_ticket_floor(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """The retry gets its fresh ticket even a millisecond after the first.

    The dead path the v1.0.2 verification audit recorded: the rejected ticket
    is by definition seconds old, so the client's own 60 s live-ticket floor
    refused every retry and the session died with a misleading throttle error
    instead of reconnecting. Run against a monotonic clock that does not move
    between the two calls — production's own case — the retry must still
    succeed. On the pre-fix code this test fails with the attempt recorded as a
    failure and no session ever reaching the streaming state.
    """
    harness = await build_live(monotonic=_FixedMonotonic())
    harness.server.reject_next_handshakes = 1

    await _push_device(harness, _detail(tile_state=REGENERATING))
    await _wait_live(harness)

    state = harness.manager.state
    assert state.consecutive_failures == 0
    assert state.last_error is None
    assert len(harness.server.live_requests) == 2
    assert harness.server.all_connections[0].ticket == "ticket-2"


async def test_a_ticket_without_a_websocket_uri_is_a_failed_attempt(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """A ticket the cloud issued with no URI fails the attempt, not the task.

    There is nothing to connect to, so the manager has to record it like any
    other failed attempt and back off, rather than raise out of the session
    task or build a websocket URL out of an empty string and hammer the host
    with it.
    """
    harness = await build_live()

    with patch.object(
        AquaHomeClient,
        "async_get_live_ticket",
        AsyncMock(return_value=LiveTicket(websocket_uri=None)),
    ):
        await _push_device(harness, _detail(tile_state=REGENERATING))
        await _settle_until(
            harness.hass,
            lambda: harness.manager.state.status == LIVE_STATUS_BACKOFF,
            "the URI-less ticket to be recorded as a failed attempt",
        )

    state = harness.manager.state
    assert state.consecutive_failures == 1
    assert state.last_error is not None
    assert "websocket URI" in state.last_error
    assert harness.server.all_connections == []


async def test_repeated_failures_back_off_from_a_minute_to_the_cap(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """The reconnect delay doubles until it reaches the half-hour ceiling."""
    harness = await build_live()
    attempts = 7
    harness.server.live_status_overrides.extend(
        [HTTPStatus.INTERNAL_SERVER_ERROR] * attempts
    )

    delays = await _drive_ticket_failures(harness, attempts)

    expected = [60.0, 120.0, 240.0, 480.0, 960.0, 1800.0, 1800.0]
    assert expected[0] == LIVE_BACKOFF_INITIAL_SECONDS
    assert expected[-1] == LIVE_BACKOFF_MAX_SECONDS
    for announced, wanted in zip(delays, expected, strict=True):
        # The announced instant is the delay plus however long the attempt took.
        assert wanted <= announced < wanted + 1.0
    assert harness.manager.state.sessions_today == 0


async def test_a_run_of_failures_files_a_repair_issue_the_next_success_clears(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """Live mode reports itself broken only on a device that says it is online."""
    harness = await build_live()
    harness.server.live_status_overrides.extend(
        [HTTPStatus.INTERNAL_SERVER_ERROR] * LIVE_FAILURES_FOR_ISSUE
    )

    delays = await _drive_ticket_failures(harness, LIVE_FAILURES_FOR_ISSUE - 1)
    assert harness.fast.device_online is True
    assert _issue(harness.hass) is None

    # One more failure crosses the threshold.
    await _fire_in(harness.hass, delays[-1] + 1.0)
    await _wait_for_failures(harness.hass, harness.manager, LIVE_FAILURES_FOR_ISSUE)
    issue = _issue(harness.hass)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == "live_mode_failing"

    # The ticket endpoint recovers; the next successful connect withdraws it.
    await _fire_in(harness.hass, LIVE_BACKOFF_MAX_SECONDS + 1.0)
    await _wait_live(harness)

    assert harness.manager.state.consecutive_failures == 0
    assert _issue(harness.hass) is None


# ---------------------------------------------------------------------------
# Frame application
# ---------------------------------------------------------------------------


async def test_streamed_frames_reach_the_polled_device_in_one_update(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """A snapshot burst re-renders every bound entity exactly once."""
    harness = await build_live()
    harness.server.script = [
        frame("total_outlet_water_gals", 47_600),
        frame("water_counter_gals", 47_600),
        frame("current_water_flow_gpm", 24),
        frame("rf_signal_strength_dbm", -57),
    ]

    await _push_device(harness, _detail(tile_state=REGENERATING))
    await _wait_live(harness)

    republishes: list[None] = []

    @callback
    def _record() -> None:
        """Count every republish of the polled device view."""
        republishes.append(None)

    unsub = harness.fast.async_add_listener(_record)
    try:
        await _quiesce(harness.hass)
        assert republishes == []

        await _fire_in(harness.hass, LIVE_COALESCE_SECONDS + 0.5)
        await _quiesce(harness.hass)
    finally:
        unsub()

    assert len(republishes) == 1
    device = harness.fast.data
    assert device is not None
    assert device.properties["total_outlet_water_gals"].value == 47_600
    assert device.properties["water_counter_gals"].value == 47_600
    assert device.properties["current_water_flow_gpm"].value == 24
    assert device.properties["rf_signal_strength_dbm"].value == -57


async def test_unchanged_and_housekeeping_frames_are_never_applied(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """Only real movement in a bound property is worth an entity update."""
    harness = await build_live()
    stamp = "2030-01-01T00:00:00Z"
    harness.server.script = [
        # Repeated by the connect snapshot at the value the poll already carries.
        frame("water_counter_gals", FIXTURE_WATER_COUNTER, timestamp=stamp),
        # The liveness heartbeat and the window signal, neither of them state.
        frame("current_time_secs", FIXTURE_CLOCK_SECS + 10, timestamp=stamp),
        frame("app_active", value=True, timestamp=stamp),
        # One property that really moved, to prove the apply pass ran at all.
        frame("rf_signal_strength_dbm", FIXTURE_RF_DBM - 20, timestamp=stamp),
    ]

    await _push_device(harness, _detail(tile_state=REGENERATING))
    await _wait_live(harness)
    await _fire_in(harness.hass, LIVE_COALESCE_SECONDS + 0.5)
    await _quiesce(harness.hass)

    device = harness.fast.data
    assert device is not None
    applied = device.properties["rf_signal_strength_dbm"]
    assert applied.value == FIXTURE_RF_DBM - 20
    assert applied.updated_at is not None
    assert applied.updated_at.year == 2030
    # An unchanged value keeps the polled timestamp: it was never applied.
    unchanged = device.properties["water_counter_gals"]
    assert unchanged.value == FIXTURE_WATER_COUNTER
    assert unchanged.updated_at is not None
    assert unchanged.updated_at.year != 2030
    # The housekeeping properties keep their polled values entirely.
    assert device.properties["current_time_secs"].value == FIXTURE_CLOCK_SECS
    assert device.properties["app_active"].value is False


async def test_a_streamed_property_outside_the_allow_list_never_reaches_the_poll(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """Only the properties entity value paths bind are merged into the view.

    The stream is unvalidated input and the subscription list is wider than the
    bound set, so a frame naming anything else — here the device's water
    hardness, a property no entity reads from a push and no poll would refresh
    at stream cadence — must be dropped rather than written into the
    coordinator's device view, where it would rewrite entity state and stay
    wrong until the next genuine poll.
    """
    harness = await build_live()
    assert UNBOUND_PROPERTY not in LIVE_PUSHED_PROPERTIES
    harness.server.script = [
        frame("app_active", True),
        frame(UNBOUND_PROPERTY, FIXTURE_HARDNESS + 10),
        # The same bound value twice: the second finds the first already queued.
        frame("current_water_flow_gpm", 24),
        frame("current_water_flow_gpm", 24),
    ]

    await _push_device(harness, _detail(tile_state=REGENERATING))
    await _wait_live(harness)
    await _fire_in(harness.hass, LIVE_COALESCE_SECONDS + 0.5)
    await _quiesce(harness.hass)

    device = harness.fast.data
    assert device is not None
    assert device.properties["current_water_flow_gpm"].value == 24
    assert device.properties[UNBOUND_PROPERTY].value == FIXTURE_HARDNESS

    # A frame arriving before the first poll has no device view to merge into.
    with patch.object(harness.fast, "data", None):
        harness.manager._buffer_frame(
            LiveFrame(name="current_water_flow_gpm", value=99, timestamp=None)
        )
    assert harness.manager._pending == {}


async def test_a_live_push_never_advances_the_active_use_baseline(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """A push is no observation: the poll behind it still sees the whole rise.

    The audit gap this closes. The poll listener ran on live pushes too, so a
    streamed ``gallons_used_today`` frame moved the active-use trigger's
    baseline; the genuine poll that followed carried exactly that value and
    read as no usage at all, blinding the trigger for the rest of the day's
    draw. The push must leave the baseline — and every other poll-driven
    trigger — untouched.
    """
    harness = await build_live()
    used = FIXTURE_GALLONS_TODAY + ACTIVE_USE_RISE
    device = harness.fast.data
    assert device is not None

    properties = dict(device.properties)
    properties["gallons_used_today"] = replace(
        properties["gallons_used_today"], value=used
    )
    harness.fast.async_apply_live_update(replace(device, properties=properties))
    await _quiesce(harness.hass)

    # The push alone opens nothing: the counter moved on the stream, which is
    # not an observation the poll-driven triggers may act on.
    assert harness.server.all_connections == []

    # The genuine poll at that very same value is still the whole rise.
    await _push_device(harness, _detail(gallons=used))
    await _wait_live(harness)

    assert harness.manager.state.source == LIVE_SOURCE_ACTIVE_USE


async def test_the_streamed_window_length_sizes_the_next_window(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """The device's streamed reporting-window length outranks the polled one.

    The client-side timer is the only thing that ends a window the device
    stopped reporting in, so a stream that re-advertises the length has to
    re-size it. Nonsense must leave the previous value standing: a string or a
    zero taken at face value would collapse the window to nothing and cycle
    connect-and-close at the ticket floor.
    """
    harness = await build_live()
    manager = harness.manager
    harness.server.script = [
        frame("app_active", True),
        frame("app_active_timeout", "soon"),
        frame("app_active_timeout", 0),
        frame("app_active_timeout", STREAMED_WINDOW_MINUTES),
    ]

    await _push_device(harness, _detail(tile_state=REGENERATING))
    await _wait_live(harness)
    await _settle_until(
        harness.hass,
        lambda: manager._timeout_minutes == float(STREAMED_WINDOW_MINUTES),
        "the streamed reporting-window length to be recorded",
    )
    assert manager._window_delay() == (
        STREAMED_WINDOW_MINUTES * 60.0 + LIVE_WINDOW_GRACE_SECONDS
    )

    # A device that advertises no window length at all falls back instead.
    manager._timeout_minutes = None
    detail = _detail()
    del detail["properties"]["app_active_timeout"]
    await _push_device(harness, detail)
    assert manager._window_delay() == (
        LIVE_WINDOW_FALLBACK_SECONDS + LIVE_WINDOW_GRACE_SECONDS
    )


# ---------------------------------------------------------------------------
# Persistence and shutdown
# ---------------------------------------------------------------------------


async def test_the_live_configuration_round_trips_through_the_entry_options(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """What the switches and numbers set is what a restart reads back."""
    harness = await build_live()

    await harness.manager.async_set_smart_windows(True)
    await harness.manager.async_set_continuous(True)
    await harness.manager.async_set_sessions_per_day(LIVE_SESSIONS_PER_DAY_MAX + 50)
    await harness.manager.async_set_min_gap(LIVE_MIN_GAP_SECONDS_MIN - 30.0)

    stored = config_from_options(harness.entry, TEST_DEVICE_ID)
    assert stored == LiveConfig(
        smart_windows=True,
        continuous=True,
        sessions_per_day=LIVE_SESSIONS_PER_DAY_MAX,
        min_gap_seconds=LIVE_MIN_GAP_SECONDS_MIN,
    )
    assert harness.manager.state.config == stored


async def test_shutdown_closes_a_running_session(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """Unloading mid-session leaves neither an open socket nor a live timer."""
    harness = await build_live()

    await harness.manager.async_set_live_view(True)
    await _wait_live(harness)
    assert len(harness.server.connections) == 1

    await harness.manager.async_shutdown()
    await _settle_until(
        harness.hass,
        lambda: harness.server.connections == [],
        "the server to record the disconnect",
    )

    assert harness.server.closed_codes
    # Nothing re-arms behind the teardown: no timer opens another socket.
    await _fire_in(harness.hass, timedelta(hours=2).total_seconds())
    await _quiesce(harness.hass)
    assert len(harness.server.all_connections) == 1


async def test_frames_still_waiting_when_the_manager_stops_are_dropped(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """An unload drops the coalescing buffer instead of applying it.

    The buffer holds up to a coalescing window of streamed values. Applying
    them from a manager that is already down would republish the polled device
    view — re-rendering every bound entity — in the middle of an unload, and
    would do it from a socket nobody owns any more. The grant gate behind it
    stays shut for good, whichever path reaches it.
    """
    harness = await build_live()
    manager = harness.manager
    harness.server.script = [
        frame("app_active", True),
        frame("current_water_flow_gpm", FIXTURE_FLOW_GPM + 31),
    ]

    await _push_device(harness, _detail(tile_state=REGENERATING))
    await _wait_live(harness)
    await _settle_until(
        harness.hass,
        lambda: bool(manager._pending),
        "the streamed frame to be buffered for the coalesced apply",
    )

    await manager.async_shutdown()
    await _fire_in(harness.hass, LIVE_COALESCE_SECONDS + 0.5)
    await _quiesce(harness.hass)

    assert manager._pending == {}
    device = harness.fast.data
    assert device is not None
    assert device.properties["current_water_flow_gpm"].value == FIXTURE_FLOW_GPM

    # Every trigger funnels through the grant gate, which refuses outright.
    manager._request(LIVE_SOURCE_MANUAL)
    await _quiesce(harness.hass)
    assert len(harness.server.all_connections) == 1


async def test_removing_the_entry_deletes_every_devices_live_issue(
    hass: HomeAssistant,
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """Uninstalling clears the live-mode repair issues the entry filed.

    The Repairs registry outlives config entries, so a "live mode keeps
    failing" card left behind would nag about an integration that is gone. The
    hook runs on an entry that may never have been loaded, which is why the ids
    are rebuilt from the device registry rather than from a live manager.
    """
    harness = await build_live()
    dr.async_get(hass).async_get_or_create(
        config_entry_id=harness.entry.entry_id,
        identifiers={(DOMAIN, SLUG)},
    )
    harness.manager._file_issue("the cloud keeps refusing tickets")
    assert _issue(hass) is not None

    async_remove_live_issues(hass, harness.entry)

    assert _issue(hass) is None


async def test_updates_landing_after_shutdown_cannot_open_a_session(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """An update delivered mid-unload to a shut-down manager is inert.

    The fast and engine listeners are released with the config entry — after
    every consumer's own shutdown has run — so a poll or analytics pass
    completing during unload delivers one more update to a manager that is
    already down. Without the stop-flag guard that update passed the grant
    gate and spent a ticket on a fresh socket nothing would ever close.
    """
    harness = await build_live()

    await harness.manager.async_shutdown()

    # A regeneration-start transition and an every-hour-peak grid: both would
    # open a session on a running manager.
    await _push_device(harness, _detail(tile_state=REGENERATING))
    await _push_result(harness, _result(peak_hours=ALL_PEAK_HOURS))
    await _quiesce(harness.hass)

    assert harness.server.all_connections == []
    assert harness.manager.state.sessions_today == 0
    assert harness.manager.state.status == LIVE_STATUS_IDLE


# ---------------------------------------------------------------------------
# Stream evidence: frames, not handshakes, are what prove the cloud healthy
# ---------------------------------------------------------------------------


async def test_a_frameless_stream_is_a_failure_not_a_renewal(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """A window that never delivers a frame escalates instead of renewing.

    A server that accepts the handshake and drops the socket without sending
    anything would otherwise cycle connect-and-die at the ticket floor forever,
    with the backoff pinned at its minimum by each "successful" connect.
    """
    harness = await build_live()
    harness.server.script = []

    await harness.manager.async_set_continuous(True)
    await _wait_live(harness)
    await harness.server.close_connections()
    await _settle_until(
        harness.hass,
        lambda: harness.manager.state.status == LIVE_STATUS_BACKOFF,
        "the frameless stream to be recorded as a failure",
    )

    state = harness.manager.state
    assert state.consecutive_failures == 1
    assert state.last_error is not None
    assert "single frame" in state.last_error
    # No renewal hammer: the one connect is all the cloud saw.
    assert len(harness.server.all_connections) == 1


async def test_the_failure_trail_clears_on_a_frame_not_on_the_handshake(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """Only stream evidence withdraws the failure count.

    A recovery attempt that connects but stays mute keeps the trail growing;
    the first frame of a real stream zeroes it.
    """
    harness = await build_live()
    harness.server.live_status_overrides.append(HTTPStatus.INTERNAL_SERVER_ERROR)
    delays = await _drive_ticket_failures(harness, 1)

    # The ticket endpoint recovers, but the stream stays mute: the handshake
    # alone must not clear anything, and the mute window is failure number two.
    harness.server.script = []
    await _fire_in(harness.hass, delays[-1] + 1.0)
    await _wait_live(harness)
    await harness.server.close_connections()
    await _wait_for_failures(harness.hass, harness.manager, 2)

    # A stream that actually talks clears the trail with its first frame.
    harness.server.script = [frame("app_active", True)]
    backoff_until = harness.manager.state.backoff_until
    assert backoff_until is not None
    await _fire_in(
        harness.hass, (backoff_until - dt_util.utcnow()).total_seconds() + 1.0
    )
    await _wait_live(harness)
    await _settle_until(
        harness.hass,
        lambda: harness.manager.state.consecutive_failures == 0,
        "the first frame to clear the failure trail",
    )
    assert harness.manager.state.last_error is None


async def test_the_window_timer_ends_a_quiet_on_demand_session(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """A session nobody holds ends at the window timer, without a server close.

    The stream falls silent well before the socket closes, so the client-side
    timer is the only thing that ends a window the device stopped reporting in.
    """
    harness = await build_live(result=_result(anomaly_active=False))

    await _push_result(harness, _result(anomaly_active=True))
    await _wait_live(harness)
    assert len(harness.server.connections) == 1

    # Fire past the reporting window (five minutes plus grace); the timer
    # closes the socket, the frame loop returns, and no hold renews it.
    await _fire_in(harness.hass, 331.0)
    await _wait_idle(harness)
    assert harness.server.connections == []
    assert len(harness.server.all_connections) == 1


async def test_a_flow_window_resets_the_no_flow_window_count(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """One window that sees water restarts the brake's count, mid-block.

    Quiet, quiet, flow, quiet, quiet, quiet inside a single held block: the
    tier stands down only at the end of the run of three that *follows* the
    flow. Counted per day rather than consecutively, this block would have
    stood down at its fourth window — punishing a household that simply draws
    its water in bursts, which is the household a peak block is armed on in
    the first place.
    """
    zone, _ = _zone_for_local_hour(DAY_HOUR)
    harness = await build_live(
        detail=_detail(tz_id=zone), result=_result(peak_hours=ALL_PEAK_HOURS)
    )
    manager = harness.manager
    await manager.async_set_smart_windows(True)
    await _fire_in(harness.hass, SMART_ARM_HORIZON_SECONDS)
    await _wait_live(harness)
    assert manager.state.source == LIVE_SOURCE_SMART

    # Windows one and two carry the connect snapshot and nothing else.
    await _renew_window(harness, 1)
    # Window three streams a counter that really moved.
    _flowing_stream(harness, 1)
    await _renew_window(harness, 2)
    # Windows four onwards fall quiet again.
    _quiet_stream(harness)
    await _renew_window(harness, 3)
    await _renew_window(harness, 4)
    await _renew_window(harness, 5)

    holding = manager.state
    assert holding.status == LIVE_STATUS_LIVE
    assert holding.smart_suspended_until is None
    # Five windows accounted and four of them dry — but only two in a row.
    assert manager._no_flow_windows == LIVE_SMART_NO_FLOW_SUSPEND - 1

    await _end_session(harness)

    assert manager.state.smart_suspended_until is not None
    assert manager.state.sessions_today == 1
    # One grant, one connection per window: quiet, quiet, flow, quiet, quiet,
    # quiet, and the sixth is where the run of three completes.
    assert len(harness.server.all_connections) == 6


async def test_the_no_flow_brake_stands_down_one_block_not_the_day(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """A quiet block forfeits itself; a later block starts fresh.

    Under the day-long latch this shipped with, a quiet 17:00 block silently
    skipped the household's real evening block — the first production day did
    exactly that. The stand-down now ends with the contiguous block: the walk
    stops at the first non-peak (or night) hour, so once that instant passes,
    the tier is free again the same day.
    """
    zone, _hour = _zone_for_local_hour(DAY_HOUR)
    harness = await build_live(
        detail=_detail(tz_id=zone), result=_result(peak_hours=ALL_PEAK_HOURS)
    )
    manager = harness.manager
    await manager.async_set_smart_windows(True)

    now = dt_util.utcnow()
    # An all-peak grid walks to the night wall, never a full day ahead.
    end = manager._block_end(now)
    assert now < end <= now + timedelta(hours=25)

    manager._publish(replace(manager.state, smart_suspended_until=end))
    assert manager._suspension_active(now) is True
    assert manager._smart_block_wanted(now) is False
    # The instant the block runs out, the same grid grants again.
    assert manager._suspension_active(end) is False
    assert manager._smart_block_wanted(end + timedelta(seconds=1)) is (
        manager._in_peak_hour(end + timedelta(seconds=1))
        and not manager._is_night(end + timedelta(seconds=1))
    )
    # The day rollover clears any leftover stamp outright.
    manager._publish(
        replace(manager.state, smart_suspended_until=now + timedelta(hours=9))
    )
    manager._day = None
    manager._roll_day(now)
    assert manager.state.smart_suspended_until is None


async def test_a_quiet_burst_held_by_the_block_counts_against_the_brake(
    build_live: Callable[..., Awaitable[LiveHarness]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A burst whose renewals the block pays for is the tier's spend to brake.

    The block hold is source-agnostic, so a regeneration burst that opens at
    the top of a peak hour renews through the whole block. Keyed to the smart
    source alone — the shipped v1.1 shape — the brake would never count those
    windows, and a quiet house could stream a burst-opened block for hours.
    The user holds stay exempt: manual and continuous are explicit choices.
    """
    zone, _ = _zone_for_local_hour(DAY_HOUR)
    harness = await build_live(
        detail=_detail(tz_id=zone), result=_result(peak_hours=ALL_PEAK_HOURS)
    )
    manager = harness.manager
    await _allow_back_to_back_grants(monkeypatch, harness)
    await manager.async_set_smart_windows(True)

    # A regeneration start opens the session; the wanted block renews it.
    await _push_device(harness, _detail(tz_id=zone, tile_state=REGENERATING))
    await _wait_live(harness)
    assert manager.state.source == LIVE_SOURCE_REGEN

    for window in range(1, LIVE_SMART_NO_FLOW_SUSPEND):
        await _renew_window(harness, window)
        assert manager.state.smart_suspended_until is None
    await _end_session(harness)

    state = manager.state
    assert state.smart_suspended_until is not None
    assert state.sessions_today == 1
    assert len(harness.server.all_connections) == LIVE_SMART_NO_FLOW_SUSPEND


# ---------------------------------------------------------------------------
# Degenerate inputs: a payload that is missing, unusable, or repeated
# ---------------------------------------------------------------------------


async def test_a_manager_without_a_usable_device_view_reads_nothing_from_it(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """Missing and unusable payloads pace live mode instead of breaking it.

    Every input the manager reads — the recharge state, the today-counter, the
    advertised window length, the device's own zone — comes straight off a
    payload that may not exist yet (the first poll has not landed) or may carry
    a timezone this host cannot resolve. Each of those is one attribute access
    away from taking the evaluator down with it, and a manager with nothing to
    read must simply do nothing rather than fabricate a trigger or a window.

    The zone falls back to the *installation's*, not to UTC. The peak grid this
    tier holds its blocks on is learned in the analytics tier, whose own
    missing-``tz_id`` fallback is the installation zone; degrading to UTC here
    instead would read four sharp peak hours — and the night window — off by
    the household's whole UTC offset while the two tiers disagreed about what
    hour it is.
    """
    harness = await build_live()
    manager = harness.manager
    installation = dt_util.get_default_time_zone()
    # The harness runs on US/Pacific, so "the installation's zone" and "UTC"
    # are two distinguishable answers here.
    assert installation is not UTC

    with patch.object(harness.fast, "data", None):
        manager._evaluate_regen(None)
        manager._evaluate_active_use(None)
        assert _device_timezone(None, SLUG) is None
        assert manager._timezone() is installation
        assert manager._window_delay() == (
            LIVE_WINDOW_FALLBACK_SECONDS + LIVE_WINDOW_GRACE_SECONDS
        )
    await _quiesce(harness.hass)
    assert harness.server.all_connections == []

    # A zone no host can resolve, and one the device left blank: both pace the
    # daily counters, the night rule and the peak-hour predicate against the
    # installation's own clock rather than raising.
    await _push_device(harness, _detail(tz_id="Mars/Olympus_Mons"))
    assert _device_timezone(harness.fast.data, SLUG) is None
    assert manager._timezone() is installation
    await _push_device(harness, _detail(tz_id=""))
    assert manager._timezone() is installation


async def test_the_live_setters_ignore_a_repeat_of_the_state_they_hold(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """Setting a switch to the value it already holds changes nothing.

    Home Assistant calls a switch's turn-on service whether or not the entity
    is already on, so a setter that acted on the repeat would spend a second
    grant (Live view), churn the entry options on every call (the two config
    flags), and — worst — restart the auto-off timer that caps the manual hold,
    which is what makes a forgotten Live view stop by itself. The cap firing
    after the hold is already gone must likewise do nothing.
    """
    harness = await build_live()
    manager = harness.manager

    await manager.async_set_live_view(True)
    await _wait_live(harness)
    cap_timer = manager._unsub_view_cap
    assert cap_timer is not None

    await manager.async_set_live_view(True)
    await manager.async_set_smart_windows(False)
    await manager.async_set_continuous(False)
    await _quiesce(harness.hass)

    assert manager._unsub_view_cap is cap_timer
    assert manager.state.sessions_today == 1
    assert len(harness.server.all_connections) == 1

    await manager.async_set_live_view(False)
    await _wait_idle(harness)
    manager._handle_view_cap(dt_util.utcnow())
    await _quiesce(harness.hass)

    assert manager.state.sessions_today == 1
    assert harness.server.connections == []


async def test_a_socket_that_will_not_close_is_abandoned_quietly(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """A close that raises drops the socket instead of failing the session.

    Closing happens on the way out of every session and every renewal,
    including the teardown path that has already published the clean end. A
    close that raises — a socket the peer killed at the TCP level — must not
    turn that clean end into a recorded failure, nor leave the manager holding
    a session object it believes is still open.
    """
    harness = await build_live()
    manager = harness.manager
    manager._session = cast("AquaHomeLiveSession", _UnclosableSession())

    await manager._async_close_socket()

    assert manager.state.consecutive_failures == 0
    assert manager.state.last_error is None
    # Dropped, not retained: a session object left behind would make the grant
    # gate believe a socket is still open.
    assert manager._session is None
