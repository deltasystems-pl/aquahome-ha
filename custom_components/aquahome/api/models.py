"""Typed models for iQua cloud API payloads.

All parsers are tolerant: unknown keys are ignored, missing optional keys
become ``None`` — the live API returns fields the OpenAPI spec does not
declare, so strictness here would break on real payloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Self

# ---------------------------------------------------------------------------
# Tolerant coercion helpers
#
# Every ``from_dict`` builds on these so that a real payload can never raise:
# a wrong type or missing key collapses to ``None`` instead of an exception.
# ---------------------------------------------------------------------------


def _as_str(value: Any) -> str | None:
    """Return ``value`` when it is a string, else ``None``."""
    return value if isinstance(value, str) else None


def _as_bool(value: Any) -> bool | None:
    """Return ``value`` when it is a real boolean, else ``None``."""
    return value if isinstance(value, bool) else None


def _as_int(value: Any) -> int | None:
    """Coerce ``value`` to ``int`` (never from ``bool``), else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _as_float(value: Any) -> float | None:
    """Coerce ``value`` to ``float`` (never from ``bool``), else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _as_scalar(value: Any) -> bool | int | float | str | None:
    """Return a JSON scalar unchanged (bool/int/float/str), else ``None``."""
    if isinstance(value, (bool, int, float, str)):
        return value
    return None


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    """Return the string items of a list as a tuple (empty when not a list)."""
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _str_or_empty(value: Any) -> str:
    """Return ``value`` when it is a string, else the empty string."""
    return value if isinstance(value, str) else ""


def _parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp to an aware ``datetime`` (assume UTC)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


@dataclass(frozen=True, slots=True)
class LoginResult:
    """Result of POST /auth/login."""

    access_token: str
    refresh_token: str
    user_id: str
    is_verified: bool
    is_admin: bool = False
    is_customer_support: bool = False
    is_marketing: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse the AuthLoginOutputBody payload."""
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            user_id=data["user_id"],
            is_verified=bool(data.get("is_verified", False)),
            is_admin=bool(data.get("is_admin", False)),
            is_customer_support=bool(data.get("is_customer_support", False)),
            is_marketing=bool(data.get("is_marketing", False)),
        )


# ---------------------------------------------------------------------------
# Converted-property pattern (ConvertedProperty / PropertyConversion)
# ---------------------------------------------------------------------------

#: ``unit_of_measure.conversion`` value that marks the stable native unit
#: (US gallons / pounds). Named to avoid a bare magic literal in comparisons.
_STABLE_UNIT_CONVERSION = 1.0


@dataclass(frozen=True, slots=True)
class Conversion:
    """One entry of a ``ConvertedProperty.conversions`` list.

    ``unit_conversion`` is the nested ``unit_of_measure.conversion`` factor;
    the entry whose factor is ``1`` carries the stable native value.
    """

    unit: str | None = None
    value: float | None = None
    display_value: str | None = None
    conversion_factor: float | None = None
    unit_conversion: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a PropertyConversion payload."""
        unit_of_measure = data.get("unit_of_measure")
        unit_conversion = (
            _as_float(unit_of_measure.get("conversion"))
            if isinstance(unit_of_measure, dict)
            else None
        )
        return cls(
            unit=_as_str(data.get("unit")),
            value=_as_float(data.get("value")),
            display_value=_as_str(data.get("display_value")),
            conversion_factor=_as_float(data.get("conversion_factor")),
            unit_conversion=unit_conversion,
        )


@dataclass(frozen=True, slots=True)
class ConvertedProperty:
    """A value the server localizes into several units.

    The top-level ``value``/``units`` follow the *account's* unit preference
    and flip whenever the user changes it, so sensors MUST NOT bind to them.
    Read a fixed ``conversions`` entry instead — ``base_value`` for the stable
    native unit, or ``value_in`` for a specific unit.
    """

    value: float | None = None
    units: str | None = None
    conversion_factor: float | None = None
    conversions: tuple[Conversion, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a ConvertedProperty payload."""
        raw = data.get("conversions")
        conversions = (
            tuple(Conversion.from_dict(item) for item in raw if isinstance(item, dict))
            if isinstance(raw, list)
            else ()
        )
        return cls(
            value=_as_float(data.get("value")),
            units=_as_str(data.get("units")),
            conversion_factor=_as_float(data.get("conversion_factor")),
            conversions=conversions,
        )

    def value_in(self, unit: str) -> float | None:
        """Return the value for ``unit`` (case-insensitive), else ``None``."""
        target = unit.casefold()
        for conversion in self.conversions:
            if conversion.unit is not None and conversion.unit.casefold() == target:
                return conversion.value
        return None

    @property
    def base_value(self) -> float | None:
        """Return the value in the stable native unit (US gallons / pounds).

        This is the ``conversions`` entry whose ``unit_conversion`` is ``1`` —
        the value that does not follow the account's unit preference.
        """
        for conversion in self.conversions:
            if conversion.unit_conversion == _STABLE_UNIT_CONVERSION:
                return conversion.value
        return None


# ---------------------------------------------------------------------------
# Enriched water-treatment sub-objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SaltLevel:
    """Salt-level block of the enriched water-treatment data."""

    monitoring_enabled: bool
    salt_level_percent: float | None = None
    salt_level_percent_rounded: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a WaterTreatmentSaltLevel payload."""
        return cls(
            monitoring_enabled=bool(data.get("monitoring_enabled", False)),
            salt_level_percent=_as_float(data.get("salt_level_percent")),
            salt_level_percent_rounded=_as_float(
                data.get("salt_level_percent_rounded")
            ),
        )


@dataclass(frozen=True, slots=True)
class RegenerationInfo:
    """Regeneration block of the enriched water-treatment data."""

    regeneration_status: str | None = None
    can_schedule: bool | None = None
    can_recharge: bool | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a Regeneration payload."""
        return cls(
            regeneration_status=_as_str(data.get("regeneration_status")),
            can_schedule=_as_bool(data.get("can_schedule")),
            can_recharge=_as_bool(data.get("can_recharge")),
        )


