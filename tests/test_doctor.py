"""Doctor verdicts: shipped-SDK games, and Steam launch-option handling."""
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import vestctl as vst

APPID = "999001"


def config_root(body):
    root = Path(tempfile.mkdtemp(prefix="bhaptics-steam-"))
    cfg = root / "userdata" / "83526121" / "config"
    cfg.mkdir(parents=True)
    (cfg / "localconfig.vdf").write_text(body)
    return root


def steam_library(launch=None, mod=False, tact=False):
    """A throwaway Steam root: one Proton game, and the launch options a real
    userdata file would carry (None = no userdata we can read)."""
    root = Path(tempfile.mkdtemp(prefix="bhaptics-lib-"))
    sa = root / "steamapps"
    gd = sa / "common" / "Some Game"
    (gd / "Some Game_Data" / "Plugins" / "x86_64").mkdir(parents=True)
    (gd / "Some Game.exe").write_bytes(b"MZ")
    (gd / "Some Game_Data" / "Plugins" / "x86_64" / "bhaptics_library.dll").write_bytes(
        b"bhaptics")
    if mod:
        (gd / "Mods").mkdir()
        (gd / "Mods" / "SomeGame_bhaptics.dll").write_bytes(b"mel BHAPTICS mel")
        (gd / "version.dll").write_bytes(b"MZ")
    if tact:
        (gd / "mod").mkdir()
        (gd / "mod" / "ShootX.tact").write_text(
            '{"project": {"name": "Internal_Name", "tracks": []}}')
    (sa / f"appmanifest_{APPID}.acf").write_text(
        f'"AppState"\n{{\n\t"appid"\t\t"{APPID}"\n\t"name"\t\t"Some Game"\n'
        '\t"installdir"\t\t"Some Game"\n}\n')
    if launch is not None:
        cfg = root / "userdata" / "83526121" / "config"
        cfg.mkdir(parents=True)
        (cfg / "localconfig.vdf").write_text(
            f'"apps"\n{{\n\t"{APPID}"\n\t{{\n'
            + (f'\t\t"LaunchOptions"\t\t"{launch}"\n' if launch else "")
            + '\t\t"Playtime"\t\t"7"\n\t}\n}\n')
    return root


class LaunchOptionsTest(unittest.TestCase):
    def test_reads_configured_launch_options(self):
        root = config_root(f'"UserLocalConfigStore"\n{{\n\t"apps"\n\t{{\n'
                           f'\t\t"{APPID}"\n\t\t{{\n'
                           '\t\t\t"LaunchOptions"\t\t"VAR=1 %command%"\n'
                           '\t\t\t"Playtime"\t\t"7"\n\t\t}\n\t}\n}\n')
        self.assertEqual(vst.steam_launch_options(APPID, [root]), "VAR=1 %command%")

    def test_empty_when_the_app_has_none(self):
        root = config_root(f'"apps"\n{{\n\t"{APPID}"\n\t{{\n\t\t"Playtime"\t"7"\n\t}}\n}}\n')
        self.assertEqual(vst.steam_launch_options(APPID, [root]), "")

    def test_none_when_the_app_is_absent(self):
        root = config_root('"apps"\n{\n\t"999002"\n\t{\n\t\t"Playtime"\t"7"\n\t}\n}\n')
        self.assertIsNone(vst.steam_launch_options(APPID, [root]))

    def test_braces_inside_a_value_do_not_confuse_it(self):
        root = config_root(f'"apps"\n{{\n\t"{APPID}"\n\t{{\n'
                           '\t\t"LaunchOptions"\t\t"%command% {x}"\n\t}\n}\n')
        self.assertEqual(vst.steam_launch_options(APPID, [root]), "%command% {x}")


class PlayerWrapperTest(unittest.TestCase):
    def test_inserted_before_command(self):
        self.assertEqual(
            vst.with_player_wrapper("PRESSURE_VESSEL_IMPORT_OPENXR_1_RUNTIMES=1 %command%",
                                    "/w.sh"),
            "PRESSURE_VESSEL_IMPORT_OPENXR_1_RUNTIMES=1 /w.sh %command%")

    def test_keeps_trailing_arguments(self):
        self.assertEqual(
            vst.with_player_wrapper('WINEDLLOVERRIDES="x=n,b" %command% -vrmode openvr',
                                    "/w.sh"),
            'WINEDLLOVERRIDES="x=n,b" /w.sh %command% -vrmode openvr')

    def test_bare_command(self):
        self.assertEqual(vst.with_player_wrapper("", "/w.sh"), "/w.sh %command%")

    def test_no_command_token_is_left_alone(self):
        self.assertIsNone(vst.with_player_wrapper("-foo -bar", "/w.sh"))


