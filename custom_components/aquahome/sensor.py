"""Sensor platform for the AquaHome integration.

Each sensor is a small, declarative :class:`AquaHomeSensorDescription`: a
``value_fn`` that reads one already-parsed value out of the coordinator's
:class:`~.api.models.Device`, and an ``exists_fn`` that decides — once, at setup
— whether the source data is present for this account/device. Every read is
None-safe: the enriched block, its sub-objects, and individual raw properties
are all optional on real payloads, so a missing value becomes ``None`` (Home
Assistant renders it as ``unknown``) rather than an error.

Two conventions are load-bearing and deliberate:

- Volume sensors bind to the stable native unit (US gallons) via
  :attr:`~.api.models.ConvertedProperty.base_value`, never the top-level
  ``value`` that follows the account's unit preference — a sensor labelled
  gallons but fed the account's litre value is the classic unit-mislabel bug.
- Measurement-class volumes use ``VOLUME_STORAGE`` (which permits the
  ``MEASUREMENT`` state class and gives metric users automatic litre display);
  only the monotonic lifetime/daily counters use the ``WATER`` class, which HA
  restricts to the ``TOTAL``/``TOTAL_INCREASING`` state classes.

The lifetime total-water counter is a :class:`~homeassistant.components.sensor.RestoreSensor`
with a monotonic clamp guard, so a transient cloud dip on the counter is not
misread by ``total_increasing`` long-term statistics as a meter reset.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfTime,
    UnitOfVolume,
)
from homeassistant.util import dt as dt_util

from .api import Device, PropertyValue, WaterTreatment, scaled_value
from .const import TOTAL_WATER_CLAMP_TOLERANCE
from .entity import AquaHomeEntity

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
    from homeassistant.helpers.typing import StateType

    from .coordinator import AquaHomeConfigEntry, AquaHomeCoordinator

_LOGGER = logging.getLogger(__name__)

#: Description key of the RestoreSensor with the clamp guard; setup dispatches on
#: it because that one sensor needs a dedicated entity class, not the generic one.
_TOTAL_WATER_KEY = "total_water"


@dataclass(frozen=True, kw_only=True)
class AquaHomeSensorDescription(SensorEntityDescription):
    """Describe one AquaHome sensor and how to read its value.

    ``value_fn`` maps a coordinator :class:`~.api.models.Device` to the sensor's
    native value (or ``None`` when the source is absent). ``exists_fn`` gates
    whether the entity is created at all for a given device — evaluated once at
    setup against the first refreshed payload.
    """

    value_fn: Callable[[Device], StateType | datetime]
    exists_fn: Callable[[Device], bool] = lambda device: True


# ---------------------------------------------------------------------------
# None-safe accessors
#
# Every value function goes through these so a missing enriched block, absent
# sub-object, or unset raw property collapses to ``None`` instead of raising.
# ---------------------------------------------------------------------------


def _enriched(device: Device) -> WaterTreatment | None:
    """Return the device's enriched water-treatment block, or ``None``."""
    return device.enriched_data


def _property(device: Device, name: str) -> PropertyValue | None:
    """Return the named raw property, or ``None`` when it is absent."""
    return device.properties.get(name)


def _prop_number(device: Device, name: str) -> float | None:
    """Return the numeric value of a raw property (via :func:`scaled_value`)."""
    prop = device.properties.get(name)
    return scaled_value(prop) if prop is not None else None


def _prop_str(device: Device, name: str) -> str | None:
    """Return a raw property's value when it is a string, else ``None``."""
    prop = device.properties.get(name)
    if prop is None or not isinstance(prop.value, str):
        return None
    return prop.value


# ---------------------------------------------------------------------------
# Value functions (Device -> native value)
# ---------------------------------------------------------------------------


def _salt_level(device: Device) -> StateType:
    """Return the salt-fill percentage from the enriched salt-level block."""
    enriched = _enriched(device)
    if enriched is None or enriched.salt_level is None:
        return None
    return enriched.salt_level.salt_level_percent


def _water_used_today(device: Device) -> StateType:
    """Return today's water use in gallons (the enriched, always-gallons field)."""
    enriched = _enriched(device)
    return enriched.gallons_used_today if enriched is not None else None


def _treated_water_available(device: Device) -> StateType:
    """Return remaining treated-water capacity in stable native gallons."""
    enriched = _enriched(device)
    if enriched is None or enriched.treated_water_available is None:
        return None
    return enriched.treated_water_available.base_value


