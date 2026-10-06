"""Shared test helper: isolate the daemon's config/state paths per test."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import player_daemon as pd  # noqa: E402


class IsolatedTestCase(unittest.TestCase):
    """Repoint player_daemon's XDG-derived globals at a throwaway directory.

    The daemon reads these module globals at call time, so patching them keeps
    real user settings untouched and makes each test hermetic.
    """

    NAMES = ("CONFIG_DIR", "STATE_DIR", "MAPPING_FILE", "PATTERNS_DIR",
             "AUDIO_CONF", "PRESETS_FILE", "LEARNED_FILE", "EFFECTS_FILE",
             "MIGRATED_MARK", "SDK2_CACHE", "FEEL_FILE", "NETWORK_FILE")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bhaptics-test-"))
        self._orig = {n: getattr(pd, n) for n in self.NAMES}
        pd.CONFIG_DIR = self.tmp
        pd.STATE_DIR = self.tmp
        pd.MAPPING_FILE = self.tmp / "mapping.json"
        pd.PATTERNS_DIR = self.tmp / "patterns"
        pd.AUDIO_CONF = self.tmp / "audio_settings.json"
        pd.PRESETS_FILE = self.tmp / "audio_presets.json"
        pd.LEARNED_FILE = self.tmp / "audio_learned.json"
        pd.EFFECTS_FILE = self.tmp / "effects.json"
        pd.MIGRATED_MARK = self.tmp / ".games-only-default"
        pd.SDK2_CACHE = self.tmp / "sdk2_cache"
        pd.FEEL_FILE = self.tmp / "feel.json"
        pd.NETWORK_FILE = self.tmp / "network.json"

    def tearDown(self):
        for name, value in self._orig.items():
            setattr(pd, name, value)


class FakeVest:
    """Minimal stand-in so PlayerState can be built without hardware."""

    def __init__(self):
        self.connected = False
        self.battery = None
        self.idle = False

    def mark_active(self):
        pass

    async def send(self, motors40):
        pass
