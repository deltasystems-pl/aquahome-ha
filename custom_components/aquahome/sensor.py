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

The two analytics sensors are a third family: they read no cloud payload at all
but the :class:`~.analytics.engine.AquaHomeAnalyticsEngine`'s verdict over the
imported long-term statistics, so they stay meaningful while the softener is
offline. ``usage_forecast`` publishes tomorrow's expected use in native gallons
(``VOLUME_STORAGE`` again, for the measurement state class), while ``night_flow``
publishes the freshest classified night's minimum hourly flow in litres per hour
— native metric because the research thresholds behind the classification are
metric, with the display unit left to the user. Both render ``None`` until the
engine's first pass completes.

The live-mode status sensor is a fourth family, on that device's
:class:`~.live.AquaHomeLiveManager`. It reports whether a websocket session is
currently open and carries the session bookkeeping — budget spent, renewals,
failure trail — as attributes, so the cost of live mode is inspectable without
turning on debug logging. Like the manager's switches it is always available:
its state is local, and a reconnect backoff is precisely what the user wants to
see while the cloud is unreachable. Streamed values themselves never create
entities; they are merged into the polled device view, so the sensors above
simply go live while a session runs.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final

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
    UnitOfMass,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolume,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import salt
from .analytics.model import NightVerdict
from .api import (
    Device,
    LeakDetector,
    LeakDetectorStatus,
    PropertyValue,
    RechargeUi,
    RegenerationInfo,
    WaterTreatment,
    WaterTreatmentStatus,
    scaled_value,
)
from .const import (
    CAPABILITY_DEBOUNCE_POLLS,
    LIVE_STATUS_BACKOFF,
    LIVE_STATUS_IDLE,
    LIVE_STATUS_LIVE,
    MAX_STATE_LENGTH,
    REGENERATION_STATUS_OPTIONS,
    TOTAL_WATER_CLAMP_TOLERANCE,
    WEEKDAY_SLOTS,
)
from .dynamic import async_setup_dynamic_entities
from .entity import (
    AquaHomeActivityEntity,
    AquaHomeAnalyticsEntity,
    AquaHomeEntity,
    AquaHomeLeakDetectorEntity,
    build_device_info,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Set as AbstractSet
    from typing import Any

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
    from homeassistant.helpers.typing import StateType

    from .analytics.model import AnalyticsResult, NightAssessment
    from .api import DeviceSettingsDocument
    from .coordinator import (
        AquaHomeConfigEntry,
        AquaHomeCoordinator,
        AquaHomeSettingsCoordinator,
        DeviceActivity,
    )
    from .live import AquaHomeLiveManager

#: Enriched ``recharge_ui.state`` / ``regeneration_status`` value meaning a live
#: recharge cycle is in progress.
_REGENERATING = "regenerating"
#: ``recharge_ui.state`` value meaning a regeneration is scheduled but not running.
_SCHEDULED = "scheduled"

_LOGGER = logging.getLogger(__name__)

# Read-only coordinator platform: entity updates never do their own I/O, so
# Home Assistant may run them unbounded (quality-scale parallel-updates rule).
PARALLEL_UPDATES = 0

#: Description key of the RestoreSensor with the clamp guard; setup dispatches on
#: it because that one sensor needs a dedicated entity class, not the generic one.
_TOTAL_WATER_KEY = "total_water"


@dataclass(frozen=True, kw_only=True)
class AquaHomeSensorDescription(SensorEntityDescription):
    """Describe one AquaHome sensor and how to read its value.

    ``value_fn`` maps a coordinator :class:`~.api.models.Device` to the sensor's
    native value (or ``None`` when the source is absent). ``exists_fn`` gates
    whether the entity is created at all for a given device — evaluated once at
    setup against the first refreshed payload. ``attributes_fn`` — when set —
    supplies the entity's extra state attributes from the same device view (used
    e.g. to surface each per-weekday slot's stale ``reported`` timestamp).
    ``suggested_unit_fn`` — when set — derives a display unit from the first
    payload (evaluated once at entity creation): Home Assistant's unit system
    never converts ``weight`` sensors, so without it a metric account would be
    shown pounds forever.
    """

    value_fn: Callable[[Device], StateType | datetime]
    exists_fn: Callable[[Device], bool] = lambda device: True
    attributes_fn: Callable[[Device], dict[str, Any]] | None = None
    suggested_unit_fn: Callable[[Device], str | None] | None = None


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


def _recharge_ui(device: Device) -> RechargeUi | None:
    """Return the enriched ``recharge_ui`` state block, or ``None``."""
    enriched = _enriched(device)
    return enriched.recharge_ui if enriched is not None else None


def _regeneration_info(device: Device) -> RegenerationInfo | None:
    """Return the enriched ``regeneration`` block, or ``None``."""
    enriched = _enriched(device)
    return enriched.regeneration if enriched is not None else None


def _status(device: Device) -> WaterTreatmentStatus | None:
    """Return the enriched water-treatment status block, or ``None``."""
    enriched = _enriched(device)
    return enriched.water_treatment_status if enriched is not None else None


def _leak_detectors(device: Device) -> tuple[LeakDetector, ...]:
    """Return the device's paired leak detectors, or an empty tuple."""
    enriched = _enriched(device)
    if enriched is None or enriched.leak_detectors is None:
        return ()
    return enriched.leak_detectors.details


def _regen_active(device: Device) -> bool:
    """Return whether a regeneration cycle is currently running.

    ``True`` when the ``recharge_ui`` tile reads ``regenerating`` or either
    regeneration-status source (the ``regeneration`` block or the enriched
    top-level ``regeneration_status``) reports ``regenerating``. None-safe: an
    absent enriched block or sub-object simply means "not regenerating".
    """
    enriched = _enriched(device)
    if enriched is None:
        return False
    recharge_ui = enriched.recharge_ui
    if recharge_ui is not None and recharge_ui.state == _REGENERATING:
        return True
    regeneration = enriched.regeneration
    if regeneration is not None and regeneration.regeneration_status == _REGENERATING:
        return True
    return enriched.regeneration_status == _REGENERATING


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
    """Return today's water use in native gallons, raw property first.

    The raw ``gallons_used_today`` property tracks the device's own pushes,
    while the curated ``enriched_data`` copy is served from a server-side
    computation that lags it badly — observed live (2026-07-27) frozen at 0
    all morning while the raw property (and the vendor app) read 10 gal. The
    enriched field remains only as a fallback for payloads without properties.
    """
    raw = _prop_number(device, "gallons_used_today")
    if raw is not None:
        return raw
    enriched = _enriched(device)
    return enriched.gallons_used_today if enriched is not None else None


def _treated_water_available(device: Device) -> StateType:
    """Return remaining treated-water capacity in stable native gallons."""
    enriched = _enriched(device)
    if enriched is None or enriched.treated_water_available is None:
        return None
    return enriched.treated_water_available.base_value


def _total_water(device: Device) -> StateType:
    """Return the lifetime treated-water total in stable native gallons.

    Reads the raw ``total_outlet_water_gals`` counter first: the enriched
    ``total_water_used`` copy lags it by days of usage (observed live
    2026-07-27: enriched 47,637 gal vs raw 47,695 gal) because the curated
    block is recomputed server-side on its own schedule. The enriched
    fixed-gallons conversion remains as a fallback only.
    """
    raw = _prop_number(device, "total_outlet_water_gals")
    if raw is not None:
        return raw
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
    """Return the RF link strength to the valve head in dBm, raw property first.

    The raw ``rf_signal_strength_dbm`` property is the value the device itself
    last reported — and the one a live session streams, so it moves within
    seconds — while the curated ``enriched_data`` copy is recomputed server-side
    on its own schedule and can lag it by hours. The enriched field remains as a
    fallback for payloads served without the property map.
    """
    raw = _prop_number(device, "rf_signal_strength_dbm")
    if raw is not None:
        return raw
    enriched = _enriched(device)
    return enriched.rf_signal_strength_dbm if enriched is not None else None


def _device_local_midnight(device: Device, days_ahead: int) -> datetime:
    """Return device-local midnight ``days_ahead`` days from today.

    The timezone comes from the device's ``tz_id`` property; a missing or
    unrecognised zone falls back to UTC. Anchoring day-countdowns to a local
    midnight yields a stable point in time instead of a jittery relative count.
    """
    tz_value = _prop_str(device, "tz_id")
    tz = (dt_util.get_time_zone(tz_value) if tz_value else None) or dt_util.UTC
    target_date = dt_util.now(tz).date() + timedelta(days=days_ahead)
    return datetime.combine(target_date, time(), tzinfo=tz)


def _out_of_salt_estimate(device: Device) -> datetime | None:
    """Return the projected out-of-salt date as a device-local midnight timestamp.

    Combines the raw ``out_of_salt_estimate_days`` countdown with the device's
    ``tz_id`` so the result is the start of the day the softener is expected to
    run out of salt.
    """
    days = _prop_number(device, "out_of_salt_estimate_days")
    if days is None:
        return None
    return _device_local_midnight(device, int(days))


def _average_daily_water_use(device: Device) -> StateType:
    """Return the rolling average daily water use in native gallons."""
    return _prop_number(device, "avg_daily_use_gals")


def _current_water_flow(device: Device) -> StateType:
    """Return the instantaneous flow through the valve in gallons per minute.

    The raw ``current_water_flow_gpm`` property is reported in tenths of a
    gallon per minute and descaled by :func:`~.api.models.scaled_value`.

    The device publishes this property **on change only** — a single frame when
    flow starts, another when it stops — so it is genuinely instantaneous only
    while a live session is open. Between sessions a poll returns whatever the
    device last published, which after a burst of use is a stale non-zero rate
    until the closing zero arrives. That is a device characteristic, not a
    fault: this sensor is a live-view instrument, and the volume counters remain
    the trustworthy source for how much water actually flowed.
    """
    return _prop_number(device, "current_water_flow_gpm")


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


def _regeneration_status(device: Device) -> StateType:
    """Return the regeneration status, constrained to the known enum options.

    Prefers the enriched ``regeneration`` block's status, falling back to the
    top-level ``regeneration_status`` field. A value the enum does not list
    collapses to ``None`` so the ENUM sensor never carries an unlisted state.
    """
    regeneration = _regeneration_info(device)
    status = regeneration.regeneration_status if regeneration is not None else None
    if status is None:
        enriched = _enriched(device)
        status = enriched.regeneration_status if enriched is not None else None
    if status not in REGENERATION_STATUS_OPTIONS:
        return None
    return status


def _regeneration_time_remaining(device: Device) -> StateType:
    """Return seconds remaining in the running regeneration, else zero.

    The device's own ``regen_time_rem_secs`` countdown is read first — it ticks
    with the valve head and is streamed live during a session — with the
    enriched ``recharge_ui`` copy as the fallback for payloads served without
    the property map.

    Whichever source supplies it, the countdown is trusted only while a
    regeneration is actually active; any other time it is forced to zero rather
    than surfacing a stale value the cloud left behind (the tile keeps its last
    countdown after the cycle ends). ``None`` is reported only when neither
    source is present.
    """
    remaining = _prop_number(device, "regen_time_rem_secs")
    if remaining is None:
        recharge_ui = _recharge_ui(device)
        if recharge_ui is None:
            return None
        remaining = recharge_ui.time_remaining_seconds
    if not _regen_active(device):
        return 0
    return remaining or 0


def _next_regeneration(device: Device) -> datetime | None:
    """Return the next scheduled regeneration as a device-local timestamp.

    Only meaningful while the ``recharge_ui`` tile reads ``scheduled``: the next
    run is device-local midnight plus the ``regen_time_secs`` offset, in the
    timezone named by the ``tz_id`` property (UTC when it is absent or
    unrecognised). A candidate that has already passed today rolls to tomorrow.
    Any other tile state yields ``None``.
    """
    recharge_ui = _recharge_ui(device)
    if recharge_ui is None or recharge_ui.state != _SCHEDULED:
        return None
    regen_secs = _prop_number(device, "regen_time_secs")
    if regen_secs is None:
        return None
    tz_value = _prop_str(device, "tz_id")
    tz = (dt_util.get_time_zone(tz_value) if tz_value else None) or dt_util.UTC
    now = dt_util.now(tz)
    candidate = datetime.combine(now.date(), time(), tzinfo=tz) + timedelta(
        seconds=int(regen_secs)
    )
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _capacity_remaining(device: Device) -> StateType:
    """Return the remaining softening capacity as a percentage (÷10 scaled)."""
    return _prop_number(device, "capacity_remaining_percent")


def _hardness_setting(device: Device) -> StateType:
    """Return the configured water hardness in grains per gallon."""
    return _prop_number(device, "hardness_grains")


def _total_salt_used(device: Device) -> StateType:
    """Return the lifetime salt consumption in pounds."""
    return _prop_number(device, "total_salt_use_lbs")


def _total_rock_removed(device: Device) -> StateType:
    """Return the lifetime hardness (rock) removed in pounds.

    NOT monotonic despite the "total" name: the device derives it as
    ``total_salt_use x salt_efficiency / 7000`` (identity verified exactly on
    two live snapshots, 2026-07-27) and the efficiency figure itself moves
    both ways, so the value dips whenever recent efficiency drops — observed
    live 175.4 -> 170.1 lb. The description therefore declares ``TOTAL``, not
    ``TOTAL_INCREASING``, or every dip would register as a phantom meter
    reset in long-term statistics.
    """
    return _prop_number(device, "total_rock_removed_lbs")


def _error_codes(device: Device) -> StateType:
    """Return the active error codes joined into one string, or ``None``.

    An absent status block, an absent ``error_codes`` field, or a present-but-
    empty list all yield ``None`` (no active error). The joined string is
    truncated to Home Assistant's maximum state length.
    """
    status = _status(device)
    if status is None or not status.error_codes:
        return None
    return ", ".join(status.error_codes)[:MAX_STATE_LENGTH]


def _error_codes_attributes(device: Device) -> dict[str, Any]:
    """Return the active error codes as a list attribute, or an empty mapping."""
    status = _status(device)
    if status is None or not status.error_codes:
        return {}
    return {"codes": list(status.error_codes)}


def _daily_use_value(prop_name: str) -> Callable[[Device], StateType | datetime]:
    """Build a value function reading one per-weekday average-use slot."""

    def _value(device: Device) -> StateType | datetime:
        """Return the slot's average daily use in native gallons."""
        return _prop_number(device, prop_name)

    return _value


def _daily_use_exists(prop_name: str) -> Callable[[Device], bool]:
    """Build an exists function true only when the slot property is present."""

    def _exists(device: Device) -> bool:
        """Return whether the average-use slot property exists for this device."""
        return _property(device, prop_name) is not None

    return _exists


def _daily_use_attributes(prop_name: str) -> Callable[[Device], dict[str, Any]]:
    """Build an attributes function exposing the slot's ``reported`` timestamp.

    Each weekday slot is refreshed only on that weekday, so a slot can be a week
    (day_7 on the dev device, a month) stale. Surfacing the raw property's
    ``updated_at`` lets the user judge how current the value actually is.
    """

    def _attributes(device: Device) -> dict[str, Any]:
        """Return the slot's last-reported timestamp, when available."""
        prop = _property(device, prop_name)
        if prop is None or prop.updated_at is None:
            return {}
        return {"reported": prop.updated_at.isoformat()}

    return _attributes


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


def _exists_regeneration_status(device: Device) -> bool:
    """Report whether any regeneration-status source is present."""
    enriched = _enriched(device)
    return enriched is not None and (
        enriched.regeneration is not None or enriched.regeneration_status is not None
    )


def _exists_recharge_ui(device: Device) -> bool:
    """Report whether the enriched ``recharge_ui`` block is present."""
    return _recharge_ui(device) is not None


def _exists_next_regeneration(device: Device) -> bool:
    """Report whether both the ``recharge_ui`` block and schedule offset exist."""
    return (
        _recharge_ui(device) is not None
        and _property(device, "regen_time_secs") is not None
    )


def _exists_property(name: str) -> Callable[[Device], bool]:
    """Build an exists function true only when the named raw property is present."""

    def _exists(device: Device) -> bool:
        """Return whether the named raw property exists for this device."""
        return _property(device, name) is not None

    return _exists


#: ``converted_value / value`` band identifying a pounds-to-kilograms account
#: conversion (the exact factor is 0.45359237).
_LB_TO_KG_RATIO_BAND = (0.43, 0.48)


def _weight_display_unit(name: str) -> Callable[[Device], str | None]:
    """Build a suggested-unit function mirroring the account's weight display.

    Home Assistant's unit system converts volumes but never weights, so a
    weight sensor would otherwise show its native pounds to every user. The
    kilogram preference is detected NUMERICALLY — a ``converted_value/value``
    ratio of ~0.4536 is the lb→kg factor — never by the ``converted_units``
    label: the server localizes that string per ``accept-language``
    ("kilograms" arrives as "kilogramy" on a Polish account, live-confirmed
    2026-07-27, community PAIN #5), so any name allow-list silently fails for
    non-English locales. The ratio is also immune to the server's missing ÷10
    on the lifetime totals, which scales both sides equally.
    """

    def _suggested_unit(device: Device) -> str | None:
        """Return kilograms when the account converts this weight to kg."""
        prop = _property(device, name)
        if prop is None or prop.converted_value is None:
            return None
        value = prop.value
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not value:
            return None
        ratio = prop.converted_value / value
        if _LB_TO_KG_RATIO_BAND[0] <= ratio <= _LB_TO_KG_RATIO_BAND[1]:
            return UnitOfMass.KILOGRAMS
        return None

    return _suggested_unit


def _exists_error_codes(device: Device) -> bool:
    """Report whether the status block carries an ``error_codes`` field.

    Present-but-empty (``()``) still creates the entity; only an absent field
    (``None`` — as on the dev device, which omits it) suppresses it.
    """
    status = _status(device)
    return status is not None and status.error_codes is not None


# ---------------------------------------------------------------------------
# Salt intelligence (Phase 6)
#
# Chemistry lives in the pure .salt module; this section only resolves the
# inputs from the coordinator payloads. Inlet hardness prefers the settings
# document's precise ``inlet_hardness`` (gpg-denominated regardless of the
# account's display unit; PATCH-echo reconciled, so an app-side change lands
# within one settings cycle), falling back to the integer ``hardness_grains``
# raw property. Outlet hardness is structurally 0 — no known iQua model
# exposes a blend setting (validated to ~8-9 % error; owner decision
# 2026-07-27: no override entity at launch). The device's own
# ``out_of_salt_estimate_days`` stays PRIMARY everywhere; the chemistry
# estimate is a cross-check only.
# ---------------------------------------------------------------------------

#: Structural outlet hardness (°dH) — see the section comment above.
OUTLET_HARDNESS_DH: Final = 0.0

#: ``inlet_hardness_source`` attribute values.
_HARDNESS_SOURCE_SETTING = "device_setting"
_HARDNESS_SOURCE_PROPERTY = "device_property"

#: ``salt_type_enum`` / settings ``salt_type`` value -> regenerant name.
_SALT_TYPE_NAMES = {0: "NaCl", 1: "KCl"}

#: ``salt.efficiency_mol_per_kg_with_source`` source -> attribute value.
_EFFICIENCY_SOURCE_NAMES = {
    salt.EFFICIENCY_SOURCE_RATED: "device_rated_property",
    salt.EFFICIENCY_SOURCE_TOTALS: "lifetime_totals",
}


def _setting_float(document: DeviceSettingsDocument | None, name: str) -> float | None:
    """Return a settings-document value coerced to ``float``, else ``None``.

    ``current_value`` arrives as a string for select settings (``"25.7"``); a
    missing document, absent setting, boolean, non-numeric string, or
    non-finite number (``json.loads`` accepts bare ``NaN``/``Infinity``
    literals, and ``int()`` on them raises) all collapse to ``None`` so
    callers fall through to their raw-property source.
    """
    if document is None:
        return None
    setting = document.get(name)
    if setting is None:
        return None
    value = setting.current_value
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def _inlet_hardness_dh(
    device: Device, document: DeviceSettingsDocument | None
) -> tuple[float, str] | None:
    """Return the inlet hardness in °dH and which source supplied it.

    Both sources are gpg-denominated; non-positive, non-finite, or missing
    values fall through, so a device that reports neither yields ``None`` —
    and a malformed ``Infinity`` payload can never reach a state attribute.
    """
    gpg = _setting_float(document, "inlet_hardness")
    if gpg is not None and gpg > 0:
        return gpg * salt.GPG_TO_DH, _HARDNESS_SOURCE_SETTING
    gpg = _prop_number(device, "hardness_grains")
    if gpg is not None and math.isfinite(gpg) and gpg > 0:
        return gpg * salt.GPG_TO_DH, _HARDNESS_SOURCE_PROPERTY
    return None


def _salt_type(device: Device, document: DeviceSettingsDocument | None) -> str | None:
    """Return the regenerant in use (``NaCl``/``KCl``), or ``None`` when unknown.

    Informational only: the self-calibrated efficiency is already denominated
    in actual salt mass, so the salt type never enters the math. The raw
    ``salt_type_enum`` property (fast-poll fresh) wins over the settings
    document's ``salt_type``; unknown enum values yield ``None``.
    """
    enum_value = _prop_number(device, "salt_type_enum")
    if enum_value is not None and math.isfinite(enum_value):
        name = _SALT_TYPE_NAMES.get(int(enum_value))
        if name is not None:
            return name
    setting_value = _setting_float(document, "salt_type")
    if setting_value is not None:
        return _SALT_TYPE_NAMES.get(int(setting_value))
    return None


def _efficiency_with_source(device: Device) -> tuple[float, str] | None:
    """Return the device's operational salt efficiency (mol/kg) and its source."""
    return salt.efficiency_mol_per_kg_with_source(
        _prop_number(device, "salt_effic_grains_per_lb"),
        _prop_number(device, "total_rock_removed_lbs"),
        _prop_number(device, "total_salt_use_lbs"),
    )


def _chemistry_daily_salt(
    device: Device, document: DeviceSettingsDocument | None
) -> float | None:
    """Return the chemistry-estimated daily salt consumption in grams."""
    hardness = _inlet_hardness_dh(device, document)
    efficiency = _efficiency_with_source(device)
    average_daily_gal = _prop_number(device, "avg_daily_use_gals")
    return salt.daily_salt_grams(
        average_daily_gal * salt.LITERS_PER_GALLON
        if average_daily_gal is not None
        else None,
        hardness[0] if hardness is not None else None,
        OUTLET_HARDNESS_DH,
        efficiency[0] if efficiency is not None else None,
    )


def _device_daily_salt(device: Device) -> float | None:
    """Return the device-observed daily salt rate in grams."""
    return salt.device_daily_salt_grams(
        _prop_number(device, "avg_salt_per_regen_lbs"),
        _prop_number(device, "avg_days_between_regens"),
    )


def _chemistry_salt_days(
    device: Device, document: DeviceSettingsDocument | None
) -> float | None:
    """Return the chemistry-timed days-until-empty cross-check."""
    return salt.cross_check_days(
        _prop_number(device, "out_of_salt_estimate_days"),
        _device_daily_salt(device),
        _chemistry_daily_salt(device, document),
    )


def _daily_salt_usage(
    device: Device, document: DeviceSettingsDocument | None
) -> StateType | datetime:
    """Return the daily-salt-usage estimate sensor value."""
    return _chemistry_daily_salt(device, document)


def _daily_salt_attributes(
    device: Device, document: DeviceSettingsDocument | None
) -> dict[str, Any]:
    """Return the estimate's inputs so the number is auditable in the UI."""
    attributes: dict[str, Any] = {"outlet_hardness_dh": OUTLET_HARDNESS_DH}
    hardness = _inlet_hardness_dh(device, document)
    if hardness is not None:
        attributes["inlet_hardness_dh"] = round(hardness[0], 2)
        attributes["inlet_hardness_source"] = hardness[1]
    efficiency = _efficiency_with_source(device)
    if efficiency is not None:
        attributes["salt_efficiency_mol_per_kg"] = round(efficiency[0], 3)
    salt_type = _salt_type(device, document)
    if salt_type is not None:
        attributes["salt_type"] = salt_type
    return attributes


def _salt_days_remaining(
    device: Device, document: DeviceSettingsDocument | None
) -> StateType | datetime:
    """Return the cross-check days-until-empty sensor value."""
    return _chemistry_salt_days(device, document)


def _salt_days_attributes(
    device: Device, document: DeviceSettingsDocument | None
) -> dict[str, Any]:
    """Return the cross-check's inputs and its deviation from the device."""
    attributes: dict[str, Any] = {}
    device_days = _prop_number(device, "out_of_salt_estimate_days")
    if device_days is not None:
        attributes["device_estimate_days"] = device_days
    chemistry_rate = _chemistry_daily_salt(device, document)
    if chemistry_rate is not None:
        attributes["chemistry_daily_salt_g"] = round(chemistry_rate, 1)
    device_rate = _device_daily_salt(device)
    if device_rate is not None:
        attributes["device_daily_salt_g"] = round(device_rate, 1)
    chemistry_days = _chemistry_salt_days(device, document)
    if chemistry_days is not None and device_days is not None and device_days > 0:
        attributes["deviation_pct"] = round((chemistry_days / device_days - 1) * 100, 1)
    return attributes


def _salt_depletion_estimate(
    device: Device, document: DeviceSettingsDocument | None
) -> datetime | None:
    """Return the cross-check depletion date as a device-local midnight."""
    days = _chemistry_salt_days(device, document)
    if days is None:
        return None
    return _device_local_midnight(device, int(days))


def _salt_efficiency(device: Device) -> StateType:
    """Return the operational salt efficiency in mol/kg."""
    efficiency = _efficiency_with_source(device)
    return efficiency[0] if efficiency is not None else None


def _salt_efficiency_attributes(device: Device) -> dict[str, Any]:
    """Return the efficiency's gr/lb form and which counter supplied it."""
    efficiency = _efficiency_with_source(device)
    if efficiency is None:
        return {}
    value, source = efficiency
    return {
        "grains_per_pound": round(value / salt.MOL_PER_KG_PER_GRAIN_PER_LB),
        "source": _EFFICIENCY_SOURCE_NAMES.get(source, source),
    }


def _exists_efficiency_inputs(device: Device) -> bool:
    """Report whether either salt-efficiency source's properties are present."""
    return _property(device, "salt_effic_grains_per_lb") is not None or (
        _property(device, "total_rock_removed_lbs") is not None
        and _property(device, "total_salt_use_lbs") is not None
    )


def _exists_daily_salt(device: Device) -> bool:
    """Report whether the daily-salt estimate's device-side inputs are present.

    The settings-document hardness cannot be consulted here (existence gates
    see only the fast payload), so the gate requires the raw fallback
    ``hardness_grains`` — present on every observed softener payload.
    """
    return (
        _property(device, "avg_daily_use_gals") is not None
        and _property(device, "hardness_grains") is not None
        and _exists_efficiency_inputs(device)
    )


def _exists_salt_days(device: Device) -> bool:
    """Report whether the days-until-empty cross-check inputs are present."""
    return (
        _exists_daily_salt(device)
        and _property(device, "out_of_salt_estimate_days") is not None
        and _property(device, "avg_salt_per_regen_lbs") is not None
        and _property(device, "avg_days_between_regens") is not None
    )


@dataclass(frozen=True, kw_only=True)
class AquaHomeSaltSensorDescription(SensorEntityDescription):
    """Describe a salt sensor that also reads the settings document.

    Unlike :class:`AquaHomeSensorDescription`, ``value_fn``/``attributes_fn``
    receive the paired settings coordinator's current document (``None`` until
    it first loads) alongside the fast device view, so the inlet-hardness
    setting can feed the chemistry live. ``exists_fn`` still sees only the
    device: entity creation must not depend on the tolerant settings fetch.
    """

    value_fn: Callable[[Device, DeviceSettingsDocument | None], StateType | datetime]
    exists_fn: Callable[[Device], bool] = lambda device: True
    attributes_fn: (
        Callable[[Device, DeviceSettingsDocument | None], dict[str, Any]] | None
    ) = None


SALT_SENSOR_DESCRIPTIONS: tuple[AquaHomeSaltSensorDescription, ...] = (
    AquaHomeSaltSensorDescription(
        key="daily_salt_usage",
        translation_key="daily_salt_usage",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="g/d",
        suggested_display_precision=0,
        value_fn=_daily_salt_usage,
        exists_fn=_exists_daily_salt,
        attributes_fn=_daily_salt_attributes,
    ),
    AquaHomeSaltSensorDescription(
        key="salt_days_remaining",
        translation_key="salt_days_remaining",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.DAYS,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_salt_days_remaining,
        exists_fn=_exists_salt_days,
        attributes_fn=_salt_days_attributes,
    ),
    AquaHomeSaltSensorDescription(
        key="salt_depletion_estimate",
        translation_key="salt_depletion_estimate",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_salt_depletion_estimate,
        exists_fn=_exists_salt_days,
    ),
)


#: Per-weekday average-use sensors. Keys and translation keys are SLOT-based
#: (``average_daily_use_day_N``) so an entity's identity never depends on the
#: weekday mapping — the day labels live in ``strings.json`` (map A) and a future
#: correction to that mapping renames only display strings, never unique IDs.
_DAILY_USE_DESCRIPTIONS: tuple[AquaHomeSensorDescription, ...] = tuple(
    AquaHomeSensorDescription(
        key=f"average_daily_use_day_{slot}",
        translation_key=f"average_daily_use_day_{slot}",
        device_class=SensorDeviceClass.VOLUME_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfVolume.GALLONS,
        value_fn=_daily_use_value(f"avg_daily_use_day_{slot}_gals"),
        exists_fn=_daily_use_exists(f"avg_daily_use_day_{slot}_gals"),
        attributes_fn=_daily_use_attributes(f"avg_daily_use_day_{slot}_gals"),
    )
    for slot in range(1, len(WEEKDAY_SLOTS) + 1)
)


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
        key="current_water_flow",
        translation_key="current_water_flow",
        device_class=SensorDeviceClass.VOLUME_FLOW_RATE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfVolumeFlowRate.GALLONS_PER_MINUTE,
        # The device meters in gallons per minute; metric users get litres per
        # minute without the sensor ever misrepresenting its native unit.
        suggested_unit_of_measurement=UnitOfVolumeFlowRate.LITERS_PER_MINUTE,
        # A tenth of a gallon per minute is the property's own resolution.
        suggested_display_precision=1,
        value_fn=_current_water_flow,
        exists_fn=_exists_property("current_water_flow_gpm"),
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
    AquaHomeSensorDescription(
        key="regeneration_status",
        translation_key="regeneration_status",
        device_class=SensorDeviceClass.ENUM,
        options=list(REGENERATION_STATUS_OPTIONS),
        value_fn=_regeneration_status,
        exists_fn=_exists_regeneration_status,
    ),
    AquaHomeSensorDescription(
        key="regeneration_time_remaining",
        translation_key="regeneration_time_remaining",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        value_fn=_regeneration_time_remaining,
        exists_fn=_exists_recharge_ui,
    ),
    AquaHomeSensorDescription(
        key="next_regeneration",
        translation_key="next_regeneration",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_next_regeneration,
        exists_fn=_exists_next_regeneration,
    ),
    *_DAILY_USE_DESCRIPTIONS,
    AquaHomeSensorDescription(
        key="capacity_remaining",
        translation_key="capacity_remaining",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=_capacity_remaining,
        exists_fn=_exists_property("capacity_remaining_percent"),
    ),
    AquaHomeSensorDescription(
        key="hardness_setting",
        translation_key="hardness_setting",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="gpg",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_hardness_setting,
        exists_fn=_exists_property("hardness_grains"),
    ),
    AquaHomeSensorDescription(
        key="total_salt_used",
        translation_key="total_salt_used",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfMass.POUNDS,
        value_fn=_total_salt_used,
        exists_fn=_exists_property("total_salt_use_lbs"),
        suggested_unit_fn=_weight_display_unit("total_salt_use_lbs"),
    ),
    AquaHomeSensorDescription(
        key="total_rock_removed",
        translation_key="total_rock_removed",
        device_class=SensorDeviceClass.WEIGHT,
        # TOTAL, not TOTAL_INCREASING: the counter dips when the device's
        # efficiency figure drops — see _total_rock_removed.
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfMass.POUNDS,
        value_fn=_total_rock_removed,
        exists_fn=_exists_property("total_rock_removed_lbs"),
        suggested_unit_fn=_weight_display_unit("total_rock_removed_lbs"),
    ),
    AquaHomeSensorDescription(
        key="salt_per_regeneration",
        translation_key="salt_per_regeneration",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfMass.POUNDS,
        suggested_display_precision=2,
        value_fn=lambda device: _prop_number(device, "avg_salt_per_regen_lbs"),
        exists_fn=_exists_property("avg_salt_per_regen_lbs"),
        suggested_unit_fn=_weight_display_unit("avg_salt_per_regen_lbs"),
    ),
    AquaHomeSensorDescription(
        key="salt_efficiency",
        translation_key="salt_efficiency",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="mol/kg",
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_salt_efficiency,
        exists_fn=_exists_efficiency_inputs,
        attributes_fn=_salt_efficiency_attributes,
    ),
    AquaHomeSensorDescription(
        key="error_codes",
        translation_key="error_codes",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_error_codes,
        exists_fn=_exists_error_codes,
        attributes_fn=_error_codes_attributes,
    ),
)


