"""Deterministic guardrails for parsed operator-note interpretations."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.contracts import DirectiveInterpretation


class InterpreterValidationError(ValueError):
    """A safe typed failure for invalid model interpretations."""

    category = "invalid_interpretation"


_INTERPRETATION_FIELDS = {
    "note_index",
    "applies",
    "directive_type",
    "structured_adjustment",
    "explanation",
}
_ADJUSTMENT_FIELDS = {
    "solar_reduction": {"hours", "factor"},
    "minimum_battery_reserve": {"hours", "minimum_energy_kwh"},
    "no_charge_window": {"hours"},
    "no_discharge_window": {"hours"},
    "max_grid_window": {"hours", "max_grid_kwh"},
}
_DIRECTIVE_TYPES = set(_ADJUSTMENT_FIELDS) | {"no_op"}


def _plain_mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        result = dump(mode="python")
        if isinstance(result, Mapping):
            return result
    raise InterpreterValidationError(f"{label} must be an object.")


def _finite_number(value: Any, *, label: str, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InterpreterValidationError(f"{label} must be a finite number.")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise InterpreterValidationError(f"{label} is outside its valid range.") from None
    if not math.isfinite(number) or number < minimum:
        raise InterpreterValidationError(f"{label} is outside its valid range.")
    return number


def _validate_hours(value: Any) -> None:
    if not isinstance(value, list) or not value:
        raise InterpreterValidationError("Applicable directives require a non-empty hours list.")
    if any(type(hour) is not int or hour < 0 or hour > 23 for hour in value):
        raise InterpreterValidationError("Hours must be integer values from 0 through 23.")
    if value != sorted(value) or len(value) != len(set(value)):
        raise InterpreterValidationError("Hours must be unique and in ascending order.")


def validate_interpretations(
    *,
    interpretations: Sequence["DirectiveInterpretation"] | Sequence[Mapping[str, Any]],
    operator_notes: list[str],
    battery_capacity_kwh: float,
) -> list["DirectiveInterpretation"] | list[Mapping[str, Any]]:
    """Validate the complete interpretation batch without mutating any input.

    The function returns the original shared Pydantic objects (or dictionaries
    supplied by deterministic unit tests) after every entry passes. Callers
    must pass this returned collection onward, never a partially checked list.
    """
    if not isinstance(operator_notes, list) or not 1 <= len(operator_notes) <= 3:
        raise InterpreterValidationError("Operator notes must contain between one and three items.")
    capacity = _finite_number(battery_capacity_kwh, label="Battery capacity")
    if not isinstance(interpretations, Sequence) or isinstance(interpretations, (str, bytes)):
        raise InterpreterValidationError("Interpretations must be a list.")
    if len(interpretations) != len(operator_notes):
        raise InterpreterValidationError("There must be exactly one interpretation per note.")

    for expected_index, item in enumerate(interpretations):
        data = _plain_mapping(item, label="Interpretation")
        if set(data) != _INTERPRETATION_FIELDS:
            raise InterpreterValidationError("Interpretation fields do not match the contract.")
        index = data["note_index"]
        if type(index) is not int or index != expected_index:
            raise InterpreterValidationError("Interpretations must map each note once in note order.")
        applies = data["applies"]
        directive_type = data["directive_type"]
        explanation = data["explanation"]
        if type(applies) is not bool or not isinstance(directive_type, str):
            raise InterpreterValidationError("Interpretation type fields are invalid.")
        if directive_type not in _DIRECTIVE_TYPES:
            raise InterpreterValidationError("Unsupported directive type.")
        if not isinstance(explanation, str) or not explanation.strip():
            raise InterpreterValidationError("Interpretation explanation must be non-empty text.")

        adjustment = data["structured_adjustment"]
        if directive_type == "no_op":
            if applies is not False or adjustment is not None:
                raise InterpreterValidationError("no_op must have applies=false and no adjustment.")
            continue

        if applies is not True:
            raise InterpreterValidationError("Supported directives must have applies=true.")
        adjustment = _plain_mapping(adjustment, label="Directive adjustment")
        if set(adjustment) != _ADJUSTMENT_FIELDS[directive_type]:
            raise InterpreterValidationError("Directive adjustment fields do not match its type.")
        _validate_hours(adjustment["hours"])

        if directive_type == "solar_reduction":
            factor = _finite_number(adjustment["factor"], label="Solar factor")
            if factor > 1:
                raise InterpreterValidationError("Solar factor must be between 0 and 1.")
        elif directive_type == "minimum_battery_reserve":
            reserve = _finite_number(
                adjustment["minimum_energy_kwh"], label="Battery reserve"
            )
            if reserve > capacity:
                raise InterpreterValidationError("Battery reserve exceeds battery capacity.")
        elif directive_type == "max_grid_window":
            _finite_number(adjustment["max_grid_kwh"], label="Grid cap")

    return list(interpretations)
