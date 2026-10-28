"""Technoa-MCP fuer Ableton Live: schlanker MCP-Server.

Ersetzt den Server aus dem PyPI-Paket ableton-mcp 1.4.5. Gruende:
- Keine Telemetrie, keine Dataset-Aufzeichnung, kein user_prompt-Feld.
  Der Code dafuer ist hier gar nicht erst vorhanden.
- Kompakte Textansichten statt grosser JSON-Antworten.
- Neue Lesewerkzeuge: Set-Uebersicht mit Gruppen, Locator, Returns und
  Master, Arrangement-Noten, Auswahl, Meter, gespeicherte Automation.
- Spuren ueberall als Index, Name, A/B (Returns) oder "master".

Spricht mit dem Remote Script "AbletonMCP" in der Technoa-Fassung
(remote_script/AbletonMCP/__init__.py) ueber 127.0.0.1:9877.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import select
import socket
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Union

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP  # noqa: E402

import als_reader  # noqa: E402
import set_snapshot  # noqa: E402
import views  # noqa: E402

HOST = "127.0.0.1"
# Nur fuer Tests umstellbar (Attrappe auf anderem Port); Adresse bleibt Loopback.
PORT = int(os.environ.get("ABLETON_PORT", "9877"))
EXPECTED_SCRIPT_VERSION = "1.7.1+technoa.9"

# Zeitbudget je Befehl im Remote Script plus Reserve fuer den Rueckweg.
SCRIPT_TIMEOUTS = {
    "create_audio_clip": 60.0,
    "get_browser_tree": 45.0,
    "get_browser_items_at_path": 30.0,
    "load_browser_item": 30.0,
    "load_instrument_or_effect": 30.0,
}
READ_COMMANDS = {
    "get_script_info", "get_session_info", "get_song_state", "get_tracks_overview", "get_track_info",
    "get_arrangement_clips", "get_clip_notes", "get_device_parameters", "get_selection", "get_meters",
    "inspect_lom", "get_browser_tree", "get_browser_items_at_path", "get_browser_item", "find_parameter_value",
    "get_routing_options", "get_clip_envelope", "convert_parameter_values",
}

logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("technoa_mcp")


class LiveError(Exception):
    """Fehler, den das Remote Script gemeldet hat."""


class LiveUnavailable(Exception):
    """Keine Verbindung zu Live."""


class LiveConnection:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.sock: Optional[socket.socket] = None
        self.lock = threading.Lock()
        self.script_info: Optional[dict] = None

    def _close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.script_info = None

    def _connect(self):
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=3.0)
        except OSError as e:
            self.sock = None
            raise LiveUnavailable(
                "Keine Verbindung zu Live auf %s:%d (%s). Laeuft Live, und steht AbletonMCP in "
                "Einstellungen > Link, Tempo & MIDI als Control Surface?" % (self.host, self.port, e))

    def _stale(self) -> bool:
        # Nach einem Neustart von Live ist die alte Verbindung tot. Ein
        # lesbarer Socket ohne offene Anfrage heisst: Gegenseite hat
        # geschlossen.
        try:
            readable, _, _ = select.select([self.sock], [], [], 0)
            if readable:
                return self.sock.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True
        return False

    def _receive(self, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        chunks = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Live hat nicht innerhalb von %.0f s geantwortet." % timeout)
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(262144)
            if not chunk:
                raise ConnectionError("Live hat die Verbindung geschlossen.")
            chunks.append(chunk)
            if chunk.rstrip().endswith(b"}"):
                data = b"".join(chunks)
                try:
                    return json.loads(data.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue

    def request(self, command: str, params: Optional[dict] = None) -> Any:
        is_read = command in READ_COMMANDS
        timeout = SCRIPT_TIMEOUTS.get(command, 20.0 if is_read else 15.0) + 5.0
        payload = json.dumps({"type": command, "params": params or {}}).encode("utf-8")
        with self.lock:
            attempts = 2 if is_read else 1
            last_error = None
            for attempt in range(attempts):
                if self.sock is not None and self._stale():
                    self._close()
                if self.sock is None:
                    self._connect()
                    self._check_script()
                try:
                    self.sock.sendall(payload)
                    response = self._receive(timeout)
                    break
                except (OSError, ConnectionError, TimeoutError) as e:
                    self._close()
                    last_error = e
                    # Schreibbefehle nicht wiederholen: Live koennte den
                    # ersten Versuch schon ausgefuehrt haben.
                    if attempt + 1 >= attempts:
                        what = "Lesebefehl" if is_read else "Schreibbefehl (nicht wiederholt, Ergebnis in Live pruefen)"
                        raise LiveUnavailable("%s '%s' fehlgeschlagen: %s" % (what, command, e))
            else:  # pragma: no cover
                raise LiveUnavailable(str(last_error))
        if response.get("status") == "error":
            raise LiveError(response.get("message") or "Unbekannter Fehler im Remote Script")
        return response.get("result")

    def _check_script(self):
        self.sock.sendall(json.dumps({"type": "get_script_info", "params": {}}).encode("utf-8"))
        info = self._receive(10.0).get("result") or {}
        self.script_info = info
        if info.get("variant") != "technoa":
            self._close()
            raise LiveUnavailable(
                "In Live laeuft noch das alte Remote Script (Version %s), nicht die Technoa-Fassung. "
                "Live einmal neu starten; Remote Scripts werden nur beim Start eingelesen."
                % info.get("script_version", "unbekannt"))
        if info.get("script_version") != EXPECTED_SCRIPT_VERSION:
            log.warning("Remote Script %s, erwartet %s", info.get("script_version"), EXPECTED_SCRIPT_VERSION)


live = LiveConnection(HOST, PORT)
mcp = FastMCP("ableton")


def _call(command: str, params: Optional[dict] = None) -> Any:
    return live.request(command, params)


def _tool(fn):
    """Fehler als lesbaren Text zurueckgeben statt die Toolausfuehrung abzubrechen."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (LiveError, LiveUnavailable, ValueError) as e:
            return "Fehler: %s" % e
        except Exception as e:  # noqa: BLE001
            log.exception("Unerwarteter Fehler in %s", fn.__name__)
            return "Unerwarteter Fehler in %s: %s: %s" % (fn.__name__, e.__class__.__name__, e)
    return wrapper


# Jeder Befehl kostet einen Durchlauf durch Lives Hauptthread. Taktart und
# Spurnamen aendern sich selten; kurz zwischenspeichern spart pro Werkzeug-
# aufruf ein bis zwei solche Durchlaeufe. Schreibbefehle, die Namen oder
# Spuren aendern, leeren den Speicher.
_CACHE: Dict[str, Any] = {}
CACHE_SECONDS = 5.0


def _cached(key: str, command: str) -> Any:
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = _call(command)
    _CACHE[key] = (time.monotonic(), value)
    return value


def _forget():
    _CACHE.clear()


def _state() -> dict:
    return _call("get_song_state")


def _session() -> dict:
    return _cached("session", "get_session_info")


def _timeline(state: Optional[dict] = None) -> views.Timeline:
    state = state or _session()
    return views.Timeline(state.get("signature_numerator"), state.get("signature_denominator"))


TrackRef = Union[int, str]


def _track(ref: TrackRef) -> Union[int, str]:
    """Normalisiert eine Spurangabe: 3, "3", "A", "return:0", "master" oder ein Spurname."""
    if isinstance(ref, bool):
        raise ValueError("Spurangabe %r ist ungueltig." % ref)
    if isinstance(ref, int):
        return ref
    text = str(ref).strip()
    low = text.lower()
    if re.fullmatch(r"-?\d+", low):
        return int(low)
    if low in ("master", "main"):
        return "master"
    m = re.fullmatch(r"return[:\s]*(\d+)", low)
    if m:
        return "return:%d" % int(m.group(1))
    m = re.fullmatch(r"(?:return[:\s]*)?([a-l])", low)
    if m:
        return "return:%d" % (ord(m.group(1)) - ord("a"))
    overview = _cached("overview", "get_tracks_overview")
    candidates = [(t["ref"], t["name"]) for t in overview["tracks"] + overview["returns"]]
    exact = [c for c in candidates if c[1].lower() == low]
    if len(exact) == 1:
        return exact[0][0]
    partial = [c for c in candidates if low in c[1].lower()]
    if len(partial) == 1:
        return partial[0][0]
    if partial:
        raise ValueError("Spurname '%s' ist mehrdeutig: %s" % (text, ", ".join("%s %s" % (views.ref_label(r), n)
                                                                               for r, n in partial)))
    raise ValueError("Keine Spur heisst '%s'. Vorhanden: %s" % (text, ", ".join(
        "%s %s" % (views.ref_label(r), n) for r, n in candidates)))


def _beat(bar: Optional[float], beat: Optional[float], tl: views.Timeline, what: str) -> Optional[float]:
    if bar is not None and beat is not None:
        raise ValueError("%s: entweder Takt (bar) oder Beat angeben, nicht beides." % what)
    if bar is not None:
        return tl.bar_to_beat(bar)
    return beat


# ======================================================================
# Lesen
# ======================================================================

@mcp.tool()
@_tool
def get_remote_script_info() -> str:
    """Version und Faehigkeiten des Remote Scripts in Live (Handshake)."""
    info = _call("get_script_info")
    info["expected_version"] = EXPECTED_SCRIPT_VERSION
    info["server"] = "technoa_mcp"
    return json.dumps(info, indent=1, ensure_ascii=False)


@mcp.tool()
@_tool
def get_set_overview() -> str:
    """Karte des ganzen Sets in einem Aufruf: Tempo, Taktart, Skala, Loop, Grooves,
    Locator, alle Spuren als Gruppenbaum mit Pegel (dB), Pan, Ausgang, Sends,
    Geraeten (an/aus, automatisierte Parameter), Arrangement-Zusammenfassung,
    dazu Returns und Master. Erster Aufruf, um sich zu orientieren."""
    return views.set_overview(_state(), _call("get_tracks_overview"))


@mcp.tool()
@_tool
def get_session_info() -> str:
    """Kurzinfo zu Tempo, Taktart, Transport, Loop und Spurzahl."""
    state = _call("get_session_info")
    tl = _timeline(state)
    loop = "aus"
    if state.get("loop"):
        loop = "%s bis %s" % (tl.pos(state["loop_start"]), tl.pos(state["loop_start"] + state["loop_length"]))
    return ("%g BPM, %d/%d | %s, Playhead %s | Loop %s | %d Spuren, %d Returns | Songlaenge %s"
            % (state["tempo"], tl.numerator, tl.denominator, "laeuft" if state["is_playing"] else "gestoppt",
               tl.pos(state["current_song_time"]), loop, state["track_count"], state["return_track_count"],
               tl.dur(state["song_length"])))


@mcp.tool()
@_tool
def get_track_info(track: TrackRef) -> str:
    """Details einer Spur: Mixer mit dB-Werten und Automationsstatus, alle Sends,
    Ein-/Ausgang, Monitoring, Gruppe, Geraete mit Index, Session-Clips.

    track: Index (0..n-1), Spurname, A/B/... fuer Returns oder "master"."""
    ref = _track(track)
    return views.track_detail(_call("get_track_info", {"track": ref}), _session())


@mcp.tool()
@_tool
def get_arrangement_clips(track: TrackRef, from_bar: Optional[float] = None, to_bar: Optional[float] = None,
                          detail: str = "runs") -> str:
    """Clips einer Spur im Arrangement, mit Takten, Laenge und Aktivierung
    (DEAKTIVIERT = im Set grau).

    detail="runs" fasst gleiche Clips mit gleichem Abstand zu einem Lauf zusammen
    (500 Kick-Clips werden eine Zeile); detail="clips" listet jeden Clip mit Index.
    from_bar/to_bar (1-basiert) schraenken den Bereich ein. Gruppen haben keine Clips."""
    ref = _track(track)
    state = _session()
    tl = _timeline(state)
    params = {"track": ref, "include_digest": True}
    if from_bar is not None:
        params["start_time"] = tl.bar_to_beat(from_bar)
    if to_bar is not None:
        params["end_time"] = tl.bar_to_beat(to_bar)
    if detail not in ("runs", "clips"):
        raise ValueError("detail muss 'runs' oder 'clips' sein.")
    return views.arrangement_clips(_call("get_arrangement_clips", params), state, detail)


@mcp.tool()
@_tool
def get_clip_notes(track: TrackRef, source: str, clip_index: Optional[int] = None, at_bar: Optional[float] = None,
                   max_notes: int = 256) -> str:
    """MIDI-Noten eines Clips als Tabelle (Ableton-Notennamen, 60 = C3).

    source: "arrangement" oder "session".
    Arrangement: clip_index wie in get_arrangement_clips, oder at_bar (1-basiert) =
    der Clip, der in diesem Takt spielt. Session: clip_index = Slot."""
    ref = _track(track)
    state = _session()
    params: Dict[str, Any] = {"track": ref, "source": source}
    if at_bar is not None:
        if source != "arrangement":
            raise ValueError("at_bar gibt es nur fuer source='arrangement'.")
        params["at_time"] = _timeline(state).bar_to_beat(at_bar) + 1e-6
    elif clip_index is not None:
        params["clip_index"] = clip_index
    else:
        raise ValueError("clip_index oder at_bar angeben.")
    return views.clip_notes(_call("get_clip_notes", params), state, max_notes)


@mcp.tool()
@_tool
def get_device_parameters(track: TrackRef, device_index: int, chain_path: Optional[List[List[int]]] = None,
                          name_filter: Optional[str] = None, only_automated: bool = False) -> str:
    """Parameter eines Geraets mit Lives Anzeigewert ("-8.0 dB", "On"), Rohwert,
    Bereich, Auswahloptionen und Automationsstatus.

    device_index: wie in get_track_info. chain_path: in Racks hinabsteigen, Liste
    von [Kette, Geraet], z. B. [[0, 1]]. name_filter: nur Parameter, deren Name den
    Text enthaelt (hilfreich bei Plugins mit hunderten Parametern)."""
    ref = _track(track)
    params = {"track": ref, "device_index": device_index, "chain_path": chain_path or [],
              "name_filter": name_filter or "", "only_automated": only_automated}
    return views.device_parameters(_call("get_device_parameters", params))


@mcp.tool()
@_tool
def get_selection() -> str:
    """Was in Live gerade ausgewaehlt ist: Spur, Clip in der Detailansicht
    (auch Arrangement-Clips, mit Index fuer weitere Abfragen), Parameter,
    Playhead und Ansicht."""
    return views.selection(_call("get_selection"), _session())


@mcp.tool()
@_tool
def watch_meters(seconds: float = 0.0, reference: TrackRef = "master", show_input: bool = False) -> str:
    """Ausgangsmeter aller Spuren, Returns und Master. seconds=0: Momentaufnahme;
    sonst wird ueber die Dauer (max. 30 s) mehrmals pro Sekunde gemessen und
    Spitze und Mittel berichtet, dazu L/R getrennt. Nur sinnvoll bei laufendem
    Transport. Zeigt, ob etwas klingt oder anschlaegt, ersetzt aber kein Hoeren.

    Jede Spitze auch in dBFS (Sample-Spitze wie Lives Spitzenwert-Anzeige),
    dazu Headroom des Masters. Gemessen geeicht fuer Meter 0,106..0,812; darueber
    gerechnet und mit '~' markiert. Fuer Pegelentscheidungen 30 s messen.
    reference: Spur, auf deren Spitze sich die dB-Abstaende beziehen (Standard
    Master). Stumme und per Solo stumme Spuren sind markiert: Ihr Meter
    schlaegt weiter aus; was zu hoeren ist, zeigen Gruppen und Master.
    show_input: zusaetzlich Lives Eingangsmeter. Bedeutung am Set ungeklaert
    (11.10.2026 widerspruechlich); nur zum Erkunden, nicht zum Messen."""
    seconds = max(0.0, min(30.0, float(seconds)))
    ref = _track(reference)
    # Frisch statt aus dem Zwischenspeicher: Stumm/Solo aendern sich gerade
    # bei A/B-Vergleichen zwischen zwei Messungen.
    overview = _call("get_tracks_overview")
    samples = [_call("get_meters")]
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(0.12)
        samples.append(_call("get_meters"))
    return views.meters(samples, seconds, overview, ref, show_input=show_input, tl=_timeline())


@mcp.tool()
@_tool
def read_saved_set(section: str = "overview", track: Optional[str] = None, path: Optional[str] = None,
                   max_events: int = 80) -> str:
    """Liest das gespeicherte Set (.als) nur lesend: Arrangement-Automation mit
    allen Punkten (Spuraktivator, Pegel, Sends, Geraeteparameter), Gruppen,
    Ausgaenge, Grundwerte, deaktivierte Clips.

    Zeigt den Stand der LETZTEN SPEICHERUNG; vorher in Live speichern lassen.
    section: "overview", "automation" oder "all". track: Index, A/B, Master oder
    Namensteil. path: ohne Angabe das in Live geoeffnete Set (song.file_path);
    nur wenn Live nicht erreichbar ist, das zuletzt geladene laut Lives Log."""
    if section not in ("overview", "automation", "all"):
        raise ValueError("section muss 'overview', 'automation' oder 'all' sein.")
    note = "angegeben"
    if not path:
        path, note = _saved_set_path()
        if not path:
            return "Kein Set gefunden: " + note
    return als_reader.describe(path=path, section=section, track=track, max_events=max_events, source_note=note)


def _saved_set_path():
    """Pfad des Sets, das in Live offen ist.

    Bis 11.10.2026 kam er aus Lives Log ("Loading document"). Dort steht aber
    auch jede Vorlage, die Live beim Anlegen einer Spur laedt, und nach
    "Speichern unter" ein veralteter Name. song.file_path kennt beides nicht.
    """
    try:
        res = _call("inspect_lom", {"path": "song.file_path", "list_attributes": False})
    except LiveUnavailable as e:
        path, note = als_reader.find_current_set_from_log()
        return path, "%s; Live nicht erreichbar (%s)" % (note, e)
    except LiveError as e:
        path, note = als_reader.find_current_set_from_log()
        return path, "%s; song.file_path nicht lesbar (%s)" % (note, e)
    path = (res.get("str") if isinstance(res, dict) else None) or ""
    # inspect_lom kuerzt Texte auf 300 Zeichen.
    if len(path) >= 300:
        found, note = als_reader.find_current_set_from_log()
        return found, "%s; song.file_path zu lang fuer inspect_lom" % note
    if not path:
        return None, ("Das Set in Live ist noch nie gespeichert worden (song.file_path ist leer). Erst speichern "
                      "lassen oder path angeben.")
    return path, "laut Live (song.file_path)"


@mcp.tool()
@_tool
def inspect_lom(path: str = "song", doc: bool = False) -> str:
    """Erkundung: zeigt Eigenschaften eines Objekts der Live-API, z. B.
    "song.tracks[1].mixer_device" oder "song.view". Liest nur, ruft keine Methoden auf.
    Fuer Fragen, die kein anderes Werkzeug beantwortet.
    doc=true: zu jeder Methode Lives eigene Beschreibung mit Signatur. Der Pfad darf
    auch mit dem Modul "Live" beginnen ("Live.Clip.AutomationEnvelope"), um Klassen
    zu sehen, an die man ueber das Set nicht herankommt (ab technoa.9)."""
    params: Dict[str, Any] = {"path": path}
    if doc or path.strip().startswith("Live"):
        _require_feature("inspect_doc", "Methodenbeschreibungen und den Pfad 'Live...'")
        params["doc"] = bool(doc)
    return json.dumps(_call("inspect_lom", params), indent=1, ensure_ascii=False, default=str)


