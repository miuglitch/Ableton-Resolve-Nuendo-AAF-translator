"""Write an Ableton Live Set (.als) from the timeline model.

An .als file is gzip-compressed XML. Rather than inventing hundreds of
elements Live expects, the writer starts from a *template set saved by Live
itself* (see README: "Make the Live template"), clones its audio track and
audio clip for every track/clip it needs, and fills in names, positions,
file references, gain, fades and automation. Sets written this way open in
the Live version the template was saved with and in newer versions.
"""
from __future__ import annotations

import copy
import gzip
import os
import wave
from fractions import Fraction

from lxml import etree

from ..model import Clip, Timeline, Track
from ..timecode import db_to_lin

# ---------------------------------------------------------------------------
# Unit choices that must be confirmed against a set saved by Live.
# Kept in one place so a test in Live only ever needs to change these.
FADE_UNITS = "beats"        # arrangement clip fade lengths: 'beats' or 'seconds'
MAX_TRACK_GAIN = 1.99526    # Live mixer volume max (+6 dB), linear
MAX_CLIP_GAIN = 15.8489     # Live clip gain max (+24 dB), linear
EARLY = "-63072000"         # Live's "before the song" time for default events
# ---------------------------------------------------------------------------

# Used only when the template contains no audio clip at all.
BUILTIN_CLIP = """<AudioClip Id="0" Time="0">
<LomId Value="0"/><LomIdView Value="0"/><CurrentStart Value="0"/><CurrentEnd Value="4"/>
<Loop><LoopStart Value="0"/><LoopEnd Value="2"/><StartRelative Value="0"/><LoopOn Value="false"/>
<OutMarker Value="2"/><HiddenLoopStart Value="0"/><HiddenLoopEnd Value="2"/></Loop>
<Name Value=""/><Annotation Value=""/><Color Value="13"/><LaunchMode Value="0"/>
<LaunchQuantisation Value="0"/>
<TimeSignature><TimeSignatures><RemoteableTimeSignature Id="0"><Numerator Value="4"/>
<Denominator Value="4"/><Time Value="0"/></RemoteableTimeSignature></TimeSignatures></TimeSignature>
<Envelopes><Envelopes/></Envelopes>
<ScrollerTimePreserver><LeftTime Value="0"/><RightTime Value="0"/></ScrollerTimePreserver>
<TimeSelection><AnchorTime Value="0"/><OtherTime Value="0"/></TimeSelection>
<Legato Value="false"/><Ram Value="false"/><GrooveSettings><GrooveId Value="-1"/></GrooveSettings>
<Disabled Value="false"/><VelocityAmount Value="0"/>
<FreezeStart Value="0"/><FreezeEnd Value="0"/><IsWarped Value="false"/><TakeId Value="1"/>
<SampleRef><FileRef><RelativePathType Value="0"/><RelativePath Value=""/><Path Value=""/>
<Type Value="1"/><LivePackName Value=""/><LivePackId Value=""/><OriginalFileSize Value="0"/>
<OriginalCrc Value="0"/></FileRef><LastModDate Value="0"/><SourceContext/>
<SampleUsageHint Value="0"/><DefaultDuration Value="0"/><DefaultSampleRate Value="48000"/></SampleRef>
<Onsets><UserOnsets/><HasUserOnsets Value="false"/></Onsets>
<WarpMode Value="0"/><GranularityTones Value="30"/><GranularityTexture Value="65"/>
<FluctuationTexture Value="25"/><TransientResolution Value="6"/><TransientLoopMode Value="2"/>
<TransientEnvelope Value="100"/><ComplexProFormants Value="100"/><ComplexProEnvelope Value="128"/>
<Sync Value="true"/><HiQ Value="true"/>
<Fades><FadeInLength Value="0"/><FadeOutLength Value="0"/><ClipFadesAreInitialized Value="true"/>
<CrossfadeInState Value="0"/><FadeInCurveSkew Value="0"/><FadeInCurveSlope Value="0"/>
<FadeOutCurveSkew Value="0"/><FadeOutCurveSlope Value="0"/><IsDefaultFadeIn Value="true"/>
<IsDefaultFadeOut Value="true"/></Fades>
<PitchCoarse Value="0"/><PitchFine Value="0"/><SampleVolume Value="1"/>
<WarpMarkers><WarpMarker Id="0" SecTime="0" BeatTime="0"/><WarpMarker Id="1" SecTime="1" BeatTime="2"/></WarpMarkers>
<SavedWarpMarkersForStretched/><MarkersGenerated Value="false"/><IsSongTempoLeader Value="false"/>
</AudioClip>"""

