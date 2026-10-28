"""Attrappe von Lives Python-API fuer Tests ohne Live.

Bildet nur nach, was das Remote Script benutzt, und verhaelt sich an den
bekannten Stolperstellen wie Live: Gruppenspuren werfen RuntimeError bei
arm und arrangement_clips. Jeder Zugriff auf ein API-Objekt ausserhalb des
simulierten Hauptthreads wird protokolliert, damit Tests das finden.

Was die Attrappe NICHT pruefen kann: ob Live 12 die Eigenschaften wirklich
so nennt und so liefert. Das zeigt erst der Test am echten Set.
"""
from __future__ import annotations

import math
import queue
import sys
import threading
import time
import types

MAIN_THREAD_NAME = "FakeLiveMain"
OFF_THREAD_ACCESS: list[str] = []


class LomObject:
    def __getattribute__(self, name):
        if not name.startswith("_") and threading.current_thread().name != MAIN_THREAD_NAME:
            OFF_THREAD_ACCESS.append("%s.%s" % (type(self).__name__, name))
        return object.__getattribute__(self, name)


# ----------------------------------------------------------------------
# Parameter und Geraete
# ----------------------------------------------------------------------

def volume_display(v):
    if v <= 0.0:
        return "-inf dB"
    db = 40.0 * math.log10(v / 0.85)
    if db < -69.5:
        return "-inf dB"
    return "%.1f dB" % db


def volume_for_db(db):
    return 0.85 * 10 ** (db / 40.0)


def pan_display(v):
    amount = int(round(abs(v) * 50))
    return "C" if amount == 0 else "%d%s" % (amount, "L" if v < 0 else "R")


class Param(LomObject):
    def __init__(self, name, value, lo=0.0, hi=1.0, display=None, automation_state=0, quantized=False,
                 items=None):
        self.name = name
        self.original_name = name
        self.value = value
        self.min = lo
        self.max = hi
        self.automation_state = automation_state
        self.is_quantized = quantized
        self.value_items = items or []
        self.is_enabled = True
        self._display = display or (lambda v: "%.2f" % v)

    def __str__(self):
        return self._display(self.value)

    def str_for_value(self, v):
        return self._display(v)


class LaggingParam(Param):
    """Plugin-Parameter, dessen Anzeigetext erst im naechsten Durchlauf folgt
    (am Set 11.10.2026: ein Plugin-Schalter auf 1 zeigte noch 'Off')."""
    instances = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._shown = self.value
        LaggingParam.instances.append(self)

    def __str__(self):
        return self._display(self._shown)

    def catch_up(self):
        self._shown = object.__getattribute__(self, "value")


class Device(LomObject):
    def __init__(self, name, class_name, dtype=2, params=None, active=True, chains=None):
        self.name = name
        self.class_name = class_name
        self.class_display_name = name
        self.type = dtype
        self.is_active = active
        self.can_have_drum_pads = False
        self.can_have_chains = chains is not None
        self.chains = chains or []
        on = Param("Device On", 1.0 if active else 0.0, 0.0, 1.0, lambda v: "On" if v else "Off", quantized=True,
                   items=["Off", "On"])
        self.parameters = [on] + (params or [])


class Chain(LomObject):
    def __init__(self, name, devices):
        self.name = name
        self.mute = False
        self.devices = devices


class Mixer(LomObject):
    def __init__(self, volume_db=0.0, sends_db=None, activator=1.0, activator_auto=0, send_auto=None):
        self.volume = Param("Track Volume", volume_for_db(volume_db), 0.0, 1.0, volume_display)
        self.panning = Param("Track Panning", 0.0, -1.0, 1.0, pan_display)
        self.sends = []
        for i, db in enumerate(sends_db or [None, None]):
            v = 0.0 if db is None else volume_for_db(db)
            p = Param("%s-Send" % chr(65 + i), v, 0.0, 1.0, volume_display,
                      automation_state=(send_auto or {}).get(i, 0))
            self.sends.append(p)
        self.track_activator = Param("Speaker On", activator, 0.0, 1.0, lambda v: "On" if v else "Off",
                                     automation_state=activator_auto, quantized=True, items=["Off", "On"])
        self.crossfade_assign = 1


class Routing(LomObject):
    """RoutingType bzw. RoutingChannel. category und attached_object gibt es in
    Live 12 laut API; ob category als Text oder Zahl ankommt, ist ungeprueft."""

    def __init__(self, name, category="external", attached=None):
        self.display_name = name
        self.category = category
        self.attached_object = attached


TAP_CHANNELS = ("Pre FX", "Post FX", "Post Mixer")


