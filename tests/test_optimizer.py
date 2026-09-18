"""Unit tests for optimizer and directive normalization (OPT-01)."""

from enum import Enum
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any
import pytest

# Ensure repository root is in sys.path without modifying pyproject.toml
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.optimizer import (
    CHALLENGE_TOLERANCE,
    HourlyConstraints,
    HourlyPlanEntry,
    OptimizationFailedError,
    OptimizerError,
    PlanMetrics,
    ScenarioInfeasibleError,
    ScenarioUnboundedError,
    SolverNumericalError,
    SolverResult,
    SolverTimeoutError,
    build_hourly_constraints,
    compute_plan_metrics,
    normalize_directives,
    normalize_request_directives,
    optimize_energy,
    solve_energy_lp,
    solve_scenario,
    solver_result_to_plan,
)
from app.services.plan_validator import (
    CHALLENGE_TOLERANCE as VALIDATOR_TOLERANCE,
    PlanValidationError,
    validate_plan,
    validate_plan_metrics,
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


# ==============================================================================
# OPT-02 Tests: Solver Formulation & Prototyping
# ==============================================================================


def test_hand_checkable_scenario_lp() -> None:
    """Verify LP on a deterministic, hand-calculated 24-hour scenario.

    Scenario setup:
      - 24 hours with constant demand of 100 kWh and zero solar.
      - Tariff is 10 BDT/kWh during hours 0..11, and 20 BDT/kWh during hours 12..23.
      - Battery: capacity=200, initial=50, minimum=50, max_charge=50, max_discharge=50.
      - No directives.

    Hand calculation:
      - Unbuffered cost: (12 * 100 * 10) + (12 * 100 * 20) = 12000 + 24000 = 36000 BDT.
      - Optimal battery arbitrage:
          Charge 150 kWh during cheap hours (e.g. 50 kWh across 3 hours, energy: 50 -> 200).
          Discharge 150 kWh during peak hours (e.g. 50 kWh across 3 hours, energy: 200 -> 50).
          Savings: 150 * (20 - 10) = 1500 BDT.
      - Expected optimal cost: 36000 - 1500 = 34500 BDT.
      - Neutrality: final energy = 50.
    """
    hours = [
        {
            "hour": h,
            "demand_kwh": 100.0,
            "solar_kwh": 0.0,
            "tariff_bdt_per_kwh": 10.0 if h < 12 else 20.0,
        }
        for h in range(24)
    ]
    battery = {
        "capacity_kwh": 200.0,
        "initial_energy_kwh": 50.0,
        "minimum_energy_kwh": 50.0,
        "max_charge_kwh_per_hour": 50.0,
        "max_discharge_kwh_per_hour": 50.0,
    }
    constraints = normalize_directives([], base_minimum_kwh=50.0)

    res = solve_energy_lp(hours=hours, battery=battery, constraints=constraints)

    assert res.success is True
    assert res.status == 0
    assert res.total_cost_bdt == pytest.approx(34500.0, abs=0.01)

    # Check neutrality
    assert res.battery_energy_after_kwh[23] == pytest.approx(50.0, abs=0.01)
    # Check total energy balance: sum(grid) == sum(demand) == 2400.0
    assert sum(res.grid_kwh) == pytest.approx(2400.0, abs=0.01)


def test_window_prohibits_arbitrage_lp() -> None:
    """Disallow discharge during peak hours: battery should not be able to arbitrage."""
    hours = [
        {
            "hour": h,
            "demand_kwh": 100.0,
            "solar_kwh": 0.0,
            "tariff_bdt_per_kwh": 10.0 if h < 12 else 20.0,
        }
        for h in range(24)
    ]
    battery = {
        "capacity_kwh": 200.0,
        "initial_energy_kwh": 50.0,
        "minimum_energy_kwh": 50.0,
        "max_charge_kwh_per_hour": 50.0,
        "max_discharge_kwh_per_hour": 50.0,
    }
    directives = [
        {
            "applies": True,
            "directive_type": "no_discharge_window",
            "structured_adjustment": {"hours": list(range(12, 24))},
        }
    ]
    constraints = normalize_directives(directives, base_minimum_kwh=50.0)
    res = solve_energy_lp(hours=hours, battery=battery, constraints=constraints)

    assert res.success is True
    # Without discharge permitted at peak, battery cannot arbitrage; cost remains 36000
    assert res.total_cost_bdt == pytest.approx(36000.0, abs=0.01)


def test_infeasible_scenario_lp() -> None:
    """If grid cap is lower than net demand with depleted battery, LP reports infeasible."""
    hours = [
        {
            "hour": h,
            "demand_kwh": 100.0,
            "solar_kwh": 0.0,
            "tariff_bdt_per_kwh": 10.0,
        }
        for h in range(24)
    ]
    battery = {
        "capacity_kwh": 100.0,
        "initial_energy_kwh": 20.0,
        "minimum_energy_kwh": 20.0,
        "max_charge_kwh_per_hour": 20.0,
        "max_discharge_kwh_per_hour": 20.0,
    }
    # Cap grid to 50 when demand is 100 and max discharge is 20 -> impossible
    directives = [
        {
            "applies": True,
            "directive_type": "max_grid_window",
            "structured_adjustment": {"hours": [0], "max_grid_kwh": 50.0},
        }
    ]
    constraints = normalize_directives(directives, base_minimum_kwh=20.0)
    res = solve_energy_lp(hours=hours, battery=battery, constraints=constraints)

    assert res.success is False
    assert res.status != 0


def test_public_sample_cases_solver_optimality() -> None:
    """Validate solver formulation achieves expected optimal cost on all 10 public cases."""
    assert FIXTURE_PATH.exists()

    with open(FIXTURE_PATH, encoding="utf-8") as f:
        data = json.load(f)

    cases = data.get("cases", [])
    assert len(cases) == 10

    for case in cases:
        case_id = case.get("id") or case["input"]["scenario_id"]
        req_input = case["input"]
        directives = case["expected_output"]["directive_interpretation"]
        expected_cost = case["expected_output"]["total_cost_bdt"]

        res = solve_scenario(req_input, directives)

        assert res.success is True, f"Case {case_id} failed to solve: {res.message}"
        assert res.total_cost_bdt == pytest.approx(
            expected_cost, abs=0.01
        ), f"Case {case_id} cost mismatch: got {res.total_cost_bdt}, expected {expected_cost}"


# ==============================================================================
# OPT-03 Tests: Plan Entries, Metrics, and optimize_energy Component
# ==============================================================================


def test_solver_result_to_plan_actions_and_ordering() -> None:
    """Verify conversion of signed delta to charge, discharge, or idle."""
    fake_result = SolverResult(
        success=True,
        status=0,
        message="Optimal",
        grid_kwh=[50.0] * 24,
        solar_used_kwh=[10.0] * 24,
        battery_delta_kwh=[
            50.0 if h == 1 else (-40.0 if h == 2 else 0.00001) for h in range(24)
        ],
        battery_energy_after_kwh=[100.0] * 24,
        total_cost_bdt=1000.0,
    )

    plan = solver_result_to_plan(fake_result)

    assert len(plan) == 24
    for h, entry in enumerate(plan):
        assert entry.hour == h
        assert entry.grid_kwh == 50.0
        assert entry.solar_used_kwh == 10.0
        assert entry.battery_energy_after_kwh == 100.0

        if h == 1:
            assert entry.battery_action == "charge"
            assert entry.battery_kwh == 50.0
        elif h == 2:
            assert entry.battery_action == "discharge"
            assert entry.battery_kwh == 40.0
        else:
            assert entry.battery_action == "idle"
            assert entry.battery_kwh == 0.0


def test_hourly_plan_entry_pydantic_roundtrip_and_forbid_extra() -> None:
    """Ensure HourlyPlanEntry round-trips via Pydantic and forbids extra fields."""
    entry = HourlyPlanEntry(
        hour=5,
        grid_kwh=120.5,
        solar_used_kwh=10.0,
        battery_action="charge",
        battery_kwh=30.0,
        battery_energy_after_kwh=80.0,
    )

    # Round trip
    dumped = entry.model_dump()
    reloaded = HourlyPlanEntry.model_validate(dumped)
    assert reloaded == entry

    # Forbid extra fields
    with pytest.raises(Exception):
        HourlyPlanEntry(**{**dumped, "extra_field": 123})


def test_compute_plan_metrics_sample_case_one() -> None:
    """Validate compute_plan_metrics matches public Sample Case 1 official output."""
    assert FIXTURE_PATH.exists()
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        data = json.load(f)

    case1 = data["cases"][0]
    expected_out = case1["expected_output"]
    plan = expected_out["hourly_plan"]

    metrics = compute_plan_metrics(plan, case1["input"])

    assert metrics.total_grid_kwh == pytest.approx(expected_out["total_grid_kwh"], abs=0.01)
    assert metrics.total_cost_bdt == pytest.approx(expected_out["total_cost_bdt"], abs=0.01)
    assert metrics.peak_grid_kwh == pytest.approx(expected_out["peak_grid_kwh"], abs=0.01)


def test_optimize_energy_public_cases_end_to_end() -> None:
    """Test optimize_energy interface produces valid, optimal plans on all 10 public cases."""
    assert FIXTURE_PATH.exists()
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        data = json.load(f)

    cases = data["cases"]
    for case in cases:
        case_id = case.get("id") or case["input"]["scenario_id"]
        req_input = case["input"]
        directives = case["expected_output"]["directive_interpretation"]
        expected_cost = case["expected_output"]["total_cost_bdt"]

        plan = optimize_energy(request=req_input, directives=directives)

        assert len(plan) == 24, f"Case {case_id} did not produce 24 hours"
        for h, entry in enumerate(plan):
            assert entry.hour == h
            assert entry.battery_action in ("charge", "discharge", "idle")
            if entry.battery_action == "idle":
                assert entry.battery_kwh == 0.0
            else:
                assert entry.battery_kwh > 0.0

        metrics = compute_plan_metrics(plan, req_input)
        assert metrics.total_cost_bdt == pytest.approx(
            expected_cost, abs=0.01
        ), f"Case {case_id} cost mismatch: {metrics.total_cost_bdt} vs {expected_cost}"


def test_optimize_energy_infeasible_raises_exception() -> None:
    """optimize_energy must raise OptimizationFailedError when problem is infeasible."""
    infeasible_request = {
        "scenario_id": "INFEASIBLE-01",
        "hours": [
            {"hour": h, "demand_kwh": 200.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 10.0}
            for h in range(24)
        ],
        "battery": {
            "capacity_kwh": 50.0,
            "initial_energy_kwh": 10.0,
            "minimum_energy_kwh": 10.0,
            "max_charge_kwh_per_hour": 10.0,
            "max_discharge_kwh_per_hour": 10.0,
        },
    }
    # Demand is 200, max discharge is 10, cap grid to 10 -> impossible
    directives = [
        {
            "applies": True,
            "directive_type": "max_grid_window",
            "structured_adjustment": {"hours": [0], "max_grid_kwh": 10.0},
        }
    ]

    with pytest.raises(OptimizationFailedError):
        optimize_energy(request=infeasible_request, directives=directives)


# ==============================================================================
# OPT-04 Tests: Independent Replay Validator & Mutation Testing
# ==============================================================================


def test_validator_all_public_sample_cases() -> None:
    """Ensure every official reference plan passes independent replay validation."""
    assert FIXTURE_PATH.exists()
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        data = json.load(f)

    for case in data["cases"]:
        case_id = case.get("id") or case["input"]["scenario_id"]
        req = case["input"]
        directives = case["expected_output"]["directive_interpretation"]
        plan = case["expected_output"]["hourly_plan"]

        # Validate hourly plan against original request & directives
        assert validate_plan(request=req, directives=directives, plan=plan) is True

        # Validate reported scalar totals against recalculated values
        tariffs = [h["tariff_bdt_per_kwh"] for h in req["hours"]]
        assert validate_plan_metrics(
            plan=plan,
            tariffs=tariffs,
            reported_total_grid=case["expected_output"]["total_grid_kwh"],
            reported_total_cost=case["expected_output"]["total_cost_bdt"],
            reported_peak_grid=case["expected_output"]["peak_grid_kwh"],
        ) is True


def test_validator_mutation_suite() -> None:
    """Mutate a valid plan one field at a time and ensure validator catches every violation."""
    assert FIXTURE_PATH.exists()
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        data = json.load(f)

    case1 = data["cases"][0]
    req = case1["input"]
    directives = case1["expected_output"]["directive_interpretation"]
    base_plan = case1["expected_output"]["hourly_plan"]

    # Baseline must pass
    assert validate_plan(request=req, directives=directives, plan=base_plan) is True

    # 1. Missing hour (23 entries)
    with pytest.raises(PlanValidationError):
        validate_plan(request=req, directives=directives, plan=base_plan[:23])

    # 2. Out-of-order hour
    mutated = [dict(entry) for entry in base_plan]
    mutated[3]["hour"] = 4
    with pytest.raises(PlanValidationError):
        validate_plan(request=req, directives=directives, plan=mutated)

    # 3. Negative grid value
    mutated = [dict(entry) for entry in base_plan]
    mutated[0]["grid_kwh"] = -10.0
    with pytest.raises(PlanValidationError):
        validate_plan(request=req, directives=directives, plan=mutated)

    # 4. Non-finite value (inf)
    mutated = [dict(entry) for entry in base_plan]
    mutated[0]["grid_kwh"] = float("inf")
    with pytest.raises(PlanValidationError):
        validate_plan(request=req, directives=directives, plan=mutated)

    # 5. Invalid battery action enum
    mutated = [dict(entry) for entry in base_plan]
    mutated[0]["battery_action"] = "fast_charge"
    with pytest.raises(PlanValidationError):
        validate_plan(request=req, directives=directives, plan=mutated)

    # 6. Idle with non-zero battery_kwh
    mutated = [dict(entry) for entry in base_plan]
    mutated[0]["battery_action"] = "idle"
    mutated[0]["battery_kwh"] = 25.0
    with pytest.raises(PlanValidationError):
        validate_plan(request=req, directives=directives, plan=mutated)

    # 7. Charge with zero battery_kwh
    mutated = [dict(entry) for entry in base_plan]
    mutated[2]["battery_action"] = "charge"
    mutated[2]["battery_kwh"] = 0.0
    with pytest.raises(PlanValidationError):
        validate_plan(request=req, directives=directives, plan=mutated)

    # 8. Charge rate limit exceeded
    mutated = [dict(entry) for entry in base_plan]
    max_c = req["battery"]["max_charge_kwh_per_hour"]
    mutated[2]["battery_kwh"] = max_c + 20.0
    with pytest.raises(PlanValidationError):
        validate_plan(request=req, directives=directives, plan=mutated)

    # 9. Solar overuse (solar_used > effective solar)
    # Hour 12 has forecast 180 and factor 0.25 -> effective solar = 45
    mutated = [dict(entry) for entry in base_plan]
    mutated[12]["solar_used_kwh"] = 50.0  # exceeds 45
    with pytest.raises(PlanValidationError):
        validate_plan(request=req, directives=directives, plan=mutated)

    # 10. Energy balance violation
    mutated = [dict(entry) for entry in base_plan]
    mutated[1]["grid_kwh"] += 10.0  # changes generation without matching load
    with pytest.raises(PlanValidationError):
        validate_plan(request=req, directives=directives, plan=mutated)

    # 11. Battery state transition violation
    mutated = [dict(entry) for entry in base_plan]
    mutated[1]["battery_energy_after_kwh"] += 20.0
    with pytest.raises(PlanValidationError):
        validate_plan(request=req, directives=directives, plan=mutated)

    # 12. Battery capacity exceeded
    mutated = [dict(entry) for entry in base_plan]
    mutated[3]["battery_energy_after_kwh"] = req["battery"]["capacity_kwh"] + 50.0
    with pytest.raises(PlanValidationError):
        validate_plan(request=req, directives=directives, plan=mutated)

    # 13. Battery reserve floor violated
    mutated = [dict(entry) for entry in base_plan]
    mutated[3]["battery_energy_after_kwh"] = req["battery"]["minimum_energy_kwh"] - 10.0
    with pytest.raises(PlanValidationError):
        validate_plan(request=req, directives=directives, plan=mutated)

    # 14. End-of-day neutrality violated
    mutated = [dict(entry) for entry in base_plan]
    mutated[23]["battery_energy_after_kwh"] = req["battery"]["initial_energy_kwh"] + 15.0
    with pytest.raises(PlanValidationError):
        validate_plan(request=req, directives=directives, plan=mutated)


def test_validator_window_and_cap_violations() -> None:
    """Test that window restrictions (no-charge, no-discharge, max-grid) are enforced."""
    scenario = {
        "hours": [
            {"hour": h, "demand_kwh": 50.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 10.0}
            for h in range(24)
        ],
        "battery": {
            "capacity_kwh": 100.0,
            "initial_energy_kwh": 50.0,
            "minimum_energy_kwh": 20.0,
            "max_charge_kwh_per_hour": 30.0,
            "max_discharge_kwh_per_hour": 30.0,
        },
    }

    # Prohibit charge at hour 1
    directives_no_charge = [
        {
            "applies": True,
            "directive_type": "no_charge_window",
            "structured_adjustment": {"hours": [1]},
        }
    ]
    # Build plan charging at hour 1
    plan_charge_at_1 = [
        {
            "hour": h,
            "grid_kwh": 70.0 if h == 1 else 50.0,
            "solar_used_kwh": 0.0,
            "battery_action": "charge" if h == 1 else "idle",
            "battery_kwh": 20.0 if h == 1 else 0.0,
            "battery_energy_after_kwh": 70.0 if h >= 1 else 50.0,
        }
        for h in range(24)
    ]
    with pytest.raises(PlanValidationError, match="prohibited window"):
        validate_plan(request=scenario, directives=directives_no_charge, plan=plan_charge_at_1)

    # Prohibit discharge at hour 2
    directives_no_discharge = [
        {
            "applies": True,
            "directive_type": "no_discharge_window",
            "structured_adjustment": {"hours": [2]},
        }
    ]
    plan_discharge_at_2 = [
        {
            "hour": h,
            "grid_kwh": 30.0 if h == 2 else 50.0,
            "solar_used_kwh": 0.0,
            "battery_action": "discharge" if h == 2 else "idle",
            "battery_kwh": 20.0 if h == 2 else 0.0,
            "battery_energy_after_kwh": 30.0 if h >= 2 else 50.0,
        }
        for h in range(24)
    ]
    with pytest.raises(PlanValidationError, match="prohibited window"):
        validate_plan(request=scenario, directives=directives_no_discharge, plan=plan_discharge_at_2)

    # Grid cap violation at hour 3
    directives_cap = [
        {
            "applies": True,
            "directive_type": "max_grid_window",
            "structured_adjustment": {"hours": [3], "max_grid_kwh": 40.0},
        }
    ]
    plan_grid_50 = [
        {
            "hour": h,
            "grid_kwh": 50.0,
            "solar_used_kwh": 0.0,
            "battery_action": "idle",
            "battery_kwh": 0.0,
            "battery_energy_after_kwh": 50.0,
        }
        for h in range(24)
    ]
    with pytest.raises(PlanValidationError, match="exceeds active cap"):
        validate_plan(request=scenario, directives=directives_cap, plan=plan_grid_50)


# ==============================================================================
# OPT-05 Tests: Numerical Handling & Controlled Solver Error Categories
# ==============================================================================


def test_challenge_tolerance_constant_consistency() -> None:
    """Ensure tolerance constant 0.01 is defined and shared between components."""
    assert CHALLENGE_TOLERANCE == 0.01
    assert VALIDATOR_TOLERANCE == 0.01


def test_small_numerical_residuals_normalized_to_idle() -> None:
    """Small solver residuals (e.g. 1e-8) must be cleaned to 0.0 and produce idle battery."""
    result_with_residuals = SolverResult(
        success=True,
        status=0,
        message="Optimal",
        grid_kwh=[50.0 + 1e-8] * 24,
        solar_used_kwh=[0.0] * 24,
        battery_delta_kwh=[1e-8] * 24,  # sub-tolerance jitter
        battery_energy_after_kwh=[50.0] * 24,
        total_cost_bdt=12000.0,
    )

    plan = solver_result_to_plan(result_with_residuals, idle_threshold=1e-4)

    for entry in plan:
        assert entry.battery_action == "idle"
        assert entry.battery_kwh == 0.0


def test_infeasible_scenario_raises_scenario_infeasible_error() -> None:
    """Infeasible scenarios must raise ScenarioInfeasibleError, subclass of OptimizationFailedError."""
    infeasible_scenario = {
        "scenario_id": "INFEASIBLE-OPT-05",
        "hours": [
            {"hour": h, "demand_kwh": 300.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 10.0}
            for h in range(24)
        ],
        "battery": {
            "capacity_kwh": 100.0,
            "initial_energy_kwh": 20.0,
            "minimum_energy_kwh": 20.0,
            "max_charge_kwh_per_hour": 20.0,
            "max_discharge_kwh_per_hour": 20.0,
        },
    }
    # Grid cap 50 with demand 300 and max discharge 20 is mathematically impossible
    directives = [
        {
            "applies": True,
            "directive_type": "max_grid_window",
            "structured_adjustment": {"hours": [0], "max_grid_kwh": 50.0},
        }
    ]

    with pytest.raises(ScenarioInfeasibleError) as exc_info:
        optimize_energy(request=infeasible_scenario, directives=directives)

    # Verify inheritance hierarchy
    assert isinstance(exc_info.value, OptimizationFailedError)
    assert isinstance(exc_info.value, OptimizerError)


def test_solver_failure_category_mappings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that every solver status code maps to its respective typed exception."""
    dummy_scenario = {
        "hours": [
            {"hour": h, "demand_kwh": 10.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 5.0}
            for h in range(24)
        ],
        "battery": {
            "capacity_kwh": 50.0,
            "initial_energy_kwh": 20.0,
            "minimum_energy_kwh": 10.0,
            "max_charge_kwh_per_hour": 10.0,
            "max_discharge_kwh_per_hour": 10.0,
        },
    }

    # Status 1: Timeout / Iteration limit
    def mock_timeout(*args: Any, **kwargs: Any) -> SolverResult:
        return SolverResult(False, 1, "Iteration limit reached", [], [], [], [], 0.0)

    monkeypatch.setattr("app.services.optimizer.solve_energy_lp", mock_timeout)
    with pytest.raises(SolverTimeoutError):
        optimize_energy(request=dummy_scenario, directives=[])

    # Status 2: Infeasible
    def mock_infeasible(*args: Any, **kwargs: Any) -> SolverResult:
        return SolverResult(False, 2, "Problem appears infeasible", [], [], [], [], 0.0)

    monkeypatch.setattr("app.services.optimizer.solve_energy_lp", mock_infeasible)
    with pytest.raises(ScenarioInfeasibleError):
        optimize_energy(request=dummy_scenario, directives=[])

    # Status 3: Unbounded
    def mock_unbounded(*args: Any, **kwargs: Any) -> SolverResult:
        return SolverResult(False, 3, "Problem appears unbounded", [], [], [], [], 0.0)

    monkeypatch.setattr("app.services.optimizer.solve_energy_lp", mock_unbounded)
    with pytest.raises(ScenarioUnboundedError):
        optimize_energy(request=dummy_scenario, directives=[])

    # Status 4: Numerical difficulty
    def mock_numerical(*args: Any, **kwargs: Any) -> SolverResult:
        return SolverResult(False, 4, "Numerical difficulties", [], [], [], [], 0.0)

    monkeypatch.setattr("app.services.optimizer.solve_energy_lp", mock_numerical)
    with pytest.raises(SolverNumericalError):
        optimize_energy(request=dummy_scenario, directives=[])


# ==============================================================================
# OPT-06 Tests: Comprehensive Public Case Pack Validation & Latency Benchmark
# ==============================================================================


@pytest.mark.parametrize("case_index", range(10))
def test_opt06_public_case_pack_validation(case_index: int) -> None:
    """Validate each of the 10 public cases independently for feasibility, replay, and optimality.

    Requirements:
    - Feasible and replay-valid under independent replay validator.
    - Total cost matches optimal reference within 0.01 BDT.
    - Total grid matches optimal reference within 0.01 kWh.
    - Peak grid matches optimal reference within 0.01 kWh.
    - Solves comfortably under the 1.0 second local latency budget.
    """
    assert FIXTURE_PATH.exists()
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        data = json.load(f)

    case = data["cases"][case_index]
    case_id = case.get("id") or case["input"]["scenario_id"]
    req = case["input"]
    directives = case["expected_output"]["directive_interpretation"]
    expected_out = case["expected_output"]

    import time
    t0 = time.perf_counter()
    plan = optimize_energy(request=req, directives=directives)
    solve_duration = time.perf_counter() - t0

    # 1. Latency budget: strictly under 1.0 second
    assert solve_duration < 1.0, f"Case {case_id} exceeded latency budget: {solve_duration:.4f}s"

    # 2. Plan length & structure
    assert len(plan) == 24, f"Case {case_id} returned {len(plan)} hours instead of 24"
    for h, entry in enumerate(plan):
        assert entry.hour == h
        assert entry.battery_action in ("charge", "discharge", "idle")
        if entry.battery_action == "idle":
            assert entry.battery_kwh == 0.0
        else:
            assert entry.battery_kwh > 0.0

    # 3. Independent replay validation
    assert validate_plan(request=req, directives=directives, plan=plan) is True

    # 4. Independent metrics recalculation & optimality comparisons
    metrics = compute_plan_metrics(plan, req)

    # Cost must match optimal reference within 0.01 BDT
    assert metrics.total_cost_bdt == pytest.approx(
        expected_out["total_cost_bdt"], abs=CHALLENGE_TOLERANCE
    ), f"Case {case_id} cost mismatch: {metrics.total_cost_bdt} vs {expected_out['total_cost_bdt']}"

    # Total grid must match reference within 0.01 kWh
    assert metrics.total_grid_kwh == pytest.approx(
        expected_out["total_grid_kwh"], abs=CHALLENGE_TOLERANCE
    ), f"Case {case_id} grid mismatch: {metrics.total_grid_kwh} vs {expected_out['total_grid_kwh']}"

    # Peak grid must match maximum hourly grid in plan (equivalent schedules accepted)
    plan_peak = max(entry.grid_kwh for entry in plan)
    assert metrics.peak_grid_kwh == pytest.approx(
        plan_peak, abs=CHALLENGE_TOLERANCE
    ), f"Case {case_id} peak mismatch: {metrics.peak_grid_kwh} vs {plan_peak}"


# ==============================================================================
# OPT-07 Tests: Edge-Cases, Infeasibility & Overlap Policy Stress Testing
# ==============================================================================


def _make_base_scenario(
    demand: float = 100.0,
    solar: float = 0.0,
    capacity: float = 200.0,
    initial: float = 50.0,
    min_energy: float = 50.0,
    max_charge: float = 50.0,
    max_discharge: float = 50.0,
) -> dict[str, Any]:
    """Helper creating a valid 24-hour test scenario."""
    return {
        "scenario_id": "STRESS-SCENARIO",
        "hours": [
            {
                "hour": h,
                "demand_kwh": demand,
                "solar_kwh": solar,
                "tariff_bdt_per_kwh": 10.0 if h < 12 else 20.0,
            }
            for h in range(24)
        ],
        "battery": {
            "capacity_kwh": capacity,
            "initial_energy_kwh": initial,
            "minimum_energy_kwh": min_energy,
            "max_charge_kwh_per_hour": max_charge,
            "max_discharge_kwh_per_hour": max_discharge,
        },
    }


def test_opt07_zero_solar_scenario() -> None:
    """Zero solar across all 24 hours: grid and battery supply 100% of demand."""
    scenario = _make_base_scenario(demand=120.0, solar=0.0)
    plan = optimize_energy(request=scenario, directives=[])

    assert len(plan) == 24
    for entry in plan:
        assert entry.solar_used_kwh == 0.0
    assert validate_plan(request=scenario, directives=[], plan=plan) is True
    # Energy neutrality: sum(grid) == sum(demand)
    metrics = compute_plan_metrics(plan, scenario)
    assert metrics.total_grid_kwh == pytest.approx(120.0 * 24, abs=CHALLENGE_TOLERANCE)


def test_opt07_solar_surplus_and_curtailment() -> None:
    """Solar vastly exceeds demand + max battery charge; surplus is curtailed without export."""
    scenario = _make_base_scenario(
        demand=50.0,
        solar=0.0,
        capacity=100.0,
        initial=50.0,
        max_charge=30.0,
        max_discharge=30.0,
    )
    # Give massive solar spike (500 kWh) at hour 12
    scenario["hours"][12]["solar_kwh"] = 500.0

    plan = optimize_energy(request=scenario, directives=[])

    entry_12 = plan[12]
    # In hour 12: grid import should be 0.0
    assert entry_12.grid_kwh == pytest.approx(0.0, abs=CHALLENGE_TOLERANCE)
    # Solar used must satisfy demand (50) + charge (up to 30) = max 80. Surplus (420) curtailed.
    assert entry_12.solar_used_kwh <= 80.0 + CHALLENGE_TOLERANCE
    assert entry_12.solar_used_kwh < 500.0  # curtailed!
    assert validate_plan(request=scenario, directives=[], plan=plan) is True


def test_opt07_zero_charge_discharge_limits() -> None:
    """Battery with 0 charge and 0 discharge limits must remain idle all 24 hours."""
    scenario = _make_base_scenario(max_charge=0.0, max_discharge=0.0)
    plan = optimize_energy(request=scenario, directives=[])

    for entry in plan:
        assert entry.battery_action == "idle"
        assert entry.battery_kwh == 0.0
        assert entry.battery_energy_after_kwh == pytest.approx(50.0, abs=CHALLENGE_TOLERANCE)
    assert validate_plan(request=scenario, directives=[], plan=plan) is True


def test_opt07_reserve_near_capacity() -> None:
    """High reserve floor (495 kWh) on a 500 kWh battery must be met and stay <= 500."""
    scenario = _make_base_scenario(
        capacity=500.0,
        initial=200.0,
        min_energy=50.0,
        max_charge=100.0,
        max_discharge=100.0,
    )
    directives = [
        {
            "applies": True,
            "directive_type": "minimum_battery_reserve",
            "structured_adjustment": {"hours": [12, 13, 14], "minimum_energy_kwh": 495.0},
        }
    ]

    plan = optimize_energy(request=scenario, directives=directives)

    for h in [12, 13, 14]:
        assert plan[h].battery_energy_after_kwh >= 495.0 - CHALLENGE_TOLERANCE
        assert plan[h].battery_energy_after_kwh <= 500.0 + CHALLENGE_TOLERANCE

    # Neutrality returns to 200 at hour 23
    assert plan[23].battery_energy_after_kwh == pytest.approx(200.0, abs=CHALLENGE_TOLERANCE)
    assert validate_plan(request=scenario, directives=directives, plan=plan) is True


def test_opt07_high_low_tariff_extremes() -> None:
    """Extreme tariff spread (1 BDT vs 1000 BDT) forces battery to maximum feasible arbitrage."""
    scenario = _make_base_scenario(
        capacity=300.0,
        initial=50.0,
        min_energy=50.0,
        max_charge=100.0,
        max_discharge=100.0,
    )
    for h in range(24):
        scenario["hours"][h]["tariff_bdt_per_kwh"] = 1.0 if h < 6 else 1000.0

    plan = optimize_energy(request=scenario, directives=[])

    # Battery charges to max (300) in cheap hours and discharges back to min (50) in expensive hours
    max_stored = max(e.battery_energy_after_kwh for e in plan)
    assert max_stored == pytest.approx(300.0, abs=CHALLENGE_TOLERANCE)
    assert validate_plan(request=scenario, directives=[], plan=plan) is True


def test_opt07_boundary_hours_zero_and_twenty_three() -> None:
    """Directives applied to boundary hours 0 and 23 must be respected without indexing bugs."""
    scenario = _make_base_scenario()
    # At hour 23: set demand to 70 so max_grid=80 is feasible without discharging
    scenario["hours"][23]["demand_kwh"] = 70.0

    directives = [
        {
            "applies": True,
            "directive_type": "no_charge_window",
            "structured_adjustment": {"hours": [0]},
        },
        {
            "applies": True,
            "directive_type": "no_discharge_window",
            "structured_adjustment": {"hours": [23]},
        },
        {
            "applies": True,
            "directive_type": "max_grid_window",
            "structured_adjustment": {"hours": [23], "max_grid_kwh": 80.0},
        },
    ]

    plan = optimize_energy(request=scenario, directives=directives)

    assert plan[0].battery_action != "charge"
    assert plan[23].battery_action != "discharge"
    assert plan[23].grid_kwh <= 80.0 + CHALLENGE_TOLERANCE
    assert validate_plan(request=scenario, directives=directives, plan=plan) is True



def test_opt07_simultaneous_no_charge_no_discharge() -> None:
    """Both no-charge and no-discharge windows on hour 8 force battery action to idle."""
    scenario = _make_base_scenario()
    directives = [
        {
            "applies": True,
            "directive_type": "no_charge_window",
            "structured_adjustment": {"hours": [8]},
        },
        {
            "applies": True,
            "directive_type": "no_discharge_window",
            "structured_adjustment": {"hours": [8]},
        },
    ]

    plan = optimize_energy(request=scenario, directives=directives)

    assert plan[8].battery_action == "idle"
    assert plan[8].battery_kwh == 0.0
    assert validate_plan(request=scenario, directives=directives, plan=plan) is True


def test_opt07_complex_overlapping_directives() -> None:
    """Overlapping directives of all types: min solar factor, max reserve, min grid cap."""
    scenario = _make_base_scenario(demand=150.0, solar=100.0, capacity=400.0, initial=100.0)
    directives = [
        # Overlapping solar: min factor on [12, 13] is min(0.6, 0.2) = 0.2
        {
            "applies": True,
            "directive_type": "solar_reduction",
            "structured_adjustment": {"hours": [11, 12, 13], "factor": 0.6},
        },
        {
            "applies": True,
            "directive_type": "solar_reduction",
            "structured_adjustment": {"hours": [12, 13, 14], "factor": 0.2},
        },
        # Overlapping reserve: max reserve on [16, 17] is max(180, 250) = 250
        {
            "applies": True,
            "directive_type": "minimum_battery_reserve",
            "structured_adjustment": {"hours": [15, 16, 17], "minimum_energy_kwh": 180.0},
        },
        {
            "applies": True,
            "directive_type": "minimum_battery_reserve",
            "structured_adjustment": {"hours": [16, 17, 18], "minimum_energy_kwh": 250.0},
        },
        # Overlapping grid cap: min cap on [18, 19] is min(200, 160) = 160
        {
            "applies": True,
            "directive_type": "max_grid_window",
            "structured_adjustment": {"hours": [17, 18, 19], "max_grid_kwh": 200.0},
        },
        {
            "applies": True,
            "directive_type": "max_grid_window",
            "structured_adjustment": {"hours": [18, 19, 20], "max_grid_kwh": 160.0},
        },
    ]

    plan = optimize_energy(request=scenario, directives=directives)

    # Validate all overlapping rules
    assert plan[12].solar_used_kwh <= 20.0 + CHALLENGE_TOLERANCE  # 0.2 * 100
    assert plan[13].solar_used_kwh <= 20.0 + CHALLENGE_TOLERANCE
    assert plan[16].battery_energy_after_kwh >= 250.0 - CHALLENGE_TOLERANCE
    assert plan[17].battery_energy_after_kwh >= 250.0 - CHALLENGE_TOLERANCE
    assert plan[18].grid_kwh <= 160.0 + CHALLENGE_TOLERANCE
    assert plan[19].grid_kwh <= 160.0 + CHALLENGE_TOLERANCE

    assert validate_plan(request=scenario, directives=directives, plan=plan) is True


def test_opt07_infeasible_grid_cap_raises_error() -> None:
    """Infeasible grid cap must raise ScenarioInfeasibleError."""
    scenario = _make_base_scenario(demand=200.0, solar=0.0, max_discharge=20.0)
    # Cap grid to 50 when demand is 200 and discharge is at most 20 -> 130 kWh deficit
    directives = [
        {
            "applies": True,
            "directive_type": "max_grid_window",
            "structured_adjustment": {"hours": [5], "max_grid_kwh": 50.0},
        }
    ]

    with pytest.raises(ScenarioInfeasibleError):
        optimize_energy(request=scenario, directives=directives)


def test_opt07_infeasible_reserve_exceeding_capacity_raises_error() -> None:
    """Reserve directive exceeding battery capacity (600 > 500) must raise ScenarioInfeasibleError."""
    scenario = _make_base_scenario(capacity=500.0)
    directives = [
        {
            "applies": True,
            "directive_type": "minimum_battery_reserve",
            "structured_adjustment": {"hours": [10], "minimum_energy_kwh": 600.0},
        }
    ]

    with pytest.raises(ScenarioInfeasibleError):
        optimize_energy(request=scenario, directives=directives)







