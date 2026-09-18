"""Unit tests for optimizer and directive normalization (OPT-01)."""

from enum import Enum
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest

# Ensure repository root is in sys.path without modifying pyproject.toml
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.optimizer import (
    HourlyConstraints,
    normalize_directives,
    normalize_request_directives,
    build_hourly_constraints,
)


FIXTURE_PATH = (
    Path(__file__).parent.parent / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
)


def test_empty_or_none_directives_default_constraints() -> None:
    """Empty or None directives list must yield 24 default values."""
    for empty_input in ([], None):
        constraints = normalize_directives(empty_input, base_minimum_kwh=50.0)

        assert len(constraints.solar_factors) == 24
        assert len(constraints.reserve_floors) == 24
        assert len(constraints.allow_charge) == 24
        assert len(constraints.allow_discharge) == 24
        assert len(constraints.grid_caps) == 24

        assert constraints.solar_factors == [1.0] * 24
        assert constraints.reserve_floors == [50.0] * 24
        assert constraints.allow_charge == [True] * 24
        assert constraints.allow_discharge == [True] * 24
        assert constraints.grid_caps == [None] * 24


def test_solar_reduction_single_and_overlap() -> None:
    """Overlapping solar reductions must take the minimum remaining solar factor."""
    directives = [
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "solar_reduction",
            "structured_adjustment": {"hours": [12, 13, 14], "factor": 0.5},
            "explanation": "Cloud cover",
        },
        {
            "note_index": 1,
            "applies": True,
            "directive_type": "solar_reduction",
            "structured_adjustment": {"hours": [13, 14, 15], "factor": 0.2},
            "explanation": "Panel cleaning",
        },
    ]

    constraints = normalize_directives(directives)

    # Hour 12 only influenced by first directive: factor 0.5
    assert constraints.solar_factors[12] == pytest.approx(0.5)
    # Hours 13 and 14: min(0.5, 0.2) = 0.2
    assert constraints.solar_factors[13] == pytest.approx(0.2)
    assert constraints.solar_factors[14] == pytest.approx(0.2)
    # Hour 15 only influenced by second directive: factor 0.2
    assert constraints.solar_factors[15] == pytest.approx(0.2)
    # Other hours untouched (1.0)
    assert constraints.solar_factors[0] == pytest.approx(1.0)
    assert constraints.solar_factors[11] == pytest.approx(1.0)
    assert constraints.solar_factors[16] == pytest.approx(1.0)


def test_reserve_floor_single_and_overlap() -> None:
    """Overlapping minimum battery reserves must take the maximum reserve floor."""
    directives = [
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "minimum_battery_reserve",
            "structured_adjustment": {
                "hours": [17, 18, 19],
                "minimum_energy_kwh": 120.0,
            },
            "explanation": "Grid stability warning",
        },
        {
            "note_index": 1,
            "applies": True,
            "directive_type": "minimum_battery_reserve",
            "structured_adjustment": {
                "hours": [18, 19, 20],
                "minimum_energy_kwh": 160.0,
            },
            "explanation": "Storm protocol",
        },
    ]

    constraints = normalize_directives(directives, base_minimum_kwh=50.0)

    assert constraints.reserve_floors[17] == pytest.approx(120.0)
    # Hours 18 and 19: max(120.0, 160.0) = 160.0
    assert constraints.reserve_floors[18] == pytest.approx(160.0)
    assert constraints.reserve_floors[19] == pytest.approx(160.0)
    assert constraints.reserve_floors[20] == pytest.approx(160.0)
    # Baseline reserve elsewhere
    assert constraints.reserve_floors[0] == pytest.approx(50.0)
    assert constraints.reserve_floors[16] == pytest.approx(50.0)
    assert constraints.reserve_floors[21] == pytest.approx(50.0)


def test_charge_discharge_windows_union_and_idle() -> None:
    """No-charge and no-discharge windows must union hours; overlap forces idle."""
    directives = [
        {
            "applies": True,
            "directive_type": "no_charge_window",
            "structured_adjustment": {"hours": [17, 18, 19]},
        },
        {
            "applies": True,
            "directive_type": "no_charge_window",
            "structured_adjustment": {"hours": [19, 20]},
        },
        {
            "applies": True,
            "directive_type": "no_discharge_window",
            "structured_adjustment": {"hours": [19, 21, 22]},
        },
    ]

    constraints = normalize_directives(directives)

    # Union of no-charge: 17, 18, 19, 20
    for h in [17, 18, 19, 20]:
        assert constraints.allow_charge[h] is False
    assert constraints.allow_charge[16] is True
    assert constraints.allow_charge[21] is True

    # No-discharge: 19, 21, 22
    for h in [19, 21, 22]:
        assert constraints.allow_discharge[h] is False
    assert constraints.allow_discharge[18] is True
    assert constraints.allow_discharge[20] is True

    # Hour 19 has both no-charge and no-discharge -> idle forced
    assert constraints.is_idle_forced(19) is True
    assert constraints.is_idle_forced(18) is False
    assert constraints.is_idle_forced(21) is False


def test_grid_cap_single_and_overlap() -> None:
    """Overlapping grid caps must take the minimum cap."""
    directives = [
        {
            "applies": True,
            "directive_type": "max_grid_window",
            "structured_adjustment": {"hours": [18, 19], "max_grid_kwh": 150.0},
        },
        {
            "applies": True,
            "directive_type": "max_grid_window",
            "structured_adjustment": {"hours": [19, 20], "max_grid_kwh": 100.0},
        },
    ]

    constraints = normalize_directives(directives)

    assert constraints.grid_caps[18] == pytest.approx(150.0)
    # Hour 19: min(150.0, 100.0) = 100.0
    assert constraints.grid_caps[19] == pytest.approx(100.0)
    assert constraints.grid_caps[20] == pytest.approx(100.0)
    assert constraints.grid_caps[17] is None
    assert constraints.grid_caps[21] is None


