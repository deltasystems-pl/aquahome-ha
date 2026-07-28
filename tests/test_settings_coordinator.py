"""Tests for the AquaHome settings coordinator and its wiring.

Two layers are exercised, mirroring ``tests/test_activity_coordinator.py``. The
poll cadence, serve-stale taxonomy, and the write-heals-staleness reconcile are
driven against a directly-constructed
:class:`~custom_components.aquahome.coordinator.AquaHomeSettingsCoordinator` (a
standalone client over ``aioresponses`` plus an injected monotonic clock for the
exact ``SETTINGS_MAX_STALE_SECONDS`` boundary) — the sanctioned exception to the
end-to-end rule. The setup wiring (tolerant first refresh, entity availability
across a serve-stale window, dynamic select creation on a later refresh) is
exercised end-to-end through ``setup_integration``.

The ``freezer`` fixture patches ``time`` module lookups but NOT the
``time.monotonic`` reference captured as the coordinator's default argument at
import time, so the integration-created coordinator reads the real monotonic
clock; the serve-stale TTL therefore cannot be driven by the freezer and the
end-to-end tests back-date ``_last_good`` (like ``tests/test_coordinator.py``)
while the exact TTL boundary uses the direct-construction path with an injected
clock.

Every end-to-end test narrows the forwarded platforms to those under test by
patching ``PLATFORMS`` at both the ``const`` definition and the ``__init__``
import site; production code keeps the full contract-exact platform list.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import aiohttp
import pytest
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, Platform
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.aquahome.api import AquaHomeClient, AuthManager
from custom_components.aquahome.api.const import API_BASE_URL
from custom_components.aquahome.api.models import DeviceSettingsDocument
from custom_components.aquahome.const import (
    DOMAIN,
    SETTINGS_MAX_STALE_SECONDS,
    SETTINGS_UPDATE_INTERVAL,
    UPDATE_INTERVAL,
)
from custom_components.aquahome.coordinator import AquaHomeSettingsCoordinator
from tests.conftest import (
    TEST_DEVICE_ID,
    add_activity_routes,
    add_device_routes,
    add_settings_routes,
    device_url,
    devices_url,
    load_fixture,
    make_access_token,
    patch_settings_route,
    settings_url,
    setup_integration,
    with_setting_value,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aioresponses import aioresponses
    from aioresponses.core import RequestCall
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

#: Slug derived from the fixture serial ``4213377-30105-2242``.
SLUG = "4213377_30105_2242"
#: Fixed instant the end-to-end setup tests freeze to (2026-07-21T12:00:00Z).
FROZEN_INSTANT = "2026-07-21T12:00:00+00:00"
#: Settings count in the real ``settings.json`` fixture.
SETTINGS_COUNT = 18
#: A visible, registry-enabled select setting in the fixture (no conditional).
SALT_TYPE_SELECT = "select.demo_salt_type"
#: A core telemetry sensor that must stay live even when settings fail.
SALT_LEVEL_SENSOR = "sensor.demo_salt_level"

#: Coordinator logger scoped by the serve-stale log assertions.
COORDINATOR_LOGGER = "custom_components.aquahome.coordinator"
#: Substring of the settings serve-stale WARNING line.
STALE_WARNING = "settings poll failed"
#: Substring of the settings recovery INFO line.
RECOVERY_INFO = "settings recovered"

#: Transient failures that must take the serve-stale path (route kwargs).
_TRANSIENT_FAILURES: list[tuple[str, dict[str, Any]]] = [
    (
        "rate-limit",
        {
            "status": 429,
            "payload": {"code": "ThrottleLimitExceeded", "detail": "slow down"},
            "repeat": True,
        },
    ),
    (
        "server-error",
        {
            "status": 503,
            "payload": {"code": "ServiceUnavailable", "detail": "try later"},
            "repeat": True,
        },
    ),
    (
        "connection-error",
        {"exception": aiohttp.ClientError("network down"), "repeat": True},
    ),
]


@contextlib.contextmanager
def _limit_platforms(platforms: list[Platform]) -> Iterator[None]:
    """Narrow the forwarded platforms to those under test for one setup."""
    with (
        patch("custom_components.aquahome.PLATFORMS", platforms),
        patch("custom_components.aquahome.const.PLATFORMS", platforms),
    ):
        yield


class _FakeMonotonic:
    """Advanceable monotonic clock for driving the serve-stale TTL directly."""

    def __init__(self, now: float = 1_000.0) -> None:
        """Start the clock at a fixed value."""
        self._now = now

    def __call__(self) -> float:
        """Return the current monotonic reading."""
        return self._now

    def advance(self, seconds: float) -> None:
        """Move the clock forward."""
        self._now += seconds


def _standalone_client(hass: HomeAssistant) -> AquaHomeClient:
    """Build a client whose auth already holds a fresh (non-refreshing) token."""
    session = async_get_clientsession(hass)
    auth = AuthManager(session, base_url=API_BASE_URL)
    auth.set_tokens(make_access_token(), "refresh-token-1")
    return AquaHomeClient(session, auth, base_url=API_BASE_URL, language="en")


def _settings_coordinator(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    client: AquaHomeClient | None = None,
    monotonic: _FakeMonotonic | None = None,
) -> AquaHomeSettingsCoordinator:
    """Construct a settings coordinator bound to ``entry`` for the dev device."""
    kwargs: dict[str, Any] = {}
    if monotonic is not None:
        kwargs["monotonic"] = monotonic
    return AquaHomeSettingsCoordinator(
        hass,
        entry,
        client or _standalone_client(hass),
        device_id=TEST_DEVICE_ID,
        device_slug=SLUG,
        **kwargs,
    )


def _settings_get_count(mock: aioresponses) -> int:
    """Return how many ``GET /devices/{id}/settings`` requests were recorded."""
    return sum(
        len(calls)
        for (method, url), calls in mock.requests.items()
        if method == "GET" and url.path.endswith("/settings")
    )


def _settings_patch_calls(mock: aioresponses) -> list[RequestCall]:
    """Return every recorded ``PATCH /devices/{id}/settings`` request."""
    return [
        call
        for (method, url), calls in mock.requests.items()
        if method == "PATCH" and url.path.endswith("/settings")
        for call in calls
    ]


def _stale(coordinator: AquaHomeSettingsCoordinator) -> bool:
    """Return the serve-stale flag as a fresh ``bool``.

    Reading the attribute through a function boundary keeps mypy's
    ``warn_unreachable`` from persisting a ``Literal`` narrowing across the
    opaque ``_async_update_data`` / write calls that mutate it, so a test can
    assert both polarities of the flag in sequence.
    """
    return coordinator._serving_stale


def _succeeded(coordinator: AquaHomeSettingsCoordinator) -> bool:
    """Return ``last_update_success`` as a fresh ``bool`` (see :func:`_stale`)."""
    return coordinator.last_update_success


def _document(
    coordinator: AquaHomeSettingsCoordinator,
) -> DeviceSettingsDocument | None:
    """Return the coordinator's data typed as optional.

    ``DataUpdateCoordinator.data`` is typed non-optional but is ``None`` until the
    first refresh succeeds; reading it through this accessor keeps ``mypy`` from
    narrowing to ``Never`` after a ``is None`` assertion (see :func:`_stale`).
    """
    return coordinator.data


def _stale_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return the settings serve-stale WARNING records captured so far."""
    return [
        record
        for record in caplog.records
        if record.name == COORDINATOR_LOGGER
        and record.levelno == logging.WARNING
        and STALE_WARNING in record.getMessage()
    ]


