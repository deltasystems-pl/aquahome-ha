"""Tests for the AquaHome sensor platform.

These are end-to-end integration tests: a real config entry is set up against
the captured iQua fixtures served through ``aioresponses``, and the resulting
entities are inspected via the state machine, the entity registry, and (where a
native, unit-system-independent value must be asserted) the live entity object.

Time is frozen for every test that sets the integration up so the computed
``out_of_salt_estimate`` timestamp, the RestoreSensor clamp, and the coordinator
poll cadence are all deterministic. The reference account is *metric*, so volume
sensors that bind to native US gallons are displayed in litres — the assertions
deliberately check both the native gallon value and the converted litre state to
guard the historical unit-mislabel regression (community PAIN #5).
"""

from __future__ import annotations

import copy
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest
from homeassistant.components.sensor import (
    DEVICE_CLASS_STATE_CLASSES,
    SensorEntity,
)
from homeassistant.components.sensor.const import DEVICE_CLASS_UNITS
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
    UnitOfVolume,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    snapshot_platform,
)

from custom_components.aquahome.const import DOMAIN, UPDATE_INTERVAL
from custom_components.aquahome.sensor import (
    SENSOR_DESCRIPTIONS,
    AquaHomeSensorDescription,
)
from tests.conftest import (
    add_device_routes,
    device_url,
    devices_url,
    load_fixture,
    setup_integration,
)

if TYPE_CHECKING:
    from aioresponses import aioresponses
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.typing import StateType
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from syrupy.assertion import SnapshotAssertion

#: Slug derived from the fixture serial ``7384243-20203-1120``.
SLUG = "7384243_20203_1120"
#: Fixed instant every setup test freezes to (2026-07-21T12:00:00Z).
FROZEN_INSTANT = "2026-07-21T12:00:00+00:00"
#: Countdown carried by the ``out_of_salt_estimate_days`` fixture property.
OUT_OF_SALT_DAYS = 167
#: Lifetime treated-water native gallon counter in the fixture — the raw
#: ``total_outlet_water_gals`` property (the enriched copy lags it at 47420).
TOTAL_WATER_GALLONS = 47479

_ONLY_SENSOR = patch("custom_components.aquahome.PLATFORMS", [Platform.SENSOR])


# ---------------------------------------------------------------------------
# Local helpers (never mutate fixture files — always deepcopy)
# ---------------------------------------------------------------------------


def _load_detail() -> dict[str, Any]:
    """Return an isolated deep copy of the device-detail fixture."""
    return copy.deepcopy(load_fixture("device-detail.json"))


def _set_total_water_base(detail: dict[str, Any], gallons: float) -> None:
    """Overwrite the raw lifetime counter the total-water sensor binds.

    The sensor reads the raw ``total_outlet_water_gals`` property (the enriched
    copy lags it — live finding 2026-07-27), so the clamp scenarios manipulate
    that property directly.
    """
    detail["properties"]["total_outlet_water_gals"]["value"] = gallons


def _entity_id(hass: HomeAssistant, key: str) -> str:
    """Resolve the entity id for a sensor description key via its unique id."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{SLUG}_{key}")
    assert entity_id is not None, f"sensor {key} was not registered"
    return entity_id


def _sensor(hass: HomeAssistant, key: str) -> SensorEntity:
    """Return the live sensor entity object for a description key."""
    component = hass.data[DATA_INSTANCES]["sensor"]
    entity = component.get_entity(_entity_id(hass, key))
    assert entity is not None, f"sensor {key} has no live entity"
    return cast(SensorEntity, entity)


def _native(hass: HomeAssistant, key: str) -> StateType | date | datetime | Decimal:
    """Return a sensor's native value (unit-system independent)."""
    return _sensor(hass, key).native_value


# ---------------------------------------------------------------------------
# Snapshot of the whole platform
# ---------------------------------------------------------------------------


