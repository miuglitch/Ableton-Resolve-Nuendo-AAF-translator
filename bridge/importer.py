"""Resolve / Nuendo timeline (AAF or FCPXML)  →  Ableton Live Set."""
from __future__ import annotations

import glob
import os
import re
import time
from dataclasses import dataclass, field
from fractions import Fraction

from . import sidecar as sc
from .als.writer import AlsBuilder, LIVE_COLORS
from .media import MediaConverter, probe, source_timecode
from .model import Clip, Timeline
from .readers import read_timeline
from .timecode import seconds_to_tc, tc_to_seconds

ORIG_PREFIX = "ORIG - "


@dataclass
class ImportOptions:
    template: str                       # Live template .als
    out_dir: str = ""                   # where the '<name> Project' folder is created (new project)
    set_name: str = ""
    tempo: float = 120.0
    offset_tc: str | None = None        # timecode that becomes 0:00 in Live; None = timeline start
    selected: list[str] | None = None   # track names to include; None = all
    picture: str | None = None          # optional reference video for Live
    picture_tc: str | None = None       # timecode of the video's first frame; None = timeline start
    picture_video: object = None        # video.VideoSettings to transcode the picture; None = use as is
    prefix: str = ORIG_PREFIX
    sample_rate: int = 48000           # rate of the converted WAVs = your Live audio rate
    # add to an existing Live project instead of creating a new one
    existing_project: str | None = None # Live project folder
    base_set: str | None = None         # existing .als to build on (keeps its tracks); None = new tracks only
    note: str = ""                      # added to the new .als name, e.g. 'VO pickups'
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


def project_sidecar(project_dir: str, base_set: str | None = None, levels: int = 3):
    """Import settings saved with an earlier import into this project.
    Looks next to the set first, then in the folder and up to `levels`
    parent folders (Live's 'Save As' can nest a new project inside)."""
    if base_set:
        p = sc.Sidecar.path_for(base_set)
        if os.path.exists(p):
            return sc.Sidecar.load(p)
    d = os.path.abspath(project_dir)
    for _ in range(levels + 1):
        found = sorted(glob.glob(os.path.join(glob.escape(d), "*.bridge.json")),
                       key=os.path.getmtime, reverse=True)
        if found:
            return sc.Sidecar.load(found[0])
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def _unique(path):
    base, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(path):
        path = f"{base} ({n}){ext}"
        n += 1
    return path


def target_paths(opt: ImportOptions, set_name: str) -> tuple[str, str]:
    """(project folder, .als path) for the chosen destination."""
    name = _safe(set_name or "Timeline")
    if opt.existing_project:
        note = _safe(opt.note) if opt.note.strip() else time.strftime("added %Y-%m-%d %H%M")
        return opt.existing_project, os.path.join(opt.existing_project, f"{name} - {note}.als")
    project_dir = os.path.join(opt.out_dir, f"{name} Project")
    return project_dir, os.path.join(project_dir, f"{name}.als")


