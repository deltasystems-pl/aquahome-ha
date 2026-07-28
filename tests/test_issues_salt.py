"""Tests for the tiered low-salt repair issues (``issues.py``).

:func:`~custom_components.aquahome.issues.async_setup_salt_issues` is wired per
device in ``__init__.py`` right after the fast coordinator's first refresh, so
every test here boots the real integration through ``setup_integration`` against
the captured fixtures and then walks the device's own
``out_of_salt_estimate_days`` countdown across successive polls (``freezer`` +
``async_fire_time_changed`` on the fixed
:data:`~custom_components.aquahome.const.UPDATE_INTERVAL` cadence, one modified
device-detail payload per poll). Assertions read the Repairs issue registry —
the only user-visible surface this module has.

No entity platform is forwarded (the autouse ``no_platforms`` fixture): the
nudge is derived in ``__init__`` before the platforms are set up and owns no
entity, so leaving them out keeps every failure here attributable to the tier
state machine alone.

The ladder walked by :func:`test_tier_ladder_escalates_de_escalates_and_clears`
is the binding escalation sequence and is deliberately one ordered test: the
tiers are history-dependent (a value inside the hysteresis band means "keep
whatever tier you had"), so the steps only have meaning in sequence.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.aquahome.api import Device
from custom_components.aquahome.const import DOMAIN, UPDATE_INTERVAL
from tests.conftest import (
    TEST_DEVICE_ID,
    add_activity_routes,
    add_settings_routes,
    alerts_url,
    device_url,
    devices_url,
    load_fixture,
    regen_events_url,
    settings_url,
    setup_integration,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any

    from aioresponses import aioresponses
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import Event, HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

#: Slug of the captured device's serial ``4213377-30105-2242``.
SLUG = "4213377_30105_2242"
#: Issue id the captured device's nudge is filed under.
ISSUE_ID = f"low_salt_{SLUG}"
#: The captured device's nickname, rendered into the ``device`` placeholder.
DEVICE_NAME = "Demo"

#: Identity of the synthetic second device used by the per-device scoping test.
SECOND_DEVICE_ID = "9c4b1f22-0d5e-4a71-9b8c-2f6a3d0e71aa"
SECOND_SERIAL = "4213377-30105-2243"
SECOND_SLUG = "4213377_30105_2243"
SECOND_ISSUE_ID = f"low_salt_{SECOND_SLUG}"
SECOND_DEVICE_NAME = "Cottage"

#: Per-tier presentation: (translation_key, severity), as asserted below.
WARNING_TIER = ("salt_level_low", ir.IssueSeverity.WARNING)
CRITICAL_TIER = ("salt_level_critical", ir.IssueSeverity.ERROR)


# ---------------------------------------------------------------------------
# Fixtures and payload / route helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_platforms() -> Iterator[None]:
    """Forward no entity platforms: these tests assert registry state only."""
    with patch("custom_components.aquahome.PLATFORMS", []):
        yield


def _detail_with_countdown(value: float | str | None) -> dict[str, Any]:
    """Return a device-detail payload whose salt countdown carries ``value``.

    ``load_fixture`` re-parses the JSON on every call, so each payload built here
    is an independent document — the loaded fixture is never mutated in place.
    ``None`` removes the property entirely, modelling a device (or a payload)
    that does not report the countdown at all.
    """
    detail = load_fixture("device-detail.json")
    if value is None:
        del detail["properties"]["out_of_salt_estimate_days"]
    else:
        detail["properties"]["out_of_salt_estimate_days"]["value"] = value
    return detail


def _register_polls(mock: aioresponses, details: list[dict[str, Any]]) -> None:
    """Register one device-detail payload per poll, the last one repeating.

    ``details[0]`` is consumed by the coordinator's first refresh during setup
    and each later element by one scheduled poll; the final element repeats so a
    stray extra poll keeps serving it. The device list plus the activity and
    settings routes are served from the real fixtures (repeating) so the tolerant
    setup refreshes of the other coordinators succeed. aioresponses matches
    routes in registration order and consumes a non-``repeat`` route once.
    """
    mock.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    for detail in details[:-1]:
        mock.get(device_url(), payload=detail)
    mock.get(device_url(), payload=details[-1], repeat=True)
    add_activity_routes(mock)
    add_settings_routes(mock)


async def _fire_next_poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Advance one fast-coordinator interval and settle the scheduled refresh."""
    freezer.tick(UPDATE_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def _issue(hass: HomeAssistant, issue_id: str = ISSUE_ID) -> ir.IssueEntry | None:
    """Return the low-salt issue currently in the registry, or ``None``."""
    return ir.async_get(hass).issues.get((DOMAIN, issue_id))


def _assert_issue(
    hass: HomeAssistant,
    tier: tuple[str, ir.IssueSeverity],
    days: str,
    *,
    issue_id: str = ISSUE_ID,
    device: str = DEVICE_NAME,
) -> None:
    """Assert the device's nudge is filed at ``tier`` with a ``days`` placeholder."""
    translation_key, severity = tier
    issue = _issue(hass, issue_id)
    assert issue is not None, f"expected a {translation_key} issue at {days} days"
    assert issue.translation_key == translation_key
    assert issue.severity is severity
    assert issue.translation_placeholders == {"device": device, "days": days}
    assert issue.is_fixable is False
    assert issue.is_persistent is False


# ---------------------------------------------------------------------------
# The tier ladder: entry, escalation, hysteresis, de-escalation, release
# ---------------------------------------------------------------------------


async def test_healthy_countdown_raises_no_issue(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """The captured device's 167-day countdown is nowhere near a nudge."""
    _register_polls(mock_api, [_detail_with_countdown(167)])

    assert await setup_integration(hass, mock_config_entry)

    assert _issue(hass) is None


async def test_tier_ladder_escalates_de_escalates_and_clears(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Walk the binding day sequence and assert the tier at every step.

    Entry into a tier happens at its threshold (14 days warning, 7 days error);
    leaving one requires the countdown to recover past threshold +
    :data:`~custom_components.aquahome.const.SALT_DAYS_HYSTERESIS`, so 8 and 9
    days keep the error tier and 15 and 16 days keep the warning tier instead of
    flapping the issue as the estimate wobbles around a boundary.
    """
    _register_polls(
        mock_api,
        [
            _detail_with_countdown(167),  # setup: healthy
            _detail_with_countdown(14),  # warning threshold
            _detail_with_countdown(7),  # error threshold
            _detail_with_countdown(8),  # inside the error hysteresis band
            _detail_with_countdown(9),  # still inside it
            _detail_with_countdown(10),  # past 7 + 2: back to warning
            _detail_with_countdown(15),  # inside the warning hysteresis band
            _detail_with_countdown(16),  # still inside it
            _detail_with_countdown(17),  # past 14 + 2: released
        ],
    )

    assert await setup_integration(hass, mock_config_entry)
    assert _issue(hass) is None

    await _fire_next_poll(hass, freezer)  # 14 days -> warning raised
    _assert_issue(hass, WARNING_TIER, "14")

    await _fire_next_poll(hass, freezer)  # 7 days -> escalated to error
    _assert_issue(hass, CRITICAL_TIER, "7")

    await _fire_next_poll(hass, freezer)  # 8 days -> error held
    _assert_issue(hass, CRITICAL_TIER, "8")

    await _fire_next_poll(hass, freezer)  # 9 days -> error still held
    _assert_issue(hass, CRITICAL_TIER, "9")

    await _fire_next_poll(hass, freezer)  # 10 days -> de-escalated to warning
    _assert_issue(hass, WARNING_TIER, "10")

    await _fire_next_poll(hass, freezer)  # 15 days -> warning held
    _assert_issue(hass, WARNING_TIER, "15")

    await _fire_next_poll(hass, freezer)  # 16 days -> warning still held
    _assert_issue(hass, WARNING_TIER, "16")

    await _fire_next_poll(hass, freezer)  # 17 days -> issue deleted
    assert _issue(hass) is None


async def test_countdown_already_critical_at_setup_raises_the_issue(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """A device already below the error threshold nudges on the first refresh.

    The evaluation runs once immediately at setup, so a softener that was
    already nearly out of salt when Home Assistant started does not wait a full
    poll interval to say so.
    """
    _register_polls(mock_api, [_detail_with_countdown(3)])

    assert await setup_integration(hass, mock_config_entry)

    _assert_issue(hass, CRITICAL_TIER, "3")


async def test_recovery_straight_past_both_bands_clears_the_issue(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A refill seen as one jump from the error tier to healthy clears at once.

    The hysteresis only holds a tier while the countdown is inside its release
    band; a value beyond every band (a refilled brine tank) releases directly,
    without passing through the warning tier.
    """
    _register_polls(mock_api, [_detail_with_countdown(5), _detail_with_countdown(120)])

    assert await setup_integration(hass, mock_config_entry)
    _assert_issue(hass, CRITICAL_TIER, "5")

    await _fire_next_poll(hass, freezer)

    assert _issue(hass) is None


# ---------------------------------------------------------------------------
# Placeholder refresh while the tier is unchanged
# ---------------------------------------------------------------------------


async def test_day_count_change_within_a_tier_updates_the_placeholder(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A countdown ticking down inside one tier re-renders the ``days`` text.

    The tier is unchanged, but the number quoted to the user must follow the
    device. A fractional countdown renders as whole days (truncated), never as a
    decimal in the issue text.
    """
    _register_polls(
        mock_api,
        [
            _detail_with_countdown(14),
            _detail_with_countdown(12),
            _detail_with_countdown(11.6),
        ],
    )

    assert await setup_integration(hass, mock_config_entry)
    _assert_issue(hass, WARNING_TIER, "14")

    await _fire_next_poll(hass, freezer)
    _assert_issue(hass, WARNING_TIER, "12")

    await _fire_next_poll(hass, freezer)
    _assert_issue(hass, WARNING_TIER, "11")


async def test_registry_actions_track_the_tier_walk(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The registry sees create, re-file on escalation, then remove — nothing while steady.

    The lifecycle is asserted from the outside, on the Repairs registry's own
    event bus: raising moves the registry once; escalating RE-FILES the issue
    (remove + create — ``async_create_issue`` alone would preserve a user's
    dismissal and hide the critical nudge behind "show ignored issues"); a poll
    that repeats the same day count in the same tier must leave it completely
    untouched; and clearing removes it once.
    """
    actions: list[str] = []

    @callback
    def _record(event: Event[ir.EventIssueRegistryUpdatedData]) -> None:
        """Record registry actions concerning this device's nudge."""
        if event.data["domain"] == DOMAIN and event.data["issue_id"] == ISSUE_ID:
            actions.append(event.data["action"])

    hass.bus.async_listen(ir.EVENT_REPAIRS_ISSUE_REGISTRY_UPDATED, _record)
    _register_polls(
        mock_api,
        [
            _detail_with_countdown(167),
            _detail_with_countdown(14),
            _detail_with_countdown(7),
            _detail_with_countdown(7),  # unchanged: nothing to re-file
            _detail_with_countdown(17),
        ],
    )

    assert await setup_integration(hass, mock_config_entry)
    assert actions == []

    await _fire_next_poll(hass, freezer)
    assert actions == ["create"]

    await _fire_next_poll(hass, freezer)
    assert actions == ["create", "remove", "create"]

    await _fire_next_poll(hass, freezer)
    assert actions == ["create", "remove", "create"]

    await _fire_next_poll(hass, freezer)
    assert actions == ["create", "remove", "create", "remove"]


# ---------------------------------------------------------------------------
# Missing / unreadable countdown
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "unreadable",
    [
        pytest.param(None, id="property-absent"),
        pytest.param("unavailable", id="non-numeric-value"),
    ],
)
async def test_unreadable_countdown_never_raises_an_issue(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    unreadable: str | None,
) -> None:
    """A device that reports no usable countdown is never nudged."""
    _register_polls(mock_api, [_detail_with_countdown(unreadable)])

    assert await setup_integration(hass, mock_config_entry)

    assert _issue(hass) is None


@pytest.mark.parametrize(
    "unreadable",
    [
        pytest.param(None, id="property-absent"),
        pytest.param("unavailable", id="non-numeric-value"),
    ],
)
async def test_unreadable_countdown_clears_an_existing_issue(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    unreadable: str | None,
) -> None:
    """Losing the countdown deletes a raised issue instead of freezing it.

    A stale "3 days of salt left" banner left standing after the device stopped
    reporting would be a lie, so the nudge is withdrawn until a usable countdown
    comes back.
    """
    _register_polls(
        mock_api, [_detail_with_countdown(3), _detail_with_countdown(unreadable)]
    )

    assert await setup_integration(hass, mock_config_entry)
    _assert_issue(hass, CRITICAL_TIER, "3")

    await _fire_next_poll(hass, freezer)

    assert _issue(hass) is None


async def test_countdown_returning_after_a_gap_raises_a_fresh_issue(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A gap resets the remembered tier: 8 days after it is a warning, not an error.

    The hysteresis is a property of an *observed* tier. Once the countdown has
    dropped out, there is no tier to hold, so a returning 8-day estimate enters
    at the tier its own value dictates.
    """
    _register_polls(
        mock_api,
        [
            _detail_with_countdown(3),
            _detail_with_countdown(None),
            _detail_with_countdown(8),
        ],
    )

    assert await setup_integration(hass, mock_config_entry)
    _assert_issue(hass, CRITICAL_TIER, "3")

    await _fire_next_poll(hass, freezer)
    assert _issue(hass) is None

    await _fire_next_poll(hass, freezer)
    _assert_issue(hass, WARNING_TIER, "8")


# ---------------------------------------------------------------------------
# Per-device scoping and unload
# ---------------------------------------------------------------------------


def _two_device_list() -> dict[str, Any]:
    """Return the device list fixture with a synthetic second softener."""
    listing = load_fixture("devices-list.json")
    second = copy.deepcopy(listing["data"][0])
    second["id"] = SECOND_DEVICE_ID
    second["serial_number"] = SECOND_SERIAL
    second["nickname"] = SECOND_DEVICE_NAME
    listing["data"].append(second)
    listing["total"] = 2
    return listing


def _second_device_detail(value: float | None) -> dict[str, Any]:
    """Return a detail payload for the synthetic second device."""
    detail = _detail_with_countdown(value)
    detail["id"] = SECOND_DEVICE_ID
    detail["serial_number"] = SECOND_SERIAL
    detail["nickname"] = SECOND_DEVICE_NAME
    return detail


def _register_second_device_routes(mock: aioresponses, detail: dict[str, Any]) -> None:
    """Register every read route the second device's coordinators hit."""
    mock.get(device_url(SECOND_DEVICE_ID), payload=detail, repeat=True)
    mock.get(
        alerts_url(SECOND_DEVICE_ID), payload=load_fixture("alerts.json"), repeat=True
    )
    mock.get(
        regen_events_url(SECOND_DEVICE_ID),
        payload=load_fixture("regeneration-events.json"),
        repeat=True,
    )
    mock.get(
        settings_url(SECOND_DEVICE_ID),
        payload=load_fixture("settings.json"),
        repeat=True,
    )


async def test_each_device_owns_its_own_issue_id(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """Two softeners on one account nudge independently, keyed by device slug.

    The healthy device raises nothing while the empty one raises its own
    ``low_salt_<device_slug>`` issue, naming itself in the placeholder — a
    single shared issue id would let one softener's refill silence the other's.
    """
    mock_api.get(devices_url(), payload=_two_device_list(), repeat=True)
    mock_api.get(device_url(), payload=_detail_with_countdown(167), repeat=True)
    add_activity_routes(mock_api)
    add_settings_routes(mock_api)
    _register_second_device_routes(mock_api, _second_device_detail(5))

    assert await setup_integration(hass, mock_config_entry)

    assert _issue(hass, ISSUE_ID) is None
    _assert_issue(
        hass,
        CRITICAL_TIER,
        "5",
        issue_id=SECOND_ISSUE_ID,
        device=SECOND_DEVICE_NAME,
    )


async def test_unloading_the_entry_stops_the_updates(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """The coordinator listener is dropped on unload, so no nudge follows it.

    The watcher is registered through ``entry.async_on_unload``; an unloaded
    entry that somehow still saw data must not file Repairs issues for a device
    Home Assistant no longer manages.
    """
    _register_polls(mock_api, [_detail_with_countdown(167)])

    assert await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data.coordinators[TEST_DEVICE_ID]

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator.async_set_updated_data(Device.from_dict(_detail_with_countdown(2)))
    await hass.async_block_till_done()

    assert _issue(hass) is None


async def test_removing_the_entry_deletes_a_standing_issue(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """Uninstalling the integration deletes the issue instead of orphaning it.

    The Repairs registry outlives config entries, so without the
    ``async_remove_entry`` cleanup an uninstalled integration's "salt is
    almost empty" card would keep nagging until the next restart. The issue
    ids are rebuilt from the device registry, so this works even though the
    entry is already unloaded when the removal hook runs. The autouse
    ``no_platforms`` fixture means no entity ever registers the device here,
    so the test files the device-registry entry itself — exactly what the
    entity platforms do in production (and what the Phase-5 statistics
    cleanup, which shares the enumeration, relies on live).
    """
    _register_polls(mock_api, [_detail_with_countdown(7)])

    assert await setup_integration(hass, mock_config_entry)
    _assert_issue(hass, CRITICAL_TIER, "7")
    dr.async_get(hass).async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, SLUG)},
    )

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert _issue(hass) is None


async def test_reload_preserves_a_dismissal(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """A plain reload keeps the user's "Ignore" instead of resurfacing the nudge.

    ``dismissed_version`` lives on the registry entry, which survives an
    unload; the fresh setup's evaluation must take the update path (same
    issue id, same tier) so the dismissal carries over — deleting on unload
    or re-filing on the same tier would wipe it on every reload.
    """
    _register_polls(mock_api, [_detail_with_countdown(10)])

    assert await setup_integration(hass, mock_config_entry)
    _assert_issue(hass, WARNING_TIER, "10")
    ir.async_get(hass).async_ignore(DOMAIN, ISSUE_ID, ignore=True)

    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_key == "salt_level_low"
    assert issue.dismissed_version is not None


async def test_escalation_resurfaces_a_dismissed_warning(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Ignoring the warning must not silence the critical escalation.

    ``async_create_issue`` updates an existing id in place and deliberately
    preserves ``dismissed_version``, so the escalation deletes the warning
    first — otherwise a user who pressed "Ignore" at 14 days would never see
    the error-severity nudge and the softener would run dry unannounced.
    """
    _register_polls(
        mock_api,
        [_detail_with_countdown(14), _detail_with_countdown(7)],
    )

    assert await setup_integration(hass, mock_config_entry)
    _assert_issue(hass, WARNING_TIER, "14")
    ir.async_get(hass).async_ignore(DOMAIN, ISSUE_ID, ignore=True)

    await _fire_next_poll(hass, freezer)

    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_key == "salt_level_critical"
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.dismissed_version is None
