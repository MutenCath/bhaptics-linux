"""SDK2: audio-clip events, per-game event lookup, raw bundle cache."""
import asyncio
import base64
import json
import os
import unittest

import player_daemon as pd
from tests.base import FakeVest, IsolatedTestCase


def frame(values, pad=255):
    """One 20-byte clip frame; byte 19 is padding unless given."""
    vals = list(values) + [0] * (19 - len(values))
    vals = vals[:19] + [pad]
    return base64.b64encode(bytes(vals)).decode()


def mapping(key, front, back=(), intensity=100):
    return {"key": key, "intensity": intensity, "tactFilePatterns": [],
            "audioFilePatterns": [{"position": "Vest", "clip": {
                "name": key, "duration": 4,
                "patterns": {"VestFront": list(front), "VestBack": list(back)}}}]}


class ClipTest(unittest.TestCase):
    def test_extract_and_compile(self):
        bundle = {"message": {"hapticMappings": [
            mapping("hit", [frame([100, 50]), frame([0, 0, 25])])]}}
        events = pd.extract_events(bundle)
        self.assertIn("hit", events)
        tl = pd.compile_event(events["hit"])
        self.assertEqual(len(tl), 2 * pd.CLIP_FRAME_MS // pd.TICK_MS)
        kind, front, back = tl[0]
        self.assertEqual(kind, "mapped")
        self.assertEqual(front[:2], [1.0, 0.5])
        self.assertEqual(front[19], 0.0)          # 255 padding reads as off
        self.assertEqual(back, [0.0] * 20)
        self.assertEqual(tl[-1][1][2], 0.25)

    def test_padding_byte_can_be_a_real_motor(self):
        tl = pd.compile_event(pd.clip_project(mapping("w", [frame([], pad=40)])))
        self.assertEqual(tl[0][1][19], 0.4)

    def test_intensity_and_duration_ratio(self):
        proj = pd.clip_project(mapping("x", [frame([80])] * 5, intensity=50))
        tl = pd.compile_event(proj, intensity_ratio=0.5, duration_ratio=2.0)
        self.assertEqual(len(tl), 10)
        self.assertAlmostEqual(tl[0][1][0], 0.2)
        self.assertEqual(pd.event_duration_ms(proj), 5 * pd.CLIP_FRAME_MS)

    def test_layers_play_together(self):
        m = mapping("death", [frame([50])] * 5)
        m["audioFilePatterns"].append({"clip": {"patterns": {
            "VestFront": [frame([100, 0, 30])] * 2}}})
        tl = pd.compile_event(pd.extract_events([m])["death"])
        self.assertEqual(len(tl), 5)                      # longest layer
        self.assertEqual(tl[0][1][:3], [1.0, 0.0, 0.3])   # max per motor
        self.assertEqual(tl[4][1][:3], [0.5, 0.0, 0.0])   # short layer done

    def test_tact_patterns_win_over_clip(self):
        m = mapping("both", [frame([100])])
        m["tactFilePatterns"] = [{"tactFile": {"project": {"tracks": []}}}]
        self.assertNotIn("_clips", pd.extract_events([m])["both"])


class CacheTest(IsolatedTestCase):
    def test_one_file_per_source_and_dedupe(self):
        pd.SDK2_CACHE.mkdir(parents=True)
        body = json.dumps({"hapticMappings": [mapping("e", [frame([10])])]})
        dumps = {"20260101-000000": ("{}", 900),     # older, different version
                 "20260101-000100": (body, 1000),
                 "20260101-000200": (body, 1100)}    # newest
        for stamp, (text, mtime) in dumps.items():
            f = pd.SDK2_CACHE / f"auth-abc123-{stamp}.json"
            f.write_text(text)
            os.utime(f, (mtime, mtime))
        pd.dedupe_sdk2_cache()
        names = sorted(p.name for p in pd.SDK2_CACHE.iterdir())
        # newest kept under the stable name, its exact copy dropped, the
        # differing older dump left alone
        self.assertEqual(names, ["auth-abc123-20260101-000000.json", "auth-abc123.json"])
        self.assertEqual((pd.SDK2_CACHE / "auth-abc123.json").read_text(), body)
        pd.dedupe_sdk2_cache()  # idempotent
        self.assertEqual(len(list(pd.SDK2_CACHE.iterdir())), 2)
        self.assertEqual(pd.sdk2_cache_workspace("auth-abc123.json"), "abc123")
        self.assertEqual(
            pd.sdk2_cache_workspace("cloud-abc123-20260101-000000.json"), "abc123")

    def test_events_are_per_game(self):
        st = pd.PlayerState(FakeVest())
        a = pd.extract_events([mapping("death", [frame([100])])])
        b = pd.extract_events([mapping("death", [frame([0, 0, 0, 100])])])
        st._learn_sdk2(a, "gameA")
        st._learn_sdk2(b, "gameB")
        self.assertEqual(pd.compile_event(st.sdk2_event("death", "gameA"))[0][1][0], 1.0)
        self.assertEqual(pd.compile_event(st.sdk2_event("death", "gameB"))[0][1][3], 1.0)
        self.assertIsNotNone(st.sdk2_event("death", "unknown"))  # any game's

    def test_ingest_off_loop(self):
        st = pd.PlayerState(FakeVest())
        bundle = {"hapticMappings": [mapping("boom", [frame([60])])]}
        asyncio.run(st.ingest_sdk2_defs(bundle, "auth-ws1", "ws1"))
        self.assertIn("boom", st.sdk2_ws_events["ws1"])
        self.assertTrue((pd.SDK2_CACHE / "auth-ws1.json").exists())


class IdleWakeTest(IsolatedTestCase):
    def test_audio_keeps_running_while_vest_sleeps(self):
        vest = pd.VestLink()
        st = pd.PlayerState(vest)
        self.assertFalse(st.audio.vest_ready())   # searching: nothing to drive
        vest.idle = True                          # slept for battery
        self.assertTrue(st.audio.vest_ready())    # audio must be able to wake it
        vest.mark_active()
        self.assertFalse(vest.idle)


if __name__ == "__main__":
    unittest.main()


class SleepWatchTest(unittest.TestCase):
    """Asleep, the vest reconnects only after it went away and came back."""

    def run_watch(self, sightings):
        vest = pd.VestLink()
        vest.idle = True
        seq = iter(sightings)

        async def fake_find(timeout):
            v = next(seq)
            if isinstance(v, Exception):
                raise v
            return object() if v else None

        async def scenario():
            orig_find, orig_secs = vest._find, pd.IDLE_SCAN_SECS
            vest._find = fake_find
            pd.IDLE_SCAN_SECS = 0
            try:
                for _ in sightings:
                    await vest._watch_while_asleep()
                    if not vest.idle:
                        break
            finally:
                vest._find, pd.IDLE_SCAN_SECS = orig_find, orig_secs
        asyncio.run(scenario())
        return vest

    def test_still_on_after_sleep_keeps_sleeping(self):
        self.assertTrue(self.run_watch([True] * 6).idle)

    def test_off_then_on_wakes(self):
        self.assertFalse(self.run_watch([True, False, False, True]).idle)

    def test_single_missed_scan_is_not_a_power_cycle(self):
        self.assertTrue(self.run_watch([False, True, False, True, True]).idle)

    def test_scan_errors_are_ignored(self):
        busy = RuntimeError("org.bluez.Error.InProgress")
        self.assertFalse(self.run_watch([False, busy, False, True]).idle)
