"""Tests for the AquaHome diagnostics platform.

Snapshot-free, explicit-assertion tests: a real config entry is set up against
the captured iQua fixtures served through ``aioresponses``, then
:func:`~custom_components.aquahome.diagnostics.async_get_config_entry_diagnostics`
is called directly and the returned structure is inspected. The load-bearing
guarantee is redaction, so several tests recursively walk every leaf of the dump
and assert that no raw fixture secret (tokens, email, serials, owner name, SSID,
location) survives anywhere, while genuinely useful data (model string, the
non-location POSIX ``tz_dev`` rule, the activity feed) is retained.

Platforms are patched out for the whole module: ``runtime_data`` is fully
populated before the platforms are forwarded, so diagnostics needs no entity
platform and must not depend on peer-owned platform modules.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant.components.diagnostics import REDACTED
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_HOST, Platform

from custom_components.aquahome.api import RateLimitStatus
from custom_components.aquahome.const import CONF_REFRESH_TOKEN
from custom_components.aquahome.diagnostics import async_get_config_entry_diagnostics
from tests.conftest import (
    TEST_EMAIL,
    TEST_PASSWORD,
    add_device_routes,
    alerts_url,
    load_fixture,
    setup_integration,
)

if TYPE_CHECKING:
    from aioresponses import aioresponses
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

#: Diagnostics needs no entity platform; patching PLATFORMS out keeps these tests
#: independent of the peer-owned sensor/binary_sensor/event modules.
_EMPTY_PLATFORMS: list[Platform] = []
_NO_PLATFORMS = patch("custom_components.aquahome.PLATFORMS", _EMPTY_PLATFORMS)

#: Raw fixture secrets that must never appear in the redacted dump.
_TOP_SERIAL = "7384243-20203-1120"
_PROP_SERIAL = "SL00034EEB25F3"
_NICKNAME = "Dom"
_TZ_ID = "Europe/Warsaw"
_THING_NAME = "e4d6349d-779f-42c8-b2d7-4eff4bc879a3"
_IMAGE_URL = "https://app.myiquaapp.com/devices/watersofteners/Aquahome.png?v=1"
_REFRESH_TOKEN_VALUE = "refresh-token-1"

#: Data that must be retained for the dump to be useful.
_MODEL = "AquaHome 20 Smart"
_TZ_DEV = "CET-1CEST,M3.5.0,M10.5.0/3"

#: A newest alert message from the alerts fixture (present after parsing).
_ALERT_MESSAGE = "Device went offline"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _all_leaves(obj: Any) -> list[Any]:
    """Return every non-container leaf value reachable in ``obj``.

    Recurses through mappings, lists, and tuples (``dataclasses.asdict`` emits
    tuples for tuple-typed fields), yielding scalar/leaf values only — mapping
    keys are field names, never secrets, so they are intentionally skipped.
    """
    if isinstance(obj, dict):
        leaves: list[Any] = []
        for value in obj.values():
            leaves.extend(_all_leaves(value))
        return leaves
    if isinstance(obj, (list, tuple)):
        items: list[Any] = []
        for item in obj:
            items.extend(_all_leaves(item))
        return items
    return [obj]


def _leaf_blob(obj: Any) -> str:
    """Return a NUL-joined string of every leaf value, for substring checks."""
    return "\x00".join(str(leaf) for leaf in _all_leaves(obj))


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_diagnostics_structure(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The dump carries entry, one device block, activity, and telemetry keys."""
    add_device_routes(mock_api)
    with _NO_PLATFORMS:
        assert await setup_integration(hass, mock_config_entry)

    diag = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    # Entry: the host is retained, credentials/tokens are redacted.
    assert diag["entry"][CONF_HOST] == mock_config_entry.data[CONF_HOST]
    assert diag["entry"]["email"] == REDACTED
    assert diag["entry"]["password"] == REDACTED
    assert diag["entry"][CONF_ACCESS_TOKEN] == REDACTED
    assert diag["entry"][CONF_REFRESH_TOKEN] == REDACTED

    # Exactly one device block with both liveness signals.
    assert len(diag["devices"]) == 1
    block = diag["devices"][0]
    assert block["device_online"] is True
    assert block["last_update_success"] is True

    # The parsed device view is present and keeps genuinely useful data.
    device = block["device"]
    assert device["enriched_data"]["model"] == _MODEL

    # No rate-limit headers were served, so telemetry is absent (not an error).
    assert diag["rate_limit"] is None


