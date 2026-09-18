"""Deterministic energy schedule optimization and directive normalization.

This module implements:
- OPT-01: Normalizing validated directives into 24-hour mathematical constraints
  with a conservative overlap policy.
- OPT-02: 24-hour LP formulation minimizing total grid cost using scipy HiGHS.
- OPT-03: Conversion of solver output to validated HourlyPlanEntry records,
  metrics computation, and the public optimize_energy component interface.
- OPT-05: Numerical handling (residual cleanup, NaN/inf guards, challenge tolerance 0.01)
  and controlled typed solver error categories.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Literal, Sequence

import numpy as np
from pydantic import BaseModel, ConfigDict, Field
from scipy.optimize import linprog


# Global challenge tolerance constant (0.01 kWh / BDT)
CHALLENGE_TOLERANCE: float = 0.01


# Import HourlyPlanEntry from shared contracts if available, otherwise define local model
try:
    from app.contracts import HourlyPlanEntry  # type: ignore[attr-defined]
except (ImportError, AttributeError):
    class HourlyPlanEntry(BaseModel):  # type: ignore[no-redef]
        """Hourly plan entry matching OpenAPI schema."""
        model_config = ConfigDict(extra="forbid")

        hour: int = Field(..., ge=0, le=23)
        grid_kwh: float = Field(..., ge=0.0)
        solar_used_kwh: float = Field(..., ge=0.0)
        battery_action: Literal["charge", "discharge", "idle"]
        battery_kwh: float = Field(..., ge=0.0)
        battery_energy_after_kwh: float = Field(..., ge=0.0)


# ==============================================================================
# Controlled Solver Exceptions Hierarchy (OPT-05)
# ==============================================================================

class OptimizerError(Exception):
    """Base exception for all optimization errors."""
    pass


class OptimizationFailedError(OptimizerError):
    """General failure when optimization cannot produce a valid schedule."""
    pass


class ScenarioInfeasibleError(OptimizationFailedError):
    """Raised when the scenario is mathematically infeasible under constraints (status 2)."""
    pass


class ScenarioUnboundedError(OptimizationFailedError):
    """Raised when the optimization problem is unbounded (status 3)."""
    pass


class SolverTimeoutError(OptimizationFailedError):
    """Raised when the solver reaches iteration or time limits (status 1)."""
    pass


class SolverNumericalError(OptimizationFailedError):
    """Raised when solver encounters numerical difficulties, NaN, or non-finite values (status 4)."""
    pass


# ==============================================================================
# Data Structures
# ==============================================================================

@dataclass(frozen=True)
class HourlyConstraints:
    """Normalized 24-hour mathematical constraints for energy optimization.

    Attributes:
        solar_factors: 24 multipliers in [0.0, 1.0] applied to forecasted solar.
        reserve_floors: 24 minimum battery energy levels (kWh) after each hour.
        allow_charge: 24 booleans indicating if battery charging is permitted.
        allow_discharge: 24 booleans indicating if battery discharging is permitted.
        grid_caps: 24 optional upper bounds on grid import (kWh), None if unconstrained.
    """

    solar_factors: list[float]
    reserve_floors: list[float]
    allow_charge: list[bool]
    allow_discharge: list[bool]
    grid_caps: list[float | None]

    def is_idle_forced(self, hour: int) -> bool:
        """Return True if both charging and discharging are disallowed in this hour."""
        return not self.allow_charge[hour] and not self.allow_discharge[hour]


@dataclass(frozen=True)
class SolverResult:
    """Raw linear programming solver result for 24-hour energy optimization.

    Attributes:
        success: True if the solver found an optimal feasible solution.
        status: Solver termination status code (0 for optimal in HiGHS).
        message: Solver status explanation message.
        grid_kwh: List of 24 grid import values (kWh).
        solar_used_kwh: List of 24 solar used values (kWh).
        battery_delta_kwh: List of 24 signed battery changes (kWh).
        battery_energy_after_kwh: List of 24 battery state values (kWh).
        total_cost_bdt: Total grid cost in BDT.
    """

    success: bool
    status: int
    message: str
    grid_kwh: list[float]
    solar_used_kwh: list[float]
    battery_delta_kwh: list[float]
    battery_energy_after_kwh: list[float]
    total_cost_bdt: float


@dataclass(frozen=True)
class PlanMetrics:
    """Aggregated plan metrics calculated from hourly plan entries."""

    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float


def _get_field(obj: Any, name: str, default: Any = None) -> Any:
    """Extract attribute or dict key safely to support Pydantic models, dataclasses, and dicts."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


