"""Runtime capability re-detection for AquaHome entity platforms.

Some AquaHome hardware is added after the integration is first set up: a
water-shutoff valve or a leak detector paired later, a setting that only appears
once another is toggled. Home Assistant forwards each platform exactly once, so
those entities would otherwise never materialise until a full reload.

:func:`async_setup_dynamic_entities` closes that gap. A platform hands it a
``discover`` callable — the set of stable, unique keys present *right now* — and a
``create`` callable that builds entities for a given key set. The helper adds the
entities that exist at setup immediately, then watches the coordinator for keys
that appear later and adds them once they have been seen for
``debounce_polls`` consecutive updates, so a single glitched payload cannot flap
entities into existence. Keys are never removed: hardware that disappears goes
*unavailable* through each entity's own :attr:`available`, and its entity stays
registered so its history and customisations survive a transient dropout.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.core import callback

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Set as AbstractSet

    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

    from .coordinator import AquaHomeConfigEntry


@callback
def async_setup_dynamic_entities(  # noqa: PLR0913 - contract-fixed capability-detection signature
    entry: AquaHomeConfigEntry,
    coordinator: DataUpdateCoordinator[Any],
    async_add_entities: AddConfigEntryEntitiesCallback,
    *,
    discover: Callable[[], AbstractSet[str]],
    create: Callable[[AbstractSet[str]], list[Entity]],
    debounce_polls: int = 1,
) -> None:
    """Add discovered entities now and grow the set as capabilities appear.

    Platform setup runs only after the coordinator's first refresh has
    succeeded, so the keys ``discover`` reports at call time are authoritative:
    their entities are created immediately, with no debounce on the initial set.

    A listener is then registered on ``coordinator`` (removed on unload through
    ``entry.async_on_unload``). On every coordinator update it recomputes the
    current key set. A key that is not already known increments a
    consecutive-sightings counter; once a key has been seen ``debounce_polls``
    updates in a row it is created and becomes known. A key that vanishes before
    reaching the threshold has its counter reset, so only *consecutive* sightings
    count. A refresh that merely re-served cached data (the coordinator's
    ``serving_stale``) is ignored entirely — it repeats the previous payload, so
    letting it advance the counter would allow one glitched poll plus a
    rate-limited re-serve to fake the second sighting. Known keys are never
    removed — removed hardware is surfaced as unavailable by each entity, never
    deleted here.

    ``debounce_polls`` is :data:`~.const.CAPABILITY_DEBOUNCE_POLLS` (2) for the
    fast telemetry coordinator, whose payloads can blip; it is ``1`` for the
    settings coordinator, whose document is authoritative and whose 6-hour cadence
    cannot afford a two-poll delay.
    """
    known: set[str] = set(discover())
    if known:
        async_add_entities(create(known))
    #: Per-key count of consecutive updates a not-yet-known key has been seen.
    pending: dict[str, int] = {}

    @callback
    def _handle_update() -> None:
        """Grow the entity set when a new key persists for ``debounce_polls``."""
        if getattr(coordinator, "serving_stale", False):
            # A stale re-serve repeats the cached payload verbatim: it carries
            # no new observation, so it must neither advance nor reset the
            # consecutive-sightings counters. Without this, one glitched 200
            # payload followed by a rate-limited re-serve would fake the second
            # sighting and defeat the debounce.
            return
        current = discover()
        added: set[str] = set()
        for key in current:
            if key in known:
                continue
            count = pending.get(key, 0) + 1
            if count >= debounce_polls:
                added.add(key)
                pending.pop(key, None)
            else:
                pending[key] = count
        # A key that did not appear this update breaks its consecutive streak.
        for key in [key for key in pending if key not in current]:
            del pending[key]
        if added:
            known.update(added)
            async_add_entities(create(added))

    entry.async_on_unload(coordinator.async_add_listener(_handle_update))
