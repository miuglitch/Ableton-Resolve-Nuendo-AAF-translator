"""One click: the timeline open in DaVinci Resolve Studio  →  a Live Set.

Picture
  Resolve renders the picture with everything on it (grade, Fusion, titles).
  The *footage* clips decide where the cuts are: a title or other overlay that
  spans a cut is split there and goes with the shot underneath. Each shot
  becomes its own video clip in Live, on a track named like its Resolve video
  track, with a locator at its start. Each clip carries the timeline's stereo
  mix as its sound.

  Render modes
    full     one render of the whole timeline, cut at the shot changes. Head
             and tail handles show whatever is next to the shot in the edit.
    handles  real source handles: Resolve also renders a temporary timeline
             where every shot stands alone with its source extended by the
             handles (grade, Fusion and transforms copied over). The part
             inside the edit still comes from the real timeline, so titles
             are there; the handles are clean graded footage. Retimed shots,
             stills and clips at another frame rate fall back to 'full'.
             The real timeline is never changed.

Audio
  Each Resolve audio track becomes a Live track with the same name, clips in
  place, with head/tail handles (or whole files), optional normalising, clip
  gain/fades/volume automation read from an AAF Resolve exports (automation
  goes to the Gain of a Utility on the track), every track at -18 dB and a
  +18 dB Utility on the Main track.
"""
from __future__ import annotations

import copy
import glob
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from fractions import Fraction

from . import sidecar as sc
from .als.writer import AlsBuilder, LIVE_COLORS, MISSING_COLOR, VIDEO_COLORS
from .importer import _safe, _unique, project_sidecar, target_paths, ImportOptions
from .media import MediaConverter, peak_db, probe, source_timecode
from .model import Clip, Marker, Timeline, Track
from .resolve_api import ResolveError, ResolveTimeline, RItem
from .timecode import seconds_to_tc, tc_to_seconds

MODE_FULL = "full"
MODE_HANDLES = "handles"

# Inspector values copied onto the handles timeline's clips
TRANSFORM_KEYS = ("Pan", "Tilt", "ZoomX", "ZoomY", "ZoomGang", "RotationAngle", "AnchorPointX",
                  "AnchorPointY", "Pitch", "Yaw", "FlipX", "FlipY", "CropLeft", "CropRight", "CropTop",
                  "CropBottom", "CropSoftness", "CropRetain", "DynamicZoomEase", "CompositeMode",
                  "Opacity", "Distortion", "Scaling", "ResizeFilter")


@dataclass
class Options:
    template: str
    out_dir: str = ""                      # new project goes in <out_dir>/<name> Project
    set_name: str = ""
    tempo: float = 120.0
    sample_rate: int = 48000
    existing_project: str | None = None
    base_set: str | None = None
    note: str = ""
    # picture
    video: bool = True
    render_mode: str = MODE_FULL
    video_head: float = 2.0                # seconds
    video_tail: float = 2.0
    video_settings: object = None          # video.VideoSettings for Live
    shot_markers: bool = True
    resolve_markers: bool = True
    mix_track: bool = False                # also one continuous stereo-mix track
    continuous: bool = True                # one video of the whole timeline on one track (plays smoothly)
    shot_clips: bool = False               # also one video clip per shot, on the Resolve track names
    shot_track: bool = True                # a track with one empty clip per shot, to grab and loop
    render_preset: str = ""                # optional Resolve render preset (e.g. for H.264 key frames)
    # audio
    audio: bool = True
    audio_tracks: list[str] | None = None  # Resolve audio track names; None = all
    audio_whole_files: bool = False
    audio_head: float = 2.0
    audio_tail: float = 2.0
    normalize: bool = False
    normalize_db: float = -1.0
    read_aaf: bool = True                  # clip gain / fades / automation via AAF
    gain_staging: bool = True              # tracks -18 dB, +18 dB Utility on Main
    log: callable = print
    progress: callable = None              # progress(fraction 0..1, text)


@dataclass
class Shot:
    index: int
    start: int                             # timeline frames, exclusive end
    end: int
    item: RItem                            # footage clip (or the title, if nothing is under it)
    overlays: list[RItem] = field(default_factory=list)

    @property
    def name(self) -> str:
        n = self.item.name or os.path.basename(self.item.file) or f"Shot {self.index}"
        return os.path.splitext(n)[0] if self.item.file else n

    @property
    def source_in(self) -> Fraction:
        """Seconds into the media where this shot starts (if footage)."""
        return self.item.source_in + Fraction(self.start - self.item.start) / self._fps * self.item.speed

    _fps: Fraction = Fraction(25)


def plan_shots(rt: ResolveTimeline) -> list[Shot]:
    """Shots = runs of the same top-most footage clip. Overlays (titles,
    generators, adjustment clips…) never make a cut; they belong to the
    footage under them. Where there's no footage, the top overlay is the shot."""
    vis = [i for i in rt.video_items if i.enabled]
    if not vis:
        return []
    bounds = sorted({b for i in vis for b in (i.start, i.end)})
    runs: list[list] = []
    for a, b in zip(bounds, bounds[1:]):
        cover = [i for i in vis if i.start <= a and i.end >= b]
        if not cover:
            continue
        foot = [i for i in cover if i.is_footage]
        key = max(foot or cover, key=lambda i: i.track_index)
        over = [i for i in cover if not i.is_footage and i is not key]
        if runs and runs[-1][1] == a and runs[-1][2] is key:
            runs[-1][1] = b
            for o in over:
                if o not in runs[-1][3]:
                    runs[-1][3].append(o)
        else:
            runs.append([a, b, key, over])
    shots = []
    for n, (a, b, key, over) in enumerate(runs, start=1):
        s = Shot(index=n, start=a, end=b, item=key, overlays=over)
        s._fps = rt.fps
        shots.append(s)
    return shots


