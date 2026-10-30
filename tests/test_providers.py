"""LLM provider wire formats, tested against a local fake HTTP server (no network, no keys)."""
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from tests.helpers import make_settings  # noqa: F401  (sets sys.path)

from advisor.config import Settings
from advisor.llm import LLMClient, LLMError


class _Handler(BaseHTTPRequestHandler):
    fail_first = 0          # number of 503s to return before succeeding
    requests: list = []

    def log_message(self, *args):  # silence
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append((self.path, dict(self.headers), body))
        if type(self).fail_first > 0:
            type(self).fail_first -= 1
            self.send_response(503)
            self.end_headers()
            return
        if self.path == "/v1/chat/completions":
            out = {"choices": [{"message": {"content": "openai says hi [S1]"}}]}
        elif self.path == "/v1/messages":
            out = {"content": [{"type": "text", "text": "anthropic says hi [S1]"}]}
        elif self.path == "/api/chat":
            out = {"message": {"content": "ollama says hi [S1]"}}
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class TestProviders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Handler.requests.clear()
        _Handler.fail_first = 0

    def settings(self, provider: str) -> Settings:
        return Settings(llm_provider=provider, openai_api_key="sk-test", anthropic_api_key="ak-test",
                        openai_base_url=f"{self.url}/v1", anthropic_base_url=self.url, ollama_url=self.url,
                        llm_max_retries=2, jwt_secret="x")

    def test_openai(self):
        r = LLMClient(self.settings("openai")).complete("sys", "hello")
        self.assertEqual(r.text, "openai says hi [S1]")
        _path, headers, body = _Handler.requests[0]
        self.assertEqual(headers["Authorization"], "Bearer sk-test")
        self.assertEqual(body["messages"][0], {"role": "system", "content": "sys"})
        self.assertEqual(body["temperature"], 0)

    def test_anthropic(self):
        r = LLMClient(self.settings("anthropic")).complete("sys", "hello")
        self.assertEqual(r.text, "anthropic says hi [S1]")
        _path, headers, body = _Handler.requests[0]
        self.assertEqual(headers["x-api-key"], "ak-test")
        self.assertEqual(body["system"], "sys")
        self.assertEqual(body["model"], "claude-haiku-5-5")

    def test_ollama(self):
        self.assertEqual(LLMClient(self.settings("ollama")).complete("sys", "hi").text, "ollama says hi [S1]")

    def test_retry_then_success(self):
        _Handler.fail_first = 2
        r = LLMClient(self.settings("openai")).complete("sys", "hello")
        self.assertEqual(r.text, "openai says hi [S1]")
        self.assertEqual(len(_Handler.requests), 3)

    def test_retries_exhausted(self):
        _Handler.fail_first = 10
        with self.assertRaises(LLMError):
            LLMClient(self.settings("openai")).complete("sys", "hello")
        self.assertEqual(len(_Handler.requests), 3)

    def test_missing_key(self):
        s = self.settings("openai")
        s.openai_api_key = ""
        with self.assertRaises(LLMError):
            LLMClient(s).complete("sys", "hello")


if __name__ == "__main__":
    unittest.main()
