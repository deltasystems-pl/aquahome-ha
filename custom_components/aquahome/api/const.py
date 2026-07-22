"""Constants for the iQua cloud API client.

Values verified live on 2026-07-21 — see reverse-engineering/knowledge/api/api-reference.md.
"""

from __future__ import annotations

API_BASE_URL = "https://api.myiquaapp.com/v1"

# Post-migration ("iQua2") accounts live in a separate user/device database
# with an identical API surface; login simply fails on the wrong host. The
# config flow offers both hosts — see reverse-engineering/knowledge/prior-art/iqua-mutilator-fork.md.
IQUA2_BASE_URL = "https://api.iqua2.com/v1"

# App-mimicry headers: the server localizes strings/units from accept-language
# and gates behavior on the app version, so every request must look like the
# official Android app (v1.5.2 build 2794).
APP_USER_AGENT = "okhttp/4.9.2"
APP_VERSION_HEADER = "version=1.5.2,build=2794"
ACCEPT_HEADER = "application/json, text/plain, */*"

# Request the maximum allowed refresh-token lifetime (3 years) so a config
# entry survives without forcing reauth.
MAX_REFRESH_HOURS = 26280

# Access JWTs live exactly 24 h; refresh proactively when less than this many
# seconds remain so an in-flight poll never races expiry.
TOKEN_REFRESH_MARGIN_SECONDS = 2 * 3600

DEFAULT_TIMEOUT_SECONDS = 30
DEVICES_PER_PAGE = 200

# Never sent, ever: reboots risk bricking/annoying the device and
# reset_water_counter destroys the statistics baseline. The entity layer may
# later expose reset_water_counter behind an advanced gate by constructing the
# request itself; the client refuses it by default.
FORBIDDEN_COMMAND_FUNCTIONS = frozenset(
    {
        "reboot_system",
        "reboot_wireless_module",
        "reset_water_counter",
    }
)

# Documented command functions and their allowed actions (empty set = the
# action field is ignored by the API; the client sends "none").
COMMAND_FUNCTIONS: dict[str, frozenset[str]] = {
    "regenerate": frozenset({"cancel", "schedule", "regenerate"}),
    "water_shutoff_valve": frozenset({"open", "close"}),
    "set_audible_alarm": frozenset({"off"}),
    "advance_valve": frozenset(),
    "reset_error_code": frozenset(),
    "reset_wsov_error_code": frozenset(),
    "get_all_data": frozenset(),
    "leak_detector": frozenset({"start_scan", "end_scan"}),
}

# GET /devices/{id}/live is server-throttled; enforce a client-side floor
# between ticket requests.
LIVE_TICKET_MIN_INTERVAL_SECONDS = 60.0

# Fallback backoff applied after a 429 when the server sends no usable
# `ratelimit-policy` refill interval. The token-bucket 429 windows observed on
# the fork clear in ~5 min, so a one-minute floor is a safe, cheap default
# (see reverse-engineering/knowledge/research/automation-gap-analysis.md §7 D1).
DEFAULT_RATE_LIMIT_BACKOFF_SECONDS = 60.0