@dataclass(frozen=True, slots=True)
class WaterTreatmentStatus:
    """Alert/status flags of the enriched water-treatment data."""

    alert_badge_count: int | None = None
    salt_level_alert: bool | None = None
    flow_monitor_alert: bool | None = None
    connection_alert: bool | None = None
    water_usage_alert: bool | None = None
    resin_alert: bool | None = None
    error_code_alert: bool | None = None
    service_reminder_message: str | None = None
    water_to_drain_monitor_enabled: bool | None = None
    alarm_is_beeping: bool | None = None
    water_to_drain_alert: bool | None = None
    error_codes: tuple[str, ...] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a WaterTreatmentStatus payload.

        ``error_codes`` distinguishes an absent key (``None`` — the device does
        not report the field, as on the dev unit) from a present-but-empty list
        (``()`` — the field exists and no error is active).
        """
        raw_error_codes = data.get("error_codes")
        error_codes = (
            _as_str_tuple(raw_error_codes)
            if isinstance(raw_error_codes, list)
            else None
        )
        return cls(
            alert_badge_count=_as_int(data.get("alert_badge_count")),
            salt_level_alert=_as_bool(data.get("salt_level_alert")),
            flow_monitor_alert=_as_bool(data.get("flow_monitor_alert")),
            connection_alert=_as_bool(data.get("connection_alert")),
            water_usage_alert=_as_bool(data.get("water_usage_alert")),
            resin_alert=_as_bool(data.get("resin_alert")),
            error_code_alert=_as_bool(data.get("error_code_alert")),
            service_reminder_message=_as_str(data.get("service_reminder_message")),
            water_to_drain_monitor_enabled=_as_bool(
                data.get("water_to_drain_monitor_enabled")
            ),
            alarm_is_beeping=_as_bool(data.get("alarm_is_beeping")),
            water_to_drain_alert=_as_bool(data.get("water_to_drain_alert")),
            error_codes=error_codes,
        )


@dataclass(frozen=True, slots=True)
class FlowMonitorStatus:
    """Flow-monitor block of the enriched water-treatment data."""

    count: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a FlowMonitorStatus payload."""
        return cls(count=_as_int(data.get("count")))


@dataclass(frozen=True, slots=True)
class RechargeDialog:
    """Confirmation-dialog text for a recharge action."""

    title: str | None = None
    message: str | None = None
    confirm_label: str | None = None
    cancel_label: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a RechargeDialog payload."""
        return cls(
            title=_as_str(data.get("title")),
            message=_as_str(data.get("message")),
            confirm_label=_as_str(data.get("confirm_label")),
            cancel_label=_as_str(data.get("cancel_label")),
        )


@dataclass(frozen=True, slots=True)
class RechargeAction:
    """A single actionable recharge control from ``recharge_ui``."""

    action: str | None = None
    label: str | None = None
    requires_confirmation: bool | None = None
    dialog: RechargeDialog | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a RechargeAction payload."""
        dialog = data.get("dialog")
        return cls(
            action=_as_str(data.get("action")),
            label=_as_str(data.get("label")),
            requires_confirmation=_as_bool(data.get("requires_confirmation")),
            dialog=RechargeDialog.from_dict(dialog)
            if isinstance(dialog, dict)
            else None,
        )


@dataclass(frozen=True, slots=True)
class RechargeUi:
    """Recharge UI state block (``recharge_ui``)."""

    state: str | None = None
    title: str | None = None
    message: str | None = None
    actions: tuple[RechargeAction, ...] = ()
    time_remaining_seconds: int | None = None
    current_valve_state: str | None = None
    can_recharge: bool | None = None
    can_schedule: bool | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a RechargeUIState payload."""
        raw_actions = data.get("actions")
        actions = (
            tuple(
                RechargeAction.from_dict(item)
                for item in raw_actions
                if isinstance(item, dict)
            )
            if isinstance(raw_actions, list)
            else ()
        )
        return cls(
            state=_as_str(data.get("state")),
            title=_as_str(data.get("title")),
            message=_as_str(data.get("message")),
            actions=actions,
            time_remaining_seconds=_as_int(data.get("time_remaining_seconds")),
            current_valve_state=_as_str(data.get("current_valve_state")),
            can_recharge=_as_bool(data.get("can_recharge")),
            can_schedule=_as_bool(data.get("can_schedule")),
        )


@dataclass(frozen=True, slots=True)
class WsovDialogButtons:
    """Enabled-button flags of a WSOV dialog (WaterShutoffValveDialogButtons).

    Each flag is ``True`` when the server offers that action, ``False`` when it
    explicitly disables it, and ``None`` when the flag is absent. The valve
    entity treats an explicit ``False`` on ``open``/``close`` as a hard block.
    """

    acknowledge: bool | None = None
    cancel: bool | None = None
    close: bool | None = None
    open: bool | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a WaterShutoffValveDialogButtons payload."""
        return cls(
            acknowledge=_as_bool(data.get("acknowledge")),
            cancel=_as_bool(data.get("cancel")),
            close=_as_bool(data.get("close")),
            open=_as_bool(data.get("open")),
        )


