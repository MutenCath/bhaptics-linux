"""Pause switch and the game-facing status payload.

Half-Life 2: VR Mod's bundled SDK1 client dies while this daemon is driving it.
Two things came out of that: the daemon has to be able to step aside without
stopping the unit (so the UI can switch it back on), and a game socket must not
be fed the control-UI status blob.
"""
import asyncio
import json
import unittest

import player_daemon as pd
from tests.base import FakeVest, IsolatedTestCase


class DisabledFlagTest(IsolatedTestCase):
    def test_defaults_to_enabled(self):
        self.assertFalse(pd.daemon_disabled())

    def test_round_trip(self):
        pd.set_daemon_disabled(True)
        self.assertTrue(pd.daemon_disabled())
        pd.set_daemon_disabled(False)
        self.assertFalse(pd.daemon_disabled())

    def test_status_reports_the_switch(self):
        pd.set_daemon_disabled(True)
        self.assertIs(json.loads(pd.PlayerState(FakeVest()).status_message())["DaemonEnabled"],
                      False)
        pd.set_daemon_disabled(False)
        self.assertIs(json.loads(pd.PlayerState(FakeVest()).status_message())["DaemonEnabled"],
                      True)


class GameStatusPayloadTest(IsolatedTestCase):
    def test_game_gets_protocol_fields_only(self):
        st = pd.PlayerState(FakeVest())
        keys = set(json.loads(st.status_message(for_game=True)))
        self.assertTrue(keys)
        self.assertLessEqual(keys, set(pd.GAME_STATUS_KEYS))

    def test_game_reply_keeps_device_state(self):
        st = pd.PlayerState(FakeVest())
        msg = json.loads(st.status_message(for_game=True))
        self.assertIn("ConnectedDeviceCount", msg)
        self.assertIn("ConnectedPositions", msg)

    def test_ui_still_gets_the_full_message(self):
        st = pd.PlayerState(FakeVest())
        msg = json.loads(st.status_message())
        # control-UI surface that must never reach a game socket
        for key in ("Mapping", "Feel", "RelayHost", "AudioMode", "GameClients",
                    "DaemonEnabled"):
            self.assertIn(key, msg)
            self.assertNotIn(key, pd.GAME_STATUS_KEYS)


class SetDaemonEnabledTest(IsolatedTestCase):
    def test_disable_then_enable(self):
        async def go():
            st = pd.PlayerState(FakeVest())
            await st.set_daemon_enabled(False)
            self.assertTrue(pd.daemon_disabled())
            await st.set_daemon_enabled(True)
            self.assertFalse(pd.daemon_disabled())

        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
