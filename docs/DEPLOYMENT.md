# Deployment, monitoring, maintenance and expansion

## 1. Secure deployment

### Minimum production checklist
| Area | Action |
|---|---|
| Secrets | Set a long random `JWT_SECRET` (for example `python -c "import secrets;print(secrets.token_urlsafe(48))"`), real admin/user passwords and API keys in a secret store (Docker/K8s secrets, Azure Key Vault, AWS Secrets Manager). Never commit `.env`. |
| Transport | Run behind a TLS-terminating reverse proxy (Caddy, Nginx, Traefik, Azure App Gateway). The container binds to `127.0.0.1` by default. Add HSTS at the proxy. |
| Identity | Replace the two demo accounts with SSO (OIDC / Azure AD / Google Workspace). Map IdP groups to `admin` / `user`. `security.py` is the only place that needs to change. |
| Container | Ships as non-root (`uid 10001`) with a read-only root filesystem, `no-new-privileges`, and only `/data` writable (see `docker-compose.yml`). |
| Data at rest | Put `/data` on an encrypted volume. Back it up nightly (`sqlite3 advisor.db ".backup ..."` plus the `uploads/` folder). |
| Network egress | Allow outbound traffic only to the chosen LLM endpoint, SMTP relay and webhook host. Use Ollama on-premises when documents must not leave the network. |
| LLM data policy | Use an API tier with zero data retention or no training on inputs. Confidential text is only ever sent to the model for administrator queries. |
| Uploads | Size limit, extension allow-list and file-signature check are built in. For untrusted uploaders, add antivirus scanning (ClamAV) before `process_document`. |
| Rate limiting | Add per-IP / per-user limits at the proxy (for example Nginx `limit_req`) on `/api/auth/login` and `/api/ask`. |

### Hosting options
* **Single VM / on-premises server**: `docker compose up -d` behind Caddy. Good for up to a few hundred users.
* **Azure App Service / AWS ECS / Google Cloud Run**: deploy the same image. Mount persistent storage on `/data`, or
  move to managed PostgreSQL and blob storage (see *Expansion*).

## 2. Monitoring

| Signal | Where |
|---|---|
| Liveness / readiness | `GET /api/health` (database check + LLM provider state). The Docker `HEALTHCHECK` already uses it. |
| Request logs | One structured line per request with method, path, status, latency and `X-Request-ID`. Ship stdout to Loki, ELK, CloudWatch or Azure Monitor. |
| Business metrics | `GET /api/admin/metrics`: documents by status, questions by advisor and status, average latency, automation failures, denied events in the last 24 h, LLM circuit-breaker state. |
| Security | `audit_log` table and *Admin › Security audit log*. Alert when `denied` events exceed a threshold, or on any `restricted_content_request`. |
| Quality | `queries` table: track the insufficient-evidence rate, the share of extractive fallbacks (signals LLM trouble) and finance warnings. Review a weekly sample of answers. |
| Automation | `automation_runs` with `status='failed'`, so you can alert on webhook or email failures. |

Suggested alerts: health check failing for 2 minutes; circuit breaker open; more than 5 % of answers in extractive
fallback; any automation step failing 3 times in a row; a spike in `denied` events.

## 3. Maintenance

* **Dependencies**: `requirements.txt` sets minimum versions. Pin exact versions with `pip-compile`, run
  `pip-audit` in CI, and rebuild the image monthly.
* **Tests**: run `python -m unittest discover -s tests -t .` in CI on every change. The reliability suite is the
  regression guard for security and finance logic.
* **Re-indexing**: after changing parsing or extraction rules, use the *Re-run* button per document, or loop over
  `POST /api/documents/{id}/reprocess`.
* **Document lifecycle**: give each policy an `Effective date:` line so conflicts resolve automatically. Delete or
  archive superseded documents.
* **Model changes**: switch with `LLM_PROVIDER` / `LLM_MODEL`. Before switching in production, run the test suite
  and a fixed set of evaluation questions.
* **Backups and restore drills**: restore the SQLite file plus `uploads/` into a staging container quarterly.

## 4. Expansion path

| Need | Change |
|---|---|
| Better recall on paraphrased questions | Add embeddings (pgvector, Qdrant or FAISS) and fuse with BM25 (reciprocal rank fusion) in `retrieval.py`. Keep the classification filter **inside** the vector query. |
| More users / multiple instances | SQLite → PostgreSQL (all SQL lives in `db.py` and the advisors). In-process thread pool → Celery, RQ or Azure Functions queue. Files → S3 or Azure Blob. |
| Finer-grained permissions | Replace the two classifications with document ACLs (department, project, user groups) joined in the same SQL filter. |
| More advisors (Legal, Sales, Operations) | Add a lexicon to `router.py` and a handler in `advisors.py`. Grounding, citations and audit are shared. |
| Richer financial analysis | Multi-period table parser, XLSX ingestion, cash-flow and balance-sheet checks, forecasting with statsmodels. |
| More automations | Additional pipeline steps: write to Google Sheets or SharePoint, create Jira/Asana tasks for documents that need review, schedule a weekly executive digest email. |
| OCR | Add Tesseract or Azure Document Intelligence as a parsing fallback for scanned PDFs. |
