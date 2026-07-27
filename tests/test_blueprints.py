"""Contract tests for the four bundled automation blueprints.

The blueprints in ``blueprints/automation/aquahome/`` are shipped YAML, not
Python: nothing in the test suite imports them, no platform loads them, and a
user only finds out they are broken when Home Assistant refuses the import. So
they are checked here the way Home Assistant itself checks them, and then
cross-checked against the integration they drive.

Three failure modes are guarded:

* **It will not import.** Each file is read with
  :func:`homeassistant.util.yaml.load_yaml` — the loader that understands the
  ``!input`` tag, which is why a plain ``yaml.safe_load`` cannot be used here —
  validated against
  :data:`homeassistant.components.blueprint.schemas.BLUEPRINT_SCHEMA`, and then
  built into a real :class:`~homeassistant.components.blueprint.models.Blueprint`,
  which additionally rejects a wrong ``domain`` and any ``!input`` reference
  without a matching input definition.

* **It refers to something the integration does not have.** Every
  ``aquahome_event`` type, every ``aquahome.*`` action and every entity-selector
  domain named in the YAML is compared against the constants the integration
  actually publishes. A renamed event type or action would otherwise leave a
  silently dead blueprint behind.

* **Nobody can install it.** The README must carry exactly one
  ``blueprint_import`` badge per blueprint, pointing at that file's raw URL on
  the repository the manifest documents, percent-encoded the way the redirect
  service requires.

Every expectation is derived from the integration's own constants (or the
manifest), never re-typed as a literal, so a rename in production surfaces here
as a failure rather than as two copies of a stale string agreeing with each
other.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

import pytest
from homeassistant.components.blueprint.models import Blueprint
from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
from homeassistant.util.yaml import extract_inputs, load_yaml

from custom_components.aquahome import logbook
from custom_components.aquahome.const import (
    DOMAIN,
    EVENT_AQUAHOME,
    EVENT_TYPE_LEAK_SUSPECTED,
    EVENT_TYPE_LEAK_WHILE_AWAY,
    EVENT_TYPE_REGEN_DEFERRAL_EXPIRED,
    EVENT_TYPE_REGEN_DEFERRED,
    EVENT_TYPE_REGEN_SCHEDULED,
    EVENT_TYPE_USAGE_ANOMALY,
    PLATFORMS,
    SERVICE_SCHEDULE_REGENERATION,
    SERVICE_SET_VACATION_MODE,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Repository root — the tests live one directory below it.
REPO_ROOT: Final = Path(__file__).resolve().parents[1]

#: Where Home Assistant looks for a custom integration's bundled blueprints.
BLUEPRINT_DIR: Final = REPO_ROOT / "blueprints" / "automation" / "aquahome"

#: The blueprint domain every file here must declare.
BLUEPRINT_DOMAIN: Final = "automation"

#: The Home Assistant floor the blueprints declare. Pinned rather than read back
#: from the file: it is a promise about the syntax used inside (``triggers:`` /
#: ``actions:``), so it must not be able to drift downwards unnoticed.
MIN_HA_VERSION: Final = "2026.2.0"

#: Platform domains the integration provides entities in — the only domains a
#: blueprint may filter an ``aquahome`` entity selector on, and the only ones a
#: non-``aquahome`` action may address (``valve.close`` on the shutoff valve).
PLATFORM_DOMAINS: Final = frozenset(platform.value for platform in PLATFORMS)

#: Bus events a blueprint may trigger on besides the integration's own. The
#: Companion app's actionable-notification replies come back on this one.
EXTERNAL_EVENT_TYPES: Final = frozenset({"mobile_app_notification_action"})

#: Top-level keys that would mean the pre-2024.10 automation syntax.
LEGACY_AUTOMATION_KEYS: Final = frozenset({"trigger", "condition", "action"})

#: Automation run modes Home Assistant accepts.
VALID_MODES: Final = frozenset({"single", "restart", "queued", "parallel"})

#: The badge image the README's import links are rendered as.
IMPORT_BADGE_URL: Final = "https://my.home-assistant.io/badges/blueprint_import.svg"


@dataclass(frozen=True, slots=True)
class BlueprintCase:
    """One shipped blueprint and everything it promises to reference."""

    file_name: str
    name: str
    event_types: frozenset[str]
    services: frozenset[str]

    @property
    def path(self) -> Path:
        """Return the blueprint's location on disk."""
        return BLUEPRINT_DIR / self.file_name


