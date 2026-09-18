# Person 1 - API, Contracts, and Integration

## Mission

Deliver the runnable HTTP service that connects the note interpreter and energy optimizer. Own the external API, shared Python contracts, request validation, orchestration, service startup, and end-to-end integration. Do not implement LLM interpretation or optimization algorithms; consume the interfaces owned by Persons 2 and 3.

## Source of truth

- API contract: [`../contracts/gridwise.openapi.yaml`](../contracts/gridwise.openapi.yaml)
- Public fixtures: [`../BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json`](../BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json)
- Challenge rules: the Problem Statement PDF in the repository. The OpenAPI file captures the API contract; the statement remains canonical if they differ.

## Ownership and boundaries

Own:

- `pyproject.toml` and the `uv` development/test commands.
- `app/main.py`: FastAPI application and the two required routes.
- `app/contracts.py`: Pydantic models corresponding to the OpenAPI schemas. This is the shared Python model module; Persons 2 and 3 import these models rather than declaring duplicates.
- `app/orchestration.py`: request-to-interpreter-to-optimizer flow and response assembly.
- API-level error mapping, service configuration, Dockerfile, and the API/integration tests.

Do not:

- Implement phrase matching or an LLM prompt as a substitute for Person 2.
- Implement scheduling math as a substitute for Person 3.
- Add a database, frontend, authentication, or user-facing endpoints. The judge only calls `GET /health` and `POST /optimize-energy`.
- Change the external contract without agreement from Persons 2 and 3 and an update to the OpenAPI file.

## Shared component interfaces

Create `app/contracts.py` first and tell the other two people when it is ready. Use strict Pydantic models with forbidden extra fields and a discriminated union for directive interpretations.

Person 2 will provide:

```python
async def interpret_notes(
    *,
    operator_notes: list[str],
    battery_capacity_kwh: float,
) -> list[DirectiveInterpretation]: ...
```

Person 3 will provide:

```python
def optimize_energy(
    *,
    request: OptimizeEnergyRequest,
    directives: list[DirectiveInterpretation],
) -> list[HourlyPlanEntry]: ...
```

The API calls these functions; it must not depend on their private helper functions. If Person 3 chooses an async solver interface, agree on that change before integration.

## Milestones

### Milestone 1: Contract and project kickoff

1. Read the OpenAPI file and the challenge statement.
2. Agree with both teammates on the Python function signatures above and the shared `app/contracts.py` model names.
3. Create the minimal `uv` project and test command. Preserve the provided sample JSON as immutable fixture input; do not rewrite its expected outputs.
4. Publish the module/file paths and function signatures to the team before parallel implementation begins.

**Acceptance criteria**

- All three people agree on the shared model and function names.
- `uv sync` succeeds from a clean environment.
- `uv run pytest -q` is the documented test command, even if the initial suite has only a smoke test.

### Milestone 2: API skeleton and contracts

Implement the Pydantic request, response, error, directive, and plan models from OpenAPI. Add `GET /health` and a `POST /optimize-energy` route that initially uses local fake component functions or clearly marked temporary stubs. Validate the complete request before invoking either component.

**Acceptance criteria**

- `GET /health` returns HTTP 200 and exactly `{"status":"ok"}`.
- `POST /optimize-energy` accepts the contract shape and serializes only the documented response fields.
- Reject malformed JSON with HTTP 400 and the documented `ErrorResponse` shape.
- Reject well-formed but invalid requests with HTTP 422 and the documented `ErrorResponse` shape.
- Validate exactly 24 ordered input hours numbered 0 through 23, 1-3 non-empty notes, no extra object fields, finite non-negative numeric values, and battery cross-field bounds.
- Add `app/contracts.py` early enough for Persons 2 and 3 to import it; avoid parallel, incompatible copies of the models.

### Milestone 3: Orchestration and controlled failures

Replace temporary stubs with the two owned component interfaces. Call the interpreter first, validate its output against note count/index/order and directive semantics, then call the optimizer. Return a success response only after the optimizer/replay component accepts the schedule.

**Acceptance criteria**

- Interpreter output contains exactly one valid entry per input note and indices are `0..N-1` in order.
- Optimizer receives the original validated request and only validated directives.
- Response `scenario_id` exactly matches the request.
- Component failures map to safe documented error responses; no tracebacks, prompts containing secrets, API keys, or provider details are returned.
- No valid request triggers a 500 because of a serialization or orchestration defect.

### Milestone 4: End-to-end verification and deployable service

Add tests that send every public fixture through the HTTP route after the real components are integrated. Add the Dockerfile and concise startup/run instructions. The API process must bind to `0.0.0.0` and use a configurable port.

**Acceptance criteria**

- `uv run pytest -q` passes all API, contract, and integration tests.
- All 10 public cases return a response matching the contract; interpretation and schedule assertions are performed by the test suite, not just status-code checks.
- Container starts using the README command and `GET /health` succeeds from outside the container.
- No secret is committed or baked into the image; configuration is read from environment variables.
- Per-request processing has a 30-second upper timeout, with provider/solver timeouts configured below it so the service can return a controlled response.

