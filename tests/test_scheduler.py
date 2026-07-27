"""Tests for the per-device regeneration scheduler (``scheduler.py``).

The scheduler is the only place in the integration that turns an analytics
verdict into a device command, so everything it does is asserted here against
its two real inputs and its three real outputs. The inputs are driven exactly
as production drives them — crafted
:class:`~custom_components.aquahome.analytics.model.AnalyticsResult` values
published through ``engine.async_set_updated_data`` and modified device-detail
payloads published through the fast coordinator's ``async_set_updated_data`` —
and the outputs are read where a user or an automation would see them: the
``PUT /devices/{id}/command`` bodies recorded by ``aioresponses``, the
``aquahome_event`` bus payloads, and ``AutomationState.last_decision``.

What each group pins:

* the **decision matrix** — every ordered ``skipped_*`` literal is reachable and
  is the *first* unmet condition, plus the as-built recharge-ready fallback
  rules of amendment A3 (tile wins when it names a state, the ``regeneration``
  block stands in when the tile is absent, neither block means "not ready");
* the **nightly schedule** — one command per device-*local* day at capacity
  below tomorrow's forecast times
  :data:`~custom_components.aquahome.const.FORECAST_RESERVE_FACTOR`, with the
  exact event payload, the strict ``<`` at the reserve boundary, and the A3
  ordering rule that the day latch is checked *before* the capacity comparison;
* the **deferral enforcement** — the budget-free first cancel, the three-cancel
  daily budget, and the resin-hygiene cap that announces itself once and then
  deliberately lets the device's regeneration through;
* the **catch-up** on a deferral ending with a nearly exhausted resin bed, and
  its silence (an untouched ``last_decision``) when the capacity still covers
  the forecast;
* the **auto-vacation follower**, including the two rules that make it safe: a
  manual deferral is never auto-released and an unassessable (``None``) verdict
  moves nothing in either direction;
* **persistence** — the opt-ins and the deferral bookkeeping survive a
  scheduler rebuilt from the same entry while the runtime-only decision resets,
  and writing them never reloads the entry.

Time is frozen at 23:30 UTC, which is 01:30 the *next* day in the device's own
``Europe/Warsaw`` zone. That offset is deliberate: every local-day assertion
here would pass against a UTC day too if the two coincided, so the fixtures put
a UTC midnight inside a single device-local day and a device-local midnight
inside a single UTC day.

Recorder-free: no statistics are imported, so the engine's own boot pass reads
an empty series and every crafted result below is the only verdict the
scheduler ever sees. No entity platform is forwarded — the scheduler is built
in ``__init__`` and owns no entity.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from aioresponses import CallbackResult
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_ACCESS_TOKEN
from homeassistant.core import callback
from yarl import URL

from custom_components.aquahome.analytics.model import (
    SOURCE_DEVICE_AVERAGE,
    AnalyticsResult,
    AnomalyState,
    ForecastState,
    GridSummary,
    LeakState,
    VacationState,
)
from custom_components.aquahome.api import Device
from custom_components.aquahome.api.const import API_BASE_URL
from custom_components.aquahome.automation_state import (
    AutomationState,
    state_from_options,
)
from custom_components.aquahome.const import (
    DEFERRAL_SOURCE_AUTO,
    DEFERRAL_SOURCE_MANUAL,
    EVENT_AQUAHOME,
    EVENT_TYPE_REGEN_DEFERRAL_EXPIRED,
    EVENT_TYPE_REGEN_DEFERRED,
    EVENT_TYPE_REGEN_SCHEDULED,
    FORECAST_RESERVE_FACTOR,
    OPTION_AUTOMATION,
    RECHARGE_STATE_READY,
    RECHARGE_STATE_SCHEDULED,
    REGEN_CANCEL_DAILY_BUDGET,
    REGEN_DEFERRAL_MAX_DAYS,
    REGEN_REASON_CATCH_UP,
    REGEN_REASON_LOW_CAPACITY,
)
from custom_components.aquahome.scheduler import (
    DECISION_CATCH_UP,
    DECISION_DEFERRAL_EXPIRED,
    DECISION_DEFERRED,
    DECISION_NOT_NEEDED,
    DECISION_SCHEDULED,
    DECISION_SKIPPED_ALREADY_TODAY,
    DECISION_SKIPPED_COMMAND_FAILED,
    DECISION_SKIPPED_DEFERRAL,
    DECISION_SKIPPED_NO_CAPACITY,
    DECISION_SKIPPED_NO_FORECAST,
    DECISION_SKIPPED_NOT_ALLOWED,
    DECISION_SKIPPED_NOT_READY,
    DECISION_SKIPPED_OFF,
    DECISION_SKIPPED_OFFLINE,
    DECISION_SKIPPED_VACATION,
    AquaHomeRegenScheduler,
)
from tests.conftest import (
    TEST_DEVICE_ID,
    add_device_routes,
    command_url,
    load_fixture,
    make_access_token,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aioresponses import aioresponses
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import Event, HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

#: Slug derived from the fixture serial ``7384243-20203-1120`` (see entity.py).
SLUG = "7384243_20203_1120"

#: The zone the captured device reports through its ``tz_id`` property.
DEVICE_TZ = ZoneInfo("Europe/Warsaw")

#: 23:30 UTC = 01:30 the next day in the device's zone. Everything below is
#: minted relative to this instant; nothing reads the wall clock.
FROZEN_NOW = datetime(2026, 7, 21, 23, 30, tzinfo=UTC)

#: One hour on: a new UTC day (2026-07-22) but still the same device-local day
#: (2026-07-22 02:30 Warsaw) — a UTC-keyed latch would wrongly re-arm here.
SAME_LOCAL_DAY = datetime(2026, 7, 22, 0, 30, tzinfo=UTC)

#: 23 hours on: the next device-local day (2026-07-23 00:30 Warsaw) while the
#: UTC day (2026-07-22) has not turned over since :data:`SAME_LOCAL_DAY`.
NEXT_LOCAL_DAY = datetime(2026, 7, 22, 22, 30, tzinfo=UTC)

#: Tomorrow's forecast every crafted result carries, in gallons.
FORECAST_GALLONS = 100.0

#: Capacity comfortably above the reserved forecast (no action warranted).
CAPACITY_AMPLE = 500.0

#: Capacity exactly on the reserve line: the comparison is strict ``<``, so
#: this must still read as "covered".
CAPACITY_BOUNDARY = FORECAST_GALLONS * FORECAST_RESERVE_FACTOR

#: Capacity one gallon under the reserve line — the scheduling condition.
CAPACITY_LOW = CAPACITY_BOUNDARY - 1.0

#: ``regeneration_status`` value meaning a recharge is running right now.
REGENERATING = "regenerating"

#: The two command bodies the automation tier is allowed to send.
SCHEDULE_COMMAND = {"function": "regenerate", "action": "schedule"}
CANCEL_COMMAND = {"function": "regenerate", "action": "cancel"}


# ---------------------------------------------------------------------------
# Crafted analytics results
#
# One neutral value per state dataclass (the shape a pass with nothing to
# assess produces) plus a factory that overrides exactly the two blocks the
# scheduler reads. Nothing here touches the detectors, so a threshold change
# can never break a scheduling assertion.
# ---------------------------------------------------------------------------

NEUTRAL_LEAK = LeakState(
    active=None,
    consecutive_nights=0,
    rate_liters_per_hour=None,
    implied_liters_per_day=None,
    tier=None,
    persistent_flow=False,
    last_verdict_night=None,
    masking_coverage=True,
)
NEUTRAL_ANOMALY = AnomalyState(
    active=None,
    reasons=(),
    day=None,
    point_hours=0,
    drift_alarm=False,
    drift_cusum=False,
    drift_ewma=False,
)
NEUTRAL_GRID = GridSummary(
    active_hours=(False,) * 168, mature_buckets=0, hourly_samples=0
)


def _forecast(gallons: float | None) -> ForecastState:
    """Return a forecast state carrying ``gallons`` for tomorrow."""
    return ForecastState(
        gallons=gallons,
        liters=None if gallons is None else round(gallons * 3.785, 1),
        source=None if gallons is None else SOURCE_DEVICE_AVERAGE,
        band_liters=None,
        weekday=None,
        persons=None,
    )


def _result(
    *,
    forecast_gallons: float | None = FORECAST_GALLONS,
    vacation_active: bool | None = None,
    consecutive_days: int = 0,
) -> AnalyticsResult:
    """Assemble one crafted analytics pass from neutral defaults.

    Only the two blocks the scheduler consumes — the forecast and the vacation
    verdict — are parameterised; everything else stays at the "nothing to
    assess" shape so no assertion here depends on detector numerics.
    """
    return AnalyticsResult(
        computed_at=FROZEN_NOW,
        nights=(),
        days=(),
        leak=NEUTRAL_LEAK,
        anomaly=NEUTRAL_ANOMALY,
        vacation=VacationState(
            active=vacation_active, consecutive_days=consecutive_days, since=None
        ),
        forecast=_forecast(forecast_gallons),
        grid=NEUTRAL_GRID,
    )


# ---------------------------------------------------------------------------
# Crafted device payloads
# ---------------------------------------------------------------------------


def _detail(  # noqa: PLR0913 - one keyword per payload field the scheduler reads
    *,
    capacity: float | None = CAPACITY_AMPLE,
    tile: bool = True,
    tile_state: str | None = RECHARGE_STATE_READY,
    regeneration: bool = True,
    regeneration_status: str | None = "none",
    can_schedule: bool = True,
    online: bool = True,
) -> dict[str, Any]:
    """Return a device-detail payload with the scheduler's inputs set.

    ``load_fixture`` re-parses the JSON on every call, so each payload built
    here is an independent document and the fixture file is never mutated.
    ``tile`` / ``regeneration`` drop the corresponding enriched block entirely,
    which is how the amendment-A3 recharge-ready fallback rules are exercised;
    ``capacity`` of ``None`` removes the raw property a device that does not
    report its remaining capacity would omit.
    """
    detail = load_fixture("device-detail.json")
    treatment = detail["enriched_data"]["water_treatment"]
    if tile:
        treatment["recharge_ui"]["state"] = tile_state
        treatment["recharge_ui"]["can_schedule"] = can_schedule
    else:
        treatment.pop("recharge_ui", None)
    if regeneration:
        treatment["regeneration"]["regeneration_status"] = regeneration_status
        treatment["regeneration"]["can_schedule"] = can_schedule
    else:
        treatment.pop("regeneration", None)
    if capacity is None:
        detail["properties"].pop("treated_water_avail_gals", None)
    else:
        detail["properties"]["treated_water_avail_gals"]["value"] = capacity
    detail["is_online"] = online
    return detail


# ---------------------------------------------------------------------------
# Fixtures, boot and drive helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_platforms() -> Iterator[None]:
    """Forward no entity platforms: the scheduler is built in ``__init__``."""
    with patch("custom_components.aquahome.PLATFORMS", []):
        yield


def _refresh_url() -> re.Pattern[str]:
    """Match the ``POST /auth/refresh`` URL the client rotates tokens on."""
    return re.compile(rf"^{re.escape(API_BASE_URL)}/auth/refresh(\?.*)?$")


async def _boot(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock: aioresponses,
    freezer: FrozenDateTimeFactory,
    *,
    detail: dict[str, Any] | None = None,
) -> None:
    """Freeze the clock, set the entry up and settle the startup pipeline.

    The stored access token is re-minted against the frozen clock, which has to
    happen between adding the entry and setting it up — so the shared
    ``setup_integration`` helper is unrolled here. A token-refresh route is
    registered because the deferral tests jump the clock past the 24 h access
    token's life and still expect their cancel command to reach the cloud. The
    success command route is registered *last*, so a test that wants a failure
    registers its own (single-shot) route before calling this and gets the
    failure first, exactly as ``aioresponses`` orders registrations. The engine
    pass runs as an entry background task, hence the background-aware wait.

    Once the boot has settled, every cadence this suite does not drive itself is
    stood down — see :func:`_quiesce_cadences`.
    """
    freezer.move_to(FROZEN_NOW)
    add_device_routes(mock, device_detail=detail if detail is not None else _detail())

    def _rotate(_url: URL, **_kwargs: Any) -> CallbackResult:
        """Mint a token pair that is fresh against the *current* frozen clock."""
        return CallbackResult(
            payload={
                "access_token": make_access_token(),
                "refresh_token": "refresh-token-2",
            }
        )

    mock.post(_refresh_url(), callback=_rotate, repeat=True)
    mock.put(command_url(), payload={"result": "ok"}, repeat=True)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_ACCESS_TOKEN: make_access_token()}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await _quiesce_cadences(hass, entry)


async def _quiesce_cadences(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Stand down every timed refresh the integration armed during setup.

    Home Assistant binds the event loop's clock to ``time.monotonic``, which
    freezegun patches — so moving the frozen clock forward fires every armed
    timer just as ``async_fire_time_changed`` would. The local-day and
    resin-hygiene assertions below jump hours and weeks, and a telemetry poll or
    the engine's 07:35 run landing inside such a jump would replace a crafted
    input in the middle of an assertion (whichever task the loop happened to run
    last would decide the verdict). This suite publishes both of the scheduler's
    inputs explicitly, so nothing here needs a cadence: the sibling coordinators
    are shut down, the engine's daily trigger is cancelled, and the fast
    coordinator keeps serving manual updates with its polling turned off — one
    republish clears the timer setup armed, and with no interval it is never
    re-armed.
    """
    runtime = entry.runtime_data
    runtime.analytics_engines[TEST_DEVICE_ID].async_cancel_schedule()
    for coordinator in (
        runtime.activity_coordinators[TEST_DEVICE_ID],
        runtime.settings_coordinators[TEST_DEVICE_ID],
        runtime.statistics_coordinators[TEST_DEVICE_ID],
    ):
        await coordinator.async_shutdown()
    fast = runtime.coordinators[TEST_DEVICE_ID]
    fast.update_interval = None
    fast.async_set_updated_data(fast.data)
    await hass.async_block_till_done()


