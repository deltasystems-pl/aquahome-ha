"""Tests for the salt-intelligence sensors (Phase 6).

End-to-end platform tests for the five salt sensors: the chemistry estimate
(``daily_salt_usage``), its days-until-empty cross-check (``salt_days_remaining``
plus the disabled-by-default ``salt_depletion_estimate`` timestamp), and the two
device-counter sensors (``salt_per_regeneration``, ``salt_efficiency``). A real
config entry is set up against the captured iQua fixtures served through
``aioresponses`` and the entities are inspected through the state machine, the
entity registry, and — where a unit-system-independent number matters — the live
entity object.

The ground-truth numbers are the frozen Phase-6 contract table, recomputed here
from the very same fixture inputs (``inlet_hardness`` 25.7 gpg /
``hardness_grains`` 26 gpg, 47 gal/d, 2152 gr/lb, 3.8281 lb per regen over 7.35
days, 167 days of device countdown). They are asserted at the contract's
tolerances, never looser.

Two behaviours get dedicated attention because they only exist at the platform
layer: the inlet-hardness source ladder (settings document first, raw
``hardness_grains`` property as the fallback — including when the settings
endpoint never loads at all), and the settings-coordinator listener that
re-renders the sensors the moment the document changes, without waiting for a
fast poll.

Time is frozen for every setup test: the depletion-estimate timestamp is derived
from ``now()`` in the device's own timezone.
"""

from __future__ import annotations

import copy
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory, Platform, UnitOfMass
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.util import dt as dt_util

from custom_components.aquahome.api.models import DeviceSettingsDocument
from custom_components.aquahome.const import DOMAIN
from tests.conftest import (
    TEST_DEVICE_ID,
    add_device_routes,
    load_fixture,
    settings_url,
    setup_integration,
    with_setting_value,
    without_setting,
)

if TYPE_CHECKING:
    from aioresponses import aioresponses
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.typing import StateType
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from syrupy.assertion import SnapshotAssertion

#: Slug derived from the fixture serial ``7384243-20203-1120``.
SLUG = "7384243_20203_1120"
#: Fixed instant every setup test freezes to (2026-07-21T12:00:00Z == 14:00 CEST).
FROZEN_INSTANT = "2026-07-21T12:00:00+00:00"

#: The five salt sensors, in contract order.
SALT_KEYS: tuple[str, ...] = (
    "daily_salt_usage",
    "salt_days_remaining",
    "salt_depletion_estimate",
    "salt_per_regeneration",
    "salt_efficiency",
)

# --- Ground truth (Phase 6 contract table, from the real fixtures) ----------
#: Daily salt with the settings document's 25.7 gpg inlet hardness (g/d).
DAILY_SALT_SETTINGS = 254.60
#: Daily salt with the raw ``hardness_grains`` fallback of 26 gpg (g/d).
DAILY_SALT_FALLBACK = 257.57
#: Inlet hardness in °dH from the settings document (25.7 gpg).
INLET_DH_SETTINGS = 24.6489
#: Inlet hardness in °dH from the raw property fallback (26 gpg).
INLET_DH_FALLBACK = 24.9367
#: Device-observed salt rate: 3.8281 lb per regen over 7.35 days (g/d).
DEVICE_DAILY_SALT = 236.24
#: Cross-check days: 167 device days re-timed at the chemistry rate.
CROSS_CHECK_DAYS = 154.95
#: Whole days the depletion timestamp advances (``int()`` of the cross-check).
CROSS_CHECK_DAYS_INT = 154
#: Device countdown carried by ``out_of_salt_estimate_days`` in the fixture.
DEVICE_ESTIMATE_DAYS = 167
#: Salt efficiency from the rated 2152 gr/lb property (mol/kg).
EFFICIENCY_RATED = 3.07162
#: Salt efficiency from the 175.4 / 570.4 lb lifetime totals (mol/kg).
EFFICIENCY_TOTALS = 3.07237
#: Scaled ``avg_salt_per_regen_lbs`` (38281 / 10000) in native pounds.
SALT_PER_REGEN_LB = 3.8281
#: The same dose in kilograms — the metric account's display unit.
SALT_PER_REGEN_KG = 1.7364

