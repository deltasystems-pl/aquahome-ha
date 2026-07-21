"""Tests for the two feature-gated :class:`WaterTreatmentStatus` alert flags.

``alarm_is_beeping`` and ``water_to_drain_alert`` exist in the OpenAPI spec but
are absent from the dev device payload (they are feature-gated). These tests pin
the tolerant parsing contract: a real boolean survives, every non-boolean JSON
type collapses to ``None`` (never a truthiness coercion), an omitted key is
``None``, and the captured enriched fixture — which carries neither key — parses
both to ``None``. Pure model tests: no Home Assistant core involved.
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.aquahome.api.models import WaterTreatmentStatus
from tests.conftest import load_fixture

#: Sentinel meaning "the key is omitted from the payload entirely".
_ABSENT = object()

#: The two additive flags under test; both share the same tolerant parser.
_FLAGS = ("alarm_is_beeping", "water_to_drain_alert")

#: (raw payload value, expected parsed attribute) for each tolerant-parse case.
#: Only a genuine ``bool`` may pass through; ``int`` 0/1 must NOT coerce.
_CASES: tuple[tuple[Any, bool | None], ...] = (
    (True, True),
    (False, False),
    (_ABSENT, None),
    (None, None),
    ("true", None),
    ("false", None),
    (1, None),
    (0, None),
    (1.5, None),
    ([], None),
    ({}, None),
)


@pytest.mark.parametrize("flag", _FLAGS)
@pytest.mark.parametrize(("raw", "expected"), _CASES)
def test_status_flag_tolerant_parse(flag: str, raw: Any, expected: bool | None) -> None:
    """Each additive flag parses a bool through and collapses non-bools to None."""
    payload: dict[str, Any] = {} if raw is _ABSENT else {flag: raw}

    status = WaterTreatmentStatus.from_dict(payload)

    assert getattr(status, flag) is expected


def test_both_flags_present_do_not_disturb_other_flags() -> None:
    """Both new flags parse alongside the six plain alerts without cross-talk."""
    status = WaterTreatmentStatus.from_dict(
        {
            "salt_level_alert": True,
            "flow_monitor_alert": False,
            "connection_alert": False,
            "water_usage_alert": False,
            "resin_alert": False,
            "error_code_alert": True,
            "alarm_is_beeping": True,
            "water_to_drain_alert": False,
        }
    )

    assert status.alarm_is_beeping is True
    assert status.water_to_drain_alert is False
    assert status.salt_level_alert is True
    assert status.error_code_alert is True
    assert status.flow_monitor_alert is False


def test_enriched_fixture_omits_both_flags() -> None:
    """The captured enriched status block carries neither key -> both None."""
    payload = load_fixture("enriched-data.json")["water_treatment"][
        "water_treatment_status"
    ]

    status = WaterTreatmentStatus.from_dict(payload)

    assert status.alarm_is_beeping is None
    assert status.water_to_drain_alert is None
    # The six plain alert flags remain parsed from the same block.
    assert status.salt_level_alert is False
    assert status.alert_badge_count == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (_ABSENT, None),
        (None, None),
        ([], ()),
        (["E1"], ("E1",)),
        (["E1", "E2"], ("E1", "E2")),
        (["E1", 3, None], ("E1",)),
        ("E1", None),
        ({}, None),
    ],
)
def test_error_codes_tolerant_parse(raw: Any, expected: tuple[str, ...] | None) -> None:
    """``error_codes`` keeps absent (None) distinct from present-but-empty (())."""
    payload: dict[str, Any] = {} if raw is _ABSENT else {"error_codes": raw}

    status = WaterTreatmentStatus.from_dict(payload)

    assert status.error_codes == expected


def test_enriched_fixture_omits_error_codes() -> None:
    """The captured dev-device status block has no error_codes key -> None."""
    payload = load_fixture("enriched-data.json")["water_treatment"][
        "water_treatment_status"
    ]

    assert WaterTreatmentStatus.from_dict(payload).error_codes is None
