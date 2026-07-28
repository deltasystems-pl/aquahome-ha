"""Tests for the live-mode tier's entities: controls, budgets, and sensors.

Live mode adds three switches per device (the manual ``live_view`` hold plus
the two behaviour flags), the two budget numbers that bound how much of the
cloud's live-session allowance a device may spend, and a diagnostic status
sensor — all views onto the per-device live manager rather than onto a cloud
payload. It also upgrades three fast-coordinator sensors to read the raw
properties a live session streams. This module owns that projection layer: what
the entities are (unique ids, categories, icons, ranges, defaults), what they
render from the manager's published state, what a write persists into
``entry.options``, and how the raw-first sensor value functions resolve.

Nothing here opens a websocket. The integration boots against the captured
fixtures served through ``aioresponses``, which also answers the ticket
endpoint; the ticketed handshake that follows has no server to reach, so a
session a switch requests lands in the manager's failure path. That is
deliberate: the refused session is exactly the behaviour an entity test can
assert — the hold stays on, the status sensor reports the backoff and its
reason, and the daily budget is not spent — while the streaming happy path
belongs to the manager's own suite.

Three determinism rules run through the file. The clock is frozen before setup
and the stored access token re-minted against it, so the failure trail's
timestamps are fixed literals. Fixture payloads are always deep-copied before
mutation — the JSON files are never edited in place. And an autouse fixture
unloads the config entry after every test, because the hold cap and backoff
timers the manager arms are cancelled on unload and would otherwise outlive the
test that armed them.
"""

from __future__ import annotations

import copy
import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NamedTuple
from unittest.mock import patch

