from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent, UsageLimits
from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.mcp import MCPError, MCPToolset

from automatisor.contracts import (
    AnalysisRequest,
    AnalysisResponse,
    AnalysisStatus,
    Confidence,
    Settings,
    SourceReference,
)
from automatisor.personas import PERSONA_CONFIGS, build_instructions


MCP_TOOL_NAMES = {
    "list_sector_companies",
    "resolve_company",
    "get_company_snapshot",
    "compare_company_metrics",
    "get_sector_benchmark",
    "search_company_evidence",
}


class GroundedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company: str | None = None
    metric: str
    value: float | str
    unit: str | None = None
    period: str | None = None
    source_id: str


class GroundedAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: AnalysisStatus
    answer: str = Field(min_length=1)
    companies_referenced: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    confidence: Confidence
    tools_used: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    claims: list[GroundedClaim] = Field(default_factory=list)


@dataclass
class ToolTrace:
    tools_used: list[str] = field(default_factory=list)
    sectors: set[str] = field(default_factory=set)
    companies: set[str] = field(default_factory=set)
    company_aliases: dict[str, str] = field(default_factory=dict)
    sources: dict[str, SourceReference] = field(default_factory=dict)
    facts: list[GroundedClaim] = field(default_factory=list)
    error_codes: set[str] = field(default_factory=set)
    company_not_found: bool = False
    warnings: list[str] = field(default_factory=list)
    data_as_of: date | str | None = None


@dataclass
class AgentRunRecord:
    output: GroundedAnalysis
    trace: ToolTrace


class AgentRunner(Protocol):
    async def run(self, prompt: str, instructions: str) -> AgentRunRecord: ...


class GroundingError(RuntimeError):
    pass


class MCPUnavailableError(RuntimeError):
    pass


class LLMProviderError(RuntimeError):
    pass


class StaticRunner:
    """Deterministic runner used by unit tests and offline evaluations."""

    def __init__(self, outputs: list[GroundedAnalysis], traces: list[ToolTrace]) -> None:
        self.outputs = outputs
        self.traces = traces
        self.calls = 0

    async def run(self, prompt: str, instructions: str) -> AgentRunRecord:
        index = min(self.calls, len(self.outputs) - 1)
        self.calls += 1
        return AgentRunRecord(self.outputs[index], self.traces[index])


