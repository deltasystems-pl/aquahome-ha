"""Tests for the four AquaHome domain actions.

The service layer is the integration's only *scriptable* surface: an automation
or a template calls one of these four actions and consumes what comes back, so
the response payloads are as much a contract as any entity state. This module
therefore pins them literally — every key, every ISO date, every two-decimal
rounding, and the ``None`` a detector emits when it has nothing to say — against
crafted :class:`~custom_components.aquahome.analytics.model.AnalyticsResult`
values pushed into the engine, which keeps the assertions independent of what
the numeric detectors happen to decide.

Covered here:

* registration from ``async_setup``: all four actions exist on a bare Home
  Assistant with no config entry at all (the ``action-setup`` quality rule), and
  the two read-only ones are registered response-only;
* ``analyze_usage``: the full serialized result, the "nothing assessed" shape
  where every key is present and ``None``, the hour-of-week grid folded to
  ``weekday -> [hour, ...]``, ``refresh: true`` recomputing exactly once (and
  ``false`` never), and the honest refusal while the engine has never run;
* ``get_usage_forecast``: the real engine path (readings, timezone resolution,
  executor dispatch) with only the pure computation stubbed, the day-count
  bounds, and a recorder failure surfacing as ``forecast_failed``;
* ``schedule_regeneration``: the literal ``/command`` body each mode emits, the
  per-mode ``can_schedule`` / ``can_recharge`` gates, and that a blocked call
  sends nothing at all;
* ``set_vacation_mode``: the scheduler state, the switch it re-renders, and the
  options it persists;
* the wrong-target refusal of every one of the four.

The clock is frozen before setup and the stored access token re-minted against
it, so nothing depends on wall time. No recorder is loaded: the engine's own
startup pass then reads an empty meter series, which is exactly the state a
freshly installed integration is in.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, NoReturn
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.components.button.const import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.switch.const import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ACCESS_TOKEN,
    STATE_OFF,
    STATE_ON,
    Platform,
)
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.setup import async_setup_component
from yarl import URL

from custom_components.aquahome import services as services_module
from custom_components.aquahome.analytics.engine import AquaHomeAnalyticsEngine
from custom_components.aquahome.analytics.model import (
    BUCKET_EXCESS,
    REASON_DAILY_HIGH,
    REASON_DRIFT,
    SOURCE_DEVICE_AVERAGE,
    SOURCE_LEARNED_WEEKDAY,
    TIER_WARNING,
    AnalyticsInputs,
    AnalyticsResult,
    AnomalyState,
    DayAssessment,
    ForecastState,
    GridSummary,
    LeakState,
    NightAssessment,
    NightVerdict,
    VacationState,
)
from custom_components.aquahome.const import (
    ATTR_DAYS,
    ATTR_MODE,
    ATTR_REFRESH,
    ATTR_VACATION,
    DEFERRAL_SOURCE_MANUAL,
    DOMAIN,
    FORECAST_MAX_DAYS,
    OPTION_AUTOMATION,
    REGEN_MODE_CANCEL,
    REGEN_MODE_NOW,
    REGEN_MODE_SCHEDULE,
    SERVICE_ANALYZE_USAGE,
    SERVICE_GET_USAGE_FORECAST,
    SERVICE_SCHEDULE_REGENERATION,
    SERVICE_SET_VACATION_MODE,
)
from tests.conftest import (
    TEST_DEVICE_ID,
    add_device_routes,
    command_url,
    load_fixture,
    make_access_token,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from aioresponses import aioresponses
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.aquahome.scheduler import AquaHomeRegenScheduler

#: Slug of the captured device's serial ``7384243-20203-1120``.
SLUG = "7384243_20203_1120"

#: Instant every test freezes to before setup: inside the fixtures' capture
#: window, so no assertion depends on the machine's wall clock.
FROZEN_INSTANT = "2026-07-21T12:00:00+00:00"
FROZEN_UTC = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)

#: Width of the analytics hour-of-week grid (7 x 24), mirrored from the engine's
#: model so the crafted grids below are always full-size.
GRID_HOURS = 168

#: The four actions and the entity domain each is registered for.
SERVICE_DOMAINS: tuple[tuple[str, str], ...] = (
    (SERVICE_ANALYZE_USAGE, Platform.SENSOR),
    (SERVICE_GET_USAGE_FORECAST, Platform.SENSOR),
    (SERVICE_SET_VACATION_MODE, Platform.SWITCH),
    (SERVICE_SCHEDULE_REGENERATION, Platform.BUTTON),
)


# ---------------------------------------------------------------------------
# Crafted analytics results
#
# One neutral value per state dataclass (the shape a pass with nothing to
# assess produces) plus a rich counterpart carrying deliberately over-precise
# floats, so the transport rounding is visible in every assertion.
# ---------------------------------------------------------------------------

#: Instant stamped on every crafted result — distinct from the frozen clock so
#: a handler reading "now" instead of the result would be caught.
COMPUTED_AT = datetime(2026, 7, 21, 7, 35, tzinfo=UTC)

NEUTRAL_LEAK = LeakState(
    active=None,
    consecutive_nights=0,
    rate_liters_per_hour=None,
    implied_liters_per_day=None,
    tier=None,
    persistent_flow=False,
    last_verdict_night=None,
    masking_coverage=False,
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
NEUTRAL_VACATION = VacationState(active=None, consecutive_days=0, since=None)
NEUTRAL_FORECAST = ForecastState(
    gallons=None,
    liters=None,
    source=None,
    band_liters=None,
    weekday=None,
    persons=None,
)


def _grid(
    active: Iterable[int] = (), *, mature_buckets: int = 0, hourly_samples: int = 0
) -> GridSummary:
    """Build a full-size hour-of-week grid with ``active`` indices switched on."""
    switched_on = set(active)
    return GridSummary(
        active_hours=tuple(index in switched_on for index in range(GRID_HOURS)),
        mature_buckets=mature_buckets,
        hourly_samples=hourly_samples,
    )


NEUTRAL_GRID = _grid()


def _result(  # noqa: PLR0913 - one defaulted keyword per AnalyticsResult field
    *,
    computed_at: datetime = COMPUTED_AT,
    nights: tuple[NightAssessment, ...] = (),
    days: tuple[DayAssessment, ...] = (),
    leak: LeakState = NEUTRAL_LEAK,
    anomaly: AnomalyState = NEUTRAL_ANOMALY,
    vacation: VacationState = NEUTRAL_VACATION,
    forecast: ForecastState = NEUTRAL_FORECAST,
    grid: GridSummary = NEUTRAL_GRID,
) -> AnalyticsResult:
    """Assemble one crafted analytics result from neutral defaults."""
    return AnalyticsResult(
        computed_at=computed_at,
        nights=nights,
        days=days,
        leak=leak,
        anomaly=anomaly,
        vacation=vacation,
        forecast=forecast,
        grid=grid,
    )


#: A day the anomaly detector flagged, with over-precise litre figures.
ANOMALY_DAY = DayAssessment(
    day=date(2026, 7, 20),
    total_liters=812.3411,
    expected_liters=400.0071,
    spread_liters=95.5541,
    ratio=2.0308,
    bucket=BUCKET_EXCESS,
    largest_event_liters=210.1264,
    assessable=True,
)
#: A day nothing could be said about — every metric absent, not assessable.
BLANK_DAY = DayAssessment(
    day=date(2026, 7, 19),
    total_liters=None,
    expected_liters=None,
    spread_liters=None,
    ratio=None,
    bucket=None,
    largest_event_liters=None,
    assessable=False,
)

RICH_NIGHTS = (
    NightAssessment(
        night=date(2026, 7, 19), verdict=NightVerdict.NO_LEAK, min_hour_liters=0.0
    ),
    NightAssessment(
        night=date(2026, 7, 20), verdict=NightVerdict.MASKED, min_hour_liters=None
    ),
    NightAssessment(
        night=date(2026, 7, 21), verdict=NightVerdict.LEAK, min_hour_liters=12.3456
    ),
)

RICH_LEAK = LeakState(
    active=True,
    consecutive_nights=3,
    rate_liters_per_hour=12.3456,
    implied_liters_per_day=296.2944,
    tier=TIER_WARNING,
    persistent_flow=True,
    last_verdict_night=date(2026, 7, 21),
    masking_coverage=True,
)
RICH_ANOMALY = AnomalyState(
    active=True,
    reasons=(REASON_DAILY_HIGH, REASON_DRIFT),
    day=ANOMALY_DAY,
    point_hours=2,
    drift_alarm=True,
    drift_cusum=True,
    drift_ewma=False,
)
RICH_VACATION = VacationState(active=False, consecutive_days=2, since=date(2026, 7, 18))
RICH_FORECAST = ForecastState(
    gallons=61.2345,
    liters=231.8961,
    source=SOURCE_LEARNED_WEEKDAY,
    band_liters=44.4444,
    weekday="tuesday",
    persons=3,
)
#: Monday 07:00 + 08:00, Wednesday 13:00, Sunday 23:00 — one index per grid row
#: arithmetic case (first row, a middle row, the last row).
RICH_GRID = _grid(
    (7, 8, 2 * 24 + 13, 6 * 24 + 23), mature_buckets=42, hourly_samples=907
)


def rich_result() -> AnalyticsResult:
    """Return the fully populated result the response shape is pinned against."""
    return _result(
        nights=RICH_NIGHTS,
        days=(BLANK_DAY, ANOMALY_DAY),
        leak=RICH_LEAK,
        anomaly=RICH_ANOMALY,
        vacation=RICH_VACATION,
        forecast=RICH_FORECAST,
        grid=RICH_GRID,
    )


#: The exact ``analyze_usage`` payload :func:`rich_result` must serialize to.
RICH_RESPONSE: dict[str, Any] = {
    "computed_at": "2026-07-21T07:35:00+00:00",
    "leak": {
        "active": True,
        "consecutive_nights": 3,
        "rate_liters_per_hour": 12.35,
        "implied_liters_per_day": 296.29,
        "tier": TIER_WARNING,
        "persistent_flow": True,
        "masking_coverage": True,
        "last_verdict_night": "2026-07-21",
    },
    "anomaly": {
        "active": True,
        "reasons": [REASON_DAILY_HIGH, REASON_DRIFT],
        "point_hours": 2,
        "drift_alarm": True,
        "drift_cusum": True,
        "drift_ewma": False,
        "day": {
            "day": "2026-07-20",
            "total_liters": 812.34,
            "expected_liters": 400.01,
            "spread_liters": 95.55,
            "ratio": 2.03,
            "bucket": BUCKET_EXCESS,
            "largest_event_liters": 210.13,
        },
    },
    "vacation": {"active": False, "consecutive_days": 2, "since": "2026-07-18"},
    "forecast": {
        "gallons": 61.23,
        "liters": 231.9,
        "source": SOURCE_LEARNED_WEEKDAY,
        "band_liters": 44.44,
        "weekday": "tuesday",
        "persons": 3,
    },
    "grid": {
        "mature_buckets": 42,
        "hourly_samples": 907,
        "active_hours": {"monday": [7, 8], "wednesday": [13], "sunday": [23]},
    },
    "nights": [
        {"night": "2026-07-19", "verdict": "no_leak", "min_hour_liters": 0.0},
        {"night": "2026-07-20", "verdict": "masked", "min_hour_liters": None},
        {"night": "2026-07-21", "verdict": "leak", "min_hour_liters": 12.35},
    ],
    "days": [
        {
            "day": "2026-07-19",
            "total_liters": None,
            "expected_liters": None,
            "spread_liters": None,
            "ratio": None,
            "bucket": None,
            "largest_event_liters": None,
            "assessable": False,
        },
        {
            "day": "2026-07-20",
            "total_liters": 812.34,
            "expected_liters": 400.01,
            "spread_liters": 95.55,
            "ratio": 2.03,
            "bucket": BUCKET_EXCESS,
            "largest_event_liters": 210.13,
            "assessable": True,
        },
    ],
}

#: Three days of stubbed forecasts, deliberately over-precise like the result
#: above so the two-decimal transport rounding is asserted on every field.
STUB_FORECASTS: tuple[tuple[date, ForecastState], ...] = (
    (
        date(2026, 7, 22),
        ForecastState(
            gallons=40.1264,
            liters=150.7841,
            source=SOURCE_DEVICE_AVERAGE,
            band_liters=52.9962,
            weekday="wednesday",
            persons=2,
        ),
    ),
    (
        date(2026, 7, 23),
        ForecastState(
            gallons=52.9962,
            liters=199.4711,
            source=SOURCE_LEARNED_WEEKDAY,
            band_liters=66.6613,
            weekday="thursday",
            persons=2,
        ),
    ),
    (
        date(2026, 7, 24),
        ForecastState(
            gallons=None,
            liters=None,
            source=None,
            band_liters=None,
            weekday=None,
            persons=None,
        ),
    ),
)

#: The exact ``forecasts`` list :data:`STUB_FORECASTS` must serialize to.
STUB_FORECAST_RESPONSE: list[dict[str, Any]] = [
    {
        "date": "2026-07-22",
        "weekday": "wednesday",
        "gallons": 40.13,
        "liters": 150.78,
        "band_liters": 53.0,
        "source": SOURCE_DEVICE_AVERAGE,
        "persons": 2,
    },
    {
        "date": "2026-07-23",
        "weekday": "thursday",
        "gallons": 53.0,
        "liters": 199.47,
        "band_liters": 66.66,
        "source": SOURCE_LEARNED_WEEKDAY,
        "persons": 2,
    },
    {
        "date": "2026-07-24",
        "weekday": None,
        "gallons": None,
        "liters": None,
        "band_liters": None,
        "source": None,
        "persons": None,
    },
]


# ---------------------------------------------------------------------------
# Fixtures, boot and access helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _service_platforms() -> Iterator[None]:
    """Forward only the three platforms the four actions can target."""
    with patch(
        "custom_components.aquahome.PLATFORMS",
        [Platform.SENSOR, Platform.SWITCH, Platform.BUTTON],
    ):
        yield


async def _skip_startup_pipeline(statistics: object, engine: object) -> None:
    """Stand in for the backfill-then-analyze pipeline without running it."""
    return


async def boot(  # noqa: PLR0913 - one keyword knob per per-test variation
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock: aioresponses,
    freezer: FrozenDateTimeFactory,
    *,
    device_detail: dict[str, Any] | None = None,
    analytics: bool = True,
) -> None:
    """Freeze the clock, set the entry up, and settle the startup pipeline.

    The stored access token is re-minted against the frozen clock so the auth
    manager never decides it is stale mid-test, which has to happen between
    adding the entry and setting it up — hence the unrolled ``setup_integration``.
    With ``analytics=False`` the startup pipeline is replaced by a no-op, leaving
    the engine in its never-ran state (``data is None``) while every entity stays
    available, which is the state the not-ready refusal is about.
    """
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(mock, device_detail=device_detail)
    mock.put(command_url(), payload={"result": "ok"}, repeat=True)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_ACCESS_TOKEN: make_access_token()}
    )
    pipeline: AbstractContextManager[Any] = (
        nullcontext()
        if analytics
        else patch(
            "custom_components.aquahome._async_run_startup_pipeline",
            _skip_startup_pipeline,
        )
    )
    with pipeline:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)


def engine_of(entry: MockConfigEntry) -> AquaHomeAnalyticsEngine:
    """Return the analytics engine the entry built for the fixture device."""
    engine: AquaHomeAnalyticsEngine = entry.runtime_data.analytics_engines[
        TEST_DEVICE_ID
    ]
    return engine


def scheduler_of(entry: MockConfigEntry) -> AquaHomeRegenScheduler:
    """Return the automation scheduler the entry built for the fixture device."""
    scheduler: AquaHomeRegenScheduler = entry.runtime_data.schedulers[TEST_DEVICE_ID]
    return scheduler


async def push(
    hass: HomeAssistant, entry: MockConfigEntry, result: AnalyticsResult
) -> None:
    """Publish a crafted analytics result and settle every listener."""
    engine_of(entry).async_set_updated_data(result)
    await hass.async_block_till_done()


def entity_id_of(registry: er.EntityRegistry, domain: str, key: str) -> str:
    """Return the entity id registered for one description key."""
    entity_id = registry.async_get_entity_id(domain, DOMAIN, f"{SLUG}_{key}")
    assert entity_id is not None, f"entity {key} was never registered"
    return entity_id


def _detail() -> dict[str, Any]:
    """Return a deep-copied device-detail payload to mutate."""
    return load_fixture("device-detail.json")


def _recharge_ui(detail: dict[str, Any]) -> dict[str, Any]:
    """Return the mutable recharge tile of a device-detail payload."""
    tile: dict[str, Any] = detail["enriched_data"]["water_treatment"]["recharge_ui"]
    return tile


def without_regeneration_control() -> dict[str, Any]:
    """Return a device payload with no regeneration controls whatsoever.

    Neither the ``regeneration`` feature nor either enriched block a control
    could act on, which is the only way an AquaHome button can be an invalid
    target for :data:`SERVICE_SCHEDULE_REGENERATION`.
    """
    detail = _detail()
    treatment = detail["enriched_data"]["water_treatment"]
    treatment["features"] = []
    treatment.pop("recharge_ui", None)
    treatment.pop("regeneration", None)
    return detail


def command_bodies(mock: aioresponses) -> list[dict[str, Any]]:
    """Return the JSON body of every ``PUT /devices/{id}/command`` so far."""
    calls = mock.requests.get(("PUT", URL(command_url())), [])
    return [call.kwargs["json"] for call in calls]


async def call_analyze(
    hass: HomeAssistant, entity_id: str, *, refresh: bool | None = None
) -> dict[str, Any]:
    """Call ``analyze_usage`` on one entity and return the response payload."""
    data: dict[str, Any] = {ATTR_ENTITY_ID: entity_id}
    if refresh is not None:
        data[ATTR_REFRESH] = refresh
    response = await hass.services.async_call(
        DOMAIN, SERVICE_ANALYZE_USAGE, data, blocking=True, return_response=True
    )
    assert isinstance(response, dict)
    return response


async def call_forecast(
    hass: HomeAssistant, entity_id: str, *, days: int | None = None
) -> dict[str, Any]:
    """Call ``get_usage_forecast`` on one entity and return the response."""
    data: dict[str, Any] = {ATTR_ENTITY_ID: entity_id}
    if days is not None:
        data[ATTR_DAYS] = days
    response = await hass.services.async_call(
        DOMAIN, SERVICE_GET_USAGE_FORECAST, data, blocking=True, return_response=True
    )
    assert isinstance(response, dict)
    return response


async def call_regeneration(
    hass: HomeAssistant, entity_id: str, *, mode: str | None = None
) -> None:
    """Call ``schedule_regeneration`` on one button entity."""
    data: dict[str, Any] = {ATTR_ENTITY_ID: entity_id}
    if mode is not None:
        data[ATTR_MODE] = mode
    await hass.services.async_call(
        DOMAIN, SERVICE_SCHEDULE_REGENERATION, data, blocking=True
    )


async def call_vacation(hass: HomeAssistant, entity_id: str, *, vacation: bool) -> None:
    """Call ``set_vacation_mode`` on one switch entity."""
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_VACATION_MODE,
        {ATTR_ENTITY_ID: entity_id, ATTR_VACATION: vacation},
        blocking=True,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_services_exist_without_any_config_entry(hass: HomeAssistant) -> None:
    """All four actions register from ``async_setup``, with no entry loaded.

    The ``action-setup`` quality rule: an automation referencing one of these
    actions must still validate while the integration has no entry at all (or
    while its cloud login is broken), so registration may not depend on
    ``async_setup_entry`` ever running.
    """
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    assert hass.config_entries.async_entries(DOMAIN) == []
    for service, _ in SERVICE_DOMAINS:
        assert hass.services.has_service(DOMAIN, service), service


async def test_response_services_are_registered_response_only(
    hass: HomeAssistant,
) -> None:
    """The two read-only actions answer with data and refuse a fire-and-forget call.

    ``SupportsResponse.ONLY`` is what makes ``response_variable`` mandatory in a
    script: a caller that ignores the answer is asking for nothing at all, and
    Home Assistant rejects it before the handler is reached.
    """
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    registered = hass.services.async_services_for_domain(DOMAIN)
    assert registered[SERVICE_ANALYZE_USAGE].supports_response is SupportsResponse.ONLY
    assert (
        registered[SERVICE_GET_USAGE_FORECAST].supports_response
        is SupportsResponse.ONLY
    )
    assert registered[SERVICE_SET_VACATION_MODE].supports_response is (
        SupportsResponse.NONE
    )
    assert registered[SERVICE_SCHEDULE_REGENERATION].supports_response is (
        SupportsResponse.NONE
    )

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ANALYZE_USAGE,
            {ATTR_ENTITY_ID: "sensor.nope"},
            blocking=True,
        )


async def test_services_survive_a_loaded_entry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Setting an entry up neither duplicates nor drops any of the four actions.

    The engine assertion is the counterpart of the not-ready refusal below: a
    normal boot *does* complete an analytics pass, so a test that finds no
    result has genuinely suppressed the startup pipeline rather than raced it.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    for service, _ in SERVICE_DOMAINS:
        assert hass.services.has_service(DOMAIN, service), service
    assert engine_of(mock_config_entry).data is not None


# ---------------------------------------------------------------------------
# analyze_usage — response shape
# ---------------------------------------------------------------------------


async def test_analyze_usage_pins_the_full_response_shape(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
) -> None:
    """A fully populated result serializes to exactly the documented payload.

    Every block is published whole — including the fields the entities keep to
    themselves — with dates as ISO strings, tuples as lists, floats rounded to
    two decimals, and the hour-of-week grid folded into weekday rows. The
    response is keyed by the entity the action was targeted at.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)
    await push(hass, mock_config_entry, rich_result())

    entity_id = entity_id_of(entity_registry, SENSOR_DOMAIN, "usage_forecast")
    response = await call_analyze(hass, entity_id)

    assert response == {entity_id: RICH_RESPONSE}


