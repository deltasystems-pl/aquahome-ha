"""Domain services for the AquaHome integration.

Four actions, every one of them targeted at an *entity* rather than at a device
or at the integration as a whole, so a multi-device account needs no device
picker and Home Assistant's own per-entity permissions apply unchanged:

* ``analyze_usage`` and ``get_usage_forecast`` read the analytics tier through
  one of its published sensors and hand their answer back as service response
  data. Nothing in the integration changes, which is what makes them safe to
  call from a template sensor or a script loop.
* ``set_vacation_mode`` and ``schedule_regeneration`` act on a device, and each
  accepts only the control entity it was designed for.

Registration happens from ``async_setup`` — once per Home Assistant run and
independent of any config entry — so an automation referencing one of these
actions still validates while the integration is reloading or its cloud login
has expired (the ``action-setup`` quality rule). The handlers consequently
resolve everything they need from the targeted entity itself and never look a
config entry up.

Response payloads are assembled by hand rather than by dataclass reflection:
every key is always present (``None`` where a detector has nothing to say),
every date is an ISO string, every tuple a list and every float is rounded to
two decimals. That stable, JSON-safe shape is part of the service contract — a
template reading ``response["leak"]["tier"]`` must not start failing the day an
internal dataclass gains a field, nor carry a float with sixteen digits of
false precision.

The entity classes the handlers type-check against are imported inside the
handler bodies. Services are registered before any platform has been set up, so
keeping the three platform modules out of this module's import graph means the
service layer never influences platform import order — and stays importable
however Home Assistant chooses to load them.

``analyze_usage`` with ``refresh: true`` is the only path that recomputes the
analytics on demand. It costs one recorder read plus the detection pass, so it
is deliberately not the default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import voluptuous as vol
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import service as service_helper
from homeassistant.helpers.update_coordinator import UpdateFailed

from .command import async_execute_command
from .const import (
    ATTR_DAYS,
    ATTR_MODE,
    ATTR_REFRESH,
    ATTR_VACATION,
    DOMAIN,
    FEATURE_REGENERATION,
    FORECAST_MAX_DAYS,
    REGEN_MODE_CANCEL,
    REGEN_MODE_NOW,
    REGEN_MODE_SCHEDULE,
    SERVICE_ANALYZE_USAGE,
    SERVICE_GET_USAGE_FORECAST,
    SERVICE_SCHEDULE_REGENERATION,
    SERVICE_SET_VACATION_MODE,
)

if TYPE_CHECKING:
    from datetime import date, datetime

    from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse
    from homeassistant.helpers.entity import Entity

    from .analytics.model import (
        AnalyticsResult,
        DayAssessment,
        ForecastState,
        GridSummary,
        NightAssessment,
    )
    from .api import Device
    from .button import AquaHomeButton
    from .sensor import AquaHomeAnalyticsSensor
    from .switch import AquaHomeVacationDeferralSwitch

#: Decimals every published metric is rounded to. The underlying statistics are
#: hour-resolution meter reads; more digits would be false precision.
_ROUNDING: Final = 2

#: Hours per day, and with it the width of one row of the hour-of-week grid.
_HOURS_PER_DAY: Final = 24

#: Full hour-of-week grid size (7 x 24), the bound the grid is read up to.
_GRID_HOURS: Final = 168

#: Grid row labels in python weekday order (``Monday = 0``), which is how the
#: analytics grid is indexed — deliberately NOT the device's own slot order.
_WEEKDAY_NAMES: Final = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

#: The device function every regeneration action goes through, and the action
#: each service mode maps onto. All three are live-verified payloads.
_REGEN_FUNCTION: Final = "regenerate"
_REGEN_ACTIONS: Final = {
    REGEN_MODE_SCHEDULE: "schedule",
    REGEN_MODE_NOW: "regenerate",
    REGEN_MODE_CANCEL: "cancel",
}

#: Button keys that are regeneration controls by construction. A device whose
#: enriched payload no longer advertises the feature still has these buttons,
#: and refusing to command them would be a regression, so either evidence is
#: enough (local copy of the button platform's key set — the service layer must
#: not import an entity platform to read three strings).
_REGEN_BUTTON_KEYS: Final = frozenset(
    {"regenerate_now", "schedule_regeneration", "cancel_regeneration"}
)

#: The regeneration button that acts on an *indirect* (device/area/label)
#: target. Such a target expands to all three regeneration buttons and each
#: invocation would send the same cloud command; exactly one must act.
#: ``cancel_regeneration`` is the one regeneration button with no
#: ``available_fn``, so it survives whatever the ``can_schedule`` /
#: ``can_recharge`` hints say and every mode keeps working on a device target.
_REGEN_FANOUT_KEY: Final = "cancel_regeneration"

#: Plain-English targets named in the wrong-entity error.
_EXPECTED_ANALYTICS_SENSOR: Final = "an AquaHome usage analytics sensor"
_EXPECTED_VACATION_SWITCH: Final = "an AquaHome vacation deferral switch"
_EXPECTED_REGEN_BUTTON: Final = "an AquaHome regeneration button"


# ---------------------------------------------------------------------------
# None-safe payload accessors
#
# Local copies of the button platform's recharge accessors, following the same
# replication rule the scheduler does: a service handler must not import an
# entity platform to reach a three-line payload read.
# ---------------------------------------------------------------------------


def _can_schedule(device: Device) -> bool | None:
    """Return the device's ``can_schedule`` hint, ``recharge_ui`` taking priority.

    The offline-capable tile is authoritative when present; only when it is
    absent (an ``iqua2`` host) does the value come from the ``regeneration``
    block. ``None`` when neither carries the hint, which the caller reads as
    "allowed" rather than second-guessing the cloud.
    """
    enriched = device.enriched_data
    if enriched is None:
        return None
    if enriched.recharge_ui is not None:
        return enriched.recharge_ui.can_schedule
    regeneration = enriched.regeneration
    return regeneration.can_schedule if regeneration is not None else None


def _can_recharge(device: Device) -> bool | None:
    """Return the device's ``can_recharge`` hint, ``recharge_ui`` taking priority.

    Mirrors :func:`_can_schedule` for the immediate-recharge action.
    """
    enriched = device.enriched_data
    if enriched is None:
        return None
    if enriched.recharge_ui is not None:
        return enriched.recharge_ui.can_recharge
    regeneration = enriched.regeneration
    return regeneration.can_recharge if regeneration is not None else None


def _regeneration_control(device: Device) -> bool:
    """Return whether the device carries regeneration controls at all.

    True when it advertises the ``regeneration`` feature, or carries either
    enriched block the controls act on, on a host that omits the feature list.
    """
    enriched = device.enriched_data
    if enriched is None:
        return False
    return (
        FEATURE_REGENERATION in enriched.features
        or enriched.recharge_ui is not None
        or enriched.regeneration is not None
    )


# ---------------------------------------------------------------------------
# Response serialization
# ---------------------------------------------------------------------------


def _rounded(value: float | None) -> float | None:
    """Return one metric rounded for transport, or ``None`` when absent."""
    return round(value, _ROUNDING) if value is not None else None


def _isoformat(value: date | datetime | None) -> str | None:
    """Return one date or timestamp as an ISO string, or ``None`` when absent."""
    return value.isoformat() if value is not None else None


def _serialize_night(night: NightAssessment) -> dict[str, Any]:
    """Return one night's minimum-night-flow verdict."""
    return {
        "night": _isoformat(night.night),
        "verdict": night.verdict.value,
        "min_hour_liters": _rounded(night.min_hour_liters),
    }


