"""Unit tests for :mod:`custom_components.aquahome.api.websocket`.

Pure aiohttp tests driven by real local sockets: the shared fake iQua ticket +
websocket server for the normal paths, and a tiny raw-text server defined here
for deliberately malformed traffic (the fake server only ever sends well-formed
JSON objects). ``aioresponses`` is deliberately absent — ``ws_connect`` routes
through the same request path it patches, so websocket tests must speak to a
real server. No Home Assistant core is involved.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiohttp
import pytest
from aiohttp import test_utils, web

from custom_components.aquahome.api.auth import AuthManager
from custom_components.aquahome.api.client import AquaHomeClient
from custom_components.aquahome.api.exceptions import (
    AquaHomeConnectionError,
    LiveTicketExpiredError,
)
from custom_components.aquahome.api.websocket import (
    AquaHomeLiveSession,
    LiveFrame,
    live_websocket_url,
)
from tests.api.conftest import FAKE_NOW, FakeClock, make_jwt
from tests.live_server import FakeIquaLiveServer, frame

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

DEVICE_ID = "e5a7c1f3-8b2d-4e6a-b9c8-3d5f7a9b1c2e"

#: Ceiling for every frame-reading loop: a hung stream must fail the test
#: rather than block the suite (production code applies no timeout by design).
STREAM_TIMEOUT = 5.0


@pytest.fixture(autouse=True)
def _real_sockets(socket_enabled: None) -> None:
    """Allow real TCP: every test here speaks to a live local server."""


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    """Provide a real client session for the websocket handshakes."""
    async with aiohttp.ClientSession() as client:
        yield client


@pytest.fixture
async def live_server() -> AsyncIterator[FakeIquaLiveServer]:
    """Run the fake iQua /live + /ws/ server for the duration of a test."""
    server = FakeIquaLiveServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def _ws_url(server: FakeIquaLiveServer, ticket: str = "ticket-1") -> str:
    """Derive the websocket URL for a ticket on the fake server."""
    return live_websocket_url(server.base_url, f"/ws/?p={ticket}")


@asynccontextmanager
async def raw_frame_server(payloads: list[str]) -> AsyncIterator[str]:
    """Serve one websocket that sends ``payloads`` verbatim, then closes.

    Yields the websocket URL to connect to.
    """

    async def _handler(request: web.Request) -> web.StreamResponse:
        """Send every scripted payload as-is and close the socket."""
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        for payload in payloads:
            await websocket.send_str(payload)
        await websocket.close()
        return websocket

    app = web.Application()
    app.router.add_get("/ws/", _handler)
    server = test_utils.TestServer(app)
    await server.start_server()
    try:
        yield f"ws://127.0.0.1:{server.port}/ws/?p=raw-ticket"
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# URL derivation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base_url", "websocket_uri", "expected"),
    [
        # The production host: scheme upgraded, /v1 dropped, ticket kept.
        (
            "https://api.myiquaapp.com/v1",
            "/ws/?p=abc123",
            "wss://api.myiquaapp.com/ws/?p=abc123",
        ),
        # The alternate host behaves identically.
        (
            "https://api.iqua2.com/v1",
            "/ws/?p=abc123",
            "wss://api.iqua2.com/ws/?p=abc123",
        ),
        # A plain-HTTP host (local test server) stays plain, port preserved.
        (
            "http://127.0.0.1:8123/v1",
            "/ws/?p=abc123",
            "ws://127.0.0.1:8123/ws/?p=abc123",
        ),
        # An explicit HTTPS port survives the scheme upgrade.
        (
            "https://example.test:8443/v1",
            "/ws/?p=abc123",
            "wss://example.test:8443/ws/?p=abc123",
        ),
        # Any base path is dropped: the ticket URI is host-rooted.
        (
            "https://api.myiquaapp.com/v1/nested",
            "/ws/?p=abc123",
            "wss://api.myiquaapp.com/ws/?p=abc123",
        ),
        # A relative ticket URI is rooted rather than appended to the base path.
        (
            "https://api.myiquaapp.com/v1",
            "ws/?p=abc123",
            "wss://api.myiquaapp.com/ws/?p=abc123",
        ),
        # An absolute ticket URI keeps its own host; only the scheme is mapped.
        (
            "https://api.myiquaapp.com/v1",
            "https://stream.example.test/ws/?p=abc123",
            "wss://stream.example.test/ws/?p=abc123",
        ),
        # A URI that already speaks websocket is passed through unchanged.
        (
            "https://api.myiquaapp.com/v1",
            "wss://stream.example.test/ws/?p=abc123",
            "wss://stream.example.test/ws/?p=abc123",
        ),
        # Multi-value query strings survive intact.
        (
            "https://api.myiquaapp.com/v1",
            "/ws/?p=abc123&x=1",
            "wss://api.myiquaapp.com/ws/?p=abc123&x=1",
        ),
    ],
)
def test_live_websocket_url_derivation(
    base_url: str, websocket_uri: str, expected: str
) -> None:
    """The ticket URI is resolved against the API host, not its base path."""
    assert live_websocket_url(base_url, websocket_uri) == expected


# ---------------------------------------------------------------------------
# Connect and frame parsing
# ---------------------------------------------------------------------------


async def test_connect_streams_scripted_snapshot_frames(
    session: aiohttp.ClientSession, live_server: FakeIquaLiveServer
) -> None:
    """A connect snapshot parses into LiveFrames carrying raw values."""
    live_server.script = [
        frame("current_water_flow_gpm", 9, timestamp="2026-07-27T20:18:28.178386371Z"),
        frame("rf_signal_strength_dbm", -57, timestamp="2026-07-27T20:18:28Z"),
        frame("app_active", True, timestamp="2026-07-27T20:18:29Z"),
    ]
    live = AquaHomeLiveSession(session, _ws_url(live_server))

    await live.connect()
    seen: list[LiveFrame] = []
    async with asyncio.timeout(STREAM_TIMEOUT):
        async for parsed in live.frames():
            seen.append(parsed)
            if len(seen) == len(live_server.script):
                await live_server.close_connections()
    await live.close()

    assert [item.name for item in seen] == [
        "current_water_flow_gpm",
        "rf_signal_strength_dbm",
        "app_active",
    ]
    # Values arrive raw (tenths of gpm here) — decoding stays with the caller.
    assert [item.value for item in seen] == [9, -57, True]
    # Nanosecond precision is accepted and truncated to microseconds.
    assert seen[0].timestamp == datetime(2026, 7, 27, 20, 18, 28, 178386, tzinfo=UTC)
    assert seen[1].timestamp == datetime(2026, 7, 27, 20, 18, 28, tzinfo=UTC)
    # The ticket travelled in the URL query and reached the server.
    assert live_server.all_connections[-1].ticket == "ticket-1"


async def test_malformed_frames_are_skipped_and_iteration_continues(
    session: aiohttp.ClientSession,
) -> None:
    """Unusable frames are dropped without ending the session."""
    payloads = [
        "this is not JSON at all",
        '["array", "payload"]',
        '{"type": "property", "value": 5, "timestamp": "2026-07-27T20:18:28Z"}',
        '{"name": "", "value": 5}',
        '{"name": "water_counter_gals", "type": "property", "value": 4130,'
        ' "converted_property": null, "timestamp": "2026-07-27T20:18:30Z"}',
        '{"name": "current_water_flow_gpm", "value": {"nested": 1},'
        ' "timestamp": "not-a-timestamp"}',
    ]
    seen: list[LiveFrame] = []

    async with raw_frame_server(payloads) as url:
        live = AquaHomeLiveSession(session, url)
        await live.connect()
        async with asyncio.timeout(STREAM_TIMEOUT):
            async for parsed in live.frames():
                seen.append(parsed)
        await live.close()

    # Only the two named-object frames survived, in wire order.
    assert [item.name for item in seen] == [
        "water_counter_gals",
        "current_water_flow_gpm",
    ]
    assert seen[0].value == 4130
    assert seen[0].timestamp == datetime(2026, 7, 27, 20, 18, 30, tzinfo=UTC)
    # A non-scalar value and an unparseable timestamp both collapse to None.
    assert seen[1].value is None
    assert seen[1].timestamp is None


async def test_frames_before_connect_raises(
    session: aiohttp.ClientSession, live_server: FakeIquaLiveServer
) -> None:
    """Reading frames without a connected socket is a programming error."""
    live = AquaHomeLiveSession(session, _ws_url(live_server))

    with pytest.raises(RuntimeError):
        await anext(live.frames())


# ---------------------------------------------------------------------------
# Handshake failures
# ---------------------------------------------------------------------------


async def test_expired_ticket_handshake_raises_ticket_expired(
    session: aiohttp.ClientSession, live_server: FakeIquaLiveServer
) -> None:
    """A 400 handshake means a stale ticket, not a broken connection."""
    live_server.reject_next_handshakes = 1
    live = AquaHomeLiveSession(session, _ws_url(live_server, "stale-ticket"))

    with pytest.raises(LiveTicketExpiredError):
        await live.connect()

    assert live.closed is True
    assert live_server.all_connections == []


async def test_other_handshake_status_maps_to_connection_error(
    session: aiohttp.ClientSession, live_server: FakeIquaLiveServer
) -> None:
    """Any non-400 handshake rejection is an ordinary connection failure."""
    url = live_websocket_url(live_server.base_url, "/not-a-stream/?p=ticket-1")
    live = AquaHomeLiveSession(session, url)

    with pytest.raises(AquaHomeConnectionError) as excinfo:
        await live.connect()

    assert not isinstance(excinfo.value, LiveTicketExpiredError)
    assert "404" in str(excinfo.value)


async def test_unreachable_server_maps_to_connection_error(
    session: aiohttp.ClientSession,
) -> None:
    """A refused TCP connection surfaces as AquaHomeConnectionError."""
    stopped = FakeIquaLiveServer()
    await stopped.start()
    url = _ws_url(stopped)
    await stopped.stop()
    live = AquaHomeLiveSession(session, url)

    with pytest.raises(AquaHomeConnectionError):
        await live.connect()

    assert live.closed is True


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


async def test_server_close_ends_iteration(
    session: aiohttp.ClientSession, live_server: FakeIquaLiveServer
) -> None:
    """A server-side close finishes the iterator instead of raising."""
    live_server.script = [frame("water_counter_gals", 4130)]
    live = AquaHomeLiveSession(session, _ws_url(live_server))

    await live.connect()
    seen: list[LiveFrame] = []
    async with asyncio.timeout(STREAM_TIMEOUT):
        async for parsed in live.frames():
            seen.append(parsed)
            await live_server.close_connections()

    assert [item.name for item in seen] == ["water_counter_gals"]
    # The socket is known-closed once the stream ended, before close() is called.
    assert live.closed is True
    await live.close()
    assert live.closed is True


async def test_close_is_idempotent_and_reports_the_socket_state(
    session: aiohttp.ClientSession, live_server: FakeIquaLiveServer
) -> None:
    """``closed`` tracks the real socket and ``close`` tolerates repetition."""
    live = AquaHomeLiveSession(session, _ws_url(live_server))
    states = [live.closed]

    # Closing before a connect is a no-op, not an error.
    await live.close()
    states.append(live.closed)

    await live.connect()
    states.append(live.closed)

    await live.close()
    states.append(live.closed)
    # A second close changes nothing and must not raise.
    await live.close()
    states.append(live.closed)

    assert states == [True, True, False, True, True]
    assert live_server.closed_codes  # the server saw the client disconnect


# ---------------------------------------------------------------------------
# Composition with the REST client
# ---------------------------------------------------------------------------


async def test_client_ticket_composes_into_a_connectable_url(
    session: aiohttp.ClientSession, live_server: FakeIquaLiveServer
) -> None:
    """A ticket from the client, resolved against its base URL, connects."""
    auth = AuthManager(session, base_url=live_server.base_url, time_func=FakeClock())
    auth.set_tokens(make_jwt(FAKE_NOW), "refresh-token")
    client = AquaHomeClient(session, auth, base_url=live_server.base_url)
    live_server.script = [frame("treated_water_avail_gals", 812)]

    ticket = await client.async_get_live_ticket(
        DEVICE_ID, ["treated_water_avail_gals", "app_active"]
    )
    assert ticket.websocket_uri is not None
    live = AquaHomeLiveSession(
        session, live_websocket_url(client.base_url, ticket.websocket_uri)
    )
    await live.connect()
    seen: list[LiveFrame] = []
    async with asyncio.timeout(STREAM_TIMEOUT):
        async for parsed in live.frames():
            seen.append(parsed)
            await live_server.close_connections()
    await live.close()

    assert live_server.live_requests == [
        {"properties": "treated_water_avail_gals,app_active", "type": "property"}
    ]
    assert [item.name for item in seen] == ["treated_water_avail_gals"]
    # The ticket the server minted is exactly the one the handshake presented.
    assert (
        live_server.all_connections[-1].ticket
        == ticket.websocket_uri.rsplit("=", maxsplit=1)[-1]
    )
