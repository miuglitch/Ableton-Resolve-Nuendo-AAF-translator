"""Ableton Live Set  →  Resolve (FCPXML).

Clips that still play the audio converted from Resolve point back at
Resolve's original media (ORIG). Everything else is new sound design (SD):
those files are copied into a Media folder next to the export.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass, field
from fractions import Fraction

from . import importer
from .als import reader as als_reader
from .media import probe, source_timecode
from .model import Clip, Marker
from .readers.aaf import EMBEDDED
from .sidecar import Sidecar
from .timecode import parse_rate, seconds_to_tc, tc_to_seconds
from .writers import fcpxml as fcpxml_writer
from .writers.fcpxml import MediaAsset, OutTrack

SD_PREFIX = "SD - "


@dataclass
class ExportOptions:
    out_dir: str = ""                      # '' = '<project>/Export to Resolve'
    selected: list[str] | None = None      # Live track names; None = all audio tracks
    name: str = ""
    sd_prefix: str = SD_PREFIX
    split_mixed: bool = True               # ORIG and SD on the same Live track -> two tracks
    include_muted: bool = False            # muted tracks exported as disabled clips
    include_fader: bool = False            # add the static track fader level to clip gain
    include_video: bool = True             # rebuild Resolve's video tracks (or the picture reference)
    include_unimported: bool = True        # Resolve audio tracks that weren't brought into Live
    # only needed when the set has no .bridge.json (not made by this app)
    offset_tc: str = "01:00:00:00"
    frame_rate: str = "25"
    log: callable = print


@dataclass
class ExportResult:
    path: str
    report_path: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class Preflight:
    live: als_reader.LiveSet
    side: Sidecar | None
    warnings: list[str]


def preflight(als_path: str) -> Preflight:
    live = als_reader.read(als_path)
    side_path = Sidecar.path_for(als_path)
    side = Sidecar.load(side_path) if os.path.exists(side_path) else \
        importer.project_sidecar(os.path.dirname(os.path.abspath(als_path)))
    for lt in live.tracks:
        if lt.kind == "audio" and is_picture(lt):
            lt.kind = "picture"
    w = list(live.warnings)
    for lt in live.tracks:
        n = lt.track.name
        if lt.kind == "midi":
            w.append(f"MIDI track '{n}' is not exported. Freeze and flatten it (or bounce to audio) first.")
        elif lt.kind == "return" and lt.has_devices:
            w.append(f"Return track '{n}' has effects that won't be heard in Resolve. Resample it to audio if needed.")
        elif lt.kind == "group" and lt.has_devices:
            w.append(f"Group '{n}' has effects that won't be heard in Resolve.")
        elif lt.kind == "audio":
            if lt.has_devices and lt.track.clips:
                w.append(f"'{n}' has devices/plug-ins. Freeze and flatten it first so the sound is in the audio.")
            if lt.has_sends and lt.track.clips:
                w.append(f"'{n}' sends to return tracks; that part of the sound won't be in Resolve.")
    if side is None:
        w.append("No .bridge.json found for this set: enter the timeline start timecode and frame rate.")
    elif abs(side.tempo - live.tempo) > 1e-6:
        w.append(f"The set's tempo changed from {side.tempo:g} to {live.tempo:g} BPM since the import. "
                 "Clips are placed where Live plays them now.")
    return Preflight(live, side, w)


VIDEO_EXT = {".mov", ".mp4", ".m4v", ".mxf", ".avi", ".mkv"}


def is_picture(lt) -> bool:
    clips = lt.track.clips
    return bool(clips) and all(os.path.splitext(c.source_path)[1].lower() in VIDEO_EXT for c in clips)


def export_tracks(pre: "Preflight"):
    """Live tracks that can be exported (audio with clips)."""
    return [lt for lt in pre.live.tracks if lt.kind == "audio" and lt.track.clips]


def _safe(name):
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip() or "audio"


def _strip_orig(name: str) -> str:
    return name[len(importer.ORIG_PREFIX):] if name.startswith(importer.ORIG_PREFIX) else name


class _Media:
    """Collects assets; copies new sound design files into the export."""

    def __init__(self, media_dir, rate, log):
        self.dir = media_dir
        self.rate = rate
        self.log = log
        self.assets: dict[str, MediaAsset] = {}
        self.copied: dict[str, str] = {}

    def asset(self, path: str, copy: bool) -> str:
        key = os.path.normcase(os.path.abspath(path))
        if copy:
            if key in self.copied:
                return self.copied[key]
            os.makedirs(self.dir, exist_ok=True)
            dst = os.path.join(self.dir, os.path.basename(path))
            n = 2
            while os.path.exists(dst) and not _same_file(dst, path):
                b, e = os.path.splitext(os.path.basename(path))
                dst = os.path.join(self.dir, f"{b} ({n}){e}")
                n += 1
            if not os.path.exists(dst):
                shutil.copy2(path, dst)
                self.log(f"Copied {os.path.basename(path)}")
            self.copied[key] = dst
            path = dst
        key = path
        if key not in self.assets:
            self.assets[key] = _describe(path, self.rate)
        return key


def _same_file(a, b):
    try:
        return os.path.getsize(a) == os.path.getsize(b)
    except OSError:
        return False


def _describe(path: str, rate: Fraction) -> MediaAsset:
    info = probe(path)
    streams = info.get("streams", [])
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    has_video = any(s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic", 0) == 0
                    for s in streams)
    chans = sum(int(s.get("channels", 1)) for s in audio) or 1
    sr = int(audio[0].get("sample_rate", 48000)) if audio else 48000
    dur = Fraction(str(float(info.get("format", {}).get("duration", 0) or 0)))
    start = Fraction(0)
    tc = source_timecode(info)
    tags = info.get("format", {}).get("tags", {})
    tref = next((v for k, v in tags.items() if k.lower() == "time_reference"), None)
    if tc:
        try:
            start = tc_to_seconds(tc, rate)
        except ValueError:
            pass
    elif tref:
        try:
            start = Fraction(int(tref), sr)
        except ValueError:
            pass
    return MediaAsset(path=path, duration=dur, start=start, channels=chans, sample_rate=sr,
                      has_video=has_video)


def default_out_dir(als_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(als_path)), "Export to Resolve")


def run(als_path: str, opt: ExportOptions, pre: Preflight | None = None) -> ExportResult:
    log = opt.log
    pre = pre or preflight(als_path)
    live, side = pre.live, pre.side
    warnings = list(pre.warnings)

    if side:
        rate, drop = side.rate, side.drop_frame
        offset, tc_start = side.offset_s, side.start_s
    else:
        rate = parse_rate(opt.frame_rate)
        drop = ";" in opt.offset_tc
        offset = tc_start = tc_to_seconds(opt.offset_tc, rate, drop)

    name = _safe(opt.name or os.path.splitext(os.path.basename(als_path))[0])
    out_dir = opt.out_dir or default_out_dir(als_path)
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H%M")
    out_path = os.path.join(out_dir, f"{name} {stamp}.fcpxml")
    media = _Media(os.path.join(out_dir, f"{name} Media"), rate, log)

    out_tracks: list[OutTrack] = []
    n_orig = n_sd = 0
    for lt in live.tracks:
        if lt.kind != "audio" or not lt.track.clips:
            continue
        tname = lt.track.name
        if opt.selected is not None and tname not in opt.selected:
            continue
        if lt.muted and not opt.include_muted:
            continue
        orig, sd = [], []
        for c in lt.track.clips:
            if not c.source_path or not os.path.exists(c.source_path):
                warnings.append(f"{tname} / {c.name or '?'}: audio file missing ({c.source_path}) — skipped.")
                continue
            link = side.link_for(c.source_path) if side else None
            out = Clip(name=c.name, start=c.start + offset, duration=c.duration,
                       source_path="", source_in=c.source_in, fade_in=c.fade_in, fade_out=c.fade_out,
                       enabled=c.enabled and not lt.muted)
            out.volume = als_reader.volume_points(lt, c)
            # fader off: automation is measured from where the fader sits
            base = 0.0 if opt.include_fader else lt.fader_db
            if out.volume:
                for p in out.volume:
                    p.db -= base
                out.gain_db = c.gain_db
            else:
                out.gain_db = c.gain_db + als_reader.static_level_db(lt, c) - base
            if link and not link.source.startswith(EMBEDDED) and os.path.exists(link.source):
                out.source_path = media.asset(link.source, copy=False)
                out.channels = list(link.channels)
                try:                                  # WAV holds only part of the source
                    n, d = link.wav_start_in_source.split("/")
                    out.source_in = c.source_in + Fraction(int(n), int(d))
                except (ValueError, AttributeError):
                    pass
                orig.append(out)
            else:
                out.source_path = media.asset(c.source_path, copy=True)
                sd.append(out)
        base = _strip_orig(tname)
        if orig and sd and opt.split_mixed:
            out_tracks.append(OutTrack(tname, orig))
            out_tracks.append(OutTrack(opt.sd_prefix + base, sd))
        elif orig or sd:
            clips = sorted(orig + sd, key=lambda c: c.start)
            label = tname if not sd else (tname if orig else opt.sd_prefix + base)
            out_tracks.append(OutTrack(label, clips))
        n_orig += len(orig)
        n_sd += len(sd)

    # original Resolve audio tracks that were left out of the Live session
    n_extra = 0
    if opt.include_unimported and side:
        from .sidecar import clip_from_dict
        for t in side.extra_audio:
            clips = []
            for d in t["clips"]:
                c = clip_from_dict(d)
                if os.path.exists(c.source_path):
                    c.source_path = media.asset(c.source_path, copy=False)
                    clips.append(c)
            if clips:
                out_tracks.append(OutTrack(t["track"], clips))
                n_extra += 1

    # video: Resolve's edit from the import, else the picture reference in Live
    video_tracks: list[OutTrack] = []
    if opt.include_video:
        from .sidecar import clip_from_dict
        for t in (side.video if side else []):
            clips = []
            for d in t["clips"]:
                c = clip_from_dict(d)
                if os.path.exists(c.source_path):
                    c.source_path = media.asset(c.source_path, copy=False)
                    clips.append(c)
                else:
                    warnings.append(f"Video file not found: {c.source_path}")
            if clips:
                video_tracks.append(OutTrack(t["track"], clips))
        if not video_tracks:
            for lt in live.tracks:
                if lt.kind == "picture":
                    clips = []
                    for c in lt.track.clips:
                        if os.path.exists(c.source_path):
                            clips.append(Clip(name=c.name, start=c.start + offset, duration=c.duration,
                                              source_path=media.asset(c.source_path, copy=False),
                                              source_in=c.source_in))
                    if clips:
                        video_tracks.append(OutTrack("V1", clips))
                        warnings.append("No video edit saved with this project, so the picture reference "
                                        "was put on V1. Import again with v0.6+ to get Resolve's own video clips.")
                    break

    markers = [Marker(time=m.time + offset, name=m.name) for m in live.locators]
    fcpxml_writer.write(out_path, name, rate, drop, tc_start, out_tracks, media.assets, markers,
                        sample_rate=side.sample_rate if side else 48000, video=video_tracks)

    report = os.path.splitext(out_path)[0] + " - export report.txt"
    with open(report, "w", encoding="utf-8") as fh:
        fh.write(f"Live Set: {als_path}\nExport: {out_path}\n")
        fh.write(f"Timeline: {float(rate):.3f} fps, starts {seconds_to_tc(tc_start, rate, drop)}; "
                 f"Live 0:00 = {seconds_to_tc(offset, rate, drop)}; tempo {live.tempo:g} BPM\n\n")
        for t in out_tracks:
            fh.write(f"{t.name}: {len(t.clips)} clips\n")
        fh.write(f"\nOriginal Resolve media clips: {n_orig}\nNew sound design clips: {n_sd} "
                 f"({len(media.copied)} files copied to '{os.path.basename(media.dir)}')\n")
        fh.write(f"Locators → markers: {len(markers)}\n")
        fh.write(f"Video tracks: {len(video_tracks)}; original audio tracks not used in Live: {n_extra}\n")
        if warnings:
            fh.write("\nWarnings:\n" + "\n".join(f"- {w}" for w in warnings) + "\n")
    log(f"Done: {out_path}")
    for w in warnings:
        log("! " + w)
    return ExportResult(out_path, report, warnings)