async def test_analyze_usage_publishes_every_key_when_nothing_assessed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
) -> None:
    """A pass with nothing to assess still carries every key, ``None`` throughout.

    A template reading ``response["leak"]["tier"]`` must keep evaluating on a
    freshly installed system, so absence is published as ``None`` rather than as
    a missing key — and the two lists and the grid come back empty, never absent.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)
    await push(hass, mock_config_entry, _result())

    entity_id = entity_id_of(entity_registry, SENSOR_DOMAIN, "night_flow")
    payload = (await call_analyze(hass, entity_id))[entity_id]

    assert payload == {
        "computed_at": "2026-07-21T07:35:00+00:00",
        "leak": {
            "active": None,
            "consecutive_nights": 0,
            "rate_liters_per_hour": None,
            "implied_liters_per_day": None,
            "tier": None,
            "persistent_flow": False,
            "masking_coverage": False,
            "last_verdict_night": None,
        },
        "anomaly": {
            "active": None,
            "reasons": [],
            "point_hours": 0,
            "drift_alarm": False,
            "drift_cusum": False,
            "drift_ewma": False,
            "day": None,
        },
        "vacation": {"active": None, "consecutive_days": 0, "since": None},
        "forecast": {
            "gallons": None,
            "liters": None,
            "source": None,
            "band_liters": None,
            "weekday": None,
            "persons": None,
        },
        "grid": {"mature_buckets": 0, "hourly_samples": 0, "active_hours": {}},
        "nights": [],
        "days": [],
    }


async def test_analyze_usage_folds_active_hours_by_weekday(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
) -> None:
    """The 168-flag grid folds to ``weekday -> [hour, ...]``, quiet days omitted.

    The grid is indexed ``weekday(Monday = 0) * 24 + hour``; a whole active
    Tuesday therefore has to come back as every hour of one row and nothing
    else, which pins both halves of the index arithmetic at once.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)
    tuesday = _grid(range(24, 48), mature_buckets=7, hourly_samples=168)
    await push(hass, mock_config_entry, _result(grid=tuesday))

    entity_id = entity_id_of(entity_registry, SENSOR_DOMAIN, "usage_forecast")
    payload = (await call_analyze(hass, entity_id))[entity_id]

    assert payload["grid"] == {
        "mature_buckets": 7,
        "hourly_samples": 168,
        "active_hours": {"tuesday": list(range(24))},
    }


