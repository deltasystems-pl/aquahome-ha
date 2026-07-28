"""Tests for the shared entity bases in :mod:`custom_components.aquahome.entity`.

Every platform inherits one of the four bases in that module, so the bases are
exercised here through the concrete entities that carry them rather than in
isolation: :class:`~custom_components.aquahome.entity.AquaHomeSettingsEntity`
through a boolean setting switch, and
:class:`~custom_components.aquahome.entity.AquaHomeLeakDetectorEntity` through a
leak-detector binary sensor. The happy paths (device info, unique ids, the
online gate, name localization) belong to the platform suites; what this file
pins are the edges those suites never reach:

* the pre-first-document guards on ``AquaHomeSettingsEntity`` — a settings
  coordinator that has not yet produced a document must make its entities report
  no setting and go unavailable, never raise;
* ``AquaHomeSettingsEntity._async_write`` — a setting that vanished from the
  document refuses the write locally (no HTTP at all), and each API failure of
  the shared taxonomy becomes its own translated ``HomeAssistantError``;
* ``AquaHomeLeakDetectorEntity``'s detector lookup — ``None``-safe before the
  first poll (falling back to the generic sub-device name) and when a later poll
  drops the enriched leak-detector block entirely.

Fixture payloads are always deep-copied before mutation; the JSON files are
never edited in place.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, cast

import aiohttp
import pytest
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_ON,
    STATE_UNAVAILABLE,
    Platform,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_component import DATA_INSTANCES
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aquahome.api import AquaHomeClient, AuthManager
from custom_components.aquahome.api.const import API_BASE_URL
from custom_components.aquahome.api.models import Device, DeviceSettingsDocument
from custom_components.aquahome.const import DOMAIN
from custom_components.aquahome.coordinator import (
    AquaHomeCoordinator,
    AquaHomeSettingsCoordinator,
)
from custom_components.aquahome.entity import AquaHomeLeakDetectorEntity
from custom_components.aquahome.switch import AquaHomeSettingSwitch
from tests.conftest import (
    TEST_DEVICE_ID,
    add_activity_routes,
    add_device_routes,
    add_settings_routes,
    device_url,
    devices_url,
    load_fixture,
    make_access_token,
    make_leak_detector,
    make_switch_setting,
    settings_url,
    setup_integration,
    with_extra_setting,
    with_leak_detectors,
    without_setting,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aioresponses import aioresponses
    from homeassistant.core import HomeAssistant

#: The switch platform domain (``homeassistant.components.switch`` does not
#: re-export ``DOMAIN`` for typing, so derive it from the platform enum).
SWITCH_DOMAIN = Platform.SWITCH
#: Slug derived from the fixture serial ``4213377-30105-2242``.
SLUG = "4213377_30105_2242"
#: The synthetic boolean setting that carries the settings-entity base here.
SETTING_NAME = "night_mode"
#: Unique id of the switch built from that setting.
SETTING_UNIQUE_ID = f"{SLUG}_setting_{SETTING_NAME}"
#: The refresh endpoint the client hits after a 401 before retrying a request.
REFRESH_URL = f"{API_BASE_URL}/auth/refresh"


@pytest.fixture
def _only_switch_platform() -> Iterator[None]:
    """Forward only the switch platform for the duration of a test."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("custom_components.aquahome.PLATFORMS", [Platform.SWITCH])
        yield


