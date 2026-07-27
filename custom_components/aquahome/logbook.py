"""Logbook descriptions for the AquaHome bus events.

Everything the integration decides on its own — the nightly analytics verdicts,
the automation tier's regeneration commands, the alerts the cloud raises — is
announced as a single :data:`~.const.EVENT_AQUAHOME` bus event carrying a
``type`` discriminator. Those events are the automation surface (blueprints
trigger on them), but on their own they are invisible to the user: the logbook
shows raw event rows without a describer. This module turns each one into a
sentence, so the answer to "why did my softener regenerate last night?" is one
line in the device's own logbook rather than a debug log.

The describer is deliberately total and defensive. Every known ``type`` gets a
purpose-written message; anything else — the cloud alert events, and any type a
future version adds — falls back to a generic message naming the raw type. Every
payload field is read through a type-checking accessor and every message reads
correctly with all of them absent, because a logbook describer runs inside the
history query: the processor catches a describer exception and silently drops
the row, so anything less than total rendering makes events vanish unlabeled.

The two entry keys come from the logbook component's ``const`` module rather
than its package root — the same constants, imported from where they are
defined so the strict type checker sees an explicit export.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.logbook.const import (
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
)
from homeassistant.core import callback

from .const import (
    DOMAIN,
    EVENT_AQUAHOME,
    EVENT_TYPE_LEAK_CLEARED,
    EVENT_TYPE_LEAK_SUSPECTED,
    EVENT_TYPE_LEAK_WHILE_AWAY,
    EVENT_TYPE_REGEN_DEFERRAL_EXPIRED,
    EVENT_TYPE_REGEN_DEFERRED,
    EVENT_TYPE_REGEN_SCHEDULED,
    EVENT_TYPE_USAGE_ANOMALY,
    EVENT_TYPE_USAGE_ANOMALY_CLEARED,
    EVENT_TYPE_VACATION_ENDED,
    EVENT_TYPE_VACATION_STARTED,
    REGEN_REASON_CATCH_UP,
    REGEN_REASON_LOW_CAPACITY,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from typing import Any

    from homeassistant.core import Event, HomeAssistant

#: Name shown when an event carries no device label (never expected, but the
#: describer must render *something* rather than raise).
_FALLBACK_NAME = "AquaHome"

#: Type shown for an event without a usable ``type`` field.
_UNKNOWN_TYPE = "unknown"


def _text(value: object) -> str | None:
    """Return ``value`` when it is a non-empty string, else ``None``."""
    return value if isinstance(value, str) and value else None


def _number(value: object) -> float | None:
    """Return ``value`` as a float when it is numeric, else ``None``.

    Booleans are excluded on purpose: ``True`` is an ``int`` in Python and
    would otherwise render as a quantity of one.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _strings(value: object) -> list[str]:
    """Return the non-empty strings in a payload sequence, else an empty list.

    Event payloads survive a JSON round-trip through the recorder, so a tuple
    fired on the bus is read back as a list — both are accepted.
    """
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _event_type(data: Mapping[str, Any]) -> str:
    """Return the event's type discriminator, or a placeholder when absent."""
    return _text(data.get("type")) or _UNKNOWN_TYPE


def _device_name(data: Mapping[str, Any]) -> str:
    """Return the logbook entry name: the device the event is about."""
    return _text(data.get("device")) or _FALLBACK_NAME


def _leak_evidence(data: Mapping[str, Any]) -> str:
    """Return the parenthesised evidence shared by the two leak messages.

    Tier, hourly rate and implied daily loss are each included only when the
    payload carries them, and the whole suffix disappears when none does.
    """
    parts: list[str] = []
    tier = _text(data.get("tier"))
    if tier is not None:
        parts.append(f"{tier} tier")
    rate = _number(data.get("rate_liters_per_hour"))
    if rate is not None:
        parts.append(f"{rate:.1f} L/h")
    implied = _number(data.get("implied_liters_per_day"))
    if implied is not None:
        parts.append(f"about {implied:.0f} L/day")
    return f" ({', '.join(parts)})" if parts else ""


def _leak_suspected(data: Mapping[str, Any]) -> str:
    """Describe the nightly detector starting to suspect a leak."""
    return f"suspected a leak{_leak_evidence(data)}"


def _leak_cleared(data: Mapping[str, Any]) -> str:
    """Describe the suspected leak going away again."""
    return "reported the suspected leak cleared"


def _leak_while_away(data: Mapping[str, Any]) -> str:
    """Describe a leak suspected while the household is away."""
    return f"suspected a leak while the household is away{_leak_evidence(data)}"


