"""Talk to a running DaVinci Resolve Studio through its scripting API.

Needs: DaVinci Resolve *Studio* open, and Preferences > System > General >
"External scripting using" set to "Local". The free version of Resolve only
allows scripts from its own Workspace > Scripts menu, so it can't be reached.

Everything Resolve-specific is kept here; the rest of the tool works on plain
data (ResolveTimeline / RItem) so it can be tested without Resolve.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from fractions import Fraction

from .timecode import parse_rate, seconds_to_tc, tc_to_seconds


class ResolveError(RuntimeError):
    pass


HOW_TO = ("Check that:\n"
          "  • DaVinci Resolve Studio is open with a project and a timeline\n"
          "  • Resolve > Preferences > System > General > External scripting using = Local\n"
          "  • it's the Studio version (the free version doesn't allow outside scripts)")


def _lib_candidates():
    env = os.environ.get("RESOLVE_SCRIPT_LIB")
    if env:
        yield env
    if sys.platform.startswith("win"):
        for root in (os.environ.get("PROGRAMFILES", r"C:\Program Files"), r"C:\Program Files"):
            yield os.path.join(root, "Blackmagic Design", "DaVinci Resolve", "fusionscript.dll")
    elif sys.platform == "darwin":
        yield "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
    else:
        yield "/opt/resolve/libs/Fusion/fusionscript.so"


def _load_fusionscript():
    import importlib.machinery
    import importlib.util
    tried = []
    for path in _lib_candidates():
        if not os.path.isfile(path):
            tried.append(path)
            continue
        folder = os.path.dirname(path)
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(folder)
            except OSError:
                pass
        os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
        loader = importlib.machinery.ExtensionFileLoader("fusionscript", path)
        spec = importlib.util.spec_from_file_location("fusionscript", path, loader=loader)
        mod = importlib.util.module_from_spec(spec)
        try:
            loader.exec_module(mod)
        except Exception as e:
            raise ResolveError(f"Found Resolve's scripting library but couldn't load it ({e}).\n{path}")
        return mod
    raise ResolveError("DaVinci Resolve isn't installed in the usual place "
                       "(no fusionscript.dll found).\nLooked in: " + "; ".join(tried))


def connect():
    """Return Resolve's scripting object, or raise ResolveError with what to fix."""
    mod = _load_fusionscript()
    resolve = None
    try:
        resolve = mod.scriptapp("Resolve")
    except Exception:
        resolve = None
    if resolve is None:
        raise ResolveError("Couldn't connect to DaVinci Resolve.\n" + HOW_TO)
    return resolve


def current(resolve):
    """(project, timeline) currently open in Resolve."""
    pm = resolve.GetProjectManager()
    project = pm.GetCurrentProject() if pm else None
    if not project:
        raise ResolveError("Resolve has no project open.")
    tl = project.GetCurrentTimeline()
    if not tl:
        raise ResolveError(f"Project '{project.GetName()}' has no timeline open. Open the timeline to bring over.")
    return project, tl


# --- plain data ---------------------------------------------------------------
AUDIO_EXT = {".wav", ".aif", ".aiff", ".mp3", ".flac", ".bwf", ".m4a", ".aac", ".ogg", ".caf"}


@dataclass
class RItem:
    track_type: str                 # 'video' / 'audio'
    track_index: int                # 1-based
    track_name: str
    name: str
    start: int                      # timeline frames (absolute, like Resolve shows)
    end: int                        # exclusive
    file: str = ""                  # media file ('' for titles, generators, Fusion clips…)
    source_in: Fraction = Fraction(0)   # seconds into the media file
    speed: Fraction = Fraction(1)
    enabled: bool = True
    channels: list[int] = field(default_factory=list)
    source_tc: str = ""             # media's start timecode
    media_fps: Fraction | None = None
    media_frames: int | None = None
    uid: str = ""
    obj: object = None              # Resolve TimelineItem (None in tests)
    mpi: object = None              # Resolve MediaPoolItem

    @property
    def is_footage(self) -> bool:
        return bool(self.file) and os.path.splitext(self.file)[1].lower() not in AUDIO_EXT


@dataclass
class RMarker:
    frame: int                      # absolute timeline frame
    name: str
    note: str = ""
    color: str = ""
    duration: int = 1


