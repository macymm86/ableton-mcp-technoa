"""Formatierung der Rohdaten aus dem Remote Script zu kompaktem Text.

Liegt bewusst im MCP-Server und nicht im Remote Script: Darstellung aendert
sich oft, und Aenderungen hier brauchen keinen Neustart von Live.

Text statt JSON, weil grosse JSON-Antworten das Tokenlimit von Claude Code
sprengen (500 Clips einer Spur waren rund 120.000 Zeichen).
"""
from __future__ import annotations

import json
import math
import re

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Live.Groove.Base, Reihenfolge wie in Lives Groove-Pool-Auswahl. Belegt ist
# nur 3 = 1/16 ("Swing 16ths 66" in einem echten Set, gespeichert als Grid 3). Die
# uebrigen Zuordnungen sind abgeleitet; die Rohzahl wird deshalb mit ausgegeben.
GROOVE_BASES = {0: "1/4", 1: "1/8", 2: "1/8T", 3: "1/16", 4: "1/16T", 5: "1/32"}

MONITORING = {0: "In", 1: "Auto", 2: "Off"}

KIND_LABELS = {
    "midi": "MIDI",
    "audio": "Audio",
    "group": "Gruppe",
    "return": "Return",
    "master": "Master",
    "unknown": "?",
}

AUTOMATION_LABELS = {"active": "Automation aktiv", "overridden": "Automation UEBERSTEUERT (Re-Enable noetig)"}

# Notenfelder ueber Tonhoehe, Zeit, Dauer, Velocity und Stumm hinaus, mit Lives
# Voreinstellung. Gleich benannt wie im Remote Script (NOTE_EXTRA_FIELDS).
NOTE_EXTRA_FIELDS = ("probability", "velocity_deviation", "release_velocity")
NOTE_EXTRA_DEFAULTS = {"probability": 1.0, "velocity_deviation": 0.0, "release_velocity": 64.0}


def note_name(pitch: int) -> str:
    # Ableton-Konvention: MIDI 60 = C3 (nicht C4 wie in vielen anderen Programmen).
    return "%s%d" % (NOTE_NAMES[pitch % 12], pitch // 12 - 2)


def return_letter(ref) -> str:
    if isinstance(ref, str) and ref.startswith("return:"):
        return chr(ord("A") + int(ref.split(":", 1)[1]))
    return str(ref)


def ref_label(ref) -> str:
    if ref == "master":
        return "Master"
    if isinstance(ref, str) and ref.startswith("return:"):
        return return_letter(ref)
    return str(ref)


class Timeline:
    """Umrechnung zwischen Beats (Viertel ab Songanfang) und Taktpositionen.

    Geht von einer festen Taktart aus. Taktartwechsel im Arrangement liest
    Lives API nicht aus; bei so einem Set stimmen Positionen hinter dem
    Wechsel nicht.
    """

    def __init__(self, numerator: int = 4, denominator: int = 4):
        self.numerator = numerator or 4
        self.denominator = denominator or 4
        self.beats_per_bar = self.numerator * 4.0 / self.denominator

    def pos(self, beat: float | None) -> str:
        if beat is None:
            return "?"
        bar = int(math.floor(beat / self.beats_per_bar + 1e-9))
        rest = beat - bar * self.beats_per_bar
        quarter = int(math.floor(rest + 1e-9))
        frac = rest - quarter
        six = int(math.floor(frac * 4 + 1e-9))
        left = frac * 4 - six
        text = "%d.%d.%d" % (bar + 1, quarter + 1, six + 1)
        if left > 1e-3:
            text += "+%.2f" % left
        return text

    def bar_to_beat(self, bar: float) -> float:
        return (float(bar) - 1.0) * self.beats_per_bar

    def parse_pos(self, text) -> float:
        """Umkehrung von pos(): "33.3.1" oder "33.3.1+0.50" -> Beat. Eine einzelne
        Zahl gilt als Takt (1-basiert). So laesst sich eine Position direkt aus
        einer Ausgabe von get_arrangement_clips uebernehmen."""
        if isinstance(text, (int, float)) and not isinstance(text, bool):
            return self.bar_to_beat(text)
        s = str(text).strip()
        m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:\+(\d*\.?\d+))?", s)
        if m:
            bar, quarter, six = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if bar < 1 or quarter < 1 or six < 1:
                raise ValueError("Position '%s': Takt, Schlag und Sechzehntel zaehlen ab 1." % s)
            return ((bar - 1) * self.beats_per_bar + (quarter - 1) + (six - 1 + float(m.group(4) or 0)) / 4.0)
        try:
            return self.bar_to_beat(float(s.replace(",", ".")))
        except ValueError:
            raise ValueError("Position '%s' nicht lesbar. Erlaubt: Takt als Zahl (33) oder Takt.Schlag.Sechzehntel "
                             "wie in get_arrangement_clips (33.3.1)." % s)

    def dur(self, beats: float | None) -> str:
        if beats is None:
            return "?"
        bars = beats / self.beats_per_bar
        if beats > 0 and abs(bars - round(bars)) < 1e-6 and round(bars) >= 1:
            n = int(round(bars))
            return "%d Takt%s" % (n, "" if n == 1 else "e")
        if bars > 1:
            # 64.25 Beats lesen sich als "16 Takte + 1/16" besser; so fiel der
            # Loop 33-49 plus eine Sechzehntel ueberhaupt erst auf.
            n = int(math.floor(bars + 1e-9))
            return "%s + %s" % (self.dur(n * self.beats_per_bar), self.dur(beats - n * self.beats_per_bar))
        named = {0.25: "1/16", 0.5: "1/8", 1.0: "1/4", 2.0: "1/2"}
        for k, v in named.items():
            if abs(beats - k) < 1e-6:
                return v
        return "%g Beats" % round(beats, 4)


def _auto(info: dict | None) -> str:
    if not info or "automation" not in info:
        return ""
    return " [%s]" % AUTOMATION_LABELS.get(info["automation"], info["automation"])


def _display(info: dict | None) -> str:
    if not info:
        return "?"
    return str(info.get("display") if info.get("display") is not None else info.get("value"))


def _is_silent_send(info: dict) -> bool:
    d = (info.get("display") or "").lower()
    return "inf" in d and "automation" not in info


# ----------------------------------------------------------------------
# Set-Uebersicht
# ----------------------------------------------------------------------

def song_header(state: dict, tl: Timeline) -> list[str]:
    lines = []
    scale = state.get("scale") or {}
    scale_text = "?"
    if scale.get("root_note") is not None:
        scale_text = "%s %s (Skalenmodus %s)" % (NOTE_NAMES[scale["root_note"] % 12], scale.get("scale_name") or "?",
                                                  "an" if scale.get("scale_mode") else "aus")
    length = state.get("song_length")
    lines.append("SET  %g BPM | %d/%d | Songlaenge %s (endet bei %s) | Skala %s" % (
        state.get("tempo") or 0, tl.numerator, tl.denominator, tl.dur(length), tl.pos(length), scale_text))
    transport = "laeuft" if state.get("is_playing") else "gestoppt"
    loop = "aus"
    if state.get("loop"):
        ls, ll = state.get("loop_start"), state.get("loop_length")
        loop = "an, %s bis %s (%s)" % (tl.pos(ls), tl.pos((ls or 0) + (ll or 0)), tl.dur(ll))
    rec = []
    if state.get("record_mode"):
        rec.append("ARRANGEMENT-AUFNAHME AN")
    if state.get("session_record"):
        rec.append("SESSION-AUFNAHME AN")
    if state.get("arrangement_overdub"):
        rec.append("MIDI-Overdub an")
    lines.append("TRANSPORT  %s, Playhead %s | Loop %s | %s | Metronom %s" % (
        transport, tl.pos(state.get("current_song_time")), loop, ", ".join(rec) or "keine Aufnahme aktiv",
        "an" if state.get("metronome") else "aus"))
    grooves = state.get("grooves") or []
    if grooves:
        parts = []
        for g in grooves:
            base = GROOVE_BASES.get(g.get("base"), "?")
            parts.append("%s (Basis %s [Rohwert %s], Timing %g%%, Quantisierung %g%%, Random %g%%, Velocity %g%%)" % (
                g.get("name"), base, g.get("base"), g.get("timing_amount") or 0, g.get("quantization_amount") or 0,
                g.get("random_amount") or 0, g.get("velocity_amount") or 0))
        amount = state.get("groove_amount")
        lines.append("GROOVES  " + " | ".join(parts) + (" | globale Menge %g%%" % (amount * 100) if amount is not None else ""))
    cues = state.get("cue_points") or []
    if cues:
        lines.append("LOCATOR  " + " | ".join("%s %s" % (tl.pos(c.get("time")), c.get("name")) for c in cues))
    lines.append("UNDO  %s, Redo %s" % ("moeglich" if state.get("can_undo") else "nichts",
                                        "moeglich" if state.get("can_redo") else "nichts"))
    if state.get("re_enable_automation_enabled"):
        lines.append("ACHTUNG  Automation ist irgendwo UEBERSTEUERT (Lives Re-Enable-Knopf leuchtet): "
                     "re_enable_automation")
    if state.get("back_to_arranger"):
        lines.append("ACHTUNG  'Back to Arrangement' leuchtet: Mindestens eine Spur spielt Session statt "
                     "Arrangement: back_to_arrangement")
    return lines


