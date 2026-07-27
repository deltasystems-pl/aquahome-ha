"""Salt-consumption chemistry for AquaHome water softeners.

Pure, dependency-free math translating the device's own counters into a daily
salt-consumption estimate and a days-until-empty cross-check. The formulas and
constants are the triple-verified results of the salt-usage research (DVGW /
DIN EN 14743 / NSF-44 anchors); every coefficient is derived from exact unit
definitions, and the efficiency is **self-calibrated from the device's own
counters** — no literature or brand figure is ever substituted, because rated
efficiencies are laboratory maxima that operational devices do not reach.

Chain (dimensionally: mol/day ÷ mol/kg = kg/day):

    daily_salt_g = liters_per_day x (dH_in - dH_out) x DH_MMOL_PER_L / E

where E is the device's operational salt efficiency in mol of hardness (as
CaCO₃) exchanged per kg of regenerant salt. The device reports E directly as
``salt_effic_grains_per_lb``; on the reference device that property is exactly
``7000 x total_rock_removed_lbs / total_salt_use_lbs``, so the lifetime-totals
ratio is the equivalent fallback. Both are denominated in the *actual* salt
mass consumed, so the NaCl/KCl choice is already reflected and needs no
correction here.

Blending note: residential softeners fully soften the resin stream and blend
hard bypass water back in, so capacity/salt accounting uses the TOTAL metered
volume x (dH_in - dH_out) — mass balance makes this exact (DVGW twin Nr. 07).
No known iQua model exposes a blend setting, so callers pass ``dh_out=0.0``.

Every function is None-safe: a missing or physically impossible input yields
``None``, never an exception and never a fabricated value.
"""

from __future__ import annotations

from typing import Final

#: mg/L of CaCO₃ per grain-per-US-gallon: 1 grain = 0.06479891 g over one US
#: gallon (3.785411784 L), both exact definitions.
_PPM_PER_GPG: Final = 0.06479891 * 1000.0 / 3.785411784

#: mg/L of CaCO₃ per German degree of hardness (°dH = 10 mg/L CaO; CaO
#: 56.0774 g/mol → CaCO₃ 100.0869 g/mol).
_PPM_PER_DH: Final = 17.848

#: °dH per grain-per-US-gallon (≈ 0.959): converts the device's gpg-denominated
#: hardness settings to German degrees.
GPG_TO_DH: Final = _PPM_PER_GPG / _PPM_PER_DH

#: mmol/L of hardness (as CaCO₃) per °dH (≈ 0.17832).
DH_MMOL_PER_L: Final = _PPM_PER_DH / 100.0869

#: Salt efficiency conversion: (mol CaCO₃ per kg salt) per (grain per lb) —
#: 0.06479891 g/grain ÷ 100.0869 g/mol ÷ 0.45359237 kg/lb (≈ 0.0014273).
MOL_PER_KG_PER_GRAIN_PER_LB: Final = 0.06479891 / 100.0869 / 0.45359237

#: Grams per avoirdupois pound (exact).
GRAMS_PER_POUND: Final = 453.59237

#: Liters per US gallon (exact).
LITERS_PER_GALLON: Final = 3.785411784

#: Grains of capacity per pound of salt in the device's totals identity
#: (7000 grains = 1 lb exactly, since 7000 x 0.06479891 g = 453.59237 g).
_GRAINS_PER_POUND: Final = 7000.0

#: Physics ceiling on salt efficiency: the stoichiometric minimum is ~116.9 g
#: of NaCl per mol of hardness (2 Na⁺ per Ca²⁺), i.e. ~8.56 mol/kg. A reported
#: efficiency above this would beat stoichiometry — corrupt data, not a better
#: softener — so such values are rejected rather than fed into estimates.
STOICHIOMETRIC_MAX_MOL_PER_KG: Final = 8.56


#: Efficiency came from the device's own ``salt_effic_grains_per_lb`` figure.
EFFICIENCY_SOURCE_RATED: Final = "rated"
#: Efficiency came from the lifetime rock-removed ÷ salt-used totals ratio.
EFFICIENCY_SOURCE_TOTALS: Final = "totals"


def _valid_efficiency(candidate: float) -> bool:
    """Return whether an efficiency value is physically possible."""
    return 0.0 < candidate <= STOICHIOMETRIC_MAX_MOL_PER_KG


