"""Binary sensor platform for the AquaHome integration.

Every Phase-2 binary is a *cloud-side* state — the connectivity flag itself and
the enriched water-treatment alert flags. These stay meaningful while the
softener is offline (a connection alert is precisely what the user needs then),
so the platform deliberately overrides
:attr:`AquaHomeEntity._require_device_online` to ``False``: gating these on
``device_online`` would suppress exactly the signals that matter during an
outage.

Which binaries exist for a given device is discovered from the coordinator's
device view via each description's ``exists_fn``. The set present at setup is
created immediately; :func:`~.dynamic.async_setup_dynamic_entities` then grows it
(debounced :data:`~.const.CAPABILITY_DEBOUNCE_POLLS` polls) when hardware added
later — a water-shutoff valve, an audible alarm, or a paired leak detector —
first advertises a feature-gated binary, and never removes one (vanished
hardware goes unavailable, not deleted). The six plain alert flags exist only
when the enriched status block actually carries them; the two feature-gated
binaries (audible alarm, water-to-drain) exist when the device advertises the
matching feature or the field is present in the payload. On the dev device
(features ``["regeneration"]``) this yields the online binary plus the six alert
binaries; the two feature-gated binaries are absent.

The Tier-2 binaries derive the softener's recharge mode from the enriched
``recharge_ui`` state (with an ``iqua2`` fallback to the ``regeneration`` block on
hosts that omit ``recharge_ui``). They obey an offline-honesty rule: a
``recharge_ui`` whose ``state`` is ``"offline"`` — the cloud has lost the device —
tells us nothing about the underlying mode, so every derived binary reports
``unknown`` (``None``) in that case rather than a fabricated ``False``. The
water-shutoff-valve binary is feature-gated on ``"wsov"`` and thus absent on the
dev device, which advertises only ``["regeneration"]``.

Each paired leak detector contributes four binaries — leak detected (moisture),
low battery, tamper, and connectivity — registered under the detector's own
sub-device via :class:`~.entity.AquaHomeLeakDetectorEntity`. The dev device pairs
none, so none is created there; a detector that later vanishes makes its binaries
unavailable through the base entity rather than reporting a fabricated state.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import callback

from .api import (
    Device,
    LeakDetector,
    LeakDetectorFlag,
    LeakDetectorStatus,
    RechargeUi,
    RegenerationInfo,
    WaterTreatmentStatus,
)
from .const import CAPABILITY_DEBOUNCE_POLLS, RECHARGE_STATE_OFFLINE
from .coordinator import resolve_device_online
from .dynamic import async_setup_dynamic_entities
from .entity import AquaHomeEntity, AquaHomeLeakDetectorEntity

if TYPE_CHECKING:
    from collections.abc import Set as AbstractSet

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import AquaHomeConfigEntry, AquaHomeCoordinator

# Read-only coordinator platform: entity updates never do their own I/O, so
# Home Assistant may run them unbounded (quality-scale parallel-updates rule).
PARALLEL_UPDATES = 0


# ---------------------------------------------------------------------------
# None-safe payload accessors
# ---------------------------------------------------------------------------


def _status(device: Device) -> WaterTreatmentStatus | None:
    """Return the enriched water-treatment status block, or ``None``."""
    enriched = device.enriched_data
    return enriched.water_treatment_status if enriched is not None else None


def _has_feature(device: Device, feature: str) -> bool:
    """Return whether the device advertises ``feature`` in its enriched data."""
    enriched = device.enriched_data
    return enriched is not None and feature in enriched.features


def _status_value(
    accessor: Callable[[WaterTreatmentStatus], bool | None],
) -> Callable[[Device], bool | None]:
    """Build a value function reading a boolean flag off the status block.

    The result yields ``None`` when the status block is absent, so an entity
    whose backing block momentarily disappears reports ``unknown`` rather than
    a stale or fabricated state.
    """

    def _value(device: Device) -> bool | None:
        """Read the flag from the device's status block, tolerating absence."""
        status = _status(device)
        return accessor(status) if status is not None else None

    return _value


def _status_present(
    accessor: Callable[[WaterTreatmentStatus], bool | None],
) -> Callable[[Device], bool]:
    """Build an exists function true only when the flag is carried in the payload.

    A flag exists for a device when the status block is present *and* the
    specific field is non-``None`` — feature-absent devices omit the field
    entirely, so its presence is the feature gate.
    """

    def _exists(device: Device) -> bool:
        """Return whether the status block carries a non-null value for the flag."""
        status = _status(device)
        return status is not None and accessor(status) is not None

    return _exists


