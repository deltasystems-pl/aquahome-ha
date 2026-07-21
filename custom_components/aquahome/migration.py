"""Version-gated entity unique-ID migration helper.

When a future integration version renames an entity description ``key`` the
entity's ``unique_id`` suffix changes, which would otherwise orphan its recorder
history and any user customization. :func:`async_migrate_unique_ids` rewrites the
stored unique IDs in place for one config entry and raises a single Repairs issue
so every changed identity is visible to the user.

The table is empty today (no renames have happened yet), but the machinery is
real and covered by tests so a version bump can call it directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_registry import RegistryEntry

    from .coordinator import AquaHomeConfigEntry


async def async_migrate_unique_ids(
    hass: HomeAssistant, entry: AquaHomeConfigEntry, renames: Mapping[str, str]
) -> dict[str, str]:
    """Rename entity ``unique_id`` suffixes (old key -> new key) for this entry.

    ``renames`` maps an old description-key suffix to its replacement. Every
    registry entry of ``entry`` whose ``unique_id`` ends with ``f"_{old}"`` has
    that trailing suffix rewritten to ``f"_{new}"`` (the slug prefix is left
    untouched). Returns ``{old_unique_id: new_unique_id}`` for every entity
    actually migrated.

    When at least one rename happens a single Repairs issue is created
    (``unique_id_migration_{entry.entry_id}``, translation key
    ``unique_id_migration``, WARNING, not fixable) whose ``renames`` placeholder
    lists one ``entity_id: old -> new`` line per changed entity. An empty
    ``renames`` mapping is a no-op and raises no issue.
    """
    if not renames:
        return {}

    migrated: dict[str, str] = {}
    lines: list[str] = []

    def _migrate(registry_entry: RegistryEntry) -> dict[str, str] | None:
        """Return the ``new_unique_id`` update for a matching entry, else ``None``."""
        unique_id = registry_entry.unique_id
        for old, new in renames.items():
            suffix = f"_{old}"
            if unique_id.endswith(suffix):
                new_unique_id = f"{unique_id[: -len(suffix)]}_{new}"
                migrated[unique_id] = new_unique_id
                lines.append(
                    f"{registry_entry.entity_id}: {unique_id} -> {new_unique_id}"
                )
                return {"new_unique_id": new_unique_id}
        return None

    await er.async_migrate_entries(hass, entry.entry_id, _migrate)

    if migrated:
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"unique_id_migration_{entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="unique_id_migration",
            translation_placeholders={"renames": "\n".join(lines)},
        )

    return migrated