class RoutingOwner:
    """Ein- und Ausgaenge wie in Live: Setzbar ist nur ein Objekt aus der Liste
    available_*; ein neuer Typ waehlt seinen ersten Kanal. Die Objekte sind je
    Name zwischengespeichert, damit gleiche Auswahl dasselbe Objekt ist. Die
    Grundeinstellung entsteht erst beim ersten Lesen, weil eine Spur das Set
    (und damit die anderen Spuren als Quellen) erst danach kennt."""

    def _rt(self, side, name, category="external", attached=None):
        cache = self.__dict__.setdefault("_rt_cache", {})
        key = (side, name, id(attached))
        if key not in cache:
            cache[key] = Routing(name, category, attached)
        return cache[key]

    def _channels_for(self, side, rtype):
        if rtype.attached_object is not None and side == "input":
            names = TAP_CHANNELS
        elif rtype.display_name == "Ext. In":
            names = ("1", "2", "1/2")
        elif rtype.display_name == "Ext. Out":
            names = ("1/2", "3/4")
        elif rtype.display_name == "All Ins":
            names = ("All Channels", "Ch. 1", "Ch. 2")
        else:
            names = ("",)
        return [self._rt(side + "_ch:" + rtype.display_name, n) for n in names]

    def _cur(self, side, what):
        key = "_%s_%s" % (side, what)
        if key not in self.__dict__:
            self._init_routing(side, *self._default_routing(side))
        return self.__dict__[key]

    def _set_type(self, side, rtype):
        if not any(rtype is o for o in self._types(side)):
            raise RuntimeError("Invalid routing type")
        self.__dict__["_%s_type" % side] = rtype
        self.__dict__["_%s_channel" % side] = self._channels_for(side, rtype)[0]

    def _set_channel(self, side, channel):
        if not any(channel is c for c in self._channels_for(side, self._cur(side, "type"))):
            raise RuntimeError("Invalid routing channel")
        self.__dict__["_%s_channel" % side] = channel

    def _init_routing(self, side, name, channel=None):
        rtype = next(t for t in self._types(side) if t.display_name == name)
        self.__dict__["_%s_type" % side] = rtype
        chans = self._channels_for(side, rtype)
        self.__dict__["_%s_channel" % side] = next((c for c in chans if c.display_name == channel), chans[0])

    @property
    def input_routing_type(self):
        return self._cur("input", "type")

    @input_routing_type.setter
    def input_routing_type(self, value):
        self._set_type("input", value)

    @property
    def input_routing_channel(self):
        return self._cur("input", "channel")

    @input_routing_channel.setter
    def input_routing_channel(self, value):
        self._set_channel("input", value)

    @property
    def available_input_routing_types(self):
        return tuple(self._types("input"))

    @property
    def available_input_routing_channels(self):
        return tuple(self._channels_for("input", self._cur("input", "type")))


class TrackRouting(RoutingOwner):
    """Spuren haben zusaetzlich einen Ausgang. Namen wie am Set 22.10.2026:
    Spur in einer Gruppe -> Name der Gruppe, sonst "Main", Master -> "Ext. Out"."""

    @property
    def output_routing_type(self):
        return self._cur("output", "type")

    @output_routing_type.setter
    def output_routing_type(self, value):
        self._set_type("output", value)

    @property
    def output_routing_channel(self):
        return self._cur("output", "channel")

    @output_routing_channel.setter
    def output_routing_channel(self, value):
        self._set_channel("output", value)

    @property
    def available_output_routing_types(self):
        return tuple(self._types("output"))

    @property
    def available_output_routing_channels(self):
        return tuple(self._channels_for("output", self._cur("output", "type")))

    def _default_routing(self, side):
        if side == "output":
            if self._kind == "master":
                return "Ext. Out", "1/2"
            return (self.group_track.name if self.group_track is not None else "Main"), None
        if self._kind == "midi":
            return "All Ins", None
        if self._kind == "audio":
            return "Ext. In", "1"
        return "Ext. In", "1/2"

    def _types(self, side):
        song = self._song
        others = [t for t in list(song.tracks) + list(song.return_tracks) if t is not self] if song else []
        if side == "output":
            out = [self._rt(side, "Ext. Out")]
            if self._kind == "master":
                return out
            out.append(self._rt(side, "Sends Only", "none"))
            out.append(self._rt(side, "Main", "master", song.master_track if song else None))
            out += [self._rt(side, t.name, "track", t) for t in others if t._kind in ("group", "audio", "return")]
            return out
        if self._kind == "midi":
            return ([self._rt(side, n) for n in ("All Ins", "Computer Keyboard")] + [self._rt(side, "No Input", "none")]
                    + [self._rt(side, t.name, "track", t) for t in others if t._kind == "midi"])
        if self._kind == "audio":
            return ([self._rt(side, "Ext. In"), self._rt(side, "Resampling", "resampling"),
                     self._rt(side, "No Input", "none")] + [self._rt(side, t.name, "track", t) for t in others])
        return [self._rt(side, "Ext. In")]


