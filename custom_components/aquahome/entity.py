"""Base entity and device-registry wiring for AquaHome entities.

Every AquaHome entity is a :class:`~homeassistant.helpers.update_coordinator.CoordinatorEntity`
bound to a single device's :class:`~.coordinator.AquaHomeCoordinator`. This module
owns the two things all platforms share: the stable identity stem
(:func:`device_slug`, the source of both unique IDs and the device-registry
identifier) and the :class:`AquaHomeEntity` base that assembles the
:class:`~homeassistant.helpers.device_registry.DeviceInfo` and layers a
device-online availability rule on top of the coordinator's own health signal.

The coordinator imports :func:`device_slug` from here, so the coordinator type is
referenced by a forward reference in the generic base to keep the
entity<->coordinator import edge one-directional.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import DOMAIN, MANUFACTURER

if TYPE_CHECKING:
    from homeassistant.helpers.entity import EntityDescription

    from .api import Device
    from .coordinator import AquaHomeActivityCoordinator, AquaHomeCoordinator


def device_slug(device: Device) -> str:
    """Return the stable unique-id / device-identifier stem for a device.

    Slugifies the serial number, falling back to the opaque cloud device id when
    the serial is absent, so an entity's identity never depends on the account's
    unit preference or a mutable nickname.
    """
    return slugify(device.serial_number or device.id)


def build_device_info(device: Device) -> DeviceInfo:
    """Assemble the shared device-registry entry for an AquaHome device.

    Every entity base builds its ``DeviceInfo`` here so all platforms — the fast
    telemetry entities and the activity-coordinator-backed ones alike — register
    against the same device. Each enriched read is ``None``-safe because the
    enriched block is absent on feature-poor devices.
    """
    enriched = device.enriched_data
    model = enriched.model if enriched is not None else None
    return DeviceInfo(
        identifiers={(DOMAIN, device_slug(device))},
        serial_number=device.serial_number,
        manufacturer=MANUFACTURER,
        model=model or device.system_type_display,
        name=device.nickname or model or "AquaHome",
        sw_version=enriched.control_version if enriched is not None else None,
        hw_version=enriched.pwa if enriched is not None else None,
    )


class AquaHomeEntity(CoordinatorEntity["AquaHomeCoordinator"]):
    """Base entity bound to one AquaHome device coordinator.

    ``_require_device_online`` gates availability on the device's own online
    signal on top of coordinator health; subclasses that surface cloud-side
    state (which stays meaningful while the softener is offline) override it to
    ``False``.
    """

    _attr_has_entity_name = True
    _require_device_online: ClassVar[bool] = True

    def __init__(
        self, coordinator: AquaHomeCoordinator, description: EntityDescription
    ) -> None:
        """Bind the entity to its coordinator and description, building DeviceInfo.

        ``coordinator.data`` is a fully populated :class:`~.api.Device` here: the
        platform is only set up after the coordinator's first refresh succeeds.
        Every enriched read is ``None``-safe because the enriched block is absent
        on feature-poor devices.
        """
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.device_slug}_{description.key}"
        self._attr_device_info = build_device_info(coordinator.data)

    @property
    def available(self) -> bool:
        """Return whether the entity has trustworthy state to show.

        Combines the coordinator's last-update success with the device-online
        signal, unless ``_require_device_online`` is ``False`` (cloud-side state
        that stays valid while the device itself is offline).
        """
        return super().available and (
            not self._require_device_online or self.coordinator.device_online
        )


class AquaHomeActivityEntity(CoordinatorEntity["AquaHomeActivityCoordinator"]):
    """Base entity bound to one device's activity coordinator.

    Availability follows the activity coordinator's own update success only: the
    alert and regeneration history is cloud-side and stays valid while the
    softener itself is offline, so — unlike :class:`AquaHomeEntity` — there is no
    device-online gate here.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AquaHomeActivityCoordinator,
        description: EntityDescription,
        device: Device,
    ) -> None:
        """Bind the entity to its activity coordinator, description, and device.

        ``device`` is the paired fast coordinator's device view, used only to
        build the shared :class:`~homeassistant.helpers.device_registry.DeviceInfo`
        so the entity attaches to the same device as the telemetry entities.
        """
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.device_slug}_{description.key}"
        self._attr_device_info = build_device_info(device)
