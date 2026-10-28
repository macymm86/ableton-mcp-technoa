"""Schnappschuss des Sets und Vergleich zweier Schnappschuesse (Paket E).

Gemeinsam fuer den MCP-Server (snapshot, snapshot_diff, mark_undo,
undo_to_mark) und die Kommandozeile:

  set_snapshot.py snapshot <out.json>             alles Lesbare ablegen
  set_snapshot.py diff <a.json> <b.json> [zeilen] Unterschiede, wichtigste zuerst (0 = alle, Standard)
  set_snapshot.py time                            Antwortzeiten kleiner Lesebefehle

Format 2 (seit 22.10.2026) haelt zusaetzlich Playhead, Transport, Zeitpunkt,
Setdatei und die Locator-Positionen fest. Format 1 hat jedes Feld namens
"time" verworfen und damit auch die Locator-Zeiten. Alte
Schnappschuesse bleiben vergleichbar, nur ohne diese Angaben.

Abgeglichen wird nach Identitaet, nicht nach Listenplatz: Spuren nach Namen,
Arrangement-Clips nach Startposition, Geraete als Kette. Am 19.10.2026 hatte
ein einzelner neuer Clip im alten Vergleich 320 Zeilen Rauschen erzeugt.
"""
from __future__ import annotations

import difflib
import json
import os
import socket
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import views  # noqa: E402

FORMAT = 2
# Rohwerte, die bei gleicher Anzeige um hoechstens diesen Anteil des
# Wertebereichs abweichen, gelten als gleich: set_mixer(volume_db=0) ergab
# 0,849987 statt 0,85000002, Anzeige beide Male 0.0 dB.
TOLERANCE = 1e-4
# Bewegt sich beim Lesen ohnehin. Playhead und Transport stehen in Format 2
# eigens unter "playhead".
VOLATILE = {"current_song_time", "is_playing", "meter"}

# Reihenfolge der Ausgabe: Was am Set unbemerkt am meisten anrichtet, zuerst.
# Am 19.10.2026 fand ein Vergleich den verrutschten DRUMS-Ausgang erst
# zwischen Hunderten Clipzeilen.
CATEGORIES = ["SPUREN", "ROUTING", "GERAETE", "AUTOMATION", "MIXER", "SONG", "PARAMETER", "CLIPS", "ANSICHT"]
INFO_CATEGORIES = ["FOLGT AUTOMATION", "TOLERANZ", "UNDO-ZUSTAND"]


# ======================================================================
# Aufnehmen
# ======================================================================

def _strip(o):
    if isinstance(o, dict):
        return {k: _strip(v) for k, v in o.items() if k not in VOLATILE}
    if isinstance(o, list):
        return [_strip(x) for x in o]
    return o


def take(call: Callable[..., Any], label: str = "", extra: Optional[dict] = None) -> dict:
    """Liest das Set ueber call(befehl, params) und gibt den Schnappschuss zurueck.

    Rund 100 Lesebefehle; am Set 07.10.2026 7,8 s fuer 1113 Clips. Laeuft der
    Transport, gehoeren die Werte zu verschiedenen Zeitpunkten, deshalb steht
    der Playhead vom Anfang und vom Ende drin."""
    started = time.monotonic()
    song = call("get_song_state")
    overview = call("get_tracks_overview")
    tracks: Dict[str, dict] = {}
    order: List[str] = []
    for t in overview["tracks"] + overview["returns"] + [overview["master"]]:
        ref = t["ref"]
        info = call("get_track_info", {"track": ref})
        d = {"info": info}
        if t["kind"] in ("midi", "audio"):
            d["clips"] = call("get_arrangement_clips", {"track": ref, "include_digest": True})["clips"]
        d["devices"] = [call("get_device_parameters", {"track": ref, "device_index": dev["index"]})["device"]
                        for dev in info.get("devices") or []]
        tracks[str(ref)] = d
        order.append(str(ref))
    end = call("get_session_info")
    snap = {
        "format": FORMAT,
        "taken": time.strftime("%Y-%m-%d %H:%M:%S"),
        "label": label,
        "playhead": {"beat": song.get("current_song_time"), "playing": bool(song.get("is_playing")),
                     "beat_end": end.get("current_song_time"), "playing_end": bool(end.get("is_playing"))},
        "seconds": round(time.monotonic() - started, 1),
        "script_version": _safe_call(call, "get_script_info", {}, "script_version"),
        "set_file": _safe_call(call, "inspect_lom", {"path": "song.file_path", "list_attributes": False}, "str"),
        "song": _strip(song),
        "order": order,
        "tracks": _strip(tracks),
    }
    snap.update(extra or {})
    return snap


def _safe_call(call, command, params, key):
    # Nur Herkunftsangaben; ein Fehler hier soll den Schnappschuss nicht kosten.
    try:
        res = call(command, params)
    except Exception:  # noqa: BLE001
        return None
    return res.get(key) if isinstance(res, dict) else None


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        snap = json.load(fh)
    snap.setdefault("format", 1)
    if not snap.get("order"):
        snap["order"] = sorted(snap.get("tracks") or {}, key=_ref_sort)
    return snap


def save(snap: dict, path: str):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, indent=1, ensure_ascii=False, sort_keys=True)


def summary(snap: dict) -> str:
    tracks = snap.get("tracks") or {}
    n_clips = sum(len(v.get("clips", [])) for v in tracks.values())
    n_par = sum(len(d.get("parameters", [])) for v in tracks.values() for d in v.get("devices", []))
    return "%d Spuren/Returns/Master, %d Arrangement-Clips, %d Geraeteparameter, %d Locator" % (
        len(tracks), n_clips, n_par, len((snap.get("song") or {}).get("cue_points") or []))


