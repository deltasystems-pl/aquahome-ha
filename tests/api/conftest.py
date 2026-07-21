"""Shared helpers for API-client tests: fake clock and JWT factory.

These tests are pure aiohttp unit tests — no Home Assistant core involved.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

#: Fixed "now" used by the fake clock: 2026-07-21T12:00:00Z.
FAKE_NOW = 1_784_635_200.0

ACCESS_TOKEN_LIFETIME = 86_400  # the API issues exactly 24 h access JWTs


class FakeClock:
    """Deterministic replacement for time.time in auth-lifecycle tests."""

    def __init__(self, now: float = FAKE_NOW) -> None:
        """Start the clock at a fixed epoch timestamp."""
        self._now = now

    def __call__(self) -> float:
        """Return the current fake epoch time."""
        return self._now

    def advance(self, seconds: float) -> None:
        """Move the clock forward."""
        self._now += seconds


@pytest.fixture
def fake_clock() -> FakeClock:
    """Provide a fresh fake clock per test."""
    return FakeClock()


def _b64url(data: dict[str, Any]) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def make_jwt(iat: float, lifetime: int = ACCESS_TOKEN_LIFETIME) -> str:
    """Build an unsigned HS256-style JWT with the API's real claim shape."""
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "typ": "a",
        "sub": "7f1e15b0-e9c7-44a1-8f0a-1844d67bf545",
        "email": "dev@example.com",
        "iat": int(iat),
        "exp": int(iat) + lifetime,
    }
    return f"{_b64url(header)}.{_b64url(payload)}.fakesignature"
