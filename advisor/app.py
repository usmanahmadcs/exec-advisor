"""HTTP API + dashboard (Starlette / ASGI).

Run:  python -m advisor.app            (or: uvicorn advisor.app:app)
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from advisor import __version__
from advisor.advisors import AdvisoryService
from advisor.automation import AutomationPipeline
from advisor.config import Settings
from advisor.db import Database
from advisor.ingest import UploadError, delete_document, register_upload
from advisor.llm import LLMClient
from advisor.security import (AuthError, allowed_classifications, authenticate, can_delete,
                              can_upload, decode_token, ensure_user, issue_token)

log = logging.getLogger("advisor.app")
STATIC = Path(__file__).parent / "static"
DOC_FIELDS = ("id, filename, title, category, classification, status, error, num_chunks, "
              "effective_date, period, summary, uploaded_by, uploaded_at, size_bytes")


# ====================================================================== middleware
class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex[:12]
        t0 = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; frame-ancestors 'none'")
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        log.info("%s %s %s %.0fms rid=%s", request.method, request.url.path, response.status_code,
                 (time.perf_counter() - t0) * 1000, request.state.request_id)
        return response


# ====================================================================== app factory
def create_app(settings: Settings | None = None, llm: LLMClient | None = None) -> Starlette:
    s = settings or Settings.from_env()
    s.prepare_dirs()
    db = Database(s.db_path)
    ensure_user(db, s.admin_username, s.admin_password, "admin")
    ensure_user(db, s.user_username, s.user_password, "user")
    llm = llm or LLMClient(s)
    service = AdvisoryService(db, s, llm)
    pipeline = AutomationPipeline(db, s, llm)

    # ---------------------------------------------------------------- helpers
    def current_user(request: Request) -> dict:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise HTTPException(401, "authentication required")
        try:
            return decode_token(auth[7:].strip(), s.jwt_secret, db)
        except AuthError as e:
            raise HTTPException(401, str(e)) from e

    def require_admin(request: Request) -> dict:
        user = current_user(request)
        if user["role"] != "admin":
            db.audit(user, f"{request.method} {request.url.path}", request.url.path, "denied",
                     "administrator role required")
            raise HTTPException(403, "administrator role required")
        return user

    async def json_body(request: Request) -> dict:
        try:
            body = await request.json()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, "request body must be JSON") from e
        if not isinstance(body, dict):
            raise HTTPException(400, "request body must be a JSON object")
        return body

    def visible_document(user: dict, doc_id: int, action: str) -> dict:
        classes = allowed_classifications(user["role"])
        doc = db.one(f"SELECT {DOC_FIELDS}, stored_path FROM documents WHERE id = ?", (doc_id,))
        if doc is None or doc["classification"] not in classes:
            if doc is not None:
                # Same 404 as a missing document: do not reveal that a restricted one exists
                db.audit(user, action, f"document:{doc_id}", "denied",
                         f"{doc['classification']} document requested by role '{user['role']}'")
            raise HTTPException(404, "document not found")
        return doc

    # ---------------------------------------------------------------- routes
    async def index(request: Request):
        return FileResponse(STATIC / "index.html")

    async def health(request: Request):
        try:
            db.one("SELECT 1 AS ok")
            db_ok = True
        except Exception:  # noqa: BLE001
            db_ok = False
        return JSONResponse({"status": "ok" if db_ok else "degraded", "version": __version__,
                             "database": db_ok, "llm": llm.info()}, status_code=200 if db_ok else 503)

    async def login(request: Request):
        body = await json_body(request)
        username, password = str(body.get("username", ""))[:64], str(body.get("password", ""))
        try:
            user = await run_in_threadpool(authenticate, db, username, password, s.login_max_attempts,
                                           s.login_lockout_minutes)
        except AuthError as e:
            db.audit({"username": username}, "login", "auth", "denied", str(e))
            raise HTTPException(401, str(e)) from e
        db.audit(user, "login", "auth", "allowed")
        return JSONResponse({"token": issue_token(user, s.jwt_secret, s.token_ttl_minutes),
                             "user": user, "expires_in": s.token_ttl_minutes * 60})

    async def me(request: Request):
        user = current_user(request)
        return JSONResponse({**user, "can_see": list(allowed_classifications(user["role"]))})

    async def list_documents(request: Request):
        user = current_user(request)
        classes = allowed_classifications(user["role"])
        rows = db.query(f"SELECT {DOC_FIELDS} FROM documents WHERE classification IN "
                        f"({','.join('?' * len(classes))}) ORDER BY uploaded_at DESC", classes)
        return JSONResponse(rows)

    async def upload_document(request: Request):
        user = current_user(request)
        length = int(request.headers.get("content-length") or 0)
        if length > (s.max_upload_mb + 1) * 1024 * 1024:
            raise HTTPException(413, f"upload exceeds {s.max_upload_mb} MB")
        form = await request.form(max_files=1)
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(400, "multipart field 'file' is required")
        classification = str(form.get("classification") or "public").lower()
        category = str(form.get("category") or "auto").lower()
        if not can_upload(user["role"], classification):
            db.audit(user, "upload", upload.filename, "denied",
                     f"role '{user['role']}' may not upload {classification} documents")
            raise HTTPException(403, f"your role cannot upload {classification} documents")
        data = await upload.read()
        await form.close()
        try:
            doc_id = await run_in_threadpool(register_upload, db, s, upload.filename, data, category,
                                             classification, user)
        except UploadError as e:
            db.audit(user, "upload", upload.filename, "error", str(e))
            raise HTTPException(422, str(e)) from e
        db.audit(user, "upload", f"document:{doc_id}", "allowed", {"classification": classification})
        await run_in_threadpool(pipeline.submit, doc_id)
        doc = db.one(f"SELECT {DOC_FIELDS} FROM documents WHERE id = ?", (doc_id,))
        return JSONResponse(doc, status_code=202)

    async def get_document(request: Request):
        user = current_user(request)
        doc = visible_document(user, int(request.path_params["doc_id"]), "view_document")
        doc.pop("stored_path", None)
        doc["runs"] = db.query("SELECT step, status, attempts, detail, created_at FROM automation_runs "
                               "WHERE document_id = ? ORDER BY id", (doc["id"],))
        return JSONResponse(doc)

    async def download_document(request: Request):
        user = current_user(request)
        doc = visible_document(user, int(request.path_params["doc_id"]), "download_document")
        db.audit(user, "download_document", f"document:{doc['id']}", "allowed")
        return FileResponse(doc["stored_path"], filename=doc["filename"])

    async def remove_document(request: Request):
        user = current_user(request)
        doc_id = int(request.path_params["doc_id"])
        if not can_delete(user["role"]):
            db.audit(user, "delete_document", f"document:{doc_id}", "denied", "administrator role required")
            raise HTTPException(403, "administrator role required")
        if not await run_in_threadpool(delete_document, db, doc_id):
            raise HTTPException(404, "document not found")
        db.audit(user, "delete_document", f"document:{doc_id}", "allowed")
        return Response(status_code=204)

    async def reprocess(request: Request):
        user = require_admin(request)
        doc_id = int(request.path_params["doc_id"])
        if not db.one("SELECT id FROM documents WHERE id = ?", (doc_id,)):
            raise HTTPException(404, "document not found")
        db.execute("UPDATE documents SET status = 'processing' WHERE id = ?", (doc_id,))
        db.audit(user, "reprocess_document", f"document:{doc_id}", "allowed")
        await run_in_threadpool(pipeline.submit, doc_id)
        return JSONResponse({"queued": doc_id}, status_code=202)

    async def ask(request: Request):
        user = current_user(request)
        body = await json_body(request)
        question = str(body.get("question", "")).strip()
        if not 3 <= len(question) <= 2000:
            raise HTTPException(422, "question must be between 3 and 2000 characters")
        advisor = body.get("advisor") or None
        if advisor not in (None, "auto", "general", "finance", "hr"):
            raise HTTPException(422, "advisor must be auto, general, finance or hr")
        result = await run_in_threadpool(service.ask, question, user, None if advisor == "auto" else advisor)
        return JSONResponse(result)

    async def automation_runs(request: Request):
        user = current_user(request)
        classes = allowed_classifications(user["role"])
        rows = db.query(
            f"""SELECT r.id, r.document_id, d.title, d.filename, r.step, r.status, r.attempts, r.detail,
                       r.created_at
                FROM automation_runs r JOIN documents d ON d.id = r.document_id
                WHERE d.classification IN ({','.join('?' * len(classes))})
                ORDER BY r.id DESC LIMIT 200""", classes)
        return JSONResponse(rows)

    async def audit_log(request: Request):
        require_admin(request)
        outcome = request.query_params.get("outcome")
        sql = "SELECT * FROM audit_log" + (" WHERE outcome = ?" if outcome else "") + " ORDER BY id DESC LIMIT 300"
        return JSONResponse(db.query(sql, (outcome,) if outcome else ()))

    async def query_log(request: Request):
        require_admin(request)
        return JSONResponse(db.query("SELECT id, ts, username, advisor, status, question, documents_used, "
                                     "flags, latency_ms FROM queries ORDER BY id DESC LIMIT 100"))

    async def outbox(request: Request):
        require_admin(request)
        files = sorted(s.outbox_dir.glob("*.eml"), reverse=True)[:20]
        return JSONResponse([{"name": f.name, "content": f.read_text(errors="replace")[:4000]} for f in files])

    async def metrics(request: Request):
        require_admin(request)
        return JSONResponse({
            "documents": db.query("SELECT status, classification, COUNT(*) AS n FROM documents "
                                  "GROUP BY status, classification"),
            "queries": db.query("SELECT advisor, status, COUNT(*) AS n, CAST(AVG(latency_ms) AS INT) AS avg_ms "
                                "FROM queries GROUP BY advisor, status"),
            "automation_failures": db.one("SELECT COUNT(*) AS n FROM automation_runs WHERE status = 'failed'")["n"],
            "denied_events_24h": db.one("SELECT COUNT(*) AS n FROM audit_log WHERE outcome = 'denied' "
                                        "AND ts > ?", (time.time() - 86400,))["n"],
            "llm": llm.info(),
        })

    async def simulate_outage(request: Request):
        user = require_admin(request)
        body = await json_body(request)
        llm.simulate_outage = bool(body.get("enabled"))
        db.audit(user, "simulate_llm_outage", "llm", "allowed", {"enabled": llm.simulate_outage})
        return JSONResponse(llm.info())

    # ---------------------------------------------------------------- errors
    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail, "request_id": getattr(request.state, "request_id", None)},
                            status_code=exc.status_code)

    async def server_error(request: Request, exc: Exception):
        rid = getattr(request.state, "request_id", None)
        log.exception("unhandled error rid=%s", rid)
        return JSONResponse({"error": "internal error - the incident has been logged", "request_id": rid},
                            status_code=500)

    async def value_error(request: Request, exc: ValueError):
        return JSONResponse({"error": "invalid request parameter"}, status_code=400)

    routes = [
        Route("/", index),
        Route("/api/health", health),
        Route("/api/auth/login", login, methods=["POST"]),
        Route("/api/me", me),
        Route("/api/documents", list_documents, methods=["GET"]),
        Route("/api/documents", upload_document, methods=["POST"]),
        Route("/api/documents/{doc_id:int}", get_document, methods=["GET"]),
        Route("/api/documents/{doc_id:int}", remove_document, methods=["DELETE"]),
        Route("/api/documents/{doc_id:int}/file", download_document),
        Route("/api/documents/{doc_id:int}/reprocess", reprocess, methods=["POST"]),
        Route("/api/ask", ask, methods=["POST"]),
        Route("/api/automation/runs", automation_runs),
        Route("/api/admin/audit", audit_log),
        Route("/api/admin/queries", query_log),
        Route("/api/admin/outbox", outbox),
        Route("/api/admin/metrics", metrics),
        Route("/api/admin/simulate-outage", simulate_outage, methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    app = Starlette(routes=routes, middleware=[Middleware(SecurityHeaders)],
                    exception_handlers={HTTPException: http_error, ValueError: value_error,
                                        Exception: server_error})
    app.state.settings, app.state.db, app.state.llm = s, db, llm
    app.state.service, app.state.pipeline = service, pipeline
    return app


def _lazy_app():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()


def main() -> None:
    import uvicorn

    from advisor.config import _load_dotenv

    _load_dotenv(Path(".env"))            # so HOST / PORT in .env apply to the server itself
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("advisor.app:_lazy_app", factory=True, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