def playhead_text(snap: dict) -> str:
    ph = snap.get("playhead")
    if not ph or ph.get("beat") is None:
        return "Playhead nicht festgehalten (Format 1, vor 22.10.2026)"
    tl = _timeline(snap)
    text = "Playhead %s, Transport %s" % (tl.pos(ph["beat"]), "laeuft" if ph.get("playing") else "steht")
    end = ph.get("beat_end")
    if ph.get("playing") or ph.get("playing_end"):
        text += " (am Ende des Lesens %s)" % tl.pos(end)
    return text


def _timeline(snap: dict) -> views.Timeline:
    song = snap.get("song") or {}
    return views.Timeline(song.get("signature_numerator"), song.get("signature_denominator"))


def _ref_sort(key: str):
    if key == "master":
        return (2, 0)
    if key.startswith("return:"):
        return (1, int(key.split(":", 1)[1]))
    try:
        return (0, int(key))
    except ValueError:
        return (3, 0)


def _ref(key: str):
    return int(key) if key.lstrip("-").isdigit() else key


# ======================================================================
# Vergleichen
# ======================================================================

class Entry:
    """Ein Unterschied. area und refs/names sagen, wo er liegt; danach prueft
    undo_to_mark, ob ein MCP-Schritt ihn erklaert. undo=False heisst: Live legt
    dafuer keinen Undo-Schritt an (Solo, Arm, Loop ...), er kann also nicht
    zwischen MCP-Schritten in der gemeinsamen Undo-Liste liegen."""

    __slots__ = ("cat", "text", "area", "refs", "names", "undo", "automation", "group", "count")

    def __init__(self, cat, text, area, refs=(), names=(), undo=True, automation=False, group=None, count=1):
        self.cat = cat
        self.text = text
        self.area = area
        self.refs = set(refs)
        self.names = set(n for n in names if n)
        self.undo = undo
        self.automation = automation
        self.group = group
        self.count = count

    def __repr__(self):  # pragma: no cover
        return "Entry(%s: %s)" % (self.cat, self.text)


class Report:
    def __init__(self, entries: List[Entry], a: dict, b: dict, notes: List[str]):
        self.entries = entries
        self.a = a
        self.b = b
        self.notes = notes

    def counted(self) -> List[Entry]:
        return [e for e in self.entries if e.cat in CATEGORIES]

    def info(self) -> List[Entry]:
        return [e for e in self.entries if e.cat in INFO_CATEGORIES]


class _Ctx:
    def __init__(self, a, b, tol):
        self.a, self.b, self.tol = a, b, tol
        self.tl = _timeline(b)
        self.entries: List[Entry] = []
        pa, pb = a.get("playhead") or {}, b.get("playhead") or {}
        self.ph_a = pa.get("beat")
        self.ph_b = pb.get("beat")
        # Nur bei stehendem Transport heisst gleicher Playhead gleicher
        # Automationswert; laeuft er, liest jede Spur einen anderen Moment.
        self.ph_same = (self.ph_a is not None and self.ph_b is not None and abs(self.ph_a - self.ph_b) < 1e-3
                        and not any(p.get(k) for p in (pa, pb) for k in ("playing", "playing_end")))
        self.sidechain_partial = False
        self.envelopes_partial = False

    def add(self, *args, **kwargs):
        self.entries.append(Entry(*args, **kwargs))


def compare(a: dict, b: dict, tol: float = TOLERANCE) -> Report:
    ctx = _Ctx(a, b, tol)
    notes = []
    if a.get("format", 1) < 2 or b.get("format", 1) < 2:
        notes.append("Format 1 beteiligt: Playhead und Locator-Positionen sind dort nicht festgehalten.")
    _cmp_song(ctx, a.get("song") or {}, b.get("song") or {})
    la = [(k, a["tracks"][k]) for k in a.get("order") or sorted(a["tracks"], key=_ref_sort) if k in a["tracks"]]
    lb = [(k, b["tracks"][k]) for k in b.get("order") or sorted(b["tracks"], key=_ref_sort) if k in b["tracks"]]
    pairs, removed, added = _match_tracks(la, lb)
    ta, tb = dict(la), dict(lb)
    for ka in removed:
        info = ta[ka]["info"]
        ctx.add("SPUREN", "Spur entfernt: [%s] %s (%s)" % (views.ref_label(_ref(ka)), info.get("name"),
                                                         views.KIND_LABELS.get(info.get("kind"), "?")),
                "structure", refs=[_ref(ka)], names=[info.get("name")])
    for kb in added:
        info = tb[kb]["info"]
        ctx.add("SPUREN", "Spur neu: [%s] %s (%s)" % (views.ref_label(_ref(kb)), info.get("name"),
                                                    views.KIND_LABELS.get(info.get("kind"), "?")),
                "structure", refs=[_ref(kb)], names=[info.get("name")])
    if not removed and not added:
        # Nur ohne neue oder entfernte Spuren ist eine andere Nummer ein Umsortieren.
        for ka, kb in pairs:
            if ka != kb:
                info = tb[kb]["info"]
                ctx.add("SPUREN", "%s steht jetzt an [%s] statt [%s]" % (info.get("name"), views.ref_label(_ref(kb)),
                                                                       views.ref_label(_ref(ka))),
                        "structure", refs=[_ref(ka), _ref(kb)], names=[info.get("name")])
    for ka, kb in pairs:
        _cmp_track(ctx, ka, ta[ka], kb, tb[kb])
    if ctx.envelopes_partial:
        notes.append("Clip-Huellkurven: Bei mindestens einem Clip fehlt in einem Schnappschuss der Abdruck (Script "
                     "aelter als technoa.9 oder nicht lesbar); dort nur 'Clip-Huellkurven ja/nein' verglichen.")
    if ctx.sidechain_partial:
        notes.append("Sidechain-Quellen stehen nur in einem der beiden Schnappschuesse (erst ab Script technoa.7); "
                     "dort nicht verglichen.")
    return Report(ctx.entries, a, b, notes)


