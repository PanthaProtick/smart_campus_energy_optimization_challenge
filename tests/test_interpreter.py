from __future__ import annotations

import asyncio
import sys
import types
import time
from typing import Annotated, Literal, Union

import pytest
from pydantic import BaseModel, ConfigDict, Field, StrictInt

import app.services.interpreter as interpreter
from app.services.llm_provider import (
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
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
def contract_module(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = types.ModuleType("app.contracts")
    module.DirectiveInterpretation = _Directive
    monkeypatch.setitem(sys.modules, "app.contracts", module)
    return module


class _SuccessfulClient:
    def generate_content(self, *, prompt: str, response_schema: dict) -> dict:
        assert "solar forecasts" in prompt
        assert response_schema["properties"]["interpretations"]["minItems"] == 1
        return {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "parts": [
                            {
                                "text": (
                                    '{"interpretations":[{"note_index":0,"applies":true,'
                                    '"directive_type":"solar_reduction",'
                                    '"structured_adjustment":{"hours":[13,14],"factor":0.2},'
                                    '"explanation":"Solar output is reduced."}]}'
                                )
                            }
                        ]
                    },
                }
            ]
        }


def test_public_interpreter_returns_shared_model_after_validation(
    monkeypatch: pytest.MonkeyPatch, contract_module: types.ModuleType
) -> None:
    monkeypatch.setattr(interpreter, "GeminiClient", _SuccessfulClient)
    result = asyncio.run(
        interpreter.interpret_notes(
            operator_notes=["Solar output drops to 20% from 1 PM to 3 PM."],
            battery_capacity_kwh=500,
        )
    )
    assert len(result) == 1
    assert isinstance(result[0], _Solar)
    assert result[0].structured_adjustment == {"hours": [13, 14], "factor": 0.2}


@pytest.mark.parametrize(
    "provider_error,category",
    [
        (ProviderTimeoutError("do not expose secret-value"), "timeout"),
        (ProviderUnavailableError("do not expose secret-value"), "provider_unavailable"),
        (ProviderResponseError("do not expose secret-value"), "invalid_provider_response"),
    ],
)
def test_provider_failures_are_safe_typed_and_logged_without_payload(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    provider_error: Exception,
    category: str,
) -> None:
    class _FailingClient:
        def generate_content(self, **kwargs):
            raise provider_error

    monkeypatch.setattr(interpreter, "GeminiClient", _FailingClient)
    caplog.set_level("WARNING", logger=interpreter.__name__)
    with pytest.raises(interpreter.InterpreterFailure) as error:
        asyncio.run(
            interpreter.interpret_notes(
                operator_notes=["secret-value should never be logged"],
                battery_capacity_kwh=100,
            )
        )
    assert error.value.category == category
    assert "secret-value" not in str(error.value)
    assert "secret-value" not in caplog.text
    assert "correlation_id=" in caplog.text
    assert f"category={category}" in caplog.text


def test_invalid_request_is_rejected_before_model_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_client():
        raise AssertionError("Provider must not be called for invalid input")

    monkeypatch.setattr(interpreter, "GeminiClient", unexpected_client)
    with pytest.raises(interpreter.InterpreterFailure) as error:
        asyncio.run(
            interpreter.interpret_notes(operator_notes=[], battery_capacity_kwh=100)
        )
    assert error.value.category == "invalid_input"


def test_total_interpreter_timeout_is_bounded_and_safe(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _SlowClient:
        def generate_content(self, **kwargs):
            time.sleep(0.1)
            return {}

    monkeypatch.setattr(interpreter, "GeminiClient", _SlowClient)
    monkeypatch.setattr(interpreter, "_INTERPRETER_TIMEOUT_SECONDS", 0.01)
    caplog.set_level("WARNING", logger=interpreter.__name__)
    with pytest.raises(interpreter.InterpreterFailure) as error:
        asyncio.run(
            interpreter.interpret_notes(
                operator_notes=["timeout note"],
                battery_capacity_kwh=100,
            )
        )
    assert error.value.category == "timeout"
    assert "timeout note" not in caplog.text
    assert "category=timeout" in caplog.text
