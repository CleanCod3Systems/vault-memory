"""Scheduler: the command and file builders are pure and are checked for the three systems
by passing parameters; the few side effects go through `schedule._sh`, which is mocked here.
Nothing in this file touches a real crontab, launchd agent, systemd unit or scheduled task."""
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path, PurePosixPath
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import schedule as SC   # noqa: E402

VAULT = Path("Some Place") / "My Vault"    # a space in the path on purpose
ENTRY = ["py thon", "cli.py"]              # and in the program
WEEKDAYS = SC.DAYS["weekdays"]
WEEKEND = SC.DAYS["weekend"]


class TestTimes(unittest.TestCase):
    def test_on_calendar(self):
        self.assertEqual(SC.on_calendar(8, 30, None), "*-*-* 08:30:00")
        self.assertEqual(SC.on_calendar(7, 5, WEEKDAYS), "Mon..Fri *-*-* 07:05:00")
        self.assertEqual(SC.on_calendar(23, 0, WEEKEND), "Sat,Sun *-*-* 23:00:00")


class TestSystemdUnits(unittest.TestCase):
    def test_timer_catches_up_missed_runs(self):
        text = SC.systemd_timer(VAULT, 8, 30, WEEKDAYS)
        self.assertIn("OnCalendar=Mon..Fri *-*-* 08:30:00\n", text)
        self.assertIn("Persistent=true\n", text)
        self.assertIn("[Install]\nWantedBy=timers.target\n", text)

    def test_service_quotes_every_word(self):
        # systemd units only exist on Linux: POSIX paths whatever OS runs the test
        vault, log = PurePosixPath("Some Place/My Vault"), PurePosixPath("log dir/schedule.log")
        text = SC.systemd_service(vault, ENTRY, "/home/a b/.claude", log)
        self.assertIn("Type=oneshot\n", text)
        self.assertIn(f'ExecStart="py thon" "cli.py" "--vault" "{vault}" "refresh"\n', text)
        self.assertIn('Environment="CLAUDE_CONFIG_DIR=/home/a b/.claude"\n', text)
        self.assertIn(f"StandardOutput=append:{log}\n", text)

    def test_quote_escapes_systemd_specials(self):
        self.assertEqual(SC._sd_quote('a"b'), '"a\\"b"')
        self.assertEqual(SC._sd_quote("100%$HOME"), '"100%%$$HOME"')
        self.assertEqual(SC._sd_quote("a\\b"), '"a\\\\b"')

    def test_unit_names_are_per_vault(self):
        self.assertEqual(SC.systemd_unit_name(Path("x/My Vault!")), "vault-memory-my-vault")
        self.assertEqual(SC.systemctl_args("enable", "--now", "t.timer"),
                         ["systemctl", "--user", "enable", "--now", "t.timer"])


class TestSchtasks(unittest.TestCase):
    def test_daily(self):
        args = SC.schtasks_create_args(VAULT, 8, 5, None, entry=ENTRY)
        self.assertEqual(args[:6], ["schtasks", "/Create", "/SC", "DAILY", "/ST", "08:05"])
        self.assertNotIn("/D", args)
        self.assertEqual(args[args.index("/TN") + 1], "vault-memory my-vault")
        self.assertEqual(args[-1], "/F")

    def test_weekly_days(self):
        weekdays = SC.schtasks_create_args(VAULT, 18, 0, WEEKDAYS, entry=ENTRY)
        self.assertEqual(weekdays[2:6], ["/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI"])
        weekend = SC.schtasks_create_args(VAULT, 18, 0, WEEKEND, entry=ENTRY)
        self.assertEqual(weekend[weekend.index("/D") + 1], "SAT,SUN")

    def test_run_quotes_paths_with_spaces(self):
        run = SC.schtasks_run(VAULT, ENTRY)
        self.assertEqual(run, f'"py thon" "cli.py" --vault "{VAULT}" refresh')
        # what schtasks receives once subprocess builds the command line on Windows
        line = subprocess.list2cmdline(SC.schtasks_create_args(VAULT, 8, 30, None, entry=ENTRY))
        self.assertIn('/TR "\\"py thon\\" \\"cli.py\\" --vault \\"', line)
        self.assertIn('/TN "vault-memory my-vault"', line)

    def test_delete_and_query(self):
        self.assertEqual(SC.schtasks_delete_args(VAULT),
                         ["schtasks", "/Delete", "/TN", "vault-memory my-vault", "/F"])
        self.assertEqual(SC.schtasks_query_args(VAULT),
                         ["schtasks", "/Query", "/TN", "vault-memory my-vault", "/FO", "LIST"])

    def test_install_refuses_a_command_schtasks_cannot_take(self):
        long_entry = ["x" * 300]
        with mock.patch.object(SC, "_entry", return_value=long_entry), mock.patch.object(SC, "_sh") as sh:
            ok, detail = SC._win_install(VAULT, 8, 0, None)
        self.assertFalse(ok)
        self.assertIn("261", detail)
        sh.assert_not_called()


