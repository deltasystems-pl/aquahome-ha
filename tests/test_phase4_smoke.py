"""Cross-platform Phase-4 smoke test — the fully-loaded control surface.

The per-platform Phase-4 suites each isolate one platform behind a patched
``PLATFORMS``; this file, like :mod:`tests.test_phase3_smoke`, boots the
integration exactly as Home Assistant would — every platform forwarded, real
captured fixtures behind ``aioresponses`` — but against a *synthetic* device that
carries every optional capability at once. The dev cohort only advertises
``["regeneration"]``, so a water-shutoff valve, leak detectors, a number setting,
and a boolean setting cannot be captured live; they are layered onto the real
fixtures with the Phase-4 builders (``with_wsov`` / ``with_leak_detectors`` /
``with_extra_setting`` + ``make_number_setting`` / ``make_switch_setting``).

Booting that device pins the full per-domain entity inventory in one readable
assert dict — a dropped platform, a mis-gated capability, or a churned
description count is caught by a single comparison — and then drives one command
of each kind end-to-end through the real coordinators:

* a ``regenerate_now`` button press emits the exact ``{function, action}`` PUT;
* a number-setting write emits the precision-expanded ``PATCH`` body and
  reconciles its native value straight from the echoed document (no extra GET);
* the water-shutoff valve closes optimistically, then reports ``closed`` once a
  later fast poll serves a ``status: close`` device view.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from homeassistant.components.button.const import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button.const import SERVICE_PRESS
from homeassistant.components.number import NumberEntity
from homeassistant.components.number.const import ATTR_VALUE, SERVICE_SET_VALUE
from homeassistant.components.number.const import DOMAIN as NUMBER_DOMAIN
from homeassistant.components.valve.const import DOMAIN as VALVE_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_CLOSE_VALVE,
    STATE_CLOSED,
    STATE_CLOSING,
    STATE_OPEN,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from yarl import URL

from custom_components.aquahome.const import DOMAIN, UPDATE_INTERVAL
from tests.conftest import (
    alerts_url,
    command_url,
    device_url,
    devices_url,
    load_fixture,
    make_number_setting,
    make_switch_setting,
    patch_settings_route,
    regen_events_url,
    settings_url,
    setup_integration,
    with_extra_setting,
    with_leak_detectors,
    with_wsov,
)

if TYPE_CHECKING:
    from aioresponses import aioresponses
    from aioresponses.core import RequestCall
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

#: Slug derived from the fixture serial ``7384243-20203-1120`` (see other suites).
SLUG = "7384243_20203_1120"

#: Name of the synthetic number setting (``make_number_setting`` default).
NUMBER_NAME = "brine_dose"

# ---------------------------------------------------------------------------
# Expected per-domain entity inventory of the fully-loaded device.
#
# Derived honestly from the dev-fixture baselines pinned by test_phase3_smoke
# plus the deltas the synthetic builders introduce:
#   sensor        30 dev + 2 leak (temperature, signal strength)
#   binary_sensor 11 dev + wsov_closed + 4 leak (leak/battery/tamper/connectivity)
#                 + water_to_drain_alert. The last is not a leak binary: the
#                 leak_detector feature makes _water_to_drain_exists true, so the
#                 water-to-drain moisture binary — absent on the dev fixture (no
#                 feature, no status field) — is unlocked once leak detectors are
#                 paired. Total 17.
#   button         6 dev + reset_wsov_error_code (unlocked by the wsov feature)
#   select        15 dev (17 selects less 2 conditionally hidden), unchanged
#   number         1 (make_number_setting)
#   switch         leak-detector scan + 1 boolean setting switch
#   valve          1 water-shutoff valve
#   event          1 alert event, unchanged
# ---------------------------------------------------------------------------
# The Phase-3 sensor set of 30 plus the five Phase-6 salt sensors (daily
# usage, days remaining, depletion timestamp, per-regeneration, efficiency)
# plus the two Phase-7 analytics sensors (usage forecast, night flow).
_DEV_SENSORS = 37
# The Phase-2/3 set of 11 plus the three Phase-7 detection binaries
# (leak suspected, usage anomaly, vacation detected).
_DEV_BINARY_SENSORS = 14
_DEV_BUTTONS = 6
_DEV_SELECTS = 15

EXPECTED_ENTITIES: dict[str, int] = {
    "sensor": _DEV_SENSORS + 2,
    "binary_sensor": _DEV_BINARY_SENSORS + 1 + 4 + 1,
    "button": _DEV_BUTTONS + 1,
    "select": _DEV_SELECTS,
    "number": 1,
    "switch": 2,
    "valve": 1,
    "event": 1,
}


# ---------------------------------------------------------------------------
# Synthetic fixture builders (never mutate the loaded fixtures in place)
# ---------------------------------------------------------------------------


def _detail(*, valve_status: str = "open") -> dict[str, Any]:
    """Return a fully-loaded device-detail payload (valve + leak detectors).

    ``valve_status`` sets the water-shutoff-valve ``status`` enum so the same
    device can be served ``open`` at setup and ``close`` on a later poll.
    """
    base = load_fixture("device-detail.json")
    return with_wsov(with_leak_detectors(base), status=valve_status)


def _settings_doc(*, number_current: int = 125) -> dict[str, Any]:
    """Return the real settings document plus one number and one switch setting."""
    doc = load_fixture("settings.json")
    doc = with_extra_setting(doc, make_number_setting(current_value=number_current))
    return with_extra_setting(doc, make_switch_setting())


# ---------------------------------------------------------------------------
# Registry / state / request helpers
# ---------------------------------------------------------------------------


def _entity_id(registry: er.EntityRegistry, domain: str, key: str) -> str:
    """Resolve an entity id from its platform domain and unique-id suffix."""
    entity_id = registry.async_get_entity_id(domain, DOMAIN, f"{SLUG}_{key}")
    assert entity_id is not None, f"{domain} entity {key} was not registered"
    return entity_id


def _state(
    hass: HomeAssistant, registry: er.EntityRegistry, domain: str, key: str
) -> str:
    """Return the current state string of the entity with unique-id suffix ``key``."""
    state = hass.states.get(_entity_id(registry, domain, key))
    assert state is not None, f"{domain} entity {key} has no state"
    return state.state


def _number_entity(
    hass: HomeAssistant, registry: er.EntityRegistry, name: str
) -> NumberEntity:
    """Return the live number entity object for a setting name."""
    entity_id = _entity_id(registry, NUMBER_DOMAIN, f"setting_{name}")
    component = hass.data[DATA_INSTANCES][NUMBER_DOMAIN]
    entity = component.get_entity(entity_id)
    assert entity is not None, f"number setting {name} has no live entity"
    return cast("NumberEntity", entity)


def _settings_requests(mock: aioresponses, method: str) -> list[RequestCall]:
    """Return every recorded request to the ``/settings`` path for one method."""
    return [
        call
        for (call_method, url), calls in mock.requests.items()
        if call_method == method and url.path.endswith("/settings")
        for call in calls
    ]


def _command_bodies(mock: aioresponses) -> list[dict[str, Any]]:
    """Return the JSON bodies of every recorded ``PUT /command`` in fire order."""
    calls = mock.requests.get(("PUT", URL(command_url())), [])
    return [call.kwargs["json"] for call in calls]


# ---------------------------------------------------------------------------
# The one smoke test — boot everything, then one command of each kind
# ---------------------------------------------------------------------------


async def test_full_control_surface(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Boot the fully-loaded device and drive one command of each Phase-4 kind."""
    freezer.move_to("2026-07-21T12:00:00+00:00")

    # Device detail: ``open`` is served once at setup, then every later fast poll
    # sees the ``close`` view (mirrors test_phase3_smoke's arrive-between-polls
    # idiom). Everything else is repeat-served from the real/synthetic fixtures.
    mock_api.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock_api.get(device_url(), payload=_detail(valve_status="open"))
    mock_api.get(device_url(), payload=_detail(valve_status="close"), repeat=True)
    mock_api.get(
        regen_events_url(),
        payload=load_fixture("regeneration-events.json"),
        repeat=True,
    )
    mock_api.get(alerts_url(), payload=load_fixture("alerts.json"), repeat=True)
    mock_api.get(settings_url(), payload=_settings_doc(), repeat=True)
    # The PATCH echoes the freshly written document (brine_dose current_value 130).
    patch_settings_route(mock_api, payload=_settings_doc(number_current=130))
    mock_api.put(command_url(), payload={"status": "ok"}, repeat=True)

    assert await setup_integration(hass, mock_config_entry)

    # -- Full per-domain inventory of the fully-loaded device ----------------
    entries = er.async_entries_for_config_entry(
        entity_registry, mock_config_entry.entry_id
    )
    by_domain: dict[str, int] = {}
    for entry in entries:
        by_domain[entry.domain] = by_domain.get(entry.domain, 0) + 1
    assert by_domain == EXPECTED_ENTITIES

    # Baseline state before any command is issued.
    assert (
        _state(hass, entity_registry, VALVE_DOMAIN, "water_shutoff_valve") == STATE_OPEN
    )
    number = _number_entity(hass, entity_registry, NUMBER_NAME)
    assert number.native_value == 12.5  # raw current_value 125 at precision 1

    # -- Button press: the exact regenerate command leaves the entity ---------
    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: _entity_id(entity_registry, BUTTON_DOMAIN, "regenerate_now")},
        blocking=True,
    )
    assert _command_bodies(mock_api) == [
        {"function": "regenerate", "action": "regenerate"}
    ]

    # -- Settings write: precision-expanded PATCH + reconcile from the echo ----
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {
            ATTR_ENTITY_ID: _entity_id(
                entity_registry, NUMBER_DOMAIN, f"setting_{NUMBER_NAME}"
            ),
            ATTR_VALUE: 13.0,
        },
        blocking=True,
    )
    (patch_call,) = _settings_requests(mock_api, "PATCH")
    assert patch_call.kwargs["json"] == {"settings": {NUMBER_NAME: 130}}
    # Reconciled purely from the PATCH echo: no follow-up GET, native value 13.0.
    assert len(_settings_requests(mock_api, "GET")) == 1
    assert number.native_value == 13.0

    # -- Valve: optimistic closing, then closed once a poll confirms it --------
    await hass.services.async_call(
        VALVE_DOMAIN,
        SERVICE_CLOSE_VALVE,
        {
            ATTR_ENTITY_ID: _entity_id(
                entity_registry, VALVE_DOMAIN, "water_shutoff_valve"
            )
        },
        blocking=True,
    )
    assert _command_bodies(mock_api)[-1] == {
        "function": "water_shutoff_valve",
        "action": "close",
    }
    # The optimistic motion flag shows immediately, before any poll.
    assert (
        _state(hass, entity_registry, VALVE_DOMAIN, "water_shutoff_valve")
        == STATE_CLOSING
    )

    # The next fast poll serves the ``close`` device view; the valve settles closed.
    freezer.tick(UPDATE_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert (
        _state(hass, entity_registry, VALVE_DOMAIN, "water_shutoff_valve")
        == STATE_CLOSED
    )
