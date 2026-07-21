"""Embedded async client for the iQua cloud API (api.myiquaapp.com).

This package is the integration's self-contained iQua API layer: the auth token
lifecycle (:mod:`.auth`), the tolerant payload models (:mod:`.models`), the typed
error taxonomy (:mod:`.exceptions`), and the high-level device client
(:mod:`.client`). Everything downstream (config flow, coordinator, entities)
imports from here.
"""

from __future__ import annotations

from .auth import AuthManager
from .client import AquaHomeClient
from .const import (
    API_BASE_URL,
    COMMAND_FUNCTIONS,
    FORBIDDEN_COMMAND_FUNCTIONS,
    IQUA2_BASE_URL,
    MAX_REFRESH_HOURS,
)
from .exceptions import (
    ApiError,
    AquaHomeConnectionError,
    AquaHomeError,
    AuthError,
    DeviceOfflineError,
    ForbiddenCommandError,
    RateLimitError,
)
from .models import (
    SCALED_PROPERTIES,
    SENTINEL_DISABLED,
    UNVERIFIED_SCALED_PROPERTIES,
    Alert,
    AlertsPage,
    CommandResult,
    Conversion,
    ConvertedProperty,
    Datapoint,
    DatapointGraph,
    Device,
    DeviceSummary,
    FlowMonitorStatus,
    LiveTicket,
    LoginResult,
    PropertyValue,
    RateLimitStatus,
    RechargeAction,
    RechargeDialog,
    RechargeUi,
    RegenerationEvent,
    RegenerationEventsPage,
    RegenerationInfo,
    SaltLevel,
    UserSummary,
    WaterTreatment,
    WaterTreatmentStatus,
    scaled_value,
)

__all__ = [
    "API_BASE_URL",
    "COMMAND_FUNCTIONS",
    "FORBIDDEN_COMMAND_FUNCTIONS",
    "IQUA2_BASE_URL",
    "MAX_REFRESH_HOURS",
    "SCALED_PROPERTIES",
    "SENTINEL_DISABLED",
    "UNVERIFIED_SCALED_PROPERTIES",
    "Alert",
    "AlertsPage",
    "ApiError",
    "AquaHomeClient",
    "AquaHomeConnectionError",
    "AquaHomeError",
    "AuthError",
    "AuthManager",
    "CommandResult",
    "Conversion",
    "ConvertedProperty",
    "Datapoint",
    "DatapointGraph",
    "Device",
    "DeviceOfflineError",
    "DeviceSummary",
    "FlowMonitorStatus",
    "ForbiddenCommandError",
    "LiveTicket",
    "LoginResult",
    "PropertyValue",
    "RateLimitError",
    "RateLimitStatus",
    "RechargeAction",
    "RechargeDialog",
    "RechargeUi",
    "RegenerationEvent",
    "RegenerationEventsPage",
    "RegenerationInfo",
    "SaltLevel",
    "UserSummary",
    "WaterTreatment",
    "WaterTreatmentStatus",
    "scaled_value",
]
