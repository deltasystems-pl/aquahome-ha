"""Release smoke test — the live-mode tier inside a real, full boot.

The dedicated live suites drive the pieces in isolation: the websocket session,
the manager's grant gate and session lifecycle, the switches, numbers and
sensors. This file, whose recorder-backed boot mechanics follow
:mod:`tests.test_phase8_smoke`, starts the integration exactly as Home Assistant
would — every platform forwarded, a real recorder behind ``recorder_mock``, the
captured cloud payloads behind ``aioresponses`` — and checks the invariants a
release depends on:

* the entry loads with no ``aquahome`` logger emitting at ERROR, and the entity
  inventory is exactly the shipped per-platform map (83 entities in total);
* the seven live-tier entities exist with their shipped defaults — all three
  switches off, the two budget knobs at 48 sessions a day and a 120-second gap,
  the status sensor idle and the flow sensor reading zero;
* one live manager per device is armed and has published its seeded state,
  **without touching the ticket endpoint**: a plain boot must cost nothing on
  the small, separately throttled ``/live`` budget, so the absence of that
  request is asserted against the intercepted request log rather than assumed;
* the diagnostics dump carries the per-device ``live`` block, so a support
  report shows the live configuration and session bookkeeping the owner is
  running with;
* unloading the entry takes the manager down cleanly — its entities leave the
  state machine and no live-mode task or timer is left behind;
* a persisted live opt-in survives a reload: switching smart windows on writes
  through to the entry options, and the restarted entry brings the switch back
  on rather than resetting it.

Time is frozen throughout at 12:30 Europe/Warsaw — the device's own zone — and
the stored access token is re-minted against that frozen clock so the auth
manager never reaches for a refresh route. No websocket is opened anywhere in
this module, so freezing is safe here: the frozen monotonic clock that would
stall real socket I/O is never in the path of one.

The two inherited mechanics are unchanged from the earlier smokes:
:data:`~custom_components.aquahome.const.BACKFILL_REQUEST_PACING_SECONDS` is
patched to zero module-wide (a genuine ``asyncio.sleep`` would never wake under
the freezer), and the recorder is drained before the boot is considered settled.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, cast
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ACCESS_TOKEN,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    Platform,
)
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from yarl import URL

from custom_components.aquahome.const import (
    DOMAIN,
    LIVE_MIN_GAP_SECONDS_DEFAULT,
    LIVE_SESSIONS_PER_DAY_DEFAULT,
    LIVE_STATUS_IDLE,
    OPTION_LIVE,
)
from custom_components.aquahome.coordinator import AquaHomeRuntimeData
from custom_components.aquahome.diagnostics import async_get_config_entry_diagnostics
from custom_components.aquahome.live import AquaHomeLiveManager
from custom_components.aquahome.live_state import LiveConfig
from tests.conftest import (
    TEST_DEVICE_ID,
    add_datapoint_graph_routes,
    add_device_routes,
    command_url,
    load_fixture,
    make_access_token,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from aioresponses import aioresponses
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.components.recorder.core import Recorder
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

#: Slug derived from the fixture serial ``4213377-30105-2242`` (see entity.py).
SLUG: Final = "4213377_30105_2242"

#: The instant every clock in this module reads: 12:30 Europe/Warsaw on the last
#: day of the captured history, matching the earlier smokes exactly.
FROZEN_NOW: Final = datetime(2026, 7, 27, 10, 30, tzinfo=UTC)

#: Entities the captured dev fixtures create, per platform domain — the shipped
#: inventory, restated here rather than imported so this file pins the release
#: build on its own. It must stay identical to the map the Phase-8 smoke pins:
#: two files disagreeing about the inventory is itself the bug to catch.
EXPECTED_ENTITIES: Final[dict[str, int]] = {
    "sensor": 39,
    "binary_sensor": 14,
    "event": 1,
    "button": 6,
    "select": 15,
    "switch": 6,
    "number": 2,
}

#: The same inventory summed. Kept as its own pin so a platform that silently
#: stops registering anything is caught even if another grows by the same amount.
EXPECTED_TOTAL: Final = 83

#: The three live-mode switches, by unique-id suffix, in creation order. All
#: three ship off: live mode never opens a socket the owner did not ask for.
LIVE_SWITCH_KEYS: Final[tuple[str, ...]] = (
    "live_view",
    "smart_live_windows",
    "continuous_live_flow",
)

#: The two live-mode budget numbers, by unique-id suffix.
LIVE_NUMBER_KEYS: Final[tuple[str, ...]] = (
    "live_sessions_per_day",
    "live_min_gap",
)

#: The two live-mode sensors, by unique-id suffix.
LIVE_SENSOR_KEYS: Final[tuple[str, ...]] = (
    "live_mode_status",
    "current_water_flow",
)

#: Shipped default of the daily grant budget, as the number entity renders it.
EXPECTED_SESSIONS_PER_DAY: Final = 48.0

#: Shipped default of the minimum gap between grants, in seconds.
EXPECTED_MIN_GAP_SECONDS: Final = 120.0

#: The captured device reports ``current_water_flow_gpm`` at zero, which the
#: sensor scales by a tenth and renders in its suggested litres-per-minute unit.
EXPECTED_WATER_FLOW: Final = "0.0"

#: A non-default live configuration, used to prove that what the entry persists
#: is what the manager seeds, the diagnostics report, and a reload restores.
STORED_CONFIG: Final = LiveConfig(
    smart_windows=True,
    continuous=False,
    sessions_per_day=24,
    min_gap_seconds=300.0,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_recorder_before_hass(recorder_db_url: str) -> None:
    """Prepare the recorder database before Home Assistant is created.

    ``recorder_db_url`` refuses to run once a ``hass`` instance exists, and the
    suite-wide autouse ``enable_custom_integrations`` fixture pulls ``hass`` in
    ahead of everything else. Overriding this hook — which the ``hass`` fixture
    itself depends on, exactly as its upstream docstring invites recorder tests
    to do — puts the database back in front of it.
    """


@pytest.fixture(autouse=True)
def _instant_pacing() -> Iterator[None]:
    """Collapse the backfill's inter-request pacing for the whole module.

    The pacing is a real ``asyncio.sleep`` on the event-loop clock, which the
    freezer never advances, so a paced session would idle for ever rather than
    merely be slow. It is patched module-wide because a reload backfills again,
    long after the first boot's own session finished.
    """
    with patch(
        "custom_components.aquahome.statistics.BACKFILL_REQUEST_PACING_SECONDS", 0
    ):
        yield


# ---------------------------------------------------------------------------
# Route + boot helpers
# ---------------------------------------------------------------------------


def _hourly_payload(query: Mapping[str, str]) -> dict[str, Any]:
    """Return the hourly graph payload for one requested window.

    Mirrors the reference device's ~130-day hourly retention: a window inside
    the captured July serves real readings, anything older serves the
    zero-filled shape the cloud really returns past the retention floor.
    """
    if query.get("start", "").startswith("2026-07"):
        return load_fixture("graph-meter-hourly.json")
    return load_fixture("graph-meter-hourly-empty.json")


def _add_routes(mock: aioresponses) -> None:
    """Register every cloud route a full boot may hit.

    The command route is registered even though a quiet boot must never use it:
    an unexpected command then shows up as a *counted call* the assertions can
    name, rather than as an opaque connection error deep in a background task.
    The live-ticket route is deliberately **not** registered — nothing in a boot
    may reach it, and leaving it unregistered turns a regression into a loud
    failure instead of a silently accepted request.
    """
    add_device_routes(mock)
    add_datapoint_graph_routes(
        mock,
        by_period={
            "year": load_fixture("graph-meter-yearly.json"),
            "month": load_fixture("graph-meter-monthly.json"),
            "day": load_fixture("graph-meter-daily.json"),
            "hour": _hourly_payload,
        },
    )
    mock.put(command_url(), payload={"result": "ok"}, repeat=True)


async def _settle(hass: HomeAssistant) -> None:
    """Wait for a start-up (or reload) to finish everything it set in motion.

    The backfill-then-analytics pipeline is a background task deliberately kept
    off the setup path, so settling it needs the background-aware wait, the
    recorder needs its own, and the live manager — whose evaluators the fast and
    engine listeners hand to fresh tasks — needs one more pass of the loop after
    both.
    """
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)
    await hass.async_block_till_done()


async def _boot(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock: aioresponses,
    freezer: FrozenDateTimeFactory,
    options: dict[str, Any] | None = None,
) -> None:
    """Freeze the clock, boot every platform, and settle the startup pipeline.

    The stored access token is re-minted against the frozen clock so the auth
    manager never decides it is stale — which has to happen between adding the
    entry and setting it up, so the shared ``setup_integration`` helper is
    unrolled here rather than called, and ``options`` (the persisted live
    configuration a restart restores from) is written through the same call.
    """
    freezer.move_to(FROZEN_NOW)
    _add_routes(mock)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_ACCESS_TOKEN: make_access_token()},
        options=options if options is not None else entry.options,
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass)


def _runtime(entry: MockConfigEntry) -> AquaHomeRuntimeData:
    """Return the entry's runtime data, narrowed to the integration's type."""
    runtime = entry.runtime_data
    assert isinstance(runtime, AquaHomeRuntimeData)
    return runtime


def _manager(entry: MockConfigEntry) -> AquaHomeLiveManager:
    """Return the fixture device's live manager from the runtime data."""
    manager = _runtime(entry).live_managers[TEST_DEVICE_ID]
    assert isinstance(manager, AquaHomeLiveManager)
    return manager


