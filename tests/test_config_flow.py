"""Tests for the AquaHome config, verification, and reauth flow.

Every test drives the real
:class:`~custom_components.aquahome.config_flow.AquaHomeConfigFlow` through Home
Assistant's flow manager while the iQua cloud is faked at the socket layer by
``aioresponses``. Only :func:`custom_components.aquahome.async_setup_entry` (and
its unload counterpart, for the reload a successful reauth triggers) is patched,
so the flow's two-host probing, device tie-break, verification challenge, and
error mapping all run exactly as in production. Access tokens are minted fresh
under a frozen clock so the probe's device-list call never races into a token
refresh.

A rate limit on either probe request — the login itself or the follow-up
``GET /devices`` — must stop the probe immediately: continuing to the second
host would hammer an already-throttled account. Both paths are covered below.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from aioresponses import aioresponses
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_USER, ConfigFlowResult
from homeassistant.const import (
    CONF_ACCESS_TOKEN,
    CONF_CODE,
    CONF_EMAIL,
    CONF_HOST,
    CONF_PASSWORD,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aquahome.api import (
    API_BASE_URL,
    IQUA2_BASE_URL,
    MAX_REFRESH_HOURS,
)
from custom_components.aquahome.const import CONF_REFRESH_TOKEN, DOMAIN
from tests.api.conftest import make_jwt
from tests.conftest import (
    TEST_EMAIL,
    TEST_PASSWORD,
    TEST_USER_ID,
    devices_url,
    load_fixture,
)

#: Password typed into the reauth form (differs from the stored one).
NEW_PASSWORD = "brand-new-password"
#: A second account's id, used to prove the wrong-account guard.
OTHER_USER_ID = "00000000-1111-2222-3333-444444444444"

#: Patch target for the integration's real entry setup, so flows never boot it.
_SETUP_TARGET = "custom_components.aquahome.async_setup_entry"
_UNLOAD_TARGET = "custom_components.aquahome.async_unload_entry"

#: Canned error bodies (``ApiErrorModel`` shape) the iQua cloud returns.
_BAD_CREDENTIALS = {
    "code": "AuthBadUsernameOrPassword",
    "detail": "Invalid username or password",
}
_UNVERIFIED = {"code": "UserNotVerified", "detail": "Account is not verified"}
_INVALID_CODE = {"code": "AuthInvalidCodeOrEmail", "detail": "Bad code or email"}
_THROTTLED = {"code": "ThrottleLimitExceeded", "detail": "Too many requests"}
_SERVER_ERROR = {"code": "InternalServerError", "detail": "Boom"}

#: An authenticated-but-empty ``GET /devices`` page.
_EMPTY_DEVICES: dict[str, Any] = {"page": 1, "per_page": 200, "total": 0, "data": []}

#: Default refresh token minted by the login helpers when none is supplied.
_DEFAULT_REFRESH_TOKEN = "refresh-new"


@pytest.fixture(autouse=True)
def _freeze_clock(freezer: FrozenDateTimeFactory) -> None:
    """Pin the wall clock so freshly minted access JWTs stay non-refreshing."""
    freezer.move_to("2026-07-21T12:00:00+00:00")


# ---------------------------------------------------------------------------
# Route + payload helpers (local; conftest is frozen)
# ---------------------------------------------------------------------------


def _login_payload(
    *,
    user_id: str = TEST_USER_ID,
    refresh_token: str | None = None,
    verified: bool = True,
) -> dict[str, Any]:
    """Build an ``AuthLoginOutputBody`` with a fresh, non-refreshing token."""
    return {
        "access_token": make_jwt(time.time()),
        "refresh_token": refresh_token or _DEFAULT_REFRESH_TOKEN,
        "user_id": user_id,
        "is_verified": verified,
    }


def _add_login_ok(
    mock: aioresponses,
    host: str,
    *,
    user_id: str = TEST_USER_ID,
    refresh_token: str | None = None,
) -> dict[str, Any]:
    """Register a successful login on ``host`` and return its payload."""
    payload = _login_payload(user_id=user_id, refresh_token=refresh_token)
    mock.post(f"{host}/auth/login", status=200, payload=payload)
    return payload


def _add_login_error(
    mock: aioresponses, host: str, *, status: int, body: dict[str, Any]
) -> None:
    """Register a login on ``host`` that returns an error body."""
    mock.post(f"{host}/auth/login", status=status, payload=body)


def _add_login_unreachable(mock: aioresponses, host: str) -> None:
    """Register a login on ``host`` that fails at the transport layer."""
    mock.post(f"{host}/auth/login", exception=aiohttp.ClientConnectionError())


def _add_devices(
    mock: aioresponses, host: str, *, payload: dict[str, Any] | None = None
) -> None:
    """Register a non-empty ``GET /devices`` list on ``host``."""
    mock.get(
        devices_url(host),
        status=200,
        payload=payload or load_fixture("devices-list.json"),
    )


def _add_devices_empty(mock: aioresponses, host: str) -> None:
    """Register an authenticated-but-empty ``GET /devices`` list on ``host``."""
    mock.get(devices_url(host), status=200, payload=_EMPTY_DEVICES)


def _add_devices_error(
    mock: aioresponses, host: str, *, status: int, body: dict[str, Any]
) -> None:
    """Register a ``GET /devices`` list on ``host`` that returns an error."""
    mock.get(devices_url(host), status=status, payload=body)


def _add_devices_unreachable(mock: aioresponses, host: str) -> None:
    """Register a ``GET /devices`` on ``host`` that fails at the transport layer."""
    mock.get(devices_url(host), exception=aiohttp.ClientConnectionError())


# ---------------------------------------------------------------------------
# Request-record inspection helpers
# ---------------------------------------------------------------------------


def _hosts_hit(mock: aioresponses) -> set[str | None]:
    """Return the set of hosts that received any recorded request."""
    return {url.host for (_method, url) in mock.requests}


def _count_posts(mock: aioresponses, path_suffix: str) -> int:
    """Return how many POST requests were recorded to ``path_suffix``."""
    return sum(
        len(calls)
        for (method, url), calls in mock.requests.items()
        if method == "POST" and url.path.endswith(path_suffix)
    )


def _first_login_body(mock: aioresponses) -> dict[str, Any]:
    """Return the JSON body of the first recorded ``POST /auth/login``."""
    for (method, url), calls in mock.requests.items():
        if method == "POST" and url.path.endswith("/auth/login"):
            body = calls[0].kwargs.get("json")
            assert isinstance(body, dict)
            return dict(body)
    msg = "no POST /auth/login was recorded"
    raise AssertionError(msg)


# ---------------------------------------------------------------------------
# Flow-driving helpers
# ---------------------------------------------------------------------------


async def _start_user_flow(hass: HomeAssistant) -> str:
    """Open the user step and return the active flow id."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert not result["errors"]
    return result["flow_id"]