def efficiency_mol_per_kg_with_source(
    rated_grains_per_lb: float | None,
    total_rock_lb: float | None,
    total_salt_lb: float | None,
) -> tuple[float, str] | None:
    """Return the operational salt efficiency and which source produced it.

    Primary source is the device's own ``salt_effic_grains_per_lb`` figure
    (:data:`EFFICIENCY_SOURCE_RATED`); when it is absent — or fails the
    stoichiometric plausibility guard — the lifetime-totals ratio (rock removed
    ÷ salt used, :data:`EFFICIENCY_SOURCE_TOTALS`) is tried instead. ``None``
    when neither source yields a physically possible value.
    """
    if rated_grains_per_lb is not None:
        candidate = rated_grains_per_lb * MOL_PER_KG_PER_GRAIN_PER_LB
        if _valid_efficiency(candidate):
            return candidate, EFFICIENCY_SOURCE_RATED
    if total_rock_lb is not None and total_salt_lb is not None and total_salt_lb > 0:
        candidate = (
            total_rock_lb
            / total_salt_lb
            * _GRAINS_PER_POUND
            * MOL_PER_KG_PER_GRAIN_PER_LB
        )
        if _valid_efficiency(candidate):
            return candidate, EFFICIENCY_SOURCE_TOTALS
    return None


def efficiency_mol_per_kg(
    rated_grains_per_lb: float | None,
    total_rock_lb: float | None,
    total_salt_lb: float | None,
) -> float | None:
    """Return the device's operational salt efficiency in mol CaCO₃ per kg salt.

    Value-only convenience wrapper around
    :func:`efficiency_mol_per_kg_with_source`.
    """
    resolved = efficiency_mol_per_kg_with_source(
        rated_grains_per_lb, total_rock_lb, total_salt_lb
    )
    return resolved[0] if resolved is not None else None


def daily_salt_grams(
    liters_per_day: float | None,
    dh_in: float | None,
    dh_out: float,
    e_mol_per_kg: float | None,
) -> float | None:
    """Return the estimated daily salt consumption in grams.

    ``liters_per_day x (dh_in - dh_out) x DH_MMOL_PER_L`` is the daily hardness
    load in mmol; divided by the efficiency (mol/kg) that is grams of salt.
    ``None`` when an input is missing, the volume is negative, the hardness
    differential is not positive, or the efficiency is not positive.
    """
    if liters_per_day is None or dh_in is None or e_mol_per_kg is None:
        return None
    if liters_per_day < 0 or dh_in <= dh_out or e_mol_per_kg <= 0:
        return None
    return liters_per_day * (dh_in - dh_out) * DH_MMOL_PER_L / e_mol_per_kg


def device_daily_salt_grams(
    avg_salt_per_regen_lb: float | None,
    avg_days_between_regens: float | None,
) -> float | None:
    """Return the device-observed daily salt rate in grams.

    The softener's own long-run averages — salt dose per regeneration over the
    average regeneration interval — form the independent baseline the chemistry
    estimate is cross-checked against. ``None`` unless both are present and
    positive.
    """
    if avg_salt_per_regen_lb is None or avg_days_between_regens is None:
        return None
    if avg_salt_per_regen_lb <= 0 or avg_days_between_regens <= 0:
        return None
    return avg_salt_per_regen_lb * GRAMS_PER_POUND / avg_days_between_regens


def cross_check_days(
    device_estimate_days: float | None,
    device_daily_g: float | None,
    chemistry_daily_g: float | None,
) -> float | None:
    """Return the chemistry-timed days-until-empty cross-check.

    The device's countdown implies a remaining salt mass of
    ``device_daily_g x device_estimate_days``; re-timing that mass at the
    chemistry rate gives an independent estimate that reacts to *current* water
    usage and hardness settings faster than the device's long-run model. This
    is deliberately anchored to the device's own countdown (the PRIMARY
    signal) — it is a cross-check, not a replacement. ``None`` unless the
    countdown is non-negative and both rates are positive.
    """
    if (
        device_estimate_days is None
        or device_daily_g is None
        or chemistry_daily_g is None
    ):
        return None
    if device_estimate_days < 0 or device_daily_g <= 0 or chemistry_daily_g <= 0:
        return None
    return device_estimate_days * device_daily_g / chemistry_daily_g