class SidechainDevice(RoutingOwner, Device):
    """Lives Compressor mit Sidechain-Eingang (CompressorDevice.input_routing_*). Quellen: Spuren des Sets ausser der eigenen."""

    def __init__(self, name, class_name, song, owner_track, source=None, channel=None, sc_on=True, params=None):
        sc = [Param("S/C On", 1.0 if sc_on else 0.0, 0.0, 1.0, lambda v: "On" if v else "Off", quantized=True,
                    items=["Off", "On"]),
              Param("S/C Gain", 0.5, 0.0, 1.0, lambda v: "%.2f dB" % ((v - 0.5) * 48))]
        Device.__init__(self, name, class_name, params=(params or []) + sc)
        self._song = song
        self._owner_track = owner_track
        self._source = (source or "No Input", channel)

    def _default_routing(self, side):
        return self._source

    def _types(self, side):
        out = [self._rt(side, "No Input", "none"), self._rt(side, "Ext. In")]
        for t in list(self._song.tracks) + list(self._song.return_tracks):
            if t is not self._owner_track:
                out.append(self._rt(side, t.name, "track", t))
        return out


# ----------------------------------------------------------------------
# Clips
# ----------------------------------------------------------------------

class Note(LomObject):
    def __init__(self, pitch, start_time, duration, velocity, mute=False, probability=1.0, velocity_deviation=0.0,
                 release_velocity=64.0):
        self.pitch = pitch
        self.start_time = start_time
        self.duration = duration
        self.velocity = velocity
        self.mute = mute
        self.probability = probability
        self.velocity_deviation = velocity_deviation
        self.release_velocity = release_velocity


class MidiNoteSpecification:
    """Live.Clip.MidiNoteSpecification. Ob Live 12 velocity_deviation und
    release_velocity hier annimmt, zeigt erst das Set (Teil 3); die Attrappe
    nimmt es an. REJECT: Felder, die sie wie eine fremde Live-Version ablehnt."""
    REJECT: set = set()

    def __init__(self, pitch, start_time, duration, velocity=100.0, mute=False, **extra):
        for k in extra:
            if k in MidiNoteSpecification.REJECT or k not in ("probability", "velocity_deviation", "release_velocity"):
                raise TypeError("MidiNoteSpecification() got an unexpected keyword argument '%s'" % k)
        self.pitch = pitch
        self.start_time = start_time
        self.duration = duration
        self.velocity = velocity
        self.mute = mute
        self.probability = extra.get("probability", 1.0)
        self.velocity_deviation = extra.get("velocity_deviation", 0.0)
        self.release_velocity = extra.get("release_velocity", 64.0)


class Groove(LomObject):
    def __init__(self, name):
        self.name = name
        self.base = 3
        self.timing_amount = 35.0
        self.quantization_amount = 0.0
        self.random_amount = 0.0
        self.velocity_amount = 0.0


class Envelope(LomObject):
    """Clip-Huellkurve, wie Lives API sie zeigt: nur insert_step und
    value_at_time, keine Liste der Punkte. Die Attrappe speichert Stufen, die
    ihren Wert von time bis time + duration halten. Wie Live Stufen der Dauer
    0 und die Strecken zwischen Stufen formt, weiss erst der Test am Set."""

    def __init__(self, initial):
        self._initial = float(initial)
        self._steps = []

    def insert_step(self, time, duration, value):
        """insert_step( (AutomationEnvelope)self, (float)time, (float)duration, (float)value) -> None"""
        self._steps.append((float(time), float(time) + float(duration), float(value)))

    def value_at_time(self, time):
        """value_at_time( (AutomationEnvelope)self, (float)time) -> float"""
        # Wie Live 12.4.7 am Set (27.10.2026): Der Grenzpunkt gehoert zur
        # Stufe davor, am Anfang einer neuen Kurve steht noch der Reglerwert.
        v = self._initial
        for s, e, val in self._steps:
            if s < time <= e:
                v = val
        return v


