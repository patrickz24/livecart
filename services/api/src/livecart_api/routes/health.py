"""Liveness and readiness probes, outside the versioned ``/api/v1`` surface.

``/health`` touches no dependency: it only proves the process serves requests.
``/ready`` probes every dependency in parallel, each with its own timeout, and
answers 503 as soon as one of them is unavailable. It never calls Stripe or the
LLM, whose outages must not take the API out of rotation.
"""

import asyncio
from collections.abc import Sequence
from typing import Literal

import structlog
from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from livecart_api.resources import ReadinessCheck

logger = structlog.get_logger(__name__)

CheckOutcome = Literal["ok", "failed", "timeout"]


class HealthStatus(BaseModel):
    status: Literal["ok"]


class ReadinessStatus(BaseModel):
    status: Literal["ready", "not_ready"]
    # One entry per dependency; failure details stay in the logs.
    checks: dict[str, CheckOutcome]


async def _run_check(check: ReadinessCheck, timeout_s: float) -> CheckOutcome:
    try:
        async with asyncio.timeout(timeout_s):
            await check.probe()
    except TimeoutError:
        logger.warning("readiness_check_timed_out", check=check.name, timeout_s=timeout_s)
        return "timeout"
    except Exception as exc:
        # Only the exception type: messages can carry hosts or SQL.
        logger.warning("readiness_check_failed", check=check.name, error=type(exc).__name__)
        return "failed"
    return "ok"


def build_health_router(checks: Sequence[ReadinessCheck], *, timeout_s: float) -> APIRouter:
    router = APIRouter(tags=["health"])

    @router.get("/health", summary="Liveness probe")
    async def get_health() -> HealthStatus:
        return HealthStatus(status="ok")

    @router.get(
        "/ready",
        summary="Readiness probe",
        responses={
            status.HTTP_503_SERVICE_UNAVAILABLE: {
                "model": ReadinessStatus,
                "description": "At least one dependency is unavailable",
            }
        },
    )
    async def get_readiness(response: Response) -> ReadinessStatus:
        outcomes = await asyncio.gather(*(_run_check(check, timeout_s) for check in checks))
        report = {check.name: outcome for check, outcome in zip(checks, outcomes, strict=True)}
        ready = all(outcome == "ok" for outcome in outcomes)
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessStatus(status="ready" if ready else "not_ready", checks=report)

    return router