@dataclass(frozen=True, slots=True)
class WsovDialog:
    """Confirmation-dialog state for a water-shutoff-valve action."""

    button_disabled: bool | None = None
    button_label: str | None = None
    dialog_buttons: WsovDialogButtons | None = None
    dialog_explanation: str | None = None
    dialog_title: str | None = None
    state_message: str | None = None
    is_error: bool | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a WaterShutoffValveDialog payload."""
        dialog_buttons = data.get("dialog_buttons")
        return cls(
            button_disabled=_as_bool(data.get("button_disabled")),
            button_label=_as_str(data.get("button_label")),
            dialog_buttons=WsovDialogButtons.from_dict(dialog_buttons)
            if isinstance(dialog_buttons, dict)
            else None,
            dialog_explanation=_as_str(data.get("dialog_explanation")),
            dialog_title=_as_str(data.get("dialog_title")),
            state_message=_as_str(data.get("state_message")),
            is_error=_as_bool(data.get("is_error")),
        )


@dataclass(frozen=True, slots=True)
class WaterShutoffValve:
    """Water-shutoff-valve block of the enriched data (WaterShutoffValveStatus).

    ``status`` is the enum ``open``/``close``/``manual``/``not_installed``/
    ``unknown``/``error``; ``auto_shutoff_features`` lists the conditions the
    valve can auto-close on (empty when absent or null).
    """

    status: str | None = None
    is_installed: bool | None = None
    auto_shutoff_supported: bool | None = None
    auto_shutoff_features: tuple[str, ...] = ()
    error_code: str | None = None
    manual_override: bool | None = None
    dialog: WsovDialog | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a WaterShutoffValveStatus payload."""
        dialog = data.get("dialog")
        return cls(
            status=_as_str(data.get("status")),
            is_installed=_as_bool(data.get("is_installed")),
            auto_shutoff_supported=_as_bool(data.get("auto_shutoff_supported")),
            auto_shutoff_features=_as_str_tuple(data.get("auto_shutoff_features")),
            error_code=_as_str(data.get("error_code")),
            manual_override=_as_bool(data.get("manual_override")),
            dialog=WsovDialog.from_dict(dialog) if isinstance(dialog, dict) else None,
        )


@dataclass(frozen=True, slots=True)
class LeakDetectorFlag:
    """One boolean status item of a leak detector (StatusItemBool).

    Pairs the boolean ``value`` with the ``updated_at`` timestamp the device
    last reported it; either may be ``None`` when absent.
    """

    value: bool | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a WaterTreatmentLeakDetectorStatusItemBool payload."""
        return cls(
            value=_as_bool(data.get("value")),
            updated_at=_parse_datetime(data.get("updated_at")),
        )


@dataclass(frozen=True, slots=True)
class LeakDetectorTemperature:
    """Temperature reading of a leak detector.

    Only the numeric ``raw_value`` (native US unit) and ``converted_value``
    (account-preference unit) are kept; the ``display`` TranslatableValue and
    the nested ``status`` item are intentionally ignored — the entity layer
    binds the raw value, never the account-following conversion.
    """

    raw_value: float | None = None
    converted_value: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a WaterTreatmentLeakDetectorTemperature payload."""
        return cls(
            raw_value=_as_float(data.get("raw_value")),
            converted_value=_as_float(data.get("converted_value")),
        )


def _leak_flag(value: Any) -> LeakDetectorFlag | None:
    """Parse a nested StatusItemBool object, or ``None`` when it is absent."""
    return LeakDetectorFlag.from_dict(value) if isinstance(value, dict) else None


@dataclass(frozen=True, slots=True)
class LeakDetectorStatus:
    """Status block of a single leak detector (WaterTreatmentLeakDetectorStatus).

    ``signal_strength`` is flattened out of its ``{"value": int}`` wrapper; the
    four boolean flags carry their own timestamps via :class:`LeakDetectorFlag`.
    """

    in_alert_state: bool | None = None
    is_connected: LeakDetectorFlag | None = None
    leak_detected: LeakDetectorFlag | None = None
    low_battery: LeakDetectorFlag | None = None
    tampered: LeakDetectorFlag | None = None
    signal_strength: int | None = None
    temperature: LeakDetectorTemperature | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a WaterTreatmentLeakDetectorStatus payload."""
        signal = data.get("signal_strength")
        temperature = data.get("temperature")
        return cls(
            in_alert_state=_as_bool(data.get("in_alert_state")),
            is_connected=_leak_flag(data.get("is_connected")),
            leak_detected=_leak_flag(data.get("leak_detected")),
            low_battery=_leak_flag(data.get("low_battery")),
            tampered=_leak_flag(data.get("tampered")),
            signal_strength=_as_int(signal.get("value"))
            if isinstance(signal, dict)
            else None,
            temperature=LeakDetectorTemperature.from_dict(temperature)
            if isinstance(temperature, dict)
            else None,
        )


@dataclass(frozen=True, slots=True)
class LeakDetector:
    """A single leak detector's detail (WaterTreatmentLeakDetectorDetail)."""

    detector_id: int
    nickname: str | None = None
    nickname_setting_key: str | None = None
    status: LeakDetectorStatus | None = None
    last_updated_at: datetime | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self | None:
        """Parse a WaterTreatmentLeakDetectorDetail payload.

        Returns ``None`` when the payload lacks the required integer
        ``detector_id`` — such an entry cannot be addressed as an entity, so it
        is dropped rather than parsed into an unusable object.
        """
        detector_id = _as_int(data.get("detector_id"))
        if detector_id is None:
            return None
        status = data.get("status")
        return cls(
            detector_id=detector_id,
            nickname=_as_str(data.get("nickname")),
            nickname_setting_key=_as_str(data.get("nickname_setting_key")),
            status=LeakDetectorStatus.from_dict(status)
            if isinstance(status, dict)
            else None,
            last_updated_at=_parse_datetime(data.get("last_updated_at")),
        )


