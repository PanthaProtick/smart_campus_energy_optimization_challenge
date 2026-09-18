from __future__ import annotations

import sys
import types
from typing import Annotated, Literal, Union

import pytest
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from app.services.interpreter_parser import (
    InterpreterOutputTruncatedError,
    InterpreterParseError,
    parse_provider_response,
)


class _NoOp(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    note_index: StrictInt
    applies: Literal[False]
    directive_type: Literal["no_op"]
    structured_adjustment: None
    explanation: str


class _Solar(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    note_index: StrictInt
    applies: Literal[True]
    directive_type: Literal["solar_reduction"]
    structured_adjustment: dict
    explanation: str


_Directive = Annotated[Union[_NoOp, _Solar], Field(discriminator="directive_type")]


@pytest.fixture
def shared_contract_module(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = types.ModuleType("app.contracts")
    module.DirectiveInterpretation = _Directive
    monkeypatch.setitem(sys.modules, "app.contracts", module)
    return module


def _gemini_response(text: str, *, finish_reason: str = "STOP") -> dict:
    return {
        "candidates": [
            {
                "finishReason": finish_reason,
                "content": {"parts": [{"text": text}]},
            }
        ]
    }


def test_valid_output_becomes_pydantic_discriminated_model(
    shared_contract_module: types.ModuleType,
) -> None:
    result = parse_provider_response(
        _gemini_response(
            '{"interpretations":[{"note_index":0,"applies":false,'
            '"directive_type":"no_op","structured_adjustment":null,'
            '"explanation":"No supported instruction."}]}'
        )
    )
    assert len(result) == 1
    assert isinstance(result[0], _NoOp)


@pytest.mark.parametrize(
    "response",
    [
        _gemini_response(""),
        _gemini_response('{"interpretations":['),
        _gemini_response('[{"note_index":0}]'),
        _gemini_response('{"interpretations":[],"unexpected":true}'),
    ],
)
def test_empty_malformed_wrong_root_and_unknown_root_rejected(
    response: dict, shared_contract_module: types.ModuleType
) -> None:
    with pytest.raises(InterpreterParseError):
        parse_provider_response(response)


def test_truncated_provider_output_has_distinct_typed_error(
    shared_contract_module: types.ModuleType,
) -> None:
    with pytest.raises(InterpreterOutputTruncatedError):
        parse_provider_response(_gemini_response('{"interpretations":', finish_reason="MAX_TOKENS"))


@pytest.mark.parametrize(
    "interpretation",
    [
        {
            "note_index": 0,
            "applies": False,
            "directive_type": "no_op",
            "structured_adjustment": None,
            "explanation": "Looks okay.",
            "unexpected": "extra",
        },
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "not_supported",
            "structured_adjustment": {},
            "explanation": "Unsupported.",
        },
    ],
)
def test_unknown_fields_and_invalid_union_variants_rejected(
    interpretation: dict, shared_contract_module: types.ModuleType
) -> None:
    import json

    with pytest.raises(InterpreterParseError):
        parse_provider_response(json.dumps({"interpretations": [interpretation]}))
