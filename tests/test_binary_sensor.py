"""End-to-end tests for the AquaHome binary-sensor platform.

The integration is set up for real against the ``aioresponses`` HTTP fakes and
the captured device fixture; only the forwarded platform list is narrowed to the
binary-sensor platform (the canonical Home Assistant single-platform isolation),
so every assertion exercises the true set-up path — auth, client, device list,
coordinator first refresh, entity creation, and the coordinator poll cycle.

Coverage: the snapshot of all binaries created on the dev fixture, the exact
set that exists (the two feature-gated binaries stay absent), the diagnostic
category and device-class metadata, an alert flag flipping ``on`` across a poll,
both existence paths of each feature-gated binary, a device whose status block is
missing, and the availability split that keeps cloud-side alerts alive while the
softener itself is offline.
"""

from __future__ import annotations

import copy
from datetime import timedelta
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
    EntityCategory,
    Platform,
)
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    snapshot_platform,
)

from custom_components.aquahome.const import DOMAIN, UPDATE_INTERVAL
from tests.conftest import (
    add_device_routes,
    device_url,
    devices_url,
    load_fixture,
    setup_integration,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aioresponses import aioresponses
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from syrupy.assertion import SnapshotAssertion

#: Slug of the captured device's serial ``7384243-20203-1120``.
SLUG = "7384243_20203_1120"

#: Instant the snapshot test freezes to, matching the sensor suite: inside
#: the fixtures' capture window so the analytics attributes are stable.
FROZEN_INSTANT = "2026-07-21T12:00:00+00:00"

#: The binaries created on the dev fixture: the three analytics detections,
#: online, the six plain alerts, and the four recharge-state binaries
#: (wsov_closed stays feature-gated out).
EXPECTED_KEYS = (
    "leak_suspected",
    "usage_anomaly",
    "vacation_detected",
    "online",
    "salt_level_alert",
    "error_code_alert",
    "flow_monitor_alert",
    "connection_alert",
    "water_usage_alert",
    "resin_alert",
    "regenerating",
    "vacation_mode",
    "recharge_off",
    "regeneration_suspended",
)


@pytest.fixture(autouse=True)
def _only_binary_sensor_platform() -> Iterator[None]:
    """Forward only the binary-sensor platform for the duration of a test.

    The rest of set-up still runs end-to-end; narrowing the platform list keeps
    every test in this module independent of the sibling sensor platform and lets
    ``snapshot_platform`` see a single domain.
    """
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
    """Register the device-list and (repeating) device-detail routes."""
    mock.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock.get(device_url(), payload=detail, repeat=True)


def _entity_id(entity_registry: er.EntityRegistry, key: str) -> str | None:
    """Resolve a binary sensor's entity id from its unique-id suffix."""
    return entity_registry.async_get_entity_id(
        BINARY_SENSOR_DOMAIN, DOMAIN, f"{SLUG}_{key}"
    )


def _created_keys(entity_registry: er.EntityRegistry, entry_id: str) -> list[str]:
    """Return the sorted unique-id suffixes created for the config entry."""
    entries = er.async_entries_for_config_entry(entity_registry, entry_id)
    return sorted(entry.unique_id.removeprefix(f"{SLUG}_") for entry in entries)


# ---------------------------------------------------------------------------
# Snapshot + exact-set on the real dev fixture
# ---------------------------------------------------------------------------


async def test_all_binary_sensors_snapshot(  # noqa: PLR0913 - standard HA snapshot-test fixture set
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Every binary created on the dev fixture matches its registry+state snapshot.

    The clock is frozen and the startup background pipeline (statistics
    backfill, then the analytics engine's first pass) is settled before
    snapshotting: the analytics binaries' attributes exist only once the engine
    has run, so an unsettled pipeline would make the snapshot a race.
    """
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(mock_api)

    assert await setup_integration(hass, mock_config_entry)
    await hass.async_block_till_done(wait_background_tasks=True)

    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


async def test_exact_binary_set_on_dev_fixture(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """The dev fixture creates exactly the expected set; gated binaries are absent."""
    add_device_routes(mock_api)

    assert await setup_integration(hass, mock_config_entry)

    assert _created_keys(entity_registry, mock_config_entry.entry_id) == sorted(
        EXPECTED_KEYS
    )
    # The feature-gated binaries are never created for a regeneration-only
    # device: the two whose status fields are absent, and the wsov-gated one.
    assert _entity_id(entity_registry, "alarm_beeping") is None
    assert _entity_id(entity_registry, "water_to_drain_alert") is None
    assert _entity_id(entity_registry, "wsov_closed") is None


@pytest.mark.parametrize(
    ("key", "device_class", "category"),
    [
        (
            "online",
            BinarySensorDeviceClass.CONNECTIVITY,
            EntityCategory.DIAGNOSTIC,
        ),
        (
            "connection_alert",
            BinarySensorDeviceClass.PROBLEM,
            EntityCategory.DIAGNOSTIC,
        ),
        ("salt_level_alert", BinarySensorDeviceClass.PROBLEM, None),
    ],
)
async def test_binary_metadata(  # noqa: PLR0913
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    key: str,
    device_class: BinarySensorDeviceClass,
    category: EntityCategory | None,
) -> None:
    """Online is connectivity/diagnostic; connection_alert is a diagnostic problem."""
    add_device_routes(mock_api)

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry, key)
    assert entity_id is not None
    registry_entry = entity_registry.async_get(entity_id)
    assert registry_entry is not None
    assert registry_entry.entity_category == category
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes[ATTR_DEVICE_CLASS] == device_class


# ---------------------------------------------------------------------------
# Alert flip across a poll cycle
# ---------------------------------------------------------------------------


async def test_salt_level_alert_flips_on_after_poll(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A later poll returning ``salt_level_alert`` true drives the entity to ``on``."""
    alerted = _base_detail()
    _water_treatment(alerted)["water_treatment_status"]["salt_level_alert"] = True
    mock_api.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    # First refresh sees the clean fixture; every subsequent poll sees the alert.
    mock_api.get(device_url(), payload=load_fixture("device-detail.json"))
    mock_api.get(device_url(), payload=alerted, repeat=True)

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry, "salt_level_alert")
    assert entity_id is not None
    before = hass.states.get(entity_id)
    assert before is not None
    assert before.state == STATE_OFF

    freezer.tick(UPDATE_INTERVAL + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    after = hass.states.get(entity_id)
    assert after is not None
    assert after.state == STATE_ON


# ---------------------------------------------------------------------------
# Feature-gated binary existence paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("beeping", "expected_state"),
    [(None, STATE_UNKNOWN), (True, STATE_ON)],
    ids=["field-absent", "field-true"],
)
async def test_audible_alarm_created_via_feature(  # noqa: PLR0913
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    beeping: bool | None,
    expected_state: str,
) -> None:
    """The ``audible_alarm`` feature creates the sound binary regardless of the field.

    With the flag absent the state is ``unknown``; with it true the state is
    ``on``; the device class is always ``sound``.
    """
    detail = _base_detail()
    treatment = _water_treatment(detail)
    treatment["features"] = [*treatment["features"], "audible_alarm"]
    if beeping is not None:
        treatment["water_treatment_status"]["alarm_is_beeping"] = beeping
    _register_detail(mock_api, detail)

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry, "alarm_beeping")
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == expected_state
    assert state.attributes[ATTR_DEVICE_CLASS] == BinarySensorDeviceClass.SOUND
    # The sibling gated binary is not dragged in by this feature.
    assert _entity_id(entity_registry, "water_to_drain_alert") is None


@pytest.mark.parametrize(
    ("features_extra", "drain_value", "expected_state"),
    [
        (("leak_detector",), None, STATE_UNKNOWN),
        ((), True, STATE_ON),
    ],
    ids=["leak-detector-feature", "field-present-no-feature"],
)
async def test_water_to_drain_created_via_feature_and_field(  # noqa: PLR0913
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    features_extra: tuple[str, ...],
    drain_value: bool | None,
    expected_state: str,
) -> None:
    """Water-to-drain exists via a leak-detector feature or a present field alone.

    The feature path with the field absent yields ``unknown``; the bare
    field-present path (no feature advertised) yields the flag's value; the device
    class is always ``moisture``.
    """
    detail = _base_detail()
    treatment = _water_treatment(detail)
    treatment["features"] = [*treatment["features"], *features_extra]
    if drain_value is not None:
        treatment["water_treatment_status"]["water_to_drain_alert"] = drain_value
    _register_detail(mock_api, detail)

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry, "water_to_drain_alert")
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == expected_state
    assert state.attributes[ATTR_DEVICE_CLASS] == BinarySensorDeviceClass.MOISTURE
    # The audible-alarm binary is not created by either water-to-drain path.
    assert _entity_id(entity_registry, "alarm_beeping") is None


