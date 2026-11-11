"""distill providers openai-compat and ollama against a local http.server (127.0.0.1, no real network)."""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import distill as DS  # noqa: E402

SCHEMA = {"type": "object", "properties": {"supported": {"type": "boolean"}}, "required": ["supported"]}


class Server:
    """Replies are consumed in order: (status, payload). Every request is recorded."""
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get("content-length", 0))
                outer.requests.append({"path": self.path, "auth": self.headers.get("authorization"),
                                       "body": json.loads(self.rfile.read(size))})
                status, payload = outer.replies.pop(0)
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def chat(content):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class TestProviders(unittest.TestCase):
    def serve(self, *replies):
        server = Server(replies)
        self.addCleanup(server.close)
        return server

    def provider(self, server, name="openai-compat", **extra):
        cfg = {"distill": {"provider": name, "model": "local-model", "base_url": server.url, "timeout": 5, **extra}}
        return DS.get_provider(cfg)

    def test_json_schema_success_with_key(self):
        server = self.serve((200, chat('{"supported": true}')))
        with mock.patch.dict(os.environ, {"MY_KEY": "sk-secret-123"}):
            out = self.provider(server, api_key_env="MY_KEY")("the prompt", SCHEMA)
        self.assertEqual(out, {"supported": True})
        req = server.requests[0]
        self.assertEqual(req["path"], "/chat/completions")
        self.assertEqual(req["auth"], "Bearer sk-secret-123")
        self.assertEqual(req["body"]["model"], "local-model")
        self.assertEqual(req["body"]["messages"][0]["content"], "the prompt")
        self.assertEqual(req["body"]["response_format"]["type"], "json_schema")
        self.assertEqual(req["body"]["response_format"]["json_schema"]["schema"], SCHEMA)

    def test_no_key_sends_no_authorization(self):
        server = self.serve((200, chat('{"supported": false}')))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            self.provider(server)("p", SCHEMA)
        self.assertIsNone(server.requests[0]["auth"])

    def test_400_falls_back_to_json_object(self):
        server = self.serve((400, {"error": "unsupported response_format"}), (200, chat('{"supported": true}')))
        out = self.provider(server)("the prompt", SCHEMA)
        self.assertEqual(out, {"supported": True})
        self.assertEqual(len(server.requests), 2)
        second = server.requests[1]["body"]
        self.assertEqual(second["response_format"], {"type": "json_object"})
        self.assertIn(json.dumps(SCHEMA), second["messages"][0]["content"])

    def test_fenced_json_is_accepted(self):
        server = self.serve((200, chat('```json\n{"supported": true}\n```')))
        self.assertEqual(self.provider(server)("p", SCHEMA), {"supported": True})

    def test_non_json_reply_is_an_error(self):
        server = self.serve((200, chat("sure, here you go")))
        self.assertRaisesRegex(DS.DistillError, "did not return JSON", self.provider(server), "p", SCHEMA)

    def test_http_error_does_not_leak_the_key(self):
        server = self.serve((401, {"error": "bad key"}))
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-leak-me-999"}):
            with self.assertRaises(DS.DistillError) as ctx:
                self.provider(server)("p", SCHEMA)
        self.assertIn("401", str(ctx.exception))
        self.assertNotIn("sk-leak-me-999", str(ctx.exception))
        self.assertEqual(len(server.requests), 1)  # only 400 retries

    def test_connection_refused_is_a_distill_error(self):
        server = self.serve()
        provider = self.provider(server)
        server.close()
        self.assertRaises(DS.DistillError, provider, "p", SCHEMA)

    def test_default_model_alias_is_rejected_for_non_claude(self):
        server = self.serve()
        cfg = {"distill": {"provider": "openai-compat", "base_url": server.url}}
        self.assertRaisesRegex(DS.DistillError, "distill.model", DS.get_provider(cfg), "p", SCHEMA)

    def test_ollama_success(self):
        server = self.serve((200, {"message": {"role": "assistant", "content": '{"supported": true}'}, "done": True}))
        out = self.provider(server, "ollama")("the prompt", SCHEMA)
        self.assertEqual(out, {"supported": True})
        req = server.requests[0]
        self.assertEqual(req["path"], "/api/chat")
        self.assertIsNone(req["auth"])
        self.assertIs(req["body"]["stream"], False)
        self.assertEqual(req["body"]["format"], SCHEMA)
        self.assertEqual(req["body"]["model"], "local-model")

    def test_ollama_error(self):
        server = self.serve((500, {"error": "boom"}))
        self.assertRaisesRegex(DS.DistillError, "Ollama error 500", self.provider(server, "ollama"), "p", SCHEMA)

    def test_unknown_provider_lists_valid_names(self):
        with self.assertRaises(DS.DistillError) as ctx:
            DS.get_provider({"distill": {"provider": "magic"}})
        for name in ("claude-cli", "anthropic-api", "openai-compat", "ollama"):
            self.assertIn(name, str(ctx.exception))

    def test_claude_alias_map_untouched(self):
        self.assertEqual(DS.API_MODELS.get("llama3.1", "llama3.1"), "llama3.1")


class TestReadiness(unittest.TestCase):
    def test_openai_needs_key_unless_local(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("X_KEY", None)
            self.assertFalse(DS.readiness({"distill": {"provider": "openai-compat"}})[1])
            self.assertTrue(DS.readiness({"distill": {"provider": "openai-compat",
                                                      "base_url": "http://localhost:1234/v1"}})[1])
            os.environ["X_KEY"] = "k"
            name, ok, detail = DS.readiness({"distill": {"provider": "openai-compat", "api_key_env": "X_KEY"}})
            self.assertTrue(ok)
            self.assertIn("X_KEY is set", detail)
            self.assertNotIn("k\n", detail)

    def test_ollama_and_unknown(self):
        self.assertEqual(DS.readiness({"distill": {"provider": "ollama"}})[:2], ("ollama", True))
        self.assertFalse(DS.readiness({"distill": {"provider": "nope"}})[1])


if __name__ == "__main__":
    unittest.main()