# ---------------------------------------------------------------------------
# analyze_usage — refresh and readiness
# ---------------------------------------------------------------------------


async def test_analyze_usage_refresh_recomputes_exactly_once(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
) -> None:
    """``refresh: true`` runs one engine pass and answers from *its* result.

    The recompute costs a recorder read plus the detection pass, so it must
    happen once per call — never twice — and the response must carry what the
    fresh pass produced rather than the result published before it.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)
    await push(hass, mock_config_entry, _result(forecast=NEUTRAL_FORECAST))

    recomputed = _result(forecast=replace(NEUTRAL_FORECAST, gallons=88.0))
    refreshed: list[AquaHomeAnalyticsEngine] = []

    async def _recompute(engine: AquaHomeAnalyticsEngine) -> AnalyticsResult:
        """Stand in for one full analytics pass."""
        refreshed.append(engine)
        return recomputed

    entity_id = entity_id_of(entity_registry, SENSOR_DOMAIN, "usage_forecast")
    with patch.object(AquaHomeAnalyticsEngine, "_async_update_data", _recompute):
        payload = (await call_analyze(hass, entity_id, refresh=True))[entity_id]
    await hass.async_block_till_done()

    assert refreshed == [engine_of(mock_config_entry)]
    assert payload["forecast"]["gallons"] == 88.0
    assert engine_of(mock_config_entry).data == recomputed


@pytest.mark.parametrize("refresh", [None, False])
async def test_analyze_usage_without_refresh_never_recomputes(  # noqa: PLR0913
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
    refresh: bool | None,
) -> None:
    """Omitted or ``false``, the action reads the published result untouched.

    The default has to be the free path: a script polling this action every
    minute must not drive a recorder read and a detection pass each time.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)
    published = _result(forecast=replace(NEUTRAL_FORECAST, gallons=61.2345))
    await push(hass, mock_config_entry, published)

    refreshed: list[AquaHomeAnalyticsEngine] = []

    async def _recompute(engine: AquaHomeAnalyticsEngine) -> AnalyticsResult:
        """Fail the test loudly if the engine is asked to recompute."""
        refreshed.append(engine)
        return _result()

    entity_id = entity_id_of(entity_registry, SENSOR_DOMAIN, "usage_forecast")
    with patch.object(AquaHomeAnalyticsEngine, "_async_update_data", _recompute):
        payload = (await call_analyze(hass, entity_id, refresh=refresh))[entity_id]

    assert refreshed == []
    assert payload["forecast"]["gallons"] == 61.23
    assert engine_of(mock_config_entry).data == published