def _recovery_infos(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return the settings recovery INFO records captured so far."""
    return [
        record
        for record in caplog.records
        if record.name == COORDINATOR_LOGGER
        and record.levelno == logging.INFO
        and RECOVERY_INFO in record.getMessage()
    ]


async def _fire(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, interval: Any
) -> None:
    """Advance ``interval`` and let every scheduled poll run to completion."""
    freezer.tick(interval)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def _state(hass: HomeAssistant, entity_id: str) -> str:
    """Return the state string of ``entity_id``, asserting the entity exists."""
    state = hass.states.get(entity_id)
    assert state is not None
    return state.state


# ---------------------------------------------------------------------------
# Happy path parsing
# ---------------------------------------------------------------------------


async def test_first_refresh_parses_settings_document(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """A first refresh parses the real fixture into a DeviceSettingsDocument."""
    mock_config_entry.add_to_hass(hass)
    add_settings_routes(mock_api)
    coordinator = _settings_coordinator(hass, mock_config_entry)

    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    document = coordinator.data
    assert isinstance(document, DeviceSettingsDocument)
    assert len(document.settings) == SETTINGS_COUNT
    inlet = document.get("inlet_hardness")
    assert inlet is not None
    assert inlet.current_value == "25.7"
    assert _stale(coordinator) is False


# ---------------------------------------------------------------------------
# Poll cadence
# ---------------------------------------------------------------------------


async def test_polls_on_settings_interval(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The document is fetched once at setup, then once per 6-hour interval."""
    freezer.move_to(FROZEN_INSTANT)
    add_device_routes(mock_api)

    with _limit_platforms([Platform.SELECT]):
        assert await setup_integration(hass, mock_config_entry)

    # Exactly one fetch at setup.
    assert _settings_get_count(mock_api) == 1

    # A sub-interval fast poll (10 min) must not touch the settings feed.
    await _fire(hass, freezer, UPDATE_INTERVAL)
    assert _settings_get_count(mock_api) == 1

    # Each 6-hour cadence tick polls the settings document exactly once more.
    await _fire(hass, freezer, SETTINGS_UPDATE_INTERVAL)
    assert _settings_get_count(mock_api) == 2

    await _fire(hass, freezer, SETTINGS_UPDATE_INTERVAL)
    assert _settings_get_count(mock_api) == 3


# ---------------------------------------------------------------------------
# Serve-stale taxonomy (direct construction with injected monotonic)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [pytest.param(kwargs, id=name) for name, kwargs in _TRANSIENT_FAILURES],
)
async def test_transient_failure_within_window_serves_stale(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    failure: dict[str, Any],
) -> None:
    """A 429, transient 5xx, or connection error keeps the cached document."""
    mock_config_entry.add_to_hass(hass)
    clock = _FakeMonotonic()
    coordinator = _settings_coordinator(hass, mock_config_entry, monotonic=clock)
    mock_api.get(settings_url(), payload=load_fixture("settings.json"))

    await coordinator.async_refresh()
    assert coordinator.last_update_success is True
    good = coordinator.data
    assert good is not None

    clock.advance(SETTINGS_MAX_STALE_SECONDS - 1.0)
    mock_api.get(settings_url(), **failure)

    result = await coordinator._async_update_data()

    assert result is coordinator.data
    assert result.settings == good.settings
    assert _stale(coordinator) is True


