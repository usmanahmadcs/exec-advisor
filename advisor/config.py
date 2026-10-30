"""Application settings, read once from environment variables (and an optional .env file)."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines); real env vars always win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_bool(name: str, default: bool = False) -> bool:
    return _env(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # storage
    data_dir: Path = Path("data")
    # security
    jwt_secret: str = ""
    token_ttl_minutes: int = 60
    max_upload_mb: int = 15
    login_max_attempts: int = 5
    login_lockout_minutes: int = 15
    admin_username: str = "admin"
    admin_password: str = "Admin#2026!"
    user_username: str = "analyst"
    user_password: str = "Analyst#2026!"
    # llm
    llm_provider: str = "mock"          # mock | openai | anthropic | ollama
    llm_model: str = ""
    llm_timeout_s: float = 30.0
    llm_max_retries: int = 2
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    ollama_url: str = "http://localhost:11434"
    # retrieval
    top_k: int = 6
    min_evidence_coverage: float = 0.6
    # automation
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "advisor@example.com"
    smtp_starttls: bool = True
    notify_to: str = "ops@example.com"
    webhook_url: str = ""
    automation_max_attempts: int = 3
    automation_backoff_s: float = 1.0
    run_automation_inline: bool = False   # tests: run the pipeline synchronously
    extra: dict = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "advisor.db"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def outbox_dir(self) -> Path:
        return self.data_dir / "outbox"

    @classmethod
    def from_env(cls, env_file: str | None = ".env") -> "Settings":
        if env_file:
            _load_dotenv(Path(env_file))
        s = cls(
            data_dir=Path(_env("DATA_DIR", "data")),
            jwt_secret=_env("JWT_SECRET"),
            token_ttl_minutes=int(_env("TOKEN_TTL_MINUTES", "60")),
            max_upload_mb=int(_env("MAX_UPLOAD_MB", "15")),
            login_max_attempts=int(_env("LOGIN_MAX_ATTEMPTS", "5")),
            login_lockout_minutes=int(_env("LOGIN_LOCKOUT_MINUTES", "15")),
            admin_username=_env("ADMIN_USERNAME", "admin"),
            admin_password=_env("ADMIN_PASSWORD", "Admin#2026!"),
            user_username=_env("USER_USERNAME", "analyst"),
            user_password=_env("USER_PASSWORD", "Analyst#2026!"),
            llm_provider=_env("LLM_PROVIDER", "mock").lower(),
            llm_model=_env("LLM_MODEL"),
            llm_timeout_s=float(_env("LLM_TIMEOUT_S", "30")),
            llm_max_retries=int(_env("LLM_MAX_RETRIES", "2")),
            openai_api_key=_env("OPENAI_API_KEY"),
            openai_base_url=_env("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            anthropic_base_url=_env("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
            ollama_url=_env("OLLAMA_URL", "http://localhost:11434"),
            top_k=int(_env("TOP_K", "6")),
            min_evidence_coverage=float(_env("MIN_EVIDENCE_COVERAGE", "0.6")),
            smtp_host=_env("SMTP_HOST"),
            smtp_port=int(_env("SMTP_PORT", "587")),
            smtp_user=_env("SMTP_USER"),
            smtp_password=_env("SMTP_PASSWORD"),
            smtp_from=_env("SMTP_FROM", "advisor@example.com"),
            smtp_starttls=_env_bool("SMTP_STARTTLS", True),
            notify_to=_env("NOTIFY_TO", "ops@example.com"),
            webhook_url=_env("WEBHOOK_URL"),
            automation_max_attempts=int(_env("AUTOMATION_MAX_ATTEMPTS", "3")),
            automation_backoff_s=float(_env("AUTOMATION_BACKOFF_S", "1.0")),
        )
        s.ensure_secret()
        return s

    def ensure_secret(self) -> None:
        """Use JWT_SECRET if set; otherwise persist a random one in the data dir.

        A generated secret survives restarts (so sessions stay valid) but is never
        committed to source control.
        """
        if self.jwt_secret:
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        secret_file = self.data_dir / ".jwt_secret"
        if secret_file.exists():
            self.jwt_secret = secret_file.read_text().strip()
        else:
            self.jwt_secret = secrets.token_urlsafe(48)
            secret_file.write_text(self.jwt_secret)
            try:
                secret_file.chmod(0o600)
            except OSError:
                pass

    def prepare_dirs(self) -> None:
        for d in (self.data_dir, self.upload_dir, self.outbox_dir):
            d.mkdir(parents=True, exist_ok=True)
