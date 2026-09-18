"""Opt-in Gemini paraphrase checks; never make paid calls in the default suite."""

from __future__ import annotations

import asyncio
import os
import sys
import types

import pytest
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt

import app.services.interpreter as interpreter
from app.services.llm_provider import _load_project_dotenv


class _LiveInterpretation(BaseModel):
    """Local test adapter until the shared production models are published."""

    model_config = ConfigDict(extra="forbid", strict=True)
    note_index: StrictInt
    applies: StrictBool
    directive_type: str
    structured_adjustment: dict | None
    explanation: str


CASES = [
    pytest.param(
        "Solar panels will deliver only 20% of forecast from 13:00 to 15:00.",
        500,
        "solar_reduction",
        {"hours": [13, 14], "factor": 0.2},
        id="solar-drop-to-percent-24-hour",
    ),
    pytest.param(
        "Reduce usable solar by 80 percent between 1 PM and 3 PM.",
        500,
        "solar_reduction",
        {"hours": [13, 14], "factor": 0.2},
        id="solar-reduction-by-percent-am-pm",
    ),
    pytest.param(
        "Maintain a 50% battery reserve from 6 PM until 9 PM.",
        200,
        "minimum_battery_reserve",
        {"hours": [18, 19, 20], "minimum_energy_kwh": 100.0},
        id="percentage-reserve-to-kwh",
    ),
    pytest.param(
        "No charging from 09:00 until 11:00.",
        200,
        "no_charge_window",
        {"hours": [9, 10]},
        id="no-charge-24-hour",
    ),
    pytest.param(
        "Please keep the battery from discharging between 6 PM and 8 PM.",
        200,
        "no_discharge_window",
        {"hours": [18, 19]},
        id="no-discharge-am-pm",
    ),
    pytest.param(
        "Keep grid imports at or below 100 kWh from 19:00 to 21:00.",
        200,
        "max_grid_window",
        {"hours": [19, 20], "max_grid_kwh": 100.0},
        id="grid-cap-24-hour",
    ),
    pytest.param(
        "The seminar room booking was moved to next week.",
        200,
        "no_op",
        None,
        id="distractor-no-op",
    ),
]


@pytest.mark.live_provider
@pytest.mark.parametrize("note,capacity,directive_type,adjustment", CASES)
def test_gemini_paraphrase_interpretation(
    note: str,
    capacity: float,
    directive_type: str,
    adjustment: dict | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.environ.get("GRIDWISE_RUN_LIVE_TESTS") != "1":
        pytest.skip("Set GRIDWISE_RUN_LIVE_TESTS=1 to run paid Gemini checks.")
    _load_project_dotenv()
    if not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY is not configured.")

    contracts = types.ModuleType("app.contracts")
    contracts.DirectiveInterpretation = _LiveInterpretation
    monkeypatch.setitem(sys.modules, "app.contracts", contracts)

    results = asyncio.run(
        interpreter.interpret_notes(
            operator_notes=[note],
            battery_capacity_kwh=capacity,
        )
    )
    assert len(results) == 1
    result = results[0]
    assert result.note_index == 0
    assert result.directive_type == directive_type
    assert result.applies is (directive_type != "no_op")
    assert result.structured_adjustment == adjustment
