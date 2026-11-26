"""A stand-in for DaVinci Resolve's scripting API, just enough for the
Resolve → Live flow. Rendering paints every shot in its own colour (and a
darker shade on the handles timeline) so tests can check which picture ends
up in which frame of each Live clip."""
from __future__ import annotations

import os
import subprocess

COLORS = {"snow": (255, 0, 0), "GX010030.MP4": (0, 0, 255), "black": (0, 0, 0)}


def color_of(name, dim=False):
    c = COLORS.get(name) or COLORS.get(os.path.splitext(name)[0]) or (0, 255, 0)
    return tuple(v // 2 for v in c) if dim else c


class MPI:
    def __init__(self, path, fps="25", frames=500, tc="00:00:00:00"):
        self.props = {"File Path": path, "FPS": fps, "Frames": str(frames), "Start TC": tc}
        self.name = os.path.basename(path)

    def GetClipProperty(self, k):
        return self.props.get(k, "")

    def GetName(self):
        return self.name


class Item:
    _n = 0

    def __init__(self, name, start, end, mpi=None, src_in_s=0.0, fps=25, chmap=None):
        Item._n += 1
        self.uid = f"item{Item._n}"
        self.name, self.start, self.end, self.mpi = name, start, end, mpi
        self.src_in = src_in_s
        self.fps = fps
        self.chmap = chmap
        self.props = {"ZoomX": 1.0}
        self.graded = False

    def GetName(self): return self.name
    def GetStart(self): return self.start
    def GetEnd(self): return self.end
    def GetDuration(self): return self.end - self.start
    def GetMediaPoolItem(self): return self.mpi
    def GetClipEnabled(self): return True
    def GetUniqueId(self): return self.uid
    def GetSourceStartTime(self): return self.src_in if self.mpi else None
    def GetSourceEndTime(self): return self.src_in + (self.end - self.start) / self.fps if self.mpi else None

    def GetSourceAudioChannelMapping(self):
        import json
        return json.dumps(self.chmap) if self.chmap else ""

    def CopyGrades(self, items):
        for i in items:
            i.graded = True
        return True

    def GetFusionCompCount(self): return 0
    def GetProperty(self, k): return self.props.get(k)

    def SetProperty(self, k, v):
        self.props[k] = v
        return True


class FakeTimeline:
    def __init__(self, name, start=90000, fps="25"):
        self.name, self.start, self.fps = name, start, fps
        self.tracks = {"video": [], "audio": []}      # list of (name, [items])
        self.markers = {}
        self.handles = False

    def GetName(self): return self.name
    def GetUniqueId(self): return "tl-" + self.name

    def GetSetting(self, k):
        return {"timelineFrameRate": self.fps, "timelineResolutionWidth": "1920",
                "timelineResolutionHeight": "1080", "timelineDropFrameTimecode": "0",
                "useCustomSettings": "0"}.get(k, "")

    def SetSetting(self, k, v): return True
    def GetStartFrame(self): return self.start

    def GetEndFrame(self):
        ends = [i.end for _, its in self.tracks["video"] + self.tracks["audio"] for i in its]
        return max(ends) if ends else self.start

    def GetTrackCount(self, kind): return len(self.tracks[kind])
    def GetTrackName(self, kind, i): return self.tracks[kind][i - 1][0]
    def GetIsTrackEnabled(self, kind, i): return True
    def GetTrackSubType(self, kind, i): return "stereo"
    def GetItemListInTrack(self, kind, i): return list(self.tracks[kind][i - 1][1])
    def GetMarkers(self): return self.markers
    def Export(self, path, kind, sub): return False


class MediaPool:
    def __init__(self, project):
        self.project = project

    def CreateEmptyTimeline(self, name):
        t = FakeTimeline(name, start=self.project.resolve.handles_start)
        t.handles = True
        t.tracks["video"].append(("Video 1", []))
        self.project.timelines.append(t)
        self.project.current = t
        return t

    def AppendToTimeline(self, infos):
        out = []
        t = self.project.current
        for d in infos:
            mpi = d["mediaPoolItem"]
            a, b = d["startFrame"], d["endFrame"]
            rec = d.get("recordFrame", t.GetEndFrame())
            it = Item(mpi.name, rec, rec + (b - a + 1), mpi=mpi, src_in_s=a / 25)
            t.tracks["video"][0][1].append(it)
            out.append(it)
        return out

    def DeleteTimelines(self, tls):
        for t in tls:
            self.project.timelines.remove(t)
        return True


class Project:
    def __init__(self, resolve, timeline):
        self.resolve = resolve
        self.timelines = [timeline]
        self.current = timeline
        self.settings = {}
        self.jobs = {}
        self.fmt = {"format": "mp4", "codec": "H264"}
        self.mode = 0

    def GetName(self): return "Test project"
    def GetCurrentTimeline(self): return self.current

    def SetCurrentTimeline(self, t):
        self.current = t
        return True

    def GetSetting(self, k): return {"timelineFrameRate": "25"}.get(k, "")
    def GetMediaPool(self): return MediaPool(self)
    def GetRenderFormats(self): return {"QuickTime": "mov", "MP4": "mp4", "MXF OP1A": "mxf_op1a", "Wave": "wav"}

    def GetRenderCodecs(self, fmt):
        if fmt == "wav":
            return {"Linear PCM": "LinearPCM"}
        c = {"H.264": "H264", "H.265": "H265"}
        if fmt == "mov":
            c.update({"Apple ProRes 422 HQ": "ProRes422HQ", "Apple ProRes 422 LT": "ProRes422LT",
                      "Apple ProRes 422 Proxy": "ProRes422Proxy", "DNxHR LB": "DNxHRLB"})
        return c

    presets = ["Live H264"]

    def GetRenderPresetList(self): return list(self.presets)

    def LoadRenderPreset(self, name):
        if name not in self.presets:
            return False
        self.fmt = {"format": "mp4", "codec": "H264_NVENC" if self.no_gpu else "H264"}
        return True

    no_gpu = False
    def GetCurrentRenderFormatAndCodec(self): return dict(self.fmt)

    def SetCurrentRenderFormatAndCodec(self, f, c):
        self.fmt = {"format": f, "codec": c}
        return True

    def GetCurrentRenderMode(self): return self.mode

    def SetCurrentRenderMode(self, m):
        self.mode = m
        return True

    def SetRenderSettings(self, s):
        self.settings = dict(s)
        return True

    def AddRenderJob(self):
        jid = f"job{len(self.jobs) + 1}"
        self.jobs[jid] = {"settings": dict(self.settings), "timeline": self.current, "done": False,
                          "fmt": dict(self.fmt)}
        return jid

    def DeleteRenderJob(self, j):
        self.jobs.pop(j, None)
        return True

    def StartRendering(self, jobs, interactive=False):
        for j in jobs:
            if self.jobs[j]["fmt"]["codec"] != "H264_NVENC":
                _render(self.jobs[j])
        return True

    def IsRenderingInProgress(self): return False

    def GetRenderJobStatus(self, j):
        if self.jobs.get(j, {}).get("fmt", {}).get("codec") == "H264_NVENC":
            return {"JobStatus": "Failed", "Error": "No NVIDIA GPU", "CompletionPercentage": 0}
        return {"JobStatus": "Complete", "CompletionPercentage": 100}

    def GetRenderJobList(self):
        return [{"JobId": j, "TargetDir": d["settings"]["TargetDir"],
                 "OutputFilename": d["settings"]["CustomName"] + "." + d["fmt"]["format"]}
                for j, d in self.jobs.items()]


class PM:
    def __init__(self, project): self.p = project
    def GetCurrentProject(self): return self.p


class Resolve:
    EXPORT_AAF = 0
    EXPORT_AAF_NEW = 0

    def __init__(self, timeline, handles_start=86400):
        self.handles_start = handles_start
        self.project = Project(self, timeline)
        self.page = "edit"

    def GetProjectManager(self): return PM(self.project)

    def ImportRenderPreset(self, path):
        self.project.presets = self.project.presets + ["MIUGLITCH Live H264"]
        return True
    def GetCurrentPage(self): return self.page

    def OpenPage(self, p):
        self.page = p
        return True


def _render(job):
    """Paint each frame range by the top-most footage clip's colour.
    Handles-timeline jobs have silent audio (that timeline has no audio)."""
    s, tl, fmt = job["settings"], job["timeline"], job["fmt"]
    a, b = s["MarkIn"], s["MarkOut"] + 1
    out = os.path.join(s["TargetDir"], s["CustomName"] + "." + fmt["format"])
    os.makedirs(s["TargetDir"], exist_ok=True)
    dur = (b - a) / 25
    if not s.get("ExportVideo", True):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=f=440:d={dur}:sample_rate=48000",
                        "-ac", "2", "-c:a", "pcm_s24le", out], check=True)
        return
    w, h = s.get("FormatWidth", 1920), s.get("FormatHeight", 1080)
    items = [(ti, it) for ti, (_, its) in enumerate(tl.tracks["video"]) for it in its]
    cuts = sorted({a, b} | {x for _, it in items for x in (it.start, it.end) if a < x < b})
    segs = []
    for x, y in zip(cuts, cuts[1:]):
        cover = [(ti, it) for ti, it in items if it.start <= x and it.end >= y and it.mpi]
        if cover:
            it = max(cover, key=lambda p: p[0])[1]
            col = color_of(it.mpi.name, dim=tl.handles)
        else:
            col = (0, 0, 0)
        segs.append((y - x, col))
    args = ["ffmpeg", "-y", "-v", "error"]
    for n, col in segs:
        args += ["-f", "lavfi", "-i", f"color=c=0x{col[0]:02x}{col[1]:02x}{col[2]:02x}:s={w}x{h}:r=25:d={n / 25}"]
    k = len(segs)
    src = "anullsrc=r=48000:cl=stereo" if tl.handles else "sine=f=440:sample_rate=48000"
    args += ["-f", "lavfi", "-t", str(dur), "-i", src]
    fc = "".join(f"[{i}:v]" for i in range(k)) + f"concat=n={k}:v=1:a=0[v]"
    vcodec = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "1"] if fmt["codec"] == "H264" else ["-c:v", "mjpeg", "-q:v", "2"]
    acodec = ["-c:a", "aac"] if fmt["format"] == "mp4" else ["-c:a", "pcm_s16le"]
    args += ["-filter_complex", fc, "-map", "[v]", "-map", f"{k}:a", *vcodec, *acodec, "-ac", "2", "-t", str(dur), out]
    subprocess.run(args, check=True)
