"""Public FastAPI application for the GridWise challenge."""

from __future__ import annotations

import asyncio
import os

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.contracts import (
    ErrorResponse,
    HealthResponse,
    OptimizeEnergyRequest,
    OptimizeEnergyResponse,
    validate_request_invariants,
)
from app.orchestration import PipelineFailure, process_request

REQUEST_TIMEOUT_SECONDS = 30.0
app = FastAPI(
    title="GridWise Energy Optimization API",
    version="1.0.0",
    description="LLM-assisted interpretation and 24-hour campus energy scheduling.",
    docs_url="/docs",
    redoc_url=None,
)


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    body = ErrorResponse(error={"code": code, "message": message})
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    if any(error.get("type") == "json_invalid" for error in exc.errors()):
        return _error(400, "malformed_json", "Request body must be valid JSON.")
    return _error(422, "invalid_request", "Request does not match the required API contract.")


@app.get("/health", response_model=HealthResponse)
async def get_health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post("/optimize-energy", response_model=OptimizeEnergyResponse)
async def optimize_energy(request: OptimizeEnergyRequest) -> OptimizeEnergyResponse | JSONResponse:
    try:
        validate_request_invariants(request)
    except ValueError:
        return _error(
            422,
            "invalid_request",
            "hours and battery values must satisfy the required sequence and bounds.",
        )
    try:
        response = await asyncio.wait_for(
            process_request(request), timeout=REQUEST_TIMEOUT_SECONDS
        )
    except PipelineFailure as exc:
        return _error(500, exc.code, exc.public_message)
    except TimeoutError:
        return _error(
            500,
            "internal_error",
            "The request exceeded the service time limit. Please retry later.",
        )
    except Exception:
        return _error(500, "internal_error", "The request could not be completed safely.")
    return response


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