@mcp.tool()
@_tool
def get_browser_tree(category_type: str = "all") -> str:
    """Oberste Ebenen von Lives Browser (Instrumente, Effekte, Plugins, ...)."""
    result = _call("get_browser_tree", {"category_type": category_type})
    lines = ["Browser '%s' (%d Ordner):" % (category_type, result.get("total_folders", 0))]

    def walk(node, depth):
        lines.append("%s- %s%s%s" % ("  " * depth, node.get("name"), " [...]" if node.get("has_more") else "",
                                     (" uri=" + node["uri"]) if node.get("is_loadable") and node.get("uri") else ""))
        for child in node.get("children") or []:
            walk(child, depth + 1)
    for cat in result.get("categories") or []:
        walk(cat, 0)
    return "\n".join(lines)


@mcp.tool()
@_tool
def get_browser_items_at_path(path: str) -> str:
    """Eintraege an einem Browser-Pfad, z. B. "audio_effects/Delay & Loop"."""
    return views.browser_items(_call("get_browser_items_at_path", {"path": path}))


# ======================================================================
# Schreiben. Jeder Befehl ist in Live ein Undo-Schritt (Strg+Z oder undo).
# ======================================================================

def _changed(result: Any) -> str:
    return json.dumps(result, indent=1, ensure_ascii=False, default=str)


# Was am echten Set (Live 12.4.6, 07.10.2026) KEINEN Undo-Schritt anlegte:
# solo, arm, Loop, Playhead, Transport, Clips starten/stoppen. Ein undo danach
# nimmt einen aelteren Schritt zurueck, im Test einen von vor der Sitzung.
NO_UNDO_STEP = {"set_loop", "set_current_song_time", "start_playback", "stop_playback", "fire_clip", "stop_clip",
                "switch_to_arrangement_view", "back_to_arrangement", "re_enable_automation"}
MIXER_UNDOABLE_KEYS = ("volume_db", "volume", "panning", "sends", "mute")
SILENT_COMMANDS = {"start_playback", "stop_playback", "fire_clip", "stop_clip", "switch_to_arrangement_view",
                   "set_current_song_time", "re_enable_automation", "back_to_arrangement"}

# Was dieses MCP in Live angelegt hat, als Stapel mit Beschriftung: undo und
# redo duerfen nur so viele Schritte nehmen, und die Antwort sagt, WELCHE.
# Gilt fuer die Laufzeit des Servers. Aenderungen, die zwischendurch jemand
# von Hand in Live macht, sieht der Stapel nicht.
_UNDO: List[dict] = []
_REDO: List[dict] = []
# Marken: Stand des Stapels unter einem Namen. Undo-Schritte von Hand zu zaehlen
# ging schon bei 9 Faderstufen schief. Je Marke: Stapelhoehe ("undo"), Stand
# des Journals ("journal") und der Schnappschuss ("snap").
_MARKS: Dict[str, dict] = {}
# Journal aller Schreibbefehle seit Serverstart, auch der laengst
# zurueckgenommenen: Ein undo stellt den Wert zurueck, laesst aber z. B. eine
# uebersteuerte Automation stehen. Was ein Befehl beruehrt hat, gilt beim
# Vergleich mit der Marke als vom MCP, alles andere als fremd.
_JOURNAL: List[dict] = []

# Bereiche in den Begriffen von set_snapshot (Entry.area). Je Spur:
TRACK_AREAS = {
    "set_mixer": "mixer", "set_device_parameter": "devices", "delete_device": "devices",
    "move_device": "devices", "insert_device": "devices", "duplicate_device": "devices",
    # Laden kann die Spur umbenennen und scharf schalten.
    "load_browser_item": "*", "set_track_name": "*",
    "set_clip_name": "clips", "set_clip_muted": "clips", "add_notes_to_clip": "clips",
    "clear_notes_from_clip": "clips", "delete_arrangement_clip": "clips",
    "duplicate_session_clip_to_arrangement": "clips", "create_clip": "clips", "create_audio_clip": "clips",
    "delete_clip": "clips", "fire_clip": "clips", "stop_clip": "clips",
    # technoa.7. Die Sidechain-Quelle steht im Vergleich unter ROUTING, gehoert
    # aber zum Geraet (Bereich wie set_device_parameter, auch fuer 'S/C On').
    "set_clip_groove": "clips", "set_track_routing": "routing", "set_sidechain": "devices",
    # technoa.9: Clip-Huellkurven gehoeren zum Clip (der Schnappschuss fuehrt
    # ihren Abdruck am Clip), Loeschen ueber einen Bereich ebenso.
    "set_clip_envelope": "clips", "clear_clip_envelope": "clips", "delete_arrangement_clips": "clips",
}
# Am Song oder am ganzen Set:
SONG_AREAS = {
    "set_tempo": "song", "create_locator": "locators", "set_loop": "loop", "set_current_song_time": "transport",
    "start_playback": "transport", "stop_playback": "transport", "switch_to_arrangement_view": "view",
    "back_to_arrangement": "song", "create_midi_track": "structure", "create_audio_track": "structure",
    "re_enable_automation": "automation", "set_song_settings": "song",
}
SET_WIDE_AREAS = ("structure", "automation")


def _expected_step(command: str, params: dict) -> int:
    if command in NO_UNDO_STEP:
        return 0
    if command == "set_mixer":
        return 1 if any(k in params for k in MIXER_UNDOABLE_KEYS) else 0
    return 1


def _step_entry(command: str, params: dict, result: Any) -> dict:
    """Kurze Beschreibung eines Undo-Schritts fuer die Antwort von undo/redo."""
    r = result if isinstance(result, dict) else {}
    where = r.get("track_name") or (views.ref_label(params["track"]) if "track" in params else "")
    what = ""
    if command == "set_mixer":
        what = ", ".join("%s %s" % (c.get("target"), _display_of(c.get("after"))) for c in r.get("changes") or [])
    elif command == "set_device_parameter":
        after = r.get("after") or {}
        what = "%s: %s = %s" % (r.get("device"), after.get("name"), after.get("display"))
    elif command == "delete_device":
        what = "'%s' geloescht" % ((r.get("deleted") or {}).get("name"))
    elif command == "move_device":
        what = "'%s' %s -> %s" % (r.get("device"), r.get("from_index"), r.get("new_index"))
    elif command in ("insert_device", "duplicate_device"):
        what = "'%s' an %s" % (r.get("device_name") or r.get("device"), r.get("new_index"))
    elif command == "load_browser_item":
        what = "'%s' geladen" % r.get("item_name")
    elif command in ("create_midi_track", "create_audio_track"):
        what = "Spur [%s] '%s'" % (r.get("index"), r.get("name"))
    elif command == "delete_arrangement_clip":
        dc = r.get("deleted") or {}
        what = "Arrangement-Clip #%s '%s' ab Beat %s geloescht" % (dc.get("index"), dc.get("name"),
                                                                  "%g" % dc["start_time"] if "start_time" in dc else "?")
    elif command == "set_tempo":
        what = "%g BPM" % params.get("tempo")
    elif command in ("set_clip_envelope", "clear_clip_envelope"):
        clip = r.get("clip") or {}
        labels = [e.get("label") for e in r.get("cleared") or []] or [(r.get("target") or {}).get("label")]
        what = "Huellkurve %s in '%s' %s" % (", ".join(str(x) for x in labels), clip.get("name"),
                                             "geloescht" if command == "clear_clip_envelope"
                                             else "%s Stufen" % r.get("steps_written"))
    elif command == "delete_arrangement_clips":
        what = "%d Arrangement-Clips ab Beat %g bis %g geloescht" % (len(r.get("deleted") or []),
                                                                     r.get("from_time") or 0.0, r.get("to_time") or 0.0)
    elif command == "set_song_settings":
        what = ", ".join("%s %s -> %s" % (c.get("target"), c.get("before"), c.get("after"))
                         for c in r.get("changes") or [])
    elif command == "duplicate_session_clip_to_arrangement":
        what = "'%s' ins Arrangement bei Beat %g" % (r.get("clip_name"), r.get("destination_time") or 0.0)
        if (r.get("repeat") or 1) > 1:
            what += ", %dx alle %g Beats" % (r["repeat"], r.get("step") or 0.0)
    elif command == "add_notes_to_clip":
        what = "%s Noten in '%s'" % (r.get("notes_sent"), r.get("clip_name"))
    elif command == "set_clip_groove":
        what = "Groove %s fuer %d Clip(s)" % ("'%s'" % r["groove"] if r.get("groove") else "entfernt",
                                             len(r.get("changed") or []))
    elif command in ("set_track_routing", "set_sidechain"):
        what = "; ".join("%s %s -> %s" % ("Ausgang" if c.get("side") == "output" else
                                          "Quelle" if command == "set_sidechain" else "Eingang",
                                          _route_text(c.get("before")), _route_text(c.get("after")))
                         for c in r.get("changes") or [])
        if command == "set_sidechain":
            b, a = r.get("before") or {}, r.get("after") or {}
            if b.get("on") != a.get("on"):
                what = "; ".join(x for x in (what, "S/C On %s -> %s" % (views._onoff_text(b.get("on")),
                                                                        views._onoff_text(a.get("on")))) if x)
            what = "'%s' Sidechain %s" % (r.get("device"), what)
    text = command + (" [%s]" % where if where else "") + (" " + what if what else "")
    return {"text": text}


def _display_of(info: Any) -> str:
    if isinstance(info, dict):
        return str(info.get("display") if info.get("display") is not None else info.get("value"))
    return str(info)


def _write(command: str, params: Optional[dict] = None, forget: bool = False,
           extra_notes: Optional[List[str]] = None, render=None, reread=None) -> str:
    """Schreibbefehl ausfuehren, Undo-Stapel fuehren und Stolperstellen benennen.

    render: Funktion Ergebnis -> Text; ohne Angabe JSON.
    reread: Funktion Ergebnis -> Hinweis oder None; liest den Zustand nach dem
    Setzen erneut und ersetzt 'after'. Laeuft vor dem Undo-Eintrag,
    damit auch dessen Beschriftung den richtigen Stand nennt."""
    params = params or {}
    result = _call(command, params)
    if forget:
        _forget()
    reread_note = reread(result) if reread and isinstance(result, dict) else None
    check = result.pop("undo_check", None) if isinstance(result, dict) else None
    dynamic = check.get("undo_step") if check else None
    step = int(dynamic) if dynamic is not None else _expected_step(command, params)
    _JOURNAL.append({"command": command, "touched": _touched(command, params, result), "undo_step": bool(step)})
    if step:
        _UNDO.append(_step_entry(command, params, result))
        _REDO.clear()
    notes = list(extra_notes or [])
    if reread_note:
        notes.append(reread_note)
    if step == 0 and command not in SILENT_COMMANDS:
        how = "Live meldet das" if dynamic is False else "laut Test vom 07.10.2026"
        notes.append("Kein Undo-Schritt (%s). undo wuerde einen AELTEREN Schritt zuruecknehmen. "
                     "Zurueck nur durch erneutes Setzen; der alte Wert steht oben unter 'before'." % how)
    overridden = []
    if isinstance(result, dict):
        for c in (result.get("changes") or []) + ([result] if "before" in result else []):
            before = c.get("before")
            if isinstance(before, dict) and before.get("automation") == "active":
                overridden.append(before.get("name") or c.get("target") or "?")
    if overridden:
        notes.append("Automation von %s ist jetzt UEBERSTEUERT. undo stellt den Wert zurueck, NICHT die "
                     "Automation; dafuer re_enable_automation." % ", ".join(overridden))
    text = render(result) if render else _changed(result)
    if notes:
        text += "\n" + "\n".join("HINWEIS: " + n for n in notes)
    return text


def _touched(command: str, params: dict, result: Any) -> List[dict]:
    """Was ein Schreibbefehl beruehrt: Spur (ref, Name) und Bereich."""
    r = result if isinstance(result, dict) else {}
    if command in TRACK_AREAS:
        area = TRACK_AREAS[command]
        out = [{"ref": params.get("track"), "name": r.get("track_name"), "area": area}]
        if command == "move_device" and params.get("target_track") is not None:
            out.append({"ref": params["target_track"], "name": None, "area": area})
        return out
    if command in SONG_AREAS:
        area = SONG_AREAS[command]
        return [{"ref": "*" if area in SET_WIDE_AREAS else "song", "name": None, "area": area}]
    # Unbekannter Befehl: lieber nichts als fremd melden als falsch beschuldigen.
    return [{"ref": "*", "name": None, "area": "*"}]


def _covered(entry: "set_snapshot.Entry", touched: List[dict]) -> bool:
    for t in touched:
        if t["area"] not in ("*", entry.area) and not (t["area"] == "automation" and entry.automation):
            continue
        if t["ref"] == "*" or t["ref"] in entry.refs or (t["name"] and t["name"] in entry.names):
            return True
    return False


def _ledger_text() -> str:
    text = "MCP-Schritte noch zuruecknehmbar: %d, wiederherstellbar: %d" % (len(_UNDO), len(_REDO))
    marks = ["%s bei %d" % (k, v["undo"]) for k, v in sorted(_MARKS.items(), key=lambda kv: kv[1]["undo"])]
    if marks:
        text += " | Marken: " + ", ".join(marks)
    return text


@mcp.tool()
@_tool
def undo(steps: int = 1, force: bool = False) -> str:
    """Nimmt Schritte in Live zurueck, wie Strg+Z, aber nur so viele, wie dieses MCP
    selbst angelegt hat (Stapel seit Serverstart), und nennt jeden Schritt.
    force=true hebt die Sperre auf; nur nach Ruecksprache mit der Person an Live,
    denn dann trifft undo deren eigene Schritte. Fuer Serien besser mark_undo/undo_to_mark."""
    if steps < 1:
        raise ValueError("steps muss mindestens 1 sein.")
    if not force and steps > len(_UNDO):
        raise ValueError("Gesperrt: Dieses MCP hat nur %d Schritt(e) angelegt, die undo zuruecknehmen darf. "
                         "Ein weiteres undo traefe einen aelteren Schritt, vermutlich einen von Hand in Live. "
                         "Mit force=true trotzdem, aber nur nach Ruecksprache." % len(_UNDO))
    return _undo_steps(steps)


def _undo_steps(steps: int) -> str:
    result = _call("undo", {"steps": steps})
    _forget()
    done = int(result.get("undone", 0)) if isinstance(result, dict) else 0
    entries = []
    for _ in range(done):
        entry = _UNDO.pop() if _UNDO else {"text": "(Schritt, den dieses MCP nicht angelegt hat)"}
        _REDO.append(entry)
        entries.append(entry)
    lines = [_changed(result)]
    lines.extend("zurueckgenommen: " + e["text"] for e in entries)
    lines.append(_ledger_text())
    return "\n".join(lines)


@mcp.tool()
@_tool
def redo(steps: int = 1, force: bool = False) -> str:
    """Stellt zurueckgenommene MCP-Schritte wieder her, wie Strg+Y. Gesperrt wie undo."""
    if steps < 1:
        raise ValueError("steps muss mindestens 1 sein.")
    if not force and steps > len(_REDO):
        raise ValueError("Gesperrt: Nur %d zurueckgenommene MCP-Schritt(e) sind wiederherstellbar. "
                         "Mit force=true trotzdem, aber nur nach Ruecksprache." % len(_REDO))
    result = _call("redo", {"steps": steps})
    _forget()
    done = int(result.get("redone", 0)) if isinstance(result, dict) else 0
    entries = []
    for _ in range(done):
        entry = _REDO.pop() if _REDO else {"text": "(Schritt, den dieses MCP nicht angelegt hat)"}
        _UNDO.append(entry)
        entries.append(entry)
    lines = [_changed(result)]
    lines.extend("wiederhergestellt: " + e["text"] for e in entries)
    lines.append(_ledger_text())
    return "\n".join(lines)


@mcp.tool()
@_tool
def mark_undo(name: str = "vorher", snapshot: bool = True, project: Optional[str] = None) -> str:
    """Setzt eine Marke auf den jetzigen Stand des MCP-Undo-Stapels, z. B. vor einer
    Serie von Aenderungen. undo_to_mark(name) nimmt dann genau bis dorthin zurueck,
    ohne dass gezaehlt werden muss. Gilt bis zum Neustart des MCP-Servers.

    snapshot=true (Standard): liest vorher das ganze Set (rund 8 s) und legt es als
    Schnappschuss <Zeit>_marke_<name>.json im Ordner des Projekts ab (das 'vorher').
    Damit erkennt undo_to_mark fremde Aenderungen seit der Marke und prueft danach,
    ob der Stand der Marke wieder erreicht ist. project wie bei snapshot."""
    name = (name or "").strip() or "vorher"
    snap, where, folder = None, "", None
    if snapshot:
        snap = set_snapshot.take(_call, label="marke_" + name, extra={"mark": name, "mcp_steps": len(_UNDO)})
        try:
            folder, how = _snapshot_dir(project)
            where = " Schnappschuss: %s (%s)." % (_show_path(_save_snapshot(snap, folder, "marke_" + name)), how)
        except ValueError as e:
            where = " Schnappschuss nur im Speicher, nicht abgelegt: %s" % e
    _MARKS[name] = {"undo": len(_UNDO), "journal": len(_JOURNAL), "snap": snap, "folder": folder}
    text = "Marke '%s' gesetzt.%s %s" % (name, where, _ledger_text())
    if not snapshot:
        text += ("\nHINWEIS: Ohne Schnappschuss kann undo_to_mark fremde Aenderungen seit der Marke nicht erkennen. "
                 "Nach Aenderungen von Hand in Live dann eine neue Marke setzen.")
    return text


# Was ein Schnappschuss nicht sieht und deshalb auch nicht als fremd melden kann.
SNAPSHOT_BLIND = ("Spur-Automation im Arrangement, Clip-Huellkurven an MIDI-Controllern, Geraete in Racks und "
                  "Plugin-Parameter, die das Plugin Live nicht freigibt; Sidechain-Quellen erst ab Script "
                  "technoa.7, Clip-Huellkurven an Mixer- und Geraeteparametern erst ab technoa.9")


