# GridWise Energy Optimization API

FastAPI service for the BUP CSE Fest 2026 GridWise preliminary. The public API accepts a synthetic 24-hour campus energy scenario and operator notes, then returns the note interpretations and a replay-validated schedule.

## API

- `GET /health` returns `{"status":"ok"}`.
- `POST /optimize-energy` accepts one request matching `contracts/gridwise.openapi.yaml` and returns its documented response shape.

Person 1's shared models are in `app/contracts.py`. The component interfaces are:

```python
async def interpret_notes(*, operator_notes: list[str], battery_capacity_kwh: float) -> list[DirectiveInterpretation]: ...

def optimize_energy(*, request: OptimizeEnergyRequest, directives: list[DirectiveInterpretation]) -> list[HourlyPlanEntry]: ...

def validate_plan(*, request: OptimizeEnergyRequest, directives: list[DirectiveInterpretation], plan: list[HourlyPlanEntry]) -> object: ...
```

The API validates request structure and interpreter output, calls the optimizer, independently calls the replay validator, and calculates response totals from the returned plan. Component errors are returned using the documented safe error envelope. A valid schedule is not returned if replay rejects it.

## Local setup

Requires Python 3.11 or newer and `uv`.

```powershell
uv sync
uv run pytest -q
$env:PORT = "8000"
uv run uvicorn app.main:app --host 0.0.0.0 --port $env:PORT
```

Check readiness:

```powershell
Invoke-RestMethod http://localhost:8000/health
```

Send the first public sample from PowerShell:

```powershell
$pack = Get-Content -Raw .\BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json | ConvertFrom-Json
$body = $pack.cases[0].input | ConvertTo-Json -Depth 30
Invoke-RestMethod -Method Post -Uri http://localhost:8000/optimize-energy -ContentType "application/json" -Body $body
```

## Docker fallback

Build and run the service:

```powershell
docker build -t gridwise-energy:local .
docker run --rm -p 8000:8000 -e PORT=8000 gridwise-energy:local
Invoke-RestMethod http://localhost:8000/health
```

The container binds to `0.0.0.0`, runs as an unprivileged user, and reads its port from `PORT` (default `8000`). Do not bake model credentials into the image.

Environment variables:

| Name | Used by | Default / requirement |
| --- | --- | --- |
| `PORT` | API local/container startup | `8000` |
| `GRIDWISE_RUN_COMPONENT_INTEGRATION` | Opt-in real-component test run only | Leave unset for ordinary offline tests; set to `1` to enable integration tests |
| `GEMINI_API_KEY` | Person 2 interpreter | Required for real requests. Set it in the process environment or ignored project-root `.env` file. |
| `GRIDWISE_LLM_MODEL` | Person 2 interpreter | Optional Gemini model override; defaults to `gemini-3.1-flash-lite`. |
| `GRIDWISE_LLM_ENDPOINT` | Person 2 interpreter | Optional Gemini API endpoint override; defaults to `https://generativelanguage.googleapis.com/v1beta`. |

## Test scope and integration status

`uv run pytest -q` exercises the API using deterministic test doubles, including all ten public fixture cases, request validation, safe failures, response fields, and repeated requests. It does not make live model calls.

After the real interpreter and optimizer are integrated and configured, run the separate end-to-end HTTP checks over all ten cases:

```powershell
$env:GRIDWISE_RUN_COMPONENT_INTEGRATION = "1"
uv run pytest -q -m integration
```

These tests compare the machine-checkable directive fields, independently replay the returned schedules, recalculate all reported metrics, and compare the cost to each public reference. They are opt-in because the normal test command must not make paid model calls.

The API calls the Person 2 interpreter (`app.services.interpreter.interpret_notes`) followed by the Person 3 optimizer and independent replay validator (`app.services.optimizer.optimize_energy` and `app.services.plan_validator.validate_plan`). A production optimization request therefore requires `GEMINI_API_KEY`; a missing or unavailable provider produces a safe `interpretation_failed` response instead of a partial plan.

### Model configuration

The interpreter uses Gemini's `generateContent` endpoint with constrained JSON output. Its default model is `gemini-3.1-flash-lite`; override it with `GRIDWISE_LLM_MODEL` if the judging environment requires another available Gemini model. Calls use an 8-second attempt timeout, at most one retry for transient provider failures, and a 17-second overall interpreter budget inside the API's 30-second deadline. Never commit API keys or `.env` files.

## Known limits

- A request has a 30-second API time limit. The interpreter and solver should use shorter component-level timeouts.
- The API does not provide a frontend, database, live campus data, or control of physical equipment.
- The public fixture tests use reference outputs only as test doubles; they do not establish that the production interpreter or optimizer is accurate or optimal.
