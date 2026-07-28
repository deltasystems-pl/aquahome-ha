"""Switch platform for AquaHome — settings, leak scan, automation, live mode.

Four unrelated switch families live here, each on its own coordinator:

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

3. **The automation switches** — the three per-device opt-ins of the automation
   tier (vacation deferral, auto vacation, smart regeneration) on that device's
   :class:`~.scheduler.AquaHomeRegenScheduler`. Nothing gates their existence:
   the scheduler runs for every device, so all three are created unconditionally
   and start OFF, which is what makes every device-affecting automation opt-in.
   Unlike every other entity in this module they are *always available* — the
   flags are the user's own preference, persisted in the config entry, not cloud
   state — and every write goes through the scheduler's public API so exactly
   one code path persists a flag and performs its device-side side effect.

4. **The live-mode switches** — the manual live hold plus the two live-mode
   opt-ins (analytics-driven windows, continuous flow) on that device's
   :class:`~.live.AquaHomeLiveManager`. Like the automation switches they exist
   for every device, start OFF, are always available, and write only through the
   manager's public API — the single owner of the websocket lifecycle and of the
   configuration persisted in the config entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .command import async_execute_command
from .const import (
    CAPABILITY_DEBOUNCE_POLLS,
    DEFERRAL_SOURCE_MANUAL,
    FEATURE_LEAK_DETECTOR,
    OPTIMISTIC_STATE_TTL_SECONDS,
)
from .dynamic import async_setup_dynamic_entities
from .entity import AquaHomeEntity, AquaHomeSettingsEntity, build_device_info

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from collections.abc import Set as AbstractSet
    from datetime import datetime
    from typing import Any

    from homeassistant.core import CALLBACK_TYPE, HomeAssistant
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .api import Device, DeviceSetting, DeviceSettingsDocument
    from .automation_state import AutomationState
    from .coordinator import (
        AquaHomeConfigEntry,
        AquaHomeCoordinator,
        AquaHomeSettingsCoordinator,
    )
    from .live import AquaHomeLiveManager
    from .live_state import LiveState
    from .scheduler import AquaHomeRegenScheduler

# Writes serialize against the throttled cloud.
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


# ---------------------------------------------------------------------------
# Automation switches
#
# One description per opt-in flag: how it reads out of the published
# AutomationState, how a change is applied through the scheduler, and what
# bookkeeping it exposes. Everything the user can see about an automation
# decision comes from these three attribute sets.
# ---------------------------------------------------------------------------

#: Description keys of the three automation switches (also their unique-id and
#: translation keys).
_VACATION_DEFERRAL_KEY = "vacation_deferral"
_AUTO_VACATION_KEY = "auto_vacation"
_SMART_REGENERATION_KEY = "smart_regeneration"


def _vacation_deferral_on(state: AutomationState) -> bool:
    """Return whether a vacation deferral is currently active."""
    return state.vacation_deferral


def _auto_vacation_on(state: AutomationState) -> bool:
    """Return whether the deferral follows the vacation detector."""
    return state.auto_vacation


def _smart_regeneration_on(state: AutomationState) -> bool:
    """Return whether the nightly regeneration scheduler is enabled."""
    return state.smart_regeneration


async def _async_set_vacation_deferral(
    scheduler: AquaHomeRegenScheduler, enabled: bool
) -> None:
    """Start or end the vacation deferral as a *manual* act.

    Both the switch and the ``set_vacation_mode`` action land here, so a
    deferral a person started is always recorded as manual and therefore never
    released again by the auto-vacation follower.
    """
    await scheduler.async_set_vacation_deferral(enabled, source=DEFERRAL_SOURCE_MANUAL)


async def _async_set_auto_vacation(
    scheduler: AquaHomeRegenScheduler, enabled: bool
) -> None:
    """Enable or disable following the vacation detector automatically."""
    await scheduler.async_set_auto_vacation(enabled)


async def _async_set_smart_regeneration(
    scheduler: AquaHomeRegenScheduler, enabled: bool
) -> None:
    """Enable or disable the nightly capacity-versus-forecast scheduler."""
    await scheduler.async_set_smart_regeneration(enabled)


def _deferral_attributes(state: AutomationState) -> dict[str, Any]:
    """Return who started the deferral, when, and how long it has run.

    All three keys are always present — ``None`` while no deferral is active —
    so a template written against a running deferral keeps evaluating once it
    ends. ``days_deferred`` counts whole elapsed days, which is the same unit
    the resin-hygiene cap (:data:`~.const.REGEN_DEFERRAL_MAX_DAYS`) is measured
    in, so the attribute says how close the deferral is to letting a
    regeneration through.
    """
    started = state.deferral_started
    return {
        "deferral_source": state.deferral_source,
        "deferral_started": started.isoformat() if started is not None else None,
        "days_deferred": (
            (dt_util.utcnow() - dt_util.as_utc(started)).days
            if started is not None
            else None
        ),
    }


def _decision_attributes(state: AutomationState) -> dict[str, Any]:
    """Return the scheduler's latest verdict and when it was taken.

    The scheduler records a verdict on every analytics pass — an action or one
    of its ``skipped_*`` literals — so a night that passed without a
    regeneration always explains itself here rather than in the debug log.
    Both keys stay present and read ``None`` until the first pass runs.
    """
    decided_at = state.last_decision_at
    return {
        "last_decision": state.last_decision,
        "last_decision_at": (
            decided_at.isoformat() if decided_at is not None else None
        ),
    }


@dataclass(frozen=True, kw_only=True)
class AquaHomeAutomationSwitchDescription(SwitchEntityDescription):
    """Describe one automation switch: how it reads and how it writes.

    ``value_fn`` picks the flag out of the scheduler's published
    :class:`~.automation_state.AutomationState`; ``set_fn`` applies a change
    through the scheduler's public API (never by writing the options directly,
    so a flag with a device-side side effect always performs it); and
    ``attributes_fn`` — when set — exposes the bookkeeping behind the flag.
    """

    value_fn: Callable[[AutomationState], bool]
    set_fn: Callable[[AquaHomeRegenScheduler, bool], Coroutine[Any, Any, None]]
    attributes_fn: Callable[[AutomationState], dict[str, Any]] | None = None


#: The three per-device automation opt-ins, all default-off. Only the deferral
#: is a primary control (it is the one a user reaches for when leaving); the
#: other two configure how the automation behaves and are categorised as such.
AUTOMATION_SWITCHES: tuple[AquaHomeAutomationSwitchDescription, ...] = (
    AquaHomeAutomationSwitchDescription(
        key=_VACATION_DEFERRAL_KEY,
        translation_key=_VACATION_DEFERRAL_KEY,
        icon="mdi:calendar-remove",
        entity_registry_enabled_default=True,
        value_fn=_vacation_deferral_on,
        set_fn=_async_set_vacation_deferral,
        attributes_fn=_deferral_attributes,
    ),
    AquaHomeAutomationSwitchDescription(
        key=_AUTO_VACATION_KEY,
        translation_key=_AUTO_VACATION_KEY,
        icon="mdi:home-export-outline",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=True,
        value_fn=_auto_vacation_on,
        set_fn=_async_set_auto_vacation,
    ),
    AquaHomeAutomationSwitchDescription(
        key=_SMART_REGENERATION_KEY,
        translation_key=_SMART_REGENERATION_KEY,
        icon="mdi:auto-fix",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=True,
        value_fn=_smart_regeneration_on,
        set_fn=_async_set_smart_regeneration,
        attributes_fn=_decision_attributes,
    ),
)


# ---------------------------------------------------------------------------
# Live-mode switches
#
# One description per control: how it reads out of the manager's published
# LiveState, and how a change is applied through the manager. The manual hold is
# runtime-only (a restart must never leave a forgotten socket held open); the
# other two are persisted configuration.
# ---------------------------------------------------------------------------

#: Description keys of the three live-mode switches (also their unique-id and
#: translation keys).
_LIVE_VIEW_KEY = "live_view"
_SMART_LIVE_WINDOWS_KEY = "smart_live_windows"
_CONTINUOUS_LIVE_FLOW_KEY = "continuous_live_flow"


def _live_view_on(state: LiveState) -> bool:
    """Return whether the manual live hold is currently requested."""
    return state.live_view


def _smart_live_windows_on(state: LiveState) -> bool:
    """Return whether analytics-driven live windows are enabled."""
    return state.config.smart_windows


def _continuous_live_flow_on(state: LiveState) -> bool:
    """Return whether the continuous live hold is enabled."""
    return state.config.continuous


async def _async_set_live_view(manager: AquaHomeLiveManager, enabled: bool) -> None:
    """Request or release the manual live hold."""
    await manager.async_set_live_view(enabled)


async def _async_set_smart_windows(manager: AquaHomeLiveManager, enabled: bool) -> None:
    """Enable or disable the analytics-driven live windows."""
    await manager.async_set_smart_windows(enabled)


async def _async_set_continuous(manager: AquaHomeLiveManager, enabled: bool) -> None:
    """Enable or disable the continuous live hold."""
    await manager.async_set_continuous(enabled)


def _live_session_attributes(state: LiveState) -> dict[str, Any]:
    """Return what the live session currently covering this device is doing.

    All three keys are always present — ``None`` / ``0`` while nothing is
    streaming — so a template written against a running session keeps evaluating
    once it ends. ``source`` names the trigger that opened the session, which is
    not necessarily this switch: a hold requested while another trigger already
    streams is absorbed by that session rather than opening a second socket.
    ``windows_in_session`` counts the reporting-window renewals spent since the
    session was granted; the device fast-reports for roughly three minutes per
    window, so a hold kept open for a while renews repeatedly.
    """
    started = state.session_started
    return {
        "source": state.source,
        "session_started": started.isoformat() if started is not None else None,
        "windows_in_session": state.windows_in_session,
    }


@dataclass(frozen=True, kw_only=True)
class AquaHomeLiveSwitchDescription(SwitchEntityDescription):
    """Describe one live-mode switch: how it reads and how it writes.

    ``value_fn`` picks the flag out of the manager's published
    :class:`~.live_state.LiveState`; ``set_fn`` applies a change through the
    manager's public API, which is the only place a websocket hold is requested
    or released and the only place a live-mode flag is persisted; and
    ``attributes_fn`` — when set — exposes the session bookkeeping behind the
    flag.
    """

    value_fn: Callable[[LiveState], bool]
    set_fn: Callable[[AquaHomeLiveManager, bool], Coroutine[Any, Any, None]]
    attributes_fn: Callable[[LiveState], dict[str, Any]] | None = None


#: The three per-device live-mode controls, all default-off. Live view is the
#: primary control — it is what a user reaches for to watch water use as it
#: happens — while the other two configure when live mode runs on its own and
#: are categorised as configuration. Continuous flow is the advanced one: it
#: holds a session open indefinitely, which costs one ticket per reporting
#: window (about one every five minutes) and keeps a websocket open against the
#: vendor's cloud for as long as it is on.
LIVE_SWITCHES: tuple[AquaHomeLiveSwitchDescription, ...] = (
    AquaHomeLiveSwitchDescription(
        key=_LIVE_VIEW_KEY,
        translation_key=_LIVE_VIEW_KEY,
        icon="mdi:eye",
        entity_registry_enabled_default=True,
        value_fn=_live_view_on,
        set_fn=_async_set_live_view,
        attributes_fn=_live_session_attributes,
    ),
    AquaHomeLiveSwitchDescription(
        key=_SMART_LIVE_WINDOWS_KEY,
        translation_key=_SMART_LIVE_WINDOWS_KEY,
        icon="mdi:eye-refresh",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=True,
        value_fn=_smart_live_windows_on,
        set_fn=_async_set_smart_windows,
    ),
    AquaHomeLiveSwitchDescription(
        key=_CONTINUOUS_LIVE_FLOW_KEY,
        translation_key=_CONTINUOUS_LIVE_FLOW_KEY,
        icon="mdi:waves-arrow-right",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=True,
        value_fn=_continuous_live_flow_on,
        set_fn=_async_set_continuous,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AquaHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up all four switch families for every device.

    Setting switches are wired per device on the settings coordinator (paired
    with the fast device view for the shared ``DeviceInfo``); the leak-scan
    switch is wired per device on the fast coordinator. Per-device helpers build
    the closures so each captures its own coordinator rather than the last loop
    iteration's. The automation and live-mode switches need no discovery at
    all — every device has a scheduler and a live manager — so they are added
    straight away, again paired with the fast device view for their
    ``DeviceInfo``.
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
    automation_switches: list[AquaHomeAutomationSwitch] = []
    for device_id, scheduler in runtime.schedulers.items():
        fast = runtime.coordinators.get(device_id)
        if fast is None:
            continue
        automation_switches.extend(_automation_switches(scheduler, fast.data))
    async_add_entities(automation_switches)
    live_switches: list[AquaHomeLiveSwitch] = []
    for device_id, manager in runtime.live_managers.items():
        fast = runtime.coordinators.get(device_id)
        if fast is None:
            continue
        live_switches.extend(_live_switches(manager, fast.data))
    async_add_entities(live_switches)


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


def _automation_switches(
    scheduler: AquaHomeRegenScheduler, device: Device
) -> list[AquaHomeAutomationSwitch]:
    """Build one device's three automation switches.

    The vacation-deferral switch gets its own class because the
    ``set_vacation_mode`` action identifies its target by class; the other two
    need no behaviour beyond their description.
    """
    entities: list[AquaHomeAutomationSwitch] = []
    for description in AUTOMATION_SWITCHES:
        entity_class = (
            AquaHomeVacationDeferralSwitch
            if description.key == _VACATION_DEFERRAL_KEY
            else AquaHomeAutomationSwitch
        )
        entities.append(entity_class(scheduler, description, device))
    return entities


def _live_switches(
    manager: AquaHomeLiveManager, device: Device
) -> list[AquaHomeLiveSwitch]:
    """Build one device's three live-mode switches."""
    return [
        AquaHomeLiveSwitch(manager, description, device)
        for description in LIVE_SWITCHES
    ]


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


