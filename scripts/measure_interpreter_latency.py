"""Measure sequential live latency for a representative interpreter request."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time

from app.services.interpreter import interpret_notes
from app.services.llm_provider import GeminiClient


NOTES = [
    "Solar panels will deliver only 20% of forecast from 13:00 to 15:00.",
    "The seminar room booking was moved to next week.",
]


def nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


async def measure(calls: int) -> dict[str, object]:
    provider = GeminiClient()
    durations: list[float] = []
    for _ in range(calls):
        started = time.perf_counter()
        await interpret_notes(operator_notes=NOTES, battery_capacity_kwh=500)
        durations.append(time.perf_counter() - started)
    return {
        "provider": "Google Gemini API",
        "model": provider.model,
        "endpoint_region": "global endpoint (region selected by Gemini API)",
        "calls": calls,
        "p50_seconds": round(statistics.median(durations), 3),
        "p95_seconds": round(nearest_rank(durations, 0.95), 3),
        "max_seconds": round(max(durations), 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calls", type=int, default=20, help="Sequential live calls (default: 20).")
    args = parser.parse_args()
    if args.calls < 2:
        parser.error("--calls must be at least 2 to report percentiles")
    print(json.dumps(asyncio.run(measure(args.calls)), indent=2))


if __name__ == "__main__":
    main()
