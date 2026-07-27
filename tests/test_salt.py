"""Tests for the pure salt chemistry of :mod:`custom_components.aquahome.salt`.

``salt.py`` is the arithmetic floor of the salt-intelligence feature: five
stdlib-only functions that turn the softener's own counters into a daily
salt-consumption estimate and a days-until-empty cross-check. Nothing here
touches Home Assistant, a coordinator or a clock — the module imports nothing
but ``typing``, so it is tested directly, with literal inputs and expectations
derived from first principles. The entity layer that feeds these functions is
covered by ``test_sensor_salt.py``; this file is the net underneath it, which
is where a silent coefficient regression would otherwise hide unnoticed behind
a plausible-looking number.

Every expected value below is a row of the frozen Phase 6 ground-truth table,
computed independently from the real captured fixtures (``properties.json`` +
``settings.json``) and asserted at that table's tolerances — never looser. The
final section re-reads those fixtures and pins the *inputs* too, so a fixture
edit that would quietly invalidate the table fails here rather than silently
re-baselining the chemistry.

Two behaviours deserve their own attention and get it below:

* **The stoichiometric guard.** ~116.9 g of NaCl per mol of hardness (2 Na⁺ per
  Ca²⁺) is a physics floor, so ~8.56 mol/kg is a ceiling no softener can beat.
  A rated efficiency above it is corrupt data, and — crucially — it must *fall
  through* to the lifetime-totals ratio rather than poison the estimate or
  abort it.
* **None-safety.** These functions are called with whatever the cloud happened
  to return; a missing or impossible input must yield ``None``, never an
  exception and never a fabricated number.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Final

import pytest

from custom_components.aquahome.salt import (
    DH_MMOL_PER_L,
    EFFICIENCY_SOURCE_RATED,
    EFFICIENCY_SOURCE_TOTALS,
    GPG_TO_DH,
    GRAMS_PER_POUND,
    LITERS_PER_GALLON,
    MOL_PER_KG_PER_GRAIN_PER_LB,
    STOICHIOMETRIC_MAX_MOL_PER_KG,
    cross_check_days,
    daily_salt_grams,
    device_daily_salt_grams,
    efficiency_mol_per_kg,
    efficiency_mol_per_kg_with_source,
)

# ---------------------------------------------------------------------------
# Ground-truth inputs — the scaled readings of the captured fixtures
#
# The raw payload integers are scale-encoded (avg_salt_per_regen_lbs x10^4,
# avg_days_between_regens x10^2, the two lifetime totals x10); the values below
# are the decoded ones the entity layer passes in. The last section of this
# module asserts the fixtures still carry exactly these.
# ---------------------------------------------------------------------------

#: ``salt_effic_grains_per_lb`` — the device's own rated efficiency figure.
RATED_GRAINS_PER_LB: Final = 2152.0
#: ``total_rock_removed_lbs`` (1754 / 10) — lifetime hardness removed.
TOTAL_ROCK_LB: Final = 175.4
#: ``total_salt_use_lbs`` (5704 / 10) — lifetime salt consumed.
TOTAL_SALT_LB: Final = 570.4
#: Settings-document ``inlet_hardness`` — gpg-denominated whatever the label.
SETTINGS_HARDNESS_GPG: Final = 25.7
#: Raw ``hardness_grains`` — the integer fallback for the setting above.
RAW_HARDNESS_GPG: Final = 26.0
#: ``avg_daily_use_gals`` — the device's long-run daily water volume.
AVG_DAILY_USE_GALS: Final = 47.0
#: ``avg_salt_per_regen_lbs`` (38281 / 10^4) — salt dose per regeneration.
AVG_SALT_PER_REGEN_LB: Final = 3.8281
#: ``avg_days_between_regens`` (735 / 100) — mean regeneration interval.
AVG_DAYS_BETWEEN_REGENS: Final = 7.35
#: ``out_of_salt_estimate_days`` — the device countdown, PRIMARY everywhere.
DEVICE_ESTIMATE_DAYS: Final = 167.0

#: Structural outlet hardness: no known iQua model exposes a blend setting.
OUTLET_DH: Final = 0.0

# ---------------------------------------------------------------------------
# Ground-truth expectations and their binding tolerances
# ---------------------------------------------------------------------------

#: Efficiency from the rated property, mol CaCO₃ per kg salt.
E_RATED: Final = 3.07162
#: Efficiency from the lifetime-totals ratio, mol CaCO₃ per kg salt.
E_TOTALS: Final = 3.07237
#: Inlet hardness in °dH resolved from the settings document.
INLET_DH_SETTING: Final = 24.6489
#: Inlet hardness in °dH resolved from the raw property fallback.
INLET_DH_PROPERTY: Final = 24.9367
#: Daily metered volume in litres.
LITERS_PER_DAY: Final = 177.91
#: Daily salt consumption with the settings-document hardness, grams.
DAILY_SALT_G_SETTING: Final = 254.60
#: Daily salt consumption with the raw-property hardness, grams.
DAILY_SALT_G_PROPERTY: Final = 257.57
#: The device-observed daily salt rate, grams.
DEVICE_DAILY_SALT_G: Final = 236.24
#: The chemistry-timed days-until-empty cross-check.
CROSS_CHECK_DAYS: Final = 154.95
#: Cross-check deviation from the device countdown, percent.
DEVIATION_PCT: Final = -7.2
#: The band the cross-check is expected to stay inside, percent.
DEVIATION_BAND_PCT: Final = 15.0

#: Six-significant-figure tolerance for the ~0.1-1 scale constants.
CONSTANT_TOLERANCE: Final = 5e-7
#: Six-significant-figure tolerance for the ~0.001 scale constant.
SMALL_CONSTANT_TOLERANCE: Final = 5e-10
#: Efficiency tolerance, mol/kg.
E_TOLERANCE: Final = 0.0005
#: Hardness tolerance, °dH.
DH_TOLERANCE: Final = 0.001
#: Volume tolerance, litres.
LITERS_TOLERANCE: Final = 0.01
#: Salt-mass tolerance, grams per day.
GRAMS_TOLERANCE: Final = 0.1
#: Days tolerance for the cross-check.
DAYS_TOLERANCE: Final = 0.1
#: Deviation-percentage tolerance.
PERCENT_TOLERANCE: Final = 0.05

#: The rated figure that lands exactly on the stoichiometric ceiling.
CEILING_GRAINS_PER_LB: Final = (
    STOICHIOMETRIC_MAX_MOL_PER_KG / MOL_PER_KG_PER_GRAIN_PER_LB
)


# ---------------------------------------------------------------------------
# Exact constants — definitional, not measured
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("constant", "definition"),
    [
        (GPG_TO_DH, 0.06479891 * 1000.0 / 3.785411784 / 17.848),
        (DH_MMOL_PER_L, 17.848 / 100.0869),
        (MOL_PER_KG_PER_GRAIN_PER_LB, 0.06479891 / 100.0869 / 0.45359237),
        (GRAMS_PER_POUND, 453.59237),
        (LITERS_PER_GALLON, 3.785411784),
        (STOICHIOMETRIC_MAX_MOL_PER_KG, 8.56),
    ],
    ids=[
        "gpg-to-dh",
        "dh-to-mmol-per-litre",
        "grain-per-pound-to-mol-per-kg",
        "grams-per-pound",
        "litres-per-gallon",
        "stoichiometric-ceiling",
    ],
)
def test_constants_are_exactly_their_definitions(
    constant: float, definition: float
) -> None:
    """Every coefficient is built from exact unit definitions, not measurement.

    1 grain = 0.06479891 g, 1 US gallon = 3.785411784 L and 1 lb = 0.45359237
    kg are exact by definition; 1 °dH = 17.848 mg/L as CaCO₃ (10 mg/L CaO
    scaled by the 100.0869 / 56.0774 molar-mass ratio) and CaCO₃ is 100.0869
    g/mol. Nothing here is rounded or empirical, so each constant is pinned to
    the expression that produces it — bit for bit, no tolerance.
    """
    assert constant == definition


@pytest.mark.parametrize(
    ("constant", "expected", "tolerance"),
    [
        (GPG_TO_DH, 0.959102, CONSTANT_TOLERANCE),
        (DH_MMOL_PER_L, 0.178325, CONSTANT_TOLERANCE),
        (MOL_PER_KG_PER_GRAIN_PER_LB, 0.001427331, SMALL_CONSTANT_TOLERANCE),
    ],
    ids=["gpg-to-dh", "dh-to-mmol-per-litre", "grain-per-pound-to-mol-per-kg"],
)
def test_constants_match_the_ground_truth_table(
    constant: float, expected: float, tolerance: float
) -> None:
    """The derived coefficients equal the table's six-significant-figure rows."""
    assert constant == pytest.approx(expected, abs=tolerance)


