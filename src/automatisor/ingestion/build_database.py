from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any

from automatisor.contracts import Settings
from automatisor.ingestion.metrics import calculate_derived_metrics
from automatisor.ingestion.sec import (
    FilingDocument,
    extract_filing_disclosures,
    fetch_company_facts,
    fetch_recent_filing_documents,
    normalize_supported_facts,
)
from automatisor.ingestion.universe import BENCHMARKS, COMPANIES


SCHEMA_PATH = Path(__file__).parents[1] / "db" / "schema.sql"
FLOW_RATIOS = (0.22, 0.24, 0.25, 0.29)
QUARTERS = (
    ("2025-01-01", "2025-03-31", "Q1"),
    ("2025-04-01", "2025-06-30", "Q2"),
    ("2025-07-01", "2025-09-30", "Q3"),
    ("2025-10-01", "2025-12-31", "Q4"),
)
MarketHistory = dict[str, list[tuple[str, float]]]
SecFetcher = Callable[[str, str, Path], Awaitable[dict[str, Any]]]
MarketFetcher = Callable[[list[str], Path], Awaitable[MarketHistory]]
FilingFetcher = Callable[[str, str, Path], Awaitable[list[FilingDocument]]]


def validate_sec_user_agent(value: str) -> str:
    normalized = value.strip()
    has_email = re.search(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", normalized)
    if (
        not normalized
        or not has_email
        or "example.com" in normalized.casefold()
        or "your name" in normalized.casefold()
        or "contact@" in normalized.casefold()
    ):
        raise ValueError(
            "SEC_USER_AGENT must contain a real identifying name and contact email before a live build."
        )
    return normalized


def _source_id(company_id: str) -> str:
    return f"fixture_{company_id}"


def fixture_records() -> dict[str, list[dict[str, Any]]]:
    retrieved = "2026-09-11"
    records: dict[str, list[dict[str, Any]]] = {
        "companies": [],
        "sources": [],
        "financial_metrics": [],
        "operating_signals": [],
        "qualitative_evidence": [],
        "benchmarks": [],
    }
    for company in COMPANIES:
        source_id = _source_id(company.id)
        source_url = f"https://example.invalid/automatisor-financial-agent/fixtures/{company.id}"
        records["companies"].append(
            {
                "id": company.id,
                "sector": company.sector,
                "name": company.name,
                "ticker": company.ticker,
                "cik": company.cik,
                "fiscal_year_end": company.fiscal_year_end,
                "active": 1,
            }
        )
        records["sources"].append(
            {
                "id": source_id,
                "publisher": "Automatisor synthetic evaluation fixture",
                "title": f"Synthetic evaluation snapshot for {company.name}",
                "url": source_url,
                "source_type": "synthetic_fixture",
                "published_date": "2025-12-31",
                "retrieved_date": retrieved,
                "accession_number": None,
            }
        )
        annual = {
            "revenue": company.revenue,
            "gross_profit": company.revenue * company.gross_margin,
            "operating_income": company.revenue * company.operating_margin,
            "net_income": company.revenue * company.net_income_margin,
            "cash_from_operations": company.cash_from_operations,
            "capex": company.capex,
            "cash": company.cash,
            "debt": company.debt,
            "shares": company.market_cap / 50.0,
            "market_cap": company.market_cap,
        }
        periods = [("2025-01-01", "2025-12-31", "FY", 1.0), *[(*q, ratio) for q, ratio in zip(QUARTERS, FLOW_RATIOS, strict=True)]]
        for period_start, period_end, fiscal_period, ratio in periods:
            values = {
                key: round(value * ratio, 6)
                if key in {"revenue", "gross_profit", "operating_income", "net_income", "cash_from_operations", "capex"}
                else value
                for key, value in annual.items()
            }
            derived = calculate_derived_metrics(values)
            values.update(derived)
            if fiscal_period == "FY":
                values["revenue_growth"] = company.growth
            for metric_name, numeric_value in values.items():
                unit = "ratio" if metric_name.endswith("margin") or metric_name in {"ev_to_revenue", "revenue_growth"} else ("shares_millions" if metric_name == "shares" else "USD_millions")
                records["financial_metrics"].append(
                    {
                        "company_id": company.id,
                        "metric_name": metric_name,
                        "numeric_value": numeric_value,
                        "unit": unit,
                        "period_start": period_start,
                        "period_end": period_end,
                        "fiscal_period": fiscal_period,
                        "form": "10-K" if fiscal_period == "FY" else "10-Q",
                        "is_derived": int(metric_name in derived or metric_name == "revenue_growth"),
                        "source_id": source_id,
                    }
                )
        records["operating_signals"].extend(
            [
                {
                    "company_id": company.id,
                    "signal_type": "headcount",
                    "numeric_value": company.headcount,
                    "text_value": None,
                    "unit": "employees",
                    "observed_period": "2025-12-31",
                    "confidence_level": "medium",
                    "source_id": source_id,
                },
                {
                    "company_id": company.id,
                    "signal_type": "automation",
                    "numeric_value": None,
                    "text_value": "Management identifies automation and productivity as operating priorities.",
                    "unit": None,
                    "observed_period": "2025-12-31",
                    "confidence_level": "medium",
                    "source_id": source_id,
                },
            ]
        )
        evidence_templates = (
            ("growth_driver", f"{company.name} reports growth initiatives focused on its core customer proposition and execution."),
            ("risk", f"{company.name} discloses competitive, macroeconomic, and execution risks that can pressure results."),
            ("operational_lever", f"{company.name} identifies productivity, mix, and disciplined investment as potential margin levers."),
        )
        for topic, statement in evidence_templates:
            records["qualitative_evidence"].append(
                {
                    "company_id": company.id,
                    "topic": topic,
                    "statement": statement,
                    "scope": "company",
                    "period": "2025",
                    "source_id": source_id,
                }
            )
    for sector, ticker in BENCHMARKS.items():
        source_id = f"fixture_benchmark_{ticker.lower()}"
        records["sources"].append(
            {
                "id": source_id,
                "publisher": "Automatisor synthetic evaluation fixture",
                "title": f"Synthetic {ticker} benchmark snapshot",
                "url": f"https://example.invalid/automatisor-financial-agent/fixtures/{ticker.lower()}",
                "source_type": "synthetic_fixture",
                "published_date": "2025-12-31",
                "retrieved_date": retrieved,
                "accession_number": None,
            }
        )
        sector_margins = [c.operating_margin for c in COMPANIES if c.sector == sector]
        records["benchmarks"].append(
            {
                "sector": sector,
                "benchmark_ticker": ticker,
                "period_start": "2025-01-01",
                "period_end": "2025-12-31",
                "price_return": {"tech": 0.12, "retail": 0.04, "logistics": 0.07}[sector],
                "peer_metric": "operating_margin",
                "peer_median_value": median(sector_margins),
                "source_id": source_id,
            }
        )
    return records


async def fetch_market_histories(tickers: list[str], cache_dir: Path) -> MarketHistory:
    """Fetch and cache dated adjusted-close observations without hiding partial failures."""

    def fetch() -> MarketHistory:
        import yfinance as yf

        cache_dir.mkdir(parents=True, exist_ok=True)
        histories: MarketHistory = {}
        for ticker in tickers:
            frame = yf.Ticker(ticker).history(period="1y", auto_adjust=True)
            if frame.empty:
                continue
            observations = [
                (str(index.date()), float(row["Close"]))
                for index, row in frame.iterrows()
                if row.get("Close") is not None
            ]
            if observations:
                histories[ticker] = observations
                (cache_dir / f"market-{ticker.lower()}.json").write_text(
                    json.dumps(observations, indent=2), encoding="utf-8"
                )
        return histories

    return await asyncio.to_thread(fetch)


def _empty_records() -> dict[str, list[dict[str, Any]]]:
    return {
        "companies": [],
        "sources": [],
        "financial_metrics": [],
        "operating_signals": [],
        "qualitative_evidence": [],
        "benchmarks": [],
    }


def _sec_period_identity(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row["end"]),
        str(row.get("fp") or "FY"),
        str(row["form"]),
    )


