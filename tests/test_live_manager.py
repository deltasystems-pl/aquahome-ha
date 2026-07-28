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
The one rule that genuinely needs wall-clock movement is the minimum gap
between grants; it is asserted on its own, and the handful of tests that need
several grants in a row lower its floor explicitly through
:func:`_allow_back_to_back_grants`.

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
are specified for; holds renew across reporting windows — the smart tier for
the whole learned peak hour it armed on — and stand down on the conditions that
mean live mode must stop; failures back off from one minute to the half-hour
cap and raise (then withdraw) a repair issue; and streamed frames reach the
polled device view coalesced, deduplicated, allow-listed and without the two
housekeeping properties, while never being mistaken for a poll.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from datetime import UTC, timedelta
from http import HTTPStatus
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
from custom_components.aquahome.api import Device
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
    async_remove_live_issues,
)
from custom_components.aquahome.live_state import LiveConfig, config_from_options
from custom_components.aquahome.statistics import AquaHomeStatisticsCoordinator
from tests.api.conftest import FAKE_NOW, FakeClock, make_jwt
from tests.conftest import TEST_DEVICE_ID, load_fixture
from tests.live_server import FakeIquaLiveServer, frame

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

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
#: Device-local hour used when a test needs "now" to be night ([01, 07)) or day;
#: both sit in the middle of their range, so an hour boundary crossing mid-test
#: cannot flip them.
NIGHT_HOUR = 3
DAY_HOUR = 12
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


#: Learned peak hours, per python weekday (Mon=0). A real grid ranks at most
#: PEAK_HOURS_PER_WEEKDAY hours per weekday, but the manager reads the tuple as
#: an allow-list rather than a length contract, so these craft what each case
#: needs: no peak at all, every hour a peak, and peaks that fall entirely
#: inside the night window the smart tier refuses to open in.
NO_PEAK_HOURS = ((),) * _WEEKDAYS
ALL_PEAK_HOURS = (tuple(range(_HOURS_PER_DAY)),) * _WEEKDAYS
NIGHT_ONLY_PEAKS = (tuple(range(1, 7)),) * _WEEKDAYS
#: Peaks everywhere except the device-local hour a test runs in and its two
#: neighbours (so an hour boundary crossing mid-test cannot flip the answer).
#: The next window still arms, while the session it opens is NOT inside a peak
#: hour and therefore ends at its first window instead of holding the block.
PEAKS_AWAY_FROM_NOW = _peaks_except(DAY_HOUR - 1, DAY_HOUR, DAY_HOUR + 1)
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


def _zone_for_local_hour(hour: int) -> str:
    """Return a fixed-offset IANA zone in which the current instant reads ``hour``.

    The night rule and the daily counters are keyed to the *device-local* hour,
    which the device reports through ``tz_id``. Tests that need "now" to fall
    inside or outside the night window therefore choose the device's zone, since
    real websocket traffic forbids freezing the clock.
    """
    offset = (hour - dt_util.utcnow().hour) % _HOURS_PER_DAY
    if offset > _HOURS_PER_DAY // 2:
        offset -= _HOURS_PER_DAY
    # POSIX-style zone names invert the sign: Etc/GMT-3 is UTC+3.
    return f"Etc/GMT{-offset:+d}"


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
    night_zone = _zone_for_local_hour(NIGHT_HOUR)
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
            smart_suspended_today=True,
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
    manager._publish(replace(manager.state, smart_suspended_today=False))
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
    zone = _zone_for_local_hour(DAY_HOUR)
    harness = await build_live(
        detail=_detail(tz_id=zone), result=_result(peak_hours=NIGHT_ONLY_PEAKS)
    )

    await harness.manager.async_set_smart_windows(True)
    await _fire_in(harness.hass, timedelta(days=2).total_seconds())
    await _quiesce(harness.hass)
    assert harness.server.all_connections == []

    # The same grid with daytime peaks does arm the next one.
    await _push_result(harness, _result(peak_hours=PEAKS_AWAY_FROM_NOW))
    await _fire_in(harness.hass, SMART_ARM_HORIZON_SECONDS)
    await _wait_live(harness)

    assert harness.manager.state.source == LIVE_SOURCE_SMART


