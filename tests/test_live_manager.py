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
night window — is steered through the device's own reported ``tz_id`` instead.
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
are specified for; holds renew across reporting windows and stand down on the
conditions that mean live mode must stop; failures back off from one minute to
the half-hour cap and raise (then withdraw) a repair issue; and streamed frames
reach the polled device view coalesced, deduplicated, and without the two
housekeeping properties.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.core import callback
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
from custom_components.aquahome.const import (
    DOMAIN,
    LIVE_ACTIVE_USE_DELTA_GALLONS,
    LIVE_BACKOFF_INITIAL_SECONDS,
    LIVE_BACKOFF_MAX_SECONDS,
    LIVE_COALESCE_SECONDS,
    LIVE_FAILURES_FOR_ISSUE,
    LIVE_MIN_GAP_SECONDS_MIN,
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
    RECHARGE_STATE_READY,
)
from custom_components.aquahome.coordinator import (
    AquaHomeActivityCoordinator,
    AquaHomeCoordinator,
)
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
)
from custom_components.aquahome.live_state import LiveConfig, config_from_options
from custom_components.aquahome.statistics import AquaHomeStatisticsCoordinator
from tests.api.conftest import FAKE_NOW, FakeClock, make_jwt
from tests.conftest import TEST_DEVICE_ID, load_fixture
from tests.live_server import FakeIquaLiveServer, frame

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from homeassistant.core import HomeAssistant

#: Slug derived from the fixture serial ``7384243-20203-1120`` (see entity.py).
SLUG = "7384243_20203_1120"

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
#: Rise comfortably above the active-use threshold.
ACTIVE_USE_RISE = int(LIVE_ACTIVE_USE_DELTA_GALLONS) + 3

#: ``recharge_ui`` state meaning a recharge is running right now.
REGENERATING = "regenerating"

_HOURS_PER_DAY = 24
#: Device-local hour used when a test needs "now" to be night ([01, 07)) or day;
#: both sit in the middle of their range, so an hour boundary crossing mid-test
#: cannot flip them.
NIGHT_HOUR = 3
DAY_HOUR = 12
#: Two zones 26 h apart: whatever the instant, they date it to different days.
TZ_FAR_EAST = "Etc/GMT-14"
TZ_FAR_WEST = "Etc/GMT+12"

#: Learned activity grids: nothing worth streaming, every hour worth streaming,
#: and only the night hours the smart tier refuses to open in.
GRID_HOURS = 7 * _HOURS_PER_DAY
NO_ACTIVE_HOURS = (False,) * GRID_HOURS
ALL_ACTIVE_HOURS = (True,) * GRID_HOURS
NIGHT_ONLY_HOURS = tuple(
    1 <= (index % _HOURS_PER_DAY) < 7 for index in range(GRID_HOURS)
)

#: Reporting-window length (minutes) that keeps the client-side window timer
#: beyond the manual hold's auto-off cap, so the cap can be fired on its own.
LONG_WINDOW_MINUTES = 60


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
) -> AnalyticsResult:
    """Assemble one crafted analytics pass from neutral defaults.

    Only the two blocks the manager consumes — the anomaly verdict and the
    learned activity grid — are parameterised, so no assertion here depends on
    detector numerics.
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
        grid=GridSummary(active_hours=active_hours, mature_buckets=0, hourly_samples=0),
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
    ) -> LiveHarness:
        """Build and start one live manager over a seeded device and verdict."""
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
            monotonic=_AdvancingMonotonic(),
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
    """A grid whose only active hours are night hours arms nothing."""
    zone = _zone_for_local_hour(DAY_HOUR)
    harness = await build_live(
        detail=_detail(tz_id=zone), result=_result(active_hours=NIGHT_ONLY_HOURS)
    )

    await harness.manager.async_set_smart_windows(True)
    await _fire_in(harness.hass, timedelta(days=2).total_seconds())
    await _quiesce(harness.hass)
    assert harness.server.all_connections == []

    # The same grid with daytime hours active does arm the next one.
    await _push_result(harness, _result(active_hours=ALL_ACTIVE_HOURS))
    await _fire_in(harness.hass, timedelta(hours=2).total_seconds())
    await _wait_live(harness)

    assert harness.manager.state.source == LIVE_SOURCE_SMART


async def test_a_smart_window_due_at_night_is_denied(
    build_live: Callable[..., Awaitable[LiveHarness]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The smart tier never streams a sleeping household."""
    zone = _zone_for_local_hour(NIGHT_HOUR)
    harness = await build_live(
        detail=_detail(tz_id=zone), result=_result(active_hours=ALL_ACTIVE_HOURS)
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
    harness = await build_live(
        detail=_detail(tz_id=zone), result=_result(active_hours=ALL_ACTIVE_HOURS)
    )
    await _allow_back_to_back_grants(monkeypatch, harness)
    await harness.manager.async_set_smart_windows(True)

    for _ in range(LIVE_SMART_NO_FLOW_SUSPEND):
        await _fire_in(harness.hass, timedelta(hours=2).total_seconds())
        await _wait_live(harness)
        # No counter frame is ever streamed: no water moved in this window.
        await _end_session(harness)

    assert harness.manager.state.smart_suspended_today is True
    assert len(harness.server.all_connections) == LIVE_SMART_NO_FLOW_SUSPEND

    caplog.clear()
    await _fire_in(harness.hass, timedelta(hours=2).total_seconds())
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

    # A regeneration-start transition and an every-hour-active grid: both
    # would open a session on a running manager.
    await _push_device(harness, _detail(tile_state=REGENERATING))
    await _push_result(harness, _result(active_hours=ALL_ACTIVE_HOURS))
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
        detail=_detail(tz_id=zone), result=_result(active_hours=ALL_ACTIVE_HOURS)
    )
    await _allow_back_to_back_grants(monkeypatch, harness)
    await harness.manager.async_set_smart_windows(True)

    for _ in range(LIVE_SMART_NO_FLOW_SUSPEND - 1):
        await _fire_in(harness.hass, timedelta(hours=2).total_seconds())
        await _wait_live(harness)
        await _end_session(harness)

    harness.server.script = [
        frame("app_active", True),
        frame("water_counter_gals", FIXTURE_WATER_COUNTER + 9),
    ]
    await _fire_in(harness.hass, timedelta(hours=2).total_seconds())
    await _wait_live(harness)
    await _end_session(harness)

    harness.server.script = [frame("app_active", True)]
    await _fire_in(harness.hass, timedelta(hours=2).total_seconds())
    await _wait_live(harness)
    await _end_session(harness)

    assert harness.manager.state.smart_suspended_today is False
