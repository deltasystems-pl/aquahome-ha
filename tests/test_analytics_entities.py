"""Platform tests for the five analytics entities and the urgent-leak repair issue.

The analytics tier surfaces itself through exactly five entities — three
detection binaries (``leak_suspected``, ``usage_anomaly``, ``vacation_detected``)
and two sensors (``usage_forecast``, the diagnostic ``night_flow``) — plus one
Repairs issue filed at the leak detector's urgent tier. This module boots the
real integration against the ``aioresponses`` HTTP fakes (both entity platforms
forwarded, since the family spans two domains) and then drives the analytics
engine directly with crafted :class:`~custom_components.aquahome.analytics.model.AnalyticsResult`
values through ``async_set_updated_data``.

That split is deliberate. What the detectors *decide* is pinned by the detector
and engine suites over the replayed real history; what this suite owns is the
projection layer: registry metadata (categories, enabled-by-default, unique ids,
device classes, native units), the exact attribute surface each entity renders
from a given result, the tri-state on/off/unknown rendering of the binaries, and
the Repairs lifecycle. Crafted results keep every assertion here independent of
the numeric detector work — a detector threshold change must not break a
rendering test.

No recorder is loaded, so the engine's own startup pass reads an empty meter
series and every detector honestly reports "nothing to assess"; that is exactly
the ``unknown`` baseline the snapshot captures. The clock is frozen before setup
and the stored access token is re-minted against it, so nothing here depends on
the machine's wall clock.

Attribute conventions asserted throughout (contract amendment A8): analytics
attributes always emit *every* key, ``None`` when absent — a stable template
surface — with float attributes rounded to one decimal, the anomaly ratio to
two, and the forecast's litre figures to whole litres.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest
from homeassistant.components.binary_sensor import (
    DOMAIN as BINARY_SENSOR_DOMAIN,
)
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
)
from homeassistant.components.sensor import (
    DOMAIN as SENSOR_DOMAIN,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_ICON,
    CONF_ACCESS_TOKEN,
    STATE_OFF,
    STATE_ON,
    STATE_UNKNOWN,
    EntityCategory,
    Platform,
    UnitOfVolume,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.util.unit_conversion import VolumeConverter

from custom_components.aquahome.analytics.model import (
    BUCKET_EXCESS,
    REASON_DAILY_HIGH,
    REASON_POINT,
    SOURCE_DEVICE_AVERAGE,
    TIER_INFO,
    TIER_URGENT,
    TIER_WARNING,
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
from custom_components.aquahome.const import DOMAIN
from tests.conftest import TEST_DEVICE_ID, add_device_routes, make_access_token

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aioresponses import aioresponses
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import Event, HomeAssistant
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from syrupy.assertion import SnapshotAssertion

    from custom_components.aquahome.analytics.engine import AquaHomeAnalyticsEngine

#: Slug of the captured device's serial ``7384243-20203-1120`` (see the contract).
SLUG = "7384243_20203_1120"
#: The captured device's nickname, rendered into the issue's ``device`` placeholder.
DEVICE_NAME = "Dom"
#: Issue id the urgent-leak nudge is filed under for the captured device.
LEAK_ISSUE_ID = f"leak_urgent_{SLUG}"

#: Instant every test freezes to before setup, matching the sibling platform
#: suites: inside the fixtures' capture window, so nothing depends on wall time.
FROZEN_INSTANT = "2026-07-21T12:00:00+00:00"

#: The five analytics entities: (platform domain, description key, category).
#: ``night_flow`` is the only diagnostic one — it is the evidence behind the leak
#: binary rather than a headline number; the other four are normal-category.
ANALYTICS_ENTITIES: tuple[tuple[str, str, EntityCategory | None], ...] = (
    (BINARY_SENSOR_DOMAIN, "leak_suspected", None),
    (BINARY_SENSOR_DOMAIN, "usage_anomaly", None),
    (BINARY_SENSOR_DOMAIN, "vacation_detected", None),
    (SENSOR_DOMAIN, "usage_forecast", None),
    (SENSOR_DOMAIN, "night_flow", EntityCategory.DIAGNOSTIC),
)

#: Device class each analytics entity is registered with (``None`` = no class).
DEVICE_CLASSES: dict[str, str | None] = {
    "leak_suspected": BinarySensorDeviceClass.MOISTURE,
    "usage_anomaly": BinarySensorDeviceClass.PROBLEM,
    "vacation_detected": None,
    "usage_forecast": SensorDeviceClass.VOLUME_STORAGE,
    "night_flow": SensorDeviceClass.VOLUME_FLOW_RATE,
}

#: Every key the leak binary's attribute dict always carries (A8).
LEAK_ATTRIBUTE_KEYS = frozenset(
    {
        "consecutive_nights",
        "rate_liters_per_hour",
        "implied_liters_per_day",
        "tier",
        "last_verdict_night",
        "persistent_flow",
        "masking_coverage",
    }
)
#: Every key the anomaly binary's attribute dict always carries (A8).
ANOMALY_ATTRIBUTE_KEYS = frozenset(
    {
        "reasons",
        "day",
        "actual_liters",
        "expected_liters",
        "ratio",
        "ratio_bucket",
        "point_hours",
        "drift_alarm",
        "drift_cusum",
        "drift_ewma",
    }
)
#: Every key the vacation binary's attribute dict always carries (A8).
VACATION_ATTRIBUTE_KEYS = frozenset({"consecutive_days", "since"})
#: Every key the forecast sensor's attribute dict always carries (A8).
FORECAST_ATTRIBUTE_KEYS = frozenset(
    {"liters", "source", "band_liters", "weekday", "persons"}
)
#: Every key the night-flow sensor's attribute dict always carries (A8).
NIGHT_FLOW_ATTRIBUTE_KEYS = frozenset({"night", "verdict"})


# ---------------------------------------------------------------------------
# Crafted analytics results
#
# One neutral value per state dataclass — the shape a pass with nothing to
# assess produces — plus a result assembled from them. Tests override exactly
# the fields they are about via ``dataclasses.replace``, which keeps every
# crafted result fully type-checked and every test's intent on one line.
# ---------------------------------------------------------------------------

#: Instant stamped on every crafted result (never rendered; kept deterministic).
COMPUTED_AT = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)

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
NEUTRAL_VACATION = VacationState(active=None, consecutive_days=0, since=None)
NEUTRAL_FORECAST = ForecastState(
    gallons=None,
    liters=None,
    source=None,
    band_liters=None,
    weekday=None,
    persons=None,
)
NEUTRAL_GRID = GridSummary(
    active_hours=(False,) * 168, mature_buckets=0, hourly_samples=0
)


def make_result(  # noqa: PLR0913 - one defaulted keyword per AnalyticsResult field
    *,
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
        computed_at=COMPUTED_AT,
        nights=nights,
        days=days,
        leak=leak,
        anomaly=anomaly,
        vacation=vacation,
        forecast=forecast,
        grid=grid,
    )


def urgent_leak(implied_liters_per_day: float = 1200.0) -> LeakState:
    """Return a confirmed urgent-tier leak (the Repairs-filing condition).

    1200 L/day is the measured implied rate of the contract's 50 L/h injection
    (amendment A3), comfortably past the 1135 L/day urgent threshold.
    """
    return replace(
        NEUTRAL_LEAK,
        active=True,
        consecutive_nights=2,
        rate_liters_per_hour=implied_liters_per_day / 24,
        implied_liters_per_day=implied_liters_per_day,
        tier=TIER_URGENT,
        last_verdict_night=date(2026, 7, 21),
    )


# ---------------------------------------------------------------------------
# Fixtures and boot / access helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _analytics_platforms() -> Iterator[None]:
    """Forward only the two platforms the analytics entities live on.

    The rest of set-up still runs end-to-end; narrowing the platform list keeps
    these tests independent of the button/valve/settings platforms while still
    materialising both analytics domains.
    """
    with patch(
        "custom_components.aquahome.PLATFORMS",
        [Platform.BINARY_SENSOR, Platform.SENSOR],
    ):
        yield


async def boot(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Freeze the clock, set the entry up, and settle the startup pipeline.

    The stored access token is re-minted against the frozen clock so the auth
    manager never decides it is stale mid-test; that has to happen between
    adding the entry and setting it up, which is why the shared
    ``setup_integration`` helper is unrolled here. Settling with
    ``wait_background_tasks`` matters: the engine's first pass runs as an entry
    background task, and a crafted result pushed before it finished would be
    overwritten by it.
    """
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(mock)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_ACCESS_TOKEN: make_access_token()}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


