"""Cross-platform smoke test for the Phase-3 surface.

Everything else in the suite isolates one platform per module; this file boots
the integration exactly as Home Assistant would — all platforms forwarded, real
captured fixtures behind ``aioresponses`` — and follows one freshly arrived
alert through every consumer at once: the ``aquahome_event`` bus event, the
alert event entity, and the latest-alert sensor. It also pins the full entity
inventory per domain so an accidentally dropped platform or description is
caught by a single, readable count.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_OFF, STATE_UNKNOWN
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.aquahome.const import (
    ACTIVITY_UPDATE_INTERVAL,
    DOMAIN,
    EVENT_AQUAHOME,
)
from tests.conftest import (
    alerts_url,
    device_url,
    devices_url,
    load_fixture,
    regen_events_url,
    settings_url,
    setup_integration,
)

if TYPE_CHECKING:
    from aioresponses import aioresponses
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import Event, HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

#: Slug derived from the fixture serial ``7384243-20203-1120``.
SLUG = "7384243_20203_1120"

#: Entities each platform creates from the captured dev-device fixtures.
#: Buttons: the three regeneration commands, refresh-data, and the two
#: advanced service buttons (silence-alarm and the WSOV reset are feature-gated
#: off on the dev device). Selects: 17 select settings minus the two chem-feed
#: settings conditionally hidden while ``aux_control_type`` is 0. The dev
#: device has no valve, leak detectors, number settings, or switch settings.
#: Sensors: the Phase-3 set of 30 plus the five Phase-6 salt sensors (daily
#: usage, days remaining, depletion timestamp, per-regeneration, efficiency).
EXPECTED_SENSORS = 37
EXPECTED_BINARY_SENSORS = 14
EXPECTED_EVENTS = 1
EXPECTED_BUTTONS = 6
EXPECTED_SELECTS = 15

#: The alert that "arrives" between the setup refresh and the next poll.
FRESH_ALERT: dict[str, Any] = {
    "id": "smoke-fresh-alert",
    "type": "excessive_water_use_alert",
    "title": "Excessive Water Use",
    "message": "Excessive water use alert",
    "level": "warning",
    "timestamp": "2026-07-21T09:00:00Z",
    "is_read": False,
}


async def test_full_integration_alert_flow(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Boot every platform and follow one new alert through all its consumers."""
    freezer.move_to("2026-07-21T12:00:00+00:00")
    mock_api.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock_api.get(device_url(), payload=load_fixture("device-detail.json"), repeat=True)
    mock_api.get(
        regen_events_url(),
        payload=load_fixture("regeneration-events.json"),
        repeat=True,
    )
    mock_api.get(settings_url(), payload=load_fixture("settings.json"), repeat=True)
    mock_api.get(alerts_url(), payload=load_fixture("alerts.json"))
    later_alerts = copy.deepcopy(load_fixture("alerts.json"))
    later_alerts["alerts"].insert(0, FRESH_ALERT)
    mock_api.get(alerts_url(), payload=later_alerts, repeat=True)

    bus_events: list[Event[Any]] = []

    @callback
    def _capture(event: Event[Any]) -> None:
        """Collect fired bus events synchronously, preserving fire order."""
        bus_events.append(event)

    hass.bus.async_listen(EVENT_AQUAHOME, _capture)

    assert await setup_integration(hass, mock_config_entry)

    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, mock_config_entry.entry_id)
    by_domain: dict[str, int] = {}
    for entry in entries:
        by_domain[entry.domain] = by_domain.get(entry.domain, 0) + 1
    assert by_domain == {
        "sensor": EXPECTED_SENSORS,
        "binary_sensor": EXPECTED_BINARY_SENSORS,
        "event": EXPECTED_EVENTS,
        "button": EXPECTED_BUTTONS,
        "select": EXPECTED_SELECTS,
    }

    # Idle dev device: no regeneration in progress, countdown force-zeroed, and
    # the watermark-seeding first refresh fired nothing.
    assert _state(hass, registry, "binary_sensor", "regenerating") == STATE_OFF
    assert _state(hass, registry, "sensor", "regeneration_status") == "none"
    assert _state(hass, registry, "sensor", "regeneration_time_remaining") == "0"
    assert _state(hass, registry, "event", "alert") == STATE_UNKNOWN
    assert bus_events == []

    # The next activity poll sees the freshly arrived alert.
    freezer.tick(ACTIVITY_UPDATE_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert len(bus_events) == 1
    payload = bus_events[0].data
    assert payload["device"] == SLUG
    assert payload["type"] == "alert"
    assert payload["alert_id"] == FRESH_ALERT["id"]
    assert payload["alert_type"] == FRESH_ALERT["type"]

    alert_event = hass.states.get(_entity_id(registry, "event", "alert"))
    assert alert_event is not None
    assert alert_event.state not in (STATE_UNKNOWN, "")
    assert alert_event.attributes["event_type"] == FRESH_ALERT["type"]

    latest = hass.states.get(_entity_id(registry, "sensor", "latest_alert"))
    assert latest is not None
    assert latest.state == FRESH_ALERT["message"]
    assert latest.attributes["alert_id"] == FRESH_ALERT["id"]


def _entity_id(registry: er.EntityRegistry, domain: str, key: str) -> str:
    """Resolve an entity id from its platform domain and unique-id suffix."""
    entity_id = registry.async_get_entity_id(domain, DOMAIN, f"{SLUG}_{key}")
    assert entity_id is not None, f"{domain} entity {key} was not registered"
    return entity_id


def _state(
    hass: HomeAssistant, registry: er.EntityRegistry, domain: str, key: str
) -> str:
    """Return the current state string of the entity with unique-id suffix ``key``."""
    state = hass.states.get(_entity_id(registry, domain, key))
    assert state is not None, f"{domain} entity {key} has no state"
    return state.state