## Small, assignable work items

Complete these in order unless a dependency is explicitly marked as parallel.

### API-01 - Bootstrap the Python project

- Create `pyproject.toml` with FastAPI, Uvicorn, Pydantic v2, pytest, and an HTTP test client.
- Add `.gitignore` entries for virtual environments, caches, `.env`, and generated artifacts.
- Create this shared layout; keep one owner per implementation file:

  ```text
  app/
    __init__.py
    contracts.py
    main.py
    orchestration.py
    services/
      __init__.py
      interpreter.py
      interpreter_validation.py
      optimizer.py
      plan_validator.py
  tests/
    test_api.py
    test_interpreter.py
    test_optimizer.py
  ```

- Person 1 owns `pyproject.toml`, `app/contracts.py`, `app/main.py`, `app/orchestration.py`, and `tests/test_api.py`.
- Person 2 owns `app/services/interpreter.py`, `app/services/interpreter_validation.py`, and `tests/test_interpreter.py`.
- Person 3 owns `app/services/optimizer.py`, `app/services/plan_validator.py`, and `tests/test_optimizer.py`.
- Tell both teammates when the directories and `app/contracts.py` are ready. Do not wait for the LLM or optimizer implementation before starting API work.
- **Depends on:** none.
- **Done when:** a clean `uv sync` succeeds and `uv run pytest -q` runs successfully.

### API-02 - Implement public request and response models

- Translate the OpenAPI schemas into strict Pydantic models in `app/contracts.py`.
- Use a discriminated union for the six interpretation variants; forbid unknown fields.
- Add input and output field constraints, including finite non-negative numbers.
- Keep cross-object and ordered-list rules for API-03 rather than hiding them in route code.
- **Depends on:** API-01.
- **Done when:** valid and invalid model fixtures behave as the OpenAPI schema specifies.

### API-03 - Add cross-field and sequence validation

- Validate exactly 24 input hours in order `0..23`, 1–3 non-empty notes, and valid battery relationships.
- Validate interpretation count/order and `applies` / `no_op` semantics before optimizer invocation.
- Keep validation functions small and unit-testable.
- **Depends on:** API-02.
- **Done when:** tests prove duplicate, missing, out-of-order, or contradictory fields are rejected before component calls.

### API-04 - Add FastAPI route and error envelope

- Add `GET /health` and `POST /optimize-energy` in `app/main.py`.
- Map malformed JSON to HTTP 400; map structurally or semantically invalid requests to HTTP 422.
- Define safe error mapping for interpreter, optimizer, and unexpected failures using `ErrorResponse`.
- Start with temporary component stubs if Persons 2 and 3 are still working; mark stubs clearly as development-only.
- **Depends on:** API-01 and API-02; can proceed while API-03 is being completed.
- **Done when:** API tests assert status, content type, exact field names, and that internal exception text is absent.

### API-05 - Wire orchestration to component interfaces

- Add `app/orchestration.py` to call interpreter, validate the result, call optimizer, and assemble the response.
- Ensure original request data is not mutated while directives are applied.
- Ensure a failed interpreter never reaches the optimizer and a failed replay never returns HTTP 200.
- **Depends on:** API-03 and both teammate interfaces.
- **Done when:** spy/fake component tests prove call order, arguments, and failure short-circuiting.

### API-06 - Add public-case API regression tests

- Load the provided JSON fixture without copying or changing its expected outputs.
- Parameterize an API test over all ten case inputs.
- Assert the response contract, scenario ID, interpretation coverage/order, 24 plan hours, and independently checked totals/constraints.
- **Depends on:** API-05 and integrated components.
- **Done when:** all ten cases pass through HTTP, not only direct function calls.

### API-07 - Containerize and document local execution

- Add Dockerfile and `.dockerignore`; run as a non-root user if the chosen base image makes that practical.
- Bind Uvicorn to `0.0.0.0` and honor a configurable port.
- Document `uv` setup, environment-variable names, tests, sample request command, and Docker run command. Never include secret values.
- **Depends on:** API-04; finalize after API-06.
- **Done when:** a clean container build starts and `/health` is reachable from the host.

## Required tests

- Valid health request.
- One valid optimization request and one request for each public sample case.
- Missing field, extra field, invalid note count, invalid hour count, duplicate/out-of-order hour, negative/non-finite input, and invalid battery bounds.
- Interpreter returns missing, duplicate, out-of-order, `no_op` with `applies=true`, or a wrong adjustment shape.
- Interpreter and optimizer raise controlled errors; assert safe HTTP responses with no internal exception text.
- Repeated identical request; confirm no cross-request state leaks between scenarios.

## Completion handoff

Report the exact local run/test commands, Docker run command, required environment-variable names, and any remaining contract deviations. Do not claim the hosted endpoint is ready until it has been tested from outside the development environment.
