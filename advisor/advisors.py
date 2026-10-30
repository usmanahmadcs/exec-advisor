"""The three answer paths (Knowledge Assistant, Finance Advisor, HR Advisor) and the
orchestrator that routes each question to one of them.

Every path receives `classifications` derived from the caller's role and passes
it into each data access, so permission filtering happens before any text is
retrieved, analysed, or sent to the model.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from decimal import Decimal

from advisor import finance as fin
from advisor import grounding as g
from advisor.config import Settings
from advisor.db import Database
from advisor.facts import TOPIC_NAMES, topics_in
from advisor.llm import LLMClient, LLMError
from advisor.retrieval import Hit, Retriever
from advisor.router import Route, route_question
from advisor.security import allowed_classifications

ADVISOR_LABELS = {"general": "Knowledge Assistant", "finance": "Finance Advisor", "hr": "HR / Management Advisor"}


class AdvisoryService:
    def __init__(self, db: Database, settings: Settings, llm: LLMClient):
        self.db, self.s, self.llm = db, settings, llm
        self.retriever = Retriever(db)

    # ================================================================ entry point
    def ask(self, question: str, user: dict, advisor: str | None = None) -> dict:
        t0 = time.perf_counter()
        question = question.strip()
        classes = allowed_classifications(user["role"])
        if advisor in ADVISOR_LABELS:
            route = Route(advisor, {}, {}, method="manual", confidence=1.0)
        else:
            route = route_question(question, self.llm)
            if route.advisor == "general" and topics_in(question):
                # a recognised HR policy topic is a strong signal even without HR keywords
                route.advisor, route.method = "hr", "keywords+policy-topic"

        handler = {"finance": self._finance, "hr": self._hr}.get(route.advisor, self._general)
        ans: g.Answer = handler(question, classes)
        if ans.status == "insufficient_evidence" and ans.answer == g.INSUFFICIENT:
            ans.answer = g.INSUFFICIENT_MESSAGE
        if ans.llm_error and (self.llm.is_generative or self.llm.simulate_outage):
            ans.warnings.insert(0, f"AI model unavailable ({ans.llm_error}). Showing a degraded, "
                                   "extractive answer built directly from the source text.")
        latency = int((time.perf_counter() - t0) * 1000)

        self._log_query(user, ans, question, latency)
        if user["role"] != "admin":
            self._detect_restricted_probe(question, user, ans)

        out = ans.as_dict()
        out.update({"advisor_label": ADVISOR_LABELS[ans.advisor], "routing": route.as_dict(),
                    "llm": self.llm.info(), "latency_ms": latency})
        return out

    # ================================================================ knowledge assistant
    def _general(self, question: str, classes: tuple[str, ...]) -> g.Answer:
        res = self.retriever.search(question, classes, self.s.top_k)
        ans = g.Answer("general", "answered", "", retrieval=self._retrieval_info(res))
        if not res.sufficient(self.s.min_evidence_coverage):
            return self._insufficient(ans, res.missing_terms)
        text, cites, mode, err = g.generate(question, res.hits, self.llm)
        return self._finish(ans, text, cites, mode, err, res.hits)

    # ================================================================ HR advisor
    def _hr(self, question: str, classes: tuple[str, ...]) -> g.Answer:
        res = self.retriever.search(question, classes, self.s.top_k, categories=("hr", "general"))
        ans = g.Answer("hr", "answered", "", retrieval=self._retrieval_info(res))
        topics = topics_in(question)
        facts = self._policy_facts(topics, classes)

        for topic in topics:
            rows = facts.get(topic, [])
            if not rows:
                ans.missing.append(f"{TOPIC_NAMES[topic]}: no accessible policy document specifies this.")
                continue
            distinct = {(r["value"], r["unit"]) for r in rows}
            if len(distinct) > 1 and len({r["document_id"] for r in rows}) > 1:
                ans.conflicts.append(self._policy_conflict(topic, rows))
            for r in rows:
                if not r["effective_date"]:
                    ans.warnings.append(f"'{r['title']}' has no effective date, so it is unclear whether "
                                        "it is current.")

        if not res.sufficient(self.s.min_evidence_coverage) and not any(facts.values()):
            return self._insufficient(ans, res.missing_terms)

        hits = res.hits
        context = "\n".join(
            f"CONFLICT on {c['topic']}: " + "; ".join(f"{v['value']} per '{v['document']}' "
                                                      f"(effective {v['effective_date'] or 'unknown'})"
                                                      for v in c["values"])
            for c in ans.conflicts)
        text, cites, mode, err = g.generate(
            question, hits, self.llm, extra_context=context,
            extra_rules="When documents conflict, state both values with citations and say which "
                        "is more recent; recommend confirming with HR.")
        ans = self._finish(ans, text, cites, mode, err, hits)
        if ans.status == "answered":
            notes = []
            for c in ans.conflicts:
                notes.append(f"⚠ Conflicting information on {c['topic'].lower()}: "
                             + " vs ".join(f"{v['value']} ('{v['document']}')" for v in c["values"])
                             + f". {c['resolution']}")
            for m in ans.missing:
                notes.append(f"ℹ Missing information - {m}")
            if notes and mode != "generative":
                ans.answer += "\n\n" + "\n".join(notes)
        return ans

    def _policy_facts(self, topics: list[str], classes: tuple[str, ...]) -> dict[str, list[dict]]:
        if not topics:
            return {}
        rows = self.db.query(
            f"""SELECT p.topic, p.value, p.unit, p.raw, p.chunk_id, d.id AS document_id, d.title,
                       d.filename, d.effective_date, d.classification
                FROM policy_facts p JOIN documents d ON d.id = p.document_id
                WHERE d.status = 'ready'
                  AND d.classification IN ({','.join('?' * len(classes))})
                  AND p.topic IN ({','.join('?' * len(topics))})""",
            (*classes, *topics))
        out: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            out[r["topic"]].append(r)
        return out

    @staticmethod
    def _policy_conflict(topic: str, rows: list[dict]) -> dict:
        per_doc = {}
        for r in rows:
            per_doc.setdefault(r["document_id"], r)
        values = sorted(per_doc.values(), key=lambda r: r["effective_date"] or "", reverse=True)
        newest = values[0]
        dated = [r for r in values if r["effective_date"]]
        if len(dated) == len(values) and len({r["effective_date"] for r in values}) == len(values):
            resolution = (f"The most recent document ('{newest['title']}', effective "
                          f"{newest['effective_date']}) likely supersedes the others; confirm with HR.")
        else:
            resolution = "Effective dates do not establish which document prevails; escalate to HR."
        return {
            "type": "policy", "topic": TOPIC_NAMES[topic],
            "values": [{"value": f"{r['value']} {r['unit']}", "document": r["title"], "filename": r["filename"],
                        "effective_date": r["effective_date"], "raw": r["raw"]} for r in values],
            "resolution": resolution,
        }

    # ================================================================ finance advisor
    def _finance(self, question: str, classes: tuple[str, ...]) -> g.Answer:
        res = self.retriever.search(question, classes, self.s.top_k, categories=("finance", "general"))
        ans = g.Answer("finance", "answered", "", mode="deterministic", retrieval=self._retrieval_info(res))
        if not fin.is_calculation_question(question) and not fin.unsupported_metrics(question):
            # Narrative question (e.g. "what is Project Osprey?"): answer from retrieved text
            if not res.sufficient(self.s.min_evidence_coverage):
                return self._insufficient(ans, res.missing_terms)
            text, cites, mode, err = g.generate(question, res.hits, self.llm)
            return self._finish(ans, text, cites, mode, err, res.hits)
        facts = self._financial_facts(classes)
        analysis = fin.analyse(facts)
        periods = fin.requested_periods(question, analysis.periods)
        unavailable = [p for p in periods if p not in analysis.periods]
        periods = [p for p in periods if p in analysis.periods]

        for p in unavailable:
            ans.missing.append(f"No financial figures for {p} were found in the documents available to you.")
        for name in fin.unsupported_metrics(question):
            ans.missing.append(f"{name}: no source document reports this metric, so it cannot be calculated.")
        if unavailable and not periods:
            return self._insufficient(ans, [])
        if not facts or (not periods and analysis.periods == [] and not res.sufficient(self.s.min_evidence_coverage)):
            return self._insufficient(ans, res.missing_terms)
        if fin.unsupported_metrics(question) and not fin.requested_metrics(question):
            return self._insufficient(ans, [])

        calcs = fin.select_relevant(analysis, question, periods)
        ans.calculations = calcs
        ans.conflicts = [c for c in analysis.conflicts
                         if not periods or any(p in c["topic"] for p in periods)]
        checks = [k for k in analysis.checks if not periods or k["period"] in periods]
        for k in checks:
            if k["status"] == "mismatch":
                ans.warnings.append(
                    f"Calculation error in source: {k['period']} '{k['check']}' - reported {k['reported']} "
                    f"but inputs give {k['computed']} ('{k['source']['title']}').")
        ans.retrieval["checks"] = checks

        # citations: every chunk that supplied a number
        in_scope = [f.chunk_id for f in analysis.facts_used if not periods or f.period in periods]
        calc_inputs = [i["chunk_id"] for c in calcs for i in c["inputs"]]   # e.g. Q1 figures for Q1->Q2 growth
        chunk_ids = list(dict.fromkeys(in_scope + calc_inputs))
        hits = self._hits_for_chunks(chunk_ids)
        ref = {h.chunk_id: f"S{i}" for i, h in enumerate(hits, 1)}
        ans.citations = [g.make_citation(ref[h.chunk_id], h) for h in hits]
        for c in calcs:
            c["sources"] = sorted({ref[i["chunk_id"]] for i in c["inputs"] if i["chunk_id"] in ref})
        ans.documents_used = sorted({h.document_id for h in hits})

        deterministic = fin.narrative(analysis, calcs, periods, fin.requested_metrics(question))
        ans.answer = deterministic
        if self.llm.is_generative or self.llm.simulate_outage:
            facts_text = "\n".join(f"{c['name']} ({c['period']}) = {c['display']} [{', '.join(c.get('sources', []))}]"
                                   for c in calcs)
            facts_text += "\n" + "\n".join(
                f"CHECK {k['period']} {k['check']}: {k['status']} (reported {k['reported']}, computed {k['computed']})"
                for k in checks)
            try:
                r = self.llm.complete(
                    g.SYSTEM_PROMPT + "\nYou are the Finance Advisor. Write a short analysis (max 5 bullets) "
                    "using ONLY numbers that appear in VERIFIED FACTS or SOURCES. Never compute new numbers. "
                    "Mention any CHECK marked mismatch.",
                    f"SOURCES:\n{g.format_sources(hits)}\n\nVERIFIED FACTS:\n{facts_text}\n\nQUESTION: {question}")
                bad = fin.unsupported_numbers(r.text, analysis)
                if bad:
                    ans.warnings.append(
                        "The AI narrative contained figure(s) that do not match any source or computed value "
                        f"({', '.join(bad[:5])}); it was discarded and the code-verified analysis is shown.")
                elif g.INSUFFICIENT not in r.text:
                    ans.answer, ans.mode = r.text, "generative"
                    ans.retrieval["deterministic_analysis"] = deterministic
            except LLMError as e:
                ans.llm_error = str(e)
        if not calcs and not checks and not fin.requested_metrics(question):
            text, cites, mode, err = g.generate(question, res.hits, self.llm)
            return self._finish(ans, text, cites, mode, err or ans.llm_error, res.hits)
        if ans.missing:
            ans.answer += "\n\n" + "\n".join(f"ℹ Missing information - {m}" for m in ans.missing)
        return ans

    def _financial_facts(self, classes: tuple[str, ...]) -> list[fin.Fact]:
        rows = self.db.query(
            f"""SELECT f.id, f.document_id, f.chunk_id, f.metric, f.period, f.value, f.currency, f.raw,
                       d.title, d.filename
                FROM financial_facts f JOIN documents d ON d.id = f.document_id
                WHERE d.status = 'ready' AND d.classification IN ({','.join('?' * len(classes))})
                ORDER BY f.id""", classes)
        return [fin.Fact(**{**r, "value": Decimal(r["value"])}) for r in rows]

    def _hits_for_chunks(self, chunk_ids: list[int]) -> list[Hit]:
        if not chunk_ids:
            return []
        rows = self.db.query(
            f"""SELECT c.id AS chunk_id, c.document_id, c.page, c.text, d.title, d.filename, d.category,
                       d.classification, d.effective_date
                FROM chunks c JOIN documents d ON d.id = c.document_id
                WHERE c.id IN ({','.join('?' * len(chunk_ids))})""", chunk_ids)
        by_id = {r["chunk_id"]: r for r in rows}
        return [Hit(score=0.0, **by_id[i]) for i in chunk_ids if i in by_id]

    # ================================================================ helpers
    def _finish(self, ans: g.Answer, text: str, cites, mode: str, err: str | None, hits: list[Hit]) -> g.Answer:
        ans.mode, ans.llm_error = mode, err
        if text == g.INSUFFICIENT or text == g.INSUFFICIENT_MESSAGE or not cites:
            return self._insufficient(ans, ans.retrieval.get("missing_terms", []))
        ans.answer, ans.citations = text, cites
        ans.documents_used = sorted({c.document_id for c in cites})
        return ans

    @staticmethod
    def _insufficient(ans: g.Answer, missing_terms: list[str]) -> g.Answer:
        ans.status = "insufficient_evidence"
        ans.answer = g.INSUFFICIENT_MESSAGE
        if missing_terms:
            ans.answer += f" (No supporting evidence found for: {', '.join(missing_terms)}.)"
        if ans.missing:
            ans.answer += "\n\n" + "\n".join(f"ℹ {m}" for m in ans.missing)
        ans.citations, ans.calculations = [], []
        return ans

    @staticmethod
    def _retrieval_info(res) -> dict:
        return {"coverage": round(res.coverage, 2), "missing_terms": res.missing_terms,
                "query_terms": res.query_terms,
                "top_hits": [{"title": h.title, "score": h.score, "matched": h.matched_terms} for h in res.hits[:3]]}

    def _log_query(self, user: dict, ans: g.Answer, question: str, latency: int) -> None:
        self.db.execute(
            "INSERT INTO queries(ts, username, advisor, status, question, answer, documents_used, flags, latency_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), user["username"], ans.advisor, ans.status, question[:2000], ans.answer[:4000],
             json.dumps(ans.documents_used),
             json.dumps({"warnings": ans.warnings, "conflicts": len(ans.conflicts), "missing": ans.missing,
                         "mode": ans.mode}), latency))
        self.db.audit(user, "ask", ans.advisor, "allowed",
                      {"status": ans.status, "documents_used": ans.documents_used, "mode": ans.mode})

    def _detect_restricted_probe(self, question: str, user: dict, ans: g.Answer) -> None:
        """Security monitoring: did this question target content the user may not see?

        Runs a shadow search over confidential documents only. The user's answer is
        unaffected (it was produced from public documents), but a strong match is
        written to the audit log so administrators can spot probing attempts.
        """
        shadow = self.retriever.search(question, ("confidential",), top_k=3)
        if shadow.sufficient(self.s.min_evidence_coverage) and ans.status == "insufficient_evidence":
            self.db.audit(user, "restricted_content_request", "confidential documents", "denied",
                          {"question": question[:300],
                           "matched_documents": sorted({h.document_id for h in shadow.hits})})