def shot_tc(rt: ResolveTimeline, s: Shot) -> str:
    """Source timecode at the shot's first frame ('' if unknown)."""
    it = s.item
    if not (it.file and it.source_tc):
        return ""
    r = it.media_fps or rt.fps
    try:
        return seconds_to_tc(tc_to_seconds(it.source_tc, r) + s.source_in, r)
    except ValueError:
        return ""


# --- Resolve rendering ---------------------------------------------------------
class Renderer:
    """Adds our own jobs to Resolve's render queue, renders only those, and
    puts the user's render format, mode, page and timeline back afterwards.
    Every job is rendered by Resolve straight into the format Live gets."""

    def __init__(self, resolve, rt: ResolveTimeline, fmt: str, codec: str, log, progress=None,
                 preset: str = "", frame_rate: str | None = None):
        self.resolve, self.rt, self.log, self.progress = resolve, rt, log, progress
        self.project = rt.project_obj
        self.fmt, self.codec, self.preset, self.frame_rate = fmt, codec, preset, frame_rate
        self.jobs: list[str] = []
        self.names: dict[str, tuple[str, str]] = {}      # job -> (dir, name)
        self._page = self._fmt = self._mode = None

    def __enter__(self):
        for attr, call in (("_page", lambda: self.resolve.GetCurrentPage()),
                           ("_fmt", lambda: self.project.GetCurrentRenderFormatAndCodec()),
                           ("_mode", lambda: self.project.GetCurrentRenderMode())):
            try:
                setattr(self, attr, call())
            except Exception:
                setattr(self, attr, None)
        try:
            self.resolve.OpenPage("deliver")
        except Exception:
            pass
        try:
            self.project.SetCurrentRenderMode(1)            # single clip
        except Exception:
            pass
        return self

    def __exit__(self, *exc):
        for j in self.jobs:
            try:
                self.project.DeleteRenderJob(j)
            except Exception:
                pass
        try:
            self.project.SetCurrentTimeline(self.rt.obj)
        except Exception:
            pass
        if self._fmt:
            try:
                self.project.SetCurrentRenderFormatAndCodec(self._fmt.get("format"), self._fmt.get("codec"))
            except Exception:
                pass
        if self._mode is not None:
            try:
                self.project.SetCurrentRenderMode(self._mode)
            except Exception:
                pass
        if self._page:
            try:
                self.resolve.OpenPage(self._page)
            except Exception:
                pass
        return False

    def queue(self, timeline, out_dir: str, name: str, first: int, last_excl: int, size=None,
              audio=True, video=True, fmt: tuple[str, str] | None = None) -> str:
        """One render job: frames first..last_excl-1 of `timeline`."""
        if not self.project.SetCurrentTimeline(timeline):
            raise ResolveError("Resolve wouldn't switch to the timeline to render it.")
        os.makedirs(out_dir, exist_ok=True)
        for old in glob.glob(os.path.join(glob.escape(out_dir), glob.escape(name) + ".*")):
            try:
                os.remove(old)
            except OSError:
                pass
        if self.preset and fmt is None:
            try:
                if not self.project.LoadRenderPreset(self.preset):
                    raise ResolveError(f"Resolve has no render preset called '{self.preset}'.")
            except ResolveError:
                raise
            except Exception:
                pass
        else:
            f_, c_ = fmt or (self.fmt, self.codec)
            if not self.project.SetCurrentRenderFormatAndCodec(f_, c_):
                raise ResolveError(f"Resolve refused render format {f_} / {c_}.")
        settings = {"SelectAllFrames": False, "MarkIn": int(first), "MarkOut": int(last_excl) - 1,
                    "TargetDir": out_dir, "CustomName": name,
                    "ExportVideo": bool(video), "ExportAudio": bool(audio)}
        if video and size:
            settings.update(FormatWidth=int(size[0]), FormatHeight=int(size[1]))
        if video and self.frame_rate:
            settings["FrameRate"] = self.frame_rate
        if not self.project.SetRenderSettings(settings):
            raise ResolveError("Resolve didn't accept the render settings.")
        job = self.project.AddRenderJob()
        if not job:
            raise ResolveError("Resolve couldn't add the render job (is the Deliver page busy?).")
        self.jobs.append(job)
        self.names[job] = (out_dir, name)
        return job

    def render(self, jobs: list[str]) -> dict[str, str]:
        """Render and wait. Returns {job id: rendered file}."""
        if not jobs:
            return {}
        if not self.project.StartRendering(jobs, False):
            if not self.project.StartRendering(*jobs):
                raise ResolveError("Resolve didn't start rendering.")
        t0 = time.time()
        idle = 0
        while True:
            time.sleep(1.0)
            busy = self.project.IsRenderingInProgress()
            pct, ended = [], []
            for j in jobs:
                try:
                    st = self.project.GetRenderJobStatus(j) or {}
                    pct.append(float(st.get("CompletionPercentage", 0)))
                    ended.append(str(st.get("JobStatus", "")).lower() in
                                 ("complete", "completed", "failed", "cancelled", "canceled"))
                except Exception:
                    pct.append(0.0)
                    ended.append(False)
            if self.progress:
                done = sum(1 for e in ended if e)
                self.progress(sum(pct) / max(1, len(pct)) / 100.0,
                              f"Resolve is rendering ({done}/{len(jobs)} clips)")
            idle = 0 if busy else idle + 1
            if (not busy and all(ended)) or idle >= 5:
                break
            if time.time() - t0 > 6 * 3600:
                raise ResolveError("Rendering took more than 6 hours; stopped waiting.")
        listing = {}
        try:
            for d in self.project.GetRenderJobList() or []:
                listing[d.get("JobId")] = d
        except Exception:
            pass
        out = {}
        for j in jobs:
            st = {}
            try:
                st = self.project.GetRenderJobStatus(j) or {}
            except Exception:
                pass
            status = str(st.get("JobStatus", ""))
            if status and status.lower() not in ("complete", "completed"):
                raise ResolveError(f"Resolve render {status.lower()}"
                                   + (f": {st.get('Error')}" if st.get("Error") else "."))
            d = listing.get(j, {})
            folder, name = self.names[j]
            path = os.path.join(d.get("TargetDir", folder), d.get("OutputFilename", "")) \
                if d.get("OutputFilename") else ""
            if not path or not os.path.exists(path):
                hits = sorted(glob.glob(os.path.join(glob.escape(folder), glob.escape(name) + "*")),
                              key=os.path.getmtime, reverse=True)
                path = hits[0] if hits else ""
            if not path or not os.path.exists(path):
                raise ResolveError(f"Resolve says it rendered '{name}', but the file wasn't found in {folder}")
            out[j] = path
        return out


