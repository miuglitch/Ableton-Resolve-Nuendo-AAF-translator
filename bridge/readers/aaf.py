"""Read an AAF exported from DaVinci Resolve (or Nuendo / Pro Tools)."""
from __future__ import annotations

import os
from fractions import Fraction
from urllib.parse import unquote, urlparse

import aaf2

from ..model import Clip, Marker, Timeline, Track, VolumePoint
from ..timecode import lin_to_db

EMBEDDED = "aaf-embedded://"


def _rate(r) -> Fraction:
    return Fraction(r.numerator, r.denominator) if hasattr(r, "numerator") else Fraction(r)


def _url_to_path(url: str) -> str:
    if not url:
        return ""
    if url.lower().startswith("file:"):
        u = urlparse(url)
        p = unquote(u.path)
        if u.netloc and u.netloc.lower() not in ("", "localhost"):
            return "//" + u.netloc + p
        if len(p) > 2 and p[0] == "/" and p[2] == ":":
            p = p[1:]
        return p
    return unquote(url)


def _locators(mob):
    try:
        desc = mob.descriptor
    except Exception:
        return []
    out = []
    descs = [desc]
    try:
        if "FileDescriptors" in desc.keys():
            descs += list(desc["FileDescriptors"].value)
    except Exception:
        pass
    for d in descs:
        try:
            if "Locator" in d.keys():
                for loc in d["Locator"].value:
                    url = loc["URLString"].value
                    if url:
                        out.append(_url_to_path(url))
        except Exception:
            continue
    return out


def _find_source_clip(seg):
    """First SourceClip inside a segment (Sequence / OperationGroup / clip)."""
    if isinstance(seg, aaf2.components.SourceClip):
        return seg
    if isinstance(seg, aaf2.components.Sequence):
        for c in seg.components:
            sc = _find_source_clip(c)
            if sc is not None:
                return sc
    if isinstance(seg, aaf2.components.OperationGroup):
        for s in seg.segments:
            sc = _find_source_clip(s)
            if sc is not None:
                return sc
    return None


def resolve_source(sc, edit_rate: Fraction):
    """Follow SourceClip -> MasterMob -> file SourceMob.

    Returns (path, seconds_into_file, channel(1-based) or None, mob_id)."""
    offset = Fraction(sc.start or 0) / edit_rate
    mob, slot = sc.mob, sc.slot
    channel = None
    for _ in range(12):
        if mob is None or slot is None:
            break
        if isinstance(mob, aaf2.mobs.SourceMob):
            sound_slots = [s for s in mob.slots if s.media_kind == "Sound"]
            if slot in sound_slots and len(sound_slots) > 1:
                channel = sound_slots.index(slot) + 1
            try:
                ptn = slot["PhysicalTrackNumber"].value
                if ptn and len(sound_slots) > 1:
                    channel = int(ptn)
            except Exception:
                pass
            locs = _locators(mob)
            if locs:
                return locs[0], offset, channel, str(mob.mob_id)
            if mob.essence is not None:
                return EMBEDDED + str(mob.mob_id), offset, channel, str(mob.mob_id)
        inner = _find_source_clip(slot.segment)
        if inner is None or inner.mob is None:
            break
        rate = _rate(slot.edit_rate)
        offset += Fraction(inner.start or 0) / rate
        mob, slot = inner.mob, inner.slot
    return "", offset, channel, ""


def _param(og, *names):
    for p in og.parameters:
        try:
            pname = p.name or ""
        except Exception:
            pname = ""
        if any(n.lower() in pname.lower() for n in names):
            return p
    return None


class _Reader:
    def __init__(self, f, comp):
        self.f = f
        self.comp = comp
        self.warnings = []

    def segment_clips(self, seg, rate, pos, length, ctx):
        """Yield Clip objects for a segment placed at `pos` (edit units)."""
        if isinstance(seg, aaf2.components.SourceClip):
            if seg.mob is None:
                return []
            path, src_in, ch, mob_id = resolve_source(seg, rate)
            if not path:
                self.warnings.append(f"Clip at {float(pos / rate):.2f}s has no media reference — skipped.")
                return []
            name = (seg.mob.name if seg.mob is not None else "") or os.path.basename(path)
            c = Clip(name=name, start=Fraction(pos) / rate, duration=Fraction(length) / rate,
                     source_path=path, source_in=src_in,
                     channels=[ch] if ch else [])
            c.gain_db = ctx.get("gain_db", 0.0)
            c.pan = ctx.get("pan")
            c.volume = list(ctx.get("volume", []))
            return [c]
        if isinstance(seg, aaf2.components.OperationGroup):
            ctx = dict(ctx)
            opname = ""
            try:
                opname = seg.operation.name or ""
            except Exception:
                pass
            low = opname.lower()
            if "gain" in low or "level" in low or "volume" in low:
                p = _param(seg, "amplitude", "level", "gain", "volume")
                if p is not None:
                    if isinstance(p, aaf2.misc.VaryingValue):
                        pts = []
                        for cp in p.pointlist:
                            t = Fraction(cp.time) * Fraction(length) / rate
                            pts.append(VolumePoint(t, lin_to_db(float(cp.value))))
                        ctx["volume"] = pts
                    else:
                        ctx["gain_db"] = ctx.get("gain_db", 0.0) + lin_to_db(float(p.value))
            elif "pan" in low:
                p = _param(seg, "pan")
                if p is not None and not isinstance(p, aaf2.misc.VaryingValue):
                    ctx["pan"] = float(p.value) * 2 - 1      # AAF 0..1 -> -1..1
            out = []
            for s in list(seg.segments)[:1]:
                out += self.segment_clips(s, rate, pos, length, ctx)
            return out
        if isinstance(seg, aaf2.components.Sequence):
            return self.sequence(seg, rate, pos, ctx)
        return []

    def sequence(self, seq, rate, pos=0, ctx=None):
        ctx = ctx or {}
        clips = []
        pending_fade_in = 0
        for comp in seq.components:
            length = comp.length or 0
            if isinstance(comp, aaf2.components.Transition):
                pos -= length
                if clips:
                    clips[-1].fade_out = Fraction(length) / rate
                pending_fade_in = length
                continue
            if isinstance(comp, aaf2.components.Filler):
                pos += length
                pending_fade_in = 0
                continue
            new = self.segment_clips(comp, rate, pos, length, ctx)
            if new and pending_fade_in:
                new[0].fade_in = Fraction(pending_fade_in) / rate
            pending_fade_in = 0
            clips += new
            pos += length
        return clips