async def test_a_smart_window_due_at_night_is_denied(
    build_live: Callable[..., Awaitable[LiveHarness]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The smart tier never streams a sleeping household."""
    zone = _zone_for_local_hour(NIGHT_HOUR)
    harness = await build_live(
        detail=_detail(tz_id=zone), result=_result(peak_hours=ALL_PEAK_HOURS)
    )

    await harness.manager.async_set_smart_windows(True)
    caplog.clear()
    await _fire_in(harness.hass, timedelta(hours=8).total_seconds())
    await _quiesce(harness.hass)

    assert _denials(caplog) == [(LIVE_SOURCE_SMART, DENIED_NIGHT)]
    assert harness.server.all_connections == []


async def test_no_flow_smart_sessions_suspend_the_tier_for_the_day(
    build_live: Callable[..., Awaitable[LiveHarness]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Streaming a quiet house three times running stops until the day turns."""
    zone = _zone_for_local_hour(DAY_HOUR)
    # Peaks everywhere but the hour now falls in: each armed window still
    # fires, and each session ends at its first window instead of holding the
    # block, so the no-flow accounting is counted one session at a time.
    harness = await build_live(
        detail=_detail(tz_id=zone), result=_result(peak_hours=PEAKS_AWAY_FROM_NOW)
    )
    await _allow_back_to_back_grants(monkeypatch, harness)
    await harness.manager.async_set_smart_windows(True)

    for _ in range(LIVE_SMART_NO_FLOW_SUSPEND):
        await _fire_in(harness.hass, SMART_ARM_HORIZON_SECONDS)
        await _wait_live(harness)
        # No counter frame is ever streamed: no water moved in this window.
        await _end_session(harness)

    assert harness.manager.state.smart_suspended_today is True
    assert len(harness.server.all_connections) == LIVE_SMART_NO_FLOW_SUSPEND

    caplog.clear()
    await _fire_in(harness.hass, SMART_ARM_HORIZON_SECONDS)
    await _quiesce(harness.hass)
    assert _denials(caplog) == [(LIVE_SOURCE_SMART, DENIED_SUSPENDED)]
    assert len(harness.server.all_connections) == LIVE_SMART_NO_FLOW_SUSPEND

    # The suspension lasts exactly one device-local day. The two zones are 26 h
    # apart, so whatever day the streaming zone dated this instant to, at least
    # one of them dates it to another.
    await _push_device(harness, _detail(tz_id=TZ_FAR_WEST))
    await _push_device(harness, _detail(tz_id=TZ_FAR_EAST))
    await _quiesce(harness.hass)

    state = harness.manager.state
    assert state.smart_suspended_today is False
    assert state.sessions_today == 0


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
    zone = _zone_for_local_hour(DAY_HOUR)
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
    zone = _zone_for_local_hour(DAY_HOUR)
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
        harness, _result(active_hours=ALL_ACTIVE_HOURS, peak_hours=PEAKS_AWAY_FROM_NOW)
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
    for needs the socket held across the block. One grant, many windows, and
    the whole block counted as ONE quiet session — a renewal that spent a grant
    (or a no-flow count) per window would exhaust the day's budget inside an
    hour. The hard-off that ends even a wanted renewal is pinned here too: a
    hold that ignored it would reconnect against an unreachable device.
    """
    zone = _zone_for_local_hour(DAY_HOUR)
    harness = await build_live(
        detail=_detail(tz_id=zone), result=_result(peak_hours=ALL_PEAK_HOURS)
    )
    manager = harness.manager
    await manager.async_set_smart_windows(True)
    await _fire_in(harness.hass, SMART_ARM_HORIZON_SECONDS)
    await _wait_live(harness)
    assert manager.state.source == LIVE_SOURCE_SMART

    await harness.server.close_connections()
    await _settle_until(
        harness.hass,
        lambda: manager.state.windows_in_session >= 1,
        "the peak-hour hold to open its second window",
    )
    await harness.server.close_connections()
    await _settle_until(
        harness.hass,
        lambda: manager.state.windows_in_session >= 2,
        "the peak-hour hold to open its third window",
    )

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
    assert manager._no_flow_sessions == 1


async def test_the_full_block_hold_requires_every_one_of_its_conditions(
    build_live: Callable[..., Awaitable[LiveHarness]],
) -> None:
    """A peak-hour renewal needs the tier on, unsuspended, in-hour, off-night.

    Unit-level, because each condition is re-read once per reporting window and
    a regression in any single one — renewing while the tier is suspended for
    the day, holding a socket into the night hours the tier exists to respect,
    or extending the hold to a source that never asked for one — surfaces only
    as a socket that will not let go. The hard-offs that end even a wanted
    renewal are pinned alongside, since they are the only thing between a held
    block and a reconnect loop against a sick cloud.
    """
    zone = _zone_for_local_hour(DAY_HOUR)
    harness = await build_live(
        detail=_detail(tz_id=zone), result=_result(peak_hours=ALL_PEAK_HOURS)
    )
    manager = harness.manager
    await manager.async_set_smart_windows(True)
    manager._publish(
        replace(manager.state, status=LIVE_STATUS_LIVE, source=LIVE_SOURCE_SMART)
    )
    assert manager._wants_renewal(WINDOW_ENDED_ON_TIMER) is True

    # Suspended for the day by the no-flow accounting.
    manager._publish(replace(manager.state, smart_suspended_today=True))
    assert manager._wants_renewal(WINDOW_ENDED_ON_TIMER) is False
    manager._publish(replace(manager.state, smart_suspended_today=False))

    # Another source streaming at the very same instant is not a block hold.
    manager._publish(replace(manager.state, source=LIVE_SOURCE_ANOMALY))
    assert manager._wants_renewal(WINDOW_ENDED_ON_TIMER) is False
    manager._publish(replace(manager.state, source=LIVE_SOURCE_SMART))

    # The night rule outranks the grid: the same all-peak grid, read by a
    # device whose own clock says 03:00.
    await _push_device(harness, _detail(tz_id=_zone_for_local_hour(NIGHT_HOUR)))
    assert manager._wants_renewal(WINDOW_ENDED_ON_TIMER) is False
    await _push_device(harness, _detail(tz_id=zone))
    assert manager._wants_renewal(WINDOW_ENDED_ON_TIMER) is True

    # The flag itself, not just the session, ends the hold.
    await manager.async_set_smart_windows(False)
    assert manager._wants_renewal(WINDOW_ENDED_ON_TIMER) is False
    await manager.async_set_smart_windows(True)

    # And the hard-offs, in their own order.
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
    zone = _zone_for_local_hour(DAY_HOUR)
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
    zone = _zone_for_local_hour(DAY_HOUR)
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


async def test_a_flow_window_resets_the_no_flow_suspend_counter(
    build_live: Callable[..., Awaitable[LiveHarness]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One window that sees water restarts the smart tier's patience.

    Quiet, quiet, flow, quiet must not suspend: the counter counts consecutive
    dry windows, not dry windows per day.
    """
    zone = _zone_for_local_hour(DAY_HOUR)
    harness = await build_live(
        detail=_detail(tz_id=zone), result=_result(peak_hours=PEAKS_AWAY_FROM_NOW)
    )
    await _allow_back_to_back_grants(monkeypatch, harness)
    await harness.manager.async_set_smart_windows(True)

    for _ in range(LIVE_SMART_NO_FLOW_SUSPEND - 1):
        await _fire_in(harness.hass, SMART_ARM_HORIZON_SECONDS)
        await _wait_live(harness)
        await _end_session(harness)

    harness.server.script = [
        frame("app_active", True),
        frame("water_counter_gals", FIXTURE_WATER_COUNTER + 9),
    ]
    await _fire_in(harness.hass, SMART_ARM_HORIZON_SECONDS)
    await _wait_live(harness)
    await _end_session(harness)

    harness.server.script = [frame("app_active", True)]
    await _fire_in(harness.hass, SMART_ARM_HORIZON_SECONDS)
    await _wait_live(harness)
    await _end_session(harness)

    assert harness.manager.state.smart_suspended_today is False


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
    """
    harness = await build_live()
    manager = harness.manager

    with patch.object(harness.fast, "data", None):
        manager._evaluate_regen(None)
        manager._evaluate_active_use(None)
        assert manager._timezone() is UTC
        assert manager._window_delay() == (
            LIVE_WINDOW_FALLBACK_SECONDS + LIVE_WINDOW_GRACE_SECONDS
        )
    await _quiesce(harness.hass)
    assert harness.server.all_connections == []

    # A zone no host can resolve, and one the device left blank: both pace the
    # daily counters and the night rule against UTC rather than raising.
    await _push_device(harness, _detail(tz_id="Mars/Olympus_Mons"))
    assert manager._timezone() is UTC
    await _push_device(harness, _detail(tz_id=""))
    assert manager._timezone() is UTC


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
