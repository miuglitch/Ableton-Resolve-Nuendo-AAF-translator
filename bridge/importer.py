"""Resolve / Nuendo timeline (AAF or FCPXML)  →  Ableton Live Set."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from fractions import Fraction

from . import sidecar as sc
from .als.writer import AlsBuilder, LIVE_COLORS
from .media import MediaConverter, probe
from .model import Clip, Timeline
from .readers import read_timeline
from .timecode import seconds_to_tc, tc_to_seconds

ORIG_PREFIX = "ORIG - "


@dataclass
class ImportOptions:
    template: str                       # Live template .als
    out_dir: str                        # where the '<name> Project' folder is created
    set_name: str = ""
    tempo: float = 120.0
    offset_tc: str | None = None        # timecode that becomes 0:00 in Live; None = timeline start
    selected: list[str] | None = None   # track names to include; None = all
    picture: str | None = None          # optional reference video for Live
    picture_tc: str | None = None       # timecode of the video's first frame; None = timeline start
    prefix: str = ORIG_PREFIX
    log: callable = print
    progress: callable = None


@dataclass
class ImportResult:
    als_path: str
    report_path: str
    warnings: list[str] = field(default_factory=list)


def load(path: str) -> Timeline:
    return read_timeline(path)


def _safe(name):
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip() or "Timeline"


def run(timeline_path: str, opt: ImportOptions, tl: Timeline | None = None) -> ImportResult:
    log = opt.log
    tl = tl or load(timeline_path)
    warnings = list(tl.warnings)

    if opt.selected is not None:
        for t in tl.audio_tracks:
            t.selected = t.name in opt.selected

    offset = tc_to_seconds(opt.offset_tc, tl.frame_rate, tl.drop_frame) if opt.offset_tc else tl.start
    name = _safe(opt.set_name or tl.name)
    project_dir = os.path.join(opt.out_dir, f"{name} Project")
    media_dir = os.path.join(project_dir, "Media", "Imported")
    os.makedirs(media_dir, exist_ok=True)

    log(f"Timeline '{tl.name}': {float(tl.frame_rate):.3f} fps, starts {seconds_to_tc(tl.start, tl.frame_rate, tl.drop_frame)}")
    log(f"Live 0:00 = {seconds_to_tc(offset, tl.frame_rate, tl.drop_frame)}, tempo {opt.tempo} BPM")

    conv = MediaConverter(media_dir, tl.frame_rate, log=log,
                          aaf_path=timeline_path if tl.source_format == "aaf" else None)
    conv.run(tl, opt.progress)
    warnings += conv.warnings

    b = AlsBuilder(opt.template)
    b.set_tempo(opt.tempo)

    if opt.picture:
        try:
            pic_start = (tc_to_seconds(opt.picture_tc, tl.frame_rate, tl.drop_frame)
                         if opt.picture_tc else tl.start)
            dur = Fraction(str(float(probe(opt.picture)["format"]["duration"])))
            pic = Clip(name=os.path.basename(opt.picture), start=pic_start, duration=dur,
                       source_path=opt.picture, source_in=Fraction(0))
            pic.wav_path = opt.picture
            b.add_track("PICTURE", [pic], offset, LIVE_COLORS["PICTURE"], warnings)
        except Exception as e:
            warnings.append(f"Picture reference not added: {e}")

    for t in tl.audio_tracks:
        if not t.selected:
            continue
        b.add_track(opt.prefix + t.name, t.clips, offset, LIVE_COLORS["ORIG"], warnings)

    for i, m in enumerate(tl.markers):
        if m.time >= offset:
            b.add_locator(m.time - offset, m.name or m.note or f"Marker {i + 1}", i)

    als_path = os.path.join(project_dir, f"{name}.als")
    b.save(als_path)

    side = sc.make(tl, offset, opt.tempo)
    for wav, src, chans in conv.links:
        side.media.append(sc.MediaLink(wav=os.path.abspath(wav), source=src, channels=chans))
    side.save(sc.Sidecar.path_for(als_path))

    report = os.path.join(project_dir, f"{name} - import report.txt")
    with open(report, "w", encoding="utf-8") as fh:
        fh.write(f"Imported: {timeline_path}\nLive Set: {als_path}\n")
        fh.write(f"Frame rate: {float(tl.frame_rate):.3f}{' DF' if tl.drop_frame else ''}\n")
        fh.write(f"Timeline start: {seconds_to_tc(tl.start, tl.frame_rate, tl.drop_frame)}\n")
        fh.write(f"Live 0:00 = {seconds_to_tc(offset, tl.frame_rate, tl.drop_frame)}\n")
        fh.write(f"Tempo: {opt.tempo} BPM (keep it constant in this set)\n\n")
        for t in tl.audio_tracks:
            mark = "x" if t.selected else " "
            fh.write(f"[{mark}] {t.name}: {len(t.clips)} clips\n")
        fh.write(f"\nMarkers → locators: {len(tl.markers)}\n")
        fh.write(f"Converted files: {len(conv.links)}\n")
        if warnings:
            fh.write("\nWarnings:\n" + "\n".join(f"- {w}" for w in warnings) + "\n")
    log(f"Done: {als_path}")
    for w in warnings:
        log("! " + w)
    return ImportResult(als_path, report, warnings)