@mcp.tool()
@_tool
def undo_to_mark(name: str = "vorher", force: bool = False, check: bool = True, max_lines: int = 30) -> str:
    """Nimmt alle MCP-Schritte seit der Marke zurueck (siehe mark_undo) und nennt sie.

    check=true (Standard, braucht den Schnappschuss der Marke): liest das Set vorher
    neu (rund 8 s) und LEHNT AB, wenn seit der Marke fremde Aenderungen dazugekommen
    sind, also solche, die kein MCP-Befehl erklaert (Lives Undo-Liste ist gemeinsam:
    undo traefe dann zuerst die Schritte der Person an Live).
    force=true nur nach Ruecksprache mit ihr. Danach vergleicht es mit dem
    Schnappschuss der Marke, ob der alte Stand wieder erreicht ist, und legt den
    Stand danach als <Zeit>_zurueck_<name>.json ab."""
    name = (name or "").strip() or "vorher"
    if name not in _MARKS:
        raise ValueError("Keine Marke '%s'. Vorhanden: %s" % (name, ", ".join(sorted(_MARKS)) or "keine"))
    mark = _MARKS[name]
    n = len(_UNDO) - mark["undo"]
    if n < 0:
        raise ValueError("Der Stapel ist schon %d Schritt(e) hinter der Marke '%s' (es wurde weiter "
                         "zurueckgenommen). Mit redo vor, nicht mit undo." % (-n, name))
    if n == 0:
        return "Seit der Marke '%s' gibt es keine MCP-Schritte. Nichts zu tun. %s" % (name, _ledger_text())
    base = mark.get("snap")
    notes = []
    if check and base is None:
        notes.append("Die Marke hat keinen Schnappschuss (mark_undo mit snapshot=false): fremde Aenderungen "
                     "nicht geprueft, Ergebnis nicht verglichen.")
    problems, tail = [], []
    if check and base is not None:
        now = set_snapshot.take(_call, label="vor_undo_to_mark_" + name)
        report = set_snapshot.compare(base, now)
        touched = [t for j in _JOURNAL[mark["journal"]:] for t in j["touched"]]
        foreign = [e for e in report.counted() if e.undo and not _covered(e, touched)]
        own = [e for e in report.counted() if e.undo and _covered(e, touched)]
        if foreign:
            problems.append(_foreign_text(foreign, own, n, max_lines))
            tail += ["  - Soll das Fremde bleiben: die MCP-Aenderungen gezielt zuruecksetzen (Liste 'vom MCP' oben) "
                     "statt undo, oder die Person an Live nimmt ihre Aenderung selbst zurueck, wenn sie ganz "
                     "zuletzt kam (Strg+Z).",
                     "  - Soll alles bis zur Marke zurueck, auch das Fremde: force=true, nur nach Ruecksprache. "
                     "Der Vergleich danach zeigt, ob etwas stehen blieb."]
    if problems and not force:
        head = ("Abgelehnt, nichts zurueckgenommen. Seit der Marke '%s' hat das MCP %d Schritt(e) angelegt; "
                "undo_to_mark nimmt die %d juengsten Schritte in Lives gemeinsamer Undo-Liste zurueck." % (name, n, n))
        return "\n".join([head] + problems + ["Moeglich:"] + tail + ["HINWEIS: " + x for x in notes])
    if problems:
        notes.append("Mit force=true trotz: " + " / ".join(p.split("\n", 1)[0] for p in problems))
    text = _undo_steps(n)
    if check and base is not None:
        after = set_snapshot.take(_call, label="zurueck_" + name, extra={"mark": name, "mcp_steps": len(_UNDO)})
        try:
            folder = mark.get("folder") or _snapshot_dir(None)[0]
            saved = _show_path(_save_snapshot(after, folder, "zurueck_" + name))
        except ValueError:
            saved = "nicht abgelegt"
        report = set_snapshot.compare(base, after)
        if not report.counted():
            text += ("\nPRUEFUNG: Stand der Marke '%s' wieder erreicht, der Vergleich mit ihrem Schnappschuss zeigt "
                     "keine Aenderungen. Stand danach: %s" % (name, saved))
        else:
            text += ("\nPRUEFUNG: Stand der Marke '%s' NICHT ganz erreicht (Stand danach: %s):\n%s" % (
                name, saved, set_snapshot.render(report, "Marke '%s'" % name, "nach undo_to_mark", max_lines)))
            if any(e.cat == "AUTOMATION" for e in report.counted()):
                notes.append("Uebersteuerte Automation holt undo nicht zurueck: re_enable_automation.")
            # Am Set 22.10.2026 blieb nur die uebersteuerte Automation; der Hinweis
            # auf fremde Undo-Schritte fuehrte da in die Irre.
            if any(e.cat != "AUTOMATION" for e in report.counted()):
                notes.append("Blieben Aenderungen des MCP stehen, lagen vermutlich fremde Undo-Schritte dazwischen. "
                             "Dann undo(force=true) Schritt fuer Schritt und mit snapshot_diff(a='marke:%s') pruefen."
                             % name)
    return text + "".join("\nHINWEIS: " + x for x in notes)


def _foreign_text(foreign, own, n: int, max_lines: int) -> str:
    # Reihenfolge wie im Vergleich: Routing und Geraete vor Parametern und Clips.
    foreign = sorted(foreign, key=lambda e: set_snapshot.CATEGORIES.index(e.cat))
    limit = max(5, max_lines // 2) if max_lines else len(foreign)
    lines = ["FREMDE AENDERUNGEN seit der Marke (%d), kein MCP-Befehl erklaert sie:" % len(foreign)]
    lines.extend("  %s %s" % (e.cat, e.text) for e in foreign[:limit])
    if len(foreign) > limit:
        lines.append("  ... und %d weitere" % (len(foreign) - limit))
    lines.append("  Liegen sie zwischen oder nach den MCP-Schritten, nimmt undo zuerst diese zurueck, und ebenso "
                 "viele MCP-Schritte bleiben stehen. Lagen sie vor dem ersten MCP-Schritt, waere es harmlos; das "
                 "laesst sich von hier nicht unterscheiden.")
    if own:
        # " | " statt "; ": Eintraege enthalten selbst "; zurueck mit re_enable_automation".
        lines.append("Vom MCP seit der Marke (%d): %s" % (len(own), " | ".join(e.text for e in own[:6]) +
                                                          (" ..." if len(own) > 6 else "")))
    lines.append("Nicht erkennbar: %s, und fremde Aenderungen in Bereichen, die das MCP seit der Marke selbst "
                 "beruehrt hat." % SNAPSHOT_BLIND)
    return "\n".join(lines)


# ======================================================================
# Schnappschuss. Ablage je Projekt, damit ein spaeterer Chat weiss, wogegen er
# vergleicht: <Ablage>/<projekt>/. Ablage = ABLETON_MCP_SNAPSHOT_DIR, sonst
# snapshots/ neben technoa_mcp/.
# ======================================================================

DEFAULT_PROJECT = "default"


def _snapshot_root() -> str:
    return os.environ.get("ABLETON_MCP_SNAPSHOT_DIR") or os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "snapshots"))


def _project_list(root: str) -> List[str]:
    try:
        return sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
    except OSError:
        return []


def _snapshot_files(folder: str) -> List[str]:
    """Schnappschuesse im Ordner, aelteste zuerst. Nach Dateizeit, nicht nach Namen:
    Namen wie '2026-10-19_ende_live.json' sortieren sich nicht nach der Uhrzeit."""
    try:
        names = [f for f in os.listdir(folder) if f.lower().endswith(".json")]
    except OSError:
        return []
    return sorted(names, key=lambda f: os.path.getmtime(os.path.join(folder, f)))


def _snapshot_dir(project: Optional[str]) -> tuple:
    root = _snapshot_root()
    if project:
        name = project.strip()
        # Nur ein einfacher Ordnername: project darf nicht aus der Ablage herausfuehren.
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("Projektname '%s' geht nicht: nur Buchstaben, Ziffern, _ und -. Vorhanden unter %s: %s"
                             % (project, root, ", ".join(_project_list(root)) or "keine"))
        return os.path.join(root, name), "Projekt angegeben"
    newest = None
    for v in _project_list(root):
        folder = os.path.join(root, v)
        files = _snapshot_files(folder)
        if files:
            mtime = os.path.getmtime(os.path.join(folder, files[-1]))
            if newest is None or mtime > newest[0]:
                newest = (mtime, folder)
    if newest is None:
        return (os.path.join(root, DEFAULT_PROJECT),
                "noch kein Schnappschuss, Projekt '%s'; anderes mit project=..." % DEFAULT_PROJECT)
    return newest[1], "Projekt mit dem juengsten Schnappschuss; anderes mit project=..."


def _slug(label: str) -> str:
    # Umlaute als Codepunkte, damit die Datei ASCII bleibt.
    text = (label or "").strip().translate({0xE4: "ae", 0xF6: "oe", 0xFC: "ue", 0xC4: "Ae", 0xD6: "Oe",
                                            0xDC: "Ue", 0xDF: "ss"})
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_")[:60]


def _save_snapshot(snap: dict, folder: str, label: str) -> str:
    os.makedirs(folder, exist_ok=True)
    slug = _slug(label)
    for stamp in (time.strftime("%Y-%m-%d_%H%M"), time.strftime("%Y-%m-%d_%H%M%S")):
        path = os.path.join(folder, stamp + ("_" + slug if slug else "") + ".json")
        if not os.path.exists(path):
            break
    else:
        raise ValueError("Schnappschuss %s gibt es schon." % path)
    set_snapshot.save(snap, path)
    return path


def _show_path(path: str) -> str:
    # Relativ zum Ordner ueber der Ablage: kurz, und der Ablageordner bleibt sichtbar.
    try:
        return os.path.relpath(path, os.path.dirname(_snapshot_root())).replace("\\", "/")
    except ValueError:
        # Windows: anderes Laufwerk als die Ablage, dann eben voll.
        return path


def _resolve_snapshot(ref: str, folder: str, live_label: str) -> tuple:
    """Schnappschuss und Anzeigename zu 'jetzt', 'letzter', 'vorletzter',
    'marke:<name>', einem Dateinamen oder Pfad."""
    text = (ref or "").strip()
    low = text.lower()
    if low == "jetzt":
        return set_snapshot.take(_call, label="jetzt"), live_label
    if low.startswith("marke:"):
        name = text.split(":", 1)[1].strip()
        mark = _MARKS.get(name)
        if not mark or mark.get("snap") is None:
            raise ValueError("Keine Marke '%s' mit Schnappschuss in diesem Serverlauf. Vorhanden: %s" % (
                name, ", ".join(k for k, v in _MARKS.items() if v.get("snap") is not None) or "keine"))
        return mark["snap"], "Marke '%s'" % name
    if low in ("letzter", "vorletzter"):
        files = _snapshot_files(folder)
        need = 1 if low == "letzter" else 2
        if len(files) < need:
            raise ValueError("In %s liegen nur %d Schnappschuss(e)." % (_show_path(folder), len(files)))
        path = os.path.join(folder, files[-need])
        return set_snapshot.load(path), "%s = %s" % (low, os.path.basename(path))
    name = text if text.lower().endswith(".json") else text + ".json"
    candidates = [name] if os.path.isabs(name) else [os.path.join(folder, name)] + [
        os.path.join(_snapshot_root(), v, name) for v in _project_list(_snapshot_root())]
    for path in candidates:
        if os.path.isfile(path):
            return set_snapshot.load(path), os.path.basename(path)
    raise ValueError("Schnappschuss '%s' nicht gefunden. Die juengsten in %s: %s" % (
        text, _show_path(folder), ", ".join(reversed(_snapshot_files(folder)[-5:])) or "keine"))


@mcp.tool()
@_tool
def snapshot(label: str = "", project: Optional[str] = None, compare_to: Optional[str] = None,
             max_lines: int = 60) -> str:
    """Legt einen Schnappschuss des ganzen Sets ab: Spuren, Routing, Mixer, alle
    Geraeteparameter, Arrangement- und Session-Clips mit Inhalts-Fingerabdruck,
    Locator, Grooves, Playhead und Transport. AENDERT NICHTS am Set, dauert rund 8 s.
    Ablage: <Ablage>/<project>/JJJJ-MM-TT_HHMM_<label>.json; Ablage ist
    ABLETON_MCP_SNAPSHOT_DIR, sonst snapshots/ neben dem Server.

    project: Ordner je Track oder Vorhaben (z. B. "warehouse_track"), wird bei Bedarf
      angelegt; ohne Angabe das Projekt mit dem juengsten Schnappschuss, sonst "default".
    compare_to: gleich vergleichen, z. B. "letzter" (der Schnappschuss davor), ein
      Dateiname oder "marke:<name>"; Ausgabe wie snapshot_diff.
    Am Ende eines Arbeitsblocks ablegen und mit dem vorigen vergleichen."""
    folder, how = _snapshot_dir(project)
    # Vor dem Ablegen aufloesen, sonst waere 'letzter' der neue Schnappschuss selbst.
    base = _resolve_snapshot(compare_to, folder, "") if compare_to else None
    snap = set_snapshot.take(_call, label=label, extra={"mcp_steps": len(_UNDO)})
    path = _save_snapshot(snap, folder, label)
    text = "Schnappschuss abgelegt: %s (%s)\n%s | %s | %.1f s" % (
        _show_path(path), how, set_snapshot.summary(snap), set_snapshot.playhead_text(snap), snap["seconds"])
    if base is not None:
        text += "\n\n" + set_snapshot.render(set_snapshot.compare(base[0], snap), base[1], os.path.basename(path),
                                             max_lines)
    return text


@mcp.tool()
@_tool
def snapshot_diff(a: str = "letzter", b: str = "jetzt", project: Optional[str] = None,
                  max_lines: int = 60) -> str:
    """Vergleicht zwei Schnappschuesse und meldet die Unterschiede, wichtigste zuerst:
    Spuren, Routing, Geraete, Automation, Mixer, Song und Locator, Parameter, Clips.
    Spuren werden nach Namen abgeglichen, Clips nach Startposition, Geraete als Kette;
    gleiche Aenderungen an vielen Clips stehen in einer Zeile.

    a, b: "jetzt" (Set jetzt lesen, rund 8 s, nichts ablegen), "letzter"/"vorletzter"
      (im Ordner des Projekts, nach Zeit), "marke:<name>" (Schnappschuss von mark_undo),
      ein Dateiname aus snapshots/ oder ein Pfad.
    Nicht gezaehlt: automatisierte Werte, die nur dem Playhead folgen ("folgt Automation"),
    Rohwerte mit gleicher Anzeige bis 1/10000 des Bereichs daneben, Lives
    Undo-Zustand. Solo, Arm und Loop sind als "kein Undo-Schritt" markiert.
    max_lines: Laenge der Ausgabe (0 = alles)."""
    folder, _ = _snapshot_dir(project)
    snap_a, name_a = _resolve_snapshot(a, folder, "jetzt (A, nicht abgelegt)")
    snap_b, name_b = _resolve_snapshot(b, folder, "jetzt (nicht abgelegt)")
    return set_snapshot.render(set_snapshot.compare(snap_a, snap_b), name_a, name_b, max_lines)


@mcp.tool()
@_tool
def re_enable_automation() -> str:
    """Schaltet uebersteuerte Automation im ganzen Set wieder ein (Lives Knopf
    "Re-Enable Automation"). Noetig nach set_mixer/set_device_parameter auf einem
    automatisierten Wert, auch wenn danach undo lief."""
    text = _write("re_enable_automation")
    state = _state()
    return text + "\nDanach: %s" % ("noch uebersteuert" if state.get("re_enable_automation_enabled")
                                     else "keine Automation mehr uebersteuert")


@mcp.tool()
@_tool
def back_to_arrangement() -> str:
    """Wie Lives Knopf "Back to Arrangement": Spuren spielen wieder das Arrangement
    statt gestarteter Session-Clips."""
    return _write("back_to_arrangement")


@mcp.tool()
@_tool
def set_track_name(track: TrackRef, name: str) -> str:
    """Benennt eine Spur um. AENDERT DAS SET."""
    return _write("set_track_name", {"track": _track(track), "name": name}, forget=True)


@mcp.tool()
@_tool
def set_clip_name(track: TrackRef, source: str, clip_index: int, name: str) -> str:
    """Benennt einen Clip um (source "arrangement" oder "session"). AENDERT DAS SET."""
    return _write("set_clip_name", {"track": _track(track), "source": source, "clip_index": clip_index,
                                    "name": name})


@mcp.tool()
@_tool
def set_clip_muted(track: TrackRef, source: str, clip_indices: List[int], muted: bool) -> str:
    """Deaktiviert (muted=true, im Set grau) oder aktiviert Clips. Indizes wie in
    get_arrangement_clips(detail="clips"). AENDERT DAS SET."""
    return _write("set_clip_muted", {"track": _track(track), "source": source,
                                     "clip_indices": clip_indices, "muted": muted})


@mcp.tool()
@_tool
def set_mixer(track: TrackRef, volume_db: Optional[float] = None, volume: Optional[float] = None,
              pan: Optional[Union[float, str]] = None, sends: Optional[List[Dict[str, Any]]] = None,
              mute: Optional[bool] = None, solo: Optional[bool] = None, arm: Optional[bool] = None) -> str:
    """Setzt Mixerwerte einer Spur in einem Undo-Schritt. AENDERT DAS SET.

    volume_db: Pegel in dB (z. B. -8). volume: Rohwert 0..1 (0.85 = 0 dB).
    pan: -1..1 oder Text wie "C", "25L", "10R". sends: Liste wie
    [{"send": "B", "db": -24}] oder [{"send": 0, "value": 0.5}].
    mute/solo/arm: true/false. solo und arm sind in Live KEIN Undo-Schritt.
    Ist ein Wert automatisiert, gilt seine Automation danach als uebersteuert;
    undo holt sie nicht zurueck, dafuer re_enable_automation."""
    params: Dict[str, Any] = {"track": _track(track)}
    if volume_db is not None:
        params["volume_db"] = float(volume_db)
    elif volume is not None:
        params["volume"] = float(volume)
    if pan is not None:
        params["panning"] = _pan_value(pan)
    if sends:
        out = []
        for s in sends:
            key = s.get("send", s.get("index", 0))
            index = ord(key.strip().upper()) - ord("A") if isinstance(key, str) and key.strip().isalpha() else int(key)
            entry: Dict[str, Any] = {"index": index}
            if s.get("db") is not None:
                entry["db"] = float(s["db"])
            else:
                entry["value"] = float(s.get("value", 0.0))
            out.append(entry)
        params["sends"] = out
    for key, val in (("mute", mute), ("solo", solo), ("arm", arm)):
        if val is not None:
            params[key] = bool(val)
    notes = _mute_notes(params["track"], mute) if mute is not None else []
    return _write("set_mixer", params, extra_notes=notes)


def _mute_notes(ref, mute: bool) -> List[str]:
    # mute und Spuraktivator sind in Live derselbe Schalter. Lives Antwort auf
    # mute nennt keine Automation (anders als bei Pegel/Send); am 07.10. und
    # 11.10.2026 blieb dadurch unbemerkt, dass KICK BODY uebersteuert war.
    info = _call("get_track_info", {"track": ref})
    notes = []
    act = (info.get("mixer") or {}).get("track_activator") or {}
    if act.get("automation") == "active" and bool(info.get("mute")) != bool(mute):
        notes.append("Der Spuraktivator von %s ist automatisiert. mute UEBERSTEUERT diese Automation; undo "
                     "stellt den Schalter zurueck, NICHT die Automation; dafuer re_enable_automation."
                     % info.get("name", ref))
    if mute:
        notes.append("watch_meters zeigt auf stummen Spuren weiter Ausschlag (Meter sitzt vor dem Aktivator). "
                     "Fuer 'klingt sie?' die Gruppe oder den Master messen.")
    return notes


