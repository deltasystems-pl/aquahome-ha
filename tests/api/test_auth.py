"""Unit tests for :mod:`custom_components.aquahome.api.auth`.

Pure aiohttp tests: a real :class:`aiohttp.ClientSession` with all HTTP mocked
by ``aioresponses``, a deterministic fake clock, and JWTs built by ``make_jwt``.
No Home Assistant core is involved.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from aioresponses import CallbackResult, aioresponses
from aioresponses.core import RequestCall
from yarl import URL

from custom_components.aquahome.api.auth import AuthManager, async_probe_host
from custom_components.aquahome.api.const import (
    API_BASE_URL,
    IQUA2_BASE_URL,
    MAX_REFRESH_HOURS,
)
from custom_components.aquahome.api.exceptions import (
    ApiError,
    AquaHomeConnectionError,
    AuthError,
    RateLimitError,
    UserNotVerifiedError,
)
from custom_components.aquahome.api.models import LoginResult
from tests.api.conftest import FAKE_NOW, FakeClock, make_jwt

LOGIN_URL = f"{API_BASE_URL}/auth/login"
REFRESH_URL = f"{API_BASE_URL}/auth/refresh"
VALIDATE_USER_URL = f"{API_BASE_URL}/auth/validate-user"
RESEND_CODE_URL = f"{API_BASE_URL}/auth/resend-confirmation-code"
IQUA2_LOGIN_URL = f"{IQUA2_BASE_URL}/auth/login"

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
        "user_id": "1c9f8a4e-2b6d-4c3a-9e5f-7a1b3c5d7e9f",
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
    assert result.user_id == "1c9f8a4e-2b6d-4c3a-9e5f-7a1b3c5d7e9f"
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


async def test_login_throttled_raises_rate_limit_error(
    session: aiohttp.ClientSession, fake_clock: FakeClock
) -> None:
    """A 429 (or throttle code) on login raises RateLimitError, not ApiError.

    The config-flow host probe stops probing entirely on RateLimitError; a
    throttled login mapped to a generic error would make it hammer the second
    host with a throttled account's credentials.
    """
    with aioresponses() as mocked:
        mocked.post(
            LOGIN_URL,
            status=429,
            payload={"code": "ThrottleLimitExceeded", "detail": "Slow down"},
        )
        auth = AuthManager(session, time_func=fake_clock)
        with pytest.raises(RateLimitError) as excinfo:
            await auth.async_login("dev@example.com", "pw")

    assert excinfo.value.status == 429
    assert excinfo.value.code == "ThrottleLimitExceeded"


async def test_login_throttle_code_without_429_still_rate_limit_error(
    session: aiohttp.ClientSession, fake_clock: FakeClock
) -> None:
    """The throttle code maps to RateLimitError even off a non-429 status."""
    with aioresponses() as mocked:
        mocked.post(
            LOGIN_URL,
            status=400,
            payload={"code": "ThrottleLimitExceeded", "detail": "Slow down"},
        )
        auth = AuthManager(session, time_func=fake_clock)
        with pytest.raises(RateLimitError):
            await auth.async_login("dev@example.com", "pw")


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
    """Ten concurrent stale-token callers cause exactly one serialized refresh.

    The mocked refresh suspends until it is explicitly released, so a lock-less
    implementation would let every caller's refresh run at the same time.
    Asserting a *peak* of one in-flight refresh — not merely one request — is
    what proves the single-flight lock works: aioresponses resolves without
    suspending, so a plain request count passes even with the lock removed
    (sanity-checked in development by neutralizing the lock, which flips
    ``max_in_flight`` to 10 and fails this test).
    """
    caller_count = 10
    # Pump the loop generously so every caller reaches its suspension point (the
    # single held refresh, or — correctly — the contended refresh lock) before
    # the refresh completes; the correct implementation peaks at one regardless.
    scheduler_pump_turns = caller_count * 3
    new_access = make_jwt(FAKE_NOW)
    in_flight = 0
    max_in_flight = 0
    release = asyncio.Event()

    async def refresh_callback(url: URL, **kwargs: Any) -> CallbackResult:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await release.wait()
        in_flight -= 1
        return CallbackResult(
            status=200,
            payload={"access_token": new_access, "refresh_token": "refresh-new"},
        )

    with aioresponses() as mocked:
        mocked.post(REFRESH_URL, callback=refresh_callback, repeat=True)
        auth = AuthManager(session, time_func=fake_clock)
        auth.set_tokens(STALE_TOKEN, "refresh-old")

        tasks = [
            asyncio.create_task(auth.async_get_access_token())
            for _ in range(caller_count)
        ]
        for _ in range(scheduler_pump_turns):
            await asyncio.sleep(0)
        release.set()

        tokens = await asyncio.gather(*tasks)

    assert tokens == [new_access] * caller_count
    assert len(_refresh_calls(mocked)) == 1
    assert max_in_flight == 1


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


# ---------------------------------------------------------------------------
# Request timeout bounds the lock-holding refresh
#
# aioresponses ignores ClientTimeout (it replaces ClientSession._request), so a
# real aiohttp server is used to prove the per-request timeout actually fires.
# ---------------------------------------------------------------------------


async def test_auth_request_carries_configured_timeout(
    session: aiohttp.ClientSession, fake_clock: FakeClock
) -> None:
    """Every auth request is bounded by ``timeout_seconds`` (socket-free guard).

    aioresponses cannot enforce the timeout, but it records the kwargs, so this
    proves the budget is actually handed to aiohttp — the regression was a
    refresh with *no* timeout holding the single-flight lock indefinitely.
    """
    with aioresponses() as mocked:
        mocked.post(
            REFRESH_URL,
            status=200,
            payload={"access_token": make_jwt(FAKE_NOW), "refresh_token": "r"},
        )
        auth = AuthManager(session, time_func=fake_clock, timeout_seconds=7.5)
        auth.set_tokens(STALE_TOKEN, "refresh-old")
        await auth.async_refresh()

        (call,) = _refresh_calls(mocked)

    timeout = call.kwargs["timeout"]
    assert isinstance(timeout, aiohttp.ClientTimeout)
    assert timeout.total == 7.5


async def test_refresh_timeout_raises_connection_error(
    fake_clock: FakeClock, socket_enabled: None
) -> None:
    """A stalled refresh fails as a connection error within the timeout budget."""
    request_timeout = 0.05
    server_delay = 1.0

    async def stalled_refresh(request: web.Request) -> web.Response:
        await asyncio.sleep(server_delay)
        return web.json_response(
            {"access_token": make_jwt(FAKE_NOW), "refresh_token": "r"}
        )

    app = web.Application()
    app.router.add_post("/auth/refresh", stalled_refresh)
    server = TestServer(app)
    await server.start_server()
    try:
        base_url = f"http://{server.host}:{server.port}"
        async with aiohttp.ClientSession() as client:
            auth = AuthManager(
                client,
                base_url=base_url,
                time_func=fake_clock,
                timeout_seconds=request_timeout,
            )
            auth.set_tokens(STALE_TOKEN, "refresh-old")
            started = time.monotonic()
            with pytest.raises(AquaHomeConnectionError):
                await auth.async_refresh()
            elapsed = time.monotonic() - started
    finally:
        await server.close()

    # It aborted on the timeout, well before the server would have answered.
    assert elapsed < server_delay


# ---------------------------------------------------------------------------
# Email confirmation-code login challenge
# ---------------------------------------------------------------------------


async def test_login_user_not_verified_raises_user_not_verified_error(
    session: aiohttp.ClientSession, fake_clock: FakeClock
) -> None:
    """A UserNotVerified challenge raises the dedicated AuthError subclass."""
    with aioresponses() as mocked:
        mocked.post(
            LOGIN_URL,
            status=403,
            payload={
                "code": "UserNotVerified",
                "detail": "Please verify your account.",
            },
        )
        auth = AuthManager(session, time_func=fake_clock)
        with pytest.raises(UserNotVerifiedError) as excinfo:
            await auth.async_login("dev@example.com", "hunter2")

    # It IS an AuthError so existing handlers still catch it, but is distinct.
    assert isinstance(excinfo.value, AuthError)
    assert excinfo.value.code == "UserNotVerified"
    assert excinfo.value.status == 403


async def test_validate_user_success_posts_email_and_code(
    session: aiohttp.ClientSession, fake_clock: FakeClock
) -> None:
    """Validating a user posts email+code with app-mimicry headers, no bearer."""
    with aioresponses() as mocked:
        mocked.post(VALIDATE_USER_URL, status=200, payload={"status": "ok"})
        auth = AuthManager(session, time_func=fake_clock)
        await auth.async_validate_user("dev@example.com", "1A2B3C4D")

        (call,) = _calls_for(mocked, "POST", "/auth/validate-user")

    assert call.kwargs["json"] == {"email": "dev@example.com", "code": "1A2B3C4D"}
    headers = call.kwargs["headers"]
    assert headers["User-Agent"] == "okhttp/4.9.2"
    assert headers["x-app-version"] == "version=1.5.2,build=2794"
    assert "Authorization" not in headers


async def test_validate_user_bad_code_raises_auth_error(
    session: aiohttp.ClientSession, fake_clock: FakeClock
) -> None:
    """A rejected code maps to AuthError (drives a Repairs re-login flow)."""
    with aioresponses() as mocked:
        mocked.post(
            VALIDATE_USER_URL,
            status=400,
            payload={"code": "AuthInvalidCodeOrEmail", "detail": "bad code"},
        )
        auth = AuthManager(session, time_func=fake_clock)
        with pytest.raises(AuthError) as excinfo:
            await auth.async_validate_user("dev@example.com", "wrong")

    assert not isinstance(excinfo.value, UserNotVerifiedError)
    assert excinfo.value.code == "AuthInvalidCodeOrEmail"


async def test_resend_confirmation_code_success_posts_email(
    session: aiohttp.ClientSession, fake_clock: FakeClock
) -> None:
    """Resending a confirmation code posts only the email address."""
    with aioresponses() as mocked:
        mocked.post(RESEND_CODE_URL, status=200, payload={"status": "ok"})
        auth = AuthManager(session, time_func=fake_clock)
        await auth.async_resend_confirmation_code("dev@example.com")

        (call,) = _calls_for(mocked, "POST", "/auth/resend-confirmation-code")

    assert call.kwargs["json"] == {"email": "dev@example.com"}


async def test_resend_confirmation_code_error_raises_auth_error(
    session: aiohttp.ClientSession, fake_clock: FakeClock
) -> None:
    """A rejected resend maps to AuthError via the shared error taxonomy."""
    with aioresponses() as mocked:
        mocked.post(
            RESEND_CODE_URL,
            status=401,
            payload={"code": "AuthInvalidCodeOrEmail", "detail": "no such email"},
        )
        auth = AuthManager(session, time_func=fake_clock)
        with pytest.raises(AuthError):
            await auth.async_resend_confirmation_code("nobody@example.com")


# ---------------------------------------------------------------------------
# Host probe (legacy myiquaapp → migrated iqua2)
# ---------------------------------------------------------------------------


async def test_probe_host_returns_first_authenticating_host(
    session: aiohttp.ClientSession,
) -> None:
    """The probe returns the first host that accepts the credentials."""
    with aioresponses() as mocked:
        mocked.post(LOGIN_URL, status=200, payload=_login_body(FRESH_TOKEN, "r1"))
        host, result = await async_probe_host(session, "dev@example.com", "hunter2")

    assert host == API_BASE_URL
    assert result.access_token == FRESH_TOKEN


async def test_probe_host_falls_through_to_iqua2_on_legacy_401(
    session: aiohttp.ClientSession,
) -> None:
    """A migrated account is rejected on legacy and authenticates on iqua2."""
    with aioresponses() as mocked:
        mocked.post(
            LOGIN_URL,
            status=401,
            payload={"code": "AuthBadUsernameOrPassword", "detail": "no"},
        )
        mocked.post(IQUA2_LOGIN_URL, status=200, payload=_login_body(FRESH_TOKEN, "r2"))
        host, result = await async_probe_host(session, "dev@example.com", "hunter2")

    assert host == IQUA2_BASE_URL
    assert result.refresh_token == "r2"


async def test_probe_host_both_hosts_401_raises_auth_error(
    session: aiohttp.ClientSession,
) -> None:
    """When both hosts reject the credentials, AuthError wins (invalid_auth)."""
    with aioresponses() as mocked:
        mocked.post(
            LOGIN_URL,
            status=401,
            payload={"code": "AuthBadUsernameOrPassword"},
        )
        mocked.post(
            IQUA2_LOGIN_URL,
            status=401,
            payload={"code": "AuthBadUsernameOrPassword"},
        )
        with pytest.raises(AuthError):
            await async_probe_host(session, "dev@example.com", "wrong")


async def test_probe_host_auth_error_wins_over_unreachable(
    session: aiohttp.ClientSession,
) -> None:
    """A 401 on one host and an unreachable other still surfaces AuthError."""
    with aioresponses() as mocked:
        mocked.post(
            LOGIN_URL,
            status=401,
            payload={"code": "AuthBadUsernameOrPassword"},
        )
        mocked.post(IQUA2_LOGIN_URL, exception=aiohttp.ClientConnectionError("down"))
        with pytest.raises(AuthError):
            await async_probe_host(session, "dev@example.com", "wrong")


async def test_probe_host_both_unreachable_raises_connection_error(
    session: aiohttp.ClientSession,
) -> None:
    """When no host is reachable, the connection error surfaces (cannot_connect)."""
    with aioresponses() as mocked:
        mocked.post(LOGIN_URL, exception=aiohttp.ClientConnectionError("down"))
        mocked.post(IQUA2_LOGIN_URL, exception=aiohttp.ClientConnectionError("down"))
        with pytest.raises(AquaHomeConnectionError):
            await async_probe_host(session, "dev@example.com", "hunter2")


async def test_probe_host_user_not_verified_aborts_before_second_host(
    session: aiohttp.ClientSession,
) -> None:
    """An unverified challenge on the first host aborts without trying iqua2."""
    with aioresponses() as mocked:
        mocked.post(
            LOGIN_URL,
            status=403,
            payload={"code": "UserNotVerified", "detail": "verify"},
        )
        # iqua2 is intentionally unmocked: any hit would prove the probe failed
        # to abort (an unmatched request is still recorded before it errors).
        with pytest.raises(UserNotVerifiedError):
            await async_probe_host(session, "dev@example.com", "hunter2")

        login_calls = _calls_for(mocked, "POST", "/auth/login")
        hosts = {url.host for (method, url) in mocked.requests if method == "POST"}

    assert len(login_calls) == 1
    assert hosts == {"api.myiquaapp.com"}
