from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class AnswerSection:
    title: str
    body: str


@dataclass(frozen=True)
class VerifiedFact:
    Company: str
    Metric: str
    Value: str
    Period: str
    Source: str


SECTION_ALIASES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "mutual_fund_analyst": (
        ("View", ("View",)),
        ("Benchmark context", ("Benchmark context",)),
        (
            "Classification",
            (
                "Core / satellite / watch / avoid classification",
                "Core/satellite/watch/avoid classification",
                "Classification",
            ),
        ),
        ("Risks", ("Risks",)),
        ("Portfolio fit", ("Portfolio fit",)),
    ),
    "equity_analyst": (
        ("Investment view", ("Investment view",)),
        ("Financial trend", ("Financial trend",)),
        ("Valuation", ("Valuation",)),
        ("Catalysts", ("Catalysts",)),
        ("Risks", ("Risks",)),
        ("Monitoring points", ("Monitoring points",)),
    ),
    "pe_analyst": (
        ("Screening verdict", ("Screening verdict",)),
        ("Entry case", ("Entry case",)),
        ("Leverage capacity", ("Leverage capacity",)),
        (
            "Value-creation plan",
            ("Value-creation plan", "Value creation plan"),
        ),
        ("Exit routes", ("Exit routes",)),
        ("Deal-breakers", ("Deal-breakers", "Deal breakers")),
    ),
}

FACTS_HEADING = re.compile(
    r"(?im)^\s*(?:\*\*)?verified supporting facts(?:\*\*)?\s*:\s*$"
)


def _heading_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _section_pattern(persona: str) -> tuple[re.Pattern[str], dict[str, str]]:
    aliases = SECTION_ALIASES.get(persona, ())
    title_by_alias = {
        _heading_key(alias): title
        for title, title_aliases in aliases
        for alias in title_aliases
    }
    alternatives = sorted(
        (alias for _, title_aliases in aliases for alias in title_aliases),
        key=len,
        reverse=True,
    )
    escaped = [re.escape(alias).replace(r"\ ", r"\s+") for alias in alternatives]
    joined = "|".join(escaped) or r"(?!x)x"
    pattern = re.compile(
        rf"(?:\*\*\s*(?P<bold>{joined})\s*\*\*\s*:?\s*|(?P<plain>{joined})\s*:\s*)",
        re.IGNORECASE,
    )
    return pattern, title_by_alias


def _parse_fact(line: str) -> VerifiedFact:
    content = line.strip().removeprefix("-").strip()
    if "; source " not in content:
        return VerifiedFact("—", "Supporting detail", content, "—", "—")

    details, source = content.rsplit("; source ", 1)
    period = "—"
    if "; period " in details:
        details, period = details.rsplit("; period ", 1)

    if ": " not in details:
        return VerifiedFact("—", "Supporting detail", details, period, source)

    subject_metric, value = details.split(": ", 1)
    company = "Sector benchmark"
    metric = subject_metric
    company_split = re.split(r"\s+[—–-]\s+", subject_metric, maxsplit=1)
    if len(company_split) == 2:
        company, metric = company_split

    return VerifiedFact(
        Company=company,
        Metric=metric.replace("_", " ").strip().capitalize(),
        Value=value.strip(),
        Period=period.strip(),
        Source=source.strip(),
    )


def format_answer(answer: str, persona: str) -> tuple[list[AnswerSection], list[VerifiedFact]]:
    facts_match = FACTS_HEADING.search(answer)
    narrative = answer[: facts_match.start()].strip() if facts_match else answer.strip()
    facts_text = answer[facts_match.end() :].strip() if facts_match else ""

    pattern, title_by_alias = _section_pattern(persona)
    matches = list(pattern.finditer(narrative))
    sections: list[AnswerSection] = []
    if matches:
        prefix = narrative[: matches[0].start()].strip()
        if prefix:
            sections.append(AnswerSection("Summary", prefix))
        for index, match in enumerate(matches):
            heading = match.group("bold") or match.group("plain") or "Analysis"
            body_end = matches[index + 1].start() if index + 1 < len(matches) else len(narrative)
            body = narrative[match.end() : body_end].strip()
            if body:
                sections.append(
                    AnswerSection(title_by_alias.get(_heading_key(heading), heading.strip()), body)
                )
    elif narrative:
        sections.append(AnswerSection("Analysis", narrative))

    facts = [
        _parse_fact(line)
        for line in facts_text.splitlines()
        if line.strip().startswith("-")
    ]
    return sections, facts

