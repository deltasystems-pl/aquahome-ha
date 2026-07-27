"""Tests for the Tier-2 recharge/regeneration binary sensors.

These five binaries derive the softener's recharge mode from the enriched
``recharge_ui`` state, with an ``iqua2`` fallback to the ``regeneration`` block on
hosts that omit ``recharge_ui``. Like the Phase-2 binaries they are set up
end-to-end against the ``aioresponses`` HTTP fakes and the captured device
fixture, with only the binary-sensor platform forwarded so each assertion
exercises the real coordinator-first-refresh path.

Coverage: the exact Tier-2 set created on the dev fixture (``regenerating`` /
``vacation_mode`` / ``recharge_off`` / ``regeneration_suspended`` present,
``wsov_closed`` absent); every recharge-state value mapping; the offline-honesty
rule that maps an ``offline`` tile to ``unknown`` for every derived binary; the
``iqua2`` fallback where a deleted ``recharge_ui`` routes ``regenerating`` and
``regeneration_suspended`` to the ``regeneration`` block (and drops the two
recharge-only binaries); the empty case where neither block exists; the
``wsov`` feature gate; and the device-class / category metadata.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.components.binary_sensor import (
    DOMAIN as BINARY_SENSOR_DOMAIN,
)
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
)
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
)
from homeassistant.helpers import entity_registry as er

from custom_components.aquahome.const import DOMAIN
from tests.conftest import (
    add_activity_routes,
    device_url,
    devices_url,
    load_fixture,
    setup_integration,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aioresponses import aioresponses
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

#: Slug of the captured device's serial ``7384243-20203-1120``.
SLUG = "7384243_20203_1120"

#: Every Tier-2 recharge/regeneration binary key.
TIER2_KEYS = (
    "regenerating",
    "vacation_mode",
    "recharge_off",
    "regeneration_suspended",
    "wsov_closed",
)

#: The Tier-2 binaries created on the dev fixture (``wsov_closed`` is gated out).
DEV_TIER2_KEYS = (
    "regenerating",
    "vacation_mode",
    "recharge_off",
    "regeneration_suspended",
)


@pytest.fixture(autouse=True)
def _only_binary_sensor_platform() -> Iterator[None]:
    """Forward only the binary-sensor platform for the duration of a test."""
    with patch("custom_components.aquahome.PLATFORMS", [Platform.BINARY_SENSOR]):
        yield


# ---------------------------------------------------------------------------
# Local fixture-payload helpers (never mutate the loaded fixture in place)
# ---------------------------------------------------------------------------


def _base_detail() -> dict[str, Any]:
    """Return a deep copy of the captured device-detail payload to mutate."""
    return copy.deepcopy(load_fixture("device-detail.json"))


def _water_treatment(detail: dict[str, Any]) -> dict[str, Any]:
    """Return the mutable enriched ``water_treatment`` block of ``detail``."""
    treatment: dict[str, Any] = detail["enriched_data"]["water_treatment"]
    return treatment


def _register_detail(mock: aioresponses, detail: dict[str, Any]) -> None:
    """Register the device-list, device-detail, and activity routes.

    The activity routes are served from the real fixtures so the tolerant
    activity refresh at setup succeeds cleanly instead of logging a warning.
    """
    mock.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock.get(device_url(), payload=detail, repeat=True)
    add_activity_routes(mock)


def _entity_id(entity_registry: er.EntityRegistry, key: str) -> str | None:
    """Resolve a binary sensor's entity id from its unique-id suffix."""
    return entity_registry.async_get_entity_id(
        BINARY_SENSOR_DOMAIN, DOMAIN, f"{SLUG}_{key}"
    )


def _state_of(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, key: str
) -> str | None:
    """Return the current state string of the Tier-2 binary ``key``, or ``None``."""
    entity_id = _entity_id(entity_registry, key)
    if entity_id is None:
        return None
    state = hass.states.get(entity_id)
    return state.state if state is not None else None


def _with_state(state: str, *, wsov: bool = False) -> dict[str, Any]:
    """Return a device-detail payload whose ``recharge_ui.state`` is ``state``.

    When ``wsov`` is set the ``wsov`` feature is advertised so the
    ``wsov_closed`` binary is created alongside the four always-present ones.
    """
    detail = _base_detail()
    treatment = _water_treatment(detail)
    treatment["recharge_ui"]["state"] = state
    if wsov:
        treatment["features"] = [*treatment["features"], "wsov"]
    return detail


# ---------------------------------------------------------------------------
# Existence on the dev fixture
# ---------------------------------------------------------------------------