def _alarm_beeping_exists(device: Device) -> bool:
    """Return whether the audible-alarm binary applies to this device.

    True when the device advertises the ``audible_alarm`` feature, or (for hosts
    that omit the feature list but still report the flag) when the field is
    present in the status block.
    """
    if _has_feature(device, "audible_alarm"):
        return True
    status = _status(device)
    return status is not None and status.alarm_is_beeping is not None


def _water_to_drain_exists(device: Device) -> bool:
    """Return whether the water-to-drain binary applies to this device.

    True when the device advertises a water-to-drain or leak-detector feature,
    or when the field is present in the status block on a host that omits the
    feature list.
    """
    if _has_feature(device, "water_to_drain_sensor") or _has_feature(
        device, "leak_detector"
    ):
        return True
    status = _status(device)
    return status is not None and status.water_to_drain_alert is not None


# ---------------------------------------------------------------------------
# Recharge / regeneration mode accessors (Tier-2 derived binaries)
# ---------------------------------------------------------------------------


def _recharge_ui(device: Device) -> RechargeUi | None:
    """Return the enriched ``recharge_ui`` block, or ``None`` when absent."""
    enriched = device.enriched_data
    return enriched.recharge_ui if enriched is not None else None


def _regeneration(device: Device) -> RegenerationInfo | None:
    """Return the enriched ``regeneration`` block, or ``None`` when absent."""
    enriched = device.enriched_data
    return enriched.regeneration if enriched is not None else None


def _recharge_state(device: Device) -> str | None:
    """Return the recharge-mode state string, honest about an offline tile.

    Yields ``None`` when the ``recharge_ui`` block is absent, when it carries no
    ``state``, or when the state is :data:`~.const.RECHARGE_STATE_OFFLINE` — an
    offline tile means the cloud has lost the device and reveals nothing about the
    underlying mode, so callers report ``unknown`` rather than a fabricated
    ``False``.
    """
    recharge_ui = _recharge_ui(device)
    if recharge_ui is None:
        return None
    state = recharge_ui.state
    if state is None or state == RECHARGE_STATE_OFFLINE:
        return None
    return state


def _regeneration_status(device: Device) -> str | None:
    """Return the ``regeneration.regeneration_status`` string, or ``None``."""
    regeneration = _regeneration(device)
    return regeneration.regeneration_status if regeneration is not None else None


def _recharge_state_is(target: str) -> Callable[[Device], bool | None]:
    """Build a value function matching the recharge state against ``target``.

    Returns ``None`` whenever :func:`_recharge_state` is ``None`` (block absent,
    state absent, or offline) so the derived binary reports ``unknown`` instead of
    an unfounded ``False``; otherwise the boolean equality.
    """

    def _value(device: Device) -> bool | None:
        """Return whether the recharge state equals ``target``, None when unknown."""
        state = _recharge_state(device)
        return state == target if state is not None else None

    return _value


def _recharge_or_regeneration_state_is(
    recharge_target: str, regeneration_target: str
) -> Callable[[Device], bool | None]:
    """Build a value function that falls back to the regeneration block.

    When the ``recharge_ui`` block is present, the offline-honest
    :func:`_recharge_state` drives the result. Only when ``recharge_ui`` is absent
    entirely (an ``iqua2`` host) does the value fall back to
    ``regeneration.regeneration_status``. Either source yields ``None`` when its
    own state is unknown.
    """

    def _value(device: Device) -> bool | None:
        """Match the recharge state, falling back to the regeneration status."""
        if _recharge_ui(device) is not None:
            state = _recharge_state(device)
            return state == recharge_target if state is not None else None
        status = _regeneration_status(device)
        return status == regeneration_target if status is not None else None

    return _value


def _recharge_ui_present(device: Device) -> bool:
    """Return whether the device carries a ``recharge_ui`` block."""
    return _recharge_ui(device) is not None


def _recharge_or_regeneration_present(device: Device) -> bool:
    """Return whether the device carries a ``recharge_ui`` or ``regeneration`` block.

    These are the two sources the ``iqua2`` fallback reads from; a binary that can
    draw on either exists as soon as one is present.
    """
    return _recharge_ui(device) is not None or _regeneration(device) is not None


def _wsov_exists(device: Device) -> bool:
    """Return whether the water-shutoff-valve binary applies to this device."""
    return _has_feature(device, "wsov")