def _pan_value(pan: Union[float, str]) -> float:
    if isinstance(pan, (int, float)):
        return float(pan)
    text = pan.strip().upper()
    if text in ("C", "0", "MITTE"):
        return 0.0
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([LR])", text) or re.fullmatch(r"([LR])\s*(\d+(?:\.\d+)?)", text)
    if not m:
        raise ValueError("Pan '%s' nicht lesbar. Erlaubt: -1..1, 'C', '25L', '10R'." % pan)
    amount, side = (m.group(1), m.group(2)) if m.group(1)[0].isdigit() else (m.group(2), m.group(1))
    value = float(amount) / 50.0
    if value > 1.0:
        raise ValueError("Pan geht bis 50L/50R.")
    return -value if side == "L" else value


@mcp.tool()
@_tool
def set_device_parameter(track: TrackRef, device_index: int, parameter: Union[int, str], value: float,
                         chain_path: Optional[List[List[int]]] = None) -> str:
    """Setzt einen Geraeteparameter (Rohwert im Bereich aus get_device_parameters).
    parameter: Index oder exakter Name. Gibt Anzeige vorher/nachher zurueck. AENDERT DAS SET.
    Rohwert und Anzeige sind oft nicht linear (Utility Stereo Width: 1.5 = 225 %);
    die Anzeige im Ergebnis pruefen."""
    ref = _track(track)
    index = _param_index(ref, device_index, parameter, chain_path)
    params = {"track": ref, "device_index": device_index, "chain_path": chain_path or [], "parameter_index": index,
              "value": value}
    return _write("set_device_parameter", params, reread=_reread_param(params))


# Wartezeiten fuer das erneute Lesen nach dem Setzen (Sekunden). Lives
# Hauptthread laeuft etwa alle 100 ms; Plugins melden ihren Anzeigetext
# womoeglich erst danach.
REREAD_DELAYS = (0.1, 0.4)


def _reread_param(params: dict):
    def reread(result: dict) -> Optional[str]:
        first, before = result.get("after"), result.get("before") or {}
        if not isinstance(first, dict):
            return None
        changed = first.get("value") != before.get("value")
        fresh = None
        for delay in REREAD_DELAYS:
            time.sleep(delay)
            res = _call("get_device_parameters", {
                "track": params["track"], "device_index": params["device_index"],
                "chain_path": params.get("chain_path") or [], "name_filter": first.get("name") or ""})
            hits = [p for p in (res.get("device") or {}).get("parameters") or []
                    if p.get("index") == params["parameter_index"]]
            if not hits:
                return None
            fresh = hits[0]
            # Solange der Text noch der alte ist, obwohl der Wert sich geaendert hat, noch einmal.
            if not (changed and fresh.get("display") == before.get("display")):
                break
        after = dict(first)
        after.update({k: fresh[k] for k in ("value", "display", "automation") if k in fresh})
        result["after"] = after
        if changed and after.get("display") == before.get("display"):
            return ("Die Anzeige zeigt nach %.1f s noch den alten Text '%s', der Rohwert ist gesetzt (%s). "
                    "Bei Plugins bekannt; im Plugin-Fenster nachsehen." % (
                        sum(REREAD_DELAYS), after.get("display"), after.get("value")))
        if after.get("display") != first.get("display"):
            return ("'after' neu gelesen: Direkt nach dem Setzen meldete Live noch die Anzeige '%s', einen "
                    "Durchlauf spaeter '%s'." % (first.get("display"), after.get("display")))
        return None
    return reread


def _param_index(ref, device_index: int, parameter: Union[int, str], chain_path) -> int:
    if isinstance(parameter, int):
        return parameter
    if re.fullmatch(r"\d+", str(parameter).strip()):
        return int(str(parameter).strip())
    res = _call("get_device_parameters", {"track": ref, "device_index": device_index,
                                          "chain_path": chain_path or [], "name_filter": ""})
    hits = [p for p in res["device"]["parameters"] if p["name"].lower() == str(parameter).strip().lower()]
    if len(hits) != 1:
        raise ValueError("Parameter '%s' %s auf Geraet %d '%s'." % (
            parameter, "nicht gefunden" if not hits else "mehrdeutig", device_index, res["device"].get("name")))
    return hits[0]["index"]


@mcp.tool()
@_tool
def find_parameter_value(track: TrackRef, device_index: int, parameter: Union[int, str], target: str,
                         chain_path: Optional[List[List[int]]] = None) -> str:
    """Sucht den Rohwert, bei dem ein Geraeteparameter einen gewuenschten Anzeigewert
    zeigt, z. B. DS Kick Pitch auf '41.17 Hz'. AENDERT NICHTS; danach mit
    set_device_parameter und dem gefundenen Wert setzen.

    Noetig, weil Rohwert und Anzeige oft nicht linear sind (Max-Geraete, Frequenzen,
    Zeiten). target: Anzeigewert mit oder ohne Einheit ('41.17 Hz', '1.2 kHz',
    '-6 dB', '35 %', '250 ms'). Geht nur bei Zahlen-Anzeigen, die mit dem Rohwert
    stetig steigen oder fallen; Auswahlparameter (On/Off, Listen) haben feste Stufen."""
    _require("find_parameter_value")
    ref = _track(track)
    index = _param_index(ref, device_index, parameter, chain_path)
    params = {"track": ref, "device_index": device_index, "chain_path": chain_path or [], "parameter_index": index,
              "target": str(target)}
    notes = []
    try:
        r = _call("find_parameter_value", params)
    except LiveError as e:
        # DS Kick Pitch zeigt ueber die API "41.17", die Einheit Hz steht nur in
        # der Max-Oberflaeche (am Set 12.10.2026). Dann die Zahl allein suchen.
        number = re.match(r"\s*([-+]?\d+(?:[.,]\d+)?)", str(target))
        if "zeigt ''" not in str(e) or not number:
            raise
        params["target"] = number.group(1)
        r = _call("find_parameter_value", params)
        notes.append("Die Anzeige hat ueber die API keine Einheit; verglichen wurde nur die Zahl %s. Ob '%s' "
                     "dieselbe Einheit meint wie Lives Anzeige, ist nicht pruefbar." % (number.group(1), target))
    cur = r.get("current") or {}
    value = "%.10g" % r["value"]
    lines = ["[%s] %s | Geraet %d '%s' | [%d] %s: Ziel '%s' -> Rohwert %s, Anzeige dort '%s'." % (
        views.ref_label(r.get("track")), r.get("track_name"), device_index, r.get("device"), index,
        r.get("parameter"), r.get("target"), value, r.get("display")),
        "Bereich: Rohwert %g..%g = '%s'..'%s' (%s). Jetzt: %g = '%s'%s." % (
            r.get("min"), r.get("max"), r.get("display_min"), r.get("display_max"), r.get("direction"),
            cur.get("value") or 0.0, cur.get("display"), views._auto(cur)),
        "Nichts geaendert. Setzen: set_device_parameter(track=%s, device_index=%d, parameter=%d, value=%s)%s" % (
            json.dumps(r.get("track")), device_index, index, value,
            ", chain_path=%s" % json.dumps(chain_path) if chain_path else "")]
    if cur.get("automation") == "active":
        notes.append("Der Parameter ist automatisiert; Setzen uebersteuert die Automation.")
    lines.extend("HINWEIS: " + n for n in notes)
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Geraete verwalten (Remote Script technoa.5)
# ----------------------------------------------------------------------

def _script_info() -> dict:
    if live.script_info is None:
        _call("get_script_info")
    return live.script_info or {}


def _require(command: str):
    info = _script_info()
    if command not in (info.get("capabilities") or []):
        raise ValueError("Das Remote Script in Live (%s) kennt '%s' noch nicht. Fassung %s einspielen "
                         "(deploy_remote_script.py) und Live neu starten."
                         % (info.get("script_version", "?"), command, EXPECTED_SCRIPT_VERSION))


def _require_feature(feature: str, what: str):
    """Fuer neue Parameter alter Befehle: Ein aelteres Script ignoriert sie still."""
    info = _script_info()
    if feature not in (info.get("features") or []):
        raise ValueError("Das Remote Script in Live (%s) kann %s noch nicht und wuerde den Wunsch still "
                         "ignorieren. Nichts geaendert. Fassung %s einspielen (deploy_remote_script.py) und Live "
                         "neu starten." % (info.get("script_version", "?"), what, EXPECTED_SCRIPT_VERSION))


def _chain_text(names: Optional[List[str]]) -> str:
    return ", ".join("%d %s" % (i, n) for i, n in enumerate(names or [])) or "(leer)"


