from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx

from automatisor.ingestion.metrics import (
    DEBT_CURRENT_TAGS,
    DEBT_NONCURRENT_TAGS,
    DEBT_TOTAL_TAGS,
    SEC_TAGS,
)


@dataclass(frozen=True)
class FilingDocument:
    accession_number: str
    form: str
    filing_date: str
    report_date: str
    primary_document: str
    url: str
    html: str


@dataclass(frozen=True)
class EvidenceDisclosure:
    topic: str
    statement: str


@dataclass(frozen=True)
class OperatingSignalDisclosure:
    signal_type: str
    statement: str


@dataclass(frozen=True)
class FilingDisclosures:
    headcount: int | None = None
    headcount_statement: str | None = None
    evidence: tuple[EvidenceDisclosure, ...] = field(default_factory=tuple)
    operating_signals: tuple[OperatingSignalDisclosure, ...] = field(default_factory=tuple)


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style", "noscript"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth and data.strip():
            self.parts.append(data.strip())


COMBINED_HEADCOUNT_PATTERN = re.compile(
    r"\b(?:we|the company)\s+employ(?:ed|s)?\s+(?:approximately\s+)?"
    r"(?P<full_time>\d{1,3}(?:,\d{3})+|\d{3,6})\s+full[- ]time\s+and\s+"
    r"(?P<part_time>\d{1,3}(?:,\d{3})+|\d{3,6})\s+part[- ]time\s+"
    r"(?:employees|associates|team members|teammates|people)\b",
    re.IGNORECASE,
)

HEADCOUNT_PATTERNS = (
    re.compile(
        r"\b(?:we|the company)\s+(?:had|have|employ(?:ed|s)?)\s+"
        r"(?:a\s+total\s+of\s+)?(?:approximately\s+)?"
        r"(?P<count>\d{1,3}(?:,\d{3})+|\d{3,6})\s+"
        r"(?:(?:full[- ]time|part[- ]time|active|regular|global|total)\s+)?"
        r"(?:employees|associates|team members|teammates|people)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:workforce|team)\s+(?:of|consisted of|consists of)\s+(?:approximately\s+)?"
        r"(?P<count>\d{1,3}(?:,\d{3})+|\d{3,6})\s+"
        r"(?:employees|associates|team members|people)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:our|with)\s+(?:approximately\s+)?"
        r"(?P<count>\d{1,3}(?:,\d{3})+|\d{3,6})\s+"
        r"(?:employees|associates|team members|teammates|people)\b",
        re.IGNORECASE,
    ),
)

EVIDENCE_TOPICS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("strategy", ("strategy", "strategic", "disciplined execution")),
    ("growth_driver", ("growth", "expand", "adoption", "demand")),
    ("risk", ("risk", "adversely affect", "uncertain", "volatility")),
    ("competitive_position", ("competition", "competitive")),
    ("labor", ("employee", "workforce", "labor", "hiring")),
    ("operational_lever", ("productivity", "efficiency", "margin improvement", "cost reduction")),
    ("automation", ("automation", "automated", "artificial intelligence")),
    ("customer_concentration", ("customer concentration", "largest customer")),
)

OPERATING_SIGNAL_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("hiring", ("hiring", "recruiting", "recruitment")),
    ("restructuring", ("restructuring", "workforce reduction", "severance", "layoff")),
    ("automation", ("automation", "automated", "artificial intelligence")),
    ("facility_footprint", ("distribution center", "warehouse", "facility footprint")),
)

BOILERPLATE_TERMS = (
    "indicate by check mark",
    "large accelerated filer",
    "emerging growth company",
    "quantitative and qualitative disclosures",
    "forward-looking statements",
    "words such as",
    "financial accounting standards board",
    "fasb",
    "accounting standards update",
    "table of contents",
    "incorporated by reference",
)