# Live 12 nennt den Hauptausgang "Main", aeltere Fassungen "Master".
MAIN_OUTPUTS = ("main", "master")


def usual_output(t: dict) -> str | None:
    """Der uebliche Ausgang einer Spur: in einer Gruppe die Gruppe, sonst Main.
    Am Set 22.10.2026 so gelesen; Master (Ext. Out) gilt immer als ueblich."""
    if t.get("kind") == "master":
        return None
    if t.get("is_grouped") and t.get("group_name"):
        return t["group_name"]
    return "Main"


def unusual_output(t: dict) -> str | None:
    """Der uebliche Ausgang, wenn der jetzige davon abweicht, sonst None.

    Am 19.10.2026 ist beim Setzen einer Sidechain von Hand der DRUMS-Ausgang
    auf Ext. Out gerutscht; in der Uebersicht stand das unauffaellig
    zwischen lauter '-> Main' und '-> Gruppe'."""
    out = (t.get("routing") or {}).get("output_type")
    usual = usual_output(t)
    if not out or usual is None or out == usual:
        return None
    if usual == "Main" and out.strip().lower() in MAIN_OUTPUTS:
        return None
    return usual


def tapped_input(t: dict, names: set | None) -> str | None:
    """'<- Spur / Kanal', wenn eine MIDI- oder Audiospur eine andere Spur abhoert
    (z. B. KICK_PRINT_v1 <- KICK BODY / Post FX) oder resampelt."""
    if t.get("kind") not in ("midi", "audio"):
        return None
    routing = t.get("routing") or {}
    src = routing.get("input_type")
    if not src or not (src == "Resampling" or (names and src in names)):
        return None
    chan = routing.get("input_channel")
    return "<- %s%s" % (src, " / " + chan if chan else "")


def _onoff_text(v) -> str:
    return {True: "an", False: "aus"}.get(v, "unbekannt")


def sidechain_text(d: dict) -> str | None:
    sc = d.get("sidechain")
    if not sc:
        return None
    state = {True: "an", False: "aus"}.get(sc.get("on"), "Schalter unbekannt")
    return "S/C <- %s%s (%s)" % (sc.get("type") or "?", " / " + sc["channel"] if sc.get("channel") else "", state)


def _track_line(t: dict, tl: Timeline, depth: int, names: set | None = None) -> list[str]:
    pad = "    " * depth
    kind = KIND_LABELS.get(t.get("kind"), t.get("kind"))
    extra = []
    if t.get("kind") == "group":
        extra.append("eingeklappt" if t.get("fold_state") else "aufgeklappt")
    head = "%s[%s] %s (%s%s)" % (pad, ref_label(t.get("ref")), t.get("name"), kind,
                                 ", " + ", ".join(extra) if extra else "")
    mixer = t.get("mixer") or {}
    parts = [head]
    parts.append(_display(mixer.get("volume")) + _auto(mixer.get("volume")))
    parts.append("Pan " + _display(mixer.get("panning")) + _auto(mixer.get("panning")))
    routing = t.get("routing") or {}
    if routing.get("output_type"):
        parts.append("-> " + routing["output_type"])
    tapped = tapped_input(t, names)
    if tapped:
        parts.append(tapped)
    flags = []
    usual = unusual_output(t)
    if usual:
        flags.append("AUSGANG UNGEWOEHNLICH (ueblich: %s)" % usual)
    if t.get("mute"):
        flags.append("STUMM")
    if t.get("solo"):
        flags.append("SOLO")
    if t.get("arm"):
        flags.append("SCHARF (nimmt auf)")
    elif t.get("implicit_arm"):
        flags.append("implizit scharf (implicit_arm, vermutlich Lives Auto-Arm)")
    act = mixer.get("track_activator")
    if act:
        if act.get("automation"):
            flags.append("Aktivator: " + AUTOMATION_LABELS.get(act["automation"], act["automation"]))
        elif act.get("value") == 0 and not t.get("mute"):
            # mute und Spuraktivator sind in Live derselbe Schalter.
            flags.append("Spur AUS (Aktivator)")
    if flags:
        parts.append(", ".join(flags))
    lines = [" | ".join(parts)]
    sends = [s for s in mixer.get("sends") or [] if not _is_silent_send(s)]
    if sends:
        lines.append(pad + "      Sends: " + ", ".join(
            "%s %s%s" % (chr(65 + s.get("index", 0)), _display(s), _auto(s)) for s in sends))
    devs = t.get("devices") or []
    if devs:
        texts = []
        for d in devs:
            txt = d.get("name") or "?"
            if not d.get("is_active", True):
                txt += " (AUS)"
            auto = d.get("automated") or []
            over = d.get("automation_overridden") or []
            if auto:
                txt += " [automatisiert: %s]" % ", ".join(auto[:6]) + (" ..." if len(auto) > 6 else "")
            if over:
                txt += " [uebersteuert: %s]" % ", ".join(over[:6])
            sc = d.get("sidechain")
            if sc and sc.get("on") is not False:
                txt += " [%s]" % sidechain_text(d)
            texts.append(txt)
        lines.append(pad + "      Geraete: " + ", ".join(texts))
    arr = t.get("arrangement")
    if arr and arr.get("clip_count"):
        lines.append(pad + "      Arrangement: %d Clip%s, %s bis %s, %d deaktiviert" % (
            arr["clip_count"], "" if arr["clip_count"] == 1 else "s", tl.pos(arr.get("first_start")), tl.pos(arr.get("last_end")), arr.get("muted_count", 0)))
    sess = t.get("session_clips")
    if sess:
        lines.append(pad + "      Session-Clips in Slots: " + ", ".join(str(s if isinstance(s, int) else s.get("index")) for s in sess))
    return lines


def _depths(tracks: list[dict]) -> dict:
    by_ref = {t["ref"]: t for t in tracks}
    depth = {}
    for t in tracks:
        d, cur, seen = 0, t, set()
        while cur.get("group") is not None and cur["group"] in by_ref and cur["ref"] not in seen:
            seen.add(cur["ref"])
            d += 1
            cur = by_ref[cur["group"]]
        depth[t["ref"]] = d
    return depth