def test_grain_and_pound_definitions_close_exactly() -> None:
    """7000 grains weigh exactly one pound — the totals-ratio identity.

    The lifetime-totals fallback multiplies the rock/salt ratio by 7000 to
    reach grains per pound; that factor is only legitimate because the two
    exact definitions close on each other, which is what this asserts.
    """
    seven_thousand_grains_in_grams = 7000.0 * 0.06479891
    assert seven_thousand_grains_in_grams == pytest.approx(GRAMS_PER_POUND, abs=1e-9)


def test_efficiency_source_sentinels_are_stable_strings() -> None:
    """The sensor layer maps these to attribute values, so they are API."""
    assert EFFICIENCY_SOURCE_RATED == "rated"
    assert EFFICIENCY_SOURCE_TOTALS == "totals"


# ---------------------------------------------------------------------------
# efficiency_mol_per_kg_with_source — source preference
# ---------------------------------------------------------------------------


def test_rated_property_is_the_primary_efficiency_source() -> None:
    """The device's own rated figure wins whenever it is plausible."""
    resolved = efficiency_mol_per_kg_with_source(
        RATED_GRAINS_PER_LB, TOTAL_ROCK_LB, TOTAL_SALT_LB
    )
    assert resolved is not None
    value, source = resolved
    assert value == pytest.approx(E_RATED, abs=E_TOLERANCE)
    assert source == EFFICIENCY_SOURCE_RATED