def _entry_state(entry: MockConfigEntry) -> ConfigEntryState:
    """Return the entry's current lifecycle state.

    Read through a call so each assertion compares the state as it is at that
    point rather than as the type checker last narrowed it.
    """
    return cast("ConfigEntryState", entry.state)


def _entity_id(registry: er.EntityRegistry, domain: str, key: str) -> str:
    """Resolve an entity id from its platform domain and unique-id suffix."""
    entity_id = registry.async_get_entity_id(domain, DOMAIN, f"{SLUG}_{key}")
    assert entity_id is not None, f"{domain} entity {key} was not registered"
    return entity_id


def _state(
    hass: HomeAssistant, registry: er.EntityRegistry, domain: str, key: str
) -> str:
    """Return the state string of the entity with unique-id suffix ``key``."""
    state = hass.states.get(_entity_id(registry, domain, key))
    assert state is not None, f"{domain} entity {key} has no state"
    return state.state


def _entities_by_domain(
    registry: er.EntityRegistry, entry: MockConfigEntry
) -> dict[str, int]:
    """Return how many entities the entry registered, per platform domain."""
    counts: dict[str, int] = {}
    for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
        counts[registered.domain] = counts.get(registered.domain, 0) + 1
    return counts


def _stored_live_options(config: LiveConfig = STORED_CONFIG) -> dict[str, Any]:
    """Return entry options carrying one device's persisted live configuration."""
    return {
        OPTION_LIVE: {
            TEST_DEVICE_ID: {
                "smart_windows": config.smart_windows,
                "continuous": config.continuous,
                "sessions_per_day": config.sessions_per_day,
                "min_gap_seconds": config.min_gap_seconds,
            }
        }
    }