async def test_analyze_usage_refuses_before_the_first_pass(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
) -> None:
    """An engine that never completed a pass is a validation error, not an empty dict.

    Answering ``{}`` (or a payload of ``None`` blocks) would let an automation
    act on "no leak" when the truth is "nothing analysed yet".
    """
    await boot(hass, mock_config_entry, mock_api, freezer, analytics=False)
    assert engine_of(mock_config_entry).data is None

    entity_id = entity_id_of(entity_registry, SENSOR_DOMAIN, "usage_forecast")
    with pytest.raises(ServiceValidationError) as caught:
        await call_analyze(hass, entity_id)

    assert caught.value.translation_domain == DOMAIN
    assert caught.value.translation_key == "analytics_not_ready"


# ---------------------------------------------------------------------------
# get_usage_forecast
# ---------------------------------------------------------------------------


async def test_get_usage_forecast_runs_the_real_engine_path(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
) -> None:
    """``days: 3`` reaches the pure computation through the engine's own gathering.

    Only the numeric core is stubbed: the readings load, the device-timezone
    resolution, the input assembly and the executor dispatch are the production
    ones, so this pins the plumbing between the action and the detectors — the
    requested day count arrives, the inputs are the real ones (device zone, the
    frozen "now", the empty recorder-free series), and the answer is serialized
    per day with ISO dates and two-decimal rounding.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)
    seen: list[tuple[AnalyticsInputs, int]] = []

    def _stub(
        inputs: AnalyticsInputs, days: int
    ) -> tuple[tuple[date, ForecastState], ...]:
        """Stand in for the pure forecast computation."""
        seen.append((inputs, days))
        return STUB_FORECASTS[:days]

    entity_id = entity_id_of(entity_registry, SENSOR_DOMAIN, "usage_forecast")
    with patch("custom_components.aquahome.analytics.engine.compute_forecasts", _stub):
        response = await call_forecast(hass, entity_id, days=3)

    assert response == {entity_id: {"forecasts": STUB_FORECAST_RESPONSE}}
    assert len(seen) == 1
    inputs, days = seen[0]
    assert days == 3
    assert isinstance(inputs, AnalyticsInputs)
    assert inputs.tz_key == "Europe/Warsaw"
    assert inputs.readings == ()
    assert inputs.now == FROZEN_UTC
    assert inputs.device_online is True


async def test_get_usage_forecast_defaults_to_one_day(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
) -> None:
    """Called without ``days`` the action asks for tomorrow alone."""
    await boot(hass, mock_config_entry, mock_api, freezer)
    seen: list[int] = []

    def _stub(
        inputs: AnalyticsInputs, days: int
    ) -> tuple[tuple[date, ForecastState], ...]:
        """Record the requested day count and answer with that many days."""
        seen.append(days)
        return STUB_FORECASTS[:days]

    entity_id = entity_id_of(entity_registry, SENSOR_DOMAIN, "night_flow")
    with patch("custom_components.aquahome.analytics.engine.compute_forecasts", _stub):
        payload = (await call_forecast(hass, entity_id))[entity_id]

    assert seen == [1]
    assert payload == {"forecasts": STUB_FORECAST_RESPONSE[:1]}


@pytest.mark.parametrize("days", [0, -1, FORECAST_MAX_DAYS + 1])
async def test_get_usage_forecast_rejects_out_of_range_days(  # noqa: PLR0913
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
    days: int,
) -> None:
    """The day count is bounded by the schema, before the engine is touched.

    A forecast beyond :data:`FORECAST_MAX_DAYS` would be extrapolation dressed
    up as data, and zero days is a call that cannot mean anything.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)
    called: list[int] = []

    def _stub(
        inputs: AnalyticsInputs, requested: int
    ) -> tuple[tuple[date, ForecastState], ...]:
        """Fail the test loudly if an out-of-range call reaches the computation."""
        called.append(requested)
        return ()

    entity_id = entity_id_of(entity_registry, SENSOR_DOMAIN, "usage_forecast")
    with (
        patch("custom_components.aquahome.analytics.engine.compute_forecasts", _stub),
        pytest.raises(vol.Invalid),
    ):
        await call_forecast(hass, entity_id, days=days)

    assert called == []