def _usage_anomaly(data: Mapping[str, Any]) -> str:
    """Describe unusual water usage, listing the reasons that triggered it."""
    reasons = _strings(data.get("reasons"))
    detail = f" ({', '.join(reasons)})" if reasons else ""
    return f"reported unusual water usage{detail}"


def _usage_anomaly_cleared(data: Mapping[str, Any]) -> str:
    """Describe water usage returning to the learned expectation."""
    return "reported water usage back to normal"


def _vacation_started(data: Mapping[str, Any]) -> str:
    """Describe the vacation detector concluding the household is away."""
    days = _number(data.get("consecutive_days"))
    detail = f" ({days:.0f} quiet days)" if days is not None else ""
    return f"detected the household is away{detail}"


def _vacation_ended(data: Mapping[str, Any]) -> str:
    """Describe the vacation detector seeing normal usage return."""
    return "detected the household is back home"


def _regen_scheduled(data: Mapping[str, Any]) -> str:
    """Describe a regeneration the automation tier scheduled, and why."""
    reason = _text(data.get("reason"))
    if reason == REGEN_REASON_LOW_CAPACITY:
        why = " because the remaining capacity is below tomorrow's forecast"
    elif reason == REGEN_REASON_CATCH_UP:
        why = " to catch up after the vacation deferral ended"
    else:
        why = ""
    capacity = _number(data.get("capacity_gallons"))
    forecast = _number(data.get("forecast_gallons"))
    detail = (
        f" ({capacity:.0f} gal left, {forecast:.0f} gal forecast)"
        if capacity is not None and forecast is not None
        else ""
    )
    return f"scheduled a regeneration{why}{detail}"


def _regen_deferred(data: Mapping[str, Any]) -> str:
    """Describe a scheduled regeneration cancelled by an active deferral."""
    source = _text(data.get("deferral_source"))
    which = f"{source} vacation deferral" if source is not None else "vacation deferral"
    return f"cancelled the scheduled regeneration ({which})"


def _regen_deferral_expired(data: Mapping[str, Any]) -> str:
    """Describe a deferral hitting the cap and letting a regeneration through."""
    days = _number(data.get("days_deferred"))
    detail = f" after {days:.0f} days" if days is not None else ""
    return (
        f"let the scheduled regeneration through{detail} "
        "(the vacation deferral reached its resin-hygiene limit)"
    )


def _generic(data: Mapping[str, Any]) -> str:
    """Describe any other event by naming its raw type.

    This is what renders the cloud alert events — whose own entity already
    carries the alert text — and anything a future version starts firing. The
    raw type is quoted rather than prettified so an unrecognised event is still
    traceable back to the code that fired it.
    """
    return f"reported the '{_event_type(data)}' event"


#: One message builder per known event type; everything else uses ``_generic``.
_MESSAGES: dict[str, Callable[[Mapping[str, Any]], str]] = {
    EVENT_TYPE_LEAK_SUSPECTED: _leak_suspected,
    EVENT_TYPE_LEAK_CLEARED: _leak_cleared,
    EVENT_TYPE_LEAK_WHILE_AWAY: _leak_while_away,
    EVENT_TYPE_USAGE_ANOMALY: _usage_anomaly,
    EVENT_TYPE_USAGE_ANOMALY_CLEARED: _usage_anomaly_cleared,
    EVENT_TYPE_VACATION_STARTED: _vacation_started,
    EVENT_TYPE_VACATION_ENDED: _vacation_ended,
    EVENT_TYPE_REGEN_SCHEDULED: _regen_scheduled,
    EVENT_TYPE_REGEN_DEFERRED: _regen_deferred,
    EVENT_TYPE_REGEN_DEFERRAL_EXPIRED: _regen_deferral_expired,
}


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[
        [str, str, Callable[[Event[Mapping[str, Any]]], dict[str, str]]], None
    ],
) -> None:
    """Teach the logbook how to render the integration's bus events."""

    @callback
    def async_describe_aquahome_event(
        event: Event[Mapping[str, Any]],
    ) -> dict[str, str]:
        """Render one ``aquahome_event`` as a logbook entry."""
        data = event.data
        message = _MESSAGES.get(_event_type(data), _generic)
        return {
            LOGBOOK_ENTRY_NAME: _device_name(data),
            LOGBOOK_ENTRY_MESSAGE: message(data),
        }

    async_describe_event(DOMAIN, EVENT_AQUAHOME, async_describe_aquahome_event)