def _total_water(device: Device) -> StateType:
    """Return the lifetime treated-water total in stable native gallons."""
    enriched = _enriched(device)
    if enriched is None or enriched.total_water_used is None:
        return None
    return enriched.total_water_used.base_value


def _days_since_last_recharge(device: Device) -> StateType:
    """Return whole days elapsed since the last recharge."""
    enriched = _enriched(device)
    return enriched.days_since_last_recharge if enriched is not None else None


def _days_powered_up(device: Device) -> StateType:
    """Return the cumulative days the unit has been powered up."""
    enriched = _enriched(device)
    return enriched.days_powered_up if enriched is not None else None


def _total_recharges(device: Device) -> StateType:
    """Return the lifetime recharge count."""
    enriched = _enriched(device)
    return enriched.total_recharges if enriched is not None else None


def _rf_signal_strength(device: Device) -> StateType:
    """Return the RF link strength to the valve head in dBm."""
    enriched = _enriched(device)
    return enriched.rf_signal_strength_dbm if enriched is not None else None


def _out_of_salt_estimate(device: Device) -> datetime | None:
    """Return the projected out-of-salt date as a device-local midnight timestamp.

    Combines the raw ``out_of_salt_estimate_days`` countdown with the device's
    ``tz_id`` so the result is a stable point in time — the start of the day the
    softener is expected to run out of salt — rather than a jittery relative
    count. A missing or unrecognised timezone falls back to UTC.
    """
    days = _prop_number(device, "out_of_salt_estimate_days")
    if days is None:
        return None
    tz_value = _prop_str(device, "tz_id")
    tz = (dt_util.get_time_zone(tz_value) if tz_value else None) or dt_util.UTC
    target_date = dt_util.now(tz).date() + timedelta(days=int(days))
    return datetime.combine(target_date, time(), tzinfo=tz)


def _average_daily_water_use(device: Device) -> StateType:
    """Return the rolling average daily water use in native gallons."""
    return _prop_number(device, "avg_daily_use_gals")


def _model(device: Device) -> StateType:
    """Return the marketing model name."""
    enriched = _enriched(device)
    return enriched.model if enriched is not None else None


def _serial_number(device: Device) -> StateType:
    """Return the device serial number."""
    return device.serial_number


def _control_version(device: Device) -> StateType:
    """Return the control-board firmware version string."""
    enriched = _enriched(device)
    return enriched.control_version if enriched is not None else None


def _wifi_module_version(device: Device) -> StateType:
    """Return the Wi-Fi module firmware/part version string."""
    enriched = _enriched(device)
    return enriched.wifi_module_version if enriched is not None else None


# ---------------------------------------------------------------------------
# Existence gates
# ---------------------------------------------------------------------------


def _exists_salt_level(device: Device) -> bool:
    """Report whether the salt-level block is present and monitoring is on."""
    enriched = _enriched(device)
    return (
        enriched is not None
        and enriched.salt_level is not None
        and enriched.salt_level.monitoring_enabled
    )


def _exists_out_of_salt_estimate(device: Device) -> bool:
    """Report whether the out-of-salt countdown property is present."""
    return _property(device, "out_of_salt_estimate_days") is not None


def _exists_average_daily_water_use(device: Device) -> bool:
    """Report whether the average-daily-use property is present."""
    return _property(device, "avg_daily_use_gals") is not None


