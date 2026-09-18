# Person 2 - LLM Operator-Note Interpreter

## Mission

Implement the required language-model interpretation path for each operator note and deterministic validation of its structured result. The interpreter identifies one supported directive or `no_op` per note; it does not schedule energy or optimize costs.

## Source of truth

- Directive/API contract: [`../contracts/gridwise.openapi.yaml`](../contracts/gridwise.openapi.yaml)
- Public interpretation examples: [`../BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json`](../BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json)
- Challenge rules: the Problem Statement PDF in the repository. The OpenAPI file captures the API contract; the statement remains canonical if they differ.

## Ownership and boundaries

Own:

- `app/services/interpreter.py`: model call and conversion to shared `DirectiveInterpretation` models.
- `app/services/interpreter_validation.py`: deterministic, non-LLM validation of model results against the notes and battery capacity.
- Prompt/model configuration and interpreter unit tests. Keep provider-specific logic behind one small client boundary so a provider can be replaced without changing the API or optimizer.

Do not:

- Return a schedule, calculate grid cost, or mutate the input scenario.
- Use phrase matching as the only interpreter. The generative language model must produce the structured operator-note interpretation used by the optimizer.
- Let the model set demand, solar forecasts, tariff, battery capacity, or any unsupported parameter.
- Trust model JSON merely because it parses; validate type, index mapping, fields, hours, ranges, and semantics deterministically.
- Put API keys in source, fixtures, prompts, logs, tests, or error responses.

## Shared component interface

Import `DirectiveInterpretation` and related discriminated models from Person 1's `app/contracts.py`. Implement:

```python
async def interpret_notes(
    *,
    operator_notes: list[str],
    battery_capacity_kwh: float,
) -> list[DirectiveInterpretation]: ...
```

Return results in note order. Do not change this signature without agreeing with Person 1. The model must have enough context to convert percentage reserves into kWh; use `battery_capacity_kwh` for that conversion. Do not send unnecessary scenario data or secrets to the provider.

## Supported directives

- `solar_reduction`: `{"hours": [...], "factor": number}`; factor is the usable fraction remaining, in `[0,1]`. An 80% reduction means factor `0.2`.
- `minimum_battery_reserve`: `{"hours": [...], "minimum_energy_kwh": number}`; reserve must be finite, non-negative, and no greater than battery capacity.
- `no_charge_window`: `{"hours": [...]}`.
- `no_discharge_window`: `{"hours": [...]}`.
- `max_grid_window`: `{"hours": [...], "max_grid_kwh": number}`; cap must be finite and non-negative.
- `no_op`: `applies=false`, `structured_adjustment=null`.

Every relevant directive has `applies=true`. Every note maps exactly once to an existing `note_index`. Each hours list has unique integer hours in ascending order from 0 through 23. Time intervals are start-inclusive and end-exclusive: 1 PM to 3 PM is `[13,14]`.

## Milestones

### Milestone 1: Contract and test-data kickoff

1. Read the OpenAPI directive schemas and the interpretation sections of the statement.
2. Import the shared Pydantic models as soon as Person 1 publishes `app/contracts.py`.
3. Extract the expected directive meaning for all 10 public cases into tests or a test report; do not copy sample wording into production rules.
4. Agree on the interpreter function signature and configuration names with Person 1.

**Acceptance criteria**

- The shared result model and function signature match Person 1's API orchestration.
- Tests cover all supported directive types, irrelevant notes, paraphrases, and percentage reserve conversion.
- Model/provider choice, model identifier, environment-variable names, timeout, and fallback behavior are documented for Person 1's README.

### Milestone 2: Structured LLM interpretation

Implement a constrained model request that returns structured fields for each note. Prefer provider-supported structured output/JSON schema; still parse and validate the returned data locally. The prompt should direct the model to classify each note independently, preserve the note order, use only the six allowed types, and avoid inventing values not supported by the note and allowed context.

**Acceptance criteria**

- A real language-capable generative model is invoked on the production interpretation path.
- Exactly one result is returned for each input note, including distractors.
- Interpretation output includes the required fields and exact adjustment shape for the selected type.
- Notes expressing the same rule with different wording map to the same type, hours, and numeric values.
- No model prose or Markdown fences are required to parse the structured result.

### Milestone 3: Deterministic guardrails and failure handling

Validate the entire model response before returning any directives to the API. Reject malformed/unsupported output instead of silently coercing it into another directive. Keep validation independent of model/prompt logic.

**Acceptance criteria**

- Enforce count, `note_index` uniqueness/order/range, allowed directive enum, exact adjustment fields, `applies` semantics, hours range/uniqueness/order, finite numeric values, solar factor `[0,1]`, and reserve `<= battery_capacity_kwh`.
- Reject empty hours for an applicable directive, wrong JSON types, booleans where numeric values are expected, extra fields, and inconsistent `no_op` data.
- Provider timeout, rate limit, malformed output, and provider outage become typed internal failures that Person 1 can map to safe API errors.
- Do not retry non-idempotent or failed requests indefinitely. Use at most one bounded retry only for clearly transient provider failures, with a total interpreter budget that keeps the HTTP request below 30 seconds.
- Logs contain request correlation ID and safe error category only; do not log credentials or full prompts/provider payloads.

### Milestone 4: Accuracy, latency, and handoff

