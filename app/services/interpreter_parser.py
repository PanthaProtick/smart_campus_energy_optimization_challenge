"""Parse Gemini output into the shared discriminated interpretation models."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter, ValidationError

if TYPE_CHECKING:
    from app.contracts import DirectiveInterpretation


class InterpreterParseError(RuntimeError):
    """A safe, typed failure to extract or validate interpreter output."""

    category = "malformed_model_output"


class InterpreterOutputTruncatedError(InterpreterParseError):
    category = "truncated_model_output"


def _extract_json_text(response: dict[str, Any] | str) -> str:
    """Extract plain structured JSON text from Gemini's REST response."""
    if isinstance(response, str):
        if not response.strip():
            raise InterpreterParseError("Interpreter returned empty output.")
        return response
    if not isinstance(response, dict):
        raise InterpreterParseError("Interpreter provider response has an invalid root type.")

    candidates = response.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 1:
        raise InterpreterParseError("Interpreter provider response has no unique candidate.")
    candidate = candidates[0]
    if not isinstance(candidate, dict):
        raise InterpreterParseError("Interpreter provider candidate is malformed.")
    if candidate.get("finishReason") == "MAX_TOKENS":
        raise InterpreterOutputTruncatedError("Interpreter output was truncated.")
    finish_reason = candidate.get("finishReason")
    if finish_reason not in (None, "STOP"):
        raise InterpreterParseError("Interpreter provider did not complete a valid response.")
    content = candidate.get("content")
    if not isinstance(content, dict):
        raise InterpreterParseError("Interpreter provider response has no content.")
    parts = content.get("parts")
    if not isinstance(parts, list) or not parts:
        raise InterpreterParseError("Interpreter provider returned empty content.")
    text_parts: list[str] = []
    for part in parts:
        if not isinstance(part, dict) or not isinstance(part.get("text"), str):
            raise InterpreterParseError("Interpreter provider returned non-text content.")
        text_parts.append(part["text"])
    text = "".join(text_parts)
    if not text.strip():
        raise InterpreterParseError("Interpreter provider returned empty content.")
    return text


def parse_provider_response(
    response: dict[str, Any] | str,
) -> list["DirectiveInterpretation"]:
    """Parse provider JSON and validate entries as shared DirectiveInterpretation models.

    The import is deliberately delayed so parser envelope/error paths can load
    during contract-first parallel work before Person 1 publishes contracts.py.
    """
    text = _extract_json_text(response)
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise InterpreterParseError("Interpreter returned malformed JSON.") from None
    if not isinstance(payload, dict):
        raise InterpreterParseError("Interpreter output root must be a JSON object.")
    if set(payload) != {"interpretations"}:
        raise InterpreterParseError("Interpreter output has missing or unknown root fields.")
    interpretations = payload["interpretations"]
    if not isinstance(interpretations, list) or not interpretations:
        raise InterpreterParseError("Interpreter output must contain interpretations.")

    try:
        from app.contracts import DirectiveInterpretation
    except (ImportError, AttributeError):
        raise InterpreterParseError(
            "Shared interpretation contract is unavailable."
        ) from None

    adapter = TypeAdapter(DirectiveInterpretation)
    parsed: list["DirectiveInterpretation"] = []
    try:
        for interpretation in interpretations:
            parsed.append(adapter.validate_python(interpretation, strict=True))
    except (ValidationError, TypeError, ValueError):
        raise InterpreterParseError(
            "Interpreter output does not match the shared directive contract."
        ) from None
    return parsed