def _coerce_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _extract_trace(messages: list[Any]) -> ToolTrace:
    trace = ToolTrace()

    def visit(value: Any, inherited_company: str | None = None) -> None:
        value = _coerce_json(value)
        if isinstance(value, dict):
            scoped_company = inherited_company
            embedded_company = value.get("company")
            if isinstance(embedded_company, dict) and isinstance(embedded_company.get("name"), str):
                scoped_company = embedded_company["name"]
            if value.get("status") == "not_found":
                trace.company_not_found = True
            error_code = value.get("error_code")
            if value.get("ok") is False and isinstance(error_code, str):
                trace.error_codes.add(error_code)
            warnings = value.get("warnings")
            if isinstance(warnings, list):
                for warning in warnings:
                    if isinstance(warning, str) and warning not in trace.warnings:
                        trace.warnings.append(warning)
            source_id = value.get("id") if all(key in value for key in ("title", "url", "publisher")) else None
            if isinstance(source_id, str):
                trace.sources[source_id] = SourceReference(
                    id=source_id,
                    title=str(value["title"]),
                    url=str(value["url"]),
                    publisher=str(value["publisher"]),
                )
            if isinstance(value.get("name"), str) and isinstance(value.get("ticker"), str):
                canonical_name = value["name"]
                scoped_company = canonical_name
                trace.companies.add(canonical_name)
                for alias in (value.get("id"), value.get("name"), value.get("ticker")):
                    if isinstance(alias, str):
                        trace.company_aliases[alias.casefold()] = canonical_name
            as_of = value.get("as_of") or value.get("period_end") or value.get("observed_period")
            if isinstance(as_of, str) and len(as_of) >= 10:
                candidate = as_of[:10]
                if trace.data_as_of is None or candidate > str(trace.data_as_of):
                    trace.data_as_of = candidate
            fact_source = value.get("source_id")
            if isinstance(fact_source, str):
                fact_specs = (
                    ("metric_name", "numeric_value", "period_end"),
                    ("signal_type", "numeric_value", "observed_period"),
                    ("signal_type", "text_value", "observed_period"),
                    ("topic", "statement", "period"),
                )
                company = value.get("name") or value.get("company_id") or scoped_company
                for metric_key, value_key, period_key in fact_specs:
                    fact_value = value.get(value_key)
                    metric = value.get(metric_key) if metric_key else value_key
                    if isinstance(metric, str) and isinstance(fact_value, (int, float, str)):
                        unit = value.get("unit")
                        if not unit and (
                            metric == "price_return"
                            or metric.endswith("_margin")
                            or metric.endswith("_growth")
                        ):
                            unit = "ratio"
                        trace.facts.append(
                            GroundedClaim(
                                company=str(company) if company else None,
                                metric=metric,
                                value=fact_value,
                                unit=str(unit) if unit else None,
                                period=str(value[period_key]) if value.get(period_key) is not None else None,
                                source_id=fact_source,
                            )
                        )
                price_return = value.get("price_return")
                if isinstance(price_return, (int, float)):
                    period_start = value.get("period_start")
                    period_end = value.get("period_end")
                    period = (
                        f"{period_start} to {period_end}"
                        if period_start is not None and period_end is not None
                        else str(period_end) if period_end is not None else None
                    )
                    trace.facts.append(
                        GroundedClaim(
                            metric="benchmark_price_return",
                            value=price_return,
                            unit="ratio",
                            period=period,
                            source_id=fact_source,
                        )
                    )
                peer_metric = value.get("peer_metric")
                peer_median = value.get("peer_median_value")
                if isinstance(peer_metric, str) and isinstance(peer_median, (int, float)):
                    median_unit = (
                        "ratio"
                        if peer_metric.endswith("_margin") or peer_metric.endswith("_growth")
                        else None
                    )
                    period_start = value.get("period_start")
                    period_end = value.get("period_end")
                    period = (
                        f"{period_start} to {period_end}"
                        if period_start is not None and period_end is not None
                        else str(period_end) if period_end is not None else None
                    )
                    trace.facts.append(
                        GroundedClaim(
                            metric=f"peer_median_{peer_metric}",
                            value=peer_median,
                            unit=median_unit,
                            period=period,
                            source_id=fact_source,
                        )
                    )
                peer_dispersion = value.get("peer_dispersion")
                if isinstance(peer_metric, str) and isinstance(peer_dispersion, dict):
                    period_start = value.get("period_start")
                    period_end = value.get("period_end")
                    period = (
                        f"{period_start} to {period_end}"
                        if period_start is not None and period_end is not None
                        else str(period_end) if period_end is not None else None
                    )
                    dispersion_unit = (
                        "ratio"
                        if peer_metric.endswith("_margin") or peer_metric.endswith("_growth")
                        else None
                    )
                    for field_name in ("minimum", "maximum", "range", "company_count"):
                        fact_value = peer_dispersion.get(field_name)
                        if isinstance(fact_value, (int, float)):
                            trace.facts.append(
                                GroundedClaim(
                                    metric=f"peer_{peer_metric}_{field_name}",
                                    value=fact_value,
                                    unit="companies" if field_name == "company_count" else dispersion_unit,
                                    period=period,
                                    source_id=fact_source,
                                )
                            )
            for item in value.values():
                visit(item, scoped_company)
        elif isinstance(value, list):
            for item in value:
                visit(item, inherited_company)

    for message in messages:
        for part in getattr(message, "parts", []):
            kind = getattr(part, "part_kind", "")
            if kind == "tool-call":
                name = getattr(part, "tool_name", "")
                if name in MCP_TOOL_NAMES and name not in trace.tools_used:
                    trace.tools_used.append(name)
                try:
                    arguments = part.args_as_dict()
                except Exception:
                    arguments = _coerce_json(getattr(part, "args", {}))
                if isinstance(arguments, dict) and isinstance(arguments.get("sector"), str):
                    trace.sectors.add(arguments["sector"])
            elif kind == "tool-return":
                visit(getattr(part, "content", None))
    trace.facts = [
        fact.model_copy(
            update={
                "company": trace.company_aliases.get(fact.company.casefold(), fact.company)
                if fact.company
                else None
            }
        )
        for fact in trace.facts
    ]
    return trace