# --- Ground truth for the re-rendered 30 gpg settings document --------------
#: Daily salt after the inlet hardness is raised to 30 gpg (g/d).
DAILY_SALT_30_GPG = 297.20
#: Inlet hardness in °dH at 30 gpg, rounded as the attribute rounds it.
INLET_DH_30_GPG = 28.77

_ONLY_SENSOR = patch("custom_components.aquahome.PLATFORMS", [Platform.SENSOR])


# ---------------------------------------------------------------------------
# Local helpers (never mutate fixture files — always deepcopy)
# ---------------------------------------------------------------------------


def _load_detail() -> dict[str, Any]:
    """Return an isolated deep copy of the device-detail fixture."""
    return copy.deepcopy(load_fixture("device-detail.json"))


def _without_property(name: str) -> dict[str, Any]:
    """Return the device-detail fixture with one raw property removed."""
    detail = _load_detail()
    del detail["properties"][name]
    return detail


def _lookup(hass: HomeAssistant, key: str) -> str | None:
    """Resolve a sensor key's entity id via its unique id, or ``None``."""
    registry = er.async_get(hass)
    return registry.async_get_entity_id("sensor", DOMAIN, f"{SLUG}_{key}")


def _entity_id(hass: HomeAssistant, key: str) -> str:
    """Resolve a sensor key's entity id, asserting the entity was created."""
    entity_id = _lookup(hass, key)
    assert entity_id is not None, f"sensor {key} was not registered"
    return entity_id


def _sensor(hass: HomeAssistant, key: str) -> SensorEntity:
    """Return the live sensor entity object for a description key."""
    component = hass.data[DATA_INSTANCES]["sensor"]
    entity = component.get_entity(_entity_id(hass, key))
    assert entity is not None, f"sensor {key} has no live entity"
    return cast(SensorEntity, entity)


def _native(hass: HomeAssistant, key: str) -> StateType | date | datetime | Decimal:
    """Return a sensor's native value (unit-system independent)."""
    return _sensor(hass, key).native_value


def _state_value(hass: HomeAssistant, key: str) -> str:
    """Return a sensor's state string, asserting the state exists."""
    state = hass.states.get(_entity_id(hass, key))
    assert state is not None, f"sensor {key} has no state"
    return state.state


def _attributes(hass: HomeAssistant, key: str) -> dict[str, Any]:
    """Return a sensor's state attributes as a plain dict."""
    state = hass.states.get(_entity_id(hass, key))
    assert state is not None, f"sensor {key} has no state"
    return dict(state.attributes)


def _fail_settings_route(mock: aioresponses) -> None:
    """Register a permanently failing settings route ahead of the fixtures.

    ``aioresponses`` matches registrations in order, so this repeating 500 wins
    over the healthy route :func:`add_device_routes` registers afterwards.
    """
    mock.get(
        settings_url(),
        status=500,
        payload={"code": "ServerError", "detail": "boom"},
        repeat=True,
    )


def _device_detail_get_count(mock: aioresponses) -> int:
    """Return how many ``GET /devices/{id}`` detail polls were recorded."""
    return sum(
        len(calls)
        for (method, url), calls in mock.requests.items()
        if method == "GET" and url.path.endswith(TEST_DEVICE_ID)
    )


# ---------------------------------------------------------------------------
# Snapshot of the five salt entities
# ---------------------------------------------------------------------------