class Clip(LomObject):
    # Nur fuer Tests: so tun, als lehne Live Huellkurven an diesem Clip ab.
    ENVELOPES_REFUSED = False

    def __init__(self, name, start, length, midi=True, muted=False, notes=None, arrangement=True, file_path=None,
                 groove=None):
        self.name = name
        self.start_time = start
        self.end_time = start + length
        self.length = length
        self.muted = muted
        self.looping = True
        self.loop_start = 0.0
        self.loop_end = length
        self.start_marker = 0.0
        self.end_marker = length
        self.color_index = 5
        self._envelopes = {}
        self.is_playing = False
        self.is_triggered = False
        self.is_midi_clip = midi
        self.is_audio_clip = not midi
        self.is_arrangement_clip = arrangement
        self._groove = groove
        self.canonical_parent = None
        self._notes = list(notes or [])
        if not midi:
            self.file_path = file_path or "D:/Samples/kick_print.wav"
            self.gain_display_string = "0.0 dB"
            self.warping = True
            self.warp_mode = 0
            self.pitch_coarse = 0
            self.pitch_fine = 0.0

    # Am Set 22.10.2026 (Live 12.4.6): clip.groove = None scheitert an der
    # C++-Signatur. Ob "del clip.groove" geht, ist ungeprueft; die Attrappe
    # erlaubt es, Tests koennen es abschalten.
    DEL_GROOVE_WORKS = True

    @property
    def groove(self):
        return self._groove

    @groove.setter
    def groove(self, value):
        if value is None:
            raise TypeError("Python argument types in\n    None.None(Clip, NoneType)\ndid not match C++ signature:\n"
                            "    None(class TPyHandle<class AClip>, class TPyHandle<class AAbstractGroove>)")
        self._groove = value

    @groove.deleter
    def groove(self):
        if not Clip.DEL_GROOVE_WORKS:
            raise AttributeError("can't delete attribute")
        self._groove = None

    @property
    def has_groove(self):
        return self._groove is not None

    @property
    def has_envelopes(self):
        return bool(self._envelopes)

    def automation_envelope(self, param):
        """automation_envelope( (Clip)self, (DeviceParameter)param) -> AutomationEnvelope :
    Returns None for Arrangement clips."""
        # Live 12.4.7: Arrangement-Clips geben keine Huellkurve heraus,
        # auch wenn sie eine haben (_envelopes dient dann nur has_envelopes).
        if self.is_arrangement_clip:
            return None
        return self._envelopes.get(id(param))

    def create_automation_envelope(self, param):
        if Clip.ENVELOPES_REFUSED or self.is_arrangement_clip:
            raise RuntimeError("Envelope could not be created")
        if id(param) in self._envelopes:
            raise RuntimeError("Envelope already exists")
        env = Envelope(param.value)
        self._envelopes[id(param)] = env
        return env

    def clear_envelope(self, param):
        self._envelopes.pop(id(param), None)

    def clear_all_envelopes(self):
        self._envelopes.clear()

    def _move(self, start, end):
        # Nur fuer die Attrappe: Arrangement-Clip kuerzen, wie Live es beim
        # Ueberschreiben tut (siehe Track.duplicate_clip_to_arrangement).
        self.start_time, self.end_time = start, end
        self.length = end - start

    def get_notes_extended(self, from_pitch, pitch_span, from_time, time_span):
        return tuple(n for n in self._notes if from_pitch <= n.pitch < from_pitch + pitch_span
                     and from_time <= n.start_time < from_time + time_span)

    # Felder, die die Attrappe annimmt, aber nicht speichert (wie eine Live-
    # Version, die sie still verwirft). Prueft das Nachlesen im Server.
    IGNORED_NOTE_FIELDS: set = set()

    def add_new_notes(self, specs):
        for s in specs:
            extra = dict((k, getattr(s, k)) for k in ("probability", "velocity_deviation", "release_velocity")
                         if k not in Clip.IGNORED_NOTE_FIELDS)
            self._notes.append(Note(s.pitch, s.start_time, s.duration, s.velocity, s.mute, **extra))

    def set_notes(self, notes):
        for n in notes:
            self._notes.append(Note(*n))

    def remove_notes_extended(self, from_pitch, pitch_span, from_time, time_span):
        self._notes = [n for n in self._notes if not (from_pitch <= n.pitch < from_pitch + pitch_span
                                                      and from_time <= n.start_time < from_time + time_span)]


class ClipSlot(LomObject):
    def __init__(self, track):
        self.canonical_parent = track
        self.clip = None

    @property
    def has_clip(self):
        return self.clip is not None

    def create_clip(self, length):
        c = Clip("", 0.0, length, arrangement=False)
        c.canonical_parent = self
        self.clip = c

    def delete_clip(self):
        self.clip = None

    def fire(self):
        if self.clip is not None:
            self.clip.is_triggered = True

    def stop(self):
        if self.clip is not None:
            self.clip.is_playing = False
            self.clip.is_triggered = False


# ----------------------------------------------------------------------
# Spuren und Song
# ----------------------------------------------------------------------