def test_lifetime_totals_are_the_fallback_efficiency_source() -> None:
    """Without a rated figure the lifetime rock/salt ratio is used."""
    resolved = efficiency_mol_per_kg_with_source(None, TOTAL_ROCK_LB, TOTAL_SALT_LB)
    assert resolved is not None
    value, source = resolved
    assert value == pytest.approx(E_TOTALS, abs=E_TOLERANCE)
    assert source == EFFICIENCY_SOURCE_TOTALS


def test_both_efficiency_sources_agree_on_the_reference_device() -> None:
    """The two sources are the same operational ratio, verified on the device.

    ``salt_effic_grains_per_lb`` is 7000 x rock ÷ salt on the reference unit
    (2152.52 vs the reported 2152), which is why no literature or brand
    coefficient is ever needed — and why the NaCl/KCl choice never enters the
    math: both figures are already denominated in actual salt mass.
    """
    implied_grains_per_lb = 7000.0 * TOTAL_ROCK_LB / TOTAL_SALT_LB
    assert implied_grains_per_lb == pytest.approx(RATED_GRAINS_PER_LB, abs=1.0)

    rated = efficiency_mol_per_kg(RATED_GRAINS_PER_LB, None, None)
    totals = efficiency_mol_per_kg(None, TOTAL_ROCK_LB, TOTAL_SALT_LB)
    assert rated is not None
    assert totals is not None
    assert rated == pytest.approx(totals, rel=5e-4)


def test_rated_source_needs_no_totals_at_all() -> None:
    """A device reporting only the rated property still yields an efficiency."""
    resolved = efficiency_mol_per_kg_with_source(RATED_GRAINS_PER_LB, None, None)
    assert resolved is not None
    value, source = resolved
    assert value == pytest.approx(E_RATED, abs=E_TOLERANCE)
    assert source == EFFICIENCY_SOURCE_RATED


# ---------------------------------------------------------------------------
# efficiency_mol_per_kg_with_source — the stoichiometric guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rated",
    [0.0, -1.0, -RATED_GRAINS_PER_LB, CEILING_GRAINS_PER_LB * 1.0001, 1e9],
    ids=[
        "zero",
        "slightly-negative",
        "negated-real-value",
        "just-above-the-ceiling",
        "absurdly-large",
    ],
)
def test_implausible_rated_value_falls_through_to_the_totals(rated: float) -> None:
    """A rated figure failing the guard must not abort the estimate.

    Falling through to the lifetime totals is the whole point of the guard: a
    single corrupt property should degrade the source, not the feature.
    """
    resolved = efficiency_mol_per_kg_with_source(rated, TOTAL_ROCK_LB, TOTAL_SALT_LB)
    assert resolved is not None
    value, source = resolved
    assert value == pytest.approx(E_TOTALS, abs=E_TOLERANCE)
    assert source == EFFICIENCY_SOURCE_TOTALS


@pytest.mark.parametrize(
    "rated",
    [0.0, -1.0, -RATED_GRAINS_PER_LB, CEILING_GRAINS_PER_LB * 1.0001, 1e9],
    ids=[
        "zero",
        "slightly-negative",
        "negated-real-value",
        "just-above-the-ceiling",
        "absurdly-large",
    ],
)
def test_implausible_rated_value_without_totals_yields_none(rated: float) -> None:
    """With no fallback available a corrupt rated figure yields nothing."""
    assert efficiency_mol_per_kg_with_source(rated, None, None) is None


def test_rated_value_exactly_at_the_ceiling_is_accepted() -> None:
    """The guard is inclusive: stoichiometry itself is not impossible."""
    resolved = efficiency_mol_per_kg_with_source(
        CEILING_GRAINS_PER_LB, TOTAL_ROCK_LB, TOTAL_SALT_LB
    )
    assert resolved is not None
    value, source = resolved
    assert value == pytest.approx(STOICHIOMETRIC_MAX_MOL_PER_KG, abs=1e-12)
    assert source == EFFICIENCY_SOURCE_RATED


@pytest.mark.parametrize(
    ("rock", "salt_used"),
    [
        (TOTAL_ROCK_LB, 0.0),
        (TOTAL_ROCK_LB, -TOTAL_SALT_LB),
        (0.0, TOTAL_SALT_LB),
        (-TOTAL_ROCK_LB, TOTAL_SALT_LB),
        (TOTAL_SALT_LB, TOTAL_ROCK_LB),
        (1e6, 1.0),
    ],
    ids=[
        "zero-salt-used",
        "negative-salt-used",
        "zero-rock-removed",
        "negative-rock-removed",
        "totals-swapped",
        "implausible-ratio",
    ],
)
def test_implausible_totals_yield_none(rock: float, salt_used: float) -> None:
    """A division by zero, a negative counter or a super-stoichiometric ratio.

    ``totals-swapped`` is the realistic corruption: rock and salt transposed
    gives ~32.5 mol/kg, nearly four times the physics ceiling, and must be
    rejected rather than silently quartering the salt estimate.
    """
    assert efficiency_mol_per_kg_with_source(None, rock, salt_used) is None