def _match_tracks(la, lb):
    """Spuren zuerst nach eindeutigem Namen, dann nach Nummer (= umbenannt)."""
    a, b = dict(la), dict(lb)
    kind = lambda t: (t.get("info") or {}).get("kind")  # noqa: E731
    pairs = []
    if "master" in a and "master" in b:
        pairs.append(("master", "master"))

    def unique(lst):
        seen: Dict[Any, List[str]] = {}
        for k, t in lst:
            if k != "master":
                seen.setdefault((t.get("info") or {}).get("name"), []).append(k)
        return {n: ks[0] for n, ks in seen.items() if len(ks) == 1}

    ua, ub = unique(la), unique(lb)
    for name, ka in ua.items():
        kb = ub.get(name)
        if kb is not None and kind(a[ka]) == kind(b[kb]):
            pairs.append((ka, kb))
    done_a = {p[0] for p in pairs}
    done_b = {p[1] for p in pairs}
    for ka, t in la:
        if ka not in done_a and ka in b and ka not in done_b and kind(t) == kind(b[ka]):
            pairs.append((ka, ka))
            done_a.add(ka)
            done_b.add(ka)
    removed = [k for k, _ in la if k not in done_a]
    added = [k for k, _ in lb if k not in done_b]
    pairs.sort(key=lambda p: _ref_sort(p[1]))
    return pairs, removed, added


def _yn(v) -> str:
    return "ja" if v else "nein"


def _onoff(v) -> str:
    return "an" if v else "aus"


AUTOMATION_WORDS = {None: "keine Automation", "active": "Automation aktiv",
                    "overridden": "Automation UEBERSTEUERT"}


def _cmp_param(ctx: _Ctx, cat: str, area: str, where: str, pa: dict, pb: dict, refs, names, group=None):
    va, vb = pa.get("value"), pb.get("value")
    da, db = pa.get("display"), pb.get("display")
    aa, ab = pa.get("automation"), pb.get("automation")
    if va == vb and da == db and aa == ab:
        return
    if aa != ab:
        text = "%s: %s -> %s" % (where, AUTOMATION_WORDS.get(aa, aa), AUTOMATION_WORDS.get(ab, ab))
        if da != db:
            text += " (Wert %s -> %s)" % (da, db)
        if ab == "overridden":
            text += "; zurueck mit re_enable_automation"
        ctx.add("AUTOMATION", text, area, refs, names, automation=True, group=group)
        return
    lo, hi = pb.get("min"), pb.get("max")
    span = abs(hi - lo) if lo is not None and hi is not None and hi != lo else 1.0
    within = va is not None and vb is not None and abs(va - vb) <= ctx.tol * span
    if aa == "active":
        if ctx.ph_same:
            ctx.add("AUTOMATION", "%s folgt Automation, ist aber bei gleichem Playhead (%s, Transport steht) "
                    "anders: %s -> %s. Automationskurve geaendert?" % (where, ctx.tl.pos(ctx.ph_b), da, db),
                    area, refs, names, automation=True, group=group)
        else:
            ctx.add("FOLGT AUTOMATION", "%s %s -> %s" % (where, da, db), area, refs, names, undo=False,
                    automation=True)
        return
    if da == db and within:
        ctx.add("TOLERANZ", "%s %.6g -> %.6g (Anzeige %s)" % (where, va, vb, da), area, refs, names, undo=False)
        return
    if da == db:
        text = "%s: Anzeige gleich '%s', Rohwert %.6g -> %.6g" % (where, da, va, vb)
    else:
        text = "%s: %s -> %s" % (where, da, db)
    ctx.add(cat, text, area, refs, names, group=group)