def _deleted_dir() -> str:
    # Umstellbar nur fuer Tests.
    return os.environ.get("TECHNOA_DELETED_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                 "geloeschte_geraete")


def _save_json(folder: str, label: str, data: dict) -> Optional[str]:
    # Rueckweg, falls Lives Undo den Zustand nicht vollstaendig zurueckbringt
    # oder laengst andere Schritte darueber liegen.
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", label)[:80]
    base = os.path.join(folder, "%s_%s" % (time.strftime("%Y%m%d-%H%M%S"), safe))
    try:
        os.makedirs(folder, exist_ok=True)
        # Acht gleichnamige Clips in derselben Sekunde
        # ergaben drei Dateien, jede Sicherung ueberschrieb die vorige. "x"
        # legt nur neu an; ist der Name belegt, kommt die naechste Nummer.
        n = 1
        while True:
            path = base + (".json" if n == 1 else "_%d.json" % n)
            try:
                fh = open(path, "x", encoding="utf-8")
                break
            except FileExistsError:
                n += 1
        with fh:
            json.dump(dict(data, gesichert_um=time.strftime("%Y-%m-%d %H:%M:%S")), fh, indent=1, ensure_ascii=False)
        return path
    except OSError as e:
        log.warning("Sicherung nach %s fehlgeschlagen: %s", folder, e)
        return None


def _save_deleted(result: dict) -> Optional[str]:
    dev = result.get("deleted") or {}
    return _save_json(_deleted_dir(), "%s_%s" % (result.get("track_name"), dev.get("name")), result)


def _render_delete(result: dict) -> str:
    dev = result.get("deleted") or {}
    params = result.get("parameters") or []
    saved = _save_deleted(result)
    lines = ["[%s] %s: '%s' (Position %s, %s) geloescht." % (
        views.ref_label(result.get("track")), result.get("track_name"), dev.get("name"), dev.get("index"),
        dev.get("class_display_name") or dev.get("class_name")),
        "Kette vorher: " + _chain_text(result.get("chain_before")),
        "Kette jetzt:  " + _chain_text(result.get("chain_after"))]
    shown = ["%s=%s" % (p.get("name"), p.get("display")) for p in params[:40]]
    lines.append("Parameter vorher (%d%s): %s" % (len(params), ", erste 40" if len(params) > 40 else "",
                                                   "; ".join(shown)))
    lines.append("Vollstaendig gesichert: %s" % (saved or "NICHT gesichert (Schreibfehler, siehe Log)"))
    if dev.get("automated"):
        lines.append("HINWEIS: Das Geraet hatte Automation auf %s. Die ist mit geloescht; undo bringt sie "
                     "vermutlich zurueck, geprueft ist das nicht." % ", ".join(dev["automated"]))
    if "plugin" in (dev.get("class_name") or "").lower():
        lines.append("HINWEIS: Plugin. Ob undo den inneren Zustand (z. B. Serum-Patch) "
                     "vollstaendig zurueckbringt, ist ungeprueft.")
    return "\n".join(lines)


@mcp.tool()
@_tool
def delete_device(track: TrackRef, device_index: int, device_name: str) -> str:
    """Loescht ein Geraet von einer Spur (nicht aus Racks). AENDERT DAS SET, ein Undo-Schritt.

    device_name muss zum Geraet an device_index passen (Name aus get_track_info),
    sonst passiert nichts: Schutz gegen verrutschte Indizes nach Loeschen oder
    Verschieben. Die Antwort nennt die Parameterwerte vorher; vollstaendig als
    JSON in technoa_mcp/geloeschte_geraete/. Ob undo bei Plugins den inneren
    Zustand zurueckbringt, ist ungeprueft."""
    _require("delete_device")
    return _write("delete_device", {"track": _track(track), "device_index": device_index,
                                    "expect_name": device_name}, render=_render_delete)


def _render_move(result: dict) -> str:
    cross = result.get("target_chain_after") is not None
    lines = ["[%s] %s: '%s' von Position %s nach %s%s." % (
        views.ref_label(result.get("track")), result.get("track_name"), result.get("device"),
        result.get("from_index"), result.get("new_index"),
        (" auf [%s] %s" % (views.ref_label(result.get("target_track")), result.get("target_track_name")))
        if cross else "")]
    lines.append("Kette vorher: " + _chain_text(result.get("chain_before")))
    lines.append("Kette jetzt:  " + _chain_text(result.get("chain_after")))
    if cross:
        lines.append("Ziel vorher:  " + _chain_text(result.get("target_chain_before")))
        lines.append("Ziel jetzt:   " + _chain_text(result.get("target_chain_after")))
    if result.get("new_index") != result.get("requested_index"):
        lines.append("ACHTUNG: Gewuenscht war Position %s, das Geraet steht aber bei %s (Live meldet %s). "
                     "Mit undo zurueck oder erneut verschieben." % (result.get("requested_index"),
                                                                    result.get("new_index"),
                                                                    result.get("live_position")))
    return "\n".join(lines)


@mcp.tool()
@_tool
def move_device(track: TrackRef, device_index: int, to_index: int, to_track: Optional[TrackRef] = None,
                device_name: Optional[str] = None) -> str:
    """Verschiebt ein Geraet in der Kette (oder auf eine andere Spur). AENDERT DAS SET,
    ein Undo-Schritt. Die Instanz bleibt dieselbe (Plugin-Zustand).

    to_index: Position NACH dem Verschieben, wie get_track_info sie dann zeigt
    (0 = ganz vorn). device_name (empfohlen): muss zum Geraet an device_index
    passen, sonst passiert nichts. Unmoegliche Positionen (Audioeffekt vor dem
    Instrument) lehnt Live ab; dann bleibt alles, wie es war."""
    _require("move_device")
    params: Dict[str, Any] = {"track": _track(track), "device_index": device_index, "target_index": to_index}
    if to_track is not None:
        params["target_track"] = _track(to_track)
    if device_name is not None:
        params["expect_name"] = device_name
    return _write("move_device", params, render=_render_move)


def _render_new(verb: str):
    def render(result: dict) -> str:
        name = result.get("device_name") or result.get("device")
        lines = ["[%s] %s: '%s' %s an Position %s." % (views.ref_label(result.get("track")), result.get("track_name"),
                                                     name, verb, result.get("new_index"))]
        lines.append("Kette jetzt: " + _chain_text(result.get("chain_after")))
        if result.get("new_index") is None:
            lines.append("ACHTUNG: Das neue Geraet war nicht eindeutig zu finden (neu: %s). Kette pruefen."
                         % result.get("new_indices"))
        elif result.get("requested_position") is not None and result["new_index"] != result["requested_position"]:
            lines.append("ACHTUNG: Gewuenscht war Position %s." % result["requested_position"])
        return "\n".join(lines)
    return render


@mcp.tool()
@_tool
def insert_device(track: TrackRef, device_name: str, position: Optional[int] = None) -> str:
    """Fuegt ein Live-eigenes Geraet direkt an einer Position ein (Lives
    Track.insert_device). AENDERT DAS SET, ein Undo-Schritt.

    device_name: Name wie im Browser, z. B. "Utility", "EQ Eight", "Shifter".
    position: Index danach (0 = ganz vorn); ohne Angabe ans Ende. Plugins und
    Presets gehen so nicht: load_instrument_or_effect mit position."""
    _require("insert_device")
    params: Dict[str, Any] = {"track": _track(track), "device_name": device_name}
    if position is not None:
        params["position"] = int(position)
    return _write("insert_device", params, render=_render_new("eingefuegt"))


@mcp.tool()
@_tool
def duplicate_device(track: TrackRef, device_index: int, device_name: Optional[str] = None) -> str:
    """Dupliziert ein Geraet; die Kopie landet direkt dahinter. AENDERT DAS SET, ein
    Undo-Schritt. device_name (empfohlen) wie bei move_device. Eine Plugin-Kopie
    ist eine neue Instanz."""
    _require("duplicate_device")
    params: Dict[str, Any] = {"track": _track(track), "device_index": device_index}
    if device_name is not None:
        params["expect_name"] = device_name
    return _write("duplicate_device", params, render=_render_new("dupliziert"))


# ----------------------------------------------------------------------
# Groove, Routing, Sidechain (Remote Script technoa.7)
# ----------------------------------------------------------------------

NO_GROOVE = ("", "keiner", "kein", "ohne", "aus", "none")


def _route_text(side: Optional[dict]) -> str:
    side = side or {}
    chan = side.get("channel")
    return "%s%s" % (side.get("type") or "?", " / %s" % chan if chan else "")


def _starts_text(tl: views.Timeline, starts: List[float]) -> str:
    starts = sorted(starts)
    if len(starts) <= 4:
        return ", ".join(tl.pos(s) for s in starts)
    return "%s, %s ... %s" % (tl.pos(starts[0]), tl.pos(starts[1]), tl.pos(starts[-1]))


def _groove_name(groove: Optional[str]) -> Optional[str]:
    """Name aus dem Groove-Pool (exakt oder eindeutiger Teil) oder None = entfernen."""
    if groove is None or str(groove).strip().lower() in NO_GROOVE:
        return None
    pool = [g.get("name") for g in _state().get("grooves") or [] if g.get("name")]
    text = str(groove).strip().lower()
    hits = [n for n in pool if n.lower() == text] or [n for n in pool if text in n.lower()]
    if len(hits) == 1:
        return hits[0]
    listing = ", ".join("'%s'" % n for n in pool) or "nichts (Groove erst in Lives Groove-Pool ziehen)"
    raise ValueError("Groove '%s' ist im Groove-Pool %s. Dort liegt: %s." % (
        groove, "nicht zu finden" if not hits else "mehrdeutig", listing))


def _render_groove(tl: views.Timeline, start: Optional[float], end: Optional[float]):
    def render(r: dict) -> str:
        where = "[%s] %s" % (views.ref_label(r.get("track")), r.get("track_name"))
        if start is None and end is None:
            rng = "ganze Spur"
        else:
            rng = "Clips, die %s%s beginnen" % ("ab %s" % tl.pos(start) if start is not None else "vom Anfang",
                                                " bis vor %s" % tl.pos(end) if end is not None else " bis zum Ende")
        target = ("Groove '%s'" % r["groove"]) if r.get("groove") else "keinen Groove"
        changed = r.get("changed") or []
        lines = ["%s: %d Clip(s) auf %s gesetzt (%s), ein Undo-Schritt." % (where, len(changed), target, rng)]
        by_before: Dict[Any, List[float]] = {}
        for c in changed:
            by_before.setdefault(c.get("before"), []).append(c["start_time"])
        for before, starts in by_before.items():
            lines.append("  vorher %s: %s" % ("'%s'" % before if before else "kein Groove", _starts_text(tl, starts)))
        if r.get("already"):
            lines.append("  hatten schon %s, unveraendert: %d Clip(s), %s" % (
                target, len(r["already"]), _starts_text(tl, r["already"])))
        if r.get("failed"):
            lines.append("  ACHTUNG: %d Clip(s) hat Live nicht angenommen, z. B. ab %s: %s" % (
                len(r["failed"]), tl.pos(r["failed"][0].get("start_time")), r["failed"][0].get("error")))
        if r.get("reaching_in"):
            lines.append("  Nicht erfasst, weil sie vor dem Bereich beginnen und hineinreichen: %s. Gehoeren sie dazu, "
                         "from_bar frueher setzen." % _starts_text(tl, r["reaching_in"]))
        return "\n".join(lines)
    return render


def _reread_groove(ref, start: Optional[float], end: Optional[float], name: Optional[str]):
    def reread(result: dict) -> Optional[str]:
        changed = {round(c["start_time"], 4) for c in result.get("changed") or []}
        if not changed:
            return None
        time.sleep(REREAD_DELAYS[0])
        clips = _call("get_arrangement_clips", {"track": ref, "start_time": start, "end_time": end,
                                                "include_digest": False}).get("clips") or []
        wrong = [c for c in clips if round(c.get("start_time", -1.0), 4) in changed and c.get("groove") != name]
        if not wrong:
            return None
        tl = _timeline()
        return ("Neu gelesen haben %d der %d geaenderten Clips NICHT %s, z. B. ab %s: %s. In Live nachsehen." % (
            len(wrong), len(changed), "'%s'" % name if name else "keinen Groove",
            tl.pos(wrong[0].get("start_time")), wrong[0].get("groove") or "kein Groove"))
    return reread


@mcp.tool()
@_tool
def set_clip_groove(track: TrackRef, from_bar: Optional[Union[float, str]], to_bar: Optional[Union[float, str]],
                    groove: Optional[str]) -> str:
    """Setzt oder entfernt den Groove aller Arrangement-Clips einer Spur, die im Bereich
    BEGINNEN. AENDERT DAS SET, ein Undo-Schritt fuer alle Clips zusammen.

    from_bar: erster Takt (zaehlt mit), to_bar: Takt, ab dem nicht mehr (zaehlt nicht mit);
      Takt als Zahl (33) oder wie in get_arrangement_clips ("33.3.1"). null = vom Anfang
      bzw. bis zum Ende. Clips, die vorher beginnen und hineinreichen, bleiben, wie sie
      sind; die Antwort nennt sie.
    groove: Name aus Lives Groove-Pool (Zeile GROOVES in get_set_overview) oder ein
      eindeutiger Teil davon. null oder "keiner" = Groove entfernen: Das bietet Lives API
      nicht an (Live 12.4.6 und 12.4.7, am Set 22.10.2026); die Antwort nennt dann den
      Handgriff, geaendert wird nichts.
    Beispiel: set_clip_groove("SYNTH PULSE", 33, 65, "Swing 16ths 66") = Takte 33 bis 64."""
    _require("set_clip_groove")
    ref = _track(track)
    tl = _timeline()
    start = tl.parse_pos(from_bar) if from_bar is not None else None
    end = tl.parse_pos(to_bar) if to_bar is not None else None
    if start is not None and end is not None and end <= start:
        raise ValueError("to_bar %s liegt nicht hinter from_bar %s. to_bar ist der erste Takt, der NICHT mehr "
                         "dazugehoert (Takte 33 bis 64: from_bar=33, to_bar=65)." % (to_bar, from_bar))
    name = _groove_name(groove)
    return _write("set_clip_groove", {"track": ref, "start_time": start, "end_time": end, "groove": name},
                  render=_render_groove(tl, start, end), reread=_reread_groove(ref, start, end, name))


TRACK_REF_TEXT = re.compile(r"-?\d+|[a-l]|return[:\s]*\d+|master|main", re.IGNORECASE)


def _routing_choice(options: List[dict], wanted: Union[int, str], what: str) -> dict:
    """Eine Moeglichkeit aus get_routing_options: Spur (Index, A/B, master), exakter
    Name oder eindeutiger Namensteil. Spuren gehen ueber Lives attached_object, damit
    zwei gleich benannte Spuren unterscheidbar bleiben."""
    if not options:
        raise ValueError("%s: Live bietet hier keine Auswahl an." % what)
    text = str(wanted).strip()
    if not isinstance(wanted, bool) and (isinstance(wanted, int) or TRACK_REF_TEXT.fullmatch(text)):
        ref = _track(wanted)
        hits = [o for o in options if o.get("track") == ref]
        if len(hits) == 1:
            return hits[0]
        ov = _cached("overview", "get_tracks_overview")
        name = next((t["name"] for t in ov["tracks"] + ov["returns"] + [ov["master"]] if t["ref"] == ref), None)
        same = [o for o in options if name and (o.get("name") or "").lower() == name.lower()]
        if len(same) == 1:
            return same[0]
    low = text.lower()
    hits = ([o for o in options if (o.get("name") or "").lower() == low]
            or [o for o in options if low in (o.get("name") or "").lower()])
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ValueError("%s '%s' gibt es hier nicht. Waehlbar: %s." % (
            what, wanted, ", ".join(views._option(o) for o in options)))
    raise ValueError("%s '%s' ist mehrdeutig: %s. Genauer angeben, bei Spuren auch als Nummer." % (
        what, wanted, ", ".join(views._option(o) for o in hits)))


def _reread_routing(ref, device_index: Optional[int], seen: dict):
    """Liest Ein-/Ausgang bzw. Sidechain einen Durchlauf spaeter neu. Ob Live
    Routing im selben Durchlauf uebernimmt, war vor technoa.7 ungeprueft."""
    def reread(result: dict) -> Optional[str]:
        time.sleep(REREAD_DELAYS[0])
        if device_index is None:
            info = _call("get_track_info", {"track": ref})
            seen["info"] = info
            routing = info.get("routing") or {}
            fresh = {side: {"type": routing.get(side + "_type"), "channel": routing.get(side + "_channel")}
                     for side in ("output", "input")}
        else:
            dev = _call("get_device_parameters", {"track": ref, "device_index": device_index, "chain_path": [],
                                                  "name_filter": "S/C On"})["device"]
            sc = dev.get("sidechain") or {}
            fresh = {"input": {"type": sc.get("type"), "channel": sc.get("channel")}}
            if sc and result.get("after") != sc:
                seen["switch"] = (result.get("after") or {}).get("on"), sc.get("on")
                result["after"] = sc
        notes = []
        for c in result.get("changes") or []:
            f = fresh.get(c.get("side"))
            now = c.get("after") or {}
            if f and (f["type"], f["channel"] or "") != (now.get("type"), now.get("channel") or ""):
                notes.append("direkt nach dem Setzen %s, einen Durchlauf spaeter %s" % (_route_text(now),
                                                                                      _route_text(f)))
                c["after"] = f
        if notes:
            return "'nachher' neu gelesen: " + "; ".join(notes) + "."
        return None
    return reread


def _render_routing(r: dict) -> str:
    where = "[%s] %s" % (views.ref_label(r.get("track")), r.get("track_name"))
    if r.get("device") is not None:
        where += " | Geraet %s '%s'" % (r.get("device_index"), r.get("device"))
    lines = []
    for c in r.get("changes") or []:
        what = "Ausgang" if c.get("side") == "output" else ("Sidechain-Quelle" if r.get("device") else "Eingang")
        lines.append("%s: %s %s -> %s" % (where, what, _route_text(c.get("before")), _route_text(c.get("after"))))
        lines.extend("  ACHTUNG: " + w for w in c.get("warnings") or [])
    if r.get("device") is not None:
        b, a = r.get("before") or {}, r.get("after") or {}
        if b.get("on") != a.get("on"):
            lines.append("%s: Schalter S/C On %s -> %s" % (where, views._onoff_text(b.get("on")),
                                                           views._onoff_text(a.get("on"))))
        lines.append("Sidechain jetzt: %s" % views.sidechain_text({"sidechain": a}))
    return "\n".join(lines)


@mcp.tool()
@_tool
def get_routing_options(track: TrackRef, device_index: Optional[int] = None) -> str:
    """Ein- und Ausgang einer Spur mit allen waehlbaren Moeglichkeiten und Kanaelen; mit
    device_index der Sidechain-Eingang eines Geraets (Lives Compressor). AENDERT NICHTS.
    Vor set_routing/set_sidechain, wenn unklar ist, wie Live eine Quelle nennt."""
    _require("get_routing_options")
    params: Dict[str, Any] = {"track": _track(track)}
    if device_index is not None:
        params["device_index"] = device_index
    return views.routing_options(_call("get_routing_options", params))


@mcp.tool()
@_tool
def set_routing(track: TrackRef, output: Optional[Union[int, str]] = None, output_channel: Optional[str] = None,
                input: Optional[Union[int, str]] = None, input_channel: Optional[str] = None) -> str:
    """Setzt Ausgang und/oder Eingang einer Spur. AENDERT DAS SET, ein Undo-Schritt.

    output/input: Name wie in get_routing_options ("Main", "Ext. Out", Gruppenname,
      "Resampling") oder eine Spur als Nummer/A/B; ein eindeutiger Namensteil reicht.
    output_channel/input_channel: Kanal wie in get_routing_options ("Post FX", "1/2").
      Ohne Angabe waehlt Live nach einem Wechsel den ersten Kanal.
    Der Eingang zaehlt nur bei MIDI- und Audiospuren (Aufnahme, Monitoring, Abhoeren).
    Beispiel: set_routing("HAT CLOSED", output="DRUMS")."""
    _require("set_track_routing")
    ref = _track(track)
    opts = _call("get_routing_options", {"track": ref})
    if (input is not None or input_channel is not None) and opts.get("kind") not in ("midi", "audio"):
        raise ValueError("[%s] %s (%s): Dort hat der Eingang keine Bedeutung, nur bei MIDI- und Audiospuren. "
                         "Nichts geaendert." % (views.ref_label(ref), opts.get("track_name"),
                                                views.KIND_LABELS.get(opts.get("kind"), "?")))
    params: Dict[str, Any] = {"track": ref}
    for side, wanted, chan, what in (("output", output, output_channel, "Ausgang"),
                                     ("input", input, input_channel, "Eingang")):
        if wanted is not None:
            choice = _routing_choice(((opts.get(side) or {}).get("types") or []), wanted, what)
            params[side + "_type"] = choice["name"]
            if choice.get("track") is not None:
                params[side + "_type_track"] = choice["track"]
        if chan is not None:
            params[side + "_channel"] = str(chan)
    if len(params) == 1:
        raise ValueError("Nichts zu setzen: output, output_channel, input oder input_channel angeben.")
    seen: Dict[str, Any] = {}
    text = _write("set_track_routing", params, render=_render_routing, reread=_reread_routing(ref, None, seen))
    info = seen.get("info")
    if info is not None and "output_type" in params:
        before = (((opts.get("output") or {}).get("type")) or {}).get("name")
        was = views.unusual_output(dict(info, routing={"output_type": before}))
        now = views.unusual_output(info)
        if now:
            text += ("\nHINWEIS: Der Ausgang weicht jetzt vom Ueblichen ab (ueblich: %s); get_set_overview markiert "
                     "ihn, bis er zurueckgesetzt ist." % now)
        elif was:
            text += "\nAusgang ist wieder der uebliche (%s)." % views.usual_output(info)
    return text


@mcp.tool()
@_tool
def set_sidechain(track: TrackRef, device_index: int, source: Optional[Union[int, str]] = None,
                  channel: Optional[str] = None, enable: Optional[bool] = None,
                  device_name: Optional[str] = None) -> str:
    """Setzt die Sidechain-Quelle eines Geraets (Lives Compressor) und auf Wunsch den
    Schalter "S/C On". AENDERT DAS SET, ein Undo-Schritt.

    source: Spur als Name, Namensteil oder Nummer, wie in get_routing_options(track,
      device_index). channel: "Pre FX", "Post FX" oder "Post Mixer"; ohne Angabe nimmt
      Live nach einem Wechsel den ersten Kanal. enable: true/false schaltet S/C On.
      Ohne S/C On hoert das Geraet die Quelle nicht.
    device_name (empfohlen): Name des Geraets; passt er nicht zum Index, passiert nichts.
    Beispiel: set_sidechain("RUMBLE", 2, source="KICK_PRINT_v1", channel="Post FX")."""
    _require("set_sidechain")
    ref = _track(track)
    params: Dict[str, Any] = {"track": ref, "device_index": device_index}
    if device_name is not None:
        params["expect_name"] = device_name
    opts = _call("get_routing_options", dict(params))
    if source is not None:
        choice = _routing_choice(((opts.get("input") or {}).get("types") or []), source, "Sidechain-Quelle")
        params["input_type"] = choice["name"]
        if choice.get("track") is not None:
            params["input_type_track"] = choice["track"]
    if channel is not None:
        params["input_channel"] = str(channel)
    if enable is not None:
        params["enable"] = bool(enable)
    if not any(k in params for k in ("input_type", "input_channel", "enable")):
        raise ValueError("Nichts zu setzen: source, channel oder enable angeben.")
    seen: Dict[str, Any] = {}
    text = _write("set_sidechain", params, render=_render_routing, reread=_reread_routing(ref, device_index, seen))
    if "switch" in seen:
        text += "\nHINWEIS: Der Schalter S/C On stand direkt nach dem Setzen auf %s, einen Durchlauf spaeter auf %s." % (
            views._onoff_text(seen["switch"][0]), views._onoff_text(seen["switch"][1]))
    return text


@mcp.tool()
@_tool
def set_tempo(bpm: float) -> str:
    """Setzt das Songtempo. AENDERT DAS SET."""
    return _write("set_tempo", {"tempo": bpm})


def _off_grid(start: float, length: float, bar: float) -> Optional[tuple]:
    """(Rest am Loop-Ende, Versatz des Loop-Anfangs) in Beats gegen das Taktraster
    ab Songanfang, oder None, wenn der Loop in ganzen Takten aufgeht."""
    if not bar or length <= 0:
        return None

    def rest(x):
        r = math.fmod(x, bar)
        return 0.0 if min(abs(r), abs(bar - r)) < 1e-6 else r

    tail, offset = rest(length), rest(start)
    return None if tail == 0.0 and offset == 0.0 else (tail, offset)


@mcp.tool()
@_tool
def set_loop(start_bar: Optional[float] = None, length_bars: Optional[float] = None, enabled: Optional[bool] = None,
             start_beat: Optional[float] = None, length_beats: Optional[float] = None) -> str:
    """Setzt den Arrangement-Loop. Takte 1-basiert (start_bar=33, length_bars=16)
    oder genau in Beats. enabled schaltet den Loop an/aus. AENDERT DAS SET.
    In Live KEIN Undo-Schritt: zum Zuruecksetzen die alten Werte erneut setzen."""
    tl = _timeline()
    params: Dict[str, Any] = {}
    start = _beat(start_bar, start_beat, tl, "Loopstart")
    if start is not None:
        params["loop_start"] = start
    if length_bars is not None and length_beats is not None:
        raise ValueError("Laenge entweder in Takten oder in Beats angeben.")
    if length_bars is not None:
        params["loop_length"] = length_bars * tl.beats_per_bar
    elif length_beats is not None:
        params["loop_length"] = length_beats
    if enabled is not None:
        params["enabled"] = enabled

    def reread(result: dict) -> Optional[str]:
        # Den Schalter uebernimmt Live erst im naechsten Durchlauf, Start und
        # Laenge sofort. Ein eigener Befehl laeuft in einem spaeteren Durchlauf.
        st = _call("get_session_info")
        first = result.get("after") or {}
        fresh = {"loop": bool(st.get("loop")), "loop_start": float(st.get("loop_start") or 0.0),
                 "loop_length": float(st.get("loop_length") or 0.0)}
        result["after"] = fresh
        if first != fresh:
            return ("'nachher' neu gelesen: Direkt nach dem Setzen meldete Live noch %s." % (
                "Loop " + ("an" if first.get("loop") else "aus")
                if first.get("loop") != fresh["loop"] else json.dumps(first)))
        return None

    def render(result: dict) -> str:
        def one(s):
            start, length = s.get("loop_start") or 0.0, s.get("loop_length") or 0.0
            return "%s, %s bis %s (%g Beats)" % ("an" if s.get("loop") else "aus", tl.pos(start),
                                                tl.pos(start + length), length)
        lines = ["Loop vorher:  " + one(result.get("before") or {}), "Loop nachher: " + one(result.get("after") or {})]
        misfit = _off_grid((result.get("after") or {}).get("loop_start") or 0.0,
                           (result.get("after") or {}).get("loop_length") or 0.0, tl.beats_per_bar)
        if misfit:
            lines.append("HINWEIS: Der Loop geht nicht in ganzen Takten auf (Rest %g, Versatz %g Beats). "
                         "Meist ein Versehen; wer am Loop misst oder ihn kopiert, bekommt am Sprung einen "
                         "angeschnittenen Takt." % misfit)
        return "\n".join(lines)

    return _write("set_loop", params, forget=True, render=render, reread=reread)


@mcp.tool()
@_tool
def set_arrangement_time(bar: Optional[float] = None, beat: Optional[float] = None) -> str:
    """Setzt den Playhead (Takt 1-basiert oder Beat ab 0)."""
    state = _call("get_session_info")
    tl = _timeline(state)
    t = _beat(bar, beat, tl, "Position")
    if t is None:
        raise ValueError("bar oder beat angeben.")
    if t > state["song_length"] + 1e-6:
        raise ValueError("%s liegt hinter dem Songende (%s). Live setzt den Playhead nicht dorthin."
                         % (tl.pos(t), tl.pos(state["song_length"])))
    _write("set_current_song_time", {"time": t})
    # Live uebernimmt den Playhead bei gestopptem Transport erst im naechsten
    # Durchlauf; die Antwort des Befehls selbst zeigt noch den alten Wert.
    now = _call("get_session_info")["current_song_time"]
    return "Playhead jetzt bei %s (Beat %g)." % (tl.pos(now), now)


@mcp.tool()
@_tool
def start_playback(bar: Optional[Union[float, str]] = None) -> str:
    """Startet die Wiedergabe. ACHTUNG: Live startet am Startmarker, NICHT am Playhead
    und nicht im Loop (am 12.10.2026 begann es bei Takt 1, obwohl der Playhead bei
    Takt 45 im Loop stand). bar: Position, an die danach gesprungen wird: Takt (33),
    Takt.Schlag.Sechzehntel ("33.1.1") oder "loop" fuer den Loopanfang. Fuer
    Messungen im Loop also bar="loop". Die Antwort meldet, wo die Wiedergabe
    tatsaechlich laeuft."""
    tl = _timeline()
    _write("start_playback")
    target = None
    if bar is not None:
        if str(bar).strip().lower() == "loop":
            target = _call("get_session_info")["loop_start"]
        else:
            target = tl.parse_pos(bar)
        _write("set_current_song_time", {"time": target})
    # Live uebernimmt Start und Sprung erst in den naechsten Durchlaeufen.
    time.sleep(0.3)
    st = _call("get_session_info")
    now = st.get("current_song_time")
    loop = (st.get("loop_start"), (st.get("loop_start") or 0.0) + (st.get("loop_length") or 0.0)) \
        if st.get("loop") else None
    lines = ["Wiedergabe %s, Song-Position jetzt %s%s." % (
        "laeuft" if st.get("is_playing") else "STEHT", tl.pos(now),
        (" | Loop an, %s bis %s" % (tl.pos(loop[0]), tl.pos(loop[1]))) if loop else " | Loop aus")]
    if target is not None and now is not None and not (target - 0.01 <= now <= target + 2 * tl.beats_per_bar):
        lines.append("HINWEIS: Gewuenscht war %s; der Sprung ist (noch) nicht angekommen. Lives Quantisierung "
                     "kann ihn verzoegern. Mit get_session_info nachsehen." % tl.pos(target))
    elif loop and now is not None and now < loop[0]:
        lines.append("HINWEIS: Die Position liegt vor dem Loop; bis zum Loopanfang spielt das Arrangement davor "
                     "(Intro). Fuer Messungen im Loop bar=\"loop\".")
    return "\n".join(lines)


@mcp.tool()
@_tool
def stop_playback() -> str:
    """Stoppt die Wiedergabe."""
    return _write("stop_playback")


@mcp.tool()
@_tool
def switch_to_arrangement_view() -> str:
    """Holt die Arrangement-Ansicht nach vorn."""
    return _write("switch_to_arrangement_view")


@mcp.tool()
@_tool
def create_locator(name: str, bar: Optional[float] = None, beat: Optional[float] = None) -> str:
    """Setzt einen Locator (oder benennt einen vorhandenen an derselben Stelle um). AENDERT DAS SET.
    Nur bei gestopptem Transport und nicht hinter dem Songende."""
    t = _beat(bar, beat, _timeline(), "Locator")
    if t is None:
        raise ValueError("bar oder beat angeben.")
    if (live.script_info or {}).get("script_version") == "1.7.1+technoa.2":
        raise ValueError("Mit Remote Script technoa.2 landet der Locator an der alten Playhead-Position "
                         "(Fehler vom 07.10.2026). Erst Live neu starten, damit technoa.3 laeuft.")
    return _write("create_locator", {"name": name, "time": t})


# Arm nach neuer Spur bzw. geladenem Instrument (11.10.2026): KICK SUB war
# danach scharf, ohne dass jemand Arm gedrueckt hatte. Arm ist in Live kein
# Undo-Schritt; ein unbemerktes Arm nimmt bei der naechsten Aufnahme auf, und
# mit Lives "Exclusive Arm" koennte es eine andere Spur (ATMO PAD) still
# ausschalten. Deshalb vorher und nachher alle Spuren vergleichen.

def _arm_states() -> List[dict]:
    ov = _call("get_tracks_overview")
    return [{"ref": t["ref"], "name": t.get("name"), "arm": bool(t.get("arm")),
             "implicit": bool(t.get("implicit_arm"))} for t in ov["tracks"]]


def _arm_notes(before: List[dict], after: List[dict], focus, what: str, disarm: bool,
               inserted: Optional[int] = None) -> List[str]:
    """Zeilen (HINWEIS/ACHTUNG) zu Arm auf der Spur focus und auf allen anderen Spuren.

    inserted: Index einer neu angelegten Spur; die steht im Vorher-Stand nicht,
    und alle Spuren dahinter sind um eins verrutscht."""
    notes = []
    focus_after = next((t for t in after if t["ref"] == focus), None)
    focus_before = next((t for t in before if t["ref"] == focus), None) if inserted is None else None
    compare = [t for t in after if t["ref"] != inserted]
    # Die Spur selbst darf anders heissen: Live benennt "15-MIDI" beim Laden
    # eines Instruments in "15-DS Kick" um (am Set 12.10.2026).
    if len(compare) == len(before) and all(a["name"] == b["name"] for a, b in zip(compare, before)
                                           if a["ref"] != focus):
        for b, a in zip(before, compare):
            if a["ref"] == focus:
                continue
            if b["arm"] != a["arm"] or b["implicit"] != a["implicit"]:
                notes.append("ACHTUNG: Arm hat sich auch auf [%s] %s geaendert: %s -> %s (Lives Exclusive Arm?). "
                             "Kein Undo-Schritt; zurueck mit set_mixer(track=%s, arm=%s)." % (
                                 a["ref"], a["name"], _arm_word(b), _arm_word(a), json.dumps(a["ref"]),
                                 "true" if b["arm"] else "false"))
    else:
        notes.append("HINWEIS: Arm der uebrigen Spuren nicht verglichen: Die Spurliste hat sich unerwartet "
                     "geaendert.")
    if focus_after is None:
        return notes
    label = "[%s] %s" % (focus_after["ref"], focus_after["name"])
    was = focus_before["arm"] if focus_before else False
    if focus_after["arm"] and was:
        notes.append("HINWEIS: %s war schon vorher scharf (Arm an)." % label)
    elif focus_after["arm"] and disarm:
        _write("set_mixer", {"track": focus, "arm": False})
        now = next((t for t in _arm_states() if t["ref"] == focus), focus_after)
        notes.append("HINWEIS: %s war nach %s SCHARF (Lives Auto-Arm) und ist jetzt %s (disarm=true, kein "
                     "Undo-Schritt)." % (label, what, "wieder aus" if not now["arm"] else "IMMER NOCH SCHARF"))
        focus_after = now
    elif focus_after["arm"]:
        notes.append("HINWEIS: %s ist nach %s SCHARF (Lives Auto-Arm): Eine Aufnahme landet dort, ein "
                     "MIDI-Keyboard spielt das Instrument. Zuruecknehmen: set_mixer(track=%s, arm=false) oder gleich disarm=true; "
                     "kein Undo-Schritt." % (label, what, json.dumps(focus)))
    if focus_after["implicit"] and not focus_after["arm"]:
        notes.append("HINWEIS: %s hat implicit_arm (Lives Auto-Arm ueber die Auswahl, ohne Arm-Knopf). Ob darauf "
                     "aufgenommen wird, ist ungeprueft." % label)
    return notes


def _arm_word(t: dict) -> str:
    return "scharf" if t["arm"] else ("implizit scharf" if t["implicit"] else "aus")


def _create_track(command: str, kind: str, index: int, disarm: bool) -> str:
    before = _arm_states()
    result: Dict[str, Any] = {}
    write_notes = _write(command, {"index": index}, forget=True,
                         render=lambda r: result.update(r if isinstance(r, dict) else {}) or "").strip()
    after = _arm_states()
    new_index = result.get("index", len(after) - 1 if index == -1 else index)
    info = _call("get_track_info", {"track": new_index})
    routing = info.get("routing") or {}
    lines = ["Neue %s-Spur [%d] '%s' angelegt%s | Ausgang %s | ein Undo-Schritt." % (
        kind, new_index, info.get("name"),
        (" in Gruppe [%s] %s" % (info.get("group"), info.get("group_name"))) if info.get("group") is not None else "",
        routing.get("output_type", "?"))]
    if routing.get("output_type", "").lower() == "no output":
        lines.append("HINWEIS: 'No Output' hatte eine neue MIDI-Spur am 11.10.2026 auch, bis ein Instrument geladen "
                     "war; danach stimmte der Ausgang. Nach dem Laden pruefen.")
    if write_notes:
        lines.append(write_notes)
    lines.extend(_arm_notes(before, after, new_index, "dem Anlegen", disarm, inserted=new_index))
    lines.append("Spuren, die vorher ab Index %d standen, haben jetzt einen Index mehr." % new_index if index != -1
                 else "Spur ans Ende gehaengt; andere Indizes unveraendert.")
    return "\n".join(lines)


@mcp.tool()
@_tool
def create_midi_track(index: int = -1, disarm: bool = False) -> str:
    """Legt eine MIDI-Spur an (-1 = ans Ende; ein Index innerhalb einer Gruppe legt sie
    in die Gruppe). AENDERT DAS SET, ein Undo-Schritt. Meldet Gruppe, Ausgang und ob
    Live die Spur scharf geschaltet hat (Auto-Arm); disarm=true nimmt das gleich
    zurueck (Arm ist kein Undo-Schritt). Danach meist load_instrument_or_effect."""
    return _create_track("create_midi_track", "MIDI", index, disarm)


@mcp.tool()
@_tool
def create_audio_track(index: int = -1, disarm: bool = False) -> str:
    """Legt eine Audiospur an (-1 = ans Ende; ein Index innerhalb einer Gruppe legt sie
    in die Gruppe). AENDERT DAS SET, ein Undo-Schritt. Meldet Arm wie create_midi_track."""
    return _create_track("create_audio_track", "Audio", index, disarm)


@mcp.tool()
@_tool
def create_clip(track: TrackRef, clip_index: int, length: float = 4.0) -> str:
    """Legt einen leeren MIDI-Clip in einem Session-Slot an (Laenge in Beats). AENDERT DAS SET."""
    return _write("create_clip", {"track": _track(track), "clip_index": clip_index, "length": length})


@mcp.tool()
@_tool
def create_audio_clip(track: TrackRef, clip_index: int, path: str) -> str:
    """Importiert eine Audiodatei (absoluter Pfad) in einen Session-Slot einer Audiospur. AENDERT DAS SET."""
    # forget: Live benennt die Spur nach dem ersten Audioclip um.
    return _write("create_audio_clip", {"track": _track(track), "clip_index": clip_index, "path": path},
                  forget=True)


# Notenfelder, die das Script an Live gibt (technoa.8), mit Lives Bereichen.
NOTE_REQUIRED = ("pitch", "start_time", "duration")
NOTE_FIELDS = NOTE_REQUIRED + ("velocity", "mute") + views.NOTE_EXTRA_FIELDS
NOTE_DEFAULTS = {"velocity": 100.0, "mute": False}
NOTE_RANGES = {"pitch": (0, 127), "velocity": (0, 127), "probability": (0.0, 1.0),
               "velocity_deviation": (-127.0, 127.0), "release_velocity": (0, 127)}
# Andere Schreibweisen, die vorkommen: Notenlisten aus anderen Werkzeugen nutzen
# z. B. start_beat und duration_beat.
NOTE_ALIASES = {"start_beat": "start_time", "start": "start_time", "time": "start_time", "position": "start_time",
                "duration_beat": "duration", "duration_beats": "duration", "length": "duration",
                "note": "pitch", "midi": "pitch", "vel": "velocity", "muted": "mute",
                "chance": "probability", "prob": "probability", "velocity_dev": "velocity_deviation",
                "vel_deviation": "velocity_deviation", "deviation": "velocity_deviation",
                "release": "release_velocity", "velocity_release": "release_velocity",
                "off_velocity": "release_velocity"}


def _check_notes(notes: Any) -> List[dict]:
    """Prueft die Noten vor dem Senden. Bei einem Fehler wird nichts geschrieben.

    Bis technoa.7 fiel ein unbekanntes Feld still weg, und ein fehlendes bekam
    einen Ersatzwert: Noten mit start_beat statt start_time waeren alle auf
    Beat 0 gelandet, als Sechzehntel. Eine abgelehnte Liste kostet einen
    Aufruf, ein falsch geschriebener Clip einen Undo-Schritt und ein Nachhoeren."""
    if not isinstance(notes, list) or not notes:
        raise ValueError('notes ist leer. Erwartet: [{"pitch": 36, "start_time": 0.0, "duration": 0.25}].')
    problems: List[str] = []
    clean: List[dict] = []
    for i, n in enumerate(notes):
        if not isinstance(n, dict):
            problems.append("Note %d ist kein Objekt: %r" % (i, n))
            continue
        for k in n:
            if k not in NOTE_FIELDS:
                alias = NOTE_ALIASES.get(str(k).lower())
                problems.append("Note %d: unbekanntes Feld '%s'%s" % (i, k, " (gemeint: '%s'?)" % alias if alias else ""))
        problems.extend("Note %d: '%s' fehlt" % (i, k) for k in NOTE_REQUIRED if n.get(k) is None)
        c: Dict[str, Any] = dict(NOTE_DEFAULTS)
        for k in NOTE_FIELDS:
            v = n.get(k)
            if v is None:
                continue
            if k == "mute":
                if v not in (True, False):
                    problems.append("Note %d: mute muss true oder false sein, nicht %r" % (i, v))
                c[k] = bool(v)
                continue
            if isinstance(v, bool):
                problems.append("Note %d: '%s' ist keine Zahl (%r)" % (i, k, v))
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                problems.append("Note %d: '%s' ist keine Zahl (%r)" % (i, k, v))
                continue
            lo, hi = NOTE_RANGES.get(k, (None, None))
            if k == "pitch" and v != int(v):
                problems.append("Note %d: pitch muss ganzzahlig sein, nicht %g" % (i, v))
            elif lo is not None and not lo <= v <= hi:
                problems.append("Note %d: %s=%g liegt ausserhalb von %g bis %g" % (i, k, v, lo, hi))
            elif k == "duration" and v <= 0:
                problems.append("Note %d: duration muss groesser als 0 sein, nicht %g" % (i, v))
            c[k] = int(v) if k == "pitch" else v
        clean.append(c)
    if problems:
        shown = problems[:8]
        raise ValueError("Noten nicht geschrieben, nichts geaendert:\n- " + "\n- ".join(shown)
                         + ("\n- ... %d weitere" % (len(problems) - len(shown)) if len(problems) > len(shown) else "")
                         + "\nErlaubt: %s. Pflicht: %s (Zeiten in Beats ab Clip-Anfang)."
                         % (", ".join(NOTE_FIELDS), ", ".join(NOTE_REQUIRED)))
    return clean


@mcp.tool()
@_tool
def add_notes_to_clip(track: TrackRef, source: str, clip_index: int, notes: List[Dict[str, Any]]) -> str:
    """Fuegt Noten hinzu (vorhandene bleiben). AENDERT DAS SET, ein Undo-Schritt.

    notes: [{"pitch": 36, "start_time": 0.0, "duration": 0.25, "velocity": 100}], Zeiten in
    Beats ab Clip-Anfang (wie get_clip_notes). Pflicht: pitch, start_time, duration.
    Optional: velocity (Standard 100), mute, probability (0..1), velocity_deviation
    (-127..127, Zufallsbereich um velocity), release_velocity (0..127).
    Unbekannte, fehlende oder unpassende Felder: Es wird NICHTS geschrieben, die Antwort
    nennt sie. Nach dem Schreiben wird nachgelesen; jede Note, die Live anders speichert
    als gesendet, steht in der Antwort. Zum Ersetzen erst clear_notes_from_clip."""
    clean = _check_notes(notes)
    ref = _track(track)
    state = _session()

    def render(result: dict) -> str:
        # Eigener Befehl, also ein Durchlauf spaeter: Live uebernimmt manches
        # erst dann.
        try:
            back = (_call("get_clip_notes", {"track": ref, "source": source, "clip_index": clip_index})
                    .get("clip") or {}).get("notes")
        except Exception as e:  # noqa: BLE001 - die Noten sind geschrieben, die Antwort muss trotzdem kommen
            back = None
            log.warning("Nachlesen nach add_notes_to_clip fehlgeschlagen: %s", e)
        return views.notes_added(result, clean, back, state)

    return _write("add_notes_to_clip", {"track": ref, "source": source, "clip_index": clip_index, "notes": clean},
                  render=render)


@mcp.tool()
@_tool
def clear_notes_from_clip(track: TrackRef, source: str, clip_index: int) -> str:
    """Loescht alle Noten eines Clips. AENDERT DAS SET."""
    return _write("clear_notes_from_clip", {"track": _track(track), "source": source, "clip_index": clip_index})


@mcp.tool()
@_tool
def delete_clip(track: TrackRef, clip_index: int) -> str:
    """Loescht den Clip in einem Session-Slot. AENDERT DAS SET.
    Clips im Arrangement: delete_arrangement_clip."""
    return _write("delete_clip", {"track": _track(track), "clip_index": clip_index})


def _deleted_clips_dir() -> str:
    # Umstellbar nur fuer Tests.
    return os.environ.get("TECHNOA_DELETED_CLIPS_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                       "geloeschte_clips")


def _clip_span(c: dict, tl: views.Timeline) -> str:
    return "#%s %s bis %s '%s'%s" % (c.get("index"), tl.pos(c.get("start_time")), tl.pos(c.get("end_time")),
                                     c.get("name") or "", " (deaktiviert)" if c.get("muted") else "")


def _render_clip_delete(tl: views.Timeline):
    def render(result: dict) -> str:
        dc = result.get("deleted") or {}
        saved = _save_json(_deleted_clips_dir(), "%s_%s_%s" % (result.get("track_name"), tl.pos(dc.get("start_time")),
                                                               dc.get("name") or "Clip"), result)
        if dc.get("is_midi"):
            content = "MIDI, %d Noten" % len(dc.get("notes") or [])
        else:
            content = "Audio, Datei %s" % ((dc.get("file_path") or "?").replace("\\", "/").split("/")[-1])
        lines = ["[%s] %s: Arrangement-Clip %s geloescht (%s, %s)." % (
            views.ref_label(result.get("track")), result.get("track_name"), _clip_span(dc, tl),
            tl.dur((dc.get("end_time") or 0) - (dc.get("start_time") or 0)), content),
            "Clips auf der Spur: %s -> %s. Spaetere Clips haben jetzt einen Index weniger." % (
                result.get("clip_count_before"), result.get("clip_count_after")),
            "Inhalt gesichert: %s" % (saved or "NICHT gesichert (Schreibfehler, siehe Log)")]
        lines += _envelope_backup_note(dc)
        return "\n".join(lines)
    return render


@mcp.tool()
@_tool
def delete_arrangement_clip(track: TrackRef, clip_index: int, start: Union[float, str],
                            clip_name: Optional[str] = None) -> str:
    """Loescht einen Clip im Arrangement. AENDERT DAS SET, ein Undo-Schritt.

    clip_index wie in get_arrangement_clips(detail="clips"). start ist Pflicht:
    die Startposition dieses Clips, als Takt.Schlag.Sechzehntel aus derselben Liste
    ("33.1.1") oder als Taktzahl (33). Passt sie nicht zum Clip an clip_index,
    passiert nichts. Schutz gegen nachgerueckte Indizes (nach jedem Loeschen) und
    gleichnamige Clips. clip_name optional als zweite Pruefung.
    Inhalt (Noten bzw. Audiodatei, Marker, Gain) vorher als JSON in
    technoa_mcp/geloeschte_clips/. Session-Clips: delete_clip."""
    _require("delete_arrangement_clip")
    tl = _timeline()
    params: Dict[str, Any] = {"track": _track(track), "clip_index": int(clip_index),
                              "expect_start": tl.parse_pos(start)}
    if clip_name is not None:
        params["expect_name"] = clip_name
    try:
        return _write("delete_arrangement_clip", params, render=_render_clip_delete(tl))
    except LiveError as e:
        # Das Script nennt Beats; gefragt war meist "130.3.1". Beides zeigen.
        raise LiveError(re.sub(r"(Beat|nicht bei) (\d+(?:\.\d+)?)", lambda m: "%s %s (%s)" % (
            m.group(1), m.group(2), tl.pos(float(m.group(2)))), str(e)))


@mcp.tool()
@_tool
def fire_clip(track: TrackRef, clip_index: int) -> str:
    """Startet einen Session-Clip. Startet auch den Transport, und die Spur folgt
    danach der Session statt dem Arrangement (back_to_arrangement)."""
    return _write("fire_clip", {"track": _track(track), "clip_index": clip_index})


@mcp.tool()
@_tool
def stop_clip(track: TrackRef, clip_index: int) -> str:
    """Stoppt einen Session-Slot."""
    return _write("stop_clip", {"track": _track(track), "clip_index": clip_index})


@mcp.tool()
@_tool
def duplicate_to_arrangement(track: TrackRef, clip_index: int, bar: Optional[Union[float, str]] = None,
                             beat: Optional[float] = None, overwrite: bool = False, repeat: int = 1,
                             every_bars: Optional[float] = None) -> str:
    """Kopiert einen Session-Clip ins Arrangement an Takt/Beat. AENDERT DAS SET, ein Undo-Schritt.

    bar: Takt (33) oder Takt.Schlag.Sechzehntel ("33.3.1"); beat: genau in Beats.
    repeat: so viele Kopien hintereinander (Standard 1, hoechstens 256), alle in EINEM
    Undo-Schritt. every_bars: Abstand der Kopien in Takten (Standard: Cliplaenge, also
    lueckenlos; kleiner als der Clip geht nicht). Beispiel: 2-Takt-Clip ab Takt 17 bis
    Takt 49 = bar=17, repeat=16.
    Liegen in einer der Kopien schon Clips, wuerde Live sie ueberschreiben (ganz
    ueberdeckte sind danach weg). Dann passiert NICHTS und die Antwort listet
    sie; mit overwrite=true wird ueberschrieben und der Bereich vorher/nachher
    gezeigt. Die Kopie bekommt in Live den Groove des Sets."""
    ref = _track(track)
    tl = _timeline()
    if bar is not None and beat is not None:
        raise ValueError("Ziel: entweder Takt (bar) oder Beat angeben, nicht beides.")
    t = tl.parse_pos(bar) if bar is not None else beat
    if t is None:
        raise ValueError("bar oder beat angeben.")
    repeat = int(repeat)
    if repeat < 1 or repeat > 256:
        raise ValueError("repeat=%d geht nicht; erlaubt sind 1 bis 256 Kopien. Nichts geaendert." % repeat)
    if repeat > 1 or every_bars is not None:
        # technoa.7 kennt repeat nicht und haette still genau eine Kopie angelegt.
        _require_feature("duplicate_repeat", "mehrere Kopien (repeat, every_bars)")
    info = _call("get_track_info", {"track": ref})
    slot = next((c for c in info.get("session_clips") or [] if isinstance(c, dict) and c.get("index") == clip_index),
                None)
    if slot is None:
        raise ValueError("Im Session-Slot %d von [%s] %s liegt kein Clip." % (clip_index, views.ref_label(ref),
                                                                             info.get("name")))
    length = float(slot.get("length") or 0.0)
    step = float(every_bars) * tl.beats_per_bar if every_bars is not None else length
    if repeat > 1 and step < length - 1e-6:
        raise ValueError("every_bars=%g (%s) ist kuerzer als der Clip (%s): Jede Kopie wuerde die vorige "
                         "ueberschreiben. Nichts geaendert." % (every_bars, tl.dur(step), tl.dur(length)))
    starts = [t + i * step for i in range(repeat)]
    end = starts[-1] + length
    # Geprueft werden genau die Spannen der Kopien: Bei Luecken (every_bars >
    # Cliplaenge) bleibt, was dazwischen liegt, unberuehrt und ist kein Hindernis.
    found = _call("get_arrangement_clips", {"track": ref, "start_time": t, "end_time": end,
                                            "include_digest": False}).get("clips") or []
    before = [c for c in found if any((c.get("end_time") or 0.0) > s + 1e-9
                                      and (c.get("start_time") or 0.0) < s + length - 1e-9 for s in starts)]
    where = "%s bis %s (%s)" % (tl.pos(t), tl.pos(end), tl.dur(end - t))
    if before and not overwrite:
        raise ValueError("Im Zielbereich %s auf [%s] %s liegen schon %d Clip(s): %s. Die Kopie wuerde sie "
                         "ueberschreiben (ganz ueberdeckte verschwinden, teilweise ueberdeckte werden gekuerzt). "
                         "Nichts geaendert. Mit overwrite=true trotzdem." % (
                             where, views.ref_label(ref), info.get("name"), len(before),
                             "; ".join(_clip_span(c, tl) for c in before)))
    params: Dict[str, Any] = {"track": ref, "clip_index": clip_index, "destination_time": t}
    if repeat > 1:
        params.update(repeat=repeat, step=step)
    many = "%dx " % repeat if repeat > 1 else ""
    gap = (", alle %s" % tl.dur(step)) if repeat > 1 else ""
    text = _write("duplicate_session_clip_to_arrangement", params,
                  render=lambda r: "[%s] %s: Session-Slot %d '%s' %sins Arrangement kopiert, %s%s." % (
                      views.ref_label(ref), info.get("name"), clip_index, r.get("clip_name"), many, where, gap))
    lo = min([t] + [c.get("start_time") or t for c in before])
    hi = max([end] + [c.get("end_time") or end for c in before])
    after = _call("get_arrangement_clips", {"track": ref, "start_time": lo, "end_time": hi,
                                            "include_digest": False}).get("clips") or []
    lines = [text]
    if before:
        lines.append("Vorher im Bereich: " + "; ".join(_clip_span(c, tl) for c in before))
        lines.append("Nachher:           " + "; ".join(_clip_span(c, tl) for c in after))
    new = [next((c for c in after if abs((c.get("start_time") or 0.0) - s) < 1e-6), None) for s in starts]
    hit = [c for c in new if c is not None]
    groove = (", Groove " + hit[0]["groove"]) if hit and hit[0].get("groove") else ", ohne Groove"
    if repeat == 1 and hit:
        lines.append("Neuer Clip: %s%s" % (_clip_span(hit[0], tl), groove))
    elif hit:
        spans = [_clip_span(c, tl) for c in hit]
        listed = "; ".join(spans) if len(spans) <= 4 else "%s; %s ... %s" % (spans[0], spans[1], spans[-1])
        lines.append("Neue Clips: %d von %d gefunden: %s%s" % (len(hit), repeat, listed, groove))
    missing = [tl.pos(s) for s, c in zip(starts, new) if c is None]
    if missing:
        lines.append("ACHTUNG: Bei %s ist danach kein Clip zu finden. Arrangement pruefen." % ", ".join(missing))
    return "\n".join(lines)


@mcp.tool()
@_tool
def load_instrument_or_effect(track: TrackRef, uri: str, position: Optional[int] = None,
                              disarm: bool = False) -> str:
    """Laedt ein Geraet aus dem Browser (URI aus get_browser_items_at_path) auf eine Spur.
    AENDERT DAS SET. position: Index danach (0 = ganz vorn); dann wird das neue
    Geraet nach dem Laden dorthin verschoben, also zwei Undo-Schritte. Fuer
    Live-eigene Geraete geht es in einem Schritt mit insert_device.
    Live waehlt die Spur dabei aus und kann sie scharf schalten (Auto-Arm);
    die Antwort meldet das, disarm=true nimmt es gleich zurueck."""
    ref = _track(track)
    if position is not None:
        _require("move_device")
    arm_before = _arm_states()
    before = [d.get("name") for d in _call("get_track_info", {"track": ref}).get("devices") or []]
    text = _write("load_browser_item", {"track": ref, "item_uri": uri},
                  render=lambda r: "[%s] %s: '%s' aus dem Browser geladen (ein Undo-Schritt)." % (
                      views.ref_label(r.get("track")), r.get("track_name"), r.get("item_name")))
    # Live haengt das Geraet nicht immer im selben Durchlauf an; kurz warten.
    after = before
    end = time.monotonic() + 3.0
    while time.monotonic() < end:
        after = [d.get("name") for d in _call("get_track_info", {"track": ref}).get("devices") or []]
        if len(after) > len(before):
            break
        time.sleep(0.15)
    arm = _arm_notes(arm_before, _arm_states(), ref, "dem Laden", disarm)
    tail = "".join("\n" + n for n in arm)
    if len(after) != len(before) + 1:
        return text + ("\nHINWEIS: Nach dem Laden %d statt %d Geraete; das neue ist nicht eindeutig.%s Kette "
                       "jetzt: %s" % (len(after), len(before) + 1, " NICHT verschoben." if position is not None
                                      else "", _chain_text(after))) + tail
    new_index = next((i for i in range(len(after)) if i >= len(before) or before[i] != after[i]), len(after) - 1)
    text += "\n'%s' steht an Position %d. Kette jetzt: %s" % (after[new_index], new_index, _chain_text(after))
    if position is None or new_index == position:
        return text + tail
    try:
        moved = _write("move_device", {"track": ref, "device_index": new_index, "expect_name": after[new_index],
                                       "target_index": int(position)}, render=_render_move)
    except (LiveError, ValueError) as e:
        # Geladen ist es trotzdem; das darf in der Fehlermeldung nicht untergehen.
        return text + ("\nFEHLER beim Verschieben: %s\n'%s' ist geladen und steht an Position %d (ein Undo-Schritt)."
                       % (e, after[new_index], new_index)) + tail
    return text + "\n" + moved + "\nHINWEIS: Zwei Undo-Schritte (Laden, Verschieben)." + tail


# ======================================================================
# Clip-Huellkurven (Paket Automation, technoa.9)
# ======================================================================

# Notenwerte als Raster in Beats (Viertel).
ENVELOPE_RESOLUTIONS = {"1/4": 1.0, "1/8": 0.5, "1/16": 0.25, "1/32": 0.125, "1/64": 0.0625, "1/128": 0.03125}
MIXER_TARGETS = {"volume": "volume", "vol": "volume", "lautstaerke": "volume", "pegel": "volume", "fader": "volume",
                 "pan": "panning", "panning": "panning", "panorama": "panning",
                 "activator": "activator", "spur an": "activator", "track on": "activator"}
# Lesen nach dem Schreiben: Abweichung, ab der ein Wert als falsch gilt (Anteil
# des Wertebereichs). Rohwerte kommen aus Live mit 6 Nachkommastellen zurueck.
ENVELOPE_CHECK_TOLERANCE = 0.001


def _resolution(value: Union[str, float]) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        res = float(value)
    else:
        key = str(value).strip()
        if key not in ENVELOPE_RESOLUTIONS:
            raise ValueError("Raster '%s' unbekannt. Erlaubt: %s oder eine Zahl in Beats."
                             % (value, ", ".join(ENVELOPE_RESOLUTIONS)))
        res = ENVELOPE_RESOLUTIONS[key]
    if res <= 0:
        raise ValueError("Raster muss groesser als 0 sein.")
    return res


def _mixer_target(parameter: Union[int, str]) -> str:
    low = str(parameter).strip().lower().replace("ä", "ae")
    if low in MIXER_TARGETS:
        return MIXER_TARGETS[low]
    m = re.fullmatch(r"send[:\s]*([a-l])", low)
    if m:
        return "send:%d" % (ord(m.group(1)) - ord("a"))
    m = re.fullmatch(r"send[:\s]*(\d+)", low)
    if m:
        return "send:%d" % int(m.group(1))
    raise ValueError("Parameter '%s' ohne device_index: erlaubt sind 'volume', 'pan', 'send A' bis 'send L' und "
                     "'activator' (Mixer der Spur). Fuer einen Geraeteparameter device_index angeben (wie in "
                     "get_track_info) und parameter als Name oder Index (wie in get_device_parameters)." % parameter)


def _envelope_params(ref, parameter, device_index, chain_path) -> dict:
    if parameter is None:
        raise ValueError("parameter fehlt: 'volume', 'pan', 'send A' ... oder mit device_index der Name des "
                         "Geraeteparameters.")
    if device_index is None:
        return {"mixer": _mixer_target(parameter)}
    return {"device_index": int(device_index), "chain_path": chain_path or [],
            "parameter_index": _param_index(ref, int(device_index), parameter, chain_path)}


def _clip_address(ref, source: str, clip_index: Optional[int], at_bar, tl: views.Timeline) -> dict:
    source = (source or "").strip().lower()
    if source not in ("arrangement", "session"):
        raise ValueError("source muss 'arrangement' oder 'session' sein.")
    params: Dict[str, Any] = {"track": ref, "source": source}
    if at_bar is not None:
        if source != "arrangement":
            raise ValueError("at_bar gibt es nur fuer source='arrangement'; bei Session-Clips clip_index = Slot.")
        params["at_time"] = tl.parse_pos(at_bar) + 1e-6
    elif clip_index is not None:
        params["clip_index"] = int(clip_index)
    else:
        raise ValueError("clip_index oder at_bar angeben.")
    return params


def _envelope_window(clip: dict) -> tuple:
    # Wie _envelope_window im Remote Script: Start- oder Loopanfang bis End-
    # oder Loopende, je nachdem, was weiter reicht.
    lo = min(clip.get("start_marker") or 0.0, clip.get("loop_start") or 0.0)
    hi = max(clip.get("end_marker") or 0.0, clip.get("loop_end") or 0.0)
    if hi <= lo:
        hi = lo + (clip.get("length") or 0.0)
    return lo, hi


def _envelope_backup_note(clip: dict) -> List[str]:
    if not clip.get("has_envelopes"):
        return []
    saved = clip.get("envelope_samples")
    if saved:
        return ["Clip-Automation (%s) steht als abgetastete Werte in der Sicherung (envelope_samples, Raster 1/16)."
                % ", ".join(e.get("label") or "?" for e in saved)]
    why = ("Arrangement-Clip, Lives API gibt dessen Huellkurven nicht heraus" if clip.get("start_time") is not None
           else "MIDI-Controller oder Script aelter als technoa.9")
    return ["HINWEIS: Der Clip hatte Clip-Automation, die nicht lesbar war (%s). Sie steht NICHT in der Sicherung; "
            "zurueck nur ueber undo." % why]


# Am Set 27.10.2026 (Live 12.4.7): Lives API gibt Huellkurven von
# Arrangement-Clips nicht heraus (automation_envelope: "Returns None for
# Arrangement clips"), und Spur-Automation im Arrangement erreicht sie gar nicht.
# Der Weg ins Arrangement: Kurve in einen Session-Clip schreiben und den Clip mit
# duplicate_to_arrangement einsetzen; dort wirkt sie im Clip absolut, davor und
# danach gilt wieder der Fader.
ARRANGEMENT_ENVELOPE_TEXT = (
    "Lives API (12.4.7) gibt Huellkurven von Arrangement-Clips nicht heraus und laesst dort keine anlegen; "
    "Spur-Automation im Arrangement ist ueber die API gar nicht erreichbar. Weg ins Arrangement: die Kurve "
    "mit set_clip_envelope in einen Session-Clip schreiben und den Clip mit duplicate_to_arrangement einsetzen. "
    "Im Clip gilt die Kurve dann absolut, davor und danach wieder der Fader; lesen oder aendern kann die API sie "
    "dort nicht mehr (nur loeschen mit dem Clip). Gespeicherte Spur-Automation liest read_saved_set.")


def _refuse_arrangement(source: str, what: str):
    if (source or "").strip().lower() == "arrangement":
        raise ValueError("%s geht an Arrangement-Clips nicht. %s" % (what, ARRANGEMENT_ENVELOPE_TEXT))


@mcp.tool()
@_tool
def get_clip_envelope(track: TrackRef, source: str, clip_index: Optional[int] = None,
                      at_bar: Optional[Union[float, str]] = None, parameter: Optional[Union[int, str]] = None,
                      device_index: Optional[int] = None, chain_path: Optional[List[List[int]]] = None,
                      resolution: Union[str, float] = "1/16", probe: bool = False) -> str:
    """Clip-Automation (Huellkurve) eines Clips lesen: Punkte mit Takt und Anzeigewert
    ("-12.0 dB"), dazwischen gehalten, Rampe oder Sprung.

    source: "arrangement" oder "session". Clip: clip_index (wie get_arrangement_clips
    bzw. Session-Slot) oder at_bar (Arrangement: der Clip, der in diesem Takt liegt).
    parameter ohne device_index: "volume", "pan", "send A" bis "send L", "activator" (Mixer
    der Spur). Mit device_index (wie get_track_info): Name oder Index des Geraeteparameters,
    chain_path fuer Geraete in Racks. Ohne parameter: alle Huellkurven des Clips.
    resolution: Leseraster, "1/16" (Standard) bis "1/128". Lives API nennt keine
    Knickpunkte, deshalb wird abgetastet. probe=true haengt Lives Beschreibung der
    Huellkurven-Methoden an. Nur Session-Clips: Arrangement-Clips gibt Live nicht
    heraus; dort meldet die Antwort nur, ob der Clip Clip-Automation hat."""
    _require("get_clip_envelope")
    ref = _track(track)
    state = _session()
    tl = _timeline(state)
    params = _clip_address(ref, source, clip_index, at_bar, tl)
    if parameter is not None or device_index is not None:
        params.update(_envelope_params(ref, parameter, device_index, chain_path))
    params["grid"] = _resolution(resolution)
    if probe:
        params["probe"] = True
    result = _call("get_clip_envelope", params)
    text = views.envelope_view(result, state)
    if params["source"] == "arrangement":
        has = (result.get("clip") or {}).get("has_envelopes")
        text = "\n".join([text.splitlines()[0], "Clip-Automation laut Live: %s. %s" % (
            "ja" if has else "nein", ARRANGEMENT_ENVELOPE_TEXT)])
    return text


def _envelope_points(points: List[dict], clip: dict, source: str, tl: views.Timeline) -> List[list]:
    """Punkte -> [Clip-Zeit, Wert, Angabe] mit Pruefung, alles vor dem Schreiben."""
    if not points:
        raise ValueError("points fehlt: Liste von {\"bar\": Position, \"value\": Wert}.")
    lo, hi = _envelope_window(clip)
    marker = clip.get("start_marker") or 0.0
    first_end = clip.get("loop_end") if clip.get("looping") else clip.get("end_marker")
    out, problems = [], []
    for i, p in enumerate(points):
        if not isinstance(p, dict) or "value" not in p or not ({"bar", "beat"} & set(p)):
            problems.append("Punkt %d: {\"bar\": Position, \"value\": Wert} oder {\"beat\": ..., \"value\": ...} "
                            "erwartet, nicht %r" % (i, p))
            continue
        unknown = set(p) - {"bar", "beat", "value"}
        if unknown:
            problems.append("Punkt %d: unbekanntes Feld %s" % (i, ", ".join("'%s'" % u for u in sorted(unknown))))
            continue
        try:
            if "beat" in p:
                t = float(p["beat"])
            elif source == "arrangement":
                song_beat = tl.parse_pos(p["bar"])
                c0, c1 = clip.get("start_time") or 0.0, clip.get("end_time")
                if song_beat < c0 - 1e-6 or (c1 is not None and song_beat > c1 + 1e-6):
                    problems.append("Punkt %d: Takt %s liegt ausserhalb des Clips (%s bis %s)"
                                    % (i, p["bar"], tl.pos(c0), tl.pos(c1)))
                    continue
                t = marker + (song_beat - c0)
                if first_end is not None and t > first_end + 1e-6:
                    problems.append("Punkt %d: Takt %s liegt hinter dem ersten Durchlauf des Clip-Loops (Clip endet "
                                    "beim Loopen auf %s). Clip-Huellkurven wiederholen sich mit dem Loop; "
                                    "Position im Clip als 'beat' angeben." % (i, p["bar"], tl.pos(
                                        (clip.get("start_time") or 0.0) + first_end - marker)))
                    continue
            else:
                t = tl.parse_pos(p["bar"])
        except (TypeError, ValueError) as e:
            problems.append("Punkt %d: %s" % (i, e))
            continue
        if t < lo - 1e-6 or t > hi + 1e-6:
            where = ("Songtakt %s bis %s" % (tl.pos((clip.get("start_time") or 0.0) + lo - marker),
                                            tl.pos((clip.get("start_time") or 0.0) + hi - marker))
                     if source == "arrangement" else "Clip-Takt %s bis %s" % (tl.pos(lo), tl.pos(hi)))
            problems.append("Punkt %d: Position ausserhalb des Clips (%s, Clip-Zeit %g bis %g Beats)"
                            % (i, where, lo, hi))
            continue
        out.append([t, p["value"], p])
    if problems:
        raise ValueError("Nichts geschrieben:\n" + "\n".join(problems))
    out.sort(key=lambda x: x[0])
    return out


def _envelope_steps(pts: List[tuple], shape: str, res: float, lo: float, hi: float, hold_edges: bool) -> List[tuple]:
    """Punkte (Clip-Zeit, Rohwert) -> Stufen (time, duration, value) fuer insert_step.

    Lives API (insert_step) kennt nur Stufen. Eine Rampe wird deshalb in gleich
    lange Stufen von hoechstens res Beats zerlegt, die erste auf dem Start-, die
    letzte auf dem Zielwert. Zwei Punkte an derselben Stelle ergeben einen Sprung."""
    steps: List[tuple] = []
    if hold_edges and pts[0][0] > lo + 1e-9:
        steps.append((lo, pts[0][0] - lo, pts[0][1]))
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        span = t1 - t0
        if span <= 1e-9:
            continue
        if shape == "steps" or abs(v1 - v0) < 1e-12:
            steps.append((t0, span, v0))
            continue
        n = max(2, int(math.ceil(span / res - 1e-9)))
        # Erste Stufe auf dem Start-, letzte auf dem Zielwert: Live zaehlt den
        # Grenzpunkt zur Stufe davor (am Set 27.10.2026). Endete die Rampe eine
        # Stufe unter dem Ziel, laese man am naechsten Punkt -0.26 dB statt 0 dB.
        for k in range(n):
            steps.append((t0 + span * k / n, span / n, v0 + (v1 - v0) * k / float(n - 1)))
    last_t, last_v = pts[-1]
    if hold_edges and last_t < hi - 1e-9:
        steps.append((last_t, hi - last_t, last_v))
    return steps


def _expected_at(steps: List[tuple], t: float) -> List[float]:
    """Was nach dem Schreiben bei t stehen sollte. An Stufengrenzen gelten beide
    Seiten, weil offen ist, welcher Stufe Live den Grenzpunkt zuschlaegt. Grenzt
    t an einen Teil der Kurve, den der Befehl nicht geschrieben hat, ist nichts
    zu erwarten (leere Liste)."""
    out = []
    for probe in (t - 1e-6, t, t + 1e-6):
        hit = None
        for s, d, v in steps:
            if s - 1e-9 <= probe < s + d - 1e-9:
                hit = v
        if hit is None:
            return []
        out.append(hit)
    return out


def _start_point_note(steps: List[tuple], after: dict, tol: float, tl: views.Timeline) -> str:
    """Live legt eine neue Kurve mit dem Reglerwert als Punkt am Clipanfang an,
    und insert_step ueberschreibt genau diesen Punkt nicht (Grenzpunkt gehoert zur
    Stufe davor). Am Set 27.10.2026 als Knacken beim Loop-Neustart gehoert; weg,
    nachdem die Kurve bei Fader auf dem Startwert neu angelegt war."""
    t0 = after.get("from") or 0.0
    values = after.get("values") or []
    wanted = [v for s, d, v in steps if abs(s - t0) < 1e-9]
    if not values or not wanted or abs(values[0] - wanted[-1]) <= tol:
        return ""
    shown = (after.get("displays") or [None])[0] or "%g" % values[0]
    return ("\nACHTUNG: Am Clipanfang (Clip %s) steht noch %s statt des Startwerts. Live legt eine neue "
            "Kurve mit dem Reglerwert an, und dieser Punkt laesst sich mit Stufen nicht ueberschreiben; beim "
            "Clipstart und jedem Loop-Neustart blitzt der alte Wert kurz durch (am Set als Knacken gehoert). "
            "Abhilfe bis technoa.10: Clip stoppen, den Regler (set_mixer bzw. set_device_parameter) auf den "
            "Startwert setzen, die Kurve mit replace=true neu schreiben, Regler zurueck. Am Set 27.10.2026 "
            "geprueft: danach kein Knacken." % (tl.pos(t0), shown))


def _envelope_check(steps: List[tuple], after: Optional[dict], param: dict, tl: views.Timeline) -> str:
    if not after:
        return "ACHTUNG: Nach dem Schreiben liefert Live keine Huellkurve fuer diesen Parameter."
    span = abs((param.get("max") or 1.0) - (param.get("min") or 0.0)) or 1.0
    tol = span * ENVELOPE_CHECK_TOLERANCE
    t0, grid = after.get("from") or 0.0, after.get("grid") or 0.25
    compared, wrong = 0, []
    for i, v in enumerate(after.get("values") or []):
        t = t0 + i * grid
        expected = _expected_at(steps, t)
        if not expected:
            continue
        compared += 1
        if min(abs(v - e) for e in expected) > tol:
            wrong.append((t, v, expected[0], (after.get("displays") or [None] * (i + 1))[i]))
    start = _start_point_note(steps, after, tol, tl)
    if not compared:
        return "Nachgelesen: kein Rasterpunkt im geschriebenen Bereich, nicht verglichen." + start
    if not wrong:
        return ("Nachgelesen: stimmt (%d Werte im Raster %s mit den geschriebenen Stufen verglichen)."
                % (compared, views.grid_name(grid))) + start
    shown = "; ".join("Clip %s: Live %s (roh %g), erwartet roh %g" % (tl.pos(t), d, v, e) for t, v, e, d in wrong[:5])
    return ("ACHTUNG: %d von %d Werten weichen ab (z. B. %s). Live legt die Stufen anders an als gedacht; "
            "Kurve oben pruefen, notfalls undo." % (len(wrong), compared, shown))


def _render_envelope_write(tl: views.Timeline, state: dict, steps: List[tuple], shape: str, res: float):
    def render(result: dict) -> str:
        clip = result.get("clip") or {}
        param = result.get("param") or {}
        label = (result.get("target") or {}).get("label")
        lines = [views._clip_head(result, tl)]
        how = "neu angelegt" if result.get("created") else ("ersetzt" if result.get("replaced") else "ergaenzt")
        lines.append("Huellkurve %s %s: %s von %s Stufen geschrieben%s." % (
            label, how, result.get("steps_written"), result.get("steps_sent"),
            " (Rampen als Stufen im Raster %s)" % views.grid_name(res) if shape == "linear" else ""))
        before = result.get("before")
        if before:
            lines.append("Vorher:")
            lines += views.envelope_rows(before, clip, tl, param.get("min"), param.get("max"), max_rows=12)
        else:
            lines.append("Vorher: keine Huellkurve fuer %s." % label)
        if result.get("after"):
            lines.append("Jetzt:")
            lines += views.envelope_rows(result["after"], clip, tl, param.get("min"), param.get("max"))
        lines.append(_envelope_check(steps, result.get("after"), param, tl))
        for w in result.get("warnings") or []:
            lines.append("ACHTUNG: " + w)
        lines.append("Reglerwert ausserhalb der Kurve: %s." % param.get("display"))
        return "\n".join(lines)
    return render


@mcp.tool()
@_tool
def set_clip_envelope(track: TrackRef, source: str, points: List[dict], clip_index: Optional[int] = None,
                      at_bar: Optional[Union[float, str]] = None, parameter: Union[int, str] = "volume",
                      device_index: Optional[int] = None, chain_path: Optional[List[List[int]]] = None,
                      shape: str = "linear", replace: bool = False,
                      resolution: Union[str, float] = "1/64") -> str:
    """Schreibt Clip-Automation (Huellkurve). AENDERT DAS SET, ein Undo-Schritt.

    points: Liste von {"bar": Position, "value": Wert}. Position: bei Arrangement-Clips
    Songtakt (33 oder "33.3.1"), bei Session-Clips Takt im Clip (1 = Clipanfang).
    Alternativ {"beat": Beats ab Clipanfang (Clip-Zeit), "value": ...}.
    value: Anzeigewert als Text ("-12 dB", "-inf dB", "800 Hz", "50 %", "On") oder Rohwert
    als Zahl. Parameter und Clip wie bei get_clip_envelope.
    shape: "linear" = Rampen zwischen den Punkten, "steps" = jeder Wert gilt bis zum
    naechsten Punkt. Zwei Punkte an derselben Stelle = Sprung.
    Ist die Kurve neu oder replace=true, gilt vor dem ersten Punkt dessen Wert und nach
    dem letzten der letzte, bis Clipende. In eine bestehende Kurve (replace=false) wird
    nur zwischen erstem und letztem Punkt geschrieben.
    Rampen entstehen als feine Stufen im Raster resolution (Standard "1/64"); Lives API
    kennt nur Stufen. Danach wird nachgelesen und verglichen.
    Nur Session-Clips. Ins Arrangement danach mit duplicate_to_arrangement: dort
    gilt die Kurve im Clip absolut, davor und danach der Fader."""
    _require("set_clip_envelope")
    _refuse_arrangement(source, "Clip-Automation schreiben")
    if shape not in ("linear", "steps"):
        raise ValueError("shape muss 'linear' oder 'steps' sein.")
    ref = _track(track)
    state = _session()
    tl = _timeline(state)
    address = _clip_address(ref, source, clip_index, at_bar, tl)
    target = _envelope_params(ref, parameter, device_index, chain_path)
    res = _resolution(resolution)
    current = _call("get_clip_envelope", dict(address, **target))
    clip = current.get("clip") or {}
    # Ab hier ueber den Index: at_bar koennte nach einer Aenderung einen
    # anderen Clip treffen als den eben gelesenen.
    address = {"track": ref, "source": address["source"], "clip_index": clip.get("index")}
    pts = _envelope_points(points, clip, address["source"], tl)
    converted = _call("convert_parameter_values", dict(target, track=ref, values=[p[1] for p in pts]))["values"]
    raw = [(p[0], c["value"]) for p, c in zip(pts, converted)]
    hold_edges = replace or not current.get("exists")
    if len(raw) < 2 and not hold_edges:
        raise ValueError("Ein einzelner Punkt in einer bestehenden Kurve beschreibt keinen Bereich. Zwei Punkte "
                         "angeben (Anfang und Ende) oder replace=true fuer eine gleichbleibende Kurve.")
    lo, hi = _envelope_window(clip)
    steps = _envelope_steps(raw, shape, res, lo, hi, hold_edges)
    if not steps:
        raise ValueError("Aus den Punkten ergibt sich keine Stufe (alle an derselben Stelle?). Nichts geschrieben.")
    params = dict(address, **target)
    params.update({"steps": [{"time": s, "duration": d, "value": v} for s, d, v in steps], "replace": bool(replace),
                   "grid": ENVELOPE_RESOLUTIONS["1/16"]})
    return _write("set_clip_envelope", params, render=_render_envelope_write(tl, state, steps, shape, res))


def _render_envelope_clear(tl: views.Timeline):
    def render(result: dict) -> str:
        clip = result.get("clip") or {}
        lines = [views._clip_head(result, tl)]
        cleared = result.get("cleared") or []
        saved = _save_json(_deleted_clips_dir(), "%s_%s_huellkurven" % (result.get("track_name"), clip.get("name")),
                           result)
        for e in cleared:
            p = e.get("param") or {}
            lines.append("Geloescht: Huellkurve %s. Sie war:" % e.get("label"))
            lines += views.envelope_rows(e.get("samples") or {}, clip, tl, p.get("min"), p.get("max"), max_rows=12)
        if not cleared:
            lines.append("Geloescht: alle Huellkurven; lesbar war keine (MIDI-Controller?).")
        if result.get("exists_after"):
            lines.append("ACHTUNG: Live meldet danach immer noch eine Huellkurve fuer diesen Parameter.")
        lines.append("Clip-Automation jetzt: %s." % ("noch vorhanden (andere Parameter)" if clip.get("has_envelopes")
                                                     else "keine"))
        lines.append("Werte vorher gesichert: %s" % (saved or "NICHT gesichert (Schreibfehler, siehe Log)"))
        return "\n".join(lines)
    return render


@mcp.tool()
@_tool
def clear_clip_envelope(track: TrackRef, source: str, clip_index: Optional[int] = None,
                        at_bar: Optional[Union[float, str]] = None, parameter: Optional[Union[int, str]] = None,
                        device_index: Optional[int] = None, chain_path: Optional[List[List[int]]] = None,
                        all_envelopes: bool = False) -> str:
    """Loescht Clip-Automation. AENDERT DAS SET, ein Undo-Schritt.

    Clip und Parameter wie bei get_clip_envelope; all_envelopes=true loescht alle
    Huellkurven des Clips. Die Werte vorher stehen in der Antwort und als JSON in
    technoa_mcp/geloeschte_clips/. Nur Session-Clips; Clip-Automation eines
    Arrangement-Clips verschwindet nur mit dem Clip."""
    _require("clear_clip_envelope")
    _refuse_arrangement(source, "Clip-Automation loeschen")
    ref = _track(track)
    tl = _timeline()
    params = _clip_address(ref, source, clip_index, at_bar, tl)
    if all_envelopes:
        if parameter is not None or device_index is not None:
            raise ValueError("Entweder all_envelopes=true oder ein Parameter, nicht beides.")
        params["all"] = True
    else:
        params.update(_envelope_params(ref, parameter, device_index, chain_path))
    return _write("clear_clip_envelope", params, render=_render_envelope_clear(tl))


@mcp.tool()
@_tool
def set_song(groove_amount_percent: Optional[float] = None, metronome: Optional[bool] = None) -> str:
    """Globale Groove-Menge (Lives "Global Amount" im Groove-Pool, 0 bis 130 %) und
    Metronom. AENDERT DAS SET. Die globale Menge wirkt auf alle Clips mit Groove und
    taugt so als A/B-Schalter fuer Swing."""
    _require("set_song_settings")
    params: Dict[str, Any] = {}
    if groove_amount_percent is not None:
        if not 0.0 <= float(groove_amount_percent) <= 130.0:
            raise ValueError("groove_amount_percent %g liegt ausserhalb von 0 bis 130." % groove_amount_percent)
        params["groove_amount"] = float(groove_amount_percent) / 100.0
    if metronome is not None:
        params["metronome"] = bool(metronome)
    if not params:
        raise ValueError("Nichts zu setzen: groove_amount_percent oder metronome angeben.")

    def render(result: dict) -> str:
        parts = []
        for c in result.get("changes") or []:
            if c.get("target") == "groove_amount":
                parts.append("Groove-Menge global %s -> %s" % (
                    "%g %%" % round(c["before"] * 100, 2) if c.get("before") is not None else "?",
                    "%g %%" % round(c["after"] * 100, 2) if c.get("after") is not None else "?"))
            else:
                parts.append("Metronom %s -> %s" % (views._onoff_text(c.get("before")),
                                                    views._onoff_text(c.get("after"))))
            if c.get("error"):
                parts[-1] += " FEHLER: %s" % c["error"]
        return "; ".join(parts)
    return _write("set_song_settings", params, render=render)


def _render_clips_delete(tl: views.Timeline, reaching: List[dict]):
    def render(result: dict) -> str:
        deleted = result.get("deleted") or []
        saved = [_save_json(_deleted_clips_dir(), "%s_%s_%s" % (result.get("track_name"), tl.pos(c.get("start_time")),
                                                                c.get("name") or "Clip"),
                            dict(result, deleted=c, deleted_together=len(deleted))) for c in deleted]
        lines = ["[%s] %s: %d Arrangement-Clips geloescht, die in %s bis %s beginnen (ein Undo-Schritt)." % (
            views.ref_label(result.get("track")), result.get("track_name"), len(deleted),
            tl.pos(result.get("from_time")), tl.pos(result.get("to_time")))]
        for c in deleted[:12]:
            content = "%d Noten" % len(c.get("notes") or []) if c.get("is_midi") else "Audio"
            lines.append("  " + _clip_span(c, tl) + ", " + content)
        if len(deleted) > 12:
            lines.append("  ... und %d weitere bis %s" % (len(deleted) - 12, tl.pos(deleted[-1].get("start_time"))))
        lines.append("Clips auf der Spur: %s -> %s. Spaetere Clips haben jetzt kleinere Indizes." % (
            result.get("clip_count_before"), result.get("clip_count_after")))
        ok = [s for s in saved if s]
        lines.append("Gesichert: %d von %d Clips einzeln in %s" % (len(ok), len(deleted),
                                                                  os.path.dirname(ok[0]) if ok else "-"))
        for c in deleted:
            lines += _envelope_backup_note(c)
        if reaching:
            lines.append("Nicht geloescht (beginnen vorher, reichen hinein): " + "; ".join(_clip_span(c, tl)
                                                                                      for c in reaching))
        for w in result.get("warnings") or []:
            lines.append("ACHTUNG: " + w)
        return "\n".join(lines)
    return render


@mcp.tool()
@_tool
def delete_arrangement_clips(track: TrackRef, from_bar: Union[float, str], to_bar: Union[float, str],
                             expect_count: Optional[int] = None) -> str:
    """Loescht alle Arrangement-Clips einer Spur, die im Bereich BEGINNEN (to_bar zaehlt
    nicht mehr mit, wie bei set_clip_groove). AENDERT DAS SET, ein Undo-Schritt.

    Positionen als Takt (57) oder "57.3.1". expect_count: so viele Clips erwartest du
    (z. B. aus get_arrangement_clips); passt die Zahl nicht, passiert nichts. Jeder Clip
    wird einzeln als JSON in technoa_mcp/geloeschte_clips/ gesichert, mit Noten und
    Clip-Automation. Clips, die vorher beginnen und hineinreichen, bleiben stehen."""
    _require("delete_arrangement_clips")
    ref = _track(track)
    tl = _timeline()
    start, end = tl.parse_pos(from_bar), tl.parse_pos(to_bar)
    if end <= start:
        raise ValueError("to_bar muss hinter from_bar liegen.")
    seen = _call("get_arrangement_clips", {"track": ref, "start_time": start, "end_time": end,
                                           "include_digest": False})["clips"]
    starting = [c for c in seen if start - 1e-6 <= (c.get("start_time") or 0.0) < end - 1e-6]
    reaching = [c for c in seen if (c.get("start_time") or 0.0) < start - 1e-6]
    if expect_count is not None and int(expect_count) != len(starting):
        raise ValueError("Im Bereich beginnen %d Clips, erwartet waren %d. Nichts geloescht. Liste: %s" % (
            len(starting), expect_count, "; ".join(_clip_span(c, tl) for c in starting[:12]) or "-"))
    if not starting:
        raise ValueError("Im Bereich %s bis %s beginnt kein Clip. Nichts geloescht." % (tl.pos(start), tl.pos(end)))
    params = {"track": ref, "from_time": start, "to_time": end, "expect_count": len(starting)}
    return _write("delete_arrangement_clips", params, render=_render_clips_delete(tl, reaching))


def main():
    mcp.run()


if __name__ == "__main__":
    main()
