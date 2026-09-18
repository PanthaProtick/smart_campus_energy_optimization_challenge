# Person 3 - Energy Optimizer and Independent Replay Validator

## Mission

Implement the deterministic cost-minimizing 24-hour energy schedule and a separate replay validator. The optimizer consumes validated directives; it does not interpret natural language or alter their meaning.

## Source of truth

- Request/response contract: [`../contracts/gridwise.openapi.yaml`](../contracts/gridwise.openapi.yaml)
- Worked input and reference schedules: [`../BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json`](../BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json)
- Challenge rules: the Problem Statement PDF in the repository. The OpenAPI file captures the API contract; the statement remains canonical if they differ.

## Ownership and boundaries

Own:

- `app/services/optimizer.py`: mathematical optimization and schedule creation.
- `app/services/plan_validator.py`: independent replay of a candidate plan against the original scenario and validated directives.
- Optimizer and validator unit tests, solver dependency choice, and a short explanation of the mathematical model for Person 1's README/video.

Do not:

- Parse operator-note text, call an LLM, or change the meaning of a directive.
- Rely on an optimizer's successful status as proof the returned plan is valid. The separate replay validator must check it.
- Use starting battery energy as free energy: final battery energy must equal its initial value.
- Add a database, background scheduler, or real-time control loop; this is a single 24-hour request/response calculation.

## Shared component interface

Import `OptimizeEnergyRequest`, `DirectiveInterpretation`, and `HourlyPlanEntry` from Person 1's `app/contracts.py`. Implement:

```python
def optimize_energy(
    *,
    request: OptimizeEnergyRequest,
    directives: list[DirectiveInterpretation],
) -> list[HourlyPlanEntry]: ...
```

Before returning, replay-validate the plan. Raise a typed, safe internal exception if the scenario is infeasible or the plan fails replay; Person 1 maps it to an API error. Do not return a partial or invalid schedule.

## Mathematical requirements

For each hour `h`:

- `grid[h] + solar_used[h] + discharge[h] = demand[h] + charge[h]`.
- `0 <= solar_used[h] <= effective_solar[h]`; unused solar may be curtailed; exporting is not allowed.
- `E_after[h] = E_before[h] + charge[h] - discharge[h]`.
- `base_minimum <= E_after[h] <= capacity`, raised to an active directive reserve when applicable.
- Charge/discharge magnitudes respect their hourly limits, and at most one action is used in an hour. `idle` has magnitude zero.
- Charge and discharge are both zero inside their respective prohibited windows.
- Grid use stays below every active grid cap for that hour.
- `E_after[23] == initial_energy_kwh` within the challenge tolerance.
- Minimize `sum(grid[h] * tariff[h])` across all 24 hours.

A linear program can use one signed battery delta per hour (positive means charge; negative means discharge) to make simultaneous charge/discharge impossible without binary variables. It must still model grid import, solar used, energy state, all bounds, and final neutrality. If the selected solver has numerical tolerances, replay the rounded response values before returning them.

## Deterministic overlap policy

The statement does not specify how multiple directives of the same type combine when their hours overlap. To unblock implementation, use the intersection/most restrictive rule and document it in code and the README:

- Overlapping solar reductions: use the **minimum remaining solar factor** for that hour.
- Overlapping minimum reserves: use the **maximum** required reserve.
- Overlapping grid caps: use the **minimum** cap.
- No-charge and no-discharge windows: union their hours.
- If both no-charge and no-discharge apply in the same hour, battery action must be idle.

Do not silently multiply solar factors. If organizers clarify a different rule, update the single directive-composition function and its tests.

## Milestones

### Milestone 1: Contract and fixtures kickoff

1. Read the energy-accounting and directive rules in the statement.
2. Import Person 1's shared contract models when available.
3. Build a fixture loader for `cases[*].input` and `cases[*].expected_output`; do not alter the official fixture file.
4. Confirm the signed battery delta interface/model choice with Person 1 and the overlap policy above with both teammates.

**Acceptance criteria**

- Optimizer signature and shared model imports are agreed before implementation.
- The optimizer has no dependency on the LLM/provider.
- Test fixtures can be selected by sample ID and errors identify the case ID.

### Milestone 2: Feasible schedule model

Implement an optimizer that returns exactly one plan entry for each hour, with battery action/magnitude and state-after values. Apply all passed directives before solving.

**Acceptance criteria**

- Correct energy balance, effective solar, battery bounds/rates/transitions, directive effects, grid caps, and end-of-day neutrality.
- Grid import and solar/battery values are non-negative; idle battery magnitude is zero.
- Output order is exactly hours 0 through 23.
- Totals can be recomputed from the plan; optimizer does not trust or copy `expected_output` totals.
- Infeasibility becomes a controlled typed error, not a solver crash or fabricated fallback plan.

### Milestone 3: Independent replay validator

Implement `validate_plan(request, directives, plan)` in a separate module. Recompute effective solar, active reserves/caps/windows, energy states, hourly balance, and final totals directly from the original request and directives.

**Acceptance criteria**

- Reject missing/duplicate/out-of-order hours, negative/non-finite values, invalid action/magnitude pairs, balance failures, solar overuse, battery transition/bound/rate violations, directive violations, and failed neutrality.
- Validate all response totals against recalculated plan totals within absolute tolerance `0.01` (kWh or BDT as appropriate).
- The validator does not call or reuse the optimizer's constraint-building logic; use a separate replay path to reduce common-mode errors.
- Run validation on the exact rounded values that will be serialized in the API response.

### Milestone 4: Optimality and integration verification