import pytest
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.number.const import (
    ATTR_MAX,
    ATTR_MIN,
    ATTR_STEP,
    ATTR_VALUE,
    SERVICE_SET_VALUE,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.components.sensor.const import ATTR_OPTIONS, ATTR_STATE_CLASS
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_ENTITY_ID,
    ATTR_ICON,
    ATTR_MODE,
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_ACCESS_TOKEN,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    EntityCategory,
    Platform,
    UnitOfTime,
    UnitOfVolumeFlowRate,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES

from custom_components.aquahome.api import Device
from custom_components.aquahome.api.const import API_BASE_URL
from custom_components.aquahome.const import (
    DOMAIN,
    LIVE_MIN_GAP_SECONDS_DEFAULT,
    LIVE_MIN_GAP_SECONDS_MAX,
    LIVE_MIN_GAP_SECONDS_MIN,
    LIVE_SESSIONS_PER_DAY_DEFAULT,
    LIVE_SESSIONS_PER_DAY_MAX,
    LIVE_SESSIONS_PER_DAY_MIN,
    LIVE_STATUS_BACKOFF,
    LIVE_STATUS_IDLE,
    OPTION_LIVE,
)
from custom_components.aquahome.live_state import LiveConfig, config_from_options
from tests.conftest import (
    TEST_DEVICE_ID,
    add_device_routes,
    load_fixture,
    make_access_token,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from aioresponses import aioresponses
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant, State
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from syrupy.assertion import SnapshotAssertion

    from custom_components.aquahome.live import AquaHomeLiveManager

#: Platform domains the live entities live in (the components do not re-export
#: their ``DOMAIN`` for typing, so they are derived from the platform enum).
SWITCH_DOMAIN = Platform.SWITCH
NUMBER_DOMAIN = Platform.NUMBER
SENSOR_DOMAIN = Platform.SENSOR

#: Slug of the captured device's serial ``4213377-30105-2242``.
SLUG = "4213377_30105_2242"

#: Instant every test freezes to before setup, matching the sibling platform
#: suites: inside the fixtures' capture window, so nothing depends on wall time.
FROZEN_INSTANT = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
#: End of the first failure backoff (one minute) as an attribute renders it.
FIRST_BACKOFF_UNTIL_ISO = "2026-07-21T12:01:00+00:00"
#: ``updated_at`` stamp given to raw properties a test adds to a payload.
FIXTURE_STAMP = "2026-07-21T06:00:00Z"

#: The ticket the faked cloud issues. It authenticates the websocket handshake
#: on its own, so it is a credential: no entity attribute and no recorded error
#: may ever quote it back.
LIVE_TICKET = "live-ticket-secret-value"
WEBSOCKET_URI = f"/ws/?p={LIVE_TICKET}"

#: The three live-mode switches: (description key, category, icon). Only the
#: manual hold is a primary control; the other two configure when live mode
#: opens a session on its own and are therefore CONFIG-categorised.
LIVE_SWITCHES: tuple[tuple[str, EntityCategory | None, str], ...] = (
    ("live_view", None, "mdi:eye"),
    ("smart_live_windows", EntityCategory.CONFIG, "mdi:eye-refresh"),
    ("continuous_live_flow", EntityCategory.CONFIG, "mdi:waves-arrow-right"),
)

#: The two configuration flags, in the order they appear above minus the
#: runtime-only hold — the pair a write has to persist.
CONFIG_SWITCHES = ("smart_live_windows", "continuous_live_flow")


class NumberSpec(NamedTuple):
    """One live-mode budget number's registry metadata and value range."""

    key: str
    icon: str
    default: float
    minimum: float
    maximum: float
    step: float
    unit: str | None


#: The two budget knobs, with the ranges the manager clamps writes to.
LIVE_NUMBERS: tuple[NumberSpec, ...] = (
    NumberSpec(
        key="live_sessions_per_day",
        icon="mdi:counter",
        default=float(LIVE_SESSIONS_PER_DAY_DEFAULT),
        minimum=float(LIVE_SESSIONS_PER_DAY_MIN),
        maximum=float(LIVE_SESSIONS_PER_DAY_MAX),
        step=1.0,
        unit=None,
    ),
    NumberSpec(
        key="live_min_gap",
        icon="mdi:timer-outline",
        default=LIVE_MIN_GAP_SECONDS_DEFAULT,
        minimum=LIVE_MIN_GAP_SECONDS_MIN,
        maximum=LIVE_MIN_GAP_SECONDS_MAX,
        step=10.0,
        unit=UnitOfTime.SECONDS,
    ),
)

#: Keys the status sensor's attribute dict always carries, so a template
#: written against a running session keeps evaluating once it ends.
STATUS_ATTRIBUTE_KEYS = frozenset(
    {
        "source",
        "sessions_today",
        "sessions_per_day",
        "windows_in_session",
        "last_session_end",
        "consecutive_failures",
        "backoff_until",
        "last_error",
        "smart_suspended_until",
    }
)

#: Keys the manual hold's attribute dict always carries.
VIEW_ATTRIBUTE_KEYS = frozenset({"source", "session_started", "windows_in_session"})

#: The five live-mode controls, as (platform domain, description key) — the set
#: the snapshot covers. The two sensors are snapshotted by the sensor suite.
LIVE_CONTROLS: tuple[tuple[Platform, str], ...] = (
    *((SWITCH_DOMAIN, key) for key, _category, _icon in LIVE_SWITCHES),
    *((NUMBER_DOMAIN, spec.key) for spec in LIVE_NUMBERS),
)


# ---------------------------------------------------------------------------
# Fixtures, boot and access helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _live_platforms() -> Iterator[None]:
    """Forward only the platforms hosting live entities."""
    with patch(
        "custom_components.aquahome.PLATFORMS",
        [Platform.SWITCH, Platform.NUMBER, Platform.SENSOR],
    ):
        yield


@pytest.fixture(autouse=True)
async def _unload_entry(hass: HomeAssistant) -> AsyncIterator[None]:
    """Unload the entry after every test so no live timer outlives it.

    The manual hold's auto-off cap and the failure backoff are plain delayed
    callbacks owned by the manager and cancelled in its shutdown, which runs on
    unload. A test that requested a session would otherwise leave one armed
    behind it.
    """
    yield
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def live_ticket_url(
    device_id: str = TEST_DEVICE_ID, host: str = API_BASE_URL
) -> re.Pattern[str]:
    """Match the ``GET /devices/{id}/live`` ticket URL on ``host``."""
    return re.compile(rf"^{re.escape(host)}/devices/{re.escape(device_id)}/live\?.*$")


def ticket_requests(mock: aioresponses) -> int:
    """Return how many live-session tickets were requested from the cloud.

    The ticket endpoint is separately throttled by the vendor, so *whether* a
    control asked for one is as much a part of its behaviour as the state it
    renders.
    """
    return sum(
        len(calls)
        for (method, url), calls in mock.requests.items()
        if method == "GET" and url.path.endswith("/live")
    )


async def boot(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock: aioresponses,
    freezer: FrozenDateTimeFactory,
    *,
    device_detail: dict[str, Any] | None = None,
) -> None:
    """Freeze the clock and set the entry up against the faked cloud.

    The stored access token is re-minted against the frozen clock so the auth
    manager never decides it is stale mid-test; that has to happen between
    adding the entry and setting it up, which is why the shared
    ``setup_integration`` helper is unrolled here. The ticket endpoint is
    registered alongside the polling routes so a requested session gets as far
    as the handshake — the point at which it fails, there being no websocket
    server behind the mocked transport.

    Settling with ``wait_background_tasks`` matters throughout: both the
    analytics startup pass and any live session run as entry background tasks.
    """
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(mock, device_detail=device_detail)
    mock.get(live_ticket_url(), payload={"websocket_uri": WEBSOCKET_URI}, repeat=True)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_ACCESS_TOKEN: make_access_token()}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


def manager_of(entry: MockConfigEntry) -> AquaHomeLiveManager:
    """Return the live manager the entry built for the fixture device."""
    manager: AquaHomeLiveManager = entry.runtime_data.live_managers[TEST_DEVICE_ID]
    return manager


def entity_id_of(registry: er.EntityRegistry, domain: Platform, key: str) -> str:
    """Return the entity id registered for one live description key."""
    entity_id = registry.async_get_entity_id(domain, DOMAIN, f"{SLUG}_{key}")
    assert entity_id is not None, f"live {domain} entity {key} was never registered"
    return entity_id


def state_of(
    hass: HomeAssistant, registry: er.EntityRegistry, domain: Platform, key: str
) -> State:
    """Return the current state object of one live entity."""
    state = hass.states.get(entity_id_of(registry, domain, key))
    assert state is not None, f"live {domain} entity {key} has no state"
    return state


def switch_state(hass: HomeAssistant, registry: er.EntityRegistry, key: str) -> str:
    """Return the state string of one live-mode switch."""
    return state_of(hass, registry, SWITCH_DOMAIN, key).state


def status_attributes(
    hass: HomeAssistant, registry: er.EntityRegistry
) -> dict[str, Any]:
    """Return the live-mode status sensor's current attributes."""
    return dict(state_of(hass, registry, SENSOR_DOMAIN, "live_mode_status").attributes)


def number_entity(
    hass: HomeAssistant, registry: er.EntityRegistry, key: str
) -> NumberEntity:
    """Return the live entity object behind one budget number."""
    component = hass.data[DATA_INSTANCES][NUMBER_DOMAIN]
    entity = component.get_entity(entity_id_of(registry, NUMBER_DOMAIN, key))
    assert isinstance(entity, NumberEntity)
    return entity


def sensor_entity(
    hass: HomeAssistant, registry: er.EntityRegistry, key: str
) -> SensorEntity:
    """Return the live entity object behind one sensor description key."""
    component = hass.data[DATA_INSTANCES][SENSOR_DOMAIN]
    entity = component.get_entity(entity_id_of(registry, SENSOR_DOMAIN, key))
    assert isinstance(entity, SensorEntity)
    return entity


def sensor_native(hass: HomeAssistant, registry: er.EntityRegistry, key: str) -> Any:
    """Return a sensor's native value (unit-system independent)."""
    return sensor_entity(hass, registry, key).native_value


async def switch_to(
    hass: HomeAssistant, registry: er.EntityRegistry, key: str, *, on: bool
) -> None:
    """Drive one live-mode switch through the real turn_on/turn_off action."""
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON if on else SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: entity_id_of(registry, SWITCH_DOMAIN, key)},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)