def run(timeline_path: str, opt: ImportOptions, tl: Timeline | None = None) -> ImportResult:
    log = opt.log
    tl = tl or load(timeline_path)
    warnings = list(tl.warnings)

    if opt.selected is not None:
        for t in tl.audio_tracks:
            t.selected = t.name in opt.selected

    offset = tc_to_seconds(opt.offset_tc, tl.frame_rate, tl.drop_frame) if opt.offset_tc else tl.start
    name = _safe(opt.set_name or tl.name)
    project_dir, als_path = target_paths(opt, name)
    earlier = None
    if opt.existing_project:
        if not os.path.isdir(project_dir):
            raise FileNotFoundError(f"Project folder not found: {project_dir}")
        als_path = _unique(als_path)
        earlier = project_sidecar(project_dir, opt.base_set)
        if earlier:
            # line new material up with what's already in the project
            offset, opt.tempo, opt.sample_rate = earlier.offset_s, earlier.tempo, earlier.media_sample_rate
            log(f"Using this project's earlier import settings (offset, tempo, sample rate).")
        else:
            warnings.append("No earlier import info (.bridge.json) found in this project, so the offset, "
                            "tempo and sample rate entered in the app were used. Check the alignment.")
    media_dir = os.path.join(project_dir, "Samples", "Imported")
    os.makedirs(media_dir, exist_ok=True)

    log(f"Timeline '{tl.name}': {float(tl.frame_rate):.3f} fps, starts {seconds_to_tc(tl.start, tl.frame_rate, tl.drop_frame)}")
    log(f"Live 0:00 = {seconds_to_tc(offset, tl.frame_rate, tl.drop_frame)}, tempo {opt.tempo} BPM, "
        f"audio at {opt.sample_rate} Hz")

    conv = MediaConverter(media_dir, tl.frame_rate, log=log,
                          aaf_path=timeline_path if tl.source_format == "aaf" else None,
                          sample_rate=opt.sample_rate)
    conv.run(tl, opt.progress)
    warnings += conv.warnings

    b = AlsBuilder(opt.template, base_path=opt.base_set)
    if opt.base_set and earlier and abs(b.current_tempo() - opt.tempo) > 1e-6:
        warnings.append(f"The set's tempo is {b.current_tempo()} BPM but it was imported at {opt.tempo} BPM; "
                        "it was set back so new audio lines up. Existing clips are unaffected if unwarped.")
    b.set_tempo(opt.tempo)
    if b.used_builtin_clip:
        warnings.append("The template has no audio clip, so the built-in clip definition was used. "
                        "If Live won't open the set, drag any WAV onto the template's audio track and save it.")

    if opt.picture:
        try:
            pic_file = opt.picture
            info = probe(pic_file)
            pic_tc = opt.picture_tc or source_timecode(info)
            pic_start = (tc_to_seconds(pic_tc, tl.frame_rate, tl.drop_frame)
                         if pic_tc else tl.start)
            log(f"Picture starts at {seconds_to_tc(pic_start, tl.frame_rate, tl.drop_frame)}"
                f"{' (from the file)' if not opt.picture_tc and pic_tc else ''}")
            if opt.picture_video is not None:
                from .video import transcode
                pic_file = transcode(pic_file, os.path.join(project_dir, "Samples", "Picture"),
                                     opt.picture_video, log)
            dur = Fraction(str(float(probe(pic_file)["format"]["duration"])))
            pic = Clip(name=os.path.basename(pic_file), start=pic_start, duration=dur,
                       source_path=pic_file, source_in=Fraction(0))
            pic.wav_path = pic_file
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

    b.save(als_path)
    if not opt.existing_project:
        from .projectfolder import make_live_project
        try:
            if not make_live_project(project_dir, os.path.dirname(os.path.abspath(opt.template)), log):
                log("Project icon: no Ableton project icon found near the template to copy.")
        except OSError as e:
            log(f"Project icon not set: {e}")

    side = sc.make(tl, offset, opt.tempo)
    side.media_sample_rate = opt.sample_rate
    from .readers.aaf import EMBEDDED
    side.video = sc.tracks_to_list([t for t in tl.tracks if t.kind == "video"])
    side.extra_audio = sc.tracks_to_list(
        [t for t in tl.audio_tracks if not t.selected
         and not any(c.source_path.startswith(EMBEDDED) for c in t.clips)])
    if earlier and not side.video:
        side.video = earlier.video
    if earlier:                                   # keep track of earlier imports' originals
        side.media = list(earlier.media)
    known = {os.path.normcase(m.wav) for m in side.media}
    for wav, src, chans in conv.links:
        if os.path.normcase(os.path.abspath(wav)) in known:
            continue
        side.media.append(sc.MediaLink(wav=os.path.abspath(wav), source=src, channels=chans))
    side.save(sc.Sidecar.path_for(als_path))

    report = os.path.splitext(als_path)[0] + " - import report.txt"
    with open(report, "w", encoding="utf-8") as fh:
        fh.write(f"Imported: {timeline_path}\nLive Set: {als_path}\n")
        fh.write(f"Frame rate: {float(tl.frame_rate):.3f}{' DF' if tl.drop_frame else ''}\n")
        fh.write(f"Timeline start: {seconds_to_tc(tl.start, tl.frame_rate, tl.drop_frame)}\n")
        fh.write(f"Live 0:00 = {seconds_to_tc(offset, tl.frame_rate, tl.drop_frame)}\n")
        fh.write(f"Tempo: {opt.tempo} BPM (keep it constant in this set)\n")
        fh.write(f"Audio converted to: {opt.sample_rate} Hz (timeline: {tl.sample_rate} Hz)\n\n")
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