def _cmp_track(ctx: _Ctx, ka, ta, kb, tb):
    ia, ib = ta.get("info") or {}, tb.get("info") or {}
    name_a, name_b = ia.get("name"), ib.get("name")
    refs, names = [_ref(ka), _ref(kb)], [name_a, name_b]
    label = "[%s] %s" % (views.ref_label(_ref(kb)), name_b)
    if name_a != name_b:
        ctx.add("SPUREN", "[%s] umbenannt: '%s' -> '%s'" % (views.ref_label(_ref(kb)), name_a, name_b),
                "name", refs, names)
    ga, gb = ia.get("group_name") if ia.get("is_grouped") else None, ib.get("group_name") if ib.get("is_grouped") else None
    if ga != gb:
        ctx.add("SPUREN", "%s: Gruppe %s -> %s" % (label, ga or "keine", gb or "keine"), "structure", refs, names)
    # Routing
    ra, rb = ia.get("routing") or {}, ib.get("routing") or {}
    for key, what in (("output_type", "Ausgang"), ("output_channel", "Ausgangskanal"),
                      ("input_type", "Eingang"), ("input_channel", "Eingangskanal")):
        if ra.get(key) != rb.get(key):
            ctx.add("ROUTING", "%s: %s %s -> %s" % (label, what, ra.get(key) or "-", rb.get(key) or "-"),
                    "routing", refs, names)
    # Mixer
    ma, mb = ia.get("mixer") or {}, ib.get("mixer") or {}
    for key, what in (("volume", "Pegel"), ("panning", "Pan"), ("track_activator", "Aktivator")):
        if ma.get(key) and mb.get(key):
            _cmp_param(ctx, "MIXER", "mixer", "%s %s" % (label, what), ma[key], mb[key], refs, names)
    sa = {s.get("index"): s for s in ma.get("sends") or []}
    sb = {s.get("index"): s for s in mb.get("sends") or []}
    for i in sorted(set(sa) & set(sb)):
        _cmp_param(ctx, "MIXER", "mixer", "%s Send %s (%s)" % (label, chr(65 + i), sb[i].get("name")),
                   sa[i], sb[i], refs, names)
    if len(sa) != len(sb):
        ctx.add("MIXER", "%s: Sends %d -> %d (Return-Spur dazu oder weg)" % (label, len(sa), len(sb)),
                "structure", refs, names)
    if ma.get("crossfade_assign") != mb.get("crossfade_assign"):
        ctx.add("MIXER", "%s Crossfader-Zuweisung %s -> %s" % (label, ma.get("crossfade_assign"),
                                                             mb.get("crossfade_assign")), "mixer", refs, names)
    for key, what, undo in (("mute", "Mute", True), ("solo", "Solo", False), ("arm", "Arm", False)):
        if key in ia and key in ib and ia[key] != ib[key]:
            ctx.add("MIXER", "%s %s %s -> %s%s" % (label, what, _onoff(ia[key]), _onoff(ib[key]),
                                                   "" if undo else " (kein Undo-Schritt)"),
                    "mixer", refs, names, undo=undo)
    if "monitoring" in ia and "monitoring" in ib and ia["monitoring"] != ib["monitoring"]:
        ctx.add("MIXER", "%s Monitoring %s -> %s" % (label, views.MONITORING.get(ia["monitoring"], ia["monitoring"]),
                                                     views.MONITORING.get(ib["monitoring"], ib["monitoring"])),
                "mixer", refs, names)
    # Ansicht
    if ia.get("color_index") != ib.get("color_index"):
        ctx.add("ANSICHT", "%s Farbe %s -> %s" % (label, ia.get("color_index"), ib.get("color_index")),
                "view", refs, names)
    for key, what in (("fold_state", "Gruppe eingeklappt"), ("is_visible", "sichtbar")):
        if key in ia and key in ib and ia[key] != ib[key]:
            ctx.add("ANSICHT", "%s %s %s -> %s (kein Undo-Schritt)" % (label, what, _yn(ia[key]), _yn(ib[key])),
                    "view", refs, names, undo=False)
    _cmp_devices(ctx, label, refs, names, ta.get("devices") or [], tb.get("devices") or [], kb)
    _cmp_clips(ctx, label, refs, names, ta.get("clips") or [], tb.get("clips") or [], arrangement=True)
    sca = [c for c in ia.get("session_clips") or [] if isinstance(c, dict)]
    scb = [c for c in ib.get("session_clips") or [] if isinstance(c, dict)]
    _cmp_clips(ctx, label, refs, names, sca, scb, arrangement=False)


# ----------------------------------------------------------------------
# Geraete
# ----------------------------------------------------------------------

def _dev_key(d):
    # Plugins haben alle class_name "PluginDevice"; erst class_display_name
    # unterscheidet Serum von Diva. name ist der umbenennbare Titel.
    return (d.get("class_name"), d.get("class_display_name"), d.get("name"))


def _dev_on(d) -> Optional[bool]:
    for p in d.get("parameters") or []:
        if p.get("name") == "Device On":
            return bool(p.get("value"))
    return d.get("is_active")


def _cmp_devices(ctx: _Ctx, label, refs, names, da: List[dict], db: List[dict], kb):
    sm = difflib.SequenceMatcher(None, [_dev_key(d) for d in da], [_dev_key(d) for d in db], autojunk=False)
    pairs: List[Tuple[int, int]] = []
    removed: List[int] = []
    added: List[int] = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            pairs.extend(zip(range(i1, i2), range(j1, j2)))
        elif op == "delete":
            removed.extend(range(i1, i2))
        elif op == "insert":
            added.extend(range(j1, j2))
        else:
            n = min(i2 - i1, j2 - j1)
            for i, j in zip(range(i1, i1 + n), range(j1, j1 + n)):
                if _dev_key(da[i])[:2] == _dev_key(db[j])[:2]:
                    pairs.append((i, j))
                else:
                    removed.append(i)
                    added.append(j)
            removed.extend(range(i1 + n, i2))
            added.extend(range(j1 + n, j2))
    moved = []
    for i in list(removed):
        for j in list(added):
            if _dev_key(da[i]) == _dev_key(db[j]):
                moved.append((i, j))
                removed.remove(i)
                added.remove(j)
                break
    area = "devices"
    for i in removed:
        ctx.add("GERAETE", "%s: Geraet entfernt, war Position %d '%s'" % (label, i, da[i].get("name")),
                area, refs, names)
    for j in added:
        d = db[j]
        cls = d.get("class_display_name")
        ctx.add("GERAETE", "%s: Geraet neu an Position %d '%s'%s" % (
            label, j, d.get("name"), " (%s)" % cls if cls and cls != d.get("name") else ""), area, refs, names)
    for i, j in moved:
        ctx.add("GERAETE", "%s: '%s' verschoben, Position %d -> %d" % (label, db[j].get("name"), i, j),
                area, refs, names)
    for i, j in sorted(pairs + moved, key=lambda p: p[1]):
        a, b = da[i], db[j]
        where = "%s / %d %s" % (label, j, b.get("name"))
        if a.get("name") != b.get("name"):
            ctx.add("GERAETE", "%s: Geraet %d umbenannt '%s' -> '%s'" % (label, j, a.get("name"), b.get("name")),
                    area, refs, names)
        on_a, on_b = _dev_on(a), _dev_on(b)
        if on_a != on_b:
            ctx.add("GERAETE", "%s: %s -> %s" % (where, _onoff(on_a), _onoff(on_b)), area, refs, names)
        if a.get("parameter_count") != b.get("parameter_count"):
            ctx.add("GERAETE", "%s: Parameterzahl %s -> %s" % (where, a.get("parameter_count"),
                                                              b.get("parameter_count")), area, refs, names)
        if (a.get("chains") or []) != (b.get("chains") or []):
            ctx.add("GERAETE", "%s: Ketten im Rack geaendert (Geraete in Racks vergleicht der Schnappschuss nicht "
                    "im Einzelnen)" % where, area, refs, names)
        if "sidechain" in a and "sidechain" in b:
            sa, sb = a.get("sidechain") or {}, b.get("sidechain") or {}
            if (sa.get("type"), sa.get("channel")) != (sb.get("type"), sb.get("channel")):
                # Unter ROUTING, weil so etwas unbemerkt am meisten anrichtet; der
                # Bereich bleibt "devices", denn set_sidechain gehoert zum Geraet.
                ctx.add("ROUTING", "%s: Sidechain-Quelle %s -> %s" % (where, _sc_text(sa), _sc_text(sb)), area,
                        refs, names)
        elif "sidechain" in a or "sidechain" in b:
            ctx.sidechain_partial = True
        _cmp_params(ctx, where, refs, names, a.get("parameters") or [], b.get("parameters") or [], (kb, j))


