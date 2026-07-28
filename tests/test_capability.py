"""Tests for runtime capability re-detection (``dynamic.py``).

The integration forwards each platform exactly once, so hardware paired after
setup — a water-shutoff valve, a leak detector, a feature that only later shows
up in the enriched signature — would never materialise without a full reload.
:func:`~custom_components.aquahome.dynamic.async_setup_dynamic_entities` closes
that gap by watching the fast telemetry coordinator and growing the entity set
once a new capability key has been seen
:data:`~custom_components.aquahome.const.CAPABILITY_DEBOUNCE_POLLS` consecutive
polls (a single glitched payload cannot flap an entity into existence), while
never removing a key it has already created.

Two layers are exercised. The end-to-end tests boot the real integration on the
plain dev fixture (only the platform(s) under test forwarded) and then serve
synthetic ``with_wsov`` / ``with_leak_detectors`` payloads on successive polls,
driven by ``freezer`` + ``async_fire_time_changed`` on the fixed
:data:`~custom_components.aquahome.const.UPDATE_INTERVAL` cadence — asserting the
debounce delay, the flap reset, the leak sub-device wiring, the feature-gated
retrofit of the static platforms, and that removed hardware goes unavailable but
stays registered. The unit-level tests drive
:func:`async_setup_dynamic_entities` directly against a fake coordinator /
config-entry / add-entities harness to pin the debounce arithmetic on its own:
the initial set is added immediately, a first sighting only pends, the threshold
crossing adds, a vanish resets the streak, and a known key is never recreated or
removed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.const import STATE_UNAVAILABLE, Platform
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.aquahome.const import DOMAIN, UPDATE_INTERVAL
from custom_components.aquahome.dynamic import async_setup_dynamic_entities
from tests.conftest import (
    add_activity_routes,
    add_settings_routes,
    device_url,
    devices_url,
    load_fixture,
    setup_integration,
    with_leak_detectors,
    with_wsov,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from collections.abc import Set as AbstractSet
    from typing import Any

    from aioresponses import aioresponses
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.aquahome.coordinator import AquaHomeConfigEntry

#: Slug of the captured device's serial ``4213377-30105-2242``.
SLUG = "4213377_30105_2242"
#: Unique-id suffix of the single water-shutoff valve entity.
_VALVE_UID = f"{SLUG}_water_shutoff_valve"
#: Unique-id suffix of the feature-gated ``wsov_closed`` binary sensor.
_WSOV_CLOSED_UID = f"{SLUG}_wsov_closed"
#: The four per-detector binary keys and two sensor keys for detector id 1.
_LEAK_BINARY_KEYS = (
    "leak_detected",
    "leak_low_battery",
    "leak_tampered",
    "leak_connectivity",
)
_LEAK_SENSOR_KEYS = ("leak_temperature", "leak_signal_strength")


# ---------------------------------------------------------------------------
# End-to-end helpers (route sequencing + poll driving)
# ---------------------------------------------------------------------------


def _base_detail() -> dict[str, Any]:
    """Return a fresh copy of the captured (regeneration-only) device payload."""
    return load_fixture("device-detail.json")


def _register_polls(mock: aioresponses, details: list[dict[str, Any]]) -> None:
    """Register a device-detail payload per poll, the last one repeating.

    ``details[0]`` is consumed by the coordinator's first refresh at setup and
    each later element by one scheduled poll; the final element repeats so any
    extra poll keeps serving it. The device list, activity, and settings routes
    are served from the real fixtures (repeat) so the tolerant setup refreshes of
    the activity and settings coordinators succeed regardless of the platform
    under test. aioresponses matches routes in registration order and consumes a
    non-repeat route once.
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


# ---------------------------------------------------------------------------
# WSOV valve appears / flaps / is removed (feature-gated new platform)
# ---------------------------------------------------------------------------


