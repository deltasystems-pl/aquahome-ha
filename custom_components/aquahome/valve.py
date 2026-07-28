"""Valve platform for the AquaHome integration — the water-shutoff valve (WSOV).

Exposes the single hardware water-shutoff valve some iQua softeners carry as one
:class:`~homeassistant.components.valve.ValveEntity` per device. The valve is an
open/close actuator with no position feedback (``reports_position`` is ``False``);
its :attr:`~AquaHomeValve.is_closed` state is read from the enriched
``water_shutoff_valve`` block's ``status`` enum — ``close`` is closed, ``open`` is
open, and anything else (``manual``, ``error``, ``not_installed``, ``unknown``, or
the block being absent) is reported as ``unknown`` rather than a fabricated
open/closed.

The cloud gates some actions behind a confirmation dialog: when the enriched
``dialog.dialog_buttons`` explicitly disables ``open`` (or ``close``), the matching
service raises before any I/O rather than sending a command the server would only
reject. A successful command optimistically shows motion
(:attr:`~AquaHomeValve.is_opening` / :attr:`~AquaHomeValve.is_closing`) for
:data:`~.const.OPTIMISTIC_STATE_TTL_SECONDS`, cleared early by the first poll that
confirms the target state. The polled ``status`` itself is never fabricated — only
the transient motion hint is.

The valve exists for a device as soon as it advertises the :data:`~.const.FEATURE_WSOV`
feature *or* carries a ``water_shutoff_valve`` block, discovered through
:func:`~.dynamic.async_setup_dynamic_entities` so a valve paired after setup appears
without a reload.

.. warning::
   Ships **untested against real hardware**: no device in the developer cohort
   carries a water-shutoff valve (every dev device advertises only
   ``["regeneration"]``), so this platform is exercised solely by synthetic
   fixtures. The status mapping and ``open``/``close`` command actions follow the
   OpenAPI spec and the active community fork; a tester with a WSOV-equipped unit
   is needed to confirm them live (Phase 4 live-unverified ledger).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.valve import (
    ValveDeviceClass,
    ValveEntity,
    ValveEntityDescription,
    ValveEntityFeature,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later

from .api import Device, WaterShutoffValve
from .command import async_execute_command
from .const import (
    CAPABILITY_DEBOUNCE_POLLS,
    DOMAIN,
    FEATURE_WSOV,
    OPTIMISTIC_STATE_TTL_SECONDS,
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

PARALLEL_UPDATES = 1

#: Stable discovery / unique-id key for the one valve a device carries.
_VALVE_KEY = "water_shutoff_valve"
#: The ``/command`` function that drives the valve, and its two actions. The
#: function name coincides with the entity key but is a distinct API namespace.
_COMMAND_FUNCTION = "water_shutoff_valve"
_ACTION_OPEN = "open"
_ACTION_CLOSE = "close"
#: The two ``status`` enum members that map to a definite valve position; every
#: other value (manual/error/not_installed/unknown/absent) reports ``unknown``.
_STATUS_CLOSED = "close"
_STATUS_OPEN = "open"

VALVE_DESCRIPTION = ValveEntityDescription(
    key=_VALVE_KEY,
    translation_key=_VALVE_KEY,
    device_class=ValveDeviceClass.WATER,
    reports_position=False,
)


# ---------------------------------------------------------------------------
# None-safe payload accessors
# ---------------------------------------------------------------------------


def _has_feature(device: Device, feature: str) -> bool:
    """Return whether the device advertises ``feature`` in its enriched data."""
    enriched = device.enriched_data
    return enriched is not None and feature in enriched.features


def _valve(device: Device) -> WaterShutoffValve | None:
    """Return the enriched water-shutoff-valve block, or ``None`` when absent."""
    enriched = device.enriched_data
    return enriched.water_shutoff_valve if enriched is not None else None


def _valve_exists(device: Device) -> bool:
    """Return whether the water-shutoff valve applies to this device.

    True when the device advertises the :data:`~.const.FEATURE_WSOV` feature or
    (for hosts that omit the feature list) when the ``water_shutoff_valve`` block
    is present in the enriched payload — either alone must suffice, because the
    two are populated independently by the cloud.
    """
    return _has_feature(device, FEATURE_WSOV) or _valve(device) is not None


def _closed_from_status(block: WaterShutoffValve | None) -> bool | None:
    """Map a valve block's ``status`` onto closed / open / unknown.

    ``close`` is closed, ``open`` is open; every other value — ``manual``,
    ``error``, ``not_installed``, ``unknown``, or the block being absent — yields
    ``None`` so the valve reports ``unknown`` rather than a fabricated position.
    """
    if block is None:
        return None
    if block.status == _STATUS_CLOSED:
        return True
    if block.status == _STATUS_OPEN:
        return False
    return None


# ---------------------------------------------------------------------------
# Entity
# ---------------------------------------------------------------------------


class AquaHomeValve(AquaHomeEntity, ValveEntity):
    """The one water-shutoff valve of an AquaHome device.

    An open/close actuator without position feedback: ``is_closed`` mirrors the
    polled ``status`` and only the optimistic motion flags are ever fabricated,
    for at most :data:`~.const.OPTIMISTIC_STATE_TTL_SECONDS` after a command.
    """

    entity_description: ValveEntityDescription
    _attr_supported_features = ValveEntityFeature.OPEN | ValveEntityFeature.CLOSE

    def __init__(self, coordinator: AquaHomeCoordinator) -> None:
        """Bind the valve to its device coordinator with motion flags cleared."""
        super().__init__(coordinator, VALVE_DESCRIPTION)
        self._attr_is_opening = False
        self._attr_is_closing = False
        #: Cancel handle for the pending optimistic-motion TTL timer, if armed.
        self._optimistic_unsub: CALLBACK_TYPE | None = None
        #: ``is_closed`` value the in-flight command drives toward (``True`` when
        #: closing, ``False`` when opening); ``None`` when not moving optimistically.
        self._optimistic_target_closed: bool | None = None

    @property
    def _block(self) -> WaterShutoffValve | None:
        """Return this device's valve block, or ``None`` when it is absent."""
        return _valve(self.coordinator.data)

    @property
    def available(self) -> bool:
        """Return whether the valve has trustworthy state to show.

        Extends the base online gate with the valve block being present and not
        explicitly reporting ``is_installed`` false — a device advertising the
        feature but not (yet) an installed valve stays unavailable rather than
        showing an unknowable state.
        """
        block = self._block
        return (
            super().available and block is not None and block.is_installed is not False
        )

    @property
    def is_closed(self) -> bool | None:
        """Return whether the valve is closed, or ``None`` when it is unknown."""
        return _closed_from_status(self._block)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the valve's diagnostic fields, omitting any that are ``None``.

        ``None`` when the valve block itself is absent; otherwise the non-null
        subset of ``error_code``, ``manual_override``, and ``auto_shutoff_supported``.
        """
        block = self._block
        if block is None:
            return None
        attrs: dict[str, Any] = {}
        if block.error_code is not None:
            attrs["error_code"] = block.error_code
        if block.manual_override is not None:
            attrs["manual_override"] = block.manual_override
        if block.auto_shutoff_supported is not None:
            attrs["auto_shutoff_supported"] = block.auto_shutoff_supported
        return attrs

    async def async_open_valve(self) -> None:
        """Open the valve, refusing first if the cloud dialog blocks the action."""
        self._raise_if_blocked(opening=True)
        await async_execute_command(
            self.coordinator.client,
            self.coordinator.device_id,
            _COMMAND_FUNCTION,
            _ACTION_OPEN,
        )
        self._start_optimistic(opening=True)

    async def async_close_valve(self) -> None:
        """Close the valve, refusing first if the cloud dialog blocks the action."""
        self._raise_if_blocked(opening=False)
        await async_execute_command(
            self.coordinator.client,
            self.coordinator.device_id,
            _COMMAND_FUNCTION,
            _ACTION_CLOSE,
        )
        self._start_optimistic(opening=False)

    def _raise_if_blocked(self, *, opening: bool) -> None:
        """Raise when the confirmation dialog explicitly disables this action.

        Only an *explicit* ``False`` on the relevant ``dialog_buttons`` flag is a
        hard block; an absent dialog, absent buttons, or a ``None`` flag leaves the
        action allowed (the server is the final arbiter).
        """
        block = self._block
        if block is None or block.dialog is None or block.dialog.dialog_buttons is None:
            return
        buttons = block.dialog.dialog_buttons
        flag = buttons.open if opening else buttons.close
        if flag is False:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="valve_action_blocked"
            )

    @callback
    def _start_optimistic(self, *, opening: bool) -> None:
        """Show optimistic motion and arm the TTL that falls back to polled truth."""
        self._cancel_optimistic_timer()
        self._attr_is_opening = opening
        self._attr_is_closing = not opening
        self._optimistic_target_closed = not opening
        self.async_write_ha_state()
        self._optimistic_unsub = async_call_later(
            self.hass, OPTIMISTIC_STATE_TTL_SECONDS, self._expire_optimistic
        )

    @callback
    def _expire_optimistic(self, _now: datetime) -> None:
        """Drop the optimistic motion flags once the TTL elapses without a poll."""
        self._optimistic_unsub = None
        self._optimistic_target_closed = None
        self._attr_is_opening = False
        self._attr_is_closing = False
        self.async_write_ha_state()

    @callback
    def _cancel_optimistic_timer(self) -> None:
        """Cancel the pending TTL timer if one is armed."""
        if self._optimistic_unsub is not None:
            self._optimistic_unsub()
            self._optimistic_unsub = None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Clear optimistic motion once a poll confirms the target position."""
        if (
            self._optimistic_target_closed is not None
            and _closed_from_status(self._block) is self._optimistic_target_closed
        ):
            self._cancel_optimistic_timer()
            self._optimistic_target_closed = None
            self._attr_is_opening = False
            self._attr_is_closing = False
        super()._handle_coordinator_update()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel any pending optimistic-motion timer before removal."""
        self._cancel_optimistic_timer()
        await super().async_will_remove_from_hass()


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------


@callback
def _async_add_valve(
    entry: AquaHomeConfigEntry,
    coordinator: AquaHomeCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Register the one device's valve, growing in if the hardware appears later."""

    def _discover() -> AbstractSet[str]:
        """Return the valve key when the device carries (or advertises) a valve."""
        return {_VALVE_KEY} if _valve_exists(coordinator.data) else set()

    def _create(keys: AbstractSet[str]) -> list[Entity]:
        """Build the valve entity for each discovered key (at most one)."""
        return [AquaHomeValve(coordinator) for _ in keys]

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
    """Set up the water-shutoff valve for each configured device."""
    for coordinator in entry.runtime_data.coordinators.values():
        _async_add_valve(entry, coordinator, async_add_entities)