def _scheduler(entry: MockConfigEntry) -> AquaHomeRegenScheduler:
    """Return the scheduler the entry built for the fixture device."""
    scheduler: AquaHomeRegenScheduler = entry.runtime_data.schedulers[TEST_DEVICE_ID]
    return scheduler


async def _push_device(
    hass: HomeAssistant, entry: MockConfigEntry, detail: dict[str, Any]
) -> None:
    """Publish a device view on the fast coordinator and settle the listeners."""
    entry.runtime_data.coordinators[TEST_DEVICE_ID].async_set_updated_data(
        Device.from_dict(detail)
    )
    await hass.async_block_till_done()


async def _push_result(
    hass: HomeAssistant, entry: MockConfigEntry, result: AnalyticsResult
) -> None:
    """Publish an analytics verdict on the engine and settle the listeners."""
    entry.runtime_data.analytics_engines[TEST_DEVICE_ID].async_set_updated_data(result)
    await hass.async_block_till_done()


def _commands(mock: aioresponses) -> list[dict[str, Any]]:
    """Return every ``/command`` body PUT so far, in order."""
    calls = mock.requests.get(("PUT", URL(command_url())), [])
    bodies: list[dict[str, Any]] = [call.kwargs["json"] for call in calls]
    return bodies


def _events(hass: HomeAssistant, event_type: str) -> list[dict[str, Any]]:
    """Start collecting ``aquahome_event`` payloads of one type.

    Returns the (initially empty) list the listener appends to, so a test reads
    it after acting. The listener is a plain ``@callback`` — bus listeners must
    never be coroutines.
    """
    seen: list[dict[str, Any]] = []

    @callback
    def _collect(event: Event[dict[str, Any]]) -> None:
        """Record one automation-tier event of the requested type."""
        if event.data.get("type") == event_type:
            seen.append(dict(event.data))

    hass.bus.async_listen(EVENT_AQUAHOME, _collect)
    return seen