class PydanticAgentRunner:
    def __init__(self, settings: Settings) -> None:
        toolset = MCPToolset(settings.mcp_url, include_return_schema=True)
        self.agent = Agent(
            settings.llm_model,
            output_type=GroundedAnalysis,
            toolsets=[toolset],
            retries=1,
            defer_model_check=True,
            max_concurrency=1,
            model_settings={"parallel_tool_calls": False, "timeout": 60.0},
        )

    async def run(self, prompt: str, instructions: str) -> AgentRunRecord:
        try:
            result = await self.agent.run(
                prompt,
                instructions=instructions,
                usage_limits=UsageLimits(request_limit=18, tool_calls_limit=16),
            )
        except (MCPError, httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError) as error:
            raise MCPUnavailableError("MCP connection failed") from error
        except (ModelAPIError, UnexpectedModelBehavior, UsageLimitExceeded) as error:
            raise LLMProviderError("Model provider failed") from error
        return AgentRunRecord(result.output, _extract_trace(result.all_messages()))


def _values_match(left: float | str, right: float | str) -> bool:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-9)
    return " ".join(str(left).casefold().split()) == " ".join(str(right).casefold().split())


def _canonical_metric_label(metric: str) -> str:
    canonical = re.sub(r"[^a-z0-9]+", "_", metric.casefold()).strip("_")
    if canonical == "price_return" or canonical.endswith("_price_return"):
        return "benchmark_price_return"
    if "peer_median_" in canonical:
        return canonical[canonical.index("peer_median_") :]
    return canonical


def _periods_match(claim_period: str, fact_period: str) -> bool:
    claim_dates = re.findall(r"\d{4}-\d{2}-\d{2}", claim_period)
    fact_dates = re.findall(r"\d{4}-\d{2}-\d{2}", fact_period)
    if claim_dates and fact_dates:
        if len(claim_dates) > 1:
            return len(fact_dates) > 1 and claim_dates[0] == fact_dates[0] and claim_dates[-1] == fact_dates[-1]
        return claim_dates[-1] == fact_dates[-1]
    return claim_period.strip().casefold() == fact_period.strip().casefold()


def _find_supporting_fact(
    claim: GroundedClaim, facts: list[GroundedClaim]
) -> GroundedClaim | None:
    for fact in facts:
        metrics_match = _canonical_metric_label(claim.metric) == _canonical_metric_label(fact.metric)
        if isinstance(claim.value, str) and isinstance(fact.value, str):
            metrics_match = True
        if (
            metrics_match
            and claim.source_id == fact.source_id
            and claim.unit == fact.unit
            and (
                claim.period is None
                or (fact.period is not None and _periods_match(claim.period, fact.period))
            )
            and claim.company == fact.company
            and _values_match(claim.value, fact.value)
        ):
            return fact
    return None


def _claim_supported(claim: GroundedClaim, facts: list[GroundedClaim]) -> bool:
    return _find_supporting_fact(claim, facts) is not None


NUMBER_PATTERN = re.compile(
    r"(?<![\w\d])\$?-?\d[\d,]*(?:\.\d+)?\s*(?:%|[kKmMbB]|[xX])?"
)


def _validate_grounding(output: GroundedAnalysis, trace: ToolTrace, sector: str) -> None:
    errors: list[str] = []
    if not trace.tools_used:
        errors.append("No MCP tool was called")
    if trace.sectors != {sector}:
        errors.append(f"Tool sector mismatch: {sorted(trace.sectors)}")
    if (trace.company_not_found or "company_not_found" in trace.error_codes) and output.status != AnalysisStatus.OUT_OF_SCOPE:
        errors.append("A company not_found result requires status=out_of_scope")
    unknown_companies = sorted(set(output.companies_referenced) - trace.companies)
    if unknown_companies:
        errors.append(f"Companies absent from MCP results: {', '.join(unknown_companies)}")
    unknown_sources = sorted(set(output.source_ids) - set(trace.sources))
    if unknown_sources:
        errors.append(f"Sources absent from MCP results: {', '.join(unknown_sources)}")
    uncalled_tools = sorted(set(output.tools_used) - set(trace.tools_used))
    if uncalled_tools:
        errors.append(f"Tools absent from MCP trace: {', '.join(uncalled_tools)}")
    if output.status == AnalysisStatus.COMPLETED and output.companies_referenced and not output.source_ids:
        errors.append("A company conclusion has no source")
    if output.status == AnalysisStatus.COMPLETED and output.companies_referenced and not output.claims:
        errors.append("A company conclusion has no structured claims")
    for claim in output.claims:
        if isinstance(claim.value, (int, float)) and not claim.unit:
            errors.append(f"Numeric claim has no unit: {claim.metric}")
        if claim.source_id not in output.source_ids:
            errors.append(f"Claim source absent from structured source_ids: {claim.source_id}")
        elif not _claim_supported(claim, trace.facts):
            errors.append(
                f"Claim absent from MCP results: {claim.metric}={claim.value} ({claim.source_id})"
            )
    narrative = re.sub(r"(?m)^\s*\d+[.)]\s+", "", output.answer)
    undeclared = [token.strip() for token in NUMBER_PATTERN.findall(narrative)]
    if undeclared:
        errors.append(f"Undeclared numeric assertion in answer: {', '.join(undeclared)}")
    if trace.error_codes.intersection({"invalid_metric", "insufficient_data"}):
        if output.status == AnalysisStatus.COMPLETED and not output.limitations:
            errors.append("Missing or invalid metrics must be disclosed as a limitation")
    if errors:
        raise GroundingError("; ".join(errors))


