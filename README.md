# GridWise Energy Optimization API

An LLM-assisted FastAPI service for the BUP CSE Fest 2026 Smart Campus Energy Optimization Challenge. Given a synthetic 24-hour campus scenario and one to three operator notes, it returns a validated interpretation for every note and a cost-minimizing, physically feasible energy plan.

The implementation is designed for reproducibility: a clean checkout, the commands in this document, and a valid Gemini credential are sufficient to run the service and execute the public samples. No live campus, utility, billing, or personal data is used.

## What the service does

`POST /optimize-energy` executes this pipeline:

```text
operator notes
    -> Gemini structured JSON interpretation
    -> deterministic parser and directive validation
    -> SciPy HiGHS linear-program optimizer
    -> independent hour-by-hour replay validation
    -> JSON response
```

The LLM is used only to interpret natural-language notes into the challenge's fixed directive schema. It is not trusted to calculate energy schedules. The optimizer receives only interpretations that pass deterministic validation, and a plan is returned only after a separate validator replays it against the original scenario and directives.

### Supported directives

| Directive | Effect on the 24-hour model |
| --- | --- |
| `solar_reduction` | Reduces usable solar in specified hours by a remaining factor from 0 to 1. |
| `minimum_battery_reserve` | Raises the battery-energy floor for specified hours. |
| `no_charge_window` | Disallows charging during specified hours. |
| `no_discharge_window` | Disallows discharging during specified hours. |
| `max_grid_window` | Caps grid import during specified hours. |
| `no_op` | Marks a note as irrelevant to this schedule. |

Windows are whole-hour, start-inclusive and end-exclusive: 1 PM to 3 PM maps to `[13, 14]`. Every `hours` array is validated as ascending, unique integers from 0 through 23. For overlapping same-type directives, the service takes the conservative constraint: lowest solar factor or grid cap, highest reserve, and the union of prohibited charge/discharge windows.

## Repository layout

| Path | Purpose |
| --- | --- |
| `app/main.py` | FastAPI application and safe HTTP error handling. |
| `app/orchestration.py` | Enforces interpreter -> optimizer -> replay ordering. |
| `app/services/interpreter.py` | Gemini-backed operator-note interpretation. |
| `app/services/interpreter_validation.py` | Deterministic validation of untrusted model output. |
| `app/services/optimizer.py` | SciPy HiGHS linear-program formulation. |
| `app/services/plan_validator.py` | Independent replay of energy, battery, and directive rules. |
| `contracts/gridwise.openapi.yaml` | API contract. |
| `BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json` | Ten public input/expected-output fixtures. |
| `tests/` | Offline, integration, provider, optimizer, and replay tests. |
| `Dockerfile` | Reproducible container fallback. |

## Prerequisites

