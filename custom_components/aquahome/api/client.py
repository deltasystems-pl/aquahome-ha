"""High-level async client for the iQua cloud API (api.myiquaapp.com).

:class:`AquaHomeClient` wraps an :class:`~.auth.AuthManager` and the device
endpoints behind typed, tolerant methods that return the models in
:mod:`.models`. Every request carries the app-mimicry headers plus a bearer
token, transparently refreshes an expired token exactly once, records the
server's rate-limit telemetry, and maps failures onto :mod:`.exceptions`.

The client is deliberately conservative: it refuses to send device-endangering
commands (see :data:`~.const.FORBIDDEN_COMMAND_FUNCTIONS`) before any I/O and
throttles the live-ticket endpoint client-side. Tokens are never logged.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, NoReturn

import aiohttp

from .const import (
    ACCEPT_HEADER,
    API_BASE_URL,
    APP_USER_AGENT,
    APP_VERSION_HEADER,
    COMMAND_FUNCTIONS,
    DEFAULT_RATE_LIMIT_BACKOFF_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    DEVICES_PER_PAGE,
    FORBIDDEN_COMMAND_FUNCTIONS,
    LIVE_TICKET_MIN_INTERVAL_SECONDS,
)
from .exceptions import (
    ApiError,
    AquaHomeConnectionError,
    AuthError,
    ForbiddenCommandError,
    RateLimitError,
)
from .models import (
    AlertsPage,
    CommandResult,
    DatapointGraph,
    Device,
    DeviceSettingsDocument,
    DeviceSummary,
    LiveTicket,
    PropertyValue,
    RateLimitStatus,
    RegenerationEventsPage,
    WaterTreatment,
)

if TYPE_CHECKING:
    from datetime import datetime

    from .auth import AuthManager

_LOGGER = logging.getLogger(__name__)

#: Machine-readable error code the API returns when it throttles a request.
_THROTTLE_CODE = "ThrottleLimitExceeded"
#: Error codes that map to :class:`AuthError` regardless of the HTTP status.
_AUTH_ERROR_CODES = frozenset({"AuthBadUsernameOrPassword", "AuthCannotRefreshToken"})
#: Action sent for command functions whose ``action`` field the API ignores.
DEFAULT_COMMAND_ACTION = "none"


def _encode_params(params: Mapping[str, Any]) -> dict[str, str]:
    """Encode query parameters as strings, formatting bools as ``true``/``false``.

    ``None`` values are dropped so optional filters can be passed uniformly.
    """
    encoded: dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            encoded[key] = "true" if value else "false"
        else:
            encoded[key] = str(value)
    return encoded


def _coerce_int(value: Any) -> int | None:
    """Return ``value`` when it is a non-boolean integer, else ``None``."""
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _header_int(value: str | None) -> int | None:
    """Parse an integer header value, returning ``None`` when it is malformed."""
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


class AquaHomeClient:
    """Typed async client for a single iQua account's devices."""

    def __init__(  # noqa: PLR0913 - deliberate dependency-injection signature
        self,
        session: aiohttp.ClientSession,
        auth: AuthManager,
        *,
        base_url: str = API_BASE_URL,
        language: str = "en",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the client to an aiohttp session, auth manager, and API host.

        ``language`` is sent as ``accept-language`` on every request; the server
        localizes display strings and units from it. ``monotonic`` is injected
        so the live-ticket throttle can be driven deterministically in tests.
        """
        self._session = session
        self._auth = auth
        self._base_url = base_url
        self._language = language
        self._timeout_seconds = timeout_seconds
        self._monotonic = monotonic
        #: Latest rate-limit telemetry seen on any response (``None`` until one).
        self.rate_limit: RateLimitStatus | None = None
        self._last_live_ticket_at: float | None = None
        #: Monotonic deadline until which every request is refused client-side
        #: after a 429, so a throttled account is not hammered further.
        self._backoff_until: float | None = None

    # -- Devices -----------------------------------------------------------

    async def async_get_devices(self, *, props: bool = False) -> list[Device]:
        """Return every device the account can access, following pagination.

        Pages ``GET /devices`` until ``total`` devices have been collected,
        tolerating a ``null`` ``data`` array (returned as an empty page).
        """
        devices: list[Device] = []
        page = 1
        while True:
            body = await self._request(
                "GET",
                "/devices",
                params={
                    "page": page,
                    "per_page": DEVICES_PER_PAGE,
                    "props": props,
                },
            )
            data = body.get("data")
            if not isinstance(data, list) or not data:
                break
            devices.extend(
                Device.from_dict(item) for item in data if isinstance(item, dict)
            )
            total = _coerce_int(body.get("total"))
            if total is None or len(devices) >= total:
                break
            page += 1
        return devices

    async def async_get_device(self, device_id: str, *, props: bool = True) -> Device:
        """Return the full device view — the primary coordinator poll source."""
        body = await self._request(
            "GET", f"/devices/{device_id}", params={"props": props}
        )
        return Device.from_dict(body)

    async def async_get_enriched_data(self, device_id: str) -> WaterTreatment:
        """Return the curated ``water_treatment`` block for a device."""
        body = await self._request("GET", f"/devices/{device_id}/enriched-data")
        water_treatment = body.get("water_treatment")
        return WaterTreatment.from_dict(
            water_treatment if isinstance(water_treatment, dict) else {}
        )

    async def async_get_properties(
        self, device_id: str, *, properties: Iterable[str] | None = None
    ) -> dict[str, PropertyValue]:
        """Return the raw property map, optionally filtered to ``properties``."""
        params: dict[str, Any] = {}
        if properties is not None:
            params["properties"] = ",".join(properties)
        body = await self._request(
            "GET", f"/devices/{device_id}/properties", params=params or None
        )
        raw = body.get("properties")
        result: dict[str, PropertyValue] = {}
        if isinstance(raw, dict):
            for name, item in raw.items():
                if isinstance(item, dict):
                    result[name] = PropertyValue.from_dict(item)
        return result

    async def async_get_summary(self, device_id: str) -> DeviceSummary:
        """Return the identity-only device summary."""
        body = await self._request("GET", f"/devices/{device_id}/summary")
        return DeviceSummary.from_dict(body)

    async def async_get_settings(self, device_id: str) -> DeviceSettingsDocument:
        """Return the parsed device settings document (DeviceSettingsBody)."""
        body = await self._request("GET", f"/devices/{device_id}/settings")
        return DeviceSettingsDocument.from_dict(body)

    async def async_update_settings(
        self, device_id: str, settings: Mapping[str, Any]
    ) -> DeviceSettingsDocument:
        """Update device settings and return the refreshed settings document.

        Sends ``PATCH /devices/{id}/settings`` with a
        ``{"settings": {name: value}}`` body (spec ``DeviceSettingsUpdateBody``)
        and returns the parsed ``DeviceSettingsBody`` document the server echoes
        back, so a write and its reconcile happen in a single round-trip.

        The client is a thin transport: it forwards ``settings`` verbatim.
        Validating each value against the setting's own ``select_rules`` /
        ``NumberRule`` — including number precision-expansion (e.g. ``12.5``
        grains -> ``125``) — is the responsibility of the Phase-4 entity layer,
        NOT this method.
        """
        body = await self._request(
            "PATCH",
            f"/devices/{device_id}/settings",
            json_body={"settings": dict(settings)},
        )
        return DeviceSettingsDocument.from_dict(body)

    async def async_get_alerts(
        self, device_id: str, *, page: int = 1, per_page: int = 20
    ) -> AlertsPage:
        """Return one page of the device alert history."""
        body = await self._request(
            "GET",
            f"/devices/{device_id}/alerts",
            params={"page": page, "per_page": per_page},
        )
        return AlertsPage.from_dict(body)

    async def async_get_regeneration_events(
        self, device_id: str, *, page: int = 1, per_page: int = 20
    ) -> RegenerationEventsPage:
        """Return one page of the device regeneration history."""
        body = await self._request(
            "GET",
            f"/devices/{device_id}/regeneration-events",
            params={"page": page, "per_page": per_page},
        )
        return RegenerationEventsPage.from_dict(body)

    async def async_get_datapoint_graph(  # noqa: PLR0913 - deliberate query signature
        self,
        device_id: str,
        property_name: str,
        *,
        period_type: str,
        start: datetime,
        end: datetime,
        value_type: str,
        keep_negatives: bool = False,
        language: str | None = None,
    ) -> DatapointGraph:
        """Return a datapoint graph series for a device property.

        ``start`` and ``end`` are serialized RFC3339 with their offset; the
        server aligns periods to the timezone carried by ``start``. ``language``
        overrides the client-level ``accept-language`` for this request only —
        the response ``units`` string is server-localized, so a caller that
        parses it pins a known language instead of the account's UI locale.
        """
        body = await self._request(
            "GET",
            f"/devices/{device_id}/datapoints/{property_name}/graph",
            params={
                "period_type": period_type,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "value_type": value_type,
                "keep_negatives": keep_negatives,
            },
            language=language,
        )
        return DatapointGraph.from_dict(body)

    async def async_get_datapoint_summary(
        self,
        device_id: str,
        property_name: str,
        *,
        period_type: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        """Return the raw per-period datapoint summary document."""
        return await self._request(
            "GET",
            f"/devices/{device_id}/datapoints/{property_name}/summary",
            params={
                "period_type": period_type,
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )

    async def async_send_command(
        self, device_id: str, function: str, action: str = DEFAULT_COMMAND_ACTION
    ) -> CommandResult:
        """Send a command to the device, refusing forbidden functions first.

        Functions in :data:`~.const.FORBIDDEN_COMMAND_FUNCTIONS` raise
        :class:`ForbiddenCommandError` before any I/O. Unknown functions or
        undocumented actions are allowed through (they are live-tested in a
        later phase) but logged at warning level.
        """
        if function in FORBIDDEN_COMMAND_FUNCTIONS:
            msg = f"Refusing to send forbidden command function {function!r}"
            raise ForbiddenCommandError(msg)
        self._validate_command(function, action)
        body = await self._request(
            "PUT",
            f"/devices/{device_id}/command",
            json_body={"function": function, "action": action},
        )
        return CommandResult.from_dict(body)

    async def async_get_live_ticket(
        self,
        device_id: str,
        properties: Iterable[str],
        *,
        type_: str = "property",
    ) -> LiveTicket:
        """Return a websocket ticket for live streaming, throttled client-side.

        Raises :class:`RateLimitError` when called within
        :data:`~.const.LIVE_TICKET_MIN_INTERVAL_SECONDS` of the previous ticket,
        since the endpoint is server-throttled and must not be polled.
        """
        now = self._monotonic()
        last = self._last_live_ticket_at
        if last is not None and now - last < LIVE_TICKET_MIN_INTERVAL_SECONDS:
            msg = "Live-ticket requests are throttled; try again shortly"
            raise RateLimitError(msg, rate_limit=self.rate_limit)
        self._last_live_ticket_at = now
        # /live is a SEPARATE server throttle domain with its own small budget
        # (~6 requests / 10 min — see automation-gap-analysis.md §7 D3): a /live
        # 429 must not freeze the primary device poll, and a REST backoff must
        # not gate /live, so the shared backoff is bypassed in both directions.
        body = await self._request(
            "GET",
            f"/devices/{device_id}/live",
            params={"properties": ",".join(properties), "type": type_},
            shared_backoff=False,
        )
        return LiveTicket.from_dict(body)

    # -- Command validation ------------------------------------------------

    @staticmethod
    def _validate_command(function: str, action: str) -> None:
        """Warn about undocumented command functions or actions (never raise)."""
        allowed = COMMAND_FUNCTIONS.get(function)
        if allowed is None:
            _LOGGER.warning("Sending undocumented command function %s", function)
            return
        if allowed and action not in allowed and action != DEFAULT_COMMAND_ACTION:
            _LOGGER.warning(
                "Undocumented action %s for command function %s", action, function
            )

    # -- Request plumbing --------------------------------------------------

    async def _request(  # noqa: PLR0913 - internal plumbing, keyword-only knobs
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        shared_backoff: bool = True,
        language: str | None = None,
    ) -> dict[str, Any]:
        """Send an authenticated request, refreshing once on a 401 and retrying.

        Maps error responses onto the :mod:`.exceptions` taxonomy and records
        rate-limit telemetry from every response. With ``shared_backoff`` (the
        REST throttle domain) the call refuses immediately, without any I/O,
        while a 429 backoff window is still open, and a throttle response arms
        that window. ``/live`` passes ``shared_backoff=False`` — it is a
        separate server throttle domain governed only by its own client-side
        minimum interval. ``language`` overrides the ``accept-language`` header
        for this request only.
        """
        if shared_backoff:
            self._enforce_backoff()
        status, body = await self._send(method, path, params, json_body, language)
        if status == HTTPStatus.UNAUTHORIZED:
            await self._auth.async_refresh()
            status, body = await self._send(method, path, params, json_body, language)
        if status >= HTTPStatus.BAD_REQUEST:
            self._raise_for_status(status, body, arm_backoff=shared_backoff)
        return body

    def _enforce_backoff(self) -> None:
        """Raise a :class:`RateLimitError` immediately while backing off.

        After a 429 the client holds off all traffic until
        :attr:`_backoff_until`; calling out again during that window would only
        deepen the throttle, so it fails fast with no I/O.
        """
        until = self._backoff_until
        if until is not None and self._monotonic() < until:
            msg = "Backing off after a rate-limit response; try again shortly"
            raise RateLimitError(msg, rate_limit=self.rate_limit)

    def _begin_backoff(self) -> None:
        """Arm the client-level backoff window after a throttle response.

        Uses the server's ``ratelimit-policy`` refill interval when it parses,
        otherwise falls back to
        :data:`~.const.DEFAULT_RATE_LIMIT_BACKOFF_SECONDS`.
        """
        refill = self.rate_limit.refill_seconds if self.rate_limit is not None else None
        delay = refill or DEFAULT_RATE_LIMIT_BACKOFF_SECONDS
        self._backoff_until = self._monotonic() + delay

    async def _send(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None,
        json_body: dict[str, Any] | None,
        language: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Perform one HTTP round-trip and return ``(status, parsed_body)``."""
        url = f"{self._base_url}{path}"
        token = await self._auth.async_get_access_token()
        headers = self._headers()
        if language is not None:
            headers["accept-language"] = language
        headers["Authorization"] = f"Bearer {token}"
        query = _encode_params(params) if params else None
        try:
            async with self._session.request(
                method,
                url,
                params=query,
                json=json_body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                status = response.status
                self._update_rate_limit(response.headers)
                body = await self._read_json(response)
        except (aiohttp.ClientError, TimeoutError) as err:
            msg = "Could not reach the iQua cloud"
            raise AquaHomeConnectionError(msg) from err
        return status, body

    def _headers(self) -> dict[str, str]:
        """Build the app-mimicry headers (minus the bearer token) per request."""
        return {
            "User-Agent": APP_USER_AGENT,
            "x-app-version": APP_VERSION_HEADER,
            "accept": ACCEPT_HEADER,
            "accept-language": self._language,
        }

    def _update_rate_limit(self, headers: Mapping[str, str]) -> None:
        """Record the latest ``ratelimit-*`` telemetry; never raise on garbage."""
        limit = headers.get("ratelimit-limit")
        remaining = headers.get("ratelimit-remaining")
        policy = headers.get("ratelimit-policy")
        if limit is None and remaining is None and policy is None:
            return
        self.rate_limit = RateLimitStatus(
            limit=_header_int(limit),
            remaining=_header_int(remaining),
            policy=policy if isinstance(policy, str) else None,
        )

    @staticmethod
    async def _read_json(response: aiohttp.ClientResponse) -> dict[str, Any]:
        """Parse a JSON object body tolerantly; anything else becomes ``{}``."""
        try:
            data = await response.json(content_type=None)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _raise_for_status(
        self, status: int, body: dict[str, Any], *, arm_backoff: bool = True
    ) -> NoReturn:
        """Map an error response onto the typed exception taxonomy.

        ``arm_backoff`` is ``False`` for the ``/live`` throttle domain, whose
        429s must not open the shared REST backoff window.
        """
        raw_code = body.get("code")
        code = raw_code if isinstance(raw_code, str) else None
        raw_fields = body.get("fields")
        fields = raw_fields if isinstance(raw_fields, dict) else None
        detail = body.get("detail") or body.get("message")
        message = (
            detail if isinstance(detail, str) and detail else f"iQua API error {status}"
        )
        if status == HTTPStatus.TOO_MANY_REQUESTS or code == _THROTTLE_CODE:
            if arm_backoff:
                self._begin_backoff()
            raise RateLimitError(
                message,
                status=status,
                code=code,
                fields=fields,
                rate_limit=self.rate_limit,
            )
        if status == HTTPStatus.UNAUTHORIZED or code in _AUTH_ERROR_CODES:
            raise AuthError(message, status=status, code=code, fields=fields)
        raise ApiError(message, status=status, code=code, fields=fields)
