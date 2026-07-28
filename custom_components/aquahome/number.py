"""Number platform for AquaHome — device settings and the live-mode budget.

Two unrelated number families live here, each on its own coordinator:

1. **Settings numbers** — a device setting whose rule block carries
   ``number_rules`` (and no usable ``select_rules``), built at runtime on the
   settings coordinator via :func:`~.dynamic.async_setup_dynamic_entities`.

   The one subtlety is precision scaling. The iQua cloud stores a number setting
   as a precision-expanded integer: a value of ``12.5`` grains at ``precision=1``
   arrives as ``125``, and its ``min`` / ``max`` / ``step`` bounds are expanded
   the same way. The entity therefore divides the raw value and bounds by
   ``10**precision`` for display, and multiplies back (rounding to the nearest
   integer) on write. No boolean device setting is a number on the dev device, so
   the number path is synthetic-fixture-tested only; the scaling math mirrors the
   verified ``inlet_hardness`` select values (raw ``25.7`` ⇒ ``440 PPM``).

2. **The live-mode budget numbers** — the two knobs that bound how much of the
   cloud's live-session budget this device may spend, on that device's
   :class:`~.live.AquaHomeLiveManager`. They exist for every device, write only
   through the manager's public API (which clamps and persists them into the
   config entry), and are *always available*: they configure the integration's
   own behaviour rather than the device's, so an offline softener must never
   make them unusable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    LIVE_MIN_GAP_SECONDS_MAX,
    LIVE_MIN_GAP_SECONDS_MIN,
    LIVE_SESSIONS_PER_DAY_MAX,
    LIVE_SESSIONS_PER_DAY_MIN,
)
from .dynamic import async_setup_dynamic_entities
from .entity import AquaHomeSettingsEntity, build_device_info

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from collections.abc import Set as AbstractSet
    from typing import Any

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .api import Device, DeviceSetting, DeviceSettingsDocument, NumberRules
    from .coordinator import AquaHomeConfigEntry, AquaHomeSettingsCoordinator
    from .live import AquaHomeLiveManager
    from .live_state import LiveConfig

# Writes serialize against the throttled cloud.
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


# ---------------------------------------------------------------------------
# Live-mode budget numbers
#
# The two knobs bounding live-session spend. Their ranges are the supported ones
# rather than advice: the ticket endpoint that opens a live session runs its own
# token bucket — measured at six tickets per ten minutes with a burst of sixty,
# refilling roughly one ticket every 100 s — so the defaults (48 sessions a day,
# 120 s apart) sit far inside it while the maxima still cannot outrun the refill
# over a day. Exposing them as entities is deliberate: the budget is the one
# live-mode decision a household may reasonably want to tune, and doing it here
# keeps the integration free of an options flow.
# ---------------------------------------------------------------------------

#: Description keys of the two live-mode numbers (also their unique-id and
#: translation keys).
_LIVE_SESSIONS_PER_DAY_KEY = "live_sessions_per_day"
_LIVE_MIN_GAP_KEY = "live_min_gap"

#: Whole sessions; the gap is coarse enough that ten-second steps are plenty.
_LIVE_SESSIONS_PER_DAY_STEP = 1
_LIVE_MIN_GAP_STEP = 10


def _sessions_per_day(config: LiveConfig) -> float:
    """Return the configured daily live-session budget."""
    return float(config.sessions_per_day)


def _min_gap_seconds(config: LiveConfig) -> float:
    """Return the configured minimum gap between live sessions, in seconds."""
    return config.min_gap_seconds


async def _async_set_sessions_per_day(
    manager: AquaHomeLiveManager, value: float
) -> None:
    """Set the daily live-session budget (whole sessions)."""
    await manager.async_set_sessions_per_day(int(value))


async def _async_set_min_gap(manager: AquaHomeLiveManager, value: float) -> None:
    """Set the minimum gap between live sessions, in seconds."""
    await manager.async_set_min_gap(float(value))


@dataclass(frozen=True, kw_only=True)
class AquaHomeLiveNumberDescription(NumberEntityDescription):
    """Describe one live-mode number: how it reads and how it writes.

    ``value_fn`` picks the knob out of the manager's published
    :class:`~.live_state.LiveConfig`; ``set_fn`` applies a change through the
    manager's public API, which clamps the value to the supported range and
    persists it into the config entry's options.
    """

    value_fn: Callable[[LiveConfig], float]
    set_fn: Callable[[AquaHomeLiveManager, float], Coroutine[Any, Any, None]]


#: The two per-device live-budget knobs. Both are entered as numbers rather than
#: dragged on a slider: they are set once to a considered value, not tuned by
#: feel.
LIVE_NUMBERS: tuple[AquaHomeLiveNumberDescription, ...] = (
    AquaHomeLiveNumberDescription(
        key=_LIVE_SESSIONS_PER_DAY_KEY,
        translation_key=_LIVE_SESSIONS_PER_DAY_KEY,
        icon="mdi:counter",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=True,
        mode=NumberMode.BOX,
        native_min_value=LIVE_SESSIONS_PER_DAY_MIN,
        native_max_value=LIVE_SESSIONS_PER_DAY_MAX,
        native_step=_LIVE_SESSIONS_PER_DAY_STEP,
        value_fn=_sessions_per_day,
        set_fn=_async_set_sessions_per_day,
    ),
    AquaHomeLiveNumberDescription(
        key=_LIVE_MIN_GAP_KEY,
        translation_key=_LIVE_MIN_GAP_KEY,
        icon="mdi:timer-outline",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=True,
        mode=NumberMode.BOX,
        native_min_value=LIVE_MIN_GAP_SECONDS_MIN,
        native_max_value=LIVE_MIN_GAP_SECONDS_MAX,
        native_step=_LIVE_MIN_GAP_STEP,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        value_fn=_min_gap_seconds,
        set_fn=_async_set_min_gap,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AquaHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up both number families for every device.

    Settings numbers are discovered per device on that device's settings
    coordinator, paired with its fast coordinator's device view for the shared
    ``DeviceInfo``; a per-device helper builds the discover/create closures so
    each device captures its own coordinator and device. The live-mode numbers
    need no discovery — every device has a live manager — so they are added
    straight away, again paired with the fast device view.
    """
    runtime = entry.runtime_data
    for device_id, coordinator in runtime.settings_coordinators.items():
        fast = runtime.coordinators.get(device_id)
        if fast is None:
            continue
        _async_setup_device_numbers(entry, coordinator, fast.data, async_add_entities)
    live_numbers: list[AquaHomeLiveNumber] = []
    for device_id, manager in runtime.live_managers.items():
        fast = runtime.coordinators.get(device_id)
        if fast is None:
            continue
        live_numbers.extend(_live_numbers(manager, fast.data))
    async_add_entities(live_numbers)


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