DURATION_METRICS = {
    "revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "cash_from_operations",
    "capex",
}
CUMULATIVE_CASH_FLOW_METRICS = {"cash_from_operations", "capex"}


def _duration_days(row: dict[str, Any]) -> int | None:
    if not row.get("start") or not row.get("end"):
        return None
    return (date.fromisoformat(str(row["end"])) - date.fromisoformat(str(row["start"]))).days


def _select_sec_row(
    metric_name: str,
    rows: list[dict[str, Any]],
    identity: tuple[str, str, str],
) -> dict[str, Any] | None:
    candidates = [row for row in rows if _sec_period_identity(row) == identity]
    if not candidates:
        return None
    if metric_name in DURATION_METRICS:
        durations = [(row, _duration_days(row)) for row in candidates]
        duration_rows = [(row, days) for row, days in durations if days is not None]
        if not duration_rows:
            return None
        if identity[1] == "FY":
            target_duration = max(days for _, days in duration_rows)
        else:
            target_duration = min(days for _, days in duration_rows)
            if metric_name in CUMULATIVE_CASH_FLOW_METRICS and target_duration > 120:
                return None
        candidates = [row for row, days in duration_rows if days == target_duration]
    return max(
        candidates,
        key=lambda row: (
            str(row.get("filed", "")),
            str(row.get("accn", "")),
            str(row.get("start", "")),
        ),
    )