async def _submit_credentials(
    hass: HomeAssistant, flow_id: str, *, password: str = TEST_PASSWORD
) -> ConfigFlowResult:
    """Submit the user-step credential form and return the next flow result."""
    return await hass.config_entries.flow.async_configure(
        flow_id, {CONF_EMAIL: TEST_EMAIL, CONF_PASSWORD: password}
    )


async def _start_reauth(
    hass: HomeAssistant, entry: MockConfigEntry
) -> ConfigFlowResult:
    """Begin a reauth flow for ``entry`` and return the reauth_confirm form."""
    result: ConfigFlowResult = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    # HA injects its own ``name`` placeholder for reauth; the flow adds ``email``.
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders[CONF_EMAIL] == TEST_EMAIL
    return result


# ---------------------------------------------------------------------------
# User step: form + happy paths
# ---------------------------------------------------------------------------


async def test_user_form_is_shown(hass: HomeAssistant) -> None:
    """The initial user step renders an empty credential form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}
    assert result["data_schema"] is not None


async def test_user_happy_path_legacy_host(
    hass: HomeAssistant, mock_api: aioresponses
) -> None:
    """A legacy-host login creates an entry with the exact data schema."""
    with patch(_SETUP_TARGET, return_value=True) as mock_setup:
        flow_id = await _start_user_flow(hass)
        payload = _add_login_ok(
            mock_api, API_BASE_URL, user_id=TEST_USER_ID, refresh_token="legacy-refresh"
        )
        _add_devices(mock_api, API_BASE_URL)
        result = await _submit_credentials(hass, flow_id)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == TEST_EMAIL
    assert result["data"] == {
        CONF_EMAIL: TEST_EMAIL,
        CONF_PASSWORD: TEST_PASSWORD,
        CONF_HOST: API_BASE_URL,
        CONF_ACCESS_TOKEN: payload["access_token"],
        CONF_REFRESH_TOKEN: "legacy-refresh",
    }
    assert result["result"].unique_id == TEST_USER_ID
    # The login body mimics the app: email, password, and the max refresh window.
    assert _first_login_body(mock_api) == {
        CONF_EMAIL: TEST_EMAIL,
        CONF_PASSWORD: TEST_PASSWORD,
        "refresh_hours": MAX_REFRESH_HOURS,
    }
    # iQua2 is never contacted once the legacy host authenticates with devices.
    assert "api.iqua2.com" not in _hosts_hit(mock_api)
    assert mock_setup.call_count == 1


async def test_iqua2_fallback_stores_iqua2_host(
    hass: HomeAssistant, mock_api: aioresponses
) -> None:
    """A legacy 401 falls back to iQua2 and stores the iQua2 host + tokens."""
    with patch(_SETUP_TARGET, return_value=True):
        flow_id = await _start_user_flow(hass)
        _add_login_error(mock_api, API_BASE_URL, status=401, body=_BAD_CREDENTIALS)
        payload = _add_login_ok(
            mock_api,
            IQUA2_BASE_URL,
            user_id=TEST_USER_ID,
            refresh_token="iqua2-refresh",
        )
        _add_devices(mock_api, IQUA2_BASE_URL)
        result = await _submit_credentials(hass, flow_id)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == IQUA2_BASE_URL
    assert result["data"][CONF_ACCESS_TOKEN] == payload["access_token"]
    assert result["data"][CONF_REFRESH_TOKEN] == "iqua2-refresh"


async def test_device_list_failure_falls_back_to_iqua2(
    hass: HomeAssistant, mock_api: aioresponses
) -> None:
    """A host that authenticates but cannot list devices loses to one that can."""
    with patch(_SETUP_TARGET, return_value=True):
        flow_id = await _start_user_flow(hass)
        # Legacy authenticates, then its device listing fails with a 5xx: the
        # probe must record that and keep going rather than give up on the host.
        _add_login_ok(mock_api, API_BASE_URL, user_id=TEST_USER_ID)
        _add_devices_error(mock_api, API_BASE_URL, status=500, body=_SERVER_ERROR)
        payload = _add_login_ok(
            mock_api,
            IQUA2_BASE_URL,
            user_id=TEST_USER_ID,
            refresh_token="iqua2-refresh",
        )
        _add_devices(mock_api, IQUA2_BASE_URL)
        result = await _submit_credentials(hass, flow_id)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == IQUA2_BASE_URL
    assert result["data"][CONF_ACCESS_TOKEN] == payload["access_token"]
    assert result["data"][CONF_REFRESH_TOKEN] == "iqua2-refresh"
    assert result["result"].unique_id == TEST_USER_ID
    assert {"api.myiquaapp.com", "api.iqua2.com"} <= _hosts_hit(mock_api)


async def test_device_tiebreak_prefers_host_with_devices(
    hass: HomeAssistant, mock_api: aioresponses
) -> None:
    """Both hosts authenticate, but the host with devices wins the tie-break."""
    with patch(_SETUP_TARGET, return_value=True):
        flow_id = await _start_user_flow(hass)
        # Legacy authenticates but owns no devices.
        _add_login_ok(mock_api, API_BASE_URL, user_id=OTHER_USER_ID)
        _add_devices_empty(mock_api, API_BASE_URL)
        # iQua2 authenticates and owns the device -> it must be chosen.
        payload = _add_login_ok(
            mock_api,
            IQUA2_BASE_URL,
            user_id=TEST_USER_ID,
            refresh_token="iqua2-refresh",
        )
        _add_devices(mock_api, IQUA2_BASE_URL)
        result = await _submit_credentials(hass, flow_id)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == IQUA2_BASE_URL
    assert result["data"][CONF_REFRESH_TOKEN] == "iqua2-refresh"
    assert result["result"].unique_id == TEST_USER_ID
    assert result["data"][CONF_ACCESS_TOKEN] == payload["access_token"]


async def test_both_hosts_empty_shows_no_devices(
    hass: HomeAssistant, mock_api: aioresponses
) -> None:
    """Both hosts authenticate but have no devices -> ``no_devices`` on user step."""
    flow_id = await _start_user_flow(hass)
    _add_login_ok(mock_api, API_BASE_URL, user_id=TEST_USER_ID)
    _add_devices_empty(mock_api, API_BASE_URL)
    _add_login_ok(mock_api, IQUA2_BASE_URL, user_id=OTHER_USER_ID)
    _add_devices_empty(mock_api, IQUA2_BASE_URL)
    result = await _submit_credentials(hass, flow_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "no_devices"}
    # Both hosts were probed before the first (legacy) empty outcome was accepted.
    assert {"api.myiquaapp.com", "api.iqua2.com"} <= _hosts_hit(mock_api)


# ---------------------------------------------------------------------------
# User step: probe-failure error mapping
# ---------------------------------------------------------------------------


def _both_reject_credentials(mock: aioresponses) -> None:
    """Both hosts return HTTP 401 for the login."""
    _add_login_error(mock, API_BASE_URL, status=401, body=_BAD_CREDENTIALS)
    _add_login_error(mock, IQUA2_BASE_URL, status=401, body=_BAD_CREDENTIALS)


def _both_unreachable(mock: aioresponses) -> None:
    """Neither host can be reached at all."""
    _add_login_unreachable(mock, API_BASE_URL)
    _add_login_unreachable(mock, IQUA2_BASE_URL)


def _legacy_401_iqua2_unreachable(mock: aioresponses) -> None:
    """Legacy rejects the credentials while iQua2 is unreachable."""
    _add_login_error(mock, API_BASE_URL, status=401, body=_BAD_CREDENTIALS)
    _add_login_unreachable(mock, IQUA2_BASE_URL)


def _both_device_lists_fail(mock: aioresponses) -> None:
    """Both hosts authenticate, but neither can return its device list."""
    _add_login_ok(mock, API_BASE_URL, user_id=TEST_USER_ID)
    _add_devices_error(mock, API_BASE_URL, status=500, body=_SERVER_ERROR)
    _add_login_ok(mock, IQUA2_BASE_URL, user_id=OTHER_USER_ID)
    _add_devices_unreachable(mock, IQUA2_BASE_URL)


@pytest.mark.parametrize(
    ("arrange", "expected_error"),
    [
        (_both_reject_credentials, "invalid_auth"),
        (_both_unreachable, "cannot_connect"),
        (_legacy_401_iqua2_unreachable, "cannot_connect"),
        (_both_device_lists_fail, "cannot_connect"),
    ],
    ids=[
        "both-401",
        "both-unreachable",
        "mixed-401-and-unreachable",
        "both-device-lists-fail",
    ],
)
async def test_user_step_probe_failures_map_to_form_errors(
    hass: HomeAssistant,
    mock_api: aioresponses,
    arrange: Callable[[aioresponses], None],
    expected_error: str,
) -> None:
    """Both-host failures map to the plan's single-error redraw on the user step."""
    flow_id = await _start_user_flow(hass)
    arrange(mock_api)
    result = await _submit_credentials(hass, flow_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": expected_error}


async def test_rate_limited_does_not_probe_iqua2(
    hass: HomeAssistant, mock_api: aioresponses
) -> None:
    """A throttled device poll surfaces ``rate_limited`` without touching iQua2."""
    flow_id = await _start_user_flow(hass)
    _add_login_ok(mock_api, API_BASE_URL, user_id=TEST_USER_ID)
    _add_devices_error(mock_api, API_BASE_URL, status=429, body=_THROTTLED)
    # Deliberately register NO iQua2 routes: any request there would fail loudly.
    result = await _submit_credentials(hass, flow_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "rate_limited"}
    assert "api.iqua2.com" not in _hosts_hit(mock_api)


async def test_rate_limited_login_does_not_probe_iqua2(
    hass: HomeAssistant, mock_api: aioresponses
) -> None:
    """A throttled login itself surfaces ``rate_limited`` without touching iQua2."""
    flow_id = await _start_user_flow(hass)
    _add_login_error(mock_api, API_BASE_URL, status=429, body=_THROTTLED)
    # Deliberately register NO iQua2 routes: any request there would fail loudly.
    result = await _submit_credentials(hass, flow_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "rate_limited"}
    assert "api.iqua2.com" not in _hosts_hit(mock_api)


# ---------------------------------------------------------------------------
# Unverified-account (verify) step
# ---------------------------------------------------------------------------


async def test_unverified_routes_to_verify_and_resends_code(
    hass: HomeAssistant, mock_api: aioresponses
) -> None:
    """An unverified login opens the verify step and requests a fresh code once."""
    flow_id = await _start_user_flow(hass)
    _add_login_error(mock_api, API_BASE_URL, status=403, body=_UNVERIFIED)
    mock_api.post(
        f"{API_BASE_URL}/auth/resend-confirmation-code",
        status=200,
        payload={"status": "ok"},
    )
    result = await _submit_credentials(hass, flow_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "verify"
    assert result["description_placeholders"] == {CONF_EMAIL: TEST_EMAIL}
    assert _count_posts(mock_api, "/auth/resend-confirmation-code") == 1
    # UserNotVerified is account state -> the second host is never probed.
    assert "api.iqua2.com" not in _hosts_hit(mock_api)


async def test_verify_wrong_code_shows_invalid_code(
    hass: HomeAssistant, mock_api: aioresponses
) -> None:
    """A rejected confirmation code redraws the verify step with ``invalid_code``."""
    flow_id = await _start_user_flow(hass)
    _add_login_error(mock_api, API_BASE_URL, status=403, body=_UNVERIFIED)
    mock_api.post(
        f"{API_BASE_URL}/auth/resend-confirmation-code",
        status=200,
        payload={"status": "ok"},
    )
    mock_api.post(
        f"{API_BASE_URL}/auth/validate-user", status=401, payload=_INVALID_CODE
    )
    challenge = await _submit_credentials(hass, flow_id)
    assert challenge["step_id"] == "verify"

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_CODE: "000000"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "verify"
    assert result["errors"] == {"base": "invalid_code"}


async def test_verify_correct_code_creates_entry(
    hass: HomeAssistant, mock_api: aioresponses
) -> None:
    """A correct code clears the challenge, re-probes, and creates the entry."""
    with patch(_SETUP_TARGET, return_value=True):
        flow_id = await _start_user_flow(hass)
        # First login is challenged; the re-probe login succeeds (FIFO ordering).
        _add_login_error(mock_api, API_BASE_URL, status=403, body=_UNVERIFIED)
        mock_api.post(
            f"{API_BASE_URL}/auth/resend-confirmation-code",
            status=200,
            payload={"status": "ok"},
        )
        mock_api.post(
            f"{API_BASE_URL}/auth/validate-user", status=200, payload={"status": "ok"}
        )
        payload = _add_login_ok(
            mock_api,
            API_BASE_URL,
            user_id=TEST_USER_ID,
            refresh_token="verified-refresh",
        )
        _add_devices(mock_api, API_BASE_URL)

        challenge = await _submit_credentials(hass, flow_id)
        assert challenge["step_id"] == "verify"
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_CODE: "123456"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == API_BASE_URL
    assert result["data"][CONF_REFRESH_TOKEN] == "verified-refresh"
    assert result["data"][CONF_ACCESS_TOKEN] == payload["access_token"]
    assert result["result"].unique_id == TEST_USER_ID
    assert _count_posts(mock_api, "/auth/validate-user") == 1


def _validate_user_throttled(mock: aioresponses) -> None:
    """Register a ``POST /auth/validate-user`` the server throttles."""
    mock.post(f"{API_BASE_URL}/auth/validate-user", status=429, payload=_THROTTLED)


def _validate_user_unreachable(mock: aioresponses) -> None:
    """Register a ``POST /auth/validate-user`` that fails at the transport layer."""
    mock.post(
        f"{API_BASE_URL}/auth/validate-user", exception=aiohttp.ClientConnectionError()
    )


@pytest.mark.parametrize(
    ("arrange", "expected_error"),
    [
        (_validate_user_throttled, "rate_limited"),
        (_validate_user_unreachable, "cannot_connect"),
    ],
    ids=["throttled", "unreachable"],
)
async def test_verify_transient_failure_redraws_then_retry_succeeds(
    hass: HomeAssistant,
    mock_api: aioresponses,
    arrange: Callable[[aioresponses], None],
    expected_error: str,
) -> None:
    """A code submission that gets no verdict redraws verify and stays usable.

    Neither a throttle nor an unreachable cloud says anything about the code, so
    the step must keep the challenge open: the retry below clears it.
    """
    with patch(_SETUP_TARGET, return_value=True):
        flow_id = await _start_user_flow(hass)
        _add_login_error(mock_api, API_BASE_URL, status=403, body=_UNVERIFIED)
        mock_api.post(
            f"{API_BASE_URL}/auth/resend-confirmation-code",
            status=200,
            payload={"status": "ok"},
        )
        # The first code submission fails; the retry is accepted (FIFO ordering).
        arrange(mock_api)
        mock_api.post(
            f"{API_BASE_URL}/auth/validate-user", status=200, payload={"status": "ok"}
        )
        payload = _add_login_ok(
            mock_api,
            API_BASE_URL,
            user_id=TEST_USER_ID,
            refresh_token="verified-refresh",
        )
        _add_devices(mock_api, API_BASE_URL)

        challenge = await _submit_credentials(hass, flow_id)
        assert challenge["step_id"] == "verify"
        blocked = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_CODE: "123456"}
        )
        assert blocked["type"] is FlowResultType.FORM
        assert blocked["step_id"] == "verify"
        assert blocked["errors"] == {"base": expected_error}
        assert blocked["description_placeholders"] == {CONF_EMAIL: TEST_EMAIL}

        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_CODE: "123456"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == API_BASE_URL
    assert result["data"][CONF_ACCESS_TOKEN] == payload["access_token"]
    assert result["data"][CONF_REFRESH_TOKEN] == "verified-refresh"
    assert _count_posts(mock_api, "/auth/validate-user") == 2
    # Only the challenged login and the post-verification re-probe: a submission
    # that never reached a verdict must not have re-probed the account.
    assert _count_posts(mock_api, "/auth/login") == 2