def _serialize_day(day: DayAssessment) -> dict[str, Any]:
    """Return one noon-day measured against its expectation.

    The shared view of a day: the anomaly block publishes exactly this, while
    the day list adds the assessability flag on top.
    """
    return {
        "day": _isoformat(day.day),
        "total_liters": _rounded(day.total_liters),
        "expected_liters": _rounded(day.expected_liters),
        "spread_liters": _rounded(day.spread_liters),
        "ratio": _rounded(day.ratio),
        "bucket": day.bucket,
        "largest_event_liters": _rounded(day.largest_event_liters),
    }


def _serialize_active_hours(grid: GridSummary) -> dict[str, list[int]]:
    """Return the learned active hours as ``weekday -> [hour, ...]``.

    The engine carries the grid as 168 flags indexed ``weekday * 24 + hour``,
    which is compact to compute with and unreadable in a template. Weekdays
    without a single active hour are left out entirely rather than mapped to an
    empty list: their absence is the same statement, and it keeps a cold-start
    response short. A grid shorter than a full week (nothing learned yet) simply
    contributes fewer rows.
    """
    hours: dict[str, list[int]] = {}
    for index, active in enumerate(grid.active_hours[:_GRID_HOURS]):
        if active:
            weekday = _WEEKDAY_NAMES[index // _HOURS_PER_DAY]
            hours.setdefault(weekday, []).append(index % _HOURS_PER_DAY)
    return hours


def _serialize_result(result: AnalyticsResult) -> dict[str, Any]:
    """Return one complete analytics pass as a JSON-safe response payload.

    Every detector block is published whole, including the fields the entities
    keep to themselves (each drift chart's individual vote, the masking
    coverage, the grid maturity counts), because the point of the action is to
    expose the reasoning a binary sensor can only summarise. The night and day
    lists keep the engine's own order — oldest first, newest last.
    """
    leak = result.leak
    anomaly = result.anomaly
    vacation = result.vacation
    forecast = result.forecast
    grid = result.grid
    return {
        "computed_at": _isoformat(result.computed_at),
        "leak": {
            "active": leak.active,
            "consecutive_nights": leak.consecutive_nights,
            "rate_liters_per_hour": _rounded(leak.rate_liters_per_hour),
            "implied_liters_per_day": _rounded(leak.implied_liters_per_day),
            "tier": leak.tier,
            "persistent_flow": leak.persistent_flow,
            "masking_coverage": leak.masking_coverage,
            "last_verdict_night": _isoformat(leak.last_verdict_night),
        },
        "anomaly": {
            "active": anomaly.active,
            "reasons": list(anomaly.reasons),
            "point_hours": anomaly.point_hours,
            "drift_alarm": anomaly.drift_alarm,
            "drift_cusum": anomaly.drift_cusum,
            "drift_ewma": anomaly.drift_ewma,
            "day": _serialize_day(anomaly.day) if anomaly.day is not None else None,
        },
        "vacation": {
            "active": vacation.active,
            "consecutive_days": vacation.consecutive_days,
            "since": _isoformat(vacation.since),
        },
        "forecast": {
            "gallons": _rounded(forecast.gallons),
            "liters": _rounded(forecast.liters),
            "source": forecast.source,
            "band_liters": _rounded(forecast.band_liters),
            "weekday": forecast.weekday,
            "persons": forecast.persons,
        },
        "grid": {
            "mature_buckets": grid.mature_buckets,
            "hourly_samples": grid.hourly_samples,
            "active_hours": _serialize_active_hours(grid),
        },
        "nights": [_serialize_night(night) for night in result.nights],
        "days": [
            {**_serialize_day(day), "assessable": day.assessable} for day in result.days
        ],
    }


def _serialize_forecast(day: date, forecast: ForecastState) -> dict[str, Any]:
    """Return one day's forecast, keyed by the day it applies to.

    ``weekday`` is the label the expectation was resolved under, so a forecast
    can always be traced back to the weekday statistics behind it, and
    ``source`` names which link of the resolution chain produced the number.
    """
    return {
        "date": _isoformat(day),
        "weekday": forecast.weekday,
        "gallons": _rounded(forecast.gallons),
        "liters": _rounded(forecast.liters),
        "band_liters": _rounded(forecast.band_liters),
        "source": forecast.source,
        "persons": forecast.persons,
    }


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


def _wrong_entity(expected: str) -> ServiceValidationError:
    """Return the wrong-target error naming what the action expects.

    Every action is registered for a whole entity domain, so any AquaHome
    sensor, switch or button can be pointed at it. Refusing an unsuitable one
    with the entity it *should* have been given beats letting the handler fail
    on a missing attribute.
    """
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="service_wrong_entity",
        translation_placeholders={"expected": expected},
    )