# ==============================================================================
# OPT-01: Directive Normalization & Overlap Resolution
# ==============================================================================

def normalize_directives(
    directives: Sequence[Any] | None,
    *,
    base_minimum_kwh: float = 0.0,
) -> HourlyConstraints:
    """Convert validated directive objects into 24-hour hourly constraints.

    Overlap Policy (Conservative / Most Restrictive):
    - Overlapping solar reductions: use the minimum remaining solar factor for that hour.
    - Overlapping minimum reserves: use the maximum required reserve for that hour.
    - Overlapping grid caps: use the minimum cap for that hour.
    - No-charge and no-discharge windows: union of prohibited hours.
    - If both no-charge and no-discharge apply in the same hour, battery action must be idle.
    - Non-applicable directives (applies=False) and no_op directives are ignored.

    Args:
        directives: Sequence of directive objects or dictionaries (or None).
        base_minimum_kwh: Default baseline minimum battery reserve (kWh).

    Returns:
        HourlyConstraints with exactly 24 elements per constraint list.
    """
    solar_factors = [1.0] * 24
    reserve_floors = [float(base_minimum_kwh)] * 24
    allow_charge = [True] * 24
    allow_discharge = [True] * 24
    grid_caps: list[float | None] = [None] * 24

    for directive in (directives or []):
        applies = _get_field(directive, "applies", True)
        if not applies:
            continue

        directive_type = _get_field(directive, "directive_type")
        if hasattr(directive_type, "value"):
            directive_type = directive_type.value

        if directive_type == "no_op" or not directive_type:
            continue

        adjustment = _get_field(directive, "structured_adjustment")
        if adjustment is None:
            continue

        hours = _get_field(adjustment, "hours", [])

        if directive_type == "solar_reduction":
            factor = float(_get_field(adjustment, "factor", 1.0))
            for h in hours:
                if 0 <= h < 24:
                    solar_factors[h] = min(solar_factors[h], factor)

        elif directive_type == "minimum_battery_reserve":
            min_kwh = float(_get_field(adjustment, "minimum_energy_kwh", 0.0))
            for h in hours:
                if 0 <= h < 24:
                    reserve_floors[h] = max(reserve_floors[h], min_kwh)

        elif directive_type == "no_charge_window":
            for h in hours:
                if 0 <= h < 24:
                    allow_charge[h] = False

        elif directive_type == "no_discharge_window":
            for h in hours:
                if 0 <= h < 24:
                    allow_discharge[h] = False

        elif directive_type == "max_grid_window":
            max_grid = float(_get_field(adjustment, "max_grid_kwh", 0.0))
            for h in hours:
                if 0 <= h < 24:
                    if grid_caps[h] is None:
                        grid_caps[h] = max_grid
                    else:
                        grid_caps[h] = min(grid_caps[h], max_grid)

    return HourlyConstraints(
        solar_factors=solar_factors,
        reserve_floors=reserve_floors,
        allow_charge=allow_charge,
        allow_discharge=allow_discharge,
        grid_caps=grid_caps,
    )


# Alias matching Person 3 specification terminology
build_hourly_constraints = normalize_directives


def normalize_request_directives(
    request: Any,
    directives: Sequence[Any] | None,
) -> HourlyConstraints:
    """Convenience helper extracting baseline constraints from an OptimizeEnergyRequest."""
    battery = _get_field(request, "battery")
    base_min = float(_get_field(battery, "minimum_energy_kwh", 0.0)) if battery else 0.0
    return normalize_directives(directives, base_minimum_kwh=base_min)


# ==============================================================================
# OPT-02 & OPT-05: Linear Programming Solver Formulation & Residual Handling
# ==============================================================================