MAX_VERIFIED_FACTS = 12
MAX_BENCHMARK_FACTS = 4

BROAD_SECTOR_COMPANY_PATTERNS = {
    "operating_margin",
    "gross_margin",
    "free_cash_flow",
    "fcf_margin",
    "revenue",
    "revenue_growth",
    "cash",
    "debt",
    "market_cap",
    "enterprise_value",
}

FACT_RELEVANCE_GROUPS: tuple[tuple[set[str], set[str]], ...] = (
    (
        {"headcount", "employee", "employees", "workforce", "hiring", "hire", "staff"},
        {"headcount", "hiring", "labor", "restructuring"},
    ),
    (
        {"margin", "margins", "profitability", "profitable"},
        {"operating_margin", "gross_margin", "fcf_margin", "net_margin", "peer_operating_margin"},
    ),
    (
        {"cash", "cashflow", "conversion", "liquidity"},
        {"cash", "free_cash_flow", "cash_from_operations", "fcf_margin"},
    ),
    (
        {"revenue", "sales", "growth", "grow"},
        {"revenue", "revenue_growth", "growth_driver"},
    ),
    (
        {"valuation", "value", "multiple", "price", "expensive", "cheap"},
        {"valuation", "enterprise_value", "market_cap", "price_return"},
    ),
    (
        {"debt", "leverage", "leveraged", "balance", "sheet"},
        {"debt", "net_debt", "cash", "leverage"},
    ),
    (
        {"benchmark", "sector", "peer", "peers"},
        {"benchmark", "peer"},
    ),
    (
        {"risk", "risks", "catalyst", "catalysts", "strategy"},
        {"risk", "catalyst", "strategy", "competitive_position"},
    ),
)


def _deduplicate_facts(facts: list[GroundedClaim]) -> list[GroundedClaim]:
    unique: list[GroundedClaim] = []
    seen: set[tuple[str | None, str, float | str, str | None, str | None, str]] = set()
    for fact in facts:
        key = (
            fact.company,
            fact.metric,
            fact.value,
            fact.unit,
            fact.period,
            fact.source_id,
        )
        if key not in seen:
            seen.add(key)
            unique.append(fact)
    return unique


def _metric_matches(metric: str, patterns: set[str]) -> bool:
    canonical = _canonical_metric_label(metric)
    return any(pattern == canonical or pattern in canonical for pattern in patterns)


def _round_robin_facts(
    facts: list[GroundedClaim],
    limit: int,
) -> list[GroundedClaim]:
    buckets: dict[str, list[GroundedClaim]] = {}
    for fact in facts:
        buckets.setdefault(fact.company or "", []).append(fact)
    selected: list[GroundedClaim] = []
    while len(selected) < limit and any(buckets.values()):
        for bucket in buckets.values():
            if bucket and len(selected) < limit:
                selected.append(bucket.pop(0))
    return selected


def _select_relevant_facts(
    query: str,
    facts: list[GroundedClaim],
) -> list[GroundedClaim]:
    unique = _deduplicate_facts(facts)
    query_terms = set(re.findall(r"[a-z0-9]+", query.casefold()))
    active_patterns: set[str] = set()
    for trigger_terms, metric_patterns in FACT_RELEVANCE_GROUPS:
        if query_terms.intersection(trigger_terms):
            active_patterns.update(metric_patterns)

    if active_patterns:
        # A broad sector question needs both the benchmark backdrop and issuer
        # evidence. Treating the word "sector" as a benchmark-only filter made
        # company comparisons appear without their supporting SEC sources.
        if active_patterns == {"benchmark", "peer"}:
            benchmark_facts = [
                fact for fact in unique if _metric_matches(fact.metric, active_patterns)
            ][:MAX_BENCHMARK_FACTS]
            company_facts = [
                fact
                for fact in unique
                if fact.company
                and _metric_matches(fact.metric, BROAD_SECTOR_COMPANY_PATTERNS)
            ]
            return benchmark_facts + _round_robin_facts(
                company_facts,
                MAX_VERIFIED_FACTS - len(benchmark_facts),
            )

        candidates = [fact for fact in unique if _metric_matches(fact.metric, active_patterns)]
        priority = {"headcount": 0, "hiring": 1, "labor": 2, "restructuring": 3}
        candidates.sort(
            key=lambda fact: (
                priority.get(_canonical_metric_label(fact.metric), 10),
                0 if isinstance(fact.value, (int, float)) else 1,
            )
        )
        return candidates[:MAX_VERIFIED_FACTS]

    # Broad questions need representative evidence across the covered companies,
    # rather than dozens of rows from whichever company appeared first.
    return _round_robin_facts(unique, MAX_VERIFIED_FACTS)


