"""Paket Automation (technoa.9) ohne Live: Script ueber den Socket, Server-Texte.

Die Attrappe verhaelt sich wie Live 12.4.7 am Set (27.10.2026): Huellkurven
nur an Session-Clips, der Grenzpunkt einer Stufe gehoert zur Stufe davor. Was
sie nicht nachbildet: dass eine Session-Kurve nach duplicate_to_arrangement im
Arrangement-Clip weiter wirkt.
"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))
import fake_live  # noqa: E402
from test_remote_script import Client, free_port, load_script_module, start_fake_live  # noqa: E402

PULSE = 5  # SYNTH PULSE: Arrangement-Clip ueber 16 Beats; Session-Slot 0 legt setUp an


def session_clip(song, c, track=PULSE, length=16.0):
    slot = song.tracks[track].clip_slots[0]
    if not slot.has_clip:
        c.run_on_main(lambda: slot.create_clip(length))
    slot.clip.clear_all_envelopes()
    return slot.clip


class ScriptEnvelopeTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = load_script_module()
        cls.port = free_port()
        cls.song, cls.app, cls.c, cls.script = start_fake_live(cls.mod, cls.port)
        cls.client = Client(cls.port)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.c.run_on_main(cls.script.disconnect)
        cls.c.stop()

    def setUp(self):
        self.clip = session_clip(self.song, self.c)
        fake_live.Clip.ENVELOPES_REFUSED = False

    def where(self, **extra):
        p = {"track": PULSE, "source": "session", "clip_index": 0}
        p.update(extra)
        return p

    def test_write_read_clear_volume(self):
        vol = self.song.tracks[PULSE].mixer_device.volume
        res = self.client.ok("set_clip_envelope", mixer="volume", steps=[
            {"time": 0.0, "duration": 4.0, "value": fake_live.volume_for_db(-12.0)},
            {"time": 4.0, "duration": 12.0, "value": fake_live.volume_for_db(0.0)}], **self.where())
        self.assertTrue(res["created"])
        self.assertIsNone(res["before"])
        self.assertEqual(res["steps_written"], 2)
        self.assertEqual(res["target"]["label"], "Mixer: Volume")
        after = res["after"]
        self.assertEqual(len(after["values"]), 64)
        # Wie Live: Am Anfang einer neuen Kurve steht noch der Reglerwert.
        self.assertEqual(after["displays"][0], "-18.0 dB")
        self.assertEqual(after["displays"][1], "-12.0 dB")
        self.assertEqual(after["displays"][16], "-12.0 dB")
        self.assertEqual(after["displays"][17], "0.0 dB")
        self.assertIsNotNone(res["undo_check"])
        self.assertIsNotNone(self.clip.automation_envelope(vol))

        got = self.client.ok("get_clip_envelope", mixer="volume", **self.where())
        self.assertTrue(got["exists"])
        self.assertEqual(got["samples"]["values"], after["values"])

        listed = self.client.ok("get_clip_envelope", **self.where())
        self.assertEqual([e["label"] for e in listed["envelopes"]], ["Mixer: Volume"])
        self.assertTrue(listed["envelopes"][0]["digest"].startswith("e64-"))

        cleared = self.client.ok("clear_clip_envelope", mixer="volume", **self.where())
        self.assertFalse(cleared["exists_after"])
        self.assertEqual(cleared["cleared"][0]["samples"]["displays"][1], "-12.0 dB")
        self.assertIsNone(self.clip.automation_envelope(vol))

    def test_arrangement_clip_like_live(self):
        # Live legt an Arrangement-Clips keine Huellkurve an; vorher ist nichts geaendert.
        resp = self.client.call("set_clip_envelope", mixer="volume", track=PULSE, source="arrangement",
                                clip_index=0, steps=[{"time": 0.0, "duration": 1.0, "value": 0.5}])
        self.assertEqual(resp["status"], "error")
        self.assertIn("Envelope could not be created", resp["message"])
        self.assertFalse(self.song.tracks[PULSE].arrangement_clips[0].has_envelopes)

    def test_device_param_and_digest_in_track_info(self):
        # Serum 2 auf KICK BODY: Geraet 0, Parameter 2 = Cutoff (0 ist "Device On").
        clip = session_clip(self.song, self.c, track=1, length=4.0)
        self.client.ok("set_clip_envelope", device_index=0, parameter_index=2, track=1, source="session",
                       clip_index=0, steps=[{"time": 0.0, "duration": 0.25, "value": 0.9}])
        info = self.client.ok("get_track_info", track=1)
        self.assertEqual(list(info["session_clips"][0]["envelopes"]), ["[0] Serum 2: Cutoff"])
        clips = self.client.ok("get_arrangement_clips", track=1, include_digest=True)["clips"]
        self.assertNotIn("envelopes", clips[0])
        clip.clear_all_envelopes()

    def test_refuses_before_change(self):
        resp = self.client.call("set_clip_envelope", mixer="volume",
                                steps=[{"time": 0.0, "duration": 1.0, "value": 5.0}], **self.where())
        self.assertEqual(resp["status"], "error")
        self.assertIn("ausserhalb", resp["message"])
        self.assertFalse(self.clip.has_envelopes)
        fake_live.Clip.ENVELOPES_REFUSED = True
        resp = self.client.call("set_clip_envelope", mixer="send:1",
                                steps=[{"time": 0.0, "duration": 1.0, "value": 0.5}], **self.where())
        self.assertIn("Envelope could not be created", resp["message"])
        resp = self.client.call("clear_clip_envelope", mixer="volume", **self.where())
        self.assertIn("hat keine Huellkurve fuer Mixer: Volume", resp["message"])

    def test_replace_and_probe(self):
        self.client.ok("set_clip_envelope", mixer="panning",
                       steps=[{"time": 0.0, "duration": 16.0, "value": -1.0}], **self.where())
        res = self.client.ok("set_clip_envelope", mixer="panning", replace=True,
                             steps=[{"time": 8.0, "duration": 8.0, "value": 1.0}], **self.where())
        self.assertTrue(res["replaced"])
        self.assertEqual(res["before"]["displays"][1], "50L")
        # Nach replace gilt vor Beat 8 wieder der Reglerwert (Attrappe: C).
        self.assertEqual(res["after"]["displays"][1], "C")
        got = self.client.ok("get_clip_envelope", mixer="panning", probe=True, **self.where())
        self.assertIn("insert_step(", got["api"]["envelope"]["insert_step"])
        self.assertIn("automation_envelope(", got["api"]["clip"]["automation_envelope"])

    def test_inspect_doc_and_live_root(self):
        res = self.client.ok("inspect_lom", path="Live.Clip.Clip", doc=True)
        self.assertTrue(res["attributes"]["automation_envelope"].startswith("<Methode> automation_envelope("))
        plain = self.client.ok("inspect_lom", path="song.tracks[5].arrangement_clips[0]")
        self.assertEqual(plain["attributes"]["automation_envelope"], "<Methode>")
        resp = self.client.call("inspect_lom", path="song.tracks[5].arrangement_clips[0].automation_envelope")
        self.assertIn("ruft keine Methoden auf", resp["message"])

    def test_song_settings(self):
        self.song.groove_amount = 0.05
        res = self.client.ok("set_song_settings", groove_amount=1.0, metronome=True)
        self.assertEqual([(c["target"], c["before"], c["after"]) for c in res["changes"]],
                         [("groove_amount", 0.05, 1.0), ("metronome", False, True)])
        self.song.metronome = False

    def test_convert_values(self):
        res = self.client.ok("convert_parameter_values", track=PULSE, mixer="volume",
                             values=["-12 dB", "-inf dB", 0.85])
        self.assertEqual([v["display"] for v in res["values"]], ["-12.0 dB", "-inf dB", "0.0 dB"])
        self.assertAlmostEqual(res["values"][0]["value"], fake_live.volume_for_db(-12.0), delta=0.001)
        res = self.client.ok("convert_parameter_values", track=7, device_index=0, parameter_index=1,
                             values=["41.17"])
        self.assertAlmostEqual(res["values"][0]["value"], 105.1, delta=0.1)
        res = self.client.ok("convert_parameter_values", track=7, device_index=0, parameter_index=0,
                             values=["off"])
        self.assertEqual(res["values"][0]["value"], 0.0)
        for bad, msg in (("-6 dB", "zeigt keine dB"), ("Mitte", "nicht lesbar")):
            resp = self.client.call("convert_parameter_values", track=7, device_index=0, parameter_index=2,
                                    values=[bad])
            self.assertIn(msg, resp["message"])

    def test_delete_range(self):
        # RUMBLE [2]: 16 Audioclips je 4 Beats. Clips, die in Beat 8..16 beginnen: #2, #3.
        track = self.song.tracks[2]
        before = len(track.arrangement_clips)
        resp = self.client.call("delete_arrangement_clips", track=2, from_time=8.0, to_time=16.0, expect_count=3)
        self.assertIn("beginnen jetzt 2 Clips im Bereich, erwartet waren 3", resp["message"])
        self.assertEqual(len(track.arrangement_clips), before)
        res = self.client.ok("delete_arrangement_clips", track=2, from_time=8.0, to_time=16.0, expect_count=2)
        self.assertEqual([d["start_time"] for d in res["deleted"]], [8.0, 12.0])
        self.assertEqual(res["clip_count_after"], before - 2)


class ServerEnvelopeTest(unittest.TestCase):
    """Die Werkzeuge des Servers gegen dieselbe Attrappe, direkt aufgerufen."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        import server
        cls.server = server
        cls.mod = load_script_module()
        cls.port = free_port()
        cls.song, cls.app, cls.c, cls.script = start_fake_live(cls.mod, cls.port)
        cls.saved_live = server.live
        server.live = server.LiveConnection("127.0.0.1", cls.port)
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["TECHNOA_DELETED_CLIPS_DIR"] = cls.tmp.name

    @classmethod
    def tearDownClass(cls):
        cls.server.live._close()
        cls.server.live = cls.saved_live
        os.environ.pop("TECHNOA_DELETED_CLIPS_DIR", None)
        cls.tmp.cleanup()
        cls.c.run_on_main(cls.script.disconnect)
        cls.c.stop()

    def setUp(self):
        self.server._forget()
        self.clip = session_clip(self.song, self.c)

    def write(self, **kw):
        args = {"track": PULSE, "source": "session", "clip_index": 0}
        args.update(kw)
        return self.server.set_clip_envelope(**args)

    def test_ramp_written_as_steps_and_checked(self):
        text = self.write(points=[{"bar": 2, "value": "-inf dB"}, {"bar": "3.1.1", "value": "0 dB"}])
        self.assertIn("Huellkurve Mixer: Volume neu angelegt: 66 von 66 Stufen geschrieben", text)
        self.assertIn("Nachgelesen: stimmt", text)
        # Am Clipanfang steht wie in Live noch der Reglerwert.
        self.assertIn("ACHTUNG: Am Clipanfang (Clip 1.1.1) steht noch -18.0 dB statt des Startwerts.", text)
        self.assertIn("Clip 1.1.2: -inf dB, gehalten bis Clip 2.1.1\n", text)
        self.assertIn("Clip 2.1.1: -inf dB, Rampe bis Clip 3.1.1\n", text)
        self.assertIn("Clip 3.1.1: 0.0 dB, gehalten bis Clip-Ende (Clip 5.1.1)", text)
        env = self.clip.automation_envelope(self.song.tracks[PULSE].mixer_device.volume)
        self.assertAlmostEqual(env.value_at_time(6.0), fake_live.volume_for_db(0.0) / 2, delta=0.01)
        read = self.server.get_clip_envelope(track=PULSE, source="session", clip_index=0, parameter="vol")
        self.assertIn("Clip 2.1.1: -inf dB, Rampe bis Clip 3.1.1", read)
        listed = self.server.get_clip_envelope(track=PULSE, source="session", clip_index=0)
        self.assertIn("Huellkurve Mixer: Volume (Reglerwert jetzt -18.0 dB):", listed)

    def test_steps_insert_and_mismatch_warning(self):
        self.write(parameter="send B", points=[{"bar": 1, "value": "-30 dB"}], shape="steps")
        text = self.write(parameter="send B", points=[{"beat": 4, "value": "-10 dB"}, {"beat": 8, "value": "-10 dB"}])
        self.assertIn("ergaenzt: 1 von 1 Stufen", text)
        self.assertNotIn("ACHTUNG: Am Clipanfang", text)  # nichts am Clipanfang geschrieben
        self.assertIn("Vorher:", text)
        self.assertIn("Clip 1.1.2: -30.0 dB, gehalten bis Clip 2.1.2, dort Sprung\n", text)
        self.assertIn("Clip 2.1.2: -10.0 dB, gehalten bis Clip 3.1.2, dort Sprung\n", text)
        self.assertIn("Clip 3.1.2: -30.0 dB, gehalten bis Clip-Ende (Clip 5.1.1)", text)
        self.assertIn("Nachgelesen: stimmt", text)
        steps = [(0.0, 4.0, 0.5)]
        warn = self.server._envelope_check(steps, {"from": 0.0, "grid": 1.0, "values": [0.5, 0.5, 0.2, 0.5],
                                                   "displays": ["a", "b", "c", "d"]},
                                           {"min": 0.0, "max": 1.0}, self.server._timeline())
        self.assertIn("ACHTUNG: 1 von 3 Werten weichen ab", warn)

    def test_refusals_before_writing(self):
        text = self.write(points=[{"bar": 9, "value": "0 dB"}, {"takt": 2, "value": 1}])
        self.assertIn("Nichts geschrieben", text)
        self.assertIn("Punkt 0: Position ausserhalb des Clips (Clip-Takt 1.1.1 bis 5.1.1", text)
        self.assertIn("{\"bar\": Position, \"value\": Wert}", text)
        self.assertIn("Fehler: Parameter 'filter' ohne device_index",
                      self.server.get_clip_envelope(track=PULSE, source="session", clip_index=0, parameter="filter"))
        self.assertFalse(self.clip.has_envelopes)

    def test_arrangement_explained(self):
        text = self.server.set_clip_envelope(track=PULSE, source="arrangement", clip_index=0,
                                             points=[{"bar": 1, "value": "0 dB"}])
        self.assertIn("Fehler: Clip-Automation schreiben geht an Arrangement-Clips nicht.", text)
        self.assertIn("duplicate_to_arrangement", text)
        read = self.server.get_clip_envelope(track=PULSE, source="arrangement", at_bar=1)
        self.assertIn("Clip-Automation laut Live: nein. Lives API (12.4.7) gibt Huellkurven", read)

    def test_clear_and_snapshot_diff(self):
        import set_snapshot
        before = set_snapshot.take(self.server._call)
        self.write(parameter="pan", points=[{"bar": 1, "value": -1.0}, {"bar": 5, "value": 1.0}])
        mid = set_snapshot.take(self.server._call)
        report = set_snapshot.render(set_snapshot.compare(before, mid), "a", "b", 0)
        self.assertIn("Huellkurve Mixer: Pan neu", report)
        self.assertNotIn("Clip-Huellkurven nein -> ja", report)
        text = self.server.clear_clip_envelope(track=PULSE, source="session", clip_index=0, parameter="pan")
        self.assertIn("Geloescht: Huellkurve Mixer: Pan. Sie war:", text)
        self.assertIn("Werte vorher gesichert: " + self.tmp.name, text)
        after = set_snapshot.take(self.server._call)
        report = set_snapshot.render(set_snapshot.compare(mid, after), "a", "b", 0)
        self.assertIn("Huellkurve Mixer: Pan entfernt", report)
        # Aelterer Schnappschuss ohne Abdruck: kein Schein-"entfernt", nur ja/nein und ein Hinweis.
        for clip in mid["tracks"][str(PULSE)]["info"]["session_clips"]:
            clip.pop("envelopes", None)
        report = set_snapshot.render(set_snapshot.compare(mid, after), "a", "b", 0)
        self.assertIn("Clip-Huellkurven ja -> nein", report)
        self.assertNotIn("Huellkurve Mixer: Pan entfernt", report)

    def test_delete_range_unique_backups(self):
        # HAT CLOSED [4]: 16 gleichnamige Clips je 4 Beats (gleiche Namen, gleiche Sekunde).
        text = self.server.delete_arrangement_clips(track=4, from_bar=3, to_bar=5, expect_count=3)
        self.assertIn("Im Bereich beginnen 2 Clips, erwartet waren 3. Nichts geloescht", text)
        text = self.server.delete_arrangement_clips(track=4, from_bar=3, to_bar=5)
        self.assertIn("2 Arrangement-Clips geloescht, die in 3.1.1 bis 5.1.1 beginnen", text)
        self.assertIn("Gesichert: 2 von 2 Clips einzeln", text)
        names = sorted(os.listdir(self.tmp.name))
        self.assertEqual(len([n for n in names if "HAT_CLOSED" in n]), 2)

    def test_song_settings_and_inspect(self):
        self.song.groove_amount = 0.05
        text = self.server.set_song(groove_amount_percent=100, metronome=True)
        self.assertEqual(text.splitlines()[0], "Groove-Menge global 5 % -> 100 %; Metronom aus -> an")
        self.song.metronome = False
        self.assertIn("Fehler: groove_amount_percent 140", self.server.set_song(groove_amount_percent=140))
        self.assertIn("<Methode> insert_step(", self.server.inspect_lom("Live.Clip.AutomationEnvelope", doc=True))


if __name__ == "__main__":
    unittest.main()
