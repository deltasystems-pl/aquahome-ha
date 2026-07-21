"""Tests for the rule-driven settings-number platform.

No number setting exists on the dev device (its settings document is 17 selects
plus a text nickname), so the whole platform is exercised against *synthetic*
number settings appended to the real captured document via the Phase-4
``with_extra_setting`` / ``make_number_setting`` builders. Every test forwards
only the number platform, so each assertion runs the real settings-coordinator
first-refresh and dynamic-adder path.

The focus is the precision-scaling contract: the cloud stores a number as a
precision-expanded integer (``12.5`` at ``precision=1`` arrives as ``125``, with
its bounds expanded the same way), so the entity divides for display and
multiplies back on write. Writes go out as a ``PATCH {"settings": {name: int}}``
and reconcile from the document the server echoes back — never a second GET.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest
from homeassistant.components.number import NumberEntity
from homeassistant.const import Platform
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES

from custom_components.aquahome.const import DOMAIN
from tests.conftest import (
    add_device_routes,
    load_fixture,
    make_number_setting,
    make_switch_setting,
    patch_settings_route,
    setup_integration,
    with_extra_setting,
)

if TYPE_CHECKING:
    from aioresponses import aioresponses
    from aioresponses.core import RequestCall
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

#: Slug derived from the fixture serial ``7384243-20203-1120`` (see other suites).
SLUG = "7384243_20203_1120"
#: Default synthetic number setting name (``make_number_setting``'s default).
NUMBER_NAME = "brine_dose"


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def _only_number() -> Any:
    """Return the platform-isolation patcher forwarding only the number platform."""
    return patch("custom_components.aquahome.PLATFORMS", [Platform.NUMBER])


def _doc_with(*settings: dict[str, Any]) -> dict[str, Any]:
    """Return a fresh settings document with the given settings appended."""
    doc = load_fixture("settings.json")
    for setting in settings:
        doc = with_extra_setting(doc, setting)
    return doc


def _number_entity_id(hass: HomeAssistant, name: str) -> str | None:
    """Resolve the number entity id for a setting name via its unique id."""
    registry = er.async_get(hass)
    return registry.async_get_entity_id("number", DOMAIN, f"{SLUG}_setting_{name}")


def _number(hass: HomeAssistant, name: str) -> NumberEntity:
    """Return the live number entity object for a setting name."""
    entity_id = _number_entity_id(hass, name)
    assert entity_id is not None, f"number setting {name} was not registered"
    component = hass.data[DATA_INSTANCES]["number"]
    entity = component.get_entity(entity_id)
    assert entity is not None, f"number setting {name} has no live entity"
    return cast(NumberEntity, entity)


def _settings_requests(mock: aioresponses, method: str) -> list[RequestCall]:
    """Return every recorded request to the ``/settings`` path for one method."""
    return [
        call
        for (call_method, url), calls in mock.requests.items()
        if call_method == method and url.path.endswith("/settings")
        for call in calls
    ]


# ---------------------------------------------------------------------------
# Precision scaling — value and bounds
# ---------------------------------------------------------------------------


async def test_number_scaled_value_and_bounds(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A precision-1 setting divides its value and bounds by ten for display."""
    add_device_routes(mock_api, settings=_doc_with(make_number_setting()))
    with _only_number():
        await setup_integration(hass, mock_config_entry)

    entity = _number(hass, NUMBER_NAME)
    # raw current_value 125 / min 50 / max 250 / step 5, all ÷ 10**precision.
    assert entity.native_value == 12.5
    assert entity.native_min_value == 5.0
    assert entity.native_max_value == 25.0
    assert entity.native_step == 0.5


# ---------------------------------------------------------------------------
# Write path — precision-expanded PATCH body and echo reconcile
# ---------------------------------------------------------------------------


