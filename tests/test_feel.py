"""Feel output stage: strength, minimum level, dithering, punch, paths."""
import unittest

import player_daemon as pd
from tests.base import IsolatedTestCase


class FeelTest(IsolatedTestCase):
    def feel(self, **kw):
        f = pd.Feel()
        f.update({**pd.Feel.DEFAULTS, **kw}, save=False)
        return f

    def test_off_is_off_and_full_is_full(self):
        f = self.feel()
        out = f.shape([0.0] * 20 + [1.0] * 20)
        self.assertEqual(out, [0] * 20 + [15] * 20)

    def test_dither_averages_between_steps(self):
        f = self.feel(smooth=True)
        ticks = [f.shape([0.25] + [0.0] * 39)[0]
                 for _ in range(200)]
        # 0.25 * 15 = 3.75 steps: dithering alternates 3 and 4, mean ~3.75
        self.assertEqual(set(ticks), {3, 4})
        self.assertAlmostEqual(sum(ticks) / len(ticks), 3.75, delta=0.05)

    def test_no_dither_rounds(self):
        f = self.feel(smooth=False)
        self.assertEqual({f.shape([0.25] + [0.0] * 39)[0] for _ in range(10)}, {4})

    def test_floor_lifts_weak_levels(self):
        f = self.feel(floor=4, smooth=False)
        self.assertEqual(f.shape([0.01] + [0.0] * 39)[0], 4)
        self.assertEqual(f.shape([1.0] + [0.0] * 39)[0], 15)

    def test_strength_scales_and_clips(self):
        f = self.feel(strength=0.5, smooth=False)
        self.assertEqual(f.shape([1.0] + [0.0] * 39)[0], 8)
        f = self.feel(strength=2.0, smooth=False)
        self.assertEqual(f.shape([0.8] + [0.0] * 39)[0], 15)

    def test_punch_kicks_first_tick_only(self):
        f = self.feel(punch=True, smooth=False)
        lvl = [0.2] + [0.0] * 39
        self.assertEqual(f.shape(lvl)[0], 15)
        self.assertEqual(f.shape(lvl)[0], 3)
        f.shape([0.0] * 40)                       # rest again
        self.assertEqual(f.shape(lvl)[0], 15)

    def test_persisted(self):
        pd.Feel().update({"strength": 1.5, "floor": 2})
        f = pd.Feel()
        self.assertEqual((f.strength, f.floor), (1.5, 2))


class PathTest(unittest.TestCase):
    def test_spread_is_symmetric_between_motors(self):
        f = [0.0] * 20
        pd.spread_point(f, 0.5, 0.5, 1.0)
        self.assertEqual(f[9], f[10])
        self.assertEqual(f[9], 1.0)

    def test_path_interpolates(self):
        pts = [{"X": 0, "Y": 0, "Intensity": 0, "Time": 0},
               {"X": 1, "Y": 0, "Intensity": 100, "Time": 400}]
        x, y, inten = pd.path_at(pts, 100)
        self.assertAlmostEqual(x, 0.25)
        self.assertAlmostEqual(inten, 0.25)   # 0..100 normalised first

    def test_project_path_glides(self):
        project = {"Tracks": [{"Effects": [{"StartTime": 0, "OffsetTime": 0, "Modes": {
            "VestFront": {"PathMode": {"Feedback": [{"PointList": [
                {"X": 0.0, "Y": 0.1, "Intensity": 1.0, "Time": 0},
                {"X": 1.0, "Y": 0.1, "Intensity": 1.0, "Time": 400}]}]}}}}]}]}
        tl = pd.compile_project(project)
        peaks = [max(range(4), key=lambda c: e[1][c]) for e in tl]
        self.assertEqual(peaks[0], 0)
        self.assertEqual(peaks[-1], 3)
        self.assertEqual(peaks, sorted(peaks))   # moves left to right


if __name__ == "__main__":
    unittest.main()
