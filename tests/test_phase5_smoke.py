"""Cross-cutting Phase-5 smoke test — history backfill inside a real boot.

The dedicated Phase-5 suites drive the row algorithm and the statistics
coordinator in isolation. This file, like :mod:`tests.test_phase3_smoke` and
:mod:`tests.test_phase4_smoke`, boots the integration exactly as Home Assistant
would — every platform forwarded, a real recorder behind ``recorder_mock``, the
captured cloud payloads behind ``aioresponses`` — and checks that the whole
thing hangs together:

* the entry loads with a statistics coordinator per device, and its background
  first run really lands rows in the recorder (405 of them, the independently
  computed merge of the captured daily and hourly meter fixtures);
* the older coordinators are undisturbed — the fast poll's entities still hold
  their states while the backfill runs;
* unloading is clean: the entry goes ``NOT_LOADED`` and the 12-hour cadence is
  disarmed, so a later time jump produces no further cloud traffic.

The datapoint routes reproduce the reference device's retention: hourly windows
inside the captured July return real readings, every older window returns the
zero-filled "no data" shape that stops the walk-backward probe.

A note on pacing. :data:`~custom_components.aquahome.const.BACKFILL_REQUEST_PACING_SECONDS`
is patched to zero here because the event loop's own clock is frozen with
everything else: a genuine ``asyncio.sleep`` would never wake, so the paced
session would deadlock rather than merely be slow. Pacing is asserted where it
can be observed, in the statistics-coordinator suite; this file only cares that
the session completes.

The remaining tests pin the ``PARALLEL_UPDATES`` contract across all eight
platforms — read-only platforms unlimited, every writing platform serialised
against the throttled cloud.
"""

from __future__ import annotations

from datetime import timedelta
from types import ModuleType
from typing import TYPE_CHECKING, Any, Final, cast
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    statistics_during_period,
)

