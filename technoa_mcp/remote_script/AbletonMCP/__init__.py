# AbletonMCP Remote Script - Technoa-Fassung
#
# Grundlage: ahujasid/ableton-mcp, Remote Script v1.7.1 (MIT-Lizenz),
# https://github.com/ahujasid/ableton-mcp (AbletonMCP_Remote_Script).
#
# Was hier anders ist und warum:
# - Duenne Schicht: Das Script liefert rohe, vollstaendige Daten aus der
#   Live-API. Zusammenfassen und Formatieren macht der MCP-Server. Jede
#   Aenderung an dieser Datei kostet einen Neustart von Live, Aenderungen am
#   Server nicht.
# - Alles laeuft auf Lives Hauptthread, auch der Socket: update_display (von
#   Live etwa alle 100 ms aufgerufen) nimmt Verbindungen an, liest Befehle,
#   fuehrt sie aus und sendet die Antwort. Das Original las vom Socket-Thread
#   aus; die Live-API ist nicht threadsicher. Mit Threads plus Uebergabe an
#   den Hauptthread dauerte jeder Befehl 200 bis 400 ms (Median 316 ms,
#   07.10.2026), vermutlich weil Lives Python Hintergrund-Threads nur im Takt
#   der eigenen Aufrufe laufen laesst.
# - Keine passiven Listener mehr. Sie haengten an jedem Clip-Slot und jedem
#   Clip und fuetterten nur die Dataset-Aufzeichnung (Supabase), die wir nicht
#   benutzen.
# - Schreibende Befehle laufen als ein Undo-Schritt, damit Strg+Z einen
#   Befehl ganz zuruecknimmt.
# - Spuren werden einheitlich adressiert: Index (0..n-1), "return:N" oder
#   "master". Damit sind Returns und Master erreichbar.
# - technoa.5 (11.10.2026): Geraete loeschen, verschieben, an Position
#   einfuegen und duplizieren; Eingangsmeter. Gesammelt bei der Arbeit an
#   einem echten Set.
# - technoa.6 (12.10.2026): Arrangement-Clips loeschen, Rohwert zu
#   einem Anzeigewert suchen, implicit_arm lesen, Fehlertext von
#   insert_device bei abgelehnter Position.
# - technoa.7 (22.10.2026): Clip-Groove fuer einen Bereich setzen,
#   Ein-/Ausgang einer Spur und Sidechain-Quelle eines Geraets setzen und
#   lesen.
# - technoa.8 (23.10.2026, Teil 3): add_notes_to_clip schreibt auch
#   velocity_deviation und release_velocity und meldet jedes Notenfeld, das
#   Live nicht bekommen hat; Session-Clip mehrfach ins Arrangement (repeat)
#   in einem Undo-Schritt. Den Versuchsbefehl pulse_parameter wieder
#   entfernt: Live schliesst einen Undo-Schritt je Durchlauf.
# - technoa.9 (27.10.2026, Paket Automation): Clip-Huellkurven lesen,
#   schreiben und loeschen; Abdruck der Huellkurven fuer den Schnappschuss;
#   inspect_lom zeigt auf Wunsch Lives Methodenbeschreibungen und kommt an das
#   Modul Live. Dazu globale Groove-Menge und Metronom setzen und
#   Arrangement-Clips ueber einen Bereich loeschen.
#
# Kommentare deutsch, aber ohne Umlaute: Lives Log und Windows-Konsolen mit
# Codepage 850 sollen nichts zerlegen.
from __future__ import absolute_import, print_function, unicode_literals

from _Framework.ControlSurface import ControlSurface
import json
import os
import socket
import time
import traceback

DEFAULT_PORT = 9877
# Fest auf Loopback. Der Port nimmt Befehle ohne Authentifizierung an; das
# Original liess per Umgebungsvariable auch andere Adressen zu. Lab-Regel 8:
# nur localhost.
HOST = "127.0.0.1"

# Ein Befehl, der nie vollstaendig ankommt, darf den Puffer nicht endlos
# wachsen lassen.
MAX_REQUEST_BYTES = 16 * 1024 * 1024

SCRIPT_VERSION = "1.7.1+technoa.9"
# Was der Server vor dem Senden pruefen muss, weil ein aelteres Script den
# Parameter still ignorieren wuerde (technoa.7 haette bei repeat=16 genau eine
# Kopie angelegt und Erfolg gemeldet).
FEATURES = ["duplicate_repeat", "note_fields_reported", "clip_envelopes", "envelope_digest", "inspect_doc",
            "song_settings", "delete_clip_range"]
# Rahmung unveraendert (ein JSON-Objekt pro Anfrage, ohne Trenner), deshalb
# bleibt das Protokoll bei 1. Der Original-Server kann so weiter lesen.
PROTOCOL_VERSION = 1

# Hoechstens so lange darf ein Durchlauf Antworten senden, bevor er Live
# wieder freigibt. Den Rest sendet der naechste Durchlauf.
SEND_BUDGET_SECONDS = 0.05
# Kommt update_display so lange nicht, uebernimmt ein schedule_message-Takt.
FALLBACK_AFTER_SECONDS = 0.5
# So viele Durchlaeufe wartet create_locator hoechstens auf den Playhead.
MAX_WAIT_TICKS = 30
# Alle Kopien laufen in einem Durchlauf; mehr wuerde Live spuerbar anhalten.
MAX_REPEAT = 256

# Notenfelder, die add_notes_to_clip an Live gibt. Die Grundfelder gehen an
# beide Wege (add_new_notes, set_notes), die Zusatzfelder nur an
# add_new_notes. Alles andere wird gemeldet, nicht geschrieben.
NOTE_BASE_FIELDS = ("pitch", "start_time", "duration", "velocity", "mute")
NOTE_EXTRA_FIELDS = ("probability", "velocity_deviation", "release_velocity")
# Lives Voreinstellung der Zusatzfelder. Nur Abweichungen davon gehen in den
# Inhalts-Fingerabdruck, damit Clips ohne sie denselben Abdruck behalten wie
# bis technoa.7 (sonst meldete snapshot_diff nach dem Wechsel jeden MIDI-Clip).
NOTE_EXTRA_DEFAULTS = {"probability": 1.0, "velocity_deviation": 0.0, "release_velocity": 64.0}

AUTOMATION_STATES = {0: "none", 1: "active", 2: "overridden"}

# Lives API nennt die Knickpunkte einer Huellkurve nicht, sie kennt nur
# value_at_time. Gelesen wird deshalb in einem Raster (Beats); der Server
# fasst die Werte wieder zu Punkten und Rampen zusammen.
ENVELOPE_GRID = 0.25
# Obergrenzen je Befehl, weil alles in einem Durchlauf des Hauptthreads
# laeuft: 160 Takte im Raster 1/16 sind 2560 Werte.
MAX_ENVELOPE_SAMPLES = 8192
MAX_ENVELOPE_STEPS = 4096
# So tief sucht der Abdruck in Racks nach Parametern mit Huellkurve.
ENVELOPE_SCAN_DEPTH = 3

# Je Richtung: Liste der Moeglichkeiten, Auswahl, Kanaele, Kanal. Spuren haben
# beide Richtungen, Geraete mit Sidechain (Compressor) nur "input".
ROUTING_ATTRS = {
    "output": ("available_output_routing_types", "output_routing_type",
               "available_output_routing_channels", "output_routing_channel"),
    "input": ("available_input_routing_types", "input_routing_type",
              "available_input_routing_channels", "input_routing_channel"),
}
# Name des Sidechain-Schalters bei Lives eigenen Geraeten (Compressor, Gate,
# Glue). Steht nur der Eingang ohne diesen Schalter, hoert das Geraet nichts.
SIDECHAIN_SWITCH = "s/c on"


def create_instance(c_instance):
    return AbletonMCP(c_instance)


def _fingerprint(text):
    """FNV-1a, 32 Bit. Anders als hash() auf Text ist das ueber Neustarts
    gleich: Python wuerfelt hash() fuer str bei jedem Start neu. Mit hash()
    aenderten sich am 07.10.2026 alle Audio-Fingerabdruecke nach einem
    Live-Neustart, obwohl die Clips gleich waren."""
    h = 0x811C9DC5
    for b in text.encode("utf-8"):
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


def _safe(fn, default=None):
    """Liest einen Wert, der je nach Spurart oder Live-Version fehlen darf.

    Live wirft bei Gruppen-, Return- und Master-Spuren fuer manche
    Eigenschaften RuntimeError statt sie einfach nicht anzubieten (z. B. arm).
    Ein einzelner fehlender Wert soll nie eine ganze Uebersicht kippen.
    """
    try:
        return fn()
    except Exception:
        return default


# Einheiten, die Live in Anzeigetexten mischt: DS Kick zeigt "41.17 Hz",
# EQ Eight springt bei 1000 auf "1.20 kHz", Zeiten wechseln zwischen "ms"
# und "s". Verglichen wird in der Grundeinheit.
DISPLAY_UNITS = {"khz": ("hz", 1000.0), "hz": ("hz", 1.0), "ms": ("ms", 1.0), "s": ("ms", 1000.0)}


def _parse_display(text):
    """Anzeigetext -> (Zahl, Grundeinheit) oder None, wenn keine Zahl vorne steht.

    "-inf dB" -> (-inf, "db"). Nicht lesbar sind z. B. "On", "C3", "25L".
    """
    import re
    if text is None:
        return None
    m = re.match(r"^\s*([-+]?(?:inf|\d+(?:[.,]\d*)?|[.,]\d+))\s*(.*?)\s*$", str(text).replace(chr(0x2212), "-"),
                 re.IGNORECASE)
    if not m:
        return None
    num = m.group(1).replace(",", ".").lower()
    unit = m.group(2).lower()
    if unit and not re.match(r"^[a-z%]+$", unit):
        return None
    value = float(num)
    base, factor = DISPLAY_UNITS.get(unit, (unit, 1.0))
    return value * factor, base


