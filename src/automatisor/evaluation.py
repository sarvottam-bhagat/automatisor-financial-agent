from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from automatisor.contracts import Persona, Sector


class EvaluationCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["matrix", "headcount", "out_of_scope", "missing_metric"]
    persona: Persona
    sector: Sector
    query: str
    expected_company: str | None = None
    expected_value: str | None = None


class EvaluationFailure(BaseModel):
    code: str
    message: str


class EvaluationResult(BaseModel):
    case_id: str
    passed: bool
    failures: list[EvaluationFailure] = Field(default_factory=list)


LENS_GROUPS: dict[str, tuple[tuple[str, ...], ...]] = {
    "mutual_fund_analyst": (
        ("benchmark", "index", "relative"),
        ("durable", "sustainable", "core", "portfolio", "downside"),
    ),
    "equity_analyst": (
        ("earnings", "revenue"),
        ("margin",),
        ("valuation", "multiple"),
        ("catalyst", "risk"),
    ),
    "pe_analyst": (
        ("entry", "valuation"),
        ("fcf", "free cash flow", "cash conversion"),
        ("leverage", "debt capacity"),
        ("operational", "value creation", "margin gap"),
        ("exit",),
    ),
}

SECTOR_COMPANIES = {
    "tech": {"Box", "Dropbox", "Teradata", "Rapid7"},
    "retail": {"Abercrombie & Fitch", "Urban Outfitters", "Dick's Sporting Goods", "Best Buy"},
    "logistics": {"GXO Logistics", "Hub Group", "ArcBest", "C.H. Robinson"},
}


def _failure(code: str, message: str) -> EvaluationFailure:
    return EvaluationFailure(code=code, message=message)


def evaluate_response(case: EvaluationCase, response: dict[str, Any]) -> EvaluationResult:
    failures: list[EvaluationFailure] = []
    if response.get("persona") != case.persona.value:
        failures.append(_failure("persona_mismatch", "Response persona does not match the case."))
    if response.get("sector") != case.sector.value:
        failures.append(_failure("sector_mismatch", "Response sector does not match the case."))
    if not response.get("tools_used"):
        failures.append(_failure("missing_tools", "No MCP tool usage was reported."))
    if response.get("confidence") not in {"high", "medium", "low"}:
        failures.append(_failure("invalid_confidence", "Confidence is missing or invalid."))
    if response.get("status") == "completed" and not response.get("data_as_of"):
        failures.append(_failure("missing_date", "Completed analysis has no data date."))
    if (
        response.get("status") == "completed"
        and response.get("companies_referenced")
        and not response.get("sources")
    ):
        failures.append(_failure("missing_sources", "Referenced companies have no linked sources."))
    unexpected_companies = sorted(
        set(response.get("companies_referenced", [])) - SECTOR_COMPANIES[case.sector.value]
    )
    if unexpected_companies:
        failures.append(
            _failure(
                "cross_sector_company",
                f"Response includes companies outside {case.sector.value}: {', '.join(unexpected_companies)}.",
            )
        )
    for source in response.get("sources", []):
        if not source.get("id") or not str(source.get("url", "")).startswith("https://"):
            failures.append(_failure("invalid_source", "A source lacks an ID or HTTPS URL."))
            break
    answer = re.sub(r"[-–—_/]+", " ", str(response.get("answer", "")).lower())
    limitations = " ".join(str(item).lower() for item in response.get("limitations", []))
    if any("synthetic" in str(source.get("publisher", "")).lower() for source in response.get("sources", [])):
        if "synthetic" not in limitations:
            failures.append(_failure("synthetic_not_disclosed", "Synthetic fixture use is not disclosed."))
    if case.kind == "out_of_scope":
        if response.get("status") != "out_of_scope":
            failures.append(_failure("scope_failure", "Unknown company was not marked out of scope."))
    elif case.kind == "headcount":
        compact_answer = answer.replace(",", "")
        if (
            not case.expected_company
            or case.expected_company.lower() not in answer
            or not case.expected_value
            or case.expected_value not in compact_answer
            or not any(term in answer for term in ("headcount", "employee", "workforce"))
        ):
            failures.append(_failure("headcount_mismatch", "Headcount answer lacks the expected company and value."))
    elif case.kind == "missing_metric":
        disclosed = response.get("status") == "insufficient_data" or any(
            term in limitations for term in ("missing", "unavailable", "not available", "not in")
        )
        if not disclosed:
            failures.append(
                _failure("missing_metric_not_disclosed", "Unavailable requested metric was not disclosed.")
            )
    elif response.get("status") == "completed":
        missing_groups = [
            group for group in LENS_GROUPS[case.persona.value] if not any(term in answer for term in group)
        ]
        if missing_groups:
            failures.append(
                _failure(
                    "missing_lens",
                    f"Answer misses {len(missing_groups)} required {case.persona.value} lens elements.",
                )
            )
    return EvaluationResult(case_id=case.id, passed=not failures, failures=failures)