# ---------------------------------------------------------------------------
# Entity description and table
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class AquaHomeBinarySensorDescription(BinarySensorEntityDescription):
    """Describe an AquaHome binary sensor and how to derive it from a device."""

    value_fn: Callable[[Device], bool | None]
    exists_fn: Callable[[Device], bool]


BINARY_SENSORS: tuple[AquaHomeBinarySensorDescription, ...] = (
    AquaHomeBinarySensorDescription(
        key="online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=resolve_device_online,
        exists_fn=lambda device: True,
    ),
    AquaHomeBinarySensorDescription(
        key="salt_level_alert",
        translation_key="salt_level_alert",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_status_value(lambda status: status.salt_level_alert),
        exists_fn=_status_present(lambda status: status.salt_level_alert),
    ),
    AquaHomeBinarySensorDescription(
        key="error_code_alert",
        translation_key="error_code_alert",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_status_value(lambda status: status.error_code_alert),
        exists_fn=_status_present(lambda status: status.error_code_alert),
    ),
    AquaHomeBinarySensorDescription(
        key="flow_monitor_alert",
        translation_key="flow_monitor_alert",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_status_value(lambda status: status.flow_monitor_alert),
        exists_fn=_status_present(lambda status: status.flow_monitor_alert),
    ),
    AquaHomeBinarySensorDescription(
        key="connection_alert",
        translation_key="connection_alert",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_status_value(lambda status: status.connection_alert),
        exists_fn=_status_present(lambda status: status.connection_alert),
    ),
    AquaHomeBinarySensorDescription(
        key="water_usage_alert",
        translation_key="water_usage_alert",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_status_value(lambda status: status.water_usage_alert),
        exists_fn=_status_present(lambda status: status.water_usage_alert),
    ),
    AquaHomeBinarySensorDescription(
        key="resin_alert",
        translation_key="resin_alert",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_status_value(lambda status: status.resin_alert),
        exists_fn=_status_present(lambda status: status.resin_alert),
    ),
    AquaHomeBinarySensorDescription(
        key="alarm_beeping",
        translation_key="alarm_beeping",
        device_class=BinarySensorDeviceClass.SOUND,
        value_fn=_status_value(lambda status: status.alarm_is_beeping),
        exists_fn=_alarm_beeping_exists,
    ),
    AquaHomeBinarySensorDescription(
        key="water_to_drain_alert",
        translation_key="water_to_drain_alert",
        device_class=BinarySensorDeviceClass.MOISTURE,
        value_fn=_status_value(lambda status: status.water_to_drain_alert),
        exists_fn=_water_to_drain_exists,
    ),
    AquaHomeBinarySensorDescription(
        key="regenerating",
        translation_key="regenerating",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=_recharge_or_regeneration_state_is("regenerating", "regenerating"),
        exists_fn=_recharge_or_regeneration_present,
    ),
    AquaHomeBinarySensorDescription(
        key="vacation_mode",
        translation_key="vacation_mode",
        value_fn=_recharge_state_is("vacation_mode"),
        exists_fn=_recharge_ui_present,
    ),
    AquaHomeBinarySensorDescription(
        key="recharge_off",
        translation_key="recharge_off",
        value_fn=_recharge_state_is("recharge_off"),
        exists_fn=_recharge_ui_present,
    ),
    AquaHomeBinarySensorDescription(
        key="regeneration_suspended",
        translation_key="regeneration_suspended",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_recharge_or_regeneration_state_is("suspended", "suspended"),
        exists_fn=_recharge_or_regeneration_present,
    ),
    AquaHomeBinarySensorDescription(
        key="wsov_closed",
        translation_key="wsov_closed",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_recharge_state_is("wsov_closed"),
        exists_fn=_wsov_exists,
    ),
)


# ---------------------------------------------------------------------------
# Per-leak-detector binaries (one sub-device per detector)
# ---------------------------------------------------------------------------


def _leak_detectors(device: Device) -> tuple[LeakDetector, ...]:
    """Return the device's paired leak detectors, or an empty tuple."""
    enriched = device.enriched_data
    if enriched is None or enriched.leak_detectors is None:
        return ()
    return enriched.leak_detectors.details


def _leak_flag_value(
    accessor: Callable[[LeakDetectorStatus], LeakDetectorFlag | None],
) -> Callable[[LeakDetectorStatus], bool | None]:
    """Build a value function reading one boolean flag off a detector status.

    Yields ``None`` when the flag object is absent, so a detector that omits a
    particular flag reports ``unknown`` for it rather than a fabricated state.
    """

    def _value(status: LeakDetectorStatus) -> bool | None:
        """Read the flag's boolean value, tolerating an absent flag object."""
        flag = accessor(status)
        return flag.value if flag is not None else None

    return _value


