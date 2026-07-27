"""Cross-platform Phase-6 smoke test — salt intelligence inside a real boot.

The dedicated Phase-6 suites drive the chemistry (:mod:`tests.test_salt`), the
five sensors (:mod:`tests.test_sensor_salt`) and the repair nudge
(:mod:`tests.test_issues_salt`) in isolation. This file, like
:mod:`tests.test_phase3_smoke` and :mod:`tests.test_phase5_smoke`, boots the
integration exactly as Home Assistant would — every platform forwarded, the real
captured cloud payloads behind ``aioresponses`` — and checks that the salt layer
hangs together with everything else:

* all five salt sensors materialise beside the pre-existing inventory (35
  sensors in total) and carry the ground-truth values computed from first
  principles against the very same fixtures — 254.6 g/d of salt, a 155-day
  cross-check of the device's own 167-day countdown, 3.07 mol/kg of measured
  efficiency and the 3.8281 lb per-regeneration dose rendered in kilograms;
* the depletion timestamp ships registry-disabled, and once enabled it renders a
  real device-local midnight rather than a broken entity;
* the device's countdown — never the chemistry estimate — drives the Repairs
  nudge: the captured 167-day payload raises nothing, while the same boot
  against a 7-day payload lands the critical low-salt issue with its rendered
  placeholders.

Time is frozen throughout: the depletion sensor projects from ``now()`` in the
device's own timezone, so the expected timestamp below is only stable against a
fixed clock.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, Final

import pytest
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, UnitOfMass, UnitOfTime
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.aquahome.const import DOMAIN
from tests.conftest import add_device_routes, load_fixture, setup_integration

if TYPE_CHECKING:
    from aioresponses import aioresponses
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

#: Slug derived from the fixture serial ``7384243-20203-1120`` (see entity.py).
SLUG: Final = "7384243_20203_1120"

#: Repair issue id the salt nudge maintains for the fixture device.
ISSUE_ID: Final = f"low_salt_{SLUG}"

#: The instant every clock in this module reads — 14:00 Europe/Warsaw, the
#: device's own zone, so the projected depletion date below is deterministic.
FROZEN_NOW: Final = "2026-07-21T12:00:00+00:00"

#: Sensors the captured dev fixtures create: the Phase-3 set of 30 plus the five
#: Phase-6 salt sensors (daily usage, days remaining, depletion timestamp,
#: per-regeneration, efficiency). Pinned identically by test_phase3_smoke.
EXPECTED_SENSORS: Final = 37

# ---------------------------------------------------------------------------
# Ground truth (PHASE6_CONTRACT.md), computed from first principles against the
# real fixtures: inlet_hardness 25.7 gpg, avg_daily_use_gals 47,
# salt_effic_grains_per_lb 2152, avg_salt_per_regen_lbs 3.8281,
# avg_days_between_regens 7.35, out_of_salt_estimate_days 167.
# ---------------------------------------------------------------------------

#: Chemistry daily consumption, g/d (settings-document hardness).
DAILY_SALT_GRAMS: Final = 254.60
#: Cross-check countdown: 167 d re-timed from the device rate to the chemistry rate.
CROSS_CHECK_DAYS: Final = 154.95
#: Operational efficiency from the device's rated grains-per-pound counter.
EFFICIENCY_MOL_PER_KG: Final = 3.07162
#: Per-regeneration dose: the 3.8281 lb property in the account's kilograms.
SALT_PER_REGEN_KG: Final = 1.7364

#: Device-local midnight 154 days (``int`` of the cross-check) past the frozen
#: clock: 2026-12-22 00:00 in Europe/Warsaw, which is 23:00 UTC the day before.
DEPLETION_TIMESTAMP: Final = "2026-12-21T23:00:00+00:00"

#: Countdown served by the second boot's payload — inside the critical tier.
CRITICAL_DAYS: Final = 7
#: The cross-check scales with the device countdown it re-times (155 x 7/167).
CRITICAL_CROSS_CHECK_DAYS: Final = 6.495


# ---------------------------------------------------------------------------
# Fixture builders and registry/state helpers
# ---------------------------------------------------------------------------


def _detail_with_salt_days(days: int) -> dict[str, Any]:
    """Return the captured device detail with a rewritten salt countdown.

    ``out_of_salt_estimate_days`` is unscaled, so the raw property value is the
    day count the integration reads.
    """
    detail = copy.deepcopy(load_fixture("device-detail.json"))
    detail["properties"]["out_of_salt_estimate_days"]["value"] = days
    return detail


def _entity_id(registry: er.EntityRegistry, key: str) -> str:
    """Resolve a sensor entity id from its unique-id suffix."""
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{SLUG}_{key}")
    assert entity_id is not None, f"sensor {key} was not registered"
    return entity_id


def _state(hass: HomeAssistant, registry: er.EntityRegistry, key: str) -> str:
    """Return the state string of the sensor with unique-id suffix ``key``."""
    state = hass.states.get(_entity_id(registry, key))
    assert state is not None, f"sensor {key} has no state"
    return state.state


def _attributes(
    hass: HomeAssistant, registry: er.EntityRegistry, key: str
) -> dict[str, Any]:
    """Return the state attributes of the sensor with unique-id suffix ``key``."""
    state = hass.states.get(_entity_id(registry, key))
    assert state is not None, f"sensor {key} has no state"
    return dict(state.attributes)


def _sensor_count(registry: er.EntityRegistry, entry: MockConfigEntry) -> int:
    """Return how many sensor entities the entry registered."""
    return sum(
        1
        for registered in er.async_entries_for_config_entry(registry, entry.entry_id)
        if registered.domain == "sensor"
    )


# ---------------------------------------------------------------------------
# Full boot: the salt layer alongside every pre-existing entity
# ---------------------------------------------------------------------------


async def test_full_boot_exposes_salt_intelligence(  # noqa: PLR0913 - one fixture per subsystem the boot touches
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    issue_registry: ir.IssueRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Boot every platform and check the five salt sensors and the quiet nudge."""
    freezer.move_to(FROZEN_NOW)
    add_device_routes(mock_api)

    assert await setup_integration(hass, mock_config_entry)

    # The salt sensors join the existing inventory rather than replacing any.
    assert _sensor_count(entity_registry, mock_config_entry) == EXPECTED_SENSORS

    # -- Chemistry estimate: 254.6 g/d from the settings-document hardness ----
    daily = _state(hass, entity_registry, "daily_salt_usage")
    assert float(daily) == pytest.approx(DAILY_SALT_GRAMS, abs=0.1)
    daily_attributes = _attributes(hass, entity_registry, "daily_salt_usage")
    assert daily_attributes[ATTR_UNIT_OF_MEASUREMENT] == "g/d"
    assert daily_attributes["inlet_hardness_dh"] == pytest.approx(24.65, abs=0.001)
    assert daily_attributes["inlet_hardness_source"] == "device_setting"
    assert daily_attributes["outlet_hardness_dh"] == 0.0
    assert daily_attributes["salt_type"] == "NaCl"

    # -- Cross-check: the device's 167 d countdown re-timed to ~155 d ---------
    days = _state(hass, entity_registry, "salt_days_remaining")
    assert float(days) == pytest.approx(CROSS_CHECK_DAYS, abs=0.1)
    days_attributes = _attributes(hass, entity_registry, "salt_days_remaining")
    assert days_attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfTime.DAYS
    assert days_attributes["device_estimate_days"] == 167
    assert days_attributes["chemistry_daily_salt_g"] == pytest.approx(254.6, abs=0.1)
    assert days_attributes["device_daily_salt_g"] == pytest.approx(236.2, abs=0.1)
    # Well inside the +-15 % agreement band the cross-check exists to police.
    assert days_attributes["deviation_pct"] == pytest.approx(-7.2, abs=0.1)

    # -- Self-calibrated efficiency and the dose that produced it -------------
    efficiency = _state(hass, entity_registry, "salt_efficiency")
    assert float(efficiency) == pytest.approx(EFFICIENCY_MOL_PER_KG, abs=0.0005)
    efficiency_attributes = _attributes(hass, entity_registry, "salt_efficiency")
    assert efficiency_attributes[ATTR_UNIT_OF_MEASUREMENT] == "mol/kg"
    assert efficiency_attributes["grains_per_pound"] == 2152
    assert efficiency_attributes["source"] == "device_rated_property"

    per_regen = _state(hass, entity_registry, "salt_per_regeneration")
    assert float(per_regen) == pytest.approx(SALT_PER_REGEN_KG, abs=1e-4)
    per_regen_attributes = _attributes(hass, entity_registry, "salt_per_regeneration")
    assert per_regen_attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfMass.KILOGRAMS

    # -- The depletion timestamp ships disabled, and works once enabled -------
    depletion_id = _entity_id(entity_registry, "salt_depletion_estimate")
    depletion_entry = entity_registry.async_get(depletion_id)
    assert depletion_entry is not None
    assert depletion_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(depletion_id) is None

    entity_registry.async_update_entity(depletion_id, disabled_by=None)
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert _state(hass, entity_registry, "salt_depletion_estimate") == (
        DEPLETION_TIMESTAMP
    )

    # -- A comfortable 167-day countdown nudges nobody ------------------------
    assert issue_registry.async_get_issue(DOMAIN, ISSUE_ID) is None


