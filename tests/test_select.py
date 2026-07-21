"""Tests for the settings-driven select platform.

The select platform materialises one :class:`~homeassistant.components.select.SelectEntity`
per *visible* select setting in the iQua ``GET /devices/{id}/settings`` document,
built at runtime on the settings coordinator through
:func:`~custom_components.aquahome.dynamic.async_setup_dynamic_entities`. These
tests run end-to-end against the real 18-item ``settings.json`` fixture with only
the select platform forwarded, so every assertion exercises the true
coordinator-first-refresh, registry, and write-reconcile paths.

Coverage: the exact 15-select set created on the fixture (17 select settings minus
the two ``aux_control_type``-conditional ``chem_feed_*`` selects, plus the text
``nickname`` contributing nothing); the six display-preference selects being
registry-disabled while the rest stay enabled; the CONFIG category and
server-label naming; ``inlet_hardness`` option/label resolution; the PATCH write
body and reconcile-without-extra-GET round trip; the defensive ``invalid_option``
raise; drifted-value and duplicate-label disambiguation; and the conditional
appear/disappear dynamics driven by a settings refresh.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from aioresponses.core import RequestCall
from homeassistant.components.select import (
    ATTR_OPTIONS,
    DATA_COMPONENT,
    SERVICE_SELECT_OPTION,
    SelectEntity,
)
from homeassistant.components.select import (
    DOMAIN as SELECT_DOMAIN,
)
from homeassistant.components.select.const import ATTR_OPTION
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_UNAVAILABLE,
    EntityCategory,
    Platform,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import RegistryEntryDisabler
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.aquahome.const import (
    DISPLAY_PREFERENCE_SETTINGS,
    DOMAIN,
    SETTINGS_UPDATE_INTERVAL,
)
from tests.conftest import (
    add_activity_routes,
    add_settings_routes,
    device_url,
    devices_url,
    load_fixture,
    settings_url,
    setup_integration,
    with_extra_setting,
    with_setting_value,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aioresponses import aioresponses
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

#: Slug derived from the captured device serial ``7384243-20203-1120``.
SLUG = "7384243_20203_1120"

#: Fixed instant the conditional-dynamics test freezes to.
FROZEN_INSTANT = "2026-07-21T12:00:00+00:00"

#: Visible select settings created on the dev fixture (17 selects less 2 hidden).
EXPECTED_SELECT_COUNT = 15

#: The two conditionally-hidden selects (``aux_control_type`` is 0 on the fixture).
HIDDEN_SELECTS = ("chem_feed_volume", "chem_feed_seconds")

#: A handful of always-visible, non-display-preference selects (registry-enabled).
ENABLED_SELECTS = (
    "inlet_hardness",
    "salt_type",
    "regeneration_time",
    "aux_control_type",
)

#: ``inlet_hardness`` current value ``"25.7"`` localises to this option label.
INLET_CURRENT_LABEL = "440 PPM (24 dH/44 fH)"
#: A different offered ``inlet_hardness`` option: raw value ``"26.3"``.
INLET_TARGET_LABEL = "450 PPM (25 dH/45 fH)"
INLET_TARGET_VALUE = "26.3"


@pytest.fixture(autouse=True)
def _only_select_platform() -> Iterator[None]:
    """Forward only the select platform for the duration of a test."""
    with patch("custom_components.aquahome.PLATFORMS", [Platform.SELECT]):
        yield


# ---------------------------------------------------------------------------
# Route / lookup helpers
# ---------------------------------------------------------------------------


def _register_base_routes(mock: aioresponses) -> None:
    """Register the device list/detail and activity routes (no settings route)."""
    mock.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock.get(device_url(), payload=load_fixture("device-detail.json"), repeat=True)
    add_activity_routes(mock)


def _uid(name: str) -> str:
    """Return the select entity's unique id for a setting ``name``."""
    return f"{SLUG}_setting_{name}"


def _entity_id(registry: er.EntityRegistry, name: str) -> str | None:
    """Resolve a select entity id from its setting name, or ``None``."""
    return registry.async_get_entity_id(SELECT_DOMAIN, DOMAIN, _uid(name))