async def test_tier2_binaries_created_on_dev_fixture(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """The dev fixture (state ``ready``) creates four mode binaries, all ``off``.

    ``wsov_closed`` is gated on the ``wsov`` feature, which the regeneration-only
    dev device does not advertise, so it is never created.
    """
    _register_detail(mock_api, _base_detail())

    assert await setup_integration(hass, mock_config_entry)

    for key in DEV_TIER2_KEYS:
        assert _entity_id(entity_registry, key) is not None
        assert _state_of(hass, entity_registry, key) == STATE_OFF
    assert _entity_id(entity_registry, "wsov_closed") is None


# ---------------------------------------------------------------------------
# Recharge-state value mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "on_key"),
    [
        ("regenerating", "regenerating"),
        ("vacation_mode", "vacation_mode"),
        ("recharge_off", "recharge_off"),
        ("suspended", "regeneration_suspended"),
        ("wsov_closed", "wsov_closed"),
    ],
)
async def test_recharge_state_value_mapping(  # noqa: PLR0913
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    state: str,
    on_key: str,
) -> None:
    """Each recharge state drives exactly its own binary ``on`` and the rest ``off``.

    The ``wsov`` feature is advertised so all five binaries exist, letting the
    ``wsov_closed`` mapping be exercised alongside the four default binaries.
    """
    _register_detail(mock_api, _with_state(state, wsov=True))

    assert await setup_integration(hass, mock_config_entry)

    for key in TIER2_KEYS:
        expected = STATE_ON if key == on_key else STATE_OFF
        assert _state_of(hass, entity_registry, key) == expected


# ---------------------------------------------------------------------------
# Offline-honesty rule
# ---------------------------------------------------------------------------


async def test_offline_tile_reports_unknown_not_false(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """An ``offline`` recharge tile maps every derived binary to ``unknown``.

    The cloud has lost the device, so its underlying mode is unknowable; reporting
    ``off`` would fabricate a state. Each binary stays available (no derived binary
    gates on ``device_online``) while reading ``unknown``.
    """
    _register_detail(mock_api, _with_state("offline", wsov=True))

    assert await setup_integration(hass, mock_config_entry)

    for key in TIER2_KEYS:
        assert _entity_id(entity_registry, key) is not None
        state = _state_of(hass, entity_registry, key)
        assert state == STATE_UNKNOWN
        assert state != STATE_UNAVAILABLE


# ---------------------------------------------------------------------------
# iqua2 fallback to the regeneration block
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("regeneration_status", "on_key"),
    [
        ("regenerating", "regenerating"),
        ("suspended", "regeneration_suspended"),
    ],
)
async def test_iqua2_fallback_to_regeneration_block(  # noqa: PLR0913
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    regeneration_status: str,
    on_key: str,
) -> None:
    """Without ``recharge_ui`` the derived binaries fall back to ``regeneration``.

    ``regenerating`` and ``regeneration_suspended`` read the ``regeneration`` block
    directly; the two recharge-only binaries (``vacation_mode`` / ``recharge_off``)
    have no source and are not created.
    """
    detail = _base_detail()
    treatment = _water_treatment(detail)
    del treatment["recharge_ui"]
    treatment["regeneration"]["regeneration_status"] = regeneration_status
    _register_detail(mock_api, detail)

    assert await setup_integration(hass, mock_config_entry)

    assert _state_of(hass, entity_registry, on_key) == STATE_ON
    off_key = "regeneration_suspended" if on_key == "regenerating" else "regenerating"
    assert _state_of(hass, entity_registry, off_key) == STATE_OFF
    # The recharge-only binaries have no fallback source and stay absent.
    assert _entity_id(entity_registry, "vacation_mode") is None
    assert _entity_id(entity_registry, "recharge_off") is None


async def test_no_recharge_or_regeneration_blocks_drops_all_derived(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """With neither ``recharge_ui`` nor ``regeneration`` present, no Tier-2 binary exists."""
    detail = _base_detail()
    treatment = _water_treatment(detail)
    del treatment["recharge_ui"]
    del treatment["regeneration"]
    _register_detail(mock_api, detail)

    assert await setup_integration(hass, mock_config_entry)

    for key in TIER2_KEYS:
        assert _entity_id(entity_registry, key) is None


# ---------------------------------------------------------------------------
# wsov feature gate
# ---------------------------------------------------------------------------


async def test_wsov_feature_gates_creation(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """The ``wsov_closed`` binary is created only when the ``wsov`` feature is present.

    With the feature advertised and a ``wsov_closed`` recharge tile the binary
    reads ``on``; without the feature it is never created (see the dev-fixture
    test) — here the synthetic feature list gates it in.
    """
    _register_detail(mock_api, _with_state("wsov_closed", wsov=True))

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry, "wsov_closed")
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == STATE_ON
    assert state.attributes[ATTR_DEVICE_CLASS] == BinarySensorDeviceClass.PROBLEM


# ---------------------------------------------------------------------------
# Device-class / category metadata
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "device_class"),
    [
        ("regenerating", BinarySensorDeviceClass.RUNNING),
        ("vacation_mode", None),
        ("recharge_off", None),
        ("regeneration_suspended", BinarySensorDeviceClass.PROBLEM),
        ("wsov_closed", BinarySensorDeviceClass.PROBLEM),
    ],
)
async def test_tier2_metadata(  # noqa: PLR0913
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    key: str,
    device_class: BinarySensorDeviceClass | None,
) -> None:
    """The mode binaries carry running/problem device classes; both are user-visible.

    The ``wsov`` feature is advertised so ``wsov_closed`` exists; no Tier-2 binary
    is a diagnostic, so none carries an entity category.
    """
    _register_detail(mock_api, _with_state("ready", wsov=True))

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry, key)
    assert entity_id is not None
    registry_entry = entity_registry.async_get(entity_id)
    assert registry_entry is not None
    assert registry_entry.entity_category is None
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes.get(ATTR_DEVICE_CLASS) == device_class
