"""Read a DaVinci Resolve FCPXML (1.8 – 1.11, also .fcpxmld bundles)."""
from __future__ import annotations

import os
import re
from fractions import Fraction

from lxml import etree

from ..paths import url_to_path
from ..model import Clip, Marker, Timeline, Track, VolumePoint
from ..timecode import parse_fcp_time as T

CLIP_TAGS = {"asset-clip", "clip", "audio", "video", "gap", "ref-clip",
             "sync-clip", "mc-clip", "title", "transition"}


def _db(s: str | None) -> float:
    if not s:
        return 0.0
    s = s.strip().lower().replace("db", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _channels(s: str | None) -> list[int]:
    if not s:
        return []
    out = []
    for part in s.replace(" ", "").split(","):
        if part.isdigit():
            out.append(int(part))
    return out


class _Reader:
    def __init__(self, root, path):
        self.root = root
        self.path = path
        self.assets = {}
        self.formats = {}
        self.media = {}
        self.raw_clips = []      # (lane, Clip, asset_id, implicit)
        self.markers: list[Marker] = []
        self.warnings: list[str] = []
        self.video_clips = []
        for f in root.iter("format"):
            self.formats[f.get("id")] = f
        for a in root.iter("asset"):
            self.assets[a.get("id")] = a
        for m in root.iter("media"):
            self.media[m.get("id")] = m

    # -- helpers ---------------------------------------------------------
    def asset_path(self, asset) -> str:
        rep = asset.find("media-rep")
        src = rep.get("src") if rep is not None else asset.get("src")
        return url_to_path(src or "")

    def _volume(self, el, clip: Clip, local_start: Fraction):
        for av in el.findall("adjust-volume"):
            clip.gain_db += _db(av.get("amount"))
            for param in av.findall("param"):
                if param.get("name") not in (None, "amount", "volume"):
                    continue
                fi = param.find("fadeIn")
                fo = param.find("fadeOut")
                if fi is not None:
                    clip.fade_in = T(fi.get("duration"))
                if fo is not None:
                    clip.fade_out = T(fo.get("duration"))
                kfs = param.find("keyframeAnimation")
                kf_parent = kfs if kfs is not None else param
                for kf in kf_parent.findall("keyframe"):
                    clip.volume.append(VolumePoint(T(kf.get("time")) - local_start,
                                                   _db(kf.get("value"))))
        if clip.volume and clip.gain_db:
            # keyframes already include the static amount in FCPXML
            clip.gain_db = 0.0

    def _marker(self, m, abs_start, local_start):
        self.markers.append(Marker(
            time=abs_start + (T(m.get("start")) - local_start),
            name=m.get("value", ""),
            note=m.get("note", ""),
            duration=T(m.get("duration")),
        ))

    # -- traversal -------------------------------------------------------
    def walk(self, parent, parent_abs: Fraction, parent_start: Fraction,
             parent_lane: int, in_spine: bool, inherited_enabled=True):
        for el in parent:
            tag = el.tag if isinstance(el.tag, str) else ""
            if tag in ("marker", "chapter-marker"):
                self._marker(el, parent_abs, parent_start)
                continue
            if tag == "spine":
                off = T(el.get("offset")) if el.get("offset") else parent_start
                lane = parent_lane + int(el.get("lane", 0))
                self.walk(el, parent_abs + (off - parent_start), off, lane, True,
                          inherited_enabled)
                continue
            if tag not in CLIP_TAGS or tag == "transition":
                continue
            offset = T(el.get("offset"))
            start = T(el.get("start"))
            dur = T(el.get("duration"))
            lane = parent_lane + int(el.get("lane", 0))
            enabled = inherited_enabled and el.get("enabled", "1") != "0"
            abs_start = parent_abs + (offset - parent_start)

            if tag in ("asset-clip", "audio", "video"):
                self._media_clip(el, tag, abs_start, start, dur, lane, enabled)
            elif tag == "ref-clip":
                self.warnings.append(
                    f"Compound clip '{el.get('name')}' at {float(abs_start):.2f}s "
                    "was skipped — decompose compound clips in Resolve before export.")
            elif tag in ("sync-clip", "mc-clip"):
                self.warnings.append(
                    f"{tag} '{el.get('name')}' — only its connected audio is read; "
                    "flatten multicam/synced clips in Resolve for best results.")

            # children (connected clips, markers, inner audio/video of <clip>)
            self.walk(el, abs_start, start, lane, False, enabled)

    def _media_clip(self, el, tag, abs_start, start, dur, lane, enabled):
        asset = self.assets.get(el.get("ref"))
        if asset is None:
            return
        has_audio = asset.get("hasAudio") == "1" or tag == "audio"
        has_video = asset.get("hasVideo") == "1" and tag != "audio"
        path = self.asset_path(asset)
        src_in = start - T(asset.get("start"))
        name = el.get("name") or asset.get("name") or os.path.basename(path)

        if has_video and tag != "audio" and lane >= 0:
            self.video_clips.append((lane, Clip(name, abs_start, dur, path, src_in,
                                                enabled=enabled)))
        if not has_audio or tag == "video":
            return
        implicit = has_video and lane >= 0          # audio carried inside a video clip
        chans = _channels(el.get("srcCh"))
        disabled_all = False
        acs = el.findall("audio-channel-source")
        if acs:
            used = [a for a in acs if a.get("enabled", "1") != "0"]
            if not used:
                disabled_all = True
            for a in used:
                chans += _channels(a.get("srcCh"))
        if disabled_all:
            return
        clip = Clip(name, abs_start, dur, path, src_in, channels=sorted(set(chans)),
                    enabled=enabled)
        self._volume(el, clip, start)
        self.raw_clips.append((lane, clip, el.get("ref"), implicit))


def read(path: str, project_name: str | None = None) -> Timeline:
    if os.path.isdir(path):                       # .fcpxmld bundle
        path = os.path.join(path, "Info.fcpxml")
    tree = etree.parse(path)
    root = tree.getroot()
    r = _Reader(root, path)

    projects = list(root.iter("project"))
    if project_name:
        projects = [p for p in projects if p.get("name") == project_name] or projects
    if projects:
        project = projects[0]
        seq = project.find("sequence")
        name = project.get("name", "Timeline")
    else:
        seq = next(root.iter("sequence"))
        name = "Timeline"

    fmt = r.formats.get(seq.get("format"))
    if fmt is not None and fmt.get("frameDuration"):
        rate = 1 / T(fmt.get("frameDuration"))
    else:
        rate = Fraction(25)
    tc_start = T(seq.get("tcStart"))
    drop = (seq.get("tcFormat") == "DF")

    tl = Timeline(name=name, frame_rate=rate, drop_frame=drop, start=tc_start,
                  source_format="fcpxml", source_file=os.path.abspath(path))
    ar = seq.get("audioRate", "48k").lower()
    tl.sample_rate = int(float(ar.replace("k", "")) * 1000) if "k" in ar else int(ar or 48000)

    spine = seq.find("spine")
    # spine offsets are in sequence time, which already starts at tcStart
    r.walk(spine, Fraction(0), Fraction(0), 0, True)

    # drop implicit (inside-video) audio when the same media is also placed
    # as an explicit audio clip at the same time — avoids doubled audio
    explicit = {(ref, c.start) for lane, c, ref, imp in r.raw_clips if not imp}
    clips = [(lane, c) for lane, c, ref, imp in r.raw_clips
             if not (imp and (ref, c.start) in explicit)]

    lanes = sorted({lane for lane, _ in clips}, key=lambda l: (l != 0, -l if l < 0 else l))
    has_lane0 = 0 in lanes
    for lane in lanes:
        if lane == 0:
            tname = "A1"
        elif lane < 0:
            tname = f"A{-lane + (1 if has_lane0 else 0)}"
        else:
            tname = f"A (lane {lane})"
        trk = Track(name=tname, kind="audio",
                    clips=sorted([c for l, c in clips if l == lane], key=lambda c: c.start))
        tl.tracks.append(trk)

    vlanes = sorted({lane for lane, _ in r.video_clips})
    for lane in vlanes:
        tl.tracks.append(Track(name=f"V{lane + 1}", kind="video",
                               clips=sorted([c for l, c in r.video_clips if l == lane],
                                            key=lambda c: c.start)))

    tl.markers = sorted(r.markers, key=lambda m: m.time)
    tl.warnings += r.warnings
    _check_overlaps(tl)
    return tl


def _check_overlaps(tl: Timeline):
    for t in tl.audio_tracks:
        for a, b in zip(t.clips, t.clips[1:]):
            if b.start < a.end:
                tl.warnings.append(
                    f"Track {t.name}: clips '{a.name}' and '{b.name}' overlap; "
                    "the later clip wins in Live.")
