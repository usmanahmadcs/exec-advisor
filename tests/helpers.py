"""Shared test fixtures: an isolated data dir seeded with the sample documents."""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from advisor.config import Settings  # noqa: E402
from advisor.llm import Provider  # noqa: E402

ADMIN = {"username": "admin", "role": "admin"}
USER = {"username": "analyst", "role": "user"}


def make_settings(tmp: Path) -> Settings:
    s = Settings(data_dir=tmp, jwt_secret="test-secret-" + "x" * 32, run_automation_inline=True,
                 automation_backoff_s=0.0, llm_max_retries=1)
    s.prepare_dirs()
    return s


class ScriptedProvider(Provider):
    """Fake LLM: returns a fixed reply, or raises, so generative paths can be tested offline."""

    name = "scripted"

    def __init__(self, settings, reply: str = "", error: Exception | None = None):
        super().__init__(settings)
        self.reply, self.error, self.calls = reply, error, []

    def complete(self, system: str, user: str, max_tokens: int) -> str:
        self.calls.append((system, user))
        if self.error:
            raise self.error
        return self.reply


class SeededTestCase(unittest.TestCase):
    """Seeds the sample corpus once per test class."""

    @classmethod
    def setUpClass(cls):
        from scripts.seed_demo import seed  # noqa: WPS433

        cls.tmp = Path(tempfile.mkdtemp(prefix="advisor-test-"))
        if not (ROOT / "sample_docs" / "company_overview.md").exists():
            import runpy
            runpy.run_path(str(ROOT / "scripts" / "make_sample_docs.py"), run_name="__main__")
        cls.settings = make_settings(cls.tmp)
        seed(cls.settings, verbose=False)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
