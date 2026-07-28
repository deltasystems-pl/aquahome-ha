"""Tests for the AquaHome event platform.

These are end-to-end integration tests: a real config entry is set up against
the captured iQua fixtures served through ``aioresponses`` and the resulting
``alert`` event entity is inspected via the state machine and entity registry.
Only the ``event`` platform is forwarded so the tests depend on nothing but the
event platform, the activity coordinator, and the shared foundation — peer
platforms are left out.

The activity coordinator seeds its watermark on the first refresh (so a fresh
setup never replays the backlog) and only surfaces alerts unseen since the
previous page as :attr:`~coordinator.DeviceActivity.new_alerts`. To exercise a
newly arriving alert, the alerts route serves the plain fixture on the setup
refresh and a fixture with an extra alert prepended on every later refresh; the
activity coordinator is then driven by advancing the frozen clock one
:data:`~const.ACTIVITY_UPDATE_INTERVAL` and firing the time-changed listener.
Time is frozen throughout so each event's recorded trigger timestamp is
deterministic.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant.components.event import ATTR_EVENT_TYPE, ATTR_EVENT_TYPES
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, Platform
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.aquahome.const import (
    ACTIVITY_UPDATE_INTERVAL,
    ALERT_EVENT_TYPE_OTHER,
    DOMAIN,
    EVENT_AQUAHOME,
    KNOWN_ALERT_TYPES,
)
from tests.conftest import (
    alerts_url,
    device_url,
    devices_url,
    load_fixture,
    regen_events_url,
    setup_integration,
)

if TYPE_CHECKING:
    from aioresponses import aioresponses
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

#: Slug derived from the fixture serial ``4213377-30105-2242``.
SLUG = "4213377_30105_2242"
#: Fixed instant every setup test freezes to (2026-07-21T12:00:00Z).
FROZEN_INSTANT = "2026-07-21T12:00:00+00:00"

#: A brand-new alert of a catalogued type, newer than any in the fixture.
NEW_KNOWN_ALERT: dict[str, Any] = {
    "id": "new-alert-known",
    "type": "salt_level_2",
    "title": "Low Salt",
    "message": "Salt level is low",
    "level": "info",
    "timestamp": "2026-07-20T00:00:00Z",
    "is_read": False,
}
#: A brand-new alert whose vendor type is not catalogued (maps to ``other``).
NEW_UNKNOWN_ALERT: dict[str, Any] = {
    "id": "new-alert-unknown",
    "type": "furnace_overheated",
    "title": "Furnace overheated",
    "message": "An uncatalogued vendor alert",
    "level": "warning",
    "timestamp": "2026-07-20T06:00:00Z",
    "is_read": True,
}

#: Narrow the forwarded platforms to the event platform for isolated setup.
_ONLY_EVENT = patch("custom_components.aquahome.PLATFORMS", [Platform.EVENT])


def _register_routes(
    mock: aioresponses,
    *,
    alerts_first: dict[str, Any],
    alerts_rest: dict[str, Any] | None = None,
) -> None:
    """Register the read routes an event-only setup and its refreshes hit.

    ``alerts_first`` answers the setup refresh (seeding the watermark);
    ``alerts_rest`` (defaulting to ``alerts_first``) answers every later refresh,
    so a distinct payload there models a freshly arrived alert. Device, device
    list, and regeneration routes repeat the plain fixtures.
    """
    mock.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock.get(device_url(), payload=load_fixture("device-detail.json"), repeat=True)
    mock.get(
        regen_events_url(),
        payload=load_fixture("regeneration-events.json"),
        repeat=True,
    )
    mock.get(alerts_url(), payload=alerts_first)
    mock.get(alerts_url(), payload=alerts_rest or alerts_first, repeat=True)


def _alerts_with(*prepended: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of the alerts fixture with alerts prepended newest-first.

    The arguments are given oldest-first for readability and inserted at the head
    in that order, so the last argument ends up as the newest (index 0) alert.
    """
    payload = copy.deepcopy(load_fixture("alerts.json"))
    for alert in prepended:
        payload["alerts"].insert(0, copy.deepcopy(alert))
    return payload


def _entity_id(hass: HomeAssistant) -> str:
    """Resolve the alert event entity id via its unique id."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("event", DOMAIN, f"{SLUG}_alert")
    assert entity_id is not None, "alert event entity was not registered"
    return entity_id


def _entity_state(hass: HomeAssistant) -> str:
    """Return the alert event entity's current state string."""
    state = hass.states.get(_entity_id(hass))
    assert state is not None, "alert event entity has no state"
    return state.state


