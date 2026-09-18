"""Deterministic energy schedule optimization and directive normalization.

This module implements:
- OPT-01: Normalizing validated directives into 24-hour mathematical constraints
  with a conservative overlap policy.
- Downstream optimization logic (OPT-02+).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


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


def _get_field(obj: Any, name: str, default: Any = None) -> Any:
    """Extract attribute or dict key safely to support Pydantic models, dataclasses, and dicts."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


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
