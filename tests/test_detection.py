"""Detection and suppression: newest-stream preference, active-client gating."""
import asyncio
import unittest

import player_daemon as pd
from tests.base import FakeVest, IsolatedTestCase


class ListAudioAppsTest(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self._orig_sinks = pd.sink_inputs

    def tearDown(self):
        pd.sink_inputs = self._orig_sinks
        super().tearDown()

    def test_newest_stream_first_and_dedup(self):
        pd.sink_inputs = lambda: [
            {"index": 3, "properties": {"application.name": "Game"}},
            {"index": 7, "properties": {"application.name": "Launcher"}},
            {"index": 2, "properties": {"application.name": "Game"}},
        ]
        apps = pd.list_audio_apps()
        self.assertEqual([a["key"] for a in apps], ["launcher", "game"])
        game = next(a for a in apps if a["key"] == "game")
        self.assertEqual(game["index"], 3)  # dedup keeps the newest stream

    def test_falls_back_to_process_binary(self):
        pd.sink_inputs = lambda: [
            {"index": 1, "properties": {"application.process.binary": "foo.exe"}}]
        self.assertEqual(pd.list_audio_apps()[0]["key"], "foo.exe")

    def test_drops_app_when_process_binary_is_nongame(self):
        # Discord reports the friendly name "WEBRTC VoiceEngine" but runs as
        # binary "Discord" — the denylist must be checked against both
        pd.sink_inputs = lambda: [
            {"index": 5, "properties": {"application.name": "WEBRTC VoiceEngine",
                                        "application.process.binary": "Discord"}},
            {"index": 6, "properties": {"application.name": "ELDEN RING NIGHTREIGN",
                                        "application.process.binary": "wine64-preloader"}},
        ]
        self.assertEqual([a["key"] for a in pd.list_audio_apps()],
                         ["elden ring nightreign"])


class GameStrengthTest(unittest.TestCase):
    STEAM = {"elden ring nightreign", "quantum void"}

    def test_steam_name_is_strongest(self):
        self.assertEqual(pd.game_strength(
            {"key": "elden ring nightreign", "binary": "wine64-preloader"},
            self.STEAM), 2)

    def test_exe_stripped_to_match_steam_name(self):
        self.assertEqual(pd.game_strength(
            {"key": "quantum void.exe", "binary": "wine64-preloader"},
            self.STEAM), 2)

    def test_proton_exe_is_medium(self):
        self.assertEqual(pd.game_strength(
            {"key": "some unknown.exe", "binary": "wine64-preloader"},
            self.STEAM), 1)

    def test_unknown_native_is_last_resort(self):
        self.assertEqual(pd.game_strength(
            {"key": "native game", "binary": "native game"}, self.STEAM), 0)


class InstalledGamesTest(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self._orig_roots = pd.STEAM_ROOTS
        self._orig_cache = pd._game_names_cache
        sa = self.tmp / "steam" / "steamapps"
        sa.mkdir(parents=True, exist_ok=True)
        (sa / "appmanifest_1.acf").write_text(
            '"AppState"\n{\n\t"name"\t\t"ELDEN RING NIGHTREIGN"\n'
            '\t"installdir"\t\t"ELDEN RING NIGHTREIGN"\n}\n')
        pd.STEAM_ROOTS = (self.tmp / "steam",)
        pd._game_names_cache = (0.0, frozenset())
        self.addCleanup(self._restore)

    def _restore(self):
        pd.STEAM_ROOTS = self._orig_roots
        pd._game_names_cache = self._orig_cache

    def test_parses_manifest_names(self):
        self.assertIn("elden ring nightreign", pd.installed_game_names())


class SuppressionTest(IsolatedTestCase):
    def test_inert_client_does_not_suppress(self):
        st = pd.PlayerState(FakeVest())
        st.game_clients[object()] = "mod that never sends patterns"
        self.assertFalse(st.audio_suppressed)

    def test_active_game_suppresses(self):
        st = pd.PlayerState(FakeVest())
        st.touch_game()
        self.assertTrue(st.audio_suppressed)

    def test_ui_submit_does_not_suppress(self):
        st = pd.PlayerState(FakeVest())
        st.registered["k"] = {"Tracks": [{"Effects": [{
            "StartTime": 0, "EndTime": 50,
            "Modes": {"VestFront": {"DotMode": {"Feedback": [{
                "StartTime": 0, "EndTime": 50,
                "PointList": [{"Index": 0, "Intensity": 1.0}]}]}}}}]}]}
        asyncio.run(st.handle({"Submit": [{"Type": "key", "Key": "k"}]}, game=False))
        self.assertFalse(st.audio_suppressed)
        asyncio.run(st.handle({"Submit": [{"Type": "key", "Key": "k"}]}, game=True))
        self.assertTrue(st.audio_suppressed)
