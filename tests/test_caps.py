"""Bounds on dicts that external input (games, imports, apps) can grow."""
import unittest

import player_daemon as pd
from tests.base import FakeVest, IsolatedTestCase


class CapsTest(IsolatedTestCase):
    def test_registered_cap_drops_oldest(self):
        st = pd.PlayerState(FakeVest())
        for i in range(pd.MAX_PATTERNS + 1):
            st.add_pattern(f"k{i}", {"x": 1}, save=False)
        self.assertEqual(len(st.registered), pd.MAX_PATTERNS)
        self.assertNotIn("k0", st.registered)
        self.assertIn(f"k{pd.MAX_PATTERNS}", st.registered)

    def test_sdk2_events_cap(self):
        st = pd.PlayerState(FakeVest())
        st._learn_sdk2({f"e{i}": {} for i in range(pd.MAX_PATTERNS + 10)}, "")
        self.assertEqual(len(st.sdk2_events), pd.MAX_PATTERNS)
        self.assertNotIn("e0", st.sdk2_events)

    def test_learned_apps_cap(self):
        st = pd.PlayerState(FakeVest())
        for i in range(pd.MAX_LEARNED_APPS + 1):
            st.audio.remember_app(f"game{i}")
        self.assertEqual(len(st.audio.learned), pd.MAX_LEARNED_APPS)
        self.assertNotIn("game0", st.audio.learned)
        self.assertIn(f"game{pd.MAX_LEARNED_APPS}", st.audio.learned)


if __name__ == "__main__":
    unittest.main()