def engine_of(entry: MockConfigEntry) -> AquaHomeAnalyticsEngine:
    """Return the analytics engine the entry built for the fixture device."""
    engine: AquaHomeAnalyticsEngine = entry.runtime_data.analytics_engines[
        TEST_DEVICE_ID
    ]
    return engine


async def push(
    hass: HomeAssistant, entry: MockConfigEntry, result: AnalyticsResult
) -> None:
    """Publish a crafted analytics result and settle every listener."""
    engine_of(entry).async_set_updated_data(result)
    await hass.async_block_till_done()


def entity_id_of(registry: er.EntityRegistry, domain: str, key: str) -> str:
    """Return the entity id registered for one analytics description key."""
    entity_id = registry.async_get_entity_id(domain, DOMAIN, f"{SLUG}_{key}")
    assert entity_id is not None, f"analytics entity {key} was never registered"
    return entity_id


def state_of(hass: HomeAssistant, registry: er.EntityRegistry, key: str) -> str:
    """Return the current state string of one analytics entity."""
    domain = next(item[0] for item in ANALYTICS_ENTITIES if item[1] == key)
    state = hass.states.get(entity_id_of(registry, domain, key))
    assert state is not None, f"analytics entity {key} has no state"
    return state.state


def attributes_of(
    hass: HomeAssistant, registry: er.EntityRegistry, key: str
) -> dict[str, Any]:
    """Return the current state attributes of one analytics entity."""
    domain = next(item[0] for item in ANALYTICS_ENTITIES if item[1] == key)
    state = hass.states.get(entity_id_of(registry, domain, key))
    assert state is not None, f"analytics entity {key} has no state"
    return dict(state.attributes)