def _live_numbers(
    manager: AquaHomeLiveManager, device: Device
) -> list[AquaHomeLiveNumber]:
    """Build one device's two live-mode budget numbers."""
    return [
        AquaHomeLiveNumber(manager, description, device) for description in LIVE_NUMBERS
    ]


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

        Non-finite values need their own guard: Home Assistant's service layer
        coerces YAML ``.nan``/``.inf`` to real floats and its own range check
        (like the one below) is ``False`` for NaN, so without this branch
        ``round`` would raise a raw, untranslated ``ValueError``.
        """
        minimum = self.native_min_value
        maximum = self.native_max_value
        if not math.isfinite(value) or value < minimum or value > maximum:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="number_out_of_range",
                translation_placeholders={
                    "min": f"{minimum:g}",
                    "max": f"{maximum:g}",
                },
            )
        await self._async_write(round(value * self._factor))


class AquaHomeLiveNumber(CoordinatorEntity["AquaHomeLiveManager"], NumberEntity):
    """One live-mode budget knob on a device's live manager.

    A view onto the manager's published
    :class:`~.live_state.LiveConfig`: setting a value calls the manager's public
    setter, which clamps it to the supported range, persists it into the config
    entry's options and republishes the state the entity re-renders from. The
    new bound applies to the next session the manager considers granting; a
    session already streaming is never cut short by a budget change.
    """

    _attr_has_entity_name = True
    entity_description: AquaHomeLiveNumberDescription

    def __init__(
        self,
        coordinator: AquaHomeLiveManager,
        description: AquaHomeLiveNumberDescription,
        device: Device,
    ) -> None:
        """Bind the number to its live manager, description, and device view.

        ``device`` is the paired fast coordinator's device view, used only to
        build the shared :class:`~homeassistant.helpers.device_registry.DeviceInfo`
        so the number attaches to the same device as the telemetry entities.
        """
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.device_slug}_{description.key}"
        self._attr_device_info = build_device_info(device)

    @property
    def available(self) -> bool:
        """Return ``True`` unconditionally — the knob is local, not cloud state.

        The value is the user's own preference, held in the config entry's
        options, so it stays settable while the cloud is unreachable or the
        softener is offline.
        """
        return True

    @property
    def native_value(self) -> float:
        """Return the knob's current value from the published live state."""
        return self.entity_description.value_fn(self.coordinator.state.config)

    async def async_set_native_value(self, value: float) -> None:
        """Apply a new budget value through the manager."""
        await self.entity_description.set_fn(self.coordinator, value)
