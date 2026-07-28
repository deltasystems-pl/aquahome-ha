"""Tests for the Tier-2 sensor additions (regeneration, weekday, activity feed).

These exercise the new fast-coordinator sensors (regeneration status / time
remaining / next regeneration, the seven per-weekday average-use slots, capacity,
hardness, lifetime salt & rock, error codes) and the two activity-coordinator
sensors (last regeneration, latest alert) end-to-end against the captured iQua
fixtures served through ``aioresponses``. Only the sensor platform is forwarded,
so every assertion runs the real coordinator-first-refresh path.

Time is frozen for every setup test so the ``next_regeneration`` timestamp
computation is deterministic, mirroring ``test_sensor.py``. Fixture payloads are
always deep-copied before mutation — the JSON files are never edited in place.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.util import dt as dt_util

from custom_components.aquahome.const import DOMAIN, MAX_STATE_LENGTH
from tests.conftest import (
    add_device_routes,
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
    from homeassistant.helpers.typing import StateType
    from pytest_homeassistant_custom_component.common import MockConfigEntry

#: Slug derived from the fixture serial ``4213377-30105-2242``.
SLUG = "4213377_30105_2242"
#: Fixed instant every setup test freezes to (2026-07-21T12:00:00Z == 14:00 CEST).
FROZEN_INSTANT = "2026-07-21T12:00:00+00:00"

_ONLY_SENSOR = patch("custom_components.aquahome.PLATFORMS", [Platform.SENSOR])


# ---------------------------------------------------------------------------
# Local helpers (never mutate fixture files — always deepcopy)
# ---------------------------------------------------------------------------


def _load_detail() -> dict[str, Any]:
    """Return an isolated deep copy of the device-detail fixture."""
    return copy.deepcopy(load_fixture("device-detail.json"))


def _treatment(detail: dict[str, Any]) -> dict[str, Any]:
    """Return the mutable enriched ``water_treatment`` block of ``detail``."""
    treatment: dict[str, Any] = detail["enriched_data"]["water_treatment"]
    return treatment


def _entity_id(hass: HomeAssistant, key: str) -> str | None:
    """Resolve the entity id for a sensor description key via its unique id."""
    registry = er.async_get(hass)
    return registry.async_get_entity_id("sensor", DOMAIN, f"{SLUG}_{key}")


def _require_entity_id(hass: HomeAssistant, key: str) -> str:
    """Resolve the entity id, asserting the sensor was registered."""
    entity_id = _entity_id(hass, key)
    assert entity_id is not None, f"sensor {key} was not registered"
    return entity_id


def _sensor(hass: HomeAssistant, key: str) -> SensorEntity:
    """Return the live sensor entity object for a description key."""
    component = hass.data[DATA_INSTANCES]["sensor"]
    entity = component.get_entity(_require_entity_id(hass, key))
    assert entity is not None, f"sensor {key} has no live entity"
    return cast(SensorEntity, entity)


def _native(hass: HomeAssistant, key: str) -> Any:
    """Return a sensor's native value (unit-system independent)."""
    return _sensor(hass, key).native_value


def _state_value(hass: HomeAssistant, key: str) -> StateType:
    """Return the state-machine string for a sensor key, or ``None`` if absent."""
    entity_id = _entity_id(hass, key)
    if entity_id is None:
        return None
    state = hass.states.get(entity_id)
    return state.state if state is not None else None


# ---------------------------------------------------------------------------
# Fast-coordinator sensor values on the dev fixture
# ---------------------------------------------------------------------------


