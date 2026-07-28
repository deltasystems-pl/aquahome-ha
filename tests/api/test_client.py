"""Unit tests for :mod:`custom_components.aquahome.api.client`.

Pure aiohttp tests: a real :class:`aiohttp.ClientSession` with every socket
mocked by ``aioresponses``, a deterministic fake clock feeding the auth manager
a fresh token so no unexpected refresh fires, and the real captured fixtures
driving every parser. URLs are matched by regex so query strings can be asserted
separately from routing. No Home Assistant core is involved.
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest
from aioresponses import aioresponses
from aioresponses.core import RequestCall

from custom_components.aquahome.api.auth import AuthManager
from custom_components.aquahome.api.client import AquaHomeClient
from custom_components.aquahome.api.const import API_BASE_URL, IQUA2_BASE_URL
from custom_components.aquahome.api.exceptions import (
    ApiError,
    AquaHomeConnectionError,
    AuthError,
    ForbiddenCommandError,
    RateLimitError,
)
from custom_components.aquahome.api.models import (
    AlertsPage,
    CommandResult,
    DatapointGraph,
    Device,
    DeviceSettingsDocument,
    DeviceSummary,
    LiveTicket,
    RegenerationEventsPage,
    WaterTreatment,
)
from tests.api.conftest import FAKE_NOW, FakeClock, make_jwt
from tests.conftest import load_fixture

DEVICE_ID = "e5a7c1f3-8b2d-4e6a-b9c8-3d5f7a9b1c2e"
ACCESS_TOKEN = make_jwt(FAKE_NOW)
REFRESH_URL = f"{API_BASE_URL}/auth/refresh"
#: Europe/Warsaw summer offset carried by the datapoint fixture's period labels.
WARSAW = timezone(timedelta(hours=2))


class FakeMonotonic:
    """Advanceable monotonic clock for driving the live-ticket throttle."""

    def __init__(self, now: float = 1_000.0) -> None:
        """Start the clock at a fixed value."""
        self._now = now

    def __call__(self) -> float:
        """Return the current monotonic reading."""
        return self._now

    def advance(self, seconds: float) -> None:
        """Move the clock forward."""
        self._now += seconds


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    """Provide a real client session with all sockets mocked by aioresponses."""
    async with aiohttp.ClientSession() as client:
        yield client


def _make_client(
    session: aiohttp.ClientSession,
    *,
    base_url: str = API_BASE_URL,
    language: str = "pl",
    monotonic: Callable[[], float] = time.monotonic,
) -> AquaHomeClient:
    """Build a client whose auth already holds a fresh (non-refreshing) token."""
    auth = AuthManager(session, base_url=base_url, time_func=FakeClock())
    auth.set_tokens(ACCESS_TOKEN, "refresh-token")
    return AquaHomeClient(
        session, auth, base_url=base_url, language=language, monotonic=monotonic
    )


def _pattern(path: str, *, base: str = API_BASE_URL) -> re.Pattern[str]:
    """Build a URL regex matching ``base + path`` with any (or no) query string."""
    return re.compile("^" + re.escape(base) + path + r"(\?.*)?$")


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


# ---------------------------------------------------------------------------
# Request plumbing: headers, bearer token, accept-language
# ---------------------------------------------------------------------------


async def test_request_sends_mimicry_bearer_and_language_headers(
    session: aiohttp.ClientSession,
) -> None:
    """A real call carries app-mimicry, accept-language, and bearer headers."""
    with aioresponses() as mocked:
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}"),
            payload=load_fixture("device-detail.json"),
        )
        client = _make_client(session, language="pl")
        await client.async_get_device(DEVICE_ID)

        (call,) = _calls_for(mocked, "GET", f"/devices/{DEVICE_ID}")

    headers = call.kwargs["headers"]
    assert headers["User-Agent"] == "okhttp/4.9.2"
    assert headers["x-app-version"] == "version=1.5.2,build=2794"
    assert headers["accept"] == "application/json, text/plain, */*"
    assert headers["accept-language"] == "pl"
    assert headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"


# ---------------------------------------------------------------------------
# Endpoint parsing against real fixtures
# ---------------------------------------------------------------------------


async def test_get_device_parses_detail_fixture(
    session: aiohttp.ClientSession,
) -> None:
    """GET device returns a fully parsed Device with its 123 raw properties."""
    with aioresponses() as mocked:
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}"),
            payload=load_fixture("device-detail.json"),
        )
        client = _make_client(session)
        device = await client.async_get_device(DEVICE_ID)

        (call,) = _calls_for(mocked, "GET", f"/devices/{DEVICE_ID}")

    assert isinstance(device, Device)
    assert device.id == DEVICE_ID
    assert device.online is True
    assert len(device.properties) == 123
    assert device.enriched_data is not None
    assert device.enriched_data.features == ("regeneration",)
    # props defaults to true on the single-device poll.
    assert call.kwargs["params"] == {"props": "true"}


async def test_get_devices_single_page(session: aiohttp.ClientSession) -> None:
    """GET devices parses a one-page list into Device objects."""
    with aioresponses() as mocked:
        mocked.get(_pattern("/devices"), payload=load_fixture("devices-list.json"))
        client = _make_client(session)
        devices = await client.async_get_devices()

        calls = _calls_for(mocked, "GET", "/devices")

    assert len(calls) == 1
    assert calls[0].kwargs["params"] == {
        "page": "1",
        "per_page": "200",
        "props": "false",
    }
    assert len(devices) == 1
    assert devices[0].id == DEVICE_ID


async def test_get_devices_paginates_two_pages(
    session: aiohttp.ClientSession,
) -> None:
    """GET devices follows pagination until ``total`` devices are collected."""
    page_one = {
        "page": 1,
        "per_page": 1,
        "total": 2,
        "data": [{"id": "dev-1"}],
    }
    page_two = {
        "page": 2,
        "per_page": 1,
        "total": 2,
        "data": [{"id": "dev-2"}],
    }
    with aioresponses() as mocked:
        mocked.get(_pattern("/devices"), payload=page_one)
        mocked.get(_pattern("/devices"), payload=page_two)
        client = _make_client(session)
        devices = await client.async_get_devices(props=True)

        calls = _calls_for(mocked, "GET", "/devices")

    assert [d.id for d in devices] == ["dev-1", "dev-2"]
    assert [call.kwargs["params"]["page"] for call in calls] == ["1", "2"]
    assert calls[0].kwargs["params"]["props"] == "true"


async def test_get_devices_tolerates_null_data(
    session: aiohttp.ClientSession,
) -> None:
    """A ``data: null`` page yields an empty list with a single request."""
    with aioresponses() as mocked:
        mocked.get(
            _pattern("/devices"),
            payload={"page": 1, "per_page": 200, "total": 0, "data": None},
        )
        client = _make_client(session)
        devices = await client.async_get_devices()

        calls = _calls_for(mocked, "GET", "/devices")

    assert devices == []
    assert len(calls) == 1


async def test_get_enriched_data_unwraps_water_treatment(
    session: aiohttp.ClientSession,
) -> None:
    """GET enriched-data unwraps the ``water_treatment`` block into a model."""
    with aioresponses() as mocked:
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}/enriched-data"),
            payload=load_fixture("enriched-data.json"),
        )
        client = _make_client(session)
        treatment = await client.async_get_enriched_data(DEVICE_ID)

    assert isinstance(treatment, WaterTreatment)
    assert treatment.treatment_system_type == "softener"
    assert treatment.salt_level_percent == 37.5
    assert treatment.features == ("regeneration",)


async def test_get_properties_all_and_filtered(
    session: aiohttp.ClientSession,
) -> None:
    """GET properties returns the parsed map; a filter builds the CSV param."""
    with aioresponses() as mocked:
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}/properties"),
            payload=load_fixture("properties.json"),
        )
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}/properties"),
            payload=load_fixture("properties.json"),
        )
        client = _make_client(session)
        props = await client.async_get_properties(DEVICE_ID)
        await client.async_get_properties(
            DEVICE_ID, properties=["salt_level_tenths", "rf_signal_strength_dbm"]
        )

        calls = _calls_for(mocked, "GET", f"/devices/{DEVICE_ID}/properties")

    assert props["salt_level_tenths"].value == 30
    # No filter -> no params on the first call.
    assert calls[0].kwargs["params"] is None
    assert calls[1].kwargs["params"] == {
        "properties": "salt_level_tenths,rf_signal_strength_dbm"
    }


async def test_get_summary_parses_fixture(session: aiohttp.ClientSession) -> None:
    """GET summary returns a DeviceSummary with the nested user."""
    with aioresponses() as mocked:
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}/summary"),
            payload=load_fixture("summary.json"),
        )
        client = _make_client(session)
        summary = await client.async_get_summary(DEVICE_ID)

    assert isinstance(summary, DeviceSummary)
    assert summary.nickname == "Demo"
    assert summary.user is not None
    assert summary.user.email == "dev@example.com"


async def test_get_settings_returns_parsed_document(
    session: aiohttp.ClientSession,
) -> None:
    """GET settings returns a parsed DeviceSettingsDocument."""
    fixture = load_fixture("settings.json")
    with aioresponses() as mocked:
        mocked.get(_pattern(f"/devices/{DEVICE_ID}/settings"), payload=fixture)
        client = _make_client(session)
        settings = await client.async_get_settings(DEVICE_ID)

    assert isinstance(settings, DeviceSettingsDocument)
    assert len(settings.settings) == 18
    inlet = settings.get("inlet_hardness")
    assert inlet is not None
    assert inlet.current_value == "25.7"


async def test_get_alerts_parses_fixture(session: aiohttp.ClientSession) -> None:
    """GET alerts returns a parsed AlertsPage and forwards pagination params."""
    with aioresponses() as mocked:
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}/alerts"),
            payload=load_fixture("alerts.json"),
        )
        client = _make_client(session)
        page = await client.async_get_alerts(DEVICE_ID, page=1, per_page=20)

        (call,) = _calls_for(mocked, "GET", f"/devices/{DEVICE_ID}/alerts")

    assert isinstance(page, AlertsPage)
    assert page.total == 59
    assert len(page.alerts) == 20
    assert call.kwargs["params"] == {"page": "1", "per_page": "20"}


async def test_get_regeneration_events_parses_fixture(
    session: aiohttp.ClientSession,
) -> None:
    """GET regeneration-events returns a parsed page of events."""
    with aioresponses() as mocked:
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}/regeneration-events"),
            payload=load_fixture("regeneration-events.json"),
        )
        client = _make_client(session)
        page = await client.async_get_regeneration_events(DEVICE_ID)

    assert isinstance(page, RegenerationEventsPage)
    assert page.total == 18
    assert len(page.events) == 18


async def test_get_datapoint_graph_serializes_rfc3339_and_parses(
    session: aiohttp.ClientSession,
) -> None:
    """GET graph serializes offset-aware datetimes and parses the series."""
    start = datetime(2026, 7, 14, tzinfo=WARSAW)
    end = datetime(2026, 7, 22, tzinfo=WARSAW)
    with aioresponses() as mocked:
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}/datapoints/[^/]+/graph"),
            payload=load_fixture("graph-daily-usage.json"),
        )
        client = _make_client(session)
        graph = await client.async_get_datapoint_graph(
            DEVICE_ID,
            "total_outlet_water_gals",
            period_type="day",
            start=start,
            end=end,
            value_type="max_diff",
        )

        (call,) = _calls_for(mocked, "GET", "/graph")

    assert isinstance(graph, DatapointGraph)
    assert graph.units == "Liters"
    assert len(graph.data) == 8
    params = call.kwargs["params"]
    assert params["period_type"] == "day"
    assert params["value_type"] == "max_diff"
    assert params["keep_negatives"] == "false"
    assert params["start"] == "2026-07-14T00:00:00+02:00"
    assert params["end"] == "2026-07-22T00:00:00+02:00"


async def test_get_datapoint_summary_returns_raw(
    session: aiohttp.ClientSession,
) -> None:
    """GET datapoint summary returns the raw document with serialized dates."""
    start = datetime(2026, 7, 14, tzinfo=WARSAW)
    end = datetime(2026, 7, 22, tzinfo=WARSAW)
    with aioresponses() as mocked:
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}/datapoints/[^/]+/summary"),
            payload={"data": [{"period": "2026-07-14", "value": 185}]},
        )
        client = _make_client(session)
        summary = await client.async_get_datapoint_summary(
            DEVICE_ID,
            "total_outlet_water_gals",
            period_type="day",
            start=start,
            end=end,
        )

        (call,) = _calls_for(mocked, "GET", "/summary")

    assert summary == {"data": [{"period": "2026-07-14", "value": 185}]}
    assert call.kwargs["params"]["start"] == "2026-07-14T00:00:00+02:00"


async def test_send_command_posts_body_and_parses_result(
    session: aiohttp.ClientSession,
) -> None:
    """PUT command sends the function/action body and parses the result."""
    with aioresponses() as mocked:
        mocked.put(
            _pattern(f"/devices/{DEVICE_ID}/command"),
            payload={"status": "success", "message": "Queued"},
        )
        client = _make_client(session)
        result = await client.async_send_command(DEVICE_ID, "regenerate", "regenerate")

        (call,) = _calls_for(mocked, "PUT", f"/devices/{DEVICE_ID}/command")

    assert isinstance(result, CommandResult)
    assert result.status == "success"
    assert call.kwargs["json"] == {"function": "regenerate", "action": "regenerate"}


async def test_send_command_defaults_action_to_none(
    session: aiohttp.ClientSession,
) -> None:
    """An action-less command function sends the ignored ``none`` action."""
    with aioresponses() as mocked:
        mocked.put(
            _pattern(f"/devices/{DEVICE_ID}/command"),
            payload={"status": "success", "message": "ok"},
        )
        client = _make_client(session)
        await client.async_send_command(DEVICE_ID, "get_all_data")

        (call,) = _calls_for(mocked, "PUT", f"/devices/{DEVICE_ID}/command")

    assert call.kwargs["json"] == {"function": "get_all_data", "action": "none"}


async def test_forbidden_command_raises_without_any_request(
    session: aiohttp.ClientSession,
) -> None:
    """A forbidden command function raises before any HTTP call is made."""
    with aioresponses() as mocked:
        client = _make_client(session)
        with pytest.raises(ForbiddenCommandError):
            await client.async_send_command(DEVICE_ID, "reboot_system")

        assert mocked.requests == {}


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------


async def test_401_refreshes_and_retries_once_success(
    session: aiohttp.ClientSession,
) -> None:
    """A 401 triggers exactly one refresh and a successful retry."""
    with aioresponses() as mocked:
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}"),
            status=401,
            payload={"code": "Unauthorized", "detail": "token expired"},
        )
        mocked.post(
            REFRESH_URL,
            status=200,
            payload={
                "access_token": make_jwt(FAKE_NOW + 60),
                "refresh_token": "refresh-rotated",
            },
        )
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}"),
            status=200,
            payload=load_fixture("device-detail.json"),
        )
        client = _make_client(session)
        device = await client.async_get_device(DEVICE_ID)

        device_calls = _calls_for(mocked, "GET", f"/devices/{DEVICE_ID}")
        refresh_calls = _calls_for(mocked, "POST", "/auth/refresh")

    assert device.id == DEVICE_ID
    assert len(device_calls) == 2
    assert len(refresh_calls) == 1
    # The retry carried the freshly-refreshed bearer token.
    assert device_calls[1].kwargs["headers"]["Authorization"] == (
        f"Bearer {make_jwt(FAKE_NOW + 60)}"
    )


async def test_401_twice_raises_auth_error(
    session: aiohttp.ClientSession,
) -> None:
    """A second 401 after refresh surfaces as AuthError."""
    with aioresponses() as mocked:
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}"),
            status=401,
            payload={"code": "Unauthorized", "detail": "still bad"},
            repeat=True,
        )
        mocked.post(
            REFRESH_URL,
            status=200,
            payload={
                "access_token": make_jwt(FAKE_NOW + 60),
                "refresh_token": "refresh-rotated",
            },
        )
        client = _make_client(session)
        with pytest.raises(AuthError) as excinfo:
            await client.async_get_device(DEVICE_ID)

        refresh_calls = _calls_for(mocked, "POST", "/auth/refresh")

    assert excinfo.value.status == 401
    assert len(refresh_calls) == 1


async def test_429_raises_rate_limit_error_with_telemetry(
    session: aiohttp.ClientSession,
) -> None:
    """A 429 raises RateLimitError carrying the parsed rate-limit headers."""
    with aioresponses() as mocked:
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}"),
            status=429,
            headers={
                "ratelimit-limit": "5",
                "ratelimit-remaining": "0",
                "ratelimit-policy": "5;w=60;burst=50;policy=token_bucket",
            },
            payload={"code": "ThrottleLimitExceeded", "detail": "slow down"},
        )
        client = _make_client(session)
        with pytest.raises(RateLimitError) as excinfo:
            await client.async_get_device(DEVICE_ID)

    assert excinfo.value.status == 429
    assert excinfo.value.rate_limit is not None
    assert excinfo.value.rate_limit.limit == 5
    assert excinfo.value.rate_limit.remaining == 0
    assert excinfo.value.rate_limit.policy == "5;w=60;burst=50;policy=token_bucket"


async def test_throttle_code_without_429_maps_to_rate_limit(
    session: aiohttp.ClientSession,
) -> None:
    """The ThrottleLimitExceeded code maps to RateLimitError off any status."""
    with aioresponses() as mocked:
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}"),
            status=400,
            payload={"code": "ThrottleLimitExceeded", "detail": "slow down"},
        )
        client = _make_client(session)
        with pytest.raises(RateLimitError):
            await client.async_get_device(DEVICE_ID)


async def test_400_with_error_body_raises_api_error(
    session: aiohttp.ClientSession,
) -> None:
    """A 400 with an ApiErrorModel body raises ApiError with code and fields."""
    with aioresponses() as mocked:
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}"),
            status=400,
            payload={
                "code": "InvalidRequest",
                "detail": "bad device id",
                "fields": {"device_id": "invalid"},
            },
        )
        client = _make_client(session)
        with pytest.raises(ApiError) as excinfo:
            await client.async_get_device(DEVICE_ID)

    assert not isinstance(excinfo.value, (AuthError, RateLimitError))
    assert excinfo.value.status == 400
    assert excinfo.value.code == "InvalidRequest"
    assert excinfo.value.fields == {"device_id": "invalid"}
    assert "bad device id" in str(excinfo.value)


async def test_network_error_raises_connection_error(
    session: aiohttp.ClientSession,
) -> None:
    """A transport failure raises AquaHomeConnectionError."""
    with aioresponses() as mocked:
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}"),
            exception=aiohttp.ClientConnectionError("boom"),
        )
        client = _make_client(session)
        with pytest.raises(AquaHomeConnectionError):
            await client.async_get_device(DEVICE_ID)


# ---------------------------------------------------------------------------
# Rate-limit telemetry on success
# ---------------------------------------------------------------------------


async def test_rate_limit_headers_update_status(
    session: aiohttp.ClientSession,
) -> None:
    """Successful responses update ``client.rate_limit`` from the headers."""
    with aioresponses() as mocked:
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}"),
            headers={
                "ratelimit-limit": "5",
                "ratelimit-remaining": "4",
                "ratelimit-policy": "5;w=60;burst=50;policy=token_bucket",
            },
            payload=load_fixture("device-detail.json"),
        )
        client = _make_client(session)
        await client.async_get_device(DEVICE_ID)

    assert client.rate_limit is not None
    assert client.rate_limit.limit == 5
    assert client.rate_limit.remaining == 4
    assert client.rate_limit.policy == "5;w=60;burst=50;policy=token_bucket"


async def test_malformed_rate_limit_headers_never_raise(
    session: aiohttp.ClientSession,
) -> None:
    """Garbage numeric rate-limit headers collapse to None instead of raising."""
    with aioresponses() as mocked:
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}"),
            headers={"ratelimit-limit": "abc", "ratelimit-remaining": ""},
            payload=load_fixture("device-detail.json"),
        )
        client = _make_client(session)
        await client.async_get_device(DEVICE_ID)

    assert client.rate_limit is not None
    assert client.rate_limit.limit is None
    assert client.rate_limit.remaining is None


# ---------------------------------------------------------------------------
# Live-ticket throttle
# ---------------------------------------------------------------------------


async def test_live_ticket_throttles_then_recovers(
    session: aiohttp.ClientSession,
) -> None:
    """A second live-ticket call within 60 s raises; it recovers after the floor."""
    clock = FakeMonotonic()
    with aioresponses() as mocked:
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}/live"),
            payload={"websocket_uri": "/ws/?p=ticket-a"},
            repeat=True,
        )
        client = _make_client(session, monotonic=clock)

        first = await client.async_get_live_ticket(
            DEVICE_ID, ["current_water_flow_gpm"]
        )
        assert isinstance(first, LiveTicket)
        assert first.websocket_uri == "/ws/?p=ticket-a"

        clock.advance(30)
        with pytest.raises(RateLimitError):
            await client.async_get_live_ticket(DEVICE_ID, ["current_water_flow_gpm"])

        clock.advance(31)
        again = await client.async_get_live_ticket(
            DEVICE_ID, ["current_water_flow_gpm"]
        )
        assert isinstance(again, LiveTicket)

        live_calls = _calls_for(mocked, "GET", f"/devices/{DEVICE_ID}/live")

    # Only the two allowed calls reached the network; the throttled one did not.
    assert len(live_calls) == 2
    assert live_calls[0].kwargs["params"] == {
        "properties": "current_water_flow_gpm",
        "type": "property",
    }


# ---------------------------------------------------------------------------
# Alternate host (iQua2)
# ---------------------------------------------------------------------------


async def test_iqua2_base_url_is_used_for_requests(
    session: aiohttp.ClientSession,
) -> None:
    """A client built for the iQua2 host talks only to that host."""
    with aioresponses() as mocked:
        mocked.get(
            _pattern("/devices", base=IQUA2_BASE_URL),
            payload=load_fixture("devices-list.json"),
        )
        client = _make_client(session, base_url=IQUA2_BASE_URL)
        devices = await client.async_get_devices()

        hosts = {url.host for (_method, url) in mocked.requests}

    assert len(devices) == 1
    assert hosts == {"api.iqua2.com"}


# ---------------------------------------------------------------------------
# Settings PATCH round-trip
# ---------------------------------------------------------------------------


async def test_update_settings_patches_and_returns_document(
    session: aiohttp.ClientSession,
) -> None:
    """PATCH settings sends ``{settings: {...}}`` and returns the parsed document."""
    fixture = load_fixture("settings.json")
    with aioresponses() as mocked:
        mocked.patch(_pattern(f"/devices/{DEVICE_ID}/settings"), payload=fixture)
        client = _make_client(session)
        result = await client.async_update_settings(
            DEVICE_ID, {"inlet_hardness": "7.0"}
        )

        (call,) = _calls_for(mocked, "PATCH", f"/devices/{DEVICE_ID}/settings")

    # The refreshed DeviceSettingsBody document is parsed from the echoed body.
    assert isinstance(result, DeviceSettingsDocument)
    assert len(result.settings) == 18
    inlet = result.get("inlet_hardness")
    assert inlet is not None
    assert inlet.current_value == "25.7"
    # The request wraps the update map under a single ``settings`` key.
    assert call.kwargs["json"] == {"settings": {"inlet_hardness": "7.0"}}


# ---------------------------------------------------------------------------
# Rate-limit backoff (shared client-level throttle)
# ---------------------------------------------------------------------------


async def test_429_arms_backoff_and_next_call_raises_without_io(
    session: aiohttp.ClientSession,
) -> None:
    """After a 429 the client refuses the next request with no HTTP round-trip."""
    clock = FakeMonotonic()
    with aioresponses() as mocked:
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}"),
            status=429,
            headers={
                "ratelimit-limit": "5",
                "ratelimit-remaining": "0",
                "ratelimit-policy": "5;w=60;burst=50;policy=token_bucket",
            },
            payload={"code": "ThrottleLimitExceeded", "detail": "slow down"},
        )
        client = _make_client(session, monotonic=clock)

        with pytest.raises(RateLimitError):
            await client.async_get_device(DEVICE_ID)

        # Clock frozen: the 12 s refill window is still open. A second call must
        # fail fast — reaching the network would hit an unmatched mock instead.
        with pytest.raises(RateLimitError) as excinfo:
            await client.async_get_device(DEVICE_ID)

        calls = _calls_for(mocked, "GET", f"/devices/{DEVICE_ID}")

    assert len(calls) == 1
    # The client-side refusal still carries the last-seen telemetry.
    assert excinfo.value.rate_limit is not None
    assert excinfo.value.rate_limit.remaining == 0


async def test_backoff_clears_once_the_refill_window_elapses(
    session: aiohttp.ClientSession,
) -> None:
    """Advancing the injected monotonic past the refill window lets calls flow."""
    clock = FakeMonotonic()
    with aioresponses() as mocked:
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}"),
            status=429,
            headers={"ratelimit-policy": "5;w=60;burst=50;policy=token_bucket"},
            payload={"code": "ThrottleLimitExceeded", "detail": "slow down"},
        )
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}"),
            payload=load_fixture("device-detail.json"),
        )
        client = _make_client(session, monotonic=clock)

        with pytest.raises(RateLimitError):
            await client.async_get_device(DEVICE_ID)

        clock.advance(13)  # past the 60 / 5 == 12 s refill window
        device = await client.async_get_device(DEVICE_ID)

        calls = _calls_for(mocked, "GET", f"/devices/{DEVICE_ID}")

    assert device.id == DEVICE_ID
    assert len(calls) == 2


async def test_garbage_policy_falls_back_to_default_backoff(
    session: aiohttp.ClientSession,
) -> None:
    """An unparsable policy backs off for the default constant, not 12 s."""
    clock = FakeMonotonic()
    with aioresponses() as mocked:
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}"),
            status=429,
            headers={"ratelimit-policy": "not-a-policy"},
            payload={"code": "ThrottleLimitExceeded", "detail": "slow down"},
        )
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}"),
            payload=load_fixture("device-detail.json"),
        )
        client = _make_client(session, monotonic=clock)

        with pytest.raises(RateLimitError):
            await client.async_get_device(DEVICE_ID)

        # A 12 s refill would already be clear; the 60 s default is not.
        clock.advance(30)
        with pytest.raises(RateLimitError):
            await client.async_get_device(DEVICE_ID)

        # Past the 60 s default the window clears and traffic resumes.
        clock.advance(31)
        device = await client.async_get_device(DEVICE_ID)

        calls = _calls_for(mocked, "GET", f"/devices/{DEVICE_ID}")

    assert device.id == DEVICE_ID
    # The blocked middle call never reached the network.
    assert len(calls) == 2


async def test_live_429_does_not_freeze_the_rest_domain(
    session: aiohttp.ClientSession,
) -> None:
    """A /live throttle response must not arm the shared REST backoff."""
    clock = FakeMonotonic()
    with aioresponses() as mocked:
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}/live"),
            status=429,
            headers={"ratelimit-policy": "6;w=600;burst=60"},
            payload={"code": "ThrottleLimitExceeded", "detail": "live budget"},
        )
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}"),
            payload=load_fixture("device-detail.json"),
        )
        client = _make_client(session, monotonic=clock)

        with pytest.raises(RateLimitError):
            await client.async_get_live_ticket(DEVICE_ID, ["total_outlet_water_gals"])

        # Clock frozen: had the /live 429 armed the shared backoff (100 s
        # refill), this poll would fail fast. The REST domain must stay open.
        device = await client.async_get_device(DEVICE_ID)

    assert device.id == DEVICE_ID


async def test_rest_backoff_does_not_gate_the_live_domain(
    session: aiohttp.ClientSession,
) -> None:
    """An armed REST backoff leaves /live governed only by its own interval."""
    clock = FakeMonotonic()
    with aioresponses() as mocked:
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}"),
            status=429,
            headers={"ratelimit-policy": "5;w=60;burst=50;policy=token_bucket"},
            payload={"code": "ThrottleLimitExceeded", "detail": "slow down"},
        )
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}/live"),
            payload={"websocket_uri": "/ws/?p=ticket-a"},
        )
        client = _make_client(session, monotonic=clock)

        with pytest.raises(RateLimitError):
            await client.async_get_device(DEVICE_ID)

        # REST is backing off (12 s window, clock frozen) — /live still flows,
        # subject only to its own minimum-interval throttle.
        ticket = await client.async_get_live_ticket(
            DEVICE_ID, ["total_outlet_water_gals"]
        )

    assert ticket.websocket_uri == "/ws/?p=ticket-a"


# ---------------------------------------------------------------------------
# Per-request accept-language override (datapoint graphs)
# ---------------------------------------------------------------------------


async def test_datapoint_graph_language_overrides_only_its_own_request(
    session: aiohttp.ClientSession,
) -> None:
    """A pinned graph language does not leak into any later request."""
    start = datetime(2026, 7, 20, tzinfo=WARSAW)
    end = datetime(2026, 7, 27, tzinfo=WARSAW)
    with aioresponses() as mocked:
        # The server localizes the ``units`` string from accept-language, so the
        # two graph routes are the real English and Polish captures in the order
        # the two calls consume them.
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}/datapoints/[^/]+/graph"),
            payload=load_fixture("graph-meter-daily.json"),
        )
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}/datapoints/[^/]+/graph"),
            payload=load_fixture("graph-usage-daily-pl.json"),
        )
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}"),
            payload=load_fixture("device-detail.json"),
        )
        client = _make_client(session, language="pl")
        pinned = await client.async_get_datapoint_graph(
            DEVICE_ID,
            "total_outlet_water_gals",
            period_type="day",
            start=start,
            end=end,
            value_type="max",
            language="en",
        )
        localized = await client.async_get_datapoint_graph(
            DEVICE_ID,
            "total_outlet_water_gals",
            period_type="day",
            start=start,
            end=end,
            value_type="max_diff",
        )
        await client.async_get_device(DEVICE_ID)

        graph_calls = _calls_for(mocked, "GET", "/graph")
        (device_call,) = _calls_for(mocked, "GET", f"/devices/{DEVICE_ID}")

    assert [call.kwargs["headers"]["accept-language"] for call in graph_calls] == [
        "en",
        "pl",
    ]
    # A plain follow-up call still speaks the client's own language.
    assert device_call.kwargs["headers"]["accept-language"] == "pl"
    # ...which is exactly why the backfill pins one: the same field comes back
    # localized, and only the pinned response is parseable.
    assert pinned.units == "Liters"
    assert localized.units == "Litry"


async def test_datapoint_graph_without_language_keeps_the_client_language(
    session: aiohttp.ClientSession,
) -> None:
    """``language=None`` sends no override, leaving the mimicry headers intact."""
    start = datetime(2026, 7, 20, tzinfo=WARSAW)
    end = datetime(2026, 7, 27, tzinfo=WARSAW)
    with aioresponses() as mocked:
        mocked.get(
            _pattern(f"/devices/{DEVICE_ID}/datapoints/[^/]+/graph"),
            payload=load_fixture("graph-meter-daily.json"),
        )
        client = _make_client(session, language="pl")
        graph = await client.async_get_datapoint_graph(
            DEVICE_ID,
            "total_outlet_water_gals",
            period_type="day",
            start=start,
            end=end,
            value_type="max",
            language=None,
        )

        (call,) = _calls_for(mocked, "GET", "/graph")

    assert isinstance(graph, DatapointGraph)
    headers = call.kwargs["headers"]
    assert headers["accept-language"] == "pl"
    assert headers["User-Agent"] == "okhttp/4.9.2"
    assert headers["x-app-version"] == "version=1.5.2,build=2794"
    assert headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