def solve_energy_lp(
    *,
    hours: Sequence[Any],
    battery: Any,
    constraints: HourlyConstraints,
) -> SolverResult:
    """Solve the 24-hour energy scheduling LP to minimize total grid cost.

    Formulation:
      Variables (96 total, indexed 0..95):
        - grid[h]: 0 <= grid[h] <= grid_caps[h]                    (indices 0..23)
        - solar_used[h]: 0 <= solar_used[h] <= effective_solar[h]  (indices 24..47)
        - delta[h]: -max_discharge <= delta[h] <= max_charge      (indices 48..71)
        - energy_after[h]: reserve_floors[h] <= E[h] <= capacity  (indices 72..95)

      Objective:
        Minimize sum_{h=0..23} (grid[h] * tariff[h])

      Equality Constraints (49 total):
        - 24 Hourly Energy Balance:
            grid[h] + solar_used[h] - delta[h] = demand[h]
        - 24 Battery Transitions:
            h=0: E[0] - delta[0] = initial_energy_kwh
            h>0: E[h] - E[h-1] - delta[h] = 0
        - 1 End-of-Day Neutrality:
            E[23] = initial_energy_kwh
    """
    T = 24
    if len(hours) != T:
        raise ValueError(f"Expected exactly 24 hours, got {len(hours)}")

    # Extract battery parameters
    capacity_kwh = float(_get_field(battery, "capacity_kwh"))
    initial_energy_kwh = float(_get_field(battery, "initial_energy_kwh"))
    max_charge = float(_get_field(battery, "max_charge_kwh_per_hour"))
    max_discharge = float(_get_field(battery, "max_discharge_kwh_per_hour"))

    # 1. Objective: c vector (length 96)
    c = np.zeros(4 * T, dtype=float)
    for h in range(T):
        c[h] = float(_get_field(hours[h], "tariff_bdt_per_kwh", 0.0))

    # 2. Equality constraints: A_eq (49 x 96), b_eq (49)
    num_eq = 2 * T + 1  # 24 balance + 24 transitions + 1 neutrality = 49
    A_eq = np.zeros((num_eq, 4 * T), dtype=float)
    b_eq = np.zeros(num_eq, dtype=float)

    # 2a. Hourly energy balance: grid[h] + solar_used[h] - delta[h] = demand[h]
    for h in range(T):
        A_eq[h, h] = 1.0           # grid[h]
        A_eq[h, T + h] = 1.0       # solar_used[h]
        A_eq[h, 2 * T + h] = -1.0  # -delta[h]
        b_eq[h] = float(_get_field(hours[h], "demand_kwh", 0.0))

    # 2b. Battery transition constraints
    # h = 0: E[0] - delta[0] = initial_energy_kwh
    A_eq[T, 3 * T + 0] = 1.0       # E[0]
    A_eq[T, 2 * T + 0] = -1.0      # -delta[0]
    b_eq[T] = initial_energy_kwh

    # h = 1..23: E[h] - E[h-1] - delta[h] = 0
    for h in range(1, T):
        row = T + h
        A_eq[row, 3 * T + h] = 1.0         # E[h]
        A_eq[row, 3 * T + (h - 1)] = -1.0  # -E[h-1]
        A_eq[row, 2 * T + h] = -1.0        # -delta[h]
        b_eq[row] = 0.0

    # 2c. End-of-day neutrality: E[23] = initial_energy_kwh
    row_neutral = 2 * T
    A_eq[row_neutral, 3 * T + (T - 1)] = 1.0
    b_eq[row_neutral] = initial_energy_kwh

    # 3. Variable bounds (length 96)
    bounds: list[tuple[float | None, float | None]] = []

    # 3a. grid[h] bounds: 0 <= grid[h] <= grid_caps[h]
    for h in range(T):
        cap = constraints.grid_caps[h]
        ub = float(cap) if cap is not None else None
        bounds.append((0.0, ub))

    # 3b. solar_used[h] bounds: 0 <= solar_used[h] <= effective_solar[h]
    for h in range(T):
        forecast = float(_get_field(hours[h], "solar_kwh", 0.0))
        effective_solar = max(0.0, constraints.solar_factors[h] * forecast)
        bounds.append((0.0, effective_solar))

    # 3c. delta[h] bounds: -max_discharge <= delta[h] <= max_charge
    for h in range(T):
        lb = -max_discharge
        ub = max_charge
        if not constraints.allow_charge[h]:
            ub = min(ub, 0.0)
        if not constraints.allow_discharge[h]:
            lb = max(lb, 0.0)
        bounds.append((lb, ub))

    # 3d. energy_after[h] bounds: reserve_floors[h] <= E[h] <= capacity_kwh
    for h in range(T):
        floor = constraints.reserve_floors[h]
        bounds.append((floor, capacity_kwh))

    # 4. Solve via HiGHS
    res = linprog(c=c, A_eq=A_eq, b_eq=b_eq, bounds=bounds, method="highs")

    if not res.success:
        return SolverResult(
            success=False,
            status=int(res.status),
            message=str(res.message),
            grid_kwh=[],
            solar_used_kwh=[],
            battery_delta_kwh=[],
            battery_energy_after_kwh=[],
            total_cost_bdt=0.0,
        )

    x = res.x

    # OPT-05: Reject NaN and infinite solver results
    if x is None or np.isnan(x).any() or np.isinf(x).any():
        raise SolverNumericalError("Solver returned NaN or non-finite values")

    # OPT-05: Reject values materially below zero (beyond challenge tolerance)
    for i, val in enumerate(x):
        if i < 2 * T and val < -CHALLENGE_TOLERANCE:
            raise SolverNumericalError(
                f"Solver returned materially negative quantity at variable {i}: {val}"
            )

    # OPT-05: Clean small numerical residuals near zero (|val| <= 1e-7)
    grid_vals = [max(0.0, float(x[h])) if abs(x[h]) > 1e-7 else 0.0 for h in range(T)]
    solar_vals = [max(0.0, float(x[T + h])) if abs(x[T + h]) > 1e-7 else 0.0 for h in range(T)]
    delta_vals = [float(x[2 * T + h]) if abs(x[2 * T + h]) > 1e-7 else 0.0 for h in range(T)]
    energy_vals = [float(x[3 * T + h]) for h in range(T)]

    return SolverResult(
        success=True,
        status=int(res.status),
        message=str(res.message),
        grid_kwh=grid_vals,
        solar_used_kwh=solar_vals,
        battery_delta_kwh=delta_vals,
        battery_energy_after_kwh=energy_vals,
        total_cost_bdt=float(res.fun),
    )


