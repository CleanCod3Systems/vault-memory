"""Vaults made by earlier versions (Spanish names, `.cerebro/`) keep working and migrate."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

import core as C        # noqa: E402
import legacy as LG     # noqa: E402

LEGACY_CONFIG = {
    "modulos": {"contrato": True, "grafo": True, "patrones": True, "panel": False, "bases": False,
                "plantillas": False, "tickets": True, "chat": False, "ids": True, "agenda": False},
    "areas": {"Soporte": "soporte"},
    "tipos_por_carpeta": {"Soporte/Incidentes": "incidente", "Docs": "doc"},
    "tipos_extra": {"vuln": "conocimiento"},
    "referencias": {"tickets": {"prefijos": ["ABC"]}},
    "recall": {"saltar_rutas": ["Diario/"]},
    "hook": {"piso": 12, "formato": "compacto"},
}
INCIDENT = ("---\ntipo_nota: incidente\narea: soporte\ntags:\n  - incidente\nfecha: 2026-05-01\n"
            "estado: resuelto\nseveridad: alta\nsistema: pagos\ntipo: pasarela\nverificado: 2026-09-01\n---\n"
            "\n# Pagos rechazados por la pasarela\n\nLa pasarela rechazó los pagos del pedido 48213. ABC-12.\n")
DOC = ("---\ntype: note\ntipo_nota: doc\narea: general\ntags: [doc]\nclave: pasarela-activa\n---\n"
       "\n# Pasarela activa\n\nLa pasarela activa se cambia desde el panel de pagos.\n")


class TestLegacyVault(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        (self.vault / ".obsidian").mkdir(parents=True)
        (self.vault / ".cerebro").mkdir()
        (self.vault / ".cerebro" / "config.json").write_text(json.dumps(LEGACY_CONFIG), encoding="utf-8")
        (self.vault / "Soporte" / "Incidentes").mkdir(parents=True)
        (self.vault / "Docs").mkdir()
        (self.vault / "Soporte" / "Incidentes" / "Inc - pasarela.md").write_text(INCIDENT, encoding="utf-8")
        (self.vault / "Docs" / "Pasarela activa.md").write_text(DOC, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def cli(self, *args):
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "cli.py"), "--vault", str(self.vault), *args],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_config_translation(self):
        cfg = C.load_config(self.vault)
        self.assertEqual(cfg["_state"].name, ".cerebro")
        self.assertTrue(cfg["modules"]["contract"])
        self.assertEqual(cfg["types_by_folder"]["Soporte/Incidentes"], "incident")
        self.assertEqual(cfg["extra_types"], {"vuln": "knowledge"})
        self.assertEqual(cfg["references"]["tickets"]["prefixes"], ["ABC"])
        self.assertEqual(cfg["recall"]["skip_paths"], ["Diario/"])
        self.assertEqual(cfg["hook"], {"floor": 12, "format": "compact"})
        self.assertEqual(cfg["language"]["stemmer"], "es", "legacy vaults were Spanish by default")

    def test_frontmatter_translation(self):
        fm = LG.frontmatter(C.raw_frontmatter(INCIDENT)[0])
        self.assertEqual((fm["type"], fm["date"], fm["status"], fm["class"], fm["verified"]),
                         ("incident", "2026-05-01", "resuelto", "pasarela", "2026-09-01"))
        self.assertEqual(LG.frontmatter(C.raw_frontmatter(DOC)[0])["type"], "doc",
                         "tipo_nota wins over a leftover `type: note`")

    def test_works_before_migrating(self):
        rows = json.loads(self.cli("recall", "pagos rechazados por la pasarela", "--json"))
        self.assertEqual(rows[0]["type"], "incident")
        self.assertEqual(rows[0]["status"], "resuelto")
        report = json.loads(self.cli("doctor", "--json"))
        self.assertEqual(report["health"]["contract_coverage"], 100.0)
        self.assertEqual(report["health"]["open_records"], 0, "Spanish closed statuses still close")

    def test_migrate_dry_run_then_apply(self):
        incident = self.vault / "Soporte" / "Incidentes" / "Inc - pasarela.md"
        dry = self.cli("migrate")
        self.assertIn("Dry run", dry)
        self.assertEqual(incident.read_text(encoding="utf-8"), INCIDENT)
        self.cli("migrate", "--apply")
        self.assertTrue((self.vault / ".vault-memory").is_dir())
        self.assertFalse((self.vault / ".cerebro").exists())
        text = incident.read_text(encoding="utf-8")
        self.assertIn("type: incident\n", text)
        self.assertIn("class: pasarela\n", text)
        self.assertIn("verified: 2026-09-01\n", text)
        self.assertNotIn("tipo_nota", text)
        self.assertTrue(text.endswith("La pasarela rechazó los pagos del pedido 48213. ABC-12.\n"), "body untouched")
        doc = (self.vault / "Docs" / "Pasarela activa.md").read_text(encoding="utf-8")
        self.assertEqual(doc.count("type:"), 1)
        self.assertIn("type: doc\n", doc)
        self.assertIn("key: pasarela-activa\n", doc)
        config = json.loads((self.vault / ".vault-memory" / "config.json").read_text(encoding="utf-8"))
        self.assertIn("types_by_folder", config)
        self.assertNotIn("tipos_por_carpeta", config)
        self.assertTrue(list((self.vault / ".vault-memory" / "backups").rglob("*.md")))
        self.assertIn("Nothing to migrate", self.cli("migrate"), "idempotent")
        rows = json.loads(self.cli("recall", "pagos rechazados por la pasarela", "--json"))
        self.assertEqual(rows[0]["type"], "incident")


if __name__ == "__main__":
    unittest.main()