def _visible_filing_text(html: str) -> str:
    parser = _VisibleTextParser()
    parser.feed(html)
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def _filing_sentences(html: str) -> list[str]:
    text = _visible_filing_text(html)
    return [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", text)
        if 30 <= len(sentence.strip()) <= 700
    ]


def _evidence_score(sentence: str, terms: tuple[str, ...], topic: str) -> int:
    folded = sentence.casefold()
    if any(term in folded for term in BOILERPLATE_TERMS):
        return -1
    matched = [index for index, term in enumerate(terms) if term in folded]
    if not matched:
        return -1
    score = (len(terms) - min(matched)) * 10 + len(matched) * 3
    if folded.startswith(("our ", "we ", "the company ", "as of ")):
        score += 5
    if "adversely affect" in folded or "could adversely" in folded:
        score += 5
    if topic == "growth_driver":
        negative_growth_language = (
            "adversely",
            "could decrease",
            "reduced demand",
            "negatively impact",
            "difficult to predict",
            "uncertain",
            "may not",
        )
        positive_growth_language = (
            "we believe",
            "our growth",
            "growth strategy",
            "expand",
            "grew",
            "increase",
            "strong demand",
            "driven by",
            "growth opportunity",
            "adoption of our",
            "we invest",
        )
        if any(term in folded for term in negative_growth_language) or not any(
            term in folded for term in positive_growth_language
        ):
            return -1
    if topic == "automation":
        if "compete" in folded or "competition" in folded:
            score -= 30
        if any(
            term in folded
            for term in ("we use", "we launched", "we introduced", "we announced", "we deploy")
        ):
            score += 20
    if 60 <= len(sentence) <= 350:
        score += 2
    return score


def _best_evidence_sentence(
    sentences: list[str], terms: tuple[str, ...], topic: str
) -> str | None:
    ranked = sorted(
        ((_evidence_score(sentence, terms, topic), sentence) for sentence in sentences),
        key=lambda item: item[0],
        reverse=True,
    )
    if not ranked or ranked[0][0] < 0:
        return None
    return ranked[0][1]


def extract_filing_disclosures(html: str) -> FilingDisclosures:
    sentences = _filing_sentences(html)
    headcount: int | None = None
    headcount_statement: str | None = None
    for sentence in sentences:
        if combined_match := COMBINED_HEADCOUNT_PATTERN.search(sentence):
            headcount = sum(
                int(combined_match.group(name).replace(",", ""))
                for name in ("full_time", "part_time")
            )
            headcount_statement = sentence
            break
        for pattern in HEADCOUNT_PATTERNS:
            if match := pattern.search(sentence):
                headcount = int(match.group("count").replace(",", ""))
                headcount_statement = sentence
                break
        if headcount is not None:
            break

    evidence: list[EvidenceDisclosure] = []
    for topic, terms in EVIDENCE_TOPICS:
        statement = _best_evidence_sentence(sentences, terms, topic)
        if statement:
            evidence.append(EvidenceDisclosure(topic=topic, statement=statement))

    operating_signals: list[OperatingSignalDisclosure] = []
    for signal_type, terms in OPERATING_SIGNAL_TERMS:
        statement = _best_evidence_sentence(sentences, terms, signal_type)
        if statement:
            operating_signals.append(
                OperatingSignalDisclosure(signal_type=signal_type, statement=statement)
            )

    return FilingDisclosures(
        headcount=headcount,
        headcount_statement=headcount_statement,
        evidence=tuple(evidence),
        operating_signals=tuple(operating_signals),
    )