def _explicitly_targeted(entity: Entity, call: ServiceCall) -> bool:
    """Return whether the call named this entity itself.

    Only ``entity_id`` names an entity; a device, area, floor or label target
    reaches it indirectly, which is the case Home Assistant's own
    entity-service filters skip rather than refuse.
    """
    named = call.data.get(ATTR_ENTITY_ID)
    return isinstance(named, list) and entity.entity_id in named


def _analytics_sensor(
    entity: Entity, call: ServiceCall
) -> AquaHomeAnalyticsSensor | None:
    """Return the targeted entity as an analytics sensor, or ``None`` to skip it.

    A device or area target expands to every AquaHome sensor; refusing one of
    them would abort the whole call and throw away the answers the analytics
    sensors did produce, so an indirectly reached bystander is skipped instead.
    An explicitly named wrong entity is still refused.
    """
    from .sensor import AquaHomeAnalyticsSensor  # noqa: PLC0415

    if isinstance(entity, AquaHomeAnalyticsSensor):
        return entity
    if _explicitly_targeted(entity, call):
        raise _wrong_entity(_EXPECTED_ANALYTICS_SENSOR)
    return None


def _vacation_switch(entity: Entity) -> AquaHomeVacationDeferralSwitch:
    """Return the targeted entity as the vacation switch, or refuse the call."""
    from .switch import AquaHomeVacationDeferralSwitch  # noqa: PLC0415

    if not isinstance(entity, AquaHomeVacationDeferralSwitch):
        raise _wrong_entity(_EXPECTED_VACATION_SWITCH)
    return entity


