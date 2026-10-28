"""Teil 3 (technoa.8) ohne Live: Notenpruefung, aelteres Script, Nachlese-Texte.

Faelle, die die Kette ueber die Attrappe nicht nachspielt: ein Remote Script
technoa.7 in Live, das repeat und die Zusatzfelder still ignorieren wuerde.
"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))
import server  # noqa: E402
import views  # noqa: E402

STATE = {"signature_numerator": 4, "signature_denominator": 4}


class CheckNotesTest(unittest.TestCase):

    def test_defaults_and_types(self):
        clean = server._check_notes([{"pitch": 36.0, "start_time": 0, "duration": 0.25}])
        self.assertEqual(clean, [{"pitch": 36, "start_time": 0.0, "duration": 0.25, "velocity": 100.0,
                                  "mute": False}])
        self.assertIsInstance(clean[0]["pitch"], int)

    def test_problems_refuse_everything(self):
        with self.assertRaises(ValueError) as cm:
            server._check_notes([{"pitch": 36, "start_time": 0.0, "duration": 0.25},
                                 {"pitch": 60.5, "start_time": "eins", "duration": 0, "mute": "ja",
                                  "velocity_dev": 10, "probability": 2}])
        text = str(cm.exception)
        self.assertIn("Note 1: unbekanntes Feld 'velocity_dev' (gemeint: 'velocity_deviation'?)", text)
        self.assertIn("Note 1: pitch muss ganzzahlig sein, nicht 60.5", text)
        self.assertIn("Note 1: 'start_time' ist keine Zahl ('eins')", text)
        self.assertIn("Note 1: duration muss groesser als 0 sein, nicht 0", text)
        self.assertIn("Note 1: mute muss true oder false sein, nicht 'ja'", text)
        self.assertIn("Note 1: probability=2 liegt ausserhalb von 0 bis 1", text)
        self.assertNotIn("Note 0", text)
        self.assertIn("Pflicht: pitch, start_time, duration", text)

    def test_empty_and_unknown_without_alias(self):
        with self.assertRaises(ValueError):
            server._check_notes([])
        with self.assertRaises(ValueError) as cm:
            server._check_notes([{"pitch": 36, "start_time": 0.0, "duration": 0.25, "farbe": "rot"}])
        self.assertIn("unbekanntes Feld 'farbe'\n", str(cm.exception) + "\n")


class OlderScriptTest(unittest.TestCase):
    """technoa.7 in Live: repeat wird verweigert, bevor etwas gesendet wird."""

    def setUp(self):
        self.saved = (server._call, server.live.script_info)
        self.sent = []

        def fake_call(command, params=None):
            self.sent.append(command)
            if command == "get_session_info":
                return dict(STATE)
            raise AssertionError("unerwartet gesendet: %s" % command)
        server._call = fake_call
        server.live.script_info = {"variant": "technoa", "script_version": "1.7.1+technoa.7",
                                   "capabilities": ["duplicate_session_clip_to_arrangement"]}

    def tearDown(self):
        server._call, server.live.script_info = self.saved

    def test_repeat_refused(self):
        text = server.duplicate_to_arrangement(track=8, clip_index=0, bar=9, repeat=4)
        self.assertTrue(text.startswith("Fehler: Das Remote Script in Live (1.7.1+technoa.7) kann mehrere Kopien "
                                        "(repeat, every_bars) noch nicht"), text)
        self.assertIn("Nichts geaendert. Fassung 1.7.1+technoa.9 einspielen", text)
        self.assertNotIn("duplicate_session_clip_to_arrangement", self.sent)


class NotesAddedTextTest(unittest.TestCase):

    def result(self, **extra):
        r = {"track": 5, "track_name": "SYNTH PULSE", "source": "arrangement", "clip_index": 0,
             "clip_name": "PULSE", "notes_sent": 1, "notes_before": 2, "notes_after": 3, "method": "add_new_notes"}
        r.update(extra)
        return r

    def test_older_script_without_report(self):
        sent = [{"pitch": 48, "start_time": 0.5, "duration": 0.25, "velocity": 100.0, "mute": False,
                 "velocity_deviation": 20.0}]
        back = [{"pitch": 48, "start_time": 0.5, "duration": 0.25, "velocity": 100.0, "mute": False,
                 "velocity_deviation": 0.0}, {"pitch": 48, "start_time": 0.0, "duration": 0.5, "velocity": 100.0,
                                              "mute": False}, {"pitch": 55, "start_time": 1.0, "duration": 0.5,
                                                               "velocity": 80.0, "mute": False}]
        text = views.notes_added(self.result(), sent, back, STATE)
        self.assertIn("aelter als technoa.8", text)
        self.assertIn("C2 (48) bei 1.1.3: velocity_deviation gesendet 20, in Live 0", text)
        self.assertNotIn("erwartet waren", text)

    def test_missing_note_and_count(self):
        sent = [{"pitch": 60, "start_time": 2.0, "duration": 0.25, "velocity": 100.0, "mute": False}]
        text = views.notes_added(self.result(not_written=[], fields_written=list(views.NOTE_EXTRA_FIELDS)), sent,
                                 [], STATE)
        self.assertIn("ACHTUNG: 1 Note(n) danach nicht im Clip (gesucht nach Tonhoehe und Startzeit): C3 (60) bei "
                      "1.3.1", text)
        self.assertIn("Im Clip sind jetzt 0 Noten, erwartet waren 3 (vorher 2 + 1 gesendet)", text)

    def test_not_written_and_unreadable(self):
        sent = [{"pitch": 60, "start_time": 0.0, "duration": 0.25, "velocity": 100.0, "mute": False,
                 "release_velocity": 30.0}]
        back = [{"pitch": 60, "start_time": 0.0, "duration": 0.25, "velocity": 100.0, "mute": False}]
        res = self.result(method="set_notes", not_written=[{"field": "release_velocity", "notes": 1,
                                                            "reason": "Rueckfall auf set_notes"}],
                          fields_written=["pitch", "start_time", "duration", "velocity", "mute"])
        text = views.notes_added(res, sent, back, STATE)
        self.assertIn("1 Noten gesendet (set_notes)", text)
        self.assertIn("ACHTUNG, NICHT GESCHRIEBEN: release_velocity bei 1 Note(n), Rueckfall auf set_notes.", text)
        self.assertIn("release_velocity gesendet 30, in Live nicht lesbar", text)
        self.assertNotIn("Zusatzfelder an Live", text)

    def test_readback_failed(self):
        text = views.notes_added(self.result(not_written=[]), [{"pitch": 60, "start_time": 0.0, "duration": 0.25,
                                                                "velocity": 100.0, "mute": False}], None, STATE)
        self.assertIn("Nachlesen fehlgeschlagen", text)
        self.assertIn("jetzt 3.", text)


if __name__ == "__main__":
    unittest.main()