def _stored_live_config(entry: MockConfigEntry) -> dict[str, Any]:
    """Return the live options the entry currently persists for the device."""
    devices = entry.options.get(OPTION_LIVE)
    assert isinstance(devices, dict), "the entry persists no live options"
    stored = devices.get(TEST_DEVICE_ID)
    assert isinstance(stored, dict), "the fixture device has no persisted live config"
    return dict(stored)


def _requested_paths(mock: aioresponses, suffix: str) -> list[str]:
    """Return every intercepted request path ending in ``suffix``."""
    return [
        url.path
        for (_method, url) in mock.requests
        if isinstance(url, URL) and url.path.endswith(suffix)
    ]


def _command_calls(mock: aioresponses) -> int:
    """Return how many device commands have been sent to the cloud."""
    return len(mock.requests.get(("PUT", URL(command_url())), []))


def _live_tasks() -> list[str]:
    """Return the names of every still-running live-mode task."""
    prefix = f"{DOMAIN} {SLUG} live"
    return [
        task.get_name()
        for task in asyncio.all_tasks()
        if task.get_name().startswith(prefix)
    ]


def _aquahome_errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return every ERROR-or-worse message the integration's loggers emitted."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.ERROR
        and record.name.startswith("custom_components.aquahome")
    ]


def _live_diagnostics(diagnostics: dict[str, Any]) -> dict[str, Any]:
    """Return the single device's ``live`` block out of a diagnostics dump."""
    devices = diagnostics["devices"]
    assert len(devices) == 1
    live = devices[0]["live"]
    assert isinstance(live, dict), "the diagnostics carry no live block"
    return live


