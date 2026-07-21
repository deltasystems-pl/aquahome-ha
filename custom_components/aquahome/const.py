"""Constants for the AquaHome integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "aquahome"
MANUFACTURER: Final = "iQua"

PLATFORMS: Final[list[Platform]] = [Platform.BINARY_SENSOR, Platform.SENSOR]

# Fixed poll cadence with no user-facing knob: community evidence shows accounts
# banned (and softeners knocked off their own cloud) at aggressive cadences, so
# the interval option is deliberately absent (implementation plan, decision
# 2026-07-21).
UPDATE_INTERVAL: Final = timedelta(minutes=10)

# How long the coordinator keeps serving last-good data across 429/transient-5xx
# polls before going honestly unavailable. Sized for the observed ~5-min throttle
# windows and short cloud blips — never hours (gap analysis §7 D12).
MAX_STALE_SECONDS: Final = 1800.0

# Config-entry data key for the stored refresh token (access token uses
# homeassistant.const.CONF_ACCESS_TOKEN).
CONF_REFRESH_TOKEN: Final = "refresh_token"  # noqa: S105 - entry-data key, not a secret

# Relative dip on the lifetime water counter treated as a cloud glitch and
# clamped, protecting total_increasing statistics from phantom meter resets.
# Larger drops are accepted as a genuine counter reset.
TOTAL_WATER_CLAMP_TOLERANCE: Final = 0.05

CONFIG_VERSION: Final = 1
CONFIG_MINOR_VERSION: Final = 1