@dataclass
class ResolveTimeline:
    project: str
    name: str
    fps: Fraction
    drop_frame: bool
    start: int                      # first frame (e.g. 86400 = 01:00:00:00 at 24 fps)
    end: int                        # exclusive
    width: int
    height: int
    sample_rate: int = 48000
    video_tracks: dict[int, str] = field(default_factory=dict)   # index -> name
    audio_tracks: dict[int, str] = field(default_factory=dict)
    audio_kinds: dict[int, str] = field(default_factory=dict)    # index -> 'mono'/'stereo'/…
    items: list[RItem] = field(default_factory=list)
    markers: list[RMarker] = field(default_factory=list)
    uid: str = ""
    obj: object = None
    project_obj: object = None
    warnings: list[str] = field(default_factory=list)

    def sec(self, frame) -> Fraction:
        return Fraction(frame) / self.fps

    def tc(self, frame) -> str:
        return seconds_to_tc(self.sec(frame), self.fps, self.drop_frame)

    @property
    def video_items(self):
        return [i for i in self.items if i.track_type == "video"]

    @property
    def audio_items(self):
        return [i for i in self.items if i.track_type == "audio"]


# --- reading ------------------------------------------------------------------
def _call(obj, name, *args, default=None):
    fn = getattr(obj, name, None)
    if fn is None:
        return default
    try:
        v = fn(*args)
    except Exception:
        return default
    return default if v is None else v


def _num(v, default=None):
    try:
        return Fraction(str(v).strip())
    except (ValueError, TypeError, ZeroDivisionError):
        return default


def _rate(v) -> Fraction | None:
    if v in (None, ""):
        return None
    s = str(v).replace("DF", "").replace("NDF", "").strip()
    try:
        return parse_rate(s)
    except (ValueError, ZeroDivisionError):
        return None


def _channel_map(item) -> list[list[int]]:
    """Source channels per audio track of the item's clip, from Resolve's
    GetSourceAudioChannelMapping (Resolve 18.5+). [] when unknown."""
    raw = _call(item, "GetSourceAudioChannelMapping", default="")
    if not raw:
        return []
    try:
        d = json.loads(raw)
    except ValueError:
        return []
    tm = d.get("track_mapping") or {}
    out = []
    for k in sorted(tm, key=lambda x: int(x) if str(x).isdigit() else 0):
        e = tm[k] or {}
        if e.get("mute"):
            continue
        idx = [int(c) for c in (e.get("channel_idx") or []) if str(c).lstrip("-").isdigit() and int(c) > 0]
        if idx:
            out.append(idx)
    return out


def read(resolve) -> ResolveTimeline:
    project, tl = current(resolve)
    fps = _rate(_call(tl, "GetSetting", "timelineFrameRate")) or _rate(
        _call(project, "GetSetting", "timelineFrameRate")) or Fraction(25)
    drop = str(_call(tl, "GetSetting", "timelineDropFrameTimecode", default="0")).strip() in ("1", "true", "True")
    w = int(_num(_call(tl, "GetSetting", "timelineResolutionWidth"), 1920))
    h = int(_num(_call(tl, "GetSetting", "timelineResolutionHeight"), 1080))
    sr = int(_num(_call(project, "GetSetting", "audioPlayoutSampleRate") or
                  _call(project, "GetSetting", "timelineSampleRate"), 48000) or 48000)
    start = int(_call(tl, "GetStartFrame", default=0))
    end = int(_call(tl, "GetEndFrame", default=start))
    rt = ResolveTimeline(project=project.GetName(), name=tl.GetName(), fps=fps, drop_frame=drop,
                         start=start, end=end, width=w, height=h, sample_rate=sr,
                         uid=str(_call(tl, "GetUniqueId", default="")), obj=tl, project_obj=project)

    for kind in ("video", "audio"):
        n = int(_call(tl, "GetTrackCount", kind, default=0))
        for i in range(1, n + 1):
            name = _call(tl, "GetTrackName", kind, i, default="") or f"{kind[0].upper()}{i}"
            enabled = _call(tl, "GetIsTrackEnabled", kind, i, default=True)
            (rt.video_tracks if kind == "video" else rt.audio_tracks)[i] = name
            if kind == "audio":
                rt.audio_kinds[i] = str(_call(tl, "GetTrackSubType", kind, i, default="stereo")).lower()
            if enabled is False:
                rt.warnings.append(f"Track '{name}' is switched off in Resolve, so it was left out.")
                continue
            for it in _call(tl, "GetItemListInTrack", kind, i, default=[]) or []:
                ri = _item(it, kind, i, name, fps)
                if ri is not None:
                    rt.items.append(ri)

    # which of the clip's audio tracks does each audio item play? Items of the
    # same clip cut at the same place on several tracks take them in order.
    groups: dict = {}
    for ri in rt.audio_items:
        groups.setdefault((ri.file.lower(), ri.start, ri.end), []).append(ri)
    for g in groups.values():
        g.sort(key=lambda r: r.track_index)
        for k, ri in enumerate(g):
            maps = ri.channels                      # temporarily holds the full map
            ri.channels = maps[k] if k < len(maps) else (maps[0] if len(maps) == 1 else [])
    for ri in rt.audio_items:
        if not ri.channels:
            ri.channels = [1] if rt.audio_kinds.get(ri.track_index) == "mono" else [1, 2]

    for frame, m in (_call(tl, "GetMarkers", default={}) or {}).items():
        try:
            f = int(round(float(frame)))
        except (TypeError, ValueError):
            continue
        rt.markers.append(RMarker(frame=start + f, name=m.get("name", "") or "", note=m.get("note", "") or "",
                                  color=m.get("color", "") or "", duration=int(m.get("duration", 1) or 1)))
    rt.markers.sort(key=lambda m: m.frame)
    return rt


