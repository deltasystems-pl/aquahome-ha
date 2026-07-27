"""Frozen state types and options persistence for the automation tier.

The Phase-8 automation tier (three default-off switches plus the per-device
regeneration scheduler) keeps its user-set flags in the config entry's options
so a Home Assistant restart never forgets an opt-in — there is deliberately no
options *flow*; the options dict is written programmatically by the scheduler
only. This module owns the one frozen state type everything shares and the
(de)serialization between it and ``entry.options``, so the scheduler, the
switch platform, the service layer, and the tests all agree on a single
vocabulary without importing each other.

Only the user-set flags and the deferral bookkeeping persist; the scheduler's
observability fields (``last_decision`` / ``last_decision_at``) are runtime
state that must reset honestly on every restart.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from .const import OPTION_AUTOMATION

if TYPE_CHECKING:
    from datetime import datetime

    from .coordinator import AquaHomeConfigEntry

__all__ = [
    "AutomationState",
    "options_with_state",
    "state_from_options",
]

#: The option keys that persist across restarts (everything else is runtime).
_PERSISTED_FLAGS = ("vacation_deferral", "auto_vacation", "smart_regeneration")


@dataclass(frozen=True, slots=True)
class AutomationState:
    """One device's automation flags and scheduler bookkeeping.

    ``deferral_source`` and ``deferral_started`` are populated exactly while
    ``vacation_deferral`` is active; ``last_decision`` records the scheduler's
    most recent verdict (``scheduled`` / ``deferred`` / ``not_needed`` /
    ``catch_up`` / ``deferral_expired`` / one of the ``skipped_*`` literals)
    for the smart-regeneration switch's attributes.
    """

    vacation_deferral: bool = False
    auto_vacation: bool = False
    smart_regeneration: bool = False
    deferral_source: str | None = None
    deferral_started: datetime | None = None
    last_decision: str | None = None
    last_decision_at: datetime | None = None

    def with_decision(self, decision: str, now: datetime) -> AutomationState:
        """Return a copy carrying ``decision`` as the latest scheduler verdict."""
        return replace(self, last_decision=decision, last_decision_at=now)


def state_from_options(entry: AquaHomeConfigEntry, device_id: str) -> AutomationState:
    """Rebuild one device's persisted automation state from the entry options.

    Absent or malformed values fall back to the all-off defaults — an option
    written by a future version must never crash setup. The runtime-only
    decision fields always start empty.
    """
    devices = entry.options.get(OPTION_AUTOMATION)
    stored = devices.get(device_id) if isinstance(devices, dict) else None
    if not isinstance(stored, dict):
        return AutomationState()
    flags = {name: stored.get(name) is True for name in _PERSISTED_FLAGS}
    source = stored.get("deferral_source")
    started = stored.get("deferral_started")
    started_dt = dt_util.parse_datetime(started) if isinstance(started, str) else None
    if not flags["vacation_deferral"]:
        # Deferral bookkeeping without an active deferral is stale residue.
        source = None
        started_dt = None
    return AutomationState(
        vacation_deferral=flags["vacation_deferral"],
        auto_vacation=flags["auto_vacation"],
        smart_regeneration=flags["smart_regeneration"],
        deferral_source=source if isinstance(source, str) else None,
        deferral_started=started_dt,
    )


def options_with_state(
    entry: AquaHomeConfigEntry, device_id: str, state: AutomationState
) -> dict[str, Any]:
    """Return a new options dict with ``device_id``'s persisted subset replaced.

    Other devices' stored flags and unrelated option keys are carried through
    untouched; the runtime-only decision fields are deliberately not written.
    """
    devices_raw = entry.options.get(OPTION_AUTOMATION)
    devices: dict[str, Any] = dict(devices_raw) if isinstance(devices_raw, dict) else {}
    devices[device_id] = {
        "vacation_deferral": state.vacation_deferral,
        "auto_vacation": state.auto_vacation,
        "smart_regeneration": state.smart_regeneration,
        "deferral_source": state.deferral_source,
        "deferral_started": (
            state.deferral_started.isoformat()
            if state.deferral_started is not None
            else None
        ),
    }
    return {**entry.options, OPTION_AUTOMATION: devices}