@pytest.fixture
def _only_binary_sensor_platform() -> Iterator[None]:
    """Forward only the binary-sensor platform for the duration of a test."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "custom_components.aquahome.PLATFORMS", [Platform.BINARY_SENSOR]
        )
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _standalone_client(hass: HomeAssistant) -> AquaHomeClient:
    """Build a client whose auth already holds a fresh (non-refreshing) token."""
    session = async_get_clientsession(hass)
    auth = AuthManager(session, base_url=API_BASE_URL)
    auth.set_tokens(make_access_token(), "refresh-token-1")
    return AquaHomeClient(session, auth, base_url=API_BASE_URL, language="en")


def _settings_doc(*, value: bool = True) -> dict[str, Any]:
    """Return the real settings document with one boolean setting appended."""
    return with_extra_setting(
        load_fixture("settings.json"),
        make_switch_setting(name=SETTING_NAME, current_value=value),
    )


def _register_switch_routes(
    mock: aioresponses, *, settings: dict[str, Any] | None = None
) -> None:
    """Register every read route a switch-only setup hits."""
    add_device_routes(mock, settings=settings or _settings_doc())


def _setting_switch_id(entity_registry: er.EntityRegistry) -> str:
    """Resolve the entity id of the boolean setting switch."""
    entity_id = entity_registry.async_get_entity_id(
        SWITCH_DOMAIN, DOMAIN, SETTING_UNIQUE_ID
    )
    assert entity_id is not None, "the boolean setting switch was not registered"
    return entity_id


async def _turn_on(hass: HomeAssistant, entity_id: str) -> None:
    """Call ``switch.turn_on`` on an entity, letting errors propagate."""
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )


def _live_switch(hass: HomeAssistant, entity_id: str) -> SwitchEntity:
    """Return the live switch entity object behind ``entity_id``.

    The service layer skips entities that report themselves unavailable, so a
    test about *what an unavailable entity does when written to* has to hold the
    entity itself.
    """
    component = hass.data[DATA_INSTANCES][SWITCH_DOMAIN]
    entity = component.get_entity(entity_id)
    assert entity is not None, f"{entity_id} has no live entity"
    return cast(SwitchEntity, entity)


def _patch_calls(mock: aioresponses) -> int:
    """Return how many ``PATCH /devices/{id}/settings`` writes were recorded."""
    return sum(
        len(calls)
        for (method, url), calls in mock.requests.items()
        if method == "PATCH" and url.path.endswith("/settings")
    )


# ---------------------------------------------------------------------------
# AquaHomeSettingsEntity — before the first settings document
# ---------------------------------------------------------------------------


async def test_settings_entity_without_document_has_no_setting_and_is_unavailable(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A settings entity built before the first document reads ``None``, unavailable.

    A settings coordinator carries ``data is None`` until its first fetch
    succeeds while ``last_update_success`` is still ``True``, so both guards are
    load-bearing: without the one in ``setting`` the property would raise
    ``AttributeError`` on ``None.get``, and without the one in ``available`` the
    entity would advertise itself as available with nothing behind it (the
    settings fetch is deliberately tolerant, so a failed first fetch does not
    abort setup).
    """
    mock_config_entry.add_to_hass(hass)
    device = Device.from_dict(load_fixture("device-detail.json"))
    coordinator = AquaHomeSettingsCoordinator(
        hass,
        mock_config_entry,
        _standalone_client(hass),
        device_id=TEST_DEVICE_ID,
        device_slug=SLUG,
    )
    entity = AquaHomeSettingSwitch(coordinator, device, SETTING_NAME)

    # ``data`` is typed as the document but is ``None`` until a fetch succeeds;
    # the annotation keeps mypy's narrowing off the rest of the test.
    document: DeviceSettingsDocument | None = coordinator.data
    assert document is None
    assert coordinator.last_update_success is True
    assert entity.setting is None
    assert entity.available is False