# ---------------------------------------------------------------------------
# The premise the local-day assertions rest on
# ---------------------------------------------------------------------------


def test_the_frozen_instants_separate_the_utc_and_device_calendars() -> None:
    """The three instants really do straddle the two calendars as documented.

    Every once-per-day assertion below is only meaningful because the device's
    day and UTC's day disagree here: a latch keyed on the wrong zone has to
    change an observable outcome. If a future edit moved these instants somewhere
    the two calendars coincide, the suite would keep passing while testing
    nothing — so the property is pinned rather than left to the comments.
    """
    assert FROZEN_NOW.date() != FROZEN_NOW.astimezone(DEVICE_TZ).date()

    # A UTC midnight inside one device-local day.
    assert FROZEN_NOW.date() != SAME_LOCAL_DAY.date()
    assert (
        FROZEN_NOW.astimezone(DEVICE_TZ).date()
        == SAME_LOCAL_DAY.astimezone(DEVICE_TZ).date()
    )

    # A device-local midnight inside one UTC day.
    assert SAME_LOCAL_DAY.date() == NEXT_LOCAL_DAY.date()
    assert (
        SAME_LOCAL_DAY.astimezone(DEVICE_TZ).date()
        != NEXT_LOCAL_DAY.astimezone(DEVICE_TZ).date()
    )


