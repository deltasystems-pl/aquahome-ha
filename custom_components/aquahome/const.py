"""Constants for the AquaHome integration."""

from __future__ import annotations

from datetime import time, timedelta
from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "aquahome"
MANUFACTURER: Final = "iQua"

PLATFORMS: Final[list[Platform]] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.VALVE,
]

# Fixed poll cadence with no user-facing knob: community evidence shows accounts
# banned (and softeners knocked off their own cloud) at aggressive cadences, so
# the interval option is deliberately absent (decision 2026-07-21).
UPDATE_INTERVAL: Final = timedelta(minutes=10)

# How long the coordinator keeps serving last-good data across 429/transient-5xx
# polls before going honestly unavailable. Sized for the observed ~5-min throttle
# windows and short cloud blips — never hours (the folkloric daily vendor
# outage was investigated and refuted; long stale-serving only hides problems).
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

# Alert types observed on a live account's real alert feed. The event
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

# Weekday carried by each avg_daily_use_day_N slot (slot 1 first). Map B
# (day_1=Saturday) — flipped from the Map A default on 2026-07-27 after live
# correlation against 8 weeks of daily-usage graphs (r ≈ +0.73 for Saturday vs
# ≈ 0 for Sunday across 28/42/56-day windows; owner-approved). Display-only:
# entity identities are slot-based, so a future correction changes labels,
# never unique IDs.
WEEKDAY_SLOTS: Final[tuple[str, ...]] = (
    "saturday",
    "sunday",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
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

# Cadence of the per-device statistics coordinator (external water-usage
# statistics backfill). Long-term statistics are hour-bucketed and the cloud
# counter history is immutable, so a slow cadence loses nothing; every run is
# idempotent by bucket start.
STATISTICS_UPDATE_INTERVAL: Final = timedelta(hours=12)

# External statistic id suffix: aquahome:<device_slug>_water.
WATER_STATISTIC_SUFFIX: Final = "_water"

# The raw property whose datapoint history feeds the water statistics — the
# same lifetime outlet counter the live total_water sensor exposes (verified
# identical against the live counter, 2026-07-27).
DATAPOINT_WATER_PROPERTY: Final = "total_outlet_water_gals"

# Datapoint value_type for the backfill: "max" returns the raw counter reading
# per bucket (0 = no reading; a lifetime counter is never genuinely zero) and
# diffing consecutive readings client-side loses no usage. "max_diff" (the
# app's choice) under-counts ~4 % at hourly resolution by dropping inter-bucket
# usage, and "actual" returns HTTP 500 (both live-verified 2026-07-27).
DATAPOINT_METER_VALUE_TYPE: Final = "max"

# accept-language pinned on every backfill request so the response `units`
# string parses against a fixed English vocabulary. The field is BOTH
# account-preference-driven and server-localized ("Liters" vs "Litry",
# community PAIN #5) — never parse it in the account's own language.
BACKFILL_LANGUAGE: Final = "en"

# Pause between consecutive datapoint requests inside one backfill run. The
# measured REST budget is a 5-per-60s token bucket with burst 50; a full
# first-run backfill is well under a dozen requests, so this pacing keeps the
# whole session far inside one refill window.
BACKFILL_REQUEST_PACING_SECONDS: Final = 2.0

# How far before the newest stored statistics row a backfill run re-fetches and
# recomputes, absorbing meter readings the device uploaded late. History behind
# this window is never rewritten.
BACKFILL_OVERLAP_DAYS: Final = 30

# Depth-probe horizon: the yearly sweep that finds the earliest retained
# reading starts this many years back.
BACKFILL_DEPTH_PROBE_YEARS: Final = 15

# Chunk ceilings for the datapoint fetches. No server-side row cap was observed
# (2185 hourly rows returned in one response), so these bound payload sizes and
# keep each request's bucket labels inside one DST regime.
BACKFILL_HOURLY_CHUNK_DAYS: Final = 92
BACKFILL_DAILY_CHUNK_DAYS: Final = 366

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
# (the active community fork sends none of them either). The
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

# Tiered low-salt Repairs nudge, driven by the device's own
# out_of_salt_estimate_days countdown (the PRIMARY salt signal — never the
# chemistry cross-check). Warning at <=14 days, error severity at <=7, each
# releasing only once the countdown has recovered past threshold + hysteresis
# so a day-to-day wobble at the boundary never flaps the issue
# (owner-confirmed tiers, 2026-07-27).
SALT_DAYS_WARNING_THRESHOLD: Final = 14
SALT_DAYS_CRITICAL_THRESHOLD: Final = 7
SALT_DAYS_HYSTERESIS: Final = 2

# --- Analytics tier. Every threshold below traces to published water-demand
# research (residential end-use studies, CUSUM/EWMA process-control practice)
# or to measurements on the reference device — no folklore thresholds.

# Daily engine run, device-local wall clock: just after the 01-07 minimum-night-
# flow window closes, so the freshest complete night is classifiable the same
# morning. Not on the hour, as a small politeness offset.
ANALYTICS_RUN_LOCAL_TIME: Final = time(7, 35)

# Rolling window the hour-of-week baseline grid and daily statistics are built
# from (26 weekly cycles), and the shorter window the night/vacation/ratio
# verdicts are assessed over. The activity coordinator's single history page
# (20 events x ~7 day cadence) comfortably covers the detector window with
# regeneration masking data.
BASELINE_WINDOW_DAYS: Final = 182
DETECTOR_WINDOW_DAYS: Final = 35

# Minimum-night-flow window, local hours [start, end) — the leak-detection
# window validated on 21,845 users [44]. Scheduled regenerations fire ~02:00
# local, squarely inside it, hence the mandatory masking.
MNF_WINDOW_START_HOUR: Final = 1
MNF_WINDOW_END_HOUR: Final = 7

# Leak debounce: consecutive classifiable LEAK nights before the binary turns
# on (lower bound of the study's 2-3-day recommendation [44][51]), and the
# persistent-flow fallback window [45].
LEAK_CONSECUTIVE_NIGHTS: Final = 2
PERSISTENT_FLOW_HOURS: Final = 72

# Tiered implied-continuous-rate thresholds anchored to the REU2016 skewed
# leakage distribution [47]: mean household leakage, a clearly-broken fixture,
# and the burst-pipe tail. Only the urgent tier files a Repairs issue
# (owner decision 2026-07-27).
LEAK_TIER_INFO_LITERS_PER_DAY: Final = 67.0
LEAK_TIER_WARNING_LITERS_PER_DAY: Final = 380.0
LEAK_TIER_URGENT_LITERS_PER_DAY: Final = 1135.0

# REU application-ratio buckets on daily totals [47]: below LOW is an
# away/vacation candidate, above EXCESS is guests or a possible leak.
RATIO_LOW: Final = 0.70
RATIO_EXCESS: Final = 1.30

# Vacation detection [55][47]: sustained multi-day low usage only (water alone
# cannot resolve short absences). An unoccupied day needs BOTH stage-1 features
# of the occupancy research [55]: consumption below VACATION_RATIO of
# expectation AND at most VACATION_MAX_EVENTS distinct draws (an empty house
# shows zero; a frugal occupied morning shows several — live-verified on the
# owner's own return morning, 3 draws totalling just 34 L). No single event may
# exceed a shower-scale draw either (REU fixture volumes: toilet 1.6 gal,
# dishwasher 4-6 gal, shower 15-20 gal).
VACATION_RATIO: Final = 0.30
VACATION_MIN_DAYS: Final = 3
VACATION_LARGE_EVENT_GALLONS: Final = 10.0
VACATION_MAX_EVENTS: Final = 1

# Robust-statistics band multiplier (k ~ 3 on 1.4826*MAD) [31][45], the number
# of anomalous hours within a day required to call a point anomaly, and the
# maturity gates: grid buckets need MIN_BUCKET_SAMPLES samples, learned daily
# statistics need two full weekly cycles (the Flo warm-up doubled [61]).
ANALYTICS_K: Final = 3.0
POINT_ANOMALY_MIN_HOURS: Final = 2
MIN_BUCKET_SAMPLES: Final = 4
LEARNED_DAILY_MIN_DAYS: Final = 14

# Freshness guard on the device's own per-weekday averages (observed live:
# slots go weeks stale — the fixture's Friday slot was 43 days old and 4x off).
# updated_at is a change-stamp, so a stable-valued fresh slot can look stale;
# the guard is deliberately conservative and falls back to learned statistics.
WEEKDAY_SLOT_FRESHNESS_DAYS: Final = 14

# Drift detection on daily totals: standard CUSUM design (k = 0.5 sigma,
# h = 5 sigma) [35] with an EWMA control chart complement [36]; the input
# series is Hampel-cleaned (local median replacement) first [32]. Both charts
# watch the UPWARD side only (a sustained drop is the vacation detector's
# domain, and flagging it as a "problem" would fire on every absence), run
# over a bounded trailing window (a batch CUSUM over an ever-growing window
# crosses any finite decision interval eventually — measured 41-85 % false
# alarms at 60-182 days on stationary synthetic households), and the
# user-facing drift reason requires BOTH charts to agree; each chart alone is
# exposed as an attribute.
CUSUM_K_SIGMA: Final = 0.5
CUSUM_H_SIGMA: Final = 5.0
EWMA_LAMBDA: Final = 0.2
EWMA_L: Final = 3.0
HAMPEL_WINDOW: Final = 7
DRIFT_WINDOW_DAYS: Final = 60

# A night or noon-day is assessable only when meter readings bound it within
# this many hours on both sides — otherwise the silence is indistinguishable
# from a data gap (device offline, backfill stale) and no verdict is honest.
ASSESSABLE_BOUND_HOURS: Final = 48

# Fallback duration for an open regeneration event (end_time null): observed
# cycles run ~2 h, padded for masking safety.
NOMINAL_REGEN_DURATION: Final = timedelta(hours=3)

# REU2016 North-American indoor per-capita reference (58.6 gpcd = 222 L/day)
# used only for the coarse occupancy estimate attribute [47].
OCCUPANCY_LITERS_PER_PERSON: Final = 222.0

# aquahome_event types fired on analytics verdict transitions.
EVENT_TYPE_LEAK_SUSPECTED: Final = "leak_suspected"
EVENT_TYPE_LEAK_CLEARED: Final = "leak_cleared"
EVENT_TYPE_USAGE_ANOMALY: Final = "usage_anomaly"
EVENT_TYPE_USAGE_ANOMALY_CLEARED: Final = "usage_anomaly_cleared"
EVENT_TYPE_VACATION_STARTED: Final = "vacation_started"
EVENT_TYPE_VACATION_ENDED: Final = "vacation_ended"

# --- Automation tier (Phase 8). Every device-affecting automation is opt-in
# (default-off switches / explicit confirmations), driven by daily-level
# signals from the analytics tier, and built ONLY on the live-verified
# regenerate/schedule/cancel command surface (the vacation-mode /command
# payloads remain unverified).

# Schedule a regeneration when the remaining treated-water capacity drops below
# tomorrow's forecast times this factor (a 50 % reserve).
FORECAST_RESERVE_FACTOR: Final = 1.5

# Resin-hygiene cap on vacation deferral: after this many deferred days the
# next scheduled regeneration is let through rather than cancelled (the device
# default max_days_between_recharges is 14; 21 = 1.5x headroom).
REGEN_DEFERRAL_MAX_DAYS: Final = 21

# Maximum deferral cancels per local day, so a disagreement with the device's
# own scheduling logic can never turn into a command fight on the throttled
# cloud.
REGEN_CANCEL_DAILY_BUDGET: Final = 3

# Night hours a quiet-hour regeneration-time proposal may pick from. The
# proposal itself is only ever a fixable Repairs suggestion, never a silent
# settings write (owner decision 2026-07-27).
QUIET_REGEN_CANDIDATE_HOURS: Final = (22, 23, 0, 1, 2, 3, 4, 5)

# recharge_ui.state values the scheduler acts on (observed live 2026-07-21).
RECHARGE_STATE_READY: Final = "ready"
RECHARGE_STATE_SCHEDULED: Final = "scheduled"

# entry.options key holding the persisted per-device automation flags.
OPTION_AUTOMATION: Final = "automation"

# aquahome_event types fired by the automation tier.
EVENT_TYPE_LEAK_WHILE_AWAY: Final = "leak_while_away"
EVENT_TYPE_REGEN_SCHEDULED: Final = "regen_scheduled"
EVENT_TYPE_REGEN_DEFERRED: Final = "regen_deferred"
EVENT_TYPE_REGEN_DEFERRAL_EXPIRED: Final = "regen_deferral_expired"

# Scheduler event payload reasons.
REGEN_REASON_LOW_CAPACITY: Final = "low_capacity"
REGEN_REASON_CATCH_UP: Final = "catch_up"

# Deferral actor labels. A MANUAL deferral (switch, service, blueprint) is
# never auto-released; an AUTO deferral (auto-vacation follower or a confirmed
# repair suggestion) releases itself when the household returns.
DEFERRAL_SOURCE_MANUAL: Final = "manual"
DEFERRAL_SOURCE_AUTO: Final = "auto"

# Service names and field attributes.
SERVICE_ANALYZE_USAGE: Final = "analyze_usage"
SERVICE_GET_USAGE_FORECAST: Final = "get_usage_forecast"
SERVICE_SET_VACATION_MODE: Final = "set_vacation_mode"
SERVICE_SCHEDULE_REGENERATION: Final = "schedule_regeneration"
ATTR_REFRESH: Final = "refresh"
ATTR_DAYS: Final = "days"
ATTR_VACATION: Final = "vacation"
ATTR_MODE: Final = "mode"

# schedule_regeneration mode field values.
REGEN_MODE_SCHEDULE: Final = "schedule"
REGEN_MODE_NOW: Final = "now"
REGEN_MODE_CANCEL: Final = "cancel"

# Ceiling on the get_usage_forecast days field (one weekly cycle).
FORECAST_MAX_DAYS: Final = 7

# --- Live mode (Phase 9). One per-device manager owns the single websocket
# lifecycle; every trigger path shares one grant budget. The /live endpoint is
# its own measured throttle domain (token bucket, 6 tickets per 600 s with
# burst 60 — distinct from the REST bucket), so sustained live use must stay
# at or below one ticket per ~100 s; the ~5-minute window renewal cycle sits
# comfortably inside that.

# Properties subscribed on every live session. current_time_secs is the
# device's ~10 s liveness heartbeat and app_active the fast-reporting-window
# signal; neither is ever pushed into the coordinator (they would rewrite
# every entity's state every few seconds for no user-visible change).
LIVE_SUBSCRIBED_PROPERTIES: Final[tuple[str, ...]] = (
    "total_outlet_water_gals",
    "water_counter_gals",
    "gallons_used_today",
    "treated_water_avail_gals",
    "current_water_flow_gpm",
    "regen_time_rem_secs",
    "rf_signal_strength_dbm",
    "app_active",
    "app_active_timeout",
    "current_time_secs",
)

# The subset of subscribed properties that entity value paths actually bind;
# only these are merged into the fast coordinator's device view.
LIVE_PUSHED_PROPERTIES: Final = frozenset(
    {
        "total_outlet_water_gals",
        "water_counter_gals",
        "gallons_used_today",
        "treated_water_avail_gals",
        "current_water_flow_gpm",
        "regen_time_rem_secs",
        "rf_signal_strength_dbm",
    }
)

# Session-grant budget defaults (owner decision 2026-07-27): at most this many
# trigger grants per device-local day with a minimum gap between grants.
# Renewals within one held session consume tickets but never grants. Both
# knobs are user-configurable through CONFIG number entities.
LIVE_SESSIONS_PER_DAY_DEFAULT: Final = 48
LIVE_MIN_GAP_SECONDS_DEFAULT: Final = 120.0
LIVE_SESSIONS_PER_DAY_MIN: Final = 4
LIVE_SESSIONS_PER_DAY_MAX: Final = 200
LIVE_MIN_GAP_SECONDS_MIN: Final = 60.0
LIVE_MIN_GAP_SECONDS_MAX: Final = 900.0

# Fast-reporting window sizing. The device advertises its own window via the
# app_active_timeout property (minutes; 5 on the reference device) — the
# fallback covers a payload that lacks it. The grace keeps the client-side
# window timer from racing the server's own app_active=false frame. On the
# iqua2 host sessions reportedly run ~an hour and the server pushes
# app_active=false when it wants a reconnect (fork observation — no iqua2
# account exists to verify against).
LIVE_WINDOW_FALLBACK_SECONDS: Final = 300.0
LIVE_WINDOW_GRACE_SECONDS: Final = 30.0
LIVE_IQUA2_WINDOW_SECONDS: Final = 3600.0

# The manual Live-view hold renews window after window while the switch is on;
# this cap flips it off if the user forgets it (the continuous-flow switch is
# the deliberate always-on mode).
LIVE_VIEW_HOLD_MAX_SECONDS: Final = 1800.0

# Poll-detected active use (trigger e): a today-counter rise of at least this
# many gallons between two consecutive fresh polls starts a session, with a
# cooldown so routine household use cannot drain the daily budget. Night
# sessions are deliberately allowed — per-gallon streaming during unexpected
# night flow is leak evidence.
LIVE_ACTIVE_USE_DELTA_GALLONS: Final = 2.0
LIVE_ACTIVE_USE_COOLDOWN_SECONDS: Final = 1800.0

# Analytics-driven smart windows (trigger c): after this many consecutive
# smart sessions that saw no water movement, suspend further smart windows for
# the rest of the device-local day.
LIVE_SMART_NO_FLOW_SUSPEND: Final = 3

# Websocket failure recovery: bounded exponential backoff between reconnect
# attempts, silent fallback to polling throughout, and a Repairs issue only
# after several consecutive failures while the device itself is online
# (auto-dismissed by the next success).
LIVE_BACKOFF_INITIAL_SECONDS: Final = 60.0
LIVE_BACKOFF_MAX_SECONDS: Final = 1800.0
LIVE_FAILURES_FOR_ISSUE: Final = 5

# Streamed frames are applied to the coordinator coalesced, so a connect
# snapshot burst becomes one entity update instead of a dozen.
LIVE_COALESCE_SECONDS: Final = 1.0

# entry.options key holding the persisted per-device live-mode configuration.
OPTION_LIVE: Final = "live"

# LiveState.status literals — also the connection-status enum sensor options.
# The sensor is deliberately non-churning: state changes on grant, end, and
# failure only, never on the renewals inside a held session.
LIVE_STATUS_IDLE: Final = "idle"
LIVE_STATUS_LIVE: Final = "live"
LIVE_STATUS_BACKOFF: Final = "backoff"

# Session source literals (observability attributes).
LIVE_SOURCE_MANUAL: Final = "manual"
LIVE_SOURCE_SMART: Final = "smart_window"
LIVE_SOURCE_REGEN: Final = "regen_burst"
LIVE_SOURCE_ANOMALY: Final = "anomaly_burst"
LIVE_SOURCE_ACTIVE_USE: Final = "active_use"
LIVE_SOURCE_CONTINUOUS: Final = "continuous"

CONFIG_VERSION: Final = 1
CONFIG_MINOR_VERSION: Final = 1