class Track(TrackRouting, LomObject):
    def __init__(self, name, kind, group=None, volume_db=0.0, devices=None, clips=None, arm=False, **mixer_kw):
        self.name = name
        self._kind = kind
        self.is_foldable = kind == "group"
        self.group_track = group
        self.is_grouped = group is not None
        self.has_midi_input = kind == "midi"
        self.has_audio_input = kind in ("audio", "group", "return", "master")
        self.mute = False
        self.solo = False
        self._arm = arm
        self.implicit_arm = False
        self.can_be_armed = kind in ("midi", "audio")
        self.current_monitoring_state = 1
        self.is_visible = True
        self.fold_state = 0
        self.color_index = 3
        self.mixer_device = Mixer(volume_db=volume_db, **mixer_kw)
        self.devices = devices or []
        self.clip_slots = [ClipSlot(self) for _ in range(8)] if kind in ("midi", "audio") else []
        self._arrangement = list(clips or [])
        for c in self._arrangement:
            c.canonical_parent = self
        self._song = None
        self.output_meter_left = 0.0
        self.output_meter_right = 0.0
        self.output_meter_level = 0.0
        self.input_meter_left = 0.0
        self.input_meter_right = 0.0
        self.input_meter_level = 0.0

    # Geraete verwalten wie Live 12.4.6 (technoa.5). Nur native Geraete lassen
    # sich per insert_device einfuegen; Plugins wirft Live dort hinaus.
    NATIVE_DEVICES = ("Utility", "EQ Eight", "Shifter", "Saturator", "Reverb", "Compressor", "Delay")

    def delete_device(self, index):
        del self.devices[index]

    def duplicate_device(self, index):
        d = self.devices[index]
        copy = Device(d.name, d.class_name, d.type, active=d.is_active)
        self.devices.insert(index + 1, copy)

    def insert_device(self, device_name, target_index):
        if device_name not in self.NATIVE_DEVICES:
            raise RuntimeError("Unknown or non-native device: %s" % device_name)
        # Wie Live am 11.10.2026: Audioeffekt vor dem Instrument abgelehnt,
        # mit dem Vorschlag der naechsten gueltigen Position.
        inst = [i for i, d in enumerate(self.devices) if d.type == 1]
        if inst and target_index <= inst[0]:
            raise RuntimeError("Invalid device index. A valid index would be %d" % (inst[0] + 1))
        d = Device(device_name, device_name.replace(" ", ""))
        self.devices.insert(target_index, d)
        return d

    @property
    def arm(self):
        if not self.can_be_armed:
            raise RuntimeError("Master and Return Tracks have no 'Arm' state!")
        return self._arm

    @arm.setter
    def arm(self, value):
        if not self.can_be_armed:
            raise RuntimeError("Master and Return Tracks have no 'Arm' state!")
        self._arm = value

    @property
    def arrangement_clips(self):
        if self._kind not in ("midi", "audio"):
            raise RuntimeError("Main, Group and Return Tracks have no arrangement clips")
        return tuple(self._arrangement)

    def duplicate_clip_to_arrangement(self, clip, time):
        # Am Set gesehen (11.10.2026): Ein ganz ueberdeckter Clip ist danach
        # weg, und die Kopie bekommt den Groove. Dass teilweise ueberdeckte
        # Clips gekuerzt bzw. geteilt werden, ist Lives uebliches Verhalten im
        # Arrangement, hier aber nur angenommen.
        end = time + clip.length
        kept = []
        for old in self._arrangement:
            if old.end_time <= time or old.start_time >= end:
                kept.append(old)
            elif old.start_time < time and old.end_time > end:
                tail = Clip(old.name, end, old.end_time - end, midi=old.is_midi_clip, muted=old.muted,
                            notes=old._notes, groove=old.groove)
                tail.canonical_parent = self
                old._move(old.start_time, time)
                kept.extend([old, tail])
            elif old.start_time < time:
                old._move(old.start_time, time)
                kept.append(old)
            elif old.end_time > end:
                old._move(end, old.end_time)
                kept.append(old)
        c = Clip(clip.name, time, clip.length, midi=clip.is_midi_clip, notes=clip._notes, groove=clip.groove)
        c.canonical_parent = self
        kept.append(c)
        self._arrangement = sorted(kept, key=lambda x: x.start_time)

    def delete_clip(self, clip):
        for i, c in enumerate(self._arrangement):
            if c is clip:
                del self._arrangement[i]
                return
        raise RuntimeError("Clip not found in arrangement")


class CuePoint(LomObject):
    def __init__(self, name, time):
        self.name = name
        self.time = time


class SongView(LomObject):
    def __init__(self):
        self.selected_track = None
        self.selected_scene = None
        self.detail_clip = None
        self.selected_parameter = None


class GroovePool(LomObject):
    def __init__(self, grooves):
        self.grooves = grooves