def _normalize_grounded_output(
    output: GroundedAnalysis, trace: ToolTrace, query: str = ""
) -> GroundedAnalysis:
    if (
        trace.company_not_found or "company_not_found" in trace.error_codes
    ) and output.status == AnalysisStatus.OUT_OF_SCOPE:
        output = output.model_copy(
            update={"companies_referenced": [], "source_ids": [], "claims": []}
        )
    companies = [
        trace.company_aliases.get(company.casefold(), company)
        for company in output.companies_referenced
    ]
    claims = _select_relevant_facts(query, trace.facts)
    return output.model_copy(
        update={
            "companies_referenced": companies,
            "source_ids": list(dict.fromkeys(claim.source_id for claim in claims)),
            "tools_used": trace.tools_used,
            "claims": claims,
        }
    )


def _sanitize_narrative(output: GroundedAnalysis) -> GroundedAnalysis:
    sentences = re.split(r"(?<=[.!?])\s+", output.answer.strip())
    retained: list[str] = []
    for sentence in sentences:
        without_list_marker = re.sub(r"^\s*\d+[.)]\s+", "", sentence).strip()
        if without_list_marker and not NUMBER_PATTERN.search(without_list_marker):
            retained.append(without_list_marker)
    answer = " ".join(retained) or "The conclusion is supported by the verified facts below."
    return output.model_copy(update={"answer": answer})


def _apply_disclosures(output: GroundedAnalysis, trace: ToolTrace, persona: str) -> GroundedAnalysis:
    disclaimer = PERSONA_CONFIGS[persona].disclaimer
    answer = output.answer if disclaimer in output.answer else f"{output.answer}\n\n{disclaimer}"
    limitations = [
        limitation
        for limitation in output.limitations
        if not (
            "mcp trace" in limitation.casefold()
            or "application renders" in limitation.casefold()
            or "answer omits numeric support" in limitation.casefold()
        )
    ]
    for warning in trace.warnings:
        if warning not in limitations:
            limitations.append(warning)
    return output.model_copy(update={"answer": answer, "limitations": limitations})


def _render_verified_claims(output: GroundedAnalysis) -> GroundedAnalysis:
    if not output.claims:
        return output
    lines = []
    for claim in output.claims:
        subject = f"{claim.company} — " if claim.company else ""
        metric = claim.metric.replace("_", " ")
        if isinstance(claim.value, (int, float)) and claim.unit == "ratio":
            value = f"{claim.value * 100:g}%"
        elif isinstance(claim.value, (int, float)) and claim.unit == "employees":
            value = f"{claim.value:,.0f} employees"
        elif isinstance(claim.value, (int, float)) and claim.unit == "USD_millions":
            value = f"{claim.value:g} USD millions"
        elif isinstance(claim.value, (int, float)) and claim.unit == "shares_millions":
            value = f"{claim.value:g} million shares"
        elif isinstance(claim.value, (int, float)):
            value = f"{claim.value:g} {claim.unit or ''}".strip()
        else:
            value = f"{claim.value} {claim.unit or ''}".strip()
        period = f"; period {claim.period}" if claim.period else ""
        lines.append(
            f"- {subject}{metric}: {value}{period}; source {claim.source_id}"
        )
    answer = f"{output.answer}\n\nVerified supporting facts:\n" + "\n".join(lines)
    return output.model_copy(update={"answer": answer})


def _replace_claims_with_traced_facts(
    output: GroundedAnalysis, trace: ToolTrace
) -> GroundedAnalysis:
    claims = [
        fact
        for claim in output.claims
        if (fact := _find_supporting_fact(claim, trace.facts)) is not None
    ]
    return output.model_copy(update={"claims": claims})