async def test_get_usage_forecast_accepts_the_maximum_day_count(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
) -> None:
    """The documented upper bound itself is a valid request (inclusive range)."""
    await boot(hass, mock_config_entry, mock_api, freezer)
    seen: list[int] = []

    def _stub(
        inputs: AnalyticsInputs, days: int
    ) -> tuple[tuple[date, ForecastState], ...]:
        """Record the requested day count."""
        seen.append(days)
        return STUB_FORECASTS

    entity_id = entity_id_of(entity_registry, SENSOR_DOMAIN, "usage_forecast")
    with patch("custom_components.aquahome.analytics.engine.compute_forecasts", _stub):
        await call_forecast(hass, entity_id, days=FORECAST_MAX_DAYS)

    assert seen == [FORECAST_MAX_DAYS]


async def test_get_usage_forecast_maps_a_recorder_failure(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
) -> None:
    """A failed statistics read surfaces as a translated ``forecast_failed`` error.

    The engine reports a recorder problem as ``UpdateFailed``; the action must
    turn that into a user-facing error carrying the cause — and not into a
    validation error, which would blame the caller for an infrastructure fault.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    async def _boom(engine: AquaHomeAnalyticsEngine) -> NoReturn:
        """Fail the statistics read the way a broken recorder does."""
        message = "Reading imported statistics failed: database is locked"
        raise UpdateFailed(message)

    entity_id = entity_id_of(entity_registry, SENSOR_DOMAIN, "usage_forecast")
    with (
        patch.object(AquaHomeAnalyticsEngine, "_async_load_readings", _boom),
        pytest.raises(HomeAssistantError) as caught,
    ):
        await call_forecast(hass, entity_id, days=2)

    assert not isinstance(caught.value, ServiceValidationError)
    assert caught.value.translation_domain == DOMAIN
    assert caught.value.translation_key == "forecast_failed"
    assert caught.value.translation_placeholders == {
        "message": "Reading imported statistics failed: database is locked"
    }


# ---------------------------------------------------------------------------
# schedule_regeneration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "action"),
    [
        (REGEN_MODE_SCHEDULE, "schedule"),
        (REGEN_MODE_NOW, "regenerate"),
        (REGEN_MODE_CANCEL, "cancel"),
    ],
)
async def test_schedule_regeneration_sends_the_mode_command(  # noqa: PLR0913
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
    mode: str,
    action: str,
) -> None:
    """Each mode PUTs the live-verified ``regenerate`` body for that action.

    All three calls target the *same* button on purpose: the action the device
    receives must come from the ``mode`` field, never from which regeneration
    button happened to be picked as the target.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    entity_id = entity_id_of(entity_registry, BUTTON_DOMAIN, "regenerate_now")
    await call_regeneration(hass, entity_id, mode=mode)

    assert command_bodies(mock_api) == [{"function": "regenerate", "action": action}]


