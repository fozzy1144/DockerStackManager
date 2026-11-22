"""Tests for the remembered window state in :mod:`gui.settings`."""

import json
import tempfile
import unittest
from pathlib import Path

from gui import settings

SCREEN = (0, 0, 1920, 1080)


class TestUsableGeometry(unittest.TestCase):
    def test_a_window_on_screen_is_kept(self):
        self.assertEqual(settings.usable_geometry("1320x820+100+80", SCREEN), "1320x820+100+80")

    def test_off_screen_positions_are_rejected(self):
        # A monitor unplugged since the last run must not strand the window.
        self.assertIsNone(settings.usable_geometry("1320x820+4000+80", SCREEN))
        self.assertIsNone(settings.usable_geometry("1320x820+100+2000", SCREEN))

    def test_a_title_bar_above_the_desktop_is_rejected(self):
        self.assertIsNone(settings.usable_geometry("1320x820+100+-300", SCREEN))

    def test_negative_offsets_survive_as_plus_minus(self):
        # Tk reads "-8" as measured from the right edge; "+-8" is x = -8.
        self.assertEqual(settings.usable_geometry("1320x820+-8+-0", (-1920, 0, 3840, 1080)),
                         "1320x820+-8+0")

    def test_a_second_monitor_to_the_left_counts(self):
        desktop = (-1920, 0, 3840, 1080)
        self.assertEqual(settings.usable_geometry("1200x800+-1700+100", desktop),
                         "1200x800+-1700+100")

    def test_tiny_or_malformed_values_are_rejected(self):
        for value in ("200x100+0+0", "", "fullscreen", "1320x820"):
            with self.subTest(value):
                self.assertIsNone(settings.usable_geometry(value, SCREEN))


class TestLoadSave(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / settings.FILENAME

    def tearDown(self):
        self.dir.cleanup()

    def test_round_trip(self):
        data = {"geometry": "1320x820+10+10", "maximized": False, "last_host": "u@h:22"}
        settings.save(data, self.path)
        self.assertEqual(settings.load(self.path), data)

    def test_a_missing_file_is_empty(self):
        self.assertEqual(settings.load(self.path), {})

    def test_a_corrupt_file_is_empty(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(settings.load(self.path), {})

    def test_a_non_object_is_empty(self):
        self.path.write_text(json.dumps([1, 2]), encoding="utf-8")
        self.assertEqual(settings.load(self.path), {})

    def test_no_temp_file_is_left_behind(self):
        settings.save({"a": 1}, self.path)
        self.assertEqual([p.name for p in Path(self.dir.name).iterdir()], [settings.FILENAME])


if __name__ == "__main__":
    unittest.main()
