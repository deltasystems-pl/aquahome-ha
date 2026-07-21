"""Access-token lifecycle for the iQua cloud API.

``AuthManager`` owns the only unauthenticated requests the integration makes
(``POST /auth/login`` and ``POST /auth/refresh``). It stores the access/refresh
token pair, decodes the access JWT locally to know when it is about to expire,
and refreshes it transparently — exactly once even under concurrent callers.

Tokens are never logged. The JWT is decoded WITHOUT signature verification
purely to read its ``exp`` claim; the server remains the sole authority on
validity.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Callable
from http import HTTPStatus
from typing import Any, NoReturn

import aiohttp

from .const import (
    ACCEPT_HEADER,
    API_BASE_URL,
    APP_USER_AGENT,
    APP_VERSION_HEADER,
    MAX_REFRESH_HOURS,
    TOKEN_REFRESH_MARGIN_SECONDS,
)
from .exceptions import ApiError, AquaHomeConnectionError, AuthError
from .models import LoginResult

_LOGGER = logging.getLogger(__name__)

#: Error code returned by ``POST /auth/login`` for bad credentials.
_AUTH_BAD_CREDENTIALS_CODE = "AuthBadUsernameOrPassword"
#: Error code returned by ``POST /auth/refresh`` for an unusable refresh token.
_AUTH_CANNOT_REFRESH_CODE = "AuthCannotRefreshToken"


class AuthManager:
    """Manage the iQua access/refresh token pair for a single account."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        base_url: str = API_BASE_URL,
        time_func: Callable[[], float] = time.time,
        on_token_update: Callable[[str, str], None] | None = None,
    ) -> None:
        """Bind the manager to an aiohttp session and API host.

        ``on_token_update(access_token, refresh_token)`` fires on every token
        change caused by login or refresh, so the config entry can persist the
        new pair. It does NOT fire for :meth:`set_tokens`, which merely restores
        a previously persisted pair.
        """
        self._session = session
        self._base_url = base_url
        self._time_func = time_func
        self._on_token_update = on_token_update
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._lock = asyncio.Lock()

    async def async_login(
        self,
        email: str,
        password: str,
        *,
        refresh_hours: int = MAX_REFRESH_HOURS,
    ) -> LoginResult:
        """Log in with credentials and store the returned token pair.

        Raises :class:`AuthError` on bad credentials (HTTP 401 /
        ``AuthBadUsernameOrPassword``), :class:`ApiError` for any other error
        response, and :class:`AquaHomeConnectionError` on a network failure.
        """
        body = await self._post(
            "/auth/login",
            {"email": email, "password": password, "refresh_hours": refresh_hours},
            auth_code=_AUTH_BAD_CREDENTIALS_CODE,
        )
        result = LoginResult.from_dict(body)
        self._store_tokens(result.access_token, result.refresh_token, notify=True)
        return result

    def set_tokens(self, access_token: str, refresh_token: str) -> None:
        """Restore a persisted token pair without firing the update callback."""
        self._store_tokens(access_token, refresh_token, notify=False)

    async def async_get_access_token(self) -> str:
        """Return a currently-valid access token, refreshing first if stale.

        The token is considered stale when fewer than
        ``TOKEN_REFRESH_MARGIN_SECONDS`` remain until its ``exp`` claim (an
        unparsable token counts as stale). Refresh is serialized by a lock and
        re-checks freshness after acquiring it, so concurrent callers trigger at
        most one ``POST /auth/refresh``.
        """
        token = self._access_token
        if token is not None and self._token_is_fresh(token):
            return token
        async with self._lock:
            token = self._access_token
            if token is not None and self._token_is_fresh(token):
                return token
            await self._async_refresh_locked()
            refreshed = self._access_token
            if refreshed is None:
                msg = "Token refresh did not produce an access token"
                raise AuthError(msg)
            return refreshed

    async def async_refresh(self) -> None:
        """Exchange the refresh token for a new token pair, serialized.

        Concurrent callers (e.g. two requests that both hit a 401) are
        serialized by the lock so each rotation uses the latest refresh token —
        a parallel refresh with an already-rotated token would be rejected by
        the server and force a needless reauthentication.
        """
        async with self._lock:
            await self._async_refresh_locked()

    async def _async_refresh_locked(self) -> None:
        """Exchange the refresh token for a new pair; caller holds the lock.

        Raises :class:`AuthError` when no refresh token is stored, or when the
        server rejects it (HTTP 401 / ``AuthCannotRefreshToken``) — Home
        Assistant maps this to a reauthentication flow.
        """
        refresh_token = self._refresh_token
        if refresh_token is None:
            msg = "No refresh token available; reauthentication required"
            raise AuthError(msg)
        _LOGGER.debug("Refreshing iQua access token")
        body = await self._post(
            "/auth/refresh",
            {"refresh_token": refresh_token, "refresh_hours": MAX_REFRESH_HOURS},
            auth_code=_AUTH_CANNOT_REFRESH_CODE,
        )
        access_token = body.get("access_token")
        new_refresh_token = body.get("refresh_token")
        if not isinstance(access_token, str) or not isinstance(new_refresh_token, str):
            msg = "Refresh response did not contain a valid token pair"
            raise AuthError(msg)
        self._store_tokens(access_token, new_refresh_token, notify=True)

    def _store_tokens(
        self, access_token: str, refresh_token: str, *, notify: bool
    ) -> None:
        """Replace the stored token pair, optionally firing the callback."""
        self._access_token = access_token
        self._refresh_token = refresh_token
        if notify and self._on_token_update is not None:
            self._on_token_update(access_token, refresh_token)

    def _token_is_fresh(self, token: str) -> bool:
        """Return whether ``token`` has enough life left to skip a refresh."""
        expiry = self._token_expiry(token)
        if expiry is None:
            return False
        return expiry - self._time_func() >= TOKEN_REFRESH_MARGIN_SECONDS

    @staticmethod
    def _token_expiry(token: str) -> float | None:
        """Read the ``exp`` epoch claim from a JWT without verifying it.

        Returns ``None`` for any token that cannot be parsed into a numeric
        ``exp`` — the caller treats that as already expired.
        """
        try:
            payload_segment = token.split(".")[1]
            padding = "=" * (-len(payload_segment) % 4)
            decoded = base64.urlsafe_b64decode(payload_segment + padding)
            exp = json.loads(decoded)["exp"]
        except (IndexError, KeyError, TypeError, ValueError):
            return None
        if isinstance(exp, bool) or not isinstance(exp, (int, float)):
            return None
        return float(exp)

    def _headers(self) -> dict[str, str]:
        """Build the app-mimicry headers sent on every auth request."""
        return {
            "User-Agent": APP_USER_AGENT,
            "x-app-version": APP_VERSION_HEADER,
            "accept": ACCEPT_HEADER,
        }

    async def _post(
        self, path: str, payload: dict[str, Any], *, auth_code: str
    ) -> dict[str, Any]:
        """POST ``payload`` to ``path`` and return the parsed JSON body.

        ``auth_code`` is the machine-readable error code that, alongside HTTP
        401, maps this endpoint's failures to :class:`AuthError`.
        """
        url = f"{self._base_url}{path}"
        try:
            async with self._session.post(
                url, json=payload, headers=self._headers()
            ) as response:
                status = response.status
                body = await self._read_json(response)
        except (aiohttp.ClientError, TimeoutError) as err:
            msg = "Could not reach the iQua cloud"
            raise AquaHomeConnectionError(msg) from err
        if status >= HTTPStatus.BAD_REQUEST:
            self._raise_error(status, body, auth_code)
        return body

    @staticmethod
    async def _read_json(response: aiohttp.ClientResponse) -> dict[str, Any]:
        """Parse a JSON object body tolerantly; non-JSON becomes ``{}``."""
        try:
            data = await response.json(content_type=None)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _raise_error(status: int, body: dict[str, Any], auth_code: str) -> NoReturn:
        """Map an error response body to the appropriate typed exception."""
        raw_code = body.get("code")
        code = raw_code if isinstance(raw_code, str) else None
        raw_fields = body.get("fields")
        fields = raw_fields if isinstance(raw_fields, dict) else None
        detail = body.get("detail") or body.get("message")
        message = (
            detail if isinstance(detail, str) and detail else f"iQua API error {status}"
        )
        if status == HTTPStatus.UNAUTHORIZED or code == auth_code:
            raise AuthError(message, status=status, code=code, fields=fields)
        raise ApiError(message, status=status, code=code, fields=fields)
