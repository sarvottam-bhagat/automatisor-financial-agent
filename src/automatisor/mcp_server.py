from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Awaitable, Callable

from mcp.server.fastmcp import FastMCP

from automatisor.contracts import Sector, Settings, SourceReference, ToolResult
from automatisor.db.repository import FinancialRepository, RepositoryError


RepositoryCall = Callable[[], Awaitable[dict[str, Any]]]


def _validate_sector(value: str) -> str:
    try:
        return Sector(value).value
    except ValueError as error:
        raise RepositoryError("invalid_sector", f"Unsupported sector: {value}") from error


def _source_references(rows: list[dict[str, Any]]) -> list[SourceReference]:
    references: dict[str, SourceReference] = {}
    for row in rows:
        source_id = row.get("id") or row.get("source_id")
        if not source_id or not row.get("url"):
            continue
        references[source_id] = SourceReference(
            id=source_id,
            title=row["title"],
            url=row["url"],
            publisher=row["publisher"],
        )
    return list(references.values())


def _latest_date(data: dict[str, Any], sources: list[dict[str, Any]]) -> date | None:
    candidates: list[str] = []
    date_keys = {"period_end", "observed_period", "latest_financial_date", "latest_signal_date", "published_date"}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in date_keys and isinstance(item, str) and len(item) >= 10:
                    candidates.append(item[:10])
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(data)
    visit(sources)
    return date.fromisoformat(max(candidates)) if candidates else None


async def _envelope(call: RepositoryCall, repository: FinancialRepository) -> ToolResult:
    try:
        data = await call()
        embedded_sources = data.pop("sources", [])
        source_ids: set[str] = set()

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                source_id = value.get("source_id")
                if isinstance(source_id, str):
                    source_ids.add(source_id)
                for item in value.values():
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        collect(data)
        source_rows = embedded_sources or await repository.get_sources(sorted(source_ids))
        coverage = data.pop("coverage", {})
        warnings = []
        uses_synthetic = any(
            row.get("source_type") == "synthetic_fixture" for row in source_rows
        ) or await repository.contains_source_type("synthetic_fixture")
        if uses_synthetic:
            warnings.append(
                "This result contains synthetic evaluation fixtures, not reported company or live market data."
            )
        return ToolResult(
            ok=True,
            data=data,
            sources=_source_references(source_rows),
            as_of=_latest_date(data, source_rows),
            coverage=coverage,
            warnings=warnings,
        )
    except RepositoryError as error:
        return ToolResult(ok=False, error_code=error.code, error_message=str(error))
    except Exception:
        return ToolResult(
            ok=False,
            error_code="dependency_failed",
            error_message="The financial data service could not complete the request.",
        )


def create_mcp_server(database_path: Path) -> FastMCP:
    repository = FinancialRepository(database_path)
    server = FastMCP(
        "Automatisor Financial Data",
        instructions="Read-only, sector-scoped financial facts and evidence.",
        host="127.0.0.1",
        port=8001,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
    )

    @server.tool()
    async def list_sector_companies(sector: str) -> ToolResult:
        """List covered companies and the latest available data dates for a sector."""
        return await _envelope(
            lambda: repository.list_sector_companies(_validate_sector(sector)), repository
        )

    @server.tool()
    async def resolve_company(sector: str, query: str) -> ToolResult:
        """Resolve a company name or ticker within one sector without inventing identifiers."""
        return await _envelope(
            lambda: repository.resolve_company(_validate_sector(sector), query), repository
        )

    @server.tool()
    async def get_company_snapshot(sector: str, company_id: str, periods: int = 5) -> ToolResult:
        """Return financial history, signals, evidence, coverage, and sources for a covered company."""
        return await _envelope(
            lambda: repository.get_company_snapshot(_validate_sector(sector), company_id, periods),
            repository,
        )

    @server.tool()
    async def compare_company_metrics(
        sector: str,
        company_ids: list[str] | None = None,
        metrics: list[str] | None = None,
        periods: int = 5,
    ) -> ToolResult:
        """Compare allowed metrics for up to five covered companies and five periods."""
        return await _envelope(
            lambda: repository.compare_company_metrics(
                _validate_sector(sector), company_ids or [], metrics or [], periods
            ),
            repository,
        )

    @server.tool()
    async def get_sector_benchmark(sector: str) -> ToolResult:
        """Return benchmark performance and peer median context for one sector."""
        return await _envelope(
            lambda: repository.get_sector_benchmark(_validate_sector(sector)), repository
        )

    @server.tool()
    async def search_company_evidence(
        sector: str,
        query: str,
        company_ids: list[str] | None = None,
        topics: list[str] | None = None,
        limit: int = 10,
    ) -> ToolResult:
        """Search only stored qualitative evidence for covered companies."""
        return await _envelope(
            lambda: repository.search_company_evidence(
                _validate_sector(sector), query, company_ids or [], topics or [], limit
            ),
            repository,
        )

    return server


def main() -> None:
    settings = Settings()
    create_mcp_server(settings.database_path).run(transport="streamable-http")


if __name__ == "__main__":
    main()