# The video formats offered in the app, as Resolve names them:
# app label -> (file extension, words to find the codec in Resolve's list)
RESOLVE_CODECS = {
    "H.264 (.mp4)": ("mp4", ["H.264", "H264"]),
    "H.264 (.mov)": ("mov", ["H.264", "H264"]),
    "ProRes 422 Proxy (.mov)": ("mov", ["ProRes 422 Proxy"]),
    "ProRes 422 LT (.mov)": ("mov", ["ProRes 422 LT"]),
    "DNxHR LB (.mov)": ("mov", ["DNxHR LB"]),
    "DNxHR LB (.mxf)": ("mxf", ["DNxHR LB"]),
}


def resolve_format(project, label: str) -> tuple[str, str]:
    """(format, codec) names Resolve uses for one of the app's video formats."""
    ext, words = RESOLVE_CODECS.get(label, RESOLVE_CODECS["H.264 (.mp4)"])
    formats = {}
    try:
        formats = project.GetRenderFormats() or {}
    except Exception:
        pass
    fmt = next((f for f in formats.values() if str(f).lower() == ext), None) or \
        next((f for f in formats.values() if str(f).lower().startswith(ext)), None) or ext
    try:
        codecs = project.GetRenderCodecs(fmt) or {}
    except Exception:
        codecs = {}
    bad = ("265", "hevc", "hq", "444")
    for w in words:
        for desc, name in codecs.items():
            d = str(desc).lower()
            if d.strip() == w.lower() or (w.lower() in d and not any(b in d.replace(w.lower(), "") for b in bad)):
                return fmt, name
    if codecs:
        raise ResolveError(f"Resolve can't render {label} on this computer. It offers: "
                           + ", ".join(map(str, codecs)) + ". Pick another video format.")
    return fmt, words[-1].replace(".", "")


def audio_format(project) -> tuple[str, str]:
    """Resolve's audio-only WAV format, for the stereo mix track."""
    formats = project.GetRenderFormats() or {}
    fmt = next((f for f in formats.values() if str(f).lower() == "wav"), "wav")
    codecs = project.GetRenderCodecs(fmt) or {}
    return fmt, (next(iter(codecs.values())) if codecs else "LinearPCM")


LIVE_PRESET = "MIUGLITCH Live H264"
PRESET_NAMES = ("ableton video", "miuglitch live h264")      # accepted as the app's preset


def fps_label(fps: Fraction) -> str:
    """Resolve's frame rate text: 25, 23.976, 29.97…"""
    return f"{float(fps):.3f}".rstrip("0").rstrip(".")


def preset_file() -> str | None:
    """The Resolve render preset shipped in bridge/assets (H.264, key frame every frame)."""
    from .template import builtin
    folder = os.path.dirname(builtin())
    hits = sorted(glob.glob(os.path.join(glob.escape(folder), "*.xml")))
    return hits[0] if hits else None


def ensure_live_preset(resolve, project, log) -> str | None:
    """Make sure Resolve has the app's H.264 preset; return its name, or None."""
    def names():
        try:
            return [str(n) for n in (project.GetRenderPresetList() or [])]
        except Exception:
            return []
    have = names()
    for n in have:
        if n.strip().lower() in PRESET_NAMES:
            return n
    path = preset_file()
    if not path:
        return None
    ok = False
    for obj in (resolve, resolve.GetProjectManager()):
        fn = getattr(obj, "ImportRenderPreset", None)
        if fn is None:
            continue
        try:
            ok = bool(fn(path))
        except Exception:
            ok = False
        if ok:
            break
    after = names()
    new = [n for n in after if n not in have]
    for n in after:
        if n.strip().lower() in PRESET_NAMES:
            log(f"Added the render preset '{n}' to Resolve")
            return n
    if new:
        log(f"Added the render preset '{new[0]}' to Resolve")
        return new[0]
    return None


def h264_every_frame(src: str, dst: str) -> str:
    """Fallback only: H.264 with a key frame on every frame, from Resolve's all-intra render."""
    from .media import tool, _run
    mp4 = dst.lower().endswith(".mp4")
    r = _run([tool("ffmpeg"), "-y", "-v", "error", "-i", src, "-map", "0:v:0", "-map", "0:a?",
              "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
              "-g", "1", "-keyint_min", "1", "-sc_threshold", "0", "-bf", "0",
              "-c:a", "aac" if mp4 else "pcm_s16le", *(["-b:a", "256k"] if mp4 else []),
              "-movflags", "+faststart", dst])
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-300:])
    return dst


def join_copy(parts: list[str], dst: str) -> str:
    """Join Resolve's head / body / tail renders into one file without
    re-encoding (same format, each piece starts on a key frame)."""
    from .media import tool, _run
    lst = dst + ".parts.txt"
    with open(lst, "w", encoding="utf-8") as fh:
        for p in parts:
            fh.write("file '" + os.path.abspath(p).replace("\\", "/").replace("'", "'\\''") + "'\n")
    r = _run([tool("ffmpeg"), "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst,
              "-map", "0", "-c", "copy", "-movflags", "+faststart", dst])
    os.remove(lst)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-300:])
    return dst


