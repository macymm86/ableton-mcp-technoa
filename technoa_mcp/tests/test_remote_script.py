"""Remote Script gegen die Live-Attrappe, ueber den echten Socket.

Aufruf aus technoa_mcp/:  ..\\venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fake_live  # noqa: E402

SCRIPT = os.path.join(HERE, "..", "remote_script", "AbletonMCP", "__init__.py")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def load_script_module():
    fake_live.install_stubs()
    spec = importlib.util.spec_from_file_location("technoa_remote_script_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Client:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)

    def call(self, command, **params):
        self.sock.sendall(json.dumps({"type": command, "params": params}).encode("utf-8"))
        buf = b""
        while True:
            buf += self.sock.recv(65536)
            try:
                return json.loads(buf.decode("utf-8"))
            except ValueError:
                continue

    def ok(self, command, **params):
        resp = self.call(command, **params)
        if resp.get("status") != "success":
            raise AssertionError("%s: %s" % (command, resp))
        return resp["result"]

    def close(self):
        self.sock.close()


def start_fake_live(mod, port):
    song, app = fake_live.build_demo_set()
    c_instance = fake_live.CInstance(song, app)
    mod.DEFAULT_PORT = port
    script = mod.create_instance(c_instance)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    return song, app, c_instance, script


class RemoteScriptTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = load_script_module()
        cls.port = free_port()
        cls.song, cls.app, cls.c, cls.script = start_fake_live(cls.mod, cls.port)
        cls.client = Client(cls.port)
        fake_live.OFF_THREAD_ACCESS.clear()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.c.run_on_main(cls.script.disconnect)
        cls.c.stop()

    def test_00_script_info(self):
        info = self.client.ok("get_script_info")
        self.assertEqual(info["variant"], "technoa")
        self.assertEqual(info["script_version"], "1.7.1+technoa.9")
        for cmd in ("delete_device", "move_device", "insert_device", "duplicate_device", "delete_arrangement_clip",
                    "find_parameter_value", "set_clip_groove", "get_routing_options", "set_track_routing",
                    "set_sidechain", "create_locator"):
            self.assertIn(cmd, info["capabilities"])
        # Teil 3: Versuchsbefehl entfernt, neue Parameter als Merkmale.
        self.assertNotIn("pulse_parameter", info["capabilities"])
        self.assertEqual(sorted(info["features"]), ["clip_envelopes", "delete_clip_range", "duplicate_repeat", "envelope_digest", "inspect_doc", "note_fields_reported", "song_settings"])
        self.assertTrue(info["undo_grouping"])
        self.assertIn("get_tracks_overview", info["capabilities"])

    def test_01_overview_groups_and_returns(self):
        ov = self.client.ok("get_tracks_overview")
        tracks = {t["name"]: t for t in ov["tracks"]}
        self.assertEqual(tracks["LOW BUS"]["kind"], "group")
        self.assertEqual(tracks["KICK BODY"]["group"], 0)
        self.assertEqual(tracks["HAT CLOSED"]["group"], 3)
        self.assertNotIn("arm", tracks["LOW BUS"])
        self.assertTrue(tracks["ATMO PAD"]["arm"])
        self.assertEqual(tracks["KICK BODY"]["mixer"]["volume"]["display"], "-12.0 dB")
        self.assertEqual(tracks["KICK BODY"]["mixer"]["track_activator"]["automation"], "active")
        self.assertEqual(tracks["KICK BODY"]["devices"][0]["automated"], ["Cutoff"])
        self.assertFalse(tracks["KICK BODY"]["devices"][1]["is_active"])
        self.assertEqual(tracks["RUMBLE"]["arrangement"]["muted_count"], 4)
        self.assertEqual(tracks["SYNTH PULSE"]["mixer"]["sends"][1]["automation"], "active")
        self.assertEqual([r["ref"] for r in ov["returns"]], ["return:0", "return:1"])
        self.assertEqual(ov["master"]["ref"], "master")

    def test_02_song_state(self):
        st = self.client.ok("get_song_state")
        self.assertEqual(st["scale"]["scale_name"], "Major")
        self.assertEqual(st["grooves"][0]["name"], "Swing 16ths 66")
        self.assertEqual([c["name"] for c in st["cue_points"]][:2], ["INTRO", "AUFBAU"])

    def test_03_arrangement_clips_and_digest(self):
        res = self.client.ok("get_arrangement_clips", track=1)
        self.assertEqual(res["clip_count_total"], 64)
        digests = set(c["content_digest"] for c in res["clips"])
        self.assertEqual(len(digests), 1)
        res = self.client.ok("get_arrangement_clips", track="RUMBLE" if False else 2, start_time=0, end_time=20)
        self.assertEqual([c["muted"] for c in res["clips"]], [True, True, True, True, False])

    def test_04_group_has_no_clips(self):
        resp = self.client.call("get_arrangement_clips", track=0)
        self.assertEqual(resp["status"], "error")
        self.assertIn("Gruppe", resp["message"].replace("group", "Gruppe"))

    def test_05_clip_notes_arrangement(self):
        res = self.client.ok("get_clip_notes", track=4, source="arrangement", at_time=33.0)
        self.assertEqual(res["clip"]["index"], 8)
        self.assertEqual(len(res["clip"]["notes"]), 4)
        self.assertEqual(res["clip"]["groove"], "Swing 16ths 66")

    def test_06_device_parameters(self):
        res = self.client.ok("get_device_parameters", track=1, device_index=0, name_filter="cut")
        self.assertEqual([p["name"] for p in res["device"]["parameters"]], ["Cutoff"])
        self.assertEqual(res["device"]["parameters"][0]["automation"], "active")

    def test_07_selection(self):
        sel = self.client.ok("get_selection")
        self.assertEqual(sel["selected_track"]["ref"], 4)
        self.assertEqual(sel["detail_clip"]["source"], "arrangement")
        self.assertEqual(sel["detail_clip"]["track"], 4)
        self.assertEqual(sel["detail_clip"]["clip_index"], 8)
        self.assertEqual(sel["focused_view"], "Arranger")

    def test_08_meters_and_inspect(self):
        m = self.client.ok("get_meters")
        self.assertEqual(m["meters"][-1]["ref"], "master")
        insp = self.client.ok("inspect_lom", path="song.tracks[1].mixer_device")
        self.assertIn("track_activator", insp["attributes"])
        bad = self.client.call("inspect_lom", path="song.__class__")
        self.assertEqual(bad["status"], "error")
        bad = self.client.call("inspect_lom", path="song.create_midi_track")
        self.assertEqual(bad["status"], "error")

    def test_10_set_mixer_db_and_undo_group(self):
        self.c.run_on_main(lambda: self.song.undo_log.clear())
        res = self.client.ok("set_mixer", track=1, volume_db=-6.0, panning=-0.5)
        after = {c["target"]: c["after"]["display"] for c in res["changes"]}
        self.assertEqual(after["volume"], "-6.0 dB")
        self.assertEqual(after["panning"], "25L")
        self.assertEqual(self.c.run_on_main(lambda: list(self.song.undo_log)), ["begin", "end"])
        res = self.client.ok("set_mixer", track=6, arm=False)
        self.assertFalse(res["changes"][0]["after"])
        res = self.client.ok("set_mixer", track="return:1", sends=[{"index": 0, "db": -10}])
        self.assertEqual(res["changes"][0]["after"]["display"], "-10.0 dB")

    def test_11_arm_on_group_is_refused(self):
        resp = self.client.call("set_mixer", track=0, arm=True)
        self.assertEqual(resp["status"], "error")

    def test_12_clip_muted_and_names(self):
        res = self.client.ok("set_clip_muted", track=2, source="arrangement", clip_indices=[0, 1], muted=False)
        self.assertEqual([c["after"] for c in res["clips"]], [False, False])
        res = self.client.ok("set_clip_name", track=2, source="arrangement", clip_index=0, name="RUMBLE A")
        self.assertEqual(res["old_name"], "RUMBLE")

    def test_13_notes_roundtrip(self):
        res = self.client.ok("add_notes_to_clip", track=5, source="arrangement", clip_index=0,
                             notes=[{"pitch": 60, "start_time": 2.0, "duration": 0.5, "velocity": 99}])
        self.assertEqual(res["notes_after"], res["notes_before"] + 1)
        self.assertEqual(res["method"], "add_new_notes")
        self.assertEqual(res["not_written"], [])
        res = self.client.ok("clear_notes_from_clip", track=5, source="arrangement", clip_index=0)
        self.assertEqual(res["notes_after"], 0)

    def test_13b_note_fields_written_and_reported(self):
        # Teil 3: velocity_deviation und release_velocity gehen an Live; was
        # nicht geschrieben wird, steht mit Grund in not_written.
        notes = [{"pitch": 48, "start_time": 0.0, "duration": 0.5, "velocity": 90, "probability": 0.5,
                  "velocity_deviation": 20, "release_velocity": 30},
                 {"pitch": 55, "start_time": 1.0, "duration": 0.5, "velocity": 80, "velocity_deviation": -10}]
        self.client.ok("clear_notes_from_clip", track=5, source="arrangement", clip_index=0)
        res = self.client.ok("add_notes_to_clip", track=5, source="arrangement", clip_index=0, notes=notes)
        self.assertEqual(res["method"], "add_new_notes")
        self.assertEqual(res["not_written"], [])
        self.assertEqual(res["fields_written"][-3:], ["probability", "velocity_deviation", "release_velocity"])
        back = self.client.ok("get_clip_notes", track=5, source="arrangement", clip_index=0)["clip"]["notes"]
        self.assertEqual([(n["velocity_deviation"], n["release_velocity"], n["probability"]) for n in back],
                         [(20.0, 30.0, 0.5), (-10.0, 64.0, 1.0)])
        # Unbekanntes Feld: gemeldet, der Rest geschrieben.
        res = self.client.ok("add_notes_to_clip", track=5, source="arrangement", clip_index=0,
                             notes=[{"pitch": 60, "start_beat": 2.0, "duration": 0.25}])
        self.assertEqual(res["not_written"], [{"field": "start_beat", "notes": 1, "reason": "unbekanntes Feld"}])
        # Eine Live-Version, die velocity_deviation ablehnt: nur dieses Feld faellt weg.
        fake_live.MidiNoteSpecification.REJECT = {"velocity_deviation"}
        try:
            res = self.client.ok("add_notes_to_clip", track=5, source="arrangement", clip_index=0, notes=notes)
        finally:
            fake_live.MidiNoteSpecification.REJECT = set()
        self.assertEqual(res["method"], "add_new_notes")
        self.assertEqual([(w["field"], w["notes"]) for w in res["not_written"]], [("velocity_deviation", 2)])
        self.assertIn("von Live abgelehnt: MidiNoteSpecification() got an unexpected keyword argument "
                      "'velocity_deviation'", res["not_written"][0]["reason"])
        self.assertIn("release_velocity", res["fields_written"])
        # add_new_notes scheitert: Rueckfall auf set_notes, alle Zusatzfelder gemeldet.
        original = fake_live.Clip.add_new_notes

        def broken(clip, specs):
            raise RuntimeError("kaputt")
        fake_live.Clip.add_new_notes = broken
        try:
            res = self.client.ok("add_notes_to_clip", track=5, source="arrangement", clip_index=0, notes=notes)
        finally:
            fake_live.Clip.add_new_notes = original
        self.assertEqual((res["method"], res["fallback_reason"]), ("set_notes", "kaputt"))
        self.assertEqual([w["field"] for w in res["not_written"]],
                         ["probability", "velocity_deviation", "release_velocity"])
        self.assertIn("Rueckfall auf set_notes", res["not_written"][0]["reason"])
        self.client.ok("clear_notes_from_clip", track=5, source="arrangement", clip_index=0)

    def test_13c_digest_sees_extra_fields_only_when_set(self):
        # Clips ohne Zusatzwerte behalten den Abdruck von technoa.7, sonst meldete
        # snapshot_diff nach dem Script-Wechsel jeden MIDI-Clip als geaendert.
        res = self.client.ok("get_arrangement_clips", track=1, start_time=0, end_time=1)
        old = "m1-%08x" % self.mod._fingerprint("36,0.0000,0.2500,110.00,0")
        self.assertEqual(res["clips"][0]["content_digest"], old)
        clip = self.c.run_on_main(lambda: self.song.tracks[1].arrangement_clips[0])
        self.c.run_on_main(lambda: setattr(clip._notes[0], "velocity_deviation", 12.0))
        try:
            res = self.client.ok("get_arrangement_clips", track=1, start_time=0, end_time=1)
            self.assertNotEqual(res["clips"][0]["content_digest"], old)
        finally:
            self.c.run_on_main(lambda: setattr(clip._notes[0], "velocity_deviation", 0.0))

    def test_14_locator_and_loop(self):
        # Die Attrappe uebernimmt den Playhead wie Live erst im naechsten
        # Durchlauf. Die alte Fassung setzte den Locator deshalb an die alte
        # Playhead-Position.
        res = self.client.ok("create_locator", name="TEST", time=96.0)
        self.assertTrue(res["created"])
        self.assertEqual(res["time"], 96.0)
        cues = self.c.run_on_main(lambda: [(c.time, c.name) for c in self.song.cue_points])
        self.assertIn((96.0, "TEST"), cues)
        self.assertFalse([t for t, _ in cues if abs(t - 190.66) < 1e-3], cues)
        self.assertAlmostEqual(self.c.run_on_main(lambda: self.song.current_song_time), 190.66)
        res = self.client.ok("create_locator", name="TEST2", time=96.0)
        self.assertFalse(res["created"])
        self.assertEqual(res["name"], "TEST2")
        res = self.client.ok("set_loop", loop_start=128.0, loop_length=64.0)
        self.assertEqual(res["after"]["loop_length"], 64.0)

    def test_14b_locator_behind_song_end_or_while_playing(self):
        resp = self.client.call("create_locator", name="X", time=600.0)
        self.assertEqual(resp["status"], "error")
        self.assertIn("Songende", resp["message"])
        self.c.run_on_main(lambda: setattr(self.song, "is_playing", True))
        try:
            resp = self.client.call("create_locator", name="X", time=32.0)
        finally:
            self.c.run_on_main(lambda: setattr(self.song, "is_playing", False))
        self.assertEqual(resp["status"], "error")
        self.assertIn("laufendem Transport", resp["message"])
        cues = self.c.run_on_main(lambda: [c.name for c in self.song.cue_points])
        self.assertNotIn("X", cues)

    def test_14c_undo_check(self):
        # Neuer Undo-Schritt leert die Redo-Liste: erkannt.
        self.c.run_on_main(lambda: setattr(self.song, "can_redo", True))
        res = self.client.ok("set_tempo", tempo=130.0)
        self.assertEqual(res["undo_check"]["undo_step"], True)
        # Kein Schritt (wie solo/arm/Loop in Live): Redo-Liste bleibt.
        def no_step():
            self.song.can_redo = True
            self.song.next_step_is_undoable = False
        self.c.run_on_main(no_step)
        try:
            res = self.client.ok("set_mixer", track=1, solo=True)
        finally:
            self.c.run_on_main(lambda: setattr(self.song, "next_step_is_undoable", True))
        self.assertEqual(res["undo_check"]["undo_step"], False)
        # Ohne Redo-Liste vorher laesst sich nichts sagen.
        self.c.run_on_main(lambda: setattr(self.song, "can_redo", False))
        res = self.client.ok("set_tempo", tempo=136.0)
        self.assertIsNone(res["undo_check"]["undo_step"])
        self.client.ok("set_mixer", track=1, solo=False)

    def test_14d_automation_and_back_to_arrangement(self):
        self.c.run_on_main(lambda: setattr(self.song, "re_enable_automation_enabled", True))
        st = self.client.ok("get_song_state")
        self.assertTrue(st["re_enable_automation_enabled"])
        res = self.client.ok("re_enable_automation")
        self.assertTrue(res["was_overridden"])
        self.assertFalse(self.client.ok("get_song_state")["re_enable_automation_enabled"])
        self.c.run_on_main(lambda: setattr(self.song, "back_to_arranger", True))
        res = self.client.ok("back_to_arrangement")
        self.assertTrue(res["was_lit"])
        self.assertFalse(self.c.run_on_main(lambda: self.song.back_to_arranger))
        self.assertNotIn("undo_check", res)

    def test_14e_session_clip_state(self):
        self.client.ok("create_clip", track=1, clip_index=2, length=4.0)
        self.client.ok("fire_clip", track=1, clip_index=2)
        info = self.client.ok("get_track_info", track=1)
        slot = [c for c in info["session_clips"] if c["index"] == 2][0]
        self.assertTrue(slot["is_triggered"])
        self.client.ok("stop_clip", track=1, clip_index=2)
        self.client.ok("delete_clip", track=1, clip_index=2)

    def test_14f_served_by_update_display(self):
        before = self.client.ok("get_script_info")["ticks"]["update_display"]
        t0 = time.monotonic()
        self.client.ok("get_session_info")
        elapsed = time.monotonic() - t0
        after = self.client.ok("get_script_info")["ticks"]["update_display"]
        self.assertGreater(after, before)
        # Attrappe: update_display alle 20 ms. Ein Lesebefehl braucht einen
        # Durchlauf, nicht mehrere.
        self.assertLess(elapsed, 0.2)

    def test_14g_fallback_without_update_display(self):
        self.c.update_display_enabled = False
        try:
            time.sleep(0.6)
            res = self.client.ok("get_session_info")
            self.assertEqual(res["tempo"], 136.0)
            self.assertGreater(self.client.ok("get_script_info")["ticks"]["fallback"], 0)
        finally:
            self.c.update_display_enabled = True

    def test_14h_two_requests_in_one_packet(self):
        payload = (json.dumps({"type": "get_session_info", "params": {}})
                   + json.dumps({"type": "get_script_info", "params": {}})).encode("utf-8")
        self.client.sock.sendall(payload)
        buf, answers = b"", []
        decoder = json.JSONDecoder()
        while len(answers) < 2:
            buf += self.client.sock.recv(65536)
            text, pos = buf.decode("utf-8"), 0
            answers = []
            while pos < len(text):
                try:
                    obj, pos = decoder.raw_decode(text, pos)
                except ValueError:
                    break
                answers.append(obj)
        self.assertIn("tempo", answers[0]["result"])
        self.assertEqual(answers[1]["result"]["variant"], "technoa")

    def test_14i_fingerprint_is_stable(self):
        # FNV-1a-Referenzwert: unabhaengig von Pythons hash()-Zufall.
        self.assertEqual(self.mod._fingerprint("a"), 0xE40C292C)
        res = self.client.ok("get_arrangement_clips", track=2, start_time=0, end_time=8)
        self.assertTrue(res["clips"][0]["content_digest"].startswith("a-"))

    def test_15_undo_not_grouped(self):
        self.c.run_on_main(lambda: self.song.undo_log.clear())
        self.client.ok("undo")
        self.assertEqual(self.c.run_on_main(lambda: list(self.song.undo_log)), ["undo"])

    def test_16_errors_are_readable(self):
        resp = self.client.call("get_track_info", track=99)
        self.assertEqual(resp["status"], "error")
        self.assertIn("Spur 99", resp["message"])
        resp = self.client.call("nonsense")
        self.assertIn("Unbekannter Befehl", resp["message"])
        resp = self.client.call("get_track_info", track="return:5")
        self.assertIn("Return 5", resp["message"])

    def test_17_browser(self):
        res = self.client.ok("get_browser_items_at_path", path="audio_effects")
        self.assertEqual(res["items"][0]["name"], "Utility")
        res = self.client.ok("load_browser_item", track=1, item_uri="query:AudioFx#Utility")
        self.assertTrue(res["loaded"])

    def chain(self, track):
        return self.c.run_on_main(lambda: [d.name for d in self.song.tracks[track].devices])

    def test_18_insert_move_duplicate_delete(self):
        # RUMBLE (Spur 2) hat in der Attrappe keine Geraete.
        self.assertEqual(self.chain(2), [])
        self.c.run_on_main(lambda: self.song.undo_log.clear())
        res = self.client.ok("insert_device", track=2, device_name="Utility")
        self.assertEqual((res["new_index"], res["chain_after"]), (0, ["Utility"]))
        self.assertEqual(self.c.run_on_main(lambda: list(self.song.undo_log)), ["begin", "end"])
        res = self.client.ok("insert_device", track=2, device_name="EQ Eight", position=0)
        self.assertEqual((res["new_index"], res["chain_after"]), (0, ["EQ Eight", "Utility"]))
        resp = self.client.call("insert_device", track=2, device_name="Serum 2")
        self.assertIn("Live-eigene Geraete", resp["message"])
        self.assertEqual(self.chain(2), ["EQ Eight", "Utility"])

        # Nach hinten: Position danach 1 heisst fuer Live Luecke 2.
        res = self.client.ok("move_device", track=2, device_index=0, expect_name="EQ Eight", target_index=1)
        self.assertEqual((res["new_index"], res["gap"]), (1, 2))
        self.assertEqual(self.chain(2), ["Utility", "EQ Eight"])
        res = self.client.ok("move_device", track=2, device_index=1, expect_name="eq eight", target_index=0)
        self.assertEqual((res["new_index"], res["gap"]), (0, 0))
        self.assertEqual(self.chain(2), ["EQ Eight", "Utility"])

        resp = self.client.call("move_device", track=2, device_index=0, expect_name="Utility", target_index=1)
        self.assertIn("liegt an Position 0 'EQ Eight', nicht 'Utility'", resp["message"])
        resp = self.client.call("move_device", track=2, device_index=0, target_index=0)
        self.assertIn("schon an Position 0", resp["message"])
        # KICK BODY: Saturator (Audioeffekt) darf nicht vor Serum (Instrument).
        resp = self.client.call("move_device", track=1, device_index=1, expect_name="Saturator", target_index=0)
        self.assertIn("kann laut Live nicht an Position 0", resp["message"])
        self.assertEqual(self.chain(1)[:2], ["Serum 2", "Saturator"])

        # Auf eine andere Spur und zurueck.
        res = self.client.ok("move_device", track=2, device_index=1, target_track=0, target_index=0)
        self.assertEqual((res["new_index"], res["target_chain_after"]), (0, ["Utility", "Insight 2"]))
        self.assertEqual(self.chain(2), ["EQ Eight"])
        self.client.ok("move_device", track=0, device_index=0, expect_name="Utility", target_track=2, target_index=1)
        self.assertEqual(self.chain(0), ["Insight 2"])
        self.assertEqual(self.chain(2), ["EQ Eight", "Utility"])

        res = self.client.ok("duplicate_device", track=2, device_index=0, expect_name="EQ Eight")
        self.assertEqual((res["new_index"], res["chain_after"]), (1, ["EQ Eight", "EQ Eight", "Utility"]))

        resp = self.client.call("delete_device", track=2, device_index=1, expect_name="Utility")
        self.assertEqual(resp["status"], "error")
        self.assertEqual(self.chain(2), ["EQ Eight", "EQ Eight", "Utility"])
        res = self.client.ok("delete_device", track=2, device_index=1, expect_name="EQ Eight")
        self.assertEqual(res["deleted"]["name"], "EQ Eight")
        self.assertEqual(res["parameters"][0]["name"], "Device On")
        self.assertEqual(res["chain_after"], ["EQ Eight", "Utility"])
        self.client.ok("delete_device", track=2, device_index=1, expect_name="Utility")
        self.client.ok("delete_device", track=2, device_index=0, expect_name="EQ Eight")
        self.assertEqual(self.chain(2), [])

    def test_19_input_meters(self):
        def setup():
            self.song.tracks[2].input_meter_left = 0.58
            self.song.tracks[2].input_meter_right = 0.57
        self.c.run_on_main(setup)
        res = self.client.ok("get_meters")
        rumble = [m for m in res["meters"] if m["ref"] == 2][0]
        self.assertEqual((rumble["in_left"], rumble["in_right"]), (0.58, 0.57))

    def test_20_delete_arrangement_clip_guarded(self):
        # RUMBLE (Spur 2): 16 Clips a 4 Beats, #5 beginnt bei Beat 20.
        self.c.run_on_main(lambda: self.song.undo_log.clear())
        resp = self.client.call("delete_arrangement_clip", track=2, clip_index=5, expect_start=24.0)
        self.assertEqual(resp["status"], "error")
        self.assertIn("beginnt bei Beat 20, nicht bei 24", resp["message"])
        self.assertIn("Bei Beat 24 beginnt #6", resp["message"])
        resp = self.client.call("delete_arrangement_clip", track=2, clip_index=5)
        self.assertIn("Pflicht", resp["message"])
        resp = self.client.call("delete_arrangement_clip", track=2, clip_index=5, expect_start=20.0,
                                expect_name="FALSCH")
        self.assertIn("heisst", resp["message"])
        resp = self.client.call("delete_arrangement_clip", track=0, clip_index=0, expect_start=0.0)
        self.assertIn("keine Arrangement-Clips", resp["message"])
        self.assertEqual(self.c.run_on_main(lambda: len(self.song.tracks[2].arrangement_clips)), 16)
        self.c.run_on_main(lambda: self.song.undo_log.clear())
        res = self.client.ok("delete_arrangement_clip", track=2, clip_index=5, expect_start=20.0,
                             expect_name="rumble")
        self.assertEqual((res["clip_count_before"], res["clip_count_after"]), (16, 15))
        self.assertEqual(res["deleted"]["start_time"], 20.0)
        self.assertTrue(res["deleted"]["content_digest"].startswith("a-"))
        self.assertEqual(self.c.run_on_main(lambda: list(self.song.undo_log)), ["begin", "end"])
        starts = self.c.run_on_main(lambda: [c.start_time for c in self.song.tracks[2].arrangement_clips])
        self.assertNotIn(20.0, starts)
        # MIDI-Clip: Noten gehen mit in die Antwort (Rueckweg).
        res = self.client.ok("delete_arrangement_clip", track=4, clip_index=15, expect_start=60.0)
        self.assertEqual(len(res["deleted"]["notes"]), 4)

    def test_21_find_parameter_value(self):
        # KICK SUB (Spur 7), DS Kick, Parameter 1 = Pitch (0 ist Device On).
        res = self.client.ok("find_parameter_value", track=7, device_index=0, parameter_index=1, target="41.17")
        self.assertEqual(res["display"], "41.17")
        self.assertAlmostEqual(res["value"], 105.1, delta=0.3)
        self.assertEqual(res["direction"], "steigend")
        # Nichts gesetzt.
        self.assertEqual(self.c.run_on_main(lambda: self.song.tracks[7].devices[0].parameters[1].value), 105.1)
        res = self.client.ok("find_parameter_value", track=7, device_index=0, parameter_index=1, target="60")
        self.assertEqual(res["display"], "60.00")
        # Wie am Set: Anzeige ohne Einheit, Ziel mit Einheit -> Script lehnt ab, der Server faengt das ab.
        resp = self.client.call("find_parameter_value", track=7, device_index=0, parameter_index=1, target="41.17 Hz")
        self.assertIn("zeigt ''", resp["message"])
        res = self.client.ok("find_parameter_value", track=7, device_index=0, parameter_index=2, target="27.6 %")
        self.assertEqual(res["display"], "27.6 %")
        for target, text in (("500", "ausserhalb"), ("41 dB", "Einheit passt nicht"), ("hoch", "nicht lesbar")):
            resp = self.client.call("find_parameter_value", track=7, device_index=0, parameter_index=1, target=target)
            self.assertIn(text, resp["message"], target)
        resp = self.client.call("find_parameter_value", track=7, device_index=0, parameter_index=0, target="1")
        self.assertIn("feste Stufen (0=Off / 1=On)", resp["message"])

    def test_22_display_parser(self):
        p = self.mod._parse_display
        self.assertEqual(p("41.17 Hz"), (41.17, "hz"))
        self.assertEqual(p("1.20 kHz"), (1200.0, "hz"))
        self.assertEqual(p("1.5 s"), (1500.0, "ms"))
        self.assertEqual(p("-6.0 dB"), (-6.0, "db"))
        self.assertEqual(p("-inf dB"), (float("-inf"), "db"))
        self.assertEqual(p("35 %"), (35.0, "%"))
        self.assertIsNone(p("On"))
        self.assertIsNone(p("C3"))
        self.assertIsNone(p("C"))

    def test_23_insert_device_position_text(self):
        # KICK BODY: Utility vor Serum (Instrument) lehnt Live selbst ab.
        resp = self.client.call("insert_device", track=1, device_name="Utility", position=0)
        self.assertIn("Live lehnt Position 0", resp["message"])
        self.assertNotIn("Live-eigene", resp["message"])

    def test_24_overwrite_and_arm_like_live(self):
        # Attrappe wie Live: Kopie ueber einen Clip ersetzt ihn.
        self.client.ok("create_clip", track=5, clip_index=0, length=4.0)
        before = self.c.run_on_main(lambda: [(c.start_time, c.end_time) for c in self.song.tracks[5].arrangement_clips])
        self.assertEqual(before, [(0.0, 16.0)])
        self.client.ok("duplicate_session_clip_to_arrangement", track=5, clip_index=0, destination_time=4.0)
        after = self.c.run_on_main(lambda: [(c.start_time, c.end_time) for c in self.song.tracks[5].arrangement_clips])
        self.assertEqual(after, [(0.0, 4.0), (4.0, 8.0), (8.0, 16.0)])
        # Teil 3: mehrere Kopien in einem Undo-Schritt, Abstand frei waehlbar.
        self.c.run_on_main(lambda: self.song.undo_log.clear())
        res = self.client.ok("duplicate_session_clip_to_arrangement", track=5, clip_index=0, destination_time=32.0,
                             repeat=3, step=8.0)
        self.assertEqual(res["destinations"], [32.0, 40.0, 48.0])
        self.assertEqual(self.c.run_on_main(lambda: list(self.song.undo_log)), ["begin", "end"])
        after = self.c.run_on_main(lambda: [(c.start_time, c.end_time) for c in self.song.tracks[5].arrangement_clips
                                            if c.start_time >= 32.0])
        self.assertEqual(after, [(32.0, 36.0), (40.0, 44.0), (48.0, 52.0)])
        res = self.client.ok("duplicate_session_clip_to_arrangement", track=5, clip_index=0, destination_time=64.0,
                             repeat=2)
        self.assertEqual((res["destinations"], res["step"]), ([64.0, 68.0], 4.0))
        resp = self.client.call("duplicate_session_clip_to_arrangement", track=5, clip_index=0, destination_time=80.0,
                                repeat=2, step=2.0)
        self.assertIn("kuerzer als der Clip", resp["message"])
        resp = self.client.call("duplicate_session_clip_to_arrangement", track=5, clip_index=0, destination_time=80.0,
                                repeat=0)
        self.assertIn("erlaubt sind 1 bis 256", resp["message"])
        n = self.c.run_on_main(lambda: len([c for c in self.song.tracks[5].arrangement_clips if c.start_time >= 80.0]))
        self.assertEqual(n, 0)
        self.client.ok("delete_clip", track=5, clip_index=0)
        info = self.client.ok("get_track_info", track=7)
        self.assertIn("implicit_arm", info)
        self.assertFalse(info["arm"])

    def test_25_clip_groove_range(self):
        # HAT CLOSED (Spur 4): Clips a 4 Beats ab Beat 0, alle mit "Swing 16ths 66"; den
        # letzten (Beat 60) hat test_20 geloescht, es bleiben 15.
        hat = self.c.run_on_main(lambda: self.song.tracks[4])
        self.c.run_on_main(lambda: self.song.undo_log.clear())
        res = self.client.ok("set_clip_groove", track=4, start_time=16.0, end_time=32.0, groove=None)
        self.assertEqual([c["start_time"] for c in res["changed"]], [16.0, 20.0, 24.0, 28.0])
        self.assertEqual({c["before"] for c in res["changed"]}, {"Swing 16ths 66"})
        self.assertEqual({c.get("after") for c in res["changed"]}, {None})
        self.assertEqual(self.c.run_on_main(lambda: list(self.song.undo_log)), ["begin", "end"])
        self.assertEqual(self.c.run_on_main(lambda: [c.groove is None for c in hat.arrangement_clips]).count(True), 4)
        resp = self.client.call("set_clip_groove", track=4, start_time=16.0, end_time=32.0, groove=None)
        self.assertIn("haben schon keinen Groove. Nichts geaendert", resp["message"])
        # Wieder an: 4 geaendert, 11 hatten ihn schon.
        res = self.client.ok("set_clip_groove", track=4, start_time=None, end_time=None, groove="Swing 16ths 66")
        self.assertEqual(len(res["changed"]), 4)
        self.assertEqual(len(res["already"]), 11)
        # Clips, die vor dem Bereich beginnen und hineinreichen, zaehlen nicht mit.
        res = self.client.ok("set_clip_groove", track=4, start_time=18.0, end_time=26.0, groove=None)
        self.assertEqual([c["start_time"] for c in res["changed"]], [20.0, 24.0])
        self.assertEqual(res["reaching_in"], [16.0])
        self.client.ok("set_clip_groove", track=4, start_time=18.0, end_time=26.0, groove="Swing 16ths 66")
        resp = self.client.call("set_clip_groove", track=5, start_time=1.0, end_time=3.0, groove=None)
        self.assertIn("kein Arrangement-Clip (1 beginnen davor und reichen hinein)", resp["message"])
        resp = self.client.call("set_clip_groove", track=4, start_time=0.0, end_time=8.0, groove="Shuffle")
        self.assertIn("nicht im Groove-Pool (dort: 'Swing 16ths 66')", resp["message"])
        resp = self.client.call("set_clip_groove", track=3, start_time=0.0, end_time=8.0, groove=None)
        self.assertIn("group-Spur", resp["message"])
        resp = self.client.call("set_clip_groove", track=4, start_time=0.0, end_time=8.0)
        self.assertIn("groove fehlt", resp["message"])
        # Entfernen geht in Live nicht ueber None (22.10.2026); der Rueckweg heisst "del".
        res = self.client.ok("set_clip_groove", track=4, start_time=0.0, end_time=4.0, groove=None)
        self.assertEqual(res["method"], ["del"])
        self.client.ok("set_clip_groove", track=4, start_time=0.0, end_time=4.0, groove="Swing 16ths 66")
        fake_live.Clip.DEL_GROOVE_WORKS = False
        try:
            resp = self.client.call("set_clip_groove", track=4, start_time=0.0, end_time=4.0, groove=None)
        finally:
            fake_live.Clip.DEL_GROOVE_WORKS = True
        self.assertIn("Live hat keinen der 1 Clips angenommen (clip.groove = None: Python argument types in / "
                      "del clip.groove: can't delete attribute). Nichts geaendert. Entfernen geht ueber die API dieser "
                      "Live-Version nicht", resp["message"])
        self.assertTrue(self.c.run_on_main(lambda: all(c.groove is not None for c in hat.arrangement_clips)))

    def test_26_track_routing(self):
        opts = self.client.ok("get_routing_options", track=4)
        self.assertEqual(opts["output"]["type"]["name"], "DRUMS")
        self.assertEqual(opts["output"]["type"]["track"], 3)
        names = [t["name"] for t in opts["output"]["types"]]
        self.assertIn("Main", names)
        self.assertIn("Ext. Out", names)
        self.assertEqual(opts["input"]["type"]["name"], "All Ins")
        self.assertIn("Ch. 1", opts["input"]["channels"])
        self.c.run_on_main(lambda: self.song.undo_log.clear())
        res = self.client.ok("set_track_routing", track=4, output_type="Main")
        ch = res["changes"][0]
        self.assertEqual((ch["side"], ch["before"]["type"], ch["after"]["type"]), ("output", "DRUMS", "Main"))
        self.assertEqual(self.c.run_on_main(lambda: list(self.song.undo_log)), ["begin", "end"])
        resp = self.client.call("set_track_routing", track=4, output_type="main")
        self.assertIn("schon so geroutet", resp["message"])
        resp = self.client.call("set_track_routing", track=4, output_type="Nirgendwo", input_type="Ch. 1")
        self.assertIn("Ausgang 'Nirgendwo' gibt es hier nicht. Waehlbar:", resp["message"])
        self.assertEqual(self.c.run_on_main(lambda: self.song.tracks[4].output_routing_type.display_name), "Main")
        # Mehrdeutiger Namensteil: KICK BODY und KICK SUB.
        resp = self.client.call("set_track_routing", track=2, input_type="KICK")
        self.assertIn("mehrdeutig", resp["message"])
        # RUMBLE (Audio) hoert KICK BODY ab: Art und Kanal in einem Schritt.
        res = self.client.ok("set_track_routing", track=2, input_type="KICK BODY", input_type_track=1,
                             input_channel="Post FX")
        self.assertEqual(res["changes"][0]["after"], {"type": "KICK BODY", "channel": "Post FX"})
        self.assertEqual(res["changes"][0]["warnings"], [])
        # Nur der Kanal: vorher pruefbar, also ein Fehler ohne Aenderung.
        resp = self.client.call("set_track_routing", track=2, input_channel="Kanal 9")
        self.assertIn("Eingangskanal 'Kanal 9' gibt es hier nicht", resp["message"])
        # Neue Art mit unpassendem Kanal: Art gesetzt, Kanal als Warnung.
        res = self.client.ok("set_track_routing", track=2, input_type="Ext. In", input_channel="Post FX")
        self.assertEqual(res["changes"][0]["after"]["type"], "Ext. In")
        self.assertIn("Kanal nicht gesetzt", res["changes"][0]["warnings"][0])
        self.assertEqual(res["changes"][0]["after"]["channel"], "1")
        self.client.ok("set_track_routing", track=4, output_type="DRUMS")
        info = self.client.ok("get_track_info", track=4)
        self.assertEqual(info["routing"]["output_type"], "DRUMS")

    def test_27_sidechain(self):
        dev = self.client.ok("get_device_parameters", track=3, device_index=0)["device"]
        self.assertEqual(dev["sidechain"], {"type": "KICK BODY", "channel": "Post FX", "on": True})
        ov = self.client.ok("get_tracks_overview")
        drums = [t for t in ov["tracks"] if t["name"] == "DRUMS"][0]
        self.assertEqual(drums["devices"][0]["sidechain"]["type"], "KICK BODY")
        self.assertNotIn("sidechain", ov["tracks"][1]["devices"][0])
        opts = self.client.ok("get_routing_options", track=3, device_index=0)
        names = [t["name"] for t in opts["input"]["types"]]
        self.assertIn("HAT CLOSED", names)
        self.assertNotIn("DRUMS", names)
        self.assertEqual(opts["input"]["channels"], ["Pre FX", "Post FX", "Post Mixer"])
        self.c.run_on_main(lambda: self.song.undo_log.clear())
        res = self.client.ok("set_sidechain", track=3, device_index=0, expect_name="compressor",
                             input_type="KICK SUB", input_channel="Pre FX")
        self.assertEqual(res["before"]["type"], "KICK BODY")
        self.assertEqual(res["after"], {"type": "KICK SUB", "channel": "Pre FX", "on": True})
        self.assertEqual(self.c.run_on_main(lambda: list(self.song.undo_log)), ["begin", "end"])
        res = self.client.ok("set_sidechain", track=3, device_index=0, enable=False)
        self.assertFalse(res["after"]["on"])
        self.assertEqual(res["changes"], [])
        resp = self.client.call("set_sidechain", track=3, device_index=0, enable=False, input_type="KICK SUB")
        self.assertIn("schon so eingestellt", resp["message"])
        resp = self.client.call("set_sidechain", track=1, device_index=0, input_type="KICK SUB")
        self.assertIn("keinen Sidechain-Eingang", resp["message"])
        resp = self.client.call("set_sidechain", track=3, device_index=0, expect_name="Gate", enable=True)
        self.assertIn("nicht 'Gate'", resp["message"])
        resp = self.client.call("set_sidechain", track=3, input_type="KICK SUB")
        self.assertIn("device_index fehlt", resp["message"])
        self.client.ok("set_sidechain", track=3, device_index=0, input_type="KICK BODY", input_channel="Post FX",
                       enable=True)
        dev = self.client.ok("get_device_parameters", track=3, device_index=0)["device"]
        self.assertEqual(dev["sidechain"], {"type": "KICK BODY", "channel": "Post FX", "on": True})

    def test_28_pulse_parameter_removed(self):
        # Versuch am Set beendet (22.10.2026), Befehl mit technoa.8 entfernt.
        resp = self.client.call("pulse_parameter", track=1, device_index=1, parameter_index=1, high=1.0)
        self.assertEqual(resp["status"], "error")
        self.assertIn("Unbekannter Befehl 'pulse_parameter'", resp["message"])

    def test_99_everything_on_main_thread(self):
        self.assertEqual(fake_live.OFF_THREAD_ACCESS, [], "Live-API ausserhalb des Hauptthreads benutzt")
        self.assertFalse([line for line in self.c.log if "TASK-FEHLER" in line], self.c.log)


if __name__ == "__main__":
    unittest.main()