async def set_number(
    hass: HomeAssistant, registry: er.EntityRegistry, key: str, value: float
) -> None:
    """Drive one budget number through the real ``number.set_value`` action."""
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id_of(registry, NUMBER_DOMAIN, key), ATTR_VALUE: value},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)


async def seed_device(
    hass: HomeAssistant, entry: MockConfigEntry, detail: dict[str, Any]
) -> None:
    """Publish a crafted device view on the fast coordinator.

    The same call a poll makes, so the entities re-render exactly as they would
    against a fresh payload — without waiting for (or faking) the poll interval.
    """
    entry.runtime_data.coordinators[TEST_DEVICE_ID].async_set_updated_data(
        Device.from_dict(detail)
    )
    await hass.async_block_till_done(wait_background_tasks=True)


async def enable_and_reload(
    hass: HomeAssistant, entry: MockConfigEntry, registry: er.EntityRegistry, key: str
) -> None:
    """Enable a registry-disabled sensor and reload the entry so it is created."""
    entity_id = entity_id_of(registry, SENSOR_DOMAIN, key)
    registry_entry = registry.async_get(entity_id)
    assert registry_entry is not None
    assert registry_entry.disabled_by is not None
    registry.async_update_entity(entity_id, disabled_by=None)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


# ---------------------------------------------------------------------------
# Crafted device payloads (never mutate the fixture files — always deepcopy)
# ---------------------------------------------------------------------------