def build_handles_timeline(resolve, rt: ResolveTimeline, shots: list[Shot], head: int, tail: int, log):
    """Temporary timeline with each eligible shot on its own, source extended
    by head/tail frames. Returns (timeline, {shot index: (body_start, pre, post)})
    in that timeline's frames. Shots that can't be extended are left out."""
    project = rt.project_obj
    mp = project.GetMediaPool()
    name = f"{rt.name} - MIUGLITCH handles {time.strftime('%H%M%S')}"
    tl = mp.CreateEmptyTimeline(name)
    if not tl:
        raise ResolveError("Resolve couldn't make the temporary handles timeline.")
    try:
        if str(rt.obj.GetSetting("useCustomSettings")) == "1":
            tl.SetSetting("useCustomSettings", "1")
            for k in ("timelineFrameRate", "timelineResolutionWidth", "timelineResolutionHeight",
                      "timelineDropFrameTimecode"):
                v = rt.obj.GetSetting(k)
                if v not in (None, ""):
                    tl.SetSetting(k, v)
    except Exception:
        pass
    project.SetCurrentTimeline(tl)
    from .resolve_api import _rate
    fr = _rate(tl.GetSetting("timelineFrameRate"))
    if fr and fr != rt.fps:
        mp.DeleteTimelines([tl])
        project.SetCurrentTimeline(rt.obj)
        raise ResolveError(f"The temporary timeline came out at {float(fr):g} fps instead of "
                           f"{float(rt.fps):g} fps (project setting).")
    placed = {}
    gap = int(round(rt.fps))
    cursor = int(tl.GetStartFrame() or 0)
    tmp = tempfile.mkdtemp(prefix="miuglitch-comp-")
    for s in shots:
        it = s.item
        why = None
        if not it.is_footage or it.mpi is None:
            why = "not footage"
        elif it.speed != 1:
            why = "retimed"
        elif it.media_fps and it.media_fps != rt.fps:
            why = f"{float(it.media_fps):g} fps source"
        elif not it.media_frames or it.media_frames <= 1:
            why = "still image"
        if why:
            log(f"  shot {s.index} {s.name}: {why}, handles come from the edit")
            continue
        src_in = int(round(it.source_in * rt.fps)) + (s.start - it.start)
        length = s.end - s.start
        a = max(0, src_in - head)
        b = min(it.media_frames, src_in + length + tail)
        info = {"mediaPoolItem": it.mpi, "startFrame": a, "endFrame": b - 1,
                "trackIndex": 1, "recordFrame": cursor, "mediaType": 1}
        try:
            new = mp.AppendToTimeline([info])
        except Exception:
            new = None
        if not new:
            log(f"  shot {s.index} {s.name}: Resolve wouldn't add it, handles come from the edit")
            continue
        ni = new[0]
        n_start, n_end = int(ni.GetStart()), int(ni.GetEnd())
        pre = src_in - a
        post = (n_end - n_start) - pre - length
        if post < 0 or pre < 0:
            log(f"  shot {s.index} {s.name}: placed with unexpected length, handles come from the edit")
            continue
        _copy_look(it.obj, ni, tmp)
        placed[s.index] = (n_start + pre, pre, post)
        cursor = n_end + gap
    shutil.rmtree(tmp, ignore_errors=True)
    return tl, placed


def _copy_look(src, dst, tmp):
    if src is None:
        return
    try:
        src.CopyGrades([dst])
    except Exception:
        pass
    try:
        n = int(src.GetFusionCompCount() or 0)
        for k in range(1, n + 1):
            p = os.path.join(tmp, f"c{id(dst)}_{k}.comp")
            if src.ExportFusionComp(p, k):
                dst.ImportFusionComp(p)
    except Exception:
        pass
    for key in TRANSFORM_KEYS:
        try:
            v = src.GetProperty(key)
            if v is not None:
                dst.SetProperty(key, v)
        except Exception:
            pass


def render_start_frame(path: str, fps: Fraction, fallback: int) -> int:
    """First frame of a render, from its timecode (falls back to the mark in)."""
    try:
        tc = source_timecode(probe(path))
        if tc:
            return int(round(tc_to_seconds(tc, fps) * fps))
    except Exception:
        pass
    return fallback


# --- audio ---------------------------------------------------------------------
def audio_timeline(rt: ResolveTimeline, names: list[str] | None) -> Timeline:
    tl = Timeline(name=rt.name, frame_rate=rt.fps, drop_frame=rt.drop_frame, start=rt.sec(rt.start),
                  sample_rate=rt.sample_rate, width=rt.width, height=rt.height,
                  source_format="resolve", source_file=f"{rt.project} / {rt.name}")
    for idx in sorted(rt.audio_tracks):
        tname = rt.audio_tracks[idx]
        t = Track(name=tname, kind="audio")
        t.selected = names is None or tname in names
        for ri in sorted((i for i in rt.audio_items if i.track_index == idx), key=lambda i: i.start):
            if not ri.file:
                tl.warnings.append(f"{tname} / {ri.name}: no media file in Resolve, a red placeholder clip "
                                   "marks it in Live.")
            t.clips.append(Clip(name=ri.name, start=rt.sec(ri.start), duration=rt.sec(ri.end - ri.start),
                                source_path=ri.file, source_in=ri.source_in, channels=list(ri.channels),
                                enabled=ri.enabled, speed=ri.speed))
        t.channels = 1 if rt.audio_kinds.get(idx) == "mono" else 2
        tl.tracks.append(t)
    for idx in sorted(rt.video_tracks):
        t = Track(name=rt.video_tracks[idx], kind="video")
        for ri in sorted((i for i in rt.video_items if i.track_index == idx and i.is_footage),
                         key=lambda i: i.start):
            t.clips.append(Clip(name=ri.name, start=rt.sec(ri.start), duration=rt.sec(ri.end - ri.start),
                                source_path=ri.file, source_in=ri.source_in, enabled=ri.enabled,
                                speed=ri.speed))
        tl.tracks.append(t)
    for m in rt.markers:
        tl.markers.append(Marker(time=rt.sec(m.frame), name=m.name, note=m.note,
                                 duration=rt.sec(m.duration)))
    return tl


