"""Number platform for AquaHome's rule-driven device settings.

A *number* setting is one whose rule block carries ``number_rules`` (and no
usable ``select_rules``). This module turns each into a
:class:`~homeassistant.components.number.NumberEntity`, built at runtime on the
settings coordinator via :func:`~.dynamic.async_setup_dynamic_entities`.

The one subtlety is precision scaling. The iQua cloud stores a number setting as
a precision-expanded integer: a value of ``12.5`` grains at ``precision=1``
arrives as ``125``, and its ``min`` / ``max`` / ``step`` bounds are expanded the
same way. The entity therefore divides the raw value and bounds by
``10**precision`` for display, and multiplies back (rounding to the nearest
integer) on write. No boolean device setting is a number on the dev device, so
the number path is synthetic-fixture-tested only; the scaling math mirrors the
verified ``inlet_hardness`` select values (raw ``25.7`` ⇒ ``440 PPM``).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from homeassistant.components.number import NumberEntity
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

    from .api import Device, DeviceSetting, DeviceSettingsDocument, NumberRules
    from .coordinator import AquaHomeConfigEntry, AquaHomeSettingsCoordinator

# Writes serialize against the throttled cloud (Phase-4 contract).
PARALLEL_UPDATES = 1

#: Classification token this module claims (see :func:`_classify_setting`).
_PLATFORM = "number"

# Fallbacks used when the optional NumberRule bounds are absent — the spec marks
# min/max/step optional. These are native (display-space) defaults, matching
# Home Assistant's own NumberEntity defaults, not raw precision-expanded values.
_DEFAULT_MIN = 0.0
_DEFAULT_MAX = 100.0
_DEFAULT_STEP = 1.0


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


def _coerce_float(value: bool | int | float | str | None) -> float | None:
    """Tolerantly coerce a raw current value to a finite ``float``, else ``None``.

    A boolean is never a number; a non-finite result (``NaN`` / infinity) is
    rejected so the entity reports ``unknown`` rather than a broken value.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AquaHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the settings-number platform for every device with a settings feed.

    Each settings coordinator is paired with its fast coordinator's device view
    for the shared ``DeviceInfo``; a per-device helper builds the discover/create
    closures so each device captures its own coordinator and device.
    """
    runtime = entry.runtime_data
    for device_id, coordinator in runtime.settings_coordinators.items():
        fast = runtime.coordinators.get(device_id)
        if fast is None:
            continue
        _async_setup_device_numbers(entry, coordinator, fast.data, async_add_entities)


@callback
def _async_setup_device_numbers(
    entry: AquaHomeConfigEntry,
    coordinator: AquaHomeSettingsCoordinator,
    device: Device,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Wire the dynamic number adder for one device's settings document.

    ``discover`` reports the names of every *visible* number setting; a
    conditionally hidden setting is not created. The document is authoritative,
    so the adder runs with ``debounce_polls=1``.
    """

    def _discover() -> set[str]:
        """Return the visible number-setting names in the latest document."""
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
        """Build a number entity for each discovered setting name (sorted)."""
        entities: list[Entity] = [
            AquaHomeNumber(coordinator, device, name) for name in sorted(keys)
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


class AquaHomeNumber(AquaHomeSettingsEntity, NumberEntity):
    """A number entity for one rule-driven number setting.

    Every bound and the value are precision-scaled from the live setting on each
    access, so a server-side rule change follows the coordinator. The spec
    provides no unit, so none is set; the mode stays the NumberEntity default.
    """

    @property
    def _rules(self) -> NumberRules | None:
        """Return the live ``number_rules`` block, or ``None`` when absent."""
        setting = self.setting
        if setting is None or setting.rules is None:
            return None
        return setting.rules.number_rules

    @property
    def _factor(self) -> float:
        """Return ``10**precision`` (an absent precision ⇒ factor ``1``)."""
        rules = self._rules
        if rules is None or rules.precision is None:
            return 1.0
        return float(10**rules.precision)

    @property
    def native_min_value(self) -> float:
        """Return the precision-scaled minimum, falling back to ``0``."""
        rules = self._rules
        if rules is None or rules.min is None:
            return _DEFAULT_MIN
        return rules.min / self._factor

    @property
    def native_max_value(self) -> float:
        """Return the precision-scaled maximum, falling back to ``100``."""
        rules = self._rules
        if rules is None or rules.max is None:
            return _DEFAULT_MAX
        return rules.max / self._factor

    @property
    def native_step(self) -> float:
        """Return the precision-scaled step, falling back to ``1``."""
        rules = self._rules
        if rules is None or rules.step is None:
            return _DEFAULT_STEP
        return rules.step / self._factor

    @property
    def native_value(self) -> float | None:
        """Return the precision-scaled current value, or ``None`` when unusable."""
        setting = self.setting
        if setting is None:
            return None
        numeric = _coerce_float(setting.current_value)
        if numeric is None:
            return None
        return numeric / self._factor

    async def async_set_native_value(self, value: float) -> None:
        """Validate against the scaled range, then write the raw expanded integer.

        A value outside ``[native_min, native_max]`` raises
        :class:`~homeassistant.exceptions.ServiceValidationError`; a valid value
        is multiplied back by ``10**precision`` and rounded to the nearest
        integer the device expects.
        """
        minimum = self.native_min_value
        maximum = self.native_max_value
        if value < minimum or value > maximum:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="number_out_of_range",
                translation_placeholders={
                    "min": f"{minimum:g}",
                    "max": f"{maximum:g}",
                },
            )
        await self._async_write(round(value * self._factor))