SEC_ACCESSION_PATTERN = re.compile(r"(?<!\d)(\d{10}-\d{2}-\d{6}|\d{18})(?!\d)")


def _source_identity(source: SourceReference) -> str:
    for value in (source.id, source.title, source.url):
        if match := SEC_ACCESSION_PATTERN.search(value):
            return f"sec:{match.group(1).replace('-', '')}"
    return f"id:{source.id}"


def _source_preference(source: SourceReference) -> tuple[int, int]:
    return (
        0 if source.id.startswith("sec_filing_") else 1,
        0 if re.search(r"\.(?:htm|html|txt)(?:$|[?#])", source.url, re.IGNORECASE) else 1,
    )


def _collapse_equivalent_sources(
    output: GroundedAnalysis,
    sources: dict[str, SourceReference],
) -> GroundedAnalysis:
    groups: dict[str, list[SourceReference]] = {}
    for source_id in output.source_ids:
        source = sources.get(source_id)
        if source is not None:
            groups.setdefault(_source_identity(source), []).append(source)

    replacements: dict[str, str] = {}
    source_ids: list[str] = []
    for group in groups.values():
        preferred = min(group, key=_source_preference)
        source_ids.append(preferred.id)
        replacements.update({source.id: preferred.id for source in group})
    claims = [
        claim.model_copy(update={"source_id": replacements.get(claim.source_id, claim.source_id)})
        for claim in output.claims
    ]
    return output.model_copy(update={"source_ids": source_ids, "claims": claims})


def _prompt(request: AnalysisRequest) -> str:
    history = "\n".join(f"{item.role}: {item.content}" for item in request.history)
    context = f"Prior conversation:\n{history}\n\n" if history else ""
    return f"{context}Question: {request.query}\nReturn the structured analysis only."


class AgentService:
    def __init__(self, runner: AgentRunner) -> None:
        self.runner = runner

    async def analyze(self, request: AnalysisRequest) -> AnalysisResponse:
        instructions = build_instructions(request.persona.value, request.sector.value)
        prompt = _prompt(request)
        last_error: GroundingError | None = None
        record: AgentRunRecord | None = None
        for attempt in range(3):
            suffix = ""
            if attempt and last_error:
                suffix = f"\nPrevious structured output failed grounding validation: {last_error}. Correct it using MCP results only."
            record = await self.runner.run(prompt, instructions + suffix)
            record.output = _normalize_grounded_output(
                record.output, record.trace, request.query
            )
            record.output = _sanitize_narrative(record.output)
            record.output = _apply_disclosures(record.output, record.trace, request.persona.value)
            try:
                _validate_grounding(record.output, record.trace, request.sector.value)
                break
            except GroundingError as error:
                last_error = error
        else:
            assert record is not None
            company_not_found = (
                record.trace.company_not_found
                or "company_not_found" in record.trace.error_codes
            )
            fallback = GroundedAnalysis(
                status=(
                    AnalysisStatus.OUT_OF_SCOPE
                    if company_not_found
                    else AnalysisStatus.INSUFFICIENT_DATA
                ),
                answer=(
                    "The requested company is outside the selected sector coverage."
                    if company_not_found
                    else "The analysis provider could not produce a valid grounded response from the available data."
                ),
                confidence="low",
                tools_used=record.trace.tools_used,
                limitations=[
                    "No analytical conclusion is shown because the generated response did not pass grounding validation."
                ],
            )
            record = AgentRunRecord(
                _apply_disclosures(fallback, record.trace, request.persona.value),
                record.trace,
            )
        assert record is not None
        record.output = _replace_claims_with_traced_facts(record.output, record.trace)
        record.output = _collapse_equivalent_sources(record.output, record.trace.sources)
        record.output = _render_verified_claims(record.output)
        sources = [record.trace.sources[source_id] for source_id in record.output.source_ids]
        return AnalysisResponse(
            request_id=f"req_{uuid.uuid4().hex[:16]}",
            status=record.output.status,
            persona=request.persona,
            sector=request.sector,
            answer=record.output.answer,
            companies_referenced=record.output.companies_referenced,
            sources=sources,
            confidence=record.output.confidence,
            data_as_of=record.trace.data_as_of,
            tools_used=record.trace.tools_used,
            limitations=record.output.limitations,
        )


def create_agent_service(settings: Settings | None = None) -> AgentService:
    resolved = settings or Settings()
    return AgentService(PydanticAgentRunner(resolved))
