"""Permission-aware lexical retrieval (BM25) with an evidence-sufficiency check.

Security model: an index is built *per set of allowed classifications*, from
rows that SQL has already filtered. Restricted chunks are therefore absent from
the index, the IDF statistics and the candidate set - they cannot be ranked,
quoted, summarised or leaked through term statistics.

BM25 was chosen over embeddings for the prototype because it is deterministic,
explainable (matched terms are shown to the user), dependency-free and strong
on business documents full of exact terms ("probation", "COGS"). The
`Retriever` interface is small enough to swap in hybrid BM25 + vector search.
"""
from __future__ import annotations

import math
import re
import threading
from collections import Counter
from dataclasses import dataclass, field

from advisor.db import Database

STOPWORDS = set("""
a about above after again against all am an and any are as at be because been before being
below between both but by can could did do does doing down during each few for from further
had has have having he her here hers herself him himself his how i if in into is it its itself
just me more most my myself no nor not now of off on once only or other our ours ourselves out
over own same she should so some such than that the their theirs them themselves then there
these they this those through to too under until up very was we were what when where which
while who whom why will with would you your yours yourself yourselves tell show give please
explain describe know find get list according say says said
""".split())

# Words that carry little meaning in a business-question context. They still
# contribute to ranking but are ignored by the evidence-coverage check.
GENERIC_TERMS = set("""
company policy policies employee employees staff document documents information details
much many total amount number rate current currently latest new year annual per our us
summary overview report reports state allowed entitled
""".split())


def _stem(word: str) -> str:
    """Tiny suffix stripper - enough to match leave/leaves, expense/expenses, etc."""
    for suffix in ("ies", "ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            if suffix == "ies":
                return word[:-3] + "y"
            if suffix == "es" and not word.endswith(("ses", "xes", "ches", "shes")):
                return word[:-1]   # "leaves" -> "leave", "expenses" -> "expense"
            return word[: -len(suffix)]
    return word


# Query-side expansions for common business abbreviations / synonyms (keys are stems)
SYNONYMS: dict[str, list[str]] = {
    "ceo": ["chief", "executive", "officer"],
    "cfo": ["chief", "financial", "officer"],
    "hr": ["human", "resource"],
    "wfh": ["remote", "home"],
    "home": ["remotely", "remote"],
    "vacation": ["annual", "leave"],
    "holiday": ["annual", "leave"],
    "pay": ["salary", "compensation"],
    "salary": ["compensation", "pay"],
    "boss": ["manager"],
    "profit": ["income"],
    "income": ["profit"],
    "sale": ["revenue"],
    "turnover": ["revenue"],
    "valuation": ["value"],
}


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+(?:[-'][a-z0-9]+)*", text.lower())
    return [_stem(w) for w in words if w not in STOPWORDS and (len(w) > 1 or w.isdigit())]


@dataclass
class Hit:
    chunk_id: int
    document_id: int
    title: str
    filename: str
    category: str
    classification: str
    effective_date: str | None
    page: int | None
    text: str
    score: float
    matched_terms: list[str] = field(default_factory=list)


@dataclass
class RetrievalResult:
    hits: list[Hit]
    query_terms: list[str]
    coverage: float            # IDF-weighted share of specific query terms found in the hits
    missing_terms: list[str]

    def sufficient(self, threshold: float) -> bool:
        return bool(self.hits) and self.coverage >= threshold


class _Index:
    def __init__(self, rows: list[dict], k1: float = 1.5, b: float = 0.75):
        self.rows = rows
        self.k1, self.b = k1, b
        self.tfs = [Counter(tokenize(f"{r['title']}\n{r['text']}")) for r in rows]
        self.lens = [sum(tf.values()) for tf in self.tfs]
        self.avgdl = (sum(self.lens) / len(self.lens)) if self.lens else 1.0
        df: Counter = Counter()
        for tf in self.tfs:
            df.update(tf.keys())
        n = len(rows)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        self.max_idf = math.log(1 + (n + 0.5) / 0.5) if n else 1.0   # idf of an unseen term

    def score(self, terms: list[str], i: int) -> float:
        tf, dl, s = self.tfs[i], self.lens[i], 0.0
        for t in terms:
            f = tf.get(t, 0)
            if f:
                s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
        return s


class Retriever:
    def __init__(self, db: Database):
        self.db = db
        self._cache: dict[tuple, tuple[int, _Index]] = {}
        self._lock = threading.Lock()

    def _index_for(self, classifications: tuple[str, ...]) -> _Index:
        key = tuple(sorted(classifications))
        version = self.db.corpus_version()
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] == version:
                return cached[1]
        placeholders = ",".join("?" * len(key))
        rows = self.db.query(
            f"""SELECT c.id AS chunk_id, c.document_id, c.page, c.text, d.title, d.filename,
                       d.category, d.classification, d.effective_date
                FROM chunks c JOIN documents d ON d.id = c.document_id
                WHERE d.status = 'ready' AND d.classification IN ({placeholders})
                ORDER BY c.id""",
            key,
        )
        index = _Index(rows)
        with self._lock:
            self._cache[key] = (version, index)
        return index

    def search(self, query: str, classifications: tuple[str, ...], top_k: int = 6,
               categories: tuple[str, ...] | None = None) -> RetrievalResult:
        index = self._index_for(classifications)
        base = list(dict.fromkeys(tokenize(query)))
        terms = list(dict.fromkeys(base + [x for t in base for x in SYNONYMS.get(t, [])]))
        scored = []
        for i, row in enumerate(index.rows):
            if categories and row["category"] not in categories:
                continue
            s = index.score(terms, i)
            if s > 0:
                scored.append((s, i))
        scored.sort(reverse=True)
        hits = []
        for s, i in scored[:top_k]:
            r = index.rows[i]
            hits.append(Hit(
                chunk_id=r["chunk_id"], document_id=r["document_id"], title=r["title"],
                filename=r["filename"], category=r["category"], classification=r["classification"],
                effective_date=r["effective_date"], page=r["page"], text=r["text"], score=round(s, 3),
                matched_terms=[t for t in terms if index.tfs[i].get(t)],
            ))
        coverage, missing = self._coverage(index, base, hits)
        return RetrievalResult(hits, base, coverage, missing)

    @staticmethod
    def _coverage(index: _Index, terms: list[str], hits: list[Hit]) -> tuple[float, list[str]]:
        """IDF-weighted fraction of the *specific* query terms present in the top hits.

        Terms never seen in the (accessible) corpus get the maximum weight: a question
        about "parental leave" against documents that never mention "parental" is not
        answerable, even though "leave" matches strongly.
        """
        specific = [t for t in terms if t not in GENERIC_TERMS] or terms
        if not specific:
            return 0.0, []
        found = {t for h in hits for t in h.matched_terms}
        total = matched = 0.0
        missing = []
        for t in specific:
            syn = SYNONYMS.get(t, [])
            w = index.idf.get(t, max([index.idf[x] for x in syn if x in index.idf], default=index.max_idf))
            total += w
            if t in found or (syn and all(x in found for x in syn[:2])):
                matched += w
            else:
                missing.append(t)
        return (matched / total if total else 0.0), missing