# ---------------------------------------------------------------------------
# Full boot against a nearly-empty brine tank
# ---------------------------------------------------------------------------


async def test_full_boot_raises_the_critical_salt_issue(  # noqa: PLR0913 - one fixture per subsystem the boot touches
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    issue_registry: ir.IssueRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A 7-day countdown raises the critical repair issue during setup itself."""
    freezer.move_to(FROZEN_NOW)
    add_device_routes(mock_api, device_detail=_detail_with_salt_days(CRITICAL_DAYS))

    assert await setup_integration(hass, mock_config_entry)

    issue = issue_registry.async_get_issue(DOMAIN, ISSUE_ID)
    assert issue is not None
    assert issue.translation_key == "salt_level_critical"
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.is_fixable is False
    assert issue.is_persistent is False
    assert issue.translation_placeholders == {"device": "Dom", "days": "7"}

    # The sensors boot from the same payload: the cross-check re-times the
    # shortened countdown, and the rates behind it are unchanged.
    days = _state(hass, entity_registry, "salt_days_remaining")
    assert float(days) == pytest.approx(CRITICAL_CROSS_CHECK_DAYS, abs=0.01)
    days_attributes = _attributes(hass, entity_registry, "salt_days_remaining")
    assert days_attributes["device_estimate_days"] == CRITICAL_DAYS
    assert float(_state(hass, entity_registry, "daily_salt_usage")) == pytest.approx(
        DAILY_SALT_GRAMS, abs=0.1
    )
