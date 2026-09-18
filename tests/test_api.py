"""HTTP and orchestration tests for Person 1's API layer."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main, orchestration
from app.contracts import (
    DirectiveInterpretation,
    HourlyPlanEntry,
    OptimizeEnergyRequest,
)
from pydantic import TypeAdapter


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
FIXTURES = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["cases"]
RUN_COMPONENT_INTEGRATION = os.getenv("GRIDWISE_RUN_COMPONENT_INTEGRATION") == "1"
DIRECTIVE_ADAPTER = TypeAdapter(DirectiveInterpretation)


def _replay_fixture(request, directives, plan):
    """Independent test replay for fixture plans, matching the public rules."""
    assert [item.hour for item in plan] == list(range(24))
    by_hour = {hour: {} for hour in range(24)}
    for directive in directives:
        if directive.directive_type == "no_op":
            continue
        for hour in directive.structured_adjustment.hours:
            by_hour[hour].setdefault(directive.directive_type, []).append(
                directive.structured_adjustment
            )
    energy = request.battery.initial_energy_kwh
    for row in plan:
        inputs = request.hours[row.hour]
        solar_factor = min(
            (adjustment.factor for adjustment in by_hour[row.hour].get("solar_reduction", [])),
            default=1.0,
        )
        effective_solar = inputs.solar_kwh * solar_factor
        charge = row.battery_kwh if row.battery_action == "charge" else 0.0
        discharge = row.battery_kwh if row.battery_action == "discharge" else 0.0
        assert row.solar_used_kwh <= effective_solar + 0.01
        assert row.grid_kwh + row.solar_used_kwh + discharge == pytest.approx(
            inputs.demand_kwh + charge, abs=0.01
        )
        expected_energy = energy + charge - discharge
        assert row.battery_energy_after_kwh == pytest.approx(expected_energy, abs=0.01)
        assert row.battery_energy_after_kwh <= request.battery.capacity_kwh + 0.01
        reserve = max(
            [request.battery.minimum_energy_kwh]
            + [
                adjustment.minimum_energy_kwh
                for adjustment in by_hour[row.hour].get("minimum_battery_reserve", [])
            ]
        )
        assert row.battery_energy_after_kwh >= reserve - 0.01
        assert charge <= request.battery.max_charge_kwh_per_hour + 0.01
        assert discharge <= request.battery.max_discharge_kwh_per_hour + 0.01
        assert not (charge and "no_charge_window" in by_hour[row.hour])
        assert not (discharge and "no_discharge_window" in by_hour[row.hour])
        for adjustment in by_hour[row.hour].get("max_grid_window", []):
            assert row.grid_kwh <= adjustment.max_grid_kwh + 0.01
        energy = row.battery_energy_after_kwh
    assert energy == pytest.approx(request.battery.initial_energy_kwh, abs=0.01)
    return True


def _install_fixture_components(monkeypatch, *, broken=None):
    """Use public expected values as deterministic component fakes for API tests."""
    fixtures = {item["input"]["scenario_id"]: item for item in FIXTURES}
    calls = {"optimizer": 0, "replay": 0}

    async def interpret_notes(*, operator_notes, battery_capacity_kwh):
        case = next(
            entry
            for entry in FIXTURES
            if entry["input"]["operator_notes"] == operator_notes
        )
        if broken == "bad_interpretation":
            return []
        return case["expected_output"]["directive_interpretation"]

    def optimize_energy(*, request: OptimizeEnergyRequest, directives):
        calls["optimizer"] += 1
        if broken == "optimizer_error":
            raise RuntimeError("private solver details")
        expected = fixtures[request.scenario_id]["expected_output"]["hourly_plan"]
        return [HourlyPlanEntry.model_validate(entry) for entry in expected]

    def validate_plan(*, request, directives, plan):
        calls["replay"] += 1
        if broken == "replay_failure":
            return False
        return _replay_fixture(request, directives, plan)

    monkeypatch.setattr(
        orchestration,
        "_load_components",
        lambda: (interpret_notes, optimize_energy, validate_plan),
    )
    return calls


def _client(monkeypatch, *, broken=None):
    calls = _install_fixture_components(monkeypatch, broken=broken)
    return TestClient(main.app), calls


def test_health_is_exactly_the_required_readiness_response():
    with TestClient(main.app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("case", FIXTURES, ids=lambda case: case["id"])
def test_all_public_cases_through_http_contract(case, monkeypatch):
    client, calls = _client(monkeypatch)
    response = client.post("/optimize-energy", json=case["input"])
    assert response.status_code == 200, response.text
    data = response.json()
    expected = case["expected_output"]
    assert set(data) == {
        "scenario_id",
        "directive_interpretation",
        "hourly_plan",
        "total_grid_kwh",
        "total_cost_bdt",
        "peak_grid_kwh",
        "plan_summary",
    }
    assert data["scenario_id"] == case["input"]["scenario_id"]
    assert data["directive_interpretation"] == expected["directive_interpretation"]
    assert [row["hour"] for row in data["hourly_plan"]] == list(range(24))
    assert data["total_grid_kwh"] == pytest.approx(expected["total_grid_kwh"], abs=0.01)
    assert data["total_cost_bdt"] == pytest.approx(expected["total_cost_bdt"], abs=0.01)
    assert data["peak_grid_kwh"] == pytest.approx(expected["peak_grid_kwh"], abs=0.01)
    assert data["plan_summary"].strip()
    assert calls == {"optimizer": 1, "replay": 1}


@pytest.mark.integration
@pytest.mark.skipif(
    not RUN_COMPONENT_INTEGRATION,
    reason="Set GRIDWISE_RUN_COMPONENT_INTEGRATION=1 when the real interpreter and optimizer are configured.",
)
@pytest.mark.parametrize("case", FIXTURES, ids=lambda case: case["id"])
def test_all_public_cases_through_http_with_real_components(case):
    """Opt-in end-to-end regression using the real model interpreter and solver."""
    try:
        orchestration._load_components()
    except orchestration.PipelineFailure as exc:
        pytest.fail(f"Real component integration was requested but is unavailable: {exc.public_message}")

    with TestClient(main.app) as client:
        response = client.post("/optimize-energy", json=case["input"])
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    data = response.json()
    expected = case["expected_output"]
    assert set(data) == {
        "scenario_id",
        "directive_interpretation",
        "hourly_plan",
        "total_grid_kwh",
        "total_cost_bdt",
        "peak_grid_kwh",
        "plan_summary",
    }
    assert data["scenario_id"] == case["input"]["scenario_id"]
    assert len(data["directive_interpretation"]) == len(case["input"]["operator_notes"])
    assert [item["note_index"] for item in data["directive_interpretation"]] == list(
        range(len(case["input"]["operator_notes"]))
    )
    for actual, reference in zip(data["directive_interpretation"], expected["directive_interpretation"]):
        for field in ("note_index", "applies", "directive_type", "structured_adjustment"):
            assert actual[field] == reference[field]

    request = OptimizeEnergyRequest.model_validate(case["input"])
    directives = [DIRECTIVE_ADAPTER.validate_python(item) for item in data["directive_interpretation"]]
    plan = [HourlyPlanEntry.model_validate(item) for item in data["hourly_plan"]]
    assert [item.hour for item in plan] == list(range(24))
    _replay_fixture(request, directives, plan)

    total_grid = sum(item.grid_kwh for item in plan)
    total_cost = sum(item.grid_kwh * request.hours[item.hour].tariff_bdt_per_kwh for item in plan)
    peak_grid = max(item.grid_kwh for item in plan)
    assert data["total_grid_kwh"] == pytest.approx(total_grid, abs=0.01)
    assert data["total_cost_bdt"] == pytest.approx(total_cost, abs=0.01)
    assert data["peak_grid_kwh"] == pytest.approx(peak_grid, abs=0.01)
    assert total_cost == pytest.approx(expected["total_cost_bdt"], abs=0.01)


def test_malformed_json_returns_documented_400_envelope():
    with TestClient(main.app) as client:
        response = client.post("/optimize-energy", content="{", headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert response.json() == {
        "error": {"code": "malformed_json", "message": "Request body must be valid JSON."}
    }


def _invalid_requests():
    base = FIXTURES[0]["input"]
    yield "missing_field", {key: value for key, value in base.items() if key != "battery"}
    extra = json.loads(json.dumps(base))
    extra["surprise"] = "not allowed"
    yield "extra_field", extra
    notes = json.loads(json.dumps(base))
    notes["operator_notes"] = []
    yield "empty_notes", notes
    notes = json.loads(json.dumps(base))
    notes["operator_notes"] = ["a", "b", "c", "d"]
    yield "too_many_notes", notes
    hours = json.loads(json.dumps(base))
    hours["hours"].pop()
    yield "wrong_hour_count", hours
    hours = json.loads(json.dumps(base))
    hours["hours"][1]["hour"] = 0
    yield "duplicate_hour", hours
    hours = json.loads(json.dumps(base))
    hours["hours"][0]["demand_kwh"] = -1
    yield "negative_number", hours
    hours = json.loads(json.dumps(base))
    hours["hours"][0]["demand_kwh"] = float("nan")
    yield "non_finite_number", hours
    battery = json.loads(json.dumps(base))
    battery["battery"]["initial_energy_kwh"] = battery["battery"]["capacity_kwh"] + 1
    yield "battery_over_capacity", battery
    battery = json.loads(json.dumps(base))
    battery["battery"]["minimum_energy_kwh"] = battery["battery"]["initial_energy_kwh"] + 1
    yield "reserve_above_initial", battery


@pytest.mark.parametrize("label,payload", list(_invalid_requests()), ids=lambda value: value if isinstance(value, str) else None)
def test_invalid_request_returns_controlled_422(label, payload):
    with TestClient(main.app) as client:
        if label == "non_finite_number":
            response = client.post(
                "/optimize-energy",
                content=json.dumps(payload, allow_nan=True),
                headers={"content-type": "application/json"},
            )
        else:
            response = client.post("/optimize-energy", json=payload)
    assert response.status_code == 422, label
    assert response.json()["error"]["code"] == "invalid_request"
    assert set(response.json()["error"]) == {"code", "message"}


@pytest.mark.parametrize(
    "broken,expected_code",
    [
        ("bad_interpretation", "interpretation_failed"),
        ("optimizer_error", "optimization_failed"),
        ("replay_failure", "optimization_failed"),
    ],
)
def test_component_failures_are_safe_and_do_not_leak_details(broken, expected_code, monkeypatch):
    client, calls = _client(monkeypatch, broken=broken)
    response = client.post("/optimize-energy", json=FIXTURES[0]["input"])
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == expected_code
    assert "private solver details" not in response.text
    if broken == "bad_interpretation":
        assert calls == {"optimizer": 0, "replay": 0}
    if broken == "replay_failure":
        assert calls == {"optimizer": 1, "replay": 1}


def test_repeated_request_has_no_cross_scenario_state(monkeypatch):
    client, calls = _client(monkeypatch)
    first = client.post("/optimize-energy", json=FIXTURES[0]["input"])
    second = client.post("/optimize-energy", json=FIXTURES[1]["input"])
    assert first.status_code == second.status_code == 200
    assert first.json()["scenario_id"] == FIXTURES[0]["input"]["scenario_id"]
    assert second.json()["scenario_id"] == FIXTURES[1]["input"]["scenario_id"]
    assert calls == {"optimizer": 2, "replay": 2}


def test_invalid_interpretation_shape_is_rejected_before_optimizer(monkeypatch):
    client, calls = _client(monkeypatch, broken="bad_interpretation")
    response = client.post("/optimize-energy", json=FIXTURES[0]["input"])
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "interpretation_failed"
    assert calls["optimizer"] == 0


@pytest.mark.parametrize("mutation", ["duplicate", "out_of_order", "no_op_applies", "wrong_adjustment"])
def test_invalid_interpreter_variants_never_reach_optimizer(monkeypatch, mutation):
    source = FIXTURES[0]["expected_output"]["directive_interpretation"]
    values = json.loads(json.dumps(source))
    if mutation == "duplicate":
        values[1]["note_index"] = values[0]["note_index"]
    elif mutation == "out_of_order":
        values.reverse()
    elif mutation == "no_op_applies":
        values[1]["applies"] = True
    else:
        values[0]["structured_adjustment"]["unexpected"] = "extra"
    calls = {"optimizer": 0}

    async def interpreter(**_kwargs):
        return values

    def optimizer(**_kwargs):
        calls["optimizer"] += 1
        raise AssertionError("optimizer must not receive invalid interpretations")

    def replay_validator(**_kwargs):
        raise AssertionError("replay must not be called")

    monkeypatch.setattr(
        orchestration,
        "_load_components",
        lambda: (interpreter, optimizer, replay_validator),
    )
    with TestClient(main.app) as client:
        response = client.post("/optimize-energy", json=FIXTURES[0]["input"])
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "interpretation_failed"
    assert calls["optimizer"] == 0


def test_interpreter_exception_is_mapped_without_exposing_provider_details(monkeypatch):
    async def interpreter(**_kwargs):
        raise RuntimeError("secret=do-not-return")

    def optimizer(**_kwargs):
        raise AssertionError("optimizer must not be called")

    def replay_validator(**_kwargs):
        raise AssertionError("replay must not be called")

    monkeypatch.setattr(
        orchestration,
        "_load_components",
        lambda: (interpreter, optimizer, replay_validator),
    )
    with TestClient(main.app) as client:
        response = client.post("/optimize-energy", json=FIXTURES[0]["input"])
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "interpretation_failed"
    assert "do-not-return" not in response.text


def test_orchestration_order_arguments_and_request_immutability():
    case = FIXTURES[0]
    request = OptimizeEnergyRequest.model_validate(case["input"])
    original_demand = request.hours[0].demand_kwh
    original_interpretations = case["expected_output"]["directive_interpretation"]
    events = []

    async def interpreter(*, operator_notes, battery_capacity_kwh):
        events.append(("interpreter", operator_notes, battery_capacity_kwh))
        assert operator_notes == case["input"]["operator_notes"]
        assert battery_capacity_kwh == request.battery.capacity_kwh
        return original_interpretations

    def optimizer(*, request: OptimizeEnergyRequest, directives):
        events.append(("optimizer", request, directives))
        assert request is not request_original
        assert request.scenario_id == request_original.scenario_id
        assert request.hours[0].demand_kwh == original_demand
        assert directives[0].directive_type == "solar_reduction"
        request.hours[0].demand_kwh = 999999.0
        directives[0].structured_adjustment.factor = 0.0
        return case["expected_output"]["hourly_plan"]

    def replay_validator(*, request: OptimizeEnergyRequest, directives, plan):
        events.append(("replay", request, directives, plan))
        assert request is not request_original
        assert request.hours[0].demand_kwh == original_demand
        assert directives[0].structured_adjustment.factor == original_interpretations[0]["structured_adjustment"]["factor"]
        assert [entry.hour for entry in plan] == list(range(24))
        return True

    request_original = request
    response = asyncio.run(
        orchestration.process_request(
            request,
            components=(interpreter, optimizer, replay_validator),
        )
    )
    assert [event[0] for event in events] == ["interpreter", "optimizer", "replay"]
    assert request.hours[0].demand_kwh == original_demand
    assert response.directive_interpretation[0].structured_adjustment.factor == original_interpretations[0]["structured_adjustment"]["factor"]
    assert response.total_cost_bdt == pytest.approx(case["expected_output"]["total_cost_bdt"], abs=0.01)