# ---------------------------------------------------------------------------
# The release boot
# ---------------------------------------------------------------------------


async def test_release_boot_ships_the_live_tier_at_rest(  # noqa: PLR0913 - one fixture per faked subsystem
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A full boot registers the shipped inventory and arms live mode idle.

    One boot, every release-critical invariant: the entry loads quietly, the
    per-platform entity map is exactly what the build ships, the seven live-tier
    entities are present at their documented defaults, and the manager behind
    them is armed and has published — all without spending a single request on
    the ticket endpoint or sending a single command to the softener.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)

    # -- the entry loads, quietly, with the shipped inventory -----------------
    assert _entry_state(mock_config_entry) is ConfigEntryState.LOADED
    assert _aquahome_errors(caplog) == []

    counts = _entities_by_domain(entity_registry, mock_config_entry)
    assert counts == EXPECTED_ENTITIES
    assert sum(counts.values()) == EXPECTED_TOTAL

    # -- the seven live-tier entities, at their defaults ----------------------
    for key in LIVE_SWITCH_KEYS:
        registered = entity_registry.async_get(
            _entity_id(entity_registry, Platform.SWITCH, key)
        )
        assert registered is not None
        # Registry-enabled by default: a control the owner cannot see is not a
        # control at all.
        assert registered.disabled_by is None
        assert _state(hass, entity_registry, Platform.SWITCH, key) == STATE_OFF

    # The shipped budget: 48 grants a device-local day, at least 120 s apart.
    assert LIVE_SESSIONS_PER_DAY_DEFAULT == EXPECTED_SESSIONS_PER_DAY
    assert LIVE_MIN_GAP_SECONDS_DEFAULT == EXPECTED_MIN_GAP_SECONDS
    sessions, gap = LIVE_NUMBER_KEYS
    assert (
        float(_state(hass, entity_registry, Platform.NUMBER, sessions))
        == EXPECTED_SESSIONS_PER_DAY
    )
    assert (
        float(_state(hass, entity_registry, Platform.NUMBER, gap))
        == EXPECTED_MIN_GAP_SECONDS
    )

    status, flow = LIVE_SENSOR_KEYS
    assert _state(hass, entity_registry, Platform.SENSOR, status) == LIVE_STATUS_IDLE
    assert _state(hass, entity_registry, Platform.SENSOR, flow) == EXPECTED_WATER_FLOW

    # -- the manager behind them is armed and has published -------------------
    runtime = _runtime(mock_config_entry)
    assert set(runtime.live_managers) == set(runtime.coordinators)

    manager = _manager(mock_config_entry)
    assert manager.device_id == TEST_DEVICE_ID
    assert manager.device_slug == SLUG
    # No poll cycle: the manager reacts to its two sources and nothing else.
    assert manager.update_interval is None
    published = manager.data
    assert published is not None
    assert published is manager.state
    assert published.status == LIVE_STATUS_IDLE
    assert published.source is None
    assert published.sessions_today == 0
    assert published.consecutive_failures == 0
    assert published.config == LiveConfig()

    # -- and it has cost the scarce budgets nothing ---------------------------
    # The ticket endpoint runs its own small token bucket, so an idle boot that
    # reaches for it would spend a session's worth of budget for no data.
    assert _requested_paths(mock_api, "/live") == []
    assert _command_calls(mock_api) == 0
    # Positive control for the two assertions above: the same request log does
    # hold the reads the boot really made, so an empty list means the endpoint
    # went untouched rather than that nothing was recorded at all.
    assert _requested_paths(mock_api, "/settings") != []


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


