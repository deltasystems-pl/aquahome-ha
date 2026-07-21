"""Constants for the AquaHome integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "aquahome"
MANUFACTURER: Final = "iQua"

PLATFORMS: Final[list[Platform]] = [
    Platform.BINARY_SENSOR,
    Platform.EVENT,
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

# The regeneration_status enum as documented by the OpenAPI spec, minus its
# "unknown" member: HA reserves the literal state "unknown" (STATE_UNKNOWN), so
# a device-reported "unknown" falls through the options check to None and
# renders as the genuine unknown state instead of a colliding enum option.
REGENERATION_STATUS_OPTIONS: Final[tuple[str, ...]] = (
    "regenerating",
    "scheduled",
    "none",
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

# Cadence of the per-device settings coordinator. The rule-driven settings
# document changes only when the owner reconfigures the device, so a gentle
# interval suffices; every PATCH write reconciles immediately from the
# document the server returns, independent of this cadence.
SETTINGS_UPDATE_INTERVAL: Final = timedelta(hours=6)

# Serve-stale window for the settings coordinator. Device configuration stays
# valid across long cloud blips, so this is the widest of the three windows
# while still going honestly unavailable within a day.
SETTINGS_MAX_STALE_SECONDS: Final = 86400.0

# How long an optimistic control state (valve motion, scan switch) is shown
# before falling back to polled truth. Sized for the cloud round-trip feel the
# prior-art fork validated (~10 s), not for the device's real actuation time.
OPTIMISTIC_STATE_TTL_SECONDS: Final = 10.0

# A capability discovered by the fast poll (wsov/leak hardware added after
# setup) must be seen this many consecutive polls before its entities are
# created, so a glitched payload cannot flap entities into existence.
CAPABILITY_DEBOUNCE_POLLS: Final = 2

# Delay between sending the get_all_data command (which asks the DEVICE to push
# fresh state to the cloud) and the follow-up coordinator poll that reads it.
REFRESH_BUTTON_POLL_DELAY_SECONDS: Final = 15.0

# Settings that configure the phone app's display/account preferences rather
# than the water treatment itself. They become entities like every other
# setting but are registry-disabled by default (user decision 2026-07-21):
# our sensors bind fixed conversions, so flipping these only affects the app.
DISPLAY_PREFERENCE_SETTINGS: Final = frozenset(
    {
        "volume_units",
        "weight_units",
        "hardness_units",
        "date_format",
        "time_format",
        "timezone",
    }
)

# The recharge_ui tile advertises vacation_mode / recharge_off / enable_recharge
# actions, but their /command payload mapping is undocumented and unverified
# (gap-analysis ledger P1; the active community fork sends none of them). The
# buttons exist in code but are not created until a supervised live test proves
# the payloads and flips this gate.
RECHARGE_ACTION_COMMANDS_VERIFIED: Final = False

# Feature-list tokens from enriched_data.water_treatment.features.
FEATURE_REGENERATION: Final = "regeneration"
FEATURE_AUDIBLE_ALARM: Final = "audible_alarm"
FEATURE_WSOV: Final = "wsov"
FEATURE_LEAK_DETECTOR: Final = "leak_detector"

# Config-entry data key for the stored refresh token (access token uses
# homeassistant.const.CONF_ACCESS_TOKEN).
CONF_REFRESH_TOKEN: Final = "refresh_token"  # noqa: S105 - entry-data key, not a secret

# Relative dip on the lifetime water counter treated as a cloud glitch and
# clamped, protecting total_increasing statistics from phantom meter resets.
# Larger drops are accepted as a genuine counter reset.
TOTAL_WATER_CLAMP_TOLERANCE: Final = 0.05

CONFIG_VERSION: Final = 1
CONFIG_MINOR_VERSION: Final = 1
