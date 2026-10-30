"""Finance calculations in code (Decimal arithmetic) + verification of reported figures.

The language model never computes a number. It may only *describe* numbers that
this module produced, and `unsupported_numbers` rejects any narrative that
mentions a figure we cannot trace back to a source document or a calculation.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from advisor.facts import METRIC_NAMES, OPEX_COMPONENTS

TOLERANCE = Decimal("0.005")         # 0.5 % relative tolerance for reported-vs-computed checks
_PRELIM = re.compile(r"prelim|estimate|draft|forecast|unaudited", re.I)


@dataclass
class Fact:
    id: int
    document_id: int
    chunk_id: int
    metric: str
    period: str | None
    value: Decimal
    currency: str | None
    raw: str
    title: str
    filename: str

    @property
    def preliminary(self) -> bool:
        return bool(_PRELIM.search(self.raw))

    def source(self) -> dict:
        return {"document_id": self.document_id, "chunk_id": self.chunk_id, "title": self.title,
                "filename": self.filename, "raw": self.raw}


@dataclass
class FinanceResult:
    calculations: list[dict] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    periods: list[str] = field(default_factory=list)
    chosen: dict = field(default_factory=dict)         # period -> metric -> Fact
    facts_used: list[Fact] = field(default_factory=list)


# ------------------------------------------------------------------ formatting
def money(v: Decimal, cur: str = "AED") -> str:
    q = v.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"{cur} {q:,}" if q >= 0 else f"-{cur} {abs(q):,}"


def pct(v: Decimal) -> str:
    return f"{(v * 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}%"


def period_key(p: str | None) -> tuple:
    if not p:
        return (0, 0)
    m = re.match(r"(Q([1-4])|FY) (\d{4})", p)
    if not m:
        return (0, 0)
    return (int(m.group(3)), int(m.group(2)) if m.group(2) else 5)


# ------------------------------------------------------------------ question parsing
def requested_periods(question: str, available: list[str]) -> list[str]:
    q = question.upper()
    wanted = set()
    for m in re.finditer(r"\b(Q[1-4])\s*(?:FY)?\s*(\d{4})?\b", q):
        quarter, year = m.group(1), m.group(2)
        wanted |= {p for p in available if p.startswith(quarter) and (not year or p.endswith(year))}
        if not any(p.startswith(quarter) and (not year or p.endswith(year)) for p in available):
            wanted.add(f"{quarter} {year}" if year else quarter)     # asked but not available
    for m in re.finditer(r"\bFY\s*(\d{4})\b", q):
        wanted |= {p for p in available if p.endswith(m.group(1))} or {f"FY {m.group(1)}"}
    return sorted(wanted, key=period_key)


METRIC_KEYWORDS = {
    "revenue": ["revenue", "sales", "top line", "turnover"],
    "cogs": ["cogs", "cost of goods", "cost of sales"],
    "gross_profit": ["gross profit", "gross margin"],
    "total_opex": ["opex", "operating expense", "expenses", "costs", "spend"],
    "operating_profit": ["operating profit", "operating income", "operating margin", "ebit"],
    "net_profit": ["net profit", "net income", "net margin", "bottom line"],
    "budget_revenue": ["budget", "target", "variance", "plan"],
    "cash": ["cash", "runway", "liquidity"],
    "opex_marketing": ["marketing"],
    "opex_salaries": ["salaries", "payroll", "staff cost"],
}
UNSUPPORTED_METRICS = {"ebitda": "EBITDA", "free cash flow": "Free cash flow", "capex": "Capital expenditure",
                       "debt": "Debt", "dividend": "Dividends", "share price": "Share price",
                       "headcount": "Headcount", "accounts receivable": "Accounts receivable"}


def _has(q: str, phrase: str) -> bool:
    return re.search(rf"\b{re.escape(phrase)}", q) is not None and not (
        phrase == "ebit" and "ebitda" in q)


def requested_metrics(question: str) -> list[str]:
    q = question.lower()
    return [m for m, kws in METRIC_KEYWORDS.items() if any(_has(q, k) for k in kws)]


BROAD_TERMS = ("analy", "performance", "financial", "results", "summary", "summarise", "summarize",
               "overview", "health", "how did we do", "kpi")


def is_calculation_question(question: str) -> bool:
    """Does the question ask about figures we compute (vs. a narrative fact in a document)?"""
    q = question.lower()
    return bool(requested_metrics(question)
                or any(k in q for kws in RELEVANCE.values() for k in kws)
                or re.search(r"\b(q[1-4]|fy\s?\d{4})\b", q)
                or any(b in q for b in BROAD_TERMS))


def unsupported_metrics(question: str) -> list[str]:
    q = question.lower()
    return [name for k, name in UNSUPPORTED_METRICS.items() if k in q]


# ------------------------------------------------------------------ core analysis
def choose(facts: list[Fact]) -> tuple[Fact, list[Fact]]:
    """Pick the authoritative fact: final over preliminary, then most recent document."""
    ranked = sorted(facts, key=lambda f: (not f.preliminary, f.document_id), reverse=True)
    return ranked[0], ranked[1:]


def analyse(facts: list[Fact]) -> FinanceResult:
    res = FinanceResult()
    by_period: dict[str, dict[str, list[Fact]]] = defaultdict(lambda: defaultdict(list))
    for f in facts:
        if f.period:
            by_period[f.period][f.metric].append(f)
    res.periods = sorted(by_period, key=period_key)

    # 1) pick one value per metric/period; flag cross-document disagreements
    for period in res.periods:
        res.chosen[period] = {}
        for metric, fs in by_period[period].items():
            best, others = choose(fs)
            res.chosen[period][metric] = best
            res.facts_used.append(best)
            differing = [o for o in others if o.value != best.value and o.document_id != best.document_id]
            if differing:
                res.facts_used.extend(differing)
                res.conflicts.append({
                    "type": "financial", "topic": f"{METRIC_NAMES[metric]} ({period})",
                    "values": [{"value": money(x.value, x.currency or "AED"), "document": x.title,
                                "filename": x.filename, "raw": x.raw, "preliminary": x.preliminary}
                               for x in [best] + differing],
                    "resolution": f"Using {money(best.value)} from '{best.title}'"
                                  + (" (final figure preferred over preliminary)" if any(o.preliminary for o in differing) and not best.preliminary else " (most recent document)")
                                  + ". Confirm with Finance before external use.",
                })

    # 2) per-period ratios and checks
    for period in res.periods:
        c = res.chosen[period]
        v = {m: f.value for m, f in c.items()}
        cur = next((f.currency for f in c.values() if f.currency), "AED")

        def calc(cid, name, formula, inputs, result, kind):
            res.calculations.append({
                "id": cid, "period": period, "name": name, "formula": formula,
                "inputs": [{"metric": METRIC_NAMES.get(m, m), "value": money(c[m].value, cur), **c[m].source()}
                           for m in inputs if m in c],
                "result": str(result),
                "display": pct(result) if kind == "ratio" else money(result, cur),
                "kind": kind,
            })

        def check(name, metric, computed, formula):
            if metric not in c:
                return
            reported = c[metric].value
            diff = reported - computed
            ok = abs(diff) <= max(Decimal(1), abs(computed) * TOLERANCE)
            res.checks.append({
                "period": period, "check": name, "formula": formula,
                "reported": money(reported, cur), "computed": money(computed, cur),
                "difference": money(diff, cur), "status": "ok" if ok else "mismatch",
                "source": c[metric].source(),
            })

        rev = v.get("revenue")
        gp = None
        if rev is not None and "cogs" in v:
            gp_calc = rev - abs(v["cogs"])
            check("Gross profit = Revenue - COGS", "gross_profit", gp_calc, "revenue - cogs")
            gp = gp_calc
        elif "gross_profit" in v:
            gp = v["gross_profit"]
        if gp is not None and rev:
            calc("gross_margin", "Gross margin", "(revenue - cogs) / revenue", ["revenue", "cogs"], gp / rev, "ratio")

        comps = [m for m in OPEX_COMPONENTS if m in v]
        opex = None
        if comps:
            opex_calc = sum((abs(v[m]) for m in comps), Decimal(0))
            if "total_opex" in v:
                check(f"Total opex = sum of {len(comps)} line items", "total_opex", opex_calc,
                      " + ".join(comps))
            opex = opex_calc
        elif "total_opex" in v:
            opex = abs(v["total_opex"])
        if opex is not None and rev:
            calc("opex_ratio", "Operating expense ratio", "total_opex / revenue",
                 ["total_opex", *comps], opex / rev, "ratio")

        if gp is not None and opex is not None:
            op_calc = gp - opex
            check("Operating profit = Gross profit - Total opex", "operating_profit", op_calc,
                  "gross_profit - total_opex")
            calc("operating_profit", "Operating profit (recomputed)", "gross_profit - total_opex",
                 ["revenue", "cogs", "total_opex"], op_calc, "amount")
            if rev:
                calc("operating_margin", "Operating margin", "(gross_profit - total_opex) / revenue",
                     ["revenue", "cogs", "total_opex"], op_calc / rev, "ratio")
        if "net_profit" in v and rev:
            calc("net_margin", "Net margin", "net_profit / revenue", ["net_profit", "revenue"],
                 v["net_profit"] / rev, "ratio")
        if "budget_revenue" in v and rev is not None and v["budget_revenue"]:
            calc("budget_variance", "Revenue vs budget", "(revenue - budget) / budget",
                 ["revenue", "budget_revenue"], (rev - v["budget_revenue"]) / v["budget_revenue"], "ratio")

    # 3) period-over-period growth
    for prev, curr in zip(res.periods, res.periods[1:]):
        for metric, label in (("revenue", "Revenue growth"), ("net_profit", "Net profit growth")):
            a, b = res.chosen[prev].get(metric), res.chosen[curr].get(metric)
            if a and b and a.value:
                g = (b.value - a.value) / abs(a.value)
                res.calculations.append({
                    "id": f"{metric}_growth", "period": f"{prev} → {curr}", "name": label,
                    "formula": f"({metric}[{curr}] - {metric}[{prev}]) / {metric}[{prev}]",
                    "inputs": [{"metric": f"{METRIC_NAMES[metric]} {p}", "value": money(f.value), **f.source()}
                               for p, f in ((prev, a), (curr, b))],
                    "result": str(g), "display": pct(g), "kind": "ratio",
                })
    return res


RELEVANCE = {
    "gross_margin": ["margin", "gross", "profitab", "cogs", "cost of"],
    "opex_ratio": ["opex", "expense", "cost", "spend", "efficien"],
    "operating_profit": ["operating", "profit", "ebit"],
    "operating_margin": ["operating", "margin", "profitab"],
    "net_margin": ["net", "margin", "profitab", "bottom line"],
    "budget_variance": ["budget", "target", "plan", "variance"],
    "revenue_growth": ["growth", "grow", "increase", "trend", "change", "compare", "versus", "vs"],
    "net_profit_growth": ["growth", "grow", "trend", "compare", "versus", "vs"],
}


def select_relevant(res: FinanceResult, question: str, periods: list[str]) -> list[dict]:
    q = question.lower()
    in_scope = [c for c in res.calculations
                if not periods or any(p in c["period"] for p in periods)]
    wanted = {cid for cid, kws in RELEVANCE.items() if any(k in q for k in kws)}
    qualifiers = {"gross_margin": "gross", "operating_margin": "operating", "net_margin": "net"}
    named = {cid for cid, word in qualifiers.items() if word in q}
    if "margin" in q and named:
        # "gross margin" means gross margin only, not every margin
        wanted = (wanted - set(qualifiers)) | named
        if "operating" not in q:
            wanted.discard("operating_profit")
    if not wanted:
        if requested_metrics(question) and not any(b in q for b in BROAD_TERMS):
            return []                         # plain lookup ("Q1 revenue?") -> figures only
        return in_scope                       # broad question ("analyse Q2") -> everything
    return [c for c in in_scope if c["id"] in wanted]


# ------------------------------------------------------------------ LLM number guard
_NUM = re.compile(r"(?<![\w\[])(-?\d[\d,]*(?:\.\d+)?)\s*(%|million|mn|m\b|k\b|thousand)?", re.I)


def _allowed_values(res: FinanceResult) -> list[Decimal]:
    vals = [f.value for f in res.facts_used]
    for c in res.calculations:
        r = Decimal(c["result"])
        vals += [r, r * 100] if c["kind"] == "ratio" else [r]
    for chk in res.checks:
        for k in ("reported", "computed", "difference"):
            vals.append(Decimal(chk[k].replace("AED", "").replace(",", "").replace(" ", "")))
    return vals


def unsupported_numbers(text: str, res: FinanceResult) -> list[str]:
    """Numbers in an LLM narrative that match no source figure or computed result."""
    allowed = _allowed_values(res)
    bad = []
    for m in _NUM.finditer(text):
        raw, unit = m.group(1), (m.group(2) or "").lower()
        try:
            n = Decimal(raw.replace(",", ""))
        except Exception:  # noqa: BLE001
            continue
        if unit in ("million", "mn", "m"):
            n *= 1_000_000
        elif unit in ("k", "thousand"):
            n *= 1_000
        if not unit and n == n.to_integral() and (abs(n) <= 12 or 1900 <= n <= 2100):
            continue                          # quarter numbers, counts, years
        if any(abs(abs(n) - abs(a)) <= max(Decimal("0.01"), abs(a) * TOLERANCE) for a in allowed):
            continue
        bad.append(m.group(0).strip())
    return bad


def narrative(res: FinanceResult, calcs: list[dict], periods: list[str],
              metrics: list[str] | None = None) -> str:
    """Deterministic short analysis (used without an LLM, or when the LLM's text fails checks)."""
    lines = []
    scope = periods or res.periods
    show = [m for m in (metrics or []) if m in METRIC_NAMES] or ["revenue", "net_profit"]
    for p in scope:
        c = res.chosen.get(p)
        if not c:
            continue
        parts = [f"{METRIC_NAMES[m].lower()} {money(c[m].value)}" for m in show if m in c]
        if parts:
            lines.append(f"- {p}: " + ", ".join(parts) + ".")
    for c in calcs:
        lines.append(f"- {c['name']} ({c['period']}): {c['display']}  [{c['formula']}]")
    bad = [k for k in res.checks if k["status"] == "mismatch" and (not periods or k["period"] in periods)]
    for k in bad:
        lines.append(f"- ⚠ {k['period']}: reported figure fails check '{k['check']}': reported "
                     f"{k['reported']} vs computed {k['computed']} (difference {k['difference']}).")
    trend = [c for c in calcs if c["id"] == "revenue_growth"]
    if trend:
        g = Decimal(trend[0]["result"])
        lines.append(f"- Assessment: revenue {'grew' if g > 0 else 'declined'} {pct(abs(g))} period-on-period"
                     + ("; source figures contain a calculation error that should be corrected before reporting."
                        if bad else "."))
    elif bad:
        lines.append("- Assessment: source figures contain a calculation error that should be corrected before reporting.")
    return "\n".join(lines)