async def test_verify_reached_even_when_resend_fails(
    hass: HomeAssistant, mock_api: aioresponses
) -> None:
    """A failed resend is best-effort: the verify step is still shown."""
    flow_id = await _start_user_flow(hass)
    _add_login_error(mock_api, API_BASE_URL, status=403, body=_UNVERIFIED)
    mock_api.post(
        f"{API_BASE_URL}/auth/resend-confirmation-code",
        status=500,
        payload=_SERVER_ERROR,
    )
    result = await _submit_credentials(hass, flow_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "verify"
    assert _count_posts(mock_api, "/auth/resend-confirmation-code") == 1


# ---------------------------------------------------------------------------
# Duplicate account
# ---------------------------------------------------------------------------


async def test_duplicate_account_aborts(
    hass: HomeAssistant, mock_api: aioresponses, mock_config_entry: MockConfigEntry
) -> None:
    """Re-adding an already-configured account aborts as ``already_configured``."""
    mock_config_entry.add_to_hass(hass)
    flow_id = await _start_user_flow(hass)
    _add_login_ok(mock_api, API_BASE_URL, user_id=TEST_USER_ID)
    _add_devices(mock_api, API_BASE_URL)
    result = await _submit_credentials(hass, flow_id)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# Reauth
# ---------------------------------------------------------------------------


async def test_reauth_updates_credentials_and_reloads(
    hass: HomeAssistant, mock_api: aioresponses, mock_config_entry: MockConfigEntry
) -> None:
    """Reauth persists the new password + tokens and reloads the entry."""
    mock_config_entry.add_to_hass(hass)
    with (
        patch(_SETUP_TARGET, return_value=True) as mock_setup,
        patch(_UNLOAD_TARGET, return_value=True),
    ):
        result = await _start_reauth(hass, mock_config_entry)
        payload = _add_login_ok(
            mock_api, API_BASE_URL, user_id=TEST_USER_ID, refresh_token="reauth-refresh"
        )
        _add_devices(mock_api, API_BASE_URL)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: NEW_PASSWORD}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == NEW_PASSWORD
    assert mock_config_entry.data[CONF_HOST] == API_BASE_URL
    assert mock_config_entry.data[CONF_ACCESS_TOKEN] == payload["access_token"]
    assert mock_config_entry.data[CONF_REFRESH_TOKEN] == "reauth-refresh"
    assert mock_setup.call_count == 1


