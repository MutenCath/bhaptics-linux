"""Remote Play toggle: the persisted LAN-bind setting and the host forwarder."""
import json
from unittest import mock

import player_daemon as pd
from tests.base import FakeVest, IsolatedTestCase


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


class RelayHostTest(IsolatedTestCase):
    def test_default_inactive(self):
        st = pd.PlayerState(FakeVest())
        self.assertFalse(st.relay_host)
        self.assertEqual(st.host_remote, "")
        self.assertEqual(st.relay_error, "")

    def test_stop_clears_state(self):
        st = pd.PlayerState(FakeVest())
        st.relay_host = True
        st.host_remote = "10.0.0.5"
        st.relay_error = "boom"
        st.stop_relay_host()
        self.assertFalse(st.relay_host)
        self.assertEqual(st.host_remote, "")
        self.assertEqual(st.relay_error, "")

    def test_status_exposes_relay_keys(self):
        st = pd.PlayerState(FakeVest())
        msg = json.loads(st.status_message())
        self.assertIn("RelayHost", msg)
        self.assertIn("RelayHostRemote", msg)
        self.assertIn("RelayHostError", msg)

    def test_remote_host_from_config(self):
        pd.NETWORK_FILE.write_text('{"remote": "10.0.0.7"}')
        self.assertEqual(pd.remote_host_from_config(), "10.0.0.7")

    def test_remote_host_missing(self):
        self.assertEqual(pd.remote_host_from_config(), "")