def _sec_source(company: Any, row: dict[str, Any], retrieved: str) -> dict[str, Any]:
    accession = str(row.get("accn") or "company-facts")
    compact_accession = accession.replace("-", "")
    source_id = f"sec_{company.id}_{compact_accession}"
    return {
        "id": source_id,
        "publisher": "U.S. Securities and Exchange Commission",
        "title": f"{company.name} {row.get('form', 'filing')} ({accession})",
        "url": f"https://www.sec.gov/Archives/edgar/data/{int(company.cik)}/{compact_accession}/",
        "source_type": "sec_filing",
        "published_date": row.get("filed") or row.get("end"),
        "retrieved_date": retrieved,
        "accession_number": None if accession == "company-facts" else accession,
    }


def _filing_source(
    company: Any, filing: FilingDocument, retrieved: str
) -> dict[str, Any]:
    compact_accession = filing.accession_number.replace("-", "")
    return {
        "id": f"sec_filing_{company.id}_{compact_accession}",
        "publisher": "U.S. Securities and Exchange Commission",
        "title": f"{company.name} {filing.form} ({filing.accession_number})",
        "url": filing.url,
        "source_type": "sec_filing_document",
        "published_date": filing.filing_date,
        "retrieved_date": retrieved,
        "accession_number": filing.accession_number,
    }


def _available_derived(values: dict[str, float]) -> dict[str, float]:
    derived: dict[str, float] = {}
    revenue = values.get("revenue")
    if revenue:
        if "gross_profit" in values:
            derived["gross_margin"] = round(values["gross_profit"] / revenue, 6)
        if "operating_income" in values:
            derived["operating_margin"] = round(values["operating_income"] / revenue, 6)
        if "cash_from_operations" in values and "capex" in values:
            free_cash_flow = values["cash_from_operations"] - abs(values["capex"])
            derived["free_cash_flow"] = round(free_cash_flow, 6)
            derived["fcf_margin"] = round(free_cash_flow / revenue, 6)
    if "debt" in values and "cash" in values:
        derived["net_debt"] = round(values["debt"] - values["cash"], 6)
    return derived


