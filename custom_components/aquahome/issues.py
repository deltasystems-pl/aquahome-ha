"""Repair issues for AquaHome devices: low salt and urgent leak.

The salt section watches each device's fast coordinator for the softener's own
``out_of_salt_estimate_days`` countdown (the PRIMARY salt signal — the Phase-6
chemistry estimate is deliberately not used here) and raises one Repairs issue
per device when it runs low: a warning at
:data:`~.const.SALT_DAYS_WARNING_THRESHOLD` days, escalating to error severity
at :data:`~.const.SALT_DAYS_CRITICAL_THRESHOLD`. Each tier releases only after
the countdown recovers past its threshold plus
:data:`~.const.SALT_DAYS_HYSTERESIS`, so the day-to-day wobble of an estimate
hovering at a boundary never flaps the issue in and out of existence.

The leak section watches each device's analytics engine and files a single
error-severity issue when the leak detector confirms a continuous flow at the
urgent tier (:data:`~.const.LEAK_TIER_URGENT_LITERS_PER_DAY`, burst-pipe
scale) — and only then: softer leak evidence stays on the binary sensor and
the event bus (owner decision 2026-07-27). An engine pass that has nothing to
assess (``active is None``) leaves the issue untouched, so a transient
statistics failure can never silently retract a live warning.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from .analytics.model import TIER_URGENT
from .api import scaled_value
from .const import (
    DOMAIN,
    SALT_DAYS_CRITICAL_THRESHOLD,
    SALT_DAYS_HYSTERESIS,
    SALT_DAYS_WARNING_THRESHOLD,
)
from .entity import device_display_name

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .analytics.engine import AquaHomeAnalyticsEngine
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
        # NONE — the extra checks narrow the types without an assert. The
        # delete is deliberately unconditional (a no-op when absent): the tier
        # is closure-local and resets on reload, so gating it on the tracked
        # tier would leave a pre-reload issue standing after the salt was
        # refilled while the entry was unloaded.
        if new_tier is _Tier.NONE or days is None or device is None:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
            tier = _Tier.NONE
            reported_days = None
            return
        new_days = int(days)
        if new_tier is tier and new_days == reported_days:
            return
        translation_key, severity = _TIER_PRESENTATION[new_tier]
        if tier is _Tier.WARNING and new_tier is _Tier.CRITICAL:
            # An observed escalation must reach the user even if the warning
            # was ignored: async_create_issue updates an existing issue in
            # place and deliberately preserves its dismissed_version, so the
            # issue is deleted first to clear the dismissal. Only the genuine
            # WARNING -> CRITICAL transition does this — the first evaluation
            # after a restart re-enters the current tier from NONE, and
            # deleting there would wipe a legitimate dismissal on every boot.
            ir.async_delete_issue(hass, DOMAIN, issue_id)
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=severity,
            translation_key=translation_key,
            translation_placeholders={
                "device": device_display_name(device),
                "days": str(new_days),
            },
        )
        tier = new_tier
        reported_days = new_days

    _evaluate()
    entry.async_on_unload(coordinator.async_add_listener(_evaluate))


@callback
def async_setup_leak_issues(
    hass: HomeAssistant,
    entry: AquaHomeConfigEntry,
    coordinator: AquaHomeCoordinator,
    engine: AquaHomeAnalyticsEngine,
) -> None:
    """Watch one device's leak verdict and maintain its urgent repair issue.

    Filed when the analytics leak detector is active at the urgent tier,
    deleted when the detector confirms the flow has stopped (``active`` is
    ``False``). ``active is None`` — nothing to assess — changes nothing in
    either direction. The issue is re-created only when the rendered daily
    volume changes, so a steady leak does not churn the issue registry on
    every engine pass; a clear-then-refile naturally resets any dismissal,
    which is exactly right for a leak that stopped and started again.
    """
    issue_id = f"leak_urgent_{engine.device_slug}"
    reported_liters: int | None = None

    @callback
    def _evaluate() -> None:
        """Sync the repair issue with the engine's latest leak verdict."""
        nonlocal reported_liters
        result = engine.data
        if result is None:
            return
        leak = result.leak
        if leak.active is None:
            return
        urgent = leak.active and leak.tier == TIER_URGENT
        if not urgent:
            if leak.active is False:
                ir.async_delete_issue(hass, DOMAIN, issue_id)
                reported_liters = None
            return
        liters = (
            int(leak.implied_liters_per_day)
            if leak.implied_liters_per_day is not None
            else 0
        )
        if liters == reported_liters:
            return
        device: Device | None = coordinator.data
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="leak_urgent",
            translation_placeholders={
                "device": device_display_name(device) if device is not None else "?",
                "liters_per_day": str(liters),
            },
        )
        reported_liters = liters

    _evaluate()
    entry.async_on_unload(engine.async_add_listener(_evaluate))


@callback
def async_remove_leak_issues(hass: HomeAssistant, entry: AquaHomeConfigEntry) -> None:
    """Delete every device's urgent-leak issue when the entry is removed.

    Mirrors :func:`async_remove_salt_issues`: ids are rebuilt from the device
    registry because ``async_remove_entry`` may run on an entry that was never
    loaded, and deleting an id that was never filed is a documented no-op.
    """
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        for domain, identifier in device.identifiers:
            if domain == DOMAIN:
                ir.async_delete_issue(hass, DOMAIN, f"leak_urgent_{identifier}")


@callback
def async_remove_salt_issues(hass: HomeAssistant, entry: AquaHomeConfigEntry) -> None:
    """Delete every device's low-salt issue when the entry is removed.

    A Repairs issue outlives its config entry unless deleted explicitly, so
    without this an uninstalled integration would keep nagging until the next
    restart. Called from ``async_remove_entry``, which runs on an entry that
    may never have been loaded, so — like the statistics cleanup — the issue
    ids are rebuilt from the device registry (each AquaHome device identifier
    is exactly the slug the ids are built from) rather than from runtime data.
    Deleting an id that was never filed (leak-detector sub-devices, healthy
    softeners) is a documented no-op.

    Deliberately NOT wired to plain unload: the Repairs registry preserves a
    user's "Ignore" across reloads and restarts via ``dismissed_version``, and
    a delete would wipe it on every entry reload.
    """
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        for domain, identifier in device.identifiers:
            if domain == DOMAIN:
                ir.async_delete_issue(hass, DOMAIN, f"low_salt_{identifier}")