@pytest.mark.parametrize(
    ("rated", "rock", "salt_used"),
    [
        (None, None, None),
        (None, TOTAL_ROCK_LB, None),
        (None, None, TOTAL_SALT_LB),
    ],
    ids=["nothing-reported", "salt-total-missing", "rock-total-missing"],
)
def test_missing_efficiency_inputs_yield_none(
    rated: float | None, rock: float | None, salt_used: float | None
) -> None:
    """A half-present totals pair is no more usable than an absent one."""
    assert efficiency_mol_per_kg_with_source(rated, rock, salt_used) is None


def test_both_sources_failing_the_guard_yields_none() -> None:
    """Corrupt everywhere is the one case that legitimately produces nothing."""
    assert efficiency_mol_per_kg_with_source(1e9, TOTAL_SALT_LB, TOTAL_ROCK_LB) is None


# ---------------------------------------------------------------------------
# efficiency_mol_per_kg — the value-only wrapper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rated", "rock", "salt_used"),
    [
        (RATED_GRAINS_PER_LB, TOTAL_ROCK_LB, TOTAL_SALT_LB),
        (RATED_GRAINS_PER_LB, None, None),
        (None, TOTAL_ROCK_LB, TOTAL_SALT_LB),
        (0.0, TOTAL_ROCK_LB, TOTAL_SALT_LB),
        (1e9, TOTAL_ROCK_LB, TOTAL_SALT_LB),
        (-RATED_GRAINS_PER_LB, None, None),
        (None, None, None),
        (None, TOTAL_SALT_LB, TOTAL_ROCK_LB),
        (None, TOTAL_ROCK_LB, 0.0),
    ],
    ids=[
        "both-sources",
        "rated-only",
        "totals-only",
        "zero-rated-falls-through",
        "huge-rated-falls-through",
        "negative-rated-alone",
        "nothing-reported",
        "totals-swapped",
        "zero-salt-used",
    ],
)
def test_wrapper_returns_exactly_the_sourced_value(
    rated: float | None, rock: float | None, salt_used: float | None
) -> None:
    """The value-only wrapper never re-implements the plausibility guard.

    The efficiency sensor reads the value through the wrapper and its
    ``source`` attribute through the tuple variant; if the two ever diverged,
    an entity would report a number attributed to the wrong source.
    """
    resolved = efficiency_mol_per_kg_with_source(rated, rock, salt_used)
    value = efficiency_mol_per_kg(rated, rock, salt_used)
    if resolved is None:
        assert value is None
    else:
        assert value == resolved[0]


# ---------------------------------------------------------------------------
# daily_salt_grams — the chemistry estimate
# ---------------------------------------------------------------------------


def test_daily_salt_with_the_settings_hardness() -> None:
    """The headline ground-truth row: 47 gal/day at 25.7 gpg costs 254.6 g."""
    value = daily_salt_grams(
        AVG_DAILY_USE_GALS * LITERS_PER_GALLON,
        SETTINGS_HARDNESS_GPG * GPG_TO_DH,
        OUTLET_DH,
        efficiency_mol_per_kg(RATED_GRAINS_PER_LB, TOTAL_ROCK_LB, TOTAL_SALT_LB),
    )
    assert value == pytest.approx(DAILY_SALT_G_SETTING, abs=GRAMS_TOLERANCE)


def test_daily_salt_with_the_raw_property_hardness() -> None:
    """The integer ``hardness_grains`` fallback costs 1.2 % more salt.

    26 gpg instead of the settings document's 25.7 is the entire difference;
    the fallback is close enough that a missing settings document degrades the
    estimate rather than invalidating it.
    """
    value = daily_salt_grams(
        AVG_DAILY_USE_GALS * LITERS_PER_GALLON,
        RAW_HARDNESS_GPG * GPG_TO_DH,
        OUTLET_DH,
        efficiency_mol_per_kg(RATED_GRAINS_PER_LB, TOTAL_ROCK_LB, TOTAL_SALT_LB),
    )
    assert value == pytest.approx(DAILY_SALT_G_PROPERTY, abs=GRAMS_TOLERANCE)


def test_daily_salt_is_the_documented_formula() -> None:
    """Volume x differential x mmol/°dH ÷ efficiency, to the last bit."""
    litres = AVG_DAILY_USE_GALS * LITERS_PER_GALLON
    hardness = SETTINGS_HARDNESS_GPG * GPG_TO_DH
    efficiency = E_RATED
    assert daily_salt_grams(litres, hardness, OUTLET_DH, efficiency) == pytest.approx(
        litres * (hardness - OUTLET_DH) * DH_MMOL_PER_L / efficiency, rel=1e-12
    )


def test_daily_salt_scales_linearly_with_volume() -> None:
    """Twice the water is twice the hardness load, hence twice the salt."""
    single = daily_salt_grams(100.0, INLET_DH_SETTING, OUTLET_DH, E_RATED)
    double = daily_salt_grams(200.0, INLET_DH_SETTING, OUTLET_DH, E_RATED)
    assert single is not None
    assert double is not None
    assert double == pytest.approx(2.0 * single, rel=1e-12)


