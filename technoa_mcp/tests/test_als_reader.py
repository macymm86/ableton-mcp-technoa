"""Pfadsuche fuer read_saved_set ohne Live: Rueckfall ueber Lives Log.txt."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import als_reader  # noqa: E402

# Aufbau wie in Lives Log vom 11.10.2026: erst das Set, dann beim Anlegen
# einer MIDI-Spur die Vorlage aus der Core Library (mit gemischten Trennern).
LOG = """\
2026-10-11T20:04:55.457651: info: Loading document "C:\\ProgramData\\Ableton\\Live 12 Standard\\Resources\\Builtin\\Templates\\DefaultLiveSet.als"
2026-10-11T20:05:26.276348: info: Loading document "D:\\Sets\\Demo_Set.als"
2026-10-11T20:31:23.862236: info: Loading document "C:\\ProgramData\\Ableton\\Live 12 Standard\\Resources\\Core Library\\Defaults\\Creating Tracks/MIDI Track\\Default MIDI Track.als"
"""


class LogFallbackTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="technoa_log_")
        prefs = os.path.join(self.tmp, "Ableton", "Live 12.4.6", "Preferences")
        os.makedirs(prefs)
        with open(os.path.join(prefs, "Log.txt"), "w", encoding="utf-8") as fh:
            fh.write(LOG)
        self.old = os.environ.get("APPDATA")
        os.environ["APPDATA"] = self.tmp

    def tearDown(self):
        if self.old is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_skips_track_templates(self):
        path, note = als_reader.find_current_set_from_log()
        self.assertEqual(path, "D:\\Sets\\Demo_Set.als")
        self.assertIn("Rueckfall", note)

    def test_template_detection(self):
        self.assertTrue(als_reader.is_live_template(
            "C:\\ProgramData\\Ableton\\Live 12 Standard\\Resources\\Core Library\\Defaults\\Creating Tracks/"
            "Audio Track\\Default Audio Track.als"))
        self.assertTrue(als_reader.is_live_template("C:/x/Resources/Builtin/Templates/DefaultLiveSet.als"))
        self.assertFalse(als_reader.is_live_template("D:\\Musik\\Defaults_Test\\Set.als"))

    def test_missing_file_names_source(self):
        text = als_reader.describe(path="D:\\gibtsnicht.als", source_note="laut Live (song.file_path)")
        self.assertIn("Datei nicht gefunden: D:\\gibtsnicht.als (Quelle des Pfads: laut Live", text)


if __name__ == "__main__":
    unittest.main()