class SetLaunchOptionsTest(unittest.TestCase):
    def test_rewrites_in_place_and_keeps_siblings(self):
        root = config_root(f'"apps"\n{{\n\t"{APPID}"\n\t{{\n'
                           '\t\t"LaunchOptions"\t\t"VAR=1 %command%"\n'
                           '\t\t"Playtime"\t\t"7"\n\t}\n}\n')
        vst.set_steam_launch_options(APPID, "VAR=1 /w.sh %command%", [root])
        cfg = next(root.glob("userdata/*/config/localconfig.vdf"))
        self.assertEqual(vst.steam_launch_options(APPID, [root]), "VAR=1 /w.sh %command%")
        self.assertIn('"Playtime"', cfg.read_text())
        self.assertTrue(cfg.with_name(cfg.name + ".bhaptics-bak").exists())

    def test_adds_the_key_when_missing(self):
        root = config_root(f'"apps"\n{{\n\t"{APPID}"\n\t{{\n\t\t"Playtime"\t"7"\n\t}}\n}}\n')
        vst.set_steam_launch_options(APPID, "/w.sh %command%", [root])
        self.assertEqual(vst.steam_launch_options(APPID, [root]), "/w.sh %command%")

    def test_unknown_app_is_not_touched(self):
        root = config_root('"apps"\n{\n\t"999002"\n\t{\n\t\t"Playtime"\t"7"\n\t}\n}\n')
        cfg = next(root.glob("userdata/*/config/localconfig.vdf"))
        before = cfg.read_text()
        self.assertIsNone(vst.set_steam_launch_options(APPID, "/w.sh %command%", [root]))
        self.assertEqual(cfg.read_text(), before)


class VerdictTest(unittest.TestCase):
    def setUp(self):
        self._orig = (vst.steam_libraries, vst.steam_running, vst.PENDING_FILE)
        self.addCleanup(self._restore)
        vst.PENDING_FILE = Path(tempfile.mkdtemp(prefix="bhaptics-state-")) / "pending.json"
        vst.steam_running = lambda: False

    def _restore(self):
        (vst.steam_libraries, vst.steam_running, vst.PENDING_FILE) = self._orig

    def diagnose(self, root, fix=False):
        vst.steam_libraries = lambda: [root]
        report, todo = vst.diagnose(APPID)
        if fix:
            for _, action in todo:
                if action:
                    action()
            report, todo = vst.diagnose(APPID)
        return report, vst.finish_report(report, todo)

    def test_shipped_sdk_is_not_reported_ready(self):
        # a DLL in the game's own assets proves nothing: Arken Age ships one
        # and never calls it (bHaptics lists it as Mod support)
        report, done = self.diagnose(
            steam_library(launch="VAR=1 /w/proton-wrap.sh %command%"))
        self.assertEqual(report["kind"], "builtin")
        self.assertEqual(report["detected"], ["shipped bHaptics SDK"])
        self.assertFalse(done["ok"])
        self.assertTrue(any("Native/Mod" in m for m in done["manual"]))

    def test_game_mod_is_still_detected(self):
        report, _ = self.diagnose(steam_library(mod=True))
        self.assertEqual(report["kind"], "mod")
        self.assertIn("SomeGame_bhaptics.dll", report["detected"])

    def test_missing_launch_wrapper_is_flagged_and_fixable(self):
        root = steam_library(mod=True, launch="PRESSURE_VESSEL_IMPORT_X=1 %command%")
        report, done = self.diagnose(root)
        self.assertFalse(report["launch_set"])
        self.assertTrue(any("proton-wrap" in m for m in done["issues"]))

        _, done = self.diagnose(root, fix=True)          # --fix applies it
        saved = vst.steam_launch_options(APPID, [root])
        self.assertTrue(saved.startswith("PRESSURE_VESSEL_IMPORT_X=1 "))
        self.assertTrue(saved.endswith("proton-wrap.sh %command%"))
        self.assertFalse(any("proton-wrap" in m
                             for m in done["issues"] + done["manual"]))

    def test_present_launch_wrapper_is_not_flagged(self):
        report, done = self.diagnose(
            steam_library(mod=True, launch="VAR=1 /w/proton-wrap.sh %command%"))
        self.assertTrue(report["launch_set"])
        self.assertFalse(any("proton-wrap" in m
                             for m in done["issues"] + done["manual"]))

    def test_unreadable_launch_options_are_not_flagged(self):
        # no userdata at all: we can't tell, so don't nag
        report, done = self.diagnose(steam_library(mod=True))
        self.assertTrue(report["launch_set"])
        self.assertFalse(any("proton-wrap" in m
                             for m in done["issues"] + done["manual"]))

    def test_steam_change_stays_pending_until_steam_restarts(self):
        root = steam_library(mod=True, launch="VAR=1 %command%")
        vst.steam_running = lambda: True
        _, done = self.diagnose(root, fix=True)
        # written, but Steam will rewrite the file on exit: keep nagging
        self.assertTrue(any("close Steam" in m for m in done["manual"]))
        self.assertFalse(done["ok"])
        self.assertTrue(vst.is_pending(APPID, vst.KIND_LAUNCH))

        vst.steam_running = lambda: False      # confirmed: Steam will read it
        report, done = self.diagnose(root)
        self.assertTrue(report["launch_set"])
        self.assertFalse(report["launch_pending"])
        self.assertFalse(vst.is_pending(APPID, vst.KIND_LAUNCH))
        self.assertFalse(any("close Steam" in m
                             for m in done["issues"] + done["manual"]))

    def test_steam_change_is_not_pending_when_steam_was_closed(self):
        root = steam_library(mod=True, launch="VAR=1 %command%")
        report, done = self.diagnose(root, fix=True)
        self.assertFalse(report["launch_pending"])
        self.assertFalse(vst.is_pending(APPID, vst.KIND_LAUNCH))
        self.assertFalse(any("close Steam" in m
                             for m in done["issues"] + done["manual"]))

    def test_reverted_launch_option_clears_pending_and_reflags(self):
        root = steam_library(mod=True, launch="VAR=1 %command%")
        vst.steam_running = lambda: True
        self.diagnose(root, fix=True)
        self.assertTrue(vst.is_pending(APPID, vst.KIND_LAUNCH))
        # Steam wrote its in-memory copy back, dropping our option
        cfg = next(root.glob("userdata/*/config/localconfig.vdf"))
        cfg.write_text(f'"apps"\n{{\n\t"{APPID}"\n\t{{\n'
                       '\t\t"Playtime"\t"7"\n\t}\n}\n')
        report, done = self.diagnose(root)
        self.assertFalse(report["launch_set"])
        self.assertFalse(vst.is_pending(APPID, vst.KIND_LAUNCH))
        self.assertTrue(any("proton-wrap" in m for m in done["issues"]))

    def test_odd_launch_string_falls_back_to_a_manual_step(self):
        root = steam_library(mod=True, launch="-custom -flags")
        report, done = self.diagnose(root)
        self.assertFalse(report["launch_set"])
        self.assertTrue(any("proton-wrap" in m for m in done["manual"]))
        self.assertFalse(any("proton-wrap" in m for m in done["issues"]))