# ---------------------------------------------------------------------------
# Per-leak-detector sensors (one sub-device per detector)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class AquaHomeLeakSensorDescription(SensorEntityDescription):
    """Describe one per-leak-detector sensor and how to read its value.

    ``value_fn`` maps a detector's :class:`~.api.models.LeakDetectorStatus` to the
    sensor's native value (``None`` when the backing field is absent).
    """

    value_fn: Callable[[LeakDetectorStatus], StateType]


def _leak_temperature(status: LeakDetectorStatus) -> StateType:
    """Return the detector's raw (native-unit) temperature reading."""
    temperature = status.temperature
    return temperature.raw_value if temperature is not None else None


def _leak_signal_strength(status: LeakDetectorStatus) -> StateType:
    """Return the detector's RF signal strength in dBm."""
    return status.signal_strength


#: Per-leak-detector sensors. The temperature binds the detector's ``raw_value``
#: — its native unit — which the API convention reports in US customary, so the
#: entity is labelled Fahrenheit; this is live-unverified (no leak hardware in the
#: dev cohort). Signal strength mirrors the softener RF sensor: diagnostic and
#: registry-disabled by default, in dBm.
LEAK_SENSOR_DESCRIPTIONS: tuple[AquaHomeLeakSensorDescription, ...] = (
    AquaHomeLeakSensorDescription(
        key="leak_temperature",
        translation_key="leak_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        value_fn=_leak_temperature,
    ),
    AquaHomeLeakSensorDescription(
        key="leak_signal_strength",
        translation_key="leak_signal_strength",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_leak_signal_strength,
    ),
)


