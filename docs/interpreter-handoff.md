# Interpreter configuration and component handoff

## Provider configuration for Person 1

The interpreter uses Google Gemini GenerateContent with model `gemini-3.1-flash-lite`
and the global endpoint `https://generativelanguage.googleapis.com/v1beta`.

| Variable | Required | Default / purpose |
| --- | --- | --- |
| `GEMINI_API_KEY` | Yes | Gemini API credential, loaded from process environment or project `.env` |
| `GRIDWISE_LLM_MODEL` | No | `gemini-3.1-flash-lite` |
| `GRIDWISE_LLM_ENDPOINT` | No | `https://generativelanguage.googleapis.com/v1beta` |

Suggested README wording:

> Set `GEMINI_API_KEY` in the process environment or in the ignored project-root
> `.env` file before starting the API. `GRIDWISE_LLM_MODEL` and
> `GRIDWISE_LLM_ENDPOINT` are optional overrides. The interpreter uses
> `gemini-3.1-flash-lite`, an 8-second per-attempt provider timeout, at most two
> attempts for transient provider failures, and a 17-second interpreter budget
> within the API's 30-second request budget. Provider, timeout, malformed model
> output, and deterministic validation failures raise `InterpreterFailure` with
> a safe category; map these to the API's controlled interpretation error.

No credential, prompt, or provider payload is included in logs or errors.
Failure logs contain only a generated correlation ID and safe category.

## Guarantee for Person 3

`interpret_notes` returns only after `parse_provider_response` has converted
provider JSON into the shared `DirectiveInterpretation` Pydantic union and
`validate_interpretations` has accepted the entire batch. It guarantees:

- Exactly one interpretation per input note, indexed in note order.
- Only the six supported directive types and consistent `applies` semantics.
- Exact per-type adjustment keys; `no_op` has `applies=false` and a null adjustment.
- Non-empty, ascending, unique integer hours from 0 through 23.
- Finite numeric values, solar factors in `[0, 1]`, and reserves not above battery capacity.
- Boolean values are rejected where numbers or hours are required.

The optimizer should receive the returned batch only after the validator
completes successfully. Interpretation does not modify scenario data or produce
a schedule.

## Timeout, retry, and latency verification

The provider adapter retries at most once after transient timeouts, network
errors, HTTP 408/429, or 5xx responses. Authentication and other non-transient
HTTP failures are not retried. Each call is limited to 8 seconds per attempt;
the public interpreter enforces a 17-second total budget.

Live latency has **not** been measured in this checkout because `GEMINI_API_KEY`
is not configured. Do not report offline fake-provider timings as model latency.
After configuring the key, run:

```powershell
uv run python scripts/measure_interpreter_latency.py --calls 20
```

The script prints provider, model, global endpoint region, call count, p50, p95,
and maximum latency. The acceptance target is p95 at or below 5 seconds where
provider conditions permit; every individual interpretation remains bounded
below the API's 30-second request limit.

Offline failure simulations cover timeout, rate limit, malformed provider JSON,
and unavailable provider behavior in `tests/test_llm_provider.py`, with safe
typed interpreter mapping in `tests/test_interpreter.py`. Run them with:

```powershell
uv run pytest -q tests/test_llm_provider.py tests/test_interpreter.py
```