def test_only_the_hardness_differential_matters() -> None:
    """A blended outlet subtracts, exactly as the mass balance requires.

    No iQua model exposes a blend setting today, so callers pass 0.0 — but the
    parameter exists for the day one does, and it has to behave as a plain
    subtraction on the inlet figure.
    """
    blended = daily_salt_grams(200.0, 30.0, 5.0, E_RATED)
    equivalent = daily_salt_grams(200.0, 25.0, 0.0, E_RATED)
    assert blended is not None
    assert equivalent is not None
    assert blended == pytest.approx(equivalent, rel=1e-12)


def test_zero_water_use_costs_no_salt() -> None:
    """A dry day is a legitimate zero, not a missing value."""
    assert daily_salt_grams(0.0, INLET_DH_SETTING, OUTLET_DH, E_RATED) == 0.0


@pytest.mark.parametrize(
    ("litres", "dh_in", "efficiency"),
    [
        (None, INLET_DH_SETTING, E_RATED),
        (LITERS_PER_DAY, None, E_RATED),
        (LITERS_PER_DAY, INLET_DH_SETTING, None),
        (None, None, None),
    ],
    ids=["volume-missing", "hardness-missing", "efficiency-missing", "all-missing"],
)
def test_daily_salt_propagates_missing_inputs(
    litres: float | None, dh_in: float | None, efficiency: float | None
) -> None:
    """Any absent input yields ``None`` — never a partial estimate."""
    assert daily_salt_grams(litres, dh_in, OUTLET_DH, efficiency) is None


@pytest.mark.parametrize(
    ("litres", "dh_in", "dh_out", "efficiency"),
    [
        (-1.0, INLET_DH_SETTING, OUTLET_DH, E_RATED),
        (-LITERS_PER_DAY, INLET_DH_SETTING, OUTLET_DH, E_RATED),
        (LITERS_PER_DAY, 0.0, OUTLET_DH, E_RATED),
        (LITERS_PER_DAY, -INLET_DH_SETTING, OUTLET_DH, E_RATED),
        (LITERS_PER_DAY, 10.0, 10.0, E_RATED),
        (LITERS_PER_DAY, 10.0, 12.0, E_RATED),
        (LITERS_PER_DAY, INLET_DH_SETTING, OUTLET_DH, 0.0),
        (LITERS_PER_DAY, INLET_DH_SETTING, OUTLET_DH, -E_RATED),
    ],
    ids=[
        "negative-volume",
        "negative-real-volume",
        "zero-inlet-hardness",
        "negative-inlet-hardness",
        "no-hardness-differential",
        "outlet-harder-than-inlet",
        "zero-efficiency",
        "negative-efficiency",
    ],
)
def test_daily_salt_rejects_impossible_inputs(
    litres: float, dh_in: float, dh_out: float, efficiency: float
) -> None:
    """Physically impossible inputs yield ``None``, never a signed estimate.

    Softening water that is already soft cannot consume salt, and a
    non-positive efficiency would divide the load by zero or flip its sign —
    both would surface as a nonsense sensor state instead of an absent one.
    """
    assert daily_salt_grams(litres, dh_in, dh_out, efficiency) is None


# ---------------------------------------------------------------------------
# device_daily_salt_grams — the device-observed baseline
# ---------------------------------------------------------------------------


def test_device_daily_rate_from_the_long_run_averages() -> None:
    """3.8281 lb every 7.35 days is 236.2 g/day."""
    value = device_daily_salt_grams(AVG_SALT_PER_REGEN_LB, AVG_DAYS_BETWEEN_REGENS)
    assert value == pytest.approx(DEVICE_DAILY_SALT_G, abs=GRAMS_TOLERANCE)


def test_device_daily_rate_is_dose_over_interval() -> None:
    """The formula is a plain pounds-to-grams dose divided by the interval."""
    assert device_daily_salt_grams(2.0, 4.0) == pytest.approx(
        2.0 * GRAMS_PER_POUND / 4.0, rel=1e-12
    )


@pytest.mark.parametrize(
    ("dose_lb", "interval_days"),
    [
        (None, AVG_DAYS_BETWEEN_REGENS),
        (AVG_SALT_PER_REGEN_LB, None),
        (None, None),
    ],
    ids=["dose-missing", "interval-missing", "both-missing"],
)
def test_device_daily_rate_propagates_missing_inputs(
    dose_lb: float | None, interval_days: float | None
) -> None:
    """A device that has never regenerated reports neither average."""
    assert device_daily_salt_grams(dose_lb, interval_days) is None


@pytest.mark.parametrize(
    ("dose_lb", "interval_days"),
    [
        (0.0, AVG_DAYS_BETWEEN_REGENS),
        (-AVG_SALT_PER_REGEN_LB, AVG_DAYS_BETWEEN_REGENS),
        (AVG_SALT_PER_REGEN_LB, 0.0),
        (AVG_SALT_PER_REGEN_LB, -AVG_DAYS_BETWEEN_REGENS),
        (0.0, 0.0),
    ],
    ids=[
        "zero-dose",
        "negative-dose",
        "zero-interval",
        "negative-interval",
        "freshly-installed",
    ],
)
def test_device_daily_rate_rejects_non_positive_averages(
    dose_lb: float, interval_days: float
) -> None:
    """Zeroed counters on a fresh install must not divide by zero."""
    assert device_daily_salt_grams(dose_lb, interval_days) is None


# ---------------------------------------------------------------------------
# cross_check_days — re-timing the device countdown at the chemistry rate
# ---------------------------------------------------------------------------


