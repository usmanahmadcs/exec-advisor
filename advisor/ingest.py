"""Upload intake and indexing: validate -> store -> parse -> chunk -> extract facts."""
from __future__ import annotations

import hashlib
import re
import time
import uuid
from pathlib import Path

from advisor import facts, parsing
from advisor.config import Settings
from advisor.db import Database
from advisor.router import classify_category

CATEGORIES = ("finance", "hr", "general")
CLASSIFICATIONS = ("public", "confidential")


class UploadError(Exception):
    """Invalid upload (message is safe to return to the client)."""


def safe_filename(name: str) -> str:
    name = Path(name or "upload").name                    # strip any path components
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")
    return name[:120] or "upload"


def register_upload(db: Database, settings: Settings, filename: str, data: bytes, category: str,
                    classification: str, user: dict) -> int:
    """Validate and persist the raw file. Returns the new document id (status=processing)."""
    filename = safe_filename(filename)
    if classification not in CLASSIFICATIONS:
        raise UploadError(f"classification must be one of {CLASSIFICATIONS}")
    if category not in CATEGORIES + ("auto",):
        raise UploadError(f"category must be one of {CATEGORIES + ('auto',)}")
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise UploadError(f"file exceeds {settings.max_upload_mb} MB limit")
    if parsing.extension_of(filename) not in parsing.SUPPORTED_EXTENSIONS:
        raise UploadError("unsupported file type; allowed: " + ", ".join(sorted(parsing.SUPPORTED_EXTENSIONS)))
    if not data:
        raise UploadError("file is empty")

    digest = hashlib.sha256(data).hexdigest()
    dup = db.one("SELECT id, status FROM documents WHERE sha256 = ? AND classification = ?",
                 (digest, classification))
    if dup and dup["status"] != "failed":
        raise UploadError(f"identical document already uploaded (id {dup['id']})")

    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    stored = settings.upload_dir / f"{uuid.uuid4().hex}{parsing.extension_of(filename)}"
    stored.write_bytes(data)
    return db.execute(
        """INSERT INTO documents(filename, title, category, classification, status, sha256,
                                 stored_path, size_bytes, uploaded_by, uploaded_at)
           VALUES (?, ?, ?, ?, 'processing', ?, ?, ?, ?, ?)""",
        (filename, filename, category, classification, digest, str(stored), len(data),
         user["username"], time.time()),
    )


def process_document(db: Database, doc_id: int) -> dict:
    """Parse, chunk and extract facts. Marks the document ready or failed; returns stats."""
    doc = db.one("SELECT * FROM documents WHERE id = ?", (doc_id,))
    if doc is None:
        raise UploadError(f"document {doc_id} not found")
    try:
        data = Path(doc["stored_path"]).read_bytes()
        pages = parsing.extract_pages(doc["filename"], data)
    except (parsing.ParseError, OSError) as e:
        db.execute("UPDATE documents SET status = 'failed', error = ? WHERE id = ?", (str(e), doc_id))
        raise

    full_text = "\n\n".join(p.text for p in pages)
    chunks = parsing.chunk_pages(pages)
    title = parsing.guess_title(pages, doc["filename"])
    category = doc["category"]
    if category == "auto":
        category = classify_category(full_text)
    period = parsing.find_period(full_text)
    effective = parsing.find_effective_date(full_text)

    n_fin = n_pol = 0
    with db.connect() as c:
        c.execute("DELETE FROM chunks WHERE document_id = ?", (doc_id,))
        for ch in chunks:
            cur = c.execute("INSERT INTO chunks(document_id, ord, page, text) VALUES (?, ?, ?, ?)",
                            (doc_id, ch.ord, ch.page, ch.text))
            chunk_id = cur.lastrowid
            if category == "finance":
                for f in facts.extract_financial_facts(ch.text, period):
                    c.execute(
                        "INSERT INTO financial_facts(document_id, chunk_id, metric, period, value, currency, raw) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (doc_id, chunk_id, f.metric, f.period, str(f.value), f.currency, f.raw))
                    n_fin += 1
            else:
                for f in facts.extract_policy_facts(ch.text):
                    c.execute(
                        "INSERT INTO policy_facts(document_id, chunk_id, topic, value, unit, raw) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (doc_id, chunk_id, f.topic, f.value, f.unit, f.raw))
                    n_pol += 1
        c.execute(
            """UPDATE documents SET status = 'ready', error = NULL, title = ?, category = ?,
                      num_chunks = ?, period = ?, effective_date = ? WHERE id = ?""",
            (title, category, len(chunks), period, effective, doc_id))
    db.bump_corpus_version()
    return {"chunks": len(chunks), "financial_facts": n_fin, "policy_facts": n_pol,
            "category": category, "title": title, "period": period, "effective_date": effective,
            "text": full_text}


def delete_document(db: Database, doc_id: int) -> bool:
    doc = db.one("SELECT stored_path FROM documents WHERE id = ?", (doc_id,))
    if not doc:
        return False
    db.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    try:
        Path(doc["stored_path"]).unlink(missing_ok=True)
    except OSError:
        pass
    db.bump_corpus_version()
    return True
