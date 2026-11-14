"""Semantic rerank: the store `embed` builds, the rerank of BM25's candidates, its silent
fallback, and where it is (and is not) used. A fake embedding server on 127.0.0.1 with
deterministic vectors: no real network, no real model."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import bench as BN        # noqa: E402
import core as C          # noqa: E402
import embeddings as EM   # noqa: E402
import recall as RC       # noqa: E402
import telemetry as TL    # noqa: E402

CLI = REPO / "scripts" / "cli.py"
HOOK = REPO / "hooks" / "recall.py"
ENV_KEYS = ("VAULT_MEMORY_VAULT", "CEREBRO_VAULT")
OTHER = [0.0, 1.0, 0.0]


class FakeServer:
    """Ollama (/api/embed) and OpenAI-style (/embeddings) embedding endpoints. `vectorize(text)`
    gives each text its vector; every request is recorded."""
    def __init__(self, vectorize=lambda text: OTHER):
        self.vectorize, self.requests = vectorize, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(size))
                outer.requests.append({"path": self.path, "auth": self.headers.get("authorization"), "body": body})
                vectors = [outer.vectorize(text) for text in body["input"]]
                if self.path == "/api/embed":
                    payload = {"model": body["model"], "embeddings": vectors}
                else:                   # OpenAI style, rows out of order on purpose
                    payload = {"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)][::-1]}
                data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def texts(self):
        return [t for r in self.requests for t in r["body"]["input"]]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def dead_url():
    """A local port nobody listens on: connection refused, at once."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.vault = self.tmp / "vault"
        shutil.copytree(REPO / "tests" / "example_vault", self.vault)
        old = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(old)))
        os.environ["VAULT_MEMORY_TODAY"] = "2026-11-09"
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        self.server = FakeServer()
        self.addCleanup(self.server.close)

    def configure(self, embeddings=None, rerank=None):
        path = self.vault / ".vault-memory" / "config.json"
        own = json.loads(path.read_text(encoding="utf-8"))
        if embeddings is not None:
            own["embeddings"] = embeddings
        if rerank is not None:
            own.setdefault("recall", {})["rerank"] = rerank
        path.write_text(json.dumps(own), encoding="utf-8")
        self.cfg = C.load_config(self.vault)
        return self.cfg

    def ollama(self, **extra):
        return self.configure({"provider": "ollama", "model": "fake-embed", "base_url": self.server.url, **extra})

    def events(self, kind):
        return [e for e in TL.read(self.vault, days=1) if e.get("ev") == kind]


class TestConfig(Base):
    def test_off_by_default(self):
        self.assertEqual(C.CONFIG_DEF["embeddings"]["provider"], "off")
        self.assertEqual(C.CONFIG_DEF["embeddings"]["model"], "")
        self.assertEqual(C.CONFIG_DEF["recall"]["rerank"], {"weight": 0.5, "candidates": 30, "hook": False})
        cfg = C.load_config(self.vault)
        self.assertFalse(EM.enabled(cfg))
        self.assertFalse(EM.available(cfg))
        self.assertEqual(EM.readiness(cfg), ("off", False, "off"))
        results = RC.search(RC.load(cfg), "checkout down", n=5)
        self.assertEqual(EM.rerank(cfg, "checkout down", results), (results, False))
        with self.assertRaises(EM.EmbedError):
            EM.build(cfg)
        self.assertEqual(self.server.requests, [])

    def test_rerank_settings_do_not_invalidate_the_index(self):
        cfg = C.load_config(self.vault)
        before = C.cfg_hash(cfg)
        cfg["recall"]["rerank"] = {"weight": 0.9, "candidates": 10, "hook": True}
        self.assertEqual(C.cfg_hash(cfg), before)

    def test_model_is_required_and_said_without_a_network_call(self):
        cfg = self.configure({"provider": "ollama", "base_url": self.server.url})
        name, ok, detail = EM.readiness(cfg)
        self.assertFalse(ok)
        self.assertIn("embeddings.model", detail)
        cfg = self.configure({"provider": "openai-compat", "model": "m"})
        self.assertIn("embeddings.base_url", EM.readiness(cfg)[2])
        self.assertEqual(self.server.requests, [])


