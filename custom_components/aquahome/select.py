"""Select platform for AquaHome's rule-driven device settings.

The iQua ``GET /devices/{id}/settings`` document is a flat list of settings, each
carrying a ``component_type``, a ``current_value``, and a rule block. This module
turns every *select* setting — one whose ``select_rules`` offers at least one
option — into a :class:`~homeassistant.components.select.SelectEntity`, built at
runtime on the settings coordinator via :func:`~.dynamic.async_setup_dynamic_entities`
so a setting that only becomes visible once another is toggled (e.g.
``chem_feed_volume`` when ``aux_control_type`` is *Chemical Feed*) materialises
without a reload.

Three details are load-bearing and deliberate:

- The dropdown shows the server-localized ``label`` of each *non-disabled* option
  in document order; the raw ``value`` is what is written back. A language change
  re-localizes the labels on the next refresh (the base
  :class:`~.entity.AquaHomeSettingsEntity` also re-localizes the entity name).
- Two options that localize to the *same* label are disambiguated by appending
  their raw value — ``"label (value)"`` — for **all** colliding options, so Home
  Assistant never sees a duplicate option and the mapping back to a value stays
  unambiguous.
- A ``current_value`` the server reports that matches none of the offered options
  (an option list that drifted, or a value that is currently disabled) is
  surfaced as an extra option carrying its raw string, so ``current_option`` is
  never silently dropped as invalid.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError

from .const import DOMAIN
from .dynamic import async_setup_dynamic_entities
from .entity import AquaHomeSettingsEntity

if TYPE_CHECKING:
    from collections.abc import Set as AbstractSet

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .api import Device, DeviceSetting, DeviceSettingsDocument
    from .coordinator import AquaHomeConfigEntry, AquaHomeSettingsCoordinator

# Writes serialize against the throttled cloud.
PARALLEL_UPDATES = 1

#: Classification token this module claims (see :func:`_classify_setting`).
_PLATFORM = "select"


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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AquaHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the settings-select platform for every device with a settings feed.

    Each settings coordinator is paired with its fast coordinator's device view
    (same device-id key) for the shared ``DeviceInfo``; a per-device helper
    builds the discover/create closures so each device captures its own
    coordinator and device rather than the last loop iteration's.
    """
    runtime = entry.runtime_data
    for device_id, coordinator in runtime.settings_coordinators.items():
        fast = runtime.coordinators.get(device_id)
        if fast is None:
            continue
        _async_setup_device_selects(entry, coordinator, fast.data, async_add_entities)


@callback
def _async_setup_device_selects(
    entry: AquaHomeConfigEntry,
    coordinator: AquaHomeSettingsCoordinator,
    device: Device,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Wire the dynamic select adder for one device's settings document.

    ``discover`` reports the names of every *visible* select setting present in
    the latest document; a conditionally hidden setting is not created (it would
    appear later, once visible). The settings document is authoritative, so the
    adder runs with ``debounce_polls=1`` — a 6-hour cadence cannot afford a
    two-poll delay.
    """

    def _discover() -> set[str]:
        """Return the visible select-setting names in the latest document."""
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
        """Build a select entity for each discovered setting name (sorted)."""
        entities: list[Entity] = [
            AquaHomeSelect(coordinator, device, name) for name in sorted(keys)
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


class AquaHomeSelect(AquaHomeSettingsEntity, SelectEntity):
    """A select entity for one rule-driven select setting.

    Options and the current selection are derived from the live setting on every
    access, so a re-localized label or a server-side option change follows the
    coordinator without re-creating the entity.
    """

    def _resolve(self) -> tuple[list[str], dict[str, str], str | None]:
        """Return ``(display options, label->raw value, current display)``.

        Disabled options are dropped; options whose labels collide are
        disambiguated as ``"label (value)"`` for every colliding option. A
        ``current_value`` that matches no offered option's value is appended as
        an extra option carrying its raw string so the current selection is never
        silently invalid; an absent (``None``) current value yields no current
        selection and appends nothing.
        """
        setting = self.setting
        if (
            setting is None
            or setting.rules is None
            or setting.rules.select_rules is None
        ):
            return [], {}, None
        options = [
            option
            for option in setting.rules.select_rules.options
            if not option.disabled
        ]
        label_counts: dict[str, int] = {}
        for option in options:
            label_counts[option.label] = label_counts.get(option.label, 0) + 1
        displays: list[str] = []
        label_to_value: dict[str, str] = {}
        value_to_display: dict[str, str] = {}
        for option in options:
            display = (
                option.label
                if label_counts[option.label] == 1
                else f"{option.label} ({option.value})"
            )
            displays.append(display)
            label_to_value[display] = option.value
            value_to_display[option.value] = display
        current_display: str | None = None
        current = setting.current_value
        if current is not None:
            current_str = str(current)
            current_display = value_to_display.get(current_str)
            if current_display is None:
                # Drifted (or currently-disabled) value: surface the raw string so
                # ``current_option`` is not rejected as an unlisted option.
                displays.append(current_str)
                label_to_value[current_str] = current_str
                current_display = current_str
        return displays, label_to_value, current_display

    @property
    def options(self) -> list[str]:
        """Return the selectable labels in document order (drift appended last)."""
        displays, _, _ = self._resolve()
        return displays

    @property
    def current_option(self) -> str | None:
        """Return the label of the currently selected option, or ``None``."""
        _, _, current_display = self._resolve()
        return current_display

    async def async_select_option(self, option: str) -> None:
        """Write the raw value of the chosen label, rejecting an unknown one."""
        _, label_to_value, _ = self._resolve()
        value = label_to_value.get(option)
        if value is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_option",
                translation_placeholders={"option": option},
            )
        await self._async_write(value)