def test_boundary_hours_zero_and_twenty_three() -> None:
    """Constraints applied to boundary hours 0 and 23 must be preserved."""
    directives = [
        {
            "applies": True,
            "directive_type": "solar_reduction",
            "structured_adjustment": {"hours": [0], "factor": 0.0},
        },
        {
            "applies": True,
            "directive_type": "no_charge_window",
            "structured_adjustment": {"hours": [0, 23]},
        },
        {
            "applies": True,
            "directive_type": "max_grid_window",
            "structured_adjustment": {"hours": [23], "max_grid_kwh": 75.0},
        },
    ]

    constraints = normalize_directives(directives)

    assert constraints.solar_factors[0] == pytest.approx(0.0)
    assert constraints.allow_charge[0] is False
    assert constraints.allow_charge[23] is False
    assert constraints.grid_caps[23] == pytest.approx(75.0)


def test_enum_and_object_compatibility() -> None:
    """Directives passed as objects (e.g. Pydantic models) with Enum directive_type."""
    class DirectiveTypeEnum(str, Enum):
        SOLAR_REDUCTION = "solar_reduction"
        NO_OP = "no_op"

    obj_directive = SimpleNamespace(
        applies=True,
        directive_type=DirectiveTypeEnum.SOLAR_REDUCTION,
        structured_adjustment=SimpleNamespace(hours=[10, 11], factor=0.4),
        explanation="Object representation",
    )
    noop_directive = SimpleNamespace(
        applies=False,
        directive_type=DirectiveTypeEnum.NO_OP,
        structured_adjustment=None,
        explanation="No op",
    )

    constraints = normalize_directives([obj_directive, noop_directive])
    assert constraints.solar_factors[10] == pytest.approx(0.4)
    assert constraints.solar_factors[11] == pytest.approx(0.4)
    assert constraints.solar_factors[9] == pytest.approx(1.0)


def test_normalize_request_directives() -> None:
    """Convenience helper extracts base_minimum_kwh from request battery object."""
    fake_request = {
        "battery": {
            "capacity_kwh": 500,
            "minimum_energy_kwh": 75.0,
        }
    }
    directives = [
        {
            "applies": True,
            "directive_type": "no_charge_window",
            "structured_adjustment": {"hours": [1]},
        }
    ]

    constraints = normalize_request_directives(fake_request, directives)
    assert constraints.reserve_floors == [75.0] * 24
    assert constraints.allow_charge[1] is False
    assert constraints.allow_charge[0] is True


def test_no_op_and_unapplied_directives_ignored() -> None:
    """no_op or applies=False directives must be completely ignored."""
    directives = [
        {
            "applies": False,
            "directive_type": "solar_reduction",
            "structured_adjustment": {"hours": [12, 13], "factor": 0.1},
        },
        {
            "applies": False,
            "directive_type": "no_op",
            "structured_adjustment": None,
        },
        {
            "applies": True,
            "directive_type": "no_op",
            "structured_adjustment": None,
        },
    ]

    constraints = normalize_directives(directives, base_minimum_kwh=30.0)
    assert constraints.solar_factors == [1.0] * 24
    assert constraints.reserve_floors == [30.0] * 24
    assert constraints.allow_charge == [True] * 24
    assert constraints.allow_discharge == [True] * 24
    assert constraints.grid_caps == [None] * 24


def test_alias_build_hourly_constraints() -> None:
    """build_hourly_constraints alias must match normalize_directives."""
    res1 = normalize_directives([], base_minimum_kwh=40.0)
    res2 = build_hourly_constraints([], base_minimum_kwh=40.0)
    assert res1 == res2


def test_public_sample_cases_directives() -> None:
    """Validate normalize_directives against all 10 official public sample cases."""
    assert FIXTURE_PATH.exists(), f"Sample cases not found at {FIXTURE_PATH}"

    with open(FIXTURE_PATH, encoding="utf-8") as f:
        data = json.load(f)

    cases = data.get("cases", [])
    assert len(cases) == 10

    for case in cases:
        battery = case["input"]["battery"]
        directives = case["expected_output"]["directive_interpretation"]

        constraints = normalize_directives(
            directives,
            base_minimum_kwh=battery["minimum_energy_kwh"],
        )

        assert len(constraints.solar_factors) == 24
        assert len(constraints.reserve_floors) == 24
        assert len(constraints.allow_charge) == 24
        assert len(constraints.allow_discharge) == 24
        assert len(constraints.grid_caps) == 24

        # Verify specific case invariants based on their expected directives
        for d in directives:
            if not d.get("applies"):
                continue
            dtype = d["directive_type"]
            adj = d["structured_adjustment"]
            hours = adj["hours"]

            if dtype == "solar_reduction":
                for h in hours:
                    assert constraints.solar_factors[h] <= adj["factor"]
            elif dtype == "minimum_battery_reserve":
                for h in hours:
                    assert constraints.reserve_floors[h] >= adj["minimum_energy_kwh"]
            elif dtype == "no_charge_window":
                for h in hours:
                    assert constraints.allow_charge[h] is False
            elif dtype == "no_discharge_window":
                for h in hours:
                    assert constraints.allow_discharge[h] is False
            elif dtype == "max_grid_window":
                for h in hours:
                    assert constraints.grid_caps[h] <= adj["max_grid_kwh"]