def add_shot_track(b, rt: ResolveTimeline, shots, offset, project_dir, sr, warnings):
    """One empty clip per shot, named after it, to select / loop / drag in Live.
    MIDI clips when the template has a MIDI track to copy, otherwise silent audio clips."""
    if not shots:
        return
    if b.midi_proto is not None:
        b.add_midi_track("SHOTS", [(rt.sec(s.start) - offset, rt.sec(s.end - s.start), s.name,
                                    VIDEO_COLORS[k % 2]) for k, s in enumerate(shots)
                                   if rt.sec(s.start) >= offset], LIVE_COLORS["VIDEO"])
        return
    from .media import tool, _run
    d = os.path.join(project_dir, "Samples", "Imported", "_shots")
    os.makedirs(d, exist_ok=True)
    longest = max(float(rt.sec(s.end - s.start)) for s in shots)
    wav = os.path.join(d, "shot (silence).wav")
    r = _run([tool("ffmpeg"), "-y", "-v", "error", "-f", "lavfi", "-t", f"{longest + 0.1:.6f}",
              "-i", f"anullsrc=r={sr}:cl=mono", "-c:a", "pcm_s16le", wav])
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-200:])
    clips = []
    for k, s in enumerate(shots):
        c = Clip(name=s.name, start=rt.sec(s.start), duration=rt.sec(s.end - s.start), source_path=wav,
                 source_in=Fraction(0))
        c.wav_path = wav
        c.color = VIDEO_COLORS[k % 2]
        clips.append(c)
    b.add_track("SHOTS", clips, offset, LIVE_COLORS["VIDEO"], warnings)


def placeholder_wav(media_dir: str, c: Clip, n: int, sr: int) -> str:
    """A silent WAV as long as the missing clip, so Live shows where it was."""
    from .media import tool, _run
    d = os.path.join(media_dir, "_missing")
    os.makedirs(d, exist_ok=True)
    base = _safe(os.path.splitext(c.name or os.path.basename(c.source_path) or "clip")[0])[:40]
    out = os.path.join(d, f"MISSING_{n:03d}_{base}.wav")
    r = _run([tool("ffmpeg"), "-y", "-v", "error", "-f", "lavfi", "-t", f"{float(c.duration) + 0.01:.6f}",
              "-i", f"anullsrc=r={sr}:cl=stereo", "-c:a", "pcm_s24le", out])
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-200:])
    return out


def enrich_from_aaf(tl: Timeline, aaf_tl: Timeline, log) -> tuple[int, int]:
    """Copy clip gain, fades and volume automation from Resolve's own AAF
    export onto the matching clips (same start, same media file).
    Returns (clips matched, clips with automation)."""
    pool = [c for t in aaf_tl.audio_tracks for c in t.clips]
    half = Fraction(1, 2) / tl.frame_rate
    matched = auto = 0
    used = set()
    for t in tl.audio_tracks:
        for c in t.clips:
            base = os.path.basename(c.source_path).lower()
            for k, a in enumerate(pool):
                if k in used or abs(a.start - c.start) > half:
                    continue
                if os.path.basename(a.source_path).lower() not in (base, "") and \
                        os.path.splitext(os.path.basename(a.source_path))[0].lower() != os.path.splitext(base)[0]:
                    continue
                used.add(k)
                c.gain_db, c.fade_in, c.fade_out = a.gain_db, a.fade_in, a.fade_out
                c.volume = list(a.volume)
                matched += 1
                auto += bool(c.volume)
                break
    return matched, auto


# --- the whole thing -----------------------------------------------------------
@dataclass
class Result:
    als_path: str
    report_path: str
    warnings: list[str] = field(default_factory=list)


