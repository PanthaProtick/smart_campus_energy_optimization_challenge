"""Independent replay validator for 24-hour campus energy schedules (OPT-04).

This module independently verifies that candidate plans strictly adhere to:
- 24 ordered hours (0..23) with non-negative, finite numbers.
- Valid action/magnitude pairs (idle has magnitude 0; charge/discharge respect limits).
- Hourly energy balance: grid + solar_used + discharge == demand + charge (within 0.01 kWh).
- Solar usage not exceeding effective solar after reductions.
- Battery state transitions: E_after[h] == E_before[h] + charge[h] - discharge[h].
- Storage limits: reserve_floor[h] <= E_after[h] <= capacity_kwh.
- Directive windows: no charging in no_charge_window, no discharging in no_discharge_window.
- Grid caps: grid[h] <= max_grid_kwh.
- End-of-day neutrality: E_after[23] == initial_energy_kwh (within 0.01 kWh).
- Optional recalculation of total grid, total cost, and peak grid within 0.01.

Crucially, this module does NOT reuse optimizer constraint-building logic,
preventing common-mode errors.
"""

from __future__ import annotations

import math
from typing import Any, Sequence


CHALLENGE_TOLERANCE: float = 0.01


class PlanValidationError(Exception):
    """Raised when a plan violates physical, contract, or directive constraints."""
    pass


