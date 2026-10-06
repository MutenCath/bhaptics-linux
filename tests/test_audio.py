"""AudioEngine: source defaults, one-time migration, per-app learning."""
import asyncio
import json

import player_daemon as pd
from tests.base import IsolatedTestCase


class AudioLearningTest(IsolatedTestCase):
    def _write(self, **kw):
        d = {"gain": 8.0, "source": "appname:battlemarked.exe",
             "auto": True, "enabled": True, "surround": True}
        d.update(kw)
        pd.AUDIO_CONF.write_text(json.dumps(d))

    def test_appname_source_migrates_to_games_only(self):
        self._write()
        a = pd.AudioEngine()
        self.assertEqual(a.source, "auto")
        self.assertEqual(a.learned["battlemarked.exe"]["gain"], 8.0)
        self.assertTrue(a.learned["battlemarked.exe"]["surround"])

    def test_snapshot_excludes_global_keys(self):
        self._write()
        a = pd.AudioEngine()
        snap = a.learned["battlemarked.exe"]
        for key in pd.LEARN_SKIP:
            self.assertNotIn(key, snap)

    def test_marker_prevents_remigrating_a_later_pin(self):
        self._write()
        pd.AudioEngine()          # first run writes the marker and switches
        self._write()             # user deliberately pins an app again
        a = pd.AudioEngine()
        self.assertEqual(a.source, "appname:battlemarked.exe")

    def test_fresh_install_defaults_to_auto(self):
        a = pd.AudioEngine()
        self.assertEqual(a.source, "auto")

    def test_auto_captures_nothing(self):
        a = pd.AudioEngine()
        a.source = "auto"
        self.assertIsNone(a._resolve_target())

    def test_remember_app_round_trips(self):
        a = pd.AudioEngine()
        a.gain = 9.5
        a.remember_app("SomeGame.EXE")
        self.assertEqual(a.learned["somegame.exe"]["gain"], 9.5)
        self.assertTrue(pd.LEARNED_FILE.exists())

    def test_notify_setting_persists(self):
        self._write(notify=False)
        self.assertFalse(pd.AudioEngine().notify)

    def test_shutdown_does_not_persist_disabled(self):
        # main() calls set_enabled(False) on exit; that must not look like the
        # user's preference, or the next start comes up muted
        self._write(enabled=True)
        a = pd.AudioEngine()
        asyncio.run(a.set_enabled(False))          # runtime stop, no intent
        self.assertTrue(a.start_enabled)
        self.assertTrue(a.settings()["enabled"])

    def test_user_toggle_persists_intent(self):
        a = pd.AudioEngine()
        asyncio.run(a.set_enabled(False, intent=True))
        self.assertFalse(a.start_enabled)
        self.assertFalse(a.settings()["enabled"])
