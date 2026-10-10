"""Protocol / compile-path tests: frames, raw submits, .tact projects, mapping."""
import asyncio
import unittest

import player_daemon as pd
from tests.base import FakeVest, IsolatedTestCase


class HelpersTest(unittest.TestCase):
    def test_grid_to_dot_corners(self):
        self.assertEqual(pd.grid_to_dot(0.0, 0.0), 0)
        self.assertEqual(pd.grid_to_dot(0.99, 0.99), 19)
        self.assertEqual(pd.grid_to_dot(0.5, 0.5), 2 * 4 + 2)

    def test_norm_intensity(self):
        self.assertEqual(pd.norm_intensity(50), 0.5)
        self.assertEqual(pd.norm_intensity(0.5), 0.5)
        self.assertEqual(pd.norm_intensity(1), 1.0)

    def test_pack_nibbles(self):
        motors = [0] * 40
        motors[0], motors[1] = 15, 3
        data = pd.pack(motors)
        self.assertEqual(len(data), 20)
        self.assertEqual(data[0], (15 << 4) | 3)
        self.assertTrue(all(b == 0 for b in data[1:]))


class FrameSubmitTest(unittest.TestCase):
    def test_front_only(self):
        tl = pd.compile_frame_submit({
            "Position": "VestFront", "DurationMillis": 100,
            "DotPoints": [{"Index": 0, "Intensity": 100}]})
        kind, front, back = tl[0]
        self.assertEqual(kind, "mapped")
        self.assertEqual(front[0], 1.0)
        self.assertEqual(max(back), 0.0)

    def test_vest_position_addresses_back_rows(self):
        tl = pd.compile_frame_submit({
            "Position": "Vest", "DurationMillis": 40,
            "DotPoints": [{"Index": 25, "Intensity": 100}]})
        _, _front, back = tl[0]
        self.assertEqual(back[5], 1.0)

    def test_raw_submit(self):
        tl = pd.compile_raw_submit({"Motors": [100] * 40, "DurationMillis": 40})
        self.assertEqual(tl[0][0], "raw")
        self.assertEqual(len(tl[0][1]), 40)


class ProjectTest(unittest.TestCase):
    PROJECT = {"Tracks": [{"Effects": [{
        "StartTime": 0, "EndTime": 100,
        "Modes": {"VestFront": {"DotMode": {"Feedback": [{
            "StartTime": 0, "EndTime": 100,
            "PointList": [{"Index": 3, "Intensity": 1.0}]}]}}}}]}]}

    def test_compile_project_hits_dot(self):
        tl = pd.compile_project(self.PROJECT)
        self.assertTrue(tl)
        self.assertEqual(tl[0][1][3], 1.0)

    def test_compile_project_scales_intensity(self):
        tl = pd.compile_project(self.PROJECT, intensity_ratio=0.5)
        self.assertAlmostEqual(tl[0][1][3], 0.5)

    def test_empty_project(self):
        self.assertEqual(pd.compile_project({"Tracks": []}), [])


class MappingTest(IsolatedTestCase):
    def test_defaults_are_valid(self):
        m = pd.VestMapping()
        self.assertEqual(len(m.front), 20)
        self.assertEqual(len(m.back), 20)

    def test_rejects_out_of_range(self):
        m = pd.VestMapping()
        with self.assertRaises(ValueError):
            m.set([40] + [0] * 19, [0] * 20, save=False)

    def test_rejects_wrong_length(self):
        m = pd.VestMapping()
        with self.assertRaises(ValueError):
            m.set([0] * 19, [0] * 20, save=False)


class ImportTactTest(IsolatedTestCase):
    """A single .tact registers under the name the caller gave, not the
    author's internal project name — HL2 VR ships ShootAR2_L.tact whose
    project is called Recoil_LV3_Mirror, and mods ask for it by file name."""

    def imported(self, name, project):
        st = pd.PlayerState(FakeVest())
        asyncio.run(st.handle({"ImportTact": {"name": name, "project": project}}))
        return st

    def test_tact_container_is_named_by_the_request(self):
        project = {"name": "Recoil_LV3_Mirror", "tracks": [{"effects": []}]}
        st = self.imported("ShootAR2_L", {"durationMillis": 100, "size": 100,
                                          "project": project})
        self.assertIn("ShootAR2_L", st.patterns)
        self.assertNotIn("Recoil_LV3_Mirror", st.patterns)

    def test_bare_project_is_named_by_the_request(self):
        st = self.imported("HitByBullet", {"name": "Impact1_weak", "tracks": []})
        self.assertIn("HitByBullet", st.patterns)
        self.assertNotIn("Impact1_weak", st.patterns)

    def test_manifest_still_uses_its_event_names(self):
        manifest = [{"key": "death", "tracks": []}, {"key": "hit", "tracks": []}]
        st = self.imported("mod-manifest", manifest)
        self.assertIn("death", st.patterns)
        self.assertIn("hit", st.patterns)
        self.assertNotIn("mod-manifest", st.patterns)


if __name__ == "__main__":
    unittest.main()