def native_sensor(
    hass: HomeAssistant, registry: er.EntityRegistry, key: str
) -> SensorEntity:
    """Return the live sensor entity object behind one analytics sensor key.

    The registry stores the *display* unit (Home Assistant converts gallons to
    litres for a metric install), so the contract's native-unit pins can only be
    read off the entity itself.
    """
    entity_id = entity_id_of(registry, SENSOR_DOMAIN, key)
    component = hass.data[DATA_INSTANCES][SENSOR_DOMAIN]
    entity = component.get_entity(entity_id)
    assert entity is not None, f"analytics sensor {key} has no live entity"
    return cast(SensorEntity, entity)


def leak_issue(hass: HomeAssistant) -> ir.IssueEntry | None:
    """Return the urgent-leak repair issue currently filed, or ``None``."""
    return ir.async_get(hass).issues.get((DOMAIN, LEAK_ISSUE_ID))


# ---------------------------------------------------------------------------
# Registry metadata: categories, enabled defaults, unique ids, classes, units
# ---------------------------------------------------------------------------


async def test_exactly_five_analytics_entities_exist(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The device gets all five analytics entities under ``{slug}_{key}`` ids.

    Nothing gates their existence — the engine runs for every device, with no
    capability to feature-gate on — so the set is fixed and complete even on the
    regeneration-only dev fixture.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    registered = {
        entity_id_of(entity_registry, domain, key)
        for domain, key, _category in ANALYTICS_ENTITIES
    }
    assert len(registered) == len(ANALYTICS_ENTITIES)


@pytest.mark.parametrize(
    ("domain", "key", "category"),
    [pytest.param(*entity, id=entity[1]) for entity in ANALYTICS_ENTITIES],
)
async def test_registry_category_and_enabled_default(  # noqa: PLR0913 - standard HA platform-test fixture set plus the parametrized row
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    domain: str,
    key: str,
    category: EntityCategory | None,
) -> None:
    """Each analytics entity is enabled by default and in its contracted category.

    All five are always-on and individually disableable (owner decision
    2026-07-21), so none may ship registry-disabled; only ``night_flow`` is
    diagnostic.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    entry = entity_registry.async_get(entity_id_of(entity_registry, domain, key))
    assert entry is not None
    assert entry.unique_id == f"{SLUG}_{key}"
    assert entry.disabled_by is None
    assert entry.hidden_by is None
    assert entry.entity_category is category


@pytest.mark.parametrize(
    ("domain", "key"),
    [pytest.param(entity[0], entity[1], id=entity[1]) for entity in ANALYTICS_ENTITIES],
)
async def test_device_classes(  # noqa: PLR0913 - standard HA platform-test fixture set plus the parametrized row
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    domain: str,
    key: str,
) -> None:
    """Leak is moisture, anomaly is problem, vacation has none, sensors are volumes.

    ``vacation_detected`` deliberately carries no device class — none of the
    binary classes describes "the house is empty" — and leans on its own icon
    instead.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    entry = entity_registry.async_get(entity_id_of(entity_registry, domain, key))
    assert entry is not None
    assert entry.original_device_class == DEVICE_CLASSES[key]


async def test_vacation_binary_carries_its_own_icon(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The class-less vacation binary is recognisable by its beach icon (A8)."""
    await boot(hass, mock_config_entry, mock_api, freezer)

    assert attributes_of(hass, entity_registry, "vacation_detected")[ATTR_ICON] == (
        "mdi:beach"
    )
    assert ATTR_DEVICE_CLASS not in attributes_of(
        hass, entity_registry, "vacation_detected"
    )


async def test_night_flow_is_native_liters_per_hour(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Night flow measures in L/h natively: the research thresholds are metric.

    We compute it ourselves from a gallon series with a fixed factor, so there
    is no device-reported imperial value to preserve; imperial users flip the
    display unit.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    sensor = native_sensor(hass, entity_registry, "night_flow")
    assert sensor.native_unit_of_measurement == UnitOfVolumeFlowRate.LITERS_PER_HOUR
    assert sensor.state_class is SensorStateClass.MEASUREMENT


async def test_usage_forecast_is_native_gallons_volume_storage(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The forecast is a measurement-class volume in native gallons.

    ``VOLUME_STORAGE`` follows the ``treated_water_available`` precedent — it is
    the measurement-class volume this integration already uses — and the device
    reports gallons, so Home Assistant converts for the user's unit system
    rather than the integration pre-converting.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    sensor = native_sensor(hass, entity_registry, "usage_forecast")
    assert sensor.native_unit_of_measurement == UnitOfVolume.GALLONS
    assert sensor.device_class is SensorDeviceClass.VOLUME_STORAGE
    assert sensor.state_class is SensorStateClass.MEASUREMENT


async def test_analytics_entities_snapshot(  # noqa: PLR0913 - standard HA snapshot-test fixture set
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The five analytics entities match their registry + state snapshot.

    Restricted to the analytics family on purpose: the peer platform suites
    already snapshot everything else, and duplicating them here would make every
    unrelated telemetry change fail this module too. The startup pipeline is
    settled first — the attributes exist only once the engine has run — and with
    no recorder loaded the baseline is the honest "nothing to assess" one.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    for domain, key, _category in ANALYTICS_ENTITIES:
        entity_id = entity_id_of(entity_registry, domain, key)
        assert entity_registry.async_get(entity_id) == snapshot(
            name=f"{entity_id}-entry"
        )
        assert hass.states.get(entity_id) == snapshot(name=f"{entity_id}-state")


# ---------------------------------------------------------------------------
# Tri-state rendering of the detection binaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "verdict", "expected"),
    [
        pytest.param(key, verdict, expected, id=f"{key}-{expected}")
        for key in ("leak_suspected", "usage_anomaly", "vacation_detected")
        for verdict, expected in (
            (True, STATE_ON),
            (False, STATE_OFF),
            (None, STATE_UNKNOWN),
        )
    ],
)
async def test_binary_tri_state_rendering(  # noqa: PLR0913 - standard HA platform-test fixture set plus the parametrized row
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    key: str,
    verdict: bool | None,
    expected: str,
) -> None:
    """Each detector's tri-state verdict renders on / off / unknown.

    ``None`` is not "no": a detector may only claim "no leak", "no anomaly" or
    "not on vacation" once the statistics window actually supports the claim, so
    an unassessable window has to reach the user as ``unknown``.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    results = {
        "leak_suspected": make_result(leak=replace(NEUTRAL_LEAK, active=verdict)),
        "usage_anomaly": make_result(anomaly=replace(NEUTRAL_ANOMALY, active=verdict)),
        "vacation_detected": make_result(
            vacation=replace(NEUTRAL_VACATION, active=verdict)
        ),
    }
    await push(hass, mock_config_entry, results[key])

    assert state_of(hass, entity_registry, key) == expected


# ---------------------------------------------------------------------------
# Attribute rendering from crafted results
# ---------------------------------------------------------------------------


async def test_leak_attributes_render_the_full_evidence(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A confirmed leak publishes every evidence field, floats at one decimal.

    ``masking_coverage`` is part of the evidence on purpose: without
    regeneration history covering the window no LEAK verdict is ever issued, so
    a permanently-off binary is explained rather than looking broken.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    await push(
        hass,
        mock_config_entry,
        make_result(
            leak=replace(
                NEUTRAL_LEAK,
                active=True,
                consecutive_nights=5,
                rate_liters_per_hour=6.04,
                implied_liters_per_day=144.96,
                tier=TIER_INFO,
                persistent_flow=True,
                last_verdict_night=date(2026, 7, 21),
                masking_coverage=True,
            )
        ),
    )

    attributes = attributes_of(hass, entity_registry, "leak_suspected")
    assert state_of(hass, entity_registry, "leak_suspected") == STATE_ON
    assert set(attributes) >= LEAK_ATTRIBUTE_KEYS
    assert attributes["consecutive_nights"] == 5
    assert attributes["rate_liters_per_hour"] == 6.0
    assert attributes["implied_liters_per_day"] == 145.0
    assert attributes["tier"] == "info"
    assert attributes["last_verdict_night"] == "2026-07-21"
    assert attributes["persistent_flow"] is True
    assert attributes["masking_coverage"] is True


async def test_leak_attributes_keep_every_key_when_nothing_is_known(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An unassessable window still publishes the full key set, valued ``None``.

    Analytics attributes are a stable template surface (A8): a key that
    disappears when the detector has nothing to say would break every template
    written against the populated case.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    await push(
        hass,
        mock_config_entry,
        make_result(leak=replace(NEUTRAL_LEAK, masking_coverage=False)),
    )

    attributes = attributes_of(hass, entity_registry, "leak_suspected")
    assert state_of(hass, entity_registry, "leak_suspected") == STATE_UNKNOWN
    assert set(attributes) >= LEAK_ATTRIBUTE_KEYS
    assert attributes["rate_liters_per_hour"] is None
    assert attributes["implied_liters_per_day"] is None
    assert attributes["tier"] is None
    assert attributes["last_verdict_night"] is None
    assert attributes["consecutive_nights"] == 0
    assert attributes["persistent_flow"] is False
    assert attributes["masking_coverage"] is False


async def test_anomaly_attributes_round_the_ratio_and_expose_both_drift_votes(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The anomaly publishes its driving day plus each drift chart's own vote.

    ``drift_alarm`` is the consensus verdict — both charts must agree (A5) — so
    a single chart alarming leaves it ``False`` while ``drift_cusum`` /
    ``drift_ewma`` still show who voted; without them a user could not tell a
    quiet series from a split decision. The ratio renders at two decimals, every
    other float at one.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    day = DayAssessment(
        day=date(2026, 7, 20),
        total_liters=326.44,
        expected_liters=162.37,
        spread_liters=53.21,
        ratio=2.0114,
        bucket=BUCKET_EXCESS,
        largest_event_liters=40.0,
        assessable=True,
    )
    await push(
        hass,
        mock_config_entry,
        make_result(
            days=(day,),
            anomaly=replace(
                NEUTRAL_ANOMALY,
                active=True,
                reasons=(REASON_DAILY_HIGH, REASON_POINT),
                day=day,
                point_hours=3,
                drift_alarm=False,
                drift_cusum=True,
                drift_ewma=False,
            ),
        ),
    )

    attributes = attributes_of(hass, entity_registry, "usage_anomaly")
    assert state_of(hass, entity_registry, "usage_anomaly") == STATE_ON
    assert set(attributes) >= ANOMALY_ATTRIBUTE_KEYS
    assert attributes["reasons"] == ["daily_high", "point"]
    assert attributes["day"] == "2026-07-20"
    assert attributes["actual_liters"] == 326.4
    assert attributes["expected_liters"] == 162.4
    assert attributes["ratio"] == 2.01
    assert attributes["ratio_bucket"] == "excess"
    assert attributes["point_hours"] == 3
    assert attributes["drift_alarm"] is False
    assert attributes["drift_cusum"] is True
    assert attributes["drift_ewma"] is False


async def test_anomaly_attributes_survive_a_missing_daily_expectation(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An anomaly resting on the point reason alone still emits the day keys.

    No daily expectation could be resolved (stale device slots, too little
    learned history), so the day fields are present and ``None`` rather than
    absent.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    await push(
        hass,
        mock_config_entry,
        make_result(
            anomaly=replace(
                NEUTRAL_ANOMALY,
                active=True,
                reasons=(REASON_POINT,),
                point_hours=2,
            )
        ),
    )

    attributes = attributes_of(hass, entity_registry, "usage_anomaly")
    assert set(attributes) >= ANOMALY_ATTRIBUTE_KEYS
    assert attributes["reasons"] == ["point"]
    assert attributes["day"] is None
    assert attributes["actual_liters"] is None
    assert attributes["expected_liters"] is None
    assert attributes["ratio"] is None
    assert attributes["ratio_bucket"] is None
    assert attributes["point_hours"] == 2


async def test_vacation_attributes_report_the_streak(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A running absence publishes its length and the day it started."""
    await boot(hass, mock_config_entry, mock_api, freezer)

    await push(
        hass,
        mock_config_entry,
        make_result(
            vacation=replace(
                NEUTRAL_VACATION,
                active=True,
                consecutive_days=4,
                since=date(2026, 7, 18),
            )
        ),
    )

    attributes = attributes_of(hass, entity_registry, "vacation_detected")
    assert state_of(hass, entity_registry, "vacation_detected") == STATE_ON
    assert set(attributes) >= VACATION_ATTRIBUTE_KEYS
    assert attributes["consecutive_days"] == 4
    assert attributes["since"] == "2026-07-18"


async def test_vacation_attributes_keep_every_key_when_unassessable(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """No judgeable day still emits ``consecutive_days`` and a ``None`` start."""
    await boot(hass, mock_config_entry, mock_api, freezer)

    await push(hass, mock_config_entry, make_result())

    attributes = attributes_of(hass, entity_registry, "vacation_detected")
    assert state_of(hass, entity_registry, "vacation_detected") == STATE_UNKNOWN
    assert set(attributes) >= VACATION_ATTRIBUTE_KEYS
    assert attributes["consecutive_days"] == 0
    assert attributes["since"] is None


async def test_forecast_state_converts_and_attributes_round_to_whole_liters(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The forecast publishes gallons natively and whole-litre companions.

    Pinned against the contract's measured cold-start forecast (A2): 35.0 gal
    for the Tuesday slot, band 90.85 L, one person. The state itself is the
    metric rendering of the native gallons — Home Assistant's conversion, not
    ours — while ``liters`` / ``band_liters`` are whole litres, because the
    underlying statistics are hour-resolution meter reads and sub-litre
    precision would be false rigour.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    gallons = 35.0
    await push(
        hass,
        mock_config_entry,
        make_result(
            forecast=ForecastState(
                gallons=gallons,
                liters=132.4894,
                source=SOURCE_DEVICE_AVERAGE,
                band_liters=90.85,
                weekday="tuesday",
                persons=1,
            )
        ),
    )

    expected_liters = VolumeConverter.convert(
        gallons, UnitOfVolume.GALLONS, UnitOfVolume.LITERS
    )
    state = state_of(hass, entity_registry, "usage_forecast")
    assert float(state) == pytest.approx(expected_liters, abs=0.05)

    attributes = attributes_of(hass, entity_registry, "usage_forecast")
    assert set(attributes) >= FORECAST_ATTRIBUTE_KEYS
    assert attributes["liters"] == 132
    assert attributes["source"] == "device_average"
    assert attributes["band_liters"] == 91
    assert attributes["weekday"] == "tuesday"
    assert attributes["persons"] == 1


async def test_forecast_without_an_expectation_is_unknown(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """No link of the expectation chain resolved: unknown, keys present, None."""
    await boot(hass, mock_config_entry, mock_api, freezer)

    await push(hass, mock_config_entry, make_result())

    attributes = attributes_of(hass, entity_registry, "usage_forecast")
    assert state_of(hass, entity_registry, "usage_forecast") == STATE_UNKNOWN
    assert set(attributes) >= FORECAST_ATTRIBUTE_KEYS
    assert all(attributes[key] is None for key in FORECAST_ATTRIBUTE_KEYS)


# ---------------------------------------------------------------------------
# night_flow: coupled to the newest determinate night
# ---------------------------------------------------------------------------


async def test_night_flow_reports_the_newest_leak_night(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A LEAK night publishes its minimum certain hour as an L/h rate.

    The nights are handed over out of order on purpose: the sensor selects by
    date, never by position, so it cannot depend on the order the detectors
    happen to emit assessments in.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    await push(
        hass,
        mock_config_entry,
        make_result(
            nights=(
                NightAssessment(date(2026, 7, 21), NightVerdict.LEAK, 6.0),
                NightAssessment(date(2026, 7, 19), NightVerdict.NO_LEAK, 0.0),
                NightAssessment(date(2026, 7, 20), NightVerdict.MASKED, None),
            )
        ),
    )

    assert float(state_of(hass, entity_registry, "night_flow")) == 6.0
    attributes = attributes_of(hass, entity_registry, "night_flow")
    assert set(attributes) >= NIGHT_FLOW_ATTRIBUTE_KEYS
    assert attributes["night"] == "2026-07-21"
    assert attributes["verdict"] == "leak"


async def test_night_flow_reports_a_hard_zero_for_a_quiet_night(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A NO_LEAK night is 0.0 L/h, and a newer MASKED night does not displace it.

    The classifier only reaches NO_LEAK on evidence of a genuinely dry hour, so
    zero is an answer; a masked night is not one, and surfacing it would replace
    the freshest real evidence with silence.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    await push(
        hass,
        mock_config_entry,
        make_result(
            nights=(
                NightAssessment(date(2026, 7, 19), NightVerdict.NO_LEAK, 0.0),
                NightAssessment(date(2026, 7, 20), NightVerdict.MASKED, None),
                NightAssessment(date(2026, 7, 21), NightVerdict.UNKNOWN, None),
            )
        ),
    )

    assert float(state_of(hass, entity_registry, "night_flow")) == 0.0
    attributes = attributes_of(hass, entity_registry, "night_flow")
    assert attributes["night"] == "2026-07-19"
    assert attributes["verdict"] == "no_leak"


@pytest.mark.parametrize(
    "nights",
    [
        pytest.param((), id="no-nights"),
        pytest.param(
            (
                NightAssessment(date(2026, 7, 20), NightVerdict.MASKED, None),
                NightAssessment(date(2026, 7, 21), NightVerdict.UNASSESSED, None),
            ),
            id="only-indeterminate-nights",
        ),
    ],
)
async def test_night_flow_without_a_determinate_night_is_unknown(  # noqa: PLR0913 - standard HA platform-test fixture set plus the parametrized row
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    nights: tuple[NightAssessment, ...],
) -> None:
    """Nights nobody could judge leave the sensor unknown with ``None`` labels.

    An unassessed night is not a quiet one — rendering it as 0 L/h would invent
    an all-clear the classifier never issued.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    await push(hass, mock_config_entry, make_result(nights=nights))

    assert state_of(hass, entity_registry, "night_flow") == STATE_UNKNOWN
    attributes = attributes_of(hass, entity_registry, "night_flow")
    assert set(attributes) >= NIGHT_FLOW_ATTRIBUTE_KEYS
    assert attributes["night"] is None
    assert attributes["verdict"] is None


# ---------------------------------------------------------------------------
# The urgent-leak Repairs issue
# ---------------------------------------------------------------------------


async def test_urgent_leak_files_the_repair_issue(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A confirmed urgent-tier leak raises an error-severity nudge for the device.

    The issue is the loudest channel the analytics tier has and is reserved for
    the burst-pipe scale (≥ 1135 L/day after the full debounce); it names the
    device and quotes the implied daily volume.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)
    assert leak_issue(hass) is None

    await push(hass, mock_config_entry, make_result(leak=urgent_leak()))

    issue = leak_issue(hass)
    assert issue is not None
    assert issue.translation_key == "leak_urgent"
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_placeholders == {
        "device": DEVICE_NAME,
        "liters_per_day": "1200",
    }
    assert issue.is_fixable is False
    assert issue.is_persistent is False


@pytest.mark.parametrize(
    "leak",
    [
        pytest.param(
            replace(urgent_leak(), tier=TIER_WARNING, implied_liters_per_day=960.0),
            id="warning-tier",
        ),
        pytest.param(
            replace(urgent_leak(), tier=TIER_INFO, implied_liters_per_day=144.0),
            id="info-tier",
        ),
        pytest.param(replace(urgent_leak(), active=False), id="urgent-but-inactive"),
    ],
)
async def test_softer_leak_evidence_files_no_issue(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    leak: LeakState,
) -> None:
    """Anything below a confirmed urgent leak stays on the binary and the bus.

    Owner decision 2026-07-27: only the urgent tier is loud enough for Repairs —
    a 380 L/day warning that filed an issue would train users to dismiss them.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)

    await push(hass, mock_config_entry, make_result(leak=leak))

    assert leak_issue(hass) is None


async def test_leak_stopping_deletes_the_issue(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A detector that confirms the flow stopped withdraws the nudge."""
    await boot(hass, mock_config_entry, mock_api, freezer)
    await push(hass, mock_config_entry, make_result(leak=urgent_leak()))
    assert leak_issue(hass) is not None

    await push(
        hass,
        mock_config_entry,
        make_result(
            leak=replace(
                NEUTRAL_LEAK, active=False, last_verdict_night=date(2026, 7, 22)
            )
        ),
    )

    assert leak_issue(hass) is None


async def test_nothing_to_assess_leaves_a_standing_issue_alone(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """``active is None`` after an urgent leak must not retract the warning.

    A window that went entirely masked or unbounded — or a statistics import
    that failed — has no evidence either way (A4). Treating that silence as an
    all-clear would let a live burst pipe's nudge vanish on the next pass.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)
    await push(hass, mock_config_entry, make_result(leak=urgent_leak()))
    assert leak_issue(hass) is not None

    await push(hass, mock_config_entry, make_result())

    issue = leak_issue(hass)
    assert issue is not None
    assert issue.translation_placeholders == {
        "device": DEVICE_NAME,
        "liters_per_day": "1200",
    }


async def test_issue_is_refiled_only_when_the_quoted_volume_changes(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A steady leak does not churn the Repairs registry on every engine pass.

    Asserted from outside, on the registry's own event bus: the first urgent
    verdict creates the issue, a later pass whose rendered daily volume is
    unchanged must leave the registry completely untouched, and a materially
    larger leak re-renders it.
    """
    actions: list[str] = []

    @callback
    def _record(event: Event[ir.EventIssueRegistryUpdatedData]) -> None:
        """Record registry actions concerning this device's leak nudge."""
        if event.data["domain"] == DOMAIN and event.data["issue_id"] == LEAK_ISSUE_ID:
            actions.append(event.data["action"])

    hass.bus.async_listen(ir.EVENT_REPAIRS_ISSUE_REGISTRY_UPDATED, _record)
    await boot(hass, mock_config_entry, mock_api, freezer)
    assert actions == []

    await push(hass, mock_config_entry, make_result(leak=urgent_leak(1200.0)))
    assert actions == ["create"]

    await push(hass, mock_config_entry, make_result(leak=urgent_leak(1200.4)))
    assert actions == ["create"]

    await push(hass, mock_config_entry, make_result(leak=urgent_leak(1300.0)))
    assert actions == ["create", "update"]

    issue = leak_issue(hass)
    assert issue is not None
    assert issue.translation_placeholders == {
        "device": DEVICE_NAME,
        "liters_per_day": "1300",
    }


async def test_leak_restarting_refiles_a_dismissed_issue(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A leak that stopped and started again resurfaces past an "Ignore".

    Clearing deletes the registry entry, taking ``dismissed_version`` with it,
    so the next urgent verdict files a genuinely new issue — exactly right for a
    second burst after the first was acknowledged.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)
    await push(hass, mock_config_entry, make_result(leak=urgent_leak()))
    ir.async_get(hass).async_ignore(DOMAIN, LEAK_ISSUE_ID, ignore=True)

    await push(
        hass, mock_config_entry, make_result(leak=replace(NEUTRAL_LEAK, active=False))
    )
    assert leak_issue(hass) is None

    await push(hass, mock_config_entry, make_result(leak=urgent_leak()))

    issue = leak_issue(hass)
    assert issue is not None
    assert issue.dismissed_version is None


async def test_removing_the_entry_deletes_a_standing_leak_issue(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Uninstalling the integration withdraws the nudge instead of orphaning it.

    The Repairs registry outlives config entries, so without the
    ``async_remove_entry`` cleanup an uninstalled integration's burst-pipe card
    would keep nagging until the next restart. The ids are rebuilt from the
    device registry — which the entity platforms filled during setup — because
    the entry is already unloaded when the removal hook runs.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)
    await push(hass, mock_config_entry, make_result(leak=urgent_leak()))
    assert leak_issue(hass) is not None

    assert await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert leak_issue(hass) is None


async def test_unloading_the_entry_stops_watching_the_engine(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The engine listener is dropped on unload, so no nudge follows it.

    The watcher is registered through ``entry.async_on_unload``; an unloaded
    entry that somehow still saw a result must not file Repairs issues for a
    device Home Assistant no longer manages.
    """
    await boot(hass, mock_config_entry, mock_api, freezer)
    engine = engine_of(mock_config_entry)

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    engine.async_set_updated_data(make_result(leak=urgent_leak()))
    await hass.async_block_till_done()

    assert leak_issue(hass) is None
