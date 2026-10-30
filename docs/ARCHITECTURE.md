# Architecture and technical explanation

![Architecture](architecture.png)

## Request flow: asking a question
1. **Auth**: `app.py` verifies the JWT signature and expiry, then reloads the user's role from the database.
2. **Route**: `router.py` scores the question against finance and HR lexicons and picks one assistant (UI override possible).
3. **Permission scope**: `allowed_classifications(role)` gives `('public',)` or `('public','confidential')`.
4. **Evidence**:
   * `retrieval.py` builds and caches a BM25 index **per permission scope** from rows SQL has already filtered.
     Restricted chunks are absent from the index and from the term statistics.
   * The Finance and HR advisors also read `financial_facts` / `policy_facts` through the same SQL filter.
5. **Reasoning in code**: finance ratios, growth, variance and reported-figure checks (`finance.py`);
   policy conflicts and missing topics (`advisors.py`).
6. **Generation** (`grounding.py`):
   * Sources are numbered `[S1..Sn]`, and the model is told to answer only from them, cite everything, treat
     document text as data, and reply `INSUFFICIENT_EVIDENCE` if the sources don't contain the answer.
   * The output is validated: invented citation numbers are stripped, and an answer with no valid citation is
     replaced by an extractive one.
   * Finance narratives must contain only numbers traceable to sources or calculations.
7. **Fallback**: with no LLM configured, or when the provider fails (after retries, or while the circuit breaker
   is open), the answer is built extractively from the best-matching source sentences. It is still cited, and a
   warning is shown.
8. **Audit**: each question is logged (`queries`, `audit_log`). For standard users, a shadow search over
   confidential content detects probing attempts and records them as `denied`, without changing the user's answer.

## Upload flow
`POST /api/documents`, then:
1. Validation: role may upload this classification; extension allow-list; size; file signature; duplicate hash.
2. The file is stored, then the background pipeline (`automation.py`) runs: parse (PDF pages, DOCX paragraphs and
   tables) › chunk › extract facts › summary › database › email › webhook.
3. Each step is recorded with status and attempt count.

## Key design choices
| Choice | Reason |
|---|---|
| Enforce permissions in SQL / index construction, not in the prompt | An LLM cannot be relied on to keep secrets it has seen; prompt injection can't bypass a filter that runs before retrieval. |
| Deterministic fact extraction + `Decimal` maths | Financial numbers must be reproducible and auditable; the LLM only narrates, and its numbers are checked. |
| BM25 + evidence-coverage threshold | Explainable, dependency-free, strong on exact business terms; gives a measurable basis for "insufficient evidence". |
| Effective-date-based conflict resolution | Mirrors how policy documents supersede each other; undated conflicts are escalated rather than guessed. |
| Pluggable LLM with offline mock | The demo and the tests run anywhere with no keys; production can choose a cloud or on-premises model. |
| Starlette + SQLite + vanilla JS | Few dependencies, one process, easy to review; each piece has a documented upgrade path. |

## Data model (SQLite)
`users`, `documents` (classification, category, status, period, effective_date, summary), `chunks`,
`financial_facts`, `policy_facts`, `automation_runs`, `audit_log`, `queries`, `meta` (corpus version for index caching).
