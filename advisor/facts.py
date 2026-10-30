"""Deterministic extraction of structured facts at ingest time.

* Financial figures ("Revenue: AED 4,200,000", table rows "Revenue | AED 4.2m")
  -> financial_facts, used by the Finance Advisor's code-based calculations.
* HR policy parameters ("22 working days of annual leave", "probation of 6 months")
  -> policy_facts, used by the HR Advisor's conflict / missing-information checks.

Doing this with rules (not the LLM) makes the numbers reproducible and auditable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# ============================================================ financial figures
METRIC_LABELS: dict[str, list[str]] = {
    "budget_revenue": ["budgeted revenue", "revenue budget", "budget revenue", "revenue target"],
    "revenue": ["total revenue", "net revenue", "revenue", "net sales", "sales"],
    "cogs": ["cost of goods sold", "cost of sales", "cogs"],
    "gross_profit": ["gross profit"],
    "opex_salaries": ["salaries and benefits", "salaries"],
    "opex_marketing": ["marketing"],
    "opex_rent": ["rent and facilities", "rent"],
    "opex_other": ["other operating expenses"],
    "total_opex": ["total operating expenses", "total opex"],
    "operating_profit": ["operating profit", "operating income", "ebit"],
    "net_profit": ["net profit", "net income"],
    "cash": ["closing cash balance", "closing cash", "cash and cash equivalents"],
}
METRIC_NAMES = {
    "budget_revenue": "Budgeted revenue", "revenue": "Revenue", "cogs": "Cost of goods sold",
    "gross_profit": "Gross profit", "opex_salaries": "Salaries & benefits",
    "opex_marketing": "Marketing", "opex_rent": "Rent & facilities",
    "opex_other": "Other operating expenses", "total_opex": "Total operating expenses",
    "operating_profit": "Operating profit", "net_profit": "Net profit", "cash": "Closing cash",
}
OPEX_COMPONENTS = ("opex_salaries", "opex_marketing", "opex_rent", "opex_other")

_label_to_metric = {lbl: m for m, lbls in METRIC_LABELS.items() for lbl in lbls}
_labels_re = "|".join(re.escape(l) for l in sorted(_label_to_metric, key=len, reverse=True))
_PERIOD = r"(?:Q[1-4]\s*\d{4}|FY\s*\d{4})"
_FIN_LINE = re.compile(
    rf"""^\s*(?:[-*•|]\s*)*
        (?:(?P<period>{_PERIOD})\s+)?
        (?P<label>{_labels_re})\b
        (?P<qual>[^:|\n\d]{{0,40}}?)\s*[:|]\s*
        (?P<cur>AED|USD|EUR|\$)?\s*
        (?P<num>\(?-?\d[\d,]*(?:\.\d+)?\)?)(?![\d]|[.,]\d)\s*
        (?P<scale>million|thousand|mn|m|k)?(?!\s*[A-Za-z%])""",
    re.I | re.X,
)
_SCALE = {"k": 1_000, "thousand": 1_000, "m": 1_000_000, "mn": 1_000_000, "million": 1_000_000}


@dataclass
class FinancialFact:
    metric: str
    period: str | None
    value: Decimal
    currency: str | None
    raw: str


def parse_amount(num: str, scale: str | None = None) -> Decimal:
    negative = num.startswith("(") and num.endswith(")")
    clean = num.strip("()").replace(",", "")
    try:
        value = Decimal(clean)
    except InvalidOperation as e:
        raise ValueError(f"not a number: {num}") from e
    if scale:
        value *= _SCALE[scale.lower()]
    return -value if negative else value


def normalise_period(p: str | None) -> str | None:
    if not p:
        return None
    p = re.sub(r"\s+", " ", p.upper()).strip()
    return re.sub(r"^(Q[1-4]|FY)\s*(\d{4})$", r"\1 \2", p)


def extract_financial_facts(text: str, default_period: str | None) -> list[FinancialFact]:
    facts = []
    for line in text.splitlines():
        m = _FIN_LINE.match(line)
        if not m:
            continue
        if "%" in line[m.end():m.end() + 2]:
            continue                       # percentages are not amounts
        try:
            value = parse_amount(m.group("num"), m.group("scale"))
        except ValueError:
            continue
        cur = m.group("cur")
        facts.append(FinancialFact(
            metric=_label_to_metric[m.group("label").lower()],
            period=normalise_period(m.group("period")) or default_period,
            value=value,
            currency=("AED" if cur is None else ("USD" if cur == "$" else cur.upper())),
            raw=line.strip()[:300],
        ))
    return facts


# ============================================================ HR policy parameters
# Ordered: more specific topics first, so "carried over ... annual leave" is
# classified as carry-over, not as the annual leave entitlement.
POLICY_TOPICS: list[tuple[str, str, list[str]]] = [
    ("carry_over", "Leave carry-over", ["carry over", "carry-over", "carried over", "carried forward"]),
    ("parental_leave", "Parental leave", ["parental leave", "maternity leave", "paternity leave"]),
    ("sick_leave", "Sick leave", ["sick leave"]),
    ("annual_leave", "Annual leave", ["annual leave", "vacation days", "paid leave"]),
    ("probation", "Probation period", ["probation"]),
    ("notice_period", "Notice period", ["notice period"]),
    ("remote_work", "Remote work allowance", ["remote work", "work remotely", "working from home",
                                              "work from home", "hybrid"]),
    ("working_hours", "Working hours", ["working hours", "work hours", "core hours"]),
    ("training_budget", "Training budget", ["training budget", "learning budget"]),
]
TOPIC_NAMES = {t: name for t, name, _ in POLICY_TOPICS}

_VALUE = re.compile(
    r"(?:(?P<cur>AED)\s?(?P<amt>\d[\d,]*))|"
    r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>working days|business days|calendar days|days per week|"
    r"days a week|hours per week|hours per day|hours a day|days|weeks|months|hours)",
    re.I,
)
_NOT_ENTITLEMENT = re.compile(r"^\W*(?:in advance|prior|before|after|of service|of employment|notice)", re.I)
_UNIT_NORMAL = {"business days": "working days", "days a week": "days per week",
                "hours a day": "hours per day"}


@dataclass
class PolicyFact:
    topic: str
    value: str
    unit: str
    raw: str


def topics_in(text: str) -> list[str]:
    low = text.lower()
    return [t for t, _name, terms in POLICY_TOPICS if any(term in low for term in terms)]


def extract_policy_facts(text: str) -> list[PolicyFact]:
    facts = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        low = sentence.lower()
        for topic, _name, terms in POLICY_TOPICS:
            positions = [low.find(t) for t in terms if t in low]
            if not positions:
                continue
            anchor = min(positions)
            best = None
            for m in _VALUE.finditer(sentence):
                if _NOT_ENTITLEMENT.match(sentence[m.end():m.end() + 20]):
                    continue
                dist = abs(m.start() - anchor)
                if best is None or dist < best[0]:
                    best = (dist, m)
            if best:
                m = best[1]
                if m.group("cur"):
                    value, unit = m.group("amt").replace(",", ""), "AED"
                else:
                    value = m.group("num")
                    unit = m.group("unit").lower()
                    unit = _UNIT_NORMAL.get(unit, unit)
                facts.append(PolicyFact(topic, value, unit, sentence.strip()[:300]))
            break      # one (most specific) topic per sentence
    return facts
