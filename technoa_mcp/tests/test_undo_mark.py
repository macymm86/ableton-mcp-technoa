"""undo_to_mark ohne Live: Texte fuer Faelle, die die Attrappe nicht nachspielt.

Die Attrappe uebersteuert keine Automation. Am Set (22.10.2026) blieb nach
undo_to_mark genau das stehen: SYNTH Send B uebersteuert, sonst nichts.
"""
from __future__ import annotations

import copy
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))
import server  # noqa: E402
from test_snapshot import base_set  # noqa: E402


class UndoToMarkTextTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="technoa_undo_mark_")
        self.saved = (server._call, server.set_snapshot.take)
        server._UNDO[:] = []
        server._REDO[:] = []
        server._JOURNAL[:] = []
        server._MARKS.clear()

    def tearDown(self):
        server._call, server.set_snapshot.take = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, now, after):
        mark = base_set()
        server._MARKS["m"] = {"undo": 0, "journal": 0, "snap": mark, "folder": self.tmp}
        for track, name in ((1, "SYNTH PULSE"), (0, "HAT CLOSED")):
            server._UNDO.append({"text": "set_mixer [%s]" % name})
            server._JOURNAL.append({"command": "set_mixer", "undo_step": True,
                                    "touched": [{"ref": track, "name": name, "area": "mixer"}]})
        shots = [now, after]
        server.set_snapshot.take = lambda call, label="", extra=None: shots.pop(0)
        server._call = lambda command, params=None: {"undone": params["steps"]} if command == "undo" else {}
        return server.undo_to_mark("m")

    def test_only_overridden_automation_left(self):
        now = base_set()
        now["tracks"]["1"]["info"]["mixer"]["sends"][1].update(value=0.3, display="-30.0 dB",
                                                               automation="overridden")
        now["tracks"]["0"]["info"]["mixer"]["volume"].update(value=0.3, display="-23.0 dB")
        after = base_set()
        after["tracks"]["1"]["info"]["mixer"]["sends"][1].update(value=1.0, display="0.0 dB",
                                                                 automation="overridden")
        after["song"]["re_enable_automation_enabled"] = True
        out = self._run(now, after)
        self.assertIn("zurueckgenommen: set_mixer [HAT CLOSED]", out)
        self.assertIn("PRUEFUNG: Stand der Marke 'm' NICHT ganz erreicht", out)
        self.assertIn("HINWEIS: Uebersteuerte Automation holt undo nicht zurueck: re_enable_automation.", out)
        self.assertNotIn("fremde Undo-Schritte", out)
        self.assertEqual(len(os.listdir(self.tmp)), 1)

    def test_foreign_refused_and_own_listed(self):
        now = base_set()
        now["tracks"]["1"]["info"]["mixer"]["sends"][1].update(value=0.3, display="-30.0 dB",
                                                               automation="overridden")
        now["tracks"]["0"]["info"]["mixer"]["volume"].update(value=0.3, display="-23.0 dB")
        now["tracks"]["2"]["info"]["mixer"]["volume"].update(value=0.2, display="-30.0 dB")
        out = self._run(now, copy.deepcopy(now))
        self.assertTrue(out.startswith("Abgelehnt, nichts zurueckgenommen."), out)
        self.assertIn("  MIXER [2] LOW BUS Pegel: 0.0 dB -> -30.0 dB", out)
        self.assertIn("Vom MCP seit der Marke (2): [0] HAT CLOSED Pegel: 0.0 dB -> -23.0 dB | [1] SYNTH PULSE "
                      "Send B", out)
        self.assertEqual(len(server._UNDO), 2)


class SnapshotDirTest(unittest.TestCase):
    """Ablage der Schnappschuesse: Projekt angegeben, juengstes Projekt, Standard."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="technoa_snapdir_")
        self.old = os.environ.get("ABLETON_MCP_SNAPSHOT_DIR")
        os.environ["ABLETON_MCP_SNAPSHOT_DIR"] = self.tmp

    def tearDown(self):
        if self.old is None:
            os.environ.pop("ABLETON_MCP_SNAPSHOT_DIR", None)
        else:
            os.environ["ABLETON_MCP_SNAPSHOT_DIR"] = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_project_when_empty(self):
        folder, how = server._snapshot_dir(None)
        self.assertEqual(folder, os.path.join(self.tmp, "default"))
        self.assertIn("Projekt 'default'", how)

    def test_newest_project_wins(self):
        for name, age in (("alt", 100), ("neu", 0)):
            os.makedirs(os.path.join(self.tmp, name))
            path = os.path.join(self.tmp, name, "x.json")
            open(path, "w").close()
            stamp = time.time() - age
            os.utime(path, (stamp, stamp))
        folder, how = server._snapshot_dir(None)
        self.assertEqual(folder, os.path.join(self.tmp, "neu"))
        self.assertIn("juengsten", how)

    def test_project_name_cannot_leave_folder(self):
        with self.assertRaises(ValueError):
            server._snapshot_dir("../raus")
        self.assertEqual(server._snapshot_dir("neu_1")[0], os.path.join(self.tmp, "neu_1"))


if __name__ == "__main__":
    unittest.main()