def _regeneration_button(entity: Entity) -> AquaHomeButton:
    """Return the targeted entity as a regeneration button, or refuse the call.

    Any AquaHome button on a device that has regeneration controls is accepted,
    not just the three regeneration buttons themselves: the action commands the
    device, and requiring the user to pick one particular button of that device
    would be arbitrary. A device without regeneration controls at all is
    refused, since no payload it could send would be honoured.
    """
    from .button import AquaHomeButton  # noqa: PLC0415

    if not isinstance(entity, AquaHomeButton):
        raise _wrong_entity(_EXPECTED_REGEN_BUTTON)
    key = entity.entity_description.key
    if key not in _REGEN_BUTTON_KEYS and not _regeneration_control(
        entity.coordinator.data
    ):
        raise _wrong_entity(_EXPECTED_REGEN_BUTTON)
    return entity


# ---------------------------------------------------------------------------
# Service handlers
#
# Registered as plain callables rather than as entity method names, which is
# what lets an action target an entity class that knows nothing about it. Home
# Assistant hands a callable handler the entity and the whole ServiceCall (only
# the method-name form receives the fields as keyword arguments), so each
# handler reads its own fields off ``call.data`` — schema defaults already
# filled in, target keys ignored.
# ---------------------------------------------------------------------------


async def _async_analyze_usage(entity: Entity, call: ServiceCall) -> ServiceResponse:
    """Return the targeted device's full analytics result.

    With ``refresh`` set the engine recomputes first, so an automation can act
    on the state of *this* moment rather than on the nightly pass; without it
    the published result is returned untouched and the call costs nothing. An
    engine that has never completed a pass has no result to serialize, which is
    reported as a validation error rather than as an empty payload.
    """
    sensor = _analytics_sensor(entity, call)
    if sensor is None:
        return None
    engine = sensor.coordinator
    if call.data[ATTR_REFRESH]:
        await engine.async_refresh()
    result: AnalyticsResult | None = engine.data
    if result is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="analytics_not_ready"
        )
    return _serialize_result(result)