def set_overview(state: dict, overview: dict) -> str:
    tl = Timeline(state.get("signature_numerator"), state.get("signature_denominator"))
    lines = song_header(state, tl)
    lines.append("")
    lines.append("SPUREN  [Index] Name (Art) | Pegel | Pan | Ausgang (<- abgehoerte Spur) | Status. Eingerueckt = in "
                 "der Gruppe darueber.")
    tracks = overview.get("tracks") or []
    returns = overview.get("returns") or []
    master = overview.get("master")
    names = {t.get("name") for t in tracks + returns + ([master] if master else [])}
    depth = _depths(tracks)
    for t in tracks:
        lines.extend(_track_line(t, tl, depth.get(t["ref"], 0), names))
    if returns:
        lines.append("")
        lines.append("RETURNS  (ansprechbar als A, B, ... oder return:0, return:1, ...)")
        for t in returns:
            lines.extend(_track_line(t, tl, 0, names))
    if master:
        lines.append("")
        lines.append("MASTER")
        lines.extend(_track_line(master, tl, 0, names))
    armed = [t for t in tracks if t.get("arm")]
    if armed:
        lines.append("")
        lines.append("HINWEIS: Scharfgeschaltet: " + ", ".join(t["name"] for t in armed)
                     + ". Jede Aufnahme landet dort.")
    odd = [(t, unusual_output(t)) for t in tracks + returns if unusual_output(t)]
    if odd:
        lines.append("")
        lines.append("HINWEIS: Ausgang ungewoehnlich: " + "; ".join(
            "[%s] %s -> %s (ueblich %s)" % (ref_label(t.get("ref")), t.get("name"),
                                            (t.get("routing") or {}).get("output_type"), usual) for t, usual in odd)
                     + ". Absicht? Sonst set_routing (so ist am 19.10.2026 der DRUMS-Ausgang verrutscht).")
    lines.append("")
    lines.append("Pegel und Pan sind Lives Anzeigewerte. 'Automation aktiv' heisst: Der Wert folgt im Arrangement einer "
                 "Kurve; den Verlauf zeigt read_saved_set (Stand der letzten Speicherung).")
    return "\n".join(lines)


def track_detail(t: dict, state: dict) -> str:
    tl = Timeline(state.get("signature_numerator"), state.get("signature_denominator"))
    lines = _track_line(t, tl, 0)
    mixer = t.get("mixer") or {}
    lines.append("")
    lines.append("MIXER")
    lines.append("  Lautstaerke: %s (Rohwert %s)%s" % (_display(mixer.get("volume")), (mixer.get("volume") or {}).get("value"),
                                                     _auto(mixer.get("volume"))))
    lines.append("  Panorama: %s%s" % (_display(mixer.get("panning")), _auto(mixer.get("panning"))))
    act = mixer.get("track_activator")
    if act:
        lines.append("  Spuraktivator: %s%s" % (_display(act), _auto(act)))
    for s in mixer.get("sends") or []:
        lines.append("  Send %s: %s%s" % (chr(65 + s.get("index", 0)), _display(s), _auto(s)))
    if t.get("kind") in ("midi", "audio"):
        lines.append("  Monitoring: %s | scharf: %s" % (MONITORING.get(t.get("monitoring"), t.get("monitoring")),
                                                      "ja" if t.get("arm") else "nein"))
    routing = t.get("routing") or {}
    if routing:
        out = "Ausgang: %s / %s" % (routing.get("output_type", "-"), routing.get("output_channel", "-") or "-")
        # Returns, Gruppen und Master melden zwar einen Eingang, der ist dort aber ohne Bedeutung.
        if t.get("kind") in ("midi", "audio"):
            out = "Eingang: %s / %s | %s" % (routing.get("input_type", "-"), routing.get("input_channel", "-"), out)
        lines.append("  " + out)
    if t.get("group") is not None:
        lines.append("  Gruppe: [%s] %s" % (t.get("group"), t.get("group_name")))
    lines.append("")
    lines.append("GERAETE  (Index fuer get_device_parameters)")
    for d in t.get("devices") or []:
        lines.append("  [%d] %s | %s | %s%s | %d Parameter%s" % (
            d.get("index"), d.get("name"), d.get("class_display_name") or d.get("class_name"), d.get("kind"),
            "" if d.get("is_active", True) else " | AUS", d.get("parameter_count", 0),
            (" | %d Ketten" % d["chain_count"]) if d.get("chain_count") else ""))
        if d.get("automated"):
            lines.append("        automatisiert: " + ", ".join(d["automated"]))
        if d.get("automation_overridden"):
            lines.append("        Automation uebersteuert: " + ", ".join(d["automation_overridden"]))
        if d.get("sidechain"):
            lines.append("        Sidechain: " + sidechain_text(d))
    sess = [s for s in t.get("session_clips") or [] if isinstance(s, dict)]
    if sess:
        lines.append("")
        lines.append("SESSION-CLIPS")
        for c in sess:
            state_text = " | SPIELT" if c.get("is_playing") else (" | startet gleich" if c.get("is_triggered") else "")
            lines.append("  Slot %d: %s | %s%s%s" % (c.get("index"), c.get("name") or "(ohne Namen)",
                                                   tl.dur(c.get("length")), " | deaktiviert" if c.get("muted") else "",
                                                   state_text))
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Arrangement-Clips
# ----------------------------------------------------------------------

def _clip_signature(c: dict):
    length = round((c.get("end_time") or 0) - (c.get("start_time") or 0), 6)
    return (c.get("name"), c.get("muted"), length, c.get("content_digest"), c.get("looping"),
            c.get("loop_start"), c.get("loop_end"), c.get("start_marker"), c.get("groove"))


MAX_PATTERN = 16