# ---------------------------------------------------------------------------
# Activity-coordinator-backed sensors (alert + regeneration history)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class AquaHomeActivitySensorDescription(SensorEntityDescription):
    """Describe a sensor read from a device's activity coordinator.

    ``value_fn`` maps the parsed :class:`~.coordinator.DeviceActivity` to the
    sensor's native value; ``attributes_fn`` optionally supplies extra state
    attributes from the same feed. ``exists_fn`` gates creation against the
    paired fast coordinator's device view (e.g. a feature gate).
    """

    value_fn: Callable[[DeviceActivity], StateType | datetime]
    attributes_fn: Callable[[DeviceActivity], dict[str, Any]] | None = None
    exists_fn: Callable[[Device], bool] = lambda device: True


def _last_regeneration(activity: DeviceActivity) -> datetime | None:
    """Return the start time of the most recent regeneration event."""
    events = activity.regeneration_events
    return events[0].start_time if events else None


def _latest_alert(activity: DeviceActivity) -> StateType:
    """Return the newest alert's message, truncated to the max state length."""
    alerts = activity.alerts
    if not alerts or alerts[0].message is None:
        return None
    return alerts[0].message[:MAX_STATE_LENGTH]


def _latest_alert_attributes(activity: DeviceActivity) -> dict[str, Any]:
    """Return the newest alert's metadata as extra state attributes."""
    alerts = activity.alerts
    if not alerts:
        return {}
    alert = alerts[0]
    return {
        "title": alert.title,
        "level": alert.level,
        "alert_type": alert.type,
        "alert_id": alert.id,
        "timestamp": alert.timestamp.isoformat()
        if alert.timestamp is not None
        else None,
        "is_read": alert.is_read,
    }


