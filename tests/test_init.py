"""Phase 0 smoke tests: manifest sanity and test-harness wiring."""

from __future__ import annotations

import json
from pathlib import Path

from homeassistant.core import HomeAssistant

from custom_components.aquahome.const import DOMAIN

MANIFEST = Path("custom_components/aquahome/manifest.json")


def test_manifest_is_consistent() -> None:
    """Manifest domain, directory name, and const must agree."""
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["domain"] == DOMAIN
    assert MANIFEST.parent.name == DOMAIN
    assert manifest["iot_class"] == "cloud_polling"
    assert manifest["requirements"] == []
    assert manifest["version"]


async def test_harness_boots(hass: HomeAssistant) -> None:
    """The pytest-homeassistant-custom-component harness starts a core."""
    assert hass.state.value == "RUNNING"
