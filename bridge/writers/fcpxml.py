"""Write an FCPXML 1.10 timeline that DaVinci Resolve imports
(File → Import → Timeline…).

Layout: one gap spanning the whole timeline on the spine, every audio clip
connected to it on its own lane (lane -1 = A1, -2 = A2, …). That is the shape
Resolve itself writes for audio-only timelines.
"""
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field
from fractions import Fraction

from lxml import etree

from ..model import Clip, Marker
from ..timecode import SAMPLE_RATE, fcp_time


@dataclass
class MediaAsset:
    path: str
    duration: Fraction
    start: Fraction = Fraction(0)       # media start timecode, seconds
    channels: int = 2
    sample_rate: int = SAMPLE_RATE
    has_video: bool = False


@dataclass
class OutTrack:
    name: str
    clips: list[Clip] = field(default_factory=list)   # clip.source_path refers to an asset


def _t(x: Fraction, rate: Fraction) -> str:
    """Rational time: whole frames when on a frame, else exact samples."""
    x = Fraction(x)
    if (x * rate).denominator == 1:
        return fcp_time(x, rate)
    return fcp_time(Fraction(round(x * SAMPLE_RATE), SAMPLE_RATE))


def _db(v: float) -> str:
    return f"{v:.2f}dB"


def write(path: str, name: str, rate: Fraction, drop: bool, tc_start: Fraction,
          tracks: list[OutTrack], assets: dict[str, MediaAsset], markers: list[Marker],
          sample_rate: int = 48000) -> str:
    root = etree.Element("fcpxml", version="1.10")
    res = etree.SubElement(root, "resources")
    fd = 1 / rate
    etree.SubElement(res, "format", id="r0", frameDuration=f"{fd.numerator}/{fd.denominator}s",
                     width="1920", height="1080")
    ids = {}
    for i, (key, a) in enumerate(assets.items(), start=1):
        aid = f"r{i}"
        ids[key] = aid
        el = etree.SubElement(
            res, "asset", id=aid, name=os.path.splitext(os.path.basename(a.path))[0],
            start=_t(a.start, rate), duration=_t(a.duration, rate),
            hasAudio="1", audioSources="1", audioChannels=str(a.channels),
            audioRate=str(a.sample_rate))
        if a.has_video:
            el.set("hasVideo", "1")
            el.set("format", "r0")
        etree.SubElement(el, "media-rep", kind="original-media",
                         src=pathlib.Path(os.path.abspath(a.path)).as_uri())

    ends = [c.end for t in tracks for c in t.clips] + [m.time for m in markers]
    total = (max(ends) if ends else tc_start) - tc_start
    total = max(total, 1 / rate)
    total = Fraction(int(-(-total * rate // 1)), 1) / rate          # round up to a frame

    lib = etree.SubElement(root, "library")
    ev = etree.SubElement(lib, "event", name="MIUGLITCH AAF Translator")
    proj = etree.SubElement(ev, "project", name=name)
    sr = f"{sample_rate // 1000}k" if sample_rate % 1000 == 0 else str(sample_rate)
    seq = etree.SubElement(proj, "sequence", format="r0", duration=_t(total, rate),
                           tcStart=_t(tc_start, rate), tcFormat="DF" if drop else "NDF",
                           audioLayout="stereo", audioRate=sr)
    spine = etree.SubElement(seq, "spine")
    gap = etree.SubElement(spine, "gap", name="Gap", offset=_t(tc_start, rate),
                           start=_t(tc_start, rate), duration=_t(total, rate))

    for lane_i, trk in enumerate(tracks, start=1):
        for c in trk.clips:
            a = assets[c.source_path]
            src_start = a.start + c.source_in
            el = etree.SubElement(
                gap, "asset-clip", ref=ids[c.source_path], lane=str(-lane_i),
                offset=_t(c.start, rate), name=c.name or trk.name,
                start=_t(src_start, rate), duration=_t(c.duration, rate),
                audioRole="dialogue")
            if not c.enabled:
                el.set("enabled", "0")
            if c.channels and len(c.channels) < a.channels:
                etree.SubElement(el, "audio-channel-source",
                                 srcCh=", ".join(str(x) for x in c.channels), role="dialogue")
            if c.gain_db or c.volume or c.fade_in or c.fade_out:
                av = etree.SubElement(el, "adjust-volume")
                if not c.volume and c.gain_db:
                    av.set("amount", _db(c.gain_db))
                if c.volume or c.fade_in or c.fade_out:
                    param = etree.SubElement(av, "param", name="amount")
                    if c.fade_in:
                        etree.SubElement(param, "fadeIn", type="easeIn", duration=_t(c.fade_in, rate))
                    if c.fade_out:
                        etree.SubElement(param, "fadeOut", type="easeOut", duration=_t(c.fade_out, rate))
                    if c.volume:
                        kfa = etree.SubElement(param, "keyframeAnimation")
                        for p in c.volume:
                            etree.SubElement(kfa, "keyframe", time=_t(src_start + p.time, rate),
                                             value=_db(p.db + c.gain_db))
    for m in markers:
        etree.SubElement(gap, "marker", start=_t(m.time, rate), duration=_t(1 / rate, rate),
                         value=m.name or "Marker")
    # FCPXML wants markers before connected clips inside a clip
    for m in gap.findall("marker"):
        gap.remove(m)
        gap.insert(0, m)

    tree = etree.ElementTree(root)
    with open(path, "wb") as fh:
        fh.write(b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n')
        fh.write(etree.tostring(tree, pretty_print=True, encoding="UTF-8"))
    return path
