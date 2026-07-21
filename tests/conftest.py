"""Shared fixtures for the AquaHome test suite.

Two layers share this file: the pure-aiohttp API-client tests under ``tests/api``
(which add their own helpers in ``tests/api/conftest.py``) and the Home Assistant
integration tests, which use the ``mock_config_entry`` / ``mock_api`` fixtures
plus the route helpers below to fake the iQua cloud with the real captured
payloads.
"""

from __future__ import annotations

import copy
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


def alerts_url(
    device_id: str = TEST_DEVICE_ID, host: str = API_BASE_URL
) -> re.Pattern[str]:
    """Match the paginated ``GET /devices/{id}/alerts`` URL on ``host``."""
    return re.compile(rf"^{re.escape(host)}/devices/{re.escape(device_id)}/alerts\?.*$")


def regen_events_url(
    device_id: str = TEST_DEVICE_ID, host: str = API_BASE_URL
) -> re.Pattern[str]:
    """Match the ``GET /devices/{id}/regeneration-events`` URL on ``host``."""
    return re.compile(
        rf"^{re.escape(host)}/devices/{re.escape(device_id)}/regeneration-events\?.*$"
    )


def settings_url(
    device_id: str = TEST_DEVICE_ID, host: str = API_BASE_URL
) -> re.Pattern[str]:
    """Match the ``/devices/{id}/settings`` URL (GET and PATCH) on ``host``."""
    return re.compile(
        rf"^{re.escape(host)}/devices/{re.escape(device_id)}/settings(\?.*)?$"
    )


def command_url(device_id: str = TEST_DEVICE_ID, host: str = API_BASE_URL) -> str:
    """Return the exact ``PUT /devices/{id}/command`` URL on ``host``."""
    return f"{host}/devices/{device_id}/command"


def add_settings_routes(
    mock: aioresponses,
    *,
    host: str = API_BASE_URL,
    settings: dict[str, Any] | None = None,
    repeat: bool = True,
) -> None:
    """Register the settings-coordinator GET route from the real fixture."""
    mock.get(
        settings_url(host=host),
        payload=settings or load_fixture("settings.json"),
        repeat=repeat,
    )


def patch_settings_route(
    mock: aioresponses,
    *,
    host: str = API_BASE_URL,
    payload: dict[str, Any],
    repeat: bool = False,
) -> None:
    """Register a ``PATCH /devices/{id}/settings`` route returning ``payload``.

    The server echoes the refreshed settings document back from a PATCH; write
    tests register the post-write document here and assert the entity state
    reconciles from it without an extra GET.
    """
    mock.patch(settings_url(host=host), payload=payload, repeat=repeat)


# ---------------------------------------------------------------------------
# Synthetic payload builders (Phase 4)
#
# The dev device has features=["regeneration"] only, so wsov / leak-detector /
# number-setting / switch-setting payloads cannot be captured live. These
# builders derive synthetic variants from the real fixtures; every builder
# deep-copies its input and never mutates the caller's document.
# ---------------------------------------------------------------------------


def _water_treatment(device_detail: dict[str, Any]) -> dict[str, Any]:
    """Return the mutable enriched water-treatment block of a detail payload."""
    block: dict[str, Any] = device_detail["enriched_data"]["water_treatment"]
    return block


def with_wsov(  # noqa: PLR0913 - keyword-only knobs mirror the payload fields
    device_detail: dict[str, Any],
    *,
    status: str = "open",
    is_installed: bool = True,
    auto_shutoff_supported: bool = False,
    error_code: str | None = None,
    manual_override: bool | None = None,
    dialog_buttons: dict[str, bool] | None = None,
    add_feature: bool = True,
) -> dict[str, Any]:
    """Return a copy of a device-detail payload with a water-shutoff valve."""
    detail = copy.deepcopy(device_detail)
    treatment = _water_treatment(detail)
    valve: dict[str, Any] = {
        "status": status,
        "is_installed": is_installed,
        "auto_shutoff_supported": auto_shutoff_supported,
    }
    if error_code is not None:
        valve["error_code"] = error_code
    if manual_override is not None:
        valve["manual_override"] = manual_override
    if dialog_buttons is not None:
        valve["dialog"] = {"dialog_buttons": dialog_buttons}
    treatment["water_shutoff_valve"] = valve
    features = treatment.setdefault("features", [])
    if add_feature and "wsov" not in features:
        features.append("wsov")
    return detail


