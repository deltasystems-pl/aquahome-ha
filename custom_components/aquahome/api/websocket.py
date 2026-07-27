"""Low-level websocket session for the iQua live property stream.

``GET /devices/{id}/live`` returns a host-rooted, ticketed URI
(``/ws/?p=<ticket>``); the ticket alone authenticates the handshake — no bearer
header or cookie is involved — and expires roughly 300 s after issue, at which
point the server answers the handshake with HTTP 400.
:class:`AquaHomeLiveSession` models exactly one connection attempt against such
a ticket: connect, iterate frames, close. A reconnect means a fresh ticket, a
fresh URL, and a fresh session object.

The stream is a sequence of JSON text frames, one per property update::

    {"name": "water_counter_gals", "type": "property", "value": 4130,
     "converted_property": null, "timestamp": "2026-07-27T20:18:28.178386371Z"}

Two observed traits shape this module. The connect snapshot is *partial* — only
some of the subscribed properties arrive, and duplicate frames carrying an
unchanged value are normal — so a consumer must merge frames instead of
expecting a complete picture. And the device's fast-reporting window lasts only
about three minutes from connect, after which the stream falls silent while the
socket stays open indefinitely; nothing disconnects. This module therefore
applies no timeouts of its own: window timing, renewal, and cancellation belong
to the caller, which is the layer that knows the session budget.

Like the rest of the API package, this module is Home Assistant free — aiohttp
and the standard library only.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from .exceptions import AquaHomeConnectionError, LiveTicketExpiredError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_LOGGER = logging.getLogger(__name__)

#: Websocket scheme for each API scheme; anything else (already ``ws``/``wss``)
#: passes through untouched.
_WEBSOCKET_SCHEMES = {"http": "ws", "https": "wss"}

#: Message types that mean "this stream is over" — the peer closed the socket.
_STREAM_CLOSE_TYPES = frozenset(
    {
        aiohttp.WSMsgType.CLOSE,
        aiohttp.WSMsgType.CLOSING,
        aiohttp.WSMsgType.CLOSED,
    }
)


@dataclass(frozen=True, slots=True)
class LiveFrame:
    """One property update streamed over the live websocket.

    Values arrive raw, exactly as the REST property map reports them (integer
    scaling included), so the same decoding applies to both sources.
    ``timestamp`` is the device-side instant the value was recorded, or ``None``
    when the frame carried no parseable timestamp.
    """

    name: str
    value: bool | int | float | str | None
    timestamp: datetime | None


def live_websocket_url(base_url: str, websocket_uri: str) -> str:
    """Build the absolute websocket URL from the API base URL and a ticket URI.

    The ticket URI is host-rooted (``/ws/?p=<ticket>``), so the API base path
    (``/v1``) is dropped while host and port are kept; the scheme is upgraded
    (``https`` -> ``wss``, ``http`` -> ``ws``) so a plain-HTTP host — a local
    test server, for instance — stays plain. A URI that already carries its own
    scheme and host is honoured as-is apart from that scheme mapping.
    """
    base = urlsplit(base_url)
    ticket = urlsplit(websocket_uri)
    source_scheme = ticket.scheme or base.scheme
    scheme = _WEBSOCKET_SCHEMES.get(source_scheme, source_scheme)
    netloc = ticket.netloc or base.netloc
    path = ticket.path if ticket.path.startswith("/") else f"/{ticket.path}"
    return urlunsplit((scheme, netloc, path, ticket.query, ""))


def _as_scalar(value: Any) -> bool | int | float | str | None:
    """Return a JSON scalar unchanged; a list or object collapses to ``None``."""
    if isinstance(value, (bool, int, float, str)):
        return value
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse a frame timestamp into an aware ``datetime``, assuming UTC.

    Frames carry RFC3339 timestamps with nanosecond precision
    (``2026-07-27T20:18:28.178386371Z``), which :meth:`datetime.fromisoformat`
    accepts and truncates to microseconds. Anything unparseable collapses to
    ``None`` instead of raising — the same tolerance the REST payload parsers
    apply, since one malformed timestamp must never abort a live session.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _parse_frame(payload: str) -> LiveFrame | None:
    """Parse one text frame, returning ``None`` when it is not a usable update.

    Everything that is not a JSON object with a property name is skipped: the
    stream is a vendor surface that may carry protocol or diagnostic frames
    this integration has no use for, and dropping them is always preferable to
    ending a session.
    """
    try:
        data = json.loads(payload)
    except ValueError:
        _LOGGER.debug("Skipping non-JSON live frame: %s", payload)
        return None
    if not isinstance(data, dict):
        _LOGGER.debug("Skipping live frame that is not a JSON object: %s", payload)
        return None
    name = data.get("name")
    if not isinstance(name, str) or not name:
        _LOGGER.debug("Skipping live frame without a property name: %s", payload)
        return None
    return LiveFrame(
        name=name,
        value=_as_scalar(data.get("value")),
        timestamp=_parse_timestamp(data.get("timestamp")),
    )


class AquaHomeLiveSession:
    """A single ticketed websocket connection to the live property stream."""

    def __init__(self, session: aiohttp.ClientSession, ws_url: str) -> None:
        """Bind the session to an aiohttp session and one ticketed websocket URL.

        ``ws_url`` embeds the live ticket, which is a credential: it is never
        logged, and it authenticates exactly one handshake.
        """
        self._session = session
        self._ws_url = ws_url
        self._ws: aiohttp.ClientWebSocketResponse | None = None

    async def connect(self) -> None:
        """Open the websocket, mapping handshake failures onto the taxonomy.

        A ticket the server considers stale is rejected with HTTP 400; that maps
        to :class:`~.exceptions.LiveTicketExpiredError` so the caller can fetch
        one fresh ticket and retry instead of treating it as a network failure.
        Every other handshake or transport failure is an
        :class:`~.exceptions.AquaHomeConnectionError`.
        """
        try:
            # No heartbeat: the stream has its own liveness signal (the
            # subscribed clock property ticks every ~10 s while the device
            # fast-reports), and client pings are not part of the protocol.
            self._ws = await self._session.ws_connect(self._ws_url, heartbeat=None)
        except aiohttp.WSServerHandshakeError as err:
            if err.status == HTTPStatus.BAD_REQUEST:
                msg = "The live ticket expired before the websocket handshake"
                raise LiveTicketExpiredError(msg) from err
            msg = f"Live websocket handshake failed with HTTP {err.status}"
            raise AquaHomeConnectionError(msg) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            msg = "Could not open the live websocket"
            raise AquaHomeConnectionError(msg) from err
        _LOGGER.debug("Live websocket connected")

    async def frames(self) -> AsyncIterator[LiveFrame]:
        """Yield every usable property frame until the server ends the stream.

        Unparseable and non-property frames are skipped (see :func:`_parse_frame`)
        so a single odd frame cannot end a session. Iteration finishes when the
        peer closes the socket or the transport fails; it never stops on its own
        while the socket is open — a silent socket is the normal state between
        the device's fast-reporting windows, and only the caller knows how long
        to wait or when to cancel.
        """
        ws = self._ws
        if ws is None:
            msg = "The live websocket must be connected before reading frames"
            raise RuntimeError(msg)
        while True:
            message = await ws.receive()
            if message.type is aiohttp.WSMsgType.TEXT:
                parsed = _parse_frame(message.data)
                if parsed is not None:
                    yield parsed
            elif message.type is aiohttp.WSMsgType.ERROR:
                _LOGGER.debug("Live websocket failed: %s", ws.exception())
                return
            elif message.type in _STREAM_CLOSE_TYPES:
                _LOGGER.debug("Live websocket closed by the server")
                return
            else:
                # Binary payloads are not part of the property protocol; pings
                # and pongs are answered by aiohttp itself.
                _LOGGER.debug("Ignoring live frame of type %s", message.type.name)

    async def close(self) -> None:
        """Close the websocket; safe to call repeatedly and after a failure."""
        ws = self._ws
        if ws is None or ws.closed:
            return
        await ws.close()
        _LOGGER.debug("Live websocket closed")

    @property
    def closed(self) -> bool:
        """Return whether this session holds no open websocket."""
        ws = self._ws
        return ws is None or ws.closed
