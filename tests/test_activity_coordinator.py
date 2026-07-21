"""Tests for the AquaHome activity coordinator and its wiring.

Two layers are exercised. The bus-event, watermark, sort, and serve-stale
behaviour is driven against a directly-constructed
:class:`~custom_components.aquahome.coordinator.AquaHomeActivityCoordinator` (a
standalone client over ``aioresponses``, plus an injected monotonic clock for
the exact TTL boundaries) — the sanctioned exception to the end-to-end rule,
mirroring ``tests/test_coordinator.py``. The setup wiring (tolerant first
refresh, badge / regeneration triggers) is exercised end-to-end through
``setup_integration``.

The peer-owned ``event`` platform does not exist while this file is authored, so
every end-to-end test narrows the forwarded platforms to binary-sensor + sensor
by patching ``PLATFORMS`` at both the ``const`` definition and the ``__init__``
import site; production code keeps the full contract-exact platform list.
"""

from __future__ import annotations

import contextlib
import copy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntryState,
)
from homeassistant.const import Platform
from homeassistant.core import Event, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.aquahome.api import AquaHomeClient, AuthManager
from custom_components.aquahome.api.const import API_BASE_URL
from custom_components.aquahome.const import (
    ACTIVITY_MAX_STALE_SECONDS,
    DOMAIN,
    EVENT_AQUAHOME,
    UPDATE_INTERVAL,
)
from custom_components.aquahome.coordinator import (
    AquaHomeActivityCoordinator,
    DeviceActivity,
)
from tests.conftest import (
    TEST_DEVICE_ID,
    add_activity_routes,
    alerts_url,
    device_url,
    devices_url,
    load_fixture,
    make_access_token,
    regen_events_url,
    setup_integration,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aioresponses import aioresponses
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

#: Slug derived from the fixture serial ``7384243-20203-1120``.
SLUG = "7384243_20203_1120"
#: Fixed instant the end-to-end setup tests freeze to (2026-07-21T12:00:00Z).
FROZEN_INSTANT = "2026-07-21T12:00:00+00:00"
#: Newest alert id in ``alerts.json`` (the 2026-05-14 disconnection).
NEWEST_ALERT_ID = "db768cd7-b1f6-4c99-8910-5c6f2a999cbe"
#: Alert count on one page of ``alerts.json`` (per_page 20, total 59).
ALERT_PAGE_SIZE = 20
#: Regeneration-event count in ``regeneration-events.json``.
REGEN_EVENT_COUNT = 18
#: Start time of the newest regeneration in the fixture.
NEWEST_REGEN_START = datetime(2026, 7, 17, 0, 1, 1, tzinfo=UTC)

#: A brand-new alert, newer than any in the fixture, prepended to force a diff.
NEW_ALERT: dict[str, Any] = {
    "id": "new-alert-0001",
    "type": "salt_level_2",
    "title": "Low Salt",
    "message": "Salt level is low",
    "level": "info",
    "timestamp": "2026-07-20T00:00:00Z",
    "is_read": False,
}

#: Platforms forwarded while the peer-owned event platform is absent.
_ACTIVE_PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]


@contextlib.contextmanager
def _limit_platforms() -> Iterator[None]:
    """Narrow the forwarded platforms to those that exist during this test run."""
    with (
        patch("custom_components.aquahome.PLATFORMS", _ACTIVE_PLATFORMS),
        patch("custom_components.aquahome.const.PLATFORMS", _ACTIVE_PLATFORMS),
    ):
        yield


class _FakeMonotonic:
    """Advanceable monotonic clock for driving the serve-stale TTL directly."""

    def __init__(self, now: float = 1_000.0) -> None:
        """Start the clock at a fixed value."""
        self._now = now

    def __call__(self) -> float:
        """Return the current monotonic reading."""
        return self._now

    def advance(self, seconds: float) -> None:
        """Move the clock forward."""
        self._now += seconds


