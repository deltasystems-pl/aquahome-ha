"""Constants for the AquaHome integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "aquahome"
MANUFACTURER: Final = "iQua"

PLATFORMS: Final[list[Platform]] = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
]

# Fixed poll cadence with no user-facing knob: community evidence shows accounts
# banned (and softeners knocked off their own cloud) at aggressive cadences, so
# the interval option is deliberately absent (implementation plan, decision
# 2026-07-21).
UPDATE_INTERVAL: Final = timedelta(minutes=10)

# How long the coordinator keeps serving last-good data across 429/transient-5xx
# polls before going honestly unavailable. Sized for the observed ~5-min throttle
# windows and short cloud blips — never hours (gap analysis §7 D12).
MAX_STALE_SECONDS: Final = 1800.0

# Cadence of the per-device activity coordinator (alert feed + regeneration
# history). Both are slow-moving cloud history; a badge-count increase or a
# regeneration transition seen by the fast coordinator triggers an early
# refresh, so the steady-state interval can stay gentle on the throttled cloud.
ACTIVITY_UPDATE_INTERVAL: Final = timedelta(minutes=30)

# Serve-stale window for the activity coordinator. History records stay valid
# far longer than live telemetry, so this is deliberately wider than
# MAX_STALE_SECONDS while still going honestly unavailable within hours.
ACTIVITY_MAX_STALE_SECONDS: Final = 10800.0

# Page size for the alert / regeneration-event history fetches. One page of the
# newest records is all the runtime entities need.
ACTIVITY_PAGE_SIZE: Final = 20

# Bus event fired for every newly observed device alert (the <domain>_event
# convention, like zha_event).
EVENT_AQUAHOME: Final = "aquahome_event"

# Alert types observed on the live feed (knowledge/api samples). The event
# entity declares exactly these plus the catch-all; unknown vendor strings map
# to ALERT_EVENT_TYPE_OTHER with the raw type preserved in the attributes.
KNOWN_ALERT_TYPES: Final[tuple[str, ...]] = (
    "connection_status_offline",
    "connection_status_online",
    "excessive_water_use_alert",
    "water_shutoff_valve_opened",
    "salt_level_2",
)
ALERT_EVENT_TYPE_OTHER: Final = "other"

# Home Assistant's hard limit on a state string's length.
MAX_STATE_LENGTH: Final = 255

# The regeneration_status enum as documented by the OpenAPI spec.
REGENERATION_STATUS_OPTIONS: Final[tuple[str, ...]] = (
    "regenerating",
    "scheduled",
    "none",
    "unknown",
    "disabled",
    "suspended",
    "error",
    "wsov_disabled",
)

# recharge_ui.state value meaning the cloud has lost the device: derived mode
# binaries must report unknown (None) in that state, never a fabricated False.
RECHARGE_STATE_OFFLINE: Final = "offline"

# Weekday carried by each avg_daily_use_day_N slot (slot 1 first). Map A
# (day_1=Sunday, US firmware convention) — user-confirmed default 2026-07-21,
# not yet live-verified. Display-only: entity identities are slot-based, so a
# future correction changes labels, never unique IDs.
WEEKDAY_SLOTS: Final[tuple[str, ...]] = (
    "sunday",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
)

# Config-entry data key for the stored refresh token (access token uses
# homeassistant.const.CONF_ACCESS_TOKEN).
CONF_REFRESH_TOKEN: Final = "refresh_token"  # noqa: S105 - entry-data key, not a secret

# Relative dip on the lifetime water counter treated as a cloud glitch and
# clamped, protecting total_increasing statistics from phantom meter resets.
# Larger drops are accepted as a genuine counter reset.
TOTAL_WATER_CLAMP_TOLERANCE: Final = 0.05

CONFIG_VERSION: Final = 1
CONFIG_MINOR_VERSION: Final = 1
