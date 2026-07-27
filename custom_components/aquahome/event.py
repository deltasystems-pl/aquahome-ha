"""Event platform for the AquaHome integration.

Each device gets one ``alert`` :class:`~homeassistant.components.event.EventEntity`
that surfaces the device's cloud alert feed as Home Assistant events. The
activity coordinator diffs every refresh against a watermark of seen alert ids
and exposes only the alerts observed for the first time on
:attr:`~.coordinator.DeviceActivity.new_alerts` — ordered oldest-to-newest, and
empty (``()``) on the first successful refresh so a fresh setup or a restart
never replays the backlog. This entity fires each such alert as one event.

A vendor alert ``type`` is normalised to one of the declared
:data:`~.const.KNOWN_ALERT_TYPES` or the catch-all
:data:`~.const.ALERT_EVENT_TYPE_OTHER`; the raw type is always preserved in the
event attributes so a not-yet-catalogued vendor string is never lost. Every read
is ``None``-safe: no coordinator data (or a refresh that brings no new alerts)
leaves the entity untouched.

Availability follows the activity coordinator only — inherited from
:class:`~.entity.AquaHomeActivityEntity`, the alert history is cloud-side and
stays valid while the softener itself is offline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.core import callback

from .const import ALERT_EVENT_TYPE_OTHER, KNOWN_ALERT_TYPES
from .entity import AquaHomeActivityEntity

if TYPE_CHECKING:
    from typing import Any

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .api import Alert
    from .coordinator import AquaHomeConfigEntry

# Read-only coordinator platform: entity updates never do their own I/O, so
# Home Assistant may run them unbounded (quality-scale parallel-updates rule).
PARALLEL_UPDATES = 0

#: The single alert event entity's description. ``event_types`` declares exactly
#: the catalogued vendor types plus the catch-all; a vendor string outside this
#: set maps to :data:`~.const.ALERT_EVENT_TYPE_OTHER` (its raw value kept in the
#: attributes), because Home Assistant rejects triggering an undeclared type.
ALERT_EVENT_DESCRIPTION: Final = EventEntityDescription(
    key="alert",
    translation_key="alert",
    event_types=[*KNOWN_ALERT_TYPES, ALERT_EVENT_TYPE_OTHER],
)


def _event_type(alert: Alert) -> str:
    """Return the declared event type for an alert.

    A catalogued vendor ``type`` passes through unchanged; anything else — an
    unrecognised string or an absent type — collapses to
    :data:`~.const.ALERT_EVENT_TYPE_OTHER` so the fired event always uses a
    declared type.
    """
    return alert.type if alert.type in KNOWN_ALERT_TYPES else ALERT_EVENT_TYPE_OTHER


def _event_attributes(alert: Alert) -> dict[str, Any]:
    """Return the event attributes carried alongside a fired alert.

    ``alert_type`` is the *raw* vendor type (preserved even when the event type
    was normalised to ``other``); the timestamp is an ISO-8601 string, or
    ``None`` when the alert carried no parseable timestamp.
    """
    return {
        "alert_id": alert.id,
        "alert_type": alert.type,
        "title": alert.title,
        "message": alert.message,
        "level": alert.level,
        "timestamp": alert.timestamp.isoformat()
        if alert.timestamp is not None
        else None,
        "is_read": alert.is_read,
    }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AquaHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one alert event entity per configured device.

    Mirrors the activity-sensor setup: each activity coordinator is paired with
    its fast telemetry coordinator's device view (same device-id key), which
    supplies the :class:`~homeassistant.helpers.device_registry.DeviceInfo` so the
    event entity attaches to the same device as the telemetry entities.
    """
    runtime = entry.runtime_data
    entities = [
        AquaHomeAlertEvent(
            activity, ALERT_EVENT_DESCRIPTION, runtime.coordinators[device_id].data
        )
        for device_id, activity in runtime.activity_coordinators.items()
    ]
    async_add_entities(entities)


class AquaHomeAlertEvent(AquaHomeActivityEntity, EventEntity):
    """Fire a Home Assistant event for every newly observed device alert."""

    entity_description: EventEntityDescription

    @callback
    def _handle_coordinator_update(self) -> None:
        """Fire one event per alert new since the previous activity refresh.

        ``new_alerts`` is already ordered oldest-to-newest, so replaying it in
        order leaves the entity resting on the newest alert. With no coordinator
        data or no new alerts, the base availability/write handling runs and
        nothing is triggered, so an unchanged refresh never re-fires and a fresh
        setup (whose first refresh reports no new alerts) starts out ``unknown``.
        """
        data = self.coordinator.data
        if data is None or not data.new_alerts:
            super()._handle_coordinator_update()
            return
        for alert in data.new_alerts:
            self._trigger_event(_event_type(alert), _event_attributes(alert))
            self.async_write_ha_state()