# ---------------------------------------------------------------------------
# The decision matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("smart", "deferral", "vacation_active", "detail_kwargs", "forecast", "expected"),
    [
        pytest.param(
            False, False, None, {}, FORECAST_GALLONS, DECISION_SKIPPED_OFF, id="off"
        ),
        pytest.param(
            True,
            True,
            None,
            {},
            FORECAST_GALLONS,
            DECISION_SKIPPED_DEFERRAL,
            id="deferral",
        ),
        pytest.param(
            True,
            False,
            True,
            {},
            FORECAST_GALLONS,
            DECISION_SKIPPED_VACATION,
            id="vacation",
        ),
        pytest.param(
            True,
            False,
            None,
            {"online": False},
            FORECAST_GALLONS,
            DECISION_SKIPPED_OFFLINE,
            id="offline",
        ),
        pytest.param(
            True,
            False,
            None,
            {"can_schedule": False},
            FORECAST_GALLONS,
            DECISION_SKIPPED_NOT_ALLOWED,
            id="not_allowed",
        ),
        pytest.param(
            True,
            False,
            None,
            {"tile_state": RECHARGE_STATE_SCHEDULED},
            FORECAST_GALLONS,
            DECISION_SKIPPED_NOT_READY,
            id="not_ready",
        ),
        pytest.param(
            True,
            False,
            None,
            {"capacity": CAPACITY_LOW},
            None,
            DECISION_SKIPPED_NO_FORECAST,
            id="no_forecast",
        ),
        pytest.param(
            True,
            False,
            None,
            {"capacity": None},
            FORECAST_GALLONS,
            DECISION_SKIPPED_NO_CAPACITY,
            id="no_capacity",
        ),
        pytest.param(
            True,
            False,
            None,
            {"capacity": CAPACITY_AMPLE},
            FORECAST_GALLONS,
            DECISION_NOT_NEEDED,
            id="not_needed",
        ),
        pytest.param(
            True,
            False,
            None,
            {"capacity": CAPACITY_LOW},
            FORECAST_GALLONS,
            DECISION_SCHEDULED,
            id="scheduled",
        ),
    ],
)
async def test_decision_matrix_reports_the_first_unmet_condition(  # noqa: PLR0913 - the standard platform fixture set plus one parametrized row
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    smart: bool,
    deferral: bool,
    vacation_active: bool | None,
    detail_kwargs: dict[str, Any],
    forecast: float | None,
    expected: str,
) -> None:
    """Every decision literal is reachable, in the contract's reporting order.

    Each row leaves exactly one precondition unmet (later rows satisfy all the
    earlier ones), so the recorded verdict names precisely the condition the
    row is about — which is what makes ``last_decision`` a usable explanation
    of a night that passed without action rather than a vague "nothing
    happened".
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)
    scheduler = _scheduler(mock_config_entry)
    if smart:
        await scheduler.async_set_smart_regeneration(True)
    if deferral:
        await scheduler.async_set_vacation_deferral(True, source=DEFERRAL_SOURCE_MANUAL)

    await _push_device(hass, mock_config_entry, _detail(**detail_kwargs))
    await _push_result(
        hass,
        mock_config_entry,
        _result(forecast_gallons=forecast, vacation_active=vacation_active),
    )

    assert scheduler.state.last_decision == expected
    assert scheduler.state.last_decision_at == FROZEN_NOW


@pytest.mark.parametrize(
    ("detail_kwargs", "expected"),
    [
        pytest.param(
            {"tile_state": RECHARGE_STATE_READY}, DECISION_SCHEDULED, id="tile_ready"
        ),
        pytest.param(
            {"tile_state": RECHARGE_STATE_SCHEDULED},
            DECISION_SKIPPED_NOT_READY,
            id="tile_scheduled",
        ),
        pytest.param(
            {"tile_state": REGENERATING},
            DECISION_SKIPPED_NOT_READY,
            id="tile_regenerating",
        ),
        pytest.param(
            {"tile_state": "unknown_to_us"},
            DECISION_SKIPPED_NOT_READY,
            id="tile_unrecognised",
        ),
        pytest.param(
            {"tile": False, "regeneration_status": "none"},
            DECISION_SCHEDULED,
            id="fallback_idle",
        ),
        pytest.param(
            {"tile": False, "regeneration_status": RECHARGE_STATE_SCHEDULED},
            DECISION_SKIPPED_NOT_READY,
            id="fallback_scheduled",
        ),
        pytest.param(
            {"tile": False, "regeneration_status": REGENERATING},
            DECISION_SKIPPED_NOT_READY,
            id="fallback_regenerating",
        ),
        pytest.param(
            {"tile": False, "regeneration": False},
            DECISION_SKIPPED_NOT_READY,
            id="no_source_at_all",
        ),
    ],
)
async def test_recharge_ready_follows_the_as_built_fallback_rules(  # noqa: PLR0913 - the standard platform fixture set plus one parametrized row
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    detail_kwargs: dict[str, Any],
    expected: str,
) -> None:
    """Amendment A3's readiness rules decide whether a command may be sent.

    The ``recharge_ui`` tile is authoritative when it names a state and only its
    explicit ``ready`` counts; without the tile the ``regeneration`` block
    stands in, where anything other than the two busy values is idle (it reads
    ``none`` on a resting device and never names a ready state of its own); and
    with neither block present readiness is ``False``, so an unknown device
    state is never assumed schedulable.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)
    scheduler = _scheduler(mock_config_entry)
    await scheduler.async_set_smart_regeneration(True)

    await _push_device(
        hass, mock_config_entry, _detail(capacity=CAPACITY_LOW, **detail_kwargs)
    )
    await _push_result(hass, mock_config_entry, _result())

    assert scheduler.state.last_decision == expected
    expected_commands = [SCHEDULE_COMMAND] if expected == DECISION_SCHEDULED else []
    assert _commands(mock_api) == expected_commands


# ---------------------------------------------------------------------------
# The nightly schedule
# ---------------------------------------------------------------------------