async def test_wsov_valve_appears_after_two_consecutive_polls(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A valve paired after setup appears only after the second consecutive poll.

    Boot on the plain fixture (no valve), then serve ``with_wsov`` payloads: one
    poll sees the valve once and is still debounced out, the second consecutive
    sighting crosses :data:`~.const.CAPABILITY_DEBOUNCE_POLLS` and creates it.
    """
    _register_polls(mock_api, [_base_detail(), with_wsov(_base_detail())])

    with patch("custom_components.aquahome.PLATFORMS", [Platform.VALVE]):
        assert await setup_integration(hass, mock_config_entry)

        assert (
            entity_registry.async_get_entity_id(Platform.VALVE, DOMAIN, _VALVE_UID)
            is None
        )

        await _fire_next_poll(hass, freezer)
        assert (
            entity_registry.async_get_entity_id(Platform.VALVE, DOMAIN, _VALVE_UID)
            is None
        )

        await _fire_next_poll(hass, freezer)
        assert (
            entity_registry.async_get_entity_id(Platform.VALVE, DOMAIN, _VALVE_UID)
            is not None
        )


async def test_wsov_stale_reserve_does_not_fake_second_sighting(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A serve-stale re-serve is not an observation and cannot satisfy the debounce.

    One genuine poll carries the valve (streak 1); the next poll fails with a
    transient 500, so the coordinator re-serves the same cached payload. That
    re-serve repeats the previous observation — it must neither create the valve
    (a glitched payload plus a transient failure would otherwise fake the second
    sighting) nor reset the streak: the following genuine sighting is the real
    second one and creates the entity. (A 429 takes the identical coordinator
    path but also arms the client's real-monotonic backoff, which a frozen-clock
    test cannot outwait — the unit test below covers the counter arithmetic
    independent of the failure flavour.)
    """
    mock_api.get(devices_url(), payload=load_fixture("devices-list.json"), repeat=True)
    mock_api.get(device_url(), payload=_base_detail())
    mock_api.get(device_url(), payload=with_wsov(_base_detail()))
    mock_api.get(device_url(), status=500)
    mock_api.get(device_url(), payload=with_wsov(_base_detail()), repeat=True)
    add_activity_routes(mock_api)
    add_settings_routes(mock_api)

    with patch("custom_components.aquahome.PLATFORMS", [Platform.VALVE]):
        assert await setup_integration(hass, mock_config_entry)

        await _fire_next_poll(hass, freezer)  # genuine sighting: streak = 1
        await _fire_next_poll(hass, freezer)  # 500 -> stale re-serve: ignored
        assert (
            entity_registry.async_get_entity_id(Platform.VALVE, DOMAIN, _VALVE_UID)
            is None
        )

        await _fire_next_poll(hass, freezer)  # genuine second sighting
        assert (
            entity_registry.async_get_entity_id(Platform.VALVE, DOMAIN, _VALVE_UID)
            is not None
        )


async def test_wsov_flap_resets_debounce_counter(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A non-consecutive sighting sequence never crosses the threshold.

    The valve is present for one poll, absent the next, then present again once:
    the vanish resets the counter, so a lone final sighting counts as one — not
    two — and no entity is ever created.
    """
    _register_polls(
        mock_api,
        [
            _base_detail(),
            with_wsov(_base_detail()),
            _base_detail(),
            with_wsov(_base_detail()),
        ],
    )

    with patch("custom_components.aquahome.PLATFORMS", [Platform.VALVE]):
        assert await setup_integration(hass, mock_config_entry)

        await _fire_next_poll(hass, freezer)  # present: streak = 1
        await _fire_next_poll(hass, freezer)  # absent: streak reset
        await _fire_next_poll(hass, freezer)  # present again: streak = 1, not 2

        assert (
            entity_registry.async_get_entity_id(Platform.VALVE, DOMAIN, _VALVE_UID)
            is None
        )


async def test_wsov_removal_stays_registered_but_unavailable(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Once created, a valve that vanishes goes unavailable but stays registered.

    Two consecutive sightings create the valve (available, reading ``open``);
    reverting to the plain fixture drops the block, so the entity reports
    ``unavailable`` while its registry entry survives — removed hardware is never
    deleted, so history and customisations outlast a transient dropout.
    """
    _register_polls(
        mock_api,
        [
            _base_detail(),
            with_wsov(_base_detail()),
            with_wsov(_base_detail()),
            _base_detail(),
        ],
    )

    with patch("custom_components.aquahome.PLATFORMS", [Platform.VALVE]):
        assert await setup_integration(hass, mock_config_entry)

        await _fire_next_poll(hass, freezer)  # streak = 1
        await _fire_next_poll(hass, freezer)  # streak = 2 -> created

        entity_id = entity_registry.async_get_entity_id(
            Platform.VALVE, DOMAIN, _VALVE_UID
        )
        assert entity_id is not None
        created_state = hass.states.get(entity_id)
        assert created_state is not None
        assert created_state.state != STATE_UNAVAILABLE

        await _fire_next_poll(hass, freezer)  # hardware gone

        # The registry entry survives; only the live state goes unavailable.
        assert entity_registry.async_get(entity_id) is not None
        removed_state = hass.states.get(entity_id)
        assert removed_state is not None
        assert removed_state.state == STATE_UNAVAILABLE


# ---------------------------------------------------------------------------
# Leak detector appears (sub-device across two platforms)
# ---------------------------------------------------------------------------


async def test_leak_detector_appears_with_subdevice_wiring(  # noqa: PLR0913 - end-to-end fixtures + registries
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A detector paired after setup grows its binaries + sensors and sub-device.

    After two polls with ``with_leak_detectors`` the four binaries and two sensors
    exist (both platforms forwarded), and the detector is registered as its own
    device wired ``via_device`` to the softener, named for its nickname.
    """
    _register_polls(mock_api, [_base_detail(), with_leak_detectors(_base_detail())])

    with patch(
        "custom_components.aquahome.PLATFORMS",
        [Platform.BINARY_SENSOR, Platform.SENSOR],
    ):
        assert await setup_integration(hass, mock_config_entry)

        await _fire_next_poll(hass, freezer)  # streak = 1
        await _fire_next_poll(hass, freezer)  # streak = 2 -> created

        for key in _LEAK_BINARY_KEYS:
            assert (
                entity_registry.async_get_entity_id(
                    BINARY_SENSOR_DOMAIN, DOMAIN, f"{SLUG}_leak_1_{key}"
                )
                is not None
            ), key
        for key in _LEAK_SENSOR_KEYS:
            assert (
                entity_registry.async_get_entity_id(
                    SENSOR_DOMAIN, DOMAIN, f"{SLUG}_leak_1_{key}"
                )
                is not None
            ), key

        softener = device_registry.async_get_device(identifiers={(DOMAIN, SLUG)})
        assert softener is not None
        sub_device = device_registry.async_get_device(
            identifiers={(DOMAIN, f"{SLUG}_leak_1")}
        )
        assert sub_device is not None
        assert sub_device.via_device_id == softener.id
        assert sub_device.name == "Kitchen"


# ---------------------------------------------------------------------------
# Feature-gated EXISTING description grows in (retrofit of a static platform)
# ---------------------------------------------------------------------------


async def test_feature_gated_static_binary_grows_in(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: aioresponses,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A payload gaining the ``wsov`` feature retrofits the ``wsov_closed`` binary.

    The static binary-sensor platform is itself driven through the dynamic adder,
    so an existing feature-gated description that was absent at setup appears once
    the capability signature carries its feature for two consecutive polls.
    """
    _register_polls(
        mock_api,
        [_base_detail(), with_wsov(_base_detail()), with_wsov(_base_detail())],
    )

    with patch("custom_components.aquahome.PLATFORMS", [Platform.BINARY_SENSOR]):
        assert await setup_integration(hass, mock_config_entry)
        assert (
            entity_registry.async_get_entity_id(
                BINARY_SENSOR_DOMAIN, DOMAIN, _WSOV_CLOSED_UID
            )
            is None
        )

        await _fire_next_poll(hass, freezer)  # feature seen once
        assert (
            entity_registry.async_get_entity_id(
                BINARY_SENSOR_DOMAIN, DOMAIN, _WSOV_CLOSED_UID
            )
            is None
        )

        await _fire_next_poll(hass, freezer)  # second consecutive sighting
        assert (
            entity_registry.async_get_entity_id(
                BINARY_SENSOR_DOMAIN, DOMAIN, _WSOV_CLOSED_UID
            )
            is not None
        )


# ---------------------------------------------------------------------------
# Unit-level debounce arithmetic (fake coordinator / entry / add harness)
# ---------------------------------------------------------------------------


class _FakeCoordinator:
    """Minimal coordinator exposing only the listener API the helper uses."""

    def __init__(self) -> None:
        """Start with no registered listeners, serving fresh data."""
        self._listeners: list[Callable[[], None]] = []
        #: Mirrors the real coordinators' ``serving_stale`` property: ``True``
        #: while a refresh merely re-served cached data.
        self.serving_stale = False

    def async_add_listener(
        self, update_callback: Callable[[], None], context: Any = None
    ) -> Callable[[], None]:
        """Register ``update_callback`` and return its unsubscribe callable."""
        self._listeners.append(update_callback)

        def _remove() -> None:
            """Drop the registered listener."""
            self._listeners.remove(update_callback)

        return _remove

    def fire(self) -> None:
        """Simulate one coordinator update, dispatching to every listener."""
        for listener in list(self._listeners):
            listener()


class _FakeEntry:
    """Config-entry stand-in recording ``async_on_unload`` registrations."""

    def __init__(self) -> None:
        """Start with no unload callbacks registered."""
        self.unloads: list[Callable[[], None]] = []

    def async_on_unload(self, func: Callable[[], None]) -> Callable[[], None]:
        """Record and return an unload callback, mirroring the real signature."""
        self.unloads.append(func)
        return func


@dataclass
class _Harness:
    """Handles for driving and inspecting one dynamic-setup invocation.

    ``fire`` triggers a simulated coordinator update; ``present`` is the mutable
    key set ``discover`` reports; ``created`` records the key set handed to
    ``create`` on each add (mirroring what materialises); ``add_calls`` records
    every ``async_add_entities`` invocation; ``unloads`` the registered unload
    callbacks.
    """

    fire: Callable[[], None]
    present: set[str]
    created: list[set[str]]
    add_calls: list[list[object]]
    unloads: list[Callable[[], None]]
    coordinator: _FakeCoordinator


def _make_harness(initial: AbstractSet[str], *, debounce_polls: int) -> _Harness:
    """Invoke :func:`async_setup_dynamic_entities` against fakes, returning handles.

    The fakes stand in for the config entry, coordinator, and add-entities
    callback so the debounce bookkeeping can be exercised without Home Assistant.
    ``create`` returns the sorted keys (cast to the entity list the signature
    expects) purely so the batches are inspectable.
    """
    entry = _FakeEntry()
    coordinator = _FakeCoordinator()
    present: set[str] = set(initial)
    created: list[set[str]] = []
    add_calls: list[list[object]] = []

    def _discover() -> AbstractSet[str]:
        """Report the keys currently present."""
        return set(present)

    def _create(keys: AbstractSet[str]) -> list[Entity]:
        """Record the requested key set and return placeholder entities."""
        created.append(set(keys))
        return cast("list[Entity]", sorted(keys))

    def _add(entities: Iterable[object], *args: object, **kwargs: object) -> None:
        """Record one add-entities invocation."""
        add_calls.append(list(entities))

    async_setup_dynamic_entities(
        cast("AquaHomeConfigEntry", entry),
        cast("DataUpdateCoordinator[Any]", coordinator),
        cast("AddConfigEntryEntitiesCallback", _add),
        discover=_discover,
        create=_create,
        debounce_polls=debounce_polls,
    )
    return _Harness(
        coordinator.fire, present, created, add_calls, entry.unloads, coordinator
    )


def test_unit_initial_set_added_immediately() -> None:
    """Keys present at call time are created at once, with no debounce delay.

    Even at ``debounce_polls=2`` the initial set skips the debounce (platform
    setup already ran after the coordinator's first refresh), and a listener is
    armed for later growth and registered for unload.
    """
    harness = _make_harness({"a", "b"}, debounce_polls=2)

    assert harness.created == [{"a", "b"}]
    assert harness.add_calls == [["a", "b"]]
    assert len(harness.unloads) == 1


def test_unit_first_sighting_pends_then_threshold_adds() -> None:
    """A new key pends on its first sighting and is created on the second."""
    harness = _make_harness(set(), debounce_polls=2)
    assert harness.created == []

    harness.present.add("a")
    harness.fire()
    assert harness.created == []  # first sighting: pending only

    harness.fire()
    assert harness.created == [{"a"}]  # second consecutive sighting: created


def test_unit_vanish_resets_the_streak() -> None:
    """A key that vanishes before the threshold resets its consecutive streak."""
    harness = _make_harness(set(), debounce_polls=2)

    harness.present.add("a")
    harness.fire()  # streak = 1
    harness.present.discard("a")
    harness.fire()  # vanished -> streak reset
    harness.present.add("a")
    harness.fire()  # streak = 1 again, not 2
    assert harness.created == []

    harness.fire()  # streak = 2 -> finally created
    assert harness.created == [{"a"}]


def test_unit_known_key_never_recreated_or_removed() -> None:
    """A known key is never re-created when it flaps, and is never removed."""
    harness = _make_harness({"a"}, debounce_polls=1)
    assert harness.created == [{"a"}]

    harness.present.discard("a")
    harness.fire()  # known key vanishing removes nothing
    harness.present.add("a")
    harness.fire()  # already known -> not re-created

    assert harness.created == [{"a"}]
    assert len(harness.add_calls) == 1


def test_unit_stale_reserve_neither_advances_nor_resets() -> None:
    """A ``serving_stale`` update is skipped: no count-up, no streak reset.

    With the streak at 1, a stale re-serve must not promote the key (that would
    let one glitched payload plus a rate-limited re-serve fake the second
    sighting) — and it must not reset the streak either, so the next genuine
    sighting is the real second one and creates the key.
    """
    harness = _make_harness(set(), debounce_polls=2)

    harness.present.add("a")
    harness.fire()  # genuine sighting: streak = 1
    harness.coordinator.serving_stale = True
    harness.fire()  # stale re-serve: ignored entirely
    assert harness.created == []

    harness.coordinator.serving_stale = False
    harness.fire()  # genuine second sighting -> created
    assert harness.created == [{"a"}]


def test_unit_create_receives_only_the_new_subset() -> None:
    """A later growth passes only the newly-added keys to ``create``.

    Re-creating already-known keys would duplicate their entities (same unique
    IDs), so the second batch must be exactly the delta.
    """
    harness = _make_harness({"a"}, debounce_polls=1)
    assert harness.created == [{"a"}]

    harness.present.add("b")
    harness.fire()

    assert harness.created == [{"a"}, {"b"}]
    assert harness.add_calls == [["a"], ["b"]]


def test_unit_debounce_one_adds_on_first_sighting() -> None:
    """At ``debounce_polls=1`` a single sighting suffices to create the key.

    The settings coordinator uses this cadence: its document is authoritative and
    its 6-hour interval cannot afford a two-poll delay.
    """
    harness = _make_harness(set(), debounce_polls=1)
    assert harness.created == []

    harness.present.add("s")
    harness.fire()
    assert harness.created == [{"s"}]