def solve_scenario(
    request: Any,
    directives: Sequence[Any] | None = None,
) -> SolverResult:
    """Convenience entry point to solve an entire scenario from request and directives."""
    hours = _get_field(request, "hours")
    battery = _get_field(request, "battery")
    constraints = normalize_request_directives(request, directives)
    return solve_energy_lp(hours=hours, battery=battery, constraints=constraints)


# ==============================================================================
# OPT-03: Plan Entry Formatting and Metrics Recalculation
# ==============================================================================

def solver_result_to_plan(
    result: SolverResult,
    *,
    precision: int = 4,
    idle_threshold: float = 1e-4,
) -> list[HourlyPlanEntry]:
    """Convert raw SolverResult into 24 validated HourlyPlanEntry objects.

    Rules:
    - Delta > +idle_threshold  -> action='charge',    battery_kwh = round(delta)
    - Delta < -idle_threshold  -> action='discharge', battery_kwh = round(-delta)
    - Otherwise                -> action='idle',     battery_kwh = 0.0
    - Non-negative numbers are clamped to 0.0 and rounded to specified precision.
    - Exactly 24 ordered entries (hours 0..23) are returned.

    Args:
        result: Feasible SolverResult from solve_energy_lp.
        precision: Decimal places for serialized numbers.
        idle_threshold: Magnitude below which battery is considered idle.

    Returns:
        List of 24 HourlyPlanEntry objects.
    """
    if not result.success:
        raise OptimizationFailedError(f"Cannot build plan from failed solve: {result.message}")

    plan: list[HourlyPlanEntry] = []
    for h in range(24):
        delta = result.battery_delta_kwh[h]

        if delta > idle_threshold:
            action: Literal["charge", "discharge", "idle"] = "charge"
            battery_kwh = round(delta, precision)
        elif delta < -idle_threshold:
            action = "discharge"
            battery_kwh = round(-delta, precision)
        else:
            action = "idle"
            battery_kwh = 0.0

        entry = HourlyPlanEntry(
            hour=h,
            grid_kwh=round(result.grid_kwh[h], precision),
            solar_used_kwh=round(result.solar_used_kwh[h], precision),
            battery_action=action,
            battery_kwh=battery_kwh,
            battery_energy_after_kwh=round(result.battery_energy_after_kwh[h], precision),
        )
        plan.append(entry)

    return plan