async def test_salt_sensors_snapshot(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    snapshot: SnapshotAssertion,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Registry entries and states of the five salt sensors match the snapshot.

    ``salt_depletion_estimate`` is registry-disabled by default, so only its
    registry entry is snapshotted — it has no state until a user enables it
    (:func:`test_salt_depletion_estimate_is_device_local_midnight` covers the
    enabled value).
    """
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(mock_api)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    registry = er.async_get(hass)
    for key in SALT_KEYS:
        entity_id = _entity_id(hass, key)
        assert registry.async_get(entity_id) == snapshot(name=f"{key}-entry")
        if key == "salt_depletion_estimate":
            assert hass.states.get(entity_id) is None
            continue
        assert hass.states.get(entity_id) == snapshot(name=f"{key}-state")


# ---------------------------------------------------------------------------
# Inlet-hardness source ladder
# ---------------------------------------------------------------------------


async def test_daily_salt_usage_uses_settings_hardness(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The settings document's 25.7 gpg inlet hardness yields 254.6 g/d.

    The precise settings value wins over the integer ``hardness_grains``
    property, and every input of the estimate is published as an attribute so
    the number is auditable in the UI.
    """
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(mock_api)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    assert _native(hass, "daily_salt_usage") == pytest.approx(
        DAILY_SALT_SETTINGS, abs=0.1
    )
    assert float(_state_value(hass, "daily_salt_usage")) == pytest.approx(
        DAILY_SALT_SETTINGS, abs=0.1
    )

    attributes = _attributes(hass, "daily_salt_usage")
    assert attributes["inlet_hardness_source"] == "device_setting"
    assert attributes["inlet_hardness_dh"] == round(INLET_DH_SETTINGS, 2)
    assert attributes["outlet_hardness_dh"] == 0.0
    assert attributes["salt_efficiency_mol_per_kg"] == round(EFFICIENCY_RATED, 3)
    assert attributes["salt_type"] == "NaCl"
    assert attributes["unit_of_measurement"] == "g/d"


async def test_daily_salt_usage_falls_back_to_hardness_property(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Without the setting, the 26 gpg raw property drives 257.6 g/d."""
    freezer.move_to(FROZEN_INSTANT)
    settings = without_setting(load_fixture("settings.json"), "inlet_hardness")
    add_device_routes(mock_api, settings=settings)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    assert _native(hass, "daily_salt_usage") == pytest.approx(
        DAILY_SALT_FALLBACK, abs=0.1
    )
    attributes = _attributes(hass, "daily_salt_usage")
    assert attributes["inlet_hardness_source"] == "device_property"
    assert attributes["inlet_hardness_dh"] == round(INLET_DH_FALLBACK, 2)


async def test_non_finite_salt_type_property_falls_back_to_the_setting(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A non-finite ``salt_type_enum`` never crashes the attribute render.

    ``int(inf)`` raises OverflowError, so without the finiteness guard a
    corrupted raw property would blow up ``extra_state_attributes`` on every
    poll. Instead the lookup falls through to the settings document's
    ``salt_type`` and the sensor keeps its value untouched.
    """
    freezer.move_to(FROZEN_INSTANT)
    detail = load_fixture("device-detail.json")
    detail["properties"]["salt_type_enum"]["value"] = float("inf")
    add_device_routes(mock_api, device_detail=detail)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    assert _native(hass, "daily_salt_usage") == pytest.approx(
        DAILY_SALT_SETTINGS, abs=0.1
    )
    assert _attributes(hass, "daily_salt_usage")["salt_type"] == "NaCl"


@pytest.mark.parametrize(
    "malformed",
    ["Infinity", "-Infinity", "NaN", "not-a-number", "0"],
    ids=["inf", "neg-inf", "nan", "text", "zero"],
)
async def test_daily_salt_usage_rejects_malformed_setting_value(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    malformed: str,
) -> None:
    """A malformed ``inlet_hardness`` setting falls back, never a nan state.

    ``float("Infinity")`` and ``float("NaN")`` succeed, so without an explicit
    finiteness guard a corrupted settings document would poison the estimate
    (and its ``inlet_hardness_dh`` attribute) with a non-finite number instead
    of taking the honest raw-property path.
    """
    freezer.move_to(FROZEN_INSTANT)
    settings = with_setting_value(
        load_fixture("settings.json"), "inlet_hardness", malformed
    )
    add_device_routes(mock_api, settings=settings)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    assert _native(hass, "daily_salt_usage") == pytest.approx(
        DAILY_SALT_FALLBACK, abs=0.1
    )
    attributes = _attributes(hass, "daily_salt_usage")
    assert attributes["inlet_hardness_source"] == "device_property"
    assert attributes["inlet_hardness_dh"] == round(INLET_DH_FALLBACK, 2)


async def test_daily_salt_usage_falls_back_when_settings_endpoint_fails(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A settings endpoint that never loads takes the same raw-property path.

    The salt sensors must stay meaningful without the settings document: the
    entity is still created (its existence gate reads only the fast payload)
    and the estimate falls back to ``hardness_grains``.
    """
    freezer.move_to(FROZEN_INSTANT)
    _fail_settings_route(mock_api)
    add_device_routes(mock_api)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    settings_coordinator = mock_config_entry.runtime_data.settings_coordinators[
        TEST_DEVICE_ID
    ]
    assert settings_coordinator.data is None

    assert _native(hass, "daily_salt_usage") == pytest.approx(
        DAILY_SALT_FALLBACK, abs=0.1
    )
    attributes = _attributes(hass, "daily_salt_usage")
    assert attributes["inlet_hardness_source"] == "device_property"
    assert attributes["inlet_hardness_dh"] == round(INLET_DH_FALLBACK, 2)
    # The raw enum still identifies the regenerant without the document.
    assert attributes["salt_type"] == "NaCl"


# ---------------------------------------------------------------------------
# Cross-check days and the device counters
# ---------------------------------------------------------------------------


async def test_salt_days_remaining_value_and_attributes(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The cross-check re-times the device's 167 days to ~155 at -7.2 %.

    The device countdown stays PRIMARY: it is republished verbatim as the
    ``device_estimate_days`` attribute alongside both daily rates, so the
    deviation is traceable to its two inputs.
    """
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(mock_api)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    assert _native(hass, "salt_days_remaining") == pytest.approx(
        CROSS_CHECK_DAYS, abs=0.1
    )

    attributes = _attributes(hass, "salt_days_remaining")
    assert attributes["device_estimate_days"] == DEVICE_ESTIMATE_DAYS
    assert attributes["chemistry_daily_salt_g"] == round(DAILY_SALT_SETTINGS, 1)
    assert attributes["device_daily_salt_g"] == round(DEVICE_DAILY_SALT, 1)
    assert attributes["deviation_pct"] == -7.2
    # Inside the ±15 % agreement band the contract requires of the cross-check.
    assert abs(attributes["deviation_pct"]) < 15


async def test_salt_efficiency_value_and_attributes(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The rated 2152 gr/lb property yields 3.072 mol/kg, source-tagged."""
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(mock_api)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    assert _native(hass, "salt_efficiency") == pytest.approx(
        EFFICIENCY_RATED, abs=0.0005
    )
    attributes = _attributes(hass, "salt_efficiency")
    assert attributes["grains_per_pound"] == 2152
    assert attributes["source"] == "device_rated_property"
    assert attributes["unit_of_measurement"] == "mol/kg"


async def test_salt_efficiency_falls_back_to_lifetime_totals(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Without the rated property the 175.4 / 570.4 lb totals ratio serves.

    The two sources are the same operational ratio on real hardware, so the
    fallback lands within 0.001 mol/kg of the rated figure — and the attributes
    say which one produced the number.
    """
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(
        mock_api, device_detail=_without_property("salt_effic_grains_per_lb")
    )
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    assert _native(hass, "salt_efficiency") == pytest.approx(
        EFFICIENCY_TOTALS, abs=0.0005
    )
    attributes = _attributes(hass, "salt_efficiency")
    assert attributes["grains_per_pound"] == 2153
    assert attributes["source"] == "lifetime_totals"


async def test_salt_per_regeneration_native_pounds_display_kilograms(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The dose binds native pounds (38281 / 10000) and displays metric kg."""
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(mock_api)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    assert _native(hass, "salt_per_regeneration") == pytest.approx(SALT_PER_REGEN_LB)
    assert float(_state_value(hass, "salt_per_regeneration")) == pytest.approx(
        SALT_PER_REGEN_KG, abs=0.001
    )
    attributes = _attributes(hass, "salt_per_regeneration")
    assert attributes["unit_of_measurement"] == UnitOfMass.KILOGRAMS


# ---------------------------------------------------------------------------
# Registry categories and enabled-by-default flags
# ---------------------------------------------------------------------------


async def test_registry_categories_and_enabled_defaults(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The three new diagnostics carry the contract's category/enabled flags.

    ``salt_days_remaining`` and ``salt_efficiency`` are diagnostic but enabled
    (they are the cross-check a user is meant to see); the depletion timestamp
    is diagnostic and registry-disabled by default, and the two user-facing
    sensors carry no category at all.
    """
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(mock_api)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    registry = er.async_get(hass)
    entries = {key: registry.async_get(_entity_id(hass, key)) for key in SALT_KEYS}
    assert all(entry is not None for entry in entries.values())

    diagnostic_enabled = ("salt_days_remaining", "salt_efficiency")
    for key in diagnostic_enabled:
        entry = entries[key]
        assert entry is not None
        assert entry.entity_category is EntityCategory.DIAGNOSTIC
        assert entry.disabled_by is None

    depletion = entries["salt_depletion_estimate"]
    assert depletion is not None
    assert depletion.entity_category is EntityCategory.DIAGNOSTIC
    assert depletion.disabled_by is er.RegistryEntryDisabler.INTEGRATION

    for key in ("daily_salt_usage", "salt_per_regeneration"):
        entry = entries[key]
        assert entry is not None
        assert entry.entity_category is None
        assert entry.disabled_by is None


# ---------------------------------------------------------------------------
# Existence gates
# ---------------------------------------------------------------------------


async def test_salt_estimates_absent_without_average_daily_use(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """No ``avg_daily_use_gals`` means no chemistry estimate at all.

    Every chemistry-derived sensor depends on the metered volume, so all three
    disappear; the two pure device-counter sensors are unaffected.
    """
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(mock_api, device_detail=_without_property("avg_daily_use_gals"))
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    for key in ("daily_salt_usage", "salt_days_remaining", "salt_depletion_estimate"):
        assert _lookup(hass, key) is None, f"sensor {key} should not exist"
    for key in ("salt_per_regeneration", "salt_efficiency"):
        assert _lookup(hass, key) is not None, f"sensor {key} should exist"


async def test_cross_check_absent_without_salt_per_regeneration(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """No ``avg_salt_per_regen_lbs`` drops the dose sensor and both cross-checks.

    The daily estimate does not need the device's regeneration averages, so it
    survives at its full settings-driven value.
    """
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(
        mock_api, device_detail=_without_property("avg_salt_per_regen_lbs")
    )
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    for key in (
        "salt_per_regeneration",
        "salt_days_remaining",
        "salt_depletion_estimate",
    ):
        assert _lookup(hass, key) is None, f"sensor {key} should not exist"

    assert _native(hass, "daily_salt_usage") == pytest.approx(
        DAILY_SALT_SETTINGS, abs=0.1
    )


# ---------------------------------------------------------------------------
# Depletion timestamp (device-local midnight)
# ---------------------------------------------------------------------------


async def test_salt_depletion_estimate_is_device_local_midnight(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Enabled, the timestamp is device-local midnight ``int(days)`` ahead.

    The cross-check is 154.96 days, so the date advances by the whole 154 days
    (truncated, never rounded up) from the frozen 2026-07-21 device-local date,
    landing on winter-time (CET, +01:00) 2026-12-22.
    """
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(mock_api)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)
        registry = er.async_get(hass)
        registry.async_update_entity(
            _entity_id(hass, "salt_depletion_estimate"), disabled_by=None
        )
        await hass.config_entries.async_reload(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    warsaw = dt_util.get_time_zone("Europe/Warsaw")
    assert warsaw is not None
    target = dt_util.now(warsaw).date() + timedelta(days=CROSS_CHECK_DAYS_INT)
    expected = datetime.combine(target, time(), tzinfo=warsaw)

    value = _native(hass, "salt_depletion_estimate")
    assert isinstance(value, datetime)
    assert value == expected
    assert value.date() == date(2026, 12, 22)
    assert value.tzinfo == warsaw
    assert (value.hour, value.minute, value.second) == (0, 0, 0)
    assert value.utcoffset() == timedelta(hours=1)


# ---------------------------------------------------------------------------
# Settings-coordinator listener (re-render without a fast poll)
# ---------------------------------------------------------------------------


async def test_settings_update_rerenders_without_a_fast_poll(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A new settings document re-renders the salt sensors immediately.

    An owner raising the inlet hardness in the app lands through the settings
    coordinator (6-hour poll or PATCH echo). The salt sensors listen to it
    directly, so the estimate follows without waiting for — or triggering — a
    device-detail poll: the fast coordinator's view is untouched.
    """
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(mock_api)
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    assert _native(hass, "daily_salt_usage") == pytest.approx(
        DAILY_SALT_SETTINGS, abs=0.1
    )
    runtime = mock_config_entry.runtime_data
    device_before = runtime.coordinators[TEST_DEVICE_ID].data
    polls_before = _device_detail_get_count(mock_api)
    # The setup poll is recorded, so the counter below is not vacuously equal.
    assert polls_before == 1

    harder = with_setting_value(load_fixture("settings.json"), "inlet_hardness", "30.0")
    runtime.settings_coordinators[TEST_DEVICE_ID].async_set_updated_data(
        DeviceSettingsDocument.from_dict(harder)
    )
    await hass.async_block_till_done()

    assert float(_state_value(hass, "daily_salt_usage")) == pytest.approx(
        DAILY_SALT_30_GPG, abs=0.1
    )
    assert _attributes(hass, "daily_salt_usage")["inlet_hardness_dh"] == INLET_DH_30_GPG
    # The harder water burns the salt faster, so the cross-check shortens too.
    days = _native(hass, "salt_days_remaining")
    assert isinstance(days, float)
    assert days < CROSS_CHECK_DAYS

    # No fast poll happened: same device view object, same request count.
    assert runtime.coordinators[TEST_DEVICE_ID].data is device_before
    assert _device_detail_get_count(mock_api) == polls_before


# ---------------------------------------------------------------------------
# Salt-type attribute ladder (informational only)
# ---------------------------------------------------------------------------


async def test_salt_type_falls_back_to_settings_document(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Without ``salt_type_enum`` the settings document names the regenerant."""
    freezer.move_to(FROZEN_INSTANT)
    settings = with_setting_value(load_fixture("settings.json"), "salt_type", "1")
    add_device_routes(
        mock_api,
        device_detail=_without_property("salt_type_enum"),
        settings=settings,
    )
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    assert _attributes(hass, "daily_salt_usage")["salt_type"] == "KCl"


async def test_salt_type_attribute_omitted_when_unknown(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """With neither source the attribute is omitted, never guessed.

    The efficiency is already denominated in actual salt mass, so an unknown
    regenerant costs the estimate nothing — and inventing one would be folklore.
    """
    freezer.move_to(FROZEN_INSTANT)
    settings = without_setting(load_fixture("settings.json"), "salt_type")
    add_device_routes(
        mock_api,
        device_detail=_without_property("salt_type_enum"),
        settings=settings,
    )
    with _ONLY_SENSOR:
        await setup_integration(hass, mock_config_entry)

    attributes = _attributes(hass, "daily_salt_usage")
    assert "salt_type" not in attributes
    # The estimate itself is unaffected by the missing regenerant name.
    assert _native(hass, "daily_salt_usage") == pytest.approx(
        DAILY_SALT_SETTINGS, abs=0.1
    )