def clip_segments(clips: list[dict]) -> list[dict]:
    """Zerlegt die Clips einer Spur in Abschnitte, die sich regelmaessig wiederholen.

    Ein Abschnitt ist ein Muster aus p Clips, das k-mal im gleichen Abstand
    wiederkehrt. p = 1 ist ein einfacher Lauf gleicher Clips. Damit wird z. B.
    "Inhalt A auf Schlag 1, Inhalt B auf 2 bis 4, 125 Takte lang" eine Zeile
    statt 250. Bei gleicher Abdeckung gewinnt das kuerzere Muster.
    """
    ordered = sorted(clips, key=lambda x: (x.get("start_time") or 0))
    sig = [_clip_signature(c) for c in ordered]
    t = [c.get("start_time") or 0.0 for c in ordered]
    n = len(ordered)
    out = []
    i = 0
    while i < n:
        best_p, best_cover = 1, 1
        for p in range(1, min(MAX_PATTERN, (n - i) // 2) + 1):
            period = t[i + p] - t[i]
            if period <= 1e-9:
                continue
            j = i + p
            while j < n and sig[j] == sig[j - p] and abs((t[j] - t[j - p]) - period) < 1e-6:
                j += 1
            cover = ((j - i) // p) * p
            if cover >= 2 * p and cover > best_cover:
                best_p, best_cover = p, cover
        part = ordered[i:i + best_cover]
        out.append({"clips": part, "period": best_p, "repeats": best_cover // best_p,
                    "step": (t[i + best_p] - t[i]) if best_cover > best_p else None})
        i += best_cover
    return out


def _letter(n: int) -> str:
    # A..Z, danach AA, AB, ... (eine Spur mit vielen verschiedenen Clips).
    text = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        text = chr(65 + rem) + text
    return text


def _clip_flags(c: dict) -> str:
    flags = ["DEAKTIVIERT" if c.get("muted") else "aktiv"]
    if c.get("groove"):
        flags.append("Groove " + c["groove"])
    if c.get("is_audio"):
        f = c.get("file_path")
        if f:
            flags.append("Datei " + f.replace("\\", "/").split("/")[-1])
        if c.get("gain_display"):
            flags.append("Gain " + c["gain_display"])
        if c.get("pitch_coarse"):
            flags.append("Pitch %+d st" % c["pitch_coarse"])
    if c.get("has_envelopes"):
        flags.append("Clip-Automation")
    return ", ".join(flags)


def arrangement_clips(result: dict, state: dict, detail: str = "runs") -> str:
    tl = Timeline(state.get("signature_numerator"), state.get("signature_denominator"))
    clips = result.get("clips") or []
    lines = ["[%s] %s: %d von %d Arrangement-Clips im Bereich" % (
        ref_label(result.get("track")), result.get("track_name"), len(clips), result.get("clip_count_total", len(clips)))]
    if not clips:
        return lines[0]
    if detail == "clips":
        lines.append("Index | Start bis Ende | Laenge | Name | Status | Inhalt")
        for c in clips:
            lines.append("#%d | %s bis %s | %s | %s | %s | %s" % (
                c.get("index"), tl.pos(c.get("start_time")), tl.pos(c.get("end_time")),
                tl.dur((c.get("end_time") or 0) - (c.get("start_time") or 0)), c.get("name") or "(ohne Namen)",
                _clip_flags(c), c.get("content_digest") or "-"))
        return "\n".join(lines)
    segments = clip_segments(clips)
    lines.append("Zusammengefasst: gleiche Clips bzw. gleiche Clipmuster mit gleichem Abstand = eine Zeile.")
    digests = {}
    for c in sorted(clips, key=lambda x: (x.get("start_time") or 0)):
        digests.setdefault(c.get("content_digest"), len(digests) + 1)

    def content(c):
        d = c.get("content_digest")
        return "Inhalt %s" % _letter(digests[d]) if d else ""

    def kind_len(c):
        return "%s %s" % ("MIDI" if c.get("is_midi") else "Audio",
                          tl.dur((c.get("end_time") or 0) - (c.get("start_time") or 0)))

    for seg in segments:
        part = seg["clips"]
        first, last = part[0], part[-1]
        span = "#%d%s | %s bis %s" % (first.get("index"), "" if len(part) == 1 else "-#%d" % last.get("index"),
                                       tl.pos(first.get("start_time")), tl.pos(last.get("end_time")))
        if len(part) == 1:
            lines.append("%s | %s | '%s' | %s | %s" % (span, kind_len(first), first.get("name") or "",
                                                      _clip_flags(first), content(first)))
        elif seg["period"] == 1:
            lines.append("%s | %dx %s, alle %s | '%s' | %s | %s" % (
                span, len(part), kind_len(first), tl.dur(seg["step"]), first.get("name") or "",
                _clip_flags(first), content(first)))
        else:
            pattern = part[:seg["period"]]
            flags = set(_clip_flags(c) for c in pattern)
            names = set(c.get("name") or "" for c in pattern)
            lines.append("%s | Muster aus %d Clips, %dx, wiederholt alle %s%s%s" % (
                span, seg["period"], seg["repeats"], tl.dur(seg["step"]),
                (" | '%s'" % names.pop()) if len(names) == 1 else "",
                (" | " + flags.pop()) if len(flags) == 1 else ""))
            items = []
            for c in pattern:
                item = "%s %s %s" % (tl.pos(c["start_time"] - first["start_time"]), kind_len(c), content(c))
                if len(names) > 1:
                    item += " '%s'" % (c.get("name") or "")
                if len(flags) > 1:
                    item += " (%s)" % _clip_flags(c)
                items.append(item)
            lines.append("      im Muster: " + " | ".join(items))
    if any(k for k in digests):
        lines.append("Inhalt A, B, ...: gleicher Buchstabe = gleiche Noten bzw. gleicher Audioausschnitt.")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Noten
# ----------------------------------------------------------------------

def clip_notes(result: dict, state: dict, max_notes: int = 256) -> str:
    tl = Timeline(state.get("signature_numerator"), state.get("signature_denominator"))
    clip = result.get("clip") or {}
    notes = clip.get("notes") or []
    head = "[%s] %s | %s-Clip #%s '%s'" % (ref_label(result.get("track")), result.get("track_name"),
                                          "Arrangement" if result.get("source") == "arrangement" else "Session",
                                          clip.get("index"), clip.get("name") or "")
    lines = [head]
    if clip.get("start_time") is not None:
        lines.append("Position im Song: %s bis %s" % (tl.pos(clip.get("start_time")), tl.pos(clip.get("end_time"))))
    loop = "Loop %s bis %s Beats" % (clip.get("loop_start"), clip.get("loop_end")) if clip.get("looping") else "kein Loop"
    lines.append("Clip-Laenge %s | %s | Startmarker %s | %s" % (tl.dur(clip.get("length")), loop, clip.get("start_marker"),
                                                            _clip_flags(clip)))
    if not notes:
        lines.append("Keine Noten.")
        return "\n".join(lines)
    pitches = {}
    for n in notes:
        pitches[n["pitch"]] = pitches.get(n["pitch"], 0) + 1
    vels = [n["velocity"] for n in notes]
    lines.append("%d Noten | Tonhoehen: %s | Velocity %g bis %g" % (
        len(notes), ", ".join("%s (%d) x%d" % (note_name(p), p, c) for p, c in sorted(pitches.items())),
        min(vels), max(vels)))
    lines.append("Notennamen nach Ableton (60 = C3). Zeit in Beats ab Clip-Anfang; Pos als Takt.Schlag.16tel im Clip.")
    extra_cols = any(n.get("mute") for n in notes) or any(
        n.get("probability") not in (None, 1.0) for n in notes)
    # Nur zeigen, was vom Standard abweicht, sonst wird jede Tabelle breiter.
    more = [k for k in ("velocity_deviation", "release_velocity")
            if any(n.get(k) is not None and n[k] != NOTE_EXTRA_DEFAULTS[k] for n in notes)]
    labels = {"velocity_deviation": "VelDev", "release_velocity": "RelVel"}
    lines.append("Zeit | Pos | Note | Dauer | Vel" + (" | Prob | stumm" if extra_cols else "")
                 + "".join(" | " + labels[k] for k in more))
    for n in notes[:max_notes]:
        row = "%g | %s | %s | %s | %g" % (round(n["start_time"], 4), tl.pos(n["start_time"]), note_name(n["pitch"]),
                                          tl.dur(n["duration"]), n["velocity"])
        if extra_cols:
            row += " | %s | %s" % (n.get("probability", 1.0), "ja" if n.get("mute") else "")
        row += "".join(" | %g" % n.get(k, NOTE_EXTRA_DEFAULTS[k]) for k in more)
        lines.append(row)
    if len(notes) > max_notes:
        lines.append("... %d weitere Noten (max_notes erhoehen)" % (len(notes) - max_notes))
    return "\n".join(lines)


def _note_diffs(sent: dict, back: dict) -> list:
    out = []
    for k in ("duration", "velocity", "mute") + NOTE_EXTRA_FIELDS:
        if k not in sent:
            continue
        if back.get(k) is None:
            out.append((k, sent[k], None))
        elif k == "mute":
            if bool(back[k]) != bool(sent[k]):
                out.append((k, sent[k], back[k]))
        elif abs(float(back[k]) - float(sent[k])) > 1e-3:
            out.append((k, sent[k], back[k]))
    return out


def notes_readback(sent: list, back: list, tl: Timeline, max_lines: int = 6) -> list:
    """Vergleicht gesendete mit nachgelesenen Noten (gleiche Tonhoehe und Startzeit).

    Das Script meldet, was es Live NICHT gegeben hat. Ob Live das Uebergebene
    auch so speichert (begrenzt, rundet, ignoriert), zeigt erst das Nachlesen.
    """
    missing, differing = [], []
    for s in sent:
        cands = [b for b in back if b.get("pitch") == s["pitch"]
                 and abs((b.get("start_time") or 0.0) - s["start_time"]) < 1e-3]
        if not cands:
            missing.append(s)
            continue
        diffs = min((_note_diffs(s, b) for b in cands), key=len)
        if diffs:
            differing.append((s, diffs))

    def where(s):
        return "%s (%d) bei %s" % (note_name(s["pitch"]), s["pitch"], tl.pos(s["start_time"]))

    def val(v):
        if v is None:
            return "nicht lesbar"
        return ("ja" if v else "nein") if isinstance(v, bool) else "%g" % v

    lines = []
    if not missing and not differing:
        used = [k for k in NOTE_EXTRA_FIELDS if any(k in s for s in sent)]
        lines.append("Nachgelesen: alle %d Noten wie gesendet%s." % (
            len(sent), " (mit %s)" % ", ".join(used) if used else ""))
    if differing:
        lines.append("ACHTUNG, NACHGELESEN ANDERS: %d von %d Noten speichert Live anders als gesendet, der Clip "
                     "klingt also anders:" % (len(differing), len(sent)))
        for s, d in differing[:max_lines]:
            lines.append("  %s: %s" % (where(s), "; ".join("%s gesendet %s, in Live %s" % (k, val(a), val(b))
                                                          for k, a, b in d)))
        if len(differing) > max_lines:
            lines.append("  ... %d weitere" % (len(differing) - max_lines))
    if missing:
        lines.append("ACHTUNG: %d Note(n) danach nicht im Clip (gesucht nach Tonhoehe und Startzeit): %s%s" % (
            len(missing), ", ".join(where(s) for s in missing[:max_lines]),
            " ..." if len(missing) > max_lines else ""))
    return lines


def notes_added(result: dict, sent: list, back: list | None, state: dict) -> str:
    """Antwort von add_notes_to_clip: was geschrieben wurde, was nicht, und das Nachlesen."""
    tl = Timeline(state.get("signature_numerator"), state.get("signature_denominator"))
    before = result.get("notes_before")
    now = len(back) if back is not None else result.get("notes_after")
    lines = ["[%s] %s | %s-Clip #%s '%s': %d Noten gesendet (%s). Noten im Clip vorher %s, jetzt %s." % (
        ref_label(result.get("track")), result.get("track_name") or "",
        "Arrangement" if result.get("source") == "arrangement" else "Session", result.get("clip_index"),
        result.get("clip_name") or "", len(sent), result.get("method") or "?", before, now)]
    written = [k for k in result.get("fields_written") or [] if k in NOTE_EXTRA_FIELDS]
    if written:
        lines.append("Zusatzfelder an Live: " + ", ".join(
            "%s (%d Noten)" % (k, sum(1 for s in sent if k in s)) for k in written))
    for nw in result.get("not_written") or []:
        lines.append("ACHTUNG, NICHT GESCHRIEBEN: %s bei %s Note(n), %s." % (nw.get("field"), nw.get("notes"),
                                                                          nw.get("reason")))
    if "not_written" not in result:
        lines.append("HINWEIS: Dieses Remote Script meldet nicht, welche Felder es Live gegeben hat (aelter als "
                     "technoa.8). Massgeblich ist das Nachlesen.")
    if back is None:
        lines.append("ACHTUNG: Nachlesen fehlgeschlagen; die Noten mit get_clip_notes pruefen.")
        return "\n".join(lines)
    lines.extend(notes_readback(sent, back, tl))
    if before is not None and now != before + len(sent):
        lines.append("HINWEIS: Im Clip sind jetzt %d Noten, erwartet waren %d (vorher %d + %d gesendet). Live "
                     "fasst Noten gleicher Tonhoehe, die sich ueberlappen, vermutlich zusammen (ungeprueft)."
                     % (now, before + len(sent), before, len(sent)))
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Geraete, Auswahl, Meter
# ----------------------------------------------------------------------

def device_parameters(result: dict) -> str:
    d = result.get("device") or {}
    path = result.get("chain_path") or []
    lines = ["[%s] %s | Geraet %s%s: %s (%s, %s)%s" % (
        ref_label(result.get("track")), result.get("track_name"), d.get("index"),
        (" Kettenpfad %s" % path) if path else "", d.get("name"), d.get("class_display_name") or d.get("class_name"),
        d.get("kind"), "" if d.get("is_active", True) else " | GERAET AUS")]
    params = d.get("parameters") or []
    lines.append("%d Parameter angezeigt. Format: [Index] Name = Anzeige (Rohwert, Bereich)" % len(params))
    for p in params:
        rng = "%g..%g" % (p.get("min"), p.get("max")) if p.get("min") is not None else "?"
        extra = ""
        if p.get("value_items"):
            extra = " Optionen: " + " / ".join(p["value_items"][:12])
        if not p.get("is_enabled", True):
            extra += " (gesperrt)"
        lines.append("[%d] %s = %s (%s, %s)%s%s" % (p.get("index"), p.get("name"), p.get("display"),
                                                   "%g" % p["value"] if p.get("value") is not None else "?", rng,
                                                   _auto(p), extra))
    for c in d.get("chains") or []:
        lines.append("Kette %d '%s'%s: %s" % (c.get("index"), c.get("name"), " (stumm)" if c.get("mute") else "",
                                               ", ".join(c.get("devices") or [])))
    sc = d.get("sidechain")
    if sc:
        lines.append("Sidechain-Eingang: Quelle '%s', Kanal '%s', Schalter S/C On %s. Quellen: get_routing_options("
                     "track, device_index); setzen: set_sidechain." % (
                         sc.get("type"), sc.get("channel") or "-",
                         {True: "an", False: "AUS (das Geraet hoert die Quelle nicht)", None: "unbekannt"}[sc.get("on")]))
    return "\n".join(lines)


def _option(o: dict) -> str:
    return ("[%s] %s" % (ref_label(o["track"]), o.get("name"))) if o.get("track") is not None else str(o.get("name"))


def _routing_side_lines(label: str, side: dict | None, max_items: int = 40) -> list[str]:
    if not side:
        return ["%s: ueber die API nicht lesbar." % label]
    cur = side.get("type") or {}
    types = side.get("types") or []
    shown = ", ".join(_option(o) for o in types[:max_items]) + (" ..." if len(types) > max_items else "")
    return ["%s jetzt: %s / Kanal '%s'" % (label, _option(cur), side.get("channel") or "-"),
            "  waehlbar (%d): %s" % (len(types), shown or "nichts"),
            "  Kanaele dazu: %s" % (", ".join("'%s'" % c for c in side.get("channels") or [] if c)
                                    or "keine Auswahl")]


def routing_options(r: dict) -> str:
    where = "[%s] %s" % (ref_label(r.get("track")), r.get("track_name"))
    if r.get("device") is not None:
        lines = ["%s | Geraet %s '%s' | Sidechain-Eingang" % (where, r.get("device_index"), r.get("device"))]
        sc = r.get("sidechain") or {}
        lines.extend(_routing_side_lines("Quelle", r.get("input")))
        if sc.get("on") is False:
            lines.append("  S/C On ist AUS: Das Geraet hoert die Quelle nicht (set_sidechain mit enable=true).")
        lines.append("Setzen: set_sidechain(track, device_index, source=Name/Spur, channel=..., enable=...).")
    else:
        kind = r.get("kind")
        lines = ["%s (%s)" % (where, KIND_LABELS.get(kind, kind))]
        lines.extend(_routing_side_lines("Ausgang", r.get("output")))
        if kind in ("midi", "audio"):
            lines.extend(_routing_side_lines("Eingang", r.get("input")))
        else:
            lines.append("Eingang: bei %s ohne Bedeutung." % KIND_LABELS.get(kind, kind))
        lines.append("Setzen: set_routing(track, output=..., output_channel=..., input=..., input_channel=...).")
    lines.append("[n] = Spur n. Die Kanaele gelten fuer die jetzige Auswahl; nach einem Wechsel bietet Live andere an.")
    return "\n".join(lines)


def selection(result: dict, state: dict) -> str:
    tl = Timeline(state.get("signature_numerator"), state.get("signature_denominator"))
    lines = ["Ansicht: %s | Playhead %s | %s" % (result.get("focused_view"), tl.pos(result.get("current_song_time")),
                                                "laeuft" if result.get("is_playing") else "gestoppt")]
    st = result.get("selected_track")
    if st:
        lines.append("Ausgewaehlte Spur: [%s] %s" % (ref_label(st.get("ref")), st.get("name")))
    clip = result.get("detail_clip")
    if clip:
        where = "Arrangement" if clip.get("source") == "arrangement" else "Session-Slot"
        pos = ""
        if clip.get("start_time") is not None and clip.get("source") == "arrangement":
            pos = ", %s bis %s" % (tl.pos(clip.get("start_time")), tl.pos(clip.get("end_time")))
        lines.append("Clip in der Detailansicht: '%s' auf [%s] %s, %s #%s%s | %s" % (
            clip.get("name"), ref_label(clip.get("track")), clip.get("track_name"), where, clip.get("clip_index"), pos,
            _clip_flags(clip)))
    else:
        lines.append("Kein Clip in der Detailansicht.")
    p = result.get("selected_parameter")
    if p:
        lines.append("Ausgewaehlter Parameter: %s = %s (%s)%s" % (p.get("name"), p.get("display"), p.get("owner") or "?", _auto(p)))
    if result.get("selected_scene") is not None:
        lines.append("Ausgewaehlte Szene: %s" % result["selected_scene"])
    return "\n".join(lines)


def browser_items(result: dict) -> str:
    if result.get("error"):
        extra = result.get("available_categories")
        return "Fehler: %s%s" % (result["error"], (". Kategorien: " + ", ".join(extra)) if extra else "")
    items = result.get("items") or []
    lines = ["Browser '%s': %d Eintraege. uri fuer load_instrument_or_effect; [Ordner] mit Pfad weiter oeffnen."
             % (result.get("path"), len(items))]
    if result.get("is_loadable") and result.get("uri"):
        lines.append("Der Ordner selbst ist ladbar: uri=%s" % result["uri"])
    for it in items:
        tags = []
        if it.get("is_folder"):
            tags.append("Ordner")
        if it.get("is_device"):
            tags.append("Geraet")
        uri = (" uri=" + it["uri"]) if it.get("is_loadable") and it.get("uri") else ""
        lines.append("- %s%s%s" % (it.get("name"), (" [%s]" % ", ".join(tags)) if tags else "", uri))
    return "\n".join(lines)


# Eichung von Lives Meterskala (0..1) gegen dB, gemessen am 11.10.2026 gegen
# 19:45 an einer Bus-Spur eines echten Sets: Fader in Stufen von 0 bis -50 dB, je 4 s,
# Loop ab Takt 33, Spitzenwerte (watch_meters). Die Skala ist ueber den ganzen
# Bereich nahezu linear in dB (0,012 bis 0,014 je dB, Streuung je Messung
# ~0,003). Diese Punkte sind relativ (0 = die Bus-Spur bei 0-dB-Fader).
METER_CALIBRATION = [(0.106, -50.0), (0.240, -40.0), (0.369, -30.0), (0.437, -25.0), (0.499, -20.0),
                     (0.563, -15.0), (0.634, -10.0), (0.688, -6.0), (0.725, -3.0), (0.765, 0.0)]
# Etwas Spiel an den Raendern: Dieselbe Spur streute bei gleicher Einstellung
# zwischen 0,762 und 0,766.
METER_CAL_SLACK = 0.005

# Absoluter Bezug (11.10.2026 ~21:10): Lives
# Spitzenwert-Anzeige am Master von Hand abgelesen. Solo auf der Sub-Kick:
# Master = Sub = 0,657 bei "-20" (abgelesen gerundet). Voller Mix: -8,35 dBFS, watch_meters-
# Spitze ueber 30 s davor 0,812. Der Abstand der beiden (0,0133 je dB) passt
# zur Fader-Eichung; das traegt die Verbindung von relativ und absolut.
METER_DBFS_ANCHOR = (0.657, -20.0)
METER_DBFS_TOP = (0.812, -8.35)
# Steigung fuer das Stueck ueber 0,812 bis zur Oberkante 1,0. Dort gibt es
# keinen Messpunkt; jede Zahl ist gerechnet und wird so markiert. Darunter
# (unter 0,106) wird nicht gerechnet: Ob Lives Skala dort linear bleibt, ist
# offen, und Stille waere sonst eine Zahl.
METER_PER_DB = 0.0133


def meter_db(value: float | None) -> float | None:
    """Meterwert auf der relativen Fader-Eichung in dB, oder None ausserhalb davon."""
    if value is None:
        return None
    pts = METER_CALIBRATION
    if value < pts[0][0] - METER_CAL_SLACK or value > pts[-1][0] + METER_CAL_SLACK:
        return None
    for (v0, d0), (v1, d1) in zip(pts, pts[1:]):
        if value <= v1 or (v1, d1) == pts[-1]:
            return d0 + (value - v0) * (d1 - d0) / (v1 - v0)
    return None  # pragma: no cover


def _dbfs_points() -> list[tuple[float, float]]:
    offset = METER_DBFS_ANCHOR[1] - meter_db(METER_DBFS_ANCHOR[0])
    return [(m, d + offset) for m, d in METER_CALIBRATION] + [METER_DBFS_TOP]


METER_DBFS_POINTS = _dbfs_points()


def meter_dbfs_info(value: float | None) -> tuple[float | None, str]:
    """Meterwert -> (dBFS, Art). Art: 'gemessen' (zwischen Messpunkten),
    'gerechnet' (ueber 0,812 fortgesetzt), 'unter' (dBFS ist dann die
    Untergrenze -61,7) oder 'still' (0 bzw. kein Wert)."""
    if value is None or value <= 0.0:
        return None, "still"
    pts = METER_DBFS_POINTS
    if value < pts[0][0] - METER_CAL_SLACK:
        return pts[0][1], "unter"
    if value > pts[-1][0] + METER_CAL_SLACK:
        return pts[-1][1] + (value - pts[-1][0]) / METER_PER_DB, "gerechnet"
    for (v0, d0), (v1, d1) in zip(pts, pts[1:]):
        if value <= v1 or (v1, d1) == pts[-1]:
            if value == v1:
                # Auf einem Messpunkt genau dessen Wert, ohne Rundungsrest.
                return d1, "gemessen"
            return d0 + (value - v0) * (d1 - d0) / (v1 - v0), "gemessen"
    return None, "still"  # pragma: no cover


def meter_dbfs(value: float | None) -> float | None:
    d, kind = meter_dbfs_info(value)
    return d if kind in ("gemessen", "gerechnet") else None


def meter_db_diff(a: float | None, b: float | None) -> float | None:
    """dB-Abstand zweier Meterwerte; None, wenn einer still oder unter 0,106 ist."""
    da, dbb = meter_dbfs(a), meter_dbfs(b)
    return None if da is None or dbb is None else da - dbb


def _computed(*values) -> bool:
    return any(meter_dbfs_info(v)[1] == "gerechnet" for v in values)


def dbfs_text(value: float | None) -> str:
    d, kind = meter_dbfs_info(value)
    if kind == "still":
        return "-"
    if kind == "unter":
        return "unter %.1f dBFS" % d
    return "%s%+.1f dBFS" % ("~" if kind == "gerechnet" else "", d)


def _meter_flags(overview: dict | None) -> tuple[dict, list[str]]:
    """Stumm/Solo je Spur. Lives Meter sitzt VOR dem Spuraktivator: Eine stumme
    Spur schlaegt weiter aus (KICK BODY am 11.10.2026: 0,577 stumm wie laut).
    Ohne Markierung liest man daraus 'klingt'."""
    if not overview:
        return {}, []
    tracks = (overview.get("tracks") or []) + (overview.get("returns") or [])
    by_ref = {t["ref"]: t for t in tracks}

    def ancestors(t):
        out, parent, seen = [], t.get("group"), set()
        while parent is not None and parent in by_ref and parent not in seen:
            seen.add(parent)
            out.append(by_ref[parent])
            parent = by_ref[parent].get("group")
        return out

    # Solo (11.10.2026): Per Solo stumme Spuren schlagen weiter aus.
    # Hoerbar bleiben die Solo-Spuren, die Gruppen darueber und alles in einer
    # Solo-Gruppe. Returns bleiben in Live hoerbar, bekommen aber nur Sends
    # hoerbarer Spuren; sie werden deshalb nicht markiert.
    soloed = [t for t in overview.get("tracks") or [] if t.get("solo")]
    heard = set()
    for s in soloed:
        heard.add(s["ref"])
        heard.update(a["ref"] for a in ancestors(s))
    for t in overview.get("tracks") or []:
        if any(a["ref"] in {s["ref"] for s in soloed} for a in ancestors(t)):
            heard.add(t["ref"])
    flags: dict = {}
    for t in tracks:
        f = []
        if t.get("mute"):
            f.append("STUMM, Meter zeigt trotzdem Signal (vor dem Aktivator), hoerbar ist nichts")
        muted_parent = next((a for a in ancestors(t) if a.get("mute")), None)
        if muted_parent is not None:
            f.append("Gruppe %s STUMM" % muted_parent.get("name"))
        if t.get("solo"):
            f.append("SOLO")
        elif soloed and t.get("kind") != "return" and "return" not in str(t.get("ref")) and t["ref"] not in heard:
            f.append("per Solo stumm, nicht zu hoeren (Meter ignoriert Solo)")
        if f:
            flags[t["ref"]] = f
    notes = []
    all_soloed = [t["name"] for t in tracks if t.get("solo")]
    if all_soloed:
        notes.append("SOLO aktiv auf %s: Andere Spuren sind nicht zu hoeren, ihre Meter schlagen aber weiter aus "
                     ". Was zu hoeren ist, zeigen Gruppen und Master." % ", ".join(all_soloed))
    return flags, notes


def song_span(samples: list[dict], tl: Timeline) -> str | None:
    """Welcher Teil des Songs waehrend der Messung lief. Am 12.10.2026 lief eine
    Messung 'im Loop 33-49' in Wahrheit ueber Takt 1-18 (Intro), weil Live am
    Startmarker beginnt; aufgefallen ist es erst beim Zuhoeren."""
    times = [s.get("song_time") for s in samples if s.get("song_time") is not None]
    if not times:
        return None
    jumps = sum(1 for a, b in zip(times, times[1:]) if b < a - 0.5)
    text = "Song-Position waehrend der Messung: %s bis %s" % (tl.pos(min(times)), tl.pos(max(times)))
    if jumps:
        text += " (%d Ruecksprung%s, z. B. Loop)" % (jumps, "" if jumps == 1 else "e")
    return text + "."


def meters(samples: list[dict], seconds: float, overview: dict | None = None, reference=None,
           show_input: bool = False, tl: Timeline | None = None) -> str:
    if not samples:
        return "Keine Messwerte."
    first = samples[0]
    names = [(m["ref"], m["name"]) for m in first["meters"]]
    peaks, sums, peak_l, peak_r, in_peaks = {}, {}, {}, {}, {}
    for s in samples:
        for m in s["meters"]:
            left, right = m.get("left") or 0.0, m.get("right") or 0.0
            v = max(left, right)
            peaks[m["ref"]] = max(peaks.get(m["ref"], 0.0), v)
            sums[m["ref"]] = sums.get(m["ref"], 0.0) + v
            peak_l[m["ref"]] = max(peak_l.get(m["ref"], 0.0), left)
            peak_r[m["ref"]] = max(peak_r.get(m["ref"], 0.0), right)
            ins = [x for x in (m.get("in_left"), m.get("in_right")) if x is not None]
            if ins:
                in_peaks[m["ref"]] = max(in_peaks.get(m["ref"], 0.0), max(ins))
    kinds = {t["ref"]: t.get("kind") for t in ((overview or {}).get("tracks") or [])
             + ((overview or {}).get("returns") or [])}
    playing = any(s.get("is_playing") for s in samples)
    flags, notes = _meter_flags(overview)
    if reference is None or reference not in peaks:
        reference = "master"
    ref_name = dict(names).get(reference, "Master")
    ref_peak = peaks.get(reference)
    lo, hi = METER_DBFS_POINTS[0], METER_DBFS_POINTS[-1]
    lines = ["Meter ueber %.1f s, %d Abfragen, Transport %s. Rohwerte: Lives Meterskala 0..1."
             % (seconds, len(samples), "laeuft" if playing else "STEHT (Meter zeigen dann nur Ausklang)")]
    span = song_span(samples, tl or Timeline(4, 4))
    if span and playing:
        lines.append(span)
    lines.append("dBFS = Sample-Spitze wie Lives Spitzenwert-Anzeige (kein True Peak). Geeicht am 11.10.2026: "
                 "Fader-Stufen plus zwei am Master abgelesene Spitzen (%.3f = %.2f dBFS, %.3f = %.2f dBFS), "
                 "gemessen fuer Meter %.3f..%.3f (%.2f..%.2f dBFS). '~' = GERECHNET: darueber mit %.4f je dB "
                 "fortgesetzt bis 1.0 (= %+.1f dBFS), nicht gemessen. Unter %.3f nur 'unter %.1f'."
                 % (METER_DBFS_ANCHOR + METER_DBFS_TOP + (lo[0], hi[0], lo[1], hi[1], METER_PER_DB,
                                                         meter_dbfs_info(1.0)[0], lo[0], lo[1])))
    # Gegen Lives Spitzenwert-Anzeige: 11.10.2026 0,800 -> -8,37 (hier -9,2);
    # 12.10.2026 0,798 -> -8,71 (hier -9,3), 30 s Takt 1-18.
    lines.append("Abgefragt wird nur einige Male pro Sekunde; kurze Spitzen fehlen. Lives Spitzenwert-Anzeige lag "
                 "am 11./12.10.2026 0.6 bis 0.8 dB HOEHER als die Spitze hier.")
    lines.append("dB in Klammern = Abstand zur Spitze von [%s] %s; '?' wenn einer der Werte still oder unter "
                 "%.3f ist." % (ref_label(reference), ref_name, lo[0]))
    if "master" in peaks:
        d, kind = meter_dbfs_info(peaks["master"])
        if kind in ("gemessen", "gerechnet"):
            lines.append("MASTER  Spitze %s, bis 0 dBFS %s%.1f dB%s." % (
                dbfs_text(peaks["master"]), "~" if kind == "gerechnet" else "", -d,
                "" if d < 0 else " (UEBER 0 dBFS: Clipping moeglich)"))
    lines.extend("HINWEIS: " + n for n in notes)
    if show_input:
        # Am Set am 11.10.2026: KICK_PRINT (Eingang KICK BODY Post FX) zeigte
        # 0,000, RUMBLE (Ext. In, Monitoring Off) 0,735, LOW BUS 0,635, die
        # anderen Gruppen 0. MIDI-Spuren liefern nichts. Ungeklaert.
        lines.append("Eingang: BEDEUTUNG UNGEKLAERT (am Set 11.10.2026 widerspruechlich, z. B. Print-Spur 0 trotz "
                     "Eingang KICK BODY). Nicht zum Messen verwenden, nur zum Erkunden.")
    lines.append("Spur | Spitze (dB zur Bezugsspur) | Spitze dBFS | Mittel (dB zur Bezugsspur) | Spitze L / R%s | Status"
                 % (" | Eingang" if show_input else ""))

    def with_db(v):
        d = meter_db_diff(v, ref_peak)
        if d is None:
            return "%.3f (?)" % v
        return "%.3f (%s%+.1f)" % (v, "~" if _computed(v, ref_peak) else "", d)

    for ref, name in names:
        pk = peaks.get(ref, 0.0)
        avg = sums.get(ref, 0.0) / len(samples)
        status = list(flags.get(ref, []))
        if pk < 0.001:
            status.append("still")
        elif pk > 0.95:
            status.append("nahe Vollausschlag")
        left, right = peak_l.get(ref, 0.0), peak_r.get(ref, 0.0)
        lr = meter_db_diff(left, right)
        if lr is not None and abs(lr) >= 1.0:
            status.append("Schieflage %s um %s%.1f dB" % ("L lauter" if lr > 0 else "R lauter",
                                                          "~" if _computed(left, right) else "", abs(lr)))
        elif lr is None and abs(left - right) >= 0.02:
            status.append("L/R ungleich (unter der Eichung, nur Rohwert)")
        inp = ""
        if show_input:
            if ref not in in_peaks:
                inp = " | -"
            elif kinds.get(ref) == "midi":
                inp = " | MIDI %.3f" % in_peaks[ref]
            else:
                inp = " | %.3f" % in_peaks[ref]
        lines.append("[%s] %s | %s | %s | %s | %.3f / %.3f%s%s" % (
            ref_label(ref), name, with_db(pk), dbfs_text(pk) if pk >= 0.001 else "-", with_db(avg), left, right,
            inp, (" | " + "; ".join(status)) if status else ""))
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Clip-Huellkurven (Paket Automation, technoa.9)
# ----------------------------------------------------------------------

# Abweichung, bis zu der abgetastete Werte noch als eine gerade Strecke
# gelten, als Anteil des Wertebereichs. Eine Rampe aus Stufen (so schreibt
# set_clip_envelope) hinkt der Geraden um bis zu eine Stufe hinterher, weil
# Live den Grenzpunkt zur Stufe davor zaehlt (am Set 27.10.2026): bei 64
# Stufen ueber den ganzen Bereich 1,6 %. Mit 0,5 % zerfiel die Rampe -inf bis
# 0 dB in Rampe, Sprung und eine Mini-Rampe am Ende.
ENVELOPE_TOLERANCE = 0.02
GRID_NAMES = {1.0: "1/4", 0.5: "1/8", 0.25: "1/16", 0.125: "1/32", 0.0625: "1/64"}


def grid_name(grid: float) -> str:
    return GRID_NAMES.get(round(float(grid), 6), "%g Beats" % grid)


def envelope_breakpoints(values: list, span: float, tol_fraction: float = ENVELOPE_TOLERANCE) -> list:
    """Abgetastete Werte -> Indizes der Knickpunkte (gierig, gerade Strecken).

    Lives API liefert nur value_at_time; die Punkte der Kurve muessen aus
    dem Raster zurueckgewonnen werden. Eine Strecke reicht so weit, wie alle
    Zwischenwerte hoechstens tol vom Verbindungsstueck abweichen."""
    n = len(values)
    if n <= 2:
        return list(range(n))
    tol = max(abs(span) * tol_fraction, 1e-9)
    points = [0]
    a = 0
    while a < n - 1:
        b = a + 1
        while b + 1 < n:
            c = b + 1
            va, vc = values[a], values[c]
            if any(abs(values[k] - (va + (vc - va) * (k - a) / float(c - a))) > tol for k in range(a + 1, c)):
                break
            b = c
        points.append(b)
        a = b
    return points


def _clip_song_pos(clip: dict, t: float, tl: "Timeline") -> str | None:
    """Clip-Zeit -> Songposition, nur fuer Arrangement-Clips im ersten Durchlauf."""
    start = clip.get("start_time")
    if start is None:
        return None
    marker = clip.get("start_marker") or 0.0
    if t < marker - 1e-9:
        return "vor Startmarker"
    first_end = clip.get("loop_end") if clip.get("looping") else clip.get("end_marker")
    if first_end is not None and t > first_end + 1e-9:
        return None
    return tl.pos(start + (t - marker))


def envelope_rows(samples: dict, clip: dict, tl: "Timeline", lo_v: float, hi_v: float,
                  max_rows: int = 40) -> list:
    """Kurve als Zeilen: ab einer Position (Song und Clip) ein Wert und wie es bis
    zur naechsten Zeile weitergeht (gehalten, Rampe, Sprung)."""
    values = samples.get("values") or []
    if not values:
        return ["  (keine Werte gelesen)"]
    displays = samples.get("displays") or [None] * len(values)
    t0, grid = float(samples.get("from") or 0.0), float(samples.get("grid") or 0.25)
    end = samples.get("to")
    n = len(values)
    span = (hi_v - lo_v) if hi_v is not None and lo_v is not None else 1.0
    idx = envelope_breakpoints(values, span)
    span_tol = abs(span) * ENVELOPE_TOLERANCE

    def where(i):
        t = t0 + i * grid
        song = _clip_song_pos(clip, t, tl)
        clip_pos = "Clip %s" % tl.pos(t)
        return "%s (%s)" % (song, clip_pos) if song else clip_pos

    def until(i):
        # Der letzte Rasterpunkt liegt eine Rasterweite vor dem Clipende; die
        # Strecke reicht aber bis dorthin.
        if i >= n - 1 and end is not None:
            t = float(end)
            return "Clip-Ende (%s)" % (_clip_song_pos(clip, t, tl) or "Clip %s" % tl.pos(t))
        t = t0 + i * grid
        return _clip_song_pos(clip, t, tl) or "Clip %s" % tl.pos(t)

    def shown(i):
        return displays[i] if displays[i] is not None else "%g" % values[i]

    segs = []
    for a, b in zip(idx, idx[1:]):
        kind = "hold" if abs(values[b] - values[a]) <= span_tol else ("jump" if b == a + 1 else "ramp")
        segs.append((a, b, kind))
    rows = []
    if not segs:
        rows.append("  %s: %s, gleichbleibend bis %s" % (where(0), shown(0), until(n - 1)))
    i = 0
    while i < len(segs):
        a, b, kind = segs[i]
        # Gehalten, dann Sprung: Der Rasterpunkt vor dem Sprung ist keine
        # eigene Zeile wert, er saehe aus wie der Ort des Sprungs.
        if kind == "hold" and i + 1 < len(segs) and segs[i + 1][2] == "jump":
            b = segs[i + 1][1]
            text = "gehalten bis %s, dort Sprung" % until(b)
            kind = "jump"
            i += 1
        else:
            text = {"hold": "gehalten bis %s", "jump": "Sprung bei %s", "ramp": "Rampe bis %s"}[kind] % until(b)
        if b == n - 1 and kind != "hold":
            text += " auf %s" % shown(b)
        rows.append("  %s: %s, %s" % (where(a), shown(a), text))
        i += 1
    if len(rows) > max_rows:
        cut = len(rows) - max_rows
        rows = rows[:max_rows] + ["  ... %d weitere Punkte (resolution groeber waehlen oder Bereich lesen)" % cut]
    return rows


def _envelope_target_text(target: dict) -> str:
    return (target or {}).get("label") or "?"


def _clip_head(result: dict, tl: "Timeline") -> str:
    clip = result.get("clip") or {}
    where = "Arrangement-Clip #%s" % clip.get("index") if result.get("source") == "arrangement" else \
        "Session-Clip Slot %s" % clip.get("index")
    span = ""
    if clip.get("start_time") is not None:
        span = ", %s bis %s" % (tl.pos(clip.get("start_time")), tl.pos(clip.get("end_time")))
    loop = ", Clip-Loop %s bis %s Beats" % (clip.get("loop_start"), clip.get("loop_end")) if clip.get("looping") \
        else ", ohne Loop"
    return "[%s] %s | %s '%s'%s%s" % (ref_label(result.get("track")), result.get("track_name"), where,
                                      clip.get("name") or "", span, loop)


def _range_text(param: dict) -> str:
    return "%s bis %s" % (param.get("min"), param.get("max"))


def envelope_view(result: dict, state: dict) -> str:
    """Antwort von get_clip_envelope als Text."""
    tl = Timeline(state.get("signature_numerator"), state.get("signature_denominator"))
    clip = result.get("clip") or {}
    lines = [_clip_head(result, tl)]
    if "envelopes" in result:
        envs = result["envelopes"]
        if not envs:
            lines.append("Keine Huellkurve an Mixer- oder Geraeteparametern gefunden%s." % (
                " (Live meldet aber Clip-Automation: vermutlich MIDI-Controller wie Pitch Bend oder tiefer als 3 "
                "Ebenen im Rack; die liest die API nicht)" if clip.get("has_envelopes") else ""))
        for e in envs:
            p = e.get("param") or {}
            lines.append("Huellkurve %s (Reglerwert jetzt %s):" % (e.get("label"), p.get("display")))
            lines += envelope_rows(e.get("samples") or {}, clip, tl, p.get("min"), p.get("max"))
        if envs:
            lines.append(_grid_note(envs[0].get("samples") or {}))
    else:
        p = result.get("param") or {}
        label = _envelope_target_text(result.get("target"))
        if not result.get("exists"):
            lines.append("Keine Huellkurve fuer %s in diesem Clip. Reglerwert: %s." % (label, p.get("display")))
        else:
            lines.append("Huellkurve %s (Reglerwert jetzt %s, Bereich roh %s):" % (label, p.get("display"),
                                                                                  _range_text(p)))
            lines += envelope_rows(result.get("samples") or {}, clip, tl, p.get("min"), p.get("max"))
            lines.append(_grid_note(result.get("samples") or {}))
    lines.append(_envelope_time_note(result))
    if result.get("api"):
        lines.append("API (probe): " + json.dumps(result["api"], ensure_ascii=False))
    return "\n".join(lines)


def _grid_note(samples: dict) -> str:
    return ("Gelesen im Raster %s: Lives API nennt keine Knickpunkte, nur Werte je Zeitpunkt. Positionen sind "
            "deshalb auf %s genau." % (grid_name(samples.get("grid") or 0.25), grid_name(samples.get("grid") or 0.25)))


def _envelope_time_note(result: dict) -> str:
    if result.get("source") == "arrangement":
        return ("Positionen: Songtakt, in Klammern Takt im Clip (1.1.1 = Clipanfang). Clip-Huellkurven wiederholen "
                "sich mit dem Clip-Loop.")
    return "Positionen: Takt im Clip (1.1.1 = Clipanfang). Clip-Huellkurven wiederholen sich mit dem Clip-Loop."
