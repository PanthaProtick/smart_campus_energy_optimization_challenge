"""Shared, strict Pydantic contracts for the GridWise API and components."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr


NonNegative: TypeAlias = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Hour: TypeAlias = Annotated[StrictInt, Field(ge=0, le=23)]
HourList: TypeAlias = Annotated[list[Hour], Field(min_length=1, max_length=24)]
NonBlank: TypeAlias = Annotated[StrictStr, Field(min_length=1, max_length=2000, pattern=r"\S")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class HealthResponse(StrictModel):
    status: Literal["ok"]


class HourInput(StrictModel):
    hour: Hour
    demand_kwh: NonNegative
    solar_kwh: NonNegative
    tariff_bdt_per_kwh: NonNegative


class BatteryInput(StrictModel):
    capacity_kwh: NonNegative
    initial_energy_kwh: NonNegative
    minimum_energy_kwh: NonNegative
    max_charge_kwh_per_hour: NonNegative
    max_discharge_kwh_per_hour: NonNegative


class OptimizeEnergyRequest(StrictModel):
    scenario_id: Annotated[StrictStr, Field(min_length=1, max_length=128, pattern=r"\S")]
    operator_notes: Annotated[list[NonBlank], Field(min_length=1, max_length=3)]
    hours: Annotated[list[HourInput], Field(min_length=24, max_length=24)]
    battery: BatteryInput


class SolarReductionAdjustment(StrictModel):
    hours: HourList
    factor: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class ReserveAdjustment(StrictModel):
    hours: HourList
    minimum_energy_kwh: NonNegative


class WindowAdjustment(StrictModel):
    hours: HourList


class GridCapAdjustment(StrictModel):
    hours: HourList
    max_grid_kwh: NonNegative


class _InterpretationBase(StrictModel):
    note_index: Annotated[StrictInt, Field(ge=0, le=2)]
    explanation: Annotated[StrictStr, Field(min_length=1, max_length=500, pattern=r"\S")]
    applies: Literal[True]


class SolarReductionInterpretation(_InterpretationBase):
    directive_type: Literal["solar_reduction"]
    structured_adjustment: SolarReductionAdjustment


class ReserveInterpretation(_InterpretationBase):
    directive_type: Literal["minimum_battery_reserve"]
    structured_adjustment: ReserveAdjustment


class NoChargeInterpretation(_InterpretationBase):
    directive_type: Literal["no_charge_window"]
    structured_adjustment: WindowAdjustment


class NoDischargeInterpretation(_InterpretationBase):
    directive_type: Literal["no_discharge_window"]
    structured_adjustment: WindowAdjustment


class GridCapInterpretation(_InterpretationBase):
    directive_type: Literal["max_grid_window"]
    structured_adjustment: GridCapAdjustment


class NoOpInterpretation(StrictModel):
    note_index: Annotated[StrictInt, Field(ge=0, le=2)]
    applies: Literal[False]
    directive_type: Literal["no_op"]
    structured_adjustment: None
    explanation: Annotated[StrictStr, Field(min_length=1, max_length=500, pattern=r"\S")]


DirectiveInterpretation: TypeAlias = Annotated[
    SolarReductionInterpretation
    | ReserveInterpretation
    | NoChargeInterpretation
    | NoDischargeInterpretation
    | GridCapInterpretation
    | NoOpInterpretation,
    Field(discriminator="directive_type"),
]


class HourlyPlanEntry(StrictModel):
    hour: Hour
    grid_kwh: NonNegative
    solar_used_kwh: NonNegative
    battery_action: Literal["charge", "discharge", "idle"]
    battery_kwh: NonNegative
    battery_energy_after_kwh: NonNegative


class OptimizeEnergyResponse(StrictModel):
    scenario_id: Annotated[StrictStr, Field(min_length=1, max_length=128, pattern=r"\S")]
    directive_interpretation: Annotated[list[DirectiveInterpretation], Field(min_length=1, max_length=3)]
    hourly_plan: Annotated[list[HourlyPlanEntry], Field(min_length=24, max_length=24)]
    total_grid_kwh: NonNegative
    total_cost_bdt: NonNegative
    peak_grid_kwh: NonNegative
    plan_summary: Annotated[StrictStr, Field(min_length=1, max_length=1000, pattern=r"\S")]


class ErrorDetail(StrictModel):
    code: Literal[
        "malformed_json",
        "invalid_request",
        "interpretation_failed",
        "optimization_failed",
        "internal_error",
    ]
    message: Annotated[StrictStr, Field(min_length=1, max_length=500)]


class ErrorResponse(StrictModel):
    error: ErrorDetail


def validate_request_invariants(request: OptimizeEnergyRequest) -> None:
    """Validate cross-field/list invariants that are not expressible per field."""
    if [entry.hour for entry in request.hours] != list(range(24)):
        raise ValueError("hours must contain exactly one entry for each hour from 0 through 23 in order.")
    battery = request.battery
    if battery.initial_energy_kwh > battery.capacity_kwh:
        raise ValueError("initial_energy_kwh must not exceed battery capacity_kwh.")
    if battery.minimum_energy_kwh > battery.initial_energy_kwh:
        raise ValueError("minimum_energy_kwh must not exceed initial_energy_kwh.")


def validate_directive_interpretations(
    directives: list[DirectiveInterpretation], *, note_count: int, battery_capacity_kwh: float
) -> None:
    """Check component output coverage and semantics before optimization."""
    if len(directives) != note_count or [item.note_index for item in directives] != list(range(note_count)):
        raise ValueError("Interpreter must return exactly one ordered interpretation per operator note.")
    for item in directives:
        if item.directive_type == "no_op":
            if item.applies is not False or item.structured_adjustment is not None:
                raise ValueError("no_op must have applies=false and a null adjustment.")
            continue
        if item.applies is not True:
            raise ValueError("Every applicable directive must have applies=true.")
        adjustment = item.structured_adjustment
        hours = adjustment.hours
        if not hours or hours != sorted(set(hours)):
            raise ValueError("Directive hours must be non-empty, unique, and ascending.")
        if isinstance(item, ReserveInterpretation) and adjustment.minimum_energy_kwh > battery_capacity_kwh:
            raise ValueError("Directive reserve must not exceed battery capacity.")


def validate_plan_sequence(plan: list[HourlyPlanEntry]) -> None:
    """Validate response hour coverage before replay validation by the optimizer."""
    if [entry.hour for entry in plan] != list(range(24)):
        raise ValueError("hourly_plan must contain hours 0 through 23 in order.")
    if any(entry.battery_action == "idle" and entry.battery_kwh != 0 for entry in plan):
        raise ValueError("battery_kwh must be zero when battery_action is idle.")