from custom_components.aquahome import (
    binary_sensor,
    button,
    event,
    number,
    select,
    sensor,
    switch,
    valve,
)
from custom_components.aquahome.const import (
    DOMAIN,
    PLATFORMS,
    STATISTICS_UPDATE_INTERVAL,
)
from custom_components.aquahome.coordinator import AquaHomeRuntimeData
from custom_components.aquahome.statistics import statistic_id_for
from tests.conftest import (
    TEST_DEVICE_ID,
    add_datapoint_graph_routes,
    add_device_routes,
    load_fixture,
    setup_integration,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from aioresponses import aioresponses
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.components.recorder.core import Recorder
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

#: Slug derived from the fixture serial ``4213377-30105-2242`` (see entity.py).
SLUG = "4213377_30105_2242"

#: The instant every clock in this module reads. 12:30 Europe/Warsaw — the
#: device's own zone — so the whole captured history lies in the past.
FROZEN_NOW = "2026-07-27T10:30:00+00:00"

# ---------------------------------------------------------------------------
# Expected import, computed independently from the fixtures (import algorithm)
#
#   graph-meter-hourly.json  300 nonzero readings over 26 local July days
#   graph-meter-daily.json   131 nonzero readings from 2025-09-14
# A local day with hourly coverage is imported hour by hour, so the 26 July days
# contribute their 300 hourly rows and the remaining 105 daily readings one row
# each: 405 rows. The first is the baseline (no water attributed to it) and the
# last carries the whole imported volume, 5567.95 gal == 21077 L, which is what
# the yearly fixture reports for 2025 + 2026 combined.
# ---------------------------------------------------------------------------
EXPECTED_ROWS: Final = 405
#: Native gallons from the cloud; the series is stored in the unit the
#: installation reads, which is metric on a default test instance.
EXPECTED_FIRST_STATE: Final = 42122.7621
EXPECTED_LAST_STATE: Final = 47690.7164
EXPECTED_TOTAL_GALLONS: Final = 5567.9543
GAL_TO_L: Final = 3.785411784

#: ``PARALLEL_UPDATES`` every platform module must declare. Zero on the
#: read-only platforms (they only render coordinator data, so there is nothing
#: to serialise); one everywhere a service call reaches the throttled cloud.
EXPECTED_PARALLEL_UPDATES: Final[dict[Platform, int]] = {
    Platform.BINARY_SENSOR: 0,
    Platform.EVENT: 0,
    Platform.SENSOR: 0,
    Platform.BUTTON: 1,
    Platform.NUMBER: 1,
    Platform.SELECT: 1,
    Platform.SWITCH: 1,
    Platform.VALVE: 1,
}

#: The module implementing each forwarded platform.
PLATFORM_MODULES: Final[dict[Platform, ModuleType]] = {
    Platform.BINARY_SENSOR: binary_sensor,
    Platform.EVENT: event,
    Platform.SENSOR: sensor,
    Platform.BUTTON: button,
    Platform.NUMBER: number,
    Platform.SELECT: select,
    Platform.SWITCH: switch,
    Platform.VALVE: valve,
}


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


# ---------------------------------------------------------------------------
# Route + request helpers
# ---------------------------------------------------------------------------


def _hourly_payload(query: Mapping[str, str]) -> dict[str, Any]:
    """Return the hourly graph payload for one requested window.

    Mirrors the reference device's ~130-day hourly retention: a window inside
    the captured July serves real readings, anything older serves the zero-filled
    shape the cloud really returns past the retention floor (which is what stops
    the coordinator walking backwards for ever).
    """
    if query.get("start", "").startswith("2026-07"):
        return load_fixture("graph-meter-hourly.json")
    return load_fixture("graph-meter-hourly-empty.json")


def _add_backfill_routes(mock: aioresponses) -> None:
    """Register every cloud route a full Phase-5 boot hits."""
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


def _graph_request_count(mock: aioresponses) -> int:
    """Return how many datapoint-graph requests have been intercepted."""
    return sum(
        len(calls)
        for (method, url), calls in mock.requests.items()
        if method == "GET" and url.path.endswith("/graph")
    )


def _entry_state(entry: MockConfigEntry) -> ConfigEntryState:
    """Return the entry's current lifecycle state.

    Read through a call so each assertion compares the state as it is at that
    point rather than as the type checker last narrowed it.
    """
    return cast("ConfigEntryState", entry.state)


def _runtime(entry: MockConfigEntry) -> AquaHomeRuntimeData:
    """Return the entry's runtime data, narrowed for the type checker."""
    runtime = entry.runtime_data
    assert isinstance(runtime, AquaHomeRuntimeData)
    return runtime


def _state(hass: HomeAssistant, registry: er.EntityRegistry, key: str) -> str:
    """Return the state of the sensor whose unique id ends in ``key``."""
    entity_id = registry.async_get_entity_id(Platform.SENSOR, DOMAIN, f"{SLUG}_{key}")
    assert entity_id is not None, f"sensor {key} was not registered"
    state = hass.states.get(entity_id)
    assert state is not None, f"sensor {key} has no state"
    return state.state


async def _stored_rows(hass: HomeAssistant, statistic_id: str) -> list[dict[str, Any]]:
    """Return the imported statistics rows, read on the recorder executor."""
    # A window that predates every captured reading, minted off the frozen clock
    # so nothing here depends on the wall clock.
    window_start = dt_util.utcnow() - timedelta(days=3 * 365)
    stored: dict[str, list[dict[str, Any]]] = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        window_start,
        None,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return stored.get(statistic_id, [])


# ---------------------------------------------------------------------------
# Full boot: every platform, a real recorder, one paced backfill session
# ---------------------------------------------------------------------------


async def test_full_boot_backfills_statistics_and_unloads_cleanly(  # noqa: PLR0913 - one fixture per faked subsystem
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Boot everything, let the backfill land, then unload without leftovers."""
    freezer.move_to(FROZEN_NOW)
    _add_backfill_routes(mock_api)

    with patch(
        "custom_components.aquahome.statistics.BACKFILL_REQUEST_PACING_SECONDS", 0
    ):
        assert await setup_integration(hass, mock_config_entry)
        # The first backfill is a background task deliberately kept off the
        # setup path, so settling it needs the background-aware wait.
        await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    assert _entry_state(mock_config_entry) is ConfigEntryState.LOADED

    runtime = _runtime(mock_config_entry)
    assert TEST_DEVICE_ID in runtime.statistics_coordinators
    statistics = runtime.statistics_coordinators[TEST_DEVICE_ID]
    assert statistics.last_update_success is True
    assert statistics.statistic_id == statistic_id_for(SLUG)

    # -- The history really reached the recorder ------------------------------
    rows = await _stored_rows(hass, statistics.statistic_id)
    assert len(rows) == EXPECTED_ROWS
    # The baseline row attributes no water: what it counts was consumed before
    # the series existed.
    assert rows[0]["state"] == pytest.approx(EXPECTED_FIRST_STATE * GAL_TO_L, abs=1e-3)
    assert rows[0]["sum"] == pytest.approx(0.0, abs=1e-9)
    assert rows[-1]["state"] == pytest.approx(EXPECTED_LAST_STATE * GAL_TO_L, abs=1e-3)
    assert rows[-1]["sum"] == pytest.approx(EXPECTED_TOTAL_GALLONS * GAL_TO_L, abs=1e-3)
    # Meter readings are a lifetime counter: the imported states never go back.
    states = [row["state"] for row in rows]
    assert states == sorted(states)

    # -- The rest of the integration is untouched by the backfill -------------
    assert TEST_DEVICE_ID in runtime.coordinators
    assert TEST_DEVICE_ID in runtime.activity_coordinators
    assert TEST_DEVICE_ID in runtime.settings_coordinators
    assert _state(hass, entity_registry, "regeneration_status") == "none"

    # -- Clean unload: no scheduled backfill survives the entry ---------------
    graph_requests = _graph_request_count(mock_api)
    assert graph_requests > 0

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id) is True
    await hass.async_block_till_done()
    assert _entry_state(mock_config_entry) is ConfigEntryState.NOT_LOADED

    freezer.tick(STATISTICS_UPDATE_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _graph_request_count(mock_api) == graph_requests

    # Drain the states the unload just wrote, so the recorder thread is idle
    # before the fixtures tear it down.
    await async_wait_recording_done(hass)


# ---------------------------------------------------------------------------
# PARALLEL_UPDATES contract (deferred Silver item, Phase 5)
# ---------------------------------------------------------------------------


def test_every_forwarded_platform_declares_parallel_updates() -> None:
    """The contract covers exactly the platforms the entry forwards."""
    assert set(EXPECTED_PARALLEL_UPDATES) == set(PLATFORMS)
    assert set(PLATFORM_MODULES) == set(PLATFORMS)


@pytest.mark.parametrize(
    ("platform", "expected"), list(EXPECTED_PARALLEL_UPDATES.items())
)
def test_platform_declares_its_parallel_updates(
    platform: Platform, expected: int
) -> None:
    """Each platform module declares the update concurrency it is entitled to."""
    declared: int = PLATFORM_MODULES[platform].PARALLEL_UPDATES
    assert declared == expected