def _standalone_client(hass: HomeAssistant) -> AquaHomeClient:
    """Build a client whose auth already holds a fresh (non-refreshing) token."""
    session = async_get_clientsession(hass)
    auth = AuthManager(session, base_url=API_BASE_URL)
    auth.set_tokens(make_access_token(), "refresh-token-1")
    return AquaHomeClient(session, auth, base_url=API_BASE_URL, language="en")


def _activity_coordinator(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    client: AquaHomeClient | None = None,
    monotonic: _FakeMonotonic | None = None,
) -> AquaHomeActivityCoordinator:
    """Construct an activity coordinator bound to ``entry`` for the dev device."""
    kwargs: dict[str, Any] = {}
    if monotonic is not None:
        kwargs["monotonic"] = monotonic
    return AquaHomeActivityCoordinator(
        hass,
        entry,
        client or _standalone_client(hass),
        device_id=TEST_DEVICE_ID,
        device_slug=SLUG,
        **kwargs,
    )


def _capture_events(hass: HomeAssistant) -> list[Event]:
    """Subscribe to ``aquahome_event`` and collect every fired event."""
    events: list[Event] = []

    @callback
    def _listener(event: Event) -> None:
        events.append(event)

    hass.bus.async_listen(EVENT_AQUAHOME, _listener)
    return events