class AquaHomeAutomationSwitch(
    CoordinatorEntity["AquaHomeRegenScheduler"], SwitchEntity
):
    """One opt-in automation flag on a device's regeneration scheduler.

    The switch is a view onto the scheduler's
    :class:`~.automation_state.AutomationState`: turning it on or off calls the
    scheduler's public setter, which persists the flag into the config entry's
    options and republishes the state, and the entity re-renders from that
    published state like any other coordinator entity. Nothing is written here
    directly, so a flag set by the switch, by an action, or by a confirmed
    repair suggestion behaves identically.
    """

    _attr_has_entity_name = True
    entity_description: AquaHomeAutomationSwitchDescription

    def __init__(
        self,
        coordinator: AquaHomeRegenScheduler,
        description: AquaHomeAutomationSwitchDescription,
        device: Device,
    ) -> None:
        """Bind the switch to its scheduler, description, and device view.

        ``device`` is the paired fast coordinator's device view, used only to
        build the shared :class:`~homeassistant.helpers.device_registry.DeviceInfo`
        so the switch attaches to the same device as the telemetry entities.
        """
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.device_slug}_{description.key}"
        self._attr_device_info = build_device_info(device)

    @property
    def available(self) -> bool:
        """Return ``True`` unconditionally — an opt-in is local, not cloud state.

        Every other entity in this integration is gated on the cloud poll (and
        most on the device being online) because they render what the device
        reports. These three render what the *user asked for*, held in the
        config entry's options, so they must stay operable while the cloud is
        unreachable or the softener is offline — an outage must never strand an
        automation the owner wants to switch off.
        """
        return True

    @property
    def is_on(self) -> bool:
        """Return the flag's current value from the published automation state."""
        return self.entity_description.value_fn(self.coordinator.state)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the bookkeeping behind the flag, or ``None`` when it has none."""
        attributes_fn = self.entity_description.attributes_fn
        if attributes_fn is None:
            return None
        return attributes_fn(self.coordinator.state)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the automation this switch opts into."""
        await self.entity_description.set_fn(self.coordinator, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the automation this switch opts into."""
        await self.entity_description.set_fn(self.coordinator, False)


class AquaHomeVacationDeferralSwitch(AquaHomeAutomationSwitch):
    """The vacation-deferral switch, and the ``set_vacation_mode`` target.

    The action layer identifies its target by this class, so a call aimed at
    any other switch is rejected with a translated error rather than silently
    doing nothing. :meth:`async_set_vacation_mode` is deliberately the same
    call the switch itself makes: an action, a blueprint, and a tap on the
    switch all record a *manual* deferral, which the auto-vacation follower is
    then not allowed to release on the household's behalf.
    """

    async def async_set_vacation_mode(self, vacation: bool) -> None:
        """Start or end the vacation deferral on the user's behalf."""
        await _async_set_vacation_deferral(self.coordinator, vacation)


class AquaHomeLiveSwitch(CoordinatorEntity["AquaHomeLiveManager"], SwitchEntity):
    """One live-mode control on a device's live manager.

    A view onto the manager's :class:`~.live_state.LiveState`, in the same shape
    as :class:`AquaHomeAutomationSwitch` is a view onto the scheduler's state:
    turning the switch on or off calls the manager's public setter, which
    requests or releases the websocket hold, persists the two configuration
    flags into the config entry's options, and republishes the state the entity
    re-renders from.

    Turning a hold on is a *request*, not a guarantee. The manager grants it
    only when a session may run — the device is online, the day's session budget
    is not spent, the minimum gap since the previous session has elapsed — and a
    refused request leaves the flag on so the hold starts as soon as the gate
    opens. This switch therefore reports what was asked for; the live-mode
    status sensor reports what is actually running.
    """

    _attr_has_entity_name = True
    entity_description: AquaHomeLiveSwitchDescription

    def __init__(
        self,
        coordinator: AquaHomeLiveManager,
        description: AquaHomeLiveSwitchDescription,
        device: Device,
    ) -> None:
        """Bind the switch to its live manager, description, and device view.

        ``device`` is the paired fast coordinator's device view, used only to
        build the shared :class:`~homeassistant.helpers.device_registry.DeviceInfo`
        so the switch attaches to the same device as the telemetry entities.
        """
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.device_slug}_{description.key}"
        self._attr_device_info = build_device_info(device)

    @property
    def available(self) -> bool:
        """Return ``True`` unconditionally — the flag is local, not cloud state.

        Like the automation opt-ins, these switches render what the *user asked
        for*, held in the manager (and, for the two configuration flags, in the
        config entry's options), not what the device reports. They must stay
        operable while the cloud is unreachable or the softener is offline, so
        an outage can never strand a live hold the owner wants to switch off.
        """
        return True

    @property
    def is_on(self) -> bool:
        """Return the flag's current value from the published live state."""
        return self.entity_description.value_fn(self.coordinator.state)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the session bookkeeping, or ``None`` when the flag has none."""
        attributes_fn = self.entity_description.attributes_fn
        if attributes_fn is None:
            return None
        return attributes_fn(self.coordinator.state)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the live-mode behaviour this switch controls."""
        await self.entity_description.set_fn(self.coordinator, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the live-mode behaviour this switch controls."""
        await self.entity_description.set_fn(self.coordinator, False)