async def test_schedule_regeneration_defaults_to_scheduling(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
) -> None:
    """Called without a mode the action schedules — the least intrusive option."""
    await boot(hass, mock_config_entry, mock_api, freezer)

    entity_id = entity_id_of(entity_registry, BUTTON_DOMAIN, "cancel_regeneration")
    await call_regeneration(hass, entity_id)

    assert command_bodies(mock_api) == [
        {"function": "regenerate", "action": "schedule"}
    ]


@pytest.mark.parametrize(
    ("hint", "mode", "target"),
    [
        ("can_schedule", REGEN_MODE_SCHEDULE, "regenerate_now"),
        ("can_recharge", REGEN_MODE_NOW, "cancel_regeneration"),
    ],
)
async def test_schedule_regeneration_honours_the_device_refusal(  # noqa: PLR0913
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
    hint: str,
    mode: str,
    target: str,
) -> None:
    """An explicit ``can_*`` refusal blocks the matching mode before anything is sent.

    Each gate guards exactly its own mode, so the target button is deliberately
    one whose own availability does not depend on the refused hint — the error
    has to come from the action's gate, not from Home Assistant skipping an
    unavailable entity.
    """
    detail = _detail()
    _recharge_ui(detail)[hint] = False
    await boot(hass, mock_config_entry, mock_api, freezer, device_detail=detail)

    entity_id = entity_id_of(entity_registry, BUTTON_DOMAIN, target)
    with pytest.raises(ServiceValidationError) as caught:
        await call_regeneration(hass, entity_id, mode=mode)

    assert caught.value.translation_domain == DOMAIN
    assert caught.value.translation_key == "regen_not_allowed"
    assert command_bodies(mock_api) == []