async def _fire_next_poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Advance one fast-coordinator interval and let the poll run to completion."""
    freezer.tick(UPDATE_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Happy path parsing
# ---------------------------------------------------------------------------


async def test_happy_path_parses_both_fixtures(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """A first refresh parses both fixtures into a populated DeviceActivity."""
    mock_config_entry.add_to_hass(hass)
    add_activity_routes(mock_api)
    coordinator = _activity_coordinator(hass, mock_config_entry)

    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    data = coordinator.data
    assert isinstance(data, DeviceActivity)
    assert len(data.alerts) == ALERT_PAGE_SIZE
    assert len(data.regeneration_events) == REGEN_EVENT_COUNT
    assert data.new_alerts == ()
    assert data.alerts[0].id == NEWEST_ALERT_ID
    assert data.regeneration_events[0].start_time == NEWEST_REGEN_START


# ---------------------------------------------------------------------------
# Bus events + watermark
# ---------------------------------------------------------------------------


async def test_first_refresh_fires_no_bus_events(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """The first successful refresh only seeds the watermark — no events fire."""
    mock_config_entry.add_to_hass(hass)
    add_activity_routes(mock_api)
    events = _capture_events(hass)
    coordinator = _activity_coordinator(hass, mock_config_entry)

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    assert events == []


async def test_new_alert_fires_single_event_with_pinned_payload(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """A single new alert on the next refresh fires exactly one pinned event."""
    mock_config_entry.add_to_hass(hass)
    first = load_fixture("alerts.json")
    second = copy.deepcopy(first)
    second["alerts"].insert(0, copy.deepcopy(NEW_ALERT))
    mock_api.get(
        regen_events_url(),
        payload=load_fixture("regeneration-events.json"),
        repeat=True,
    )
    mock_api.get(alerts_url(), payload=first)
    mock_api.get(alerts_url(), payload=second, repeat=True)

    coordinator = _activity_coordinator(hass, mock_config_entry)
    events = _capture_events(hass)

    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert events == []

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data == {
        "device_id": TEST_DEVICE_ID,
        "device": SLUG,
        "type": "alert",
        "alert_id": "new-alert-0001",
        "alert_type": "salt_level_2",
        "title": "Low Salt",
        "message": "Salt level is low",
        "level": "info",
        "timestamp": "2026-07-20T00:00:00+00:00",
    }
    data = coordinator.data
    assert data is not None
    assert [alert.id for alert in data.new_alerts] == ["new-alert-0001"]


async def test_duplicate_ids_across_refreshes_never_refire(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """An unchanged page on the second refresh yields no new alerts and no events."""
    mock_config_entry.add_to_hass(hass)
    add_activity_routes(mock_api)
    coordinator = _activity_coordinator(hass, mock_config_entry)
    events = _capture_events(hass)

    await coordinator.async_refresh()
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert events == []
    data = coordinator.data
    assert data is not None
    assert data.new_alerts == ()


async def test_serve_stale_clears_previously_fired_new_alerts(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """A stale poll right after a fired alert must not present it as new again.

    The base coordinator notifies listeners even when serving cached data, so a
    429 immediately after a real alert would re-trigger the alert event entity
    if ``new_alerts`` survived on the cached view (adversarial-review finding).
    """
    mock_config_entry.add_to_hass(hass)
    first = load_fixture("alerts.json")
    second = copy.deepcopy(first)
    second["alerts"].insert(0, copy.deepcopy(NEW_ALERT))
    mock_api.get(
        regen_events_url(),
        payload=load_fixture("regeneration-events.json"),
        repeat=True,
    )
    mock_api.get(alerts_url(), payload=first)
    mock_api.get(alerts_url(), payload=second)
    mock_api.get(
        alerts_url(),
        status=429,
        payload={"code": "ThrottleLimitExceeded", "detail": "slow down"},
        repeat=True,
    )
    coordinator = _activity_coordinator(hass, mock_config_entry)
    events = _capture_events(hass)

    await coordinator.async_refresh()  # seeds the watermark
    await coordinator.async_refresh()  # fires the new alert once
    await coordinator.async_refresh()  # 429 -> serve-stale
    await hass.async_block_till_done()

    assert len(events) == 1
    data = coordinator.data
    assert data is not None
    assert data.new_alerts == ()
    assert data.alerts[0].id == "new-alert-0001"


async def test_empty_page_glitch_never_replays_backlog(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """A successful-but-empty page must not make old alerts look new afterwards.

    Seen ids accumulate instead of being replaced by the current page, so even
    a server glitch returning an empty ``alerts`` array cannot cause the next
    full page to re-fire the whole backlog as new alerts.
    """
    mock_config_entry.add_to_hass(hass)
    full = load_fixture("alerts.json")
    empty = copy.deepcopy(full)
    empty["alerts"] = []
    mock_api.get(
        regen_events_url(),
        payload=load_fixture("regeneration-events.json"),
        repeat=True,
    )
    mock_api.get(alerts_url(), payload=full)
    mock_api.get(alerts_url(), payload=empty)
    mock_api.get(alerts_url(), payload=full, repeat=True)
    coordinator = _activity_coordinator(hass, mock_config_entry)
    events = _capture_events(hass)

    await coordinator.async_refresh()  # seeds from the full page
    await coordinator.async_refresh()  # glitched empty page
    await coordinator.async_refresh()  # full page again
    await hass.async_block_till_done()

    assert events == []
    data = coordinator.data
    assert data is not None
    assert data.new_alerts == ()


async def test_alerts_sorted_newest_first_tolerating_undated(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """An alert with no timestamp sorts last without breaking the newest-first order."""
    mock_config_entry.add_to_hass(hass)
    payload = copy.deepcopy(load_fixture("alerts.json"))
    payload["alerts"][3]["timestamp"] = None
    undated_id = payload["alerts"][3]["id"]
    mock_api.get(alerts_url(), payload=payload, repeat=True)
    mock_api.get(
        regen_events_url(),
        payload=load_fixture("regeneration-events.json"),
        repeat=True,
    )
    coordinator = _activity_coordinator(hass, mock_config_entry)

    await coordinator.async_refresh()

    data = coordinator.data
    assert data is not None
    assert len(data.alerts) == ALERT_PAGE_SIZE
    assert data.alerts[0].id == NEWEST_ALERT_ID
    assert data.alerts[-1].id == undated_id
    assert data.alerts[-1].timestamp is None


# ---------------------------------------------------------------------------
# Serve-stale error taxonomy (direct construction with injected monotonic)
# ---------------------------------------------------------------------------


async def test_serve_stale_on_rate_limit_within_ttl_keeps_data(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """Within the TTL a 429 returns cached activity and keeps entities available."""
    mock_config_entry.add_to_hass(hass)
    clock = _FakeMonotonic()
    coordinator = _activity_coordinator(hass, mock_config_entry, monotonic=clock)
    mock_api.get(alerts_url(), payload=load_fixture("alerts.json"))
    mock_api.get(
        regen_events_url(),
        payload=load_fixture("regeneration-events.json"),
        repeat=True,
    )

    await coordinator.async_refresh()
    assert coordinator.last_update_success is True
    good = coordinator.data
    assert good is not None

    clock.advance(ACTIVITY_MAX_STALE_SECONDS - 1.0)
    mock_api.get(
        alerts_url(),
        status=429,
        payload={"code": "ThrottleLimitExceeded", "detail": "slow down"},
        repeat=True,
    )

    result = await coordinator._async_update_data()
    # The cached history survives, but new_alerts is cleared: a failed poll
    # observed nothing new, and replaying the previous cycle's batch would
    # re-trigger the alert event entity (adversarial-review finding).
    assert result.alerts == good.alerts
    assert result.regeneration_events == good.regeneration_events
    assert result.new_alerts == ()
    assert coordinator._serving_stale is True

    # A scheduled refresh over the same failure keeps the entity available.
    await coordinator.async_refresh()
    assert coordinator.last_update_success is True
    assert coordinator.data is not None
    assert coordinator.data.alerts == good.alerts


async def test_past_ttl_raises_update_failed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """Past the TTL a failing poll drops the stale data and raises UpdateFailed."""
    mock_config_entry.add_to_hass(hass)
    clock = _FakeMonotonic()
    coordinator = _activity_coordinator(hass, mock_config_entry, monotonic=clock)
    mock_api.get(alerts_url(), payload=load_fixture("alerts.json"))
    mock_api.get(
        regen_events_url(),
        payload=load_fixture("regeneration-events.json"),
        repeat=True,
    )

    await coordinator.async_refresh()
    assert coordinator.data is not None

    clock.advance(ACTIVITY_MAX_STALE_SECONDS)
    mock_api.get(
        alerts_url(),
        status=429,
        payload={"code": "ThrottleLimitExceeded", "detail": "slow down"},
        repeat=True,
    )

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_auth_error_starts_reauth(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """A rejected token whose refresh also 401s starts Home Assistant reauth."""
    mock_config_entry.add_to_hass(hass)
    coordinator = _activity_coordinator(hass, mock_config_entry)
    mock_api.get(
        alerts_url(),
        status=401,
        payload={"code": "Unauthorized", "detail": "token expired"},
        repeat=True,
    )
    mock_api.get(
        regen_events_url(),
        payload=load_fixture("regeneration-events.json"),
        repeat=True,
    )
    mock_api.post(
        f"{API_BASE_URL}/auth/refresh",
        status=401,
        payload={"code": "AuthCannotRefreshToken", "detail": "refresh rejected"},
        repeat=True,
    )

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    reauth_flows = [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["handler"] == DOMAIN and flow["context"].get("source") == SOURCE_REAUTH
    ]
    assert len(reauth_flows) == 1


# ---------------------------------------------------------------------------
# Setup wiring: tolerant first refresh + triggers (end-to-end)
# ---------------------------------------------------------------------------


async def test_tolerant_setup_when_activity_feed_fails(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A 500 on the activity feed still yields a loaded entry, activity unavailable."""
    freezer.move_to(FROZEN_INSTANT)
    mock_api.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock_api.get(device_url(), payload=load_fixture("device-detail.json"), repeat=True)
    mock_api.get(
        alerts_url(),
        status=500,
        payload={"code": "ServerError", "detail": "boom"},
        repeat=True,
    )
    mock_api.get(
        regen_events_url(),
        status=500,
        payload={"code": "ServerError", "detail": "boom"},
        repeat=True,
    )

    with _limit_platforms():
        assert await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    activity = mock_config_entry.runtime_data.activity_coordinators[TEST_DEVICE_ID]
    assert activity.last_update_success is False
    assert activity.data is None


