"""Remote Play toggle: the persisted LAN-bind setting and the host forwarder."""
import asyncio
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
        self.assertEqual(json.loads(pd.NETWORK_FILE.read_text())["bind"], "0.0.0.0")

    def test_set_remote_play_preserves_other_keys(self):
        pd.NETWORK_FILE.write_text('{"remote": "10.0.0.7"}')
        pd.set_remote_play(True)
        cfg = json.loads(pd.NETWORK_FILE.read_text())
        self.assertEqual(cfg["bind"], "0.0.0.0")
        self.assertEqual(cfg["remote"], "10.0.0.7")

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

    def test_non_dict_file_is_tolerated(self):
        pd.NETWORK_FILE.write_text('["nope"]')
        self.assertFalse(pd.remote_play_enabled())
        self.assertEqual(pd.remote_host_from_config(), "")


class RelayHostTest(IsolatedTestCase):
    def test_default_inactive(self):
        st = pd.PlayerState(FakeVest())
        self.assertFalse(st.relay_host)
        self.assertEqual(st.host_remote, "")
        self.assertEqual(st.relay_error, "")
        self.assertEqual(st.proxied, set())

    def test_persist_roundtrip(self):
        self.assertFalse(pd.relay_host_from_config())
        pd.set_relay_host(True)
        self.assertTrue(pd.relay_host_from_config())
        pd.set_relay_host(False)
        self.assertFalse(pd.relay_host_from_config())

    def test_stop_clears_state_and_persists_off(self):
        pd.set_relay_host(True)
        st = pd.PlayerState(FakeVest())
        st.relay_host = True
        st.host_remote = "10.0.0.5"
        st.relay_error = "boom"
        st.stop_relay_host()
        self.assertFalse(st.relay_host)
        self.assertEqual(st.host_remote, "")
        self.assertEqual(st.relay_error, "")
        self.assertFalse(pd.relay_host_from_config())

    def test_shutdown_stop_keeps_host_mode_configured(self):
        pd.set_relay_host(True)
        st = pd.PlayerState(FakeVest())
        st.relay_host = True
        st.stop_relay_host(persist=False)      # what daemon shutdown does
        self.assertFalse(st.relay_host)
        self.assertTrue(pd.relay_host_from_config())   # restored next start

    def test_status_exposes_relay_keys(self):
        st = pd.PlayerState(FakeVest())
        msg = json.loads(st.status_message())
        self.assertIn("RelayHost", msg)
        self.assertIn("RelayHostRemote", msg)
        self.assertIn("RelayHostError", msg)
        self.assertIn("RelayHostDevices", msg)
        self.assertIn("RelayHostBattery", msg)

    def test_host_mode_releases_the_local_vest_link(self):
        vest = FakeVest()
        st = pd.PlayerState(vest)

        async def go():
            st.start_relay_host()           # needs a loop: it starts the finder
            self.assertTrue(vest.host_mode)
            st.stop_relay_host()
            self.assertFalse(vest.host_mode)
        asyncio.run(go())

    def test_remote_device_state_from_sdk1_status(self):
        st = pd.PlayerState(FakeVest())
        st._note_remote_status(json.dumps(
            {"ConnectedDeviceCount": 1, "ConnectedPositions": ["Vest"], "Battery": 88}))
        self.assertEqual(st.host_remote_devices, ["Vest"])
        self.assertEqual(st.host_remote_battery, 88)
        st._note_remote_status(json.dumps(
            {"ConnectedDeviceCount": 0, "ConnectedPositions": []}))
        self.assertEqual(st.host_remote_devices, [])
        self.assertIsNone(st.host_remote_battery)

    def test_remote_device_state_from_sdk2_serverdevices(self):
        st = pd.PlayerState(FakeVest())
        st._note_remote_status(json.dumps({
            "Type": "ServerDevices",
            "Message": json.dumps([{"position": 0, "deviceName": "TactSuitPro",
                                    "connected": True, "battery": 55}])}))
        self.assertEqual(st.host_remote_devices, ["Vest"])
        self.assertEqual(st.host_remote_battery, 55)
        st._note_remote_status(json.dumps({
            "Type": "ServerDevices",
            "Message": json.dumps([{"position": 0, "connected": False}])}))
        self.assertEqual(st.host_remote_devices, [])

    def test_counts_plays_in_proxied_traffic(self):
        st = pd.PlayerState(FakeVest())
        stats = {"msgs": 0, "plays": 0, "types": {}}
        for payload in ({"Type": "SdkRequestAuth", "Message": "{}"},
                        {"Type": "SdkPlayDotMode", "Message": "{}"},
                        {"Type": "SdkPlay", "Message": "{}"},
                        {"Submit": [{"Type": "frame", "Key": "k"}]}):
            st._count_game_message(json.dumps(payload), stats)
        st._count_game_message(b"\x00binary frame", stats)
        self.assertEqual(stats["msgs"], 5)
        self.assertEqual(stats["plays"], 3)      # two SdkPlay* plus an SDK1 frame
        self.assertEqual(stats["types"]["SdkRequestAuth"], 1)
        self.assertEqual(stats["types"]["frame"], 1)

    def test_proxy_summary_names_the_message_types(self):
        line = pd.PlayerState._proxy_summary(
            "wss", 74.4, {"msgs": 132, "plays": 0,
                          "types": {"SdkPing": 12, "SdkRequestAuth": 1}})
        self.assertIn("WSS proxy closed after 74s", line)
        self.assertIn("132 messages, 0 haptics", line)
        self.assertIn("SdkPing x12", line)

    def test_proxy_summary_survives_no_stats(self):
        self.assertIn("0 messages, 0 haptics",
                      pd.PlayerState._proxy_summary("ws", 3.0, None))

    def test_remote_status_ignores_unrelated_or_binary_messages(self):
        st = pd.PlayerState(FakeVest())
        st._note_remote_status("not json")
        st._note_remote_status(b"\x00\x01\x02")          # binary frame
        st._note_remote_status(json.dumps({"Type": "ServerReady", "Message": ""}))
        st._note_remote_status(json.dumps({"big": "x" * 100}))  # no status keys
        self.assertEqual(st.host_remote_devices, [])
        self.assertIsNone(st.host_remote_battery)

    def test_remote_host_from_config(self):
        pd.NETWORK_FILE.write_text('{"remote": "10.0.0.7"}')
        self.assertEqual(pd.remote_host_from_config(), "10.0.0.7")

    def test_remote_host_missing(self):
        self.assertEqual(pd.remote_host_from_config(), "")

    def test_is_local_ip(self):
        self.assertTrue(pd._is_local_ip("127.0.0.1"))
        self.assertTrue(pd._is_local_ip("localhost"))
        self.assertFalse(pd._is_local_ip("203.0.113.9"))