async def test_schedule_regeneration_cancel_survives_a_schedule_refusal(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
) -> None:
    """``can_schedule: false`` must not block a cancellation.

    Stopping a regeneration is always allowed — the gates are per mode, and a
    device that refuses new work still has to accept being told to stand down.
    """
    detail = _detail()
    _recharge_ui(detail)["can_schedule"] = False
    await boot(hass, mock_config_entry, mock_api, freezer, device_detail=detail)

    entity_id = entity_id_of(entity_registry, BUTTON_DOMAIN, "regenerate_now")
    await call_regeneration(hass, entity_id, mode=REGEN_MODE_CANCEL)

    assert command_bodies(mock_api) == [{"function": "regenerate", "action": "cancel"}]


# ---------------------------------------------------------------------------
# set_vacation_mode
# ---------------------------------------------------------------------------


async def test_set_vacation_mode_starts_and_ends_a_manual_deferral(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
) -> None:
    """The action drives the scheduler, the switch state, and the stored options.

    A deferral asked for through the action is recorded as *manual* — exactly
    like a tap on the switch — because the auto-vacation follower is never
    allowed to release what a person started. Ending it clears the bookkeeping,
    and neither transition commands the device here: the captured softener is
    ``ready``, so there is no scheduled regeneration to cancel.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)
    scheduler = scheduler_of(mock_config_entry)
    entity_id = entity_id_of(entity_registry, SWITCH_DOMAIN, "vacation_deferral")
    assert scheduler.state.vacation_deferral is False

    await call_vacation(hass, entity_id, vacation=True)
    await hass.async_block_till_done()

    started = scheduler.state
    assert started.vacation_deferral is True
    assert started.deferral_source == DEFERRAL_SOURCE_MANUAL
    assert started.deferral_started == FROZEN_UTC
    switch_state = hass.states.get(entity_id)
    assert switch_state is not None
    assert switch_state.state == STATE_ON
    assert mock_config_entry.options[OPTION_AUTOMATION][TEST_DEVICE_ID] == {
        "vacation_deferral": True,
        "auto_vacation": False,
        "smart_regeneration": False,
        "deferral_source": DEFERRAL_SOURCE_MANUAL,
        "deferral_started": FROZEN_UTC.isoformat(),
    }

    await call_vacation(hass, entity_id, vacation=False)
    await hass.async_block_till_done()

    ended = scheduler.state
    assert ended.vacation_deferral is False
    assert ended.deferral_source is None
    assert ended.deferral_started is None
    switch_state = hass.states.get(entity_id)
    assert switch_state is not None
    assert switch_state.state == STATE_OFF
    assert command_bodies(mock_api) == []


async def test_set_vacation_mode_is_idempotent(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
) -> None:
    """Turning the deferral off while it is already off changes nothing.

    A blueprint that re-asserts "not on vacation" on every presence event would
    otherwise rewrite the entry options (and re-run the catch-up check) on every
    call.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)
    scheduler = scheduler_of(mock_config_entry)
    entity_id = entity_id_of(entity_registry, SWITCH_DOMAIN, "vacation_deferral")

    before = scheduler.state
    await call_vacation(hass, entity_id, vacation=False)
    await hass.async_block_till_done()

    assert scheduler.state == before
    assert command_bodies(mock_api) == []


