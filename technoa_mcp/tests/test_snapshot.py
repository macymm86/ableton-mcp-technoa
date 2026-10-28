"""Vergleich von Schnappschuessen (Paket E) an kleinen, erfundenen Sets.

Die Faelle stammen aus der Praxis: ein neuer Clip gab 320 Zeilen Rauschen
, der Fader bei 0 dB lag minimal daneben, ein automatisierter Send
folgte nur dem Playhead, Locator-Zeiten fehlten.
"""
from __future__ import annotations

import copy
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import set_snapshot  # noqa: E402


def par(name, value, display, lo=0.0, hi=1.0, auto=None):
    p = {"name": name, "value": value, "display": display, "min": lo, "max": hi}
    if auto:
        p["automation"] = auto
    return p


def dev(name, params, cls="PluginDevice", disp=None):
    on = par("Device On", 1.0, "On")
    return {"name": name, "class_name": cls, "class_display_name": disp or name, "is_active": True,
            "parameter_count": len(params) + 1, "parameters": [on] + params}


def clip(start, length=4.0, name="", digest="m4-aaaa", muted=False, index=0, groove=None):
    c = {"start_time": start, "end_time": start + length, "length": length, "name": name, "muted": muted,
         "content_digest": digest, "index": index, "is_midi": True, "is_audio": False, "looping": False,
         "loop_start": 0.0, "loop_end": length, "start_marker": 0.0, "end_marker": length, "color_index": 1,
         "has_envelopes": False}
    if groove:
        c["groove"] = groove
    return c


def track(ref, name, kind="midi", volume=(0.849987, "0.0 dB"), sends=None, devices=None, clips=None,
          output="Main"):
    info = {"ref": ref, "name": name, "kind": kind, "mute": False, "solo": False, "color_index": 1,
            "mixer": {"volume": {"name": "Track Volume", "value": volume[0], "display": volume[1]},
                      "panning": {"name": "Track Panning", "value": 0.0, "display": "C"},
                      "sends": sends or [],
                      "track_activator": {"name": "Speaker On", "value": 1.0, "display": "On"}},
            "routing": {"output_type": output, "output_channel": "", "input_type": "All Ins",
                        "input_channel": "All Channels"},
            "devices": []}
    if kind in ("midi", "audio"):
        info["arm"] = False
    d = {"info": info, "devices": devices or []}
    if kind in ("midi", "audio"):
        d["clips"] = [dict(c, index=i) for i, c in enumerate(clips or [])]
    return d


def snap(tracks, playhead=None, playing=False, cues=None, fmt=2, tempo=136.0):
    song = {"tempo": tempo, "signature_numerator": 4, "signature_denominator": 4, "loop": False,
            "loop_start": 128.0, "loop_length": 64.0, "song_length": 544.0, "can_undo": True, "can_redo": False,
            "re_enable_automation_enabled": False, "grooves": [], "groove_amount": 1.0,
            "cue_points": cues if cues is not None else [{"name": "INTRO", "time": 0.0},
                                                         {"name": "PEAK", "time": 256.0}]}
    s = {"format": fmt, "song": song, "order": [str(t["info"]["ref"]) for t in tracks],
         "tracks": {str(t["info"]["ref"]): t for t in tracks}}
    if fmt >= 2:
        s["playhead"] = {"beat": playhead, "playing": playing, "beat_end": playhead, "playing_end": playing}
    return s


def base_set():
    hat_clips = [clip(float(b * 4), 4.0, "HAT") for b in range(80)]
    synth = track(1, "SYNTH PULSE", volume=(0.4, "-18.0 dB"),
                  sends=[{"index": 0, "name": "A-REVERB SPACE", "value": 0.0, "display": "-inf dB"},
                         {"index": 1, "name": "B-DELAY THROW", "value": 0.345, "display": "-26.908 dB",
                          "automation": "active"}],
                  devices=[dev("Serum 2", [])], clips=[clip(0.0, 16.0, "PULSE")])
    low = track(2, "LOW BUS", kind="group",
                devices=[dev("Hold FX", [par("Hold", 0.0, "Off")]),
                         dev("EQ Eight", [par("1 Frequency A", 0.3, "120 Hz"), par("1 Gain A", 0.5, "0.0 dB")],
                             cls="Eq8")])
    return snap([track(0, "HAT CLOSED", clips=hat_clips), synth, low], playhead=128.0)