def test_cross_check_days_ground_truth() -> None:
    """167 device days at 236.2 g/day re-time to 155 days at 254.6 g/day."""
    value = cross_check_days(
        DEVICE_ESTIMATE_DAYS, DEVICE_DAILY_SALT_G, DAILY_SALT_G_SETTING
    )
    assert value == pytest.approx(CROSS_CHECK_DAYS, abs=DAYS_TOLERANCE)


def test_cross_check_deviation_stays_inside_the_band() -> None:
    """The reference device deviates -7.2 %, well inside the +/-15 % band.

    The cross-check is a plausibility check on the device's own countdown, not
    a replacement for it; a reference deviation this small is what justifies
    shipping the device estimate as PRIMARY.
    """
    value = cross_check_days(
        DEVICE_ESTIMATE_DAYS, DEVICE_DAILY_SALT_G, DAILY_SALT_G_SETTING
    )
    assert value is not None
    deviation = (value / DEVICE_ESTIMATE_DAYS - 1.0) * 100.0
    assert deviation == pytest.approx(DEVIATION_PCT, abs=PERCENT_TOLERANCE)
    assert abs(deviation) < DEVIATION_BAND_PCT


def test_cross_check_is_identity_when_the_rates_agree() -> None:
    """Equal rates mean the chemistry has nothing to correct."""
    assert cross_check_days(DEVICE_ESTIMATE_DAYS, 200.0, 200.0) == DEVICE_ESTIMATE_DAYS


def test_cross_check_shortens_when_the_chemistry_rate_is_higher() -> None:
    """Consuming salt faster than the device assumes empties the tank sooner."""
    value = cross_check_days(100.0, 200.0, 400.0)
    assert value == pytest.approx(50.0, rel=1e-12)


def test_cross_check_of_an_already_empty_countdown_is_zero() -> None:
    """Zero days remaining is a real reading, not a missing one."""
    assert cross_check_days(0.0, DEVICE_DAILY_SALT_G, DAILY_SALT_G_SETTING) == 0.0


@pytest.mark.parametrize(
    ("days", "device_rate", "chemistry_rate"),
    [
        (None, DEVICE_DAILY_SALT_G, DAILY_SALT_G_SETTING),
        (DEVICE_ESTIMATE_DAYS, None, DAILY_SALT_G_SETTING),
        (DEVICE_ESTIMATE_DAYS, DEVICE_DAILY_SALT_G, None),
        (None, None, None),
    ],
    ids=[
        "countdown-missing",
        "device-rate-missing",
        "chemistry-rate-missing",
        "all-missing",
    ],
)
def test_cross_check_propagates_missing_inputs(
    days: float | None, device_rate: float | None, chemistry_rate: float | None
) -> None:
    """The cross-check needs all three inputs; any gap suppresses the sensor."""
    assert cross_check_days(days, device_rate, chemistry_rate) is None


@pytest.mark.parametrize(
    ("days", "device_rate", "chemistry_rate"),
    [
        (-1.0, DEVICE_DAILY_SALT_G, DAILY_SALT_G_SETTING),
        (-DEVICE_ESTIMATE_DAYS, DEVICE_DAILY_SALT_G, DAILY_SALT_G_SETTING),
        (DEVICE_ESTIMATE_DAYS, 0.0, DAILY_SALT_G_SETTING),
        (DEVICE_ESTIMATE_DAYS, -DEVICE_DAILY_SALT_G, DAILY_SALT_G_SETTING),
        (DEVICE_ESTIMATE_DAYS, DEVICE_DAILY_SALT_G, 0.0),
        (DEVICE_ESTIMATE_DAYS, DEVICE_DAILY_SALT_G, -DAILY_SALT_G_SETTING),
    ],
    ids=[
        "negative-countdown",
        "negative-real-countdown",
        "zero-device-rate",
        "negative-device-rate",
        "zero-chemistry-rate",
        "negative-chemistry-rate",
    ],
)
def test_cross_check_rejects_impossible_inputs(
    days: float, device_rate: float, chemistry_rate: float
) -> None:
    """A non-positive rate would divide by zero or invert the countdown."""
    assert cross_check_days(days, device_rate, chemistry_rate) is None


# ---------------------------------------------------------------------------
# The full ground-truth chain, end to end
# ---------------------------------------------------------------------------


