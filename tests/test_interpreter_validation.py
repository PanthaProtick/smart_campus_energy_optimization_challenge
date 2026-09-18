from __future__ import annotations

import math

import pytest

from app.services.interpreter_validation import (
    InterpreterValidationError,
    validate_interpretations,
)


NOTES = ["Note one", "Note two"]


def _result(index: int, directive_type: str, adjustment, applies: bool = True) -> dict:
    return {
        "note_index": index,
        "applies": applies,
        "directive_type": directive_type,
        "structured_adjustment": adjustment,
        "explanation": "Interpretation explanation.",
    }


def _valid_pair() -> list[dict]:
    return [
        _result(0, "solar_reduction", {"hours": [13, 14], "factor": 0.2}),
        _result(1, "no_op", None, applies=False),
    ]


def test_all_supported_variants_and_no_op_pass_and_original_batch_is_returned() -> None:
    pairs = [
        ("solar_reduction", {"hours": [3], "factor": 0.2}),
        ("minimum_battery_reserve", {"hours": [3], "minimum_energy_kwh": 5}),
        ("no_charge_window", {"hours": [3]}),
        ("no_discharge_window", {"hours": [3]}),
        ("max_grid_window", {"hours": [3], "max_grid_kwh": 10}),
        ("no_op", None),
    ]
    for kind, adjustment in pairs:
        expected = [_result(0, kind, adjustment, applies=kind != "no_op")]
        result = validate_interpretations(
            interpretations=expected,
            operator_notes=["directive"],
            battery_capacity_kwh=10,
        )
        assert result == expected
        assert result is not expected


@pytest.mark.parametrize(
    "interpretations,notes",
    [
        ([], NOTES),
        (_valid_pair()[:1], NOTES),
        (_valid_pair() + [_valid_pair()[1]], NOTES),
        ([_result(1, "solar_reduction", {"hours": [1], "factor": 0.2}), _result(1, "no_op", None, False)], NOTES),
        ([_result(0, "no_op", None, False), _result(0, "no_op", None, False)], NOTES),
        ([_result(0, "no_op", None, False)], ["a", "b", "c", "d"]),
    ],
)
def test_count_order_and_note_index_errors_reject(
    interpretations: list[dict], notes: list[str]
) -> None:
    with pytest.raises(InterpreterValidationError):
        validate_interpretations(
            interpretations=interpretations,
            operator_notes=notes,
            battery_capacity_kwh=10,
        )


@pytest.mark.parametrize(
    "result",
    [
        {**_result(0, "solar_reduction", {"hours": [1], "factor": 0.2}), "unexpected": 1},
        _result(0, "unknown", {"hours": [1]}, True),
        _result(0, "solar_reduction", {"hours": [1], "factor": 0.2}, False),
        _result(0, "no_charge_window", {"hours": [1]}, False),
        _result(0, "no_op", {"hours": [1]}, False),
        _result(0, "no_op", None, True),
        {**_result(0, "no_op", None, False), "applies": 0},
        {**_result(0, "no_op", None, False), "note_index": True},
        _result(0, "solar_reduction", {"hours": [1], "factor": 0.2, "extra": 4}),
        _result(0, "solar_reduction", {"hours": [1]}),
        _result(0, "no_charge_window", {"hours": [1], "factor": 0.5}),
    ],
)
def test_directive_discriminator_applies_and_exact_fields_reject(result: dict) -> None:
    with pytest.raises(InterpreterValidationError):
        validate_interpretations(
            interpretations=[result],
            operator_notes=["note"],
            battery_capacity_kwh=10,
        )


@pytest.mark.parametrize("hours", [[], [True], [-1], [24], [1, 1], [2, 1], [1.0]])
def test_empty_wrong_type_out_of_range_duplicate_and_unsorted_hours_reject(hours) -> None:
    with pytest.raises(InterpreterValidationError):
        validate_interpretations(
            interpretations=[_result(0, "no_charge_window", {"hours": hours})],
            operator_notes=["note"],
            battery_capacity_kwh=10,
        )


@pytest.mark.parametrize("factor", [True, -0.1, 1.1, math.inf, math.nan, "0.5"])
def test_solar_factor_must_be_finite_numeric_and_in_range(factor) -> None:
    with pytest.raises(InterpreterValidationError):
        validate_interpretations(
            interpretations=[_result(0, "solar_reduction", {"hours": [0], "factor": factor})],
            operator_notes=["note"],
            battery_capacity_kwh=10,
        )


@pytest.mark.parametrize("reserve", [True, -1, 10.01, math.inf, math.nan, "5"])
def test_reserve_must_be_finite_nonnegative_and_at_most_capacity(reserve) -> None:
    with pytest.raises(InterpreterValidationError):
        validate_interpretations(
            interpretations=[_result(0, "minimum_battery_reserve", {"hours": [0], "minimum_energy_kwh": reserve})],
            operator_notes=["note"],
            battery_capacity_kwh=10,
        )


@pytest.mark.parametrize("capacity", [True, -1, math.inf, math.nan, "10"])
def test_battery_capacity_must_be_finite_nonnegative_numeric(capacity) -> None:
    with pytest.raises(InterpreterValidationError):
        validate_interpretations(
            interpretations=[_result(0, "no_op", None, False)],
            operator_notes=["note"],
            battery_capacity_kwh=capacity,
        )


@pytest.mark.parametrize("cap", [True, -1, math.inf, math.nan, "10"])
def test_grid_cap_must_be_finite_nonnegative_numeric(cap) -> None:
    with pytest.raises(InterpreterValidationError):
        validate_interpretations(
            interpretations=[_result(0, "max_grid_window", {"hours": [0], "max_grid_kwh": cap})],
            operator_notes=["note"],
            battery_capacity_kwh=10,
        )