async def test_serve_stale_warns_once_then_recovers(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transient failure warns exactly once; the recovery logs INFO once."""
    caplog.set_level(logging.INFO, logger=COORDINATOR_LOGGER)
    mock_config_entry.add_to_hass(hass)
    clock = _FakeMonotonic()
    coordinator = _settings_coordinator(hass, mock_config_entry, monotonic=clock)
    document = load_fixture("settings.json")
    mock_api.get(settings_url(), payload=document)

    await coordinator.async_refresh()

    # Transient failure: cached document retained, one warning, no recovery yet.
    mock_api.get(settings_url(), exception=aiohttp.ClientError("network down"))
    stale = await coordinator._async_update_data()
    assert stale is coordinator.data
    assert _stale(coordinator) is True
    assert len(_stale_warnings(caplog)) == 1
    assert _recovery_infos(caplog) == []

    # Recovery poll: fresh document flows through and the recovery is logged once.
    mock_api.get(
        settings_url(),
        payload=with_setting_value(document, "salt_type", "1"),
        repeat=True,
    )
    fresh = await coordinator._async_update_data()
    salt = fresh.get("salt_type")
    assert salt is not None
    assert salt.current_value == "1"
    assert _stale(coordinator) is False
    assert len(_recovery_infos(caplog)) == 1
    # Still exactly one warning: the recovery must not re-warn.
    assert len(_stale_warnings(caplog)) == 1


async def test_past_window_raises_update_failed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """Past SETTINGS_MAX_STALE_SECONDS a failing poll drops stale and fails."""
    mock_config_entry.add_to_hass(hass)
    clock = _FakeMonotonic()
    coordinator = _settings_coordinator(hass, mock_config_entry, monotonic=clock)
    mock_api.get(settings_url(), payload=load_fixture("settings.json"))

    await coordinator.async_refresh()
    assert coordinator.data is not None

    clock.advance(SETTINGS_MAX_STALE_SECONDS)
    mock_api.get(
        settings_url(),
        status=503,
        payload={"code": "ServiceUnavailable", "detail": "try later"},
        repeat=True,
    )

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_plain_404_fails_immediately_even_with_fresh_data(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """A 404 never serves stale, even with fresh last-good data in hand."""
    mock_config_entry.add_to_hass(hass)
    clock = _FakeMonotonic()
    coordinator = _settings_coordinator(hass, mock_config_entry, monotonic=clock)
    mock_api.get(settings_url(), payload=load_fixture("settings.json"))

    await coordinator.async_refresh()
    assert coordinator.data is not None

    # Fresh last-good (clock barely moved), yet a 404 must fail immediately.
    mock_api.get(
        settings_url(),
        status=404,
        payload={"code": "NotFound", "detail": "no settings"},
        repeat=True,
    )

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert _stale(coordinator) is False


# ---------------------------------------------------------------------------
# Authentication failure mid-run
# ---------------------------------------------------------------------------


async def test_auth_error_starts_reauth(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """A rejected token whose refresh also 401s starts Home Assistant reauth."""
    mock_config_entry.add_to_hass(hass)
    coordinator = _settings_coordinator(hass, mock_config_entry)
    mock_api.get(
        settings_url(),
        status=401,
        payload={"code": "Unauthorized", "detail": "token expired"},
        repeat=True,
    )
    mock_api.post(
        f"{API_BASE_URL}/auth/refresh",
        status=401,
        payload={"code": "AuthCannotRefreshToken", "detail": "refresh rejected"},
        repeat=True,
    )

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    reauth_flows = [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["handler"] == DOMAIN and flow["context"].get("source") == SOURCE_REAUTH
    ]
    assert len(reauth_flows) == 1


# ---------------------------------------------------------------------------
# async_write_setting: reconcile + staleness heal (direct construction)
# ---------------------------------------------------------------------------


async def test_write_heals_staleness_and_reconciles_without_get(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """A successful write reconciles from the PATCH document and clears staleness.

    The coordinator is first driven into a serve-stale state; a write then pushes
    the server-echoed document into ``data`` with no extra settings GET, clears
    the stale flag, and re-anchors the stale window at the write instant.
    """
    mock_config_entry.add_to_hass(hass)
    clock = _FakeMonotonic()
    coordinator = _settings_coordinator(hass, mock_config_entry, monotonic=clock)
    document = load_fixture("settings.json")
    mock_api.get(settings_url(), payload=document)

    await coordinator.async_refresh()
    assert coordinator.last_update_success is True

    # Drive the coordinator stale, still inside the window.
    clock.advance(SETTINGS_MAX_STALE_SECONDS - 100.0)
    mock_api.get(
        settings_url(),
        status=503,
        payload={"code": "ServiceUnavailable", "detail": "try later"},
        repeat=True,
    )
    stale = await coordinator._async_update_data()
    assert stale is coordinator.data
    assert _stale(coordinator) is True

    gets_before = _settings_get_count(mock_api)

    # A write returns the refreshed document; reconcile happens from it alone.
    patched = with_setting_value(document, "salt_type", "1")
    patch_settings_route(mock_api, payload=patched)
    await coordinator.async_write_setting("salt_type", "1")

    # No extra settings GET was issued to reconcile.
    assert _settings_get_count(mock_api) == gets_before
    # The PATCH carried the single wrapped setting.
    (call,) = _settings_patch_calls(mock_api)
    assert call.kwargs["json"] == {"settings": {"salt_type": "1"}}
    # Data reconciled from the PATCH body and staleness healed.
    assert _stale(coordinator) is False
    assert coordinator.last_update_success is True
    salt = coordinator.data.get("salt_type")
    assert salt is not None
    assert salt.current_value == "1"

    # The stale window is now measured from the write, not the first refresh.
    clock.advance(SETTINGS_MAX_STALE_SECONDS - 1.0)
    served = await coordinator._async_update_data()
    assert served is coordinator.data
    assert _stale(coordinator) is True

    clock.advance(1.0)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


# ---------------------------------------------------------------------------
# Setup wiring: tolerant first refresh + serve-stale availability (end-to-end)
# ---------------------------------------------------------------------------


async def test_tolerant_setup_then_later_refresh_adds_selects(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A settings fetch failing at setup keeps core sensors live; a later poll heals.

    The failed first fetch leaves the settings coordinator unsuccessful with no
    document, so no select entity exists yet, while the telemetry sensors load
    normally. The next successful 6-hour refresh materialises the select entities.
    """
    freezer.move_to(FROZEN_INSTANT)
    # Settings fails once at setup, then succeeds on every later poll.
    mock_api.get(
        settings_url(),
        status=500,
        payload={"code": "ServerError", "detail": "boom"},
    )
    add_device_routes(mock_api)

    with _limit_platforms([Platform.SENSOR, Platform.SELECT]):
        assert await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    settings = mock_config_entry.runtime_data.settings_coordinators[TEST_DEVICE_ID]
    assert _succeeded(settings) is False
    assert _document(settings) is None

    # Core telemetry is live; no settings entity has been created yet.
    assert _state(hass, SALT_LEVEL_SENSOR) == "37.5"
    assert hass.states.get(SALT_TYPE_SELECT) is None

    # A later successful 6-hour refresh brings the select entities up.
    await _fire(hass, freezer, SETTINGS_UPDATE_INTERVAL)

    assert _succeeded(settings) is True
    assert _document(settings) is not None
    assert _state(hass, SALT_TYPE_SELECT) != STATE_UNAVAILABLE


async def test_serve_stale_end_to_end_keeps_then_drops_select(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Within the window a select stays available; past it, it goes unavailable.

    The settings coordinator reads the real monotonic clock end-to-end, so the
    last-good marker is back-dated past the window to place a poll deterministically
    beyond the grace period without a real 24-hour wait.
    """
    caplog.set_level(logging.INFO, logger=COORDINATOR_LOGGER)
    freezer.move_to(FROZEN_INSTANT)
    mock_api.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock_api.get(device_url(), payload=load_fixture("device-detail.json"), repeat=True)
    add_activity_routes(mock_api)
    # Settings succeeds once at setup, then fails on every later poll.
    mock_api.get(settings_url(), payload=load_fixture("settings.json"))
    mock_api.get(
        settings_url(),
        status=503,
        payload={"code": "ServiceUnavailable", "detail": "try later"},
        repeat=True,
    )

    with _limit_platforms([Platform.SENSOR, Platform.SELECT]):
        assert await setup_integration(hass, mock_config_entry)

    assert _state(hass, SALT_TYPE_SELECT) != STATE_UNAVAILABLE

    # A failing poll within the window keeps the last-good selection available.
    await _fire(hass, freezer, SETTINGS_UPDATE_INTERVAL)
    settings = mock_config_entry.runtime_data.settings_coordinators[TEST_DEVICE_ID]
    assert _succeeded(settings) is True
    assert _state(hass, SALT_TYPE_SELECT) != STATE_UNAVAILABLE
    assert len(_stale_warnings(caplog)) == 1

    # Back-date the last-good marker past the window: the next failing poll drops it.
    settings._last_good = settings._monotonic() - (SETTINGS_MAX_STALE_SECONDS + 60.0)
    await _fire(hass, freezer, SETTINGS_UPDATE_INTERVAL)

    assert _succeeded(settings) is False
    assert _state(hass, SALT_TYPE_SELECT) == STATE_UNAVAILABLE
    # The past-window poll raised rather than serving stale, so no second warning.
    assert len(_stale_warnings(caplog)) == 1
