"""Remote Play toggle: the persisted LAN-bind setting behind the UI switch."""
from unittest import mock

import player_daemon as pd
from tests.base import IsolatedTestCase


class RemotePlayTest(IsolatedTestCase):
    def test_default_is_loopback(self):
        self.assertFalse(pd.remote_play_enabled())

    def test_enable_persists_lan_bind(self):
        pd.set_remote_play(True)
        self.assertTrue(pd.remote_play_enabled())
        self.assertEqual(pd.NETWORK_FILE.read_text(), '{"bind": "0.0.0.0"}')

    def test_disable_returns_to_loopback(self):
        pd.set_remote_play(True)
        pd.set_remote_play(False)
        self.assertFalse(pd.remote_play_enabled())

    def test_env_overrides_file(self):
        pd.set_remote_play(True)
        with mock.patch.dict("os.environ", {"BHAPTICS_BIND": "10.0.0.9"}):
            self.assertTrue(pd.remote_play_enabled())

    def test_malformed_file_falls_back_to_loopback(self):
        pd.NETWORK_FILE.write_text("not json")
        self.assertFalse(pd.remote_play_enabled())
