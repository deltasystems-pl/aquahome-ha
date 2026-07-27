"""A fake iQua live endpoint + websocket server for live-mode tests.

Runs a real local :class:`aiohttp.test_utils.TestServer` exposing exactly the
two surfaces live mode touches — ``GET /v1/devices/{id}/live`` (the ticket
endpoint) and ``GET /ws/`` (the ticketed websocket) — so the production client
and websocket code run unmodified against it: point the
:class:`~custom_components.aquahome.api.AquaHomeClient` at
:attr:`FakeIquaLiveServer.base_url` and the manager's derived websocket URL
lands back on this server.

Deliberately incompatible with ``aioresponses``: ``ClientSession.ws_connect``
routes through the same ``session.request`` path aioresponses patches, so any
test that touches this server must not use the ``mock_api`` fixture. Seed
coordinators with ``async_set_updated_data`` instead.

Frame scripting: each accepted websocket connection sends the frames queued in
:attr:`FakeIquaLiveServer.script` (a snapshot burst), then streams whatever
:meth:`FakeIquaLiveServer.push` publishes, until :meth:`close_connections` or
the client closes. Tickets are generated per request; making the server
``reject_next_handshakes`` answers the websocket handshake with HTTP 400 (the
expired-ticket behaviour observed live).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count
from typing import Any

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

__all__ = ["FakeIquaLiveServer", "frame"]


def frame(
    name: str,
    value: bool | int | float | str | None,
    *,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Build one property frame in the wire shape observed live."""
    return {
        "name": name,
        "type": "property",
        "value": value,
        "converted_property": None,
        "timestamp": timestamp or datetime.now(UTC).isoformat(),
    }


@dataclass
class _Connection:
    """Bookkeeping for one accepted websocket connection."""

    ticket: str
    ws: web.WebSocketResponse
    queue: asyncio.Queue[dict[str, Any] | None] = field(default_factory=asyncio.Queue)


class FakeIquaLiveServer:
    """Local stand-in for the iQua /live + /ws/ surface.

    Usage::

        server = FakeIquaLiveServer()
        await server.start()
        client = AquaHomeClient(session, auth, base_url=server.base_url)
        ...
        await server.stop()

    Observability: ``live_requests`` records every ticket request's query
    (properties CSV + type), ``connections`` every accepted websocket
    (including its ticket), ``closed_codes`` every close.
    """

    def __init__(self) -> None:
        """Prepare the aiohttp application; :meth:`start` binds the port."""
        self._app = web.Application()
        self._app.router.add_get("/v1/devices/{device_id}/live", self._handle_live)
        self._app.router.add_get("/ws/", self._handle_ws)
        self._server: TestServer | None = None
        self._ticket_counter = count(1)
        #: Frames sent to every new connection before streamed pushes (the
        #: partial connect snapshot). Mutate freely between connections.
        self.script: list[dict[str, Any]] = []
        #: Ticket requests observed: one dict per request (query echoed).
        self.live_requests: list[dict[str, str]] = []
        #: Next N /live requests answer with this HTTP status instead of 200.
        self.live_status_overrides: list[int] = []
        #: Next N websocket handshakes are rejected with HTTP 400.
        self.reject_next_handshakes: int = 0
        #: Accepted, currently open connections (newest last).
        self.connections: list[_Connection] = []
        #: Every accepted connection ever (newest last), open or closed.
        self.all_connections: list[_Connection] = []
        #: Close codes observed when clients disconnected.
        self.closed_codes: list[int | None] = []

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Bind the server to an ephemeral local port."""
        self._server = TestServer(self._app)
        await self._server.start_server()

    async def stop(self) -> None:
        """Close every connection and shut the server down."""
        await self.close_connections()
        if self._server is not None:
            await self._server.close()
            self._server = None

    @property
    def base_url(self) -> str:
        """The API base URL (with /v1) to bind the production client to."""
        if self._server is None:
            msg = "server not started"
            raise RuntimeError(msg)
        return f"http://127.0.0.1:{self._server.port}/v1"

    # -- scripting ---------------------------------------------------------

    async def push(self, payload: dict[str, Any]) -> None:
        """Stream one frame to every open connection."""
        for connection in list(self.connections):
            await connection.queue.put(payload)

    async def close_connections(self) -> None:
        """Server-side close of every open connection."""
        for connection in list(self.connections):
            await connection.queue.put(None)
        # Give the sender loops a tick to drain and close.
        await asyncio.sleep(0)

    # -- handlers ----------------------------------------------------------

    async def _handle_live(self, request: web.Request) -> web.Response:
        """Answer a ticket request, honouring scripted status overrides."""
        self.live_requests.append(dict(request.query))
        if self.live_status_overrides:
            status = self.live_status_overrides.pop(0)
            return web.json_response(
                {"code": "ThrottleLimitExceeded", "detail": "scripted"}
                if status == 429
                else {"detail": "scripted"},
                status=status,
            )
        ticket = f"ticket-{next(self._ticket_counter)}"
        return web.json_response({"websocket_uri": f"/ws/?p={ticket}"})

    async def _handle_ws(self, request: web.Request) -> web.StreamResponse:
        """Accept (or reject) a ticketed websocket and stream scripted frames."""
        if self.reject_next_handshakes > 0:
            self.reject_next_handshakes -= 1
            return web.json_response({"detail": "ticket expired"}, status=400)
        ticket = request.query.get("p", "")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        connection = _Connection(ticket=ticket, ws=ws)
        self.connections.append(connection)
        self.all_connections.append(connection)

        async def _sender() -> None:
            for scripted in self.script:
                await ws.send_str(json.dumps(scripted))
            while True:
                queued = await connection.queue.get()
                if queued is None:
                    await ws.close()
                    return
                await ws.send_str(json.dumps(queued))

        sender = asyncio.ensure_future(_sender())
        try:
            async for msg in ws:
                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
                    break
        finally:
            sender.cancel()
            self.closed_codes.append(ws.close_code)
            if connection in self.connections:
                self.connections.remove(connection)
        return ws