class TestShimPath(unittest.TestCase):
    def test_windows_uses_localappdata(self):
        with tempfile.TemporaryDirectory() as local:
            self.assertEqual(SC.shim_path("windows", home="H", env={"LOCALAPPDATA": local}),
                             Path(local) / "vault-memory" / "bin" / "vault-memory.cmd")

    def test_windows_without_localappdata(self):
        self.assertEqual(SC.shim_path("windows", home="H", env={}),
                         Path("H") / "AppData" / "Local" / "vault-memory" / "bin" / "vault-memory.cmd")

    def test_posix(self):
        for name in ("mac", "linux"):
            self.assertEqual(SC.shim_path(name, home="H"), Path("H") / ".local" / "bin" / "vault-memory")


class TestCron(unittest.TestCase):
    def test_line_keeps_marker_and_days(self):
        line = SC._cron_line(VAULT, 7, 30, WEEKDAYS)
        self.assertTrue(line.startswith("30 7 * * 1,2,3,4,5 "))
        self.assertTrue(line.endswith(SC._marker(VAULT)))


class TestRunner(unittest.TestCase):
    def test_missing_command_is_a_message_not_a_traceback(self):
        with mock.patch.object(SC.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaises(SC.NoScheduler) as caught:
                SC._sh(["schtasks", "/Query"])
        self.assertIn("schtasks", str(caught.exception))

    def test_returns_code_and_text(self):
        done = mock.Mock(returncode=3, stdout=None, stderr="boom")
        with mock.patch.object(SC.subprocess, "run", return_value=done):
            self.assertEqual(SC._sh(["x"]), (3, "", "boom"))


def _cli(system_name, vault, **flags):
    """Run `schedule` for a fake system with every side effect mocked; return (stdout, calls)."""
    args = Namespace(at=flags.get("at"), days=flags.get("days", "daily"), install=flags.get("install", False),
                     off=flags.get("off", False), status=flags.get("status", False))
    calls = []

    def fake_sh(argv, input=None):
        calls.append((list(argv), input))
        return flags.get("sh", lambda argv, input: (0, "", ""))(argv, input)

    out = io.StringIO()
    with mock.patch.object(SC, "system", return_value=system_name), \
            mock.patch.object(SC, "_sh", side_effect=fake_sh), \
            mock.patch.object(SC, "_entry", return_value=ENTRY), \
            mock.patch.object(SC.shutil, "which", return_value="/bin/systemctl" if flags.get("systemd") else None), \
            contextlib.redirect_stdout(out):
        SC.run(args, {"_vault": vault})
    return out.getvalue(), calls


class TestWindowsFlow(unittest.TestCase):
    def test_dry_run_runs_nothing(self):
        out, calls = _cli("windows", VAULT, at="08:30")
        self.assertEqual([c[1] for c, _ in calls], ["/Query"])   # only reads the current state
        self.assertIn("Dry run", out)
        self.assertIn("vault-memory my-vault", out)

    def test_install_calls_schtasks_create(self):
        out, calls = _cli("windows", VAULT, at="08:30", days="weekdays", install=True)
        create = [c for c, _ in calls if c[:2] == ["schtasks", "/Create"]]
        self.assertEqual(len(create), 1)
        self.assertEqual(create[0], SC.schtasks_create_args(VAULT, 8, 30, WEEKDAYS, entry=ENTRY))
        self.assertIn("installed", out)

    def test_install_failure_is_reported(self):
        out, _ = _cli("windows", VAULT, at="08:30", install=True, sh=lambda a, i: (1, "", "ERROR: Access denied"))
        self.assertIn("could not install: ERROR: Access denied", out)

    def test_off_queries_then_deletes(self):
        out, calls = _cli("windows", VAULT, off=True)
        self.assertEqual([c[1] for c, _ in calls], ["/Query", "/Delete"])
        self.assertIn("removed", out)

    def test_off_when_nothing_scheduled(self):
        out, calls = _cli("windows", VAULT, off=True, sh=lambda a, i: (1, "", "ERROR: not found"))
        self.assertEqual([c[1] for c, _ in calls], ["/Query"])
        self.assertIn("nothing to remove", out)

    def test_status(self):
        listing = "Folder: \\\nTaskName: \\vault-memory my-vault\nNext Run Time: 8:30:00\nStatus: Ready\n"
        out, _ = _cli("windows", VAULT, status=True, sh=lambda a, i: (0, listing, ""))
        self.assertIn("scheduled · Task Scheduler", out)
        self.assertIn("Next Run Time: 8:30:00", out)


class TestLinuxFlow(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        patch = mock.patch.object(SC, "systemd_dir", return_value=Path(self.home.name))
        patch.start()
        self.addCleanup(patch.stop)
        self.vault = Path(self.home.name) / "My Vault"

    def test_systemd_install_writes_units_and_enables_the_timer(self):
        out, calls = _cli("linux", self.vault, at="07:00", days="weekend", install=True, systemd=True)
        commands = [c for c, _ in calls]
        self.assertIn(["systemctl", "--user", "daemon-reload"], commands)
        self.assertIn(["systemctl", "--user", "enable", "--now", "vault-memory-my-vault.timer"], commands)
        timer = (Path(self.home.name) / "vault-memory-my-vault.timer").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=Sat,Sun *-*-* 07:00:00", timer)
        self.assertTrue((Path(self.home.name) / "vault-memory-my-vault.service").exists())
        self.assertIn("Persistent=true", out)

    def test_install_drops_the_old_crontab_line(self):
        old = f"0 8 * * * something  {SC._marker(self.vault)}\n5 5 * * * other\n"

        def sh(argv, input):
            return (0, old, "") if argv[:2] == ["crontab", "-l"] else (0, "", "")
        _out, calls = _cli("linux", self.vault, at="07:00", install=True, systemd=True, sh=sh)
        written = [i for c, i in calls if c == ["crontab", "-"]]
        self.assertEqual(written, ["5 5 * * * other\n"])

    def test_falls_back_to_crontab_without_systemd(self):
        out, calls = _cli("linux", self.vault, at="07:00", install=True, systemd=False)
        self.assertFalse([c for c, _ in calls if c[0] == "systemctl"])
        written = [i for c, i in calls if c == ["crontab", "-"]]
        self.assertEqual(len(written), 1)
        self.assertIn(SC._marker(self.vault), written[0])
        self.assertIn("user crontab", out)

    def test_systemd_that_does_not_answer_means_crontab(self):
        def sh(argv, input):
            return (1, "", "Failed to connect to bus") if argv[0] == "systemctl" else (0, "", "")
        _out, calls = _cli("linux", self.vault, at="07:00", install=True, systemd=True, sh=sh)
        self.assertTrue([c for c, _ in calls if c == ["crontab", "-"]])

    def test_off_removes_units_and_crontab_line(self):
        _cli("linux", self.vault, at="07:00", install=True, systemd=True)
        crontab = f"0 8 * * * x  {SC._marker(self.vault)}\n"

        def sh(argv, input):
            return (0, crontab, "") if argv[:2] == ["crontab", "-l"] else (0, "", "")
        out, calls = _cli("linux", self.vault, off=True, systemd=True, sh=sh)
        self.assertFalse((Path(self.home.name) / "vault-memory-my-vault.timer").exists())
        self.assertFalse((Path(self.home.name) / "vault-memory-my-vault.service").exists())
        self.assertIn(["systemctl", "--user", "disable", "--now", "vault-memory-my-vault.timer"], [c for c, _ in calls])
        self.assertIn("removed", out)

    def test_off_with_nothing_scheduled(self):
        out, _ = _cli("linux", self.vault, off=True, systemd=True)
        self.assertIn("nothing to remove", out)


class TestMacFlow(unittest.TestCase):
    def test_dry_run_shows_the_plist_path(self):
        out, calls = _cli("mac", VAULT, at="08:30")
        self.assertEqual(calls, [])
        self.assertIn("com.vault-memory.my-vault.plist", out)


class TestUnsupported(unittest.TestCase):
    def test_other_systems_get_the_manual_command(self):
        out, calls = _cli("freebsd", VAULT, at="08:30")
        self.assertEqual(calls, [])
        self.assertIn("No automatic support on freebsd", out)


if __name__ == "__main__":
    unittest.main()