def run(resolve, rt: ResolveTimeline, opt: Options) -> Result:
    log = opt.log
    say = opt.progress or (lambda f, t="": None)
    warnings = list(rt.warnings)
    fps = rt.fps

    name = _safe(opt.set_name or rt.name)
    iopt = ImportOptions(template=opt.template, out_dir=opt.out_dir, existing_project=opt.existing_project,
                         note=opt.note)
    project_dir, als_path = target_paths(iopt, name)
    offset = rt.sec(rt.start)
    earlier = None
    if opt.existing_project:
        if not os.path.isdir(project_dir):
            raise FileNotFoundError(f"Project folder not found: {project_dir}")
        als_path = _unique(als_path)
        earlier = project_sidecar(project_dir, opt.base_set)
        if earlier:
            offset, opt.tempo, opt.sample_rate = earlier.offset_s, earlier.tempo, earlier.media_sample_rate
            log("Using this project's earlier import settings (offset, tempo, sample rate).")
    os.makedirs(project_dir, exist_ok=True)
    log(f"Resolve: {rt.project} / {rt.name} · {float(fps):.3f} fps · {rt.width}x{rt.height} · "
        f"{rt.tc(rt.start)} – {rt.tc(rt.end)}")

    b = AlsBuilder(opt.template, base_path=opt.base_set)
    b.set_tempo(opt.tempo)
    level = None
    if opt.gain_staging:
        if b.utility_proto is None:
            warnings.append("Gain staging skipped: the Live template has no Utility device to copy. "
                            "Put a Utility (at 0 dB) on the template's audio track and save the template.")
        else:
            state = b.master_utility(18.0)
            if state == "added" and opt.base_set:
                # never make an existing set 18 dB louder: undo and leave levels alone
                main = b.liveset.find("MainTrack")
                if main is None:
                    main = b.liveset.find("MasterTrack")
                devs = main.find("DeviceChain/DeviceChain/Devices")
                devs.remove(devs.findall("StereoGain")[-1])
                warnings.append("The set you build on has no +18 dB Utility on its Main track, so new "
                                "tracks were left at 0 dB instead of -18 dB.")
            else:
                level = -18.0
                log("Gain staging: tracks at -18 dB, +18 dB Utility on the Main track"
                    + (" (already there)" if state == "present" else ""))

    tl = audio_timeline(rt, opt.audio_tracks)
    warnings += tl.warnings
    shots = plan_shots(rt) if opt.video else []
    shot_records = []
    picture_file = ""

    # ---------------- picture ----------------
    if opt.video and shots:
        from .video import VideoSettings, target_size
        vs = opt.video_settings or VideoSettings()
        size = target_size(vs, rt.width, rt.height)
        rate = None if vs.frame_rate == "Same as source" else vs.frame_rate
        preset, convert_to = opt.render_preset, None
        if not preset and vs.codec.startswith("H.264"):
            # Live needs a key frame on every frame. Resolve's scripting can't set
            # that, but a render preset can: use the one shipped with the app.
            preset = ensure_live_preset(resolve, rt.project_obj, log)
            if not preset:
                convert_to = vs.codec        # Resolve renders all-intra, app makes H.264 from it
                warnings.append("Resolve's H.264 key-frame preset couldn't be loaded, so Resolve rendered "
                                "ProRes Proxy and the app converted each clip to H.264 with a key frame "
                                "on every frame.")
        attempts = [(preset, convert_to)]
        if preset and not opt.render_preset:          # app's preset failing → safety net
            attempts.append((None, vs.codec))
        for attempt, (preset, convert_to) in enumerate(attempts):
            if attempt:
                for w in ("_parts",):
                    shutil.rmtree(os.path.join(project_dir, "Samples", "Video", w), ignore_errors=True)
            rate_ = rate or (fps_label(fps) if preset else None)
            try:
                if convert_to:
                    try:
                        fmt, codec = resolve_format(rt.project_obj, "ProRes 422 Proxy (.mov)")
                    except ResolveError:
                        fmt, codec = resolve_format(rt.project_obj, "DNxHR LB (.mov)")
                else:
                    fmt, codec = resolve_format(rt.project_obj, vs.codec)
                vdir = os.path.join(project_dir, "Samples", "Video")
                pdir = os.path.join(vdir, "_parts")
                head = int(round(Fraction(str(max(0.0, opt.video_head))) * fps))
                tail = int(round(Fraction(str(max(0.0, opt.video_tail))) * fps))
                log(f"{len(shots)} shots · Resolve renders {vs.codec} {size[0]}x{size[1]}"
                    + (f" via preset '{preset}'" if preset else "")
                    + f" · handles {opt.video_head:g}s / {opt.video_tail:g}s · "
                    + ("real source handles" if opt.render_mode == MODE_HANDLES else "handles from the edit"))
                tag = _safe(rt.name)[:40]
                sec = lambda fr: Fraction(fr) / fps          # noqa: E731
                plan = {}                                    # shot index -> dict
                with Renderer(resolve, rt, fmt, codec, log, lambda f, t="": say(0.05 + 0.6 * f, t),
                              preset=preset, frame_rate=rate_) as R:
                    htl, placed = None, {}
                    if opt.shot_clips and opt.render_mode == MODE_HANDLES:
                        try:
                            log("Building the temporary handles timeline in Resolve…")
                            htl, placed = build_handles_timeline(resolve, rt, shots, head, tail, log)
                        except ResolveError as e:
                            warnings.append(f"Real handles not possible ({e}); handles come from the edit.")
                            placed = {}
                    try:
                        for s in (shots if opt.shot_clips else []):
                            track_name = rt.video_tracks.get(s.item.track_index, f"V{s.item.track_index}")
                            nm = f"{_safe(track_name)}_{s.index:03d}_{_safe(s.name)[:50]}"
                            p = {"track": track_name, "name": nm}
                            if s.index in placed:
                                body, pre, post = placed[s.index]
                                L = s.end - s.start
                                p.update(kind="source", pre=pre, post=post, jobs=[])
                                if pre:
                                    p["jobs"].append(R.queue(htl, pdir, nm + "_head", body - pre, body, size))
                                p["jobs"].append(R.queue(rt.obj, pdir, nm + "_body", s.start, s.end, size))
                                if post:
                                    p["jobs"].append(R.queue(htl, pdir, nm + "_tail", body + L, body + L + post, size))
                            else:
                                a, e = max(rt.start, s.start - head), min(rt.end, s.end + tail)
                                p.update(kind="edit", pre=s.start - a, post=e - s.end,
                                         jobs=[R.queue(rt.obj, pdir if convert_to else vdir, nm, a, e, size)])
                            plan[s.index] = p
                        pic_job = None
                        if opt.continuous:
                            pic_job = R.queue(rt.obj, pdir if convert_to else vdir, f"{tag}_picture",
                                              rt.start, rt.end, size)
                        mix_job = None
                        if opt.mix_track:
                            try:
                                mix_job = R.queue(rt.obj, os.path.join(project_dir, "Samples", "Imported"),
                                                  f"{tag}_stereo mix", rt.start, rt.end, video=False,
                                                  fmt=audio_format(rt.project_obj))
                            except Exception as e:
                                warnings.append(f"Stereo mix track not made: {e}")
                        log(f"Resolve is rendering {len(R.jobs)} clip{'s' if len(R.jobs) != 1 else ''}…")
                        files = R.render(list(R.jobs))
                    finally:
                        if htl is not None:
                            try:
                                rt.project_obj.SetCurrentTimeline(rt.obj)
                                rt.project_obj.GetMediaPool().DeleteTimelines([htl])
                            except Exception:
                                pass
                break
            except ResolveError as e:
                if attempt == len(attempts) - 1:
                    raise
                warnings.append(f"Resolve couldn't render with the preset '{preset}' ({e}). Resolve rendered "
                                "ProRes Proxy instead and the app converted each clip to H.264 with a key "
                                "frame on every frame (the preset may use a graphics-card encoder this "
                                "computer doesn't have).")
                log("! Preset render failed, rendering ProRes Proxy and converting instead…")

        if pic_job:
            dst = files[pic_job]
            ok = True
            if convert_to:
                from .video import CODECS
                final = os.path.join(vdir, f"{tag}_picture" + CODECS[convert_to][0])
                say(0.66, "Converting the picture to H.264")
                try:
                    dst = h264_every_frame(dst, final)
                except Exception as e:
                    warnings.append(f"Picture: H.264 conversion failed ({e}).")
                    ok = False
            if ok:
                pc = Clip(name=rt.name, start=rt.sec(rt.start), duration=rt.sec(rt.end - rt.start),
                          source_path=dst, source_in=Fraction(0))
                pc.wav_path = dst
                b.add_track("PICTURE", [pc], offset, LIVE_COLORS["VIDEO"], warnings, volume_db=level)
                picture_file = os.path.abspath(dst)

        per_track: dict[int, list[Clip]] = {}
        for n, s in enumerate(shots):
            if s.index not in plan:                  # no per-shot clip: just remember the shot
                shot_records.append({
                    "index": s.index, "name": s.name,
                    "track": rt.video_tracks.get(s.item.track_index, f"V{s.item.track_index}"),
                    "start": sc._f(rt.sec(s.start)), "duration": sc._f(sec(s.end - s.start)),
                    "source": s.item.file, "source_in": sc._f(s.source_in) if s.item.file else "0/1",
                    "source_tc": shot_tc(rt, s), "speed": sc._f(Fraction(s.item.speed)),
                    "resolve_item": s.item.uid, "overlays": [o.name for o in s.overlays]})
                continue
            p = plan[s.index]
            say(0.65 + 0.15 * n / len(shots), "Collecting clips")
            parts = [files[j] for j in p["jobs"]]
            if len(parts) == 1 and p["kind"] == "edit" and not convert_to:
                dst = parts[0]
            elif len(parts) == 1:
                dst = parts[0]
            else:
                ext = os.path.splitext(parts[-1])[1]
                dst = os.path.join(pdir if convert_to else vdir, p["name"] + ext)
                try:
                    join_copy(parts, dst)
                except Exception as e:
                    warnings.append(f"Shot {s.index} {s.name}: couldn't join head/body/tail ({e}); "
                                    "the clip has no handles.")
                    body = files[p["jobs"][1 if p["pre"] else 0]]
                    shutil.copy2(body, dst)
                    p["pre"] = p["post"] = 0
            if convert_to:
                from .video import CODECS
                final = os.path.join(vdir, p["name"] + CODECS[convert_to][0])
                try:
                    h264_every_frame(dst, final)
                    dst = final
                except Exception as e:
                    warnings.append(f"Shot {s.index} {s.name}: H.264 conversion failed ({e}).")
                    continue
            vc = Clip(name=s.name, start=rt.sec(s.start), duration=sec(s.end - s.start), source_path=dst,
                      source_in=sec(p["pre"]))
            vc.wav_path = dst
            per_track.setdefault(s.item.track_index, []).append(vc)
            shot_records.append({
                "index": s.index, "name": s.name, "track": p["track"],
                "start": sc._f(rt.sec(s.start)), "duration": sc._f(sec(s.end - s.start)),
                "source": s.item.file, "source_in": sc._f(s.source_in) if s.item.file else "0/1",
                "source_tc": shot_tc(rt, s), "speed": sc._f(Fraction(s.item.speed)),
                "resolve_item": s.item.uid, "overlays": [o.name for o in s.overlays],
                "handles": p["kind"], "head": sc._f(sec(p["pre"])), "tail": sc._f(sec(p["post"])),
                "live_file": os.path.abspath(dst)})
        shutil.rmtree(pdir, ignore_errors=True)
        for idx in sorted(per_track, reverse=True):          # V2 above V1, like Resolve
            clips = per_track[idx]
            for k, c in enumerate(clips):
                c.color = VIDEO_COLORS[k % 2]
            b.add_track(rt.video_tracks.get(idx, f"V{idx}"), clips, offset, LIVE_COLORS["VIDEO"], warnings,
                        volume_db=level)

        if mix_job and mix_job in files:
            mix = files[mix_job]
            mc = Clip(name="Stereo mix", start=rt.sec(rt.start), duration=rt.sec(rt.end - rt.start),
                      source_path=mix, source_in=Fraction(0))
            mc.wav_path = mix
            b.add_track("STEREO MIX", [mc], offset, LIVE_COLORS["PICTURE"], warnings, volume_db=level)

        if opt.shot_track:
            try:
                add_shot_track(b, rt, shots, offset, project_dir, opt.sample_rate, warnings)
            except Exception as e:
                warnings.append(f"Shot track not made: {e}")
    elif opt.video:
        warnings.append("No video clips on the Resolve timeline, so no picture was made.")

    # ---------------- audio ----------------
    conv = None
    if opt.audio and any(t.selected and t.clips for t in tl.audio_tracks):
        if opt.read_aaf:
            try:
                tmp = tempfile.mkdtemp(prefix="miuglitch-aaf-")
                aaf = os.path.join(tmp, f"{_safe(rt.name)}.aaf")
                ok = rt.obj.Export(aaf, resolve.EXPORT_AAF, resolve.EXPORT_AAF_NEW)
                if ok and os.path.exists(aaf):
                    from .readers.aaf import read as read_aaf
                    m, a = enrich_from_aaf(tl, read_aaf(aaf), log)
                    log(f"Read clip gain and fades from Resolve's AAF ({m} clips); "
                        f"{a} clip{'s' if a != 1 else ''} with volume automation")
                    if a == 0:
                        log("  No volume automation came through Resolve's AAF export. Track (Fairlight) "
                            "automation isn't available to outside apps, only clip-level volume.")
                else:
                    warnings.append("Resolve didn't export an AAF, so clip gain, fades and automation "
                                    "weren't brought over.")
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception as e:
                warnings.append(f"Couldn't read clip gain/automation via AAF: {e}")

        media_dir = os.path.join(project_dir, "Samples", "Imported")
        conv = MediaConverter(media_dir, fps, log=log, sample_rate=opt.sample_rate)
        clips = [(t, c) for t in tl.audio_tracks if t.selected for c in t.clips]
        if opt.audio_whole_files:
            conv.run(tl, lambda i, n: say(0.8 + 0.15 * i / max(n, 1), "Converting audio"))
        else:
            h, tl_ = Fraction(str(max(0.0, opt.audio_head))), Fraction(str(max(0.0, opt.audio_tail)))
            for i, (t, c) in enumerate(clips):
                say(0.8 + 0.15 * i / max(len(clips), 1), "Converting audio")
                try:
                    if c.speed and c.speed != 1:
                        c.wav_path = conv.convert_retimed(c)
                        c.wav_offset = -c.source_in
                        continue
                    wav, start = conv.convert_segment(c, h, tl_, f"{t.name}_{i + 1:03d}")
                    c.wav_path, c.wav_offset = wav, -start
                except Exception as e:
                    conv.warnings.append(f"{t.name} / {c.name}: {e}")
                    c.wav_path = None
        warnings += conv.warnings
        missing = [(t, c) for t, c in clips if not c.wav_path]
        for k, (t, c) in enumerate(missing, start=1):
            try:
                c.wav_path = placeholder_wav(media_dir, c, k, opt.sample_rate)
                c.wav_offset = -c.source_in
                c.name = f"MISSING: {c.name or os.path.basename(c.source_path) or 'clip'}"
                c.color = MISSING_COLOR
                c.gain_db = 0.0
            except Exception as e:
                warnings.append(f"{t.name} / {c.name}: no placeholder clip ({e})")
        if missing:
            log(f"{len(missing)} clip{'s' if len(missing) != 1 else ''} with missing media → red placeholder "
                "clips (their volume automation is still written)")
        if opt.normalize:
            for t, c in clips:
                if not c.wav_path or c.color == MISSING_COLOR:
                    continue
                pk = peak_db(c.wav_path, float(c.source_in + c.wav_offset), float(c.duration))
                if pk is not None:
                    c.gain_db = max(-60.0, min(24.0, opt.normalize_db - pk))
            log(f"Normalised clips to {opt.normalize_db:g} dBFS peak (as clip gain, files untouched)")
        for t in tl.audio_tracks:
            if t.selected and t.clips:
                b.add_track(t.name, t.clips, offset, LIVE_COLORS["ORIG"], warnings,
                            volume_db=level, utility=b.utility_proto is not None)

    # ---------------- locators ----------------
    if opt.resolve_markers:
        for i, m in enumerate(tl.markers):
            if m.time >= offset:
                b.add_locator(m.time - offset, m.name or m.note or f"Marker {i + 1}", i)
    if opt.shot_markers:
        for s in shots:
            t = rt.sec(s.start)
            if t >= offset:
                b.add_locator(t - offset, s.name, 0)

    b.save(als_path)
    if not opt.existing_project:
        from .projectfolder import make_live_project
        try:
            make_live_project(project_dir, os.path.dirname(os.path.abspath(opt.template)), log)
        except OSError:
            pass

    # ---------------- .bridge.json ----------------
    side = sc.make(tl, offset, opt.tempo)
    side.media_sample_rate = opt.sample_rate
    side.video = sc.tracks_to_list([t for t in tl.tracks if t.kind == "video"])
    side.extra_audio = sc.tracks_to_list([t for t in tl.audio_tracks if not t.selected])
    side.resolve = {"project": rt.project, "timeline": rt.name, "timeline_id": rt.uid,
                    "render_mode": opt.render_mode if opt.video else "",
                    "built": time.strftime("%Y-%m-%d %H:%M:%S")}
    side.shots = shot_records
    side.resolve["picture"] = picture_file
    if earlier:
        side.media = list(earlier.media)
    if conv:
        known = {os.path.normcase(m.wav) for m in side.media}
        for wav, src, chans in conv.links:
            key = os.path.normcase(os.path.abspath(wav))
            if key in known:
                continue
            start = conv.seg_starts.get(key, Fraction(0))
            side.media.append(sc.MediaLink(wav=os.path.abspath(wav), source=src, channels=chans,
                                           wav_start_in_source=sc._f(start)))
    side.save(sc.Sidecar.path_for(als_path))

    report = os.path.splitext(als_path)[0] + " - import report.txt"
    with open(report, "w", encoding="utf-8") as fh:
        fh.write(f"From Resolve: {rt.project} / {rt.name}\nLive Set: {als_path}\n")
        fh.write(f"Frame rate: {float(fps):.3f}{' DF' if rt.drop_frame else ''}\n")
        fh.write(f"Live 0:00 = {seconds_to_tc(offset, fps, rt.drop_frame)}\nTempo: {opt.tempo} BPM\n")
        fh.write(f"Audio at {opt.sample_rate} Hz\n\nShots ({len(shot_records)}):\n")
        for r in shot_records:
            fh.write(f"  {r['index']:3d}  {r['track']}  {r['name']}  src {r['source_tc'] or '-'}  "
                     + (f"handles from {r['handles']}" if r.get('handles') else "") + (f"  (+ {', '.join(r['overlays'])})" if r['overlays'] else "")
                     + "\n")
        fh.write("\nAudio tracks:\n")
        for t in tl.audio_tracks:
            fh.write(f"  [{'x' if t.selected else ' '}] {t.name}: {len(t.clips)} clips\n")
        if warnings:
            fh.write("\nWarnings:\n" + "\n".join(f"- {w}" for w in warnings) + "\n")
    say(1.0, "Done")
    log(f"Done: {als_path}")
    for w in warnings:
        log("! " + w)
    return Result(als_path, report, warnings)
