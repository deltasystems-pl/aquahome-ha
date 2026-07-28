"""Button platform for the AquaHome integration.

Every button issues a single fire-and-forget device command through
:func:`~.command.async_execute_command`, which maps the client's typed error
taxonomy onto user-facing :class:`~homeassistant.exceptions.HomeAssistantError`
translations. A button has no state of its own — the effect of a command surfaces
on a later coordinator poll — so the only per-device decisions are *which* buttons
exist and *when* one is available, expressed with the same description-table and
``exists_fn`` / ``available_fn`` accessor idiom the binary-sensor platform uses.

Which buttons exist for a device is discovered from the coordinator's device
view. The set present at setup is created immediately;
:func:`~.dynamic.async_setup_dynamic_entities` then grows it (debounced
:data:`~.const.CAPABILITY_DEBOUNCE_POLLS` polls) when hardware added later — an
audible alarm or a water-shutoff valve — first advertises the matching feature,
and never removes one (vanished hardware goes unavailable via each button's
online gate, not deleted). The three regeneration controls exist when the device
advertises the ``regeneration`` feature or carries a ``recharge_ui`` /
``regeneration`` block; the silence-alarm button when the ``audible_alarm``
feature or the ``alarm_is_beeping`` flag is present; the shutoff-valve error
reset when the ``wsov`` feature or the valve block is present. Refresh-data and
the two advanced reset tools always exist. On the dev device (features
``["regeneration"]``) this yields the three regeneration buttons plus refresh
data, advance valve, and reset error code; silence alarm and the shutoff-valve
reset are absent, as are the gated recharge-mode buttons.

Availability layers the recharge-tile guidance on top of the shared online gate:
``regenerate_now`` and ``schedule_regeneration`` are unavailable only when the
device explicitly reports ``can_recharge`` / ``can_schedule`` as ``False``
(``recharge_ui`` first, falling back to the ``regeneration`` block; an absent flag
is treated as allowed rather than second-guessing the cloud).

``refresh_data`` sends the ``get_all_data`` command, which asks the *device* to
push fresh state to the cloud, then schedules one follow-up coordinator poll after
:data:`~.const.REFRESH_BUTTON_POLL_DELAY_SECONDS` so the entities reflect the
pushed values without waiting for the next scheduled poll; the timer is cancelled
if the entity is removed first.

The three recharge-mode buttons (vacation mode, recharge off, enable recharge)
are advertised by the ``recharge_ui`` tile, but their ``/command`` payload mapping
is undocumented and unverified (no capture of the official app sending them
exists yet). They are implemented but
gated behind :data:`~.const.RECHARGE_ACTION_COMMANDS_VERIFIED`: while it is
``False`` they are excluded from the table the platform iterates and so are never
created. The supervised live test at the end of the phase proves the payloads and
flips the gate.

``PARALLEL_UPDATES = 1`` serialises presses against the throttled cloud.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later

from .api import Device, RechargeUi, RegenerationInfo
from .api.client import DEFAULT_COMMAND_ACTION
from .command import async_execute_command
from .const import (
    CAPABILITY_DEBOUNCE_POLLS,
    FEATURE_AUDIBLE_ALARM,
    FEATURE_REGENERATION,
    FEATURE_WSOV,
    RECHARGE_ACTION_COMMANDS_VERIFIED,
    REFRESH_BUTTON_POLL_DELAY_SECONDS,
)
from .dynamic import async_setup_dynamic_entities
from .entity import AquaHomeEntity

if TYPE_CHECKING:
    from collections.abc import Set as AbstractSet
    from datetime import datetime

    from homeassistant.core import CALLBACK_TYPE, HomeAssistant
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import AquaHomeConfigEntry, AquaHomeCoordinator

#: Serialise presses: the cloud command endpoint is aggressively throttled.
PARALLEL_UPDATES = 1


# ---------------------------------------------------------------------------
# None-safe payload accessors
# ---------------------------------------------------------------------------


def _has_feature(device: Device, feature: str) -> bool:
    """Return whether the device advertises ``feature`` in its enriched data."""
    enriched = device.enriched_data
    return enriched is not None and feature in enriched.features


def _recharge_ui(device: Device) -> RechargeUi | None:
    """Return the enriched ``recharge_ui`` block, or ``None`` when absent."""
    enriched = device.enriched_data
    return enriched.recharge_ui if enriched is not None else None


def _regeneration(device: Device) -> RegenerationInfo | None:
    """Return the enriched ``regeneration`` block, or ``None`` when absent."""
    enriched = device.enriched_data
    return enriched.regeneration if enriched is not None else None


def _always_exists(device: Device) -> bool:
    """Return ``True`` — a device tool that applies to every softener."""
    return True


def _regeneration_control_exists(device: Device) -> bool:
    """Return whether the regeneration command buttons apply to this device.

    True when the device advertises the ``regeneration`` feature, or carries
    either enriched block the controls act on (``recharge_ui`` or
    ``regeneration``) on a host that omits the feature list.
    """
    return (
        _has_feature(device, FEATURE_REGENERATION)
        or _recharge_ui(device) is not None
        or _regeneration(device) is not None
    )


def _silence_alarm_exists(device: Device) -> bool:
    """Return whether the silence-alarm button applies to this device.

    True when the device advertises the ``audible_alarm`` feature, or (for hosts
    that omit the feature list but still report the flag) when the
    ``alarm_is_beeping`` field is present in the status block.
    """
    if _has_feature(device, FEATURE_AUDIBLE_ALARM):
        return True
    enriched = device.enriched_data
    status = enriched.water_treatment_status if enriched is not None else None
    return status is not None and status.alarm_is_beeping is not None


def _reset_wsov_error_code_exists(device: Device) -> bool:
    """Return whether the shutoff-valve error reset applies to this device.

    True when the device advertises the ``wsov`` feature, or carries the
    water-shutoff-valve block on a host that omits the feature list.
    """
    if _has_feature(device, FEATURE_WSOV):
        return True
    enriched = device.enriched_data
    return enriched is not None and enriched.water_shutoff_valve is not None


def _can_recharge(device: Device) -> bool | None:
    """Return the device's ``can_recharge`` hint, ``recharge_ui`` taking priority.

    The offline-capable ``recharge_ui`` tile is authoritative when present; only
    when it is absent (an ``iqua2`` host) does the value fall back to the
    ``regeneration`` block. ``None`` when neither block carries the hint — the
    caller then treats the action as allowed rather than guessing.
    """
    recharge_ui = _recharge_ui(device)
    if recharge_ui is not None:
        return recharge_ui.can_recharge
    regeneration = _regeneration(device)
    return regeneration.can_recharge if regeneration is not None else None


def _can_schedule(device: Device) -> bool | None:
    """Return the device's ``can_schedule`` hint, ``recharge_ui`` taking priority.

    Mirrors :func:`_can_recharge`: the ``recharge_ui`` tile is authoritative when
    present, otherwise the ``regeneration`` block, otherwise ``None``.
    """
    recharge_ui = _recharge_ui(device)
    if recharge_ui is not None:
        return recharge_ui.can_schedule
    regeneration = _regeneration(device)
    return regeneration.can_schedule if regeneration is not None else None


def _regenerate_available(device: Device) -> bool:
    """Return whether a manual recharge is allowed (unavailable only on explicit no)."""
    return _can_recharge(device) is not False


def _schedule_available(device: Device) -> bool:
    """Return whether scheduling a recharge is allowed (blocked only on explicit no)."""
    return _can_schedule(device) is not False


def _recharge_action_advertised(action: str) -> Callable[[Device], bool]:
    """Build an exists function true when ``recharge_ui`` advertises ``action``.

    The recharge-mode buttons only make sense when the tile itself offers the
    matching action; a device that does not advertise it never gets the button.
    """

    def _exists(device: Device) -> bool:
        """Return whether the recharge tile advertises ``action``."""
        recharge_ui = _recharge_ui(device)
        if recharge_ui is None:
            return False
        return any(offered.action == action for offered in recharge_ui.actions)

    return _exists


# ---------------------------------------------------------------------------
# Entity description and table
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class AquaHomeButtonDescription(ButtonEntityDescription):
    """Describe an AquaHome button: the command it sends and when it applies.

    ``function`` / ``action`` are the ``/command`` payload; ``exists_fn`` decides
    whether the button is created for a device; ``available_fn`` is an optional
    extra gate layered on top of the shared online availability; ``refresh_after``
    schedules a follow-up fast-coordinator poll once the command is accepted.
    """

    function: str
    action: str = DEFAULT_COMMAND_ACTION
    exists_fn: Callable[[Device], bool]
    available_fn: Callable[[Device], bool] | None = None
    refresh_after: bool = False


#: The buttons that are always in play — feature/presence gating and, for the
#: recharge controls, availability, are decided per device by the accessors above.
_ACTIVE_BUTTONS: tuple[AquaHomeButtonDescription, ...] = (
    AquaHomeButtonDescription(
        key="regenerate_now",
        translation_key="regenerate_now",
        function="regenerate",
        action="regenerate",
        exists_fn=_regeneration_control_exists,
        available_fn=_regenerate_available,
    ),
    AquaHomeButtonDescription(
        key="schedule_regeneration",
        translation_key="schedule_regeneration",
        function="regenerate",
        action="schedule",
        exists_fn=_regeneration_control_exists,
        available_fn=_schedule_available,
    ),
    AquaHomeButtonDescription(
        key="cancel_regeneration",
        translation_key="cancel_regeneration",
        function="regenerate",
        action="cancel",
        exists_fn=_regeneration_control_exists,
    ),
    AquaHomeButtonDescription(
        key="silence_alarm",
        translation_key="silence_alarm",
        function="set_audible_alarm",
        action="off",
        exists_fn=_silence_alarm_exists,
    ),
    AquaHomeButtonDescription(
        key="refresh_data",
        translation_key="refresh_data",
        function="get_all_data",
        entity_category=EntityCategory.DIAGNOSTIC,
        exists_fn=_always_exists,
        refresh_after=True,
    ),
    AquaHomeButtonDescription(
        key="advance_valve",
        translation_key="advance_valve",
        function="advance_valve",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        exists_fn=_always_exists,
    ),
    AquaHomeButtonDescription(
        key="reset_error_code",
        translation_key="reset_error_code",
        function="reset_error_code",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        exists_fn=_always_exists,
    ),
    AquaHomeButtonDescription(
        key="reset_wsov_error_code",
        translation_key="reset_wsov_error_code",
        function="reset_wsov_error_code",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        exists_fn=_reset_wsov_error_code_exists,
    ),
)

#: Recharge-mode buttons whose ``/command`` action payloads are UNVERIFIED —
#: the ``recharge_ui`` tile advertises the actions but
#: the mapping onto the ``regenerate`` function is a best guess the active
#: community fork does not exercise. They are excluded from :data:`BUTTONS` while
#: :data:`~.const.RECHARGE_ACTION_COMMANDS_VERIFIED` is ``False`` and so are never
#: created; the supervised live test proves the payloads and flips the gate.
_RECHARGE_ACTION_BUTTONS: tuple[AquaHomeButtonDescription, ...] = (
    AquaHomeButtonDescription(
        key="vacation_mode",
        translation_key="vacation_mode",
        function="regenerate",
        action="vacation_mode",  # UNVERIFIED payload guess (ledger P1)
        exists_fn=_recharge_action_advertised("vacation_mode"),
    ),
    AquaHomeButtonDescription(
        key="recharge_off",
        translation_key="recharge_off",
        function="regenerate",
        action="recharge_off",  # UNVERIFIED payload guess (ledger P1)
        exists_fn=_recharge_action_advertised("recharge_off"),
    ),
    AquaHomeButtonDescription(
        key="enable_recharge",
        translation_key="enable_recharge",
        function="regenerate",
        action="enable_recharge",  # UNVERIFIED payload guess (ledger P1)
        exists_fn=_recharge_action_advertised("enable_recharge"),
    ),
)

#: The description table the platform iterates. The unverified recharge-mode
#: buttons join it only once the live test flips the gate.
BUTTONS: tuple[AquaHomeButtonDescription, ...] = (
    *_ACTIVE_BUTTONS,
    *(_RECHARGE_ACTION_BUTTONS if RECHARGE_ACTION_COMMANDS_VERIFIED else ()),
)


# ---------------------------------------------------------------------------
# Entity and platform setup
# ---------------------------------------------------------------------------


class AquaHomeButton(AquaHomeEntity, ButtonEntity):
    """A single AquaHome command button backed by a coordinator device."""

    entity_description: AquaHomeButtonDescription

    def __init__(
        self,
        coordinator: AquaHomeCoordinator,
        description: AquaHomeButtonDescription,
    ) -> None:
        """Bind the button to its coordinator and description."""
        super().__init__(coordinator, description)
        #: Cancel handle for the pending refresh-after-command poll, if scheduled.
        self._refresh_unsub: CALLBACK_TYPE | None = None

    @property
    def available(self) -> bool:
        """Return whether the button can be pressed right now.

        Combines the inherited online gate with the description's optional
        ``available_fn`` (the recharge-tile ``can_recharge`` / ``can_schedule``
        guidance), which is ``None`` for buttons with no extra gate.
        """
        if not super().available:
            return False
        available_fn = self.entity_description.available_fn
        return available_fn is None or available_fn(self.coordinator.data)

    async def async_press(self) -> None:
        """Send the button's command, then optionally schedule a refresh poll."""
        await async_execute_command(
            self.coordinator.client,
            self.coordinator.device_id,
            self.entity_description.function,
            self.entity_description.action,
        )
        if self.entity_description.refresh_after:
            self._schedule_refresh()

    @callback
    def _schedule_refresh(self) -> None:
        """(Re)arm the follow-up coordinator poll after a data-refresh command.

        ``get_all_data`` asks the device to push fresh state to the cloud; the
        poll is delayed :data:`~.const.REFRESH_BUTTON_POLL_DELAY_SECONDS` so it
        reads the pushed values. A second press before the timer fires simply
        restarts it.
        """
        if self._refresh_unsub is not None:
            self._refresh_unsub()
        self._refresh_unsub = async_call_later(
            self.hass,
            REFRESH_BUTTON_POLL_DELAY_SECONDS,
            self._async_poll_after_command,
        )

    async def _async_poll_after_command(self, _now: datetime) -> None:
        """Request a coordinator refresh once the device has pushed fresh state."""
        self._refresh_unsub = None
        await self.coordinator.async_request_refresh()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel any pending refresh poll before the entity is torn down."""
        if self._refresh_unsub is not None:
            self._refresh_unsub()
            self._refresh_unsub = None
        await super().async_will_remove_from_hass()


def _make_discover(coordinator: AquaHomeCoordinator) -> Callable[[], set[str]]:
    """Build the discover callable reporting the button keys present right now."""

    @callback
    def _discover() -> set[str]:
        """Return the keys whose ``exists_fn`` passes for the current device view."""
        device = coordinator.data
        return {
            description.key for description in BUTTONS if description.exists_fn(device)
        }

    return _discover


def _make_create(
    coordinator: AquaHomeCoordinator,
) -> Callable[[AbstractSet[str]], list[Entity]]:
    """Build the create callable that materialises buttons for the given keys."""

    @callback
    def _create(keys: AbstractSet[str]) -> list[Entity]:
        """Return button entities for the descriptions in ``keys``."""
        return [
            AquaHomeButton(coordinator, description)
            for description in BUTTONS
            if description.key in keys
        ]

    return _create


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AquaHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the command buttons for each configured device.

    Each device's buttons are seeded from the first coordinator refresh and grown
    on later polls as capability-gated buttons (silence alarm, shutoff-valve
    reset) appear, debounced :data:`~.const.CAPABILITY_DEBOUNCE_POLLS` polls.
    """
    for coordinator in entry.runtime_data.coordinators.values():
        async_setup_dynamic_entities(
            entry,
            coordinator,
            async_add_entities,
            discover=_make_discover(coordinator),
            create=_make_create(coordinator),
            debounce_polls=CAPABILITY_DEBOUNCE_POLLS,
        )