@dataclass(frozen=True, slots=True)
class LeakDetectors:
    """Leak-detector collection of the enriched data (WaterTreatmentLeakDetectors).

    ``is_scanning`` is flattened out of the nested ``scanning.is_scanning``
    field; ``details`` drops any entry without a usable ``detector_id``.
    """

    details: tuple[LeakDetector, ...] = ()
    is_scanning: bool | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a WaterTreatmentLeakDetectors payload."""
        raw = data.get("details")
        details: tuple[LeakDetector, ...] = ()
        if isinstance(raw, list):
            parsed = [
                LeakDetector.from_dict(item) for item in raw if isinstance(item, dict)
            ]
            details = tuple(item for item in parsed if item is not None)
        scanning = data.get("scanning")
        is_scanning = (
            _as_bool(scanning.get("is_scanning"))
            if isinstance(scanning, dict)
            else None
        )
        return cls(details=details, is_scanning=is_scanning)


@dataclass(frozen=True, slots=True)
class WaterTreatment:
    """Curated ``water_treatment`` object (the enriched device data).

    Only ``treatment_system_type`` and ``salt_level.monitoring_enabled`` are
    guaranteed present; every other field is omitted when the corresponding
    feature is absent, so all are optional.
    """

    treatment_system_type: str
    salt_level_percent: float | None = None
    salt_level: SaltLevel | None = None
    gallons_used_today: int | None = None
    regeneration: RegenerationInfo | None = None
    recharge_ui: RechargeUi | None = None
    regeneration_status: str | None = None
    water_treatment_status: WaterTreatmentStatus | None = None
    rf_signal_strength_dbm: int | None = None
    model: str | None = None
    pwa: str | None = None
    date_code: str | None = None
    control_version: str | None = None
    wifi_module_version: str | None = None
    total_recharges: int | None = None
    days_since_last_recharge: int | None = None
    days_powered_up: int | None = None
    total_water_used: ConvertedProperty | None = None
    treated_water_available: ConvertedProperty | None = None
    wifi_ssid_name: str | None = None
    flow_monitor_status: FlowMonitorStatus | None = None
    water_shutoff_valve: WaterShutoffValve | None = None
    leak_detectors: LeakDetectors | None = None
    features: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a WaterTreatmentEnrichedData payload."""
        salt_level = data.get("salt_level")
        regeneration = data.get("regeneration")
        recharge_ui = data.get("recharge_ui")
        water_treatment_status = data.get("water_treatment_status")
        total_water_used = data.get("total_water_used")
        treated_water_available = data.get("treated_water_available")
        flow_monitor_status = data.get("flow_monitor_status")
        water_shutoff_valve = data.get("water_shutoff_valve")
        leak_detectors = data.get("leak_detectors")
        return cls(
            treatment_system_type=_str_or_empty(data.get("treatment_system_type")),
            salt_level_percent=_as_float(data.get("salt_level_percent")),
            salt_level=SaltLevel.from_dict(salt_level)
            if isinstance(salt_level, dict)
            else None,
            gallons_used_today=_as_int(data.get("gallons_used_today")),
            regeneration=RegenerationInfo.from_dict(regeneration)
            if isinstance(regeneration, dict)
            else None,
            recharge_ui=RechargeUi.from_dict(recharge_ui)
            if isinstance(recharge_ui, dict)
            else None,
            regeneration_status=_as_str(data.get("regeneration_status")),
            water_treatment_status=WaterTreatmentStatus.from_dict(
                water_treatment_status
            )
            if isinstance(water_treatment_status, dict)
            else None,
            rf_signal_strength_dbm=_as_int(data.get("rf_signal_strength_dbm")),
            model=_as_str(data.get("model")),
            pwa=_as_str(data.get("pwa")),
            date_code=_as_str(data.get("date_code")),
            control_version=_as_str(data.get("control_version")),
            wifi_module_version=_as_str(data.get("wifi_module_version")),
            total_recharges=_as_int(data.get("total_recharges")),
            days_since_last_recharge=_as_int(data.get("days_since_last_recharge")),
            days_powered_up=_as_int(data.get("days_powered_up")),
            total_water_used=ConvertedProperty.from_dict(total_water_used)
            if isinstance(total_water_used, dict)
            else None,
            treated_water_available=ConvertedProperty.from_dict(treated_water_available)
            if isinstance(treated_water_available, dict)
            else None,
            wifi_ssid_name=_as_str(data.get("wifi_ssid_name")),
            flow_monitor_status=FlowMonitorStatus.from_dict(flow_monitor_status)
            if isinstance(flow_monitor_status, dict)
            else None,
            water_shutoff_valve=WaterShutoffValve.from_dict(water_shutoff_valve)
            if isinstance(water_shutoff_valve, dict)
            else None,
            leak_detectors=LeakDetectors.from_dict(leak_detectors)
            if isinstance(leak_detectors, dict)
            else None,
            features=_as_str_tuple(data.get("features")),
        )