def _exists_last_regeneration(device: Device) -> bool:
    """Report whether the device advertises the regeneration feature (None-safe)."""
    enriched = _enriched(device)
    return enriched is not None and "regeneration" in enriched.features


ACTIVITY_SENSOR_DESCRIPTIONS: tuple[AquaHomeActivitySensorDescription, ...] = (
    AquaHomeActivitySensorDescription(
        key="last_regeneration",
        translation_key="last_regeneration",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_last_regeneration,
        exists_fn=_exists_last_regeneration,
    ),
    AquaHomeActivitySensorDescription(
        key="latest_alert",
        translation_key="latest_alert",
        value_fn=_latest_alert,
        attributes_fn=_latest_alert_attributes,
    ),
)


# ---------------------------------------------------------------------------
# Analytics-engine-backed sensors (Phase 7)
#
# The engine publishes one immutable :class:`~.analytics.model.AnalyticsResult`
# per pass; these sensors only project fields out of it. They never compute — a
# sensor that re-derived anything here would drift from the binary sensors and
# the fired events, which read the very same result.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class AquaHomeAnalyticsSensorDescription(SensorEntityDescription):
    """Describe a sensor read from a device's analytics engine.

    ``value_fn`` maps one completed :class:`~.analytics.model.AnalyticsResult` to
    the sensor's native value; ``attributes_fn`` — when set — supplies extra
    state attributes from the same result. Neither is called before the engine's
    first pass: the entity short-circuits to ``None`` while the result is absent,
    so both may assume a fully built result.
    """

    value_fn: Callable[[AnalyticsResult], StateType]
    attributes_fn: Callable[[AnalyticsResult], dict[str, Any]] | None = None


