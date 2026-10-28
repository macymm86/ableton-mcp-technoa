"""Liest ein gespeichertes Ableton-Set (.als) nur lesend aus.

Warum es das braucht: Lives Python-API verraet ueber Automation nur, OB ein
Parameter automatisiert ist, nicht WIE. Die Verlaeufe im Arrangement stehen
aber im gespeicherten Set (gzip-komprimiertes XML). Der Preis: Es zeigt den
Stand der letzten Speicherung, nicht den Live-Zustand. Deshalb nennt jede
Ausgabe Pfad und Speicherzeit.

Nur Standardbibliothek. Die Datei wird nie geschrieben.
"""
from __future__ import annotations

import datetime as _dt
import glob
import gzip
import math
import os
import re
import xml.etree.ElementTree as ET

# Lives Marke fuer den Startwert einer Automation, der vor Takt 1 gilt.
INITIAL_TIME = -63072000.0

# XML-Namen von Live-Geraeten, die anders heissen als in der Oberflaeche.
# Unbekannte Geraete erscheinen mit ihrem XML-Namen; das ist ehrlicher als
# geraten.
DEVICE_DISPLAY_NAMES = {
    "StereoGain": "Utility",
    "Eq8": "EQ Eight",
    "Compressor2": "Compressor",
    "GlueCompressor": "Glue Compressor",
    "SpectrumAnalyzer": "Spectrum",
    "AutoFilter": "Auto Filter",
    "AutoPan": "Auto Pan",
    "BeatRepeat": "Beat Repeat",
    "DrumBuss": "Drum Buss",
    "FilterDelay": "Filter Delay",
    "MultibandDynamics": "Multiband Dynamics",
    "Redux2": "Redux",
    "Chorus2": "Chorus-Ensemble",
    "PhaserNew": "Phaser-Flanger",
    "Vinyl": "Vinyl Distortion",
    "InstrumentGroupDevice": "Instrument Rack",
    "AudioEffectGroupDevice": "Audio Effect Rack",
    "MidiEffectGroupDevice": "MIDI Effect Rack",
    "DrumGroupDevice": "Drum Rack",
    "OriginalSimpler": "Simpler",
    "MultiSampler": "Sampler",
    "UltraAnalog": "Analog",
    "InstrumentVector": "Wavetable",
    "LoungeLizard": "Electric",
}

PARAM_DISPLAY_NAMES = {
    "Speaker": "Spuraktivator",
    "Volume": "Lautstaerke",
    "Pan": "Panorama",
    "On": "Geraet an/aus",
    "CrossFadeState": "Crossfader-Zuweisung",
    "Tempo": "Tempo",
    "TimeSignature": "Taktart",
}

TRACK_KINDS = {
    "MidiTrack": "midi",
    "AudioTrack": "audio",
    "GroupTrack": "group",
    "ReturnTrack": "return",
    "MainTrack": "master",
    "MasterTrack": "master",
}


# ----------------------------------------------------------------------
# Laden und Pfad finden
# ----------------------------------------------------------------------

def is_live_template(path: str) -> bool:
    """Vorlagen, die Live selbst laedt und ins Log schreibt.

    Beim Anlegen einer Spur laedt Live "Core Library\\Defaults\\Creating
    Tracks\\MIDI Track\\Default MIDI Track.als"; am 11.10.2026 hielt
    read_saved_set das fuer das Set. Live mischt dabei / und \\.
    """
    p = path.replace("\\", "/").lower()
    return "/core library/defaults/" in p or "/resources/builtin/" in p or p.endswith("/defaultliveset.als")


