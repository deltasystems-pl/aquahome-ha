"""Tests for the automation tier's repair issues and their two fix flows.

Phase 8 adds three per-device Repairs entries on top of the detection tier's
urgent-leak nudge, all of them driven by the analytics engine and the
per-device scheduler:

* ``leak_while_away_{slug}`` — an error-severity, *any tier* leak escalation
  raised only while the household is away, with a matching ``leak_while_away``
  bus event fired once per onset. Away has two independent sources (a detected
  vacation and a declared deferral) and both are exercised here. As built, the
  urgent-tier issue stands *down* while away — it is deleted, dismissal and
  all — and refiles on the first evaluation after the return.
* ``vacation_defer_{slug}`` — a fixable suggestion offered while a detected
  absence runs and the user has neither armed the follower nor started the
  deferral, withdrawn again the moment any of those three facts changes.
* ``regen_time_{slug}`` — a fixable proposal to move the device's
  ``regeneration_time`` into an hour the learned activity grid finds quiet on
  every weekday. The proposal math is pinned here against crafted
  ``GridSummary.active_hours`` grids rather than replayed history, so a detector
  change can never silently move a *user-confirmed settings write*.

The integration is booted end-to-end against the captured fixtures (the real
settings document configures ``regeneration_time`` = ``7200`` = 02:00), with no
entity platform forwarded: every watcher is wired in ``__init__`` and owns no
entity, so the only user-visible surfaces asserted are the Repairs registry, the
event bus, the scheduler state and the outgoing ``PATCH /settings`` body.
Analytics verdicts are crafted :class:`AnalyticsResult` values pushed straight
into the engine, which keeps each assertion independent of the numeric detector
work; the clock is frozen before setup and the stored access token minted
against it, so nothing depends on wall time.

Fix flows are driven both ways: directly through
``repairs.async_create_fix_flow`` for the behaviour matrix, and once end-to-end
over the real ``/api/repairs/issues/fix`` HTTP endpoints so the flow is proven
to work through Home Assistant's own flow manager.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from aioresponses import aioresponses
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_ACCESS_TOKEN
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import CLIENT_ID

from custom_components.aquahome import repairs as repairs_module
from custom_components.aquahome.analytics.model import (
    TIER_INFO,
    TIER_URGENT,
    TIER_WARNING,
    AnalyticsResult,
    AnomalyState,
    ForecastState,
    GridSummary,
    LeakState,
    VacationState,
)
from custom_components.aquahome.const import (
    DEFERRAL_SOURCE_AUTO,
    DEFERRAL_SOURCE_MANUAL,
    DOMAIN,
    EVENT_AQUAHOME,
    EVENT_TYPE_LEAK_WHILE_AWAY,
    QUIET_REGEN_CANDIDATE_HOURS,
)
from custom_components.aquahome.scheduler import DECISION_DEFERRED
from tests.conftest import (
    TEST_DEVICE_ID,
    add_device_routes,
    load_fixture,
    make_access_token,
    patch_settings_route,
    settings_url,
    with_setting_value,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence

    from aioresponses.core import RequestCall
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.auth.models import Credentials
    from homeassistant.core import Event, HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser
    from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

    from custom_components.aquahome.analytics.engine import AquaHomeAnalyticsEngine
    from custom_components.aquahome.scheduler import AquaHomeRegenScheduler

#: Slug of the captured device's serial ``4213377-30105-2242``.
SLUG = "4213377_30105_2242"
#: The captured device's nickname, rendered into every ``device`` placeholder.
DEVICE_NAME = "Demo"

#: The three automation-tier issue ids, plus the detection tier's urgent one.
LEAK_AWAY_ISSUE_ID = f"leak_while_away_{SLUG}"
VACATION_DEFER_ISSUE_ID = f"vacation_defer_{SLUG}"
REGEN_TIME_ISSUE_ID = f"regen_time_{SLUG}"
LEAK_URGENT_ISSUE_ID = f"leak_urgent_{SLUG}"

#: Instant every test freezes to before setup, matching the sibling suites:
#: inside the fixtures' capture window, so nothing depends on wall time.
FROZEN_INSTANT = "2026-07-21T12:00:00+00:00"
#: Stamped on every crafted result; never rendered, kept equal to the frozen clock.
COMPUTED_AT = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)

#: The captured settings document configures ``regeneration_time`` = 7200 s.
SETTING_NAME = "regeneration_time"
CURRENT_LABEL = "02:00"
CURRENT_HOUR = 2

#: Hours in the learned grid (``weekday(Mon=0) * 24 + hour``).
HOURS_PER_DAY = 24
WEEKDAYS = 7
GRID_HOURS = WEEKDAYS * HOURS_PER_DAY

#: Saturday 02:00 in use and nothing else: a single busy weekday is enough to
#: make the configured regeneration hour a bad one. Deliberately not Monday —
#: the grid is indexed by hour of week, and an implementation that only looked
#: at the first weekday's row would pass every assertion built on Monday.
BUSY_CONFIGURED_HOUR: dict[int, list[int]] = {5: [CURRENT_HOUR]}
#: 00:00 through 05:00 in use, spread over three different weekdays: every
#: candidate hour except 22:00 and 23:00 is busy somewhere in the week.
BUSY_EARLY_NIGHT: dict[int, list[int]] = {3: [0, 1], 5: [2, 3], 6: [4, 5]}

#: Host prefix the ``hass_client`` test server binds to; passed through the
#: aioresponses patch so the Repairs HTTP endpoints are reachable for real.
LOOPBACK = "http://127.0.0.1"

#: One of the three ways a standing vacation-defer suggestion is answered.
type ClearAction = Callable[["HomeAssistant", "MockConfigEntry"], Awaitable[None]]


# ---------------------------------------------------------------------------
# Crafted analytics results
#
# One neutral value per state dataclass (the shape a pass with nothing to
# assess produces) plus a factory that overrides only the parts a test is
# about. Everything is frozen, so the module-level constants are safe defaults.
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
NEUTRAL_VACATION = VacationState(active=None, consecutive_days=0, since=None)
NEUTRAL_FORECAST = ForecastState(
    gallons=None,
    liters=None,
    source=None,
    band_liters=None,
    weekday=None,
    persons=None,
)


def _grid(busy: Mapping[int, Sequence[int]]) -> GridSummary:
    """Return a learned grid whose only active buckets are ``{weekday: hours}``.

    Weekdays are Home Assistant's ``date.weekday()`` convention (Monday = 0) and
    hours are hours of the day, exactly as :mod:`~custom_components.aquahome.issues`
    indexes ``active_hours``. Everything not named is quiet.
    """
    flags = [False] * GRID_HOURS
    for weekday, hours in busy.items():
        for hour in hours:
            flags[weekday * HOURS_PER_DAY + hour] = True
    return GridSummary(
        active_hours=tuple(flags), mature_buckets=GRID_HOURS, hourly_samples=4032
    )


#: A household that uses no water at any hour of the week.
QUIET_GRID = _grid({})


def _result(
    *,
    leak: LeakState = NEUTRAL_LEAK,
    vacation: VacationState = NEUTRAL_VACATION,
    grid: GridSummary = QUIET_GRID,
    forecast: ForecastState = NEUTRAL_FORECAST,
) -> AnalyticsResult:
    """Assemble one crafted analytics result from the neutral defaults.

    The forecast deliberately defaults to "unknown": with it absent the
    scheduler's nightly decision always stops at ``skipped_no_forecast``, so
    arming ``smart_regeneration`` for the quiet-hour proposal tests can never
    send a regeneration command as a side effect.
    """
    return AnalyticsResult(
        computed_at=COMPUTED_AT,
        nights=(),
        days=(),
        leak=leak,
        anomaly=NEUTRAL_ANOMALY,
        vacation=vacation,
        forecast=forecast,
        grid=grid,
    )


def _leak(
    tier: str, *, active: bool | None = True, liters_per_day: float = 300.0
) -> LeakState:
    """Return a leak verdict at one tier (the away issue files at any of them)."""
    return LeakState(
        active=active,
        consecutive_nights=2,
        rate_liters_per_hour=liters_per_day / 24,
        implied_liters_per_day=liters_per_day,
        tier=tier,
        persistent_flow=True,
        last_verdict_night=date(2026, 7, 21),
        masking_coverage=True,
    )


def _vacation(active: bool | None, *, days: int = 5) -> VacationState:
    """Return a vacation verdict with its consecutive-day evidence."""
    return VacationState(
        active=active,
        consecutive_days=days if active else 0,
        since=date(2026, 7, 16) if active else None,
    )


# ---------------------------------------------------------------------------
# Boot / access helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_platforms() -> Iterator[None]:
    """Forward no entity platform: these tests assert registry state only.

    Every watcher here is wired in ``__init__`` before the platforms are set up
    and owns no entity, so leaving them out keeps each failure attributable to
    the issue/flow logic alone.
    """
    with patch("custom_components.aquahome.PLATFORMS", []):
        yield


async def _boot(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Freeze the clock, set the entry up, and settle the startup pipeline.

    The stored access token is re-minted against the frozen clock so the auth
    manager never decides it is stale mid-test, which has to happen between
    adding the entry and setting it up — hence the unrolled ``setup_integration``.
    Settling with ``wait_background_tasks`` matters: the engine's first pass runs
    as an entry background task and would otherwise overwrite a crafted result
    pushed before it finished.
    """
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(mock)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_ACCESS_TOKEN: make_access_token()}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