# ---------------------------------------------------------------------------
# AquaHomeSettingsEntity._async_write — the local guard
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_only_switch_platform")
async def test_write_to_vanished_setting_is_refused_without_any_request(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """A setting dropped by a later document refuses the write with no HTTP call.

    Dynamic entities are never removed, so a setting the cloud stops publishing
    leaves its switch behind. Without the ``setting is None`` guard the entity
    would PATCH a name the device no longer knows and surface whatever the
    server answered; the guard turns it into the local ``setting_unavailable``
    error, which this test pins by proving no PATCH was ever attempted.
    """
    mock_api.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock_api.get(device_url(), payload=load_fixture("device-detail.json"), repeat=True)
    add_activity_routes(mock_api)
    # First fetch carries the setting; every later fetch has dropped it.
    add_settings_routes(mock_api, settings=_settings_doc(), repeat=False)
    add_settings_routes(
        mock_api, settings=without_setting(_settings_doc(), SETTING_NAME)
    )

    assert await setup_integration(hass, mock_config_entry)
    entity_id = _setting_switch_id(entity_registry)

    settings_coordinator = mock_config_entry.runtime_data.settings_coordinators[
        TEST_DEVICE_ID
    ]
    await settings_coordinator.async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE

    with pytest.raises(HomeAssistantError) as caught:
        await _live_switch(hass, entity_id).async_turn_on()

    assert caught.value.translation_domain == DOMAIN
    assert caught.value.translation_key == "setting_unavailable"
    assert _patch_calls(mock_api) == 0


# ---------------------------------------------------------------------------
# AquaHomeSettingsEntity._async_write — the API error taxonomy
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_only_switch_platform")
async def test_write_throttled_maps_to_rate_limited(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """A throttled setting write surfaces as the ``rate_limited`` translation.

    ``RateLimitError`` must be caught before the generic ``ApiError`` arm; if the
    ordering were lost the user would see the raw server text under
    ``setting_rejected`` instead of the "try again shortly" wording.
    """
    _register_switch_routes(mock_api)
    mock_api.patch(
        settings_url(),
        status=429,
        payload={"code": "ThrottleLimitExceeded", "detail": "Slow down"},
    )

    assert await setup_integration(hass, mock_config_entry)

    with pytest.raises(HomeAssistantError) as caught:
        await _turn_on(hass, _setting_switch_id(entity_registry))

    assert caught.value.translation_domain == DOMAIN
    assert caught.value.translation_key == "rate_limited"


@pytest.mark.usefixtures("_only_switch_platform")
async def test_write_unauthorized_maps_to_auth_failed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """A write whose 401 survives a token refresh surfaces as ``auth_failed``.

    The client retries a 401 once behind a token refresh, so reaching the
    entity's ``AuthError`` arm needs the refresh itself to be rejected — the real
    "the account's session is gone" case. Without that arm the failure would fall
    through to ``setting_rejected`` and read as a device problem rather than a
    sign-in one.
    """
    _register_switch_routes(mock_api)
    mock_api.patch(
        settings_url(),
        status=401,
        payload={"code": "AuthTokenExpired", "detail": "token expired"},
    )
    mock_api.post(
        REFRESH_URL,
        status=401,
        payload={"code": "AuthCannotRefreshToken", "detail": "refresh rejected"},
    )

    assert await setup_integration(hass, mock_config_entry)

    with pytest.raises(HomeAssistantError) as caught:
        await _turn_on(hass, _setting_switch_id(entity_registry))

    assert caught.value.translation_domain == DOMAIN
    assert caught.value.translation_key == "auth_failed"


@pytest.mark.usefixtures("_only_switch_platform")
async def test_write_transport_failure_maps_to_cannot_connect(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """An unreachable cloud during a write surfaces as ``cannot_connect``.

    Without the ``AquaHomeConnectionError`` arm the raw aiohttp failure would
    escape the service call untranslated (the generic ``ApiError`` arm never sees
    it — a transport error is not an API error).
    """
    _register_switch_routes(mock_api)
    mock_api.patch(settings_url(), exception=aiohttp.ClientConnectionError("boom"))

    assert await setup_integration(hass, mock_config_entry)

    with pytest.raises(HomeAssistantError) as caught:
        await _turn_on(hass, _setting_switch_id(entity_registry))

    assert caught.value.translation_domain == DOMAIN
    assert caught.value.translation_key == "cannot_connect"


@pytest.mark.usefixtures("_only_switch_platform")
async def test_write_rejected_maps_to_setting_rejected_with_server_message(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """A rejected write surfaces ``setting_rejected`` carrying the server's reason.

    The server's explanation is the only clue why a value was refused, so it is
    forwarded verbatim as the ``message`` placeholder; dropping it (or mapping
    the rejection onto a generic key) would leave the user with an error that
    says nothing.
    """
    _register_switch_routes(mock_api)
    mock_api.patch(
        settings_url(),
        status=422,
        payload={"code": "ValidationError", "detail": "night mode is locked"},
    )

    assert await setup_integration(hass, mock_config_entry)

    with pytest.raises(HomeAssistantError) as caught:
        await _turn_on(hass, _setting_switch_id(entity_registry))

    assert caught.value.translation_domain == DOMAIN
    assert caught.value.translation_key == "setting_rejected"
    assert caught.value.translation_placeholders == {"message": "night mode is locked"}


# ---------------------------------------------------------------------------
# AquaHomeLeakDetectorEntity — the None-safe detector lookup
# ---------------------------------------------------------------------------


async def test_leak_entity_before_first_poll_uses_the_generic_sub_device_name(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A leak entity built before the first poll names itself and stays unavailable.

    The nickname is read from the coordinator's device view at construction; with
    no view yet (``coordinator.data is None``) the lookup must yield ``None`` and
    the sub-device fall back to ``Leak detector {id}``, rather than raising while
    the platform is being built.
    """
    mock_config_entry.add_to_hass(hass)
    device = Device.from_dict(load_fixture("device-detail.json"))
    coordinator = AquaHomeCoordinator(
        hass, mock_config_entry, _standalone_client(hass), device
    )
    entity = AquaHomeLeakDetectorEntity(
        coordinator, EntityDescription(key="leak_detected"), 1
    )

    # ``data`` is typed as the device but is ``None`` until a poll succeeds; the
    # annotation keeps mypy's narrowing off the rest of the test.
    device_view: Device | None = coordinator.data
    assert device_view is None
    assert entity.detector is None
    assert entity.available is False
    device_info = entity.device_info
    assert device_info is not None
    assert device_info["name"] == "Leak detector 1"


@pytest.mark.usefixtures("_only_binary_sensor_platform")
async def test_leak_entity_unavailable_when_the_whole_block_disappears(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
) -> None:
    """Dropping the enriched leak-detector block makes its entities unavailable.

    A detector unpaired from the app takes the whole ``leak_detectors`` block
    with it, not just its own entry in ``details``. The lookup must treat the
    missing block like a missing detector — without that leg it would raise
    ``AttributeError`` on every state write once the block was gone.
    """
    detail = load_fixture("device-detail.json")
    paired = with_leak_detectors(detail, [make_leak_detector(1, nickname="Kitchen")])
    unpaired = copy.deepcopy(paired)
    unpaired["enriched_data"]["water_treatment"].pop("leak_detectors")

    mock_api.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock_api.get(device_url(), payload=paired)
    mock_api.get(device_url(), payload=unpaired, repeat=True)
    add_activity_routes(mock_api)
    add_settings_routes(mock_api)

    assert await setup_integration(hass, mock_config_entry)

    entity_id = entity_registry.async_get_entity_id(
        Platform.BINARY_SENSOR, DOMAIN, f"{SLUG}_leak_1_leak_detected"
    )
    assert entity_id is not None
    before = hass.states.get(entity_id)
    assert before is not None
    assert before.state != STATE_UNAVAILABLE

    coordinator = mock_config_entry.runtime_data.coordinators[TEST_DEVICE_ID]
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    after = hass.states.get(entity_id)
    assert after is not None
    assert after.state == STATE_UNAVAILABLE