def _item(it, kind, idx, track_name, fps) -> RItem | None:
    try:
        start, end = int(it.GetStart()), int(it.GetEnd())
    except Exception:
        return None
    if end <= start:
        return None
    ri = RItem(track_type=kind, track_index=idx, track_name=track_name,
               name=_call(it, "GetName", default="") or "", start=start, end=end, obj=it)
    ri.enabled = bool(_call(it, "GetClipEnabled", default=True))
    ri.uid = str(_call(it, "GetUniqueId", default=""))
    mpi = _call(it, "GetMediaPoolItem")
    ri.mpi = mpi
    if mpi:
        ri.file = _call(mpi, "GetClipProperty", "File Path", default="") or ""
        ri.source_tc = _call(mpi, "GetClipProperty", "Start TC", default="") or ""
        ri.media_fps = _rate(_call(mpi, "GetClipProperty", "FPS"))
        fr = _num(_call(mpi, "GetClipProperty", "Frames"))
        ri.media_frames = int(fr) if fr else None
    dur_s = Fraction(end - start) / fps
    # where the item starts in its media: Resolve 19 has seconds, older ones frames
    s_in = _num(_call(it, "GetSourceStartTime"))
    s_out = _num(_call(it, "GetSourceEndTime"))
    if s_in is None:
        sf = _num(_call(it, "GetSourceStartFrame"))
        mf = ri.media_fps or fps
        if sf is not None:
            s_in = sf / mf
            ef = _num(_call(it, "GetSourceEndFrame"))
            s_out = ef / mf if ef is not None else None
        else:
            s_in = Fraction(int(_call(it, "GetLeftOffset", default=0))) / fps
    # some versions count from the media's timecode instead of its first frame
    if ri.source_tc and ri.media_fps and ri.media_frames:
        media_len = Fraction(ri.media_frames) / ri.media_fps
        if s_in > media_len + 1:
            try:
                tc0 = tc_to_seconds(ri.source_tc, ri.media_fps)
                s_in -= tc0
                if s_out is not None:
                    s_out -= tc0
            except ValueError:
                pass
    ri.source_in = max(Fraction(0), s_in)
    if s_out is not None and dur_s > 0 and s_out > s_in:
        sp = ((s_out - s_in) / dur_s).limit_denominator(1000)
        if abs(float(sp) - 1) > 0.01:
            ri.speed = sp
    if kind == "audio":
        ri.channels = _channel_map(it)   # list of lists until read() picks one
    return ri


# --- rendering ---------------------------------------------------------------
PREFERRED_CODECS = ("ProRes 422 LT", "ProRes 422", "DNxHR SQ", "DNxHR LB", "DNxHR HQ", "H.264", "H264")


def pick_codec(project) -> tuple[str, str]:
    codecs = _call(project, "GetRenderCodecs", "mov", default={}) or {}
    for want in PREFERRED_CODECS:
        for desc, name in codecs.items():
            if want.lower() in str(desc).lower() and "hq" not in str(desc).lower().replace(want.lower(), ""):
                return "mov", name
    for want in PREFERRED_CODECS:
        for desc, name in codecs.items():
            if want.lower() in str(desc).lower():
                return "mov", name
    if codecs:
        return "mov", next(iter(codecs.values()))
    return "mov", "H264"