def _engine(entry: MockConfigEntry) -> AquaHomeAnalyticsEngine:
    """Return the analytics engine the entry built for the fixture device."""
    engine: AquaHomeAnalyticsEngine = entry.runtime_data.analytics_engines[
        TEST_DEVICE_ID
    ]
    return engine


def _scheduler(entry: MockConfigEntry) -> AquaHomeRegenScheduler:
    """Return the automation scheduler the entry built for the fixture device."""
    scheduler: AquaHomeRegenScheduler = entry.runtime_data.schedulers[TEST_DEVICE_ID]
    return scheduler


async def _push(
    hass: HomeAssistant, entry: MockConfigEntry, result: AnalyticsResult
) -> None:
    """Publish a crafted analytics result and settle every listener.

    The scheduler's engine listener hands its pass to a task, so the block is
    what makes the automation state (and any issue it clears) observable.
    """
    _engine(entry).async_set_updated_data(result)
    await hass.async_block_till_done()


async def _set_deferral(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    active: bool,
    *,
    source: str = DEFERRAL_SOURCE_MANUAL,
) -> None:
    """Move the vacation-deferral flag and settle the watchers it publishes to."""
    await _scheduler(entry).async_set_vacation_deferral(active, source=source)
    await hass.async_block_till_done()


async def _arm_smart_regeneration(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Turn the smart-regeneration opt-in on (the quiet-hour proposal's gate)."""
    await _scheduler(entry).async_set_smart_regeneration(True)
    await hass.async_block_till_done()


def _issue(hass: HomeAssistant, issue_id: str) -> ir.IssueEntry | None:
    """Return one repair issue currently in the registry, or ``None``."""
    return ir.async_get(hass).issues.get((DOMAIN, issue_id))


def _issue_actions(hass: HomeAssistant, issue_id: str) -> list[str]:
    """Collect every registry action taken on one issue id from now on."""
    actions: list[str] = []

    @callback
    def _record(event: Event[ir.EventIssueRegistryUpdatedData]) -> None:
        """Append one registry action concerning the watched issue."""
        if event.data["domain"] == DOMAIN and event.data["issue_id"] == issue_id:
            actions.append(event.data["action"])

    hass.bus.async_listen(ir.EVENT_REPAIRS_ISSUE_REGISTRY_UPDATED, _record)
    return actions


def _away_events(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Collect every ``leak_while_away`` bus payload fired from now on."""
    payloads: list[dict[str, Any]] = []

    @callback
    def _collect(event: Event[dict[str, Any]]) -> None:
        """Append one away-leak announcement, ignoring other automation events."""
        if event.data.get("type") == EVENT_TYPE_LEAK_WHILE_AWAY:
            payloads.append(dict(event.data))

    hass.bus.async_listen(EVENT_AQUAHOME, _collect)
    return payloads


def _settings_requests(mock: aioresponses, method: str) -> list[RequestCall]:
    """Return every recorded request to the ``/settings`` path for one method."""
    return [
        call
        for (call_method, url), calls in mock.requests.items()
        if call_method == method and url.path.endswith("/settings")
        for call in calls
    ]


def _echo_document(seconds: str) -> dict[str, Any]:
    """Return the settings document the server echoes after a successful write."""
    return with_setting_value(load_fixture("settings.json"), SETTING_NAME, seconds)


# ---------------------------------------------------------------------------
# Leak while away: any tier, both away sources, never at home
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tier", "liters"),
    [
        pytest.param(TIER_INFO, 144.0, id="info"),
        pytest.param(TIER_WARNING, 480.0, id="warning"),
        pytest.param(TIER_URGENT, 1200.0, id="urgent"),
    ],
)
async def test_detected_vacation_files_the_away_leak_at_any_tier(  # noqa: PLR0913 - standard HA fixture set plus the parametrized row
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    tier: str,
    liters: float,
) -> None:
    """Water moving through an empty house escalates whatever its tier.

    At home only the burst-pipe tier is loud enough for Repairs; away, the
    slowest confirmed trickle is already either a fault or damage in progress
    (owner decision 2026-07-27), so every tier files the same error-severity
    issue and it quotes the tier it was filed at.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)
    assert _issue(hass, LEAK_AWAY_ISSUE_ID) is None

    await _push(
        hass,
        mock_config_entry,
        _result(leak=_leak(tier, liters_per_day=liters), vacation=_vacation(True)),
    )

    issue = _issue(hass, LEAK_AWAY_ISSUE_ID)
    assert issue is not None
    assert issue.translation_key == "leak_while_away"
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.is_fixable is False
    assert issue.is_persistent is False
    assert issue.translation_placeholders == {
        "device": DEVICE_NAME,
        "liters_per_day": str(int(liters)),
        "tier": tier,
    }


async def test_declared_deferral_alone_files_the_away_leak(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The deferral flag is an away signal in its own right.

    A household that declared an absence through the switch, the service or a
    blueprint gets the escalation even when the detector has not (or not yet)
    called it a vacation — and the flag moves without an analytics pass, which
    is why the watcher listens to the scheduler as well as the engine.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)
    await _push(
        hass,
        mock_config_entry,
        _result(leak=_leak(TIER_INFO), vacation=_vacation(False)),
    )
    assert _issue(hass, LEAK_AWAY_ISSUE_ID) is None

    await _set_deferral(hass, mock_config_entry, True)

    assert _issue(hass, LEAK_AWAY_ISSUE_ID) is not None

    await _set_deferral(hass, mock_config_entry, False)

    assert _issue(hass, LEAK_AWAY_ISSUE_ID) is None


@pytest.mark.parametrize(
    "tier",
    [pytest.param(TIER_INFO, id="info"), pytest.param(TIER_URGENT, id="urgent")],
)
async def test_leak_at_home_never_files_the_away_issue(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    tier: str,
) -> None:
    """With the household home the away escalation stays silent at every tier.

    Someone in the house can be running a tap; the loud "nobody is home" framing
    would be wrong, so the detection tier's rules apply unchanged.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)

    await _push(
        hass,
        mock_config_entry,
        _result(leak=_leak(tier), vacation=_vacation(False)),
    )

    assert _issue(hass, LEAK_AWAY_ISSUE_ID) is None
    assert _scheduler(mock_config_entry).state.vacation_deferral is False


async def test_away_leak_clears_when_the_flow_stops(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A detector that confirms the flow stopped withdraws the escalation."""
    await _boot(hass, mock_config_entry, mock_api, freezer)
    await _push(
        hass,
        mock_config_entry,
        _result(leak=_leak(TIER_WARNING), vacation=_vacation(True)),
    )
    assert _issue(hass, LEAK_AWAY_ISSUE_ID) is not None

    await _push(
        hass,
        mock_config_entry,
        _result(leak=_leak(TIER_WARNING, active=False), vacation=_vacation(True)),
    )

    assert _issue(hass, LEAK_AWAY_ISSUE_ID) is None


async def test_nothing_to_assess_leaves_a_standing_away_leak_alone(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A pass with no evidence retracts nothing and re-announces nothing.

    ``active is None`` means the detector had nothing to assess — a window that
    went entirely masked, or a statistics import that failed — not an all-clear.
    Treating that silence as "the leak stopped" would withdraw a live escalation
    from an empty house and, worse, arm the onset announcement again, so the very
    next pass would notify the household a second time about one leak.
    """
    events = _away_events(hass)
    await _boot(hass, mock_config_entry, mock_api, freezer)
    away_leak = _result(leak=_leak(TIER_WARNING), vacation=_vacation(True))
    await _push(hass, mock_config_entry, away_leak)
    assert len(events) == 1

    await _push(hass, mock_config_entry, _result(vacation=_vacation(True)))

    issue = _issue(hass, LEAK_AWAY_ISSUE_ID)
    assert issue is not None
    assert issue.translation_placeholders == {
        "device": DEVICE_NAME,
        "liters_per_day": "300",
        "tier": TIER_WARNING,
    }

    await _push(hass, mock_config_entry, away_leak)

    assert len(events) == 1


async def test_urgent_issue_stands_down_while_away_and_refiles_on_return(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Away supersedes the urgent nudge, which returns fresh with the household.

    The as-built rule: the urgent-tier issue is *deleted* for the duration of
    the absence (two Repairs cards for one leak would split the user's
    attention) and the away issue speaks for it. Deleting takes the
    registry entry's ``dismissed_version`` with it, so a leak that was ignored
    before the trip is deliberately put back in front of the user on return —
    and that return is noticed on the very next evaluation, not the next nightly
    analytics pass, because the watcher also listens to the scheduler.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)
    await _push(
        hass,
        mock_config_entry,
        _result(leak=_leak(TIER_URGENT, liters_per_day=1200.0)),
    )
    assert _issue(hass, LEAK_URGENT_ISSUE_ID) is not None
    ir.async_get(hass).async_ignore(DOMAIN, LEAK_URGENT_ISSUE_ID, ignore=True)
    dismissed = _issue(hass, LEAK_URGENT_ISSUE_ID)
    assert dismissed is not None
    assert dismissed.dismissed_version is not None

    await _set_deferral(hass, mock_config_entry, True)

    assert _issue(hass, LEAK_URGENT_ISSUE_ID) is None
    assert _issue(hass, LEAK_AWAY_ISSUE_ID) is not None

    await _set_deferral(hass, mock_config_entry, False)

    restored = _issue(hass, LEAK_URGENT_ISSUE_ID)
    assert restored is not None
    assert restored.dismissed_version is None
    assert restored.translation_placeholders == {
        "device": DEVICE_NAME,
        "liters_per_day": "1200",
    }
    assert _issue(hass, LEAK_AWAY_ISSUE_ID) is None


async def test_away_leak_event_fires_once_per_onset(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The bus announcement marks the onset, not every re-render of the issue.

    The blueprint behind this event pushes an actionable notification, so a
    household that has already been told must not be told again because the
    measured volume moved. Only a condition that has been false in between
    counts as a new onset.
    """
    events = _away_events(hass)
    await _boot(hass, mock_config_entry, mock_api, freezer)
    assert events == []

    await _push(
        hass,
        mock_config_entry,
        _result(
            leak=_leak(TIER_WARNING, liters_per_day=480.0), vacation=_vacation(True)
        ),
    )

    assert events == [
        {
            "device_id": TEST_DEVICE_ID,
            "device": SLUG,
            "type": EVENT_TYPE_LEAK_WHILE_AWAY,
            "tier": TIER_WARNING,
            "rate_liters_per_hour": 480.0 / 24,
            "implied_liters_per_day": 480.0,
        }
    ]

    # A larger leak re-renders the issue but is the same, still-running onset.
    await _push(
        hass,
        mock_config_entry,
        _result(
            leak=_leak(TIER_WARNING, liters_per_day=600.0), vacation=_vacation(True)
        ),
    )

    issue = _issue(hass, LEAK_AWAY_ISSUE_ID)
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["liters_per_day"] == "600"
    assert len(events) == 1

    # The flow stops and starts again: a genuinely new onset, announced again.
    await _push(
        hass,
        mock_config_entry,
        _result(leak=_leak(TIER_WARNING, active=False), vacation=_vacation(True)),
    )
    await _push(
        hass,
        mock_config_entry,
        _result(
            leak=_leak(TIER_WARNING, liters_per_day=480.0), vacation=_vacation(True)
        ),
    )

    assert len(events) == 2


# ---------------------------------------------------------------------------
# Vacation-defer suggestion: lifecycle
# ---------------------------------------------------------------------------


async def test_vacation_defer_suggestion_files_on_a_detected_absence(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A detected absence with both flags off is offered as a fixable suggestion.

    Regenerating a softener that is treating no water spends salt, water and
    resin capacity on nothing — but starting the deferral is device-affecting,
    so it is offered rather than done, and the issue carries the identifiers its
    fix flow needs to find the right device across a reload.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)
    assert _issue(hass, VACATION_DEFER_ISSUE_ID) is None

    await _push(hass, mock_config_entry, _result(vacation=_vacation(True, days=6)))

    issue = _issue(hass, VACATION_DEFER_ISSUE_ID)
    assert issue is not None
    assert issue.translation_key == "vacation_defer"
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.is_fixable is True
    assert issue.is_persistent is False
    assert issue.data == {
        "entry_id": mock_config_entry.entry_id,
        "device_id": TEST_DEVICE_ID,
    }
    assert issue.translation_placeholders == {
        "device": DEVICE_NAME,
        "consecutive_days": "6",
    }


async def _end_the_vacation(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Clear the suggestion by having the detector see the household return."""
    await _push(hass, entry, _result(vacation=_vacation(False)))


async def _start_the_deferral(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Clear the suggestion by starting the deferral the user was offered."""
    await _set_deferral(hass, entry, True)


async def _arm_auto_vacation(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Clear the suggestion by arming the follower that automates the answer.

    Armed during a data gap (``vacation.active is None``, which leaves a
    standing suggestion untouched) so it is the *flag alone* that retires the
    issue: arming the follower while the detector still reports the absence
    would immediately start the deferral too, and the assertion could then not
    tell the two clears apart. The flags are deliberately checked ahead of the
    verdict, so a user acting during a gap still sees the card disappear.
    """
    await _push(hass, entry, _result(vacation=_vacation(None)))
    assert _issue(hass, VACATION_DEFER_ISSUE_ID) is not None

    await _scheduler(entry).async_set_auto_vacation(True)
    await hass.async_block_till_done()

    assert _scheduler(entry).state.auto_vacation is True
    assert _scheduler(entry).state.vacation_deferral is False


@pytest.mark.parametrize(
    "clear",
    [
        pytest.param(_end_the_vacation, id="vacation-ended"),
        pytest.param(_start_the_deferral, id="deferral-started"),
        pytest.param(_arm_auto_vacation, id="auto-vacation-armed"),
    ],
)
async def test_vacation_defer_suggestion_is_withdrawn(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    clear: ClearAction,
) -> None:
    """Each of the three answers to the question retires it.

    Nothing is offered to a household that already decided: the absence ended,
    the deferral is running, or the follower will handle it from now on. All
    three are asserted from the same standing suggestion.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)
    await _push(hass, mock_config_entry, _result(vacation=_vacation(True)))
    assert _issue(hass, VACATION_DEFER_ISSUE_ID) is not None

    await clear(hass, mock_config_entry)

    assert _issue(hass, VACATION_DEFER_ISSUE_ID) is None


# ---------------------------------------------------------------------------
# Quiet-hour proposal: the grid maths behind a user-confirmed settings write
# ---------------------------------------------------------------------------


async def test_no_proposal_while_smart_regeneration_is_off(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The proposal is gated on the opt-in, whatever the grid says.

    The switch defaults off, so a fresh install never proposes rewriting a
    device setting it was not invited to touch.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)

    await _push(hass, mock_config_entry, _result(grid=_grid(BUSY_CONFIGURED_HOUR)))

    assert _issue(hass, REGEN_TIME_ISSUE_ID) is None


async def test_busy_configured_hour_proposes_the_nearest_quiet_hour(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """One busy weekday at 02:00 is enough, and the nearest quiet hour wins.

    The captured device regenerates at 02:00. With only Saturday's 02:00 bucket
    active the hour is already a bad time to lose softened water — the grid is
    read across all seven weekday rows, not just one — and 01:00 and 03:00 are
    both one hour away, so the tie goes to the earlier hour for an answer that is
    stable rather than dependent on iteration order.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)
    await _arm_smart_regeneration(hass, mock_config_entry)

    await _push(hass, mock_config_entry, _result(grid=_grid(BUSY_CONFIGURED_HOUR)))

    issue = _issue(hass, REGEN_TIME_ISSUE_ID)
    assert issue is not None
    assert issue.translation_key == "regen_time"
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.is_fixable is True
    assert issue.data == {
        "entry_id": mock_config_entry.entry_id,
        "device_id": TEST_DEVICE_ID,
        "proposed_seconds": "3600",
        "proposed_label": "01:00",
        "current_label": CURRENT_LABEL,
    }
    assert issue.translation_placeholders == {
        "device": DEVICE_NAME,
        "current": CURRENT_LABEL,
        "proposed": "01:00",
    }


async def test_proposal_measures_distance_around_midnight(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """23:00 is three hours from 02:00, not twenty-one.

    With 00:00-05:00 in use across three different weekdays the only candidates
    left are 22:00 and 23:00. A naive absolute difference would rank 22:00
    (|22 - 2| = 20) ahead of 23:00 (21) and move the regeneration an hour further
    than necessary; the circular distance picks 23:00.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)
    await _arm_smart_regeneration(hass, mock_config_entry)

    await _push(hass, mock_config_entry, _result(grid=_grid(BUSY_EARLY_NIGHT)))

    issue = _issue(hass, REGEN_TIME_ISSUE_ID)
    assert issue is not None
    assert issue.data == {
        "entry_id": mock_config_entry.entry_id,
        "device_id": TEST_DEVICE_ID,
        "proposed_seconds": "82800",
        "proposed_label": "23:00",
        "current_label": CURRENT_LABEL,
    }


@pytest.mark.parametrize(
    "busy",
    [
        pytest.param({}, id="grid-entirely-quiet"),
        pytest.param({2: [8, 9], 5: [19, 20]}, id="busy-elsewhere-only"),
    ],
)
async def test_no_proposal_when_the_configured_hour_is_already_quiet(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    busy: Mapping[int, Sequence[int]],
) -> None:
    """A regeneration that already sits in a quiet hour is left alone.

    There is no problem to fix: nagging a correctly configured device would only
    train the user to dismiss Repairs cards.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)
    await _arm_smart_regeneration(hass, mock_config_entry)

    await _push(hass, mock_config_entry, _result(grid=_grid(busy)))

    assert _issue(hass, REGEN_TIME_ISSUE_ID) is None


async def test_no_proposal_when_every_candidate_hour_is_busy(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A household busy through every night hour gets no proposal at all.

    Only :data:`QUIET_REGEN_CANDIDATE_HOURS` may be proposed, and only when the
    grid finds them quiet on all seven weekdays. With none of them free the
    honest answer is silence rather than a bad suggestion — and definitely not
    an hour that is quiet on average but busy on Sundays.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)
    await _arm_smart_regeneration(hass, mock_config_entry)

    # Each candidate hour is used on a different weekday, so no hour of the day
    # is quiet across the whole week even though every single day looks calm.
    busy: dict[int, list[int]] = {}
    for index, hour in enumerate(QUIET_REGEN_CANDIDATE_HOURS):
        busy.setdefault(index % WEEKDAYS, []).append(hour)
    await _push(hass, mock_config_entry, _result(grid=_grid(busy)))

    assert _issue(hass, REGEN_TIME_ISSUE_ID) is None


async def test_changed_proposal_refiles_while_a_steady_grid_does_not_churn(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A different hour is a different question, so it is asked again.

    Asserted from outside on the issue registry's own event bus: an unchanged
    proposal must not touch the registry on every engine pass, while a moved
    proposal is deleted and re-created — which clears any dismissal, because the
    user is now being asked to confirm a different settings write.
    """
    actions = _issue_actions(hass, REGEN_TIME_ISSUE_ID)
    await _boot(hass, mock_config_entry, mock_api, freezer)
    await _arm_smart_regeneration(hass, mock_config_entry)
    assert actions == []

    await _push(hass, mock_config_entry, _result(grid=_grid(BUSY_CONFIGURED_HOUR)))
    assert actions == ["create"]

    await _push(hass, mock_config_entry, _result(grid=_grid(BUSY_CONFIGURED_HOUR)))
    assert actions == ["create"]

    await _push(hass, mock_config_entry, _result(grid=_grid(BUSY_EARLY_NIGHT)))

    assert actions == ["create", "remove", "create"]
    issue = _issue(hass, REGEN_TIME_ISSUE_ID)
    assert issue is not None
    assert issue.translation_placeholders == {
        "device": DEVICE_NAME,
        "current": CURRENT_LABEL,
        "proposed": "23:00",
    }


# ---------------------------------------------------------------------------
# Fix flows, driven directly
# ---------------------------------------------------------------------------


async def _file_vacation_defer_issue(
    hass: HomeAssistant, entry: MockConfigEntry
) -> ir.IssueEntry:
    """Bring the device to a standing vacation-defer suggestion."""
    await _push(hass, entry, _result(vacation=_vacation(True, days=6)))
    issue = _issue(hass, VACATION_DEFER_ISSUE_ID)
    assert issue is not None
    return issue


async def _file_regen_time_issue(
    hass: HomeAssistant, entry: MockConfigEntry
) -> ir.IssueEntry:
    """Bring the device to a standing quiet-hour proposal (02:00 -> 01:00)."""
    await _arm_smart_regeneration(hass, entry)
    await _push(hass, entry, _result(grid=_grid(BUSY_CONFIGURED_HOUR)))
    issue = _issue(hass, REGEN_TIME_ISSUE_ID)
    assert issue is not None
    return issue


async def test_vacation_fix_flow_confirms_into_an_auto_deferral(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Confirming the suggestion starts a self-releasing deferral.

    The user confirmed an answer to a *detected* absence, so the deferral is
    recorded as ``auto``: the same detector that proposed it releases it again
    when the household comes home. The confirmation form carries the issue's own
    placeholders, so the dialog names the device rather than showing raw keys.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)
    issue = await _file_vacation_defer_issue(hass, mock_config_entry)

    flow = await repairs_module.async_create_fix_flow(
        hass, VACATION_DEFER_ISSUE_ID, issue.data
    )
    assert isinstance(flow, repairs_module.VacationDeferFixFlow)
    flow.hass = hass
    flow.issue_id = VACATION_DEFER_ISSUE_ID
    flow.data = issue.data

    form = await flow.async_step_init(None)
    assert form["type"] is FlowResultType.FORM
    assert form["step_id"] == "confirm"
    assert form["description_placeholders"] == {
        "device": DEVICE_NAME,
        "consecutive_days": "6",
    }
    # Nothing may happen before the user actually submits the form.
    assert _scheduler(mock_config_entry).state.vacation_deferral is False

    result = await flow.async_step_confirm({})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    state = _scheduler(mock_config_entry).state
    assert state.vacation_deferral is True
    assert state.deferral_source == DEFERRAL_SOURCE_AUTO
    assert state.deferral_started == datetime.fromisoformat(FROZEN_INSTANT)
    assert state.last_decision == DECISION_DEFERRED
    # The question has been answered: the suggestion retires itself.
    assert _issue(hass, VACATION_DEFER_ISSUE_ID) is None


async def test_regen_time_fix_flow_writes_the_proposed_setting(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Confirming the proposal PATCHes exactly the value that was offered.

    The written value comes from the issue's own data, not from a re-derivation
    at confirm time: what the user agreed to is what is sent, even if the grid
    has moved since the card was filed. The server echoes the refreshed document
    back, and the proposal withdraws itself because 01:00 is quiet.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)
    issue = await _file_regen_time_issue(hass, mock_config_entry)
    patch_settings_route(mock_api, payload=_echo_document("3600"))

    flow = await repairs_module.async_create_fix_flow(
        hass, REGEN_TIME_ISSUE_ID, issue.data
    )
    assert isinstance(flow, repairs_module.RegenTimeFixFlow)
    flow.hass = hass
    flow.issue_id = REGEN_TIME_ISSUE_ID
    flow.data = issue.data

    form = await flow.async_step_init(None)
    assert form["type"] is FlowResultType.FORM
    assert form["description_placeholders"] == {
        "device": DEVICE_NAME,
        "current": CURRENT_LABEL,
        "proposed": "01:00",
    }
    assert _settings_requests(mock_api, "PATCH") == []

    result = await flow.async_step_confirm({})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    (patch_call,) = _settings_requests(mock_api, "PATCH")
    assert patch_call.kwargs["json"] == {"settings": {SETTING_NAME: "3600"}}
    assert _issue(hass, REGEN_TIME_ISSUE_ID) is None


async def test_regen_time_fix_flow_aborts_when_the_write_fails(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A cloud refusal is reported plainly and leaves the proposal standing.

    The user is looking at the dialog, so the honest answer is an abort saying
    the setting is unchanged; the issue stays filed so the same proposal can be
    confirmed again once the cloud recovers.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)
    issue = await _file_regen_time_issue(hass, mock_config_entry)
    mock_api.patch(settings_url(), status=500, payload={"detail": "upstream failure"})

    flow = await repairs_module.async_create_fix_flow(
        hass, REGEN_TIME_ISSUE_ID, issue.data
    )
    assert isinstance(flow, repairs_module.RegenTimeFixFlow)
    flow.hass = hass
    flow.issue_id = REGEN_TIME_ISSUE_ID
    flow.data = issue.data

    await flow.async_step_init(None)
    result = await flow.async_step_confirm({})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "write_failed"
    assert len(_settings_requests(mock_api, "PATCH")) == 1
    assert _issue(hass, REGEN_TIME_ISSUE_ID) is not None


async def test_regen_time_fix_flow_aborts_without_a_usable_proposal(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Issue data missing the proposed value writes nothing at all.

    Issue data survives restarts as plain JSON and may have been written by
    another version, so every field is treated as untrusted: without a usable
    ``proposed_seconds`` the flow aborts instead of guessing a time to write to
    somebody's softener.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)
    await _file_regen_time_issue(hass, mock_config_entry)
    patch_settings_route(mock_api, payload=_echo_document("3600"))

    flow = await repairs_module.async_create_fix_flow(
        hass,
        REGEN_TIME_ISSUE_ID,
        {"entry_id": mock_config_entry.entry_id, "device_id": TEST_DEVICE_ID},
    )
    assert isinstance(flow, repairs_module.RegenTimeFixFlow)

    result = await flow.async_step_confirm({})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "write_failed"
    assert _settings_requests(mock_api, "PATCH") == []


@pytest.mark.parametrize(
    ("issue_id", "extra"),
    [
        pytest.param(VACATION_DEFER_ISSUE_ID, {}, id="vacation-defer"),
        pytest.param(
            REGEN_TIME_ISSUE_ID, {"proposed_seconds": "3600"}, id="regen-time"
        ),
    ],
)
async def test_fix_flows_abort_when_the_entry_is_gone(  # noqa: PLR0913 - standard HA fixture set plus the parametrized row
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    issue_id: str,
    extra: dict[str, str],
) -> None:
    """A Repairs card outliving its config entry aborts instead of acting.

    Both flows resolve the objects they drive through the entry's runtime data
    at confirm time, so a card that sat in the sidebar until the integration was
    removed has nothing to act with and says so.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)
    patch_settings_route(mock_api, payload=_echo_document("3600"))

    flow = await repairs_module.async_create_fix_flow(
        hass,
        issue_id,
        {"entry_id": "a-config-entry-that-never-existed", **extra},
    )
    assert isinstance(
        flow, repairs_module.VacationDeferFixFlow | repairs_module.RegenTimeFixFlow
    )
    flow.hass = hass
    flow.issue_id = issue_id

    result = await flow.async_step_confirm({})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_loaded"
    assert _settings_requests(mock_api, "PATCH") == []


async def test_vacation_fix_flow_aborts_while_the_entry_is_unloaded(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Only a LOADED entry carries the coordinators a flow is allowed to drive.

    An entry that is disabled, unloaded or failed setup still exists in the
    registry, so the flow checks its state rather than its mere presence.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)
    issue = await _file_vacation_defer_issue(hass, mock_config_entry)
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED

    flow = await repairs_module.async_create_fix_flow(
        hass, VACATION_DEFER_ISSUE_ID, issue.data
    )
    assert isinstance(flow, repairs_module.VacationDeferFixFlow)
    result = await flow.async_step_confirm({})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_loaded"


# ---------------------------------------------------------------------------
# One end-to-end HTTP fix flow through Home Assistant's own flow manager
# ---------------------------------------------------------------------------


async def test_http_fix_flow_applies_the_deferral_and_clears_the_issue(  # noqa: PLR0913 - the HA fixture set plus the admin identity the endpoints require
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    hass_client: ClientSessionGenerator,
    hass_admin_user: MockUser,
    hass_admin_credential: Credentials,
) -> None:
    """The whole flow works through the real Repairs endpoints, not just direct calls.

    Everything the direct-drive tests skip is covered exactly once here: the
    repairs platform is discovered on the custom integration, the flow manager
    builds the flow from the registry entry, the form round-trips as JSON, and
    the manager deletes the resolved issue on completion. The aioresponses patch
    is given the loopback prefix so the test server's own traffic is not
    intercepted alongside the faked iQua cloud.

    The admin's Home Assistant access token is minted *after* the clock is
    frozen: the shared ``hass_access_token`` fixture is built at fixture-setup
    time against the wall clock, and a JWT whose ``iat`` sits in the future of
    the frozen instant is rejected by the auth middleware.
    """
    with aioresponses(passthrough=[LOOPBACK]) as mock:
        await _boot(hass, mock_config_entry, mock, freezer)
        await _file_vacation_defer_issue(hass, mock_config_entry)
        assert await async_setup_component(hass, "repairs", {})
        await hass.async_block_till_done()
        await hass.auth.async_link_user(hass_admin_user, hass_admin_credential)
        refresh_token = await hass.auth.async_create_refresh_token(
            hass_admin_user, CLIENT_ID, credential=hass_admin_credential
        )
        client = await hass_client(hass.auth.async_create_access_token(refresh_token))

        start = await client.post(
            "/api/repairs/issues/fix",
            json={"handler": DOMAIN, "issue_id": VACATION_DEFER_ISSUE_ID},
        )
        assert start.status == HTTPStatus.OK
        form = await start.json()
        assert form["type"] == "form"
        assert form["step_id"] == "confirm"
        assert form["description_placeholders"] == {
            "device": DEVICE_NAME,
            "consecutive_days": "6",
        }

        submit = await client.post(
            f"/api/repairs/issues/fix/{form['flow_id']}", json={}
        )
        assert submit.status == HTTPStatus.OK
        assert (await submit.json())["type"] == "create_entry"
        await hass.async_block_till_done()

        state = _scheduler(mock_config_entry).state
        assert state.vacation_deferral is True
        assert state.deferral_source == DEFERRAL_SOURCE_AUTO
        assert _issue(hass, VACATION_DEFER_ISSUE_ID) is None


# ---------------------------------------------------------------------------
# Uninstall cleanup
# ---------------------------------------------------------------------------


async def test_removing_the_entry_deletes_all_three_automation_issues(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Uninstalling withdraws every automation card, fixable ones included.

    The Repairs registry outlives config entries, so without the
    ``async_remove_entry`` cleanup an uninstalled integration would keep nagging
    until the next restart — and worse, would keep *offering fix flows* for
    coordinators that no longer exist. The ids are rebuilt from the device
    registry, which is the only place the device slugs survive the removal; the
    autouse ``no_platforms`` fixture means no entity registers the device here,
    so the test files the registry entry itself, exactly as the entity platforms
    do in production.
    """
    await _boot(hass, mock_config_entry, mock_api, freezer)
    await _arm_smart_regeneration(hass, mock_config_entry)
    await _push(
        hass,
        mock_config_entry,
        _result(
            leak=_leak(TIER_INFO),
            vacation=_vacation(True),
            grid=_grid(BUSY_CONFIGURED_HOUR),
        ),
    )
    assert _issue(hass, LEAK_AWAY_ISSUE_ID) is not None
    assert _issue(hass, VACATION_DEFER_ISSUE_ID) is not None
    assert _issue(hass, REGEN_TIME_ISSUE_ID) is not None
    dr.async_get(hass).async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, SLUG)},
    )

    assert await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert _issue(hass, LEAK_AWAY_ISSUE_ID) is None
    assert _issue(hass, VACATION_DEFER_ISSUE_ID) is None
    assert _issue(hass, REGEN_TIME_ISSUE_ID) is None
