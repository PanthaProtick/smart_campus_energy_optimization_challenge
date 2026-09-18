"""Public LLM-backed operator-note interpretation service."""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from typing import TYPE_CHECKING

from app.services.interpreter_parser import InterpreterParseError, parse_provider_response
from app.services.interpreter_prompt import (
    build_interpretation_prompt,
    interpretation_schema,
)
from app.services.interpreter_validation import (
    InterpreterValidationError,
    validate_interpretations,
)
from app.services.llm_provider import (
    GeminiClient,
    ProviderConfigurationError,
    ProviderError,
    ProviderRateLimitError,
    ProviderRejectedError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

if TYPE_CHECKING:
    from app.contracts import DirectiveInterpretation


logger = logging.getLogger(__name__)
_INTERPRETER_TIMEOUT_SECONDS = 17.0


class InterpreterFailure(RuntimeError):
    """Safe typed failure for the API layer to map to its error response."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


def _safe_log_failure(correlation_id: str, category: str) -> None:
    logger.warning(
        "operator_note_interpretation_failed correlation_id=%s category=%s",
        correlation_id,
        category,
    )


async def interpret_notes(
    *,
    operator_notes: list[str],
    battery_capacity_kwh: float,
) -> list["DirectiveInterpretation"]:
    """Interpret each operator note once and validate the full result batch."""
    correlation_id = uuid.uuid4().hex
    if not isinstance(operator_notes, list) or not 1 <= len(operator_notes) <= 3:
        _safe_log_failure(correlation_id, "invalid_input")
        raise InterpreterFailure(
            "invalid_input", "Operator notes must contain between one and three items."
        )
    if any(not isinstance(note, str) or not note.strip() for note in operator_notes):
        _safe_log_failure(correlation_id, "invalid_input")
        raise InterpreterFailure("invalid_input", "Operator notes must be non-empty text.")
    try:
        capacity = float(battery_capacity_kwh)
    except (OverflowError, TypeError, ValueError):
        capacity = math.nan
    if (
        isinstance(battery_capacity_kwh, bool)
        or not isinstance(battery_capacity_kwh, (int, float))
        or not math.isfinite(capacity)
        or capacity < 0
    ):
        _safe_log_failure(correlation_id, "invalid_input")
        raise InterpreterFailure("invalid_input", "Battery capacity must be a finite non-negative number.")

    try:
        prompt = build_interpretation_prompt(
            operator_notes=operator_notes,
            battery_capacity_kwh=capacity,
        )
        response_schema = interpretation_schema(len(operator_notes))
        client = GeminiClient()
        provider_response = await asyncio.wait_for(
            asyncio.to_thread(
                client.generate_content,
                prompt=prompt,
                response_schema=response_schema,
            ),
            timeout=_INTERPRETER_TIMEOUT_SECONDS,
        )
        parsed = parse_provider_response(provider_response)
        return validate_interpretations(
            interpretations=parsed,
            operator_notes=operator_notes,
            battery_capacity_kwh=capacity,
        )
    except asyncio.TimeoutError:
        category = "timeout"
        _safe_log_failure(correlation_id, category)
        raise InterpreterFailure(category, "Operator notes could not be interpreted before timeout.") from None
    except ProviderConfigurationError:
        category = "configuration_error"
        _safe_log_failure(correlation_id, category)
        raise InterpreterFailure(
            category, "Interpreter provider is not configured."
        ) from None
    except ProviderTimeoutError:
        category = "timeout"
        _safe_log_failure(correlation_id, category)
        raise InterpreterFailure(category, "Interpreter provider request timed out.") from None
    except ProviderRateLimitError:
        category = "rate_limited"
        _safe_log_failure(correlation_id, category)
        raise InterpreterFailure(
            category, "Gemini rate limit or quota was reached; retry later or check project quota."
        ) from None
    except ProviderRejectedError:
        category = "provider_rejected"
        _safe_log_failure(correlation_id, category)
        raise InterpreterFailure(
            category, "Gemini rejected the request; check key access, model ID, and request format."
        ) from None
    except ProviderUnavailableError:
        category = "provider_unavailable"
        _safe_log_failure(correlation_id, category)
        raise InterpreterFailure(category, "Interpreter provider is unavailable.") from None
    except ProviderResponseError:
        category = "invalid_provider_response"
        _safe_log_failure(correlation_id, category)
        raise InterpreterFailure(category, "Interpreter provider returned an invalid response.") from None
    except ProviderError:
        category = "provider_error"
        _safe_log_failure(correlation_id, category)
        raise InterpreterFailure(category, "Interpreter provider request failed.") from None
    except InterpreterParseError:
        category = "malformed_model_output"
        _safe_log_failure(correlation_id, category)
        raise InterpreterFailure(category, "Interpreter returned invalid structured output.") from None
    except InterpreterValidationError:
        category = "invalid_interpretation"
        _safe_log_failure(correlation_id, category)
        raise InterpreterFailure(category, "Interpreter output failed deterministic validation.") from None
    except Exception:
        category = "internal_error"
        _safe_log_failure(correlation_id, category)
        raise InterpreterFailure(category, "Operator notes could not be interpreted safely.") from None