async def test_number_set_value_writes_expanded_and_reconciles(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Setting 13.0 PATCHes the expanded integer and reconciles from the echo.

    The GET route serves ``current_value`` 125 (native 12.5); the PATCH echoes a
    document carrying 130, so a resulting native value of 13.0 can only have come
    from the PATCH response — and no second settings GET is issued.
    """
    add_device_routes(mock_api, settings=_doc_with(make_number_setting()))
    # The server echoes back the freshly written document (current_value 130).
    patch_settings_route(
        mock_api, payload=_doc_with(make_number_setting(current_value=130))
    )
    with _only_number():
        await setup_integration(hass, mock_config_entry)

    entity = _number(hass, NUMBER_NAME)
    assert entity.native_value == 12.5

    await entity.async_set_native_value(13.0)
    await hass.async_block_till_done()

    # 13.0 grains at precision 1 is written as the raw integer 130.
    (patch_call,) = _settings_requests(mock_api, "PATCH")
    assert patch_call.kwargs["json"] == {"settings": {NUMBER_NAME: 130}}

    # Reconciled purely from the PATCH echo: no follow-up GET, native value 13.0.
    assert len(_settings_requests(mock_api, "GET")) == 1
    assert entity.native_value == 13.0


# ---------------------------------------------------------------------------
# Precision 0 / absent — no scaling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "setting",
    [
        make_number_setting(precision=0),
        {
            # ``precision`` key absent entirely ⇒ factor 1 (no scaling).
            "component_type": "number",
            "name": NUMBER_NAME,
            "label": "Brine Dose",
            "current_value": 125,
            "rules": {"number_rules": {"min": 50, "max": 250, "step": 5}},
        },
    ],
    ids=["precision-0", "precision-absent"],
)
async def test_number_no_scaling_when_precision_zero_or_absent(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    setting: dict[str, Any],
) -> None:
    """Precision 0 (or an absent precision) leaves value and bounds unscaled."""
    add_device_routes(mock_api, settings=_doc_with(setting))
    with _only_number():
        await setup_integration(hass, mock_config_entry)

    entity = _number(hass, NUMBER_NAME)
    assert entity.native_value == 125.0
    assert entity.native_min_value == 50.0
    assert entity.native_max_value == 250.0
    assert entity.native_step == 5.0


# ---------------------------------------------------------------------------
# Out-of-range writes — validation before any I/O
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [2.0, 30.0], ids=["below-min", "above-max"])
async def test_number_out_of_range_raises_and_makes_no_request(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    value: float,
) -> None:
    """A value outside the scaled ``[5, 25]`` range is rejected before any write."""
    add_device_routes(mock_api, settings=_doc_with(make_number_setting()))
    # Registered but must stay unconsumed: validation happens before any I/O.
    patch_settings_route(
        mock_api, payload=_doc_with(make_number_setting(current_value=130))
    )
    with _only_number():
        await setup_integration(hass, mock_config_entry)

    entity = _number(hass, NUMBER_NAME)
    with pytest.raises(ServiceValidationError) as err:
        await entity.async_set_native_value(value)

    assert err.value.translation_key == "number_out_of_range"
    assert err.value.translation_placeholders == {"min": "5", "max": "25"}
    # No PATCH ever left the entity.
    assert _settings_requests(mock_api, "PATCH") == []


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"]
)
async def test_number_non_finite_value_rejected_as_out_of_range(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
    value: float,
) -> None:
    """A non-finite value is rejected as a translated validation error.

    Home Assistant's service layer coerces YAML ``.nan``/``.inf`` to real floats
    and its own range guard is ``False`` for NaN, so without the entity's
    finiteness check ``round`` would escape as a raw, untranslated
    ``ValueError`` (adversarial-review finding, 2026-07-21).
    """
    add_device_routes(mock_api, settings=_doc_with(make_number_setting()))
    with _only_number():
        await setup_integration(hass, mock_config_entry)

    entity = _number(hass, NUMBER_NAME)
    with pytest.raises(ServiceValidationError) as err:
        await entity.async_set_native_value(value)

    assert err.value.translation_key == "number_out_of_range"
    assert _settings_requests(mock_api, "PATCH") == []


# ---------------------------------------------------------------------------
# Absent bounds — native-space fallbacks
# ---------------------------------------------------------------------------


async def test_number_absent_bounds_fall_back_to_defaults(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Missing min/max/step fall back to 0/100/1 in native (display) space.

    The fallbacks are native, not raw, so precision does not scale them: with a
    ``number_rules`` block carrying only ``precision`` the value still divides by
    ten while the bounds stay at their plain 0/100/1 defaults.
    """
    setting: dict[str, Any] = {
        "component_type": "number",
        "name": NUMBER_NAME,
        "label": "Brine Dose",
        "current_value": 125,
        "rules": {"number_rules": {"precision": 1}},
    }
    add_device_routes(mock_api, settings=_doc_with(setting))
    with _only_number():
        await setup_integration(hass, mock_config_entry)

    entity = _number(hass, NUMBER_NAME)
    assert entity.native_value == 12.5
    assert entity.native_min_value == 0.0
    assert entity.native_max_value == 100.0
    assert entity.native_step == 1.0


# ---------------------------------------------------------------------------
# Classification — only number_rules settings become number entities
# ---------------------------------------------------------------------------


async def test_select_and_bool_settings_create_no_number_entity(
    hass: HomeAssistant,
    mock_api: aioresponses,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A select or boolean setting is not a number; only ``number_rules`` is.

    All three share one classification predicate: a ``select_rules`` block with an
    option wins first, then ``number_rules``, then a JSON-boolean current value.
    Here a select and a switch setting sit alongside a real number setting; only
    the last produces a number entity.
    """
    select_setting: dict[str, Any] = {
        "component_type": "select",
        "name": "drive_mode",
        "label": "Drive Mode",
        "current_value": "eco",
        "rules": {
            "select_rules": {
                "options": [
                    {"value": "eco", "label": "Eco"},
                    {"value": "max", "label": "Max"},
                ]
            }
        },
    }
    doc = _doc_with(
        make_number_setting(),
        select_setting,
        make_switch_setting(name="night_mode"),
    )
    add_device_routes(mock_api, settings=doc)
    with _only_number():
        await setup_integration(hass, mock_config_entry)

    # Only the number_rules setting is claimed by the number platform.
    assert _number_entity_id(hass, NUMBER_NAME) is not None
    assert _number_entity_id(hass, "drive_mode") is None
    assert _number_entity_id(hass, "night_mode") is None