async def _advance_activity(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Advance one activity interval and let the activity refresh run."""
    freezer.tick(ACTIVITY_UPDATE_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Creation / initial state
# ---------------------------------------------------------------------------


async def test_event_entity_created_unknown_initially(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """One alert event entity exists per device and starts out ``unknown``.

    Its declared ``event_types`` are exactly the catalogued alert types plus the
    catch-all — nothing has fired yet, so it has no last event type.
    """
    freezer.move_to(FROZEN_INSTANT)
    _register_routes(mock_api, alerts_first=load_fixture("alerts.json"))
    with _ONLY_EVENT:
        await setup_integration(hass, mock_config_entry)

    state = hass.states.get(_entity_id(hass))
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes[ATTR_EVENT_TYPES] == [
        *KNOWN_ALERT_TYPES,
        ALERT_EVENT_TYPE_OTHER,
    ]
    assert state.attributes[ATTR_EVENT_TYPE] is None


# ---------------------------------------------------------------------------
# A newly arriving alert triggers the entity
# ---------------------------------------------------------------------------


async def test_new_known_alert_updates_state_and_records_type(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A new catalogued alert moves the state off ``unknown`` and records its type."""
    freezer.move_to(FROZEN_INSTANT)
    _register_routes(
        mock_api,
        alerts_first=load_fixture("alerts.json"),
        alerts_rest=_alerts_with(NEW_KNOWN_ALERT),
    )
    with _ONLY_EVENT:
        await setup_integration(hass, mock_config_entry)
        assert _entity_state(hass) == STATE_UNKNOWN
        await _advance_activity(hass, freezer)

    state = hass.states.get(_entity_id(hass))
    assert state is not None
    assert state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE)
    assert state.attributes[ATTR_EVENT_TYPE] == "salt_level_2"
    assert state.attributes["alert_type"] == "salt_level_2"
    assert state.attributes["alert_id"] == "new-alert-known"
    assert state.attributes["title"] == "Low Salt"
    assert state.attributes["message"] == "Salt level is low"
    assert state.attributes["level"] == "info"
    assert state.attributes["timestamp"] == "2026-07-20T00:00:00+00:00"
    assert state.attributes["is_read"] is False


