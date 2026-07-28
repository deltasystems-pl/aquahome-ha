"""Tests for the entity unique-ID migration helper.

:func:`custom_components.aquahome.migration.async_migrate_unique_ids` rewrites the
trailing key suffix of an entry's entity ``unique_id`` values and raises a single
Repairs issue listing every changed identity. These tests seed the entity
registry directly (no cloud involved) and assert the rewrites, the returned map,
and the issue — including the no-op paths that must create nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aquahome.const import (
    CONFIG_MINOR_VERSION,
    CONFIG_VERSION,
    DOMAIN,
)
from custom_components.aquahome.migration import async_migrate_unique_ids
from tests.conftest import TEST_USER_ID

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

#: Slug stem shared by every seeded entity (matches the device fixture serial).
SLUG = "4213377_30105_2242"


@pytest.fixture
def migration_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Return a config entry added to hass for registry-migration tests."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_USER_ID,
        version=CONFIG_VERSION,
        minor_version=CONFIG_MINOR_VERSION,
    )
    entry.add_to_hass(hass)
    return entry


def _current_unique_id(registry: er.EntityRegistry, entity_id: str) -> str:
    """Return the stored ``unique_id`` of a registry entity (which must exist)."""
    entity = registry.async_get(entity_id)
    assert entity is not None
    return entity.unique_id


def _migration_issue(
    hass: HomeAssistant, entry: MockConfigEntry
) -> ir.IssueEntry | None:
    """Return the entry's unique-id-migration Repairs issue, if any."""
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"unique_id_migration_{entry.entry_id}"
    )


async def test_migrate_unique_ids_renames_and_creates_issue(
    hass: HomeAssistant, migration_entry: MockConfigEntry
) -> None:
    """Matching suffixes are rewritten, non-matching untouched, one issue raised."""
    registry = er.async_get(hass)
    salt = registry.async_get_or_create(
        "sensor", DOMAIN, f"{SLUG}_salt_pct", config_entry=migration_entry
    )
    gallons = registry.async_get_or_create(
        "sensor", DOMAIN, f"{SLUG}_gals_today", config_entry=migration_entry
    )
    untouched = registry.async_get_or_create(
        "sensor", DOMAIN, f"{SLUG}_total_water", config_entry=migration_entry
    )

    renames = {"salt_pct": "salt_level", "gals_today": "water_used_today"}
    result = await async_migrate_unique_ids(hass, migration_entry, renames)

    assert result == {
        f"{SLUG}_salt_pct": f"{SLUG}_salt_level",
        f"{SLUG}_gals_today": f"{SLUG}_water_used_today",
    }
    assert _current_unique_id(registry, salt.entity_id) == f"{SLUG}_salt_level"
    assert _current_unique_id(registry, gallons.entity_id) == f"{SLUG}_water_used_today"
    # A suffix absent from the rename table keeps its original identity.
    assert _current_unique_id(registry, untouched.entity_id) == f"{SLUG}_total_water"

    issue = _migration_issue(hass, migration_entry)
    assert issue is not None
    assert issue.translation_key == "unique_id_migration"
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.is_fixable is False
    assert issue.translation_placeholders is not None
    lines = issue.translation_placeholders["renames"]
    assert f"{salt.entity_id}: {SLUG}_salt_pct -> {SLUG}_salt_level" in lines
    assert f"{gallons.entity_id}: {SLUG}_gals_today -> {SLUG}_water_used_today" in lines

    domain_issues = [
        issue
        for (domain, _), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN
    ]
    assert len(domain_issues) == 1


async def test_migrate_unique_ids_empty_table_is_noop(
    hass: HomeAssistant, migration_entry: MockConfigEntry
) -> None:
    """An empty rename table changes nothing and raises no Repairs issue."""
    registry = er.async_get(hass)
    entity = registry.async_get_or_create(
        "sensor", DOMAIN, f"{SLUG}_salt_level", config_entry=migration_entry
    )

    result = await async_migrate_unique_ids(hass, migration_entry, {})

    assert result == {}
    assert _current_unique_id(registry, entity.entity_id) == f"{SLUG}_salt_level"
    assert _migration_issue(hass, migration_entry) is None


async def test_migrate_unique_ids_no_match_raises_no_issue(
    hass: HomeAssistant, migration_entry: MockConfigEntry
) -> None:
    """A rename that matches no entity leaves the registry and issues untouched."""
    registry = er.async_get(hass)
    entity = registry.async_get_or_create(
        "sensor", DOMAIN, f"{SLUG}_total_water", config_entry=migration_entry
    )

    result = await async_migrate_unique_ids(
        hass, migration_entry, {"salt_pct": "salt_level"}
    )

    assert result == {}
    assert _current_unique_id(registry, entity.entity_id) == f"{SLUG}_total_water"
    assert _migration_issue(hass, migration_entry) is None
