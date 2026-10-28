"""Ganze Kette ohne Live: MCP-Client -> server.py (stdio) -> Remote Script -> Attrappe.

Prueft, was Claude Code spaeter wirklich sieht: Werkzeugliste und Textausgaben.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fake_live  # noqa: E402
from test_remote_script import free_port, load_script_module, start_fake_live  # noqa: E402

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

SERVER = os.path.join(HERE, "..", "server.py")

FORBIDDEN_TOOLS = {"set_dataset_consent", "submit_intent", "rate_last_action", "prefer_candidate",
                   "reject_last_action", "record_audition"}


async def run_session(port, calls, extra_env=None):
    env = {"ABLETON_PORT": str(port)}
    env.update(extra_env or {})
    params = StdioServerParameters(command=sys.executable, args=[SERVER], env=env)
    results = {}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            results["__tools__"] = tools.tools
            for key, (name, args) in calls.items():
                if name == "__py__":
                    # Handgriff "von Hand in Live" an der Attrappe zwischen zwei MCP-Befehlen.
                    args()
                    continue
                res = await session.call_tool(name, args)
                results[key] = "\n".join(c.text for c in res.content if getattr(c, "type", "") == "text")
    return results


class ChainTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = load_script_module()
        cls.port = free_port()
        cls.song, cls.app, cls.c, cls.script = start_fake_live(cls.mod, cls.port)
        cls.tmp = tempfile.mkdtemp(prefix="technoa_chain_")
        low, kick = cls.song.tracks[0], cls.song.tracks[1]
        # Plugin, dessen Anzeige wie in Live einen Durchlauf hinterherhinkt.
        low.devices.append(fake_live.Device("Hold FX", "PluginDevice", dtype=2,
                                            params=[fake_live.Param("Mix", 0.0),
                                                    fake_live.LaggingParam("Hold", 0.0, display=lambda v: "On" if v
                                                                           else "Off", quantized=True)]))
        low.output_meter_left = low.output_meter_right = 0.764
        kick.output_meter_left = kick.output_meter_right = 0.577
        kick.mute = True
        cls.song.master_track.output_meter_left = cls.song.master_track.output_meter_right = 0.764
        # Zugriffe beim Aufbau der Attrappe zaehlen nicht; geprueft wird das Script.
        fake_live.OFF_THREAD_ACCESS.clear()
        calls = {
            "info": ("get_remote_script_info", {}),
            "overview": ("get_set_overview", {}),
            "track_by_name": ("get_track_info", {"track": "KICK BODY"}),
            "return_b": ("get_track_info", {"track": "B"}),
            "kick_runs": ("get_arrangement_clips", {"track": "body"}),
            "kick_ambiguous": ("get_track_info", {"track": "kick"}),
            "rumble_clips": ("get_arrangement_clips", {"track": 2, "to_bar": 3, "detail": "clips"}),
            "hat_notes": ("get_clip_notes", {"track": "HAT CLOSED", "source": "arrangement", "at_bar": 9}),
            "params": ("get_device_parameters", {"track": 1, "device_index": 0}),
            "selection": ("get_selection", {}),
            "meters": ("watch_meters", {"seconds": 0.5}),
            "mixer": ("set_mixer", {"track": "SYNTH PULSE", "volume_db": -10, "pan": "10R",
                                     "sends": [{"send": "B", "db": -30}]}),
            "bad_track": ("get_track_info", {"track": "GIBTSNICHT"}),
            "group_clips": ("get_arrangement_clips", {"track": "LOW BUS"}),
            "param_by_name": ("set_device_parameter", {"track": 1, "device_index": 0, "parameter": "Cutoff",
                                                       "value": 0.7}),
            "loop": ("set_loop", {"start_bar": 33, "length_bars": 16}),
            "locator": ("create_locator", {"name": "NEU", "bar": 25}),
            # Bis hier drei MCP-Schritte (mixer, param_by_name, locator); der Loop zaehlt nicht.
            "undo_too_many": ("undo", {"steps": 4}),
            "undo_ok": ("undo", {"steps": 1}),
            "redo_too_many": ("redo", {"steps": 2}),
            "redo_ok": ("redo", {"steps": 1}),
            "playhead": ("set_arrangement_time", {"bar": 9}),
            "playhead_behind_end": ("set_arrangement_time", {"bar": 500}),
            "browser": ("get_browser_items_at_path", {"path": "audio_effects"}),
            "reenable": ("re_enable_automation", {}),
            "solo": ("set_mixer", {"track": 1, "solo": True}),
            "unsolo": ("set_mixer", {"track": 1, "solo": False}),
        }
        calls.update({
            "loop_crooked": ("set_loop", {"start_bar": 33, "length_beats": 64.25}),
            "loop_back": ("set_loop", {"start_bar": 33, "length_bars": 16}),
            # 'nachher' neu gelesen.
            "loop_off": ("set_loop", {"enabled": False}),
            "loop_on": ("set_loop", {"enabled": True}),
            "hold": ("set_device_parameter", {"track": "LOW BUS", "device_index": 1, "parameter": "Hold",
                                              "value": 1}),
            "undo_hold": ("undo", {}),
            "meters_ref": ("watch_meters", {"reference": "LOW BUS"}),
            # KICK BODY: Aktivator automatisiert, in der Attrappe stumm gesetzt.
            "unmute_auto": ("set_mixer", {"track": "KICK BODY", "mute": False}),
            "mute_plain": ("set_mixer", {"track": "RUMBLE", "mute": True}),
        })
        # Geraete verwalten (technoa.5) auf HAT CLOSED, dort liegt in der Attrappe nichts.
        calls.update({
            "ins": ("insert_device", {"track": "HAT CLOSED", "device_name": "Utility"}),
            "ins_pos": ("insert_device", {"track": "HAT CLOSED", "device_name": "EQ Eight", "position": 0}),
            "ins_plugin": ("insert_device", {"track": "HAT CLOSED", "device_name": "Serum 2"}),
            "mark": ("mark_undo", {"name": "serie"}),
            "mv": ("move_device", {"track": "HAT CLOSED", "device_index": 0, "to_index": 1,
                                   "device_name": "EQ Eight"}),
            "mv_bad": ("move_device", {"track": "HAT CLOSED", "device_index": 0, "to_index": 1,
                                       "device_name": "Saturator"}),
            "dup": ("duplicate_device", {"track": "HAT CLOSED", "device_index": 1, "device_name": "EQ Eight"}),
            "del_bad": ("delete_device", {"track": "HAT CLOSED", "device_index": 0, "device_name": "EQ Eight"}),
            "del": ("delete_device", {"track": "HAT CLOSED", "device_index": 2, "device_name": "EQ Eight"}),
            "undo_mark": ("undo_to_mark", {"name": "serie"}),
            "undo_mark_again": ("undo_to_mark", {"name": "serie"}),
            "mark_unknown": ("undo_to_mark", {"name": "gibtsnicht"}),
            # Die Attrappe nimmt bei undo nichts zurueck: Kette jetzt Utility, EQ Eight.
            "load_pos": ("load_instrument_or_effect", {"track": "HAT CLOSED", "uri": "query:AudioFx#Utility",
                                                       "position": 0}),
            "meters_in": ("watch_meters", {"show_input": True}),
        })
        # Schritt 3 (technoa.6), am Ende, damit sich keine Indizes davor verschieben.
        # Neue Spur [8] hinter KICK SUB [7].
        cls.song.exclusive_arm = True
        calls.update({
            "saved_set": ("read_saved_set", {}),
            "new_track": ("create_midi_track", {}),
            "load_drift": ("load_instrument_or_effect", {"track": 8, "uri": "query:Synths#Drift"}),
            "new_track2": ("create_midi_track", {"disarm": True}),
            "load_drift2": ("load_instrument_or_effect", {"track": 9, "uri": "query:Synths#Drift", "disarm": True}),
            "slot": ("create_clip", {"track": 8, "clip_index": 0, "length": 4.0}),
            "arr_dup": ("duplicate_to_arrangement", {"track": 8, "clip_index": 0, "bar": 2}),
            "arr_dup_over": ("duplicate_to_arrangement", {"track": 8, "clip_index": 0, "bar": "2.3.1"}),
            "arr_dup_over_ok": ("duplicate_to_arrangement", {"track": 8, "clip_index": 0, "bar": "2.3.1",
                                                             "overwrite": True}),
            "del_clip_bad": ("delete_arrangement_clip", {"track": 8, "clip_index": 0, "start": "2.3.1"}),
            "del_clip": ("delete_arrangement_clip", {"track": 8, "clip_index": 1, "start": "2.3.1"}),
            "undo_del_clip": ("undo", {}),
            "find_value": ("find_parameter_value", {"track": "KICK SUB", "device_index": 0, "parameter": "Pitch",
                                                    "target": "41.17 Hz"}),
            "find_value_bad": ("find_parameter_value", {"track": "KICK SUB", "device_index": 0,
                                                        "parameter": "Device On", "target": "1"}),
            "play_plain": ("start_playback", {}),
            "play_meters": ("watch_meters", {}),
            "play_stop": ("stop_playback", {}),
            "play_loop": ("start_playback", {"bar": "loop"}),
            "play_stop2": ("stop_playback", {}),
        })
        # Schnappschuss, Vergleich, fremde Aenderungen vor undo_to_mark.
        cls.snapshots = os.path.join(cls.tmp, "snapshots")
        # Leeres Projekt: Dort darf nichts landen, und "juengstes Projekt" muss es ueberspringen.
        os.makedirs(os.path.join(cls.snapshots, "other_track"))
        hat, sub = cls.c.run_on_main(lambda: (cls.song.tracks[4], cls.song.tracks[7]))

        def hand_change():
            def f():
                sub.mixer_device.volume.value = fake_live.volume_for_db(-10.0)
                c = fake_live.Clip("HAND", 2.0, 1.0, notes=[fake_live.Note(42, 0.0, 0.25, 100)])
                c.canonical_parent = hat
                hat._arrangement = sorted(hat._arrangement + [c], key=lambda x: x.start_time)
            cls.c.run_on_main(f)

        def hand_revert():
            def f():
                sub.mixer_device.volume.value = fake_live.volume_for_db(-12.0)
                hat._arrangement = [c for c in hat._arrangement if c.name != "HAND"]
            cls.c.run_on_main(f)

        calls.update({
            "e_snap": ("snapshot", {"label": "Start Gr" + chr(0xF6) + chr(0xDF) + "e", "project": "demo_track"}),
            "e_bad_project": ("snapshot", {"project": "../raus"}),
            "e_diff_same": ("snapshot_diff", {}),
            "e_mark": ("mark_undo", {"name": "e"}),
            "e_mcp": ("set_mixer", {"track": "SYNTH PULSE", "volume_db": -20}),
            "e_hand": ("__py__", hand_change),
            "e_diff": ("snapshot_diff", {"a": "marke:e", "b": "jetzt"}),
            "e_undo_refused": ("undo_to_mark", {"name": "e"}),
            "e_hand_revert": ("__py__", hand_revert),
            "e_undo_ok": ("undo_to_mark", {"name": "e"}),
            # Marke ohne Schnappschuss: zurueck geht, geprueft wird nichts.
            "e_mark_x": ("mark_undo", {"name": "x", "snapshot": False}),
            "e_param": ("set_device_parameter", {"track": 1, "device_index": 0, "parameter": "Cutoff",
                                                 "value": 0.6}),
            "e_undo_x": ("undo_to_mark", {"name": "x"}),
            # Ein MCP-Schritt ohne Wirkung: Stand der Marke danach wieder erreicht.
            "e_mark_leer": ("mark_undo", {"name": "leer", "project": "demo_track"}),
            "e_tempo_same": ("set_tempo", {"bpm": 136}),
            "e_undo_reached": ("undo_to_mark", {"name": "leer"}),
            "e_snap_cmp": ("snapshot", {"label": "ende", "compare_to": "letzter"}),
            "e_diff_missing": ("snapshot_diff", {"a": "gibtsnicht"}),
        })

        # Teil 2 (technoa.7): Groove, Routing, Sidechain; dazwischen wird der
        # Ausgang von KICK SUB von Hand umgestellt (fremde Routing-Aenderung).
        def hand_routing(name):
            def f():
                sub.output_routing_type = next(t for t in sub.available_output_routing_types
                                               if t.display_name == name)
            return lambda: cls.c.run_on_main(f)

        calls.update({
            "cd_mark": ("mark_undo", {"name": "cd", "project": "demo_track"}),
            "g_off": ("set_clip_groove", {"track": "HAT CLOSED", "from_bar": 5, "to_bar": 9, "groove": "keiner"}),
            "g_same": ("set_clip_groove", {"track": "HAT CLOSED", "from_bar": 5, "to_bar": 9, "groove": None}),
            "g_on": ("set_clip_groove", {"track": "HAT CLOSED", "from_bar": None, "to_bar": None, "groove": "swing"}),
            "g_order": ("set_clip_groove", {"track": "HAT CLOSED", "from_bar": 9, "to_bar": 5, "groove": "swing"}),
            "g_unknown": ("set_clip_groove", {"track": "HAT CLOSED", "from_bar": 1, "to_bar": 5, "groove": "Shuffle"}),
            "g_reach": ("set_clip_groove", {"track": "HAT CLOSED", "from_bar": "5.3.1", "to_bar": 7, "groove": None}),
            "r_opts": ("get_routing_options", {"track": "HAT CLOSED"}),
            "r_out": ("set_routing", {"track": "HAT CLOSED", "output": "main"}),
            "r_overview": ("get_set_overview", {}),
            "r_back": ("set_routing", {"track": "HAT CLOSED", "output": 3}),
            "r_in_group": ("set_routing", {"track": "DRUMS", "input": "Ext. In"}),
            "r_in": ("set_routing", {"track": "RUMBLE", "input": "KICK BODY", "input_channel": "Post FX"}),
            "r_nothing": ("set_routing", {"track": "RUMBLE"}),
            "r_ambiguous": ("set_routing", {"track": "RUMBLE", "input": "KICK"}),
            "s_params": ("get_device_parameters", {"track": "DRUMS", "device_index": 0, "name_filter": "S/C"}),
            "s_opts": ("get_routing_options", {"track": "DRUMS", "device_index": 0}),
            "s_set": ("set_sidechain", {"track": "DRUMS", "device_index": 0, "source": "KICK SUB", "channel": "pre",
                                        "device_name": "Compressor"}),
            "s_off": ("set_sidechain", {"track": "DRUMS", "device_index": 0, "enable": False}),
            "s_no_input": ("set_sidechain", {"track": "KICK BODY", "device_index": 0, "source": "KICK SUB"}),
            "s_wrong_name": ("set_sidechain", {"track": "DRUMS", "device_index": 0, "enable": True,
                                               "device_name": "Gate"}),
            "s_overview": ("get_set_overview", {}),
            "cd_diff": ("snapshot_diff", {"a": "marke:cd", "b": "jetzt", "max_lines": 0}),
            "cd_hand": ("__py__", hand_routing("Ext. Out")),
            "cd_undo_refused": ("undo_to_mark", {"name": "cd"}),
            "cd_hand_revert": ("__py__", hand_routing("Main")),
            "cd_undo": ("undo_to_mark", {"name": "cd"}),
        })

        # Teil 3 (technoa.8) auf der neuen Spur [8]: Session-Slot 0 ist ein
        # leerer 1-Takt-Clip, im Arrangement liegt nur #0 bei 2.1.1 bis 2.3.1.
        def ignore_fields(fields):
            def f():
                fake_live.Clip.IGNORED_NOTE_FIELDS = set(fields)
            return f

        notes = [{"pitch": 48, "start_time": 0.0, "duration": 0.5, "velocity": 90, "probability": 0.5,
                  "velocity_deviation": 20},
                 {"pitch": 55, "start_time": 1.0, "duration": 0.5, "velocity": 80, "release_velocity": 30}]
        calls.update({
            "t3_notes": ("add_notes_to_clip", {"track": 8, "source": "session", "clip_index": 0, "notes": notes}),
            "t3_notes_bad": ("add_notes_to_clip", {"track": 8, "source": "session", "clip_index": 0, "notes": [
                {"pitch": 60, "start_beat": 2.0, "duration_beat": 0.25, "velocity": 300}]}),
            "t3_ignore_on": ("__py__", ignore_fields(["release_velocity"])),
            "t3_notes_ignored": ("add_notes_to_clip", {"track": 8, "source": "session", "clip_index": 0, "notes": [
                {"pitch": 60, "start_time": 2.0, "duration": 0.25, "release_velocity": 30}]}),
            "t3_ignore_off": ("__py__", ignore_fields([])),
            "t3_clip_notes": ("get_clip_notes", {"track": 8, "source": "session", "clip_index": 0}),
            "t3_dup_repeat": ("duplicate_to_arrangement", {"track": 8, "clip_index": 0, "bar": 9, "repeat": 4}),
            "t3_fill": ("duplicate_to_arrangement", {"track": 8, "clip_index": 0, "bar": 19}),
            # Takt 19 liegt in der Luecke zwischen 17 und 21: kein Hindernis.
            "t3_dup_every": ("duplicate_to_arrangement", {"track": 8, "clip_index": 0, "bar": 17, "repeat": 3,
                                                          "every_bars": 4}),
            "t3_undo": ("undo", {}),
            "t3_dup_guard": ("duplicate_to_arrangement", {"track": 8, "clip_index": 0, "bar": 11, "repeat": 3}),
            "t3_dup_tight": ("duplicate_to_arrangement", {"track": 8, "clip_index": 0, "bar": 40, "repeat": 2,
                                                          "every_bars": 0.5}),
            "t3_dup_many": ("duplicate_to_arrangement", {"track": 8, "clip_index": 0, "bar": 40, "repeat": 257}),
        })
        cls.out = asyncio.run(run_session(cls.port, calls, {
            "TECHNOA_DELETED_DIR": os.path.join(cls.tmp, "geloescht"),
            "TECHNOA_DELETED_CLIPS_DIR": os.path.join(cls.tmp, "geloeschte_clips"),
            "ABLETON_MCP_SNAPSHOT_DIR": cls.snapshots}))

    @classmethod
    def tearDownClass(cls):
        cls.c.run_on_main(cls.script.disconnect)
        cls.c.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_device_management(self):
        o = self.out
        self.assertIn("[4] HAT CLOSED: 'Utility' eingefuegt an Position 0.", o["ins"])
        self.assertIn("Kette jetzt: 0 EQ Eight, 1 Utility", o["ins_pos"])
        self.assertIn("Live-eigene Geraete", o["ins_plugin"])
        self.assertIn("'EQ Eight' von Position 0 nach 1.", o["mv"])
        self.assertTrue(o["mv_bad"].startswith("Fehler:"), o["mv_bad"])
        self.assertIn("liegt an Position 0 'Utility', nicht 'Saturator'", o["mv_bad"])
        self.assertIn("'EQ Eight' dupliziert an Position 2.", o["dup"])
        self.assertTrue(o["del_bad"].startswith("Fehler:"), o["del_bad"])
        self.assertIn("'EQ Eight' (Position 2, EQ Eight) geloescht.", o["del"])
        self.assertIn("Parameter vorher (1): Device On=On", o["del"])
        saved = os.listdir(os.path.join(self.tmp, "geloescht"))
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0].endswith("_HAT_CLOSED_EQ_Eight.json"), saved)
        self.assertIn(saved[0], o["del"])

    def test_undo_mark(self):
        o = self.out
        self.assertIn("Marke 'serie' gesetzt.", o["mark"])
        lines = [l for l in o["undo_mark"].splitlines() if l.startswith("zurueckgenommen:")]
        self.assertEqual(lines, ["zurueckgenommen: delete_device [HAT CLOSED] 'EQ Eight' geloescht",
                                 "zurueckgenommen: duplicate_device [HAT CLOSED] 'EQ Eight' an 2",
                                 "zurueckgenommen: move_device [HAT CLOSED] 'EQ Eight' 0 -> 1"])
        self.assertIn("keine MCP-Schritte", o["undo_mark_again"])
        self.assertIn("Keine Marke 'gibtsnicht'", o["mark_unknown"])

    def test_snapshot_tools(self):
        o = self.out
        snap = o["e_snap"]
        self.assertRegex(snap, r"Schnappschuss abgelegt: snapshots/demo_track/\d{4}-\d\d-\d\d_\d{4}"
                               r"_Start_Groesse\.json \(Projekt angegeben\)")
        self.assertIn("13 Spuren/Returns/Master", snap)
        self.assertIn("Playhead 33.1.1, Transport steht", snap)
        self.assertIn("Fehler: Projektname '../raus' geht nicht", o["e_bad_project"])
        self.assertIn("demo_track, other_track", o["e_bad_project"])
        same = o["e_diff_same"]
        self.assertIn("A: letzter = ", same)
        self.assertIn("B: jetzt (nicht abgelegt)", same)
        self.assertIn("Ergebnis: KEINE Aenderungen", same)
        diff = o["e_diff"]
        self.assertIn("A: Marke 'e'", diff)
        self.assertIn("[5] SYNTH PULSE Pegel: -10.0 dB -> -20.0 dB", diff)
        self.assertIn("[7] KICK SUB Pegel: -12.0 dB -> -10.0 dB", diff)
        # ein neuer Clip ist eine Zeile, keine Verschiebung aller folgenden.
        self.assertIn("[4] HAT CLOSED Clip ab 1.3.1: neu: 'HAND', 1/4, 1 Noten", diff)
        self.assertIn("Ergebnis: 3 Aenderung(en): 2 Mixer, 1 Clips", diff)
        self.assertIn("Fehler: Schnappschuss 'gibtsnicht' nicht gefunden", o["e_diff_missing"])
        cmp_ = o["e_snap_cmp"]
        self.assertIn("A: letzter = ", cmp_)
        self.assertIn("_zurueck_leer.json", cmp_)
        self.assertIn("Ergebnis: KEINE Aenderungen", cmp_)
        files = os.listdir(os.path.join(self.snapshots, "demo_track"))
        self.assertEqual(sorted(f.split("_", 2)[2] for f in files),
                         ["Start_Groesse.json", "ende.json", "marke_cd.json", "marke_e.json", "marke_leer.json",
                          "zurueck_cd.json", "zurueck_e.json", "zurueck_leer.json"])
        # Die Marke 'serie' kam vor jedem Projekt: Sie liegt im Standardprojekt.
        self.assertEqual(sorted(os.listdir(self.snapshots)), ["default", "demo_track", "other_track"])
        self.assertEqual(sorted(f.split("_", 2)[2] for f in os.listdir(os.path.join(self.snapshots, "default"))),
                         ["marke_serie.json", "zurueck_serie.json"])
        self.assertEqual(os.listdir(os.path.join(self.snapshots, "other_track")), [])

    def test_undo_to_mark_foreign(self):
        o = self.out
        self.assertIn("Marke 'e' gesetzt. Schnappschuss: snapshots/demo_track/", o["e_mark"])
        refused = o["e_undo_refused"]
        self.assertTrue(refused.startswith("Abgelehnt, nichts zurueckgenommen. Seit der Marke 'e' hat das MCP 1 "
                                           "Schritt(e) angelegt"), refused)
        self.assertIn("FREMDE AENDERUNGEN seit der Marke (2), kein MCP-Befehl erklaert sie:", refused)
        # Mixer vor Clips, wie im Vergleich.
        self.assertIn("  MIXER [7] KICK SUB Pegel: -12.0 dB -> -10.0 dB\n"
                      "  CLIPS [4] HAT CLOSED Clip ab 1.3.1: neu: 'HAND'", refused)
        self.assertIn("Soll das Fremde bleiben", refused)
        self.assertIn("Vom MCP seit der Marke (1): [5] SYNTH PULSE Pegel: -10.0 dB -> -20.0 dB", refused)
        self.assertNotIn("zurueckgenommen:", refused)
        ok = o["e_undo_ok"]
        self.assertIn("zurueckgenommen: set_mixer [SYNTH PULSE] volume -20.0 dB", ok)
        # Die Attrappe nimmt bei undo nichts zurueck: Die Pruefung muss das merken.
        self.assertIn("PRUEFUNG: Stand der Marke 'e' NICHT ganz erreicht", ok)
        self.assertIn("[5] SYNTH PULSE Pegel: -10.0 dB -> -20.0 dB", ok)
        self.assertIn("undo(force=true) Schritt fuer Schritt", ok)
        reached = o["e_undo_reached"]
        self.assertIn("zurueckgenommen: set_tempo 136 BPM", reached)
        self.assertIn("PRUEFUNG: Stand der Marke 'leer' wieder erreicht", reached)

    def test_undo_to_mark_without_snapshot(self):
        o = self.out
        self.assertIn("HINWEIS: Ohne Schnappschuss kann undo_to_mark fremde Aenderungen", o["e_mark_x"])
        undone = o["e_undo_x"]
        self.assertNotIn("Abgelehnt", undone)
        self.assertIn("zurueckgenommen: set_device_parameter [1] Serum 2: Cutoff = 0.60", undone)
        self.assertIn("HINWEIS: Die Marke hat keinen Schnappschuss", undone)
        self.assertNotIn("PRUEFUNG", undone)

    def test_clip_groove(self):
        o = self.out
        self.assertIn("[4] HAT CLOSED: 4 Clip(s) auf keinen Groove gesetzt (Clips, die ab 5.1.1 bis vor 9.1.1 "
                      "beginnen), ein Undo-Schritt.\n  vorher 'Swing 16ths 66': 5.1.1, 6.1.1, 7.1.1, 8.1.1", o["g_off"])
        self.assertIn("Fehler: Alle 4 Clips im Bereich haben schon keinen Groove. Nichts geaendert.", o["g_same"])
        # Namensteil reicht; 12 Clips hatten ihn schon.
        self.assertIn("4 Clip(s) auf Groove 'Swing 16ths 66' gesetzt (ganze Spur)", o["g_on"])
        self.assertIn("hatten schon Groove 'Swing 16ths 66', unveraendert: 12 Clip(s), 1.1.1, 2.1.1 ... 16.1.1",
                      o["g_on"])
        self.assertIn("to_bar ist der erste Takt, der NICHT mehr dazugehoert", o["g_order"])
        self.assertIn("Groove 'Shuffle' ist im Groove-Pool nicht zu finden. Dort liegt: 'Swing 16ths 66'.",
                      o["g_unknown"])
        self.assertIn("1 Clip(s) auf keinen Groove gesetzt (Clips, die ab 5.3.1 bis vor 7.1.1 beginnen)",
                      o["g_reach"])
        self.assertIn("Nicht erfasst, weil sie vor dem Bereich beginnen und hineinreichen: 5.1.1.", o["g_reach"])
        self.assertNotIn("NICHT", o["g_off"])

    def test_routing(self):
        o = self.out
        opts = o["r_opts"]
        self.assertIn("Ausgang jetzt: [3] DRUMS / Kanal '-'", opts)
        self.assertIn("[Master] Main, [0] LOW BUS", opts)
        self.assertIn("Kanaele dazu: keine Auswahl", opts)
        self.assertIn("Kanaele dazu: 'All Channels', 'Ch. 1', 'Ch. 2'", opts)
        self.assertIn("[4] HAT CLOSED: Ausgang DRUMS -> Main", o["r_out"])
        self.assertIn("HINWEIS: Der Ausgang weicht jetzt vom Ueblichen ab (ueblich: DRUMS)", o["r_out"])
        # Hervorgehoben in der Uebersicht, danach per Nummer zurueck.
        self.assertIn("[4] HAT CLOSED (MIDI) | -15.9 dB | Pan C | -> Main | AUSGANG UNGEWOEHNLICH (ueblich: DRUMS)",
                      o["r_overview"])
        self.assertIn("HINWEIS: Ausgang ungewoehnlich: [4] HAT CLOSED -> Main (ueblich DRUMS)", o["r_overview"])
        self.assertIn("[4] HAT CLOSED: Ausgang Main -> DRUMS\nAusgang ist wieder der uebliche (DRUMS).", o["r_back"])
        self.assertIn("[3] DRUMS (Gruppe): Dort hat der Eingang keine Bedeutung", o["r_in_group"])
        self.assertIn("[2] RUMBLE: Eingang Ext. In / 1 -> KICK BODY / Post FX", o["r_in"])
        self.assertIn("Nichts zu setzen", o["r_nothing"])
        self.assertIn("Eingang 'KICK' ist mehrdeutig: [1] KICK BODY, [7] KICK SUB.", o["r_ambiguous"])
        self.assertIn("[2] RUMBLE (Audio) | -8.0 dB | Pan C | -> LOW BUS | <- KICK BODY / Post FX", o["s_overview"])
        self.assertNotIn("UNGEWOEHNLICH", o["s_overview"])

    def test_sidechain(self):
        o = self.out
        self.assertIn("Sidechain-Eingang: Quelle 'KICK BODY', Kanal 'Post FX', Schalter S/C On an.", o["s_params"])
        self.assertIn("Geraete: Compressor [S/C <- KICK BODY / Post FX (an)]", o["r_overview"])
        self.assertIn("Quelle jetzt: [1] KICK BODY / Kanal 'Post FX'", o["s_opts"])
        self.assertIn("[4] HAT CLOSED", o["s_opts"])
        self.assertNotIn("[3] DRUMS,", o["s_opts"])
        self.assertIn("[3] DRUMS | Geraet 0 'Compressor': Sidechain-Quelle KICK BODY / Post FX -> KICK SUB / Pre FX",
                      o["s_set"])
        self.assertIn("Schalter S/C On an -> aus\nSidechain jetzt: S/C <- KICK SUB / Pre FX (aus)", o["s_off"])
        self.assertIn("hat ueber die API keinen Sidechain-Eingang", o["s_no_input"])
        self.assertIn("nicht 'Gate'", o["s_wrong_name"])
        # Mit S/C aus nennt die Uebersicht die Quelle nicht mehr.
        self.assertIn("Geraete: Compressor\n", o["s_overview"])

    def test_routing_in_snapshot_and_undo_mark(self):
        o = self.out
        diff = o["cd_diff"]
        self.assertIn("Ergebnis: 5 Aenderung(en): 3 Routing, 1 Parameter, 1 Clips", diff)
        self.assertIn("ROUTING (3)\n  [2] RUMBLE: Eingang Ext. In -> KICK BODY\n  [2] RUMBLE: Eingangskanal 1 -> "
                      "Post FX\n  [3] DRUMS / 0 Compressor: Sidechain-Quelle KICK BODY / Post FX -> KICK SUB / Pre FX",
                      diff)
        self.assertIn("[4] HAT CLOSED Clip ab 6.1.1: Groove 'Swing 16ths 66' -> keiner", diff)
        # Der Ausgang von Hand ist fremd; Groove, Routing und Sidechain des MCP nicht (TRACK_AREAS).
        refused = o["cd_undo_refused"]
        self.assertIn("FREMDE AENDERUNGEN seit der Marke (2), kein MCP-Befehl erklaert sie:\n"
                      "  ROUTING [7] KICK SUB: Ausgang Main -> Ext. Out", refused)
        self.assertIn("Vom MCP seit der Marke (5):", refused)
        self.assertIn("Sidechain-Quelle KICK BODY / Post FX -> KICK SUB / Pre FX", refused)
        undone = o["cd_undo"]
        self.assertIn("zurueckgenommen: set_sidechain [DRUMS] 'Compressor' Sidechain S/C On an -> aus", undone)
        self.assertIn("zurueckgenommen: set_sidechain [DRUMS] 'Compressor' Sidechain Quelle KICK BODY / Post FX -> "
                      "KICK SUB / Pre FX", undone)
        self.assertIn("zurueckgenommen: set_track_routing [RUMBLE] Eingang Ext. In / 1 -> KICK BODY / Post FX", undone)
        self.assertIn("zurueckgenommen: set_clip_groove [HAT CLOSED] Groove 'Swing 16ths 66' fuer 4 Clip(s)", undone)
        self.assertNotIn("Abgelehnt", undone)

    def test_load_at_position(self):
        out = self.out["load_pos"]
        self.assertIn("'Utility' von Position 2 nach 0.", out)
        self.assertIn("Zwei Undo-Schritte", out)
        chain = self.c.run_on_main(lambda: [d.name for d in self.song.tracks[4].devices])
        self.assertEqual(chain, ["Utility", "Utility", "EQ Eight"])

    def test_input_meters(self):
        m = self.out["meters_in"]
        self.assertIn("| Eingang | Status", m)
        self.assertIn("[1] KICK BODY | 0.577", m)
        self.assertIn("/ 0.577 | MIDI 0.000", m)

    def test_meter_hints(self):
        m = self.out["meters_ref"]
        self.assertIn("Abstand zur Spitze von [0] LOW BUS", m)
        self.assertIn("[1] KICK BODY | 0.577 (-13.9) | -25.7 dBFS", m)
        self.assertIn("STUMM, Meter zeigt trotzdem Signal", m)
        self.assertIn("MASTER  Spitze -11.8 dBFS, bis 0 dBFS 11.8 dB.", m)

    def test_saved_set_path_from_live(self):
        out = self.out["saved_set"]
        self.assertIn("Datei nicht gefunden: D:\\Sets\\Demo_Set.als", out)
        self.assertIn("laut Live (song.file_path)", out)

    def test_new_track_and_arm(self):
        o = self.out
        self.assertIn("Neue MIDI-Spur [8] '9-MIDI' angelegt | Ausgang Main | ein Undo-Schritt.", o["new_track"])
        self.assertNotIn("SCHARF", o["new_track"])
        self.assertIn("Spur ans Ende gehaengt", o["new_track"])
        load = o["load_drift"]
        # Live benennt die Spur beim Laden um; der Vergleich der uebrigen Spuren muss trotzdem laufen.
        self.assertNotIn("nicht verglichen", load)
        self.assertIn("[8] 9-Drift ist nach dem Laden SCHARF (Lives Auto-Arm)", load)
        self.assertIn("set_mixer(track=8, arm=false)", load)
        self.assertIn("ACHTUNG: Arm hat sich auch auf [6] ATMO PAD geaendert: scharf -> aus", load)
        self.assertIn("set_mixer(track=6, arm=true)", load)
        load2 = o["load_drift2"]
        self.assertIn("[9] 10-Drift war nach dem Laden SCHARF (Lives Auto-Arm) und ist jetzt wieder aus", load2)
        self.assertIn("[8] 9-Drift geaendert: scharf -> aus", load2)
        self.assertFalse(self.c.run_on_main(lambda: self.song.tracks[9].arm))

    def test_start_playback_position(self):
        plain = self.out["play_plain"]
        self.assertIn("Wiedergabe laeuft, Song-Position jetzt 1.1.1 | Loop an, 33.1.1 bis 49.1.1", plain)
        self.assertIn("vor dem Loop", plain)
        self.assertIn("Song-Position waehrend der Messung: 1.1.1 bis 1.1.1", self.out["play_meters"])
        looped = self.out["play_loop"]
        self.assertIn("Song-Position jetzt 33.1.1", looped)
        self.assertNotIn("HINWEIS", looped)

    def test_duplicate_overwrite_guard(self):
        o = self.out
        self.assertIn("ins Arrangement kopiert, 2.1.1 bis 3.1.1 (1 Takt).", o["arr_dup"])
        self.assertIn("Neuer Clip: #0 2.1.1 bis 3.1.1 '', ohne Groove", o["arr_dup"])
        self.assertNotIn("Vorher im Bereich", o["arr_dup"])
        refused = o["arr_dup_over"]
        self.assertTrue(refused.startswith("Fehler: Im Zielbereich 2.3.1 bis 3.3.1"), refused)
        self.assertIn("liegen schon 1 Clip(s): #0 2.1.1 bis 3.1.1 ''", refused)
        self.assertIn("Nichts geaendert. Mit overwrite=true trotzdem.", refused)
        ok = o["arr_dup_over_ok"]
        self.assertIn("Vorher im Bereich: #0 2.1.1 bis 3.1.1 ''", ok)
        self.assertIn("Nachher:           #0 2.1.1 bis 2.3.1 ''; #1 2.3.1 bis 3.3.1 ''", ok)

    def test_teil3_notes(self):
        o = self.out
        ok = o["t3_notes"]
        self.assertIn("[8] 9-Drift | Session-Clip #0 '': 2 Noten gesendet (add_new_notes). Noten im Clip vorher 0, "
                      "jetzt 2.", ok)
        self.assertIn("Zusatzfelder an Live: probability (1 Noten), velocity_deviation (1 Noten), release_velocity "
                      "(1 Noten)", ok)
        self.assertIn("Nachgelesen: alle 2 Noten wie gesendet (mit probability, velocity_deviation, "
                      "release_velocity).", ok)
        self.assertNotIn("ACHTUNG", ok)
        bad = o["t3_notes_bad"]
        self.assertTrue(bad.startswith("Fehler: Noten nicht geschrieben, nichts geaendert:"), bad)
        self.assertIn("Note 0: unbekanntes Feld 'start_beat' (gemeint: 'start_time'?)", bad)
        self.assertIn("Note 0: unbekanntes Feld 'duration_beat' (gemeint: 'duration'?)", bad)
        self.assertIn("Note 0: 'start_time' fehlt", bad)
        self.assertIn("Note 0: velocity=300 liegt ausserhalb von 0 bis 127", bad)
        # Live nimmt das Feld an, speichert es aber nicht: nur das Nachlesen sieht es.
        ign = o["t3_notes_ignored"]
        self.assertIn("ACHTUNG, NACHGELESEN ANDERS: 1 von 1 Noten speichert Live anders als gesendet", ign)
        self.assertIn("C3 (60) bei 1.3.1: release_velocity gesendet 30, in Live 64", ign)
        self.assertIn("Noten im Clip vorher 2, jetzt 3.", ign)
        table = o["t3_clip_notes"]
        self.assertIn("Zeit | Pos | Note | Dauer | Vel | Prob | stumm | VelDev | RelVel", table)
        self.assertIn("0 | 1.1.1 | C2 | 1/8 | 90 | 0.5 |  | 20 | 64", table)

    def test_teil3_duplicate_repeat(self):
        o = self.out
        rep = o["t3_dup_repeat"]
        self.assertIn("Session-Slot 0 '' 4x ins Arrangement kopiert, 9.1.1 bis 13.1.1 (4 Takte), alle 1 Takt.", rep)
        self.assertIn("Neue Clips: 4 von 4 gefunden: #1 9.1.1 bis 10.1.1 ''; #2 10.1.1 bis 11.1.1 ''; "
                      "#3 11.1.1 bis 12.1.1 ''; #4 12.1.1 bis 13.1.1 '', ohne Groove", rep)
        self.assertNotIn("HINWEIS: Kein Undo-Schritt", rep)
        every = o["t3_dup_every"]
        self.assertIn("3x ins Arrangement kopiert, 17.1.1 bis 26.1.1 (9 Takte), alle 4 Takte.", every)
        self.assertNotIn("Vorher im Bereich", every)
        self.assertIn("Neue Clips: 3 von 3 gefunden", every)
        self.assertIn("zurueckgenommen: duplicate_session_clip_to_arrangement [9-Drift] '' ins Arrangement bei "
                      "Beat 64, 3x alle 16 Beats", o["t3_undo"])
        guard = o["t3_dup_guard"]
        self.assertTrue(guard.startswith("Fehler: Im Zielbereich 11.1.1 bis 14.1.1 (3 Takte)"), guard)
        self.assertIn("liegen schon 2 Clip(s): #3 11.1.1 bis 12.1.1 ''; #4 12.1.1 bis 13.1.1 ''", guard)
        self.assertIn("every_bars=0.5 (1/2) ist kuerzer als der Clip (1 Takt)", o["t3_dup_tight"])
        self.assertIn("erlaubt sind 1 bis 256 Kopien", o["t3_dup_many"])

    def test_delete_arrangement_clip(self):
        o = self.out
        self.assertTrue(o["del_clip_bad"].startswith("Fehler:"), o["del_clip_bad"])
        self.assertIn("beginnt bei Beat 4 (2.1.1), nicht bei 6 (2.3.1). Nichts geloescht. Bei Beat 6 (2.3.1) beginnt #1.",
                      o["del_clip_bad"])
        out = o["del_clip"]
        self.assertIn("[8] 9-Drift: Arrangement-Clip #1 2.3.1 bis 3.3.1 '' geloescht (1 Takt, MIDI, 0 Noten).", out)
        self.assertIn("Clips auf der Spur: 2 -> 1.", out)
        saved = os.listdir(os.path.join(self.tmp, "geloeschte_clips"))
        self.assertEqual(len(saved), 1)
        self.assertIn(saved[0], out)
        self.assertIn("zurueckgenommen: delete_arrangement_clip [9-Drift] Arrangement-Clip #1 '' ab Beat 6 geloescht",
                      o["undo_del_clip"])

    def test_find_parameter_value(self):
        out = self.out["find_value"]
        self.assertIn("[7] KICK SUB | Geraet 0 'DS Kick' | [1] Pitch: Ziel '41.17' -> Rohwert 10", out)
        self.assertIn("Anzeige dort '41.17'", out)
        self.assertIn("Nichts geaendert. Setzen: set_device_parameter(track=7, device_index=0, parameter=1, value=10",
                      out)
        self.assertIn("HINWEIS: Die Anzeige hat ueber die API keine Einheit; verglichen wurde nur die Zahl 41.17.", out)
        self.assertIn("feste Stufen", self.out["find_value_bad"])

    def test_mute_hints(self):
        self.assertIn("Spuraktivator von KICK BODY ist automatisiert", self.out["unmute_auto"])
        self.assertNotIn("automatisiert", self.out["mute_plain"])
        self.assertIn("Meter sitzt vor dem Aktivator", self.out["mute_plain"])

    def test_tool_list(self):
        names = {t.name for t in self.out["__tools__"]}
        self.assertFalse(names & FORBIDDEN_TOOLS, "Dataset-Werkzeuge duerfen nicht mehr existieren")
        for t in self.out["__tools__"]:
            props = (t.inputSchema or {}).get("properties", {})
            self.assertNotIn("user_prompt", props, t.name)
        for needed in ("get_set_overview", "read_saved_set", "get_selection", "watch_meters", "set_mixer", "undo"):
            self.assertIn(needed, names)

    def test_info(self):
        self.assertIn('"variant": "technoa"', self.out["info"])

    def test_overview(self):
        ov = self.out["overview"]
        self.assertIn("SET  136 BPM | 4/4", ov)
        self.assertIn("LOCATOR  1.1.1 INTRO", ov)
        self.assertIn("    [1] KICK BODY (MIDI)", ov)
        self.assertIn("Aktivator: Automation aktiv", ov)
        self.assertIn("Saturator (AUS)", ov)
        self.assertIn("SCHARF (nimmt auf)", ov)
        self.assertIn("[A] REVERB SPACE (Return)", ov)
        self.assertIn("Swing 16ths 66", ov)
        self.assertIn("HINWEIS: Scharfgeschaltet: ATMO PAD", ov)

    def test_track_detail(self):
        self.assertIn("Lautstaerke: -12.0 dB", self.out["track_by_name"])
        self.assertIn("Gruppe: [0] LOW BUS", self.out["track_by_name"])
        self.assertIn("[B] DELAY THROW", self.out["return_b"])

    def test_runs(self):
        runs = self.out["kick_runs"]
        self.assertIn("#0-#63 | 1.1.1 bis 16.4.2 | 64x MIDI 1/16, alle 1/4 | 'KICK'", runs)
        self.assertNotIn("\n#1 ", runs)

    def test_clip_list(self):
        self.assertIn("DEAKTIVIERT", self.out["rumble_clips"])

    def test_notes(self):
        notes = self.out["hat_notes"]
        self.assertIn("Arrangement-Clip #8 'HAT'", notes)
        self.assertIn("F#1 (42) x4", notes)
        self.assertIn("Groove Swing 16ths 66", notes)

    def test_params(self):
        self.assertIn("[2] Cutoff", self.out["params"])
        self.assertIn("Automation aktiv", self.out["params"])

    def test_selection(self):
        self.assertIn("Arrangement #8", self.out["selection"])

    def test_meters(self):
        self.assertIn("Meter ueber 0.5 s", self.out["meters"])

    def test_writes(self):
        self.assertIn('"-10.0 dB"', self.out["mixer"])
        self.assertIn('"10R"', self.out["mixer"])
        self.assertIn('"-30.0 dB"', self.out["mixer"])
        self.assertIn('"name": "Cutoff"', self.out["param_by_name"])
        self.assertIn("Loop vorher:  an, 33.1.1 bis 49.1.2 (64.25 Beats)", self.out["loop"])
        self.assertIn("Loop nachher: an, 33.1.1 bis 49.1.1 (64 Beats)", self.out["loop"])
        self.assertNotIn("ganzen Takten", self.out["loop"])
        self.assertIn('"time": 96.0', self.out["locator"])

    def test_after_is_reread(self):
        # Die Attrappe uebernimmt den Loop-Schalter wie Live erst im naechsten Durchlauf.
        off = self.out["loop_off"]
        self.assertIn("Loop nachher: aus, 33.1.1 bis 49.1.1", off)
        self.assertIn("Direkt nach dem Setzen meldete Live noch Loop an", off)
        self.assertIn("Loop nachher: an", self.out["loop_on"])
        self.assertIn("geht nicht in ganzen Takten auf (Rest 0.25", self.out["loop_crooked"])
        # Plugin-Anzeige folgt einen Durchlauf spaeter.
        hold = self.out["hold"]
        self.assertIn('"display": "On"', hold)
        self.assertIn("Direkt nach dem Setzen meldete Live noch die Anzeige 'Off', einen Durchlauf spaeter "
                      "'On'", hold)
        self.assertIn("zurueckgenommen: set_device_parameter [0] Hold FX: Hold = On", self.out["undo_hold"])

    def test_write_hints(self):
        self.assertIn("Automation von B-Send ist jetzt UEBERSTEUERT", self.out["mixer"])
        self.assertIn("re_enable_automation", self.out["mixer"])
        self.assertIn("Kein Undo-Schritt", self.out["loop"])
        self.assertNotIn("Kein Undo-Schritt", self.out["locator"])
        self.assertIn("Kein Undo-Schritt", self.out["solo"])

    def test_undo_lock(self):
        self.assertIn("Gesperrt: Dieses MCP hat nur 3 Schritt(e)", self.out["undo_too_many"])
        self.assertIn("noch zuruecknehmbar: 2, wiederherstellbar: 1", self.out["undo_ok"])
        self.assertIn("Gesperrt: Nur 1 zurueckgenommene", self.out["redo_too_many"])
        self.assertIn("noch zuruecknehmbar: 3, wiederherstellbar: 0", self.out["redo_ok"])

    def test_playhead_and_browser(self):
        self.assertEqual(self.out["playhead"], "Playhead jetzt bei 9.1.1 (Beat 32).")
        self.assertIn("hinter dem Songende", self.out["playhead_behind_end"])
        self.assertIn("- Utility [Geraet] uri=query:AudioFx#Utility", self.out["browser"])
        self.assertIn("keine Automation mehr uebersteuert", self.out["reenable"])

    def test_errors(self):
        self.assertTrue(self.out["bad_track"].startswith("Fehler: Keine Spur heisst 'GIBTSNICHT'"))
        self.assertIn("mehrdeutig: 1 KICK BODY, 7 KICK SUB", self.out["kick_ambiguous"])
        self.assertTrue(self.out["group_clips"].startswith("Fehler:"), self.out["group_clips"])

    def test_main_thread(self):
        self.assertEqual(fake_live.OFF_THREAD_ACCESS, [])


if __name__ == "__main__":
    unittest.main()
