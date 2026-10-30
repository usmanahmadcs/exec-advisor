"""Load the sample documents into a fresh database (runs the full automation pipeline).

Usage:  python scripts/seed_demo.py [--reset]
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from advisor.automation import AutomationPipeline  # noqa: E402
from advisor.config import Settings  # noqa: E402
from advisor.db import Database  # noqa: E402
from advisor.ingest import UploadError, register_upload  # noqa: E402
from advisor.llm import LLMClient  # noqa: E402
from advisor.security import ensure_user  # noqa: E402

SAMPLES = [
    # filename, category, classification
    ("company_overview.md", "general", "public"),
    ("finance_q1_2026_report.pdf", "finance", "public"),
    ("finance_q2_2026_report.docx", "finance", "public"),
    ("board_financial_summary_confidential.txt", "finance", "confidential"),
    ("employee_handbook_2025.pdf", "hr", "public"),
    ("leave_and_remote_work_policy_2026.docx", "hr", "public"),
    ("hr_faq.txt", "hr", "public"),
    ("salary_bands_2026_confidential.txt", "hr", "confidential"),
]


def seed(settings: Settings, sample_dir: Path = ROOT / "sample_docs", verbose: bool = True) -> list[int]:
    settings.run_automation_inline = True
    settings.prepare_dirs()
    db = Database(settings.db_path)
    ensure_user(db, settings.admin_username, settings.admin_password, "admin")
    ensure_user(db, settings.user_username, settings.user_password, "user")
    pipeline = AutomationPipeline(db, settings, LLMClient(settings))
    admin = {"username": settings.admin_username, "role": "admin"}
    ids = []
    for name, category, classification in SAMPLES:
        path = sample_dir / name
        try:
            doc_id = register_upload(db, settings, name, path.read_bytes(), category, classification, admin)
        except UploadError as e:
            if verbose:
                print(f"  skip {name}: {e}")
            continue
        result = pipeline.run(doc_id)
        ids.append(doc_id)
        if verbose:
            print(f"  [{doc_id}] {name:45s} {classification:12s} -> {result['status']}")
    return ids


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="delete the data directory first")
    args = ap.parse_args()
    settings = Settings.from_env(str(ROOT / ".env"))
    if args.reset and settings.data_dir.exists():
        shutil.rmtree(settings.data_dir)
        settings = Settings.from_env(str(ROOT / ".env"))
    if not (ROOT / "sample_docs" / "company_overview.md").exists():
        import runpy
        runpy.run_path(str(ROOT / "scripts" / "make_sample_docs.py"), run_name="__main__")
    print(f"Seeding {settings.db_path} ...")
    seed(settings)
    print(f"Done. Log in as '{settings.admin_username}' (admin) or '{settings.user_username}' (standard user).")


if __name__ == "__main__":
    main()