def read(path: str) -> Timeline:
    f = aaf2.open(path, "r")
    comps = list(f.content.toplevel()) or list(f.content.compositionmobs())
    if not comps:
        raise ValueError("No timeline (composition) found in this AAF.")
    comp = comps[0]
    r = _Reader(f, comp)

    rate = Fraction(25)
    start = Fraction(0)
    drop = False
    tracks: list[Track] = []
    markers: list[Marker] = []
    picture_rate = None

    for slot in comp.slots:
        kind = slot.media_kind
        er = _rate(slot.edit_rate)
        seg = slot.segment
        if kind == "Timecode":
            tc = seg
            if isinstance(seg, aaf2.components.Sequence):
                tc = next((c for c in seg.components
                           if isinstance(c, aaf2.components.Timecode)), None)
            if tc is not None:
                fps = int(tc.fps)
                drop = bool(tc.drop)
                tc_rate = Fraction(fps * 1000, 1001) if er.denominator == 1001 else Fraction(fps)
                rate = tc_rate
                start = Fraction(tc.start or 0) / tc_rate
        elif kind == "Picture":
            picture_rate = er
            clips = r.sequence(seg, er) if isinstance(seg, aaf2.components.Sequence) \
                else r.segment_clips(seg, er, 0, seg.length or 0, {})
            if clips:
                tracks.append(Track(name=slot.name or f"V{len([t for t in tracks if t.kind=='video'])+1}",
                                    kind="video", clips=clips))
        elif kind == "Sound":
            clips = r.sequence(seg, er) if isinstance(seg, aaf2.components.Sequence) \
                else r.segment_clips(seg, er, 0, seg.length or 0, {})
            n = len([t for t in tracks if t.kind == "audio"]) + 1
            tracks.append(Track(name=slot.name or f"A{n}", kind="audio", clips=clips))
        elif kind in ("DescriptiveMetadata", "Event") or "Event" in type(slot).__name__:
            comps_ = seg.components if isinstance(seg, aaf2.components.Sequence) else [seg]
            for m in comps_:
                try:
                    pos = Fraction(m["Position"].value) / er
                    note = m["Comment"].value if "Comment" in m.keys() else ""
                    markers.append(Marker(time=pos, name=note or "Marker", note=note))
                except Exception:
                    continue

    if picture_rate is not None and rate == 25 and picture_rate != 25:
        rate = picture_rate

    # Composition positions are relative to the composition start; shift onto
    # the timecode clock so the model matches FCPXML (start = 01:00:00:00 etc.)
    for t in tracks:
        for c in t.clips:
            c.start += start
    for m in markers:
        m.time += start

    tl = Timeline(name=comp.name or os.path.splitext(os.path.basename(path))[0],
                  frame_rate=rate, drop_frame=drop, start=start, tracks=tracks,
                  markers=sorted(markers, key=lambda m: m.time),
                  source_format="aaf", source_file=os.path.abspath(path))
    tl.warnings += r.warnings
    _merge_split_mono(tl)
    f.close()
    return tl


def _merge_split_mono(tl: Timeline):
    """Resolve/Pro Tools write stereo as two mono tracks (L/R) with identical
    edits. Combine such pairs into one stereo track for Live."""
    audio = tl.audio_tracks
    merged = set()
    i = 0
    while i < len(audio) - 1:
        a, b = audio[i], audio[i + 1]
        same_edit = (len(a.clips) == len(b.clips) and len(a.clips) > 0 and all(
            ca.start == cb.start and ca.duration == cb.duration and ca.source_path == cb.source_path
            and ca.source_in == cb.source_in and ca.channels != cb.channels
            for ca, cb in zip(a.clips, b.clips)))
        if same_edit:
            for ca, cb in zip(a.clips, b.clips):
                ca.channels = sorted(set(ca.channels + cb.channels))
            a.channels = 2
            merged.add(id(b))
            i += 2
        else:
            i += 1
    tl.tracks = [t for t in tl.tracks if id(t) not in merged]
    for t in tl.audio_tracks:
        if all(len(c.channels) == 1 for c in t.clips) and t.clips:
            t.channels = 1


def extract_embedded(aaf_path: str, mob_id: str, out_wav: str) -> str:
    """Write embedded AAF audio essence to a WAV file."""
    with aaf2.open(aaf_path, "r") as f:
        for mob in f.content.sourcemobs():
            if str(mob.mob_id) == mob_id:
                mob.export_audio(out_wav)
                return out_wav
    raise FileNotFoundError(f"Embedded media {mob_id} not found in {aaf_path}")