def _load_detail() -> dict[str, Any]:
    """Return an isolated deep copy of the device-detail fixture."""
    return copy.deepcopy(load_fixture("device-detail.json"))


def _treatment(detail: dict[str, Any]) -> dict[str, Any]:
    """Return the mutable enriched ``water_treatment`` block of ``detail``."""
    treatment: dict[str, Any] = detail["enriched_data"]["water_treatment"]
    return treatment


def _set_property(detail: dict[str, Any], name: str, value: Any) -> None:
    """Set a raw property's value, adding the property when it is absent."""
    properties: dict[str, Any] = detail["properties"]
    existing = properties.get(name)
    if existing is None:
        properties[name] = {"name": name, "value": value, "updated_at": FIXTURE_STAMP}
        return
    existing["value"] = value


def _drop_property(detail: dict[str, Any], name: str) -> None:
    """Remove a raw property, as a payload served without the map would."""
    detail["properties"].pop(name, None)


def _set_regenerating(detail: dict[str, Any], *, active: bool) -> None:
    """Mark the crafted payload as regenerating (or back to ready)."""
    treatment = _treatment(detail)
    state = "regenerating" if active else "ready"
    treatment["recharge_ui"]["state"] = state
    treatment["regeneration"]["regeneration_status"] = state if active else "none"
    treatment["regeneration_status"] = state if active else "none"


# ---------------------------------------------------------------------------
# Existence, identity and registry metadata
# ---------------------------------------------------------------------------


async def test_five_live_controls_per_device(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Every device gets the three switches and two numbers, ids ``{slug}_{key}``.

    Nothing gates their existence — every device has a live manager — so the set
    is fixed and complete even on the regeneration-only dev fixture, and all
    five attach to the same device as the telemetry entities.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    device_ids = set()
    for domain, key in LIVE_CONTROLS:
        registry_entry = entity_registry.async_get(
            entity_id_of(entity_registry, domain, key)
        )
        assert registry_entry is not None
        assert registry_entry.unique_id == f"{SLUG}_{key}"
        device_ids.add(registry_entry.device_id)
    assert len(device_ids) == 1


@pytest.mark.parametrize(
    ("key", "category", "icon"),
    [pytest.param(*row, id=row[0]) for row in LIVE_SWITCHES],
)
async def test_switch_registry_metadata_and_icon(  # noqa: PLR0913 - standard HA platform-test fixture set plus the parametrized row
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    key: str,
    category: EntityCategory | None,
    icon: str,
) -> None:
    """Each live switch is registry-enabled, correctly categorised, and iconed.

    The manual hold is the control a user reaches for to watch water use as it
    happens, so it is a primary entity; the two behaviour flags configure how
    the integration behaves and are CONFIG.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    registry_entry = entity_registry.async_get(
        entity_id_of(entity_registry, SWITCH_DOMAIN, key)
    )
    assert registry_entry is not None
    assert registry_entry.disabled_by is None
    assert registry_entry.hidden_by is None
    assert registry_entry.entity_category == category
    assert registry_entry.translation_key == key
    assert registry_entry.original_icon == icon
    assert (
        state_of(hass, entity_registry, SWITCH_DOMAIN, key).attributes[ATTR_ICON]
        == icon
    )


@pytest.mark.parametrize(
    "spec", [pytest.param(spec, id=spec.key) for spec in LIVE_NUMBERS]
)
async def test_number_registry_metadata_and_range(  # noqa: PLR0913 - standard HA platform-test fixture set plus the parametrized row
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    spec: NumberSpec,
) -> None:
    """Each budget knob exposes its supported range as a typed-in box.

    The range is the supported one rather than advice, so it belongs on the
    entity: a value outside it is refused before it ever reaches the manager.
    Both are entered rather than dragged — they are set once to a considered
    value, not tuned by feel — and both are configuration.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    registry_entry = entity_registry.async_get(
        entity_id_of(entity_registry, NUMBER_DOMAIN, spec.key)
    )
    assert registry_entry is not None
    assert registry_entry.disabled_by is None
    assert registry_entry.hidden_by is None
    assert registry_entry.entity_category is EntityCategory.CONFIG
    assert registry_entry.translation_key == spec.key
    assert registry_entry.original_icon == spec.icon

    state = state_of(hass, entity_registry, NUMBER_DOMAIN, spec.key)
    assert float(state.state) == spec.default
    assert state.attributes[ATTR_MIN] == spec.minimum
    assert state.attributes[ATTR_MAX] == spec.maximum
    assert state.attributes[ATTR_STEP] == spec.step
    assert state.attributes[ATTR_MODE] == NumberMode.BOX
    assert state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) == spec.unit