def _sc_text(sc: dict) -> str:
    return "%s / %s" % (sc.get("type") or "-", sc.get("channel") or "-")


def _cmp_params(ctx: _Ctx, where, refs, names, pa: List[dict], pb: List[dict], group):
    by_name_a: Dict[str, dict] = {}
    for p in pa:
        by_name_a.setdefault(p.get("name"), p)
    seen = set()
    missing = []
    for k, p in enumerate(pb):
        name = p.get("name")
        # Erst am selben Platz, dann nach Namen: Die Liste eines Geraets ist
        # fest, aber Plugins koennen sie nach "Configure" erweitern.
        q = pa[k] if k < len(pa) and pa[k].get("name") == name else by_name_a.get(name)
        if q is None:
            if name != "Device On":
                missing.append(name)
            continue
        seen.add(id(q))
        if name == "Device On":
            # Steht schon als "an -> aus" unter GERAETE.
            continue
        _cmp_param(ctx, "PARAMETER", "devices", "%s / %s" % (where, name), q, p, refs, names, group=group)
    gone = [p.get("name") for p in pa if id(p) not in seen and p.get("name") != "Device On"]
    if missing or gone:
        parts = []
        if missing:
            parts.append("neu: " + _short_list(missing))
        if gone:
            parts.append("weg: " + _short_list(gone))
        ctx.add("GERAETE", "%s: Parameter %s" % (where, "; ".join(parts)), "devices", refs, names)


def _short_list(items, n=5):
    items = [str(x) for x in items]
    return ", ".join(items[:n]) + (" ... (%d)" % len(items) if len(items) > n else "")


# ----------------------------------------------------------------------
# Clips
# ----------------------------------------------------------------------

CLIP_IGNORE = {"index", "is_playing", "is_triggered", "is_midi", "is_audio", "start_time", "envelopes"}


def _known_envelopes(c: dict) -> Optional[dict]:
    """Abdruck der Huellkurven {Beschriftung: Abdruck}; None = unbekannt.

    Ab technoa.9 steht "envelopes" an jedem Clip mit has_envelopes. Fehlt es
    dort, war das Script aelter oder Lesen nicht moeglich; das ist nicht
    dasselbe wie "keine Huellkurve"."""
    if not c.get("has_envelopes"):
        return {}
    # Leer trotz has_envelopes: nicht lesbar (Arrangement-Clip oder
    # MIDI-Controller), also unbekannt.
    return c.get("envelopes") or None


def _envelope_changes(ctx: "_Ctx", a: dict, b: dict) -> Optional[List[str]]:
    ea, eb = _known_envelopes(a), _known_envelopes(b)
    if ea is None or eb is None:
        if ea or eb:
            ctx.envelopes_partial = True
        return None
    out = []
    for label in sorted(set(ea) | set(eb)):
        if label not in ea:
            out.append("Huellkurve %s neu" % label)
        elif label not in eb:
            out.append("Huellkurve %s entfernt" % label)
        elif ea[label] != eb[label]:
            out.append("Huellkurve %s geaendert" % label)
    return out


def _digest_text(d: Optional[str]) -> str:
    if not d:
        return "?"
    if d.startswith("m"):
        return "%s Noten" % d[1:].split("-", 1)[0]
    return "Audio"


def _clip_changes(ctx: _Ctx, a: dict, b: dict, arrangement: bool) -> List[str]:
    out = []
    keys = sorted((set(a) | set(b)) - CLIP_IGNORE)
    envelopes = _envelope_changes(ctx, a, b)
    for k in keys:
        va, vb = a.get(k), b.get(k)
        if va == vb:
            continue
        if k == "has_envelopes" and envelopes is not None:
            # Die Zeilen je Huellkurve sagen mehr als ja/nein.
            continue
        if k == "name":
            out.append("Name '%s' -> '%s'" % (va or "", vb or ""))
        elif k == "muted":
            out.append("deaktiviert %s -> %s" % (_yn(va), _yn(vb)))
        elif k == "end_time":
            out.append("Ende %s -> %s" % (ctx.tl.pos(va), ctx.tl.pos(vb)))
        elif k == "length":
            if not arrangement:
                out.append("Laenge %s -> %s" % (ctx.tl.dur(va), ctx.tl.dur(vb)))
        elif k == "content_digest":
            ta, tb = _digest_text(va), _digest_text(vb)
            out.append("Inhalt geaendert (%s)" % ta if ta == tb else "Inhalt %s -> %s" % (ta, tb))
        elif k == "groove":
            out.append("Groove %s -> %s" % ("'%s'" % va if va else "keiner", "'%s'" % vb if vb else "keiner"))
        elif k == "looping":
            out.append("Clip-Loop %s -> %s" % (_onoff(va), _onoff(vb)))
        elif k in ("loop_start", "loop_end", "start_marker", "end_marker"):
            what = {"loop_start": "Loopstart", "loop_end": "Loopende", "start_marker": "Startmarker",
                    "end_marker": "Endmarker"}[k]
            out.append("%s %s -> %s Beats" % (what, "%g" % va if va is not None else "?",
                                              "%g" % vb if vb is not None else "?"))
        elif k == "has_envelopes":
            out.append("Clip-Huellkurven %s -> %s" % (_yn(va), _yn(vb)))
        elif k == "color_index":
            out.append("Farbe %s -> %s" % (va, vb))
        elif k == "file_path":
            out.append("Datei %s -> %s" % (os.path.basename(va or "") or "-", os.path.basename(vb or "") or "-"))
        elif k == "gain_display":
            out.append("Gain %s -> %s" % (va, vb))
        else:
            out.append("%s %s -> %s" % (k, va, vb))
    return out + (envelopes or [])


