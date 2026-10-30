"""Request routing: decide which advisor handles a question.

Primary method is a transparent weighted-keyword classifier (fast, deterministic,
explainable: the UI shows which terms triggered the route). When a generative
LLM is configured and the keyword result is ambiguous, the LLM is asked to break
the tie; any LLM failure silently keeps the keyword decision.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

FINANCE_TERMS = {
    "revenue": 3, "profit": 3, "margin": 3, "cogs": 3, "ebit": 3, "opex": 3, "budget": 2,
    "expense": 2, "expenses": 2, "cost": 2, "costs": 2, "cash": 2, "financial": 3, "finance": 3,
    "growth": 2, "sales": 2, "income": 2, "forecast": 2, "variance": 3, "spend": 2, "spending": 2,
    "q1": 2, "q2": 2, "q3": 2, "q4": 2, "quarter": 2, "quarterly": 2, "fy": 2, "earnings": 3,
    "roi": 3, "ebitda": 3, "capex": 3, "dividend": 3, "debt": 2, "gross": 2, "operating": 1, "net": 1, "acquisition": 2, "valuation": 3, "aed": 2,
    "marketing": 1, "runway": 3, "balance": 1,
}
HR_TERMS = {
    "leave": 3, "vacation": 3, "holiday": 2, "sick": 3, "probation": 3, "notice": 2, "remote": 3,
    "hybrid": 3, "wfh": 3, "home": 2, "remotely": 3, "hr": 3, "employee": 2, "employees": 2, "staff": 1, "salary": 3,
    "salaries": 2, "bonus": 3, "band": 2, "bands": 2, "grade": 2, "hiring": 3, "recruitment": 3,
    "onboarding": 3, "performance": 2, "appraisal": 3, "parental": 3, "maternity": 3,
    "paternity": 3, "training": 2, "grievance": 3, "disciplinary": 3, "termination": 3,
    "resignation": 3, "overtime": 3, "working": 1, "hours": 1, "policy": 1, "handbook": 3,
    "entitled": 2, "entitlement": 2, "benefits": 2, "carry": 1, "allowance": 2, "manager": 1,
    "conduct": 2, "harassment": 3,
}


@dataclass
class Route:
    advisor: str                        # finance | hr | general
    scores: dict[str, int]
    matched: dict[str, list[str]] = field(default_factory=dict)
    method: str = "keywords"
    confidence: float = 0.0

    def as_dict(self) -> dict:
        return {"advisor": self.advisor, "scores": self.scores, "matched": self.matched,
                "method": self.method, "confidence": round(self.confidence, 2)}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def score(text: str) -> Route:
    words = _words(text)
    fin = [w for w in words if w in FINANCE_TERMS]
    hr = [w for w in words if w in HR_TERMS]
    f_score = sum(FINANCE_TERMS[w] for w in fin)
    h_score = sum(HR_TERMS[w] for w in hr)
    scores = {"finance": f_score, "hr": h_score}
    matched = {"finance": sorted(set(fin)), "hr": sorted(set(hr))}
    total = f_score + h_score
    if total == 0 or f_score == h_score:
        return Route("general", scores, matched, confidence=0.0)
    winner = "finance" if f_score > h_score else "hr"
    return Route(winner, scores, matched, confidence=max(f_score, h_score) / total)


def route_question(question: str, llm=None) -> Route:
    r = score(question)
    ambiguous = r.advisor == "general" and sum(r.scores.values()) > 0 or 0 < r.confidence < 0.65
    if llm is not None and getattr(llm, "is_generative", False) and ambiguous:
        try:
            res = llm.complete(
                "Classify the user's question for a business advisory system. Reply with exactly "
                "one word: finance, hr, or general.",
                question, max_tokens=5)
            label = res.text.strip().lower().strip(".")
            if label in {"finance", "hr", "general"}:
                r.advisor, r.method = label, "llm-tiebreak"
        except Exception:  # noqa: BLE001 - routing must never fail
            pass
    return r


def classify_category(text: str) -> str:
    """Auto-categorise an uploaded document (same lexicon, applied to its first ~4k chars)."""
    # currency codes appear in HR documents too (salary bands): ignore them here
    r = score(re.sub(r"\b(?:AED|USD|EUR)\b", " ", text[:4000]))
    if sum(r.scores.values()) < 6:
        return "general"
    return r.advisor if r.confidence >= 0.6 else "general"
