"""Formatierung ohne Live: Mustererkennung und Positionen."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import views  # noqa: E402


def clip(i, start, length, digest, muted=False, name=""):
    return {"index": i, "start_time": start, "end_time": start + length, "content_digest": digest,
            "muted": muted, "name": name, "is_midi": True}


class SegmentTest(unittest.TestCase):

    def test_kick_pattern_like_sols_set(self):
        # Wie KICK BODY am 07.10.2026: Schlag 1 Inhalt A, Schlaege 2-4 Inhalt B, 125 Takte.
        clips = [clip(i, float(i), 0.25, "A" if i % 4 == 0 else "B") for i in range(500)]
        segs = views.clip_segments(clips)
        self.assertEqual(len(segs), 1)
        self.assertEqual((segs[0]["period"], segs[0]["repeats"], segs[0]["step"]), (4, 125, 4.0))
        text = views.arrangement_clips({"track": 1, "track_name": "KICK BODY", "clips": clips,
                                        "clip_count_total": 500}, {"signature_numerator": 4,
                                                                   "signature_denominator": 4})
        self.assertIn("Muster aus 4 Clips, 125x, wiederholt alle 1 Takt", text)
        self.assertIn("im Muster: 1.1.1 MIDI 1/16 Inhalt A | 1.2.1 MIDI 1/16 Inhalt B", text)

    def test_simple_runs_prefer_short_period(self):
        clips = [clip(i, i * 4.0, 4.0, "A", muted=i < 8) for i in range(16)]
        segs = views.clip_segments(clips)
        self.assertEqual([(s["period"], len(s["clips"])) for s in segs], [(1, 8), (1, 8)])

    def test_break_in_pattern(self):
        clips = [clip(i, float(i), 0.25, "A") for i in range(8)] + [clip(8, 8.0, 0.25, "X")]
        segs = views.clip_segments(clips)
        self.assertEqual([len(s["clips"]) for s in segs], [8, 1])

    def test_positions(self):
        tl = views.Timeline(4, 4)
        self.assertEqual(tl.pos(0), "1.1.1")
        self.assertEqual(tl.pos(128.0), "33.1.1")
        self.assertEqual(tl.pos(192.25), "49.1.2")
        self.assertEqual(tl.bar_to_beat(33), 128.0)
        self.assertEqual(views.note_name(60), "C3")
        self.assertEqual(views.note_name(36), "C1")

    def test_durations(self):
        tl = views.Timeline(4, 4)
        self.assertEqual(tl.dur(64.0), "16 Takte")
        self.assertEqual(tl.dur(64.25), "16 Takte + 1/16")
        self.assertEqual(tl.dur(0.25), "1/16")
        self.assertEqual(tl.dur(4.0), "1 Takt")
        self.assertEqual(tl.dur(5.0), "1 Takt + 1/4")

    def test_mute_is_not_reported_twice(self):
        t = {"ref": 8, "name": "LOW PERC", "kind": "midi", "mute": True,
             "mixer": {"track_activator": {"value": 0.0, "display": "Off"}}}
        line = views._track_line(t, views.Timeline(4, 4), 0)[0]
        self.assertIn("STUMM", line)
        self.assertNotIn("Spur AUS", line)

    def test_header_warnings(self):
        lines = views.song_header({"re_enable_automation_enabled": True, "back_to_arranger": True},
                                  views.Timeline(4, 4))
        text = "\n".join(lines)
        self.assertIn("UEBERSTEUERT", text)
        self.assertIn("Back to Arrangement", text)


class MeterTest(unittest.TestCase):

    def test_calibration_points(self):
        # Eichung vom 11.10.2026, Fader-Stufen 0 bis -50 dB an LOW BUS.
        self.assertAlmostEqual(views.meter_db_diff(0.634, 0.765), -10.0, places=3)
        self.assertAlmostEqual(views.meter_db_diff(0.499, 0.765), -20.0, places=3)
        self.assertAlmostEqual(views.meter_db_diff(0.106, 0.765), -50.0, places=3)
        self.assertAlmostEqual(views.meter_db(0.3045), -35.0, places=2)

    def test_relative_table_unchanged(self):
        # Die relative Fader-Eichung selbst rechnet weiterhin nicht ueber ihre Punkte hinaus.
        self.assertIsNone(views.meter_db(0.05))
        self.assertIsNone(views.meter_db(0.9))
        self.assertIsNotNone(views.meter_db(0.766))

    def test_dbfs_anchors(self):
        # Lives Spitzenwert-Anzeige am Master, 11.10.2026 ~21:10.
        self.assertAlmostEqual(views.meter_dbfs(0.657), -20.0, places=6)
        self.assertAlmostEqual(views.meter_dbfs(0.812), -8.35, places=6)
        # LOW BUS bei 0-dB-Fader: README sagt "etwa -11,7 dBFS".
        self.assertAlmostEqual(views.meter_dbfs(0.765), -11.70, places=2)
        # Die gerundete Formel 6 + (m - 1) / 0,0133 liegt ueberall nahe dran.
        for m in (0.106, 0.24, 0.437, 0.657, 0.765, 0.812, 0.9, 1.0):
            self.assertLess(abs(views.meter_dbfs(m) - (6 + (m - 1) / 0.0133)), 0.6, m)

    def test_dbfs_kinds(self):
        self.assertEqual(views.meter_dbfs_info(0.7)[1], "gemessen")
        self.assertEqual(views.meter_dbfs_info(0.9)[1], "gerechnet")
        self.assertAlmostEqual(views.meter_dbfs_info(1.0)[0], 5.79, places=2)
        self.assertEqual(views.meter_dbfs_info(0.05), (views.METER_DBFS_POINTS[0][1], "unter"))
        self.assertEqual(views.meter_dbfs_info(0.0)[1], "still")
        self.assertEqual(views.dbfs_text(0.812), "-8.3 dBFS")
        self.assertEqual(views.dbfs_text(0.9), "~-1.7 dBFS")
        self.assertEqual(views.dbfs_text(0.05), "unter -61.7 dBFS")
        self.assertIsNone(views.meter_db_diff(0.05, 0.764))
        # Ueber der Fader-Eichung gibt es jetzt Abstaende (vorher '?').
        self.assertAlmostEqual(views.meter_db_diff(0.800, 0.812), -0.86, places=2)

    def test_parse_pos(self):
        tl = views.Timeline(4, 4)
        for beat in (0.0, 128.0, 130.5, 192.25, 17.5):
            self.assertAlmostEqual(tl.parse_pos(tl.pos(beat)), beat)
        self.assertAlmostEqual(tl.parse_pos(tl.pos(130.1)), 130.1)
        self.assertEqual(tl.parse_pos(33), 128.0)
        self.assertEqual(tl.parse_pos("33"), 128.0)
        self.assertEqual(tl.parse_pos("5.2.3"), 17.5)
        with self.assertRaises(ValueError):
            tl.parse_pos("33.0.1")
        with self.assertRaises(ValueError):
            tl.parse_pos("Takt 33")

    def test_meter_table(self):
        sample = {"is_playing": True, "meters": [
            {"ref": 0, "name": "LOW BUS", "left": 0.764, "right": 0.740},
            {"ref": 1, "name": "KICK BODY", "left": 0.577, "right": 0.577},
            {"ref": 2, "name": "RUMBLE", "left": 0.08, "right": 0.05},
            {"ref": 3, "name": "HAT", "left": 0.0, "right": 0.0},
            {"ref": "master", "name": "Main", "left": 0.764, "right": 0.764}]}
        overview = {"tracks": [{"ref": 0, "name": "LOW BUS", "group": None},
                               {"ref": 1, "name": "KICK BODY", "group": 0, "mute": True},
                               {"ref": 2, "name": "RUMBLE", "group": 0},
                               {"ref": 3, "name": "HAT", "group": None, "solo": True}], "returns": []}
        text = views.meters([sample], 0.0, overview, "master")
        self.assertIn("[0] LOW BUS | 0.764 (+0.0) | -11.8 dBFS", text)
        self.assertIn("Schieflage L lauter um 1.8 dB", text)
        self.assertIn("[1] KICK BODY | 0.577 (-13.9) | -25.7 dBFS", text)
        self.assertIn("STUMM, Meter zeigt trotzdem Signal", text)
        self.assertIn("[2] RUMBLE | 0.080 (?) | unter -61.7 dBFS", text)
        self.assertIn("L/R ungleich (unter der Eichung", text)
        self.assertIn("SOLO aktiv auf HAT", text)
        self.assertIn("[3] HAT | 0.000 (?) | - |", text)
        self.assertIn("MASTER  Spitze -11.8 dBFS, bis 0 dBFS 11.8 dB.", text)

    def test_loud_reference_like_master_after_sub(self):
        # 11.10.2026: Master 0,812 nach dem Sub. Vorher stand bei jedem dB-Abstand '?'.
        sample = {"is_playing": True, "meters": [
            {"ref": 0, "name": "LOW BUS", "left": 0.800, "right": 0.800},
            {"ref": 1, "name": "KICK SUB", "left": 0.657, "right": 0.657},
            {"ref": "master", "name": "Main", "left": 0.812, "right": 0.812}]}
        text = views.meters([sample], 0.0, {"tracks": [], "returns": []}, "master")
        self.assertIn("[0] LOW BUS | 0.800 (-0.9) | -9.2 dBFS", text)
        self.assertRegex(text, r"\[1\] KICK SUB \| 0\.657 \(-11\.[67]\) \| -20\.0 dBFS")
        self.assertIn("MASTER  Spitze -8.3 dBFS, bis 0 dBFS 8.3 dB.", text)
        hot = {"is_playing": True, "meters": [{"ref": "master", "name": "Main", "left": 0.95, "right": 0.9}]}
        text = views.meters([hot], 0.0, {"tracks": [], "returns": []}, "master")
        self.assertIn("| ~+2.0 dBFS |", text)
        self.assertIn("MASTER  Spitze ~+2.0 dBFS, bis 0 dBFS ~-2.0 dB (UEBER 0 dBFS", text)
        self.assertIn("Schieflage L lauter um ~3.8 dB", text)

    def test_solo_marks_unheard_tracks(self):
        # Solo auf KICK SUB. LOW BUS (Gruppe darueber) und Master hoerbar,
        # KICK BODY in derselben Gruppe und DRUMS nicht. Returns nie markiert.
        sample = {"is_playing": True, "meters": [{"ref": r, "name": n, "left": 0.6, "right": 0.6} for r, n in
                                                 ((0, "LOW BUS"), (1, "KICK BODY"), (2, "KICK SUB"), (3, "DRUMS"),
                                                  (4, "HAT"), ("return:0", "A"))]}
        overview = {"tracks": [{"ref": 0, "name": "LOW BUS", "kind": "group"},
                               {"ref": 1, "name": "KICK BODY", "group": 0},
                               {"ref": 2, "name": "KICK SUB", "group": 0, "solo": True},
                               {"ref": 3, "name": "DRUMS", "kind": "group"},
                               {"ref": 4, "name": "HAT", "group": 3}],
                    "returns": [{"ref": "return:0", "name": "A", "kind": "return"}]}
        lines = {l.split(" | ")[0]: l for l in views.meters([sample], 0.0, overview).splitlines() if l.startswith("[")}
        self.assertNotIn("per Solo stumm", lines["[0] LOW BUS"])
        self.assertIn("per Solo stumm", lines["[1] KICK BODY"])
        self.assertIn("SOLO", lines["[2] KICK SUB"])
        self.assertNotIn("per Solo stumm", lines["[2] KICK SUB"])
        self.assertIn("per Solo stumm", lines["[3] DRUMS"])
        self.assertIn("per Solo stumm", lines["[4] HAT"])
        self.assertNotIn("per Solo stumm", lines["[A] A"])
        # Solo auf einer Gruppe: alles darin bleibt hoerbar.
        overview["tracks"][2]["solo"] = False
        overview["tracks"][3]["solo"] = True
        lines = {l.split(" | ")[0]: l for l in views.meters([sample], 0.0, overview).splitlines() if l.startswith("[")}
        self.assertNotIn("per Solo stumm", lines["[4] HAT"])
        self.assertIn("per Solo stumm", lines["[0] LOW BUS"])

    def test_song_span(self):
        tl = views.Timeline(4, 4)
        # 12.10.2026: Start am Startmarker statt im Loop, ~17 Takte Intro.
        samples = [{"song_time": t, "meters": []} for t in (0.0, 20.0, 68.0)]
        self.assertEqual(views.song_span(samples, tl), "Song-Position waehrend der Messung: 1.1.1 bis 18.1.1.")
        looped = [{"song_time": t, "meters": []} for t in (180.0, 192.0, 128.5, 140.0)]
        self.assertEqual(views.song_span(looped, tl),
                         "Song-Position waehrend der Messung: 33.1.3 bis 49.1.1 (1 Ruecksprung, z. B. Loop).")
        self.assertIsNone(views.song_span([{"meters": []}], tl))

    def test_unusual_output_like_drums_on_19_10(self):
        # Namen wie am Set 22.10.2026. In der Gruppe ist der Ausgang der
        # Gruppenname, sonst Main; Master geht immer nach Ext. Out.
        def t(name, out, group=None, kind="midi"):
            return {"name": name, "kind": kind, "is_grouped": group is not None, "group_name": group,
                    "routing": {"output_type": out}}
        self.assertIsNone(views.unusual_output(t("HAT CLOSED", "DRUMS", "DRUMS")))
        self.assertIsNone(views.unusual_output(t("DRUMS", "Main", kind="group")))
        self.assertIsNone(views.unusual_output(t("DRUMS", "Master", kind="group")))
        self.assertIsNone(views.unusual_output(t("Main", "Ext. Out", kind="master")))
        self.assertEqual(views.unusual_output(t("DRUMS", "Ext. Out", kind="group")), "Main")
        self.assertEqual(views.unusual_output(t("HAT CLOSED", "Main", "DRUMS")), "DRUMS")
        names = {"KICK BODY", "KICK_PRINT_v1"}
        tapped = {"kind": "audio", "routing": {"input_type": "KICK BODY", "input_channel": "Post FX"}}
        self.assertEqual(views.tapped_input(tapped, names), "<- KICK BODY / Post FX")
        self.assertIsNone(views.tapped_input({"kind": "audio", "routing": {"input_type": "Ext. In"}}, names))
        self.assertIsNone(views.tapped_input({"kind": "group", "routing": {"input_type": "KICK BODY"}}, names))

    def test_muted_group_marks_children(self):
        sample = {"is_playing": True, "meters": [{"ref": 0, "name": "G", "left": 0.6, "right": 0.6},
                                                 {"ref": 1, "name": "K", "left": 0.6, "right": 0.6}]}
        # Ohne Master in der Messung gibt es keine Bezugsspitze, also keine dB-Abstaende.
        overview = {"tracks": [{"ref": 0, "name": "G", "group": None, "mute": True},
                               {"ref": 1, "name": "K", "group": 0}]}
        text = views.meters([sample], 0.0, overview)
        self.assertIn("[1] K | 0.600 (?) | -24.1 dBFS | 0.600 (?) | 0.600 / 0.600 | Gruppe G STUMM", text)


if __name__ == "__main__":
    unittest.main()