async def test_all_sensor_entities(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    snapshot: SnapshotAssertion,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Every sensor's registry entry and state matches the stored snapshot.

    ``rf_signal_strength`` and ``salt_depletion_estimate`` are disabled by
    default, so they are enabled in the registry and the entry reloaded before
    snapshotting — ``snapshot_platform`` requires every entity to be enabled,
    which lets the snapshot cover the full platform including the disabled
    diagnostics.
    """
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(mock_api)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)
        registry = er.async_get(hass)
        for key in ("rf_signal_strength", "salt_depletion_estimate"):
            entity_id = _entity_id(hass, key)
            entry = registry.async_get(entity_id)
            assert entry is not None
            assert entry.disabled_by is not None
            registry.async_update_entity(entity_id, disabled_by=None)
        await hass.config_entries.async_reload(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    await snapshot_platform(
        hass, er.async_get(hass), snapshot, mock_config_entry.entry_id
    )


# ---------------------------------------------------------------------------
# High-value explicit assertions (independent of the snapshot)
# ---------------------------------------------------------------------------


async def test_native_values_and_unit_labels(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Volume sensors expose native gallons and display metric litres.

    Binding to the stable native unit (not the account's litre preference) is
    the fix for the unit-mislabel regression: the lifetime counter's native
    value is the raw 47479-gallon property and Home Assistant converts that to
    ~179728 L for the metric account, never the raw litres masqueraded as
    gallons. The counters read the raw properties (47479 / 3), not the lagging
    enriched copies (47420 / 0) captured in the very same fixture.
    """
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(mock_api)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    assert _native(hass, "total_water") == TOTAL_WATER_GALLONS
    assert _native(hass, "treated_water_available") == 185
    assert _native(hass, "water_used_today") == 3
    assert _native(hass, "salt_level") == 37.5
    assert _native(hass, "average_daily_water_use") == 47

    total_state = hass.states.get(_entity_id(hass, "total_water"))
    assert total_state is not None
    assert total_state.attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfVolume.LITERS
    assert float(total_state.state) == pytest.approx(179727.6, rel=1e-4)

    salt_state = hass.states.get(_entity_id(hass, "salt_level"))
    assert salt_state is not None
    assert salt_state.state == "37.5"
    assert salt_state.attributes[ATTR_UNIT_OF_MEASUREMENT] == "%"


async def test_out_of_salt_estimate_uses_device_timezone(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The out-of-salt timestamp is midnight, device-local, today + 167 days."""
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(mock_api)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    warsaw = dt_util.get_time_zone("Europe/Warsaw")
    assert warsaw is not None
    target = dt_util.now(warsaw).date() + timedelta(days=OUT_OF_SALT_DAYS)
    expected = datetime.combine(target, time(), tzinfo=warsaw)

    value = _native(hass, "out_of_salt_estimate")
    assert isinstance(value, datetime)
    assert value == expected
    # Device-local midnight of the target date (winter CET, +01:00 on 2027-01-04).
    assert value.tzinfo == warsaw
    assert (value.hour, value.minute, value.second) == (0, 0, 0)
    assert value.utcoffset() == timedelta(hours=1)


# ---------------------------------------------------------------------------
# Description-table validity against Home Assistant's own contracts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("description", SENSOR_DESCRIPTIONS, ids=lambda d: d.key)
def test_description_class_unit_combinations_valid(
    description: AquaHomeSensorDescription,
) -> None:
    """Each described device_class/state_class/unit trio is HA-valid.

    Mirrors the checks Home Assistant performs on a sensor: a set state class
    must be permitted for the device class, and a set native unit must be one
    the device class accepts. Descriptions without a device class are skipped.
    """
    device_class = description.device_class
    if device_class is None:
        pytest.skip("no device class to validate")

    allowed_state_classes = DEVICE_CLASS_STATE_CLASSES.get(device_class)
    if description.state_class is not None:
        assert allowed_state_classes, f"{device_class} permits no state class"
        assert description.state_class in allowed_state_classes

    allowed_units = DEVICE_CLASS_UNITS.get(device_class)
    if description.native_unit_of_measurement is not None:
        assert allowed_units is not None, f"{device_class} defines no units"
        assert description.native_unit_of_measurement in allowed_units


# ---------------------------------------------------------------------------
# out_of_salt_estimate edge cases
# ---------------------------------------------------------------------------


async def test_out_of_salt_estimate_missing_tz_falls_back_to_utc(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A missing ``tz_id`` property yields UTC midnight, not a crash."""
    freezer.move_to(FROZEN_INSTANT)
    detail = _load_detail()
    del detail["properties"]["tz_id"]
    add_device_routes(mock_api, device_detail=detail)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    target = dt_util.now(dt_util.UTC).date() + timedelta(days=OUT_OF_SALT_DAYS)
    expected = datetime.combine(target, time(), tzinfo=dt_util.UTC)

    value = _native(hass, "out_of_salt_estimate")
    assert isinstance(value, datetime)
    assert value == expected
    assert value.utcoffset() == timedelta(0)


async def test_out_of_salt_estimate_absent_when_property_missing(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """No countdown property means the entity is never created (exists_fn)."""
    freezer.move_to(FROZEN_INSTANT)
    detail = _load_detail()
    del detail["properties"]["out_of_salt_estimate_days"]
    add_device_routes(mock_api, device_detail=detail)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id("sensor", DOMAIN, f"{SLUG}_out_of_salt_estimate")
        is None
    )


# ---------------------------------------------------------------------------
# salt_level existence gate
# ---------------------------------------------------------------------------


async def test_salt_level_absent_when_monitoring_disabled(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Salt monitoring turned off in the payload suppresses the salt entity."""
    freezer.move_to(FROZEN_INSTANT)
    detail = _load_detail()
    detail["enriched_data"]["water_treatment"]["salt_level"]["monitoring_enabled"] = (
        False
    )
    add_device_routes(mock_api, device_detail=detail)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{SLUG}_salt_level") is None


# ---------------------------------------------------------------------------
# total_water monotonic clamp guard
# ---------------------------------------------------------------------------


async def _advance_one_poll(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Advance the frozen clock by one interval and drive a coordinator poll."""
    freezer.tick(UPDATE_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_total_water_clamps_small_downward_dip(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A ~2 % dip on the lifetime counter is held at the last reported value."""
    freezer.move_to(FROZEN_INSTANT)
    dipped = _load_detail()
    _set_total_water_base(dipped, TOTAL_WATER_GALLONS * 0.98)
    dipped["properties"]["gallons_used_today"]["value"] = 5

    mock_api.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock_api.get(device_url(), payload=load_fixture("device-detail.json"))
    mock_api.get(device_url(), payload=dipped, repeat=True)

    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)
        assert _native(hass, "total_water") == TOTAL_WATER_GALLONS
        await _advance_one_poll(hass, freezer)

    # The poll landed (daily-use sensor moved) but the counter held its value.
    assert _native(hass, "water_used_today") == 5
    assert _native(hass, "total_water") == TOTAL_WATER_GALLONS


async def test_total_water_accepts_large_counter_reset(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A large (60 %) drop is accepted as a genuine counter reset."""
    freezer.move_to(FROZEN_INSTANT)
    reset = _load_detail()
    _set_total_water_base(reset, TOTAL_WATER_GALLONS * 0.4)

    mock_api.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock_api.get(device_url(), payload=load_fixture("device-detail.json"))
    mock_api.get(device_url(), payload=reset, repeat=True)

    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)
        assert _native(hass, "total_water") == TOTAL_WATER_GALLONS
        await _advance_one_poll(hass, freezer)

    assert _native(hass, "total_water") == pytest.approx(TOTAL_WATER_GALLONS * 0.4)


async def test_total_water_restore_still_clamps_after_restart(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A restored last value clamps a small dip served on the next start."""
    freezer.move_to(FROZEN_INSTANT)
    dipped = _load_detail()
    _set_total_water_base(dipped, TOTAL_WATER_GALLONS * 0.98)

    mock_api.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock_api.get(device_url(), payload=load_fixture("device-detail.json"))
    mock_api.get(device_url(), payload=dipped, repeat=True)

    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)
        assert _native(hass, "total_water") == TOTAL_WATER_GALLONS

        await hass.config_entries.async_unload(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    # New entity restored 47420 and clamps the 2 % lower value now served.
    assert _native(hass, "total_water") == TOTAL_WATER_GALLONS


# ---------------------------------------------------------------------------
# None-safety across an absent enriched block
# ---------------------------------------------------------------------------


async def test_value_sensors_unknown_when_enriched_absent(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A null enriched block yields ``unknown`` enriched sensors, never a crash.

    The water counters survive on their raw-property sources; only sensors with
    no raw twin go unknown.
    """
    freezer.move_to(FROZEN_INSTANT)
    detail = _load_detail()
    detail["enriched_data"] = None
    add_device_routes(mock_api, device_detail=detail)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    for key in ("treated_water_available", "model"):
        state = hass.states.get(_entity_id(hass, key))
        assert state is not None
        assert state.state == STATE_UNKNOWN

    # The counters read raw properties, so a broken enriched block cannot
    # blank them (the enriched copy lags the raw truth anyway).
    assert _native(hass, "total_water") == TOTAL_WATER_GALLONS
    assert _native(hass, "water_used_today") == 3

    # A device-root field survives regardless of the enriched block.
    serial_state = hass.states.get(_entity_id(hass, "serial_number"))
    assert serial_state is not None
    assert serial_state.state == "7384243-20203-1120"
    assert serial_state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE)


async def test_water_counters_fall_back_to_enriched_without_raw_properties(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Without the raw counter properties, the enriched copies still serve.

    The lagging-but-present enriched values (47420 / 0 in the fixture) are the
    honest fallback for a payload variant that omits the properties map.
    """
    freezer.move_to(FROZEN_INSTANT)
    detail = _load_detail()
    del detail["properties"]["total_outlet_water_gals"]
    del detail["properties"]["gallons_used_today"]
    add_device_routes(mock_api, device_detail=detail)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    assert _native(hass, "total_water") == 47420
    assert _native(hass, "water_used_today") == 0