Run the optimizer and replay validator over all 10 public cases, including combinations of directives. Compare cost with each public reference and investigate any discrepancy before handoff.

**Acceptance criteria**

- Every public reference case is feasible and replay-valid.
- Every reported cost matches an optimal reference within `0.01` BDT; equivalent schedules are acceptable.
- Add solver-focused tests for zero solar, zero charge/discharge rates, reserve near capacity, no-charge/no-discharge overlap, and grid-cap feasibility/infeasibility.
- Solver completes comfortably within the API budget; target <1 second per scenario locally. The external LLM is expected to dominate request latency.
- Document solver/library, reproducibility notes, overlap policy, and any numerical limitations for Person 1.

## Small, assignable work items

Start in parallel with Persons 1 and 2 after the short contract kickoff. The math and test fixtures can be developed while the API and LLM code are in progress. Use a small local typed input stub only until Person 1 publishes `app/contracts.py`; replace it before integration. Own only the Person 3 files listed in Person 1's task; do not create duplicate shared contracts or edit `pyproject.toml` without coordinating.

### OPT-01 - Normalize validated directives into hourly constraints

- Convert validated directive objects into 24-element effective-solar factors, reserve floors, charge/discharge permissions, and grid caps.
- Apply the documented conservative overlap policy in one function: minimum solar factor, maximum reserve, minimum grid cap, union of windows.
- Do not parse note text or silently reinterpret directive fields.
- **Depends on:** contract kickoff; can begin with typed local fixtures.
- **Done when:** unit tests cover every directive and overlap behavior for relevant hours.

### OPT-02 - Choose and prototype the solver formulation

- Select a maintained solver available through `uv` and suitable for the small deterministic problem; record dependency/version.
- Define variables for grid imports, solar used, signed battery change, and energy after each hour.
- Encode energy balance, bounds, directive constraints, and end-of-day neutrality; minimize total grid cost.
- Avoid separate unconstrained charge/discharge variables unless you also prevent simultaneous charge and discharge.
- **Depends on:** OPT-01.
- **Done when:** a tiny hand-checkable scenario solves to the expected feasible minimum and the model is documented.

### OPT-03 - Convert solver result to API plan entries

- Convert signed battery change into exactly one action: charge, discharge, or idle.
- Create ordered `HourlyPlanEntry` values for hours `0..23`; use the exact serialized precision intended for the API.
- Calculate grid total, cost, and peak from the resulting plan (or expose a helper for Person 1 to call).
- **Depends on:** OPT-02 and Person 1's shared models.
- **Done when:** output round-trips through Pydantic response models and contains no missing/extra hours.

### OPT-04 - Implement the independent replay validator

- Independently recompute effective solar and active constraints from the original request and validated directives.
- Replay energy balance and battery state hour by hour; check action, rate, capacity, reserve, windows, grid caps, and final neutrality.
- Recalculate totals from the exact plan values after rounding.
- **Depends on:** OPT-03; keep validator code separate from solver constraint construction.
- **Done when:** tests mutate a valid plan one field at a time and the validator catches every injected violation.

### OPT-05 - Add numerical handling and controlled solver errors

- Define one tolerance constant aligned with the challenge (`0.01` kWh / BDT) and use it consistently in replay checks.
- Reject NaN/infinite solver results and values materially below zero; normalize only tiny floating residuals before final replay.
- Convert infeasible, unbounded, timeout, and solver-failure statuses to typed internal errors; never return a partial plan.
- **Depends on:** OPT-02 through OPT-04.
- **Done when:** tests cover small numerical residuals and each solver failure category.

### OPT-06 - Validate all ten public reference cases

- Run each case through the optimizer with its expected structured directives (this tests the optimizer independently of LLM accuracy).
- Run each result through the independent replay validator.
- Compare calculated cost with the reference and investigate any mismatch; do not copy reference schedules into production code.
- **Depends on:** OPT-04 and OPT-05.
- **Done when:** all cases replay successfully and cost is within the allowed tolerance of the known optimum.

### OPT-07 - Add edge-case and infeasibility coverage

- Test no solar, solar surplus/curtailment, zero charge/discharge limits, reserve near capacity, high/low tariffs, and windows at hours 0 and 23.
- Test simultaneous no-charge/no-discharge and overlapping caps/reserves/reductions.
- Test infeasible caps or directives and confirm a controlled failure.
- **Depends on:** OPT-01 through OPT-05.
- **Done when:** edge tests pass and the overlap policy is stated in the README and code comments.

### OPT-08 - Benchmark and hand off

- Measure solver time across all public scenarios and one high-magnitude valid scenario.
- Confirm the solver has no network dependency and does not retain mutable state between requests.
- Send Person 1 the dependency, solver command, interface, failure mapping, and benchmark results.
- **Depends on:** OPT-06 and OPT-07.
- **Done when:** optimizer latency is comfortably below one second locally for this 24-hour workload and repeat calls are isolated.

## Required tests

- All public case schedules and costs, checked by the independent validator.
- Boundary hours 0 and 23, reserve windows, solar factor 0 and 1, zero battery rates, no solar, and high/low tariff patterns.
- No simultaneous charge/discharge; no export; curtailment when solar exceeds demand and storage capability.
- Contradictory or infeasible request/directive combinations return a typed failure and never an invalid plan.
- Rounding/precision test: serialized schedule still passes energy and neutrality tolerances.

## Completion handoff

Provide the exact import path/signature, solver and dependency/version, command to run optimizer tests, all 10 public-case cost comparisons, replay-validation results, overlap policy, and any unresolved solver limitations.
