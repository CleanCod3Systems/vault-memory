"""More precise recall: expanded keys (aliases, key, keys) and time expressions in the query.
Synthetic notes only."""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import core as C          # noqa: E402
import recall as RC       # noqa: E402

TODAY = dt.date(2026, 11, 11)   # a Wednesday


def day(text):
    return dt.date.fromisoformat(text)


class TestTimeQuery(unittest.TestCase):
    def span(self, query):
        spans, rest = RC.time_query(query, TODAY)
        return spans, rest

    def test_relative_expressions_in_both_languages(self):
        cases = {
            "what broke yesterday": [(day("2026-11-10"), day("2026-11-10"))],
            "qué se rompió ayer": [(day("2026-11-10"), day("2026-11-10"))],
            "deploys today": [(day("2026-11-11"), day("2026-11-11"))],
            "incidentes de hoy": [(day("2026-11-11"), day("2026-11-11"))],
            "refunds last week": [(day("2026-11-02"), day("2026-11-08"))],
            "reembolsos de la semana pasada": [(day("2026-11-02"), day("2026-11-08"))],
            "what changed last month": [(day("2026-10-01"), day("2026-10-31"))],
            "qué cambió el mes pasado": [(day("2026-10-01"), day("2026-10-31"))],
        }
        for query, expected in cases.items():
            self.assertEqual(self.span(query)[0], expected, query)

    def test_months_and_iso_dates(self):
        october = [(day("2026-10-01"), day("2026-10-31"))]
        self.assertEqual(self.span("pagos en octubre")[0], october)
        self.assertEqual(self.span("payments in October")[0], october)
        self.assertEqual(self.span("octubre 2026")[0], october)
        self.assertEqual(self.span("octubre de 2025")[0], [(day("2025-10-01"), day("2025-10-31"))])
        # Without a year, the latest month that is not in the future.
        self.assertEqual(self.span("in December")[0], [(day("2025-12-01"), day("2025-12-31"))])
        self.assertEqual(self.span("refund 2026-10-05")[0], [(day("2026-10-05"), day("2026-10-05"))])
        self.assertEqual(self.span("cierre 2026-02")[0], [(day("2026-02-01"), day("2026-02-28"))])

    def test_time_words_leave_the_query(self):
        self.assertEqual(self.span("Qué pasó con los pagos ayer")[1], "que paso con los pagos")
        self.assertEqual(self.span("refund 2026-10-05")[1], "refund")
        self.assertEqual(self.span("checkout down in October 2026")[1], "checkout down")

    def test_no_time_expression_leaves_the_query_untouched(self):
        for query in ("may we deploy on friday", "march the release", "ATL-160", "order 48213",
                      "2026-13 bad month", "payments declined"):
            self.assertEqual(self.span(query), ([], query), query)

    def test_note_dates_come_from_frontmatter_or_name(self):
        week = [(day("2026-11-02"), day("2026-11-08"))]
        self.assertTrue(RC.in_time({"date": "2026-11-03", "n": "x"}, week))
        self.assertTrue(RC.in_time({"created": "2026-11-08", "n": "x"}, week))
        self.assertTrue(RC.in_time({"verified": "2026-11-02", "n": "x"}, week))
        self.assertTrue(RC.in_time({"n": "Inc - 2026-11-05 - Outage"}, week))
        self.assertTrue(RC.in_time({"date": "2026-11", "n": "x"}, week))      # a month overlaps
        self.assertFalse(RC.in_time({"date": "2026-10-30", "n": "Note 2026-11-20"}, week))
        self.assertFalse(RC.in_time({"n": "x"}, week))


NOTES = {
    "Memory/Gateway outage early.md": "---\ntype: incident\ndate: 2026-10-20\n---\n"
                                      "# Gateway outage early\nThe payment gateway returned timeouts.\n",
    "Memory/Gateway outage late.md": "---\ntype: incident\ndate: 2026-11-04\n---\n"
                                     "# Gateway outage late\nThe payment gateway returned timeouts.\n",
    "Memory/Unrelated.md": "---\ntype: doc\ndate: 2026-11-04\n---\n# Unrelated\nLunch menu of the office.\n",
    "Memory/Retry rules.md": "---\ntype: doc\naliases: [backoff schedule]\nkey: payment-retry-rules\n"
                             "keys:\n  - how many times do we charge a card again\n---\n"
                             "# Retry rules\nThree attempts with growing waits.\n",
    "Memory/Card charges.md": "---\ntype: doc\n---\n# Card charges\nWe charge a card once per order. "
                              "Charges go through the gateway.\n",
}


