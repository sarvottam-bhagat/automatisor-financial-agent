from __future__ import annotations

from contextlib import asynccontextmanager
from itertools import chain
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite


ALLOWED_METRICS = frozenset(
    {
        "revenue",
        "revenue_growth",
        "gross_profit",
        "gross_margin",
        "operating_income",
        "operating_margin",
        "net_income",
        "cash_from_operations",
        "capex",
        "free_cash_flow",
        "fcf_margin",
        "cash",
        "debt",
        "net_debt",
        "market_cap",
        "enterprise_value",
        "ev_to_revenue",
        "shares",
    }
)
ALLOWED_TOPICS = frozenset(
    {
        "strategy",
        "growth_driver",
        "risk",
        "competitive_position",
        "labor",
        "operational_lever",
        "customer_concentration",
        "automation",
        "exit_risk",
    }
)


class RepositoryError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _dicts(rows: list[aiosqlite.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


PeriodIdentity = tuple[str, str, str]


def _period_filter(alias: str, periods: list[PeriodIdentity]) -> tuple[str, tuple[str, ...]]:
    clause = " OR ".join(
        f"({alias}.period_end = ? AND {alias}.fiscal_period = ? AND {alias}.form = ?)"
        for _ in periods
    )
    return f"({clause})", tuple(chain.from_iterable(periods))


class FinancialRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path).resolve()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        uri = f"{self.database_path.as_uri()}?mode=ro"
        connection = await aiosqlite.connect(uri, uri=True)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys = ON")
        await connection.execute("PRAGMA query_only = ON")
        try:
            yield connection
        finally:
            await connection.close()

    async def _fetchall(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        async with self.connection() as connection:
            cursor = await connection.execute(sql, parameters)
            return _dicts(await cursor.fetchall())

    async def get_sources(self, source_ids: list[str]) -> list[dict[str, Any]]:
        if not source_ids:
            return []
        placeholders = ", ".join("?" for _ in source_ids)
        return await self._fetchall(
            f"SELECT id, publisher, title, url, source_type, published_date, retrieved_date FROM sources WHERE id IN ({placeholders}) ORDER BY id",
            tuple(source_ids),
        )

    async def contains_source_type(self, source_type: str) -> bool:
        rows = await self._fetchall(
            "SELECT 1 AS present FROM sources WHERE source_type = ? LIMIT 1",
            (source_type,),
        )
        return bool(rows)

    async def _company(self, sector: str, company_id: str) -> dict[str, Any] | None:
        rows = await self._fetchall(
            "SELECT id, sector, name, ticker, cik, fiscal_year_end FROM companies WHERE sector = ? AND id = ? AND active = 1",
            (sector, company_id),
        )
        return rows[0] if rows else None

    async def list_sector_companies(self, sector: str) -> dict[str, Any]:
        companies = await self._fetchall(
            """
            SELECT c.id, c.name, c.ticker,
                   MAX(fm.period_end) AS latest_financial_date,
                   MAX(os.observed_period) AS latest_signal_date
            FROM companies c
            LEFT JOIN financial_metrics fm ON fm.company_id = c.id
            LEFT JOIN operating_signals os ON os.company_id = c.id
            WHERE c.sector = ? AND c.active = 1
            GROUP BY c.id, c.name, c.ticker
            ORDER BY c.name
            """,
            (sector,),
        )
        return {"sector": sector, "companies": companies}

    async def resolve_company(self, sector: str, query: str) -> dict[str, Any]:
        normalized = query.strip().lower()
        rows = await self._fetchall(
            """
            SELECT id, sector, name, ticker
            FROM companies
            WHERE sector = ? AND active = 1
              AND (lower(name) = ? OR lower(ticker) = ? OR lower(name) LIKE ?)
            ORDER BY CASE WHEN lower(name) = ? OR lower(ticker) = ? THEN 0 ELSE 1 END, name
            LIMIT 1
            """,
            (sector, normalized, normalized, f"%{normalized}%", normalized, normalized),
        )
        if rows:
            return {"status": "found", "company": rows[0]}
        covered = (await self.list_sector_companies(sector))["companies"]
        return {"status": "not_found", "query": query, "covered_companies": covered}

    async def _periods(self, sector: str, company_id: str, periods: int) -> list[PeriodIdentity]:
        rows = await self._fetchall(
            """
            SELECT DISTINCT fm.period_end, fm.fiscal_period, fm.form
            FROM financial_metrics fm
            JOIN companies c ON c.id = fm.company_id
            WHERE c.sector = ? AND fm.company_id = ?
            ORDER BY fm.period_end DESC,
                     CASE WHEN fm.fiscal_period = 'FY' THEN 0 ELSE 1 END,
                     fm.period_start DESC
            LIMIT ?
            """,
            (sector, company_id, periods),
        )
        return [
            (row["period_end"], row["fiscal_period"], row["form"])
            for row in rows
        ]

    async def get_company_snapshot(self, sector: str, company_id: str, periods: int = 5) -> dict[str, Any]:
        if not 1 <= periods <= 5:
            raise RepositoryError("insufficient_data", "periods must be between 1 and 5")
        company = await self._company(sector, company_id)
        if not company:
            raise RepositoryError("company_not_found", f"{company_id!r} is not covered in {sector}")
        selected_periods = await self._periods(sector, company_id, periods)
        if not selected_periods:
            raise RepositoryError("insufficient_data", "No financial periods are available")
        period_clause, period_parameters = _period_filter("financial_metrics", selected_periods)
        metrics = await self._fetchall(
            f"""
            SELECT metric_name, numeric_value, unit, period_start, period_end, fiscal_period, form, is_derived, source_id
            FROM financial_metrics
            WHERE company_id = ? AND {period_clause}
            ORDER BY period_end DESC, CASE WHEN fiscal_period = 'FY' THEN 0 ELSE 1 END, metric_name
            """,
            (company_id, *period_parameters),
        )
        signals = await self._fetchall(
            """
            SELECT signal_type, numeric_value, text_value, unit, observed_period, confidence_level, source_id
            FROM operating_signals WHERE company_id = ? ORDER BY observed_period DESC, signal_type
            """,
            (company_id,),
        )
        evidence = await self._fetchall(
            """
            SELECT topic, statement, scope, period, source_id
            FROM qualitative_evidence WHERE company_id = ? ORDER BY topic
            """,
            (company_id,),
        )
        source_ids = sorted({row["source_id"] for row in [*metrics, *signals, *evidence]})
        source_placeholders = ", ".join("?" for _ in source_ids)
        sources = await self._fetchall(
            f"SELECT id, publisher, title, url, source_type, published_date, retrieved_date FROM sources WHERE id IN ({source_placeholders}) ORDER BY id",
            tuple(source_ids),
        )
        return {
            "company": company,
            "financial_metrics": metrics,
            "operating_signals": signals,
            "qualitative_evidence": evidence,
            "sources": sources,
            "coverage": {"periods_requested": periods, "periods_returned": len(selected_periods)},
        }

    async def compare_company_metrics(
        self,
        sector: str,
        company_ids: list[str],
        metrics: list[str],
        periods: int = 5,
    ) -> dict[str, Any]:
        if not 1 <= periods <= 5:
            raise RepositoryError("insufficient_data", "periods must be between 1 and 5")
        if len(company_ids) > 5 or len(metrics) > 10:
            raise RepositoryError("insufficient_data", "comparison bounds exceeded")
        invalid = sorted(set(metrics) - ALLOWED_METRICS)
        if invalid:
            raise RepositoryError("invalid_metric", f"Unsupported metrics: {', '.join(invalid)}")
        covered = (await self.list_sector_companies(sector))["companies"]
        covered_by_id = {row["id"]: row for row in covered}
        selected_ids = company_ids or list(covered_by_id)
        missing = sorted(set(selected_ids) - set(covered_by_id))
        if missing:
            raise RepositoryError("company_not_found", f"Companies are not covered in {sector}: {', '.join(missing)}")
        if not metrics:
            raise RepositoryError("invalid_metric", "At least one metric is required")
        rows: list[dict[str, Any]] = []
        for company_id in selected_ids:
            selected_periods = await self._periods(sector, company_id, periods)
            period_clause, period_parameters = _period_filter("fm", selected_periods)
            metric_placeholders = ", ".join("?" for _ in metrics)
            rows.extend(
                await self._fetchall(
                    f"""
                    SELECT fm.company_id, c.name, c.ticker, fm.metric_name, fm.numeric_value, fm.unit,
                           fm.period_end, fm.fiscal_period, fm.source_id
                    FROM financial_metrics fm
                    JOIN companies c ON c.id = fm.company_id
                    WHERE fm.company_id = ?
                      AND fm.metric_name IN ({metric_placeholders})
                      AND {period_clause}
                    ORDER BY fm.period_end DESC,
                             CASE WHEN fm.fiscal_period = 'FY' THEN 0 ELSE 1 END,
                             fm.metric_name
                    """,
                    (company_id, *metrics, *period_parameters),
                )
            )
        return {"sector": sector, "companies": [covered_by_id[item] for item in selected_ids], "metrics": rows}

    async def get_sector_benchmark(self, sector: str) -> dict[str, Any]:
        rows = await self._fetchall(
            """
            SELECT b.sector, b.benchmark_ticker, b.period_start, b.period_end, b.price_return,
                   b.peer_metric, b.peer_median_value, b.source_id,
                   s.publisher, s.title, s.url, s.source_type, s.retrieved_date
            FROM benchmarks b JOIN sources s ON s.id = b.source_id
            WHERE b.sector = ? ORDER BY b.period_end DESC
            """,
            (sector,),
        )
        if not rows:
            raise RepositoryError("insufficient_data", f"No benchmark data is available for {sector}")
        for row in rows:
            values = await self._fetchall(
                """
                WITH ranked AS (
                    SELECT fm.numeric_value,
                           ROW_NUMBER() OVER (
                               PARTITION BY fm.company_id
                               ORDER BY fm.period_end DESC,
                                        CASE WHEN fm.fiscal_period = 'FY' THEN 0 ELSE 1 END
                           ) AS rank
                    FROM financial_metrics fm
                    JOIN companies c ON c.id = fm.company_id
                    WHERE c.sector = ? AND c.active = 1 AND fm.metric_name = ?
                )
                SELECT numeric_value FROM ranked WHERE rank = 1
                """,
                (sector, row["peer_metric"]),
            )
            observations = [float(item["numeric_value"]) for item in values]
            row["peer_dispersion"] = {
                "company_count": len(observations),
                "minimum": min(observations),
                "maximum": max(observations),
                "range": round(max(observations) - min(observations), 6),
            }
        return {"sector": sector, "benchmarks": rows}

    async def search_company_evidence(
        self,
        sector: str,
        query: str,
        company_ids: list[str] | None = None,
        topics: list[str] | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        company_ids = company_ids or []
        topics = topics or []
        if not 1 <= limit <= 10:
            raise RepositoryError("insufficient_data", "limit must be between 1 and 10")
        invalid_topics = sorted(set(topics) - ALLOWED_TOPICS)
        if invalid_topics:
            raise RepositoryError("invalid_metric", f"Unsupported topics: {', '.join(invalid_topics)}")
        conditions = ["c.sector = ?", "(lower(q.statement) LIKE ? OR lower(q.topic) LIKE ?)"]
        parameters: list[Any] = [sector, f"%{query.lower()}%", f"%{query.lower()}%"]
        if company_ids:
            conditions.append(f"q.company_id IN ({', '.join('?' for _ in company_ids)})")
            parameters.extend(company_ids)
        if topics:
            conditions.append(f"q.topic IN ({', '.join('?' for _ in topics)})")
            parameters.extend(topics)
        parameters.append(limit)
        rows = await self._fetchall(
            f"""
            SELECT q.company_id, c.name, c.ticker, q.topic, q.statement, q.scope, q.period,
                   q.source_id, s.publisher, s.title, s.url, s.source_type, s.retrieved_date
            FROM qualitative_evidence q
            JOIN companies c ON c.id = q.company_id
            JOIN sources s ON s.id = q.source_id
            WHERE {' AND '.join(conditions)}
            ORDER BY q.period DESC, c.name
            LIMIT ?
            """,
            tuple(parameters),
        )
        return {"sector": sector, "results": rows}
