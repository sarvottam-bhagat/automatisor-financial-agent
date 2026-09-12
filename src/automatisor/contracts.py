from __future__ import annotations

from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Persona(StrEnum):
    MUTUAL_FUND_ANALYST = "mutual_fund_analyst"
    EQUITY_ANALYST = "equity_analyst"
    PE_ANALYST = "pe_analyst"


class Sector(StrEnum):
    TECH = "tech"
    RETAIL = "retail"
    LOGISTICS = "logistics"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AnalysisStatus(StrEnum):
    COMPLETED = "completed"
    OUT_OF_SCOPE = "out_of_scope"
    INSUFFICIENT_DATA = "insufficient_data"


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4_000)


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=3, max_length=4_000)
    persona: Persona
    sector: Sector
    history: list[ChatMessage] = Field(default_factory=list, max_length=8)


class SourceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    url: str
    publisher: str


class AnalysisResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    status: AnalysisStatus
    persona: Persona
    sector: Sector
    answer: str
    companies_referenced: list[str] = Field(default_factory=list)
    sources: list[SourceReference] = Field(default_factory=list)
    confidence: Confidence
    data_as_of: date | None = None
    tools_used: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    data: dict[str, Any] = Field(default_factory=dict)
    sources: list[SourceReference] = Field(default_factory=list)
    as_of: date | None = None
    coverage: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    openai_api_key: str | None = None
    llm_model: str = "openai:gpt-5-mini"
    database_path: Path = Path("data/financial_agent.db")
    mcp_url: str = "http://127.0.0.1:8001/mcp"
    api_url: str = "http://127.0.0.1:8000"
    sec_user_agent: str = "Automatisor take-home contact@example.com"

