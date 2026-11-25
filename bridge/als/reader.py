"""Read an Ableton Live Set's arrangement into the neutral timeline model.

Positions come back as Live-seconds (0 = Live's song start); the exporter
adds the offset from the .bridge.json to get back onto the Resolve clock.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from fractions import Fraction

from ..model import Clip, Marker, Track, VolumePoint
from ..timecode import lin_to_db
from .writer import FADE_UNITS, load_als


@dataclass
class LiveTrack:
    track: Track
    kind: str                       # 'audio', 'midi', 'group', 'return'
    has_devices: bool = False
    muted: bool = False
    in_group: bool = False
    has_sends: bool = False
    fader_db: float = 0.0
    volume_auto: list[tuple[Fraction, float]] = field(default_factory=list)  # (seconds, linear)


@dataclass
class LiveSet:
    path: str
    tempo: float
    tempo_automated: bool
    tracks: list[LiveTrack]
    locators: list[Marker]
    warnings: list[str] = field(default_factory=list)


def _val(el, path, default=None):
    n = el.find(path)
    return n.get("Value") if n is not None else default


def _f(el, path, default=0.0):
    try:
        return float(_val(el, path, default))
    except (TypeError, ValueError):
        return default


def _sample_path(clip, set_dir) -> str:
    fr = clip.find("SampleRef/FileRef")
    if fr is None:
        return ""
    p = _val(fr, "Path", "") or ""
    if p and os.path.exists(p):
        return p
    rel = _val(fr, "RelativePath", "") or ""
    if rel:
        cand = os.path.normpath(os.path.join(set_dir, rel))
        if os.path.exists(cand):
            return cand
    return p or rel


def _tempo(liveset):
    main = liveset.find("MainTrack")
    if main is None:
        main = liveset.find("MasterTrack")
    tempo_el = main.find(".//Mixer/Tempo") if main is not None else None
    bpm = _f(tempo_el, "Manual", 120.0) if tempo_el is not None else 120.0
    automated = False
    if tempo_el is not None:
        tid = tempo_el.find("AutomationTarget")
        if tid is not None:
            for env in main.iter("AutomationEnvelope"):
                if _val(env, "EnvelopeTarget/PointeeId") == tid.get("Id"):
                    vals = {ev.get("Value") for ev in env.iter("FloatEvent")}
                    automated = len(vals) > 1
    return bpm, automated


def _envelope(track_el, target_el):
    if target_el is None:
        return []
    out = []
    for env in track_el.iter("AutomationEnvelope"):
        if _val(env, "EnvelopeTarget/PointeeId") == target_el.get("Id"):
            for ev in env.iter("FloatEvent"):
                out.append((float(ev.get("Time")), float(ev.get("Value"))))
    return sorted(out)


def read(path: str) -> LiveSet:
    root = load_als(path).getroot()
    ls = root.find("LiveSet")
    set_dir = os.path.dirname(os.path.abspath(path))
    bpm, tempo_auto = _tempo(ls)
    spb = Fraction(60) / Fraction(str(bpm))          # seconds per beat
    secs = lambda beats: Fraction(str(beats)) * spb  # noqa: E731
    warnings = []
    tracks: list[LiveTrack] = []

    for t in ls.find("Tracks"):
        tag = t.tag
        name = _val(t, "Name/EffectiveName", "") or _val(t, "Name/UserName", "") or tag
        kind = {"AudioTrack": "audio", "MidiTrack": "midi", "GroupTrack": "group",
                "ReturnTrack": "return"}.get(tag)
        if kind is None:
            continue
        lt = LiveTrack(Track(name=name), kind)
        dc = t.find("DeviceChain")
        devices = dc.find("DeviceChain/Devices") if dc is not None else None
        lt.has_devices = devices is not None and len(devices) > 0
        lt.muted = _val(t, "DeviceChain/Mixer/Speaker/Manual", "true") == "false"
        lt.in_group = _val(t, "TrackGroupId", "-1") not in ("-1", None)
        sends = t.findall(".//Mixer/Sends/TrackSendHolder/Send/Manual")
        lt.has_sends = any(float(s.get("Value", "0")) > 0.0004 for s in sends)
        vol = t.find("DeviceChain/Mixer/Volume")
        if vol is not None:
            lt.fader_db = lin_to_db(_f(vol, "Manual", 1.0))
            ev = _envelope(t, vol.find("AutomationTarget"))
            lt.volume_auto = [(secs(b), v) for b, v in ev if b > -1e6]
            if lt.volume_auto:
                early = [v for b, v in ev if b <= -1e6]
                if early:
                    lt.fader_db = lin_to_db(early[-1])
        tracks.append(lt)
        if kind != "audio":
            continue

        for c in t.findall(".//MainSequencer/Sample/ArrangerAutomation/Events/AudioClip"):
            start_b = float(c.get("Time") or _f(c, "CurrentStart"))
            end_b = _f(c, "CurrentEnd")
            dur_b = end_b - _f(c, "CurrentStart")
            loop_start = _f(c, "Loop/LoopStart")
            warped = _val(c, "IsWarped", "false") == "true"
            src_in = Fraction(str(loop_start))
            note = None
            if warped:
                # map the clip's beat position in the sample back to seconds
                wm = [(float(m.get("BeatTime")), float(m.get("SecTime")))
                      for m in c.findall("WarpMarkers/WarpMarker")]
                if len(wm) >= 2:
                    (b0, s0), (b1, s1) = wm[0], wm[-1]
                    sec_per_beat = (s1 - s0) / (b1 - b0) if b1 != b0 else float(spb)
                    src_in = Fraction(str(s0 + (loop_start - b0) * sec_per_beat))
                    if abs(sec_per_beat - float(spb)) > 1e-4 or len(wm) > 2:
                        note = "time-stretched"
                else:
                    note = "warped without markers"
            if _f(c, "PitchCoarse") or _f(c, "PitchFine"):
                note = "transposed"
            clip = Clip(
                name=_val(c, "Name", "") or "",
                start=secs(start_b), duration=secs(dur_b),
                source_path=_sample_path(c, set_dir), source_in=src_in,
                gain_db=lin_to_db(_f(c, "SampleVolume", 1.0)),
                enabled=_val(c, "Disabled", "false") != "true",
            )
            fi, fo = _f(c, "Fades/FadeInLength"), _f(c, "Fades/FadeOutLength")
            if FADE_UNITS == "beats":
                clip.fade_in, clip.fade_out = secs(fi), secs(fo)
            else:
                clip.fade_in, clip.fade_out = Fraction(str(fi)), Fraction(str(fo))
            if note:
                clip.name = clip.name or os.path.basename(clip.source_path)
                warnings.append(f"{name} / {clip.name}: {note}. Consolidate it in Live "
                                "(Ctrl+J) first, or it won't sound the same in Resolve.")
                clip.enabled = clip.enabled  # still exported, flagged
            lt.track.clips.append(clip)
        lt.track.clips.sort(key=lambda c: c.start)

    locs = []
    for l in ls.findall("Locators/Locators/Locator"):
        locs.append(Marker(time=secs(_f(l, "Time")), name=_val(l, "Name", "") or ""))
    if tempo_auto:
        warnings.append("The set's tempo is automated. Positions use the base tempo, so "
                        "audio after a tempo change will be out of place.")
    return LiveSet(path=path, tempo=bpm, tempo_automated=tempo_auto, tracks=tracks,
                   locators=sorted(locs, key=lambda m: m.time), warnings=warnings)


def volume_points(lt: LiveTrack, clip: Clip) -> list[VolumePoint]:
    """Track volume automation over one clip, as clip keyframes (dB)."""
    auto = lt.volume_auto
    if not auto:
        return []

    def at(t):
        prev = None
        for tt, v in auto:
            if tt > t:
                if prev is None:
                    return v
                (t0, v0) = prev
                return v0 + (v - v0) * float((t - t0) / (tt - t0)) if tt != t0 else v
            prev = (tt, v)
        return prev[1]

    pts = [(clip.start, at(clip.start))]
    pts += [(t, v) for t, v in auto if clip.start < t < clip.end]
    pts.append((clip.end, at(clip.end)))
    if len({round(v, 6) for _, v in pts}) == 1:
        return []                                    # flat: handled as gain
    return [VolumePoint(t - clip.start, lin_to_db(v)) for t, v in pts]


def static_level_db(lt: LiveTrack, clip: Clip) -> float:
    """Level to add as clip gain when the automation over this clip is flat."""
    if not lt.volume_auto:
        return lt.fader_db
    t = clip.start
    val = lt.volume_auto[0][1]
    for tt, v in lt.volume_auto:
        if tt <= t:
            val = v
    return lin_to_db(val)
