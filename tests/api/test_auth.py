"""Unit tests for :mod:`custom_components.aquahome.api.auth`.

Pure aiohttp tests: a real :class:`aiohttp.ClientSession` with all HTTP mocked
by ``aioresponses``, a deterministic fake clock, and JWTs built by ``make_jwt``.
No Home Assistant core is involved.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import aiohttp
import pytest
from aioresponses import aioresponses
from aioresponses.core import RequestCall

from custom_components.aquahome.api.auth import AuthManager
from custom_components.aquahome.api.const import (
    API_BASE_URL,
    IQUA2_BASE_URL,
    MAX_REFRESH_HOURS,
)
from custom_components.aquahome.api.exceptions import (
    ApiError,
    AquaHomeConnectionError,
    AuthError,
)
from custom_components.aquahome.api.models import LoginResult
from tests.api.conftest import FAKE_NOW, FakeClock, make_jwt

LOGIN_URL = f"{API_BASE_URL}/auth/login"
REFRESH_URL = f"{API_BASE_URL}/auth/refresh"

#: A stale access token: issued ~23 h ago so only ~1 h of its 24 h life remains,
#: which is inside the 2 h refresh margin.
STALE_IAT = FAKE_NOW - 82_800
#: A fresh access token issued exactly at ``FAKE_NOW`` (full 24 h ahead).
FRESH_TOKEN = make_jwt(FAKE_NOW)
STALE_TOKEN = make_jwt(STALE_IAT)


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    """Provide a real client session with all sockets mocked by aioresponses."""
    async with aiohttp.ClientSession() as client:
        yield client


def _login_body(access_token: str, refresh_token: str) -> dict[str, object]:
    """Build a realistic ``AuthLoginOutputBody`` payload."""
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user_id": "7f1e15b0-e9c7-44a1-8f0a-1844d67bf545",
        "is_verified": True,
        "is_admin": False,
        "is_customer_support": False,
        "is_marketing": False,
    }


def _calls_for(
    mocked: aioresponses, method: str, path_suffix: str
) -> list[RequestCall]:
    """Return every recorded request whose method and URL path suffix match."""
    return [
        call
        for (call_method, url), calls in mocked.requests.items()
        if call_method == method and url.path.endswith(path_suffix)
        for call in calls
    ]


def _refresh_calls(mocked: aioresponses) -> list[RequestCall]:
    """Return every recorded ``POST /auth/refresh`` request call."""
    return _calls_for(mocked, "POST", "/auth/refresh")


async def test_login_success_parses_and_stores_tokens(
    session: aiohttp.ClientSession, fake_clock: FakeClock
) -> None:
    """A successful login returns a LoginResult, stores and announces tokens."""
    updates: list[tuple[str, str]] = []
    with aioresponses() as mocked:
        mocked.post(
            LOGIN_URL,
            status=200,
            payload=_login_body(FRESH_TOKEN, "refresh-abc"),
        )
        auth = AuthManager(
            session,
            time_func=fake_clock,
            on_token_update=lambda a, r: updates.append((a, r)),
        )
        result = await auth.async_login("dev@example.com", "hunter2")

    assert isinstance(result, LoginResult)
    assert result.access_token == FRESH_TOKEN
    assert result.refresh_token == "refresh-abc"
    assert result.user_id == "7f1e15b0-e9c7-44a1-8f0a-1844d67bf545"
    assert result.is_verified is True
    # Login fires the persistence callback exactly once with the new pair.
    assert updates == [(FRESH_TOKEN, "refresh-abc")]
    # Stored: a fresh token is returned without any further HTTP call.
    assert await auth.async_get_access_token() == FRESH_TOKEN


async def test_login_sends_credentials_and_mimicry_headers(
    session: aiohttp.ClientSession, fake_clock: FakeClock
) -> None:
    """The login request carries the body and app-mimicry headers."""
    with aioresponses() as mocked:
        mocked.post(LOGIN_URL, status=200, payload=_login_body(FRESH_TOKEN, "r1"))
        auth = AuthManager(session, time_func=fake_clock)
        await auth.async_login("dev@example.com", "hunter2", refresh_hours=720)

        (call,) = _calls_for(mocked, "POST", "/auth/login")

    assert call.kwargs["json"] == {
        "email": "dev@example.com",
        "password": "hunter2",
        "refresh_hours": 720,
    }
    headers = call.kwargs["headers"]
    assert headers["User-Agent"] == "okhttp/4.9.2"
    assert headers["x-app-version"] == "version=1.5.2,build=2794"
    assert headers["accept"] == "application/json, text/plain, */*"


async def test_login_defaults_to_max_refresh_hours(
    session: aiohttp.ClientSession, fake_clock: FakeClock
) -> None:
    """Without an explicit value, login requests the maximum refresh lifetime."""
    with aioresponses() as mocked:
        mocked.post(LOGIN_URL, status=200, payload=_login_body(FRESH_TOKEN, "r1"))
        auth = AuthManager(session, time_func=fake_clock)
        await auth.async_login("dev@example.com", "hunter2")

        (call,) = _calls_for(mocked, "POST", "/auth/login")

    assert call.kwargs["json"]["refresh_hours"] == MAX_REFRESH_HOURS


async def test_login_bad_credentials_raises_auth_error(
    session: aiohttp.ClientSession, fake_clock: FakeClock
) -> None:
    """HTTP 401 on login raises AuthError carrying the parsed error details."""
    with aioresponses() as mocked:
        mocked.post(
            LOGIN_URL,
            status=401,
            payload={
                "code": "AuthBadUsernameOrPassword",
                "detail": "Invalid username or password.",
            },
        )
        auth = AuthManager(session, time_func=fake_clock)
        with pytest.raises(AuthError) as excinfo:
            await auth.async_login("dev@example.com", "wrong")

    assert excinfo.value.status == 401
    assert excinfo.value.code == "AuthBadUsernameOrPassword"
    assert "Invalid username or password." in str(excinfo.value)


async def test_login_bad_credentials_code_without_401_still_auth_error(
    session: aiohttp.ClientSession, fake_clock: FakeClock
) -> None:
    """The bad-credentials code maps to AuthError even off a non-401 status."""
    with aioresponses() as mocked:
        mocked.post(
            LOGIN_URL,
            status=400,
            payload={"code": "AuthBadUsernameOrPassword", "detail": "nope"},
        )
        auth = AuthManager(session, time_func=fake_clock)
        with pytest.raises(AuthError):
            await auth.async_login("dev@example.com", "wrong")


async def test_login_other_error_raises_api_error(
    session: aiohttp.ClientSession, fake_clock: FakeClock
) -> None:
    """A non-auth error response raises plain ApiError with status and fields."""
    with aioresponses() as mocked:
        mocked.post(
            LOGIN_URL,
            status=422,
            payload={
                "code": "ValidationError",
                "detail": "email is required",
                "fields": {"email": "required"},
            },
        )
        auth = AuthManager(session, time_func=fake_clock)
        with pytest.raises(ApiError) as excinfo:
            await auth.async_login("", "hunter2")

    assert not isinstance(excinfo.value, AuthError)
    assert excinfo.value.status == 422
    assert excinfo.value.code == "ValidationError"
    assert excinfo.value.fields == {"email": "required"}


async def test_login_network_error_raises_connection_error(
    session: aiohttp.ClientSession, fake_clock: FakeClock
) -> None:
    """A transport failure during login raises AquaHomeConnectionError."""
    with aioresponses() as mocked:
        mocked.post(LOGIN_URL, exception=aiohttp.ClientConnectionError("boom"))
        auth = AuthManager(session, time_func=fake_clock)
        with pytest.raises(AquaHomeConnectionError):
            await auth.async_login("dev@example.com", "hunter2")


async def test_fresh_token_returned_without_refresh(
    session: aiohttp.ClientSession, fake_clock: FakeClock
) -> None:
    """A token well within its lifetime is returned with no refresh request."""
    with aioresponses() as mocked:
        auth = AuthManager(session, time_func=fake_clock)
        auth.set_tokens(FRESH_TOKEN, "refresh-abc")

        assert await auth.async_get_access_token() == FRESH_TOKEN
        assert _refresh_calls(mocked) == []


async def test_stale_token_triggers_single_refresh_under_concurrency(
    session: aiohttp.ClientSession, fake_clock: FakeClock
) -> None:
    """Ten concurrent callers on a stale token cause exactly one refresh."""
    new_access = make_jwt(FAKE_NOW)
    with aioresponses() as mocked:
        mocked.post(
            REFRESH_URL,
            status=200,
            payload={
                "access_token": new_access,
                "refresh_token": "refresh-new",
            },
            repeat=True,
        )
        auth = AuthManager(session, time_func=fake_clock)
        auth.set_tokens(STALE_TOKEN, "refresh-old")

        tokens = await asyncio.gather(
            *(auth.async_get_access_token() for _ in range(10))
        )

    assert tokens == [new_access] * 10
    assert len(_refresh_calls(mocked)) == 1


async def test_refresh_rotates_tokens_and_fires_callback(
    session: aiohttp.ClientSession, fake_clock: FakeClock
) -> None:
    """Refresh replaces both tokens and announces the new pair once."""
    updates: list[tuple[str, str]] = []
    new_access = make_jwt(FAKE_NOW)
    with aioresponses() as mocked:
        mocked.post(
            REFRESH_URL,
            status=200,
            payload={
                "access_token": new_access,
                "refresh_token": "refresh-rotated",
            },
        )
        auth = AuthManager(
            session,
            time_func=fake_clock,
            on_token_update=lambda a, r: updates.append((a, r)),
        )
        auth.set_tokens(STALE_TOKEN, "refresh-old")

        await auth.async_refresh()

        # The stored refresh token is the one sent to the refresh endpoint.
        (call,) = _calls_for(mocked, "POST", "/auth/refresh")

    assert call.kwargs["json"] == {
        "refresh_token": "refresh-old",
        "refresh_hours": MAX_REFRESH_HOURS,
    }
    assert updates == [(new_access, "refresh-rotated")]
    assert await auth.async_get_access_token() == new_access


async def test_refresh_unauthorized_raises_auth_error(
    session: aiohttp.ClientSession, fake_clock: FakeClock
) -> None:
    """HTTP 401 on refresh raises AuthError (drives reauth downstream)."""
    with aioresponses() as mocked:
        mocked.post(
            REFRESH_URL,
            status=401,
            payload={"code": "AuthCannotRefreshToken", "detail": "expired"},
        )
        auth = AuthManager(session, time_func=fake_clock)
        auth.set_tokens(STALE_TOKEN, "refresh-old")
        with pytest.raises(AuthError):
            await auth.async_refresh()


async def test_refresh_cannot_refresh_code_raises_auth_error(
    session: aiohttp.ClientSession, fake_clock: FakeClock
) -> None:
    """The cannot-refresh code maps to AuthError even off a non-401 status."""
    with aioresponses() as mocked:
        mocked.post(
            REFRESH_URL,
            status=400,
            payload={"code": "AuthCannotRefreshToken", "detail": "bad token"},
        )
        auth = AuthManager(session, time_func=fake_clock)
        auth.set_tokens(STALE_TOKEN, "refresh-old")
        with pytest.raises(AuthError):
            await auth.async_refresh()


async def test_refresh_without_token_raises_auth_error(
    session: aiohttp.ClientSession, fake_clock: FakeClock
) -> None:
    """Refreshing with no stored refresh token raises AuthError without I/O."""
    with aioresponses() as mocked:
        auth = AuthManager(session, time_func=fake_clock)
        with pytest.raises(AuthError):
            await auth.async_refresh()
        assert _refresh_calls(mocked) == []


async def test_garbage_access_token_forces_refresh(
    session: aiohttp.ClientSession, fake_clock: FakeClock
) -> None:
    """An unparsable access token is treated as expired and forces a refresh."""
    new_access = make_jwt(FAKE_NOW)
    with aioresponses() as mocked:
        mocked.post(
            REFRESH_URL,
            status=200,
            payload={
                "access_token": new_access,
                "refresh_token": "refresh-new",
            },
        )
        auth = AuthManager(session, time_func=fake_clock)
        auth.set_tokens("not-a-valid-jwt", "refresh-old")

        assert await auth.async_get_access_token() == new_access
        assert len(_refresh_calls(mocked)) == 1


async def test_set_tokens_does_not_fire_callback(
    session: aiohttp.ClientSession, fake_clock: FakeClock
) -> None:
    """Restoring persisted tokens must not trigger the update callback."""
    updates: list[tuple[str, str]] = []
    auth = AuthManager(
        session,
        time_func=fake_clock,
        on_token_update=lambda a, r: updates.append((a, r)),
    )
    auth.set_tokens(FRESH_TOKEN, "refresh-abc")

    assert updates == []


async def test_iqua2_base_url_is_used_for_requests(
    session: aiohttp.ClientSession, fake_clock: FakeClock
) -> None:
    """A manager built for the iQua2 host talks only to that host."""
    iqua2_login = f"{IQUA2_BASE_URL}/auth/login"
    with aioresponses() as mocked:
        mocked.post(iqua2_login, status=200, payload=_login_body(FRESH_TOKEN, "r1"))
        auth = AuthManager(session, base_url=IQUA2_BASE_URL, time_func=fake_clock)
        result = await auth.async_login("dev@example.com", "hunter2")

        # Every request went to the iQua2 host, never the default host.
        hosts = {url.host for (_method, url) in mocked.requests}

    assert result.access_token == FRESH_TOKEN
    assert hosts == {"api.iqua2.com"}