class WithVault(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.vault = self.tmp / "vault"
        for rel, text in NOTES.items():
            path = self.vault / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        (self.vault / C.STATE).mkdir()
        (self.vault / C.STATE / "config.json").write_text("{}", encoding="utf-8")
        old = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(old)))
        os.environ["VAULT_MEMORY_TODAY"] = TODAY.isoformat()
        self.addCleanup(C.configure, C.load_config(self.vault))

    def search(self, query, **recall):
        cfg = C.load_config(self.vault)
        cfg["recall"].update(recall)
        C.configure(cfg)
        return [(round(s, 3), d["p"]) for s, d in RC.search(RC.load(cfg), query, n=10)]


class TestTimeBoost(WithVault):
    def test_defaults_are_on(self):
        self.assertTrue(C.CONFIG_DEF["recall"]["expand_keys"])
        self.assertGreater(C.CONFIG_DEF["recall"]["time_boost"], 0)

    def test_dated_note_goes_up_and_nothing_is_dropped(self):
        off = [p for _, p in self.search("gateway outage last week", time_boost=0)]
        on = [p for _, p in self.search("gateway outage last week")]
        self.assertEqual(on[0], "Memory/Gateway outage late.md")
        self.assertEqual(set(off), set(on), "a time expression only boosts, never filters")
        self.assertIn("Memory/Gateway outage early.md", on)

    def test_today_comes_from_the_pinned_date(self):
        os.environ["VAULT_MEMORY_TODAY"] = "2026-11-02"      # "last month" is October now
        self.assertEqual(self.search("gateway outage last month")[0][1], "Memory/Gateway outage early.md")

    def test_off_ignores_time_words(self):
        on = self.search("gateway outage in October")
        off = self.search("gateway outage in October", time_boost=0)
        self.assertEqual(on[0][1], "Memory/Gateway outage early.md")
        self.assertNotEqual(on, off)

    def test_only_a_date_lists_that_range_below_any_floor(self):
        rows = self.search("last week")
        self.assertEqual({p for _, p in rows}, {"Memory/Gateway outage late.md", "Memory/Unrelated.md"})
        self.assertTrue(all(score <= 1 for score, _ in rows))
        self.assertEqual(self.search("last week", time_boost=0), [])

    def test_unrelated_note_in_range_is_not_lifted_without_a_match(self):
        self.assertNotIn("Memory/Unrelated.md", [p for _, p in self.search("gateway timeouts last week")])


class TestExpandedKeys(WithVault):
    def test_alias_key_and_question_find_the_note(self):
        for query in ("backoff schedule", "payment retry rules", "how many times do we charge a card again"):
            on = self.search(query)
            self.assertEqual(on[0][1], "Memory/Retry rules.md", query)

    def test_off_does_not_index_them(self):
        self.assertEqual(self.search("backoff schedule", expand_keys=False), [])
        off = self.search("how many times do we charge a card again", expand_keys=False)
        self.assertNotEqual(off[0][1] if off else "", "Memory/Retry rules.md")

    def test_alternate_keys_are_kept_in_the_index(self):
        cfg = C.load_config(self.vault)
        docs = {d["p"]: d for d in RC.load(cfg)["docs"]}
        self.assertEqual(docs["Memory/Retry rules.md"]["alt"],
                         ["payment retry rules", "backoff schedule", "how many times do we charge a card again"])

    def test_an_index_from_an_older_version_is_rebuilt(self):
        cfg = C.load_config(self.vault)
        RC.load(cfg)
        index = cfg["_state"] / "index.json"
        stored = json.loads(index.read_text(encoding="utf-8"))
        stored["cfg"] = "older-format"
        for d in stored["docs"]:
            d.pop("alt", None)
        index.write_text(json.dumps(stored), encoding="utf-8")
        self.assertFalse(RC.up_to_date(cfg, stored))
        self.assertTrue(all("alt" in d for d in RC.load(cfg)["docs"]))


if __name__ == "__main__":
    unittest.main()
