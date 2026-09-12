from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import httpx

from automatisor.evaluation import EvaluationCase, EvaluationFailure, EvaluationResult, evaluate_response


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Automatisor agent evaluation matrix.")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--cases", type=Path, default=Path("evals/cases.json"))
    parser.add_argument("--output", type=Path, default=Path("evals/latest_results.json"))
    parser.add_argument("--case-id", action="append", default=[], help="Run only a named case; repeatable.")
    parser.add_argument("--timeout", type=float, default=90.0, help="Per-case API timeout in seconds.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    cases = [EvaluationCase.model_validate(item) for item in json.loads(args.cases.read_text(encoding="utf-8"))]
    if args.case_id:
        requested = set(args.case_id)
        cases = [case for case in cases if case.id in requested]
        missing = requested - {case.id for case in cases}
        if missing:
            parser.error(f"Unknown case IDs: {', '.join(sorted(missing))}")
    if args.dry_run:
        print(f"Loaded {len(cases)} cases ({sum(case.kind == 'matrix' for case in cases)} matrix cases).")
        return
    results: list[dict[str, Any]] = []
    with httpx.Client(base_url=args.api_url, timeout=args.timeout) as client:
        for case in cases:
            try:
                response = client.post(
                    "/v1/analyze",
                    json={"query": case.query, "persona": case.persona.value, "sector": case.sector.value},
                )
                response.raise_for_status()
                payload = response.json()
                evaluation = evaluate_response(case, payload)
            except httpx.HTTPError as error:
                payload = {"transport_error": type(error).__name__}
                evaluation = EvaluationResult(
                    case_id=case.id,
                    passed=False,
                    failures=[
                        EvaluationFailure(
                            code="api_request_failed",
                            message=f"API request failed: {type(error).__name__}",
                        )
                    ],
                )
            results.append({"case": case.model_dump(mode="json"), "response": payload, "evaluation": evaluation.model_dump()})
            print(f"{'PASS' if evaluation.passed else 'FAIL'} {case.id}")
    summary = {"passed": sum(item["evaluation"]["passed"] for item in results), "total": len(results)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"summary": summary, "results": results}, indent=2), encoding="utf-8")
    print(f"Summary: {summary['passed']}/{summary['total']} passed")
    if summary["passed"] != summary["total"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
