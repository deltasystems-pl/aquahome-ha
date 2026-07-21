"""Binary sensor platform for the AquaHome integration.

Every Phase-2 binary is a *cloud-side* state — the connectivity flag itself and
the enriched water-treatment alert flags. These stay meaningful while the
softener is offline (a connection alert is precisely what the user needs then),
so the platform deliberately overrides
:attr:`AquaHomeEntity._require_device_online` to ``False``: gating these on
``device_online`` would suppress exactly the signals that matter during an
outage.

Which binaries exist for a given device is decided once at setup from the first
coordinator refresh, via each description's ``exists_fn``. The six plain alert
flags exist only when the enriched status block actually carries them; the two
feature-gated binaries (audible alarm, water-to-drain) exist when the device
advertises the matching feature or the field is present in the payload. On the
dev device (features ``["regeneration"]``) this yields the online binary plus
the six alert binaries; the two feature-gated binaries are absent.

The Tier-2 binaries derive the softener's recharge mode from the enriched
``recharge_ui`` state (with an ``iqua2`` fallback to the ``regeneration`` block on
hosts that omit ``recharge_ui``). They obey an offline-honesty rule: a
``recharge_ui`` whose ``state`` is ``"offline"`` — the cloud has lost the device —
tells us nothing about the underlying mode, so every derived binary reports
``unknown`` (``None``) in that case rather than a fabricated ``False``. The
water-shutoff-valve binary is feature-gated on ``"wsov"`` and thus absent on the
dev device, which advertises only ``["regeneration"]``.
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

from .api import Device, RechargeUi, RegenerationInfo, WaterTreatmentStatus
from .const import RECHARGE_STATE_OFFLINE
from .coordinator import resolve_device_online
from .entity import AquaHomeEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import AquaHomeConfigEntry


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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AquaHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the binary sensors that exist for each configured device."""
    entities: list[AquaHomeBinarySensor] = []
    for coordinator in entry.runtime_data.coordinators.values():
        device = coordinator.data
        entities.extend(
            AquaHomeBinarySensor(coordinator, description)
            for description in BINARY_SENSORS
            if description.exists_fn(device)
        )
    async_add_entities(entities)