def _get_field(obj: Any, name: str, default: Any = None) -> Any:
    """Extract attribute or dictionary key safely."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def validate_plan(
    *,
    request: Any,
    directives: Sequence[Any] | None,
    plan: Sequence[Any],
    tolerance: float = CHALLENGE_TOLERANCE,
) -> bool:
    """Replay and validate a 24-hour schedule against request and directives.

    Args:
        request: Original OptimizeEnergyRequest or dictionary.
        directives: Sequence of DirectiveInterpretation objects or dictionaries.
        plan: 24-element sequence of HourlyPlanEntry objects or dictionaries.
        tolerance: Numerical tolerance for energy and cost (default 0.01).

    Returns:
        True if the plan passes all checks.

    Raises:
        PlanValidationError: On any constraint or structural violation.
    """
    if len(plan) != 24:
        raise PlanValidationError(f"Expected exactly 24 plan entries, got {len(plan)}")

    hours = _get_field(request, "hours")
    if not hours or len(hours) != 24:
        raise PlanValidationError(f"Request must contain 24 hours, got {len(hours) if hours else 0}")

    battery = _get_field(request, "battery")
    if not battery:
        raise PlanValidationError("Request missing battery parameters")

    capacity = float(_get_field(battery, "capacity_kwh"))
    initial_energy = float(_get_field(battery, "initial_energy_kwh"))
    base_minimum = float(_get_field(battery, "minimum_energy_kwh"))
    max_charge = float(_get_field(battery, "max_charge_kwh_per_hour"))
    max_discharge = float(_get_field(battery, "max_discharge_kwh_per_hour"))

    # 1. Independently compute directive constraints
    solar_factors = [1.0] * 24
    reserve_floors = [base_minimum] * 24
    allow_charge = [True] * 24
    allow_discharge = [True] * 24
    grid_caps: list[float | None] = [None] * 24

    for d in (directives or []):
        applies = _get_field(d, "applies", True)
        if not applies:
            continue

        dtype = _get_field(d, "directive_type")
        if hasattr(dtype, "value"):
            dtype = dtype.value
        if dtype == "no_op" or not dtype:
            continue

        adj = _get_field(d, "structured_adjustment")
        if not adj:
            continue

        dh_list = _get_field(adj, "hours", [])

        if dtype == "solar_reduction":
            factor = float(_get_field(adj, "factor", 1.0))
            for h in dh_list:
                if 0 <= h < 24:
                    solar_factors[h] = min(solar_factors[h], factor)
        elif dtype == "minimum_battery_reserve":
            min_kwh = float(_get_field(adj, "minimum_energy_kwh", 0.0))
            for h in dh_list:
                if 0 <= h < 24:
                    reserve_floors[h] = max(reserve_floors[h], min_kwh)
        elif dtype == "no_charge_window":
            for h in dh_list:
                if 0 <= h < 24:
                    allow_charge[h] = False
        elif dtype == "no_discharge_window":
            for h in dh_list:
                if 0 <= h < 24:
                    allow_discharge[h] = False
        elif dtype == "max_grid_window":
            cap = float(_get_field(adj, "max_grid_kwh", 0.0))
            for h in dh_list:
                if 0 <= h < 24:
                    if grid_caps[h] is None:
                        grid_caps[h] = cap
                    else:
                        grid_caps[h] = min(grid_caps[h], cap)

    # 2. Hour-by-hour replay validation
    for h in range(24):
        entry = plan[h]
        req_h = hours[h]

        # Hour ordering
        plan_h = _get_field(entry, "hour")
        if plan_h != h:
            raise PlanValidationError(
                f"Hour index mismatch at position {h}: entry has hour {plan_h}"
            )

        # Numeric field finiteness and non-negativity
        grid = float(_get_field(entry, "grid_kwh"))
        solar_used = float(_get_field(entry, "solar_used_kwh"))
        action = str(_get_field(entry, "battery_action"))
        battery_kwh = float(_get_field(entry, "battery_kwh"))
        e_after = float(_get_field(entry, "battery_energy_after_kwh"))

        for val_name, val in [
            ("grid_kwh", grid),
            ("solar_used_kwh", solar_used),
            ("battery_kwh", battery_kwh),
            ("battery_energy_after_kwh", e_after),
        ]:
            if not math.isfinite(val):
                raise PlanValidationError(f"Hour {h}: {val_name} is non-finite ({val})")
            if val < -tolerance:
                raise PlanValidationError(f"Hour {h}: {val_name} is negative ({val})")

        # Action and magnitude consistency
        if action == "idle":
            if battery_kwh > tolerance:
                raise PlanValidationError(
                    f"Hour {h}: idle battery action must have battery_kwh=0, got {battery_kwh}"
                )
            charge_kwh = 0.0
            discharge_kwh = 0.0
        elif action == "charge":
            if battery_kwh <= 0.0:
                raise PlanValidationError(f"Hour {h}: charge action must have battery_kwh > 0")
            if not allow_charge[h]:
                raise PlanValidationError(f"Hour {h}: charging attempted during prohibited window")
            if battery_kwh > max_charge + tolerance:
                raise PlanValidationError(
                    f"Hour {h}: charge magnitude {battery_kwh} exceeds limit {max_charge}"
                )
            charge_kwh = battery_kwh
            discharge_kwh = 0.0
        elif action == "discharge":
            if battery_kwh <= 0.0:
                raise PlanValidationError(f"Hour {h}: discharge action must have battery_kwh > 0")
            if not allow_discharge[h]:
                raise PlanValidationError(
                    f"Hour {h}: discharging attempted during prohibited window"
                )
            if battery_kwh > max_discharge + tolerance:
                raise PlanValidationError(
                    f"Hour {h}: discharge magnitude {battery_kwh} exceeds limit {max_discharge}"
                )
            charge_kwh = 0.0
            discharge_kwh = battery_kwh
        else:
            raise PlanValidationError(f"Hour {h}: invalid battery_action '{action}'")

        # Solar bounds
        solar_forecast = float(_get_field(req_h, "solar_kwh", 0.0))
        effective_solar = solar_factors[h] * solar_forecast
        if solar_used > effective_solar + tolerance:
            raise PlanValidationError(
                f"Hour {h}: solar_used {solar_used} exceeds effective solar {effective_solar}"
            )

        # Grid caps
        if grid_caps[h] is not None:
            cap = grid_caps[h]
            if grid > cap + tolerance:  # type: ignore[operator]
                raise PlanValidationError(
                    f"Hour {h}: grid import {grid} exceeds active cap {cap}"
                )

        # Energy balance: grid + solar_used + discharge == demand + charge
        demand = float(_get_field(req_h, "demand_kwh", 0.0))
        generation_side = grid + solar_used + discharge_kwh
        demand_side = demand + charge_kwh
        balance_diff = abs(generation_side - demand_side)
        if balance_diff > tolerance:
            raise PlanValidationError(
                f"Hour {h}: energy balance violation: supply={generation_side:.3f} != load={demand_side:.3f} "
                f"(diff={balance_diff:.4f} > tolerance {tolerance})"
            )

        # Battery transition: E_after[h] == E_prev + charge - discharge
        e_prev = initial_energy if h == 0 else float(_get_field(plan[h - 1], "battery_energy_after_kwh"))
        expected_e_after = e_prev + charge_kwh - discharge_kwh
        transition_diff = abs(e_after - expected_e_after)
        if transition_diff > tolerance:
            raise PlanValidationError(
                f"Hour {h}: battery transition violation: e_after={e_after:.3f} != "
                f"expected={expected_e_after:.3f} (diff={transition_diff:.4f})"
            )

        # Storage capacity and reserve floor
        if e_after > capacity + tolerance:
            raise PlanValidationError(
                f"Hour {h}: battery energy {e_after} exceeds capacity {capacity}"
            )
        if e_after < reserve_floors[h] - tolerance:
            raise PlanValidationError(
                f"Hour {h}: battery energy {e_after} below active reserve floor {reserve_floors[h]}"
            )

    # 3. End-of-day neutrality check
    final_energy = float(_get_field(plan[23], "battery_energy_after_kwh"))
    neutrality_diff = abs(final_energy - initial_energy)
    if neutrality_diff > tolerance:
        raise PlanValidationError(
            f"End-of-day neutrality failed: final energy {final_energy:.3f} != initial energy "
            f"{initial_energy:.3f} (diff={neutrality_diff:.4f} > tolerance {tolerance})"
        )

    return True


def validate_plan_metrics(
    *,
    plan: Sequence[Any],
    tariffs: Sequence[float],
    reported_total_grid: float,
    reported_total_cost: float,
    reported_peak_grid: float,
    tolerance: float = 0.01,
) -> bool:
    """Validate that reported scalar totals match recalculated values from the plan."""
    if len(plan) != 24 or len(tariffs) != 24:
        raise PlanValidationError("Expected exactly 24 plan entries and 24 tariffs")

    grids = [float(_get_field(p, "grid_kwh")) for p in plan]
    recalculated_grid = sum(grids)
    recalculated_cost = sum(g * t for g, t in zip(grids, tariffs))
    recalculated_peak = max(grids) if grids else 0.0

    if abs(reported_total_grid - recalculated_grid) > tolerance:
        raise PlanValidationError(
            f"total_grid_kwh mismatch: reported={reported_total_grid}, recalculated={recalculated_grid}"
        )
    if abs(reported_total_cost - recalculated_cost) > tolerance:
        raise PlanValidationError(
            f"total_cost_bdt mismatch: reported={reported_total_cost}, recalculated={recalculated_cost}"
        )
    if abs(reported_peak_grid - recalculated_peak) > tolerance:
        raise PlanValidationError(
            f"peak_grid_kwh mismatch: reported={reported_peak_grid}, recalculated={recalculated_peak}"
        )

    return True
