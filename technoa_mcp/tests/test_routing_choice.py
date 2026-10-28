"""Auswahl einer Routing-Moeglichkeit im Server, ohne Live.

Ob Live 12 zu jeder Moeglichkeit die Spur (RoutingType.attached_object)
liefert, ist vor dem Test am Set ungeprueft. Fehlt sie, muss eine Spurnummer
ueber den Spurnamen trotzdem treffen.
"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import server  # noqa: E402

OVERVIEW = {"tracks": [{"ref": 1, "name": "KICK BODY"}, {"ref": 2, "name": "KICK_PRINT_v1"},
                       {"ref": 7, "name": "KICK SUB"}],
            "returns": [{"ref": "return:0", "name": "REVERB SPACE"}],
            "master": {"ref": "master", "name": "Main"}}


class RoutingChoiceTest(unittest.TestCase):

    def setUp(self):
        self.saved = server._call
        server._forget()
        server._call = lambda command, params=None: OVERVIEW

    def tearDown(self):
        server._call = self.saved
        server._forget()

    def test_without_attached_track(self):
        opts = [{"name": "No Input"}, {"name": "Ext. In"}, {"name": "KICK BODY"}, {"name": "KICK_PRINT_v1"},
                {"name": "KICK SUB"}, {"name": "REVERB SPACE"}]
        self.assertEqual(server._routing_choice(opts, 2, "Quelle")["name"], "KICK_PRINT_v1")
        self.assertEqual(server._routing_choice(opts, "A", "Quelle")["name"], "REVERB SPACE")
        self.assertEqual(server._routing_choice(opts, "kick body", "Quelle")["name"], "KICK BODY")
        self.assertEqual(server._routing_choice(opts, "print", "Quelle")["name"], "KICK_PRINT_v1")
        with self.assertRaisesRegex(ValueError, "mehrdeutig: KICK BODY, KICK_PRINT_v1, KICK SUB"):
            server._routing_choice(opts, "kick", "Quelle")
        with self.assertRaisesRegex(ValueError, "'HAT' gibt es hier nicht. Waehlbar: No Input, Ext. In"):
            server._routing_choice(opts, "HAT", "Quelle")

    def test_attached_track_wins_over_equal_names(self):
        # Zwei Spuren gleichen Namens: Nur die Spurnummer unterscheidet sie.
        opts = [{"name": "Main", "track": "master"}, {"name": "KICK", "track": 1}, {"name": "KICK", "track": 7}]
        self.assertEqual(server._routing_choice(opts, 7, "Ausgang"), {"name": "KICK", "track": 7})
        self.assertEqual(server._routing_choice(opts, "main", "Ausgang")["name"], "Main")
        with self.assertRaisesRegex(ValueError, r"mehrdeutig: \[1\] KICK, \[7\] KICK"):
            server._routing_choice(opts, "KICK", "Ausgang")


if __name__ == "__main__":
    unittest.main()