async def test_tier2_fast_sensor_values(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The dev fixture yields the expected scaled/native Tier-2 sensor values."""
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(mock_api)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    # capacity_remaining_percent 1000 -> 100.0 (÷10 via SCALED_PROPERTIES).
    assert _native(hass, "capacity_remaining") == 100.0
    assert _native(hass, "hardness_setting") == 26
    # The lifetime weight totals are tenths of pounds (÷10): 149 regenerations
    # x the validated 3.8281 lb dose = 570.4 lb, exactly raw 5704 ÷ 10.
    assert _native(hass, "total_salt_used") == 570.4
    assert _native(hass, "total_rock_removed") == 175.4
    # recharge tile is "ready" and nothing is regenerating -> forced to zero.
    assert _native(hass, "regeneration_time_remaining") == 0
    assert _native(hass, "regeneration_status") == "none"
    # Weekday slots read the avg_daily_use_day_N_gals native gallon values.
    assert _native(hass, "average_daily_use_day_1") == 41
    assert _native(hass, "average_daily_use_day_7") == 10


async def test_weekday_reported_attribute(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Each weekday slot exposes its raw property's ``updated_at`` as ``reported``.

    Slots refresh only on their own weekday, so a slot can be a week (day_7, over
    a month) stale; the ``reported`` timestamp lets the user judge freshness.
    """
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(mock_api)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    day_1 = hass.states.get(_require_entity_id(hass, "average_daily_use_day_1"))
    assert day_1 is not None
    assert day_1.attributes["reported"] == "2026-07-19T00:01:01+00:00"

    # day_7 is the stalest slot in the fixture (last reported in June).
    day_7 = hass.states.get(_require_entity_id(hass, "average_daily_use_day_7"))
    assert day_7 is not None
    assert day_7.attributes["reported"] == "2026-06-14T02:53:37+00:00"


# ---------------------------------------------------------------------------
# regeneration_time_remaining — force-zero countdown rule
# ---------------------------------------------------------------------------


async def test_regeneration_time_remaining_force_zero(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A nonzero stale countdown is forced to zero while not regenerating.

    The cloud leaves the last countdown on the recharge tile after a cycle ends;
    trusting it would show a phantom remaining time, so any non-active state
    reports zero.
    """
    freezer.move_to(FROZEN_INSTANT)
    detail = _load_detail()
    treatment = _treatment(detail)
    treatment["recharge_ui"]["state"] = "ready"
    treatment["recharge_ui"]["time_remaining_seconds"] = 300
    add_device_routes(mock_api, device_detail=detail)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    assert _native(hass, "regeneration_time_remaining") == 0


async def test_regeneration_time_remaining_while_active(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """While regenerating the actual remaining seconds are reported verbatim.

    The device's own ``regen_time_rem_secs`` countdown is the primary source, so
    the scenario sets it alongside the enriched tile copy.
    """
    freezer.move_to(FROZEN_INSTANT)
    detail = _load_detail()
    treatment = _treatment(detail)
    treatment["recharge_ui"]["state"] = "regenerating"
    treatment["recharge_ui"]["time_remaining_seconds"] = 300
    detail["properties"]["regen_time_rem_secs"]["value"] = 300
    add_device_routes(mock_api, device_detail=detail)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    assert _native(hass, "regeneration_time_remaining") == 300


# ---------------------------------------------------------------------------
# regeneration_status — enum value, fallback source, out-of-options
# ---------------------------------------------------------------------------


async def test_regeneration_status_falls_back_to_top_level(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """With no ``regeneration`` block the enriched top-level status is used."""
    freezer.move_to(FROZEN_INSTANT)
    detail = _load_detail()
    treatment = _treatment(detail)
    del treatment["regeneration"]
    treatment["regeneration_status"] = "scheduled"
    add_device_routes(mock_api, device_detail=detail)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    assert _native(hass, "regeneration_status") == "scheduled"


async def test_regeneration_status_unknown_option_is_none(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A status the enum does not list collapses to ``None`` (unknown state)."""
    freezer.move_to(FROZEN_INSTANT)
    detail = _load_detail()
    _treatment(detail)["regeneration"]["regeneration_status"] = "made_up_value"
    add_device_routes(mock_api, device_detail=detail)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    assert _native(hass, "regeneration_status") is None
    assert _state_value(hass, "regeneration_status") == STATE_UNKNOWN


# ---------------------------------------------------------------------------
# next_regeneration — scheduled / rollover / unscheduled / existence
# ---------------------------------------------------------------------------


async def test_next_regeneration_scheduled_rolls_past_today(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A scheduled tile yields device-local midnight + offset, rolled if passed.

    Frozen at 14:00 CEST, the 02:00 candidate has already passed today, so the
    next occurrence rolls to tomorrow at device-local 02:00.
    """
    freezer.move_to(FROZEN_INSTANT)
    detail = _load_detail()
    _treatment(detail)["recharge_ui"]["state"] = "scheduled"
    add_device_routes(mock_api, device_detail=detail)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    warsaw = dt_util.get_time_zone("Europe/Warsaw")
    assert warsaw is not None
    now = dt_util.now(warsaw)
    candidate = datetime.combine(now.date(), time(), tzinfo=warsaw) + timedelta(
        seconds=7200
    )
    if candidate <= now:
        candidate += timedelta(days=1)

    value = _native(hass, "next_regeneration")
    assert isinstance(value, datetime)
    assert value == candidate
    assert value > now
    # 02:00 today was already in the past, so the result rolled to tomorrow.
    assert value.date() == now.date() + timedelta(days=1)
    assert (value.hour, value.minute) == (2, 0)


async def test_next_regeneration_scheduled_no_rollover(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """When the candidate is still ahead today it is not rolled to tomorrow."""
    # 2026-07-20T23:30Z == 2026-07-21 01:30 CEST, before the 02:00 candidate.
    freezer.move_to("2026-07-20T23:30:00+00:00")
    detail = _load_detail()
    _treatment(detail)["recharge_ui"]["state"] = "scheduled"
    add_device_routes(mock_api, device_detail=detail)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    warsaw = dt_util.get_time_zone("Europe/Warsaw")
    assert warsaw is not None
    now = dt_util.now(warsaw)
    expected = datetime.combine(now.date(), time(), tzinfo=warsaw) + timedelta(
        seconds=7200
    )

    value = _native(hass, "next_regeneration")
    assert isinstance(value, datetime)
    assert value == expected
    assert value.date() == now.date()
    assert (value.hour, value.minute) == (2, 0)


async def test_next_regeneration_unscheduled_is_none_but_entity_exists(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The dev tile (``ready``) creates the entity but reports no timestamp."""
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(mock_api)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    assert _entity_id(hass, "next_regeneration") is not None
    assert _native(hass, "next_regeneration") is None
    assert _state_value(hass, "next_regeneration") == STATE_UNKNOWN


async def test_next_regeneration_absent_without_schedule_offset(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """No ``regen_time_secs`` property means the entity is never created."""
    freezer.move_to(FROZEN_INSTANT)
    detail = _load_detail()
    del detail["properties"]["regen_time_secs"]
    add_device_routes(mock_api, device_detail=detail)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    assert _entity_id(hass, "next_regeneration") is None


# ---------------------------------------------------------------------------
# error_codes — absent on dev, joined, truncation, empty
# ---------------------------------------------------------------------------


async def test_error_codes_absent_on_dev_device(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The dev status block omits ``error_codes`` entirely, so no entity exists."""
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(mock_api)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    assert _entity_id(hass, "error_codes") is None


async def test_error_codes_joined_with_attributes(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A non-empty ``error_codes`` list is joined into the state with a codes list."""
    freezer.move_to(FROZEN_INSTANT)
    detail = _load_detail()
    _treatment(detail)["water_treatment_status"]["error_codes"] = ["E1", "E3"]
    add_device_routes(mock_api, device_detail=detail)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    assert _native(hass, "error_codes") == "E1, E3"
    state = hass.states.get(_require_entity_id(hass, "error_codes"))
    assert state is not None
    assert state.attributes["codes"] == ["E1", "E3"]


async def test_error_codes_truncated_to_max_state_length(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A very long joined string is truncated to Home Assistant's state limit."""
    freezer.move_to(FROZEN_INSTANT)
    codes = [f"ERRORCODE{index:03d}" for index in range(40)]
    detail = _load_detail()
    _treatment(detail)["water_treatment_status"]["error_codes"] = codes
    add_device_routes(mock_api, device_detail=detail)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    expected = ", ".join(codes)[:MAX_STATE_LENGTH]
    value = _native(hass, "error_codes")
    assert value == expected
    assert isinstance(value, str)
    assert len(value) == MAX_STATE_LENGTH


async def test_error_codes_empty_list_creates_but_unknown(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A present-but-empty list creates the entity but reports no active error."""
    freezer.move_to(FROZEN_INSTANT)
    detail = _load_detail()
    _treatment(detail)["water_treatment_status"]["error_codes"] = []
    add_device_routes(mock_api, device_detail=detail)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    assert _entity_id(hass, "error_codes") is not None
    assert _native(hass, "error_codes") is None
    assert _state_value(hass, "error_codes") == STATE_UNKNOWN


# ---------------------------------------------------------------------------
# Activity-coordinator sensors (last regeneration, latest alert)
# ---------------------------------------------------------------------------


async def test_activity_sensors_values_and_attributes(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The activity sensors read the newest regeneration event and alert."""
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(mock_api)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    assert _native(hass, "last_regeneration") == datetime(
        2026, 7, 17, 0, 1, 1, tzinfo=UTC
    )

    latest = hass.states.get(_require_entity_id(hass, "latest_alert"))
    assert latest is not None
    assert latest.state == "Device went offline"
    assert latest.attributes["title"] == "Disconnected"
    assert latest.attributes["level"] == "critical"
    assert latest.attributes["alert_type"] == "connection_status_offline"
    assert latest.attributes["alert_id"] == "db768cd7-b1f6-4c99-8910-5c6f2a999cbe"
    assert latest.attributes["timestamp"] == "2026-05-14T00:20:42+00:00"
    assert latest.attributes["is_read"] is False


async def test_last_regeneration_feature_gated(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """``last_regeneration`` requires the ``regeneration`` feature; alert is always on."""
    freezer.move_to(FROZEN_INSTANT)
    detail = _load_detail()
    _treatment(detail)["features"] = []
    add_device_routes(mock_api, device_detail=detail)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    assert _entity_id(hass, "last_regeneration") is None
    assert _entity_id(hass, "latest_alert") is not None


async def test_activity_sensors_none_when_feed_unavailable(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A failed activity feed leaves the entities set up but valueless.

    The fast telemetry poll succeeds (entry loads), while the alert feed returns
    500 with no cache, so the activity coordinator has ``None`` data: the sensors
    exist, read ``None`` natively, and show unavailable — never a stale value.
    """
    freezer.move_to(FROZEN_INSTANT)
    mock_api.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock_api.get(device_url(), payload=load_fixture("device-detail.json"), repeat=True)
    mock_api.get(alerts_url(), status=500, repeat=True)
    mock_api.get(
        regen_events_url(),
        payload=load_fixture("regeneration-events.json"),
        repeat=True,
    )
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    for key in ("last_regeneration", "latest_alert"):
        assert _native(hass, key) is None
        assert _state_value(hass, key) == STATE_UNAVAILABLE


async def test_latest_alert_message_truncated_to_max_state_length(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An overlong alert message is truncated so the state write never fails."""
    freezer.move_to(FROZEN_INSTANT)
    alerts = copy.deepcopy(load_fixture("alerts.json"))
    alerts["alerts"][0]["message"] = "A" * (MAX_STATE_LENGTH + 145)
    add_device_routes(mock_api, alerts=alerts)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    value = _native(hass, "latest_alert")
    assert value == "A" * MAX_STATE_LENGTH
    assert _state_value(hass, "latest_alert") == "A" * MAX_STATE_LENGTH


async def test_activity_sensors_stay_available_while_device_offline(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Cloud-side history stays available while the softener itself is offline.

    The activity entities deliberately skip the device-online availability gate:
    the alert history (a disconnect alert, most likely) is exactly what the user
    needs during an outage.
    """
    freezer.move_to(FROZEN_INSTANT)
    detail = _load_detail()
    detail["is_online"] = False
    add_device_routes(mock_api, device_detail=detail)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    assert _state_value(hass, "last_regeneration") not in (
        None,
        STATE_UNAVAILABLE,
        STATE_UNKNOWN,
    )
    assert _state_value(hass, "latest_alert") == "Device went offline"


async def test_weight_totals_scaled_and_displayed_in_kilograms(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The tenths-of-pounds totals surface as kilograms for a metric account.

    Native values are the ÷10-scaled pounds; the account's ``converted_units``
    (kilograms) drives ``suggested_unit_of_measurement``, so the state machine
    shows kg. Pins both the scaling and the suggested-unit derivation (the
    latter survived mutation testing when only native values were asserted).
    """
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(mock_api)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    salt = hass.states.get(_require_entity_id(hass, "total_salt_used"))
    assert salt is not None
    assert salt.attributes["unit_of_measurement"] == "kg"
    assert float(salt.state) == pytest.approx(258.73, abs=0.01)

    rock = hass.states.get(_require_entity_id(hass, "total_rock_removed"))
    assert rock is not None
    assert rock.attributes["unit_of_measurement"] == "kg"
    assert float(rock.state) == pytest.approx(79.56, abs=0.01)