def _clip_desc(ctx: _Ctx, c: dict, arrangement: bool) -> str:
    parts = ["'%s'" % (c.get("name") or "")]
    parts.append(ctx.tl.dur(c.get("length")))
    if c.get("content_digest"):
        parts.append(_digest_text(c.get("content_digest")))
    if c.get("muted"):
        parts.append("deaktiviert")
    if c.get("envelopes"):
        parts.append("Huellkurven " + ", ".join(sorted(c["envelopes"])))
    return ", ".join(parts)


def _positions(ctx: _Ctx, keys, arrangement: bool) -> str:
    keys = sorted(keys)
    fmt = (lambda k: ctx.tl.pos(k)) if arrangement else (lambda k: "Slot %d" % k)
    if len(keys) <= 4:
        return ", ".join(fmt(k) for k in keys)
    return "%s, %s ... %s" % (fmt(keys[0]), fmt(keys[1]), fmt(keys[-1]))


def _cmp_clips(ctx: _Ctx, label, refs, names, ca: List[dict], cb: List[dict], arrangement: bool):
    if not ca and not cb:
        return
    key = (lambda c: round(float(c.get("start_time") or 0.0), 4)) if arrangement else (lambda c: c.get("index"))
    ga: Dict[Any, List[dict]] = {}
    gb: Dict[Any, List[dict]] = {}
    for c in ca:
        ga.setdefault(key(c), []).append(c)
    for c in cb:
        gb.setdefault(key(c), []).append(c)
    pairs, removed, added = [], [], []
    for k in sorted(set(ga) | set(gb), key=lambda x: (x is None, x)):
        xa, xb = ga.get(k, []), gb.get(k, [])
        n = min(len(xa), len(xb))
        pairs.extend((k, xa[i], xb[i]) for i in range(n))
        removed.extend((k, c) for c in xa[n:])
        added.extend((k, c) for c in xb[n:])
    ident = lambda c: (c.get("name"), c.get("length"), c.get("content_digest"))  # noqa: E731
    moved = []
    for r in list(removed):
        for ad in list(added):
            if ident(r[1]) == ident(ad[1]):
                moved.append((r, ad))
                removed.remove(r)
                added.remove(ad)
                break
    where = label if arrangement else "%s Session" % label
    groups: Dict[str, List[Any]] = {}
    for k, a, b in pairs:
        changes = _clip_changes(ctx, a, b, arrangement)
        if changes:
            groups.setdefault("; ".join(changes), []).append(k)
    for k, c in removed:
        groups.setdefault("entfernt: %s" % _clip_desc(ctx, c, arrangement), []).append(k)
    for k, c in added:
        groups.setdefault("neu: %s" % _clip_desc(ctx, c, arrangement), []).append(k)
    for text, keys in groups.items():
        n = len(keys)
        if n == 1:
            head = "%s Clip %s %s" % (where, "ab" if arrangement else "in", _positions(ctx, keys, arrangement))
        else:
            head = "%s, %d Clips (%s)" % (where, n, _positions(ctx, keys, arrangement))
        ctx.add("CLIPS", "%s: %s" % (head, text), "clips", refs, names, count=n)
    for (ka, a), (kb, b) in moved:
        fmt = ctx.tl.pos if arrangement else (lambda k: "Slot %d" % k)
        ctx.add("CLIPS", "%s Clip '%s' verschoben: %s -> %s" % (where, b.get("name") or "", fmt(ka), fmt(kb)),
                "clips", refs, names)


# ----------------------------------------------------------------------
# Song
# ----------------------------------------------------------------------