class AbletonMCP(ControlSurface):

    def __init__(self, c_instance):
        ControlSurface.__init__(self, c_instance)
        self.log_message("AbletonMCP (Technoa) initialisiert... (Script v%s)" % SCRIPT_VERSION)
        self._song = self.song()
        self.server = None
        self.running = False
        # Je Verbindung: Eingangspuffer, offene Anfragen, laufender Auftrag
        # (Generator, siehe _command_job) und noch nicht gesendete Antwort.
        self._clients = {}
        self._ticks = {"update_display": 0, "fallback": 0}
        self._last_display_tick = 0.0

        self._read_commands = {
            "get_script_info": self._cmd_get_script_info,
            "get_session_info": self._cmd_get_session_info,
            "get_song_state": self._cmd_get_song_state,
            "get_tracks_overview": self._cmd_get_tracks_overview,
            "get_track_info": self._cmd_get_track_info,
            "get_arrangement_clips": self._cmd_get_arrangement_clips,
            "get_clip_notes": self._cmd_get_clip_notes,
            "get_device_parameters": self._cmd_get_device_parameters,
            "get_selection": self._cmd_get_selection,
            "get_meters": self._cmd_get_meters,
            "inspect_lom": self._cmd_inspect_lom,
            "get_browser_tree": self._cmd_get_browser_tree,
            "get_browser_items_at_path": self._cmd_get_browser_items_at_path,
            "get_browser_item": self._cmd_get_browser_item,
            "find_parameter_value": self._cmd_find_parameter_value,
            "get_routing_options": self._cmd_get_routing_options,
            "get_clip_envelope": self._cmd_get_clip_envelope,
            "convert_parameter_values": self._cmd_convert_parameter_values,
        }
        self._write_commands = {
            "create_midi_track": self._cmd_create_midi_track,
            "create_audio_track": self._cmd_create_audio_track,
            "set_track_name": self._cmd_set_track_name,
            "create_clip": self._cmd_create_clip,
            "create_audio_clip": self._cmd_create_audio_clip,
            "add_notes_to_clip": self._cmd_add_notes_to_clip,
            "clear_notes_from_clip": self._cmd_clear_notes_from_clip,
            "set_clip_name": self._cmd_set_clip_name,
            "set_arrangement_clip_name": self._cmd_set_arrangement_clip_name,
            "set_clip_muted": self._cmd_set_clip_muted,
            "delete_clip": self._cmd_delete_clip,
            "delete_arrangement_clip": self._cmd_delete_arrangement_clip,
            "set_tempo": self._cmd_set_tempo,
            "set_loop": self._cmd_set_loop,
            "fire_clip": self._cmd_fire_clip,
            "stop_clip": self._cmd_stop_clip,
            "start_playback": self._cmd_start_playback,
            "stop_playback": self._cmd_stop_playback,
            "switch_to_arrangement_view": self._cmd_switch_to_arrangement_view,
            "set_current_song_time": self._cmd_set_current_song_time,
            "duplicate_session_clip_to_arrangement": self._cmd_duplicate_session_clip_to_arrangement,
            "load_browser_item": self._cmd_load_browser_item,
            "load_instrument_or_effect": self._cmd_load_browser_item,
            "set_device_parameter": self._cmd_set_device_parameter,
            "delete_device": self._cmd_delete_device,
            "move_device": self._cmd_move_device,
            "insert_device": self._cmd_insert_device,
            "duplicate_device": self._cmd_duplicate_device,
            "set_mixer": self._cmd_set_mixer,
            "set_clip_groove": self._cmd_set_clip_groove,
            "set_track_routing": self._cmd_set_track_routing,
            "set_sidechain": self._cmd_set_sidechain,
            "re_enable_automation": self._cmd_re_enable_automation,
            "back_to_arrangement": self._cmd_back_to_arrangement,
            "set_clip_envelope": self._cmd_set_clip_envelope,
            "clear_clip_envelope": self._cmd_clear_clip_envelope,
            "set_song_settings": self._cmd_set_song_settings,
            "delete_arrangement_clips": self._cmd_delete_arrangement_clips,
            "undo": self._cmd_undo,
            "redo": self._cmd_redo,
        }
        # Befehle, die mehrere Durchlaeufe des Hauptthreads brauchen. Sie sind
        # Generatoren; jedes yield wartet auf den naechsten Durchlauf.
        self._staged_commands = {
            "create_locator": self._staged_create_locator,
        }
        # undo/redo duerfen nicht selbst in einen Undo-Schritt eingeschlossen
        # werden, sonst nimmt Live den Rueckgaengig-Schritt als neue Aktion.
        self._no_undo_group = set(["undo", "redo", "start_playback", "stop_playback",
                                   "fire_clip", "stop_clip", "switch_to_arrangement_view",
                                   "set_current_song_time", "back_to_arrangement"])

        self.start_server()
        self.log_message("AbletonMCP (Technoa) bereit")
        self.show_message("AbletonMCP: Listening for commands on port " + str(DEFAULT_PORT))

    # ------------------------------------------------------------------
    # Socket-Server
    # ------------------------------------------------------------------

    def disconnect(self):
        self.log_message("AbletonMCP wird beendet...")
        self.running = False
        for client in list(self._clients):
            self._drop(client)
        if self.server:
            try:
                self.server.close()
            except Exception:
                pass
        ControlSurface.disconnect(self)
        self.log_message("AbletonMCP beendet")

    def start_server(self):
        try:
            self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind((HOST, DEFAULT_PORT))
            self.server.listen(5)
            self.server.setblocking(False)
            self.running = True
            self.schedule_message(1, self._fallback_tick)
            self.log_message("Server laeuft auf %s:%d" % (HOST, DEFAULT_PORT))
        except Exception as e:
            self.log_message("Server konnte nicht starten: " + str(e))
            self.show_message("AbletonMCP: Error starting server - " + str(e))

    def update_display(self):
        ControlSurface.update_display(self)
        if not self.running:
            return
        self._ticks["update_display"] += 1
        self._last_display_tick = time.time()
        self._service()

    def _fallback_tick(self):
        # Rueckfall, falls Live update_display einmal nicht aufruft. Laeuft
        # update_display, tut dieser Takt nichts; so wird jeder Auftrag nur
        # von einem Takt vorangetrieben.
        if not self.running:
            return
        if time.time() - self._last_display_tick > FALLBACK_AFTER_SECONDS:
            self._ticks["fallback"] += 1
            self._service()
        self.schedule_message(1, self._fallback_tick)

    def _service(self):
        """Ein Durchlauf: annehmen, lesen, Auftraege vorantreiben, senden."""
        try:
            self._accept()
        except Exception as e:
            self.log_message("Fehler bei accept: " + str(e))
        for client in list(self._clients):
            state = self._clients.get(client)
            if state is None:
                continue
            try:
                self._read(client, state)
                if state["job"] is None and state["requests"]:
                    state["job"] = self._command_job(state["requests"].pop(0))
                if state["job"] is not None:
                    try:
                        next(state["job"])
                    except StopIteration as done:
                        state["job"] = None
                        # default=str: ein versehentlich durchgereichtes
                        # Live-Objekt soll als Text ankommen, nicht die
                        # Verbindung abreissen.
                        state["out"] += json.dumps(done.value, default=str).encode("utf-8")
                self._flush(client, state)
                if state["closing"] and not state["out"] and state["job"] is None:
                    self._drop(client)
            except Exception as e:
                # Nach einem Protokollfehler ist der Datenstrom nicht mehr
                # synchron. Fehler melden und Verbindung schliessen; der
                # MCP-Server verbindet sich neu.
                self.log_message("Verbindungsfehler: " + str(e))
                try:
                    client.setblocking(True)
                    client.settimeout(0.2)
                    client.sendall(json.dumps({"status": "error", "message": str(e)}).encode("utf-8"))
                except Exception:
                    pass
                self._drop(client)

    def _accept(self):
        while True:
            try:
                client, address = self.server.accept()
            except (BlockingIOError, socket.timeout):
                return
            except OSError as e:
                if getattr(e, "winerror", None) == 10035 or getattr(e, "errno", None) in (11, 35, 10035):
                    return
                raise
            client.setblocking(False)
            try:
                client.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
            except Exception:
                pass
            self._clients[client] = {"buffer": b"", "requests": [], "job": None, "out": b"", "closing": False}
            self.log_message("Verbindung von " + str(address))
            self.show_message("AbletonMCP: Client connected")

    def _read(self, client, state):
        while True:
            try:
                data = client.recv(65536)
            except (BlockingIOError, socket.timeout):
                break
            except OSError as e:
                if getattr(e, "winerror", None) == 10035 or getattr(e, "errno", None) in (11, 35, 10035):
                    break
                raise
            if not data:
                state["closing"] = True
                break
            state["buffer"] += data
            if len(state["buffer"]) > MAX_REQUEST_BYTES:
                raise ValueError("Anfrage ueber %d Bytes ohne vollstaendiges JSON" % MAX_REQUEST_BYTES)
        self._split_requests(state)

    def _split_requests(self, state):
        # Rohe Bytes sammeln und erst vollstaendige Befehle dekodieren: ein
        # mehrbytiges UTF-8-Zeichen (Umlaut im Clipnamen) kann ueber zwei
        # recv()-Grenzen reichen. raw_decode trennt auch zwei Befehle, die
        # in einem Paket ankommen.
        if not state["buffer"]:
            return
        try:
            text = state["buffer"].decode("utf-8")
        except UnicodeDecodeError:
            return
        decoder = json.JSONDecoder()
        pos = 0
        while True:
            while pos < len(text) and text[pos].isspace():
                pos += 1
            if pos >= len(text):
                break
            try:
                obj, end = decoder.raw_decode(text, pos)
            except ValueError:
                break
            state["requests"].append(obj)
            pos = end
        state["buffer"] = text[pos:].encode("utf-8")

    def _flush(self, client, state):
        deadline = time.time() + SEND_BUDGET_SECONDS
        while state["out"]:
            try:
                sent = client.send(state["out"][:1048576])
            except (BlockingIOError, socket.timeout):
                if time.time() > deadline:
                    return
                time.sleep(0.0005)
                continue
            except OSError as e:
                if getattr(e, "winerror", None) == 10035 or getattr(e, "errno", None) in (11, 35, 10035):
                    if time.time() > deadline:
                        return
                    time.sleep(0.0005)
                    continue
                raise
            state["out"] = state["out"][sent:]

    def _drop(self, client):
        self._clients.pop(client, None)
        try:
            client.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Befehlsverteilung
    # ------------------------------------------------------------------

    def _command_job(self, command):
        """Ein Befehl als Generator. Jedes yield wartet auf den naechsten
        Durchlauf; der Rueckgabewert ist die Antwort an den Client."""
        command_type = command.get("type", "") if isinstance(command, dict) else ""
        params = (command.get("params") if isinstance(command, dict) else None) or {}
        staged = command_type in self._staged_commands
        if command_type in self._read_commands:
            handler = self._read_commands[command_type]
            write = False
        elif command_type in self._write_commands or staged:
            handler = self._staged_commands[command_type] if staged else self._write_commands[command_type]
            write = command_type not in self._no_undo_group
        else:
            known = sorted(set(self._read_commands) | set(self._write_commands) | set(self._staged_commands))
            return {"status": "error",
                    "message": "Unbekannter Befehl '%s'. Bekannt sind: %s" % (command_type, ", ".join(known))}

        def redo_state():
            return _safe(lambda: bool(self._song.can_redo))

        try:
            redo_before = redo_state() if write else None
            if staged:
                result = yield from handler(params)
            elif write:
                result = self._in_undo_step(lambda: handler(params))
            else:
                result = handler(params)
            if write and isinstance(result, dict):
                # Ob Live einen Undo-Schritt angelegt hat, verraet die API
                # nicht direkt. Ein neuer Schritt leert aber die Redo-Liste.
                # Am 07.10.2026 nahm ein undo nach einem nicht undo-faehigen
                # Befehl (arm) stattdessen einen aelteren Schritt zurueck.
                # Gelesen wird im naechsten Durchlauf, weil Live manches erst
                # dann uebernimmt.
                yield
                redo_after = redo_state()
                step = None
                if redo_before is True:
                    step = not redo_after
                result["undo_check"] = {"redo_before": redo_before, "redo_after": redo_after, "undo_step": step}
            return {"status": "success", "result": result}
        except Exception as e:
            self.log_message("Fehler in %s: %s" % (command_type, str(e)))
            self.log_message(traceback.format_exc())
            return {"status": "error", "message": str(e) or e.__class__.__name__}

    def _in_undo_step(self, fn):
        """Fasst alle Aenderungen eines Befehls zu einem Undo-Schritt zusammen.

        Ohne Klammer erzeugt z. B. create_locator mehrere Schritte (Playhead
        verschieben, Cue setzen, umbenennen), und ein Strg+Z nimmt nur einen
        Teil davon zurueck. Fehlt die API, laeuft der Befehl ungeklammert.
        """
        song = self._song
        began = False
        if hasattr(song, "begin_undo_step"):
            try:
                song.begin_undo_step()
                began = True
            except Exception as e:
                self.log_message("begin_undo_step fehlgeschlagen: " + str(e))
        try:
            return fn()
        finally:
            if began:
                try:
                    song.end_undo_step()
                except Exception as e:
                    self.log_message("end_undo_step fehlgeschlagen: " + str(e))

    # ------------------------------------------------------------------
    # Adressierung
    # ------------------------------------------------------------------

    def _track_param(self, params, key="track"):
        if key in params and params[key] is not None:
            return params[key]
        if "track_index" in params:
            return params["track_index"]
        raise ValueError("Spurangabe fehlt: 'track' als Index (0..n-1), 'return:N' oder 'master' mitgeben.")

    def _resolve_track(self, ref):
        """Gibt (track, kanonische_ref) zurueck."""
        song = self._song
        if isinstance(ref, bool):
            raise ValueError("Spurangabe %r ist kein Index." % (ref,))
        if isinstance(ref, str):
            r = ref.strip().lower()
            if r in ("master", "main"):
                return song.master_track, "master"
            if r.startswith("return:"):
                idx = int(r.split(":", 1)[1])
                returns = list(song.return_tracks)
                if idx < 0 or idx >= len(returns):
                    raise IndexError("Return %d gibt es nicht; das Set hat %d Return-Spuren (return:0 bis return:%d)."
                                     % (idx, len(returns), len(returns) - 1))
                return returns[idx], "return:%d" % idx
            if r.lstrip("-").isdigit():
                ref = int(r)
            else:
                raise ValueError("Unbekannte Spurangabe '%s'. Erlaubt: Index (0..n-1), 'return:N' oder 'master'." % ref)
        if isinstance(ref, float) and ref == int(ref):
            ref = int(ref)
        if not isinstance(ref, int):
            raise ValueError("Unbekannte Spurangabe %r." % (ref,))
        tracks = list(song.tracks)
        if ref < 0 or ref >= len(tracks):
            raise IndexError("Spur %d gibt es nicht; das Set hat %d Spuren (0 bis %d)." % (ref, len(tracks), len(tracks) - 1))
        return tracks[ref], ref

    def _same(self, a, b):
        if a is None or b is None:
            return False
        try:
            if a == b:
                return True
        except Exception:
            pass
        pa = getattr(a, "_live_ptr", None)
        pb = getattr(b, "_live_ptr", None)
        return pa is not None and pa == pb

    def _index_in(self, seq, obj):
        for i, item in enumerate(seq):
            if self._same(item, obj):
                return i
        return None

    def _ref_of_track(self, track):
        song = self._song
        idx = self._index_in(list(song.tracks), track)
        if idx is not None:
            return idx
        idx = self._index_in(list(song.return_tracks), track)
        if idx is not None:
            return "return:%d" % idx
        if self._same(song.master_track, track):
            return "master"
        # Letzter Ausweg, falls Live fuer dasselbe Objekt unvergleichbare
        # Huellen liefert: eindeutiger Name unter den normalen Spuren.
        name = _safe(lambda: track.name)
        hits = [i for i, t in enumerate(song.tracks) if _safe(lambda: t.name) == name]
        if name is not None and len(hits) == 1:
            return hits[0]
        return None

    def _track_kind(self, track, ref):
        if ref == "master":
            return "master"
        if isinstance(ref, str) and ref.startswith("return:"):
            return "return"
        if _safe(lambda: track.is_foldable, False):
            return "group"
        if _safe(lambda: track.has_midi_input, False):
            return "midi"
        if _safe(lambda: track.has_audio_input, False):
            return "audio"
        return "unknown"

    def _resolve_device(self, track, params):
        """Geraet per device_index, optional tiefer ueber chain_path.

        chain_path ist eine Liste von [chain_index, device_index]-Paaren, um in
        Racks hinabzusteigen, z. B. [[0, 1]] = Kette 0, Geraet 1 im Rack.
        """
        devices = list(track.devices)
        d_idx = params.get("device_index", 0)
        if d_idx < 0 or d_idx >= len(devices):
            raise IndexError("Geraet %d gibt es auf dieser Spur nicht (%d Geraete, 0 bis %d)."
                             % (d_idx, len(devices), len(devices) - 1))
        device = devices[d_idx]
        for step in params.get("chain_path") or []:
            c_idx, nd_idx = int(step[0]), int(step[1])
            chains = list(_safe(lambda: device.chains, []) or [])
            if c_idx < 0 or c_idx >= len(chains):
                raise IndexError("Kette %d gibt es in '%s' nicht (%d Ketten)." % (c_idx, device.name, len(chains)))
            nested = list(chains[c_idx].devices)
            if nd_idx < 0 or nd_idx >= len(nested):
                raise IndexError("Geraet %d gibt es in Kette %d von '%s' nicht." % (nd_idx, c_idx, device.name))
            device = nested[nd_idx]
        return device

    def _resolve_clip(self, track, params):
        """Gibt (clip, source, index) zurueck.

        source "arrangement": clip_index zaehlt in track.arrangement_clips
        (Reihenfolge wie get_arrangement_clips), alternativ at_time in Beats.
        source "session": clip_index ist der Clip-Slot.
        """
        source = (params.get("source") or "session").lower()
        if source == "arrangement":
            clips = list(_safe(lambda: track.arrangement_clips, []) or [])
            if params.get("at_time") is not None:
                t = float(params["at_time"])
                for i, clip in enumerate(clips):
                    if clip.start_time <= t < clip.end_time:
                        return clip, source, i
                raise ValueError("Auf dieser Spur liegt bei Beat %g kein Arrangement-Clip." % t)
            idx = params.get("clip_index")
            if idx is None:
                raise ValueError("clip_index oder at_time angeben.")
            if idx < 0 or idx >= len(clips):
                raise IndexError("Arrangement-Clip %d gibt es nicht; die Spur hat %d (0 bis %d)."
                                 % (idx, len(clips), len(clips) - 1))
            return clips[idx], source, idx
        if source != "session":
            raise ValueError("source muss 'session' oder 'arrangement' sein, nicht '%s'." % source)
        slots = list(track.clip_slots)
        idx = params.get("clip_index", 0)
        if idx < 0 or idx >= len(slots):
            raise IndexError("Clip-Slot %d gibt es nicht; die Spur hat %d Slots." % (idx, len(slots)))
        if not slots[idx].has_clip:
            raise ValueError("Im Session-Slot %d liegt kein Clip." % idx)
        return slots[idx].clip, source, idx

    # ------------------------------------------------------------------
    # Serialisierung
    # ------------------------------------------------------------------

    def _param_info(self, param, index=None, full=False):
        # str(param) ist Lives Anzeigetext ("-8.0 dB", "35 %", "On"). Liefert
        # eine Live-Version stattdessen die Objektdarstellung "<...>", hilft
        # str_for_value mit dem aktuellen Wert.
        display = _safe(lambda: str(param))
        if display is None or display.startswith("<"):
            display = _safe(lambda: param.str_for_value(param.value))
        info = {
            "name": _safe(lambda: param.name, "?"),
            "value": _safe(lambda: float(param.value)),
            "display": display,
        }
        if index is not None:
            info["index"] = index
        state = _safe(lambda: int(param.automation_state), 0)
        if state:
            info["automation"] = AUTOMATION_STATES.get(state, str(state))
        if full:
            info["min"] = _safe(lambda: float(param.min))
            info["max"] = _safe(lambda: float(param.max))
            info["is_quantized"] = _safe(lambda: bool(param.is_quantized), False)
            info["is_enabled"] = _safe(lambda: bool(param.is_enabled), True)
            if info["is_quantized"]:
                items = _safe(lambda: [str(x) for x in param.value_items], None)
                if items:
                    info["value_items"] = items
            original = _safe(lambda: param.original_name)
            if original and original != info["name"]:
                info["original_name"] = original
        return info

    def _device_kind(self, device):
        if _safe(lambda: device.can_have_drum_pads, False):
            return "drum_rack"
        if _safe(lambda: device.can_have_chains, False):
            return "rack"
        t = _safe(lambda: int(device.type))
        # Live.Device.DeviceType: 0 undefined, 1 instrument, 2 audio_effect, 4 midi_effect
        return {1: "instrument", 2: "audio_effect", 4: "midi_effect"}.get(t, "unknown")

    def _device_summary(self, device, index):
        info = {
            "index": index,
            "name": _safe(lambda: device.name, "?"),
            "class_name": _safe(lambda: device.class_name),
            "class_display_name": _safe(lambda: device.class_display_name),
            "kind": self._device_kind(device),
            "is_active": _safe(lambda: bool(device.is_active), True),
            "parameter_count": _safe(lambda: len(device.parameters), 0),
        }
        automated, overridden = [], []
        switch = None
        for p in _safe(lambda: list(device.parameters), []) or []:
            st = _safe(lambda: int(p.automation_state), 0)
            if st == 1:
                automated.append(_safe(lambda: p.name, "?"))
            elif st == 2:
                overridden.append(_safe(lambda: p.name, "?"))
            if switch is None and (_safe(lambda: p.name, "") or "").strip().lower() == SIDECHAIN_SWITCH:
                switch = p
        if automated:
            info["automated"] = automated
        if overridden:
            info["automation_overridden"] = overridden
        if _safe(lambda: device.can_have_chains, False):
            info["chain_count"] = _safe(lambda: len(device.chains), 0)
        sidechain = self._sidechain_info(device, switch)
        if sidechain is not None:
            info["sidechain"] = sidechain
        return info

    def _sidechain_info(self, device, switch):
        """Sidechain-Eingang eines Geraets oder None, wenn es keinen ueber die API hat.

        Bis technoa.6 war die Quelle nur ueber inspect_lom zu sehen. Beim
        Setzen von Hand ist am 19.10.2026 nebenbei der DRUMS-Ausgang verrutscht;
        ohne die Quelle im Schnappschuss faellt so etwas erst beim Hoeren auf.
        """
        rtype = _safe(lambda: device.input_routing_type)
        if rtype is None:
            return None
        info = {"type": _safe(lambda: rtype.display_name),
                "channel": _safe(lambda: device.input_routing_channel.display_name)}
        if switch is not None:
            info["on"] = bool(_safe(lambda: switch.value, 0.0))
        return info

    def _mixer_info(self, track):
        mixer = track.mixer_device
        info = {
            "volume": _safe(lambda: self._param_info(mixer.volume)),
            "panning": _safe(lambda: self._param_info(mixer.panning)),
        }
        sends = []
        for i, send in enumerate(_safe(lambda: list(mixer.sends), []) or []):
            s = self._param_info(send)
            s["index"] = i
            sends.append(s)
        info["sends"] = sends
        activator = _safe(lambda: mixer.track_activator)
        if activator is not None:
            info["track_activator"] = self._param_info(activator)
        crossfade = _safe(lambda: int(mixer.crossfade_assign))
        if crossfade is not None:
            info["crossfade_assign"] = crossfade
        return info

    def _routing_info(self, track):
        out = {}
        for key, attr in (("output_type", "output_routing_type"),
                          ("output_channel", "output_routing_channel"),
                          ("input_type", "input_routing_type"),
                          ("input_channel", "input_routing_channel")):
            val = _safe(lambda: getattr(track, attr).display_name)
            if val is not None:
                out[key] = val
        return out

    def _clip_info(self, clip, index=None, with_digest=False, track=None):
        """track: nur mitgeben, wenn der Abdruck der Huellkurven gebraucht
        wird (Schnappschuss). Dafuer muessen alle Parameter der Spur gefragt
        werden, ob sie eine Huellkurve in diesem Clip haben."""
        is_midi = _safe(lambda: bool(clip.is_midi_clip), False)
        info = {
            "name": _safe(lambda: clip.name, ""),
            "is_midi": is_midi,
            "is_audio": _safe(lambda: bool(clip.is_audio_clip), False),
            "length": _safe(lambda: float(clip.length)),
            # muted ist der Clip-Aktivator: True = im Set grau/deaktiviert.
            "muted": _safe(lambda: bool(clip.muted), False),
            "looping": _safe(lambda: bool(clip.looping)),
            "loop_start": _safe(lambda: float(clip.loop_start)),
            "loop_end": _safe(lambda: float(clip.loop_end)),
            "start_marker": _safe(lambda: float(clip.start_marker)),
            "end_marker": _safe(lambda: float(clip.end_marker)),
            "color_index": _safe(lambda: int(clip.color_index)),
            "has_envelopes": _safe(lambda: bool(clip.has_envelopes)),
            # Bei Session-Clips sichtbar, ob fire_clip/stop_clip gewirkt hat.
            "is_playing": _safe(lambda: bool(clip.is_playing)),
            "is_triggered": _safe(lambda: bool(clip.is_triggered)),
        }
        if index is not None:
            info["index"] = index
        start = _safe(lambda: float(clip.start_time))
        if start is not None and _safe(lambda: bool(clip.is_arrangement_clip), True):
            info["start_time"] = start
            info["end_time"] = _safe(lambda: float(clip.end_time))
        groove = _safe(lambda: clip.groove)
        if groove is not None:
            info["groove"] = _safe(lambda: groove.name)
        if info["is_audio"]:
            info["file_path"] = _safe(lambda: clip.file_path)
            info["gain_display"] = _safe(lambda: clip.gain_display_string)
            info["warping"] = _safe(lambda: bool(clip.warping))
            info["warp_mode"] = _safe(lambda: int(clip.warp_mode))
            info["pitch_coarse"] = _safe(lambda: int(clip.pitch_coarse))
            info["pitch_fine"] = _safe(lambda: float(clip.pitch_fine))
        if with_digest:
            info["content_digest"] = self._content_digest(clip, info)
        if track is not None and info.get("has_envelopes"):
            # Fehlt der Schluessel bei has_envelopes=True, war das Lesen nicht
            # moeglich; der Vergleich wertet das als "unbekannt", nicht als leer.
            found = _safe(lambda: self._clip_envelopes(clip, track))
            if found is not None:
                info["envelopes"] = dict((e["label"], e["digest"]) for e in found)
        return dict((k, v) for k, v in info.items() if v is not None)

    def _note_time_window(self, clip):
        # Noten koennen vor 0 oder hinter length liegen, wenn Start- oder
        # Loopmarker verschoben sind. Fenster grosszuegig waehlen.
        lo = min(0.0, _safe(lambda: float(clip.start_marker), 0.0) or 0.0,
                 _safe(lambda: float(clip.loop_start), 0.0) or 0.0)
        hi = max(_safe(lambda: float(clip.length), 0.0) or 0.0,
                 _safe(lambda: float(clip.end_marker), 0.0) or 0.0,
                 _safe(lambda: float(clip.loop_end), 0.0) or 0.0)
        return lo, (hi - lo) + 1.0

    def _notes_from_clip(self, clip):
        notes = []
        lo, span = self._note_time_window(clip)
        if hasattr(clip, "get_notes_extended"):
            raw = clip.get_notes_extended(0, 128, lo, span)
            for n in raw:
                entry = {
                    "pitch": int(n.pitch),
                    "start_time": float(n.start_time),
                    "duration": float(n.duration),
                    "velocity": float(n.velocity),
                    "mute": bool(n.mute),
                }
                for opt in NOTE_EXTRA_FIELDS:
                    val = _safe(lambda: float(getattr(n, opt)))
                    if val is not None:
                        entry[opt] = val
                notes.append(entry)
        else:
            for n in clip.get_notes(lo, 0, span, 128):
                notes.append({"pitch": int(n[0]), "start_time": float(n[1]), "duration": float(n[2]),
                              "velocity": float(n[3]), "mute": bool(n[4]) if len(n) > 4 else False})
        notes.sort(key=lambda x: (x["start_time"], x["pitch"]))
        return notes

    def _content_digest(self, clip, info):
        """Kurzer Fingerabdruck des Clipinhalts.

        Damit kann der Server 500 gleiche Kick-Clips als einen Lauf
        zusammenfassen, ohne alle Noten zu uebertragen. _fingerprint statt
        hash(), damit der Abdruck auch nach einem Neustart von Live gleich
        bleibt.
        """
        try:
            if info.get("is_midi"):
                notes = self._notes_from_clip(clip)
                key = ";".join("%d,%.4f,%.4f,%.2f,%d" % (n["pitch"], n["start_time"], n["duration"], n["velocity"],
                                                          1 if n["mute"] else 0)
                               + "".join(",%s%.3f" % (k[0], n[k]) for k in NOTE_EXTRA_FIELDS
                                         if n.get(k) is not None and abs(n[k] - NOTE_EXTRA_DEFAULTS[k]) > 1e-6)
                               for n in notes)
                return "m%d-%08x" % (len(notes), _fingerprint(key))
            key = (info.get("file_path"), info.get("start_marker"), info.get("end_marker"),
                   info.get("loop_start"), info.get("loop_end"), info.get("gain_display"),
                   info.get("pitch_coarse"), info.get("pitch_fine"), info.get("warp_mode"))
            return "a-%08x" % _fingerprint("|".join(str(x) for x in key))
        except Exception:
            return None

    def _arrangement_summary(self, track):
        clips = list(_safe(lambda: track.arrangement_clips, []) or [])
        if not clips:
            return {"clip_count": 0}
        muted = 0
        first, last = None, None
        for c in clips:
            if _safe(lambda: bool(c.muted), False):
                muted += 1
            s = _safe(lambda: float(c.start_time))
            e = _safe(lambda: float(c.end_time))
            if s is not None and (first is None or s < first):
                first = s
            if e is not None and (last is None or e > last):
                last = e
        return {"clip_count": len(clips), "muted_count": muted, "first_start": first, "last_end": last}

    def _track_info(self, track, ref, detail=False):
        kind = self._track_kind(track, ref)
        info = {"ref": ref, "name": _safe(lambda: track.name, "?"), "kind": kind}
        if kind not in ("master",):
            info["mute"] = _safe(lambda: bool(track.mute))
            info["solo"] = _safe(lambda: bool(track.solo))
        if kind in ("midi", "audio"):
            if _safe(lambda: bool(track.can_be_armed), False):
                info["arm"] = _safe(lambda: bool(track.arm), False)
                # Am 11.10.2026 war eine neue Spur nach dem Laden eines
                # Instruments scharf. Ob das Lives Auto-Arm ueber
                # implicit_arm ist oder ein echtes arm, soll man sehen koennen.
                implicit = _safe(lambda: bool(track.implicit_arm))
                if implicit is not None:
                    info["implicit_arm"] = implicit
            info["monitoring"] = _safe(lambda: int(track.current_monitoring_state))
        if kind not in ("master", "return"):
            info["is_grouped"] = _safe(lambda: bool(track.is_grouped), False)
            if info["is_grouped"]:
                parent = _safe(lambda: track.group_track)
                info["group"] = self._ref_of_track(parent) if parent is not None else None
                info["group_name"] = _safe(lambda: parent.name)
            info["is_visible"] = _safe(lambda: bool(track.is_visible))
        if kind == "group":
            info["fold_state"] = _safe(lambda: int(track.fold_state))
        info["color_index"] = _safe(lambda: int(track.color_index))
        info["mixer"] = _safe(lambda: self._mixer_info(track), {})
        info["routing"] = _safe(lambda: self._routing_info(track), {})
        info["devices"] = [self._device_summary(d, i)
                           for i, d in enumerate(_safe(lambda: list(track.devices), []) or [])]
        if kind in ("midi", "audio"):
            info["arrangement"] = self._arrangement_summary(track)
            slots = _safe(lambda: list(track.clip_slots), []) or []
            filled = []
            for i, slot in enumerate(slots):
                if _safe(lambda: slot.has_clip, False):
                    filled.append(self._clip_info(slot.clip, i, track=track) if detail else i)
            info["session_clips"] = filled
            info["session_slot_count"] = len(slots)
        info["meter"] = {
            "left": _safe(lambda: float(track.output_meter_left)),
            "right": _safe(lambda: float(track.output_meter_right)),
        }
        return info

    # ------------------------------------------------------------------
    # Lesende Befehle
    # ------------------------------------------------------------------

    def _cmd_get_script_info(self, params):
        return {
            "name": "AbletonMCP",
            "variant": "technoa",
            "script_version": SCRIPT_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "port": DEFAULT_PORT,
            "capabilities": sorted(list(self._read_commands) + list(self._write_commands)
                                   + list(self._staged_commands)),
            "features": list(FEATURES),
            "undo_grouping": hasattr(self._song, "begin_undo_step"),
            "main_thread": True,
            "transport": "socket auf dem Hauptthread, abgefragt in update_display",
            "ticks": dict(self._ticks),
            "passive_listeners": False,
        }

    def _cmd_get_session_info(self, params):
        song = self._song
        return {
            "tempo": _safe(lambda: float(song.tempo)),
            "signature_numerator": _safe(lambda: int(song.signature_numerator)),
            "signature_denominator": _safe(lambda: int(song.signature_denominator)),
            "track_count": len(song.tracks),
            "return_track_count": len(song.return_tracks),
            "is_playing": _safe(lambda: bool(song.is_playing), False),
            "current_song_time": _safe(lambda: float(song.current_song_time), 0.0),
            "song_length": _safe(lambda: float(song.song_length), 0.0),
            "loop": _safe(lambda: bool(song.loop), False),
            "loop_start": _safe(lambda: float(song.loop_start), 0.0),
            "loop_length": _safe(lambda: float(song.loop_length), 0.0),
        }

    def _cmd_get_song_state(self, params):
        song = self._song
        state = self._cmd_get_session_info(params)
        state.update({
            "record_mode": _safe(lambda: bool(song.record_mode)),
            "arrangement_overdub": _safe(lambda: bool(song.arrangement_overdub)),
            "session_record": _safe(lambda: bool(song.session_record)),
            "metronome": _safe(lambda: bool(song.metronome)),
            "back_to_arranger": _safe(lambda: bool(song.back_to_arranger)),
            "can_undo": _safe(lambda: bool(song.can_undo)),
            "can_redo": _safe(lambda: bool(song.can_redo)),
            # True, sobald irgendwo Automation uebersteuert ist. undo stellt
            # einen uebersteuerten Wert zurueck, die Automation aber nicht.
            "re_enable_automation_enabled": _safe(lambda: bool(song.re_enable_automation_enabled)),
            "scale": {
                "root_note": _safe(lambda: int(song.root_note)),
                "scale_name": _safe(lambda: str(song.scale_name)),
                "scale_mode": _safe(lambda: bool(song.scale_mode)),
                "scale_intervals": _safe(lambda: [int(x) for x in song.scale_intervals]),
            },
            "groove_amount": _safe(lambda: float(song.groove_amount)),
            "scene_count": _safe(lambda: len(song.scenes)),
        })
        grooves = []
        for g in _safe(lambda: list(song.groove_pool.grooves), []) or []:
            grooves.append({
                "name": _safe(lambda: g.name),
                "base": _safe(lambda: int(g.base)),
                "timing_amount": _safe(lambda: float(g.timing_amount)),
                "quantization_amount": _safe(lambda: float(g.quantization_amount)),
                "random_amount": _safe(lambda: float(g.random_amount)),
                "velocity_amount": _safe(lambda: float(g.velocity_amount)),
            })
        state["grooves"] = grooves
        cues = []
        for cue in _safe(lambda: list(song.cue_points), []) or []:
            cues.append({"name": _safe(lambda: cue.name, ""), "time": _safe(lambda: float(cue.time))})
        cues.sort(key=lambda c: c["time"] if c["time"] is not None else 0.0)
        state["cue_points"] = cues
        return state

    def _cmd_get_tracks_overview(self, params):
        song = self._song
        tracks = [self._track_info(t, i) for i, t in enumerate(song.tracks)]
        returns = [self._track_info(t, "return:%d" % i) for i, t in enumerate(song.return_tracks)]
        master = self._track_info(song.master_track, "master")
        return {"tracks": tracks, "returns": returns, "master": master}

    def _cmd_get_track_info(self, params):
        track, ref = self._resolve_track(self._track_param(params))
        return self._track_info(track, ref, detail=True)

    def _cmd_get_arrangement_clips(self, params):
        track, ref = self._resolve_track(self._track_param(params))
        kind = self._track_kind(track, ref)
        if kind not in ("midi", "audio"):
            raise ValueError("'%s' ist eine %s-Spur und hat keine Arrangement-Clips. "
                             "Fuer Gruppen die einzelnen Spuren darin abfragen." % (track.name, kind))
        start = params.get("start_time")
        end = params.get("end_time")
        with_notes = bool(params.get("include_notes", False))
        with_digest = bool(params.get("include_digest", True))
        clips = []
        for i, clip in enumerate(list(track.arrangement_clips)):
            s = _safe(lambda: float(clip.start_time), 0.0)
            e = _safe(lambda: float(clip.end_time), 0.0)
            if start is not None and e <= float(start):
                continue
            if end is not None and s >= float(end):
                continue
            info = self._clip_info(clip, i, with_digest=with_digest, track=track if with_digest else None)
            if with_notes and info.get("is_midi"):
                info["notes"] = self._notes_from_clip(clip)
            clips.append(info)
        return {"track": ref, "track_name": track.name, "clip_count_total": len(track.arrangement_clips),
                "clips": clips}

    def _cmd_get_clip_notes(self, params):
        track, ref = self._resolve_track(self._track_param(params))
        clip, source, idx = self._resolve_clip(track, params)
        if not _safe(lambda: bool(clip.is_midi_clip), False):
            raise ValueError("Clip '%s' ist ein Audioclip und hat keine Noten." % clip.name)
        info = self._clip_info(clip, idx)
        info["notes"] = self._notes_from_clip(clip)
        return {"track": ref, "track_name": track.name, "source": source, "clip": info}

    def _cmd_get_device_parameters(self, params):
        track, ref = self._resolve_track(self._track_param(params))
        device = self._resolve_device(track, params)
        info = self._device_summary(device, params.get("device_index", 0))
        name_filter = (params.get("name_filter") or "").lower()
        only_automated = bool(params.get("only_automated", False))
        out = []
        for i, p in enumerate(list(device.parameters)):
            pi = self._param_info(p, i, full=True)
            if name_filter and name_filter not in pi["name"].lower():
                continue
            if only_automated and "automation" not in pi:
                continue
            out.append(pi)
        info["parameters"] = out
        chains = []
        for c_i, chain in enumerate(_safe(lambda: list(device.chains), []) or []):
            chains.append({"index": c_i, "name": _safe(lambda: chain.name, ""),
                           "mute": _safe(lambda: bool(chain.mute)),
                           "devices": [_safe(lambda: d.name, "?") for d in _safe(lambda: list(chain.devices), []) or []]})
        if chains:
            info["chains"] = chains
        return {"track": ref, "track_name": track.name, "chain_path": params.get("chain_path") or [], "device": info}

    def _cmd_find_parameter_value(self, params):
        """Sucht den Rohwert, bei dem Live einen bestimmten Anzeigewert zeigt. Setzt nichts.

        Bei Max-Geraeten ist der Rohwert nicht linear zur Anzeige: DS Kick
        Pitch laeuft roh von 30 bis 200 und zeigt Hz (11.10.2026: 105,1
        -> 41,17 Hz). Statt zu raten wird ueber str_for_value halbiert. Das
        setzt eine monotone Anzeige voraus; das wird vorher an 33 Stellen
        geprueft.
        """
        track, ref = self._resolve_track(self._track_param(params))
        device = self._resolve_device(track, params)
        plist = list(device.parameters)
        idx = int(params.get("parameter_index", 0))
        if idx < 0 or idx >= len(plist):
            raise IndexError("Parameter %d gibt es in '%s' nicht (%d Parameter)." % (idx, device.name, len(plist)))
        param = plist[idx]
        out = {"track": ref, "track_name": track.name, "device": device.name, "parameter": param.name,
               "parameter_index": idx, "current": self._param_info(param, idx)}
        out.update(self._search_display(param, params.get("target", "")))
        return out

    def _search_display(self, param, target_text):
        """Halbiert ueber str_for_value bis zum Anzeigewert target_text.
        Gemeinsam fuer find_parameter_value und die Umrechnung der
        Huellkurven-Werte (technoa.9)."""
        if _safe(lambda: bool(param.is_quantized), False):
            items = _safe(lambda: [str(x) for x in param.value_items], []) or []
            raise ValueError("'%s' hat feste Stufen (%s); den Index der Stufe direkt setzen." % (
                param.name, " / ".join("%d=%s" % (i, x) for i, x in enumerate(items)) or "keine Liste"))
        target_text = str(target_text).strip()
        target = _parse_display(target_text)
        if target is None:
            raise ValueError("Zielwert '%s' nicht lesbar. Beispiele: '41.17 Hz', '1.2 kHz', '-6 dB', '35 %%'."
                             % target_text)
        lo, hi = float(param.min), float(param.max)

        def shown(v):
            return _safe(lambda: param.str_for_value(v))

        grid = [lo + (hi - lo) * k / 32.0 for k in range(33)]
        samples = [(v, shown(v)) for v in grid]
        parsed = [_parse_display(s) for _, s in samples]
        if any(p is None for p in parsed):
            bad = [s for (_, s), p in zip(samples, parsed) if p is None][:3]
            raise ValueError("Die Anzeige von '%s' ist keine Zahl (z. B. %s); Suche nicht moeglich."
                             % (param.name, ", ".join("'%s'" % b for b in bad)))
        units = set(p[1] for p in parsed)
        unit = parsed[len(parsed) // 2][1]
        goal = target[0]
        if len(units) > 1 or (target[1] and target[1] != unit):
            raise ValueError("Einheit passt nicht: Ziel '%s', '%s' zeigt %s." % (
                target_text, param.name, ", ".join("'%s'" % u for u in sorted(units))))
        nums = [p[0] for p in parsed]
        rising = all(b >= a for a, b in zip(nums, nums[1:]))
        falling = all(b <= a for a, b in zip(nums, nums[1:]))
        if not (rising or falling):
            raise ValueError("Die Anzeige von '%s' ist nicht monoton (%s ...); Suche nicht moeglich."
                             % (param.name, ", ".join(s for _, s in samples[:6])))
        if goal < min(nums) or goal > max(nums):
            raise ValueError("'%s' reicht nur von '%s' bis '%s'; '%s' liegt ausserhalb."
                             % (param.name, samples[0][1], samples[-1][1], target_text))

        def number(v):
            return _parse_display(shown(v))[0]

        # a: Seite unter dem Ziel, b: Seite darueber, egal in welche Richtung
        # die Anzeige laeuft.
        a, b = (lo, hi) if rising else (hi, lo)
        for _ in range(60):
            mid = (a + b) / 2.0
            if number(mid) < goal:
                a = mid
            else:
                b = mid
        best = self._bucket_center(lo, hi, min((a, b), key=lambda v: abs(number(v) - goal)), number)
        return {"target": target_text, "value": best, "display": shown(best),
                "min": lo, "max": hi, "display_min": samples[0][1], "display_max": samples[-1][1],
                "direction": "steigend" if rising else "fallend"}

    def _bucket_center(self, lo, hi, v, number):
        """Mitte des Rohwert-Bereichs, in dem Live dieselbe Anzeige zeigt wie bei v.

        Die Halbierung landet am Rand dieses Bereichs. Dort kippt die Anzeige
        schon bei der Rundung auf 6 Stellen: In der Attrappe wurde aus "0 dB"
        nach dem Nachlesen "-0.1 dB" (27.10.2026). Setzt eine monotone Anzeige
        voraus, wie die Halbierung selbst."""
        here = number(v)

        def edge(inside, outside):
            for _ in range(50):
                mid = (inside + outside) / 2.0
                if number(mid) == here:
                    inside = mid
                else:
                    outside = mid
            return inside

        low = lo if number(lo) == here else edge(v, lo)
        high = hi if number(hi) == here else edge(v, hi)
        return (low + high) / 2.0

    def _raw_for(self, param, value):
        """Wert fuer einen Parameter: Zahl = Rohwert, Text = Anzeigewert.

        Stufen-Parameter nehmen den Namen der Stufe ("On", "Off"). dB-Werte
        gehen ueber _value_for_db, weil dort "-inf" erlaubt ist und die
        Kurve des Faders nicht linear ist; alles andere ueber die Halbierung
        von _search_display.
        """
        lo, hi = float(param.min), float(param.max)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            v = float(value)
            if v < lo - 1e-9 or v > hi + 1e-9:
                raise ValueError("Rohwert %g liegt ausserhalb von '%s' (%g bis %g)." % (v, param.name, lo, hi))
            return min(max(v, lo), hi)
        text = str(value).strip()
        if _safe(lambda: bool(param.is_quantized), False):
            items = _safe(lambda: [str(x) for x in param.value_items], []) or []
            for i, item in enumerate(items):
                if item.strip().lower() == text.lower():
                    return lo + i
            raise ValueError("'%s' hat nur die Stufen %s, nicht '%s'." % (param.name, " / ".join(items), text))
        parsed = _parse_display(text)
        if parsed is not None and parsed[1] == "db":
            top = _safe(lambda: param.str_for_value(hi), "") or ""
            if "db" not in top.lower():
                raise ValueError("'%s' zeigt keine dB ('%s'); '%s' passt nicht." % (param.name, top, text))
            raw = self._value_for_db(param, parsed[0])
            if parsed[0] == float("-inf"):
                # Ganz unten bleiben: -inf ist Stille, nicht die Mitte davon.
                return raw
            # set_mixer bleibt beim Rand (am Set so geprueft); fuer
            # Huellkurven zaehlt, dass die Anzeige beim Nachlesen stimmt.
            return self._bucket_center(lo, hi, raw, lambda v: self._parse_db(param.str_for_value(v)))
        return self._search_display(param, text)["value"]

    def _cmd_convert_parameter_values(self, params):
        """Anzeigewerte -> Rohwerte fuer einen Parameter, ohne etwas zu setzen.
        Adressierung wie bei den Huellkurven (mixer oder Geraet)."""
        track, ref = self._resolve_track(self._track_param(params))
        param, target = self._envelope_target(track, params)
        out = []
        for v in params.get("values") or []:
            raw = self._raw_for(param, v)
            out.append({"input": v, "value": raw, "display": _safe(lambda: param.str_for_value(raw))})
        return {"track": ref, "track_name": track.name, "target": target, "param": self._param_info(param, full=True),
                "values": out}

    def _cmd_get_selection(self, params):
        song = self._song
        view = song.view
        out = {
            "current_song_time": _safe(lambda: float(song.current_song_time)),
            "is_playing": _safe(lambda: bool(song.is_playing)),
            "focused_view": _safe(lambda: self._focused_view()),
        }
        sel_track = _safe(lambda: view.selected_track)
        if sel_track is not None:
            out["selected_track"] = {"ref": self._ref_of_track(sel_track), "name": _safe(lambda: sel_track.name)}
        sel_scene = _safe(lambda: view.selected_scene)
        if sel_scene is not None:
            out["selected_scene"] = self._index_in(list(song.scenes), sel_scene)
        clip = _safe(lambda: view.detail_clip)
        if clip is not None:
            out["detail_clip"] = self._locate_clip(clip)
        param = _safe(lambda: view.selected_parameter)
        if param is not None:
            pinfo = self._param_info(param)
            owner = _safe(lambda: param.canonical_parent)
            if owner is not None:
                pinfo["owner"] = _safe(lambda: owner.name)
            out["selected_parameter"] = pinfo
        return out

    def _focused_view(self):
        # "Session" oder "Arranger", je nachdem, welche Hauptansicht vorne ist.
        return str(self.application().view.focused_document_view)

    def _locate_clip(self, clip):
        info = self._clip_info(clip)
        parent = _safe(lambda: clip.canonical_parent)
        if parent is None:
            return info
        if hasattr(parent, "has_clip"):
            # Session-Clip: Elternteil ist der Clip-Slot, dessen Elternteil die Spur.
            track = _safe(lambda: parent.canonical_parent)
            info["source"] = "session"
            if track is not None:
                info["track"] = self._ref_of_track(track)
                info["track_name"] = _safe(lambda: track.name)
                info["clip_index"] = self._index_in(list(track.clip_slots), parent)
        else:
            info["source"] = "arrangement"
            info["track"] = self._ref_of_track(parent)
            info["track_name"] = _safe(lambda: parent.name)
            info["clip_index"] = self._index_in(list(_safe(lambda: parent.arrangement_clips, []) or []), clip)
        return info

    def _cmd_get_meters(self, params):
        song = self._song

        def meter(t, ref):
            return {"ref": ref, "name": _safe(lambda: t.name, "?"),
                    "left": _safe(lambda: float(t.output_meter_left)),
                    "right": _safe(lambda: float(t.output_meter_right)),
                    "level": _safe(lambda: float(t.output_meter_level)),
                    # Eingang = was laut Routing am Spureingang anliegt, auch
                    # bei Monitoring Off. Clipwiedergabe zaehlt dort vermutlich
                    # nicht; am 11.10.2026 noch ungeprueft.
                    "in_left": _safe(lambda: float(t.input_meter_left)),
                    "in_right": _safe(lambda: float(t.input_meter_right)),
                    "in_level": _safe(lambda: float(t.input_meter_level))}
        out = [meter(t, i) for i, t in enumerate(song.tracks)]
        out += [meter(t, "return:%d" % i) for i, t in enumerate(song.return_tracks)]
        out.append(meter(song.master_track, "master"))
        return {"time": time.time(), "is_playing": _safe(lambda: bool(song.is_playing)),
                "song_time": _safe(lambda: float(song.current_song_time)), "meters": out}

    def _cmd_inspect_lom(self, params):
        """Schaut lesend in ein Objekt der Live-API, z. B. 'song.tracks[1].mixer_device'.

        Fuer die Erkundung, welche Eigenschaften diese Live-Version anbietet,
        ohne dafuer das Script zu aendern und Live neu zu starten. Es werden
        nur Eigenschaften gelesen, nie Methoden aufgerufen; Namen mit '_'
        sind gesperrt.
        """
        path = (params.get("path") or "song").strip()
        obj = self._walk_lom_path(path)
        out = {"path": path, "type": type(obj).__name__, "str": _safe(lambda: str(obj)[:300])}
        if isinstance(obj, (list, tuple)) or (hasattr(obj, "__len__") and not isinstance(obj, str)):
            out["length"] = _safe(lambda: len(obj))
        # doc=True: Lives Beschreibung je Methode (Signatur und Text aus der
        # C++-Bindung). Liest nur __doc__, ruft nichts auf. Seit technoa.9,
        # damit sich neue API-Teile ohne Script-Aenderung erkunden lassen.
        with_doc = bool(params.get("doc"))
        if params.get("list_attributes", True):
            attrs = {}
            for name in dir(obj):
                if name.startswith("_") or name.endswith("_listener") or name.endswith("_has_listener"):
                    continue
                try:
                    val = getattr(obj, name)
                except Exception as e:
                    attrs[name] = "<Fehler: %s>" % (str(e)[:80],)
                    continue
                if callable(val):
                    doc = (_safe(lambda: val.__doc__) or "").strip() if with_doc else ""
                    attrs[name] = ("<Methode> " + doc[:600]) if doc else "<Methode>"
                elif isinstance(val, (bool, int, float)) or val is None:
                    attrs[name] = val
                elif isinstance(val, str):
                    attrs[name] = val[:200]
                else:
                    n = _safe(lambda: len(val))
                    attrs[name] = "<%s%s>" % (type(val).__name__, "" if n is None else " len=%d" % n)
            out["attributes"] = attrs
        return out

    def _walk_lom_path(self, path):
        import re
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9_]*|\[\-?\d+\]", path)
        if not tokens or "".join(tokens) != path.replace(".", ""):
            raise ValueError("Pfad '%s' nicht lesbar. Erlaubt: Namen und [Index], z. B. song.tracks[0].mixer_device" % path)
        root = tokens[0]
        if root == "song":
            obj = self._song
        elif root in ("application", "app"):
            obj = self.application()
        elif root == "Live":
            # Das Modul selbst: Klassen wie Live.Clip.AutomationEnvelope sind
            # nur so erreichbar, an Objekten haengen sie erst nach einem
            # Methodenaufruf.
            import Live
            obj = Live
        else:
            raise ValueError("Pfad muss mit 'song', 'application' oder 'Live' beginnen.")
        for tok in tokens[1:]:
            if tok.startswith("["):
                obj = list(obj)[int(tok[1:-1])]
            else:
                if tok.startswith("_"):
                    raise ValueError("Private Namen sind gesperrt.")
                obj = getattr(obj, tok)
                # Klassen im Modul Live sind aufrufbar, aber keine Methoden;
                # sie zu betreten ruft nichts auf.
                if callable(obj) and not (root == "Live" and isinstance(obj, type)):
                    raise ValueError("'%s' ist eine Methode; inspect_lom ruft keine Methoden auf." % tok)
        return obj

    # ------------------------------------------------------------------
    # Schreibende Befehle
    # ------------------------------------------------------------------

    def _cmd_create_midi_track(self, params):
        index = int(params.get("index", -1))
        self._song.create_midi_track(index)
        new_index = len(self._song.tracks) - 1 if index == -1 else index
        return {"index": new_index, "name": self._song.tracks[new_index].name}

    def _cmd_create_audio_track(self, params):
        index = int(params.get("index", -1))
        self._song.create_audio_track(index)
        new_index = len(self._song.tracks) - 1 if index == -1 else index
        return {"index": new_index, "name": self._song.tracks[new_index].name}

    def _cmd_set_track_name(self, params):
        track, ref = self._resolve_track(self._track_param(params))
        old = track.name
        track.name = params.get("name", "")
        return {"track": ref, "old_name": old, "name": track.name}

    def _cmd_create_clip(self, params):
        track, ref = self._resolve_track(self._track_param(params))
        slots = list(track.clip_slots)
        idx = params.get("clip_index", 0)
        if idx < 0 or idx >= len(slots):
            raise IndexError("Clip-Slot %d gibt es nicht." % idx)
        if slots[idx].has_clip:
            raise ValueError("Slot %d ist schon belegt." % idx)
        slots[idx].create_clip(float(params.get("length", 4.0)))
        return {"track": ref, "clip_index": idx, "name": slots[idx].clip.name, "length": slots[idx].clip.length}

    def _cmd_create_audio_clip(self, params):
        track, ref = self._resolve_track(self._track_param(params))
        path = params.get("path", "")
        if not path or not os.path.isabs(path):
            raise ValueError("Absoluter Pfad zur Audiodatei noetig (bekommen: '%s')." % path)
        if _safe(lambda: track.has_midi_input, False) or not _safe(lambda: track.has_audio_input, False):
            raise ValueError("'%s' ist keine Audiospur." % track.name)
        slots = list(track.clip_slots)
        idx = params.get("clip_index", 0)
        if idx < 0 or idx >= len(slots):
            raise IndexError("Clip-Slot %d gibt es nicht." % idx)
        if slots[idx].has_clip:
            raise ValueError("Slot %d ist schon belegt." % idx)
        if not hasattr(slots[idx], "create_audio_clip"):
            raise RuntimeError("ClipSlot.create_audio_clip fehlt; braucht Live 12.0.5 oder neuer.")
        slots[idx].create_audio_clip(path)
        clip = slots[idx].clip
        return {"track": ref, "clip_index": idx, "name": clip.name, "length": clip.length}

    def _cmd_add_notes_to_clip(self, params):
        track, ref = self._resolve_track(self._track_param(params))
        clip, source, idx = self._resolve_clip(track, params)
        if not _safe(lambda: bool(clip.is_midi_clip), False):
            raise ValueError("Clip '%s' ist kein MIDI-Clip." % clip.name)
        notes = params.get("notes") or []
        before = len(self._notes_from_clip(clip))
        report = self._add_notes(clip, notes)
        after = len(self._notes_from_clip(clip))
        result = {"track": ref, "track_name": track.name, "source": source, "clip_index": idx,
                  "clip_name": clip.name, "notes_sent": len(notes), "notes_before": before, "notes_after": after}
        result.update(report)
        return result

    def _add_notes(self, clip, notes):
        """Schreibt Noten und sagt, welche Felder Live NICHT bekommen hat.

        Bis technoa.7 gab das Script nur probability weiter; velocity_deviation
        und release_velocity fielen still weg, ebenso Tippfehler wie
        'start_beat'. Man hoerte dann einen anderen Clip als beabsichtigt
        (am Set 22.10.2026). Jetzt steht jedes nicht geschriebene Feld mit
        Grund in 'not_written'. Ob Live das Geschriebene auch so speichert,
        liest der Server danach eigens nach.
        """
        unknown = {}
        used = {}
        for n in notes:
            for k, v in n.items():
                if k in NOTE_EXTRA_FIELDS:
                    if v is not None:
                        used[k] = used.get(k, 0) + 1
                elif k not in NOTE_BASE_FIELDS:
                    unknown[k] = unknown.get(k, 0) + 1
        extras = [k for k in NOTE_EXTRA_FIELDS if k in used]
        not_written = [{"field": k, "notes": c, "reason": "unbekanntes Feld"} for k, c in sorted(unknown.items())]
        fallback_reason = None
        # add_new_notes ist seit Live 11 der vorgesehene Weg; set_notes ist
        # veraltet, aber noch vorhanden und war der Weg im Original.
        if hasattr(clip, "add_new_notes"):
            try:
                import Live
                specs, rejected = self._note_specs(Live, notes, extras)
                clip.add_new_notes(specs)
                for k in extras:
                    if k in rejected:
                        not_written.append({"field": k, "notes": used[k], "reason": "von Live abgelehnt: " + rejected[k]})
                return {"method": "add_new_notes", "not_written": not_written,
                        "fields_written": list(NOTE_BASE_FIELDS) + [k for k in extras if k not in rejected]}
            except Exception as e:
                fallback_reason = str(e) or e.__class__.__name__
                self.log_message("add_new_notes fehlgeschlagen, nehme set_notes: " + fallback_reason)
        live_notes = tuple(tuple(self._note_kwargs(n, [])[k] for k in NOTE_BASE_FIELDS) for n in notes)
        clip.set_notes(live_notes)
        for k in extras:
            not_written.append({"field": k, "notes": used[k], "reason": "Rueckfall auf set_notes, das kennt das Feld "
                                "nicht" + (" (add_new_notes: %s)" % fallback_reason if fallback_reason else
                                           " (add_new_notes fehlt in dieser Live-Version)")})
        return {"method": "set_notes", "fallback_reason": fallback_reason, "not_written": not_written,
                "fields_written": list(NOTE_BASE_FIELDS)}

    def _note_kwargs(self, n, extras):
        kwargs = dict(pitch=int(n.get("pitch", 60)), start_time=float(n.get("start_time", 0.0)),
                      duration=float(n.get("duration", 0.25)), velocity=float(n.get("velocity", 100)),
                      mute=bool(n.get("mute", False)))
        for k in extras:
            if n.get(k) is not None:
                kwargs[k] = float(n[k])
        return kwargs

    def _note_specs(self, Live, notes, extras):
        """MidiNoteSpecification je Note. Lehnt Live ein Zusatzfeld ab, faellt
        nur dieses weg, nicht gleich alle: Welches es war, verraet Lives
        Fehlertext nicht verlaesslich, also wird jedes Feld einzeln versucht.
        Scheitern auch die Grundfelder allein, geht der Fehler nach oben."""
        rejected = {}
        while True:
            fields = [k for k in extras if k not in rejected]
            try:
                return tuple(Live.Clip.MidiNoteSpecification(**self._note_kwargs(n, fields)) for n in notes), rejected
            except Exception as e:
                culprit = None
                for k in fields:
                    try:
                        for n in notes:
                            Live.Clip.MidiNoteSpecification(**self._note_kwargs(n, [k]))
                    except Exception as e_k:
                        culprit, e = k, e_k
                        break
                if culprit is None:
                    raise
                rejected[culprit] = str(e) or e.__class__.__name__

    def _cmd_clear_notes_from_clip(self, params):
        track, ref = self._resolve_track(self._track_param(params))
        clip, source, idx = self._resolve_clip(track, params)
        if not _safe(lambda: bool(clip.is_midi_clip), False):
            raise ValueError("Clip '%s' ist kein MIDI-Clip." % clip.name)
        before = len(self._notes_from_clip(clip))
        lo, span = self._note_time_window(clip)
        if hasattr(clip, "remove_notes_extended"):
            clip.remove_notes_extended(0, 128, lo, span)
        else:
            clip.remove_notes(lo, 0, span, 128)
        return {"track": ref, "source": source, "clip_index": idx, "clip_name": clip.name,
                "cleared_count": before, "notes_after": len(self._notes_from_clip(clip))}

    def _cmd_set_clip_name(self, params):
        track, ref = self._resolve_track(self._track_param(params))
        clip, source, idx = self._resolve_clip(track, params)
        old = clip.name
        clip.name = params.get("name", "")
        return {"track": ref, "source": source, "clip_index": idx, "old_name": old, "name": clip.name}

    def _cmd_set_arrangement_clip_name(self, params):
        p = dict(params)
        p["source"] = "arrangement"
        return self._cmd_set_clip_name(p)

    def _cmd_set_clip_muted(self, params):
        track, ref = self._resolve_track(self._track_param(params))
        indices = params.get("clip_indices")
        if indices is None:
            indices = [params.get("clip_index")]
        muted = bool(params.get("muted", True))
        changed = []
        for idx in indices:
            p = dict(params)
            p["clip_index"] = idx
            clip, source, i = self._resolve_clip(track, p)
            before = bool(clip.muted)
            clip.muted = muted
            changed.append({"clip_index": i, "name": clip.name, "before": before, "after": bool(clip.muted)})
        return {"track": ref, "track_name": track.name, "source": params.get("source", "session"), "clips": changed}

    def _cmd_delete_clip(self, params):
        track, ref = self._resolve_track(self._track_param(params))
        slots = list(track.clip_slots)
        idx = params.get("clip_index", 0)
        if idx < 0 or idx >= len(slots):
            raise IndexError("Clip-Slot %d gibt es nicht." % idx)
        if not slots[idx].has_clip:
            return {"deleted": False, "reason": "Slot war schon leer"}
        name = slots[idx].clip.name
        slots[idx].delete_clip()
        return {"deleted": True, "track": ref, "clip_index": idx, "name": name}

    def _cmd_delete_arrangement_clip(self, params):
        """Loescht einen Arrangement-Clip ueber Track.delete_clip.

        Die erwartete Startposition ist Pflicht: Nach jedem Loeschen ruecken
        die Indizes nach, und auf KICK BODY heissen 500 Clips gleich. Ein
        alter Index zeigte dann still auf den naechsten Clip. Geprueft wird im
        selben Durchlauf wie das Loeschen. Inhalt und Eigenschaften gehen in
        die Antwort, als Rueckweg, falls Lives Undo nicht reicht.
        """
        track, ref = self._resolve_track(self._track_param(params))
        kind = self._track_kind(track, ref)
        if kind not in ("midi", "audio"):
            raise ValueError("'%s' ist eine %s-Spur und hat keine Arrangement-Clips." % (track.name, kind))
        if params.get("clip_index") is None or params.get("expect_start") is None:
            raise ValueError("clip_index und expect_start (Startposition in Beats) sind beide Pflicht.")
        clips = list(track.arrangement_clips)
        idx = int(params["clip_index"])
        expect = float(params["expect_start"])
        starts = [_safe(lambda: float(c.start_time)) for c in clips]
        if idx < 0 or idx >= len(clips):
            raise IndexError("Arrangement-Clip %d gibt es auf '%s' nicht (%d Clips, 0 bis %d). Nichts geloescht."
                             % (idx, track.name, len(clips), len(clips) - 1))
        if starts[idx] is None or abs(starts[idx] - expect) > 1e-3:
            there = [i for i, s in enumerate(starts) if s is not None and abs(s - expect) <= 1e-3]
            raise ValueError("Arrangement-Clip #%d auf '%s' beginnt bei Beat %s, nicht bei %g. Nichts geloescht.%s"
                             % (idx, track.name, "?" if starts[idx] is None else "%g" % starts[idx], expect,
                                (" Bei Beat %g beginnt #%d." % (expect, there[0])) if there
                                else " Bei Beat %g beginnt kein Clip." % expect))
        clip = clips[idx]
        expect_name = params.get("expect_name")
        if expect_name is not None and (clip.name or "").strip().lower() != str(expect_name).strip().lower():
            raise ValueError("Arrangement-Clip #%d auf '%s' heisst '%s', nicht '%s'. Nichts geloescht."
                             % (idx, track.name, clip.name, expect_name))
        delete = self._api(track, "delete_clip", "Track.delete_clip")
        info = self._backup_info(clip, idx, track)
        delete(clip)
        return {"track": ref, "track_name": track.name, "deleted": info, "clip_count_before": len(clips),
                "clip_count_after": len(list(track.arrangement_clips))}

    def _backup_info(self, clip, idx, track):
        """Alles, was ein geloeschter Clip als Rueckweg braucht: Eigenschaften,
        Noten und seit technoa.9 die Huellkurven als abgetastete Werte."""
        info = self._clip_info(clip, idx, with_digest=True)
        if info.get("is_midi"):
            info["notes"] = self._notes_from_clip(clip)
        if info.get("has_envelopes"):
            info["envelope_samples"] = _safe(lambda: self._clip_envelopes(clip, track, samples=True))
        return info

    def _cmd_delete_arrangement_clips(self, params):
        """Loescht alle Arrangement-Clips, die in [from_time, to_time) beginnen,
        in einem Undo-Schritt (acht gleiche Hat-Clips waren acht Aufrufe).

        expect_count ist Pflicht: Der Server hat den Bereich vorher gelesen
        und gezeigt. Liegt jetzt eine andere Anzahl darin, hat sich etwas
        dazwischen geaendert, und es wird nichts geloescht.
        """
        track, ref = self._resolve_track(self._track_param(params))
        kind = self._track_kind(track, ref)
        if kind not in ("midi", "audio"):
            raise ValueError("'%s' ist eine %s-Spur und hat keine Arrangement-Clips." % (track.name, kind))
        if params.get("from_time") is None or params.get("to_time") is None or params.get("expect_count") is None:
            raise ValueError("from_time, to_time (Beats) und expect_count sind Pflicht.")
        start, end = float(params["from_time"]), float(params["to_time"])
        if end <= start:
            raise ValueError("Bereich leer: to_time %g liegt nicht hinter from_time %g." % (end, start))
        clips = list(track.arrangement_clips)
        hits = []
        for i, clip in enumerate(clips):
            s = _safe(lambda: float(clip.start_time))
            if s is not None and start - 1e-6 <= s < end - 1e-6:
                hits.append((i, clip))
        expect = int(params["expect_count"])
        if len(hits) != expect:
            raise ValueError("Auf '%s' beginnen jetzt %d Clips im Bereich, erwartet waren %d. Nichts geloescht; "
                             "Bereich neu lesen." % (track.name, len(hits), expect))
        if not hits:
            raise ValueError("Auf '%s' beginnt im Bereich kein Clip. Nichts geloescht." % track.name)
        delete = self._api(track, "delete_clip", "Track.delete_clip")
        infos = [self._backup_info(clip, i, track) for i, clip in hits]
        deleted, warnings = [], []
        for (i, clip), info in zip(hits, infos):
            # Ueber das Objekt, nicht den Index: Nach jedem Loeschen ruecken
            # die Indizes nach.
            try:
                delete(clip)
            except Exception as e:
                if not deleted:
                    raise
                warnings.append("Clip #%d bei Beat %s nicht geloescht: %s. Davor geloescht: %d von %d."
                                % (i, info.get("start_time"), e, len(deleted), len(hits)))
                break
            deleted.append(info)
        out = {"track": ref, "track_name": track.name, "deleted": deleted, "from_time": start, "to_time": end,
               "clip_count_before": len(clips), "clip_count_after": len(list(track.arrangement_clips))}
        if warnings:
            out["warnings"] = warnings
        return out

    def _cmd_set_tempo(self, params):
        old = self._song.tempo
        self._song.tempo = float(params.get("tempo", 120.0))
        return {"old_tempo": old, "tempo": self._song.tempo}

    def _cmd_set_loop(self, params):
        song = self._song
        before = {"loop": bool(song.loop), "loop_start": float(song.loop_start), "loop_length": float(song.loop_length)}
        if params.get("loop_start") is not None:
            song.loop_start = float(params["loop_start"])
        if params.get("loop_length") is not None:
            song.loop_length = float(params["loop_length"])
        if params.get("enabled") is not None:
            song.loop = bool(params["enabled"])
        after = {"loop": bool(song.loop), "loop_start": float(song.loop_start), "loop_length": float(song.loop_length)}
        return {"before": before, "after": after}

    def _cmd_fire_clip(self, params):
        track, ref = self._resolve_track(self._track_param(params))
        slots = list(track.clip_slots)
        idx = params.get("clip_index", 0)
        if idx < 0 or idx >= len(slots) or not slots[idx].has_clip:
            raise ValueError("Im Session-Slot %d liegt kein Clip." % idx)
        slots[idx].fire()
        return {"fired": True, "track": ref, "clip_index": idx}

    def _cmd_stop_clip(self, params):
        track, ref = self._resolve_track(self._track_param(params))
        slots = list(track.clip_slots)
        idx = params.get("clip_index", 0)
        if idx < 0 or idx >= len(slots):
            raise IndexError("Clip-Slot %d gibt es nicht." % idx)
        slots[idx].stop()
        return {"stopped": True, "track": ref, "clip_index": idx}

    def _cmd_start_playback(self, params):
        self._song.start_playing()
        return {"playing": True}

    def _cmd_stop_playback(self, params):
        self._song.stop_playing()
        return {"playing": False}

    def _cmd_switch_to_arrangement_view(self, params):
        self.application().view.show_view("Arranger")
        return {"view": "Arranger"}

    def _cmd_set_current_song_time(self, params):
        self._song.current_song_time = float(params.get("time", 0.0))
        return {"current_song_time": float(self._song.current_song_time)}

    def _cmd_duplicate_session_clip_to_arrangement(self, params):
        """Session-Clip ins Arrangement, auf Wunsch mehrfach (repeat), ein Undo-Schritt.

        Die Kopien liegen im Abstand step (Beats, Standard: Cliplaenge, also
        lueckenlos). Bis technoa.7 war es je Kopie ein Aufruf und ein
        Undo-Schritt, ein 2-Takter ueber 32 Takte also 16 (am Set 22.10.2026).
        Alle Kopien entstehen im selben Durchlauf, sonst schliesst Live den
        Undo-Schritt dazwischen. Ob im Zielbereich schon Clips liegen,
        prueft der Server vorher.
        """
        track, ref = self._resolve_track(self._track_param(params))
        slots = list(track.clip_slots)
        idx = params.get("clip_index", 0)
        if idx < 0 or idx >= len(slots) or not slots[idx].has_clip:
            raise ValueError("Im Session-Slot %d liegt kein Clip." % idx)
        clip = slots[idx].clip
        dest = float(params.get("destination_time", 0.0))
        repeat = int(params.get("repeat", 1))
        if repeat < 1 or repeat > MAX_REPEAT:
            raise ValueError("repeat=%d geht nicht; erlaubt sind 1 bis %d Kopien. Nichts kopiert." % (repeat, MAX_REPEAT))
        length = float(clip.length)
        step = float(params["step"]) if params.get("step") is not None else length
        if repeat > 1 and step < length - 1e-6:
            raise ValueError("Abstand %g Beats ist kuerzer als der Clip (%g Beats): Jede Kopie wuerde die vorige "
                             "ueberschreiben. Nichts kopiert." % (step, length))
        destinations = [dest + i * step for i in range(repeat)]
        for t in destinations:
            track.duplicate_clip_to_arrangement(clip, t)
        return {"track": ref, "track_name": track.name, "clip_name": clip.name, "destination_time": dest,
                "repeat": repeat, "step": step, "clip_length": length, "destinations": destinations}

    def _cue_at(self, target, tol=1e-3):
        for cue in self._song.cue_points:
            if abs(cue.time - target) < tol:
                return cue
        return None

    def _staged_create_locator(self, params):
        """Locator setzen: Playhead hin, auf Live warten, Cue setzen, Playhead zurueck.

        set_or_delete_cue wirkt an der Playhead-Position. Live uebernimmt ein
        gesetztes current_song_time bei gestopptem Transport aber erst im
        naechsten Durchlauf. In einem Durchlauf landete der Locator am
        07.10.2026 an der alten Playhead-Position statt am Ziel.
        """
        song = self._song
        target = float(params.get("time", 0.0))
        name = params.get("name", "")
        if song.is_playing:
            raise ValueError("Locator bei laufendem Transport nicht moeglich: Live setzt ihn an die "
                             "wandernde Playhead-Position. Erst stop_playback.")

        def rename(cue, created):
            if name:
                cue.name = str(name)
            return {"time": float(cue.time), "name": cue.name, "created": created}

        existing = self._cue_at(target)
        if existing is not None:
            return self._in_undo_step(lambda: rename(existing, False))
        length = float(song.song_length)
        if target > length + 1e-6:
            raise ValueError("Beat %g liegt hinter dem Songende (Beat %g). Live setzt den Playhead nicht "
                             "hinter das Songende, also auch keinen Locator dort." % (target, length))
        original = float(song.current_song_time)
        song.current_song_time = target
        try:
            for _ in range(MAX_WAIT_TICKS):
                yield
                if abs(float(song.current_song_time) - target) <= 1e-3:
                    break
            else:
                raise RuntimeError("Playhead steht bei Beat %g statt %g; Locator nicht gesetzt. Erneut versuchen."
                                   % (float(song.current_song_time), target))

            def place():
                song.set_or_delete_cue()
                cue = self._cue_at(target)
                if cue is None:
                    raise RuntimeError("Locator bei Beat %g liess sich nicht anlegen." % target)
                return rename(cue, True)
            return self._in_undo_step(place)
        finally:
            _safe(lambda: setattr(song, "current_song_time", original))

    def _cmd_re_enable_automation(self, params):
        song = self._song
        before = bool(song.re_enable_automation_enabled)
        song.re_enable_automation()
        return {"was_overridden": before}

    def _cmd_back_to_arrangement(self, params):
        song = self._song
        before = bool(song.back_to_arranger)
        song.back_to_arranger = False
        return {"was_lit": before}

    def _cmd_set_device_parameter(self, params):
        track, ref = self._resolve_track(self._track_param(params))
        device = self._resolve_device(track, params)
        plist = list(device.parameters)
        idx = params.get("parameter_index", 0)
        if idx < 0 or idx >= len(plist):
            raise IndexError("Parameter %d gibt es in '%s' nicht (%d Parameter)." % (idx, device.name, len(plist)))
        param = plist[idx]
        value = float(params.get("value", 0.0))
        if value < param.min or value > param.max:
            raise ValueError("Wert %g liegt ausserhalb von '%s' (%g bis %g)." % (value, param.name, param.min, param.max))
        before = self._param_info(param, idx)
        param.value = value
        return {"track": ref, "device": device.name, "before": before, "after": self._param_info(param, idx)}

    # ------------------------------------------------------------------
    # Geraete verwalten (technoa.5). Nur auf Spurebene, nicht in Racks.
    # ------------------------------------------------------------------

    def _chain_names(self, track):
        return [_safe(lambda: d.name, "?") for d in _safe(lambda: list(track.devices), []) or []]

    def _device_at(self, track, params):
        """Geraet per device_index, auf Wunsch mit Namenspruefung.

        expect_name schuetzt vor dem falschen Geraet: Nach jedem Loeschen oder
        Verschieben ruecken die Indizes nach, ein alter Index zeigt dann auf
        ein anderes Geraet. Geprueft wird hier im selben Durchlauf wie die
        Aenderung, nicht vorher im Server.
        """
        devices = list(track.devices)
        if params.get("device_index") is None:
            raise ValueError("device_index fehlt.")
        idx = int(params["device_index"])
        if idx < 0 or idx >= len(devices):
            raise IndexError("Geraet %d gibt es auf '%s' nicht (%d Geraete: %s)."
                             % (idx, track.name, len(devices), ", ".join(self._chain_names(track))))
        device = devices[idx]
        expect = params.get("expect_name")
        if expect is not None:
            name = _safe(lambda: device.name, "") or ""
            if name.strip().lower() != str(expect).strip().lower():
                raise ValueError("Auf '%s' liegt an Position %d '%s', nicht '%s'. Kette jetzt: %s. Nichts geaendert."
                                 % (track.name, idx, name, expect, ", ".join(
                                     "%d %s" % (i, n) for i, n in enumerate(self._chain_names(track)))))
        return device, idx, devices

    def _api(self, obj, name, what):
        method = getattr(obj, name, None)
        if method is None:
            raise RuntimeError("Diese Live-Version bietet %s nicht an. Mit inspect_lom nachsehen, was das Objekt "
                               "hier kann (die Geraetebefehle gab es in Live 12.4.6, gesehen am 11.10.2026)." % what)
        return method

    def _new_devices(self, before, after):
        return [i for i, d in enumerate(after) if self._index_in(before, d) is None]

    def _cmd_delete_device(self, params):
        track, ref = self._resolve_track(self._track_param(params))
        device, idx, _ = self._device_at(track, params)
        delete = self._api(track, "delete_device", "Track.delete_device")
        # Alles vorher lesen: Ob Lives Undo den inneren Zustand eines Plugins
        # vollstaendig zurueckbringt, ist nicht belegt. Die Werte in der
        # Antwort sind dann der Rueckweg.
        summary = self._device_summary(device, idx)
        values = [self._param_info(p, i) for i, p in enumerate(_safe(lambda: list(device.parameters), []) or [])]
        before = self._chain_names(track)
        delete(idx)
        return {"track": ref, "track_name": track.name, "deleted": summary, "parameters": values,
                "chain_before": before, "chain_after": self._chain_names(track)}

    def _cmd_move_device(self, params):
        track, ref = self._resolve_track(self._track_param(params))
        device, idx, _ = self._device_at(track, params)
        if params.get("target_track") is not None:
            target, target_ref = self._resolve_track(params["target_track"])
        else:
            target, target_ref = track, ref
        same = self._same(track, target)
        target_count = len(list(target.devices))
        if params.get("target_index") is None:
            raise ValueError("target_index fehlt: Position, an der das Geraet danach stehen soll.")
        new_index = int(params["target_index"])
        limit = target_count - 1 if same else target_count
        if new_index < 0 or new_index > limit:
            raise IndexError("Zielposition %d gibt es nicht; erlaubt 0 bis %d." % (new_index, limit))
        if same and new_index == idx:
            raise ValueError("'%s' liegt schon an Position %d. Nichts geaendert." % (device.name, idx))
        # Live zaehlt beim Verschieben Luecken: 0 = vor dem ersten Geraet,
        # Anzahl = ans Ende, das bewegte Geraet mitgezaehlt. Nach aussen gilt
        # die Position NACH dem Verschieben, wie get_track_info sie dann zeigt.
        gap = new_index + 1 if (same and new_index > idx) else new_index
        song = self._song
        mover = self._api(song, "move_device", "Song.move_device")
        finder = getattr(song, "find_device_position", None)
        if finder is not None:
            valid = int(finder(device, target, gap))
            if valid != gap:
                raise ValueError("'%s' kann laut Live nicht an Position %d stehen (Luecke %d, Live schlaegt %d vor), "
                                 "z. B. ein Audioeffekt vor dem Instrument. Nichts geaendert."
                                 % (device.name, new_index, gap, valid))
        chain_before = self._chain_names(track)
        target_before = None if same else self._chain_names(target)
        returned = mover(device, target, gap)
        landed = self._index_in(list(target.devices), device)
        return {"track": ref, "track_name": track.name, "device": _safe(lambda: device.name, "?"),
                "from_index": idx, "target_track": target_ref, "target_track_name": target.name,
                "requested_index": new_index, "new_index": landed, "live_position": returned, "gap": gap,
                "chain_before": chain_before, "target_chain_before": target_before,
                "chain_after": self._chain_names(track),
                "target_chain_after": None if same else self._chain_names(target)}

    def _cmd_insert_device(self, params):
        track, ref = self._resolve_track(self._track_param(params))
        name = (params.get("device_name") or "").strip()
        if not name:
            raise ValueError("device_name fehlt, z. B. 'Utility' oder 'EQ Eight'.")
        insert = self._api(track, "insert_device", "Track.insert_device")
        before = list(track.devices)
        # Immer mit Position aufrufen: Ob der zweite Parameter in Lives
        # Python-API wirklich optional ist wie in der Max-Doku, ist ungeprueft.
        pos = params.get("position")
        pos = len(before) if pos is None else int(pos)
        if pos < 0 or pos > len(before):
            raise IndexError("Position %d gibt es nicht; erlaubt 0 bis %d." % (pos, len(before)))
        try:
            insert(name, pos)
        except Exception as e:
            # Live lehnt eine unmoegliche Position selbst ab ("A valid index
            # would be 1", 11.10.2026). Der Hinweis auf Live-eigene Geraete
            # fuehrte dann in die Irre.
            if "index" in str(e).lower():
                raise ValueError("Live lehnt Position %d fuer '%s' ab (%s), z. B. ein Audioeffekt vor dem "
                                 "Instrument. Nichts geaendert." % (pos, name, e))
            raise ValueError("Live konnte '%s' nicht einfuegen (%s). insert_device kennt nur Live-eigene Geraete "
                             "unter ihrem Namen, z. B. 'Utility', 'EQ Eight', 'Shifter'. Plugins ueber "
                             "load_instrument_or_effect mit position." % (name, e))
        new = self._new_devices(before, list(track.devices))
        return {"track": ref, "track_name": track.name, "device_name": name, "requested_position": pos,
                "new_index": new[0] if len(new) == 1 else None, "new_indices": new,
                "chain_after": self._chain_names(track)}

    def _cmd_duplicate_device(self, params):
        track, ref = self._resolve_track(self._track_param(params))
        device, idx, before = self._device_at(track, params)
        duplicate = self._api(track, "duplicate_device", "Track.duplicate_device")
        duplicate(idx)
        new = self._new_devices(before, list(track.devices))
        return {"track": ref, "track_name": track.name, "device": _safe(lambda: device.name, "?"), "from_index": idx,
                "new_index": new[0] if len(new) == 1 else None, "new_indices": new,
                "chain_after": self._chain_names(track)}

    def _cmd_set_mixer(self, params):
        """Setzt Mixerwerte einer Spur in einem Undo-Schritt.

        Fuer Pegel in dB gibt es keine dokumentierte Umrechnungsformel. Die
        Kurve von Lives Lautstaerkeregler ist nicht linear. Deshalb wird der
        Reglerwert gesucht, dessen Anzeigetext dem Ziel am naechsten kommt.
        """
        track, ref = self._resolve_track(self._track_param(params))
        mixer = track.mixer_device
        changes = []

        def apply(label, param, value=None, db=None):
            before = self._param_info(param)
            if db is not None:
                value = self._value_for_db(param, float(db))
            value = float(value)
            if value < param.min or value > param.max:
                raise ValueError("%s: Wert %g ausserhalb %g bis %g." % (label, value, param.min, param.max))
            param.value = value
            changes.append({"target": label, "before": before, "after": self._param_info(param)})

        if params.get("volume_db") is not None:
            apply("volume", mixer.volume, db=params["volume_db"])
        elif params.get("volume") is not None:
            apply("volume", mixer.volume, value=params["volume"])
        if params.get("panning") is not None:
            apply("panning", mixer.panning, value=params["panning"])
        sends = list(_safe(lambda: mixer.sends, []) or [])
        for s in params.get("sends") or []:
            i = int(s.get("index", 0))
            if i < 0 or i >= len(sends):
                raise IndexError("Send %d gibt es nicht (%d Sends)." % (i, len(sends)))
            if s.get("db") is not None:
                apply("send:%d" % i, sends[i], db=s["db"])
            else:
                apply("send:%d" % i, sends[i], value=s.get("value", 0.0))
        for flag in ("mute", "solo", "arm"):
            if params.get(flag) is not None:
                if flag == "arm" and not _safe(lambda: bool(track.can_be_armed), False):
                    raise ValueError("'%s' laesst sich nicht scharf schalten." % track.name)
                before = bool(getattr(track, flag))
                setattr(track, flag, bool(params[flag]))
                changes.append({"target": flag, "before": before, "after": bool(getattr(track, flag))})
        if not changes:
            raise ValueError("Nichts zu setzen: volume_db, volume, panning, sends, mute, solo oder arm angeben.")
        return {"track": ref, "track_name": track.name, "changes": changes}

    def _parse_db(self, text):
        t = (text or "").strip().lower().replace("db", "").replace(chr(0x2212), "-").strip()
        if t in ("-inf", "-" + chr(0x221E)):
            return float("-inf")
        return float(t)

    def _value_for_db(self, param, target_db):
        lo, hi = float(param.min), float(param.max)
        if target_db == float("-inf"):
            return lo
        db_hi = self._parse_db(param.str_for_value(hi))
        if target_db >= db_hi:
            return hi
        # Bisektion ueber den Anzeigetext: die Kurve steigt monoton.
        for _ in range(50):
            mid = (lo + hi) / 2.0
            if self._parse_db(param.str_for_value(mid)) < target_db:
                lo = mid
            else:
                hi = mid
        return hi

    # ------------------------------------------------------------------
    # Clip-Huellkurven (technoa.9)
    # ------------------------------------------------------------------

    def _send_letter(self, i):
        return chr(ord("A") + i) if 0 <= i < 26 else str(i)

    def _envelope_target(self, track, params):
        """Parameter einer Clip-Huellkurve -> (param, Beschreibung).

        Mixer ueber mixer = "volume" | "panning" | "activator" | "send:N",
        sonst device_index (+ chain_path) und parameter_index wie bei
        set_device_parameter. Die Beschriftung ist dieselbe wie im Abdruck,
        damit Server und Schnappschuss dieselbe Kurve wiederfinden.
        """
        key = params.get("mixer")
        if key:
            mixer = track.mixer_device
            key = str(key).strip().lower()
            if key == "volume":
                return mixer.volume, {"mixer": key, "label": "Mixer: Volume"}
            if key == "panning":
                return mixer.panning, {"mixer": key, "label": "Mixer: Pan"}
            if key == "activator":
                return mixer.track_activator, {"mixer": key, "label": "Mixer: Spur an"}
            if key.startswith("send:"):
                sends = list(_safe(lambda: mixer.sends, []) or [])
                i = int(key.split(":", 1)[1])
                if i < 0 or i >= len(sends):
                    raise IndexError("Send %d gibt es auf '%s' nicht (%d Sends)." % (i, track.name, len(sends)))
                return sends[i], {"mixer": key, "label": "Mixer: Send %s" % self._send_letter(i)}
            raise ValueError("mixer '%s' unbekannt. Erlaubt: volume, panning, activator, send:N." % key)
        if params.get("device_index") is None or params.get("parameter_index") is None:
            raise ValueError("Parameter der Huellkurve fehlt: mixer ('volume', 'panning', 'activator', 'send:N') "
                             "oder device_index mit parameter_index.")
        device = self._resolve_device(track, params)
        plist = list(device.parameters)
        idx = int(params["parameter_index"])
        if idx < 0 or idx >= len(plist):
            raise IndexError("Parameter %d gibt es in '%s' nicht (%d Parameter)." % (idx, device.name, len(plist)))
        path = [[int(c), int(d)] for c, d in (params.get("chain_path") or [])]
        target = {"device_index": int(params["device_index"]), "chain_path": path, "parameter_index": idx,
                  "device": _safe(lambda: device.name, "?"), "parameter": _safe(lambda: plist[idx].name, "?")}
        target["label"] = self._device_param_label(target)
        return plist[idx], target

    def _device_param_label(self, t):
        where = "%d" % t["device_index"] + "".join(" K%d.%d" % (c, d) for c, d in t.get("chain_path") or [])
        return "[%s] %s: %s" % (where, t["device"], t["parameter"])

    def _envelope_candidates(self, track):
        """Alle Parameter einer Spur, an denen eine Clip-Huellkurve haengen
        kann: Mixer, Geraete und Geraete in Racks bis ENVELOPE_SCAN_DEPTH.
        Lives API hat keine Liste der Huellkurven eines Clips, nur die Frage
        je Parameter."""
        out = []
        mixer = _safe(lambda: track.mixer_device)
        if mixer is not None:
            for key in ("volume", "panning", "activator"):
                p = _safe(lambda: self._envelope_target(track, {"mixer": key}))
                if p is not None:
                    out.append((p[1], p[0]))
            for i in range(len(_safe(lambda: list(mixer.sends), []) or [])):
                p = _safe(lambda: self._envelope_target(track, {"mixer": "send:%d" % i}))
                if p is not None:
                    out.append((p[1], p[0]))

        def walk(device, top, path, depth):
            name = _safe(lambda: device.name, "?")
            for j, p in enumerate(_safe(lambda: list(device.parameters), []) or []):
                t = {"device_index": top, "chain_path": list(path), "parameter_index": j, "device": name,
                     "parameter": _safe(lambda: p.name, "?")}
                t["label"] = self._device_param_label(t)
                out.append((t, p))
            if depth >= ENVELOPE_SCAN_DEPTH:
                return
            for c, chain in enumerate(_safe(lambda: list(device.chains), []) or []):
                for d, inner in enumerate(_safe(lambda: list(chain.devices), []) or []):
                    walk(inner, top, path + [[c, d]], depth + 1)

        for i, device in enumerate(_safe(lambda: list(track.devices), []) or []):
            walk(device, i, [], 0)
        return out

    def _envelope_window(self, clip):
        """Bereich in Clip-Zeit (Beats wie bei den Noten), in dem eine
        Huellkurve wirkt: vom Start- oder Loopanfang bis zum End- oder
        Loopende, je nachdem, was weiter reicht."""
        lo = min(_safe(lambda: float(clip.start_marker), 0.0) or 0.0,
                 _safe(lambda: float(clip.loop_start), 0.0) or 0.0)
        hi = max(_safe(lambda: float(clip.end_marker), 0.0) or 0.0,
                 _safe(lambda: float(clip.loop_end), 0.0) or 0.0)
        if hi <= lo:
            hi = lo + (_safe(lambda: float(clip.length), 0.0) or 0.0)
        return lo, hi

    def _envelope_times(self, lo, hi, grid):
        if grid <= 0:
            raise ValueError("Raster %g ist nicht groesser als 0." % grid)
        n = int((hi - lo) / grid + 1e-9)
        if (hi - lo) - n * grid > 1e-9:
            n += 1
        n = max(n, 1)
        if n > MAX_ENVELOPE_SAMPLES:
            raise ValueError("%d Werte im Raster %g Beats sind zu viele (hoechstens %d). Groberes Raster oder "
                             "kleineren Bereich waehlen." % (n, grid, MAX_ENVELOPE_SAMPLES))
        return [lo + k * grid for k in range(n)]

    def _envelope_samples(self, env, param, lo, hi, grid, with_display=True):
        times = self._envelope_times(lo, hi, grid)
        values = [round(float(env.value_at_time(t)), 6) for t in times]
        out = {"from": lo, "to": hi, "grid": grid, "values": values}
        if with_display:
            shown = {}
            for v in values:
                if v not in shown and len(shown) < MAX_ENVELOPE_SAMPLES:
                    shown[v] = _safe(lambda: param.str_for_value(v))
            out["displays"] = [shown.get(v) for v in values]
        return out

    def _envelope_digest(self, values):
        return "e%d-%08x" % (len(values), _fingerprint(";".join("%.4f" % v for v in values)))

    def _clip_envelopes(self, clip, track, samples=False, grid=ENVELOPE_GRID):
        """Alle Huellkurven eines Clips, die an einem Parameter der Spur
        haengen. MIDI-Controller-Huellkurven (Pitch Bend, CC) sind keine
        Parameter und bleiben unsichtbar. None, wenn Live die Frage nicht
        anbietet."""
        get = getattr(clip, "automation_envelope", None)
        if get is None:
            return None
        lo, hi = self._envelope_window(clip)
        found = []
        for target, param in self._envelope_candidates(track):
            env = _safe(lambda: get(param))
            if env is None:
                continue
            # Der Abdruck immer im festen Raster, sonst gaelte dieselbe Kurve
            # bei anderem Leseraster als geaendert.
            same_grid = abs(grid - ENVELOPE_GRID) < 1e-9
            base = self._envelope_samples(env, param, lo, hi, ENVELOPE_GRID, with_display=samples and same_grid)
            entry = dict(target)
            entry["digest"] = self._envelope_digest(base["values"])
            if samples:
                entry["samples"] = base if same_grid else self._envelope_samples(env, param, lo, hi, grid)
                entry["param"] = self._param_info(param, full=True)
            found.append(entry)
        return found

    def _envelope_api(self, clip, env):
        """Lives eigene Beschreibung der Huellkurven-Methoden (Boost-Docstrings
        mit Signatur). Damit laesst sich am Set klaeren, was diese Live-Version
        anbietet, ohne das Script erneut zu aendern."""
        def doc(obj, name):
            m = _safe(lambda: getattr(obj, name))
            if m is None:
                return None
            return (_safe(lambda: m.__doc__) or "")[:600] if callable(m) else "<Eigenschaft: %s>" % (
                _safe(lambda: str(m)[:80]),)
        out = {"clip": dict((n, doc(clip, n)) for n in ("automation_envelope", "create_automation_envelope",
                                                         "clear_envelope", "clear_all_envelopes", "has_envelopes"))}
        if env is not None:
            out["envelope_type"] = type(env).__name__
            out["envelope"] = dict((n, doc(env, n)) for n in dir(env) if not n.startswith("_"))
        return out

    def _cmd_get_clip_envelope(self, params):
        track, ref = self._resolve_track(self._track_param(params))
        clip, source, idx = self._resolve_clip(track, params)
        out = {"track": ref, "track_name": track.name, "source": source, "clip": self._clip_info(clip, idx)}
        grid = float(params.get("grid") or ENVELOPE_GRID)
        env = None
        if params.get("mixer") or params.get("device_index") is not None:
            param, target = self._envelope_target(track, params)
            get = self._api(clip, "automation_envelope", "Clip.automation_envelope")
            env = get(param)
            lo, hi = self._envelope_window(clip)
            out["target"] = target
            out["param"] = self._param_info(param, full=True)
            out["exists"] = env is not None
            if env is not None:
                out["samples"] = self._envelope_samples(env, param, lo, hi, grid)
        else:
            found = self._clip_envelopes(clip, track, samples=True, grid=grid)
            if found is None:
                raise RuntimeError("Diese Live-Version bietet Clip.automation_envelope nicht an.")
            out["envelopes"] = found
        if params.get("probe"):
            out["api"] = self._envelope_api(clip, env)
        return out

    def _cmd_set_clip_envelope(self, params):
        """Schreibt Stufen (time, duration, value; Clip-Zeit in Beats,
        Rohwerte) in die Huellkurve eines Parameters, ein Undo-Schritt.

        Erst wird alles geprueft, dann geaendert. Die Werte kommen als
        Rohwerte; Anzeigewerte rechnet der Server vorher um. replace=True
        loescht die vorhandene Kurve dieses Parameters vorher.
        """
        track, ref = self._resolve_track(self._track_param(params))
        clip, source, idx = self._resolve_clip(track, params)
        param, target = self._envelope_target(track, params)
        steps = params.get("steps") or []
        if not steps:
            raise ValueError("steps fehlt: Liste von {time, duration, value} in Clip-Beats und Rohwerten.")
        if len(steps) > MAX_ENVELOPE_STEPS:
            raise ValueError("%d Stufen sind zu viele (hoechstens %d in einem Befehl)." % (len(steps),
                                                                                    MAX_ENVELOPE_STEPS))
        lo_v, hi_v = float(param.min), float(param.max)
        clean = []
        for i, s in enumerate(steps):
            t, d, v = float(s["time"]), float(s.get("duration", 0.0)), float(s["value"])
            if d < 0:
                raise ValueError("Stufe %d: Dauer %g ist negativ. Nichts geschrieben." % (i, d))
            if v < lo_v - 1e-9 or v > hi_v + 1e-9:
                raise ValueError("Stufe %d: Wert %g liegt ausserhalb von '%s' (%g bis %g). Nichts geschrieben."
                                 % (i, v, param.name, lo_v, hi_v))
            clean.append((t, d, min(max(v, lo_v), hi_v)))
        get = self._api(clip, "automation_envelope", "Clip.automation_envelope")
        create = self._api(clip, "create_automation_envelope", "Clip.create_automation_envelope")
        lo, hi = self._envelope_window(clip)
        grid = float(params.get("grid") or ENVELOPE_GRID)
        env = get(param)
        before = self._envelope_samples(env, param, lo, hi, grid) if env is not None else None
        replace = bool(params.get("replace"))
        if env is None:
            # Scheitert das, ist noch nichts geaendert; Fehler darf hoch.
            env = create(param)
            if env is None:
                raise RuntimeError("Live hat fuer '%s' in Clip '%s' keine Huellkurve angelegt "
                                   "(create_automation_envelope gab None). Nichts geschrieben."
                                   % (param.name, clip.name))
            created = True
        else:
            created = False
            if replace:
                clip.clear_envelope(param)
                env = create(param)
        warnings = []
        written = 0
        if env is None:
            warnings.append("Die alte Kurve ist geloescht, aber Live hat keine neue angelegt. undo holt die alte "
                            "zurueck.")
        else:
            for t, d, v in clean:
                try:
                    env.insert_step(t, d, v)
                except Exception as e:
                    warnings.append("Stufe %d (Beat %g) nicht geschrieben: %s. Davor geschrieben: %d von %d."
                                    % (written, t, e, written, len(clean)))
                    break
                written += 1
        after_env = _safe(lambda: get(param))
        out = {"track": ref, "track_name": track.name, "source": source, "clip_index": idx,
               "clip": self._clip_info(clip, idx), "target": target, "param": self._param_info(param, full=True),
               "created": created, "replaced": replace and not created, "steps_sent": len(clean),
               "steps_written": written, "before": before,
               "after": self._envelope_samples(after_env, param, lo, hi, grid) if after_env is not None else None}
        if warnings:
            out["warnings"] = warnings
        return out

    def _cmd_clear_clip_envelope(self, params):
        """Loescht die Huellkurve eines Parameters oder (all=True) alle
        Huellkurven des Clips. Die Werte vorher gehen in die Antwort."""
        track, ref = self._resolve_track(self._track_param(params))
        clip, source, idx = self._resolve_clip(track, params)
        out = {"track": ref, "track_name": track.name, "source": source, "clip_index": idx}
        if params.get("all"):
            clear_all = self._api(clip, "clear_all_envelopes", "Clip.clear_all_envelopes")
            found = self._clip_envelopes(clip, track, samples=True)
            if not _safe(lambda: bool(clip.has_envelopes), False):
                raise ValueError("Clip '%s' hat keine Huellkurven. Nichts geaendert." % clip.name)
            clear_all()
            out["cleared"] = found or []
        else:
            param, target = self._envelope_target(track, params)
            get = self._api(clip, "automation_envelope", "Clip.automation_envelope")
            clear = self._api(clip, "clear_envelope", "Clip.clear_envelope")
            env = get(param)
            if env is None:
                raise ValueError("Clip '%s' hat keine Huellkurve fuer %s. Nichts geaendert."
                                 % (clip.name, target["label"]))
            lo, hi = self._envelope_window(clip)
            entry = dict(target)
            entry["samples"] = self._envelope_samples(env, param, lo, hi, float(params.get("grid") or ENVELOPE_GRID))
            entry["param"] = self._param_info(param, full=True)
            clear(param)
            out["cleared"] = [entry]
            out["exists_after"] = _safe(lambda: get(param)) is not None
        out["clip"] = self._clip_info(clip, idx)
        return out

    # ------------------------------------------------------------------
    # Song-Einstellungen (technoa.9)
    # ------------------------------------------------------------------

    def _cmd_set_song_settings(self, params):
        """Globale Groove-Menge (Rohwert, 1.0 = 100 %) und Metronom."""
        song = self._song
        plan = []
        if params.get("groove_amount") is not None:
            plan.append(("groove_amount", float(params["groove_amount"])))
        if params.get("metronome") is not None:
            plan.append(("metronome", bool(params["metronome"])))
        if not plan:
            raise ValueError("Nichts zu setzen: groove_amount oder metronome angeben.")
        changes = []
        for key, value in plan:
            before = getattr(song, key)
            try:
                setattr(song, key, value)
            except Exception as e:
                if not changes:
                    raise
                changes.append({"target": key, "before": before, "error": str(e)})
                break
            changes.append({"target": key, "before": before, "after": getattr(song, key)})
        return {"changes": changes}

    def _cmd_undo(self, params):
        song = self._song
        steps = int(params.get("steps", 1))
        done = 0
        for _ in range(max(1, steps)):
            if not song.can_undo:
                break
            song.undo()
            done += 1
        return {"undone": done, "can_undo": bool(song.can_undo), "can_redo": bool(song.can_redo)}

    def _cmd_redo(self, params):
        song = self._song
        steps = int(params.get("steps", 1))
        done = 0
        for _ in range(max(1, steps)):
            if not song.can_redo:
                break
            song.redo()
            done += 1
        return {"redone": done, "can_undo": bool(song.can_undo), "can_redo": bool(song.can_redo)}

    # ------------------------------------------------------------------
    # Groove, Routing, Sidechain (technoa.7). Regel fuer alle drei: Fehler
    # nur, solange noch nichts geaendert ist. Danach wird ein Teilerfolg als
    # Warnung gemeldet; ein Fehler liesse in Lives Undo-Liste einen Schritt
    # zurueck, den der Server nicht mitzaehlt.
    # ------------------------------------------------------------------

    def _cmd_set_clip_groove(self, params):
        """Setzt oder entfernt den Groove aller Arrangement-Clips, die im Bereich beginnen.

        Am 19.10.2026 zweimal Handarbeit: SYNTH 32 Clips an, Hat 4 Clips
        aus. Ausgewaehlt wird im selben Durchlauf wie das Setzen, damit kein
        Index zwischendurch auf einen anderen Clip zeigt.
        """
        track, ref = self._resolve_track(self._track_param(params))
        kind = self._track_kind(track, ref)
        if kind not in ("midi", "audio"):
            raise ValueError("'%s' ist eine %s-Spur und hat keine Arrangement-Clips. Nichts geaendert."
                             % (track.name, kind))
        if "groove" not in params:
            raise ValueError("groove fehlt: Name aus dem Groove-Pool oder null zum Entfernen.")
        name = params.get("groove")
        groove = None
        if name is not None:
            pool = list(_safe(lambda: self._song.groove_pool.grooves, []) or [])
            hits = [g for g in pool if (_safe(lambda: g.name, "") or "") == name]
            if len(hits) != 1:
                raise ValueError("Groove '%s' liegt %s im Groove-Pool (dort: %s). Nichts geaendert." % (
                    name, "nicht" if not hits else "mehrfach", ", ".join(
                        "'%s'" % _safe(lambda: g.name, "?") for g in pool) or "nichts"))
            groove = hits[0]
        start = params.get("start_time")
        end = params.get("end_time")
        lo = float("-inf") if start is None else float(start)
        hi = float("inf") if end is None else float(end)
        chosen, reaching_in = [], []
        for i, clip in enumerate(list(track.arrangement_clips)):
            s = _safe(lambda: float(clip.start_time))
            e = _safe(lambda: float(clip.end_time))
            if s is None:
                continue
            if lo - 1e-6 <= s < hi - 1e-6:
                chosen.append((i, s, clip))
            elif s < lo and e is not None and e > lo + 1e-6:
                reaching_in.append(s)
        if not chosen:
            raise ValueError("Auf '%s' beginnt im Bereich kein Arrangement-Clip%s. Nichts geaendert." % (
                track.name, " (%d beginnen davor und reichen hinein)" % len(reaching_in) if reaching_in else ""))

        def current(clip):
            g = _safe(lambda: clip.groove)
            return None if g is None else _safe(lambda: g.name, "?")

        todo = [(i, s, clip, current(clip)) for i, s, clip in chosen]
        same = [s for i, s, clip, before in todo if before == name]
        todo = [t for t in todo if t[3] != name]
        if not todo:
            raise ValueError("Alle %d Clips im Bereich haben schon %s. Nichts geaendert." % (
                len(chosen), "den Groove '%s'" % name if name is not None else "keinen Groove"))
        changed, failed, how = [], [], set()
        for i, s, clip, before in todo:
            try:
                if groove is None:
                    how.add(self._clear_groove(clip))
                else:
                    clip.groove = groove
            except Exception as e:
                failed.append({"clip_index": i, "start_time": s, "before": before,
                               "error": str(e).strip().splitlines()[0] if str(e).strip() else e.__class__.__name__})
                continue
            changed.append({"clip_index": i, "start_time": s, "before": before, "after": current(clip)})
        if not changed:
            # Erst hier sicher: Nichts hat sich geaendert, also auch kein Undo-Schritt.
            raise ValueError("Live hat keinen der %d Clips angenommen (%s). Nichts geaendert.%s" % (
                len(failed), failed[0]["error"], " Entfernen geht ueber die API dieser Live-Version nicht; von Hand: "
                "Clips markieren, im Clip-Fenster Groove 'None' waehlen." if name is None else ""))
        return {"track": ref, "track_name": track.name, "groove": name, "range": [start, end],
                "changed": changed, "already": same, "failed": failed, "reaching_in": reaching_in,
                "method": sorted(how)}

    def _clear_groove(self, clip):
        """Groove entfernen. Live 12.4.6 lehnt clip.groove = None ab (am Set
        22.10.2026: die C++-Signatur verlangt einen Groove). Ob del geht, ist
        bis zum naechsten Test am Set offen; die Antwort nennt den Weg."""
        try:
            clip.groove = None
            return "none"
        except Exception as first:
            try:
                del clip.groove
            except Exception as second:
                raise RuntimeError("clip.groove = None: %s / del clip.groove: %s" % (
                    str(first).strip().splitlines()[0] if str(first).strip() else first.__class__.__name__,
                    str(second).strip() or second.__class__.__name__))
            return "del"

    def _routing_entry(self, rtype):
        entry = {"name": _safe(lambda: rtype.display_name, "?")}
        category = _safe(lambda: rtype.category)
        if category is not None:
            entry["category"] = _safe(lambda: str(category))
        attached = _safe(lambda: rtype.attached_object)
        # Nur Spuren: Ketten in Racks haben auch einen Mixer und koennten ueber
        # den Namensvergleich in _ref_of_track einer Spur zugeordnet werden.
        if attached is not None and type(attached).__name__ == "Track":
            ref = self._ref_of_track(attached)
            if ref is not None:
                entry["track"] = ref
        return entry

    def _routing_side(self, owner, side):
        avail_t, cur_t, avail_c, cur_c = ROUTING_ATTRS[side]
        current = _safe(lambda: getattr(owner, cur_t))
        if current is None:
            return None
        return {
            "type": self._routing_entry(current),
            "channel": _safe(lambda: getattr(owner, cur_c).display_name),
            "types": [self._routing_entry(t) for t in _safe(lambda: list(getattr(owner, avail_t)), []) or []],
            "channels": [_safe(lambda: c.display_name, "?")
                         for c in _safe(lambda: list(getattr(owner, avail_c)), []) or []],
        }

    def _routing_owner(self, params):
        """Spur oder, mit device_index, ein Geraet mit Sidechain-Eingang."""
        track, ref = self._resolve_track(self._track_param(params))
        if params.get("device_index") is None:
            return track, ref, None, None
        device, idx, _ = self._device_at(track, params)
        if _safe(lambda: device.input_routing_type) is None:
            raise ValueError("'%s' (Geraet %d auf '%s') hat ueber die API keinen Sidechain-Eingang. Lives Compressor "
                             "hat einen (input_routing_type); Plugins und Racks zeigen ihn nicht." % (
                                 device.name, idx, track.name))
        return track, ref, device, idx

    def _cmd_get_routing_options(self, params):
        track, ref, device, idx = self._routing_owner(params)
        out = {"track": ref, "track_name": track.name, "kind": self._track_kind(track, ref)}
        if device is not None:
            out["device_index"] = idx
            out["device"] = _safe(lambda: device.name, "?")
            out["input"] = self._routing_side(device, "input")
            out["sidechain"] = self._device_summary(device, idx).get("sidechain")
        else:
            out["output"] = self._routing_side(track, "output")
            out["input"] = self._routing_side(track, "input")
        return out

    def _pick(self, items, name, label, what):
        """Exakter Name (ohne Gross/klein), sonst eindeutiger Namensteil."""
        text = str(name).strip().lower()
        exact = [x for x in items if label(x).strip().lower() == text]
        if len(exact) == 1:
            return exact[0]
        hits = exact or [x for x in items if text in label(x).strip().lower()]
        if len(hits) == 1:
            return hits[0]
        names = ", ".join("'%s'" % label(x) for x in items) or "keine"
        if not hits:
            raise ValueError("%s '%s' gibt es hier nicht. Waehlbar: %s." % (what, name, names))
        raise ValueError("%s '%s' ist mehrdeutig: %s." % (what, name, ", ".join("'%s'" % label(x) for x in hits)))

    def _plan_routing(self, owner, side, type_name, type_track, channel):
        """Prueft eine Richtung, bevor irgendetwas gesetzt wird. Gibt den Plan oder None zurueck."""
        if type_name is None and channel is None:
            return None
        avail_t, cur_t, avail_c, cur_c = ROUTING_ATTRS[side]
        what = "Ausgang" if side == "output" else "Eingang"
        current = _safe(lambda: getattr(owner, cur_t))
        if current is None:
            raise ValueError("%s laesst sich hier nicht lesen oder setzen. Nichts geaendert." % what)
        plan = {"side": side, "before": {"type": _safe(lambda: current.display_name),
                                         "channel": _safe(lambda: getattr(owner, cur_c).display_name)},
                "type": None, "channel": channel}
        if type_name is not None:
            options = list(_safe(lambda: getattr(owner, avail_t), []) or [])
            label = lambda t: _safe(lambda: t.display_name, "") or ""  # noqa: E731
            if type_track is not None:
                hits = [t for t in options if self._routing_entry(t).get("track") == type_track]
                if len(hits) == 1:
                    plan["type"] = hits[0]
            if plan["type"] is None:
                plan["type"] = self._pick(options, type_name, label, what)
            # Gleicher Name reicht nicht: Zwei Spuren koennen gleich heissen.
            if self._same(plan["type"], current) or (
                    label(plan["type"]) == plan["before"]["type"]
                    and self._routing_entry(plan["type"]).get("track") == self._routing_entry(current).get("track")):
                plan["type"] = None
        if plan["type"] is None and channel is not None:
            # Kanal ohne neue Art: jetzt schon pruefbar.
            chans = list(_safe(lambda: getattr(owner, avail_c), []) or [])
            pick = self._pick(chans, channel, lambda c: _safe(lambda: c.display_name, "") or "", what + "skanal")
            if _safe(lambda: pick.display_name) == plan["before"]["channel"]:
                plan["channel"] = None
            else:
                plan["channel_obj"] = pick
        if plan["type"] is None and plan["channel"] is None:
            return None
        return plan

    def _apply_routing(self, owner, plan):
        avail_t, cur_t, avail_c, cur_c = ROUTING_ATTRS[plan["side"]]
        warnings = []
        if plan["type"] is not None:
            setattr(owner, cur_t, plan["type"])
        if plan.get("channel_obj") is not None:
            setattr(owner, cur_c, plan["channel_obj"])
        elif plan["channel"] is not None:
            # Nach einem Wechsel der Art bietet Live andere Kanaele an. Ob die
            # Liste im selben Durchlauf schon stimmt, ist am Set ungeprueft.
            chans = list(_safe(lambda: getattr(owner, avail_c), []) or [])
            try:
                pick = self._pick(chans, plan["channel"], lambda c: _safe(lambda: c.display_name, "") or "", "Kanal")
                setattr(owner, cur_c, pick)
            except Exception as e:
                warnings.append("Kanal nicht gesetzt: %s Live hat den Kanal '%s' gewaehlt." % (
                    e, _safe(lambda: getattr(owner, cur_c).display_name)))
        after = {"type": _safe(lambda: getattr(owner, cur_t).display_name),
                 "channel": _safe(lambda: getattr(owner, cur_c).display_name)}
        return {"side": plan["side"], "before": plan["before"], "after": after, "warnings": warnings}

    def _cmd_set_track_routing(self, params):
        """Ein- und Ausgang einer Spur in einem Undo-Schritt (am 19.10.2026
        ist beim Setzen von Hand der DRUMS-Ausgang auf Ext. Out gerutscht)."""
        track, ref = self._resolve_track(self._track_param(params))
        plans = []
        for side in ("output", "input"):
            plan = self._plan_routing(track, side, params.get(side + "_type"), params.get(side + "_type_track"),
                                      params.get(side + "_channel"))
            if plan is not None:
                plans.append(plan)
        if not plans:
            raise ValueError("'%s' ist schon so geroutet, oder es fehlt output_type/input_type/..._channel. "
                             "Nichts geaendert." % track.name)
        changes = [self._apply_routing(track, p) for p in plans]
        return {"track": ref, "track_name": track.name, "changes": changes}

    def _cmd_set_sidechain(self, params):
        """Sidechain-Quelle eines Geraets und auf Wunsch der Schalter 'S/C On', ein Undo-Schritt."""
        track, ref, device, idx = self._routing_owner(params)
        if device is None:
            raise ValueError("device_index fehlt: Sidechain gehoert zu einem Geraet, z. B. dem Compressor.")
        plan = self._plan_routing(device, "input", params.get("input_type"), params.get("input_type_track"),
                                  params.get("input_channel"))
        enable = params.get("enable")
        switch = None
        if enable is not None:
            switch = next((p for p in _safe(lambda: list(device.parameters), []) or []
                           if (_safe(lambda: p.name, "") or "").strip().lower() == SIDECHAIN_SWITCH), None)
            if switch is None:
                raise ValueError("'%s' hat keinen Parameter 'S/C On'; enable geht hier nicht. Nichts geaendert."
                                 % device.name)
            if bool(switch.value) == bool(enable):
                switch = None
        if plan is None and switch is None:
            raise ValueError("Sidechain von '%s' auf '%s' ist schon so eingestellt. Nichts geaendert."
                             % (device.name, track.name))
        before = self._device_summary(device, idx).get("sidechain")
        changes = [self._apply_routing(device, plan)] if plan is not None else []
        if switch is not None:
            switch.value = switch.max if enable else switch.min
        return {"track": ref, "track_name": track.name, "device_index": idx, "device": device.name,
                "before": before, "after": self._device_summary(device, idx).get("sidechain"), "changes": changes}

    # ------------------------------------------------------------------
    # Browser (aus dem Original uebernommen, auf den Hauptthread verlegt)
    # ------------------------------------------------------------------

    _BROWSER_ROOTS = ("instruments", "sounds", "drums", "audio_effects", "midi_effects")
    _URI_ROOT_HINTS = (
        ("plugins", ("vst:", "vst3:", "au:", "query:plugins", "plugin#")),
        ("max_for_live", ("max for live", "maxforlive", "m4l", "query:max")),
        ("user_library", ("user library", "userlibrary", "query:user library", "query:user-library")),
        ("packs", ("query:packs", "/packs/")),
        ("samples", ("query:samples", "sample:", "/samples/")),
        ("drums", ("query:drums", "/drums/")),
        ("instruments", ("query:instruments", "/instruments/")),
        ("sounds", ("query:sounds", "/sounds/")),
        ("audio_effects", ("query:audio effects", "audioeffects", "/audio_effects/")),
        ("midi_effects", ("query:midi effects", "midieffects", "/midi_effects/")),
    )

    def _browser(self):
        app = self.application()
        browser = _safe(lambda: app.browser)
        if browser is None:
            raise RuntimeError("Lives Browser ist nicht verfuegbar. Ist Live fertig geladen?")
        return browser

    def _browser_item_info(self, item):
        return {
            "name": _safe(lambda: item.name, "?"),
            "is_folder": bool(_safe(lambda: item.children, None)),
            "is_device": _safe(lambda: bool(item.is_device), False),
            "is_loadable": _safe(lambda: bool(item.is_loadable), False),
            "uri": _safe(lambda: item.uri),
        }

    def _cmd_get_browser_tree(self, params):
        browser = self._browser()
        category_type = params.get("category_type", "all")
        attrs = [a for a in dir(browser) if not a.startswith("_")]
        max_children = 64
        folder_count = [0]

        def process(item, depth):
            children = list(_safe(lambda: item.children, []) or [])
            node = self._browser_item_info(item)
            node["children"] = []
            if children:
                folder_count[0] += 1
            if children and depth >= 1:
                node["has_more"] = True
                return node
            for child in children[:max_children]:
                node["children"].append(process(child, depth + 1))
            if len(children) > max_children:
                node["has_more"] = True
            return node

        categories = []
        for attr in attrs:
            if category_type not in ("all", attr):
                continue
            item = _safe(lambda: getattr(browser, attr))
            if item is None or callable(item) or not hasattr(item, "children"):
                continue
            node = process(item, 0)
            node["name"] = attr
            categories.append(node)
        return {"type": category_type, "categories": categories, "available_categories": attrs,
                "total_folders": folder_count[0]}

    def _browser_root(self, browser, name):
        name = name.lower()
        for attr in dir(browser):
            if attr.lower() == name and not attr.startswith("_"):
                return getattr(browser, attr)
        return None

    def _cmd_get_browser_items_at_path(self, params):
        browser = self._browser()
        path = params.get("path", "")
        parts = [p for p in path.split("/")]
        current = self._browser_root(browser, parts[0]) if parts and parts[0] else None
        if current is None:
            return {"path": path, "error": "Unbekannte Browser-Kategorie '%s'" % (parts[0] if parts else ""),
                    "available_categories": [a for a in dir(browser) if not a.startswith("_")], "items": []}
        for part in parts[1:]:
            if not part:
                continue
            found = None
            for child in _safe(lambda: current.children, []) or []:
                if _safe(lambda: child.name.lower(), "") == part.lower():
                    found = child
                    break
            if found is None:
                return {"path": path, "error": "Pfadteil '%s' nicht gefunden" % part, "items": []}
            current = found
        info = self._browser_item_info(current)
        info["path"] = path
        info["items"] = [self._browser_item_info(c) for c in _safe(lambda: current.children, []) or []]
        return info

    def _cmd_get_browser_item(self, params):
        browser = self._browser()
        uri = params.get("uri")
        result = {"uri": uri, "found": False}
        if uri:
            item = self._find_browser_item_by_uri(browser, uri)
            if item is not None:
                result["found"] = True
                result["item"] = self._browser_item_info(item)
        return result

    def _order_roots_by_uri(self, roots, uri):
        lowered = (uri or "").lower()
        for attr, markers in self._URI_ROOT_HINTS:
            if any(m in lowered for m in markers):
                return [r for r in roots if r[0] == attr] + [r for r in roots if r[0] != attr]
        return roots

    def _find_browser_item_by_uri(self, browser, uri, max_depth=10):
        cache = getattr(self, "_uri_cache", None)
        if cache is None:
            self._uri_cache = cache = {}
        if uri in cache:
            return cache[uri]
        roots = []
        for attr in self._BROWSER_ROOTS + ("plugins", "max_for_live", "user_library", "packs", "samples"):
            item = _safe(lambda: getattr(browser, attr))
            if item is not None:
                roots.append((attr, item))
        for _attr, root in self._order_roots_by_uri(roots, uri):
            found = self._walk_for_uri(root, uri, max_depth, 1)
            if found is not None:
                cache[uri] = found
                return found
        return None

    def _walk_for_uri(self, item, uri, max_depth, depth):
        if _safe(lambda: item.uri) == uri:
            return item
        if depth >= max_depth:
            return None
        for child in _safe(lambda: item.children, []) or []:
            found = self._walk_for_uri(child, uri, max_depth, depth + 1)
            if found is not None:
                return found
        return None

    def _cmd_load_browser_item(self, params):
        track, ref = self._resolve_track(self._track_param(params))
        uri = params.get("item_uri") or params.get("uri") or ""
        browser = self._browser()
        item = self._find_browser_item_by_uri(browser, uri)
        if item is None:
            raise ValueError("Browser-Eintrag mit URI '%s' nicht gefunden. Mit get_browser_items_at_path suchen." % uri)
        self._song.view.selected_track = track
        browser.load_item(item)
        return {"loaded": True, "item_name": item.name, "track": ref, "track_name": track.name, "uri": uri}