- Python 3.11 or newer (the Docker image uses Python 3.12)
- [uv](https://docs.astral.sh/uv/) for locked dependency installation
- A Gemini API key with access to the configured model for real API requests
- Docker Desktop or Docker Engine for the container path (optional)

Dependencies are pinned by `uv.lock`. The application uses FastAPI/Uvicorn, Pydantic, SciPy (including the HiGHS solver), and the Python standard library HTTP client for the Gemini REST API.

## Clean local reproduction (PowerShell)

Run the following from a fresh clone. The first block installs exactly the locked dependencies and creates a local, ignored environment file.

```powershell
git clone <YOUR_REPOSITORY_URL>
Set-Location .\smart_campus_energy_optimization_challenge

uv sync --frozen
Copy-Item .env.example .env
notepad .env
```

In `.env`, set only the value of `GEMINI_API_KEY`. Do not commit this file. The application reads the project-root `.env` without overwriting variables already set in the process environment.

```dotenv
GEMINI_API_KEY=your_key_here
```

Start the API in one terminal:

```powershell
$env:PORT = "8000"
uv run uvicorn app.main:app --host 0.0.0.0 --port $env:PORT
```

The service is ready when the following command, run from a second terminal, returns `status : ok`:

```powershell
Invoke-RestMethod http://localhost:8000/health
```

### Run a real public sample

This invokes Gemini and therefore requires the configured credential and may incur provider usage. It sends the first public fixture unchanged and prints a compact result.

```powershell
$pack = Get-Content -Raw .\BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json | ConvertFrom-Json
$body = $pack.cases[0].input | ConvertTo-Json -Depth 30
$response = Invoke-RestMethod -Method Post `
  -Uri http://localhost:8000/optimize-energy `
  -ContentType "application/json" `
  -Body $body

$response | Select-Object scenario_id, total_grid_kwh, total_cost_bdt, peak_grid_kwh, directive_interpretation
```

Expected success criteria: HTTP 200; `scenario_id` is `SAMPLE-01`; there is one `directive_interpretation` item per input note in index order; `hourly_plan` has 24 entries for hours 0 through 23; and all reported totals are calculated from that plan. Exact `plan_summary` text is not part of fixture matching.

To exercise the interactive API documentation locally, open `http://localhost:8000/docs` after the service starts.

## Configuration

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `GEMINI_API_KEY` | Yes for real optimization requests | none | Gemini credential. Keep it in the process environment or ignored `.env`. |
| `GRIDWISE_LLM_MODEL` | No | `gemini-3.1-flash-lite` | Gemini model identifier. |
| `GRIDWISE_LLM_ENDPOINT` | No | `https://generativelanguage.googleapis.com/v1beta` | Gemini REST API base URL. |
| `PORT` | No | `8000` | Uvicorn listening port. |
| `GRIDWISE_RUN_COMPONENT_INTEGRATION` | No | unset | Set to `1` only for real-component public-fixture integration tests. |
| `GRIDWISE_RUN_LIVE_TESTS` | No | unset | Set to `1` only for live Gemini paraphrase tests. |

Provider calls use temperature 0, an 8-second per-attempt timeout, at most two attempts for transient failures, and a 17-second interpreter budget. The public API has a 30-second request budget. Missing credentials, provider failures, malformed provider output, invalid directives, infeasible schedules, and replay failures produce controlled error responses without returning secrets or stack traces.

## Verification

### Offline test suite

This is the standard reproducible check. It makes no paid model calls and uses deterministic test doubles where an API response is needed.

```powershell
uv run pytest -q
```

Verified in this checkout: **171 passed, 17 skipped**. The skipped tests are opt-in real-provider or real-component checks.

### End-to-end public fixtures

With Gemini configured, run all ten public cases through the real interpreter, optimizer, and independent replay validator:

```powershell
$env:GRIDWISE_RUN_COMPONENT_INTEGRATION = "1"
uv run pytest -q -m integration
Remove-Item Env:GRIDWISE_RUN_COMPONENT_INTEGRATION
```

This test compares the machine-checkable interpretation fields with the public expected output, replays each returned plan, recalculates totals, and checks the public reference cost within the challenge tolerance of 0.01 kWh/BDT.

### Optional live paraphrase checks and latency measurement

These commands call Gemini and may incur cost:

```powershell
$env:GRIDWISE_RUN_LIVE_TESTS = "1"
uv run pytest -q -m live_provider tests/test_interpreter_live.py
Remove-Item Env:GRIDWISE_RUN_LIVE_TESTS

uv run python scripts/measure_interpreter_latency.py --calls 20
```

The challenge target is a ready `/health` endpoint within 60 seconds, requests under 30 seconds, and p95 latency at or below 5 seconds for full performance credit. Provider availability, quota, and network conditions are external dependencies; measure them in the intended deployment environment before submission.

## Docker fallback

Build and run the same service without a host Python environment:

```powershell
docker build -t gridwise-energy:local .
docker run --rm -p 8000:8000 --env-file .env gridwise-energy:local
Invoke-RestMethod http://localhost:8000/health
```

The image listens on `0.0.0.0:8000`, runs as a non-root user, and has no credential baked in. For a final submission, replace the local image name with the published immutable tag or digest, for example:

```powershell
docker pull <REGISTRY>/<IMAGE>@sha256:<DIGEST>
docker run --rm -p 8000:8000 --env-file .env <REGISTRY>/<IMAGE>@sha256:<DIGEST>
```

Do not publish a registry image until `/health` and a real public sample have been checked from the pulled image.

## API contract

| Endpoint | Success response |
| --- | --- |
| `GET /health` | `200 {"status":"ok"}` |
| `POST /optimize-energy` | `200` with the scenario ID, one interpretation per note, 24-hour plan, recalculated grid/cost/peak totals, and a short summary. |

The complete request and response schema is in [`contracts/gridwise.openapi.yaml`](contracts/gridwise.openapi.yaml). Requests must contain exactly 24 ordered hourly records (`0` through `23`), one to three non-empty notes, and valid battery bounds. The returned plan must satisfy hourly energy balance, effective-solar bounds, battery transitions and rates, active directive constraints, and end-of-day battery neutrality.

## Security and operational notes

- `.env` is ignored. Never commit API keys, tokens, Docker registry credentials, or provider payloads containing secrets.
- This service needs Gemini availability, quota, and a valid model ID at runtime; the public `/health` endpoint intentionally checks process readiness rather than provider availability.
- There is no database, frontend, persistent state, grid export, or control of physical campus equipment.
- Hidden tests may paraphrase directives. Do not hard-code public phrases or assume public examples cover all wording.
- The submitted endpoint must be reachable without login, VPN, manual approval, or private-network access throughout judging.

## Pre-submission checklist

- [ ] `uv sync --frozen` and `uv run pytest -q` succeed on a clean checkout.
- [ ] A real Gemini-backed public sample returns HTTP 200 and passes replay validation.
- [ ] The hosted service exposes both exact endpoints and is reachable externally.
- [ ] A pulled Docker image starts using the documented command and reaches `/health`.
- [ ] The deployed provider key, quota, and model access are valid for repeated requests.
- [ ] No `.env` or secret is tracked by Git (`git status --ignored` can help verify this).
- [ ] The repository, endpoint, Docker image reference, and required video follow the event's visibility and availability rules.

## Source challenge materials

- `BUP_CSE_FEST_2026_Preliminary_Problem_Statement_GridWise_LLM.pdf` is the canonical source for behavior, constraints, and API semantics.
- `BUP_CSE_FEST_2026_Participant_Guide_&_Evaluation_Rubric_GridWise_LLM.pdf` defines deployment, scoring, submission, and reproducibility expectations.
