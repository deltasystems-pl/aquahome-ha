"""Frozen state types and options persistence for the live-mode tier.

The live-mode manager keeps its user-set configuration (the two opt-in flags
and the two budget knobs) in the config entry's options so a Home Assistant
restart never forgets them — like the automation tier, there is deliberately
no options *flow*; the options dict is written programmatically by the manager
only. This module owns the frozen types everything shares and the
(de)serialization between the configuration subset and ``entry.options``, so
the manager, the switch/number platforms, and the tests agree on a single
vocabulary without importing each other.

Only :class:`LiveConfig` persists. Everything else on :class:`LiveState` —
the session bookkeeping, budget counters, and failure/backoff trail — is
runtime state that must reset honestly on every restart.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from .const import (
    LIVE_MIN_GAP_SECONDS_DEFAULT,
    LIVE_MIN_GAP_SECONDS_MAX,
    LIVE_MIN_GAP_SECONDS_MIN,
    LIVE_SESSIONS_PER_DAY_DEFAULT,
    LIVE_SESSIONS_PER_DAY_MAX,
    LIVE_SESSIONS_PER_DAY_MIN,
    LIVE_STATUS_IDLE,
    OPTION_LIVE,
)

if TYPE_CHECKING:
    from datetime import datetime

    from .coordinator import AquaHomeConfigEntry

__all__ = [
    "LiveConfig",
    "LiveState",
    "clamp_min_gap",
    "clamp_sessions_per_day",
    "config_from_options",
    "options_with_config",
]


def clamp_sessions_per_day(value: int) -> int:
    """Return ``value`` clamped to the allowed sessions-per-day range."""
    return max(LIVE_SESSIONS_PER_DAY_MIN, min(LIVE_SESSIONS_PER_DAY_MAX, value))


def clamp_min_gap(value: float) -> float:
    """Return ``value`` clamped to the allowed minimum-gap range (seconds)."""
    return max(LIVE_MIN_GAP_SECONDS_MIN, min(LIVE_MIN_GAP_SECONDS_MAX, value))


@dataclass(frozen=True, slots=True)
class LiveConfig:
    """One device's persisted live-mode configuration."""

    smart_windows: bool = False
    continuous: bool = False
    sessions_per_day: int = LIVE_SESSIONS_PER_DAY_DEFAULT
    min_gap_seconds: float = LIVE_MIN_GAP_SECONDS_DEFAULT


@dataclass(frozen=True, slots=True)
class LiveState:
    """One device's live-mode configuration plus session bookkeeping.

    ``source`` and ``session_started`` are populated exactly while a session
    is active (``status == "live"``); ``sessions_today`` counts trigger
    *grants* on the device-local day (renewals within one held session are
    ticket spends, never grants); ``backoff_until`` is set exactly while
    ``status == "backoff"``.
    """

    config: LiveConfig
    status: str = LIVE_STATUS_IDLE
    source: str | None = None
    live_view: bool = False
    session_started: datetime | None = None
    windows_in_session: int = 0
    sessions_today: int = 0
    last_session_end: datetime | None = None
    consecutive_failures: int = 0
    backoff_until: datetime | None = None
    last_error: str | None = None
    #: End of the peak block the no-flow brake stood down; ``None`` when
    #: the tier is free to hold. Later blocks the same day start fresh.
    smart_suspended_until: datetime | None = None

    def with_config(self, config: LiveConfig) -> LiveState:
        """Return a copy carrying ``config`` as the current configuration."""
        return replace(self, config=config)


def config_from_options(entry: AquaHomeConfigEntry, device_id: str) -> LiveConfig:
    """Rebuild one device's persisted live configuration from the entry options.

    Absent or malformed values fall back to the defaults — an option written
    by a future version must never crash setup. Numeric knobs are clamped to
    their allowed ranges on the way in.
    """
    devices = entry.options.get(OPTION_LIVE)
    stored = devices.get(device_id) if isinstance(devices, dict) else None
    if not isinstance(stored, dict):
        return LiveConfig()
    sessions_raw = stored.get("sessions_per_day")
    sessions = (
        clamp_sessions_per_day(sessions_raw)
        if isinstance(sessions_raw, int) and not isinstance(sessions_raw, bool)
        else LIVE_SESSIONS_PER_DAY_DEFAULT
    )
    gap_raw = stored.get("min_gap_seconds")
    gap = (
        clamp_min_gap(float(gap_raw))
        if isinstance(gap_raw, (int, float)) and not isinstance(gap_raw, bool)
        else LIVE_MIN_GAP_SECONDS_DEFAULT
    )
    return LiveConfig(
        smart_windows=stored.get("smart_windows") is True,
        continuous=stored.get("continuous") is True,
        sessions_per_day=sessions,
        min_gap_seconds=gap,
    )


def options_with_config(
    entry: AquaHomeConfigEntry, device_id: str, config: LiveConfig
) -> dict[str, Any]:
    """Return a new options dict with ``device_id``'s live configuration replaced.

    Other devices' stored configuration and unrelated option keys are carried
    through untouched; runtime state is deliberately not written.
    """
    devices_raw = entry.options.get(OPTION_LIVE)
    devices: dict[str, Any] = dict(devices_raw) if isinstance(devices_raw, dict) else {}
    devices[device_id] = {
        "smart_windows": config.smart_windows,
        "continuous": config.continuous,
        "sessions_per_day": config.sessions_per_day,
        "min_gap_seconds": config.min_gap_seconds,
    }
    return {**entry.options, OPTION_LIVE: devices}
