from __future__ import annotations

from typing import Any

import httpx

from automatisor.contracts import AnalysisResponse, ChatMessage


class APIClientError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class FinancialAgentClient:
    def __init__(self, api_url: str, timeout: float = 60.0) -> None:
        self.endpoint = f"{api_url.rstrip('/')}/v1/analyze"
        self.client = httpx.Client(timeout=timeout)

    def analyze(
        self,
        query: str,
        persona: str,
        sector: str,
        history: list[dict[str, Any]],
    ) -> AnalysisResponse:
        safe_history = [
            ChatMessage(role=item["role"], content=item["content"]).model_dump()
            for item in history[-8:]
            if item.get("role") in {"user", "assistant"} and item.get("content")
        ]
        try:
            response = self.client.post(
                self.endpoint,
                json={
                    "query": query,
                    "persona": persona,
                    "sector": sector,
                    "history": safe_history,
                },
            )
            response.raise_for_status()
            return AnalysisResponse.model_validate(response.json())
        except httpx.HTTPStatusError as error:
            message = "The analysis request failed."
            try:
                payload = error.response.json()
            except ValueError:
                payload = {}
            if isinstance(payload, dict):
                detail = payload.get("detail", {})
                if isinstance(detail, dict) and isinstance(detail.get("message"), str):
                    message = detail["message"]
            raise APIClientError(message, error.response.status_code) from error
        except (httpx.HTTPError, ValueError) as error:
            raise APIClientError("The analysis API is unavailable or returned an invalid response.") from error
