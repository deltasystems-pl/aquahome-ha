"""Tiered low-salt repair issues for AquaHome devices.

Watches each device's fast coordinator for the softener's own
``out_of_salt_estimate_days`` countdown (the PRIMARY salt signal — the Phase-6
chemistry estimate is deliberately not used here) and raises one Repairs issue
per device when it runs low: a warning at
:data:`~.const.SALT_DAYS_WARNING_THRESHOLD` days, escalating to error severity
at :data:`~.const.SALT_DAYS_CRITICAL_THRESHOLD`. Each tier releases only after
the countdown recovers past its threshold plus
:data:`~.const.SALT_DAYS_HYSTERESIS`, so the day-to-day wobble of an estimate
hovering at a boundary never flaps the issue in and out of existence.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir

from .api import scaled_value
from .const import (
    DOMAIN,
    SALT_DAYS_CRITICAL_THRESHOLD,
    SALT_DAYS_HYSTERESIS,
    SALT_DAYS_WARNING_THRESHOLD,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .api import Device
    from .coordinator import AquaHomeConfigEntry, AquaHomeCoordinator


class _Tier(enum.Enum):
    """Severity tier of the low-salt condition."""

    NONE = enum.auto()
    WARNING = enum.auto()
    CRITICAL = enum.auto()


#: Per-tier issue presentation: (translation_key, severity).
_TIER_PRESENTATION = {
    _Tier.WARNING: ("salt_level_low", ir.IssueSeverity.WARNING),
    _Tier.CRITICAL: ("salt_level_critical", ir.IssueSeverity.ERROR),
}


def _out_of_salt_days(device: Device) -> float | None:
    """Return the device's scaled out-of-salt countdown, or ``None``."""
    prop = device.properties.get("out_of_salt_estimate_days")
    return scaled_value(prop) if prop is not None else None


def _device_display_name(device: Device) -> str:
    """Return the human-facing device name used in the issue text."""
    enriched = device.enriched_data
    model = enriched.model if enriched is not None else None
    return device.nickname or model or "AquaHome"


def _next_tier(current: _Tier, days: float | None) -> _Tier:
    """Return the tier for ``days``, honouring the release hysteresis.

    Entry into a tier happens at its threshold; leaving it (downgrade or clear)
    requires the countdown to recover past threshold + hysteresis, so a value
    oscillating around a boundary keeps its current tier.
    """
    if days is None:
        return _Tier.NONE
    if days <= SALT_DAYS_CRITICAL_THRESHOLD:
        return _Tier.CRITICAL
    if current is _Tier.CRITICAL and days <= (
        SALT_DAYS_CRITICAL_THRESHOLD + SALT_DAYS_HYSTERESIS
    ):
        return _Tier.CRITICAL
    if days <= SALT_DAYS_WARNING_THRESHOLD:
        return _Tier.WARNING
    if current in (_Tier.WARNING, _Tier.CRITICAL) and days <= (
        SALT_DAYS_WARNING_THRESHOLD + SALT_DAYS_HYSTERESIS
    ):
        return _Tier.WARNING
    return _Tier.NONE


@callback
def async_setup_salt_issues(
    hass: HomeAssistant,
    entry: AquaHomeConfigEntry,
    coordinator: AquaHomeCoordinator,
) -> None:
    """Watch one device's salt countdown and maintain its repair issue.

    Evaluates the current payload immediately, then on every coordinator
    update (the listener is removed on unload). The issue is re-created —
    :func:`~homeassistant.helpers.issue_registry.async_create_issue` updates in
    place — only when the tier or the rendered day count changes, so a steady
    countdown does not churn the issue registry every poll.
    """
    issue_id = f"low_salt_{coordinator.device_slug}"
    tier = _Tier.NONE
    reported_days: int | None = None

    @callback
    def _evaluate() -> None:
        """Re-derive the tier from the latest payload and sync the issue."""
        nonlocal tier, reported_days
        device: Device | None = coordinator.data
        days = _out_of_salt_days(device) if device is not None else None
        new_tier = _next_tier(tier, days)
        # ``days``/``device`` cannot be None below: _next_tier maps None to
        # NONE — the extra checks narrow the types without an assert.
        if new_tier is _Tier.NONE or days is None or device is None:
            if tier is not _Tier.NONE:
                ir.async_delete_issue(hass, DOMAIN, issue_id)
            tier = _Tier.NONE
            reported_days = None
            return
        new_days = int(days)
        if new_tier is tier and new_days == reported_days:
            return
        translation_key, severity = _TIER_PRESENTATION[new_tier]
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=severity,
            translation_key=translation_key,
            translation_placeholders={
                "device": _device_display_name(device),
                "days": str(new_days),
            },
        )
        tier = new_tier
        reported_days = new_days

    _evaluate()
    entry.async_on_unload(coordinator.async_add_listener(_evaluate))
