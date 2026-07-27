"""The logbook describer renders every event type it registers for.

The describer is pure — it registers one callback for the ``aquahome_event``
bus event and turns each payload into a name/message pair — so this suite
drives it directly through :func:`~custom_components.aquahome.logbook.
async_describe_events`, exactly the way the logbook component would, without a
Home Assistant instance. What matters is totality: every known type renders a
real sentence with a full payload *and* with an empty one, the scheduled-
regeneration row explains its reason, and no payload — however malformed — can
make the callback raise, because the logbook processor catches a describer
exception and silently drops the row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest
from homeassistant.core import Event

from custom_components.aquahome import logbook
from custom_components.aquahome.const import (
    DOMAIN,
    EVENT_AQUAHOME,
    REGEN_REASON_CATCH_UP,
    REGEN_REASON_LOW_CAPACITY,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from homeassistant.core import HomeAssistant

type Describer = Callable[[Event[Mapping[str, Any]]], dict[str, str]]

#: A payload carrying every detail field any renderer reads.
_FULL_PAYLOAD: dict[str, Any] = {
    "device": "softener",
    "tier": "urgent",
    "rate_liters_per_hour": 3.25,
    "implied_liters_per_day": 78.0,
    "reasons": ["daily_high", "drift"],
    "consecutive_days": 4,
    "since": "2026-07-20",
    "capacity_gallons": 120.4,
    "forecast_gallons": 250.9,
    "deferral_source": "manual",
    "days_deferred": 8,
}


def _describer() -> Describer:
    """Register the describer the way the logbook component does."""
    captured: dict[str, Any] = {}

    def _async_describe_event(
        domain: str, event_name: str, describe: Describer
    ) -> None:
        captured.update(domain=domain, event_name=event_name, describe=describe)

    logbook.async_describe_events(cast("HomeAssistant", None), _async_describe_event)
    # A rename or a wrong event name silently unlabels every row in production.
    assert captured["domain"] == DOMAIN
    assert captured["event_name"] == EVENT_AQUAHOME
    return cast("Describer", captured["describe"])


def _render(data: dict[str, Any]) -> dict[str, str]:
    """Render one payload through the registered describer."""
    return _describer()(Event(EVENT_AQUAHOME, data))


@pytest.mark.parametrize("event_type", sorted(logbook._MESSAGES))
def test_known_type_renders_with_and_without_detail(event_type: str) -> None:
    """Every known type names the device and reads correctly with no detail."""
    full = _render({"type": event_type, **_FULL_PAYLOAD})
    assert full["name"] == "softener"
    assert full["message"]
    assert "unknown" not in full["message"]

    bare = _render({"type": event_type})
    assert bare["name"] == "AquaHome"
    assert bare["message"]
    assert "None" not in bare["message"]


@pytest.mark.parametrize(
    ("reason", "fragment"),
    [
        (REGEN_REASON_LOW_CAPACITY, "below tomorrow's forecast"),
        (REGEN_REASON_CATCH_UP, "catch up"),
        ("something_new", "scheduled a regeneration"),
    ],
)
def test_regen_scheduled_explains_its_reason(reason: str, fragment: str) -> None:
    """The scheduled-regeneration row says why, including for an unknown reason."""
    message = _render({"type": "regen_scheduled", "reason": reason, **_FULL_PAYLOAD})[
        "message"
    ]
    assert fragment in message


def test_unknown_type_gets_the_generic_message() -> None:
    """A type no renderer claims falls back to a message naming the raw type."""
    entry = _render({"type": "salt_level_2", "device": "softener"})
    assert entry["name"] == "softener"
    assert "salt_level_2" in entry["message"]


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"type": "alert", "device": "softener"},
        {
            "type": "leak_suspected",
            "device": 5,
            "tier": None,
            "rate_liters_per_hour": True,
            "implied_liters_per_day": "x",
            "reasons": "not-a-list",
        },
    ],
)
def test_describer_never_raises(data: dict[str, Any]) -> None:
    """A describer runs inside the history query: it must render, never raise."""
    entry = _render(data)
    assert entry["name"]
    assert entry["message"]