async def live_records(
    user_agent: str,
    cache_dir: Path,
    *,
    sec_fetcher: SecFetcher = fetch_company_facts,
    market_fetcher: MarketFetcher = fetch_market_histories,
    filing_fetcher: FilingFetcher = fetch_recent_filing_documents,
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Build records from cached primary SEC rows and dated Yahoo price history."""

    retrieved = date.today().isoformat()
    records = _empty_records()
    sources: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    market_tickers = [company.ticker for company in COMPANIES] + list(BENCHMARKS.values())
    histories = await market_fetcher(market_tickers, cache_dir)

    for company in COMPANIES:
        records["companies"].append(
            {
                "id": company.id,
                "sector": company.sector,
                "name": company.name,
                "ticker": company.ticker,
                "cik": company.cik,
                "fiscal_year_end": company.fiscal_year_end,
                "active": 1,
            }
        )
        payload = await sec_fetcher(company.cik, user_agent, cache_dir)
        normalized = normalize_supported_facts(payload)
        filing_documents = await filing_fetcher(company.cik, user_agent, cache_dir)
        recorded_headcount = False
        recorded_signal_types: set[str] = set()
        recorded_evidence: set[tuple[str, str]] = set()
        for filing in filing_documents:
            source = _filing_source(company, filing, retrieved)
            sources[source["id"]] = source
            disclosures = extract_filing_disclosures(filing.html)
            observed_period = filing.report_date or filing.filing_date
            if disclosures.headcount is not None and not recorded_headcount:
                records["operating_signals"].append(
                    {
                        "company_id": company.id,
                        "signal_type": "headcount",
                        "numeric_value": disclosures.headcount,
                        "text_value": disclosures.headcount_statement,
                        "unit": "employees",
                        "observed_period": observed_period,
                        "confidence_level": "high",
                        "source_id": source["id"],
                    }
                )
                recorded_headcount = True
            for signal in disclosures.operating_signals:
                if signal.signal_type in recorded_signal_types:
                    continue
                records["operating_signals"].append(
                    {
                        "company_id": company.id,
                        "signal_type": signal.signal_type,
                        "numeric_value": None,
                        "text_value": signal.statement,
                        "unit": None,
                        "observed_period": observed_period,
                        "confidence_level": "medium",
                        "source_id": source["id"],
                    }
                )
                recorded_signal_types.add(signal.signal_type)
            for evidence in disclosures.evidence:
                evidence_key = (evidence.topic, evidence.statement)
                if evidence_key in recorded_evidence:
                    continue
                records["qualitative_evidence"].append(
                    {
                        "company_id": company.id,
                        "topic": evidence.topic,
                        "statement": evidence.statement,
                        "scope": "company",
                        "period": observed_period,
                        "source_id": source["id"],
                    }
                )
                recorded_evidence.add(evidence_key)
        all_rows = [row for rows in normalized.values() for row in rows if row.get("end")]
        identities = sorted(
            {_sec_period_identity(row) for row in all_rows},
            key=lambda item: (item[0], item[1] == "FY"),
            reverse=True,
        )[:5]
        values_by_period: dict[tuple[str, str, str], dict[str, float]] = {
            identity: {} for identity in identities
        }
        source_by_period: dict[tuple[str, str, str], str] = {}
        metric_source_by_period: dict[tuple[str, str, str], dict[str, str]] = {
            identity: {} for identity in identities
        }
        start_by_period: dict[tuple[str, str, str], str] = {
            identity: identity[0] for identity in identities
        }
        for metric_name, rows in normalized.items():
            candidates = {
                identity: selected
                for identity in identities
                if (selected := _select_sec_row(metric_name, rows, identity)) is not None
            }
            for identity, row in candidates.items():
                source = _sec_source(company, row, retrieved)
                sources[source["id"]] = source
                raw_value = float(row["val"])
                numeric_value = raw_value / 1_000_000
                unit = "shares_millions" if metric_name == "shares" else "USD_millions"
                if row.get("start") and str(row["start"]) < start_by_period[identity]:
                    start_by_period[identity] = str(row["start"])
                records["financial_metrics"].append(
                    {
                        "company_id": company.id,
                        "metric_name": metric_name,
                        "numeric_value": numeric_value,
                        "unit": unit,
                        "period_start": str(row.get("start") or identity[0]),
                        "period_end": identity[0],
                        "fiscal_period": identity[1],
                        "form": identity[2],
                        "is_derived": 0,
                        "source_id": source["id"],
                    }
                )
                values_by_period[identity][metric_name] = numeric_value
                metric_source_by_period[identity][metric_name] = source["id"]
                source_by_period.setdefault(identity, source["id"])

        history = histories.get(company.ticker, [])
        identity = identities[0] if identities else None
        eligible_prices = (
            [observation for observation in history if identity and observation[0] <= identity[0]]
            if identity
            else []
        )
        market_observation = max(eligible_prices, key=lambda item: item[0]) if eligible_prices else None
        if market_observation and identity and "shares" in values_by_period[identity]:
            market_date, market_price = market_observation
            shares_source = sources[metric_source_by_period[identity]["shares"]]
            market_source_id = f"derived_market_cap_{company.id}_{market_date}"
            sources[market_source_id] = {
                "id": market_source_id,
                "publisher": "Automatisor derivation from SEC and Yahoo Finance",
                "title": (
                    f"{company.ticker} market cap from SEC shares "
                    f"({shares_source.get('accession_number')}) and Yahoo price on {market_date}"
                ),
                "url": f"https://finance.yahoo.com/quote/{company.ticker}/history/",
                "source_type": "derived_sec_market",
                "published_date": market_date,
                "retrieved_date": retrieved,
                "accession_number": shares_source.get("accession_number"),
            }
            market_cap = round(values_by_period[identity]["shares"] * market_price, 6)
            values_by_period[identity]["market_cap"] = market_cap
            records["financial_metrics"].append(
                {
                    "company_id": company.id,
                    "metric_name": "market_cap",
                    "numeric_value": market_cap,
                    "unit": "USD_millions",
                    "period_start": start_by_period[identity],
                    "period_end": identity[0],
                    "fiscal_period": identity[1],
                    "form": identity[2],
                    "is_derived": 1,
                    "source_id": market_source_id,
                }
            )

        for identity, values in values_by_period.items():
            for metric_name, numeric_value in _available_derived(values).items():
                records["financial_metrics"].append(
                    {
                        "company_id": company.id,
                        "metric_name": metric_name,
                        "numeric_value": numeric_value,
                        "unit": "ratio" if metric_name.endswith("margin") or metric_name == "ev_to_revenue" else "USD_millions",
                        "period_start": start_by_period[identity],
                        "period_end": identity[0],
                        "fiscal_period": identity[1],
                        "form": identity[2],
                        "is_derived": 1,
                        "source_id": source_by_period[identity],
                    }
                )

    latest_margins: dict[str, list[float]] = {sector: [] for sector in BENCHMARKS}
    latest_by_company: dict[str, tuple[str, float]] = {}
    company_sector = {company.id: company.sector for company in COMPANIES}
    for row in records["financial_metrics"]:
        if row["metric_name"] != "operating_margin":
            continue
        current = latest_by_company.get(row["company_id"])
        if current is None or row["period_end"] > current[0]:
            latest_by_company[row["company_id"]] = (row["period_end"], row["numeric_value"])
    for company_id, (_, value) in latest_by_company.items():
        latest_margins[company_sector[company_id]].append(value)
    for sector, ticker in BENCHMARKS.items():
        history = histories.get(ticker, [])
        margins = latest_margins[sector]
        if len(history) < 2 or not margins:
            warnings.append(f"No complete live benchmark coverage for {sector}.")
            continue
        source_id = f"yahoo_{ticker.lower()}_{history[-1][0]}"
        sources[source_id] = {
            "id": source_id,
            "publisher": "Yahoo Finance",
            "title": f"{ticker} dated market history",
            "url": f"https://finance.yahoo.com/quote/{ticker}/history/",
            "source_type": "market_data",
            "published_date": history[-1][0],
            "retrieved_date": retrieved,
            "accession_number": None,
        }
        records["benchmarks"].append(
            {
                "sector": sector,
                "benchmark_ticker": ticker,
                "period_start": history[0][0],
                "period_end": history[-1][0],
                "price_return": round(history[-1][1] / history[0][1] - 1, 6),
                "peer_metric": "operating_margin",
                "peer_median_value": median(margins),
                "source_id": source_id,
            }
        )
    records["sources"] = list(sources.values())
    companies_with_headcount = {
        row["company_id"]
        for row in records["operating_signals"]
        if row["signal_type"] == "headcount"
    }
    missing_headcount = [
        company.name for company in COMPANIES if company.id not in companies_with_headcount
    ]
    if missing_headcount:
        warnings.append(
            "No explicit headcount disclosure was extracted for: "
            + ", ".join(missing_headcount)
            + "."
        )
    evidence_counts = Counter(
        row["company_id"] for row in records["qualitative_evidence"]
    )
    missing_evidence = [
        company.name for company in COMPANIES if evidence_counts[company.id] < 3
    ]
    if missing_evidence:
        warnings.append(
            "Fewer than three qualitative filing disclosures were extracted for: "
            + ", ".join(missing_evidence)
            + "."
        )
    return records, warnings


def _reject_duplicates(records: dict[str, list[dict[str, Any]]]) -> None:
    keys = [
        (row["company_id"], row["metric_name"], row["period_end"], row["fiscal_period"])
        for row in records["financial_metrics"]
    ]
    duplicate = next((key for key, count in Counter(keys).items() if count > 1), None)
    if duplicate:
        raise ValueError(f"duplicate financial metric: {duplicate}")


def build_database(path: Path, records: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    _reject_duplicates(records)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f"{path.stem}-", suffix=".db", dir=path.parent)
    os.close(handle)
    temporary_path = Path(temporary_name)
    try:
        connection = sqlite3.connect(temporary_path)
        try:
            connection.executescript("BEGIN;\n" + SCHEMA_PATH.read_text(encoding="utf-8"))
            for table in ("companies", "sources", "financial_metrics", "operating_signals", "qualitative_evidence", "benchmarks"):
                rows = records[table]
                if not rows:
                    continue
                columns = tuple(rows[0])
                placeholders = ", ".join("?" for _ in columns)
                connection.executemany(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                    [tuple(row[column] for column in columns) for row in rows],
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        temporary_path.replace(path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    source_types = {row["source_type"] for row in records["sources"]}
    warnings = []
    if "synthetic_fixture" in source_types:
        warnings.extend(
            [
                "All fixture facts and evidence are synthetic evaluation data, not reported company or market data.",
                "Headcount is synthetic and must not be presented as a reported company disclosure.",
            ]
        )
    return {
        "companies": len(records["companies"]),
        "financial_metrics": len(records["financial_metrics"]),
        "operating_signals": len(records["operating_signals"]),
        "qualitative_evidence": len(records["qualitative_evidence"]),
        "sources": len(records["sources"]),
        "warnings": warnings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Automatisor sample financial database.")
    parser.add_argument("--output", type=Path, default=Path("data/financial_agent.db"))
    parser.add_argument("--mode", choices=("fixture", "live"), default="fixture")
    parser.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    args = parser.parse_args()
    live_warnings: list[str] = []
    if args.mode == "live":
        try:
            sec_user_agent = validate_sec_user_agent(Settings().sec_user_agent)
        except ValueError as error:
            parser.error(str(error))
        records, live_warnings = asyncio.run(
            live_records(sec_user_agent, args.cache_dir)
        )
    else:
        records = fixture_records()
    report = build_database(args.output, records)
    report["warnings"].extend(live_warnings)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