def test_full_chain_reproduces_every_ground_truth_row() -> None:
    """Walk the whole chain from raw readings to the cross-check.

    This is the table itself, in one pass: each intermediate is asserted at
    its own tolerance so a regression names the step that broke rather than
    just the final number.
    """
    efficiency = efficiency_mol_per_kg(
        RATED_GRAINS_PER_LB, TOTAL_ROCK_LB, TOTAL_SALT_LB
    )
    assert efficiency == pytest.approx(E_RATED, abs=E_TOLERANCE)

    fallback_efficiency = efficiency_mol_per_kg(None, TOTAL_ROCK_LB, TOTAL_SALT_LB)
    assert fallback_efficiency == pytest.approx(E_TOTALS, abs=E_TOLERANCE)

    setting_dh = SETTINGS_HARDNESS_GPG * GPG_TO_DH
    property_dh = RAW_HARDNESS_GPG * GPG_TO_DH
    assert setting_dh == pytest.approx(INLET_DH_SETTING, abs=DH_TOLERANCE)
    assert property_dh == pytest.approx(INLET_DH_PROPERTY, abs=DH_TOLERANCE)

    litres = AVG_DAILY_USE_GALS * LITERS_PER_GALLON
    assert litres == pytest.approx(LITERS_PER_DAY, abs=LITERS_TOLERANCE)

    chemistry_rate = daily_salt_grams(litres, setting_dh, OUTLET_DH, efficiency)
    fallback_rate = daily_salt_grams(litres, property_dh, OUTLET_DH, efficiency)
    assert chemistry_rate == pytest.approx(DAILY_SALT_G_SETTING, abs=GRAMS_TOLERANCE)
    assert fallback_rate == pytest.approx(DAILY_SALT_G_PROPERTY, abs=GRAMS_TOLERANCE)

    device_rate = device_daily_salt_grams(
        AVG_SALT_PER_REGEN_LB, AVG_DAYS_BETWEEN_REGENS
    )
    assert device_rate == pytest.approx(DEVICE_DAILY_SALT_G, abs=GRAMS_TOLERANCE)

    days = cross_check_days(DEVICE_ESTIMATE_DAYS, device_rate, chemistry_rate)
    assert days == pytest.approx(CROSS_CHECK_DAYS, abs=DAYS_TOLERANCE)


def test_full_chain_survives_a_totals_only_device() -> None:
    """A device without the rated property still produces the whole chain.

    The totals fallback shifts the estimate by 0.02 %, which is the practical
    statement of "both sources are the same operational ratio".
    """
    efficiency = efficiency_mol_per_kg(None, TOTAL_ROCK_LB, TOTAL_SALT_LB)
    chemistry_rate = daily_salt_grams(
        AVG_DAILY_USE_GALS * LITERS_PER_GALLON,
        SETTINGS_HARDNESS_GPG * GPG_TO_DH,
        OUTLET_DH,
        efficiency,
    )
    device_rate = device_daily_salt_grams(
        AVG_SALT_PER_REGEN_LB, AVG_DAYS_BETWEEN_REGENS
    )
    days = cross_check_days(DEVICE_ESTIMATE_DAYS, device_rate, chemistry_rate)
    assert chemistry_rate == pytest.approx(DAILY_SALT_G_SETTING, abs=GRAMS_TOLERANCE)
    assert days == pytest.approx(CROSS_CHECK_DAYS, abs=DAYS_TOLERANCE)


def test_chain_collapses_to_none_when_the_efficiency_is_unusable() -> None:
    """One corrupt input at the top suppresses every downstream sensor."""
    efficiency = efficiency_mol_per_kg(1e9, TOTAL_SALT_LB, TOTAL_ROCK_LB)
    chemistry_rate = daily_salt_grams(
        AVG_DAILY_USE_GALS * LITERS_PER_GALLON,
        SETTINGS_HARDNESS_GPG * GPG_TO_DH,
        OUTLET_DH,
        efficiency,
    )
    device_rate = device_daily_salt_grams(
        AVG_SALT_PER_REGEN_LB, AVG_DAYS_BETWEEN_REGENS
    )
    assert efficiency is None
    assert chemistry_rate is None
    assert device_rate is not None
    assert cross_check_days(DEVICE_ESTIMATE_DAYS, device_rate, chemistry_rate) is None


# ---------------------------------------------------------------------------
# Fixture pinning — the table's inputs are the captured payloads
# ---------------------------------------------------------------------------

#: Captured payloads, read directly: this module stays free of the HA-importing
#: conftest so it can run as a plain unit-test file.
FIXTURES_DIR: Final = Path(__file__).parent / "fixtures"

#: Raw property name -> the verified divisor that decodes its integer value.
#: Mirrors ``api.models.SCALED_PROPERTIES`` for the salt inputs only; kept
#: local so this file imports nothing but the module under test.
SALT_PROPERTY_DIVISORS: Final[dict[str, float]] = {
    "salt_effic_grains_per_lb": 1.0,
    "total_rock_removed_lbs": 10.0,
    "total_salt_use_lbs": 10.0,
    "avg_salt_per_regen_lbs": 10_000.0,
    "avg_days_between_regens": 100.0,
    "avg_daily_use_gals": 1.0,
    "hardness_grains": 1.0,
    "out_of_salt_estimate_days": 1.0,
}


def scaled_fixture_property(name: str) -> float:
    """Return a captured raw property decoded by its verified divisor."""
    payload: dict[str, Any] = json.loads((FIXTURES_DIR / "properties.json").read_text())
    value: float = payload["properties"][name]["value"]
    return value / SALT_PROPERTY_DIVISORS[name]