async def test_diagnostics_report_the_seeded_live_configuration(  # noqa: PLR0913 - one fixture per faked subsystem
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The diagnostics dump carries the live block the manager was seeded with.

    A support report has to show which budget the owner is actually running and
    whether live mode is healthy, so the block is asserted end to end: the
    persisted configuration on the way in, the runtime bookkeeping on the way
    out.
    """
    await _boot(
        hass, mock_config_entry, mock_api, freezer, options=_stored_live_options()
    )

    assert _entry_state(mock_config_entry) is ConfigEntryState.LOADED
    assert _aquahome_errors(caplog) == []

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    live = _live_diagnostics(diagnostics)

    assert live["config"] == {
        "smart_windows": True,
        "continuous": False,
        "sessions_per_day": 24,
        "min_gap_seconds": 300.0,
    }
    # Runtime bookkeeping is reported honestly as "nothing has happened yet",
    # and the ticket that authenticates a socket is never part of it.
    assert live["status"] == LIVE_STATUS_IDLE
    assert live["source"] is None
    assert live["live_view"] is False
    assert live["session_started"] is None
    assert live["sessions_today"] == 0
    assert live["consecutive_failures"] == 0
    assert live["backoff_until"] is None
    assert live["last_error"] is None

    # The seeded configuration is the manager's, not just the dump's.
    assert _manager(mock_config_entry).state.config == STORED_CONFIG


# ---------------------------------------------------------------------------
# Unload
# ---------------------------------------------------------------------------


async def test_unloading_the_entry_shuts_live_mode_down_cleanly(  # noqa: PLR0913 - one fixture per faked subsystem
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unloading removes the live entities and leaves no live-mode work behind.

    The manager owns a background task and up to five timers, so an unload that
    forgets any of them would keep a socket, a renewal or a backoff alive
    against an entry Home Assistant considers gone.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)

    live_entity_ids = [
        _entity_id(entity_registry, Platform.SWITCH, key) for key in LIVE_SWITCH_KEYS
    ]
    live_entity_ids += [
        _entity_id(entity_registry, Platform.NUMBER, key) for key in LIVE_NUMBER_KEYS
    ]
    live_entity_ids += [
        _entity_id(entity_registry, Platform.SENSOR, key) for key in LIVE_SENSOR_KEYS
    ]
    assert [
        entity_id
        for entity_id in live_entity_ids
        if hass.states.is_state(entity_id, STATE_UNAVAILABLE)
    ] == []

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert _entry_state(mock_config_entry) is ConfigEntryState.NOT_LOADED
    # The entity registry keeps the rows, so every live entity is left behind as
    # an unavailable placeholder rather than a value nobody is updating.
    assert [
        entity_id
        for entity_id in live_entity_ids
        if not hass.states.is_state(entity_id, STATE_UNAVAILABLE)
    ] == []
    assert _live_tasks() == []
    assert _aquahome_errors(caplog) == []


# ---------------------------------------------------------------------------
# Reload
# ---------------------------------------------------------------------------


async def test_reload_restores_the_persisted_live_opt_in(  # noqa: PLR0913 - one fixture per faked subsystem
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Switching smart windows on persists it, and a reload brings it back on.

    The live tier has no options flow: the switch itself is the only way the
    flag is written, so the round trip through ``entry.options`` and back into a
    freshly seeded manager is what makes the opt-in survive a restart.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)

    smart_windows = _entity_id(entity_registry, Platform.SWITCH, "smart_live_windows")
    assert hass.states.is_state(smart_windows, STATE_OFF)

    await hass.services.async_call(
        Platform.SWITCH,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: smart_windows},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert hass.states.is_state(smart_windows, STATE_ON)
    # Written straight through to the entry: nothing else persists this flag.
    assert _stored_live_config(mock_config_entry)["smart_windows"] is True

    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await _settle(hass)

    assert _entry_state(mock_config_entry) is ConfigEntryState.LOADED
    assert _stored_live_config(mock_config_entry)["smart_windows"] is True
    # The restarted manager seeds from those options, and the switch — a view
    # onto it — renders the opt-in the owner made before the restart.
    assert _manager(mock_config_entry).state.config.smart_windows is True
    assert hass.states.is_state(smart_windows, STATE_ON)

    # Arming a smart window is a promise to open a session later, never now.
    assert _requested_paths(mock_api, "/live") == []
    assert _aquahome_errors(caplog) == []