def _calls_for(mock: aioresponses, method: str, path_suffix: str) -> list[RequestCall]:
    """Return every recorded request whose method and URL path suffix match."""
    return [
        call
        for (call_method, url), calls in mock.requests.items()
        if call_method == method and url.path.endswith(path_suffix)
        for call in calls
    ]


def _settings_get_count(mock: aioresponses) -> int:
    """Return how many GET requests hit the settings endpoint."""
    return len(_calls_for(mock, "GET", "/settings"))


def _labels(setting: dict[str, Any]) -> list[str]:
    """Return the option labels of a raw select-setting payload, in order."""
    return [option["label"] for option in setting["rules"]["select_rules"]["options"]]


def _find_setting(doc: dict[str, Any], name: str) -> dict[str, Any]:
    """Return the raw setting payload named ``name`` from a settings document."""
    return next(item for item in doc["settings"] if item["name"] == name)


# ---------------------------------------------------------------------------
# Boot: exactly fifteen selects
# ---------------------------------------------------------------------------


async def test_boot_creates_exactly_fifteen_selects(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """The fixture yields 15 selects; ``nickname`` and hidden ``chem_feed_*`` none.

    Seventeen of the eighteen settings are selects; the two ``chem_feed_*`` selects
    are hidden because ``aux_control_type`` is 0, and the lone ``text`` setting
    (``nickname``) is out of scope, so exactly 15 select entities register.
    """
    _register_base_routes(mock_api)
    add_settings_routes(mock_api)

    assert await setup_integration(hass, mock_config_entry)

    entries = [
        entry
        for entry in er.async_entries_for_config_entry(
            entity_registry, mock_config_entry.entry_id
        )
        if entry.domain == SELECT_DOMAIN
    ]
    assert len(entries) == EXPECTED_SELECT_COUNT
    # The text setting contributes no entity, and the conditional selects are hidden.
    assert _entity_id(entity_registry, "nickname") is None
    for name in HIDDEN_SELECTS:
        assert _entity_id(entity_registry, name) is None


# ---------------------------------------------------------------------------
# Registry defaults: display preferences disabled, category, naming
# ---------------------------------------------------------------------------


async def test_display_preference_selects_disabled_others_enabled(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """The six display-preference selects are registry-disabled; the rest enabled.

    All six live in :data:`DISPLAY_PREFERENCE_SETTINGS` and are selects on the
    fixture, so each registers disabled-by-integration; ``inlet_hardness`` and the
    other functional selects register enabled.
    """
    _register_base_routes(mock_api)
    add_settings_routes(mock_api)

    assert await setup_integration(hass, mock_config_entry)

    for name in DISPLAY_PREFERENCE_SETTINGS:
        entity_id = _entity_id(entity_registry, name)
        assert entity_id is not None, name
        entry = entity_registry.async_get(entity_id)
        assert entry is not None
        assert entry.disabled_by is RegistryEntryDisabler.INTEGRATION, name

    for name in ENABLED_SELECTS:
        entity_id = _entity_id(entity_registry, name)
        assert entity_id is not None, name
        entry = entity_registry.async_get(entity_id)
        assert entry is not None
        assert entry.disabled_by is None, name


async def test_all_selects_are_config_and_named_from_server_labels(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """Every select is a CONFIG entity named from the server-localized label.

    The registry ``original_name`` is the setting's ``label`` verbatim (the base
    entity sets ``_attr_name`` to it), and every settings entity carries the CONFIG
    category.
    """
    fixture = load_fixture("settings.json")
    _register_base_routes(mock_api)
    add_settings_routes(mock_api, settings=fixture)

    assert await setup_integration(hass, mock_config_entry)

    for name in (*ENABLED_SELECTS, *DISPLAY_PREFERENCE_SETTINGS):
        entity_id = _entity_id(entity_registry, name)
        assert entity_id is not None, name
        entry = entity_registry.async_get(entity_id)
        assert entry is not None
        assert entry.entity_category is EntityCategory.CONFIG, name
        assert entry.original_name == _find_setting(fixture, name)["label"], name


# ---------------------------------------------------------------------------
# Option / current-option resolution
# ---------------------------------------------------------------------------


async def test_inlet_hardness_options_are_labels_and_current_resolves(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """``inlet_hardness`` exposes labels as options and resolves ``25.7`` to a label.

    The offered options are the raw ``label`` strings in document order (raw values
    like ``"25.7"`` never appear as options), and ``current_option`` is the label
    whose option value equals the current value.
    """
    fixture = load_fixture("settings.json")
    _register_base_routes(mock_api)
    add_settings_routes(mock_api, settings=fixture)

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry, "inlet_hardness")
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    expected_labels = _labels(_find_setting(fixture, "inlet_hardness"))
    assert state.attributes[ATTR_OPTIONS] == expected_labels
    assert "25.7" not in state.attributes[ATTR_OPTIONS]
    assert state.state == INLET_CURRENT_LABEL


# ---------------------------------------------------------------------------
# Write path: PATCH body + reconcile without an extra GET
# ---------------------------------------------------------------------------


async def test_select_option_writes_patch_and_reconciles_without_extra_get(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """Selecting a label PATCHes its raw value and reconciles from the echoed doc.

    The write sends ``{"settings": {"inlet_hardness": "26.3"}}`` and the entity
    adopts the new label from the document the PATCH returns, with no follow-up
    settings GET issued.
    """
    fixture = load_fixture("settings.json")
    post_write = with_setting_value(fixture, "inlet_hardness", INLET_TARGET_VALUE)
    _register_base_routes(mock_api)
    add_settings_routes(mock_api, settings=fixture)
    mock_api.patch(settings_url(), payload=post_write)

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry, "inlet_hardness")
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == INLET_CURRENT_LABEL
    gets_before = _settings_get_count(mock_api)

    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: entity_id, ATTR_OPTION: INLET_TARGET_LABEL},
        blocking=True,
    )
    await hass.async_block_till_done()

    patch_bodies = [
        call.kwargs["json"] for call in _calls_for(mock_api, "PATCH", "/settings")
    ]
    assert patch_bodies == [{"settings": {"inlet_hardness": INLET_TARGET_VALUE}}]
    # State reconciled from the PATCH-returned document.
    reconciled = hass.states.get(entity_id)
    assert reconciled is not None
    assert reconciled.state == INLET_TARGET_LABEL
    # No extra settings GET was issued to observe the new value.
    assert _settings_get_count(mock_api) == gets_before


# ---------------------------------------------------------------------------
# Defensive invalid-option raise (entity method, below the service guard)
# ---------------------------------------------------------------------------


async def test_unknown_label_raises_invalid_option(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """An unknown label raises ``ServiceValidationError(invalid_option)``.

    Home Assistant's ``select_option`` service guards the option against
    :attr:`SelectEntity.options` first (raising its own ``not_valid_option``), so
    the entity method is invoked directly to exercise our defensive check.
    """
    _register_base_routes(mock_api)
    add_settings_routes(mock_api)

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry, "inlet_hardness")
    assert entity_id is not None
    entity = hass.data[DATA_COMPONENT].get_entity(entity_id)
    assert isinstance(entity, SelectEntity)

    with pytest.raises(ServiceValidationError) as excinfo:
        await entity.async_select_option("Not An Offered Option")
    assert excinfo.value.translation_key == "invalid_option"


# ---------------------------------------------------------------------------
# Drifted current value surfaced as an extra option
# ---------------------------------------------------------------------------


async def test_drifted_current_value_becomes_extra_option(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """A current value matching no option is appended and becomes the selection.

    The server reports ``inlet_hardness`` as ``"999.9"`` — offered by none of the
    options — so its raw string is appended as an extra option and reported as
    ``current_option`` instead of being dropped as invalid.
    """
    fixture = load_fixture("settings.json")
    drifted = with_setting_value(fixture, "inlet_hardness", "999.9")
    _register_base_routes(mock_api)
    add_settings_routes(mock_api, settings=drifted)

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry, "inlet_hardness")
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    options = state.attributes[ATTR_OPTIONS]
    base_count = len(_labels(_find_setting(fixture, "inlet_hardness")))
    assert len(options) == base_count + 1
    assert options[-1] == "999.9"
    assert state.state == "999.9"


# ---------------------------------------------------------------------------
# Duplicate labels disambiguated as "label (value)"
# ---------------------------------------------------------------------------


async def test_duplicate_labels_are_disambiguated(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """Two options sharing a label are both rendered as ``"label (value)"``.

    A synthetic select whose first two options both localise to ``"Same"`` has
    each disambiguated by its raw value, while the unique label is left untouched;
    the current value ``"b"`` resolves to its disambiguated form.
    """
    dup_setting: dict[str, Any] = {
        "component_type": "select",
        "name": "dup_test",
        "label": "Duplicate Test",
        "rules": {
            "select_rules": {
                "options": [
                    {"value": "a", "label": "Same"},
                    {"value": "b", "label": "Same"},
                    {"value": "c", "label": "Unique"},
                ]
            }
        },
        "current_value": "b",
    }
    doc = with_extra_setting(load_fixture("settings.json"), dup_setting)
    _register_base_routes(mock_api)
    add_settings_routes(mock_api, settings=doc)

    assert await setup_integration(hass, mock_config_entry)

    entity_id = _entity_id(entity_registry, "dup_test")
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes[ATTR_OPTIONS] == ["Same (a)", "Same (b)", "Unique"]
    assert state.state == "Same (b)"


# ---------------------------------------------------------------------------
# Conditional dynamics: appear on refresh, then go unavailable (not deleted)
# ---------------------------------------------------------------------------


async def test_conditional_selects_appear_then_go_unavailable(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """``chem_feed_*`` materialise when ``aux_control_type`` becomes 4, then hide.

    The settings adder runs debounce-1, so a single refresh whose document sets
    ``aux_control_type`` to ``"4"`` makes the two conditional selects appear. A
    later refresh returning them to hidden leaves the entities registered but
    unavailable — removed capabilities are never deleted.
    """
    freezer.move_to(FROZEN_INSTANT)
    base = load_fixture("settings.json")
    visible = with_setting_value(base, "aux_control_type", "4")
    _register_base_routes(mock_api)
    mock_api.get(settings_url(), payload=base)  # setup: aux_control_type 0 → hidden
    mock_api.get(settings_url(), payload=visible)  # refresh 1: aux 4 → visible
    mock_api.get(settings_url(), payload=base, repeat=True)  # refresh 2+: hidden again

    assert await setup_integration(hass, mock_config_entry)

    # Hidden at boot: not created.
    for name in HIDDEN_SELECTS:
        assert _entity_id(entity_registry, name) is None

    # Refresh 1 flips aux_control_type to 4 — the selects appear (debounce 1).
    freezer.tick(SETTINGS_UPDATE_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    vol_id = _entity_id(entity_registry, "chem_feed_volume")
    sec_id = _entity_id(entity_registry, "chem_feed_seconds")
    assert vol_id is not None
    assert sec_id is not None
    vol_state = hass.states.get(vol_id)
    sec_state = hass.states.get(sec_id)
    assert vol_state is not None
    assert sec_state is not None
    assert vol_state.state == "4 liters"
    assert sec_state.state == "1 second"

    # Refresh 2 hides them again — registered but unavailable, never deleted.
    freezer.tick(SETTINGS_UPDATE_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert _entity_id(entity_registry, "chem_feed_volume") == vol_id
    assert _entity_id(entity_registry, "chem_feed_seconds") == sec_id
    hidden_vol = hass.states.get(vol_id)
    hidden_sec = hass.states.get(sec_id)
    assert hidden_vol is not None
    assert hidden_sec is not None
    assert hidden_vol.state == STATE_UNAVAILABLE
    assert hidden_sec.state == STATE_UNAVAILABLE