#: The four blueprints the integration bundles, with the integration surface
#: each one is required to drive. The event types and action names come from the
#: integration's constants, so a rename in production fails these tests instead
#: of quietly orphaning a blueprint.
BLUEPRINT_CASES: Final[tuple[BlueprintCase, ...]] = (
    BlueprintCase(
        file_name="leak_alert.yaml",
        name="AquaHome leak alert",
        event_types=frozenset({EVENT_TYPE_LEAK_SUSPECTED, EVENT_TYPE_LEAK_WHILE_AWAY}),
        services=frozenset(),
    ),
    BlueprintCase(
        file_name="auto_vacation_presence.yaml",
        name="AquaHome auto vacation",
        event_types=frozenset(),
        services=frozenset({SERVICE_SET_VACATION_MODE}),
    ),
    BlueprintCase(
        file_name="smart_regeneration_companion.yaml",
        name="AquaHome smart regeneration companion",
        event_types=frozenset(
            {
                EVENT_TYPE_REGEN_SCHEDULED,
                EVENT_TYPE_REGEN_DEFERRED,
                EVENT_TYPE_REGEN_DEFERRAL_EXPIRED,
            }
        ),
        services=frozenset({SERVICE_SCHEDULE_REGENERATION}),
    ),
    BlueprintCase(
        file_name="anomaly_check.yaml",
        name="AquaHome usage anomaly check",
        event_types=frozenset({EVENT_TYPE_USAGE_ANOMALY}),
        services=frozenset(),
    ),
)


# ---------------------------------------------------------------------------
# Loading + tree-walking helpers
# ---------------------------------------------------------------------------


def _load(case: BlueprintCase) -> dict[str, Any]:
    """Return one blueprint's parsed YAML.

    ``load_yaml`` rather than ``yaml.safe_load``: the blueprint files carry
    ``!input`` tags, which only Home Assistant's own loader resolves into the
    ``Input`` markers the blueprint machinery expects.
    """
    data = load_yaml(case.path)
    assert isinstance(data, dict), f"{case.file_name} is not a YAML mapping"
    return data