SENSOR_DESCRIPTIONS: tuple[AquaHomeSensorDescription, ...] = (
    AquaHomeSensorDescription(
        key="salt_level",
        translation_key="salt_level",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=_salt_level,
        exists_fn=_exists_salt_level,
    ),
    AquaHomeSensorDescription(
        key="water_used_today",
        translation_key="water_used_today",
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfVolume.GALLONS,
        value_fn=_water_used_today,
    ),
    AquaHomeSensorDescription(
        key="treated_water_available",
        translation_key="treated_water_available",
        device_class=SensorDeviceClass.VOLUME_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfVolume.GALLONS,
        value_fn=_treated_water_available,
    ),
    AquaHomeSensorDescription(
        key=_TOTAL_WATER_KEY,
        translation_key=_TOTAL_WATER_KEY,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfVolume.GALLONS,
        value_fn=_total_water,
    ),
    AquaHomeSensorDescription(
        key="days_since_last_recharge",
        translation_key="days_since_last_recharge",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.DAYS,
        value_fn=_days_since_last_recharge,
    ),
    AquaHomeSensorDescription(
        key="days_powered_up",
        translation_key="days_powered_up",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.DAYS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_days_powered_up,
    ),
    AquaHomeSensorDescription(
        key="total_recharges",
        translation_key="total_recharges",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=_total_recharges,
    ),
    AquaHomeSensorDescription(
        key="rf_signal_strength",
        translation_key="rf_signal_strength",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_rf_signal_strength,
    ),
    AquaHomeSensorDescription(
        key="out_of_salt_estimate",
        translation_key="out_of_salt_estimate",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_out_of_salt_estimate,
        exists_fn=_exists_out_of_salt_estimate,
    ),
    AquaHomeSensorDescription(
        key="average_daily_water_use",
        translation_key="average_daily_water_use",
        device_class=SensorDeviceClass.VOLUME_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfVolume.GALLONS,
        value_fn=_average_daily_water_use,
        exists_fn=_exists_average_daily_water_use,
    ),
    AquaHomeSensorDescription(
        key="model",
        translation_key="model",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_model,
    ),
    AquaHomeSensorDescription(
        key="serial_number",
        translation_key="serial_number",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_serial_number,
    ),
    AquaHomeSensorDescription(
        key="control_version",
        translation_key="control_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_control_version,
    ),
    AquaHomeSensorDescription(
        key="wifi_module_version",
        translation_key="wifi_module_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_wifi_module_version,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AquaHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create sensor entities for every coordinator whose source data exists."""
    entities: list[SensorEntity] = []
    for coordinator in entry.runtime_data.coordinators.values():
        device = coordinator.data
        for description in SENSOR_DESCRIPTIONS:
            if not description.exists_fn(device):
                continue
            if description.key == _TOTAL_WATER_KEY:
                entities.append(AquaHomeTotalWaterSensor(coordinator, description))
            else:
                entities.append(AquaHomeSensor(coordinator, description))
    async_add_entities(entities)


class AquaHomeSensor(AquaHomeEntity, SensorEntity):
    """A generic AquaHome sensor backed by a description's ``value_fn``."""

    entity_description: AquaHomeSensorDescription

    @property
    def native_value(self) -> StateType | datetime:
        """Return the current value by applying the description's ``value_fn``."""
        return self.entity_description.value_fn(self.coordinator.data)


class AquaHomeTotalWaterSensor(AquaHomeEntity, RestoreSensor):
    """Lifetime treated-water counter with a monotonic clamp guard.

    The cloud occasionally reports a small downward blip on this ever-rising
    counter. Left unguarded, ``total_increasing`` long-term statistics would
    read the next rise as a meter reset and record a giant phantom consumption.
    This sensor therefore remembers the last value it reported (restored across
    restarts) and clamps any dip within :data:`.const.TOTAL_WATER_CLAMP_TOLERANCE`
    back up to it, while still accepting a large drop as a genuine reset.
    """

    entity_description: AquaHomeSensorDescription

    def __init__(
        self,
        coordinator: AquaHomeCoordinator,
        description: AquaHomeSensorDescription,
    ) -> None:
        """Bind the sensor and initialise the last-reported-value memory."""
        super().__init__(coordinator, description)
        self._last_value: float | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the last reported value so the clamp survives a restart."""
        await super().async_added_to_hass()
        last_data = await self.async_get_last_sensor_data()
        if last_data is not None:
            self._last_value = _coerce_float(last_data.native_value)

    @property
    def native_value(self) -> StateType:
        """Return the counter value, clamping spurious small downward dips.

        ``None`` is reported honestly (never the cached value) when the source
        is absent. A dip that stays within the tolerance of the last value is
        treated as a cloud glitch and the last value is held; a larger drop is
        accepted as a real counter reset.
        """
        new = self.entity_description.value_fn(self.coordinator.data)
        if not isinstance(new, (int, float)) or isinstance(new, bool):
            return None
        new_value = float(new)
        last = self._last_value
        if last is not None and new_value < last:
            if new_value >= last * (1 - TOTAL_WATER_CLAMP_TOLERANCE):
                _LOGGER.debug(
                    "Clamping total-water dip %s -> %s (within tolerance)",
                    new_value,
                    last,
                )
                return last
            _LOGGER.debug(
                "Accepting total-water counter reset %s -> %s", last, new_value
            )
        self._last_value = new_value
        return new_value


def _coerce_float(value: object) -> float | None:
    """Tolerantly coerce a restored native value to ``float``, else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None