def make_leak_detector(  # noqa: PLR0913 - keyword-only knobs mirror the payload fields
    detector_id: int = 1,
    *,
    nickname: str = "Kitchen",
    leak: bool = False,
    low_battery: bool = False,
    tampered: bool = False,
    connected: bool = True,
    in_alert: bool = False,
    temperature_raw: int = 68,
    signal: int = -60,
) -> dict[str, Any]:
    """Build one synthetic leak-detector detail payload (spec-shaped)."""

    def flag(value: bool) -> dict[str, Any]:
        """Build a StatusItemBool payload."""
        return {"value": value, "updated_at": "2026-07-21T10:00:00Z"}

    return {
        "detector_id": detector_id,
        "nickname": nickname,
        "nickname_setting_key": f"leak_detector_{detector_id}_nickname",
        "last_updated_at": "2026-07-21T10:00:00Z",
        "device_time_last_updated_at": "2026-07-21T10:00:00Z",
        "device_time_last_updated_at_display": "10:00 AM",
        "status": {
            "in_alert_state": in_alert,
            "leak_detected": flag(leak),
            "low_battery": flag(low_battery),
            "tampered": flag(tampered),
            "is_connected": flag(connected),
            "temperature": {
                "raw_value": temperature_raw,
                "converted_value": round((temperature_raw - 32) * 5 / 9),
                "display": {"key": "temperature", "params": {}},
                "status": flag(False),
            },
            "signal_strength": {"value": signal},
        },
    }


def with_leak_detectors(
    device_detail: dict[str, Any],
    detectors: list[dict[str, Any]] | None = None,
    *,
    scanning: bool = False,
    add_feature: bool = True,
) -> dict[str, Any]:
    """Return a copy of a device-detail payload with leak detectors attached."""
    detail = copy.deepcopy(device_detail)
    treatment = _water_treatment(detail)
    treatment["leak_detectors"] = {
        "details": detectors if detectors is not None else [make_leak_detector()],
        "scanning": {"is_scanning": scanning},
    }
    features = treatment.setdefault("features", [])
    if add_feature and "leak_detector" not in features:
        features.append("leak_detector")
    return detail


def with_extra_setting(
    settings_doc: dict[str, Any], setting: dict[str, Any]
) -> dict[str, Any]:
    """Return a copy of a settings document with one setting appended."""
    doc = copy.deepcopy(settings_doc)
    doc["settings"].append(setting)
    return doc


def without_setting(settings_doc: dict[str, Any], name: str) -> dict[str, Any]:
    """Return a copy of a settings document with the named setting removed."""
    doc = copy.deepcopy(settings_doc)
    doc["settings"] = [item for item in doc["settings"] if item.get("name") != name]
    return doc


def with_setting_value(
    settings_doc: dict[str, Any], name: str, value: Any
) -> dict[str, Any]:
    """Return a copy of a settings document with one current_value replaced."""
    doc = copy.deepcopy(settings_doc)
    for item in doc["settings"]:
        if item.get("name") == name:
            item["current_value"] = value
    return doc


def make_number_setting(  # noqa: PLR0913 - keyword-only knobs mirror the payload fields
    name: str = "brine_dose",
    *,
    label: str = "Brine Dose",
    current_value: Any = 125,
    minimum: int = 50,
    maximum: int = 250,
    step: int = 5,
    precision: int = 1,
) -> dict[str, Any]:
    """Build a synthetic number setting (no NumberRule setting exists live)."""
    return {
        "component_type": "number",
        "name": name,
        "label": label,
        "current_value": current_value,
        "rules": {
            "number_rules": {
                "min": minimum,
                "max": maximum,
                "step": step,
                "precision": precision,
            }
        },
    }


def make_switch_setting(
    name: str = "night_mode",
    *,
    label: str = "Night Mode",
    current_value: bool = True,
) -> dict[str, Any]:
    """Build a synthetic boolean setting (no switch setting exists live)."""
    return {
        "component_type": "switch",
        "name": name,
        "label": label,
        "current_value": current_value,
    }


def add_activity_routes(
    mock: aioresponses,
    *,
    host: str = API_BASE_URL,
    alerts: dict[str, Any] | None = None,
    regeneration_events: dict[str, Any] | None = None,
    repeat: bool = True,
) -> None:
    """Register the activity-coordinator read routes from the real fixtures."""
    mock.get(
        alerts_url(host=host),
        payload=alerts or load_fixture("alerts.json"),
        repeat=repeat,
    )
    mock.get(
        regen_events_url(host=host),
        payload=regeneration_events or load_fixture("regeneration-events.json"),
        repeat=repeat,
    )


def add_device_routes(  # noqa: PLR0913 - a keyword-only per-fixture override per route
    mock: aioresponses,
    *,
    host: str = API_BASE_URL,
    devices_list: dict[str, Any] | None = None,
    device_detail: dict[str, Any] | None = None,
    alerts: dict[str, Any] | None = None,
    regeneration_events: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
    repeat: bool = True,
) -> None:
    """Register every read route a normal entry setup hits, from real fixtures.

    Covers the device list/detail polls plus the activity-coordinator routes
    (alert feed + regeneration history) and the settings document. Tests that
    need failure sequences register their own (non-``repeat``) routes before
    calling this, or skip it entirely — aioresponses matches registrations in
    order.
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
    add_activity_routes(
        mock,
        host=host,
        alerts=alerts,
        regeneration_events=regeneration_events,
        repeat=repeat,
    )
    add_settings_routes(mock, host=host, settings=settings, repeat=repeat)


async def setup_integration(hass: HomeAssistant, entry: MockConfigEntry) -> bool:
    """Add ``entry`` to hass, set it up, and settle the event loop."""
    entry.add_to_hass(hass)
    result = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return result
