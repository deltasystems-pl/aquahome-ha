"""Shared fixtures for the AquaHome test suite."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture by file name."""
    data: dict[str, Any] = json.loads((FIXTURES_DIR / name).read_text())
    return data


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> None:
    """Enable loading custom integrations in all tests."""
    return
