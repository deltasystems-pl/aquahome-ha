"""Switch platform for AquaHome — boolean settings and the leak-scan control.

Two unrelated switch families live here, each on its own coordinator:

1. **Setting switches** — a device setting whose ``current_value`` is a JSON
   boolean (and which is neither a select nor a number). Built at runtime on the
   settings coordinator via :func:`~.dynamic.async_setup_dynamic_entities`; each
   is a :class:`~.entity.AquaHomeSettingsEntity` writing ``True``/``False`` back
   through the shared setting-write path. No boolean setting exists on the dev
   device, so this family is synthetic-fixture-tested only (live-unverified).

2. **The leak-detector scan switch** — a momentary "scan for detectors" control
   on the *fast* telemetry coordinator, present when the device advertises the
   ``leak_detector`` feature or already carries a ``leak_detectors`` block. It
   ships with untested hardware (no leak detector in the dev cohort). ``is_on``
   follows ``enriched.leak_detectors.is_scanning``, with an optimistic override
   after a start/stop command that decays after
   :data:`~.const.OPTIMISTIC_STATE_TTL_SECONDS` or as soon as the cloud reports a
   real scanning state — the same timer discipline the valve uses for motion.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later

from .command import async_execute_command
from .const import (
    CAPABILITY_DEBOUNCE_POLLS,
    FEATURE_LEAK_DETECTOR,
    OPTIMISTIC_STATE_TTL_SECONDS,
)
from .dynamic import async_setup_dynamic_entities
from .entity import AquaHomeEntity, AquaHomeSettingsEntity

if TYPE_CHECKING:
    from collections.abc import Set as AbstractSet
    from datetime import datetime
    from typing import Any

    from homeassistant.core import CALLBACK_TYPE, HomeAssistant
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .api import Device, DeviceSetting, DeviceSettingsDocument
    from .coordinator import (
        AquaHomeConfigEntry,
        AquaHomeCoordinator,
        AquaHomeSettingsCoordinator,
    )

# Writes serialize against the throttled cloud (Phase-4 contract).
PARALLEL_UPDATES = 1

#: Classification token this module claims (see :func:`_classify_setting`).
_PLATFORM = "switch"

#: Fast-coordinator key / command wiring for the leak-detector scan switch.
_LEAK_SCAN_KEY = "leak_detector_scan"
_LEAK_SCAN_FUNCTION = "leak_detector"
_LEAK_SCAN_START_ACTION = "start_scan"
_LEAK_SCAN_END_ACTION = "end_scan"

_LEAK_SCAN_DESCRIPTION = SwitchEntityDescription(
    key=_LEAK_SCAN_KEY,
    translation_key=_LEAK_SCAN_KEY,
    entity_category=EntityCategory.CONFIG,
)


def _classify_setting(setting: DeviceSetting) -> str | None:
    """Return the entity platform a setting maps to, or ``None`` for none.

    The shared Phase-4 classification rule, replicated verbatim in
    :mod:`.select`, :mod:`.number`, and :mod:`.switch` so all three agree on
    ownership: a ``select_rules`` block with at least one option is a *select*;
    otherwise a ``number_rules`` block is a *number*; otherwise a JSON-boolean
    ``current_value`` is a *switch*; anything else (text / multiselect / other)
    maps to no entity and is out of scope.
    """
    rules = setting.rules
    if (
        rules is not None
        and rules.select_rules is not None
        and rules.select_rules.options
    ):
        return "select"
    if rules is not None and rules.number_rules is not None:
        return "number"
    if isinstance(setting.current_value, bool):
        return "switch"
    return None


def _leak_scan_exists(device: Device) -> bool:
    """Report whether the leak-detector scan control applies to this device.

    True when the device advertises the ``leak_detector`` feature or already
    carries a ``leak_detectors`` block (gap-analysis "require either"). None-safe:
    an absent enriched block means no scan control.
    """
    enriched = device.enriched_data
    if enriched is None:
        return False
    return (
        FEATURE_LEAK_DETECTOR in enriched.features
        or enriched.leak_detectors is not None
    )


def _scanning(device: Device | None) -> bool | None:
    """Return the device's leak-scan state, or ``None`` when it is absent."""
    if device is None:
        return None
    enriched = device.enriched_data
    if enriched is None or enriched.leak_detectors is None:
        return None
    return enriched.leak_detectors.is_scanning


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AquaHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up both switch families for every device.

    Setting switches are wired per device on the settings coordinator (paired
    with the fast device view for the shared ``DeviceInfo``); the leak-scan
    switch is wired per device on the fast coordinator. Per-device helpers build
    the closures so each captures its own coordinator rather than the last loop
    iteration's.
    """
    runtime = entry.runtime_data
    for device_id, settings_coordinator in runtime.settings_coordinators.items():
        fast = runtime.coordinators.get(device_id)
        if fast is None:
            continue
        _async_setup_setting_switches(
            entry, settings_coordinator, fast.data, async_add_entities
        )
    for fast_coordinator in runtime.coordinators.values():
        _async_setup_leak_scan_switch(entry, fast_coordinator, async_add_entities)


@callback
def _async_setup_setting_switches(
    entry: AquaHomeConfigEntry,
    coordinator: AquaHomeSettingsCoordinator,
    device: Device,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Wire the dynamic setting-switch adder for one device's settings document.

    ``discover`` reports the names of every *visible* boolean setting; a
    conditionally hidden setting is not created. The document is authoritative,
    so the adder runs with ``debounce_polls=1``.
    """

    def _discover() -> set[str]:
        """Return the visible boolean-setting names in the latest document."""
        # ``data`` is typed non-optional but is ``None`` until the first refresh
        # succeeds (a tolerant settings fetch may not have) — keep the guard.
        document: DeviceSettingsDocument | None = coordinator.data
        if document is None:
            return set()
        return {
            setting.name
            for setting in document.settings
            if setting.name
            and _classify_setting(setting) == _PLATFORM
            and document.setting_visible(setting)
        }

    def _create(keys: AbstractSet[str]) -> list[Entity]:
        """Build a switch entity for each discovered setting name (sorted)."""
        entities: list[Entity] = [
            AquaHomeSettingSwitch(coordinator, device, name) for name in sorted(keys)
        ]
        return entities

    async_setup_dynamic_entities(
        entry,
        coordinator,
        async_add_entities,
        discover=_discover,
        create=_create,
        debounce_polls=1,
    )