async def test_reauth_heals_host_migration(
    hass: HomeAssistant, mock_api: aioresponses, mock_config_entry: MockConfigEntry
) -> None:
    """Reauth flips a migrated account from the legacy host to iQua2."""
    mock_config_entry.add_to_hass(hass)
    assert mock_config_entry.data[CONF_HOST] == API_BASE_URL
    with (
        patch(_SETUP_TARGET, return_value=True),
        patch(_UNLOAD_TARGET, return_value=True),
    ):
        result = await _start_reauth(hass, mock_config_entry)
        _add_login_error(mock_api, API_BASE_URL, status=401, body=_BAD_CREDENTIALS)
        payload = _add_login_ok(
            mock_api,
            IQUA2_BASE_URL,
            user_id=TEST_USER_ID,
            refresh_token="iqua2-refresh",
        )
        _add_devices(mock_api, IQUA2_BASE_URL)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: NEW_PASSWORD}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_HOST] == IQUA2_BASE_URL
    assert mock_config_entry.data[CONF_ACCESS_TOKEN] == payload["access_token"]
    assert mock_config_entry.data[CONF_REFRESH_TOKEN] == "iqua2-refresh"


async def test_reauth_wrong_account_is_rejected(
    hass: HomeAssistant, mock_api: aioresponses, mock_config_entry: MockConfigEntry
) -> None:
    """Reauth with a different account's credentials errors ``wrong_account``."""
    mock_config_entry.add_to_hass(hass)
    result = await _start_reauth(hass, mock_config_entry)
    _add_login_ok(mock_api, API_BASE_URL, user_id=OTHER_USER_ID)
    _add_devices(mock_api, API_BASE_URL)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: NEW_PASSWORD}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "wrong_account"}
    # The stored credentials must be left untouched by a rejected reauth.
    assert mock_config_entry.data[CONF_PASSWORD] == TEST_PASSWORD
    assert mock_config_entry.data[CONF_HOST] == API_BASE_URL


async def test_reauth_completes_even_with_no_devices(
    hass: HomeAssistant, mock_api: aioresponses, mock_config_entry: MockConfigEntry
) -> None:
    """A now-empty account must not strand reauth: it updates and reloads anyway."""
    mock_config_entry.add_to_hass(hass)
    with (
        patch(_SETUP_TARGET, return_value=True),
        patch(_UNLOAD_TARGET, return_value=True),
    ):
        result = await _start_reauth(hass, mock_config_entry)
        payload = _add_login_ok(
            mock_api, API_BASE_URL, user_id=TEST_USER_ID, refresh_token="empty-reauth"
        )
        _add_devices_empty(mock_api, API_BASE_URL)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: NEW_PASSWORD}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == NEW_PASSWORD
    assert mock_config_entry.data[CONF_REFRESH_TOKEN] == "empty-reauth"
    assert mock_config_entry.data[CONF_ACCESS_TOKEN] == payload["access_token"]
