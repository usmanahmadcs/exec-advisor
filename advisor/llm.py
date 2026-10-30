"""LLM provider layer: OpenAI, Anthropic, Ollama or an offline mock.

All providers speak plain HTTPS via `requests` (no vendor SDKs required).
`LLMClient` adds the reliability behaviour the rest of the app relies on:
  * per-call timeout
  * retries with exponential backoff on timeouts / 429 / 5xx
  * a small circuit breaker so an outage fails fast instead of stalling users
  * fault injection (admin toggle) to demonstrate degraded mode on demand
Callers catch `LLMError` and fall back to deterministic extractive answers.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import requests

from advisor.config import Settings

log = logging.getLogger("advisor.llm")

DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-5-5",
    "ollama": "llama3.1:8b",
    "mock": "extractive-mock",
}


class LLMError(Exception):
    """The language model could not produce an answer (after retries)."""


class _Retryable(Exception):
    pass


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    latency_ms: int


class Provider:
    name = "base"

    def __init__(self, settings: Settings):
        self.s = settings
        self.model = settings.llm_model or DEFAULT_MODELS.get(self.name, "")

    def complete(self, system: str, user: str, max_tokens: int) -> str:  # pragma: no cover
        raise NotImplementedError

    def _post(self, url: str, headers: dict, payload: dict) -> dict:
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=self.s.llm_timeout_s)
        except (requests.Timeout, requests.ConnectionError) as e:
            raise _Retryable(f"{type(e).__name__}") from e
        if resp.status_code == 429 or resp.status_code >= 500:
            raise _Retryable(f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            # 4xx (bad key, bad model) will not fix itself: do not retry
            raise LLMError(f"{self.name} rejected the request: HTTP {resp.status_code} "
                           f"{resp.text[:200]}")
        return resp.json()


class OpenAIProvider(Provider):
    name = "openai"

    def complete(self, system: str, user: str, max_tokens: int) -> str:
        if not self.s.openai_api_key:
            raise LLMError("OPENAI_API_KEY is not set")
        data = self._post(
            f"{self.s.openai_base_url.rstrip('/')}/chat/completions",
            {"Authorization": f"Bearer {self.s.openai_api_key}"},
            {"model": self.model, "temperature": 0, "max_tokens": max_tokens,
             "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
        )
        return data["choices"][0]["message"]["content"] or ""


class AnthropicProvider(Provider):
    name = "anthropic"

    def complete(self, system: str, user: str, max_tokens: int) -> str:
        if not self.s.anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        data = self._post(
            f"{self.s.anthropic_base_url.rstrip('/')}/v1/messages",
            {"x-api-key": self.s.anthropic_api_key, "anthropic-version": "2023-06-01"},
            {"model": self.model, "max_tokens": max_tokens, "temperature": 0, "system": system,
             "messages": [{"role": "user", "content": user}]},
        )
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


class OllamaProvider(Provider):
    name = "ollama"

    def complete(self, system: str, user: str, max_tokens: int) -> str:
        data = self._post(
            f"{self.s.ollama_url.rstrip('/')}/api/chat", {},
            {"model": self.model, "stream": False, "options": {"temperature": 0, "num_predict": max_tokens},
             "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
        )
        return data.get("message", {}).get("content", "")


class MockProvider(Provider):
    """No network. Signals callers to use their deterministic extractive path."""

    name = "mock"

    def complete(self, system: str, user: str, max_tokens: int) -> str:
        raise LLMError("mock provider: no generative model configured (extractive mode)")


PROVIDERS = {p.name: p for p in (OpenAIProvider, AnthropicProvider, OllamaProvider, MockProvider)}


class LLMClient:
    def __init__(self, settings: Settings, provider: Provider | None = None,
                 breaker_threshold: int = 3, breaker_cooldown_s: float = 60.0):
        self.s = settings
        if provider is None:
            cls = PROVIDERS.get(settings.llm_provider)
            if cls is None:
                raise ValueError(f"unknown LLM_PROVIDER '{settings.llm_provider}'")
            provider = cls(settings)
        self.provider = provider
        self.simulate_outage = False          # toggled from the admin panel
        self._failures = 0
        self._open_until = 0.0
        self._threshold = breaker_threshold
        self._cooldown = breaker_cooldown_s
        self._lock = threading.Lock()

    @property
    def is_generative(self) -> bool:
        return self.provider.name != "mock"

    def info(self) -> dict:
        return {"provider": self.provider.name, "model": self.provider.model,
                "simulate_outage": self.simulate_outage,
                "circuit_open": time.time() < self._open_until}

    def complete(self, system: str, user: str, max_tokens: int = 700) -> LLMResult:
        if self.simulate_outage:
            raise LLMError("simulated provider outage (admin fault injection)")
        if not self.is_generative:
            return self.provider.complete(system, user, max_tokens)   # raises LLMError
        if time.time() < self._open_until:
            raise LLMError("LLM circuit breaker open after repeated failures; retrying later")

        attempts = self.s.llm_max_retries + 1
        last = ""
        for attempt in range(attempts):
            t0 = time.perf_counter()
            try:
                text = self.provider.complete(system, user, max_tokens)
                self._record(success=True)
                return LLMResult(text.strip(), self.provider.name, self.provider.model,
                                 int((time.perf_counter() - t0) * 1000))
            except _Retryable as e:
                last = str(e)
                log.warning("llm attempt %d/%d failed: %s", attempt + 1, attempts, last)
                if attempt + 1 < attempts:
                    time.sleep(min(8.0, 0.5 * 2 ** attempt))
            except LLMError:
                self._record(success=False)
                raise
            except Exception as e:  # noqa: BLE001 - malformed response etc.
                last = f"unexpected response: {type(e).__name__}"
                break
        self._record(success=False)
        raise LLMError(f"{self.provider.name} unavailable after {attempts} attempt(s): {last}")

    def _record(self, success: bool) -> None:
        with self._lock:
            if success:
                self._failures = 0
                return
            self._failures += 1
            if self._failures >= self._threshold:
                self._open_until = time.time() + self._cooldown
                self._failures = 0