#: Night verdicts that carry a flow number. The other three (UNKNOWN, MASKED,
#: UNASSESSED) are explicit non-answers and must never be rendered as 0 L/h.
_DETERMINATE_NIGHT_VERDICTS: Final = frozenset(
    {NightVerdict.LEAK, NightVerdict.NO_LEAK}
)


def _usage_forecast(result: AnalyticsResult) -> StateType:
    """Return tomorrow's forecast use in native gallons, or ``None``."""
    return result.forecast.gallons


def _usage_forecast_attributes(result: AnalyticsResult) -> dict[str, Any]:
    """Return the forecast's metric form, provenance, band, and occupancy.

    ``source`` names which link of the expectation chain produced the number, so
    a forecast resting on a fallback is never mistaken for a device-reported one.
    The litre figures are rounded to whole litres: the underlying statistics are
    hour-resolution meter reads, and sub-litre precision would be false rigour.
    """
    forecast = result.forecast
    return {
        "liters": round(forecast.liters) if forecast.liters is not None else None,
        "source": forecast.source,
        "band_liters": round(forecast.band_liters)
        if forecast.band_liters is not None
        else None,
        "weekday": forecast.weekday,
        "persons": forecast.persons,
    }


def _latest_determinate_night(result: AnalyticsResult) -> NightAssessment | None:
    """Return the newest night that actually got a verdict, or ``None``.

    Selected by date rather than by position so the sensor is independent of the
    order the detectors happen to emit their assessments in.
    """
    determinate = [
        night for night in result.nights if night.verdict in _DETERMINATE_NIGHT_VERDICTS
    ]
    if not determinate:
        return None
    return max(determinate, key=lambda night: night.night)