class Song(LomObject):
    def __init__(self):
        self.tracks = []
        self.return_tracks = []
        self.master_track = None
        self.file_path = ""
        self.exclusive_arm = False
        self.tempo = 136.0
        self.signature_numerator = 4
        self.signature_denominator = 4
        self.is_playing = False
        self._song_time = 190.66
        self._pending_song_time = None
        self.song_length = 544.0
        self._loop = True
        self._pending_loop = None
        self.loop_start = 128.0
        self.loop_length = 64.25
        self.record_mode = False
        self.arrangement_overdub = False
        self.session_record = False
        self.metronome = False
        self.back_to_arranger = False
        self.root_note = 0
        self.scale_name = "Major"
        self.scale_mode = True
        self.scale_intervals = [0, 2, 4, 5, 7, 9, 11]
        self.groove_amount = 1.0
        self.scenes = [object()]
        self.groove_pool = GroovePool([])
        self.cue_points = []
        self.view = SongView()
        self.undo_log = []
        self.can_undo = True
        self.can_redo = False
        self.re_enable_automation_enabled = False
        # Ob ein Befehl in Live einen Undo-Schritt anlegt, steht hier pro Test
        # fest: False bildet z. B. solo, arm oder den Loop nach.
        self.next_step_is_undoable = True

    @property
    def current_song_time(self):
        return self._song_time

    @current_song_time.setter
    def current_song_time(self, value):
        # Wie Live bei gestopptem Transport: Der neue Wert gilt erst im
        # naechsten Durchlauf des Hauptthreads (siehe CInstance._loop).
        if value > self.song_length:
            raise RuntimeError("Cannot set the Songtime behind the Songlength")
        self._pending_song_time = value

    @property
    def loop(self):
        return self._loop

    @loop.setter
    def loop(self, value):
        # Wie Live 12.4.6 am Set (19. und 20.10.2026): Der Schalter gilt
        # erst im naechsten Durchlauf; Start und Laenge dagegen sofort.
        self._pending_loop = bool(value)

    def apply_pending(self):
        if self._pending_song_time is not None:
            self._song_time = self._pending_song_time
            self._pending_song_time = None
        if self._pending_loop is not None:
            self._loop = self._pending_loop
            self._pending_loop = None
        for p in LaggingParam.instances:
            p.catch_up()

    def begin_undo_step(self):
        self.undo_log.append("begin")

    def end_undo_step(self):
        self.undo_log.append("end")
        if self.next_step_is_undoable:
            self.can_redo = False

    def undo(self):
        self.undo_log.append("undo")
        self.can_redo = True

    def redo(self):
        self.undo_log.append("redo")

    def re_enable_automation(self):
        self.undo_log.append("re_enable_automation")
        self.re_enable_automation_enabled = False

    def _owner_of(self, device):
        for t in list(self.tracks) + list(self.return_tracks) + [self.master_track]:
            if t is not None and any(d is device for d in t.devices):
                return t
        raise RuntimeError("Device not found")

    def find_device_position(self, device, target, target_position):
        # Wie Live: Ein Audioeffekt kann auf einer Spur mit Instrument nicht
        # vor dem Instrument stehen; Live schlaegt die naechste gueltige Luecke vor.
        pos = max(0, min(int(target_position), len(target.devices)))
        inst = [i for i, d in enumerate(target.devices) if d.type == 1 and d is not device]
        if device.type == 2 and inst and pos <= inst[0]:
            return inst[0] + 1
        return pos

    def move_device(self, device, target, target_position):
        # Luecken-Zaehlung wie in Lives Doku: 0 = vor dem ersten Geraet,
        # len(devices) = ans Ende, das bewegte Geraet mitgezaehlt.
        source = self._owner_of(device)
        gap = int(target_position)
        old = next(i for i, d in enumerate(source.devices) if d is device)
        source.devices.pop(old)
        if source is target and old < gap:
            gap -= 1
        target.devices.insert(gap, device)
        self.undo_log.append("move_device")
        return gap

    def set_or_delete_cue(self):
        t = self.current_song_time
        for c in self.cue_points:
            if abs(c.time - t) < 1e-3:
                self.cue_points.remove(c)
                return
        self.cue_points.append(CuePoint("", t))

    def create_midi_track(self, index):
        t = Track("%d-MIDI" % (len(self.tracks) + 1), "midi")
        t._song = self
        if index == -1:
            self.tracks.append(t)
        else:
            self.tracks.insert(index, t)

    def create_audio_track(self, index):
        t = Track("%d-Audio" % (len(self.tracks) + 1), "audio")
        t._song = self
        if index == -1:
            self.tracks.append(t)
        else:
            self.tracks.insert(index, t)

    def start_playing(self):
        # Wie Live am 12.10.2026: Start am Startmarker (hier Takt 1), nicht am Playhead.
        self.is_playing = True
        self._song_time = 0.0

    def stop_playing(self):
        self.is_playing = False


class AppView(LomObject):
    def __init__(self):
        self.focused_document_view = "Arranger"

    def show_view(self, name):
        self.focused_document_view = name


class BrowserItem(LomObject):
    def __init__(self, name, children=None, uri=None, loadable=False):
        self.name = name
        self.children = children or []
        self.uri = uri or ("query:" + name)
        self.is_loadable = loadable
        self.is_device = loadable
        self.is_folder = bool(children)


