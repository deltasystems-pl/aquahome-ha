"""Shared fixtures for the AquaHome test suite.

Two layers share this file: the pure-aiohttp API-client tests under ``tests/api``
(which add their own helpers in ``tests/api/conftest.py``) and the Home Assistant
integration tests, which use the ``mock_config_entry`` / ``mock_api`` fixtures
plus the route helpers below to fake the iQua cloud with the real captured
payloads.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from aioresponses import aioresponses
from homeassistant.const import (
    CONF_ACCESS_TOKEN,
    CONF_EMAIL,
    CONF_HOST,
    CONF_PASSWORD,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aquahome.api.const import API_BASE_URL
from custom_components.aquahome.const import (
    CONF_REFRESH_TOKEN,
    CONFIG_MINOR_VERSION,
    CONFIG_VERSION,
    DOMAIN,
)
from tests.api.conftest import make_jwt

if TYPE_CHECKING:
    from collections.abc import Iterator

    from homeassistant.core import HomeAssistant

FIXTURES_DIR = Path(__file__).parent / "fixtures"

#: Identity constants matching the captured fixtures and the fake-JWT factory.
TEST_USER_ID = "7f1e15b0-e9c7-44a1-8f0a-1844d67bf545"
TEST_EMAIL = "dev@example.com"
TEST_PASSWORD = "test-password"
TEST_DEVICE_ID = "d32caa70-dca3-4cc9-bd3e-28b8c44df23c"


def load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture by file name."""
    data: dict[str, Any] = json.loads((FIXTURES_DIR / name).read_text())
    return data


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> None:
    """Enable loading custom integrations in all tests."""
    return


def make_access_token(now: float | None = None) -> str:
    """Build a fresh fake access JWT (24 h lifetime from ``now``).

    Uses the same claim shape as the real API so the auth manager's local expiry
    check passes without triggering a refresh mid-test. Under ``freezer`` the
    default ``time.time()`` is the frozen clock, keeping everything consistent.
    """
    return make_jwt(now if now is not None else time.time())


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a config entry matching the Phase-2 entry data schema."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=TEST_EMAIL,
        unique_id=TEST_USER_ID,
        version=CONFIG_VERSION,
        minor_version=CONFIG_MINOR_VERSION,
        data={
            CONF_EMAIL: TEST_EMAIL,
            CONF_PASSWORD: TEST_PASSWORD,
            CONF_HOST: API_BASE_URL,
            CONF_ACCESS_TOKEN: make_access_token(),
            CONF_REFRESH_TOKEN: "refresh-token-1",
        },
    )


@pytest.fixture
def mock_api() -> Iterator[aioresponses]:
    """Intercept every aiohttp request for the duration of a test."""
    with aioresponses() as mocked:
        yield mocked


def devices_url(host: str = API_BASE_URL) -> re.Pattern[str]:
    """Match the paginated ``GET /devices`` list URL on ``host``."""
    return re.compile(rf"^{re.escape(host)}/devices\?.*$")


def device_url(
    device_id: str = TEST_DEVICE_ID, host: str = API_BASE_URL
) -> re.Pattern[str]:
    """Match the ``GET /devices/{id}`` detail URL on ``host``."""
    return re.compile(rf"^{re.escape(host)}/devices/{re.escape(device_id)}\?.*$")


def add_device_routes(
    mock: aioresponses,
    *,
    host: str = API_BASE_URL,
    devices_list: dict[str, Any] | None = None,
    device_detail: dict[str, Any] | None = None,
    repeat: bool = True,
) -> None:
    """Register the two read routes a normal entry setup hits, from real fixtures.

    Tests that need failure sequences register their own (non-``repeat``) routes
    before calling this, or skip it entirely — aioresponses matches registrations
    in order.
    """
    mock.get(
        devices_url(host),
        payload=devices_list or load_fixture("devices-list.json"),
        repeat=repeat,
    )
    mock.get(
        device_url(host=host),
        payload=device_detail or load_fixture("device-detail.json"),
        repeat=repeat,
    )


async def setup_integration(hass: HomeAssistant, entry: MockConfigEntry) -> bool:
    """Add ``entry`` to hass, set it up, and settle the event loop."""
    entry.add_to_hass(hass)
    result = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return result