async def test_unknown_alert_type_maps_to_other_preserving_raw(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An uncatalogued vendor type fires as ``other`` with the raw type retained."""
    freezer.move_to(FROZEN_INSTANT)
    _register_routes(
        mock_api,
        alerts_first=load_fixture("alerts.json"),
        alerts_rest=_alerts_with(NEW_UNKNOWN_ALERT),
    )
    with _ONLY_EVENT:
        await setup_integration(hass, mock_config_entry)
        await _advance_activity(hass, freezer)

    state = hass.states.get(_entity_id(hass))
    assert state is not None
    assert state.attributes[ATTR_EVENT_TYPE] == ALERT_EVENT_TYPE_OTHER
    assert state.attributes["alert_type"] == "furnace_overheated"
    assert state.attributes["alert_id"] == "new-alert-unknown"


async def test_two_new_alerts_in_one_refresh_end_on_newest(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Two alerts in a single refresh replay in order and rest on the newest.

    ``new_alerts`` is ordered oldest-to-newest, so the entity ends on the newer
    of the two even though both fired in the same coordinator update.
    """
    freezer.move_to(FROZEN_INSTANT)
    older = {**NEW_UNKNOWN_ALERT, "id": "new-A", "timestamp": "2026-07-19T00:00:00Z"}
    newer = {**NEW_KNOWN_ALERT, "id": "new-B", "timestamp": "2026-07-20T00:00:00Z"}
    _register_routes(
        mock_api,
        alerts_first=load_fixture("alerts.json"),
        alerts_rest=_alerts_with(older, newer),
    )
    bus_events: list[Any] = []

    @callback
    def _capture(event: Any) -> None:
        """Collect fired bus events synchronously, preserving fire order."""
        bus_events.append(event)

    hass.bus.async_listen(EVENT_AQUAHOME, _capture)
    with _ONLY_EVENT:
        await setup_integration(hass, mock_config_entry)
        await _advance_activity(hass, freezer)

    # Both alerts fanned out, oldest first — not only the final resting state.
    assert [event.data["alert_id"] for event in bus_events] == ["new-A", "new-B"]
    state = hass.states.get(_entity_id(hass))
    assert state is not None
    assert state.attributes["alert_id"] == "new-B"
    assert state.attributes[ATTR_EVENT_TYPE] == "salt_level_2"


# ---------------------------------------------------------------------------
# No spurious re-triggering
# ---------------------------------------------------------------------------


async def test_refresh_without_new_alerts_does_not_retrigger(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A later refresh that brings no unseen alert leaves the state untouched.

    After a first new alert moves the state to a trigger timestamp, a subsequent
    refresh serving the same page must not fire again — the recorded timestamp
    stays put even though the clock advanced.
    """
    freezer.move_to(FROZEN_INSTANT)
    _register_routes(
        mock_api,
        alerts_first=load_fixture("alerts.json"),
        alerts_rest=_alerts_with(NEW_KNOWN_ALERT),
    )
    with _ONLY_EVENT:
        await setup_integration(hass, mock_config_entry)
        await _advance_activity(hass, freezer)
        triggered = _entity_state(hass)
        assert triggered not in (STATE_UNKNOWN, STATE_UNAVAILABLE)

        await _advance_activity(hass, freezer)

    state = hass.states.get(_entity_id(hass))
    assert state is not None
    assert state.state == triggered
    assert state.attributes["alert_id"] == "new-alert-known"


async def test_serve_stale_refresh_does_not_refire_fired_alert(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A 429 poll right after a fired alert must not re-trigger the entity.

    The coordinator notifies listeners even when serving cached data; the
    cached view carries ``new_alerts=()`` so the entity's recorded trigger
    timestamp stays put across the stale cycle (adversarial-review finding:
    without the clear, every transient throttle re-fired the last alert and
    re-ran user automations).
    """
    freezer.move_to(FROZEN_INSTANT)
    mock_api.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock_api.get(device_url(), payload=load_fixture("device-detail.json"), repeat=True)
    mock_api.get(
        regen_events_url(),
        payload=load_fixture("regeneration-events.json"),
        repeat=True,
    )
    mock_api.get(alerts_url(), payload=load_fixture("alerts.json"))
    mock_api.get(alerts_url(), payload=_alerts_with(NEW_KNOWN_ALERT))
    mock_api.get(
        alerts_url(),
        status=429,
        payload={"code": "ThrottleLimitExceeded", "detail": "slow down"},
        repeat=True,
    )
    with _ONLY_EVENT:
        await setup_integration(hass, mock_config_entry)
        await _advance_activity(hass, freezer)
        triggered = _entity_state(hass)
        assert triggered not in (STATE_UNKNOWN, STATE_UNAVAILABLE)

        # The next poll 429s: the coordinator serves stale and still notifies.
        await _advance_activity(hass, freezer)

    state = hass.states.get(_entity_id(hass))
    assert state is not None
    assert state.state == triggered
    assert state.attributes["alert_id"] == "new-alert-known"


async def test_restart_does_not_replay_backlog(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A fresh setup seeds the watermark silently — the alert history never fires.

    The fixture page is full of historical alerts; neither the initial setup nor
    a reload replays any of them, because each fresh activity coordinator only
    establishes its watermark on the first refresh.
    """
    freezer.move_to(FROZEN_INSTANT)
    _register_routes(mock_api, alerts_first=load_fixture("alerts.json"))
    with _ONLY_EVENT:
        await setup_integration(hass, mock_config_entry)
        assert _entity_state(hass) == STATE_UNKNOWN

        await hass.config_entries.async_unload(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get(_entity_id(hass))
    assert state is not None
    assert state.state == STATE_UNKNOWN


# ---------------------------------------------------------------------------
# Registry identity
# ---------------------------------------------------------------------------


async def test_event_entity_registry_identity(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The entity registers under the slug-based unique id and the ``alert`` key."""
    freezer.move_to(FROZEN_INSTANT)
    _register_routes(mock_api, alerts_first=load_fixture("alerts.json"))
    with _ONLY_EVENT:
        await setup_integration(hass, mock_config_entry)

    registry = er.async_get(hass)
    entry = registry.async_get(_entity_id(hass))
    assert entry is not None
    assert entry.unique_id == f"{SLUG}_alert"
    assert entry.translation_key == "alert"
    assert entry.device_id is not None