class Browser(LomObject):
    def __init__(self):
        self.instruments = BrowserItem("Instruments", [BrowserItem("Drift", uri="query:Synths#Drift", loadable=True)])
        self.sounds = BrowserItem("Sounds")
        self.drums = BrowserItem("Drums")
        self.audio_effects = BrowserItem("Audio Effects", [BrowserItem("Utility", uri="query:AudioFx#Utility",
                                                                       loadable=True)])
        self.midi_effects = BrowserItem("MIDI Effects")
        self.loaded = []
        self.song = None

    def load_item(self, item):
        # Wie am Set am 11.10.2026 beobachtet: Das Geraet landet am Kettenende
        # der ausgewaehlten Spur.
        self.loaded.append(item.name)
        track = self.song.view.selected_track if self.song is not None else None
        if track is not None:
            instrument = item.name in self.INSTRUMENTS
            track.devices.append(Device(item.name, item.name.replace(" ", ""), dtype=1 if instrument else 2))
            # Nach dem Laden eines Instruments war die neue MIDI-Spur
            # scharf. Ob das an der Auswahl oder am Instrument liegt, ist am
            # Set noch offen; die Attrappe macht es beim Instrument.
            if instrument and track.has_midi_input:
                # Am Set 12.10.2026: "15-MIDI" hiess nach dem Laden "15-DS Kick".
                num, _, rest = track.name.partition("-")
                if num.isdigit() and rest == "MIDI":
                    track.name = "%s-%s" % (num, item.name)
                track.arm = True
                # Lives "Exclusive Arm": Ob das beim Auto-Arm andere Spuren
                # ausschaltet, ist am Set ungeprueft. Nur zum Testen der
                # Erkennung im Server, standardmaessig aus.
                if self.song.exclusive_arm:
                    for t in self.song.tracks:
                        if t is not track and t.can_be_armed and t.arm:
                            t.arm = False

    INSTRUMENTS = ("Drift",)


class Application(LomObject):
    def __init__(self):
        self.view = AppView()
        self.browser = Browser()


# ----------------------------------------------------------------------
# Modul-Stubs und Hauptthread
# ----------------------------------------------------------------------

class CInstance:
    """Haelt Song, Application und den simulierten Hauptthread.

    Der Hauptthread ruft wie Live regelmaessig update_display des Control
    Surface auf (hier alle 20 ms statt 100 ms, damit die Tests schnell
    bleiben) und fuehrt schedule_message-Auftraege nach ihrer Verzoegerung
    aus (1 Tick = 100 ms wie in Live). Vor jedem Durchlauf uebernimmt der
    Song einen gesetzten Playhead, wie Live im naechsten Durchlauf.
    """

    DISPLAY_INTERVAL = 0.02

    def __init__(self, song, app):
        self.song = song
        self.app = app
        self.tasks = queue.Queue()
        self.timers = queue.Queue()
        self.surface = None
        self.update_display_enabled = True
        self.log = []
        self.running = True
        self.thread = threading.Thread(target=self._loop, name=MAIN_THREAD_NAME, daemon=True)
        self.thread.start()

    def _run(self, cb):
        self.song.apply_pending()
        try:
            cb()
        except Exception as e:  # noqa: BLE001
            self.log.append("TASK-FEHLER: %r" % e)

    def _loop(self):
        pending = []
        next_display = time.monotonic()
        while self.running:
            try:
                cb = self.tasks.get(timeout=0.005)
            except queue.Empty:
                cb = None
            if cb is not None:
                self._run(cb)
            while True:
                try:
                    pending.append(self.timers.get_nowait())
                except queue.Empty:
                    break
            now = time.monotonic()
            due = [t for t in pending if t[0] <= now]
            pending = [t for t in pending if t[0] > now]
            for _, timer_cb in due:
                self._run(timer_cb)
            if self.surface is not None and self.update_display_enabled and now >= next_display:
                self._run(self.surface.update_display)
                next_display = now + self.DISPLAY_INTERVAL

    def run_on_main(self, fn):
        box = queue.Queue()
        self.tasks.put(lambda: box.put(fn()))
        return box.get(timeout=5)

    def stop(self):
        self.running = False


def install_stubs():
    framework = types.ModuleType("_Framework")
    cs_mod = types.ModuleType("_Framework.ControlSurface")

    class ControlSurface:
        def __init__(self, c_instance):
            self._c_instance = c_instance
            c_instance.surface = self

        def song(self):
            return self._c_instance.song

        def application(self):
            return self._c_instance.app

        def log_message(self, *args):
            self._c_instance.log.append(" ".join(str(a) for a in args))

        def show_message(self, msg):
            self._c_instance.log.append("SHOW: " + msg)

        def schedule_message(self, delay, callback, parameter=None):
            self._c_instance.timers.put((time.monotonic() + 0.1 * delay, callback))

        def update_display(self):
            pass

        def disconnect(self):
            pass

    cs_mod.ControlSurface = ControlSurface
    framework.ControlSurface = cs_mod
    sys.modules["_Framework"] = framework
    sys.modules["_Framework.ControlSurface"] = cs_mod

    live = types.ModuleType("Live")
    clip_mod = types.SimpleNamespace()
    clip_mod.MidiNoteSpecification = MidiNoteSpecification
    clip_mod.Clip = Clip
    clip_mod.AutomationEnvelope = Envelope
    live.Clip = clip_mod
    sys.modules["Live"] = live