class ImportFixTest(unittest.TestCase):
    """`doctor --fix` runs `vestctl import` — its chatter must not reach the
    doctor's stdout (that's what made the UI report 'doctor failed'), and a
    pattern we already hold must stop being offered."""

    def setUp(self):
        self._orig = (vst.steam_libraries, vst.steam_running, vst.PENDING_FILE,
                      vst.PATTERNS_DIR, vst.subprocess.run)
        self.addCleanup(self._restore)
        tmp = Path(tempfile.mkdtemp(prefix="bhaptics-fix-"))
        vst.PENDING_FILE = tmp / "pending.json"
        vst.PATTERNS_DIR = tmp / "patterns"
        vst.steam_running = lambda: False

    def _restore(self):
        (vst.steam_libraries, vst.steam_running, vst.PENDING_FILE,
         vst.PATTERNS_DIR, vst.subprocess.run) = self._orig

    def test_json_stays_parseable_when_the_import_runs(self):
        vst.steam_libraries = lambda: [steam_library(
            mod=True, tact=True, launch="VAR=1 /w/proton-wrap.sh %command%")]
        vst.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(
            a, 0, stdout='imported pattern "ShootX"\n', stderr="")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            vst.doctor_json(APPID, True)
        report = json.loads(buf.getvalue())[0]      # raised when polluted
        self.assertTrue(any("ShootX" in n for n in report["notes"]))

    def test_import_is_not_offered_once_the_pattern_exists(self):
        root = steam_library(mod=True, tact=True,
                             launch="VAR=1 /w/proton-wrap.sh %command%")
        vst.steam_libraries = lambda: [root]

        report, todo = vst.diagnose(APPID)
        self.assertTrue(any("import" in d for d, _ in todo))

        vst.PATTERNS_DIR.mkdir(parents=True)
        (vst.PATTERNS_DIR / "ShootX.tact").write_text("{}")
        report, todo = vst.diagnose(APPID)
        self.assertFalse(any("import" in d for d, _ in todo))
        self.assertTrue(any("already imported" in n for n in report["notes"]))


if __name__ == "__main__":
    unittest.main()

