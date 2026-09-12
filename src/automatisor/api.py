from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

import uvicorn
from fastapi import FastAPI, HTTPException
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from automatisor.agent import (
    AgentService,
    GroundingError,
    LLMProviderError,
    MCPUnavailableError,
    create_agent_service,
)
from automatisor.contracts import AnalysisRequest, AnalysisResponse, Settings


class AnalysisService(Protocol):
    async def analyze(self, request: AnalysisRequest) -> AnalysisResponse: ...


MCPProbe = Callable[[str], Awaitable[bool]]


async def probe_mcp(url: str) -> bool:
    try:
        async with asyncio.timeout(3):
            async with streamable_http_client(url) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
        return True
    except Exception:
        # The MCP SDK wraps transport failures in ExceptionGroup on AnyIO task teardown.
        # A health probe must degrade safely for every dependency failure shape.
        return False


def create_app(
    service: AnalysisService | None = None,
    *,
    settings: Settings | None = None,
    mcp_probe: MCPProbe = probe_mcp,
) -> FastAPI:
    resolved_settings = settings or Settings()
    resolved_service = service
    app = FastAPI(
        title="Automatisor Financial Agent API",
        version="0.1.0",
        description="Structured access to one persona-configurable, MCP-grounded financial agent.",
    )

    def get_service() -> AnalysisService:
        nonlocal resolved_service
        if resolved_service is None:
            resolved_service = create_agent_service(resolved_settings)
        return resolved_service

    @app.post("/v1/analyze", response_model=AnalysisResponse)
    async def analyze(request: AnalysisRequest) -> AnalysisResponse:
        try:
            return await get_service().analyze(request)
        except MCPUnavailableError as error:
            raise HTTPException(
                status_code=503,
                detail={"code": "mcp_unavailable", "message": "The financial data service is unavailable."},
            ) from error
        except (LLMProviderError, GroundingError) as error:
            raise HTTPException(
                status_code=502,
                detail={"code": "llm_failed", "message": "The analysis provider could not return a valid grounded response."},
            ) from error

    @app.get("/health")
    async def health() -> dict[str, str]:
        reachable = await mcp_probe(resolved_settings.mcp_url)
        return {
            "status": "ok" if reachable else "degraded",
            "api": "ok",
            "mcp": "ok" if reachable else "unavailable",
        }

    return app


app = create_app()


def main() -> None:
    uvicorn.run("automatisor.api:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