async def test_diagnostics_activity_block_present(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The activity block carries the parsed alert and regeneration history."""
    add_device_routes(mock_api)
    with _NO_PLATFORMS:
        assert await setup_integration(hass, mock_config_entry)

    diag = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    activity = diag["devices"][0]["activity"]

    assert activity is not None
    assert len(activity["alerts"]) == 20
    assert len(activity["regeneration_events"]) == 18
    # A fresh setup only establishes the watermark; nothing is "new" yet.
    assert activity["new_alerts"] == ()
    messages = {alert["message"] for alert in activity["alerts"]}
    assert _ALERT_MESSAGE in messages


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


async def test_diagnostics_redacts_all_secrets(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
) -> None:
    """No token, email, serial, owner name, or location survives the dump."""
    access_token = mock_config_entry.data[CONF_ACCESS_TOKEN]
    add_device_routes(mock_api)
    with _NO_PLATFORMS:
        assert await setup_integration(hass, mock_config_entry)

    diag = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    blob = _leaf_blob(diag)

    for secret in (
        access_token,
        _REFRESH_TOKEN_VALUE,
        TEST_EMAIL,
        TEST_PASSWORD,
        _TOP_SERIAL,
        _PROP_SERIAL,
        _NICKNAME,
        _TZ_ID,
        _THING_NAME,
        _IMAGE_URL,
    ):
        assert secret not in blob, f"unredacted secret leaked: {secret!r}"

    # Redaction actually fired, and non-sensitive data is preserved.
    assert REDACTED in blob
    assert _MODEL in blob
    assert _TZ_DEV in blob, "the non-location POSIX tz_dev rule must be kept"


async def test_diagnostics_redacts_owner_summary(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The embedded owner summary is redacted wholesale, not field by field."""
    add_device_routes(mock_api)
    with _NO_PLATFORMS:
        assert await setup_integration(hass, mock_config_entry)

    diag = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    device = diag["devices"][0]["device"]

    assert device["user"] == REDACTED
    assert device["serial_number"] == REDACTED
    assert device["thing_name"] == REDACTED
    assert device["nickname"] == REDACTED
    assert device["image_url"] == REDACTED
    # The identifying raw properties are redacted; a benign one is untouched.
    assert device["properties"]["product_serial_number"] == REDACTED
    assert device["properties"]["serial_number"] == REDACTED
    assert device["properties"]["tz_id"] == REDACTED
    assert device["properties"]["tz_dev"]["value"] == _TZ_DEV


async def test_diagnostics_redacts_wifi_ssid(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A populated SSID (enriched name and raw property) never leaks."""
    ssid = "MyHouseholdWifi5G"
    detail = copy.deepcopy(load_fixture("device-detail.json"))
    detail["enriched_data"]["water_treatment"]["wifi_ssid_name"] = ssid
    detail["properties"]["wifi_ssid"] = {
        "name": "wifi_ssid",
        "value": ssid,
        "created_at": "2025-08-30T07:48:44Z",
        "updated_at": "2026-06-14T02:53:37Z",
    }
    add_device_routes(mock_api, device_detail=detail)
    with _NO_PLATFORMS:
        assert await setup_integration(hass, mock_config_entry)

    diag = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    device = diag["devices"][0]["device"]

    assert ssid not in _leaf_blob(diag)
    assert device["enriched_data"]["wifi_ssid_name"] == REDACTED
    assert device["properties"]["wifi_ssid"] == REDACTED


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


async def test_diagnostics_without_activity_data(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A device whose activity feed never loaded dumps ``activity`` as None."""
    # The alert feed 500s forever, so the tolerant setup leaves activity.data
    # None while the fast coordinator (and its device block) succeed.
    mock_api.get(alerts_url(), status=500, repeat=True)
    add_device_routes(mock_api)
    with _NO_PLATFORMS:
        assert await setup_integration(hass, mock_config_entry)

    diag = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    block = diag["devices"][0]

    assert block["activity"] is None
    # The device block is still fully populated and its telemetry redacted.
    assert block["device"]["enriched_data"]["model"] == _MODEL
    assert block["last_update_success"] is True
    assert block["device"]["serial_number"] == REDACTED


async def test_diagnostics_reports_rate_limit(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The client's latest rate-limit telemetry is dumped when present."""
    add_device_routes(mock_api)
    with _NO_PLATFORMS:
        assert await setup_integration(hass, mock_config_entry)

    mock_config_entry.runtime_data.client.rate_limit = RateLimitStatus(
        limit=5, remaining=4, policy="5;w=60;burst=50;policy=token_bucket"
    )
    diag = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert diag["rate_limit"] == {
        "limit": 5,
        "remaining": 4,
        "policy": "5;w=60;burst=50;policy=token_bucket",
    }
