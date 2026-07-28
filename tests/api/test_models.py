"""Tests for the tolerant iQua payload models.

Every parser is exercised against a real captured fixture, plus targeted
minimal/unknown-key cases to prove tolerant parsing and the raw-property
scaling table.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.aquahome.api.models import (
    SENTINEL_DISABLED,
    Alert,
    AlertsPage,
    CommandResult,
    ConvertedProperty,
    DatapointGraph,
    Device,
    DeviceSettingsDocument,
    DeviceSummary,
    LiveTicket,
    LoginResult,
    PropertyValue,
    RateLimitStatus,
    RegenerationEventsPage,
    SaltLevel,
    SelectRules,
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

    assert summary.id == "e5a7c1f3-8b2d-4e6a-b9c8-3d5f7a9b1c2e"
    assert summary.system_type == "demand softener"
    assert summary.nickname == "Demo"
    assert summary.serial_number == "4213377-30105-2242"
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

    assert device.id == "e5a7c1f3-8b2d-4e6a-b9c8-3d5f7a9b1c2e"
    assert device.nickname == "Demo"
    assert device.serial_number == "4213377-30105-2242"
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
    assert device.id == "e5a7c1f3-8b2d-4e6a-b9c8-3d5f7a9b1c2e"
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


def test_scaled_value_applies_verified_flow_divisor() -> None:
    """Decode current_water_flow_gpm as tenths of gallons per minute.

    Verified against a live measured-flow session: the stream published 9
    while the lifetime counter stepped one gallon every ~70 s (0.86 gpm), and
    the model's spec peak 572 reads a sane 57.2 gpm.
    """
    assert scaled_value(PropertyValue(name="current_water_flow_gpm", value=9)) == 0.9
    assert scaled_value(PropertyValue(name="current_water_flow_gpm", value=572)) == 57.2


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


# ---------------------------------------------------------------------------
# Device settings document — real fixture parse
# ---------------------------------------------------------------------------


def _settings_doc(*items: Mapping[str, object]) -> DeviceSettingsDocument:
    """Build a settings document from raw setting dicts."""
    return DeviceSettingsDocument.from_dict({"settings": list(items)})


def test_settings_document_parses_real_fixture() -> None:
    """Parse the real 55 KB settings fixture: 18 items, 17 selects, 434 zones."""
    doc = DeviceSettingsDocument.from_dict(load_fixture("settings.json"))

    assert len(doc.settings) == 18
    selects = [s for s in doc.settings if s.component_type == "select"]
    assert len(selects) == 17

    timezone = doc.get("timezone")
    assert timezone is not None
    assert timezone.rules is not None
    assert timezone.rules.select_rules is not None
    assert len(timezone.rules.select_rules.options) == 434

    inlet = doc.get("inlet_hardness")
    assert inlet is not None
    assert inlet.current_value == "25.7"
    assert inlet.rules is not None
    assert inlet.rules.select_rules is not None
    first_option = inlet.rules.select_rules.options[0]
    assert first_option.value == "1.2"
    assert first_option.label == "20 PPM (1 dH/2 fH)"
    assert first_option.disabled is None


def test_settings_document_text_and_select_rules_are_typed() -> None:
    """The text setting exposes text_rules; a select exposes select_rules only."""
    doc = DeviceSettingsDocument.from_dict(load_fixture("settings.json"))

    nickname = doc.get("nickname")
    assert nickname is not None
    assert nickname.component_type == "text"
    assert nickname.rules is not None
    assert nickname.rules.text_rules is not None
    assert nickname.rules.text_rules.min_length == 1
    assert nickname.rules.text_rules.max_length == 50
    assert nickname.rules.select_rules is None

    volume = doc.get("volume_units")
    assert volume is not None
    assert volume.rules is not None
    assert volume.rules.select_rules is not None
    assert volume.rules.text_rules is None


def test_settings_document_get_missing_is_none() -> None:
    """Look up an absent setting name and get None back."""
    doc = DeviceSettingsDocument.from_dict(load_fixture("settings.json"))
    assert doc.get("does_not_exist") is None


# ---------------------------------------------------------------------------
# Conditional visibility
# ---------------------------------------------------------------------------


def test_conditional_hidden_against_fixture_and_visible_when_driver_matches() -> None:
    """chem_feed_volume is hidden while aux_control_type is 0, visible at "4"."""
    fixture = load_fixture("settings.json")
    doc = DeviceSettingsDocument.from_dict(fixture)

    chem = doc.get("chem_feed_volume")
    assert chem is not None
    assert chem.conditional is not None
    assert chem.conditional.and_rules == ()
    (rule,) = chem.conditional.or_rules
    assert rule.field == "aux_control_type"
    assert rule.comparison == "eq"
    assert rule.value == "4"

    # aux_control_type is "0" in the captured fixture -> the or-clause is False.
    assert doc.setting_visible(chem) is False
    # A driver setting with no conditional is always visible.
    aux = doc.get("aux_control_type")
    assert aux is not None
    assert doc.setting_visible(aux) is True

    # Flip the driver to "4" in a modified document -> the setting becomes visible.
    modified = copy.deepcopy(fixture)
    for setting in modified["settings"]:
        if setting["name"] == "aux_control_type":
            setting["current_value"] = "4"
    doc_visible = DeviceSettingsDocument.from_dict(modified)
    chem_visible = doc_visible.get("chem_feed_volume")
    assert chem_visible is not None
    assert doc_visible.setting_visible(chem_visible) is True


def test_setting_visible_no_conditional() -> None:
    """A setting without a conditional group is always visible."""
    doc = _settings_doc(
        {"component_type": "select", "name": "x", "label": "X", "current_value": "1"}
    )
    setting = doc.get("x")
    assert setting is not None
    assert doc.setting_visible(setting) is True


def test_setting_visible_eq_true_and_false() -> None:
    """The eq operator toggles visibility with the referenced value."""
    dep = {
        "component_type": "select",
        "name": "dep",
        "label": "Dep",
        "current_value": "1",
        "conditional": {"or": [{"field": "driver", "comparison": "eq", "value": "4"}]},
    }
    visible = _settings_doc(
        {
            "component_type": "select",
            "name": "driver",
            "label": "D",
            "current_value": "4",
        },
        dep,
    )
    dep_visible = visible.get("dep")
    assert dep_visible is not None
    assert visible.setting_visible(dep_visible) is True

    hidden = _settings_doc(
        {
            "component_type": "select",
            "name": "driver",
            "label": "D",
            "current_value": "0",
        },
        dep,
    )
    dep_hidden = hidden.get("dep")
    assert dep_hidden is not None
    assert hidden.setting_visible(dep_hidden) is False


def test_setting_visible_eq_is_string_normalized() -> None:
    """An integer current value equals a string rule value after str()."""
    doc = _settings_doc(
        {
            "component_type": "number",
            "name": "driver",
            "label": "D",
            "current_value": 4,
        },
        {
            "component_type": "select",
            "name": "dep",
            "label": "Dep",
            "current_value": "1",
            "conditional": {
                "or": [{"field": "driver", "comparison": "eq", "value": "4"}]
            },
        },
    )
    dep = doc.get("dep")
    assert dep is not None
    assert doc.setting_visible(dep) is True


def test_setting_visible_missing_referenced_field_is_hidden() -> None:
    """A clause referencing a non-existent setting evaluates False."""
    doc = _settings_doc(
        {
            "component_type": "select",
            "name": "dep",
            "label": "Dep",
            "current_value": "1",
            "conditional": {
                "or": [{"field": "ghost", "comparison": "eq", "value": "4"}]
            },
        }
    )
    dep = doc.get("dep")
    assert dep is not None
    assert doc.setting_visible(dep) is False


def test_setting_visible_unknown_comparison_fails_open() -> None:
    """An unknown comparison operator keeps the setting visible (fail open)."""
    doc = _settings_doc(
        {
            "component_type": "select",
            "name": "driver",
            "label": "D",
            "current_value": "1",
        },
        {
            "component_type": "select",
            "name": "dep",
            "label": "Dep",
            "current_value": "1",
            "conditional": {
                "and": [{"field": "driver", "comparison": "gte", "value": "4"}]
            },
        },
    )
    dep = doc.get("dep")
    assert dep is not None
    assert doc.setting_visible(dep) is True


def test_setting_visible_and_or_groups_gate_together() -> None:
    """Both groups must hold: all ``and`` clauses and any ``or`` clause."""
    conditional = {
        "and": [{"field": "a", "comparison": "eq", "value": "1"}],
        "or": [
            {"field": "b", "comparison": "eq", "value": "9"},
            {"field": "b", "comparison": "eq", "value": "2"},
        ],
    }
    doc = _settings_doc(
        {"component_type": "select", "name": "a", "label": "A", "current_value": "1"},
        {"component_type": "select", "name": "b", "label": "B", "current_value": "2"},
        {
            "component_type": "select",
            "name": "dep",
            "label": "Dep",
            "current_value": "0",
            "conditional": conditional,
        },
    )
    dep = doc.get("dep")
    assert dep is not None
    # and: a==1 True; or: b==9 False but b==2 True -> both groups hold.
    assert doc.setting_visible(dep) is True

    # Break the and-group: now a must equal "5", which it does not.
    broken = copy.deepcopy(conditional)
    broken["and"][0]["value"] = "5"
    doc_hidden = _settings_doc(
        {"component_type": "select", "name": "a", "label": "A", "current_value": "1"},
        {"component_type": "select", "name": "b", "label": "B", "current_value": "2"},
        {
            "component_type": "select",
            "name": "dep",
            "label": "Dep",
            "current_value": "0",
            "conditional": broken,
        },
    )
    dep_hidden = doc_hidden.get("dep")
    assert dep_hidden is not None
    assert doc_hidden.setting_visible(dep_hidden) is False


# ---------------------------------------------------------------------------
# Water-shutoff valve — synthetic payload
# ---------------------------------------------------------------------------


def test_water_shutoff_valve_parses_synthetic_payload() -> None:
    """Parse a synthetic WSOV block, including the nested dialog buttons."""
    payload = {
        "treatment_system_type": "softener",
        "water_shutoff_valve": {
            "status": "close",
            "is_installed": True,
            "auto_shutoff_supported": True,
            "auto_shutoff_features": ["leak_detected", "low_temperature"],
            "error_code": "open_switch_error",
            "manual_override": False,
            "dialog": {
                "button_disabled": False,
                "button_label": "Open valve",
                "dialog_explanation": "Water will flow again.",
                "dialog_title": "Open the valve?",
                "state_message": "Valve is closed",
                "is_error": False,
                "dialog_buttons": {
                    "acknowledge": True,
                    "cancel": True,
                    "close": False,
                    "open": True,
                },
            },
        },
    }
    valve = WaterTreatment.from_dict(payload).water_shutoff_valve

    assert valve is not None
    assert valve.status == "close"
    assert valve.is_installed is True
    assert valve.auto_shutoff_supported is True
    assert valve.auto_shutoff_features == ("leak_detected", "low_temperature")
    assert valve.error_code == "open_switch_error"
    assert valve.manual_override is False

    dialog = valve.dialog
    assert dialog is not None
    assert dialog.button_disabled is False
    assert dialog.button_label == "Open valve"
    assert dialog.dialog_title == "Open the valve?"
    assert dialog.state_message == "Valve is closed"
    assert dialog.is_error is False

    buttons = dialog.dialog_buttons
    assert buttons is not None
    assert buttons.acknowledge is True
    assert buttons.cancel is True
    assert buttons.close is False
    assert buttons.open is True


def test_water_treatment_without_wsov_or_leak_blocks_is_none() -> None:
    """Both new optional blocks are None when absent from the payload."""
    treatment = WaterTreatment.from_dict({"treatment_system_type": "softener"})
    assert treatment.water_shutoff_valve is None
    assert treatment.leak_detectors is None


# ---------------------------------------------------------------------------
# Leak detectors — synthetic payload with flattening
# ---------------------------------------------------------------------------


def test_leak_detectors_parse_synthetic_payload_with_flattening() -> None:
    """Parse leak detectors: flatten scanning + signal_strength, skip id-less."""
    payload = {
        "treatment_system_type": "softener",
        "leak_detectors": {
            "details": [
                {
                    "detector_id": 7,
                    "nickname": "Basement",
                    "nickname_setting_key": "leak_7_nickname",
                    "last_updated_at": "2026-07-20T10:00:00Z",
                    "status": {
                        "in_alert_state": True,
                        "is_connected": {
                            "value": True,
                            "updated_at": "2026-07-20T09:00:00Z",
                        },
                        "leak_detected": {
                            "value": True,
                            "updated_at": "2026-07-20T09:30:00Z",
                        },
                        "low_battery": {"value": False},
                        "tampered": {
                            "value": False,
                            "updated_at": "2026-07-20T08:00:00Z",
                        },
                        "signal_strength": {"value": -55},
                        "temperature": {
                            "raw_value": 71,
                            "converted_value": 22,
                            "display": {"value": "22 C", "translated": True},
                            "status": {"value": True, "updated_at": "bogus"},
                        },
                    },
                },
                {"nickname": "no id — dropped"},
            ],
            "scanning": {"is_scanning": True},
        },
    }
    detectors = WaterTreatment.from_dict(payload).leak_detectors

    assert detectors is not None
    # Flattened out of ``scanning.is_scanning``.
    assert detectors.is_scanning is True
    # The id-less entry is dropped, leaving a single addressable detector.
    assert len(detectors.details) == 1

    detector = detectors.details[0]
    assert detector.detector_id == 7
    assert detector.nickname == "Basement"
    assert detector.nickname_setting_key == "leak_7_nickname"
    assert detector.last_updated_at == datetime(2026, 7, 20, 10, 0, tzinfo=UTC)

    status = detector.status
    assert status is not None
    assert status.in_alert_state is True
    assert status.leak_detected is not None
    assert status.leak_detected.value is True
    assert status.leak_detected.updated_at == datetime(2026, 7, 20, 9, 30, tzinfo=UTC)
    assert status.low_battery is not None
    assert status.low_battery.value is False
    assert status.low_battery.updated_at is None
    # Flattened out of ``signal_strength.value``.
    assert status.signal_strength == -55

    temperature = status.temperature
    assert temperature is not None
    assert temperature.raw_value == 71
    assert temperature.converted_value == 22


# ---------------------------------------------------------------------------
# Tolerance of garbage in the new models
# ---------------------------------------------------------------------------


def test_settings_document_tolerates_garbage() -> None:
    """Wrong-typed settings payloads never raise and collapse to safe defaults."""
    assert DeviceSettingsDocument.from_dict({}).settings == ()
    assert DeviceSettingsDocument.from_dict({"settings": "nope"}).settings == ()

    doc = DeviceSettingsDocument.from_dict(
        {
            "settings": [
                {
                    "component_type": 5,
                    "name": None,
                    "label": [],
                    "current_value": {"nested": 1},
                    "rules": "not a dict",
                    "conditional": 12,
                },
                "not a dict",
            ]
        }
    )
    assert len(doc.settings) == 1
    setting = doc.settings[0]
    assert setting.component_type == ""
    assert setting.name == ""
    assert setting.label == ""
    assert setting.current_value is None
    assert setting.rules is None
    assert setting.conditional is None
    # A setting with no conditional is visible even after garbage parsing.
    assert doc.setting_visible(setting) is True


def test_select_rules_drop_options_missing_value_or_label() -> None:
    """Options without both a value and a label are dropped, never raised on."""
    rules = SelectRules.from_dict(
        {
            "options": [
                {"value": "1", "label": "One"},
                {"value": "2"},  # missing label
                {"label": "no value"},  # missing value
                "not a dict",
                {"value": 3, "label": "wrong-typed value"},  # non-string value
            ]
        }
    )
    assert len(rules.options) == 1
    assert rules.options[0].value == "1"
    assert rules.options[0].label == "One"


def test_wsov_and_leak_blocks_tolerate_garbage() -> None:
    """Wrong-typed WSOV/leak blocks parse to safe empty structures."""
    treatment = WaterTreatment.from_dict(
        {
            "treatment_system_type": "softener",
            "water_shutoff_valve": {
                "status": 5,
                "is_installed": "yes",
                "auto_shutoff_features": "not a list",
                "dialog": 7,
            },
            "leak_detectors": {"details": "not a list", "scanning": "not a dict"},
        }
    )

    valve = treatment.water_shutoff_valve
    assert valve is not None
    assert valve.status is None
    assert valve.is_installed is None
    assert valve.auto_shutoff_features == ()
    assert valve.dialog is None

    detectors = treatment.leak_detectors
    assert detectors is not None
    assert detectors.details == ()
    assert detectors.is_scanning is None