def _night_flow(result: AnalyticsResult) -> StateType:
    """Return the freshest classified night's minimum hourly flow in L/h.

    A NO_LEAK night is a hard zero — the classifier only reaches that verdict on
    evidence of a genuinely dry hour — while a LEAK night reports the smallest
    certain hour of the window, which over one hour is already a rate. Nights
    the classifier could not judge (masked by a regeneration, unbounded by
    readings, or ambiguous) leave the sensor ``None``: an unassessed night is
    not a quiet one.
    """
    night = _latest_determinate_night(result)
    if night is None:
        return None
    if night.verdict == NightVerdict.NO_LEAK:
        return 0.0
    return night.min_hour_liters


def _night_flow_attributes(result: AnalyticsResult) -> dict[str, Any]:
    """Return which night the reading belongs to and how it was classified."""
    night = _latest_determinate_night(result)
    if night is None:
        return {"night": None, "verdict": None}
    return {"night": night.night.isoformat(), "verdict": str(night.verdict)}


#: Analytics sensors, created for every device (analytics always runs — there is
#: no capability to gate on) and registry-enabled. ``night_flow`` is diagnostic:
#: it is the evidence behind the leak binary rather than a headline number.
ANALYTICS_SENSOR_DESCRIPTIONS: tuple[AquaHomeAnalyticsSensorDescription, ...] = (
    AquaHomeAnalyticsSensorDescription(
        key="usage_forecast",
        translation_key="usage_forecast",
        device_class=SensorDeviceClass.VOLUME_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfVolume.GALLONS,
        suggested_display_precision=1,
        value_fn=_usage_forecast,
        attributes_fn=_usage_forecast_attributes,
    ),
    AquaHomeAnalyticsSensorDescription(
        key="night_flow",
        translation_key="night_flow",
        device_class=SensorDeviceClass.VOLUME_FLOW_RATE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfVolumeFlowRate.LITERS_PER_HOUR,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_night_flow,
        attributes_fn=_night_flow_attributes,
    ),
)