async def _async_get_usage_forecast(
    entity: Entity, call: ServiceCall
) -> ServiceResponse:
    """Return the coming days' expected usage for the targeted device.

    Computed on demand from the imported history rather than read off the
    engine's published result, which only ever carries tomorrow. The recorder
    read this needs is the one failure mode: it arrives as ``UpdateFailed`` and
    is re-raised as a user-facing error carrying the same cause.
    """
    sensor = _analytics_sensor(entity, call)
    if sensor is None:
        return None
    engine = sensor.coordinator
    try:
        forecasts = await engine.async_compute_forecasts(call.data[ATTR_DAYS])
    except UpdateFailed as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="forecast_failed",
            translation_placeholders={"message": str(err)},
        ) from err
    return {
        "forecasts": [_serialize_forecast(day, forecast) for day, forecast in forecasts]
    }


async def _async_set_vacation_mode(entity: Entity, call: ServiceCall) -> None:
    """Turn the targeted device's regeneration deferral on or off.

    Deferral is not the iQua app's vacation tile (those command payloads remain
    unverified): while it is on, a regeneration the device schedules for itself
    is cancelled again until the resin-hygiene cap lets one through, and turning
    it off schedules a catch-up recharge when capacity has run low.
    """
    switch = _vacation_switch(entity)
    await switch.async_set_vacation_mode(call.data[ATTR_VACATION])


async def _async_schedule_regeneration(entity: Entity, call: ServiceCall) -> None:
    """Schedule, start, or cancel a regeneration on the targeted device.

    The device's own guidance is honoured before anything is sent: an explicit
    ``can_schedule`` / ``can_recharge`` refusal turns into a validation error
    instead of a command the cloud would reject. An absent hint means allowed —
    the same reading the regeneration buttons apply to their availability.
    """
    button = _regeneration_button(entity)
    if (
        not _explicitly_targeted(entity, call)
        and button.entity_description.key != _REGEN_FANOUT_KEY
    ):
        # A device/area/label target expands to every regeneration button of
        # the device; letting each of them act would triple the command on a
        # throttled cloud. Exactly one designated button acts per device.
        return
    device = button.coordinator.data
    mode: str = call.data[ATTR_MODE]
    blocked = (mode == REGEN_MODE_SCHEDULE and _can_schedule(device) is False) or (
        mode == REGEN_MODE_NOW and _can_recharge(device) is False
    )
    if blocked:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="regen_not_allowed"
        )
    await async_execute_command(
        button.coordinator.client,
        button.coordinator.device_id,
        _REGEN_FUNCTION,
        _REGEN_ACTIONS[mode],
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the four AquaHome actions on the service registry.

    Called once from ``async_setup``: the registry is global, the handlers
    resolve their targets per call, and the platform-entity registration keeps
    each action bound to the entity domain that can actually serve it.
    """
    service_helper.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_ANALYZE_USAGE,
        entity_domain=Platform.SENSOR,
        schema={vol.Optional(ATTR_REFRESH, default=False): cv.boolean},
        func=_async_analyze_usage,
        supports_response=SupportsResponse.ONLY,
    )
    service_helper.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_GET_USAGE_FORECAST,
        entity_domain=Platform.SENSOR,
        schema={
            vol.Optional(ATTR_DAYS, default=1): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=FORECAST_MAX_DAYS)
            )
        },
        func=_async_get_usage_forecast,
        supports_response=SupportsResponse.ONLY,
    )
    service_helper.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_VACATION_MODE,
        entity_domain=Platform.SWITCH,
        schema={vol.Required(ATTR_VACATION): cv.boolean},
        func=_async_set_vacation_mode,
    )
    service_helper.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SCHEDULE_REGENERATION,
        entity_domain=Platform.BUTTON,
        schema={
            vol.Optional(ATTR_MODE, default=REGEN_MODE_SCHEDULE): vol.In(
                (REGEN_MODE_SCHEDULE, REGEN_MODE_NOW, REGEN_MODE_CANCEL)
            )
        },
        func=_async_schedule_regeneration,
    )