def find_current_set_from_log() -> tuple[str | None, str]:
    """Sucht in Lives Log.txt das zuletzt geladene Set. Nur der Rueckfall,
    wenn Live selbst (song.file_path) nicht erreichbar ist.

    Live protokolliert "Loading document", aber kein "Speichern unter".
    Nach einem Speichern unter neuem Namen zeigt der Log also noch den alten
    Pfad; dann muss der Pfad ausdruecklich mitgegeben werden.
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None, "APPDATA ist nicht gesetzt; Pfad zum Set bitte angeben."
    logs = glob.glob(os.path.join(appdata, "Ableton", "Live *", "Preferences", "Log.txt"))
    if not logs:
        return None, "Kein Live-Log gefunden; Pfad zum Set bitte angeben."
    log = max(logs, key=os.path.getmtime)
    pattern = re.compile(r'Loading document "([^"]+\.als)"')
    last = None
    with open(log, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = pattern.search(line)
            if m and not is_live_template(m.group(1)):
                last = m.group(1)
    if not last:
        return None, "Im Log %s steht kein geladenes Set." % log
    return last, ("zuletzt geladen laut %s (Rueckfall; nach 'Speichern unter' veraltet, dann path angeben)"
                  % log)


def load(path: str) -> ET.Element:
    with gzip.open(path, "rb") as fh:
        return ET.fromstring(fh.read())


# ----------------------------------------------------------------------
# Hilfen
# ----------------------------------------------------------------------

def _val(el, path, default=None):
    if el is None:
        return default
    found = el.find(path)
    if found is None:
        return default
    return found.get("Value", default)


def _float(text, default=None):
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def gain_to_db(value: float | None) -> str:
    """Lineare Verstaerkung (Mixer-Lautstaerke, Sends) in dB-Text.

    Live speichert Mixerpegel linear: 1 = 0 dB. Unter -70 dB zeigt Live -inf.
    """
    if value is None:
        return "?"
    if value <= 0.000316228:
        return "-inf dB"
    return "%.1f dB" % (20.0 * math.log10(value))


def pan_text(value: float | None) -> str:
    if value is None:
        return "?"
    amount = round(abs(value) * 50)
    if amount == 0:
        return "C"
    return "%d%s" % (amount, "L" if value < 0 else "R")


def decode_time_signature(enum_value: int) -> tuple[int, int]:
    # Live kodiert die Taktart als Zaehler-1 + 99 * log2(Nenner); 201 = 4/4.
    return enum_value % 99 + 1, 2 ** (enum_value // 99)


def format_position(beat: float, beats_per_bar: float = 4.0) -> str:
    """Beats (Viertel ab Songanfang) als Takt.Schlag.Sechzehntel, 1-basiert."""
    if beat <= INITIAL_TIME / 2:
        return "Start"
    bar = int(math.floor(beat / beats_per_bar + 1e-9))
    rest = beat - bar * beats_per_bar
    quarter = int(math.floor(rest + 1e-9))
    frac = rest - quarter
    sixteenth = int(math.floor(frac * 4 + 1e-9))
    leftover = frac * 4 - sixteenth
    text = "%d.%d.%d" % (bar + 1, quarter + 1, sixteenth + 1)
    if leftover > 1e-3:
        text += "+%.2f" % leftover
    return text


def _device_name(dev: ET.Element) -> str:
    user = _val(dev, "UserName", "")
    if user:
        return user
    if dev.tag == "PluginDevice":
        for info in dev.find("PluginDesc") or []:
            name = _val(info, "Name") or _val(info, "PlugName")
            if name:
                return name
    if dev.tag.startswith("MxDevice"):
        rel = _val(dev, ".//PatchSlot//RelativePath") or _val(dev, ".//PatchSlot//Path")
        if rel:
            return os.path.splitext(os.path.basename(rel.replace("\\", "/")))[0]
    return DEVICE_DISPLAY_NAMES.get(dev.tag, dev.tag)


# ----------------------------------------------------------------------
# Auswertung
# ----------------------------------------------------------------------

class SavedSet:
    """Strukturierte Sicht auf ein gespeichertes Set."""

    def __init__(self, path: str):
        self.path = path
        self.saved_at = _dt.datetime.fromtimestamp(os.path.getmtime(path))
        self.root = load(path)
        self.live = self.root.find("LiveSet")
        self.creator = self.root.get("Creator", "?")
        self._parent = {c: p for p in self.root.iter() for c in p}
        self.tracks = list(self.live.find("Tracks"))
        main = self.live.find("MainTrack")
        if main is None:
            main = self.live.find("MasterTrack")
        self.main = main
        self.return_names = [self.track_name(t) for t in self.tracks if t.tag == "ReturnTrack"]
        self.tempo = _float(_val(self.main, "DeviceChain/Mixer/Tempo/Manual"))
        sig = _val(self.main, "DeviceChain/Mixer/TimeSignature/Manual")
        self.signature = decode_time_signature(int(sig)) if sig is not None else (4, 4)
        self.beats_per_bar = self.signature[0] * 4.0 / self.signature[1]
        self._target_labels = {}
        for tr in self.tracks + [self.main]:
            for at in tr.iter("AutomationTarget"):
                self._target_labels[at.get("Id")] = self._label_for_target(at, tr)

    # -- Spuren ---------------------------------------------------------

    def track_name(self, tr: ET.Element) -> str:
        return _val(tr, "Name/EffectiveName") or _val(tr, "Name/UserName") or tr.tag

    def track_entries(self):
        """Spuren in Set-Reihenfolge mit Index wie in Live (Returns als A, B, ...)."""
        out = []
        id_to_index = {}
        normal = 0
        ret = 0
        for tr in self.tracks:
            kind = TRACK_KINDS.get(tr.tag, tr.tag)
            if kind == "return":
                ref = chr(ord("A") + ret)
                ret += 1
            else:
                ref = normal
                id_to_index[tr.get("Id")] = normal
                normal += 1
            out.append({"el": tr, "ref": ref, "kind": kind, "name": self.track_name(tr),
                        "group_id": _val(tr, "TrackGroupId", "-1")})
        for e in out:
            gid = e["group_id"]
            e["group"] = id_to_index.get(gid) if gid not in (None, "-1") else None
        out.append({"el": self.main, "ref": "Master", "kind": "master", "name": "Master", "group": None})
        return out

    def _label_for_target(self, at: ET.Element, track: ET.Element) -> dict:
        """Beschreibt, wozu ein AutomationTarget gehoert: Mixer oder Geraet, Parameter."""
        chain = []
        el = self._parent.get(at)
        param_el = el
        device_path = []
        send_index = None
        in_mixer = False
        param_name = None
        while el is not None and el is not track:
            p = self._parent.get(el)
            if el.tag in ("PluginFloatParameter", "PluginEnumParameter", "PluginBoolParameter") and param_name is None:
                param_name = _val(el, "ParameterName")
            if el.tag.startswith("MxD") and el.tag.endswith("Parameter") and param_name is None:
                param_name = _val(el, "Name")
            if el.tag == "TrackSendHolder":
                send_index = int(el.get("Id", "0"))
            if el.tag == "Mixer":
                in_mixer = True
            if p is not None and p.tag == "Devices":
                device_path.append(_device_name(el))
            chain.append(el.tag)
            el = p
        if param_name is None:
            param_name = param_el.tag if param_el is not None else "?"
            if param_name == "Manual" and param_el is not None:
                param_name = self._parent.get(param_el).tag
        device_path.reverse()
        if send_index is not None:
            letter = chr(ord("A") + send_index)
            name = self.return_names[send_index] if send_index < len(self.return_names) else ""
            return {"scope": "mixer", "label": "Send %s%s" % (letter, (" (%s)" % name) if name else ""),
                    "kind": "gain"}
        if in_mixer and not device_path:
            kind = {"Volume": "gain", "Pan": "pan", "Speaker": "bool"}.get(param_name, "raw")
            return {"scope": "mixer", "label": PARAM_DISPLAY_NAMES.get(param_name, param_name), "kind": kind}
        label = " > ".join(device_path) if device_path else "?"
        kind = "bool" if param_name == "On" else "raw"
        return {"scope": "device", "label": "%s: %s" % (label, PARAM_DISPLAY_NAMES.get(param_name, param_name)),
                "kind": kind}

    def mixer(self, tr: ET.Element) -> dict:
        m = tr.find("DeviceChain/Mixer")
        if m is None:
            return {}
        out = {
            "volume": gain_to_db(_float(_val(m, "Volume/Manual"))),
            "pan": pan_text(_float(_val(m, "Pan/Manual"))),
            "active": _val(m, "Speaker/Manual") == "true",
        }
        sends = []
        holders = m.find("Sends")
        for h in list(holders) if holders is not None else []:
            v = _float(_val(h, "Send/Manual"))
            sends.append({"index": int(h.get("Id", "0")), "db": gain_to_db(v)})
        out["sends"] = sends
        return out

    def output(self, tr: ET.Element) -> str | None:
        return _val(tr, "DeviceChain/AudioOutputRouting/UpperDisplayString")

    def devices(self, tr: ET.Element) -> list[dict]:
        devs = tr.find("DeviceChain/DeviceChain/Devices")
        out = []
        for d in list(devs) if devs is not None else []:
            out.append({"name": _device_name(d), "xml": d.tag, "on": _val(d, "On/Manual", "true") == "true"})
        return out

    def arrangement_clips(self, tr: ET.Element) -> dict:
        seq = tr.find("DeviceChain/MainSequencer")
        if seq is None:
            return {"count": 0}
        clips = [c for ev in seq.iter("Events") for c in ev if c.tag in ("MidiClip", "AudioClip")
                 and self._parent.get(ev) is not None and self._parent[ev].tag == "ArrangerAutomation"]
        disabled = sum(1 for c in clips if _val(c, "Disabled") == "true")
        starts = [_float(c.get("Time"), 0.0) for c in clips]
        ends = [_float(_val(c, "CurrentEnd"), 0.0) for c in clips]
        return {"count": len(clips), "disabled": disabled,
                "first": min(starts) if starts else None, "last_end": max(ends) if ends else None}

    def envelopes(self, tr: ET.Element) -> list[dict]:
        out = []
        for env in tr.findall("AutomationEnvelopes/Envelopes/AutomationEnvelope"):
            pid = _val(env, "EnvelopeTarget/PointeeId")
            events_el = env.find("Automation/Events")
            events = []
            for ev in list(events_el) if events_el is not None else []:
                t = _float(ev.get("Time"), 0.0)
                v = ev.get("Value")
                events.append((t, v, ev.tag))
            events.sort(key=lambda e: e[0])
            label = self._target_labels.get(pid, {"scope": "?", "label": "unbekanntes Ziel %s" % pid,
                                                  "kind": "raw"})
            out.append({"target_id": pid, "label": label["label"], "scope": label["scope"],
                        "kind": label["kind"], "events": events})
        return out

    def locators(self) -> list[tuple[float, str]]:
        out = []
        for loc in self.live.iter("Locator"):
            out.append((_float(_val(loc, "Time"), 0.0), _val(loc, "Name", "")))
        out.sort()
        return out

    # -- Text -------------------------------------------------------------

    def value_text(self, kind: str, raw: str) -> str:
        if kind == "bool":
            return "an" if raw == "true" else "aus"
        f = _float(raw)
        if f is None:
            return raw
        if kind == "gain":
            return gain_to_db(f)
        if kind == "pan":
            return pan_text(f)
        return "%g" % f

    def pos(self, beat: float) -> str:
        return format_position(beat, self.beats_per_bar)


def _dedupe(events):
    """Entfernt Wiederholungen desselben Werts, behaelt aber Spruenge.

    Zwei Punkte zur selben Zeit (Sprung) bleiben beide erhalten, ebenso der
    letzte Punkt vor einem Wertwechsel, weil er eine Rampe begrenzt.
    """
    out = []
    for i, ev in enumerate(events):
        if out and ev[1] == out[-1][1]:
            nxt = events[i + 1] if i + 1 < len(events) else None
            if nxt is None or nxt[1] == ev[1]:
                continue
            if nxt[0] == ev[0]:
                continue
        out.append(ev)
    return out


def describe(path: str | None = None, section: str = "overview", track: str | None = None,
             max_events: int = 80, source_note: str = "angegeben") -> str:
    if not path:
        path, source_note = find_current_set_from_log()
        if not path:
            return "Kein Set gefunden: " + source_note
    if not os.path.isfile(path):
        return "Datei nicht gefunden: %s (Quelle des Pfads: %s)" % (path, source_note)
    s = SavedSet(path)
    lines = []
    lines.append("Gespeichertes Set: %s" % path)
    lines.append("Quelle des Pfads: %s" % source_note)
    lines.append("Gespeichert: %s (%s). Zeigt den Stand dieser Speicherung, nicht ungespeicherte Aenderungen."
                 % (s.saved_at.strftime("%d.%m.%Y %H:%M:%S"), s.creator))
    lines.append("Tempo %s BPM, Taktart %d/%d. Positionen als Takt.Schlag.Sechzehntel."
                 % ("%g" % s.tempo if s.tempo else "?", s.signature[0], s.signature[1]))
    entries = s.track_entries()

    wanted = None
    if track is not None:
        t = str(track).strip().lower()
        wanted = [e for e in entries if str(e["ref"]).lower() == t or e["name"].lower() == t
                  or t in e["name"].lower()]
        if not wanted:
            return "\n".join(lines + ["Keine Spur passt zu '%s'. Vorhanden: %s"
                                      % (track, ", ".join("%s %s" % (e["ref"], e["name"]) for e in entries))])

    if section in ("overview", "all"):
        locs = s.locators()
        if locs:
            lines.append("")
            lines.append("Locator: " + " | ".join("%s %s" % (s.pos(t), n) for t, n in locs))
        lines.append("")
        lines.append("Spuren (gespeicherte Grundwerte, ohne Automation):")
        for e in (wanted or entries):
            tr = e["el"]
            mixer = s.mixer(tr)
            envs = s.envelopes(tr)
            automated = set(env["label"] for env in envs if env["scope"] == "mixer")
            # Ein automatisierter Parameter folgt im Arrangement seiner Kurve;
            # der gespeicherte Grundwert gilt dann nicht. Markieren, sonst
            # liest man z. B. "Spur aus", obwohl die Automation sie einschaltet.
            mark = lambda label: " (automatisiert, siehe unten)" if label in automated else ""
            indent = "    " if e.get("group") is not None else ""
            parts = ["%s[%s] %s (%s)" % (indent, e["ref"], e["name"], e["kind"])]
            if e.get("group") is not None:
                parts.append("in Gruppe [%s]" % e["group"])
            if mixer:
                vol = mixer["volume"] + mark("Lautstaerke")
                pan = "Pan " + mixer["pan"] + mark("Panorama")
                if "Spuraktivator" in automated:
                    act = "Aktivator automatisiert"
                else:
                    act = "" if mixer["active"] else "Spur AUS"
                parts.append(", ".join(x for x in (vol, pan, act) if x))
                send_texts = []
                for x in mixer["sends"]:
                    letter = chr(65 + x["index"])
                    is_auto = any(lbl.startswith("Send %s" % letter) for lbl in automated)
                    if x["db"] != "-inf dB" or is_auto:
                        send_texts.append("%s %s%s" % (letter, x["db"], " (automatisiert)" if is_auto else ""))
                if send_texts:
                    parts.append("Sends: " + ", ".join(send_texts))
            out = s.output(tr)
            if out:
                parts.append("-> %s" % out)
            lines.append(" | ".join(parts))
            devs = s.devices(tr)
            if devs:
                lines.append(indent + "      Geraete: " + ", ".join(d["name"] + ("" if d["on"] else " (aus)") for d in devs))
            if e["kind"] in ("midi", "audio"):
                ac = s.arrangement_clips(tr)
                if ac["count"]:
                    lines.append(indent + "      Arrangement: %d Clips, %d deaktiviert, %s bis %s"
                                 % (ac["count"], ac["disabled"], s.pos(ac["first"]), s.pos(ac["last_end"])))
            if envs:
                lines.append(indent + "      Automation: " + "; ".join(
                    "%s (%d Punkte)" % (env["label"], len(env["events"])) for env in envs))

    if section in ("automation", "all"):
        lines.append("")
        lines.append("Automation im Arrangement (Wiederholungen gleicher Werte ausgelassen):")
        found = False
        for e in (wanted or entries):
            envs = s.envelopes(e["el"])
            if not envs:
                continue
            for env in envs:
                found = True
                events = _dedupe(env["events"])
                lines.append("")
                lines.append("[%s] %s - %s: %d Punkte, %d nach Bereinigung"
                             % (e["ref"], e["name"], env["label"], len(env["events"]), len(events)))
                shown = events[:max_events]
                row = []
                for t, v, _tag in shown:
                    row.append("%s %s" % (s.pos(t), s.value_text(env["kind"], v)))
                for i in range(0, len(row), 6):
                    lines.append("    " + " | ".join(row[i:i + 6]))
                if len(events) > max_events:
                    lines.append("    ... %d weitere Punkte (max_events erhoehen)" % (len(events) - max_events))
        if not found:
            lines.append("    keine")
    return "\n".join(lines)
