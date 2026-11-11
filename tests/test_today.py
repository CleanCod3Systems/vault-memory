"""The daily summary (`scripts/today.py`) and its SessionStart hook (`hooks/today.py`)."""
import datetime as dt
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import core as C        # noqa: E402
import indexer as IX    # noqa: E402
import today as TD      # noqa: E402

HOOK = REPO / "hooks" / "today.py"
ENV_KEYS = ("VAULT_MEMORY_VAULT", "CEREBRO_VAULT", "VAULT_MEMORY_DISABLE", "CLAUDE_CODE_ENTRYPOINT")
TODAY = dt.date(2026, 11, 10)

# id, title, status, priority, due, waiting_on
TASKS = [
    ("T-001", "Rotate the staging certificate", "next", "high", "2026-11-12", ""),
    ("T-002", "Write the onboarding checklist", "doing", "normal", "", ""),
    ("T-003", "Approve the vendor quote", "waiting", "", "2026-11-20", "finance"),
    ("T-004", "Renew the domain", "next", "critical", "2026-11-08", ""),
    ("T-005", "Archive the old dashboards", "next", "normal", "2026-12-30", ""),
    ("T-006", "Update the runbook index", "next", "", "", ""),
    ("T-007", "Already finished chore", "done", "high", "2026-11-09", ""),
]


def write_note(vault, rel, front, body="body"):
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n" + "".join(f"{k}: {v}\n" for k, v in front.items() if v != "")
                    + "---\n" + body + "\n", encoding="utf-8")