def _cmp_song(ctx: _Ctx, a: dict, b: dict):
    s = ["song"]
    tl = ctx.tl
    if a.get("tempo") != b.get("tempo"):
        ctx.add("SONG", "Tempo %s -> %s BPM" % (_num(a.get("tempo")), _num(b.get("tempo"))), "song", s)
    sig_a = (a.get("signature_numerator"), a.get("signature_denominator"))
    sig_b = (b.get("signature_numerator"), b.get("signature_denominator"))
    if sig_a != sig_b:
        ctx.add("SONG", "Taktart %s/%s -> %s/%s" % (sig_a + sig_b), "song", s)
    loop_a = (a.get("loop"), a.get("loop_start"), a.get("loop_length"))
    loop_b = (b.get("loop"), b.get("loop_start"), b.get("loop_length"))
    if loop_a != loop_b:
        ctx.add("SONG", "Loop %s -> %s (kein Undo-Schritt)" % (_loop_text(tl, *loop_a), _loop_text(tl, *loop_b)),
                "loop", s, undo=False)
    if a.get("song_length") != b.get("song_length"):
        # Folgt aus den Clips; die stehen selbst in der Liste.
        ctx.add("SONG", "Songende %s -> %s" % (tl.pos(a.get("song_length")), tl.pos(b.get("song_length"))),
                "song", s, undo=False)
    for key, what, undo in (("groove_amount", "Groove-Menge global", True), ("scene_count", "Szenen", True),
                            ("metronome", "Metronom", False), ("record_mode", "Aufnahme", False),
                            ("arrangement_overdub", "Overdub", False), ("session_record", "Session-Aufnahme", False),
                            ("back_to_arranger", "'Back to Arrangement' leuchtet", False)):
        if key in a and key in b and a[key] != b[key]:
            va, vb = a[key], b[key]
            if isinstance(va, bool) or isinstance(vb, bool):
                va, vb = _onoff(va), _onoff(vb)
            ctx.add("SONG", "%s %s -> %s%s" % (what, va, vb, "" if undo else " (kein Undo-Schritt)"), "song", s,
                    undo=undo)
    if a.get("re_enable_automation_enabled") != b.get("re_enable_automation_enabled"):
        # Folgt aus den Parametern; die uebersteuerten stehen einzeln da.
        ctx.add("AUTOMATION", "'Re-Enable Automation' leuchtet: %s -> %s" % (
            _yn(a.get("re_enable_automation_enabled")), _yn(b.get("re_enable_automation_enabled"))),
            "automation", s, undo=False, automation=True)
    if (a.get("scale") or {}) != (b.get("scale") or {}):
        ctx.add("SONG", "Skala %s -> %s" % (_scale_text(a.get("scale")), _scale_text(b.get("scale"))), "song", s)
    _cmp_grooves(ctx, a.get("grooves") or [], b.get("grooves") or [])
    _cmp_cues(ctx, a.get("cue_points") or [], b.get("cue_points") or [])
    for key in ("can_undo", "can_redo"):
        if key in a and key in b and a[key] != b[key]:
            ctx.add("UNDO-ZUSTAND", "%s %s -> %s" % (key, _yn(a[key]), _yn(b[key])), "song", s, undo=False)


def _num(v) -> str:
    return "?" if v is None else "%g" % v


def _loop_text(tl, on, start, length) -> str:
    if start is None or length is None:
        return _onoff(on)
    return "%s %s-%s (%s)" % (_onoff(on), tl.pos(start), tl.pos(start + length), tl.dur(length))


def _scale_text(sc) -> str:
    sc = sc or {}
    root = sc.get("root_note")
    root_name = views.NOTE_NAMES[root % 12] if isinstance(root, int) else "?"
    return "%s %s%s" % (root_name, sc.get("scale_name"), "" if sc.get("scale_mode") else " (aus)")


def _cmp_grooves(ctx: _Ctx, a, b):
    ga = {g.get("name"): g for g in a}
    gb = {g.get("name"): g for g in b}
    for name in sorted(set(ga) - set(gb), key=str):
        ctx.add("SONG", "Groove '%s' aus dem Groove-Pool entfernt" % name, "song", ["song"])
    for name in sorted(set(gb) - set(ga), key=str):
        ctx.add("SONG", "Groove '%s' im Groove-Pool neu" % name, "song", ["song"])
    for name in sorted(set(ga) & set(gb), key=str):
        diffs = ["%s %s -> %s" % (k, _num(ga[name].get(k)), _num(gb[name].get(k)))
                 for k in sorted(set(ga[name]) | set(gb[name])) if k != "name" and ga[name].get(k) != gb[name].get(k)]
        if diffs:
            ctx.add("SONG", "Groove '%s': %s" % (name, ", ".join(diffs)), "song", ["song"])


def _cmp_cues(ctx: _Ctx, a: List[dict], b: List[dict]):
    s = ["song"]
    if any(c.get("time") is None for c in a + b):
        # Format 1 kennt nur die Namen.
        na = [c.get("name") for c in a]
        nb = [c.get("name") for c in b]
        for n in [x for x in na if x not in nb]:
            ctx.add("SONG", "Locator '%s' entfernt (Position unbekannt, Format 1)" % n, "locators", s)
        for n in [x for x in nb if x not in na]:
            ctx.add("SONG", "Locator '%s' neu (Position in A unbekannt, Format 1)" % n, "locators", s)
        return
    tl = ctx.tl
    ta = {round(c["time"], 3): c.get("name") for c in a}
    tb = {round(c["time"], 3): c.get("name") for c in b}
    removed = {t: n for t, n in ta.items() if t not in tb}
    added = {t: n for t, n in tb.items() if t not in ta}
    for t in sorted(set(ta) & set(tb)):
        if ta[t] != tb[t]:
            ctx.add("SONG", "Locator bei %s umbenannt: '%s' -> '%s'" % (tl.pos(t), ta[t], tb[t]), "locators", s)
    for t, n in sorted(removed.items()):
        target = [u for u, m in added.items() if m == n]
        if target:
            u = target[0]
            del added[u]
            ctx.add("SONG", "Locator '%s' verschoben: %s -> %s" % (n, tl.pos(t), tl.pos(u)), "locators", s)
        else:
            ctx.add("SONG", "Locator '%s' bei %s entfernt" % (n, tl.pos(t)), "locators", s)
    for t, n in sorted(added.items()):
        ctx.add("SONG", "Locator '%s' neu bei %s" % (n, tl.pos(t)), "locators", s)


# ======================================================================
# Ausgabe
# ======================================================================

# Viele geaenderte Parameter eines Geraets (Preset geladen) sollen die Clips
# dahinter nicht aus der Ausgabe draengen.
PARAMS_PER_DEVICE = 6