# ---------------------------------------------------------------------------
# Wrong targets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("service", "data"),
    [
        (SERVICE_ANALYZE_USAGE, {ATTR_REFRESH: True}),
        (SERVICE_GET_USAGE_FORECAST, {ATTR_DAYS: 2}),
    ],
)
async def test_analytics_actions_refuse_a_plain_sensor(  # noqa: PLR0913
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
    service: str,
    data: dict[str, Any],
) -> None:
    """A telemetry sensor is refused with the entity the action expects named.

    Both actions are registered for the whole sensor domain, so any AquaHome
    sensor can be pointed at them; refusing an unsuitable one by name beats
    failing later on a missing coordinator attribute.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)
    await push(hass, mock_config_entry, rich_result())

    entity_id = entity_id_of(entity_registry, SENSOR_DOMAIN, "salt_level")
    with pytest.raises(ServiceValidationError) as caught:
        await hass.services.async_call(
            DOMAIN,
            service,
            {ATTR_ENTITY_ID: entity_id, **data},
            blocking=True,
            return_response=True,
        )

    assert caught.value.translation_domain == DOMAIN
    assert caught.value.translation_key == "service_wrong_entity"
    assert caught.value.translation_placeholders == {
        "expected": services_module._EXPECTED_ANALYTICS_SENSOR
    }


async def test_set_vacation_mode_refuses_another_automation_switch(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
) -> None:
    """Aimed at the auto-vacation switch the action refuses and changes nothing.

    The two configuration switches are the easiest mis-targets in the UI (same
    device, adjacent names), so the refusal must be explicit rather than a
    silent no-op — and it must not flip the sibling flag it was pointed at.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)
    scheduler = scheduler_of(mock_config_entry)
    entity_id = entity_id_of(entity_registry, SWITCH_DOMAIN, "auto_vacation")

    with pytest.raises(ServiceValidationError) as caught:
        await call_vacation(hass, entity_id, vacation=True)

    assert caught.value.translation_domain == DOMAIN
    assert caught.value.translation_key == "service_wrong_entity"
    assert caught.value.translation_placeholders == {
        "expected": services_module._EXPECTED_VACATION_SWITCH
    }
    assert scheduler.state.vacation_deferral is False
    assert scheduler.state.auto_vacation is False


async def test_schedule_regeneration_refuses_a_device_without_the_control(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
) -> None:
    """A button on a device with no regeneration control is refused, not commanded.

    Such a device carries neither the feature nor either enriched block the
    controls act on, so no payload it could be sent would be honoured — the
    refusal happens locally and nothing reaches the cloud.
    """
    await boot(
        hass,
        mock_config_entry,
        mock_api,
        freezer,
        device_detail=without_regeneration_control(),
    )

    entity_id = entity_id_of(entity_registry, BUTTON_DOMAIN, "refresh_data")
    with pytest.raises(ServiceValidationError) as caught:
        await call_regeneration(hass, entity_id, mode=REGEN_MODE_NOW)

    assert caught.value.translation_domain == DOMAIN
    assert caught.value.translation_key == "service_wrong_entity"
    assert caught.value.translation_placeholders == {
        "expected": services_module._EXPECTED_REGEN_BUTTON
    }
    assert command_bodies(mock_api) == []