def make_vault(root, tasks=TASKS, today=None, fields=None, extra=0):
    vault = Path(root) / "vault"
    (vault / ".vault-memory").mkdir(parents=True)
    fields = fields or {}
    cfg = {"today": {"type": "task", **fields}}
    (vault / ".vault-memory" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    names = {"status": "status", "priority": "priority", "due": "due", "waiting": "waiting_on",
             "id": "id", "title": "title", "type": "type"}
    for key, value in {"status_field": "status", "priority_field": "priority", "due_field": "due",
                       "waiting_field": "waiting_on", "id_field": "id", "title_field": "title"}.items():
        names[key.split("_")[0]] = fields.get(key, value)
    for tid, title, status, priority, due, waiting in tasks:
        write_note(vault, f"Tasks/{tid}.md", {
            "type": "task", names["id"]: tid, names["title"]: title, names["status"]: status,
            names["priority"]: priority, names["due"]: due, names["waiting"]: waiting})
    write_note(vault, "Notes/Meeting.md", {"type": "meeting", "status": "next", "id": "M-1"})
    for i in range(extra):
        write_note(vault, f"Docs/Doc {i}.md", {"type": "doc"}, "lorem ipsum dolor " * 40)
    refresh(vault)
    return vault


def refresh(vault):
    cfg = C.load_config(vault)
    IX.build(cfg, quiet=True)
    return cfg


class TestRender(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = make_vault(self.tmp)
        self.cfg = C.load_config(self.vault)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_sample_summary(self):
        self.assertEqual(TD.render(self.cfg, today=TODAY), "\n".join([
            "today: 5 next | 1 waiting | 2 due soon",
            "next: T-004 Renew the domain [critical] (due 2026-11-08); "
            "T-001 Rotate the staging certificate [high] (due 2026-11-12); "
            "T-005 Archive the old dashboards [normal] (due 2026-12-30) (+2)",
            "waiting: T-003 Approve the vendor quote (finance) (due 2026-11-20)",
            "full list: vault-memory today --all"]))

    def test_output_is_deterministic(self):
        self.assertEqual(TD.render(self.cfg, today=TODAY), TD.render(self.cfg, today=TODAY))

    def test_today_comes_from_the_pinned_date(self):
        os.environ["VAULT_MEMORY_TODAY"] = "2026-11-10"
        try:
            self.assertEqual(TD.render(self.cfg), TD.render(self.cfg, today=TODAY))
        finally:
            del os.environ["VAULT_MEMORY_TODAY"]

    def test_collect_selects_by_type_and_status_and_orders_by_priority_then_due_then_id(self):
        data = TD.collect(self.cfg, TODAY)
        self.assertEqual([i["id"] for i in data["groups"]["next"]],
                         ["T-004", "T-001", "T-005", "T-002", "T-006"])
        self.assertEqual([i["id"] for i in data["groups"]["waiting"]], ["T-003"])
        self.assertEqual(data["counts"], {"next": 5, "waiting": 1})
        self.assertNotIn("M-1", [i["id"] for g in data["groups"].values() for i in g])
        self.assertNotIn("T-007", [i["id"] for g in data["groups"].values() for i in g])

    def test_due_soon_means_within_the_window_or_overdue(self):
        data = TD.collect(self.cfg, TODAY)
        self.assertEqual([i["id"] for i in data["due"]], ["T-004", "T-001"])
        self.assertEqual(data["due_count"], 2)

    def test_due_line_lists_only_what_the_groups_did_not_show(self):
        cfg = C.load_config(self.vault)
        cfg["today"]["per_group"] = 1
        text = TD.render(cfg, today=TODAY)
        self.assertIn("due soon: T-001 Rotate the staging certificate [high] (2026-11-12)", text)
        self.assertNotIn("due soon: T-004", text)

    def test_overdue_is_marked(self):
        tasks = [("T-001", "Renew the domain", "next", "high", "2026-11-08", ""),
                 ("T-002", "Rotate the staging certificate", "next", "critical", "", "")]
        tmp = Path(tempfile.mkdtemp())
        try:
            cfg = C.load_config(make_vault(tmp, tasks, fields={"per_group": 1}))
            self.assertIn("due soon: T-001 Renew the domain [high] (overdue 2026-11-08)",
                          TD.render(cfg, today=TODAY))
        finally:
            shutil.rmtree(tmp)

    def test_all_items_ignores_the_caps_and_the_footer(self):
        text = TD.render(self.cfg, all_items=True, today=TODAY)
        for tid in ("T-005", "T-006"):
            self.assertIn(tid, text)
        self.assertNotIn("full list", text)

    def test_never_more_than_max_lines_and_max_bytes(self):
        cfg = C.load_config(self.vault)
        cfg["today"].update({"max_lines": 3})
        lines = TD.render(cfg, today=TODAY).split("\n")
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("today:") and lines[-1].startswith("full list"))
        cfg["today"].update({"max_lines": 8, "max_bytes": 200})
        self.assertLessEqual(len(TD.render(cfg, today=TODAY).encode()), 200)

    def test_default_budget_fits_about_700_bytes_with_long_titles(self):
        tasks = [(f"T-{i:03d}", "A very long task title that goes on and on " * 4, "next", "high", "", "")
                 for i in range(1, 9)]
        tmp = Path(tempfile.mkdtemp())
        try:
            cfg = C.load_config(make_vault(tmp, tasks))
            text = TD.render(cfg, today=TODAY)
            self.assertLessEqual(len(text.encode()), 600)
            self.assertLessEqual(len(text.split("\n")), 8)
        finally:
            shutil.rmtree(tmp)

    def test_off_without_a_type(self):
        cfg = C.load_config(self.vault)
        del cfg["today"]
        self.assertEqual(TD.render(cfg, today=TODAY), "")
        self.assertFalse(TD.collect(cfg, TODAY)["enabled"])

    def test_nothing_open(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            cfg = C.load_config(make_vault(tmp, [("T-009", "Done already", "done", "", "", "")]))
            self.assertEqual(TD.render(cfg, today=TODAY), "today: nothing open")
        finally:
            shutil.rmtree(tmp)

    def test_custom_field_names_in_spanish(self):
        fields = {"status_field": "estado", "priority_field": "prioridad", "due_field": "vence_el",
                  "waiting_field": "espera", "id_field": "codigo", "title_field": "titulo",
                  "groups": {"next": ["siguiente"], "waiting": ["esperando"]},
                  "priority_order": ["alta", "normal"]}
        tasks = [("T-001", "Rotate the staging certificate", "siguiente", "normal", "", ""),
                 ("T-002", "Approve the vendor quote", "esperando", "", "", "finance"),
                 ("T-003", "Renew the domain", "siguiente", "alta", "", "")]
        tmp = Path(tempfile.mkdtemp())
        try:
            cfg = C.load_config(make_vault(tmp, tasks, fields=fields))
            self.assertEqual(TD.render(cfg, today=TODAY).split("\n")[:3], [
                "today: 2 next | 1 waiting",
                "next: T-003 Renew the domain [alta]; T-001 Rotate the staging certificate [normal]",
                "waiting: T-002 Approve the vendor quote (finance)"])
        finally:
            shutil.rmtree(tmp)

    def test_latest_distill_event_of_the_last_two_days_is_one_line(self):
        state = self.vault / ".vault-memory"
        now = dt.datetime.now()
        events = [{"t": (now - dt.timedelta(hours=30)).isoformat(timespec="seconds"), "sid": "cli",
                   "ev": "distill", "day": "old", "kept": 9},
                  {"t": now.isoformat(timespec="seconds"), "sid": "cli", "ev": "distill",
                   "day": "2026-11-09", "sessions": 4, "proposed": 5, "kept": 2, "updated": 1,
                   "replaced": 0, "discarded": 2, "confirmed": 1}]
        (state / "telemetry.jsonl").write_text("\n".join(map(json.dumps, events)) + "\n", encoding="utf-8")
        text = TD.render(C.load_config(self.vault), today=TODAY)
        self.assertIn("distill 2026-11-09: 2 kept, 1 updated, 1 confirmed", text)
        self.assertNotIn("9 kept", text)


class TestHook(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = make_vault(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_hook(self, payload=None, entrypoint="cli", env_extra=None, vault=True):
        env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
        profile = self.tmp / "profile"
        profile.mkdir(exist_ok=True)
        env.update({"CLAUDE_CONFIG_DIR": str(profile), "VAULT_MEMORY_TODAY": "2026-11-10"})
        if vault:
            env["VAULT_MEMORY_VAULT"] = str(self.vault)
        if entrypoint:
            env["CLAUDE_CODE_ENTRYPOINT"] = entrypoint
        env.update(env_extra or {})
        stdin = json.dumps({"source": "startup", "session_id": "s1"} if payload is None else payload)
        result = subprocess.run([sys.executable, "-B", str(HOOK)], input=stdin,
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_startup_prints_the_summary_and_one_instruction_within_budget(self):
        out = self.run_hook()
        lines = out.rstrip("\n").split("\n")
        self.assertTrue(lines[0].startswith("[vault] today: 5 next | 1 waiting"), lines[0])
        self.assertIn("T-004 Renew the domain", out)
        self.assertIn("in their language", lines[-1])
        self.assertLessEqual(len(out.encode()), 700)
        events = [json.loads(x) for x in
                  (self.vault / ".vault-memory" / "telemetry.jsonl").read_text().splitlines()]
        self.assertEqual([(e["ev"], e["sid"]) for e in events], [("today", "s1")])
        self.assertEqual(events[0]["n"], len(lines) - 1)

    def test_only_once_per_day(self):
        self.assertNotEqual(self.run_hook(), "")
        marker = self.vault / ".vault-memory" / "today-shown"
        self.assertEqual(marker.read_text().strip(), "2026-11-10")
        self.assertEqual(self.run_hook(), "")
        self.assertNotEqual(self.run_hook(env_extra={"VAULT_MEMORY_TODAY": "2026-11-11"}), "")

    def test_silent_for_other_sources_entrypoints_and_disable(self):
        for source in ("resume", "clear", "compact"):
            self.assertEqual(self.run_hook({"source": source}), "", source)
        self.assertEqual(self.run_hook(entrypoint="sdk-cli"), "")
        self.assertEqual(self.run_hook(entrypoint=""), "")
        self.assertEqual(self.run_hook(env_extra={"VAULT_MEMORY_DISABLE": "1"}), "")
        self.assertFalse((self.vault / ".vault-memory" / "today-shown").exists())

    def test_entrypoints_come_from_the_config(self):
        config_file = self.vault / ".vault-memory" / "config.json"
        config = json.loads(config_file.read_text())
        config["today"]["entrypoints"] = ["cli", "claude-desktop"]
        config_file.write_text(json.dumps(config))
        self.assertNotEqual(self.run_hook(entrypoint="claude-desktop"), "")

    def test_silent_when_not_configured_or_nothing_open(self):
        config_file = self.vault / ".vault-memory" / "config.json"
        config_file.write_text("{}")
        self.assertEqual(self.run_hook(), "")
        config_file.write_text(json.dumps({"today": {"type": "bug"}}))
        self.assertEqual(self.run_hook(), "")

    def test_broken_input_exits_zero_without_output(self):
        for stdin in ("", "not json", "[]", "null"):
            self.assertEqual(self.run_hook(stdin), "", stdin)
        self.assertEqual(self.run_hook(vault=False), "")
        (self.vault / ".vault-memory" / "index.json").unlink()
        self.assertEqual(self.run_hook(), "")

    def test_latency_on_a_vault_of_500_notes(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            self.vault = make_vault(tmp, extra=480)
            times = []
            for i in range(10):
                (self.vault / ".vault-memory" / "today-shown").unlink(missing_ok=True) \
                    if sys.version_info >= (3, 8) else None
                start = time.perf_counter()
                self.assertNotEqual(self.run_hook(), "")
                times.append((time.perf_counter() - start) * 1000)
            times.sort()
            print(f"\ntoday hook latency ms: p50={statistics.median(times):.0f} p95={times[-1]:.0f}",
                  file=sys.stderr)
            # Target < 300 ms; only the median is asserted (whole process, interpreter start included).
            self.assertLess(statistics.median(times), 300)
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
