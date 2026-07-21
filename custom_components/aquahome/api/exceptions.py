"""Typed error taxonomy for the iQua cloud API client.

Mapping rules (implemented in ``client.py`` and, for the auth endpoints,
``auth.py``):

- Network failures (DNS, timeout, connection reset) -> ``AquaHomeConnectionError``.
- HTTP >= 400 with a parseable ``ApiErrorModel`` body -> ``ApiError`` or a
  subclass selected by HTTP status and the machine-readable ``code``:
  401 / ``AuthBadUsernameOrPassword`` / ``AuthCannotRefreshToken`` -> ``AuthError``,
  ``UserNotVerified`` (any status) -> ``UserNotVerifiedError``,
  429 / ``ThrottleLimitExceeded`` -> ``RateLimitError``.
- Client-side safety refusals (forbidden command functions) ->
  ``ForbiddenCommandError`` — raised before any request is made.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import RateLimitStatus


class AquaHomeError(Exception):
    """Base error for all AquaHome API failures."""


class AquaHomeConnectionError(AquaHomeError):
    """Could not reach the iQua cloud (network / timeout)."""


class ApiError(AquaHomeError):
    """The API returned an error response."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> None:
        """Store the parsed ApiErrorModel details."""
        super().__init__(message)
        self.status = status
        self.code = code
        self.fields = fields or {}


class AuthError(ApiError):
    """Authentication or token refresh failed; reauthentication required."""


class UserNotVerifiedError(AuthError):
    """The account is unverified and must clear an email confirmation-code challenge.

    Raised when the API returns the ``UserNotVerified`` code (an AWS-Cognito
    account-verification state first seen in the field ~Dec 2025). A subclass of
    :class:`AuthError` so existing ``except AuthError`` handlers still catch it,
    but distinct so the config/reauth flow can route the user to a
    ``POST /auth/validate-user`` step (submit the emailed code) rather than
    treating it as bad credentials or retry-looping.
    """


class RateLimitError(ApiError):
    """The cloud throttled the request (HTTP 429 / ThrottleLimitExceeded)."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        fields: dict[str, Any] | None = None,
        rate_limit: RateLimitStatus | None = None,
    ) -> None:
        """Store the parsed error details plus the latest rate-limit telemetry."""
        super().__init__(message, status=status, code=code, fields=fields)
        self.rate_limit = rate_limit


class DeviceOfflineError(ApiError):
    """A command could not be processed because the device is offline."""


class ForbiddenCommandError(AquaHomeError):
    """Refusing to send a command that is unsafe for the device or account."""