def _walk(node: Any) -> Iterator[Any]:
    """Yield every node of a parsed YAML document, containers included."""
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _mappings(data: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield every mapping inside a parsed YAML document."""
    for node in _walk(data):
        if isinstance(node, dict):
            yield node


def _event_triggers(data: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(event_type, event_data)`` for every event trigger in a document.

    Covers the top-level ``triggers:`` and the ``wait_for_trigger`` steps alike
    — both are plain mappings carrying an ``event_type``, so one walk finds
    them all.
    """
    for mapping in _mappings(data):
        event_type = mapping.get("event_type")
        if not isinstance(event_type, str):
            continue
        event_data = mapping.get("event_data")
        yield event_type, event_data if isinstance(event_data, dict) else {}


def _service_calls(data: dict[str, Any]) -> Iterator[tuple[str, str]]:
    """Yield ``(domain, service)`` for every literal action call in a document.

    Only dotted strings are service calls: an ``action:`` whose value is an
    ``!input`` marker (the notification service picker) is not a literal, and
    the undotted ``action:`` keys inside a notification's ``actions:`` list are
    Companion-app button ids rather than calls.
    """
    for mapping in _mappings(data):
        action = mapping.get("action")
        if isinstance(action, str) and "." in action:
            call_domain, _, service = action.partition(".")
            yield call_domain, service


def _integration_selector_domains(data: dict[str, Any]) -> Iterator[str]:
    """Yield the entity domain of every selector filtered to this integration.

    A device selector filters on the integration alone and yields nothing here;
    an entity selector names the domain as well, and that domain has to be one
    the integration actually registers entities in.
    """
    for mapping in _mappings(data):
        if mapping.get("integration") != DOMAIN:
            continue
        filtered_domain = mapping.get("domain")
        if isinstance(filtered_domain, str):
            yield filtered_domain


def _integration_selector_count(data: dict[str, Any]) -> int:
    """Return how many selector filters pin a choice to this integration."""
    return sum(1 for mapping in _mappings(data) if mapping.get("integration") == DOMAIN)


def _readme() -> str:
    """Return the repository README as text."""
    return (REPO_ROOT / "README.md").read_text(encoding="utf-8")


def _raw_base() -> str:
    """Return the raw-content base URL of the repository the manifest documents.

    Derived from the manifest rather than hard-coded, so a move to another
    account or repository name fails here instead of shipping import badges
    that 404.
    """
    manifest_path = REPO_ROOT / "custom_components" / DOMAIN / "manifest.json"
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    documentation = manifest["documentation"]
    assert isinstance(documentation, str)
    prefix = "https://github.com/"
    assert documentation.startswith(prefix), documentation
    return (
        f"https://raw.githubusercontent.com/{documentation.removeprefix(prefix)}/main"
    )


def _import_link(case: BlueprintCase) -> str:
    """Return the my.home-assistant.io import URL a blueprint must be linked by."""
    raw_url = f"{_raw_base()}/blueprints/automation/{DOMAIN}/{case.file_name}"
    return (
        "https://my.home-assistant.io/redirect/blueprint_import/"
        f"?blueprint_url={quote(raw_url, safe='')}"
    )


# ---------------------------------------------------------------------------
# The shipped set
# ---------------------------------------------------------------------------


def test_exactly_the_contract_blueprints_are_shipped() -> None:
    """The directory holds the four bundled blueprints and nothing else."""
    shipped = {path.name for path in BLUEPRINT_DIR.glob("*.yaml")}
    assert shipped == {case.file_name for case in BLUEPRINT_CASES}
    # No stray non-YAML files either: Home Assistant would try to load them.
    assert {path.name for path in BLUEPRINT_DIR.iterdir() if path.is_file()} == shipped


# ---------------------------------------------------------------------------
# Home Assistant's own acceptance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", BLUEPRINT_CASES, ids=lambda case: case.file_name)
def test_blueprint_parses_and_validates(case: BlueprintCase) -> None:
    """Each file parses with the ``!input`` loader and passes the real schema."""
    data = _load(case)

    # The schema on its own: a malformed input selector fails right here.
    BLUEPRINT_SCHEMA(data)

    # The model additionally rejects a wrong domain and any `!input` reference
    # with no matching input definition — the two mistakes a schema pass alone
    # would let through.
    blueprint = Blueprint(
        data,
        path=str(case.path),
        expected_domain=BLUEPRINT_DOMAIN,
        schema=BLUEPRINT_SCHEMA,
    )

    assert blueprint.name == case.name
    assert blueprint.domain == BLUEPRINT_DOMAIN
    assert blueprint.metadata["homeassistant"]["min_version"] == MIN_HA_VERSION
    # None means "this Home Assistant can run it" — the installed version is at
    # or above the declared floor.
    assert blueprint.validate() is None

    # A blueprint without a description is unusable in the UI picker.
    description = blueprint.metadata.get("description")
    assert isinstance(description, str)
    assert description.strip()


@pytest.mark.parametrize("case", BLUEPRINT_CASES, ids=lambda case: case.file_name)
def test_blueprint_inputs_are_declared_used_and_selectable(
    case: BlueprintCase,
) -> None:
    """Every declared input is referenced, and every one offers a selector."""
    data = _load(case)
    blueprint = Blueprint(
        data,
        path=str(case.path),
        expected_domain=BLUEPRINT_DOMAIN,
        schema=BLUEPRINT_SCHEMA,
    )

    # The model already refuses an undeclared reference; this pins the other
    # direction too, so a leftover input cannot linger in the UI doing nothing.
    assert extract_inputs(data) == set(blueprint.inputs)

    for input_name, definition in blueprint.inputs.items():
        assert isinstance(definition, dict), input_name
        assert definition.get("name"), f"input {input_name} has no display name"
        assert definition.get("description"), f"input {input_name} is undocumented"
        assert definition.get("selector"), f"input {input_name} has no selector"


@pytest.mark.parametrize("case", BLUEPRINT_CASES, ids=lambda case: case.file_name)
def test_blueprint_uses_the_modern_automation_syntax(case: BlueprintCase) -> None:
    """The plural ``triggers:``/``actions:`` keys, and a valid run mode."""
    data = _load(case)

    assert isinstance(data.get("triggers"), list)
    assert isinstance(data.get("actions"), list)
    assert not LEGACY_AUTOMATION_KEYS & set(data)
    assert data.get("mode") in VALID_MODES

    # Every trigger uses the modern `trigger:` discriminator rather than the
    # legacy `platform:` key.
    triggers = data["triggers"]
    for trigger in triggers:
        assert isinstance(trigger, dict)
        assert "platform" not in trigger
        assert isinstance(trigger.get("trigger"), str)


# ---------------------------------------------------------------------------
# Cross-checks against the integration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", BLUEPRINT_CASES, ids=lambda case: case.file_name)
def test_blueprint_triggers_on_real_integration_events(case: BlueprintCase) -> None:
    """Every ``aquahome_event`` type it listens for is one the integration fires."""
    data = _load(case)

    referenced: set[str] = set()
    for event_type, event_data in _event_triggers(data):
        if event_type != EVENT_AQUAHOME:
            # Anything else must be a bus event owned by someone else, named
            # explicitly — a typo'd `aquahome_events` would land here.
            assert event_type in EXTERNAL_EVENT_TYPES, event_type
            continue
        discriminator = event_data.get("type")
        assert isinstance(discriminator, str), (
            f"{case.file_name} listens to every {EVENT_AQUAHOME} without filtering"
        )
        referenced.add(discriminator)

    assert referenced == case.event_types
    # Anything a blueprint triggers on is user-visible history, so the logbook
    # describer must render it with a purpose-written message rather than its
    # generic fallback.
    # ``_MESSAGES`` is the describer's own dispatch table, read directly because
    # it is the only place the set of purpose-written types exists.
    assert referenced <= set(logbook._MESSAGES)


@pytest.mark.parametrize("case", BLUEPRINT_CASES, ids=lambda case: case.file_name)
def test_blueprint_calls_only_registered_actions(case: BlueprintCase) -> None:
    """Every literal action call names a real integration action or platform."""
    data = _load(case)

    called: set[str] = set()
    for call_domain, service in _service_calls(data):
        if call_domain == DOMAIN:
            called.add(service)
            continue
        # The only foreign call any blueprint makes is on an entity this
        # integration itself provides (the shutoff valve).
        assert call_domain in PLATFORM_DOMAINS, f"{call_domain}.{service}"

    assert called == case.services


@pytest.mark.parametrize("case", BLUEPRINT_CASES, ids=lambda case: case.file_name)
def test_blueprint_selectors_target_integration_platforms(
    case: BlueprintCase,
) -> None:
    """Entity pickers filtered to this integration name domains it registers."""
    data = _load(case)

    # Every blueprint pins its target to this integration at least once; a
    # blueprint that did not would offer the user every entity in the house.
    assert _integration_selector_count(data) >= 1
    for selector_domain in _integration_selector_domains(data):
        assert selector_domain in PLATFORM_DOMAINS, selector_domain


# ---------------------------------------------------------------------------
# The README import badges
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", BLUEPRINT_CASES, ids=lambda case: case.file_name)
def test_readme_offers_one_import_link_per_blueprint(case: BlueprintCase) -> None:
    """Each blueprint has exactly one correctly encoded import badge."""
    readme = _readme()
    link = _import_link(case)
    assert readme.count(link) == 1, f"{case.file_name} has no usable import badge"
    # And it is rendered as a clickable badge rather than pasted as bare text:
    # one line carrying both the badge image and the link target.
    assert any(
        IMPORT_BADGE_URL in line and f"]({link})" in line
        for line in readme.splitlines()
    ), f"{case.file_name}'s import link is not a clickable badge"


def test_readme_links_no_blueprint_that_is_not_shipped() -> None:
    """The README carries an import badge for every blueprint, and no extras."""
    readme = _readme()
    assert readme.count("blueprint_url=") == len(BLUEPRINT_CASES)
    assert readme.count(IMPORT_BADGE_URL) == len(BLUEPRINT_CASES)