@dataclass(frozen=True, kw_only=True)
class AquaHomeLeakBinarySensorDescription(BinarySensorEntityDescription):
    """Describe one per-leak-detector binary and how to derive it.

    ``value_fn`` maps a detector's :class:`~.api.models.LeakDetectorStatus` to the
    flag state (``None`` when the backing flag is absent).
    """

    value_fn: Callable[[LeakDetectorStatus], bool | None]


LEAK_BINARY_SENSORS: tuple[AquaHomeLeakBinarySensorDescription, ...] = (
    AquaHomeLeakBinarySensorDescription(
        key="leak_detected",
        translation_key="leak_detected",
        device_class=BinarySensorDeviceClass.MOISTURE,
        value_fn=_leak_flag_value(lambda status: status.leak_detected),
    ),
    AquaHomeLeakBinarySensorDescription(
        key="leak_low_battery",
        translation_key="leak_low_battery",
        device_class=BinarySensorDeviceClass.BATTERY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_leak_flag_value(lambda status: status.low_battery),
    ),
    AquaHomeLeakBinarySensorDescription(
        key="leak_tampered",
        translation_key="leak_tampered",
        device_class=BinarySensorDeviceClass.TAMPER,
        value_fn=_leak_flag_value(lambda status: status.tampered),
    ),
    AquaHomeLeakBinarySensorDescription(
        key="leak_connectivity",
        translation_key="leak_connectivity",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_leak_flag_value(lambda status: status.is_connected),
    ),
)


# ---------------------------------------------------------------------------
# Entity and platform setup
# ---------------------------------------------------------------------------


class AquaHomeBinarySensor(AquaHomeEntity, BinarySensorEntity):
    """A single AquaHome binary sensor backed by a coordinator device."""

    entity_description: AquaHomeBinarySensorDescription
    _require_device_online: ClassVar[bool] = False

    @property
    def is_on(self) -> bool | None:
        """Return the current flag state, or ``None`` when it is unknown."""
        return self.entity_description.value_fn(self.coordinator.data)


class AquaHomeLeakBinarySensor(AquaHomeLeakDetectorEntity, BinarySensorEntity):
    """A single leak-detector binary backed by its detector's status block."""

    entity_description: AquaHomeLeakBinarySensorDescription

    @property
    def is_on(self) -> bool | None:
        """Return the flag state, or ``None`` when the detector/flag is absent."""
        detector = self.detector
        if detector is None or detector.status is None:
            return None
        return self.entity_description.value_fn(detector.status)


@callback
def _async_add_dynamic_binaries(
    entry: AquaHomeConfigEntry,
    coordinator: AquaHomeCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one device's binaries now and grow the set as capabilities appear.

    Both the feature-gated static binaries and each paired leak detector's four
    binaries are keyed uniquely and handed to
    :func:`~.dynamic.async_setup_dynamic_entities`, which creates the keys present
    at setup and adds later ones once seen :data:`~.const.CAPABILITY_DEBOUNCE_POLLS`
    consecutive polls.
    """

    def _discover() -> set[str]:
        """Return the binary keys present on the current device view."""
        device = coordinator.data
        keys = {
            description.key
            for description in BINARY_SENSORS
            if description.exists_fn(device)
        }
        for detector in _leak_detectors(device):
            keys.update(
                f"leak_{detector.detector_id}_{description.key}"
                for description in LEAK_BINARY_SENSORS
            )
        return keys

    def _create(keys: AbstractSet[str]) -> list[Entity]:
        """Build the binary entities whose keys are in ``keys``."""
        device = coordinator.data
        entities: list[Entity] = [
            AquaHomeBinarySensor(coordinator, description)
            for description in BINARY_SENSORS
            if description.key in keys
        ]
        for detector in _leak_detectors(device):
            entities.extend(
                AquaHomeLeakBinarySensor(coordinator, description, detector.detector_id)
                for description in LEAK_BINARY_SENSORS
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
    """Set up each device's binaries, growing the set as capabilities appear.

    Every device's fast telemetry coordinator drives a dynamic adder: the
    feature-gated binaries and any paired leak detector's binaries materialise as
    soon as the capability signature carries them, without a reload.
    """
    for coordinator in entry.runtime_data.coordinators.values():
        _async_add_dynamic_binaries(entry, coordinator, async_add_entities)