# ---------------------------------------------------------------------------
# Live-manager-backed status sensor
#
# One per device, on that device's live manager. It publishes the manager's own
# state — no cloud payload is involved — and deliberately does not churn: the
# status changes when a session is granted, ends, or fails, never on the window
# renewals inside a held session.
# ---------------------------------------------------------------------------


#: The three states a live manager can be in, and the sensor's ENUM options.
#: Anything else would violate the ENUM contract, so the value function maps an
#: unrecognised status to ``None`` (rendered as unknown) rather than publishing it.
LIVE_STATUS_OPTIONS: Final[tuple[str, ...]] = (
    LIVE_STATUS_IDLE,
    LIVE_STATUS_LIVE,
    LIVE_STATUS_BACKOFF,
)


#: The live-mode status sensor. Diagnostic: it describes how the integration is
#: gathering data, not the water treatment itself.
LIVE_STATUS_DESCRIPTION: Final = SensorEntityDescription(
    key="live_mode_status",
    translation_key="live_mode_status",
    device_class=SensorDeviceClass.ENUM,
    options=list(LIVE_STATUS_OPTIONS),
    entity_category=EntityCategory.DIAGNOSTIC,
    icon="mdi:access-point",
)


def _isoformat(value: datetime | None) -> str | None:
    """Return a timestamp attribute in ISO-8601, or ``None`` when it is unset."""
    return value.isoformat() if value is not None else None