def fixture_setting(name: str) -> str:
    """Return a captured settings-document ``current_value``."""
    payload: dict[str, Any] = json.loads((FIXTURES_DIR / "settings.json").read_text())
    setting: dict[str, Any] = next(
        entry for entry in payload["settings"] if entry["name"] == name
    )
    return str(setting["current_value"])


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("salt_effic_grains_per_lb", RATED_GRAINS_PER_LB),
        ("total_rock_removed_lbs", TOTAL_ROCK_LB),
        ("total_salt_use_lbs", TOTAL_SALT_LB),
        ("avg_salt_per_regen_lbs", AVG_SALT_PER_REGEN_LB),
        ("avg_days_between_regens", AVG_DAYS_BETWEEN_REGENS),
        ("avg_daily_use_gals", AVG_DAILY_USE_GALS),
        ("hardness_grains", RAW_HARDNESS_GPG),
        ("out_of_salt_estimate_days", DEVICE_ESTIMATE_DAYS),
    ],
    ids=[
        "rated-efficiency",
        "lifetime-rock",
        "lifetime-salt",
        "salt-per-regeneration",
        "regeneration-interval",
        "daily-water-use",
        "raw-hardness",
        "device-countdown",
    ],
)
def test_ground_truth_inputs_are_the_captured_properties(
    name: str, expected: float
) -> None:
    """The table's inputs are the real device readings, not invented ones.

    If a fixture is ever re-captured, this fails before the expectations
    above silently start describing a different device.
    """
    assert scaled_fixture_property(name) == pytest.approx(expected, rel=1e-12)


def test_ground_truth_hardness_is_the_captured_setting() -> None:
    """``inlet_hardness`` arrives as the string ``"25.7"``, gpg-denominated.

    The label reads PPM because the account's hardness unit is PPM, but the
    value itself is grains per gallon — the misleading label is exactly why
    the entity layer multiplies by :data:`GPG_TO_DH` regardless.
    """
    assert fixture_setting("inlet_hardness") == "25.7"
    assert float(fixture_setting("inlet_hardness")) == pytest.approx(
        SETTINGS_HARDNESS_GPG, rel=1e-12
    )


# ---------------------------------------------------------------------------
# Non-finite inputs
#
# ``json.loads`` accepts bare ``NaN``/``Infinity`` literals and NaN slips
# through every ordinary ``<``/``<=`` guard (all comparisons are ``False``),
# so without an explicit finiteness check a malformed cloud payload would
# surface as a ``nan`` sensor state instead of an honest ``None``.
# ---------------------------------------------------------------------------

#: The three non-finite doubles a JSON payload can smuggle in.
NON_FINITE: Final = (math.nan, math.inf, -math.inf)


@pytest.mark.parametrize("bad", NON_FINITE, ids=["nan", "inf", "-inf"])
def test_daily_salt_rejects_non_finite_inputs(bad: float) -> None:
    """A non-finite value in any argument position yields None, never nan."""
    assert daily_salt_grams(bad, INLET_DH_SETTING, OUTLET_DH, E_RATED) is None
    assert daily_salt_grams(LITERS_PER_DAY, bad, OUTLET_DH, E_RATED) is None
    assert daily_salt_grams(LITERS_PER_DAY, INLET_DH_SETTING, bad, E_RATED) is None
    assert daily_salt_grams(LITERS_PER_DAY, INLET_DH_SETTING, OUTLET_DH, bad) is None


@pytest.mark.parametrize("bad", NON_FINITE, ids=["nan", "inf", "-inf"])
def test_device_daily_rate_rejects_non_finite_inputs(bad: float) -> None:
    """A non-finite dose or interval yields None, never nan."""
    assert device_daily_salt_grams(bad, AVG_DAYS_BETWEEN_REGENS) is None
    assert device_daily_salt_grams(AVG_SALT_PER_REGEN_LB, bad) is None


@pytest.mark.parametrize("bad", NON_FINITE, ids=["nan", "inf", "-inf"])
def test_cross_check_rejects_non_finite_inputs(bad: float) -> None:
    """A non-finite countdown or rate yields None, never nan."""
    assert cross_check_days(bad, DEVICE_DAILY_SALT_G, DAILY_SALT_G_SETTING) is None
    assert cross_check_days(DEVICE_ESTIMATE_DAYS, bad, DAILY_SALT_G_SETTING) is None
    assert cross_check_days(DEVICE_ESTIMATE_DAYS, DEVICE_DAILY_SALT_G, bad) is None


def test_negative_outlet_hardness_is_rejected() -> None:
    """A negative outlet hardness would inflate the estimate; it yields None."""
    assert daily_salt_grams(LITERS_PER_DAY, INLET_DH_SETTING, -1.0, E_RATED) is None


@pytest.mark.parametrize("bad", NON_FINITE, ids=["nan", "inf", "-inf"])
def test_non_finite_rated_efficiency_falls_through_to_the_totals(bad: float) -> None:
    """The stoichiometric guard already stops non-finite rated figures.

    ``0 < nan <= ceiling`` and ``0 < inf <= ceiling`` are both False, so the
    rated source is rejected and the lifetime-totals ratio takes over; with no
    totals either, the result is an honest None.
    """
    resolved = efficiency_mol_per_kg_with_source(bad, TOTAL_ROCK_LB, TOTAL_SALT_LB)
    assert resolved is not None
    value, source = resolved
    assert source == EFFICIENCY_SOURCE_TOTALS
    assert value == pytest.approx(E_TOTALS, abs=E_TOLERANCE)
    assert efficiency_mol_per_kg_with_source(bad, None, None) is None