Run the interpreter against expected interpretations for all public cases and an additional paraphrase set. Tune prompt/structured-output settings while keeping deterministic validation strict.

**Acceptance criteria**

- All 10 public case interpretations match their expected machine-checkable fields; explanation wording may differ.
- Add paraphrase tests for each directive family, including 12-hour clock, 24-hour clock, numeric percentages, and distractors.
- Measure latency over repeated calls; target p95 at or below 5 seconds and never exceed the 30-second API timeout under configured limits.
- Include a local test mode/mock only for development. It must be impossible to mistake that mock for the required production LLM path.
- Provide Person 1 with environment-variable names, model/provider identifier, timeout, and documented failure modes.

## Small, assignable work items

Start in parallel with Person 1 after the short contract kickoff. The provider adapter and prompt can be developed while Person 1 builds the shared models. Integrate typed outputs once `app/contracts.py` is ready. Own only the Person 2 files listed in Person 1's task; do not create duplicate shared contracts or edit `pyproject.toml` without coordinating.

### LLM-01 - Define configuration and provider boundary

- Select one language-capable model/provider that is reachable during the judging window.
- Read credentials only from environment variables; fail startup or a request with a safe, clear error when configuration is missing.
- Keep provider SDK use behind a small adapter so interpretation logic is not tied to SDK response classes.
- Configure connect/read timeouts and bounded retries; cap total interpreter time below the API's 30-second limit.
- **Depends on:** contract kickoff; can start before Person 1 completes Pydantic models.
- **Done when:** a local real-provider call works using environment configuration, and no credential is present in source or logs.

### LLM-02 - Define the model-facing structured schema and prompt

- Ask the model for one interpretation per note, in note-index order, using only the six supported types.
- Include only necessary context: notes and battery capacity, so percentage reserves can be converted to kWh.
- Require exact adjustment keys and whole-hour arrays; explain start-inclusive/end-exclusive time windows.
- Tell the model not to alter demand, solar forecasts, tariffs, or battery limits.
- **Depends on:** LLM-01 and agreed shared contract names.
- **Done when:** saved representative model outputs parse as JSON and conform to the same public directive shapes.

### LLM-03 - Implement provider response parsing

- Parse the provider's structured output without depending on Markdown formatting.
- Convert provider output to shared Pydantic directive models; do not return provider-specific objects.
- Reject truncated, empty, extra, or malformed output with a typed interpreter exception.
- **Depends on:** LLM-02 and Person 1's `app/contracts.py`.
- **Done when:** parser tests cover valid output plus malformed JSON, wrong root type, unknown keys, and invalid union variants.

### LLM-04 - Implement deterministic semantic guardrails

- Check exactly one result for every note; each note index appears once and in order.
- Check `applies`, directive type, exact adjustment keys, sorted unique valid hours, numeric ranges, and reserve not above battery capacity.
- Reject empty hour arrays for applicable directives and booleans where numeric fields are required.
- Keep semantic checks separate from the LLM call so they can be tested without provider access.
- **Depends on:** LLM-03.
- **Done when:** every invalid-result condition has a deterministic unit test and no invalid directive can reach Person 3.

### LLM-05 - Implement public interpreter function and failure behavior

- Implement the agreed `interpret_notes` signature and return only shared `DirectiveInterpretation` objects.
- Add a single bounded transient retry only if the provider error is retryable; never loop indefinitely.
- Use safe logs with correlation ID/category, not full prompt/provider payload or credentials.
- **Depends on:** LLM-03 and LLM-04.
- **Done when:** Person 1 can import and call the function; timeout/outage errors map to typed exceptions with no secret-bearing message.

### LLM-06 - Build golden and paraphrase tests

- Compare interpretation fields against the expected public-case meanings, not explanation strings.
- Add paraphrases for directive families, percentage forms, AM/PM and 24-hour times, and distractors.
- Keep live-provider integration tests separately marked so ordinary tests do not require a paid API call.
- **Depends on:** LLM-05.
- **Done when:** offline tests run reliably with `uv run pytest -q`, and a separate documented command runs live provider checks.

### LLM-07 - Measure latency and hand off configuration

- Run repeated calls on a representative note set; record p50 and p95 and note model/provider region/configuration.
- Confirm service behavior under timeout, rate limit, malformed model output, and unavailable provider.
- Send Person 1 exact environment-variable names and README wording; send Person 3 the guarantees on directive validation.
- **Depends on:** LLM-05; final after LLM-06.
- **Done when:** interpreter meets the target p95 where provider conditions permit, stays under the request timeout, and its limitations are documented.

## Required tests

- Public sample interpretation semantics for all notes in cases 1-10.
- Start-inclusive/end-exclusive time conversion across midnight-free intervals, AM/PM wording, and 24-hour clock wording.
- Solar phrase ambiguity: “drop to 20%” means factor `0.2`; “reduce by 80%” also means factor `0.2`.
- Percentage reserve conversion using supplied battery capacity.
- Every invalid result listed under LLM-04, including an unsupported directive and duplicate note index.
- Simulated timeout, rate limit, malformed model response, and unavailable provider.

## Completion handoff

Provide the import path, exact public function signature, model/provider and environment-variable names, test command, test results, measured p50/p95 latency, and any interpretation cases that remain uncertain. Do not claim success based only on a mock model.
