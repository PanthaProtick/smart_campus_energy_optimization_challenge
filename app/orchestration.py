"""API orchestration for the independently owned interpreter and optimizer."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from anyio import to_thread
from pydantic import TypeAdapter

from app.contracts import (
    DirectiveInterpretation,
    HourlyPlanEntry,
    OptimizeEnergyRequest,
    OptimizeEnergyResponse,
    validate_directive_interpretations,
    validate_plan_sequence,
)

DIRECTIVE_ADAPTER = TypeAdapter(DirectiveInterpretation)
PLAN_ADAPTER = TypeAdapter(HourlyPlanEntry)


class PipelineFailure(Exception):
    """A safe, categorized failure suitable for the public error envelope."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.public_message = message


def _load_components() -> tuple[Callable[..., Any], Callable[..., Any], Callable[..., Any]]:
    """Load teammate-owned components only when a request reaches this path."""
    try:
        from app.services.interpreter import interpret_notes
        from app.services.optimizer import optimize_energy
        from app.services.plan_validator import validate_plan
    except (ImportError, AttributeError) as exc:
        raise PipelineFailure(
            "internal_error", "The interpretation and scheduling components are not available."
        ) from exc
    return interpret_notes, optimize_energy, validate_plan


async def _call_optimizer(function: Callable[..., Any], **kwargs: Any) -> Any:
    if inspect.iscoroutinefunction(function):
        return await function(**kwargs)
    result = await to_thread.run_sync(lambda: function(**kwargs), abandon_on_cancel=True)
    if inspect.isawaitable(result):
        return await result
    return result


async def process_request(
    request: OptimizeEnergyRequest,
    *,
    components: tuple[Callable[..., Any], Callable[..., Any], Callable[..., Any]] | None = None,
) -> OptimizeEnergyResponse:
    """Interpret notes, validate them, optimize, replay, then build the response."""
    interpreter, optimizer, replay_validator = components or _load_components()
    try:
        raw_directives = interpreter(
            operator_notes=list(request.operator_notes),
            battery_capacity_kwh=request.battery.capacity_kwh,
        )
        if inspect.isawaitable(raw_directives):
            raw_directives = await raw_directives
        directives = [DIRECTIVE_ADAPTER.validate_python(value) for value in raw_directives]
        validate_directive_interpretations(
            directives,
            note_count=len(request.operator_notes),
            battery_capacity_kwh=request.battery.capacity_kwh,
        )
    except PipelineFailure:
        raise
    except Exception as exc:
        raise PipelineFailure(
            "interpretation_failed", "The operator notes could not be interpreted safely."
        ) from exc

    try:
        # Components receive independent objects: directive application must not mutate the
        # validated request retained by the API for replay and response metric calculation.
        optimizer_request = request.model_copy(deep=True)
        optimizer_directives = [item.model_copy(deep=True) for item in directives]
        raw_plan = await _call_optimizer(
            optimizer, request=optimizer_request, directives=optimizer_directives
        )
        plan = [PLAN_ADAPTER.validate_python(value) for value in raw_plan]
        validate_plan_sequence(plan)
        replay_result = await _call_optimizer(
            replay_validator,
            request=request.model_copy(deep=True),
            directives=[item.model_copy(deep=True) for item in directives],
            plan=[item.model_copy(deep=True) for item in plan],
        )
        if replay_result is False or any(
            getattr(replay_result, flag, True) is False for flag in ("passed", "valid", "success")
        ):
            raise ValueError("Independent plan replay rejected the candidate.")
    except PipelineFailure:
        raise
    except Exception as exc:
        raise PipelineFailure(
            "optimization_failed", "A valid energy schedule could not be produced."
        ) from exc

    total_grid = sum(item.grid_kwh for item in plan)
    total_cost = sum(
        item.grid_kwh * request.hours[item.hour].tariff_bdt_per_kwh for item in plan
    )
    peak_grid = max(item.grid_kwh for item in plan)
    return OptimizeEnergyResponse(
        scenario_id=request.scenario_id,
        directive_interpretation=directives,
        hourly_plan=plan,
        total_grid_kwh=total_grid,
        total_cost_bdt=total_cost,
        peak_grid_kwh=peak_grid,
        plan_summary=(
            "The schedule follows the interpreted operator rules, passes independent replay, "
            "and minimizes grid electricity cost while returning the battery to its starting level."
        ),
    )