def report(a, b):
    r = set_snapshot.compare(a, b)
    return r, set_snapshot.render(r, "A", "B", max_lines=0)


class SnapshotCompareTest(unittest.TestCase):

    def test_identical(self):
        a = base_set()
        r, text = report(a, copy.deepcopy(a))
        self.assertEqual(r.counted(), [])
        self.assertIn("Ergebnis: KEINE Aenderungen", text)

    def test_inserted_clip_is_one_line(self):
        # Ein Clip mitten hinein verschob im alten Vergleich alle folgenden.
        a = base_set()
        b = copy.deepcopy(a)
        clips = b["tracks"]["0"]["clips"]
        clips.insert(10, clip(41.0, 1.0, "NEU", digest="m1-bbbb"))
        b["tracks"]["0"]["clips"] = [dict(c, index=i) for i, c in enumerate(clips)]
        r, text = report(a, b)
        self.assertEqual(len(r.counted()), 1, text)
        self.assertIn("[0] HAT CLOSED Clip ab 11.2.1: neu: 'NEU', 1/4, 1 Noten", text)

    def test_many_equal_clip_changes_one_line(self):
        a = base_set()
        b = copy.deepcopy(a)
        for c in b["tracks"]["0"]["clips"][8:40]:
            c["groove"] = "Swing 16ths 66"
        r, text = report(a, b)
        self.assertIn("[0] HAT CLOSED, 32 Clips (9.1.1, 10.1.1 ... 40.1.1): Groove keiner -> 'Swing 16ths 66'", text)
        self.assertIn("Ergebnis: 32 Aenderung(en): 32 Clips", text)

    def test_moved_clip(self):
        a = base_set()
        b = copy.deepcopy(a)
        b["tracks"]["1"]["clips"][0].update(start_time=64.0, end_time=80.0)
        r, text = report(a, b)
        self.assertIn("[1] SYNTH PULSE Clip 'PULSE' verschoben: 1.1.1 -> 17.1.1", text)
        self.assertEqual(len(r.counted()), 1, text)

    def test_tolerance_w9(self):
        a = base_set()
        b = copy.deepcopy(a)
        b["tracks"]["0"]["info"]["mixer"]["volume"]["value"] = 0.8500000238418579
        r, text = report(a, b)
        self.assertEqual(r.counted(), [])
        self.assertIn("innerhalb der Toleranz", text)
        self.assertIn("[0] HAT CLOSED Pegel 0.849987 -> 0.85 (Anzeige 0.0 dB)", text)

    def test_raw_change_with_same_display_counts(self):
        a = base_set()
        b = copy.deepcopy(a)
        b["tracks"]["0"]["info"]["mixer"]["volume"]["value"] = 0.851
        r, text = report(a, b)
        self.assertIn("[0] HAT CLOSED Pegel: Anzeige gleich '0.0 dB', Rohwert 0.849987 -> 0.851", text)

    def test_automation_follows_playhead(self):
        # 20.10.2026: SYNTH Send B wich nur ab, weil der Playhead woanders stand.
        a = base_set()
        b = copy.deepcopy(a)
        b["playhead"]["beat"] = b["playhead"]["beat_end"] = 424.0
        b["tracks"]["1"]["info"]["mixer"]["sends"][1].update(value=0.4, display="-24.812 dB")
        r, text = report(a, b)
        self.assertEqual(r.counted(), [], text)
        self.assertIn("folgt Automation, Wert haengt am Playhead (A 33.1.1, B 107.1.1)", text)
        self.assertIn("[1] SYNTH PULSE Send B (B-DELAY THROW) -26.908 dB -> -24.812 dB", text)

    def test_automation_differs_at_same_playhead(self):
        a = base_set()
        b = copy.deepcopy(a)
        b["tracks"]["1"]["info"]["mixer"]["sends"][1].update(value=0.4, display="-24.812 dB")
        r, text = report(a, b)
        self.assertEqual([e.cat for e in r.counted()], ["AUTOMATION"])
        self.assertIn("ist aber bei gleichem Playhead (33.1.1, Transport steht) anders", text)

    def test_automation_overridden(self):
        a = base_set()
        b = copy.deepcopy(a)
        b["tracks"]["1"]["info"]["mixer"]["sends"][1].update(value=0.1, display="-40.0 dB", automation="overridden")
        r, text = report(a, b)
        self.assertIn("[1] SYNTH PULSE Send B (B-DELAY THROW): Automation aktiv -> Automation UEBERSTEUERT "
                      "(Wert -26.908 dB -> -40.0 dB); zurueck mit re_enable_automation", text)

    def test_routing_and_devices_first(self):
        a = base_set()
        b = copy.deepcopy(a)
        b["tracks"]["2"]["devices"][1]["parameters"][1].update(value=0.35, display="135 Hz")
        b["tracks"]["0"]["info"]["routing"]["output_type"] = "2-LOW BUS"
        b["tracks"]["0"]["clips"][0]["muted"] = True
        r, text = report(a, b)
        routing = text.index("ROUTING (1)")
        params = text.index("PARAMETER (1)")
        clips = text.index("CLIPS (1)")
        self.assertLess(routing, params)
        self.assertLess(params, clips)
        self.assertIn("[0] HAT CLOSED: Ausgang Main -> 2-LOW BUS", text)
        self.assertIn("[2] LOW BUS / 1 EQ Eight / 1 Frequency A: 120 Hz -> 135 Hz", text)

    def test_inserted_device_no_param_noise(self):
        a = base_set()
        b = copy.deepcopy(a)
        b["tracks"]["2"]["devices"].insert(1, dev("Utility", [par("Gain", 0.5, "0.0 dB")], cls="StereoGain"))
        r, text = report(a, b)
        self.assertEqual(len(r.counted()), 1, text)
        self.assertIn("[2] LOW BUS: Geraet neu an Position 1 'Utility'", text)

    def test_moved_and_switched_device(self):
        a = base_set()
        b = copy.deepcopy(a)
        devs = b["tracks"]["2"]["devices"]
        devs.reverse()
        devs[1]["parameters"][0].update(value=0.0, display="Off")
        r, text = report(a, b)
        # Bei zwei getauschten Geraeten ist "das andere wanderte" gleich richtig.
        self.assertIn("[2] LOW BUS: 'EQ Eight' verschoben, Position 1 -> 0", text)
        self.assertEqual(len(r.counted()), 2, text)
        self.assertIn("[2] LOW BUS / 1 Hold FX: an -> aus", text)

    def test_track_inserted_matches_by_name(self):
        a = base_set()
        b = copy.deepcopy(a)
        new = track(1, "15-DS Kick", clips=[])
        tracks = {"0": b["tracks"]["0"], "1": new}
        for old, key in (("1", "2"), ("2", "3")):
            t = b["tracks"][old]
            t["info"]["ref"] = int(key)
            tracks[key] = t
        b["tracks"] = tracks
        b["order"] = ["0", "1", "2", "3"]
        r, text = report(a, b)
        self.assertEqual([e.text for e in r.counted()], ["Spur neu: [1] 15-DS Kick (MIDI)"])

    def test_locators(self):
        a = base_set()
        b = copy.deepcopy(a)
        b["song"]["cue_points"] = [{"name": "INTRO", "time": 0.0}, {"name": "PEAK", "time": 264.0},
                                   {"name": "ENDE", "time": 512.0}]
        r, text = report(a, b)
        self.assertIn("Locator 'PEAK' verschoben: 65.1.1 -> 67.1.1", text)
        self.assertIn("Locator 'ENDE' neu bei 129.1.1", text)

    def test_format1_compatible(self):
        # Format 1: ohne Playhead, Locator ohne Zeit, zusaetzlich "overview".
        a = base_set()
        a["format"] = 1
        del a["playhead"]
        a["song"]["cue_points"] = [{"name": "INTRO"}, {"name": "PEAK"}]
        a["overview"] = {"tracks": []}
        del a["order"]
        b = copy.deepcopy(base_set())
        r, text = report(a, b)
        self.assertEqual(r.counted(), [], text)
        self.assertIn("Playhead nicht festgehalten (Format 1", text)
        self.assertIn("", text)

    def test_solo_marked_without_undo(self):
        a = base_set()
        b = copy.deepcopy(a)
        b["tracks"]["0"]["info"]["solo"] = True
        r, text = report(a, b)
        self.assertIn("[0] HAT CLOSED Solo aus -> an (kein Undo-Schritt)", text)
        self.assertFalse(r.counted()[0].undo)

    def test_max_lines(self):
        a = base_set()
        b = copy.deepcopy(a)
        for i, c in enumerate(b["tracks"]["0"]["clips"]):
            c["name"] = "X%d" % i
        r = set_snapshot.compare(a, b)
        text = set_snapshot.render(r, "A", "B", max_lines=20)
        self.assertEqual(len(text.splitlines()), 21)
        self.assertIn("weitere Zeilen nicht gezeigt (max_lines=0 zeigt alles)", text)

    def test_sidechain_source_under_routing(self):
        # technoa.7: Quelle im Geraet; Bereich "devices" wie set_sidechain.
        a = base_set()
        comp = dev("Compressor", [par("S/C On", 1.0, "On")], cls="Compressor2")
        comp["sidechain"] = {"type": "KICK_PRINT_v1", "channel": "Post FX", "on": True}
        a["tracks"]["2"]["devices"].append(comp)
        b = copy.deepcopy(a)
        b["tracks"]["2"]["devices"][2]["sidechain"].update(type="KICK BODY", channel="Pre FX")
        r, text = report(a, b)
        self.assertIn("ROUTING (1)\n  [2] LOW BUS / 2 Compressor: Sidechain-Quelle KICK_PRINT_v1 / Post FX -> "
                      "KICK BODY / Pre FX", text)
        entry = r.counted()[0]
        self.assertEqual((entry.cat, entry.area), ("ROUTING", "devices"))
        # Der Schalter allein ist ein Parameter, keine Routing-Aenderung.
        c = copy.deepcopy(a)
        c["tracks"]["2"]["devices"][2]["sidechain"]["on"] = False
        r, text = report(a, c)
        self.assertEqual(r.counted(), [], text)

    def test_sidechain_missing_in_old_snapshot(self):
        # Schnappschuss von technoa.6 (ohne Quelle) gegen technoa.7: keine Scheinaenderung.
        a = base_set()
        a["tracks"]["2"]["devices"].append(dev("Compressor", [par("S/C On", 1.0, "On")], cls="Compressor2"))
        b = copy.deepcopy(a)
        b["tracks"]["2"]["devices"][2]["sidechain"] = {"type": "KICK BODY", "channel": "Post FX", "on": True}
        r, text = report(a, b)
        self.assertEqual(r.counted(), [], text)
        self.assertIn("HINWEIS: Sidechain-Quellen stehen nur in einem der beiden Schnappschuesse (erst ab Script "
                      "technoa.7); dort nicht verglichen.", text)

    def test_entries_name_area_for_foreign_check(self):
        a = base_set()
        b = copy.deepcopy(a)
        b["tracks"]["1"]["info"]["mixer"]["volume"].update(value=0.5, display="-14.0 dB")
        b["song"]["tempo"] = 138.0
        r = set_snapshot.compare(a, b)
        by_cat = {e.cat: e for e in r.counted()}
        self.assertEqual(by_cat["MIXER"].area, "mixer")
        self.assertIn(1, by_cat["MIXER"].refs)
        self.assertIn("SYNTH PULSE", by_cat["MIXER"].names)
        self.assertEqual(by_cat["SONG"].refs, {"song"})


if __name__ == "__main__":
    unittest.main()