@callback
def _async_add_dynamic_sensors(
    entry: AquaHomeConfigEntry,
    coordinator: AquaHomeCoordinator,
    settings_coordinator: AquaHomeSettingsCoordinator | None,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one device's fast-coordinator sensors, growing the set over time.

    The telemetry descriptions, the salt-intelligence descriptions, and any
    paired leak detector's sensors are keyed uniquely and handed to
    :func:`~.dynamic.async_setup_dynamic_entities`, which creates the keys
    present at setup and adds later ones once seen
    :data:`~.const.CAPABILITY_DEBOUNCE_POLLS` consecutive polls. The lifetime
    total-water counter keeps its dedicated :class:`AquaHomeTotalWaterSensor`
    class through the retrofit; the salt sensors additionally read (and follow)
    the paired settings coordinator's document.
    """

    def _discover() -> set[str]:
        """Return the fast-sensor keys present on the current device view."""
        device = coordinator.data
        keys = {
            description.key
            for description in SENSOR_DESCRIPTIONS
            if description.exists_fn(device)
        }
        keys.update(
            description.key
            for description in SALT_SENSOR_DESCRIPTIONS
            if description.exists_fn(device)
        )
        for detector in _leak_detectors(device):
            keys.update(
                f"leak_{detector.detector_id}_{description.key}"
                for description in LEAK_SENSOR_DESCRIPTIONS
            )
        return keys

    def _create(keys: AbstractSet[str]) -> list[Entity]:
        """Build the fast-sensor entities whose keys are in ``keys``."""
        device = coordinator.data
        entities: list[Entity] = []
        for description in SENSOR_DESCRIPTIONS:
            if description.key not in keys:
                continue
            if description.key == _TOTAL_WATER_KEY:
                entities.append(AquaHomeTotalWaterSensor(coordinator, description))
            else:
                entities.append(AquaHomeSensor(coordinator, description))
        entities.extend(
            AquaHomeSaltSensor(coordinator, salt_description, settings_coordinator)
            for salt_description in SALT_SENSOR_DESCRIPTIONS
            if salt_description.key in keys
        )
        for detector in _leak_detectors(device):
            entities.extend(
                AquaHomeLeakSensor(coordinator, description, detector.detector_id)
                for description in LEAK_SENSOR_DESCRIPTIONS
                if f"leak_{detector.detector_id}_{description.key}" in keys
            )
        return entities

    async_setup_dynamic_entities(
        entry,
        coordinator,
        async_add_entities,
        discover=_discover,
        create=_create,
        debounce_polls=CAPABILITY_DEBOUNCE_POLLS,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AquaHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create sensor entities for every coordinator whose source data exists.

    Two families are set up per device. The fast-coordinator telemetry sensors
    (from :data:`SENSOR_DESCRIPTIONS`) and any paired leak detector's sensors are
    driven by a dynamic adder so a capability that surfaces later — a leak
    detector paired after setup — materialises without a reload. The
    activity-coordinator history sensors (from
    :data:`ACTIVITY_SENSOR_DESCRIPTIONS`) are static: their existence is not
    capability-driven, so each activity coordinator is paired once with its fast
    coordinator's device view (same device-id key) for the existence gate and the
    shared ``DeviceInfo``.

    The analytics sensors (from :data:`ANALYTICS_SENSOR_DESCRIPTIONS`) are static
    and ungated: the engine runs for every device regardless of what the cloud
    advertises, so each engine gets both sensors, paired with its fast
    coordinator's device view for the shared ``DeviceInfo``.

    The live-mode status sensor is static and ungated for the same reason: a
    live manager exists for every device, so each one gets exactly one status
    sensor, again paired with the fast coordinator's device view.
    """
    runtime = entry.runtime_data
    for coordinator in runtime.coordinators.values():
        _async_add_dynamic_sensors(
            entry,
            coordinator,
            runtime.settings_coordinators.get(coordinator.device_id),
            async_add_entities,
        )
    activity_entities: list[SensorEntity] = []
    for device_id, activity in runtime.activity_coordinators.items():
        fast = runtime.coordinators.get(device_id)
        if fast is None:
            continue
        device = fast.data
        activity_entities.extend(
            AquaHomeActivitySensor(activity, description, device)
            for description in ACTIVITY_SENSOR_DESCRIPTIONS
            if description.exists_fn(device)
        )
    async_add_entities(activity_entities)
    analytics_entities: list[SensorEntity] = []
    for engine_device_id, engine in runtime.analytics_engines.items():
        engine_fast = runtime.coordinators.get(engine_device_id)
        if engine_fast is None:
            continue
        analytics_entities.extend(
            AquaHomeAnalyticsSensor(engine, description, engine_fast.data)
            for description in ANALYTICS_SENSOR_DESCRIPTIONS
        )
    async_add_entities(analytics_entities)
    live_entities: list[SensorEntity] = []
    for live_device_id, manager in runtime.live_managers.items():
        live_fast = runtime.coordinators.get(live_device_id)
        if live_fast is None:
            continue
        live_entities.append(
            AquaHomeLiveStatusSensor(manager, LIVE_STATUS_DESCRIPTION, live_fast.data)
        )
    async_add_entities(live_entities)


class AquaHomeSensor(AquaHomeEntity, SensorEntity):
    """A generic AquaHome sensor backed by a description's ``value_fn``."""

    entity_description: AquaHomeSensorDescription

    def __init__(
        self,
        coordinator: AquaHomeCoordinator,
        description: AquaHomeSensorDescription,
    ) -> None:
        """Bind the sensor, deriving a display unit when the description asks.

        ``suggested_unit_fn`` runs once against the first refreshed payload;
        the registry persists the suggestion from first registration onward.
        """
        super().__init__(coordinator, description)
        if description.suggested_unit_fn is not None:
            self._attr_suggested_unit_of_measurement = description.suggested_unit_fn(
                coordinator.data
            )

    @property
    def native_value(self) -> StateType | datetime:
        """Return the current value by applying the description's ``value_fn``."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the description's extra attributes, or ``None`` when unset."""
        attributes_fn = self.entity_description.attributes_fn
        if attributes_fn is None:
            return None
        return attributes_fn(self.coordinator.data)


class AquaHomeSaltSensor(AquaHomeEntity, SensorEntity):
    """A salt-intelligence sensor reading the device and the settings document.

    Bound to the fast coordinator like every telemetry sensor, but its value
    and attribute functions additionally receive the paired settings
    coordinator's current document so the precise ``inlet_hardness`` setting
    feeds the chemistry. A listener on the settings coordinator re-renders the
    sensor the moment that document changes (6-hour poll or PATCH-echo
    reconcile) without waiting for the next fast poll; a missing or not-yet-
    loaded document simply yields ``None`` and the value functions fall back to
    raw properties. Availability deliberately ignores settings-coordinator
    health — the fallback keeps the sensor meaningful without the document.
    """

    entity_description: AquaHomeSaltSensorDescription

    def __init__(
        self,
        coordinator: AquaHomeCoordinator,
        description: AquaHomeSaltSensorDescription,
        settings_coordinator: AquaHomeSettingsCoordinator | None,
    ) -> None:
        """Bind the sensor to the fast coordinator and its settings sibling."""
        super().__init__(coordinator, description)
        self._settings_coordinator = settings_coordinator

    async def async_added_to_hass(self) -> None:
        """Follow settings-document updates in addition to the fast poll."""
        await super().async_added_to_hass()
        if self._settings_coordinator is not None:
            self.async_on_remove(
                self._settings_coordinator.async_add_listener(
                    self._handle_settings_update
                )
            )

    @callback
    def _handle_settings_update(self) -> None:
        """Re-render when the settings document changes."""
        self.async_write_ha_state()

    @property
    def _settings_document(self) -> DeviceSettingsDocument | None:
        """Return the current settings document, or ``None`` before it loads."""
        if self._settings_coordinator is None:
            return None
        document: DeviceSettingsDocument | None = self._settings_coordinator.data
        return document

    @property
    def native_value(self) -> StateType | datetime:
        """Return the value from the device view and settings document."""
        return self.entity_description.value_fn(
            self.coordinator.data, self._settings_document
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the description's extra attributes, or ``None`` when unset."""
        attributes_fn = self.entity_description.attributes_fn
        if attributes_fn is None:
            return None
        return attributes_fn(self.coordinator.data, self._settings_document)


class AquaHomeLeakSensor(AquaHomeLeakDetectorEntity, SensorEntity):
    """A per-leak-detector sensor backed by its detector's status block."""

    entity_description: AquaHomeLeakSensorDescription

    @property
    def native_value(self) -> StateType:
        """Return the value, or ``None`` when the detector/field is absent."""
        detector = self.detector
        if detector is None or detector.status is None:
            return None
        return self.entity_description.value_fn(detector.status)


class AquaHomeActivitySensor(AquaHomeActivityEntity, SensorEntity):
    """A sensor backed by a device's activity coordinator (alerts / history)."""

    entity_description: AquaHomeActivitySensorDescription

    @property
    def native_value(self) -> StateType | datetime:
        """Return the value from the activity feed, or ``None`` when it is absent."""
        data: DeviceActivity | None = self.coordinator.data
        if data is None:
            return None
        return self.entity_description.value_fn(data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the feed-derived attributes, or ``None`` when unset/absent."""
        attributes_fn = self.entity_description.attributes_fn
        if attributes_fn is None:
            return None
        data: DeviceActivity | None = self.coordinator.data
        if data is None:
            return None
        return attributes_fn(data)


class AquaHomeAnalyticsSensor(AquaHomeAnalyticsEntity, SensorEntity):
    """A sensor projecting one field of the analytics engine's latest result.

    The engine has no result until its first pass finishes (it is refreshed in
    the background after the statistics backfill, not during setup), so every
    read is guarded: an absent result renders ``unknown`` rather than raising,
    and the description's functions only ever see a complete result.
    """

    entity_description: AquaHomeAnalyticsSensorDescription

    @property
    def native_value(self) -> StateType:
        """Return the value from the latest result, or ``None`` before the first."""
        result: AnalyticsResult | None = self.coordinator.data
        if result is None:
            return None
        return self.entity_description.value_fn(result)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the result-derived attributes, or ``None`` when unset/absent."""
        attributes_fn = self.entity_description.attributes_fn
        if attributes_fn is None:
            return None
        result: AnalyticsResult | None = self.coordinator.data
        if result is None:
            return None
        return attributes_fn(result)


class AquaHomeLiveStatusSensor(CoordinatorEntity["AquaHomeLiveManager"], SensorEntity):
    """The live-mode status of one device's websocket manager.

    Reports whether a live session is currently open (``live``), whether the
    manager is waiting out a reconnect backoff (``backoff``), or neither
    (``idle``), with the session bookkeeping as attributes: which trigger opened
    the current session, how much of the daily grant budget it has spent, how
    many reporting windows the current hold has renewed, and the failure trail.
    Together they make the cost of live mode inspectable without debug logging.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AquaHomeLiveManager,
        description: SensorEntityDescription,
        device: Device,
    ) -> None:
        """Bind the sensor to one device's live manager and description.

        ``device`` is the paired fast coordinator's device view, used only to
        build the shared :class:`~homeassistant.helpers.device_registry.DeviceInfo`
        so the sensor attaches to the same device as the telemetry entities.
        """
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.device_slug}_{description.key}"
        self._attr_device_info = build_device_info(device)

    @property
    def available(self) -> bool:
        """Return ``True`` always: the manager's state is local, never fetched.

        A backoff or an idle status is exactly what the user needs to see while
        the cloud is unreachable or the softener is offline, so this sensor
        deliberately has no availability gate.
        """
        return True

    @property
    def native_value(self) -> str | None:
        """Return the manager's status, constrained to the listed enum options.

        A status outside the options would break Home Assistant's ENUM contract,
        so anything unrecognised renders as unknown instead.
        """
        status = self.coordinator.state.status
        return status if status in LIVE_STATUS_OPTIONS else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the current session's bookkeeping and the failure trail."""
        state = self.coordinator.state
        return {
            "source": state.source,
            "sessions_today": state.sessions_today,
            "sessions_per_day": state.config.sessions_per_day,
            "windows_in_session": state.windows_in_session,
            "last_session_end": _isoformat(state.last_session_end),
            "consecutive_failures": state.consecutive_failures,
            "backoff_until": _isoformat(state.backoff_until),
            "last_error": state.last_error,
            "smart_suspended_today": state.smart_suspended_today,
        }


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
