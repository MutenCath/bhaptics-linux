"""Auto-follow: newest-game binding, learn-on-exit, learned reapply."""
import asyncio
import contextlib
import unittest

import player_daemon as pd
from tests.base import FakeVest, IsolatedTestCase

BIND = 0.2   # a few passes of the loop at the patched poll interval


class AutoProfileTest(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self._orig_list = pd.list_audio_apps
        self._orig_set_enabled = pd.AudioEngine.set_enabled
        self._orig_games = pd.installed_game_names
        self._orig_poll = pd.AUTOPROFILE_POLL
        pd.AUTOPROFILE_POLL = 0.02
        self.rows = []

        async def fake_set_enabled(engine, on, intent=False):
            if intent:
                engine.start_enabled = bool(on)
            engine.enabled = on

        pd.AudioEngine.set_enabled = fake_set_enabled
        pd.list_audio_apps = lambda: [dict(r) for r in self.rows]
        pd.installed_game_names = lambda: {"newgame", "steam game"}

    def tearDown(self):
        pd.list_audio_apps = self._orig_list
        pd.AudioEngine.set_enabled = self._orig_set_enabled
        pd.installed_game_names = self._orig_games
        pd.AUTOPROFILE_POLL = self._orig_poll
        super().tearDown()

    def test_bind_learn_reapply(self):
        async def scenario():
            st = pd.PlayerState(FakeVest())
            st.audio.notify = False
            task = asyncio.create_task(st.autoprofile_loop())
            try:
                self.rows[:] = [{"key": "newgame.exe", "name": "newgame.exe",
                                 "index": 9, "binary": "wine64-preloader"}]
                await asyncio.sleep(BIND)
                self.assertEqual(st.audio.source, "appname:newgame.exe")
                self.assertEqual(st.auto_profile_app, "newgame.exe")
                self.assertTrue(st.audio.enabled)

                st.audio.gain = 9.5          # user tunes during play
                self.rows.clear()            # game exits
                await asyncio.sleep(BIND)
                self.assertEqual(st.audio.learned["newgame.exe"]["gain"], 9.5)
                self.assertEqual(st.auto_profile_app, "")

                self.rows[:] = [{"key": "newgame.exe", "name": "newgame.exe",
                                 "index": 9, "binary": "wine64-preloader"}]
                await asyncio.sleep(BIND)    # plays again
                self.assertEqual(st.audio.gain, 9.5)  # learned tuning returns
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        asyncio.run(scenario())

    def test_switches_from_generic_to_steam_game(self):
        """A generic stream binds first, then yields to a Steam game."""
        async def scenario():
            st = pd.PlayerState(FakeVest())
            st.audio.notify = False
            task = asyncio.create_task(st.autoprofile_loop())
            try:
                self.rows[:] = [{"key": "some tool", "name": "some tool",
                                 "index": 9, "binary": ""}]
                await asyncio.sleep(BIND)
                self.assertEqual(st.audio.source, "appname:some tool")

                self.rows.append({"key": "steam game", "name": "Steam Game",
                                  "index": 3, "binary": "wine64-preloader"})
                await asyncio.sleep(BIND)
                self.assertEqual(st.audio.source, "appname:Steam Game")
                self.assertEqual(st.auto_profile_app, "Steam Game")
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        asyncio.run(scenario())


    GAME = {"key": "newgame.exe", "name": "newgame.exe", "index": 9,
            "binary": "wine64-preloader"}

    def run_session(self, body):
        async def scenario():
            st = pd.PlayerState(FakeVest())
            st.audio.notify = False
            st.audio.source = "auto"
            task = asyncio.create_task(st.autoprofile_loop())
            try:
                self.rows[:] = [dict(self.GAME)]
                await asyncio.sleep(BIND)
                self.assertEqual(st.audio.source, "appname:newgame.exe")
                await body(st)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        asyncio.run(scenario())

    def test_ui_tuning_keeps_session(self):
        """Slider moves (no source) must not unbind the playing game."""
        async def body(st):
            await st.handle({"AudioMode": {"gain": 7.0, "enabled": True}})
            await asyncio.sleep(BIND)
            self.assertEqual(st.audio.source, "appname:newgame.exe")
            self.assertEqual(st.auto_profile_app, "newgame.exe")
        self.run_session(body)

    def test_arm_during_session_keeps_binding(self):
        """vestctl game on / tray "arm" sends source=auto mid-game."""
        async def body(st):
            await st.handle({"AudioMode": {"enabled": True, "auto": True,
                                           "source": "auto"}})
            await asyncio.sleep(BIND)
            self.assertEqual(st.audio.source, "appname:newgame.exe")
            self.assertIsNotNone(st.auto_session)
        self.run_session(body)

    def test_persists_baseline_not_game(self):
        async def body(st):
            await st.handle({"AudioMode": {"gain": 8.0}})
            saved = st.persisted_settings()
            self.assertEqual(saved["source"], "auto")
            self.assertNotEqual(saved["gain"], 8.0)
        self.run_session(body)

    def test_off_means_off(self):
        async def body(st):
            await st.handle({"AudioMode": False})
            await asyncio.sleep(BIND)
            self.assertIsNone(st.auto_session)
            self.assertEqual(st.audio.source, "auto")   # pre-game source back
            self.assertFalse(st.audio.enabled)
            self.rows.append({"key": "other.exe", "name": "other.exe",
                              "index": 12, "binary": "wine64-preloader"})
            await asyncio.sleep(BIND)
            self.assertFalse(st.audio.enabled)          # no auto re-enable
            self.assertIsNone(st.auto_session)
        self.run_session(body)

    def test_save_preset_for_current_game_keeps_session(self):
        async def body(st):
            await st.handle({"SavePreset": "newgame.exe"})
            await asyncio.sleep(BIND)
            self.assertEqual(st.auto_profile_app, "newgame.exe")
            self.rows.clear()
            await asyncio.sleep(BIND)
            self.assertEqual(st.audio.source, "auto")    # restored on exit
        self.run_session(body)

    def test_untouched_game_not_remembered(self):
        async def body(st):
            self.rows.clear()
            await asyncio.sleep(BIND)
            self.assertNotIn("newgame.exe", st.audio.learned)
        self.run_session(body)

    def test_restore_keeps_global_toggles(self):
        async def body(st):
            await st.handle({"AudioMode": {"notify": True}})
            self.rows.clear()
            await asyncio.sleep(BIND)
            self.assertTrue(st.audio.notify)   # not reverted to pre-game False
        self.run_session(body)


if __name__ == "__main__":
    unittest.main()