async def test_low_capacity_schedules_once_per_device_local_day(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """One command per device-local day, with the full announcement payload.

    The second pass falls on a *new UTC day* but the same Warsaw day, so a latch
    keyed on the wrong zone would re-send here; the third pass crosses the
    device-local midnight (while the UTC day does not turn over) and must arm
    again. The event carries the numbers the decision rested on so a companion
    automation can explain itself without re-reading the coordinator.
    """
    scheduled = _events(hass, EVENT_TYPE_REGEN_SCHEDULED)
    await _boot(hass, mock_config_entry, mock_api, freezer)
    scheduler = _scheduler(mock_config_entry)
    await scheduler.async_set_smart_regeneration(True)
    await _push_device(hass, mock_config_entry, _detail(capacity=CAPACITY_LOW))

    await _push_result(hass, mock_config_entry, _result())

    assert _commands(mock_api) == [SCHEDULE_COMMAND]
    assert scheduler.state.last_decision == DECISION_SCHEDULED
    assert scheduled == [
        {
            "device_id": TEST_DEVICE_ID,
            "device": SLUG,
            "type": EVENT_TYPE_REGEN_SCHEDULED,
            "reason": REGEN_REASON_LOW_CAPACITY,
            "capacity_gallons": CAPACITY_LOW,
            "forecast_gallons": FORECAST_GALLONS,
        }
    ]

    # Same device-local day, new UTC day: latched, no second command.
    freezer.move_to(SAME_LOCAL_DAY)
    await _push_result(hass, mock_config_entry, _result())

    assert _commands(mock_api) == [SCHEDULE_COMMAND]
    assert scheduler.state.last_decision == DECISION_SKIPPED_ALREADY_TODAY
    assert len(scheduled) == 1

    # Next device-local day, same UTC day: the latch releases.
    freezer.move_to(NEXT_LOCAL_DAY)
    await _push_result(hass, mock_config_entry, _result())

    assert _commands(mock_api) == [SCHEDULE_COMMAND, SCHEDULE_COMMAND]
    assert scheduler.state.last_decision == DECISION_SCHEDULED
    assert len(scheduled) == 2


async def test_day_latch_is_checked_before_the_capacity_comparison(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A second pass on a latched day reports the latch, not a recovered capacity.

    Amendment A3 fixes the ordering: once a day has scheduled, every further
    pass that day records ``skipped_already_today`` even when the capacity has
    since recovered and the comparison alone would have said ``not_needed``.
    Reporting the latch is the honest answer — the decision was not re-taken.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)
    scheduler = _scheduler(mock_config_entry)
    await scheduler.async_set_smart_regeneration(True)
    await _push_device(hass, mock_config_entry, _detail(capacity=CAPACITY_LOW))
    await _push_result(hass, mock_config_entry, _result())
    assert scheduler.state.last_decision == DECISION_SCHEDULED

    # The recharge refilled the resin bed: capacity is ample again.
    await _push_device(hass, mock_config_entry, _detail(capacity=CAPACITY_AMPLE))
    await _push_result(hass, mock_config_entry, _result())

    assert scheduler.state.last_decision == DECISION_SKIPPED_ALREADY_TODAY
    assert _commands(mock_api) == [SCHEDULE_COMMAND]


async def test_reserve_boundary_is_strict(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Capacity exactly on the reserve line covers tomorrow; one gallon under does not.

    The comparison is ``capacity < forecast x FORECAST_RESERVE_FACTOR``. Landing
    exactly on the line must not command anything — the reserve is already
    satisfied — so the boundary is asserted from both sides in one run, which
    also proves the first pass left no latch behind.
    """
    scheduled = _events(hass, EVENT_TYPE_REGEN_SCHEDULED)
    await _boot(hass, mock_config_entry, mock_api, freezer)
    scheduler = _scheduler(mock_config_entry)
    await scheduler.async_set_smart_regeneration(True)

    await _push_device(hass, mock_config_entry, _detail(capacity=CAPACITY_BOUNDARY))
    await _push_result(hass, mock_config_entry, _result())

    assert scheduler.state.last_decision == DECISION_NOT_NEEDED
    assert _commands(mock_api) == []
    assert scheduled == []

    await _push_device(hass, mock_config_entry, _detail(capacity=CAPACITY_LOW))
    await _push_result(hass, mock_config_entry, _result())

    assert scheduler.state.last_decision == DECISION_SCHEDULED
    assert _commands(mock_api) == [SCHEDULE_COMMAND]
    assert len(scheduled) == 1


async def test_failed_schedule_command_is_recorded_and_retried(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A rejected schedule records the failure, announces nothing and leaves no latch.

    Nobody is standing in front of the device at 07:35, so a failed command must
    never surface as an exception — and it must never latch the day either, or
    a single cloud hiccup would silently cost the household a regeneration.
    """
    mock_api.put(command_url(), status=422, payload={"detail": "no"})
    scheduled = _events(hass, EVENT_TYPE_REGEN_SCHEDULED)
    await _boot(hass, mock_config_entry, mock_api, freezer)
    scheduler = _scheduler(mock_config_entry)
    await scheduler.async_set_smart_regeneration(True)
    await _push_device(hass, mock_config_entry, _detail(capacity=CAPACITY_LOW))

    await _push_result(hass, mock_config_entry, _result())

    assert scheduler.state.last_decision == DECISION_SKIPPED_COMMAND_FAILED
    assert _commands(mock_api) == [SCHEDULE_COMMAND]
    assert scheduled == []

    # Same device-local day: unlatched, so the next pass simply tries again.
    await _push_result(hass, mock_config_entry, _result())

    assert scheduler.state.last_decision == DECISION_SCHEDULED
    assert _commands(mock_api) == [SCHEDULE_COMMAND, SCHEDULE_COMMAND]
    assert len(scheduled) == 1


# ---------------------------------------------------------------------------
# Deferral enforcement on the fast telemetry
# ---------------------------------------------------------------------------


async def test_deferral_cancels_a_regeneration_the_device_schedules(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """While deferred, a telemetry poll showing ``scheduled`` sends one cancel.

    Starting the deferral against a resting device commands nothing — there is
    nothing to cancel yet — which is what makes the later cancel attributable
    to the enforcement path rather than to the deferral taking effect.
    """
    deferred = _events(hass, EVENT_TYPE_REGEN_DEFERRED)
    await _boot(hass, mock_config_entry, mock_api, freezer)
    scheduler = _scheduler(mock_config_entry)

    await scheduler.async_set_vacation_deferral(True, source=DEFERRAL_SOURCE_MANUAL)

    assert _commands(mock_api) == []
    assert scheduler.state.vacation_deferral is True
    assert scheduler.state.deferral_source == DEFERRAL_SOURCE_MANUAL
    assert scheduler.state.deferral_started == FROZEN_NOW
    assert scheduler.state.last_decision == DECISION_DEFERRED

    await _push_device(
        hass, mock_config_entry, _detail(tile_state=RECHARGE_STATE_SCHEDULED)
    )

    assert _commands(mock_api) == [CANCEL_COMMAND]
    assert deferred == [
        {
            "device_id": TEST_DEVICE_ID,
            "device": SLUG,
            "type": EVENT_TYPE_REGEN_DEFERRED,
            "deferral_source": DEFERRAL_SOURCE_MANUAL,
        }
    ]
    assert scheduler.state.last_decision == DECISION_DEFERRED


async def test_deferral_enforcement_reads_the_regeneration_fallback(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A device without the recharge tile is still enforced against.

    Both payload vocabularies name the scheduled state identically, so a host
    that only publishes the enriched ``regeneration`` block gets the same
    deferral treatment as one with the tile.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)
    scheduler = _scheduler(mock_config_entry)
    await scheduler.async_set_vacation_deferral(True, source=DEFERRAL_SOURCE_MANUAL)

    await _push_device(
        hass,
        mock_config_entry,
        _detail(tile=False, regeneration_status=RECHARGE_STATE_SCHEDULED),
    )

    assert _commands(mock_api) == [CANCEL_COMMAND]


async def test_first_cancel_of_a_deferral_ignores_the_daily_budget(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Starting a deferral on an already-scheduled device spends no budget.

    That first cancel is the deferral taking effect, not the recurring fight the
    budget caps — so a full budget of three enforcement cancels must still be
    available afterwards, and only the fourth poll goes unanswered.
    """
    await _boot(
        hass,
        mock_config_entry,
        mock_api,
        freezer,
        detail=_detail(tile_state=RECHARGE_STATE_SCHEDULED),
    )
    scheduler = _scheduler(mock_config_entry)

    await scheduler.async_set_vacation_deferral(True, source=DEFERRAL_SOURCE_MANUAL)
    assert _commands(mock_api) == [CANCEL_COMMAND]

    for _ in range(REGEN_CANCEL_DAILY_BUDGET + 1):
        await _push_device(
            hass, mock_config_entry, _detail(tile_state=RECHARGE_STATE_SCHEDULED)
        )

    assert _commands(mock_api) == [CANCEL_COMMAND] * (REGEN_CANCEL_DAILY_BUDGET + 1)


async def test_cancel_budget_stops_a_command_fight_and_rolls_over(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """At most three enforcement cancels per device-local day, then silence.

    A device that keeps re-scheduling must never be answered indefinitely on a
    throttled cloud. The budget is keyed on the device-local day, so a poll an
    hour later (a new UTC day, the same Warsaw day) is still capped and only the
    device-local rollover restores it.
    """
    deferred = _events(hass, EVENT_TYPE_REGEN_DEFERRED)
    await _boot(hass, mock_config_entry, mock_api, freezer)
    scheduler = _scheduler(mock_config_entry)
    await scheduler.async_set_vacation_deferral(True, source=DEFERRAL_SOURCE_MANUAL)

    for _ in range(REGEN_CANCEL_DAILY_BUDGET + 1):
        await _push_device(
            hass, mock_config_entry, _detail(tile_state=RECHARGE_STATE_SCHEDULED)
        )

    assert _commands(mock_api) == [CANCEL_COMMAND] * REGEN_CANCEL_DAILY_BUDGET
    assert len(deferred) == REGEN_CANCEL_DAILY_BUDGET

    # A new UTC day but the same device-local day: still exhausted.
    freezer.move_to(SAME_LOCAL_DAY)
    await _push_device(
        hass, mock_config_entry, _detail(tile_state=RECHARGE_STATE_SCHEDULED)
    )
    assert _commands(mock_api) == [CANCEL_COMMAND] * REGEN_CANCEL_DAILY_BUDGET

    # The device-local day turns over and the budget comes back.
    freezer.move_to(NEXT_LOCAL_DAY)
    await _push_device(
        hass, mock_config_entry, _detail(tile_state=RECHARGE_STATE_SCHEDULED)
    )
    assert _commands(mock_api) == [CANCEL_COMMAND] * (REGEN_CANCEL_DAILY_BUDGET + 1)


async def test_failed_cancel_is_recorded_without_consuming_budget(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A rejected cancel records the failure and leaves the budget untouched.

    Amendment A3: the budget caps *sent* cancels, so a command the cloud refused
    must not eat one of the three — otherwise a flaky cloud would silently
    disarm the deferral for the rest of the day.
    """
    mock_api.put(command_url(), status=422, payload={"detail": "no"})
    deferred = _events(hass, EVENT_TYPE_REGEN_DEFERRED)
    await _boot(hass, mock_config_entry, mock_api, freezer)
    scheduler = _scheduler(mock_config_entry)
    await scheduler.async_set_vacation_deferral(True, source=DEFERRAL_SOURCE_MANUAL)

    await _push_device(
        hass, mock_config_entry, _detail(tile_state=RECHARGE_STATE_SCHEDULED)
    )

    assert scheduler.state.last_decision == DECISION_SKIPPED_COMMAND_FAILED
    assert _commands(mock_api) == [CANCEL_COMMAND]
    assert deferred == []

    for _ in range(REGEN_CANCEL_DAILY_BUDGET + 1):
        await _push_device(
            hass, mock_config_entry, _detail(tile_state=RECHARGE_STATE_SCHEDULED)
        )

    # The failure cost nothing: three cancels still landed after it.
    assert _commands(mock_api) == [CANCEL_COMMAND] * (REGEN_CANCEL_DAILY_BUDGET + 1)
    assert len(deferred) == REGEN_CANCEL_DAILY_BUDGET
    assert scheduler.state.last_decision == DECISION_DEFERRED


async def test_deferral_past_the_hygiene_cap_announces_once_and_lets_it_through(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Past 21 deferred days the regeneration is allowed, announced exactly once.

    The resin bed must not sit unregenerated indefinitely, so the cap wins over
    the deferral — and it wins *quietly*: one event per deferral, never a nag on
    every poll. The instant is bracketed: at exactly the cap the cancel still
    goes out, and only past it does the announcement replace it.
    """
    expired = _events(hass, EVENT_TYPE_REGEN_DEFERRAL_EXPIRED)
    await _boot(hass, mock_config_entry, mock_api, freezer)
    scheduler = _scheduler(mock_config_entry)
    await scheduler.async_set_vacation_deferral(True, source=DEFERRAL_SOURCE_MANUAL)

    # Exactly at the cap: the age is not yet *past* it, so enforcement holds.
    freezer.move_to(FROZEN_NOW + timedelta(days=REGEN_DEFERRAL_MAX_DAYS))
    await _push_device(
        hass, mock_config_entry, _detail(tile_state=RECHARGE_STATE_SCHEDULED)
    )

    assert _commands(mock_api) == [CANCEL_COMMAND]
    assert expired == []
    assert scheduler.state.last_decision == DECISION_DEFERRED

    # One minute past it: the device keeps its regeneration and says so once.
    freezer.move_to(FROZEN_NOW + timedelta(days=REGEN_DEFERRAL_MAX_DAYS, minutes=1))
    await _push_device(
        hass, mock_config_entry, _detail(tile_state=RECHARGE_STATE_SCHEDULED)
    )

    assert _commands(mock_api) == [CANCEL_COMMAND]
    assert expired == [
        {
            "device_id": TEST_DEVICE_ID,
            "device": SLUG,
            "type": EVENT_TYPE_REGEN_DEFERRAL_EXPIRED,
            "deferral_source": DEFERRAL_SOURCE_MANUAL,
            "days_deferred": REGEN_DEFERRAL_MAX_DAYS,
        }
    ]
    assert scheduler.state.last_decision == DECISION_DEFERRAL_EXPIRED
    assert scheduler.state.vacation_deferral is True

    # Every later poll stays silent: one announcement per deferral.
    await _push_device(
        hass, mock_config_entry, _detail(tile_state=RECHARGE_STATE_SCHEDULED)
    )

    assert _commands(mock_api) == [CANCEL_COMMAND]
    assert len(expired) == 1


# ---------------------------------------------------------------------------
# Catch-up when a deferral ends
# ---------------------------------------------------------------------------


async def test_ending_a_deferral_schedules_a_catch_up_on_low_capacity(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A household returning to a spent resin bed gets a make-up recharge.

    The device only regenerates on its own schedule, so without this the return
    home would draw hard water until the next one. The announcement carries the
    catch-up reason and the same capacity/forecast superset as the nightly
    decision (amendment A3), and the whole path works with smart regeneration
    switched off — it belongs to the deferral, not to the nightly scheduler.
    """
    scheduled = _events(hass, EVENT_TYPE_REGEN_SCHEDULED)
    await _boot(hass, mock_config_entry, mock_api, freezer)
    scheduler = _scheduler(mock_config_entry)
    await _push_device(hass, mock_config_entry, _detail(capacity=CAPACITY_LOW))
    await _push_result(hass, mock_config_entry, _result())
    await scheduler.async_set_vacation_deferral(True, source=DEFERRAL_SOURCE_MANUAL)
    assert _commands(mock_api) == []

    await scheduler.async_set_vacation_deferral(False, source=DEFERRAL_SOURCE_MANUAL)

    assert _commands(mock_api) == [SCHEDULE_COMMAND]
    assert scheduler.state.last_decision == DECISION_CATCH_UP
    assert scheduler.state.vacation_deferral is False
    assert scheduler.state.deferral_source is None
    assert scheduler.state.deferral_started is None
    assert scheduled == [
        {
            "device_id": TEST_DEVICE_ID,
            "device": SLUG,
            "type": EVENT_TYPE_REGEN_SCHEDULED,
            "reason": REGEN_REASON_CATCH_UP,
            "capacity_gallons": CAPACITY_LOW,
            "forecast_gallons": FORECAST_GALLONS,
        }
    ]


async def test_ending_a_deferral_on_ample_capacity_leaves_the_decision_untouched(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """No catch-up, no command and no invented verdict when capacity is fine.

    Amendment A3 pins the silence: turning the deferral off without a catch-up
    leaves ``last_decision`` exactly as the deferral left it, rather than
    fabricating a ``not_needed`` the scheduler never took a decision to record.
    """
    scheduled = _events(hass, EVENT_TYPE_REGEN_SCHEDULED)
    await _boot(hass, mock_config_entry, mock_api, freezer)
    scheduler = _scheduler(mock_config_entry)
    await _push_device(hass, mock_config_entry, _detail(capacity=CAPACITY_AMPLE))
    await _push_result(hass, mock_config_entry, _result())
    await scheduler.async_set_vacation_deferral(True, source=DEFERRAL_SOURCE_MANUAL)
    assert scheduler.state.last_decision == DECISION_DEFERRED

    await scheduler.async_set_vacation_deferral(False, source=DEFERRAL_SOURCE_MANUAL)

    assert _commands(mock_api) == []
    assert scheduled == []
    assert scheduler.state.vacation_deferral is False
    assert scheduler.state.last_decision == DECISION_DEFERRED


async def test_setting_the_deferral_to_its_current_value_does_nothing(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A no-op flip commands nothing and never restarts the deferral clock.

    Blueprints and the service layer both call the setter unconditionally, so an
    idempotent write must not reset ``deferral_started`` — which would silently
    postpone the resin-hygiene cap for ever.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)
    scheduler = _scheduler(mock_config_entry)
    await _push_device(hass, mock_config_entry, _detail(capacity=CAPACITY_LOW))
    await _push_result(hass, mock_config_entry, _result())
    await scheduler.async_set_vacation_deferral(True, source=DEFERRAL_SOURCE_MANUAL)

    freezer.move_to(SAME_LOCAL_DAY)
    await scheduler.async_set_vacation_deferral(True, source=DEFERRAL_SOURCE_AUTO)

    assert scheduler.state.deferral_started == FROZEN_NOW
    assert scheduler.state.deferral_source == DEFERRAL_SOURCE_MANUAL
    assert _commands(mock_api) == []


# ---------------------------------------------------------------------------
# The auto-vacation follower
# ---------------------------------------------------------------------------


async def test_auto_vacation_follows_the_detector_in_both_directions(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """With the follower on, the detector's verdict drives the deferral.

    Detection starts an ``auto`` deferral and the household's return releases it
    again; an unassessable (``None``) verdict in between moves nothing in either
    direction, the same silence rule the detection tier itself follows.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)
    scheduler = _scheduler(mock_config_entry)
    await scheduler.async_set_auto_vacation(True)
    armed = scheduler.state
    assert armed.vacation_deferral is False

    # Nothing to assess: silence.
    await _push_result(hass, mock_config_entry, _result(vacation_active=None))
    unassessed = scheduler.state
    assert unassessed.vacation_deferral is False

    await _push_result(
        hass, mock_config_entry, _result(vacation_active=True, consecutive_days=3)
    )
    away = scheduler.state
    assert away.vacation_deferral is True
    assert away.deferral_source == DEFERRAL_SOURCE_AUTO
    assert away.deferral_started == FROZEN_NOW

    # Still nothing to assess: the standing deferral is left alone.
    await _push_result(hass, mock_config_entry, _result(vacation_active=None))
    held = scheduler.state
    assert held.vacation_deferral is True

    await _push_result(hass, mock_config_entry, _result(vacation_active=False))
    home = scheduler.state
    assert home.vacation_deferral is False
    assert home.deferral_source is None
    assert home.deferral_started is None


async def test_a_manual_deferral_is_never_auto_released(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The follower only ends deferrals it started itself.

    A deferral the user (or a blueprint) asked for is theirs to end: the
    detector deciding the household is home again must not quietly undo it,
    however confident it is.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)
    scheduler = _scheduler(mock_config_entry)
    await scheduler.async_set_vacation_deferral(True, source=DEFERRAL_SOURCE_MANUAL)
    await scheduler.async_set_auto_vacation(True)

    await _push_result(hass, mock_config_entry, _result(vacation_active=False))

    assert scheduler.state.vacation_deferral is True
    assert scheduler.state.deferral_source == DEFERRAL_SOURCE_MANUAL


async def test_enabling_auto_vacation_while_already_away_defers_immediately(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Flipping the follower on mid-vacation applies the deferral at once.

    Otherwise the switch the user just turned on would appear to do nothing
    until the next nightly verdict, which is up to a day away.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)
    scheduler = _scheduler(mock_config_entry)
    await _push_result(
        hass, mock_config_entry, _result(vacation_active=True, consecutive_days=5)
    )
    before = scheduler.state
    assert before.vacation_deferral is False

    await scheduler.async_set_auto_vacation(True)

    after = scheduler.state
    assert after.auto_vacation is True
    assert after.vacation_deferral is True
    assert after.deferral_source == DEFERRAL_SOURCE_AUTO


async def test_the_follower_stays_out_of_it_while_disabled(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A detected vacation defers nothing while the opt-in switch is off.

    Every device-affecting automation is opt-in, so the default-off follower
    must leave the deferral alone no matter how sure the detector is.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)
    scheduler = _scheduler(mock_config_entry)

    await _push_result(
        hass, mock_config_entry, _result(vacation_active=True, consecutive_days=9)
    )

    assert scheduler.state.auto_vacation is False
    assert scheduler.state.vacation_deferral is False
    assert _commands(mock_api) == []


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def test_state_survives_a_scheduler_rebuilt_from_the_same_entry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The opt-ins and the deferral bookkeeping persist; the verdict resets.

    A restart must never forget an opt-in — nor silently restart the
    resin-hygiene clock, which is why ``deferral_started`` is persisted too. The
    scheduler's own verdict is runtime state and has to come back empty rather
    than claim a decision this process never took.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)
    scheduler = _scheduler(mock_config_entry)
    runtime = mock_config_entry.runtime_data
    await scheduler.async_set_smart_regeneration(True)
    await scheduler.async_set_auto_vacation(True)
    await scheduler.async_set_vacation_deferral(True, source=DEFERRAL_SOURCE_MANUAL)
    assert scheduler.state.last_decision == DECISION_DEFERRED

    assert mock_config_entry.options[OPTION_AUTOMATION][TEST_DEVICE_ID] == {
        "vacation_deferral": True,
        "auto_vacation": True,
        "smart_regeneration": True,
        "deferral_source": DEFERRAL_SOURCE_MANUAL,
        "deferral_started": FROZEN_NOW.isoformat(),
    }

    rebuilt = AquaHomeRegenScheduler(
        hass,
        mock_config_entry,
        device_id=TEST_DEVICE_ID,
        device_slug=SLUG,
        client=runtime.client,
        fast=runtime.coordinators[TEST_DEVICE_ID],
        settings=runtime.settings_coordinators[TEST_DEVICE_ID],
        engine=runtime.analytics_engines[TEST_DEVICE_ID],
    )

    assert rebuilt.state == AutomationState(
        vacation_deferral=True,
        auto_vacation=True,
        smart_regeneration=True,
        deferral_source=DEFERRAL_SOURCE_MANUAL,
        deferral_started=FROZEN_NOW,
        last_decision=None,
        last_decision_at=None,
    )
    assert state_from_options(mock_config_entry, TEST_DEVICE_ID) == rebuilt.state


async def test_persisting_a_flag_never_reloads_the_entry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Writing the options is safe because no update listener is registered.

    The scheduler persists through ``async_update_entry`` on every mutation, so
    an update listener would turn each decision into a full reload — tearing
    down the very coordinators that took it. The runtime objects surviving
    identically is the proof that no reload happened.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)
    scheduler = _scheduler(mock_config_entry)
    runtime_before = mock_config_entry.runtime_data
    assert list(mock_config_entry.update_listeners) == []

    await scheduler.async_set_smart_regeneration(True)
    await hass.async_block_till_done()

    assert (
        mock_config_entry.options[OPTION_AUTOMATION][TEST_DEVICE_ID][
            "smart_regeneration"
        ]
        is True
    )
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.runtime_data is runtime_before
    assert _scheduler(mock_config_entry) is scheduler