def render(report: Report, name_a: str, name_b: str, max_lines: int = 60) -> str:
    a, b = report.a, report.b
    head = ["Vergleich A -> B",
            "A: %s | %s | %s" % (name_a, a.get("taken") or "Zeitpunkt unbekannt", playhead_text(a)),
            "B: %s | %s | %s" % (name_b, b.get("taken") or "Zeitpunkt unbekannt", playhead_text(b))]
    if a.get("set_file") and b.get("set_file") and a["set_file"] != b["set_file"]:
        head.append("ACHTUNG: verschiedene Sets: %s / %s" % (os.path.basename(a["set_file"]),
                                                           os.path.basename(b["set_file"])))
    counted = report.counted()
    per_cat = {c: [e for e in counted if e.cat == c] for c in CATEGORIES}
    total = sum(e.count for e in counted)
    if counted:
        head.append("Ergebnis: %d Aenderung(en): %s" % (total, ", ".join(
            "%d %s" % (sum(e.count for e in es), c.title()) for c, es in per_cat.items() if es)))
    else:
        head.append("Ergebnis: KEINE Aenderungen")
    info = report.info()
    if info:
        head.append("Nicht gezaehlt: %s" % ", ".join(
            "%d %s" % (len([e for e in info if e.cat == c]), {"FOLGT AUTOMATION": "folgt Automation",
                                                             "TOLERANZ": "innerhalb der Toleranz",
                                                             "UNDO-ZUSTAND": "Undo-Zustand"}[c])
            for c in INFO_CATEGORIES if any(e.cat == c for e in info)))
    head.extend("HINWEIS: " + n for n in report.notes)
    body = []
    for cat in CATEGORIES:
        es = per_cat[cat]
        if not es:
            continue
        body.append("%s (%d)" % (cat, sum(e.count for e in es)))
        shown: Dict[Any, int] = {}
        for e in es:
            if e.group is not None and max_lines:
                shown[e.group] = shown.get(e.group, 0) + 1
                if shown[e.group] > PARAMS_PER_DEVICE:
                    continue
            body.append("  " + e.text)
        for g, n in shown.items():
            if n > PARAMS_PER_DEVICE:
                body.append("  ... und %d weitere Parameter an %s (max_lines=0 zeigt alle)" % (
                    n - PARAMS_PER_DEVICE, _group_label(es, g)))
    if info:
        body.append("NICHT GEZAEHLT")
        follow = [e for e in info if e.cat == "FOLGT AUTOMATION"]
        if follow:
            body.append("  folgt Automation, Wert haengt am Playhead (A %s, B %s):" % (
                _ph_short(a), _ph_short(b)))
            body.extend("    " + e.text for e in (follow if not max_lines else follow[:8]))
            if max_lines and len(follow) > 8:
                body.append("    ... und %d weitere" % (len(follow) - 8))
        tol = [e for e in info if e.cat == "TOLERANZ"]
        if tol:
            body.append("  innerhalb der Toleranz (Anzeige gleich, Rohwert hoechstens 1/10000 des Bereichs "
                        "daneben): %d, z. B. %s" % (len(tol), tol[0].text))
        und = [e for e in info if e.cat == "UNDO-ZUSTAND"]
        if und:
            body.append("  Lives Undo-Zustand: %s" % ", ".join(e.text for e in und))
    lines = head + body
    if max_lines and len(lines) > max_lines:
        cut = len(lines) - max_lines
        lines = lines[:max_lines] + ["... %d weitere Zeilen nicht gezeigt (max_lines=0 zeigt alles)" % cut]
    return "\n".join(lines)


def _group_label(entries: List[Entry], group) -> str:
    for e in entries:
        if e.group == group:
            return e.text.split(":", 1)[0].rsplit(" / ", 1)[0]
    return str(group)


def _ph_short(snap: dict) -> str:
    ph = snap.get("playhead") or {}
    if ph.get("beat") is None:
        return "nicht festgehalten"
    return "%s%s" % (_timeline(snap).pos(ph["beat"]), ", Transport lief" if ph.get("playing") else "")


# ======================================================================
# Kommandozeile
# ======================================================================

class C:
    """Direkter Socket-Client, ohne MCP-Server (z. B. wenn der gerade neu startet)."""

    def __init__(self):
        self.s = socket.create_connection(("127.0.0.1", 9877), timeout=60)

    def req(self, typ, params=None):
        self.s.sendall(json.dumps({"type": typ, "params": params or {}}).encode())
        buf = b""
        while True:
            buf += self.s.recv(1 << 20)
            if buf.rstrip().endswith(b"}"):
                try:
                    r = json.loads(buf.decode())
                    break
                except ValueError:
                    continue
        if r.get("status") != "success":
            raise RuntimeError("%s: %s" % (typ, r.get("message")))
        return r["result"]


def timing():
    c = C()
    for typ in ("get_script_info", "get_session_info", "get_selection", "get_session_info", "get_meters"):
        ts = []
        for _ in range(10):
            t0 = time.perf_counter()
            c.req(typ)
            ts.append((time.perf_counter() - t0) * 1000)
        ts.sort()
        print("%-18s min %6.1f  median %6.1f  max %6.1f ms" % (typ, ts[0], ts[len(ts) // 2], ts[-1]))
    t0 = time.perf_counter()
    c.req("get_tracks_overview")
    print("get_tracks_overview einmal: %.1f ms" % ((time.perf_counter() - t0) * 1000))


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "snapshot" and len(argv) == 3:
        snap = take(C().req, label=os.path.splitext(os.path.basename(argv[2]))[0])
        save(snap, argv[2])
        print("Schnappschuss %s: %s | %s | %.1f s" % (argv[2], summary(snap), playhead_text(snap), snap["seconds"]))
    elif cmd == "diff" and len(argv) in (4, 5):
        a, b = load(argv[2]), load(argv[3])
        print(render(compare(a, b), os.path.basename(argv[2]), os.path.basename(argv[3]),
                     max_lines=int(argv[4]) if len(argv) == 5 else 0))
    elif cmd == "time":
        timing()
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
