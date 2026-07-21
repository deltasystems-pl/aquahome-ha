"""Tests for the tolerant iQua payload models.

Every parser is exercised against a real captured fixture, plus targeted
minimal/unknown-key cases to prove tolerant parsing and the raw-property
scaling table.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.aquahome.api.models import (
    SCALED_PROPERTIES,
    SENTINEL_DISABLED,
    UNVERIFIED_SCALED_PROPERTIES,
    Alert,
    AlertsPage,
    CommandResult,
    ConvertedProperty,
    DatapointGraph,
    Device,
    DeviceSummary,
    LiveTicket,
    LoginResult,
    PropertyValue,
    RateLimitStatus,
    RegenerationEventsPage,
    SaltLevel,
    WaterTreatment,
    scaled_value,
)
from tests.conftest import load_fixture


def _property_map(fixture: str) -> dict[str, PropertyValue]:
    """Parse a raw ``properties`` map fixture into PropertyValue objects."""
    data = load_fixture(fixture)
    raw = data["properties"]
    return {name: PropertyValue.from_dict(item) for name, item in raw.items()}


# ---------------------------------------------------------------------------
# LoginResult (seed) — sanity only
# ---------------------------------------------------------------------------


def test_login_result_parses_and_defaults() -> None:
    """Parse a login body and default the optional role flags to False."""
    result = LoginResult.from_dict(
        {
            "access_token": "a.b.c",
            "refresh_token": "r.e.f",
            "user_id": "user-1",
            "is_verified": True,
        }
    )
    assert result.access_token == "a.b.c"
    assert result.user_id == "user-1"
    assert result.is_verified is True
    assert result.is_admin is False
    assert result.is_marketing is False


# ---------------------------------------------------------------------------
# Enriched water treatment
# ---------------------------------------------------------------------------


def test_water_treatment_parses_enriched_fixture() -> None:
    """Parse the enriched fixture and assert curated top-level values."""
    payload = load_fixture("enriched-data.json")["water_treatment"]
    treatment = WaterTreatment.from_dict(payload)

    assert treatment.treatment_system_type == "softener"
    assert treatment.salt_level_percent == 37.5
    assert treatment.rf_signal_strength_dbm == -37
    assert treatment.features == ("regeneration",)
    assert treatment.model == "AquaHome 20 Smart"
    assert treatment.total_recharges == 149
    assert treatment.days_powered_up == 2342
    assert treatment.wifi_ssid_name is None


def test_water_treatment_salt_level_block() -> None:
    """Parse the salt-level sub-object, including the duplicated percent key."""
    payload = load_fixture("enriched-data.json")["water_treatment"]
    treatment = WaterTreatment.from_dict(payload)

    assert treatment.salt_level is not None
    assert treatment.salt_level.monitoring_enabled is True
    assert treatment.salt_level.salt_level_percent == 37.5
    assert treatment.salt_level.salt_level_percent_rounded == 35


def test_water_treatment_regeneration_and_status() -> None:
    """Parse the regeneration and water-treatment-status sub-objects."""
    payload = load_fixture("enriched-data.json")["water_treatment"]
    treatment = WaterTreatment.from_dict(payload)

    assert treatment.regeneration is not None
    assert treatment.regeneration.regeneration_status == "none"
    assert treatment.regeneration.can_schedule is True
    assert treatment.regeneration.can_recharge is True

    status = treatment.water_treatment_status
    assert status is not None
    assert status.alert_badge_count == 0
    assert status.salt_level_alert is False
    assert status.service_reminder_message == "-1 months"
    assert status.water_to_drain_monitor_enabled is False


def test_water_treatment_flow_monitor_status() -> None:
    """Parse the flow-monitor sub-object into its count."""
    payload = load_fixture("enriched-data.json")["water_treatment"]
    treatment = WaterTreatment.from_dict(payload)

    assert treatment.flow_monitor_status is not None
    assert treatment.flow_monitor_status.count == 1


def test_water_treatment_recharge_ui_and_actions() -> None:
    """Parse the recharge UI block, its actions, and a nested dialog."""
    payload = load_fixture("enriched-data.json")["water_treatment"]
    recharge_ui = WaterTreatment.from_dict(payload).recharge_ui

    assert recharge_ui is not None
    assert recharge_ui.state == "ready"
    assert recharge_ui.title == "Ready"
    assert recharge_ui.time_remaining_seconds == 0
    assert recharge_ui.current_valve_state == "Service"
    assert recharge_ui.can_recharge is True
    assert len(recharge_ui.actions) == 4

    first = recharge_ui.actions[0]
    assert first.action == "recharge_now"
    assert first.label == "Recharge Now"
    assert first.requires_confirmation is True
    assert first.dialog is not None
    assert first.dialog.title == "Recharge Now"
    assert first.dialog.confirm_label == "Recharge Now"
    assert first.dialog.cancel_label == "Cancel"


# ---------------------------------------------------------------------------
# ConvertedProperty
# ---------------------------------------------------------------------------


def test_converted_property_base_value_and_value_in() -> None:
    """Read the stable native value and a specific unit case-insensitively."""
    payload = load_fixture("enriched-data.json")["water_treatment"]
    total = WaterTreatment.from_dict(payload).total_water_used

    assert total is not None
    assert total.base_value == 47420
    assert total.value_in("liters") == 179504
    assert total.value_in("LITERS") == 179504
    assert total.value_in("gallons") == 47420
    assert total.value_in("furlongs") is None


def test_converted_property_treated_water_available() -> None:
    """Parse the second ConvertedProperty on the enriched object."""
    payload = load_fixture("enriched-data.json")["water_treatment"]
    treated = WaterTreatment.from_dict(payload).treated_water_available

    assert treated is not None
    assert treated.base_value == 185
    assert treated.value_in("liters") == 700


def test_converted_property_empty_is_safe() -> None:
    """Return None from an empty ConvertedProperty rather than raising."""
    empty = ConvertedProperty.from_dict({})
    assert empty.conversions == ()
    assert empty.base_value is None
    assert empty.value_in("liters") is None


# ---------------------------------------------------------------------------
# DeviceSummary
# ---------------------------------------------------------------------------


def test_device_summary_parses_fixture() -> None:
    """Parse the summary fixture, including the nested user summary."""
    summary = DeviceSummary.from_dict(load_fixture("summary.json"))

    assert summary.id == "d32caa70-dca3-4cc9-bd3e-28b8c44df23c"
    assert summary.system_type == "demand softener"
    assert summary.nickname == "Dom"
    assert summary.serial_number == "7384243-20203-1120"
    assert summary.is_shared_with_dealer is False
    assert summary.is_rental is None
    assert summary.is_disabled is False

    assert summary.user is not None
    assert summary.user.first_name == "Dev"
    assert summary.user.email == "dev@example.com"


# ---------------------------------------------------------------------------
# Device
# ---------------------------------------------------------------------------


def test_device_parses_detail_fixture() -> None:
    """Parse the full device fixture: identity, enriched data, properties."""
    device = Device.from_dict(load_fixture("device-detail.json"))

    assert device.id == "d32caa70-dca3-4cc9-bd3e-28b8c44df23c"
    assert device.nickname == "Dom"
    assert device.serial_number == "7384243-20203-1120"
    assert device.is_online is True
    assert device.user is not None
    assert device.user.email == "dev@example.com"

    assert device.enriched_data is not None
    assert device.enriched_data.treatment_system_type == "softener"
    assert device.enriched_data.salt_level_percent == 37.5
    assert device.enriched_data.features == ("regeneration",)

    assert len(device.properties) == 123
    assert device.properties["salt_level_tenths"].value == 30


def test_device_online_prefers_internal_property() -> None:
    """Prefer the ``_internal_is_online`` property for availability."""
    device = Device.from_dict(load_fixture("device-detail.json"))
    assert device.online is True


def test_device_online_false_from_internal_property() -> None:
    """Report offline when the internal property is False."""
    device = Device(
        id="x",
        is_online=True,
        properties={
            "_internal_is_online": PropertyValue(
                name="_internal_is_online", value=False
            )
        },
    )
    assert device.online is False


def test_device_online_falls_back_to_is_online() -> None:
    """Fall back to the top-level flag when no internal property exists."""
    assert Device(id="x", is_online=True).online is True
    assert Device(id="x").online is None


def test_device_property_value_conversions() -> None:
    """Parse a property with converted value and unit conversions."""
    device = Device.from_dict(load_fixture("device-detail.json"))
    prop = device.properties["avg_daily_use_gals"]

    assert prop.name == "avg_daily_use_gals"
    assert prop.value == 47
    assert prop.converted_value == 178
    assert prop.converted_units == "Liters"
    assert prop.unit_conversions == (("Gallons", 47.0), ("Liters", 178.0))
    assert prop.updated_at is not None
    assert prop.updated_at.tzinfo is not None


def test_devices_list_parses_first_device() -> None:
    """Parse a paginated devices list and its first embedded device."""
    payload = load_fixture("devices-list.json")
    assert payload["page"] == 1
    assert payload["per_page"] == 200
    assert payload["total"] == 1

    device = Device.from_dict(payload["data"][0])
    assert device.id == "d32caa70-dca3-4cc9-bd3e-28b8c44df23c"
    assert device.enriched_data is not None
    assert device.enriched_data.features == ("regeneration",)


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


def test_alerts_page_parses_fixture() -> None:
    """Parse the alerts page and its first alert, including the timestamp."""
    page = AlertsPage.from_dict(load_fixture("alerts.json"))

    assert page.page == 1
    assert page.per_page == 20
    assert page.total == 59
    assert len(page.alerts) == 20

    first = page.alerts[0]
    assert first.id == "db768cd7-b1f6-4c99-8910-5c6f2a999cbe"
    assert first.type == "connection_status_offline"
    assert first.title == "Disconnected"
    assert first.level == "critical"
    assert first.is_read is False
    assert first.timestamp == datetime(2026, 5, 14, 0, 20, 42, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Regeneration events
# ---------------------------------------------------------------------------


def test_regeneration_events_page_parses_fixture() -> None:
    """Parse the regeneration events page and its timestamps."""
    page = RegenerationEventsPage.from_dict(load_fixture("regeneration-events.json"))

    assert page.page == 1
    assert page.total == 18
    assert len(page.events) == 18

    first = page.events[0]
    assert first.id == "e485b7e6-101f-44e3-994c-e8fc05e86d2c"
    assert first.start_time == datetime(2026, 7, 17, 0, 1, 1, tzinfo=UTC)
    assert first.end_time == datetime(2026, 7, 17, 2, 2, 43, tzinfo=UTC)
    assert first.device_start_time is not None
    assert first.device_start_time.utcoffset() == timedelta(hours=2)


def test_regeneration_event_tolerates_null_end_time() -> None:
    """Keep a still-open regeneration event whose end_time is null."""
    page = RegenerationEventsPage.from_dict(load_fixture("regeneration-events.json"))
    open_event = next(
        event
        for event in page.events
        if event.id == "6f06e49a-ee80-491d-9c42-3e343e5f7219"
    )
    assert open_event.start_time is not None
    assert open_event.end_time is None


# ---------------------------------------------------------------------------
# Datapoint graph
# ---------------------------------------------------------------------------


def test_datapoint_graph_parses_fixture() -> None:
    """Parse the daily-usage graph fixture and its aware period labels."""
    graph = DatapointGraph.from_dict(load_fixture("graph-daily-usage.json"))

    assert graph.units == "Liters"
    assert len(graph.data) == 8
    assert graph.data[0].value == 185
    assert graph.data[0].display_label == "14/07/2026"
    assert graph.data[5].value == 367
    assert graph.data[0].label is not None
    assert graph.data[0].label.utcoffset() == timedelta(hours=2)


# ---------------------------------------------------------------------------
# Command result and live ticket
# ---------------------------------------------------------------------------


def test_command_result_parses() -> None:
    """Parse a command result body."""
    result = CommandResult.from_dict({"status": "success", "message": "Queued"})
    assert result.status == "success"
    assert result.message == "Queued"


def test_live_ticket_parses() -> None:
    """Parse a live-ticket body into its websocket URI."""
    ticket = LiveTicket.from_dict({"websocket_uri": "/ws/?p=opaque-ticket"})
    assert ticket.websocket_uri == "/ws/?p=opaque-ticket"


# ---------------------------------------------------------------------------
# Raw-property scaling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "raw", "expected"),
    [
        ("salt_level_tenths", 30, 3.0),
        ("iron_level_tenths_ppm", 0, 0.0),
        ("chem_feed_tenths_secs", 1, 0.1),
        ("capacity_remaining_percent", 1000, 100.0),
        ("average_exhaustion_percent", 890, 89.0),
        ("avg_days_between_regens", 735, 7.35),
        ("avg_salt_per_regen_lbs", 38281, 3.8281),
        # Tenths of pounds despite the plain _lbs suffix: 149 regens x the
        # validated 3.8281 lb dose = 570.4 lb exactly. The server's own
        # converted_value skips the ÷10 for these two, so it must not be used.
        ("total_salt_use_lbs", 5704, 570.4),
        ("total_rock_removed_lbs", 1754, 175.4),
    ],
)
def test_scaled_value_applies_verified_divisor(
    name: str, raw: int, expected: float
) -> None:
    """Divide each verified scaled property by its documented factor."""
    prop = PropertyValue(name=name, value=raw)
    assert scaled_value(prop) == pytest.approx(expected)


def test_scaled_value_from_real_property_map() -> None:
    """Scale the live-snapshot values straight out of the parsed fixture."""
    props = _property_map("properties.json")

    assert scaled_value(props["salt_level_tenths"]) == pytest.approx(3.0)
    assert scaled_value(props["capacity_remaining_percent"]) == pytest.approx(100.0)
    assert scaled_value(props["average_exhaustion_percent"]) == pytest.approx(89.0)
    assert scaled_value(props["avg_days_between_regens"]) == pytest.approx(7.35)
    assert scaled_value(props["avg_salt_per_regen_lbs"]) == pytest.approx(3.8281)
    assert scaled_value(props["total_salt_use_lbs"]) == pytest.approx(570.4)
    assert scaled_value(props["total_rock_removed_lbs"]) == pytest.approx(175.4)


def test_scaled_value_service_reminder_sentinel_is_none() -> None:
    """Map the disabled service-reminder sentinel (-1) to None."""
    props = _property_map("properties.json")
    assert props["service_reminder_months"].value == SENTINEL_DISABLED
    assert scaled_value(props["service_reminder_months"]) is None


def test_scaled_value_unscaled_property_passthrough() -> None:
    """Pass an unscaled numeric property through unchanged."""
    assert scaled_value(PropertyValue(name="total_regens", value=149)) == 149.0


def test_scaled_value_does_not_apply_unverified_flow_divisor() -> None:
    """Never apply the unverified current_water_flow_gpm divisor."""
    assert "current_water_flow_gpm" in UNVERIFIED_SCALED_PROPERTIES
    assert "current_water_flow_gpm" not in SCALED_PROPERTIES

    prop = PropertyValue(name="current_water_flow_gpm", value=57)
    assert scaled_value(prop) == 57.0


def test_scaled_value_non_numeric_is_none() -> None:
    """Return None for boolean and string property values."""
    assert scaled_value(PropertyValue(name="service_active", value=True)) is None
    assert scaled_value(PropertyValue(name="tz_id", value="Europe/Warsaw")) is None
    assert scaled_value(PropertyValue(name="missing", value=None)) is None


# ---------------------------------------------------------------------------
# Rate-limit policy refill parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        # Both disagreeing fork docstring examples: w / limit.
        ("5;w=60;burst=50;policy=token_bucket", 12.0),
        ("6;w=600;burst=60", 100.0),
        # Field order is not assumed — w= is matched by label.
        ("5;burst=50;w=60", 12.0),
        # Garbage / incomplete strings collapse to None, never raise.
        (None, None),
        ("", None),
        ("garbage", None),
        ("5", None),  # no window field
        ("5;w=", None),  # empty window
        ("5;w=abc", None),  # non-numeric window
        ("abc;w=60", None),  # non-integer leading limit
        ("0;w=60", None),  # zero limit -> no division
        ("-5;w=60", None),  # negative limit
        ("5;w=0", None),  # zero window
    ],
)
def test_rate_limit_refill_seconds(policy: str | None, expected: float | None) -> None:
    """Derive the token-bucket refill interval defensively from the policy."""
    status = RateLimitStatus(limit=5, remaining=0, policy=policy)
    result = status.refill_seconds
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Tolerant parsing
# ---------------------------------------------------------------------------


def test_minimal_dicts_parse() -> None:
    """Parse minimal payloads that carry only the required keys."""
    treatment = WaterTreatment.from_dict({"treatment_system_type": "filter"})
    assert treatment.treatment_system_type == "filter"
    assert treatment.salt_level is None
    assert treatment.features == ()

    salt = SaltLevel.from_dict({"monitoring_enabled": True})
    assert salt.monitoring_enabled is True
    assert salt.salt_level_percent is None

    device = Device.from_dict({"id": "abc"})
    assert device.id == "abc"
    assert device.properties == {}
    assert device.enriched_data is None
    assert device.online is None

    assert CommandResult.from_dict({}).status is None
    assert Alert.from_dict({"id": "a1"}).id == "a1"


def test_unknown_keys_are_ignored() -> None:
    """Ignore keys the model does not declare instead of raising."""
    device = Device.from_dict(
        {
            "id": "abc",
            "brand_new_server_field": {"nested": [1, 2, 3]},
            "enriched_data": {
                "water_treatment": {
                    "treatment_system_type": "softener",
                    "another_unknown": True,
                }
            },
            "properties": {
                "salt_level_tenths": {
                    "name": "salt_level_tenths",
                    "value": 30,
                    "surprise": "ignored",
                }
            },
        }
    )
    assert device.id == "abc"
    assert device.enriched_data is not None
    assert device.enriched_data.treatment_system_type == "softener"
    assert device.properties["salt_level_tenths"].value == 30