LIVE_COLORS = {"ORIG": 13, "SD": 2, "PICTURE": 25, None: 13}


def load_als(path: str) -> etree._ElementTree:
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    parser = etree.XMLParser(remove_blank_text=False, huge_tree=True)
    return etree.ElementTree(etree.fromstring(data, parser))


def save_als(tree: etree._ElementTree, path: str) -> None:
    data = etree.tostring(tree, xml_declaration=True, encoding="UTF-8")
    with gzip.open(path, "wb") as fh:
        fh.write(data)


def _v(el, path: str, value) -> None:
    """Set <path Value="..."> if the element exists."""
    node = el.find(path)
    if node is not None:
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, float):
            value = repr(value)
        node.set("Value", str(value))


def _num(x) -> str:
    f = float(x)
    return repr(f) if f != int(f) else str(int(f))


class IdAllocator:
    """Live requires unique pointee ids across the whole set."""

    def __init__(self, root):
        self.root = root
        npi = root.find(".//NextPointeeId")
        mx = 0
        for el in root.iter():
            if self.is_pointee(el):
                try:
                    mx = max(mx, int(el.get("Id")))
                except (TypeError, ValueError):
                    pass
        self.next = max(mx + 1, int(npi.get("Value")) if npi is not None else 0)
        self.npi = npi

    @staticmethod
    def is_pointee(el) -> bool:
        tag = el.tag if isinstance(el.tag, str) else ""
        return el.get("Id") is not None and (
            tag.endswith("Target") or tag == "Pointee" or tag.startswith("ControllerTargets."))

    def take(self) -> int:
        n = self.next
        self.next += 1
        return n

    def renumber(self, subtree) -> dict[str, str]:
        mapping = {}
        for el in subtree.iter():
            if self.is_pointee(el):
                new = str(self.take())
                mapping[el.get("Id")] = new
                el.set("Id", new)
        # internal references (e.g. envelopes pointing at this track's targets)
        for el in subtree.iter("PointeeId"):
            if el.get("Value") in mapping:
                el.set("Value", mapping[el.get("Value")])
        return mapping

    def finish(self):
        if self.npi is not None:
            self.npi.set("Value", str(self.next))


class TemplateError(RuntimeError):
    pass