class TestEmbed(Base):
    def stored(self):
        return EM.load_store(self.cfg)["notes"]

    def test_stores_one_vector_per_eligible_note(self):
        self.ollama()
        result = EM.build(self.cfg)
        docs = RC.load(self.cfg)["docs"]
        eligible = {d["p"] for d in EM.eligible(self.cfg, docs)}
        self.assertEqual(set(self.stored()), eligible)
        self.assertEqual((result["embedded"], result["kept"], result["dropped"]), (len(eligible), 0, 0))
        self.assertFalse(any(p.startswith(("_archive/", "Templates/")) for p in self.stored()))
        self.assertTrue(any(d["archived"] for d in docs) and any(d["template"] for d in docs))
        request = self.server.requests[0]
        self.assertEqual((request["path"], request["body"]["model"]), ("/api/embed", "fake-embed"))
        self.assertLessEqual(max(len(r["body"]["input"]) for r in self.server.requests), 16)
        meta = EM.load_store(self.cfg)
        self.assertEqual((meta["model"], meta["dim"]), ("fake-embed", 3))
        self.assertEqual((self.cfg["_state"] / "embeddings" / meta["file"]).stat().st_size, 4 * 3 * len(eligible))

    def test_only_changed_notes_are_embedded_again(self):
        self.ollama()
        first = EM.build(self.cfg)
        note = self.vault / "Memory" / "Retry policy v2.md"
        note.write_text(note.read_text(encoding="utf-8") + "\nOne more line about backoff.\n", encoding="utf-8")
        before = len(self.server.texts())
        again = EM.build(self.cfg)
        sent = self.server.texts()[before:]
        self.assertEqual(len(sent), 1)
        self.assertIn("backoff", sent[0])
        self.assertEqual((again["embedded"], again["kept"]), (1, first["embedded"] - 1))
        before = len(self.server.texts())
        self.assertEqual(EM.build(self.cfg)["embedded"], 0)
        self.assertEqual(len(self.server.texts()), before, "nothing changed: no call")

    def test_a_new_model_rebuilds_everything(self):
        self.ollama()
        first = EM.build(self.cfg)
        self.ollama(model="other-model")
        self.assertFalse(EM.available(self.cfg), "the old vectors do not serve the new model")
        again = EM.build(self.cfg)
        self.assertTrue(again["rebuilt"])
        self.assertEqual((again["embedded"], again["kept"]), (first["embedded"], 0))
        self.assertEqual(EM.load_store(self.cfg)["model"], "other-model")
        self.assertEqual(EM.build(self.cfg, full=True)["embedded"], first["embedded"])

    def test_deleted_notes_are_dropped(self):
        self.ollama()
        EM.build(self.cfg)
        (self.vault / "Memory" / "Retry policy v1.md").unlink()
        result = EM.build(self.cfg)
        self.assertEqual(result["dropped"], 1)
        self.assertNotIn("Memory/Retry policy v1.md", self.stored())
        self.assertEqual(len(list((self.cfg["_state"] / "embeddings").glob("vectors-*.f32"))), 1)

    def test_a_failure_keeps_what_was_done_and_says_why(self):
        self.ollama(base_url=dead_url())
        with self.assertRaises(EM.EmbedError) as caught:
            EM.build(self.cfg)
        self.assertEqual(str(caught.exception), "connection")
        self.assertEqual(caught.exception.result["embedded"], 0)

    def test_refresh_embeds_and_a_dead_server_never_fails_it(self):
        self.ollama()
        env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
        out = subprocess.run([sys.executable, "-B", str(CLI), "--vault", str(self.vault), "refresh", "--no-distill"],
                             capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("embeddings     fake-embed", out.stdout)
        self.assertTrue(EM.available(self.cfg))
        self.ollama(base_url=dead_url(), model="another")
        out = subprocess.run([sys.executable, "-B", str(CLI), "--vault", str(self.vault), "refresh", "--no-distill"],
                             capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual([line for line in out.stdout.splitlines() if "embed:" in line],
                         ["  embed: connection (recall keeps working with plain BM25)"])


class TestRerank(Base):
    QUERY = "checkout down"

    def setUp(self):
        super().setUp()
        self.ollama()
        self.idx = RC.load(self.cfg)
        self.plain = RC.search(self.idx, self.QUERY, n=30)
        self.assertGreater(len(self.plain), 2)
        # The note BM25 puts second is the only one that means what the query means.
        self.target = self.plain[1][1]
        target_text = EM.note_text(self.cfg, self.target)
        self.server.vectorize = lambda text: [1.0, 0.0, 0.0] if text in (target_text, self.QUERY) else OTHER
        EM.build(self.cfg)

    def test_cosine_that_disagrees_with_bm25_changes_the_order(self):
        results, reranked = EM.rerank(self.cfg, self.QUERY, self.plain)
        self.assertTrue(reranked)
        self.assertEqual(results[0][1]["p"], self.target["p"])
        self.assertEqual(sorted(d["p"] for _, d in results), sorted(d["p"] for _, d in self.plain))
        self.assertAlmostEqual(results[0][0], self.plain[0][0], places=6)
        self.assertEqual([s for s, _ in results], sorted((s for s, _ in results), reverse=True))

    def test_bm25_weight_one_keeps_bm25_order(self):
        self.configure(self.cfg["embeddings"], rerank={"weight": 1.0})
        results, _ = EM.rerank(self.cfg, self.QUERY, self.plain)
        self.assertEqual([d["p"] for _, d in results], [d["p"] for _, d in self.plain])

    def test_exact_ids_and_tickets_keep_bm25_order_without_a_call(self):
        before = len(self.server.requests)
        for query in ("ATL-160", "48213"):
            plain = RC.search(self.idx, query, n=30)
            self.assertEqual(EM.rerank(self.cfg, query, plain), (plain, False))
        self.assertEqual(len(self.server.requests), before)

    def test_a_candidate_without_a_vector_gets_the_median(self):
        meta = EM.load_store(self.cfg)
        missing = self.plain[2][1]["p"]
        del meta["notes"][missing]
        (self.cfg["_state"] / "embeddings" / "index.json").write_text(json.dumps(meta), encoding="utf-8")
        results, reranked = EM.rerank(self.cfg, self.QUERY, self.plain)
        self.assertTrue(reranked)
        self.assertEqual(results[0][1]["p"], self.target["p"])
        self.assertIn(missing, [d["p"] for _, d in results])

    def test_server_down_falls_back_to_bm25_and_logs_a_short_reason(self):
        self.ollama(base_url=dead_url())
        self.assertEqual(EM.rerank(self.cfg, self.QUERY, self.plain), (self.plain, False))
        errors = self.events("rerank_error")
        # A refused connection: Windows retries it until the timeout, the others refuse at once.
        self.assertEqual([e["origin"] for e in errors], ["cli"])
        self.assertIn(errors[0]["reason"], ("connection", "timeout"))
        self.assertNotIn("q", errors[0])

    def test_wrong_dimension_falls_back(self):
        self.server.vectorize = lambda text: [1.0, 0.0]
        self.assertEqual(EM.rerank(self.cfg, self.QUERY, self.plain), (self.plain, False))
        self.assertEqual([e["reason"] for e in self.events("rerank_error")], ["dimension"])


class TestWhereItRuns(Base):
    QUERY = "checkout went down"

    def setUp(self):
        super().setUp()
        self.ollama()
        EM.build(self.cfg)
        self.env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
        profile = self.tmp / "profile"
        profile.mkdir()
        self.env["CLAUDE_CONFIG_DIR"] = str(profile)

    def recall(self, *extra):
        before = len(self.server.requests)
        out = subprocess.run([sys.executable, "-B", str(CLI), "--vault", str(self.vault), "recall", self.QUERY,
                              "--json", *extra], capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return len(self.server.requests) - before, json.loads(out.stdout)

    def test_cli_recall_reranks_and_no_rerank_forces_bm25(self):
        calls, rows = self.recall()
        self.assertEqual(calls, 1)
        self.assertTrue(rows)
        self.assertTrue(self.events("recall")[-1].get("rerank"))
        calls, plain = self.recall("--no-rerank")
        self.assertEqual(calls, 0)
        self.assertNotIn("rerank", self.events("recall")[-1])
        self.assertEqual(len(rows), len(plain))

    def hook(self):
        before = len(self.server.requests)
        out = subprocess.run([sys.executable, "-B", str(HOOK)], input=json.dumps({"prompt": "checkout went down because of the certificate, what do we do"}),
                             capture_output=True, text=True, env={**self.env, "VAULT_MEMORY_VAULT": str(self.vault)},
                             timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("[vault]", out.stdout)
        return len(self.server.requests) - before

    def test_the_prompt_hook_does_not_rerank_unless_asked(self):
        self.assertEqual(self.hook(), 0)
        self.configure(self.cfg["embeddings"], rerank={"hook": True})
        self.assertEqual(self.hook(), 1)

    def test_mcp_recall_reranks(self):
        import mcp_server as MCP
        before = len(self.server.requests)
        rows = MCP._recall(self.cfg, {"q": self.QUERY})
        self.assertTrue(rows)
        self.assertEqual(len(self.server.requests) - before, 1)
        self.assertTrue(self.events("recall")[-1].get("rerank"))


class TestDoctor(Base):
    def doctor(self):
        env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
        out = subprocess.run([sys.executable, "-B", str(CLI), "--vault", str(self.vault), "doctor", "--json"],
                             capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)["embeddings"]

    def test_shape_and_counts_without_network(self):
        keys = {"provider", "model", "eligible", "embedded", "stale", "missing", "ready", "detail"}
        off = self.doctor()
        self.assertEqual(set(off), keys)
        self.assertEqual((off["provider"], off["ready"]), ("off", False))
        self.ollama()
        before = self.doctor()
        self.assertEqual((before["embedded"], before["ready"]), (0, False))
        self.assertIn("vault-memory embed", before["detail"])
        EM.build(self.cfg)
        note = self.vault / "Memory" / "Retry policy v2.md"
        note.write_text(note.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
        calls = len(self.server.requests)
        after = self.doctor()
        self.assertEqual(len(self.server.requests), calls, "doctor never calls the provider")
        self.assertEqual((after["embedded"], after["eligible"], after["stale"], after["missing"], after["ready"]),
                         (before["eligible"], before["eligible"], 1, 0, True))


class TestBench(Base):
    def setUp(self):
        super().setUp()
        (self.vault / ".vault-memory" / "eval.tsv").write_text(
            "payment retry policy\tRetry policy v2\nrenew TLS certificate\tRenew TLS\n"
            "how to request production access\tproduction access\n", encoding="utf-8")

    def test_compare_says_why_it_cannot(self):
        self.configure()
        result = BN.run(self.cfg, do_compare=True)
        self.assertFalse(result["rerank"])
        self.assertEqual(result["compare"]["available"], False)
        self.assertIn("embeddings are off", result["compare"]["reason"])
        self.ollama()
        self.assertIn("vault-memory embed", BN.run(self.cfg, do_compare=True)["compare"]["reason"])

    def test_compare_shape_and_mode_recorded(self):
        self.ollama()
        EM.build(self.cfg)
        result = BN.run(self.cfg, do_compare=True)
        self.assertTrue(result["rerank"])
        comp = result["compare"]
        self.assertEqual(set(comp), {"available", "cases", "bm25", "rerank", "gain", "gain_ci", "by_kind"})
        self.assertEqual(comp["cases"], 3)
        self.assertEqual(set(comp["bm25"]), {"hit1", "hit3", "mrr", "mrr_ci"})
        self.assertEqual(set(comp["by_kind"]["hand"]), {"cases", "bm25", "rerank", "gain", "gain_ci"})
        self.assertEqual(len(comp["gain_ci"]), 2)
        self.assertEqual(json.loads((self.cfg["_state"] / "bench.json").read_text(encoding="utf-8"))["rerank"], True)
        self.assertEqual(result, BN.run(self.cfg, do_compare=True), "same input, same output")
        plain = BN.run(self.cfg, rerank=False)
        self.assertFalse(plain["rerank"])
        self.assertNotIn("compare", plain)
        self.assertEqual(plain["mrr"], comp["bm25"]["mrr"])

    def test_cli_prints_the_paired_difference(self):
        self.ollama()
        EM.build(self.cfg)
        env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
        out = subprocess.run([sys.executable, "-B", str(CLI), "--vault", str(self.vault), "bench", "--compare"],
                             capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("paired gain", out.stdout)
        self.assertIn("BM25 + semantic rerank", out.stdout)


class TestPrivacy(Base):
    SECRET = "Write to jane.doe@example.com with key sk-abcdefghijklmnopqrstuvwx."

    def setUp(self):
        super().setUp()
        (self.vault / "Memory" / "Contact.md").write_text(
            f"---\ntype: doc\narea: memory\ntags: [x]\n---\n# Contact\n\n{self.SECRET}\n", encoding="utf-8")

    def test_openai_compat_gets_redacted_text_and_the_key_from_the_env(self):
        self.configure({"provider": "openai-compat", "model": "text-embed", "base_url": self.server.url,
                        "api_key_env": "MY_EMBED_KEY"})
        with mock.patch.dict(os.environ, {"MY_EMBED_KEY": "sk-test-123"}):
            EM.build(self.cfg)
            results = RC.search(RC.load(self.cfg), "contact jane.doe@example.com", n=30)
            EM.rerank(self.cfg, "contact jane.doe@example.com", results)
        sent = "\n".join(self.server.texts())
        self.assertNotIn("jane.doe@example.com", sent)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx", sent)
        self.assertIn("[REDACTED:email]", sent)
        self.assertEqual({r["path"] for r in self.server.requests}, {"/embeddings"})
        self.assertEqual({r["auth"] for r in self.server.requests}, {"Bearer sk-test-123"})

    def test_local_ollama_keeps_the_text_on_the_machine_as_it_is(self):
        self.ollama()
        EM.build(self.cfg)
        self.assertIn("jane.doe@example.com", "\n".join(self.server.texts()))
        self.assertFalse(EM.leaves_machine(EM.options(self.cfg)))
        self.assertTrue(EM.leaves_machine({"provider": "ollama", "base_url": "http://gpu-box:11434"}))


if __name__ == "__main__":
    unittest.main()