# ---------------------------------------------------------------------------
# Defaults and availability
# ---------------------------------------------------------------------------


async def test_defaults_on_a_fresh_entry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A fresh entry starts with every hold off and the documented budgets.

    Live mode is opt-in by construction: with no persisted options no session
    may open by itself, the budgets are the conservative defaults, and nothing
    at all has been written to the entry — the manager persists only when the
    user changes something.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    for key, _category, _icon in LIVE_SWITCHES:
        assert switch_state(hass, entity_registry, key) == STATE_OFF
    for spec in LIVE_NUMBERS:
        state = state_of(hass, entity_registry, NUMBER_DOMAIN, spec.key)
        assert float(state.state) == spec.default

    assert mock_config_entry.options.get(OPTION_LIVE) is None
    assert config_from_options(mock_config_entry, TEST_DEVICE_ID) == LiveConfig()
    assert manager_of(mock_config_entry).state.config == LiveConfig()
    # A plain boot never asks the cloud for a live session.
    assert ticket_requests(mock_api) == 0


async def test_live_controls_stay_available_while_the_device_is_offline(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An offline softener never takes the live-mode controls with it.

    Every one of them renders what the *user asked for* — held in the manager
    and, for the persisted subset, in the config entry — not what the device
    reports, so an outage must not strand a hold the owner wants to switch off
    or a budget they want to lower. The status sensor is deliberately included:
    an idle or backing-off manager is exactly what a user needs to see while the
    cloud is unreachable.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    coordinator = mock_config_entry.runtime_data.coordinators[TEST_DEVICE_ID]
    coordinator.async_set_updated_data(replace(coordinator.data, is_online=False))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.device_online is False

    for domain, key in LIVE_CONTROLS:
        assert state_of(hass, entity_registry, domain, key).state != STATE_UNAVAILABLE
    assert (
        state_of(hass, entity_registry, SENSOR_DOMAIN, "live_mode_status").state
        != STATE_UNAVAILABLE
    )

    # Still writable in that state: the flag is local, so nothing about an
    # offline device may block turning a behaviour flag on.
    await switch_to(hass, entity_registry, "smart_live_windows", on=True)
    assert switch_state(hass, entity_registry, "smart_live_windows") == STATE_ON


# ---------------------------------------------------------------------------
# Writes: options round-trip, re-render, and range handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", [pytest.param(key, id=key) for key in CONFIG_SWITCHES])
async def test_config_switches_round_trip_through_the_entry_options(  # noqa: PLR0913 - standard HA platform-test fixture set plus the parametrized row
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    key: str,
) -> None:
    """Both behaviour flags persist into ``entry.options`` and re-render.

    The switch writes nothing itself: it calls the manager, which persists the
    configuration subset and republishes the state the entity renders. Both
    halves are asserted, because a flag that renders ON without persisting is
    forgotten on the next restart.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)
    attribute = "smart_windows" if key == "smart_live_windows" else "continuous"

    await switch_to(hass, entity_registry, key, on=True)
    assert switch_state(hass, entity_registry, key) == STATE_ON
    stored = config_from_options(mock_config_entry, TEST_DEVICE_ID)
    assert getattr(stored, attribute) is True
    assert getattr(manager_of(mock_config_entry).state.config, attribute) is True

    await switch_to(hass, entity_registry, key, on=False)
    assert switch_state(hass, entity_registry, key) == STATE_OFF
    stored = config_from_options(mock_config_entry, TEST_DEVICE_ID)
    assert getattr(stored, attribute) is False
    # Flipping one flag never drags the other with it: they share one persisted
    # block and one published state object.
    other = "continuous" if attribute == "smart_windows" else "smart_windows"
    assert getattr(stored, other) is False


async def test_the_manual_hold_is_never_persisted(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Turning the view hold on drives the manager but writes nothing.

    A hold is a request to stream *right now*; persisting it would have a
    restart silently re-open a socket nobody is watching. The flag therefore
    lives in memory only, and the entry's options stay untouched — while the two
    configuration flags on the very same manager do persist.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    await switch_to(hass, entity_registry, "live_view", on=True)
    assert switch_state(hass, entity_registry, "live_view") == STATE_ON
    assert manager_of(mock_config_entry).state.live_view is True
    assert mock_config_entry.options.get(OPTION_LIVE) is None

    await switch_to(hass, entity_registry, "live_view", on=False)
    assert switch_state(hass, entity_registry, "live_view") == STATE_OFF
    assert manager_of(mock_config_entry).state.live_view is False
    assert mock_config_entry.options.get(OPTION_LIVE) is None


@pytest.mark.parametrize(
    ("key", "value", "attribute"),
    [
        pytest.param("live_sessions_per_day", 12, "sessions_per_day", id="sessions"),
        pytest.param("live_min_gap", 300, "min_gap_seconds", id="min-gap"),
    ],
)
async def test_numbers_round_trip_through_the_entry_options(  # noqa: PLR0913 - standard HA platform-test fixture set plus the parametrized row
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    key: str,
    value: int,
    attribute: str,
) -> None:
    """A budget written on the entity persists and re-renders from the manager.

    The knobs are the one live-mode decision a household may reasonably want to
    tune, and there is no options flow to tune them in, so the round trip
    through ``entry.options`` is what makes the setting survive a restart.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    await set_number(hass, entity_registry, key, value)

    assert float(state_of(hass, entity_registry, NUMBER_DOMAIN, key).state) == value
    assert (
        getattr(config_from_options(mock_config_entry, TEST_DEVICE_ID), attribute)
        == value
    )
    assert getattr(manager_of(mock_config_entry).state.config, attribute) == value


@pytest.mark.parametrize(
    ("key", "value"),
    [
        pytest.param("live_sessions_per_day", 1000, id="sessions-above-max"),
        pytest.param("live_min_gap", 5, id="gap-below-min"),
    ],
)
async def test_the_set_value_action_refuses_a_value_outside_the_range(  # noqa: PLR0913 - standard HA platform-test fixture set plus the parametrized row
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    key: str,
    value: int,
) -> None:
    """An out-of-range budget is rejected before it reaches the manager.

    The declared range makes Home Assistant's own action layer refuse the call
    with a translated error, which is the honest answer to a script asking for a
    budget the integration does not support: nothing is written, and the entity
    keeps rendering the value it had.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    with pytest.raises(ServiceValidationError):
        await set_number(hass, entity_registry, key, value)

    assert mock_config_entry.options.get(OPTION_LIVE) is None
    assert manager_of(mock_config_entry).state.config == LiveConfig()


@pytest.mark.parametrize(
    ("key", "value", "attribute", "expected"),
    [
        pytest.param(
            "live_sessions_per_day",
            10_000,
            "sessions_per_day",
            LIVE_SESSIONS_PER_DAY_MAX,
            id="sessions-above-max",
        ),
        pytest.param(
            "live_min_gap", 1, "min_gap_seconds", LIVE_MIN_GAP_SECONDS_MIN, id="gap-min"
        ),
    ],
)
async def test_the_entity_clamps_a_value_that_bypasses_the_action_layer(  # noqa: PLR0913 - standard HA platform-test fixture set plus the parametrized row
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    key: str,
    value: float,
    attribute: str,
    expected: float,
) -> None:
    """Reaching the entity directly still cannot set an unsupported budget.

    The action layer's range check is the first line of defence; the manager's
    own clamp is the second, and it is the one that protects the cloud's session
    allowance from any caller that gets past the first.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    await number_entity(hass, entity_registry, key).async_set_native_value(value)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert getattr(manager_of(mock_config_entry).state.config, attribute) == expected
    assert (
        getattr(config_from_options(mock_config_entry, TEST_DEVICE_ID), attribute)
        == expected
    )
    assert float(state_of(hass, entity_registry, NUMBER_DOMAIN, key).state) == expected


# ---------------------------------------------------------------------------
# The live-mode status sensor
# ---------------------------------------------------------------------------


async def test_status_sensor_is_a_diagnostic_enum_starting_idle(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The status sensor is an enum of exactly the three manager states.

    It describes how the integration is gathering data rather than the water
    treatment itself, so it is diagnostic; and it carries the full session
    bookkeeping — including the keys that are empty while nothing is streaming —
    so the cost of live mode is inspectable without turning on debug logging.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    registry_entry = entity_registry.async_get(
        entity_id_of(entity_registry, SENSOR_DOMAIN, "live_mode_status")
    )
    assert registry_entry is not None
    assert registry_entry.unique_id == f"{SLUG}_live_mode_status"
    assert registry_entry.entity_category is EntityCategory.DIAGNOSTIC
    assert registry_entry.translation_key == "live_mode_status"
    assert registry_entry.original_icon == "mdi:access-point"

    state = state_of(hass, entity_registry, SENSOR_DOMAIN, "live_mode_status")
    assert state.state == LIVE_STATUS_IDLE
    assert state.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.ENUM
    assert state.attributes[ATTR_OPTIONS] == ["idle", "live", "backoff"]
    assert ATTR_STATE_CLASS not in state.attributes

    attributes = dict(state.attributes)
    assert attributes.keys() >= STATUS_ATTRIBUTE_KEYS
    assert attributes["source"] is None
    assert attributes["sessions_today"] == 0
    assert attributes["sessions_per_day"] == LIVE_SESSIONS_PER_DAY_DEFAULT
    assert attributes["windows_in_session"] == 0
    assert attributes["last_session_end"] is None
    assert attributes["consecutive_failures"] == 0
    assert attributes["backoff_until"] is None
    assert attributes["last_error"] is None
    assert attributes["smart_suspended_until"] is None


async def test_status_sensor_reports_the_configured_budget(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Lowering the budget number is visible on the status sensor at once.

    The sensor reports spend against the budget, so it reads the same published
    configuration the number writes — one source of truth, no copy that can go
    stale after a change.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)
    assert status_attributes(hass, entity_registry)["sessions_per_day"] == 48

    await set_number(hass, entity_registry, "live_sessions_per_day", 12)

    assert status_attributes(hass, entity_registry)["sessions_per_day"] == 12


async def test_a_failed_session_backs_off_while_the_hold_stays_on(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A hold whose session cannot connect reports the failure, not silence.

    The faked cloud issues a ticket but there is no websocket behind it, so the
    handshake fails. That is the interesting entity-level case: the switch keeps
    rendering what the user asked for (the manager retries the hold by itself
    once the backoff expires), while the status sensor moves to ``backoff`` and
    explains why — with the ticket, which is a credential, never quoted in the
    error. A session that never connected also spends no daily grant.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    await switch_to(hass, entity_registry, "live_view", on=True)

    assert switch_state(hass, entity_registry, "live_view") == STATE_ON
    view_attributes = dict(
        state_of(hass, entity_registry, SWITCH_DOMAIN, "live_view").attributes
    )
    assert view_attributes.keys() >= VIEW_ATTRIBUTE_KEYS
    assert view_attributes["source"] is None
    assert view_attributes["session_started"] is None
    assert view_attributes["windows_in_session"] == 0

    status = state_of(hass, entity_registry, SENSOR_DOMAIN, "live_mode_status")
    assert status.state == LIVE_STATUS_BACKOFF
    assert status.attributes["consecutive_failures"] == 1
    assert status.attributes["backoff_until"] == FIRST_BACKOFF_UNTIL_ISO
    assert status.attributes["sessions_today"] == 0
    error = status.attributes["last_error"]
    assert isinstance(error, str)
    # The handshake is the step that failed — the ticket itself was issued —
    # and the ticket never appears in what the entity publishes.
    assert "websocket" in error.lower()
    assert LIVE_TICKET not in error
    # Exactly one ticket was spent on the refused attempt, not a retry storm.
    assert ticket_requests(mock_api) == 1

    # Releasing the hold is always possible, and it does not fake a recovery:
    # the backoff is the manager's own, and it outlives the switch that armed it.
    await switch_to(hass, entity_registry, "live_view", on=False)
    assert switch_state(hass, entity_registry, "live_view") == STATE_OFF
    assert (
        state_of(hass, entity_registry, SENSOR_DOMAIN, "live_mode_status").state
        == LIVE_STATUS_BACKOFF
    )


# ---------------------------------------------------------------------------
# Sensors upgraded for the live stream (raw property first)
# ---------------------------------------------------------------------------


async def test_current_water_flow_scales_the_raw_property(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Flow is the raw tenths-of-a-gallon property, natively in gal/min.

    The device meters in gallons per minute and reports tenths, so the sensor
    binds that native unit and lets Home Assistant present litres per minute —
    the value is never re-labelled. The second reading is the one a live session
    streams during a steady tap: raw ``9`` is 0.9 gal/min, roughly 3.4 L/min.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    # The captured fixture was taken with no water moving.
    assert sensor_native(hass, entity_registry, "current_water_flow") == 0.0
    entity = sensor_entity(hass, entity_registry, "current_water_flow")
    assert entity.native_unit_of_measurement == UnitOfVolumeFlowRate.GALLONS_PER_MINUTE
    state = state_of(hass, entity_registry, SENSOR_DOMAIN, "current_water_flow")
    assert state.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.VOLUME_FLOW_RATE
    assert state.attributes[ATTR_STATE_CLASS] == SensorStateClass.MEASUREMENT
    assert (
        state.attributes[ATTR_UNIT_OF_MEASUREMENT]
        == UnitOfVolumeFlowRate.LITERS_PER_MINUTE
    )

    flowing = _load_detail()
    _set_property(flowing, "current_water_flow_gpm", 9)
    await seed_device(hass, mock_config_entry, flowing)

    assert sensor_native(hass, entity_registry, "current_water_flow") == 0.9
    flowing_state = state_of(hass, entity_registry, SENSOR_DOMAIN, "current_water_flow")
    assert float(flowing_state.state) == pytest.approx(3.4, abs=0.06)


async def test_rf_signal_strength_prefers_the_raw_property(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The link strength reads the device's own property, enriched as fallback.

    The raw property is what the device last reported — and what a live session
    streams, so it moves within seconds — while the curated copy in the same
    payload is recomputed server-side on its own schedule and can lag it by
    hours. The two are given deliberately different values to tell which one
    won; removing the raw property then proves the fallback still works for a
    payload served without the property map.
    """
    detail = _load_detail()
    _set_property(detail, "rf_signal_strength_dbm", -50)
    await boot(hass, mock_config_entry, mock_api, freezer, device_detail=detail)
    # Diagnostic radio detail is registry-disabled by default.
    await enable_and_reload(
        hass, mock_config_entry, entity_registry, "rf_signal_strength"
    )

    assert _treatment(detail)["rf_signal_strength_dbm"] == -37
    assert sensor_native(hass, entity_registry, "rf_signal_strength") == -50.0

    without_raw = copy.deepcopy(detail)
    _drop_property(without_raw, "rf_signal_strength_dbm")
    await seed_device(hass, mock_config_entry, without_raw)

    assert sensor_native(hass, entity_registry, "rf_signal_strength") == -37


async def test_regeneration_time_remaining_prefers_the_raw_countdown(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The countdown reads the valve head's own timer, and still force-zeroes.

    The raw ``regen_time_rem_secs`` property ticks with the valve and is
    streamed during a live session, so it wins over the enriched tile copy; the
    tile remains the fallback when the property map is absent. Neither source is
    trusted once the cycle is over — the cloud leaves the last countdown on the
    tile — so the force-zero rule is pinned here against a raw countdown that is
    anything but zero.
    """
    regenerating = _load_detail()
    _set_regenerating(regenerating, active=True)
    _treatment(regenerating)["recharge_ui"]["time_remaining_seconds"] = 60
    _set_property(regenerating, "regen_time_rem_secs", 900)
    await boot(hass, mock_config_entry, mock_api, freezer, device_detail=regenerating)

    assert sensor_native(hass, entity_registry, "regeneration_time_remaining") == 900

    # Same running cycle, payload served without the property map: the enriched
    # tile carries the countdown instead.
    without_raw = copy.deepcopy(regenerating)
    _drop_property(without_raw, "regen_time_rem_secs")
    await seed_device(hass, mock_config_entry, without_raw)
    assert sensor_native(hass, entity_registry, "regeneration_time_remaining") == 60

    # Cycle over, stale countdown left behind on both sources: reported as zero.
    finished = _load_detail()
    _set_regenerating(finished, active=False)
    _treatment(finished)["recharge_ui"]["time_remaining_seconds"] = 60
    _set_property(finished, "regen_time_rem_secs", 900)
    await seed_device(hass, mock_config_entry, finished)
    assert sensor_native(hass, entity_registry, "regeneration_time_remaining") == 0


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


async def test_live_controls_snapshot(  # noqa: PLR0913 - standard HA snapshot-test fixture set
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The five live-mode controls match their registry + state snapshot.

    Restricted to this family on purpose: the sensor suite already snapshots the
    status and flow sensors. The captured baseline is the honest fresh-entry one
    — every hold off, both budgets at their defaults.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    for domain, key in LIVE_CONTROLS:
        entity_id = entity_id_of(entity_registry, domain, key)
        assert entity_registry.async_get(entity_id) == snapshot(
            name=f"{entity_id}-entry"
        )
        assert hass.states.get(entity_id) == snapshot(name=f"{entity_id}-state")