# ---------------------------------------------------------------------------
# Raw device properties
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PropertyValue:
    """One entry of the raw ``properties`` map (DevicesGetPropertyResponseItem).

    ``value`` is the device-native reading; many are integer-scaled — use
    :func:`scaled_value` before exposing them. ``converted_value`` follows the
    account's unit preference, so prefer ``unit_conversions`` for a fixed unit.
    """

    name: str
    value: bool | int | float | str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    converted_value: float | None = None
    converted_units: str | None = None
    unit_conversions: tuple[tuple[str, float], ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a DevicesGetPropertyResponseItem payload."""
        raw_conversions = data.get("unit_conversions")
        conversions: tuple[tuple[str, float], ...] = ()
        if isinstance(raw_conversions, list):
            pairs: list[tuple[str, float]] = []
            for item in raw_conversions:
                if not isinstance(item, dict):
                    continue
                unit = _as_str(item.get("unit"))
                converted = _as_float(item.get("value"))
                if unit is not None and converted is not None:
                    pairs.append((unit, converted))
            conversions = tuple(pairs)
        return cls(
            name=_str_or_empty(data.get("name")),
            value=_as_scalar(data.get("value")),
            created_at=_parse_datetime(data.get("created_at")),
            updated_at=_parse_datetime(data.get("updated_at")),
            converted_value=_as_float(data.get("converted_value")),
            converted_units=_as_str(data.get("converted_units")),
            unit_conversions=conversions,
        )


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UserSummary:
    """Owner summary embedded in a device payload."""

    id: str
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a UserSummary payload."""
        return cls(
            id=_str_or_empty(data.get("id")),
            first_name=_as_str(data.get("first_name")),
            last_name=_as_str(data.get("last_name")),
            email=_as_str(data.get("email")),
        )


@dataclass(frozen=True, slots=True)
class DeviceSummary:
    """Identity-only device view (DeviceSummaryObject)."""

    id: str
    thing_name: str | None = None
    system_type: str | None = None
    system_type_display: str | None = None
    image_url: str | None = None
    serial_number: str | None = None
    nickname: str | None = None
    is_shared_with_dealer: bool | None = None
    is_rental: bool | None = None
    is_disabled: bool | None = None
    user: UserSummary | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a DeviceSummaryObject payload."""
        user = data.get("user")
        return cls(
            id=_str_or_empty(data.get("id")),
            thing_name=_as_str(data.get("thing_name")),
            system_type=_as_str(data.get("system_type")),
            system_type_display=_as_str(data.get("system_type_display")),
            image_url=_as_str(data.get("image_url")),
            serial_number=_as_str(data.get("serial_number")),
            nickname=_as_str(data.get("nickname")),
            is_shared_with_dealer=_as_bool(data.get("is_shared_with_dealer")),
            is_rental=_as_bool(data.get("is_rental")),
            is_disabled=_as_bool(data.get("is_disabled")),
            user=UserSummary.from_dict(user) if isinstance(user, dict) else None,
        )


@dataclass(frozen=True, slots=True)
class Device:
    """Full device view (DeviceObject) — the primary poll payload."""

    id: str
    thing_name: str | None = None
    system_type: str | None = None
    system_type_display: str | None = None
    image_url: str | None = None
    serial_number: str | None = None
    nickname: str | None = None
    is_shared_with_dealer: bool | None = None
    is_rental: bool | None = None
    is_disabled: bool | None = None
    is_online: bool | None = None
    user: UserSummary | None = None
    enriched_data: WaterTreatment | None = None
    properties: dict[str, PropertyValue] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a DeviceObject payload."""
        user = data.get("user")
        enriched = data.get("enriched_data")
        enriched_data: WaterTreatment | None = None
        if isinstance(enriched, dict):
            water_treatment = enriched.get("water_treatment")
            if isinstance(water_treatment, dict):
                enriched_data = WaterTreatment.from_dict(water_treatment)
        raw_properties = data.get("properties")
        properties: dict[str, PropertyValue] = {}
        if isinstance(raw_properties, dict):
            for key, item in raw_properties.items():
                if isinstance(item, dict):
                    properties[key] = PropertyValue.from_dict(item)
        return cls(
            id=_str_or_empty(data.get("id")),
            thing_name=_as_str(data.get("thing_name")),
            system_type=_as_str(data.get("system_type")),
            system_type_display=_as_str(data.get("system_type_display")),
            image_url=_as_str(data.get("image_url")),
            serial_number=_as_str(data.get("serial_number")),
            nickname=_as_str(data.get("nickname")),
            is_shared_with_dealer=_as_bool(data.get("is_shared_with_dealer")),
            is_rental=_as_bool(data.get("is_rental")),
            is_disabled=_as_bool(data.get("is_disabled")),
            is_online=_as_bool(data.get("is_online")),
            user=UserSummary.from_dict(user) if isinstance(user, dict) else None,
            enriched_data=enriched_data,
            properties=properties,
        )

    @property
    def online(self) -> bool | None:
        """Return the availability signal for the device.

        Prefer the ``_internal_is_online`` property (the freshest signal) and
        fall back to the top-level ``is_online`` flag when it is absent.
        """
        prop = self.properties.get("_internal_is_online")
        if prop is not None:
            return prop.value is True
        return self.is_online


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Alert:
    """A single device alert (AlertGetResponseItem)."""

    id: str
    type: str | None = None
    title: str | None = None
    message: str | None = None
    details: str | None = None
    level: str | None = None
    timestamp: datetime | None = None
    display_time: str | None = None
    is_read: bool | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse an AlertGetResponseItem payload."""
        return cls(
            id=_str_or_empty(data.get("id")),
            type=_as_str(data.get("type")),
            title=_as_str(data.get("title")),
            message=_as_str(data.get("message")),
            details=_as_str(data.get("details")),
            level=_as_str(data.get("level")),
            timestamp=_parse_datetime(data.get("timestamp")),
            display_time=_as_str(data.get("display_time")),
            is_read=_as_bool(data.get("is_read")),
        )


@dataclass(frozen=True, slots=True)
class AlertsPage:
    """A page of the device alert history (GetDeviceAlertsOutputBody)."""

    page: int | None = None
    per_page: int | None = None
    total: int | None = None
    alerts: tuple[Alert, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a GetDeviceAlertsOutputBody payload."""
        raw = data.get("alerts")
        alerts = (
            tuple(Alert.from_dict(item) for item in raw if isinstance(item, dict))
            if isinstance(raw, list)
            else ()
        )
        return cls(
            page=_as_int(data.get("page")),
            per_page=_as_int(data.get("per_page")),
            total=_as_int(data.get("total")),
            alerts=alerts,
        )


# ---------------------------------------------------------------------------
# Regeneration events
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegenerationEvent:
    """A completed or in-progress regeneration (RegenerationEventObject)."""

    id: str
    start_time: datetime | None = None
    end_time: datetime | None = None
    device_start_time: datetime | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a RegenerationEventObject payload."""
        return cls(
            id=_str_or_empty(data.get("id")),
            start_time=_parse_datetime(data.get("start_time")),
            end_time=_parse_datetime(data.get("end_time")),
            device_start_time=_parse_datetime(data.get("device_start_time")),
        )


@dataclass(frozen=True, slots=True)
class RegenerationEventsPage:
    """A page of regeneration history (PaginationResponseRegenerationEvent)."""

    page: int | None = None
    per_page: int | None = None
    total: int | None = None
    events: tuple[RegenerationEvent, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a PaginationResponseRegenerationEventObject payload."""
        raw = data.get("data")
        events = (
            tuple(
                RegenerationEvent.from_dict(item)
                for item in raw
                if isinstance(item, dict)
            )
            if isinstance(raw, list)
            else ()
        )
        return cls(
            page=_as_int(data.get("page")),
            per_page=_as_int(data.get("per_page")),
            total=_as_int(data.get("total")),
            events=events,
        )


# ---------------------------------------------------------------------------
# Datapoint graphs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Datapoint:
    """One period of a datapoint graph (DevicesGetDatapointSummaryGraphItem)."""

    label: datetime | None = None
    display_label: str | None = None
    value: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a DevicesGetDatapointSummaryGraphItem payload."""
        return cls(
            label=_parse_datetime(data.get("label")),
            display_label=_as_str(data.get("display_label")),
            value=_as_float(data.get("value")),
        )


@dataclass(frozen=True, slots=True)
class DatapointGraph:
    """A datapoint graph series (DeviceGetDatapointGraphBody).

    ``units`` follow the account's unit preference — treat them as a display
    hint, not a stable native unit.
    """

    units: str | None = None
    data: tuple[Datapoint, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a DeviceGetDatapointGraphBody payload."""
        raw = data.get("data")
        points = (
            tuple(Datapoint.from_dict(item) for item in raw if isinstance(item, dict))
            if isinstance(raw, list)
            else ()
        )
        return cls(units=_as_str(data.get("units")), data=points)


# ---------------------------------------------------------------------------
# Commands and live streaming
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Result of PUT /devices/{id}/command (DeviceCommandOutputBody)."""

    status: str | None = None
    message: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a DeviceCommandOutputBody payload."""
        return cls(
            status=_as_str(data.get("status")),
            message=_as_str(data.get("message")),
        )


@dataclass(frozen=True, slots=True)
class LiveTicket:
    """Live-stream ticket (GetDeviceLiveDataOutputBody).

    ``websocket_uri`` is the ticketed path to connect to; the ticket alone
    authenticates the websocket and expires shortly after issue.
    """

    websocket_uri: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a GetDeviceLiveDataOutputBody payload."""
        return cls(websocket_uri=_as_str(data.get("websocket_uri")))


# ---------------------------------------------------------------------------
# Device settings document (DeviceSettingsBody / DeviceSettingItem)
#
# The settings endpoint returns a flat list of settings, each carrying a
# component type, current value, and a rule block that describes the valid
# values. A setting may also carry a ``conditional`` group that gates whether
# it should be shown, evaluated against the *other* settings' current values —
# so visibility is a document-level operation (:meth:`DeviceSettingsDocument.
# setting_visible`), not a per-setting one.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SelectOption:
    """One option of a select/multiselect rule (SelectOption).

    ``value`` is the raw value written back to the API; ``label`` is the
    server-localized display string. Both are required — an option missing
    either is dropped by :meth:`from_dict` since it cannot be presented or
    written.
    """

    value: str
    label: str
    disabled: bool | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self | None:
        """Parse a SelectOption payload, or ``None`` when value/label is missing."""
        value = _as_str(data.get("value"))
        label = _as_str(data.get("label"))
        if value is None or label is None:
            return None
        return cls(value=value, label=label, disabled=_as_bool(data.get("disabled")))


def _parse_options(raw: Any) -> tuple[SelectOption, ...]:
    """Parse a list of SelectOption payloads, dropping malformed entries."""
    if not isinstance(raw, list):
        return ()
    parsed = [SelectOption.from_dict(item) for item in raw if isinstance(item, dict)]
    return tuple(item for item in parsed if item is not None)


@dataclass(frozen=True, slots=True)
class SelectRules:
    """Rule block for a select setting (SelectRule)."""

    options: tuple[SelectOption, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a SelectRule payload."""
        return cls(options=_parse_options(data.get("options")))


@dataclass(frozen=True, slots=True)
class NumberRules:
    """Rule block for a number setting (NumberRule).

    Every bound is optional in the spec; the entity layer applies its own
    ``precision``-scaled fallbacks when a bound is ``None``.
    """

    min: int | None = None
    max: int | None = None
    step: int | None = None
    precision: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a NumberRule payload."""
        return cls(
            min=_as_int(data.get("min")),
            max=_as_int(data.get("max")),
            step=_as_int(data.get("step")),
            precision=_as_int(data.get("precision")),
        )


@dataclass(frozen=True, slots=True)
class TextRules:
    """Rule block for a text setting (TextRule)."""

    min_length: int | None = None
    max_length: int | None = None
    regex: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a TextRule payload."""
        return cls(
            min_length=_as_int(data.get("min_length")),
            max_length=_as_int(data.get("max_length")),
            regex=_as_str(data.get("regex")),
        )


@dataclass(frozen=True, slots=True)
class MultiSelectRules:
    """Rule block for a multiselect setting (MultiSelectRule)."""

    options: tuple[SelectOption, ...] = ()
    required: bool | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a MultiSelectRule payload."""
        return cls(
            options=_parse_options(data.get("options")),
            required=_as_bool(data.get("required")),
        )


@dataclass(frozen=True, slots=True)
class ConditionalRule:
    """One clause of a setting's visibility condition (ConditionalRule).

    ``field`` names another setting, ``comparison`` is the operator (only
    ``eq`` is understood today), and ``value`` is the JSON scalar to compare
    against that other setting's current value.
    """

    field: str
    comparison: str
    value: bool | int | float | str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self | None:
        """Parse a ConditionalRule payload.

        Returns ``None`` when either the ``field`` or the ``comparison`` string
        is missing — such a clause cannot be evaluated, so it is dropped.
        """
        field_name = _as_str(data.get("field"))
        comparison = _as_str(data.get("comparison"))
        if field_name is None or comparison is None:
            return None
        return cls(
            field=field_name,
            comparison=comparison,
            value=_as_scalar(data.get("value")),
        )


def _parse_conditional_rules(raw: Any) -> tuple[ConditionalRule, ...]:
    """Parse a list of ConditionalRule payloads, dropping unparseable clauses."""
    if not isinstance(raw, list):
        return ()
    parsed = [ConditionalRule.from_dict(item) for item in raw if isinstance(item, dict)]
    return tuple(item for item in parsed if item is not None)


@dataclass(frozen=True, slots=True)
class ConditionalRuleGroup:
    """Visibility condition of a setting (ConditionalRuleGroup).

    ``and_rules`` (all must hold) come from the ``and`` key, ``or_rules`` (any
    must hold) from the ``or`` key. Either group may be empty.
    """

    and_rules: tuple[ConditionalRule, ...] = ()
    or_rules: tuple[ConditionalRule, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a ConditionalRuleGroup payload."""
        return cls(
            and_rules=_parse_conditional_rules(data.get("and")),
            or_rules=_parse_conditional_rules(data.get("or")),
        )


@dataclass(frozen=True, slots=True)
class SettingRules:
    """The rule block of a setting (DeviceSettingRule) — one shape per type."""

    select_rules: SelectRules | None = None
    number_rules: NumberRules | None = None
    text_rules: TextRules | None = None
    multiselect_rules: MultiSelectRules | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a DeviceSettingRule payload."""
        select_rules = data.get("select_rules")
        number_rules = data.get("number_rules")
        text_rules = data.get("text_rules")
        multiselect_rules = data.get("multiselect_rules")
        return cls(
            select_rules=SelectRules.from_dict(select_rules)
            if isinstance(select_rules, dict)
            else None,
            number_rules=NumberRules.from_dict(number_rules)
            if isinstance(number_rules, dict)
            else None,
            text_rules=TextRules.from_dict(text_rules)
            if isinstance(text_rules, dict)
            else None,
            multiselect_rules=MultiSelectRules.from_dict(multiselect_rules)
            if isinstance(multiselect_rules, dict)
            else None,
        )


@dataclass(frozen=True, slots=True)
class DeviceSetting:
    """One configurable setting of a device (DeviceSettingItem).

    ``current_value`` is the raw, precision-expanded scalar the server returns
    (a number setting's ``12.5`` grains arrives as ``125``); the entity layer
    applies the ``NumberRule.precision`` scaling before display.
    """

    component_type: str
    name: str
    label: str
    current_value: bool | int | float | str | None = None
    description: str | None = None
    rules: SettingRules | None = None
    conditional: ConditionalRuleGroup | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a DeviceSettingItem payload."""
        rules = data.get("rules")
        conditional = data.get("conditional")
        return cls(
            component_type=_str_or_empty(data.get("component_type")),
            name=_str_or_empty(data.get("name")),
            label=_str_or_empty(data.get("label")),
            current_value=_as_scalar(data.get("current_value")),
            description=_as_str(data.get("description")),
            rules=SettingRules.from_dict(rules) if isinstance(rules, dict) else None,
            conditional=ConditionalRuleGroup.from_dict(conditional)
            if isinstance(conditional, dict)
            else None,
        )


#: Comparison operator understood by :meth:`DeviceSettingsDocument.setting_visible`.
#: Any other operator fails open (the setting stays visible) — see that method.
_CONDITIONAL_EQ = "eq"


@dataclass(frozen=True, slots=True)
class DeviceSettingsDocument:
    """Parsed device settings document (DeviceSettingsBody)."""

    settings: tuple[DeviceSetting, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse a DeviceSettingsBody payload."""
        raw = data.get("settings")
        settings = (
            tuple(
                DeviceSetting.from_dict(item) for item in raw if isinstance(item, dict)
            )
            if isinstance(raw, list)
            else ()
        )
        return cls(settings=settings)

    def get(self, name: str) -> DeviceSetting | None:
        """Return the setting named ``name``, or ``None`` when it is absent."""
        for setting in self.settings:
            if setting.name == name:
                return setting
        return None

    def setting_visible(self, setting: DeviceSetting) -> bool:
        """Return whether ``setting`` should be shown given the document state.

        A setting without a ``conditional`` is always visible. Otherwise each
        clause references another setting by name and is evaluated as
        ``str(other.current_value) == str(rule.value)`` for the ``eq`` operator;
        a clause whose referenced setting does not exist is ``False``. Both
        groups gate together — all ``and`` clauses must hold *and* at least one
        ``or`` clause must hold — with an empty group ignored.

        **Unknown comparison operators fail open (evaluate ``True``).** We never
        hide a setting on a guess: if the guess is wrong the server rejects the
        write and the entity surfaces that, whereas a wrongly-hidden setting is
        silently unreachable. Only ``eq`` is confirmed live.
        """
        conditional = setting.conditional
        if conditional is None:
            return True
        results: list[bool] = []
        if conditional.and_rules:
            results.append(
                all(self._evaluate_rule(rule) for rule in conditional.and_rules)
            )
        if conditional.or_rules:
            results.append(
                any(self._evaluate_rule(rule) for rule in conditional.or_rules)
            )
        return all(results)

    def _evaluate_rule(self, rule: ConditionalRule) -> bool:
        """Evaluate one conditional clause against the document's settings."""
        other = self.get(rule.field)
        if other is None:
            return False
        if rule.comparison == _CONDITIONAL_EQ:
            return str(other.current_value) == str(rule.value)
        # Unknown operator: fail open (see setting_visible docstring).
        return True


# ---------------------------------------------------------------------------
# Rate-limit telemetry
# ---------------------------------------------------------------------------


def _policy_leading_limit(token: str) -> int | None:
    """Parse the leading bare-integer limit of a ``ratelimit-policy`` string."""
    try:
        return int(token.strip())
    except ValueError:
        return None


def _policy_window_seconds(tokens: list[str]) -> float | None:
    """Return the ``w=<seconds>`` window of a policy string, wherever it sits."""
    for token in tokens:
        stripped = token.strip()
        if stripped[:2].casefold() == "w=":
            try:
                return float(stripped[2:])
            except ValueError:
                return None
    return None


@dataclass(frozen=True, slots=True)
class RateLimitStatus:
    """Latest rate-limit telemetry parsed from the ``ratelimit-*`` headers.

    The iQua cloud returns ``ratelimit-limit``, ``ratelimit-remaining``, and
    ``ratelimit-policy`` (e.g. ``"5;w=60;burst=50;policy=token_bucket"``) on
    every response. Any field is ``None`` when its header is absent or
    unparsable — reading these headers must never raise.
    """

    limit: int | None = None
    remaining: int | None = None
    policy: str | None = None

    @property
    def refill_seconds(self) -> float | None:
        """Return the token-bucket refill interval (seconds), or ``None``.

        Derived from the ``ratelimit-policy`` string as ``w / limit`` — the
        window length divided by the token count, i.e. the average seconds
        between two granted tokens (``60 / 5 == 12``). The two policy shapes
        documented by the fork disagree on both numbers and field order
        (``"5;w=60;burst=50;policy=token_bucket"`` vs ``"6;w=600;burst=60"``),
        so this parses defensively: the limit is the *leading* bare integer and
        ``w=`` is matched by label in any position. Missing, zero, or unparsable
        fields yield ``None`` — reading this must never raise.
        """
        policy = self.policy
        if not policy:
            return None
        tokens = policy.split(";")
        limit = _policy_leading_limit(tokens[0])
        if limit is None or limit <= 0:
            return None
        window = _policy_window_seconds(tokens)
        if window is None or window <= 0:
            return None
        return window / limit


# ---------------------------------------------------------------------------
# Raw-property scaling
#
# Many raw properties are integer-scaled; the divisors below are verified
# against the device's own converted values (see
# reverse-engineering/knowledge/device/aquahome-20-smart.md, "Raw property scaling factors").
# ---------------------------------------------------------------------------

#: Sentinel value that means "feature disabled" for ``service_reminder_months``.
SENTINEL_DISABLED = -1

#: Raw property name -> divisor to recover the human-readable value.
#:
#: The two lifetime weight totals are tenths of pounds despite the plain
#: ``_lbs`` suffix: anchored on the validated x10^4 per-regeneration dose
#: (38281 -> 3.8281 lb ≈ 1.74 kg), 149 regenerations x 3.8281 lb = 570.4 lb —
#: exactly ``total_salt_use_lbs`` 5704 ÷ 10 (an unscaled 5704 lb would mean an
#: absurd 17 kg of salt per cycle). The server's own ``converted_value`` for
#: these two applies lb→kg WITHOUT the ÷10 (5704 -> "2587 kg"), so it is wrong
#: for them — bind the scaled raw value, never that conversion.
SCALED_PROPERTIES: dict[str, float] = {
    "salt_level_tenths": 10.0,
    "iron_level_tenths_ppm": 10.0,
    "chem_feed_tenths_secs": 10.0,
    "capacity_remaining_percent": 10.0,
    "average_exhaustion_percent": 10.0,
    "avg_days_between_regens": 100.0,
    "avg_salt_per_regen_lbs": 10_000.0,
    "total_salt_use_lbs": 10.0,
    "total_rock_removed_lbs": 10.0,
}

#: Scale factors that are plausible but NOT yet confirmed against a live,
#: nonzero reading. The entity layer must verify these before use, so
#: :func:`scaled_value` deliberately does not apply them.
UNVERIFIED_SCALED_PROPERTIES: dict[str, float] = {
    "current_water_flow_gpm": 10.0,
}


def scaled_value(prop: PropertyValue) -> float | None:
    """Return the human-readable numeric value of a raw property.

    Applies the verified divisor from :data:`SCALED_PROPERTIES`, maps the
    ``service_reminder_months`` disabled sentinel to ``None``, and passes
    unscaled numeric properties through unchanged. Non-numeric (and boolean)
    values yield ``None``. Divisors in :data:`UNVERIFIED_SCALED_PROPERTIES`
    are intentionally not applied.
    """
    value = prop.value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if prop.name == "service_reminder_months" and value == SENTINEL_DISABLED:
        return None
    divisor = SCALED_PROPERTIES.get(prop.name)
    if divisor is not None:
        return value / divisor
    return float(value)