# ---------------------------------------------------------------------------
# Missing status block and offline availability split
# ---------------------------------------------------------------------------


async def test_missing_status_block_drops_alert_binaries(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """Without a status block the alert binaries vanish but ``online`` survives.

    The recharge-state binaries key off ``recharge_ui``/``regeneration`` — not
    the status block — so they are unaffected by its absence.
    """
    detail = _base_detail()
    del _water_treatment(detail)["water_treatment_status"]
    _register_detail(mock_api, detail)

    assert await setup_integration(hass, mock_config_entry)

    assert _created_keys(entity_registry, mock_config_entry.entry_id) == sorted(
        [
            "leak_suspected",
            "usage_anomaly",
            "vacation_detected",
            "online",
            "regenerating",
            "vacation_mode",
            "recharge_off",
            "regeneration_suspended",
        ]
    )
    online_id = _entity_id(entity_registry, "online")
    assert online_id is not None
    online_state = hass.states.get(online_id)
    assert online_state is not None
    assert online_state.state == STATE_ON
    for key in ("salt_level_alert", "connection_alert", "resin_alert"):
        assert _entity_id(entity_registry, key) is None


async def test_offline_device_keeps_alert_binaries_available(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """An offline device flips ``online`` off yet leaves every alert available."""
    detail = _base_detail()
    detail["is_online"] = False
    _register_detail(mock_api, detail)

    assert await setup_integration(hass, mock_config_entry)

    online_id = _entity_id(entity_registry, "online")
    assert online_id is not None
    online_state = hass.states.get(online_id)
    assert online_state is not None
    assert online_state.state == STATE_OFF

    # No binary gates on device_online, so none goes unavailable when the
    # softener is offline — the alerts are precisely what matters during an outage.
    entries = er.async_entries_for_config_entry(
        entity_registry, mock_config_entry.entry_id
    )
    assert len(entries) == len(EXPECTED_KEYS)
    for registry_entry in entries:
        state = hass.states.get(registry_entry.entity_id)
        assert state is not None
        assert state.state != STATE_UNAVAILABLE

    salt_id = _entity_id(entity_registry, "salt_level_alert")
    assert salt_id is not None
    salt_state = hass.states.get(salt_id)
    assert salt_state is not None
    assert salt_state.state == STATE_OFF
