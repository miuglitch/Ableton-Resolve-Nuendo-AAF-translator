"""Transcode picture references to a format the target app accepts.

Live, Resolve and Nuendo each accept different video formats, so codec, frame
rate, resolution and keyframe interval are all selectable. A keyframe
interval of 1 (all-intra) scrubs smoothly in Live; ProRes, DNxHR and MJPEG are
always all-intra.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from .media import NO_WINDOW, probe, source_timecode, tool

# name -> (container ext, ffmpeg video args, supports keyframe interval)
CODECS = {
    "H.264 (.mp4)": (".mp4", ["-c:v", "libx264", "-preset", "fast", "-crf", "20",
                              "-pix_fmt", "yuv420p"], True),
    "H.264 (.mov)": (".mov", ["-c:v", "libx264", "-preset", "fast", "-crf", "20",
                              "-pix_fmt", "yuv420p"], True),
    "ProRes 422 Proxy (.mov)": (".mov", ["-c:v", "prores_ks", "-profile:v", "0",
                                         "-pix_fmt", "yuv422p10le"], False),
    "ProRes 422 LT (.mov)": (".mov", ["-c:v", "prores_ks", "-profile:v", "1",
                                      "-pix_fmt", "yuv422p10le"], False),
    "DNxHR LB (.mov)": (".mov", ["-c:v", "dnxhd", "-profile:v", "dnxhr_lb",
                                 "-pix_fmt", "yuv422p"], False),
    "DNxHR LB (.mxf)": (".mxf", ["-c:v", "dnxhd", "-profile:v", "dnxhr_lb",
                                 "-pix_fmt", "yuv422p"], False),
    "Motion JPEG (.mov)": (".mov", ["-c:v", "mjpeg", "-q:v", "3", "-pix_fmt", "yuvj422p"], False),
}

FRAME_RATES = ["Same as source", "23.976", "24", "25", "29.97", "30", "50", "59.94", "60"]
RATE_ARG = {"23.976": "24000/1001", "29.97": "30000/1001", "59.94": "60000/1001"}

# Height only: width follows the picture's own aspect ratio (no stretching, no bars).
RESOLUTIONS = ["Same as source", "2160p", "1080p", "720p", "540p", "360p"]


def _height(res: str) -> int | None:
    """'720p' -> 720; older saved values like '1280x720' -> 720."""
    if not res or res == "Same as source":
        return None
    res = res.lower().rstrip("p")
    if "x" in res:
        res = res.split("x")[1]
    try:
        return int(res)
    except ValueError:
        return None


@dataclass
class VideoSettings:
    codec: str = "H.264 (.mp4)"
    frame_rate: str = "Same as source"
    resolution: str = "720p"
    keyframe_interval: int = 1           # frames; 1 = every frame is a keyframe
    keep_audio: bool = False
    burn_timecode: bool = False


def output_name(src: str, out_dir: str, s: VideoSettings) -> str:
    ext = CODECS[s.codec][0]
    base = os.path.splitext(os.path.basename(src))[0]
    return os.path.join(out_dir, f"{base}_picture{ext}")


def _font():
    for f in (r"C:\Windows\Fonts\consola.ttf", r"C:\Windows\Fonts\arial.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"):
        if os.path.exists(f):
            return f.replace("\\", "/").replace(":", "\\:")
    return None


def build_command(src: str, dst: str, s: VideoSettings) -> list[str]:
    ext, vargs, gop_ok = CODECS[s.codec]
    info = probe(src)
    vstream = next((x for x in info["streams"] if x.get("codec_type") == "video"), None)
    if vstream is None:
        raise RuntimeError(f"No video stream in {src}")
    src_rate = vstream.get("r_frame_rate", "25/1")
    tc = source_timecode(info)

    # Treat pixels as square (Resolve renders square pixels; a stray pixel-aspect
    # flag otherwise makes players stretch the picture and add black bars),
    # then scale to the chosen height keeping the stored proportions. No padding.
    filters = ["setsar=1"]
    h = _height(s.resolution)
    if h:
        filters.append(f"scale=-2:{h}")
    rate = src_rate if s.frame_rate == "Same as source" else RATE_ARG.get(s.frame_rate, s.frame_rate)
    if s.frame_rate != "Same as source":
        filters.append(f"fps={rate}")
    if s.burn_timecode:
        font = _font()
        start = (tc or "00:00:00:00").replace(":", r"\:")
        dt = (f"drawtext=timecode='{start}':rate={rate}:fontsize=h/30:fontcolor=white:"
              f"box=1:boxcolor=black@0.6:boxborderw=8:x=(w-tw)/2:y=h/30")
        if font:
            dt += f":fontfile='{font}'"
        filters.append(dt)
    # dnxhr/prores need even sizes; H.264 needs even sizes too
    filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
    filters.append("setsar=1")

    cmd = [tool("ffmpeg"), "-y", "-v", "error", "-i", src, "-map", "0:v:0"]
    if s.keep_audio and any(x.get("codec_type") == "audio" for x in info["streams"]):
        cmd += ["-map", "0:a:0", "-c:a", "pcm_s16le" if ext in (".mov", ".mxf") else "aac",
                "-ar", "48000"]
    else:
        cmd += ["-an"]
    cmd += ["-vf", ",".join(filters)] + vargs
    if gop_ok:
        g = max(1, int(s.keyframe_interval))
        cmd += ["-g", str(g), "-keyint_min", str(g), "-sc_threshold", "0"]
        if g == 1:
            cmd += ["-bf", "0"]
    if ext in (".mp4", ".mov"):
        cmd += ["-movflags", "+faststart"]
    if tc:
        cmd += ["-timecode", tc]
    cmd += [dst]
    return cmd


def transcode_segment(src: str, dst: str, start: float, duration: float, s: VideoSettings) -> str:
    """Transcode part of a file (for video clips in Live). A silent audio track
    is added because Live loads video files as audio clips."""
    cmd = build_command(src, dst, s)
    if "-timecode" in cmd:                                    # source TC would be wrong here
        k = cmd.index("-timecode")
        del cmd[k:k + 2]
    cmd[-1:-1] = ["-map_metadata", "-1"] + (["-write_tmcd", "0"] if dst.endswith(".mov") else [])
    i = cmd.index("-i")
    cmd[i:i] = ["-ss", f"{max(start, 0):.6f}"]                 # fast seek before the input
    j = cmd.index("-i") + 2
    cmd[j:j] = ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{duration:.6f}"]
    # replace '-an' / audio mapping with the silent track
    if "-an" in cmd:
        k = cmd.index("-an")
        cmd[k:k + 1] = ["-map", "1:a:0", "-c:a", "pcm_s16le" if dst.endswith((".mov", ".mxf")) else "aac",
                        "-shortest"]
    r = subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(f"Video transcode failed: {r.stderr.strip()[-600:]}")
    return dst


def transcode(src: str, out_dir: str, s: VideoSettings, log=print) -> str:
    os.makedirs(out_dir, exist_ok=True)
    dst = output_name(src, out_dir, s)
    cmd = build_command(src, dst, s)
    log(f"Transcoding picture → {os.path.basename(dst)} ({s.codec}, {s.resolution}, "
        f"{s.frame_rate}, keyframe every {s.keyframe_interval} frame(s))")
    r = subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(f"Video transcode failed: {r.stderr.strip()[-600:]}")
    return dst
