"""Effect designer protocol: save, named play, inline preview play, clamps."""
import asyncio
import unittest

import player_daemon as pd
from tests.base import FakeVest, IsolatedTestCase


class EffectsTest(IsolatedTestCase):
    def test_save_and_play_by_name(self):
        st = pd.PlayerState(FakeVest())
        asyncio.run(st.handle({"SaveEffect": {"name": "hit", "frames": [
            {"ms": 100, "front": [1.0] * 20, "back": [0.0] * 20}]}}))
        self.assertIn("hit", st.effects)
        asyncio.run(st.handle({"PlayEffect": "hit"}))
        self.assertIn("fx:hit", st.active)
        self.assertTrue(pd.EFFECTS_FILE.exists())

    def test_inline_preview_never_touches_saved_effects(self):
        # the designer's Play used to save over a reserved "__preview" name,
        # colliding with a user effect of that name; now frames play directly
        st = pd.PlayerState(FakeVest())
        asyncio.run(st.handle({"PlayEffect": {"frames": [
            {"ms": 100, "front": [1.0] * 20, "back": []}]}}))
        self.assertIn("fx:preview", st.active)
        self.assertEqual(st.effects, {})
        self.assertFalse(pd.EFFECTS_FILE.exists())

    def test_play_unknown_is_a_noop(self):
        st = pd.PlayerState(FakeVest())
        asyncio.run(st.handle({"PlayEffect": "nope"}))
        self.assertEqual(st.active, {})

    def test_frames_are_clamped(self):
        st = pd.PlayerState(FakeVest())
        asyncio.run(st.handle({"SaveEffect": {"name": "x", "frames": [
            {"ms": 1, "front": [7.7] * 25, "back": [-1.0] * 20}]}}))
        f = st.effects["x"][0]
        self.assertEqual(f["ms"], pd.TICK_MS)
        self.assertEqual(f["front"], [1.0] * 20)
        self.assertEqual(f["back"], [0.0] * 20)

    def test_inline_preview_clamps_too(self):
        st = pd.PlayerState(FakeVest())
        asyncio.run(st.handle({"PlayEffect": {"frames": [
            {"ms": 999999, "front": [2.0] * 20, "back": []}]}}))
        fx = st.active["fx:preview"]
        # 10000 ms cap -> 10000/20 = 500 ticks
        self.assertEqual(len(fx.timeline), 500)


if __name__ == "__main__":
    unittest.main()
