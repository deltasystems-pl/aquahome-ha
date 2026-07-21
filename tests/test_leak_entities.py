"""Tests for the per-leak-detector entities across two platforms.

Each paired leak detector is registered as its own sub-device (``via_device`` the
softener) and contributes six entities split over two platforms: four binaries on
``binary_sensor`` — leak detected (moisture), low battery (battery, diagnostic),
tamper, connectivity (diagnostic) — and two sensors on ``sensor`` — a Fahrenheit
temperature (converted for display by Home Assistant's unit system) and a dBm
signal strength (diagnostic, registry-disabled by default). Because the family
spans two domains, this module forwards *both* platforms so the leak sub-device is
assembled end-to-end against the ``aioresponses`` HTTP fakes and the synthetic
leak-detector builders from ``conftest``; the softener still materialises its own
telemetry entities, which every leak-scoped helper filters out by unique-id stem.

Coverage: the exact twelve-entity set two detectors create and their
``{slug}_leak_{id}_{key}`` unique ids; every binary's payload-to-state mapping
plus its device class / category; the Fahrenheit native temperature and its
HA-side conversion; the registry-disabled diagnostic signal sensor and its value
once enabled; each detector as its own registry device wired ``via_device`` to the
softener under its nickname; a detector vanishing from a later poll going
unavailable (still registered) while the survivor is untouched; tolerance of a
missing status block and of individual missing flag objects (``unknown``, never an
exception); and a syrupy snapshot of the whole leak entity set.
"""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest
from homeassistant.components.binary_sensor import (
    DOMAIN as BINARY_SENSOR_DOMAIN,
)
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
)
from homeassistant.components.sensor import (
    DOMAIN as SENSOR_DOMAIN,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    Platform,
    UnitOfTemperature,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.helpers.entity_registry import RegistryEntryDisabler
from homeassistant.util.unit_conversion import TemperatureConverter
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.aquahome.const import DOMAIN, MANUFACTURER, UPDATE_INTERVAL
from tests.conftest import (
    add_activity_routes,
    add_device_routes,
    add_settings_routes,
    device_url,
    devices_url,
    load_fixture,
    make_leak_detector,
    setup_integration,
    with_leak_detectors,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aioresponses import aioresponses
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from syrupy.assertion import SnapshotAssertion

#: Slug of the captured device's serial ``7384243-20203-1120`` (see contract).
SLUG = "7384243_20203_1120"

#: The four per-detector binaries (``binary_sensor`` domain).
LEAK_BINARY_KEYS = (
    "leak_detected",
    "leak_low_battery",
    "leak_tampered",
    "leak_connectivity",
)
#: The two per-detector sensors (``sensor`` domain).
LEAK_SENSOR_KEYS = ("leak_temperature", "leak_signal_strength")
#: Every per-detector entity key, whatever its platform.
LEAK_KEYS = LEAK_BINARY_KEYS + LEAK_SENSOR_KEYS


@pytest.fixture(autouse=True)
def _both_leak_platforms() -> Iterator[None]:
    """Forward the two platforms the leak sub-device spans, and nothing else.

    The rest of set-up still runs end-to-end; the binaries live on
    ``binary_sensor`` and the temperature/signal sensors on ``sensor``, so both
    are needed to assemble one detector's full entity set.
    """
    with patch(
        "custom_components.aquahome.PLATFORMS",
        [Platform.BINARY_SENSOR, Platform.SENSOR],
    ):
        yield


# ---------------------------------------------------------------------------
# Payload helpers (never mutate the loaded fixture in place)
# ---------------------------------------------------------------------------


def _base_detail() -> dict[str, Any]:
    """Return a deep copy of the captured device-detail payload to mutate."""
    return copy.deepcopy(load_fixture("device-detail.json"))


def _detail_with(*detectors: dict[str, Any]) -> dict[str, Any]:
    """Return a device-detail payload carrying the given leak detectors."""
    return with_leak_detectors(_base_detail(), list(detectors))


def _domain_of(key: str) -> str:
    """Return the platform domain a leak entity key belongs to."""
    return BINARY_SENSOR_DOMAIN if key in LEAK_BINARY_KEYS else SENSOR_DOMAIN


def _uid(detector_id: int, key: str) -> str:
    """Return the leak entity unique id for ``detector_id`` and ``key``."""
    return f"{SLUG}_leak_{detector_id}_{key}"


def _entity_id(
    entity_registry: er.EntityRegistry, detector_id: int, key: str
) -> str | None:
    """Resolve a leak entity's id from its detector id and description key."""
    return entity_registry.async_get_entity_id(
        _domain_of(key), DOMAIN, _uid(detector_id, key)
    )


def _state(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    detector_id: int,
    key: str,
) -> str | None:
    """Return the state-machine string of a leak entity, or ``None`` if absent."""
    entity_id = _entity_id(entity_registry, detector_id, key)
    if entity_id is None:
        return None
    state = hass.states.get(entity_id)
    return state.state if state is not None else None


def _native(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    detector_id: int,
    key: str,
) -> Any:
    """Return a leak sensor's unit-independent native value via its live entity."""
    entity_id = _entity_id(entity_registry, detector_id, key)
    assert entity_id is not None, f"leak sensor {key} for #{detector_id} not registered"
    component = hass.data[DATA_INSTANCES][_domain_of(key)]
    entity = component.get_entity(entity_id)
    assert entity is not None, (
        f"leak sensor {key} for #{detector_id} has no live entity"
    )
    return cast(SensorEntity, entity).native_value


def _leak_unique_ids(entity_registry: er.EntityRegistry, entry_id: str) -> list[str]:
    """Return the sorted unique ids of every leak-scoped entity for the entry."""
    entries = er.async_entries_for_config_entry(entity_registry, entry_id)
    return sorted(
        entry.unique_id
        for entry in entries
        if entry.unique_id.startswith(f"{SLUG}_leak_")
    )


# ---------------------------------------------------------------------------
# Existence + identity
# ---------------------------------------------------------------------------


async def test_two_detectors_full_leak_entity_set(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """Two detectors create exactly six entities each, keyed ``{slug}_leak_{id}_{key}``."""
    add_device_routes(
        mock_api,
        device_detail=_detail_with(
            make_leak_detector(1, nickname="Kitchen"),
            make_leak_detector(2, nickname="Basement"),
        ),
    )

    assert await setup_integration(hass, mock_config_entry)

    for detector_id in (1, 2):
        for key in LEAK_KEYS:
            entity_id = _entity_id(entity_registry, detector_id, key)
            assert entity_id is not None, f"missing {key} for detector #{detector_id}"
            registry_entry = entity_registry.async_get(entity_id)
            assert registry_entry is not None
            assert registry_entry.unique_id == _uid(detector_id, key)

    # Exactly the twelve leak entities and no more, across both platforms.
    assert _leak_unique_ids(entity_registry, mock_config_entry.entry_id) == sorted(
        _uid(detector_id, key) for detector_id in (1, 2) for key in LEAK_KEYS
    )


# ---------------------------------------------------------------------------
# Binary payload mapping + metadata
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "expected_state", "device_class", "category"),
    [
        (
            "leak_detected",
            STATE_ON,
            BinarySensorDeviceClass.MOISTURE,
            None,
        ),
        (
            "leak_low_battery",
            STATE_OFF,
            BinarySensorDeviceClass.BATTERY,
            EntityCategory.DIAGNOSTIC,
        ),
        (
            "leak_tampered",
            STATE_OFF,
            BinarySensorDeviceClass.TAMPER,
            None,
        ),
        (
            "leak_connectivity",
            STATE_ON,
            BinarySensorDeviceClass.CONNECTIVITY,
            EntityCategory.DIAGNOSTIC,
        ),
    ],
)
async def test_leak_binary_values_and_metadata(  # noqa: PLR0913
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    key: str,
    expected_state: str,
    device_class: BinarySensorDeviceClass,
    category: EntityCategory | None,
) -> None:
    """Each binary maps its flag to state and carries its device class / category.

    The synthetic detector leaks (``leak_detected`` → ``on``) while battery and
    tamper are healthy (``off``) and it is connected (``leak_connectivity`` → ``on``
    for the connectivity class). Battery and connectivity are diagnostics; leak and
    tamper are user-visible.
    """
    add_device_routes(
        mock_api,
        device_detail=_detail_with(
            make_leak_detector(
                1,
                nickname="Kitchen",
                leak=True,
                low_battery=False,
                tampered=False,
                connected=True,
            )
        ),
    )

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry, 1, key)
    assert entity_id is not None
    registry_entry = entity_registry.async_get(entity_id)
    assert registry_entry is not None
    assert registry_entry.entity_category == category
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == expected_state
    assert state.attributes[ATTR_DEVICE_CLASS] == device_class