class AlsBuilder:
    def __init__(self, template_path: str):
        self.tree = load_als(template_path)
        self.root = self.tree.getroot()
        self.liveset = self.root.find("LiveSet")
        if self.liveset is None:
            raise TemplateError("Template is not a Live Set.")
        self.tracks_el = self.liveset.find("Tracks")
        protos = self.tracks_el.findall("AudioTrack")
        if not protos:
            raise TemplateError("Template has no audio track. Save the template with one audio track.")
        self.track_proto = copy.deepcopy(protos[0])
        # Prefer a clip Live wrote itself (Arrangement first, then anywhere in the
        # set, e.g. Session view); otherwise use the built-in clip definition.
        clip = protos[0].find(".//MainSequencer/Sample/ArrangerAutomation/Events/AudioClip")
        if clip is None:
            clip = self.root.find(".//AudioClip")
        self.used_builtin_clip = clip is None
        if clip is None:
            clip = etree.fromstring(BUILTIN_CLIP)
        self.clip_proto = copy.deepcopy(clip)
        for slot in self.clip_proto.iter("ClipSlot"):      # never nest session data
            slot.getparent().remove(slot)
        loc = self.liveset.find("Locators/Locators/Locator")
        self.locator_proto = copy.deepcopy(loc) if loc is not None else None
        # remove existing audio/MIDI/group tracks; keep returns
        for t in list(self.tracks_el):
            if t.tag in ("AudioTrack", "MidiTrack", "GroupTrack"):
                self.tracks_el.remove(t)
        locs = self.liveset.find("Locators/Locators")
        if locs is not None:
            for l in list(locs):
                locs.remove(l)
        self.ids = IdAllocator(self.root)
        self.track_ids = set()
        for t in self.tracks_el:
            try:
                self.track_ids.add(int(t.get("Id")))
            except (TypeError, ValueError):
                pass
        self.insert_at = 0
        self.tempo = 120.0

    # -- set-level -------------------------------------------------------
    def set_tempo(self, bpm: float):
        self.tempo = bpm
        main = self.liveset.find("MainTrack")
        if main is None:
            main = self.liveset.find("MasterTrack")
        tempo = main.find(".//Mixer/Tempo") if main is not None else None
        if tempo is None:
            return
        _v(tempo, "Manual", float(bpm))
        tgt = tempo.find("AutomationTarget")
        if tgt is None:
            return
        for env in main.iter("AutomationEnvelope"):
            pid = env.find("EnvelopeTarget/PointeeId")
            if pid is not None and pid.get("Value") == tgt.get("Id"):
                for ev in env.iter("FloatEvent"):
                    ev.set("Value", _num(bpm))

    def beats(self, seconds: Fraction) -> float:
        return float(seconds * Fraction(self.tempo) / 60)

    def add_locator(self, seconds: Fraction, name: str, idx: int):
        locs = self.liveset.find("Locators/Locators")
        if locs is None:
            return
        if self.locator_proto is not None:
            loc = copy.deepcopy(self.locator_proto)
        else:
            loc = etree.fromstring(
                '<Locator Id="0"><LomId Value="0"/><Time Value="0"/><Name Value=""/>'
                '<Annotation Value=""/><IsSongStart Value="false"/></Locator>')
        loc.set("Id", str(idx))
        _v(loc, "Time", self.beats(seconds))
        _v(loc, "Name", name)
        _v(loc, "IsSongStart", False)
        loc.tail = "\n"
        locs.append(loc)

    # -- tracks ----------------------------------------------------------
    def add_track(self, name: str, clips: list[Clip], offset: Fraction,
                  color: int | None = None, warnings: list | None = None) -> None:
        t = copy.deepcopy(self.track_proto)
        new_id = (max(self.track_ids) + 1) if self.track_ids else 8
        self.track_ids.add(new_id)
        t.set("Id", str(new_id))
        self.ids.renumber(t)
        _v(t, "Name/EffectiveName", name)
        _v(t, "Name/UserName", name)
        if color is not None:
            _v(t, "Color", color)
            _v(t, "ColorIndex", color)
        _v(t, "TrackGroupId", -1)

        events = t.find(".//MainSequencer/Sample/ArrangerAutomation/Events")
        for e in list(events):
            events.remove(e)
        envs = t.find("AutomationEnvelopes/Envelopes")
        if envs is not None:
            for e in list(envs):
                envs.remove(e)

        cid = 0
        auto_points = []
        for c in clips:
            if not c.wav_path:
                continue
            live_start = c.start - offset
            if live_start < 0:
                if warnings is not None:
                    warnings.append(f"{name} / {c.name}: starts before the offset point — skipped. "
                                    "Lower the offset to include it.")
                continue
            events.append(self._clip(c, live_start, cid, color))
            cid += 1
            if c.volume:
                auto_points.append((live_start, c))

        if auto_points and envs is not None:
            self._volume_envelope(t, envs, auto_points)

        t.tail = "\n"
        self.tracks_el.insert(self.insert_at, t)
        self.insert_at += 1

    def _clip(self, c: Clip, live_start: Fraction, cid: int, color) -> etree._Element:
        el = copy.deepcopy(self.clip_proto)
        start_b = self.beats(live_start)
        end_b = self.beats(live_start + c.duration)
        src_in = float(c.source_in + c.wav_offset)
        src_out = src_in + float(c.duration)
        el.set("Id", str(cid))
        el.set("Time", _num(start_b))
        _v(el, "CurrentStart", start_b)
        _v(el, "CurrentEnd", end_b)
        # Unwarped arrangement clips: loop/marker positions are in sample seconds
        _v(el, "Loop/LoopStart", src_in)
        _v(el, "Loop/LoopEnd", src_out)
        _v(el, "Loop/StartRelative", 0)
        _v(el, "Loop/LoopOn", False)
        _v(el, "Loop/OutMarker", src_out)
        _v(el, "Loop/HiddenLoopStart", src_in)
        _v(el, "Loop/HiddenLoopEnd", src_out)
        _v(el, "Name", c.name)
        if color is not None:
            _v(el, "Color", color)
            _v(el, "ColorIndex", color)
        _v(el, "Disabled", not c.enabled)
        _v(el, "IsWarped", False)
        _v(el, "SampleVolume", min(db_to_lin(c.gain_db), MAX_CLIP_GAIN))
        _v(el, "PitchCoarse", 0)
        _v(el, "PitchFine", 0)
        # fades
        fi, fo = c.fade_in, c.fade_out
        if FADE_UNITS == "beats":
            fi, fo = self.beats(fi), self.beats(fo)
        _v(el, "Fades/FadeInLength", float(fi))
        _v(el, "Fades/FadeOutLength", float(fo))
        _v(el, "Fades/IsDefaultFadeIn", c.fade_in == 0)
        _v(el, "Fades/IsDefaultFadeOut", c.fade_out == 0)
        _v(el, "Fades/ClipFadesAreInitialized", True)
        # warp markers: 1:1 mapping at the set tempo (clip is unwarped anyway)
        wm = el.find("WarpMarkers")
        if wm is not None:
            for m in list(wm):
                wm.remove(m)
            for i, sec in enumerate((0.0, 1.0)):
                m = etree.SubElement(wm, "WarpMarker")
                m.set("Id", str(i))
                m.set("SecTime", _num(sec))
                m.set("BeatTime", _num(sec * self.tempo / 60))
        # clip envelopes from the template clip don't apply
        ce = el.find("Envelopes/Envelopes")
        if ce is not None:
            for e in list(ce):
                ce.remove(e)
        self._sample_ref(el, c.wav_path)
        return el

    def _sample_ref(self, clip_el, path: str):
        ref = clip_el.find("SampleRef")
        fr = ref.find("FileRef")
        p = os.path.abspath(path).replace("\\", "/")
        _v(fr, "RelativePathType", 0)
        _v(fr, "RelativePath", "")
        _v(fr, "Path", p)
        _v(fr, "Type", 1)
        _v(fr, "LivePackName", "")
        _v(fr, "LivePackId", "")
        _v(fr, "OriginalFileSize", os.path.getsize(path) if os.path.exists(path) else 0)
        _v(fr, "OriginalCrc", 0)
        # Live 10-style hex path data, if the template still has it
        data = fr.find("Data")
        if data is not None:
            data.text = ""
        _v(ref, "LastModDate", int(os.path.getmtime(path)) if os.path.exists(path) else 0)
        frames, sr = wav_info(path)
        _v(ref, "DefaultDuration", frames)
        _v(ref, "DefaultSampleRate", sr)

    def _volume_envelope(self, track, envs, auto_points):
        vol_target = track.find(".//DeviceChain/Mixer/Volume/AutomationTarget")
        if vol_target is None:
            return
        env = etree.SubElement(envs, "AutomationEnvelope")
        env.set("Id", str(len(envs) - 1))
        tgt = etree.SubElement(env, "EnvelopeTarget")
        etree.SubElement(tgt, "PointeeId").set("Value", vol_target.get("Id"))
        auto = etree.SubElement(env, "Automation")
        evs = etree.SubElement(auto, "Events")
        n = 0

        def ev(time, val):
            nonlocal n
            e = etree.SubElement(evs, "FloatEvent")
            e.set("Id", str(n))
            e.set("Time", time if isinstance(time, str) else _num(time))
            e.set("Value", _num(min(val, MAX_TRACK_GAIN)))
            n += 1

        ev(EARLY, 1.0)
        tiny = 1 / 48000
        for live_start, c in sorted(auto_points, key=lambda x: x[0]):
            pts = sorted(c.volume, key=lambda p: p.time)
            s = float(live_start)
            ev(self.beats(Fraction(s - tiny)) if s > tiny else 0, 1.0)
            ev(self.beats(Fraction(s)), db_to_lin(pts[0].db))
            for p in pts:
                if 0 <= p.time <= c.duration:
                    ev(self.beats(live_start + p.time), db_to_lin(p.db))
            ev(self.beats(live_start + c.duration), db_to_lin(pts[-1].db))
            ev(self.beats(live_start + c.duration) + self.beats(Fraction(tiny)), 1.0)
        ts = etree.SubElement(auto, "AutomationTransformViewState")
        etree.SubElement(ts, "IsTransformPending").set("Value", "false")
        etree.SubElement(ts, "TimeAndValueTransforms")

    def save(self, path: str):
        self.ids.finish()
        save_als(self.tree, path)


def wav_info(path: str) -> tuple[int, int]:
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes(), w.getframerate()
    except Exception:
        pass
    try:                                   # 24-bit/extensible/RF64 WAVs: ask ffprobe
        from ..media import probe
        info = probe(path)
        for s in info.get("streams", []):
            if s.get("codec_type") == "audio":
                sr = int(s.get("sample_rate", 48000))
                frames = int(s.get("duration_ts") or round(float(s.get("duration", 0)) * sr))
                return frames, sr
    except Exception:
        pass
    return 0, 48000
