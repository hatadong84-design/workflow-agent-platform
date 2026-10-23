from __future__ import annotations

from typing import Any

from .retrieval import tokenize


SUPPORT_THRESHOLD = 0.5


def evaluate_claim(
    text: str,
    source_ids: list[str],
    sources: dict[str, str],
) -> dict[str, Any]:
    claim_tokens = set(tokenize(text or ""))
    if not source_ids:
        return {
            "text": text,
            "source_ids": [],
            "coverage": 0.0,
            "status": "no_citation",
            "matched_tokens": [],
        }
    excerpt_tokens: set[str] = set()
    for source_id in source_ids:
        excerpt_tokens.update(tokenize(sources.get(source_id, "")))
    matched = sorted(claim_tokens & excerpt_tokens)
    coverage = round(len(matched) / len(claim_tokens), 4) if claim_tokens else 0.0
    return {
        "text": text,
        "source_ids": list(source_ids),
        "coverage": coverage,
        "status": "strong" if coverage >= SUPPORT_THRESHOLD else "weak",
        "matched_tokens": matched,
    }


def evaluate_report_support(report: dict, sources: dict[str, str]) -> dict[str, Any]:
    claims = [
        evaluate_claim(claim.get("text", ""), claim.get("source_ids", []), sources)
        for claim in report.get("claims", [])
    ]
    summary_ids = report.get("summary_source_ids", [])
    if summary_ids or claims:
        summary = evaluate_claim(report.get("summary", ""), summary_ids, sources)
    else:
        summary = {
            "text": report.get("summary", ""),
            "source_ids": [],
            "coverage": 0.0,
            "status": "not_applicable",
            "matched_tokens": [],
        }
    evaluated = [summary, *claims]
    counts = {
        status: sum(1 for item in evaluated if item["status"] == status)
        for status in ("strong", "weak", "no_citation", "not_applicable")
    }
    return {
        "threshold": SUPPORT_THRESHOLD,
        "summary": summary,
        "claims": claims,
        "counts": counts,
        "needs_review": counts["weak"] > 0 or counts["no_citation"] > 0,
    }