async def test_leak_connectivity_reflects_disconnected(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """A disconnected detector drives the connectivity binary ``off``."""
    add_device_routes(
        mock_api,
        device_detail=_detail_with(
            make_leak_detector(1, nickname="Kitchen", connected=False)
        ),
    )

    assert await setup_integration(hass, mock_config_entry)

    assert _state(hass, entity_registry, 1, "leak_connectivity") == STATE_OFF


# ---------------------------------------------------------------------------
# Temperature — Fahrenheit native, HA-side conversion
# ---------------------------------------------------------------------------


async def test_leak_temperature_native_fahrenheit_converted_by_ha(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """The temperature is native Fahrenheit; HA converts it to the display unit.

    ``raw_value`` (68 °F) is the entity's native value in its native Fahrenheit
    unit; the state machine shows the unit-system unit (68 °F is 20 °C for a
    metric account), proving the conversion is HA's, not fabricated in the value
    function.
    """
    add_device_routes(
        mock_api,
        device_detail=_detail_with(
            make_leak_detector(1, nickname="Kitchen", temperature_raw=68)
        ),
    )

    assert await setup_integration(hass, mock_config_entry)

    assert _native(hass, entity_registry, 1, "leak_temperature") == 68

    entity_id = _entity_id(entity_registry, 1, "leak_temperature")
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.TEMPERATURE

    display_unit = hass.config.units.temperature_unit
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == display_unit
    expected = TemperatureConverter.convert(
        68, UnitOfTemperature.FAHRENHEIT, display_unit
    )
    assert float(state.state) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Signal strength — diagnostic, registry-disabled by default
# ---------------------------------------------------------------------------


async def test_leak_signal_strength_diagnostic_disabled_by_default(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """Signal strength is a disabled-by-default dBm diagnostic; value shows once enabled.

    Mirroring the softener RF sensor, the entity is registered disabled so it does
    not clutter the dashboard; enabling it and reloading surfaces the payload's
    ``signal_strength`` (-60 dBm).
    """
    add_device_routes(
        mock_api,
        device_detail=_detail_with(
            make_leak_detector(1, nickname="Kitchen", signal=-60)
        ),
    )

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry, 1, "leak_signal_strength")
    assert entity_id is not None
    registry_entry = entity_registry.async_get(entity_id)
    assert registry_entry is not None
    assert registry_entry.disabled_by is RegistryEntryDisabler.INTEGRATION
    assert registry_entry.entity_category == EntityCategory.DIAGNOSTIC
    assert registry_entry.original_device_class == SensorDeviceClass.SIGNAL_STRENGTH
    # Disabled entities have no live state yet.
    assert hass.states.get(entity_id) is None

    entity_registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert _native(hass, entity_registry, 1, "leak_signal_strength") == -60
    state = hass.states.get(entity_id)
    assert state is not None
    assert (
        state.attributes[ATTR_UNIT_OF_MEASUREMENT] == SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    )
    assert float(state.state) == -60


# ---------------------------------------------------------------------------
# Sub-device registration (via_device + nickname)
# ---------------------------------------------------------------------------


async def test_each_detector_is_its_own_device_via_softener(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Each detector is its own registry device, wired ``via_device`` to the softener.

    The sub-device is named for the detector's nickname, models ``Leak detector``,
    shares the ``iQua`` manufacturer, and hangs off the softener device.
    """
    add_device_routes(
        mock_api,
        device_detail=_detail_with(
            make_leak_detector(1, nickname="Kitchen"),
            make_leak_detector(2, nickname="Basement"),
        ),
    )

    assert await setup_integration(hass, mock_config_entry)

    softener = device_registry.async_get_device(identifiers={(DOMAIN, SLUG)})
    assert softener is not None

    for detector_id, nickname in ((1, "Kitchen"), (2, "Basement")):
        sub_device = device_registry.async_get_device(
            identifiers={(DOMAIN, f"{SLUG}_leak_{detector_id}")}
        )
        assert sub_device is not None
        assert sub_device.name == nickname
        assert sub_device.model == "Leak detector"
        assert sub_device.manufacturer == MANUFACTURER
        assert sub_device.via_device_id == softener.id


# ---------------------------------------------------------------------------
# A detector vanishing from a later poll
# ---------------------------------------------------------------------------


async def test_detector_vanishing_goes_unavailable_survivor_unaffected(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A detector dropped by a later poll goes unavailable but stays registered.

    Known dynamic keys are never removed, so detector #2's entities remain in the
    registry and merely go ``unavailable`` (their detector is gone) while
    detector #1 — still present — is untouched.
    """
    first = _detail_with(
        make_leak_detector(1, nickname="Kitchen", leak=True),
        make_leak_detector(2, nickname="Basement"),
    )
    second = _detail_with(make_leak_detector(1, nickname="Kitchen", leak=True))

    mock_api.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    # First refresh sees both detectors; every later poll sees only detector #1.
    mock_api.get(device_url(), payload=first)
    mock_api.get(device_url(), payload=second, repeat=True)
    add_activity_routes(mock_api)
    add_settings_routes(mock_api)

    assert await setup_integration(hass, mock_config_entry)

    # Both detectors present initially.
    assert _state(hass, entity_registry, 1, "leak_detected") == STATE_ON
    assert _state(hass, entity_registry, 2, "leak_detected") == STATE_OFF

    freezer.tick(UPDATE_INTERVAL + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    # Detector #2 vanished: every entity unavailable, yet all still registered.
    for key in (*LEAK_BINARY_KEYS, "leak_temperature"):
        entity_id = _entity_id(entity_registry, 2, key)
        assert entity_id is not None, f"detector #2 {key} was deleted, not kept"
        assert _state(hass, entity_registry, 2, key) == STATE_UNAVAILABLE

    # The surviving detector #1 is unaffected.
    assert _state(hass, entity_registry, 1, "leak_detected") == STATE_ON
    assert _state(hass, entity_registry, 1, "leak_connectivity") == STATE_ON


# ---------------------------------------------------------------------------
# Tolerance — missing status block / missing flag objects
# ---------------------------------------------------------------------------


async def test_missing_status_block_reports_unknown(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """A detector with no status block yields ``unknown`` entities, never an error.

    The detector is present (so its entities exist and stay available), but with no
    status every flag and reading is unknowable — reported as ``unknown`` rather
    than a fabricated ``off`` or an exception at setup.
    """
    detector = {
        "detector_id": 3,
        "nickname": "Garage",
        "last_updated_at": "2026-07-21T10:00:00Z",
    }
    add_device_routes(mock_api, device_detail=_detail_with(detector))

    assert await setup_integration(hass, mock_config_entry)

    for key in (*LEAK_BINARY_KEYS, "leak_temperature"):
        assert _entity_id(entity_registry, 3, key) is not None
        assert _state(hass, entity_registry, 3, key) == STATE_UNKNOWN


async def test_missing_flag_objects_report_unknown_others_intact(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """Individually absent flag objects read ``unknown`` while present ones report.

    Dropping ``leak_detected`` and ``temperature`` from an otherwise-populated
    status block collapses just those two to ``unknown``; the surviving flags
    (battery, tamper, connectivity) still map their booleans.
    """
    detector = make_leak_detector(
        4, nickname="Attic", low_battery=True, tampered=False, connected=True
    )
    del detector["status"]["leak_detected"]
    del detector["status"]["temperature"]
    add_device_routes(mock_api, device_detail=_detail_with(detector))

    assert await setup_integration(hass, mock_config_entry)

    # Absent flag objects -> unknown.
    assert _state(hass, entity_registry, 4, "leak_detected") == STATE_UNKNOWN
    assert _state(hass, entity_registry, 4, "leak_temperature") == STATE_UNKNOWN
    # Surviving flags still map their values.
    assert _state(hass, entity_registry, 4, "leak_low_battery") == STATE_ON
    assert _state(hass, entity_registry, 4, "leak_tampered") == STATE_OFF
    assert _state(hass, entity_registry, 4, "leak_connectivity") == STATE_ON


# ---------------------------------------------------------------------------
# Snapshot of the whole leak entity set
# ---------------------------------------------------------------------------


async def test_full_leak_entity_set_snapshot(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Every leak entity's registry entry and state matches the stored snapshot.

    Two detectors' twelve entities are captured across both platforms; the
    disabled-by-default signal-strength sensors are enabled and the entry reloaded
    first so the snapshot covers them too. Only leak-scoped entities are asserted,
    so the softener's own telemetry never churns this snapshot.
    """
    add_device_routes(
        mock_api,
        device_detail=_detail_with(
            make_leak_detector(1, nickname="Kitchen", leak=True, signal=-60),
            make_leak_detector(2, nickname="Basement", low_battery=True),
        ),
    )

    assert await setup_integration(hass, mock_config_entry)

    entries = er.async_entries_for_config_entry(
        entity_registry, mock_config_entry.entry_id
    )
    leak_entries = [
        entry for entry in entries if entry.unique_id.startswith(f"{SLUG}_leak_")
    ]
    for entry in leak_entries:
        if entry.disabled_by is not None:
            entity_registry.async_update_entity(entry.entity_id, disabled_by=None)
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    leak_entries = sorted(
        (
            entry
            for entry in er.async_entries_for_config_entry(
                entity_registry, mock_config_entry.entry_id
            )
            if entry.unique_id.startswith(f"{SLUG}_leak_")
        ),
        key=lambda entry: entry.entity_id,
    )
    assert leak_entries

    for entry in leak_entries:
        assert entry == snapshot(name=f"{entry.entity_id}-entry")
        assert entry.disabled_by is None
        state = hass.states.get(entry.entity_id)
        assert state is not None, f"no state for {entry.entity_id}"
        assert state == snapshot(name=f"{entry.entity_id}-state")