async def test_badge_count_increase_triggers_early_activity_refresh(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A rising alert badge on a fast poll requests an out-of-band activity refresh."""
    freezer.move_to(FROZEN_INSTANT)
    changed = copy.deepcopy(load_fixture("device-detail.json"))
    changed["enriched_data"]["water_treatment"]["water_treatment_status"][
        "alert_badge_count"
    ] = 2
    mock_api.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock_api.get(device_url(), payload=load_fixture("device-detail.json"))
    mock_api.get(device_url(), payload=changed, repeat=True)
    add_activity_routes(mock_api)

    with _limit_platforms():
        assert await setup_integration(hass, mock_config_entry)

    activity = mock_config_entry.runtime_data.activity_coordinators[TEST_DEVICE_ID]
    with patch.object(activity, "async_request_refresh", new_callable=AsyncMock) as spy:
        await _fire_next_poll(hass, freezer)

    assert spy.call_count == 1


async def test_badge_count_decrease_does_not_trigger_refresh(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A falling badge (alerts read in the app) must not spend an early refresh.

    The trigger is increase-only: a decrease carries no new-alert information,
    so reacting to any change would waste the throttled cloud budget on every
    read-in-app action (kills the ``>`` -> ``!=`` mutation).
    """
    freezer.move_to(FROZEN_INSTANT)
    high = copy.deepcopy(load_fixture("device-detail.json"))
    high["enriched_data"]["water_treatment"]["water_treatment_status"][
        "alert_badge_count"
    ] = 2
    low = copy.deepcopy(load_fixture("device-detail.json"))
    low["enriched_data"]["water_treatment"]["water_treatment_status"][
        "alert_badge_count"
    ] = 0
    mock_api.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock_api.get(device_url(), payload=high)
    mock_api.get(device_url(), payload=low, repeat=True)
    add_activity_routes(mock_api)

    with _limit_platforms():
        assert await setup_integration(hass, mock_config_entry)

    activity = mock_config_entry.runtime_data.activity_coordinators[TEST_DEVICE_ID]
    with patch.object(activity, "async_request_refresh", new_callable=AsyncMock) as spy:
        await _fire_next_poll(hass, freezer)

    assert spy.call_count == 0


async def test_regen_active_flip_triggers_early_activity_refresh(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A recharge tile flipping to regenerating requests an early activity refresh."""
    freezer.move_to(FROZEN_INSTANT)
    changed = copy.deepcopy(load_fixture("device-detail.json"))
    changed["enriched_data"]["water_treatment"]["recharge_ui"]["state"] = "regenerating"
    mock_api.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock_api.get(device_url(), payload=load_fixture("device-detail.json"))
    mock_api.get(device_url(), payload=changed, repeat=True)
    add_activity_routes(mock_api)

    with _limit_platforms():
        assert await setup_integration(hass, mock_config_entry)

    activity = mock_config_entry.runtime_data.activity_coordinators[TEST_DEVICE_ID]
    with patch.object(activity, "async_request_refresh", new_callable=AsyncMock) as spy:
        await _fire_next_poll(hass, freezer)

    assert spy.call_count == 1


async def test_steady_state_fast_poll_does_not_trigger_activity_refresh(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An unchanged fast poll (no badge rise, no regen flip) triggers nothing."""
    freezer.move_to(FROZEN_INSTANT)
    mock_api.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock_api.get(device_url(), payload=load_fixture("device-detail.json"), repeat=True)
    add_activity_routes(mock_api)

    with _limit_platforms():
        assert await setup_integration(hass, mock_config_entry)

    activity = mock_config_entry.runtime_data.activity_coordinators[TEST_DEVICE_ID]
    with patch.object(activity, "async_request_refresh", new_callable=AsyncMock) as spy:
        await _fire_next_poll(hass, freezer)

    assert spy.call_count == 0