def build_demo_set():
    """Kleines Techno-Set als Attrappe: Gruppen, deaktivierte Intro-Clips, Returns."""
    song = Song()
    swing = Groove("Swing 16ths 66")
    song.groove_pool = GroovePool([swing])
    names = ["INTRO", "AUFBAU", "GROOVE 1", "BREAK"]
    song.cue_points = [CuePoint(n, i * 64.0) for i, n in enumerate(names)]

    low = Track("LOW BUS", "group", devices=[Device("Insight 2", "PluginDevice")])
    kick_clips = [Clip("KICK", float(b), 0.25, notes=[Note(36, 0.0, 0.25, 110)]) for b in range(0, 64)]
    kick = Track("KICK BODY", "midi", group=low, volume_db=-12.0,
                 devices=[Device("Serum 2", "PluginDevice", dtype=1,
                                 params=[Param("Macro 1", 0.2), Param("Cutoff", 0.5, automation_state=1)]),
                          Device("Saturator", "Saturator", params=[Param("Drive", 0.3, 0.0, 1.0)], active=False)],
                 clips=kick_clips, activator=0.0, activator_auto=1)
    rumble_clips = [Clip("RUMBLE", float(b * 4), 4.0, midi=False, muted=b < 4) for b in range(16)]
    rumble = Track("RUMBLE", "audio", group=low, volume_db=-8.0, clips=rumble_clips)
    drums = Track("DRUMS", "group")
    hat_notes = [Note(42, 0.5 + i, 0.25, 90 + i) for i in range(4)]
    hat_clips = [Clip("HAT", float(b * 4), 4.0, muted=b < 8, notes=hat_notes, groove=swing) for b in range(16)]
    hat = Track("HAT CLOSED", "midi", group=drums, volume_db=-15.9, clips=hat_clips)
    synth = Track("SYNTH PULSE", "midi", volume_db=-18.0, sends_db=[None, -24.1], send_auto={1: 1},
                  clips=[Clip("PULSE", 0.0, 16.0, notes=[Note(48, 0.0, 0.5, 100), Note(55, 1.0, 0.5, 80)])])
    air = Track("ATMO PAD", "midi", volume_db=-20.0, arm=True,
                clips=[Clip("AIR", 0.0, 64.0, notes=[Note(36, 0.0, 64.0, 70)])])
    # DS Kick wie am Set (12.10.2026 ueber find_parameter_value): Pitch roh
    # 30..200, Anzeige 30..200 ohne Einheit (Hz steht nur in der Max-Oberflaeche),
    # Potenzkurve mit Exponent ~3,33 (105,1 -> 41,17). Lives genaue Kurve ist
    # unbekannt.
    pitch = Param("Pitch", 105.1, 30.0, 200.0, lambda v: "%.2f" % (30.0 + 170.0 * ((v - 30.0) / 170.0) ** 3.33))
    sub = Track("KICK SUB", "midi", volume_db=-12.0,
                devices=[Device("DS Kick", "MxDeviceInstrument", dtype=1,
                                params=[pitch, Param("Decay", 0.361, 0.0, 1.0, lambda v: "%.1f %%" % (v * 100))])])
    song.tracks = [low, kick, rumble, drums, hat, synth, air, sub]
    song.file_path = "D:\\Sets\\Demo_Set.als"
    ret_a = Track("REVERB SPACE", "return", devices=[Device("Reverb", "Reverb")])
    ret_b = Track("DELAY THROW", "return", devices=[Device("Delay", "Delay")])
    song.return_tracks = [ret_a, ret_b]
    song.master_track = Track("Main", "master", devices=[Device("Utility", "StereoGain")])
    for t in song.tracks + song.return_tracks + [song.master_track]:
        t._song = song
    # Wie am Set: Compressor auf der Gruppe DRUMS hoert die Kick ab.
    drums.devices.append(SidechainDevice("Compressor", "Compressor2", song, drums, source="KICK BODY",
                                         channel="Post FX", params=[Param("Threshold", 0.5)]))
    song.view.selected_track = hat
    song.view.detail_clip = hat_clips[8]
    song.view.selected_parameter = kick.mixer_device.volume
    app = Application()
    app.browser.song = song
    return song, app
