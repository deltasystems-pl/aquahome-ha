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

from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .api import ApiError, AquaHomeConnectionError, AuthError, RateLimitError
from .const import DISPLAY_PREFERENCE_SETTINGS, DOMAIN, MANUFACTURER

if TYPE_CHECKING:
    from homeassistant.helpers.entity import EntityDescription

    from .analytics.engine import AquaHomeAnalyticsEngine
    from .api import Device, DeviceSetting, DeviceSettingsDocument, LeakDetector
    from .coordinator import (
        AquaHomeActivityCoordinator,
        AquaHomeCoordinator,
        AquaHomeSettingsCoordinator,
    )


def device_slug(device: Device) -> str:
    """Return the stable unique-id / device-identifier stem for a device.

    Slugifies the serial number, falling back to the opaque cloud device id when
    the serial is absent, so an entity's identity never depends on the account's
    unit preference or a mutable nickname.
    """
    return slugify(device.serial_number or device.id)


def device_display_name(device: Device) -> str:
    """Return the human-facing device name (nickname, then model, then brand).

    The single source of the naming fallback chain: the device-registry card,
    the external-statistics metadata, and the low-salt repair issue all render
    a device through this function so the three can never drift apart.
    """
    enriched = device.enriched_data
    model = enriched.model if enriched is not None else None
    return device.nickname or model or "AquaHome"


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
        name=device_display_name(device),
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


class AquaHomeAnalyticsEntity(CoordinatorEntity["AquaHomeAnalyticsEngine"]):
    """Base entity bound to one device's analytics engine.

    Availability follows the engine's own update success only: every analytics
    verdict is derived from imported statistics and stays meaningful while the
    softener itself is offline (a leak verdict matters most then), so — like
    :class:`AquaHomeActivityEntity` — there is no device-online gate here.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AquaHomeAnalyticsEngine,
        description: EntityDescription,
        device: Device,
    ) -> None:
        """Bind the entity to its analytics engine, description, and device.

        ``device`` is the paired fast coordinator's device view, used only to
        build the shared :class:`~homeassistant.helpers.device_registry.DeviceInfo`
        so the entity attaches to the same device as the telemetry entities.
        """
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.device_slug}_{description.key}"
        self._attr_device_info = build_device_info(device)


class AquaHomeSettingsEntity(CoordinatorEntity["AquaHomeSettingsCoordinator"]):
    """Base entity for one rule-driven device setting (select / number / switch).

    Unlike the static platforms, a setting's display name is the server-localized
    ``label``: it is set at construction and refreshed on every coordinator update
    so a language change follows the account. Availability additionally honours the
    document's conditional visibility — a setting hidden by another setting's value
    is present but unavailable, never silently unreachable. The account/display
    preference settings (:data:`~.const.DISPLAY_PREFERENCE_SETTINGS`) are created
    like any other but registry-disabled by default, since our sensors bind fixed
    conversions and toggling them only affects the phone app.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: AquaHomeSettingsCoordinator,
        device: Device,
        setting_name: str,
    ) -> None:
        """Bind the entity to its settings coordinator and one setting name.

        ``device`` is the paired telemetry device view, used only to build the
        shared :class:`~homeassistant.helpers.device_registry.DeviceInfo` so the
        setting attaches to the same device as the telemetry entities.
        """
        super().__init__(coordinator)
        self._setting_name = setting_name
        self._attr_unique_id = f"{coordinator.device_slug}_setting_{setting_name}"
        self._attr_device_info = build_device_info(device)
        self._attr_entity_registry_enabled_default = (
            setting_name not in DISPLAY_PREFERENCE_SETTINGS
        )
        setting = self.setting
        if setting is not None:
            self._attr_name = setting.label

    @property
    def setting(self) -> DeviceSetting | None:
        """Return the current parsed setting, or ``None`` when it is absent."""
        # ``data`` is populated once the first refresh succeeds, but stays typed
        # as the document — annotate it optional so the pre-data guard survives.
        document: DeviceSettingsDocument | None = self.coordinator.data
        if document is None:
            return None
        return document.get(self._setting_name)

    @property
    def available(self) -> bool:
        """Return whether the setting is present and currently visible.

        Combines the coordinator's update health with the setting existing in the
        latest document and being visible under its conditional rules; a hidden or
        vanished setting is unavailable rather than showing a stale value.
        """
        if not super().available:
            return False
        document: DeviceSettingsDocument | None = self.coordinator.data
        if document is None:
            return False
        setting = document.get(self._setting_name)
        return setting is not None and document.setting_visible(setting)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Refresh the localized display name before propagating the update."""
        setting = self.setting
        if setting is not None:
            self._attr_name = setting.label
        super()._handle_coordinator_update()

    async def _async_write(self, value: bool | int | float | str) -> None:
        """Write one setting value, mapping API failures to user-facing errors.

        The setting must still exist (a conditionally hidden or removed setting
        cannot be written); every backend failure is surfaced as a translated
        :class:`~homeassistant.exceptions.HomeAssistantError`.
        """
        if self.setting is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="setting_unavailable"
            )
        try:
            await self.coordinator.async_write_setting(self._setting_name, value)
        except RateLimitError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="rate_limited"
            ) from err
        except AuthError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except AquaHomeConnectionError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="cannot_connect"
            ) from err
        except ApiError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="setting_rejected",
                translation_placeholders={"message": str(err)},
            ) from err


class AquaHomeLeakDetectorEntity(CoordinatorEntity["AquaHomeCoordinator"]):
    """Base entity for one leak detector, registered as its own sub-device.

    Each detector becomes a device in its own right (``via_device`` the softener)
    so its four binaries, temperature, and signal-strength sensors group together
    under the detector's nickname. The detector is looked up by id on every access
    and is ``None``-safe: an unpaired or vanished detector makes the entity
    unavailable rather than raising.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AquaHomeCoordinator,
        description: EntityDescription,
        detector_id: int,
    ) -> None:
        """Bind the entity to a coordinator, description, and detector id."""
        super().__init__(coordinator)
        self.entity_description = description
        self._detector_id = detector_id
        self._attr_unique_id = (
            f"{coordinator.device_slug}_leak_{detector_id}_{description.key}"
        )
        detector = self.detector
        name = (
            detector.nickname
            if detector is not None and detector.nickname
            else f"Leak detector {detector_id}"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.device_slug}_leak_{detector_id}")},
            via_device=(DOMAIN, coordinator.device_slug),
            manufacturer=MANUFACTURER,
            name=name,
            model="Leak detector",
        )

    @property
    def detector(self) -> LeakDetector | None:
        """Return this entity's leak detector, or ``None`` when it is absent."""
        device: Device | None = self.coordinator.data
        if device is None:
            return None
        enriched = device.enriched_data
        if enriched is None or enriched.leak_detectors is None:
            return None
        for detector in enriched.leak_detectors.details:
            if detector.detector_id == self._detector_id:
                return detector
        return None

    @property
    def available(self) -> bool:
        """Return whether the softener is online and this detector is present."""
        return (
            super().available
            and self.coordinator.device_online
            and self.detector is not None
        )
