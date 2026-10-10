"""Doctor verdicts: shipped-SDK games, and Steam launch-option checking."""
import tempfile
import unittest
from pathlib import Path

import vestctl as vst

APPID = "999001"


def steam_root(body):
    root = Path(tempfile.mkdtemp(prefix="bhaptics-steam-"))
    cfg = root / "userdata" / "83526121" / "config"
    cfg.mkdir(parents=True)
    (cfg / "localconfig.vdf").write_text(body)
    return root


class LaunchOptionsTest(unittest.TestCase):
    def test_reads_configured_launch_options(self):
        root = steam_root('"UserLocalConfigStore"\n{\n\t"apps"\n\t{\n'
                          f'\t\t"{APPID}"\n\t\t{{\n'
                          '\t\t\t"LaunchOptions"\t\t"VAR=1 %command%"\n'
                          '\t\t\t"Playtime"\t\t"7"\n\t\t}\n\t}\n}\n')
        self.assertEqual(vst.steam_launch_options(APPID, [root]), "VAR=1 %command%")

    def test_empty_when_the_app_has_none(self):
        root = steam_root(f'"apps"\n{{\n\t"{APPID}"\n\t{{\n\t\t"Playtime"\t"7"\n\t}}\n}}\n')
        self.assertEqual(vst.steam_launch_options(APPID, [root]), "")

    def test_none_when_the_app_is_absent(self):
        root = steam_root('"apps"\n{\n\t"999002"\n\t{\n\t\t"Playtime"\t"7"\n\t}\n}\n')
        self.assertIsNone(vst.steam_launch_options(APPID, [root]))

    def test_braces_inside_a_value_do_not_confuse_it(self):
        root = steam_root(f'"apps"\n{{\n\t"{APPID}"\n\t{{\n'
                          '\t\t"LaunchOptions"\t\t"%command% {x}"\n\t}\n}\n')
        self.assertEqual(vst.steam_launch_options(APPID, [root]), "%command% {x}")


def game_tree(mod=False):
    """A throwaway Steam library holding one Proton game with the SDK."""
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
    (sa / f"appmanifest_{APPID}.acf").write_text(
        f'"AppState"\n{{\n\t"appid"\t\t"{APPID}"\n\t"name"\t\t"Some Game"\n'
        '\t"installdir"\t\t"Some Game"\n}\n')
    return root


class VerdictTest(unittest.TestCase):
    def setUp(self):
        self._orig_libs = vst.steam_libraries
        self._orig_launch = vst.steam_launch_options
        self.addCleanup(self._restore)

    def _restore(self):
        vst.steam_libraries = self._orig_libs
        vst.steam_launch_options = self._orig_launch

    def diagnose(self, root, launch):
        vst.steam_libraries = lambda: [root]
        vst.steam_launch_options = lambda appid: launch
        report, todo = vst.diagnose(APPID)
        return report, vst.finish_report(report, todo)

    def test_shipped_sdk_is_not_reported_ready(self):
        # a DLL in the game's own assets proves nothing: Arken Age ships one
        # and never calls it (bHaptics lists it as Mod support)
        report, done = self.diagnose(game_tree(), "VAR=1 proton-wrap.sh %command%")
        self.assertEqual(report["kind"], "builtin")
        self.assertEqual(report["detected"], ["shipped bHaptics SDK"])
        self.assertFalse(done["ok"])
        self.assertTrue(any("Native/Mod" in m for m in done["manual"]))

    def test_game_mod_is_still_detected(self):
        report, _ = self.diagnose(game_tree(mod=True), "VAR=1 proton-wrap.sh %command%")
        self.assertEqual(report["kind"], "mod")
        self.assertIn("SomeGame_bhaptics.dll", report["detected"])

    def test_missing_launch_wrapper_is_flagged(self):
        report, done = self.diagnose(game_tree(mod=True), "VAR=1 %command%")
        self.assertFalse(report["launch_set"])
        self.assertTrue(any("proton-wrap" in m for m in done["manual"]))

    def test_present_launch_wrapper_is_not_flagged(self):
        report, done = self.diagnose(
            game_tree(mod=True), "VAR=1 /opt/proton-wrap.sh %command%")
        self.assertTrue(report["launch_set"])
        self.assertFalse(any("proton-wrap" in m for m in done["manual"]))

    def test_unreadable_launch_options_are_not_flagged(self):
        # no userdata at all: we can't tell, so don't nag
        report, done = self.diagnose(game_tree(mod=True), None)
        self.assertTrue(report["launch_set"])
        self.assertFalse(any("proton-wrap" in m for m in done["manual"]))


if __name__ == "__main__":
    unittest.main()
