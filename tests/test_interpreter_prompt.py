from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.interpreter_prompt import (
    DIRECTIVE_TYPES,
    build_interpretation_prompt,
    interpretation_schema,
)


def test_schema_has_exact_result_count_and_only_supported_directive_types() -> None:
    schema = interpretation_schema(2)
    items = schema["properties"]["interpretations"]
    assert items["minItems"] == items["maxItems"] == 2
    variants = items["items"]["oneOf"]
    directive_values = [variant["properties"]["directive_type"]["enum"][0] for variant in variants]
    assert directive_values == DIRECTIVE_TYPES
    shapes = {
        item["properties"]["directive_type"]["enum"][0]: item["properties"]["structured_adjustment"]
        for item in variants
    }
    assert set(shapes["solar_reduction"]["properties"]) == {"hours", "factor"}
    assert shapes["no_charge_window"]["type"] == "object"
    assert shapes["no_op"]["type"] == "null"


def test_prompt_includes_only_notes_and_capacity_context() -> None:
    prompt = build_interpretation_prompt(
        operator_notes=["No charge from 10 AM to noon."],
        battery_capacity_kwh=500,
    )
    assert "10 AM to noon" in prompt
    assert "500 kWh" in prompt
    assert "start-inclusive and end-exclusive" in prompt
    assert "[13, 14]" in prompt
    assert "demand, solar forecasts, tariffs" in prompt


@pytest.mark.parametrize("count", [0, 4])
def test_schema_rejects_note_counts_outside_shared_api_contract(count: int) -> None:
    with pytest.raises(ValueError):
        interpretation_schema(count)


def test_saved_representative_outputs_parse_and_match_directive_shapes() -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "interpreter_model_outputs.json"
    examples = json.loads(fixture_path.read_text(encoding="utf-8"))["examples"]
    expected_adjustment_keys = {
        "solar_reduction": {"hours", "factor"},
        "minimum_battery_reserve": {"hours", "minimum_energy_kwh"},
        "no_charge_window": {"hours"},
        "no_discharge_window": {"hours"},
        "max_grid_window": {"hours", "max_grid_kwh"},
        "no_op": None,
    }
    assert {example["directive_type"] for example in examples} == set(DIRECTIVE_TYPES)
    for example in examples:
        assert set(example) == {
            "note_index",
            "applies",
            "directive_type",
            "structured_adjustment",
            "explanation",
        }
        expected_keys = expected_adjustment_keys[example["directive_type"]]
        adjustment = example["structured_adjustment"]
        if expected_keys is None:
            assert example["applies"] is False
            assert adjustment is None
        else:
            assert example["applies"] is True
            assert set(adjustment) == expected_keys
            assert adjustment["hours"] == sorted(set(adjustment["hours"]))
            assert all(type(hour) is int and 0 <= hour <= 23 for hour in adjustment["hours"])