def compute_plan_metrics(
    plan: Sequence[Any],
    tariffs_or_request: Any = None,
    *,
    precision: int = 4,
) -> PlanMetrics:
    """Calculate total_grid_kwh, total_cost_bdt, and peak_grid_kwh from plan.

    Args:
        plan: 24-hour sequence of HourlyPlanEntry objects or dicts.
        tariffs_or_request: Optional sequence of tariffs (length 24) or request object.
        precision: Decimal places for rounding totals.

    Returns:
        PlanMetrics with total_grid_kwh, total_cost_bdt, peak_grid_kwh.
    """
    if len(plan) != 24:
        raise ValueError(f"Expected 24 plan entries, got {len(plan)}")

    grid_values = [float(_get_field(entry, "grid_kwh", 0.0)) for entry in plan]
    total_grid = round(sum(grid_values), precision)
    peak_grid = round(max(grid_values), precision) if grid_values else 0.0

    # Extract tariffs if supplied
    tariffs: list[float] = []
    if tariffs_or_request is not None:
        hours = _get_field(tariffs_or_request, "hours")
        if hours is not None:
            tariffs = [float(_get_field(h, "tariff_bdt_per_kwh", 0.0)) for h in hours]
        elif isinstance(tariffs_or_request, (list, tuple)):
            tariffs = [float(_get_field(t, "tariff_bdt_per_kwh", t)) for t in tariffs_or_request]

    if len(tariffs) == 24:
        total_cost = round(sum(g * t for g, t in zip(grid_values, tariffs)), precision)
    else:
        total_cost = 0.0

    return PlanMetrics(
        total_grid_kwh=total_grid,
        total_cost_bdt=total_cost,
        peak_grid_kwh=peak_grid,
    )


def optimize_energy(
    *,
    request: Any,
    directives: Sequence[Any],
) -> list[HourlyPlanEntry]:
    """Shared component interface for energy optimization (Person 3 mission).

    Solves the 24-hour cost-minimizing energy schedule, maps solver failures
    to controlled typed exceptions, and replays the final rounded schedule
    through the independent validator before returning.

    Args:
        request: Validated OptimizeEnergyRequest (or equivalent dict/object).
        directives: Validated list of DirectiveInterpretation objects.

    Returns:
        List of 24 ordered HourlyPlanEntry objects.

    Raises:
        ScenarioInfeasibleError: If the scenario is mathematically infeasible.
        ScenarioUnboundedError: If the optimization problem is unbounded.
        SolverTimeoutError: If the solver exceeded time/iteration limits.
        SolverNumericalError: If solver produced non-finite or invalid numbers.
        OptimizationFailedError: If the plan failed independent replay validation.
    """
    hours = _get_field(request, "hours")
    battery = _get_field(request, "battery")

    constraints = normalize_request_directives(request, directives)
    result = solve_energy_lp(hours=hours, battery=battery, constraints=constraints)

    # OPT-05: Map solver status to typed failure categories
    if not result.success:
        if result.status == 1:
            raise SolverTimeoutError(
                f"Optimization solver timed out or reached iteration limit: {result.message}"
            )
        elif result.status == 2:
            raise ScenarioInfeasibleError(
                f"Scenario is mathematically infeasible under constraints: {result.message}"
            )
        elif result.status == 3:
            raise ScenarioUnboundedError(
                f"Optimization formulation is unbounded: {result.message}"
            )
        elif result.status == 4:
            raise SolverNumericalError(
                f"Optimization solver encountered numerical difficulties: {result.message}"
            )
        else:
            raise OptimizationFailedError(
                f"Optimization failed: {result.message} (status={result.status})"
            )

    plan = solver_result_to_plan(result)

    # Independent replay validation before returning (Milestone 3 / OPT-04 / OPT-05)
    from app.services.plan_validator import PlanValidationError, validate_plan

    try:
        validate_plan(
            request=request,
            directives=directives,
            plan=plan,
            tolerance=CHALLENGE_TOLERANCE,
        )
    except PlanValidationError as err:
        raise OptimizationFailedError(f"Generated plan failed replay validation: {err}") from err

    return plan
