from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt

import app.services.interpreter as interpreter


class _OfflineInterpretation(BaseModel):
    """Small contract stand-in until Person 1 publishes shared contracts.py."""

    model_config = ConfigDict(extra="forbid", strict=True)
    note_index: StrictInt
    applies: StrictBool
    directive_type: str
    structured_adjustment: dict | None
    explanation: str


def _install_contract_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("app.contracts")
    module.DirectiveInterpretation = _OfflineInterpretation
    monkeypatch.setitem(sys.modules, "app.contracts", module)


@pytest.mark.parametrize(
    "case",
    json.loads(
        (
            Path(__file__).parents[1]
            / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
        ).read_text(encoding="utf-8")
    )["cases"],
    ids=lambda case: case["id"],
)
def test_public_sample_cases_match_machine_checkable_interpretation_fields(
    case: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay official expected interpretations through parsing and guardrails."""
    _install_contract_stub(monkeypatch)
    expected = case["expected_output"]["directive_interpretation"]

    class _GoldenClient:
        def generate_content(self, *, prompt: str, response_schema: dict) -> dict:
            assert all(note in prompt for note in case["input"]["operator_notes"])
            assert response_schema["properties"]["interpretations"]["minItems"] == len(expected)
            text = json.dumps({"interpretations": expected})
            return {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": text}]},
                    }
                ]
            }

    monkeypatch.setattr(interpreter, "GeminiClient", _GoldenClient)
    result = asyncio.run(
        interpreter.interpret_notes(
            operator_notes=case["input"]["operator_notes"],
            battery_capacity_kwh=case["input"]["battery"]["capacity_kwh"],
        )
    )
    assert len(result) == len(expected)
    actual = [entry.model_dump(mode="python") for entry in result]
    for actual_entry, expected_entry in zip(actual, expected, strict=True):
        for field in (
            "note_index",
            "applies",
            "directive_type",
            "structured_adjustment",
        ):
            assert actual_entry[field] == expected_entry[field], (
                case["id"],
                field,
                actual_entry[field],
                expected_entry[field],
            )