@callback
def _async_setup_leak_scan_switch(
    entry: AquaHomeConfigEntry,
    coordinator: AquaHomeCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Wire the dynamic leak-scan switch adder for one device's fast coordinator.

    The single ``leak_detector_scan`` key exists once the leak-detector
    capability is present; on the throttled fast poll it is added only after
    :data:`~.const.CAPABILITY_DEBOUNCE_POLLS` consecutive sightings so a glitched
    payload cannot flap it into existence.
    """

    def _discover() -> set[str]:
        """Return the scan key when the leak-detector capability is present."""
        # ``data`` is typed non-optional but is ``None`` until the first refresh.
        device: Device | None = coordinator.data
        if device is not None and _leak_scan_exists(device):
            return {_LEAK_SCAN_KEY}
        return set()

    def _create(keys: AbstractSet[str]) -> list[Entity]:
        """Build the leak-scan switch for the discovered key."""
        entities: list[Entity] = [
            AquaHomeLeakScanSwitch(coordinator) for _ in sorted(keys)
        ]
        return entities

    async_setup_dynamic_entities(
        entry,
        coordinator,
        async_add_entities,
        discover=_discover,
        create=_create,
        debounce_polls=CAPABILITY_DEBOUNCE_POLLS,
    )


class AquaHomeSettingSwitch(AquaHomeSettingsEntity, SwitchEntity):
    """A switch for one boolean device setting (write-through the settings API)."""

    @property
    def is_on(self) -> bool | None:
        """Return the boolean current value, or ``None`` when unavailable."""
        setting = self.setting
        if setting is None or not isinstance(setting.current_value, bool):
            return None
        return setting.current_value

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Write ``True`` to the setting."""
        await self._async_write(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Write ``False`` to the setting."""
        await self._async_write(False)


class AquaHomeLeakScanSwitch(AquaHomeEntity, SwitchEntity):
    """The leak-detector scan switch on the fast telemetry coordinator.

    ``is_on`` normally follows the polled ``is_scanning`` flag. Because the scan
    command is fire-and-forget and its effect only surfaces on a later poll, a
    successful start/stop sets an optimistic override that is cleared by whichever
    comes first: a coordinator update carrying a real (non-``None``)
    ``is_scanning`` state, or a :data:`~.const.OPTIMISTIC_STATE_TTL_SECONDS`
    fallback timer.
    """

    entity_description: SwitchEntityDescription

    def __init__(self, coordinator: AquaHomeCoordinator) -> None:
        """Bind the scan switch to its fast coordinator."""
        super().__init__(coordinator, _LEAK_SCAN_DESCRIPTION)
        #: Optimistic override while a start/stop settles; ``None`` when the
        #: polled state is authoritative again.
        self._optimistic: bool | None = None
        #: Canceller for the pending optimistic-decay timer, if any.
        self._unsub_optimistic: CALLBACK_TYPE | None = None

    @property
    def is_on(self) -> bool | None:
        """Return the optimistic override when active, else the polled state."""
        if self._optimistic is not None:
            return self._optimistic
        return _scanning(self.coordinator.data)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start a leak-detector scan and show it optimistically."""
        await async_execute_command(
            self.coordinator.client,
            self.coordinator.device_id,
            _LEAK_SCAN_FUNCTION,
            _LEAK_SCAN_START_ACTION,
        )
        self._set_optimistic(value=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """End a leak-detector scan and show it optimistically."""
        await async_execute_command(
            self.coordinator.client,
            self.coordinator.device_id,
            _LEAK_SCAN_FUNCTION,
            _LEAK_SCAN_END_ACTION,
        )
        self._set_optimistic(value=False)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Drop the optimistic override once the cloud reports a real state."""
        if _scanning(self.coordinator.data) is not None:
            self._clear_optimistic()
        super()._handle_coordinator_update()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel any pending optimistic-decay timer on removal."""
        self._cancel_optimistic_timer()
        await super().async_will_remove_from_hass()

    @callback
    def _set_optimistic(self, *, value: bool) -> None:
        """Show ``value`` optimistically and (re)arm the decay timer."""
        self._cancel_optimistic_timer()
        self._optimistic = value
        self.async_write_ha_state()
        self._unsub_optimistic = async_call_later(
            self.hass, OPTIMISTIC_STATE_TTL_SECONDS, self._optimistic_expired
        )

    @callback
    def _optimistic_expired(self, _now: datetime) -> None:
        """Fall back to the polled state when the optimistic window elapses."""
        self._unsub_optimistic = None
        self._optimistic = None
        self.async_write_ha_state()

    @callback
    def _clear_optimistic(self) -> None:
        """Cancel the decay timer and drop the optimistic override."""
        self._cancel_optimistic_timer()
        self._optimistic = None

    @callback
    def _cancel_optimistic_timer(self) -> None:
        """Cancel the pending optimistic-decay timer, if one is armed."""
        if self._unsub_optimistic is not None:
            self._unsub_optimistic()
            self._unsub_optimistic = None