async def fetch_recent_filing_documents(
    cik: str,
    user_agent: str,
    cache_dir: Path,
) -> list[FilingDocument]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    padded_cik = f"{int(cik):010d}"
    submissions_url = f"https://data.sec.gov/submissions/CIK{padded_cik}.json"
    submissions_cache = cache_dir / f"submissions-{padded_cik}.json"
    headers = {"User-Agent": user_agent}
    async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True) as client:
        if submissions_cache.exists():
            payload = json.loads(submissions_cache.read_text(encoding="utf-8"))
        else:
            response = await client.get(submissions_url)
            response.raise_for_status()
            payload = response.json()
            submissions_cache.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        recent = payload.get("filings", {}).get("recent", {})
        selected: list[dict[str, str]] = []
        seen_forms: set[str] = set()
        forms = recent.get("form", [])
        for index, form in enumerate(forms):
            if form not in {"10-K", "10-Q"} or form in seen_forms:
                continue
            selected.append(
                {
                    "accession_number": recent["accessionNumber"][index],
                    "form": form,
                    "filing_date": recent["filingDate"][index],
                    "report_date": recent["reportDate"][index],
                    "primary_document": recent["primaryDocument"][index],
                }
            )
            seen_forms.add(form)
            if seen_forms == {"10-K", "10-Q"}:
                break

        filings: list[FilingDocument] = []
        for metadata in selected:
            compact_accession = metadata["accession_number"].replace("-", "")
            url = (
                f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                f"{compact_accession}/{metadata['primary_document']}"
            )
            filing_cache = cache_dir / f"filing-{padded_cik}-{compact_accession}.html"
            if filing_cache.exists():
                html = filing_cache.read_text(encoding="utf-8")
            else:
                filing_response = await client.get(url)
                filing_response.raise_for_status()
                html = filing_response.text
                filing_cache.write_text(html, encoding="utf-8")
            filings.append(FilingDocument(url=url, html=html, **metadata))
    return filings


async def fetch_company_facts(cik: str, user_agent: str, cache_dir: Path) -> dict[str, Any]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"companyfacts-{cik}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json"
    async with httpx.AsyncClient(headers={"User-Agent": user_agent}, timeout=30) as client:
        response = await client.get(url)
        response.raise_for_status()
    payload = response.json()
    cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def normalize_supported_facts(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    us_gaap = payload.get("facts", {}).get("us-gaap", {})
    normalized: dict[str, list[dict[str, Any]]] = {}
    for metric_name, tags in SEC_TAGS.items():
        for tag in tags:
            fact = us_gaap.get(tag)
            if fact:
                units = fact.get("units", {})
                candidates = units.get("USD") or units.get("shares") or []
                normalized[metric_name] = [
                    row for row in candidates if row.get("form") in {"10-K", "10-Q"}
                ]
                break
    total_debt = _rows_for_first_tag(us_gaap, DEBT_TOTAL_TAGS)
    if total_debt:
        normalized["debt"] = total_debt
    else:
        current = _rows_for_first_tag(us_gaap, DEBT_CURRENT_TAGS)
        noncurrent = _rows_for_first_tag(us_gaap, DEBT_NONCURRENT_TAGS)
        if current and noncurrent:
            normalized["debt"] = _sum_debt_components(current, noncurrent)
    return normalized


def _rows_for_first_tag(
    facts: dict[str, Any], tags: tuple[str, ...]
) -> list[dict[str, Any]]:
    for tag in tags:
        fact = facts.get(tag)
        if not fact:
            continue
        rows = fact.get("units", {}).get("USD") or []
        return [row for row in rows if row.get("form") in {"10-K", "10-Q"}]
    return []


def _debt_context(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("start"),
        row.get("end"),
        row.get("fp"),
        row.get("form"),
        row.get("filed"),
        row.get("accn"),
        row.get("frame"),
    )


def _sum_debt_components(
    current: list[dict[str, Any]], noncurrent: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    current_by_context = {_debt_context(row): row for row in current}
    noncurrent_by_context = {_debt_context(row): row for row in noncurrent}
    combined: list[dict[str, Any]] = []
    for context in sorted(set(current_by_context) & set(noncurrent_by_context), key=str):
        row = current_by_context[context].copy()
        row["val"] = float(current_by_context[context]["val"]) + float(
            noncurrent_by_context[context]["val"]
        )
        combined.append(row)
    return combined
