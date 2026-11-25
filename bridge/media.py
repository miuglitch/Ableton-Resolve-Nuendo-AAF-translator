"""Convert Resolve media (MXF, MOV, MP4, …) to 48 kHz WAV files Live can play.

Only the tracks you tick are converted. Each source file + channel choice is
converted once, as a whole file (so you keep handles to extend clips later),
with the original timecode written into the WAV's BWF header.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from fractions import Fraction

from .model import Timeline
from .timecode import SAMPLE_RATE, tc_to_seconds

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _search_dirs():
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    here = os.path.dirname(os.path.abspath(__file__))
    dirs = []
    for r in (exe_dir, os.path.dirname(exe_dir), getattr(sys, "_MEIPASS", ""),
              here, os.path.dirname(here)):
        if r:
            dirs += [r, os.path.join(r, "ffmpeg"), os.path.join(r, "ffmpeg", "bin")]
    return dirs


def _join_parts(target: str) -> bool:
    """ffmpeg is delivered in <25 MB pieces (name.exe.001, .002, …).
    Join them into the real .exe the first time it's needed."""
    parts = []
    i = 1
    while os.path.exists(f"{target}.{i:03d}"):
        parts.append(f"{target}.{i:03d}")
        i += 1
    if not parts:
        return False
    tmp = target + ".joining"
    with open(tmp, "wb") as out:
        for p in parts:
            with open(p, "rb") as fh:
                shutil.copyfileobj(fh, out, 4 << 20)
    os.replace(tmp, target)
    for p in parts:
        try:
            os.remove(p)
        except OSError:
            pass
    return True


def tool(name: str) -> str:
    """ffmpeg/ffprobe: next to the .exe, in a sibling 'ffmpeg' folder, or on PATH."""
    exe = name + (".exe" if sys.platform == "win32" else "")
    for d in _search_dirs():
        p = os.path.join(d, exe)
        if os.path.isfile(p):
            return p
        if _join_parts(p):
            return p
    found = shutil.which(name)
    if not found:
        raise FileNotFoundError(f"{name} not found. Put the 'ffmpeg' folder next to the app.")
    return found


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW)


def probe(path: str) -> dict:
    r = _run([tool("ffprobe"), "-v", "error", "-print_format", "json",
              "-show_streams", "-show_format", path])
    if r.returncode != 0:
        raise RuntimeError(f"Can't read {path}: {r.stderr.strip()}")
    return json.loads(r.stdout)


def audio_layout(info: dict) -> list[int]:
    """Channel count of each audio stream, in order."""
    return [int(s.get("channels", 1)) for s in info.get("streams", [])
            if s.get("codec_type") == "audio"]


def source_timecode(info: dict) -> str | None:
    tags = dict(info.get("format", {}).get("tags", {}))
    for s in info.get("streams", []):
        tags.update({k: v for k, v in s.get("tags", {}).items() if k.lower() == "timecode"})
    for k, v in tags.items():
        if k.lower() == "timecode":
            return v
    return None


def _safe(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip() or "audio"


def _channel_filter(layout: list[int], chans: list[int]) -> str:
    gmap = []
    for si, n in enumerate(layout):
        for c in range(n):
            gmap.append((si, c))
    parts, labels = [], []
    for k, ch in enumerate(chans):
        si, c = gmap[ch - 1]
        parts.append(f"[0:a:{si}]pan=mono|c0=c{c}[m{k}]")
        labels.append(f"[m{k}]")
    if len(labels) == 1:
        parts.append(f"{labels[0]}anull[out]")
    else:
        parts.append(f"{''.join(labels)}amerge=inputs={len(labels)}[out]")
    return ";".join(parts)


class MediaConverter:
    def __init__(self, out_dir: str, frame_rate: Fraction, log=print, aaf_path: str | None = None):
        self.out_dir = out_dir
        self.rate = frame_rate
        self.log = log
        self.aaf_path = aaf_path
        self.cache: dict[tuple, str] = {}
        self.info_cache: dict[str, dict] = {}
        self.links: list[tuple[str, str, list[int]]] = []   # wav, source, channels
        self.warnings: list[str] = []
        os.makedirs(out_dir, exist_ok=True)

    def _info(self, path):
        if path not in self.info_cache:
            self.info_cache[path] = probe(path)
        return self.info_cache[path]

    def _embedded(self, path: str) -> str:
        from .readers.aaf import EMBEDDED, extract_embedded
        mob_id = path[len(EMBEDDED):]
        tmp = os.path.join(self.out_dir, "_embedded", hashlib.md5(mob_id.encode()).hexdigest()[:12] + ".wav")
        if not os.path.exists(tmp):
            os.makedirs(os.path.dirname(tmp), exist_ok=True)
            extract_embedded(self.aaf_path, mob_id, tmp)
        return tmp

    def convert(self, source: str, channels: list[int]) -> tuple[str, list[int]]:
        from .readers.aaf import EMBEDDED
        real = self._embedded(source) if source.startswith(EMBEDDED) else source
        if not os.path.exists(real):
            raise FileNotFoundError(f"Media offline: {real}")
        info = self._info(real)
        layout = audio_layout(info)
        total = sum(layout)
        if total == 0:
            raise RuntimeError(f"No audio in {real}")
        chans = [c for c in channels if 1 <= c <= total] or list(range(1, min(total, 2) + 1))
        if len(chans) > 2:
            self.warnings.append(
                f"{os.path.basename(real)}: {len(chans)} channels used; Live tracks are "
                f"mono/stereo, so channels {chans[0]}+{chans[1]} were kept.")
            chans = chans[:2]
        key = (os.path.normcase(os.path.abspath(real)), tuple(chans))
        if key in self.cache:
            return self.cache[key], chans

        base = _safe(os.path.splitext(os.path.basename(real))[0])
        suffix = "" if (len(chans) == total) else "_ch" + "-".join(map(str, chans))
        out = os.path.join(self.out_dir, f"{base}{suffix}.wav")
        n = 2
        while out in self.cache.values() or (os.path.exists(out) and not self._same_source(out, real)):
            out = os.path.join(self.out_dir, f"{base}{suffix}_{n}.wav")
            n += 1

        if os.path.exists(out):                 # converted on an earlier import
            self.log(f"Reusing {os.path.basename(out)}")
            self.cache[key] = out
            self.links.append((out, source, list(chans)))
            return out, chans

        cmd = [tool("ffmpeg"), "-y", "-v", "error", "-i", real,
               "-filter_complex", _channel_filter(layout, chans), "-map", "[out]",
               "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s24le", "-write_bext", "1",
               "-rf64", "auto"]
        tc = source_timecode(info)
        if tc:
            try:
                tr = int(round(tc_to_seconds(tc, self.rate) * SAMPLE_RATE))
                cmd += ["-metadata", f"time_reference={tr}"]
            except ValueError:
                pass
        cmd += ["-metadata", f"comment=bridge-source:{real}", out]
        self.log(f"Converting {os.path.basename(real)} → {os.path.basename(out)}")
        r = _run(cmd)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg failed on {real}: {r.stderr.strip()}")
        self.cache[key] = out
        self.links.append((out, source, list(chans)))
        return out, chans

    def _same_source(self, wav, real):
        """True if an existing WAV was made from this source (re-import)."""
        try:
            tags = probe(wav).get("format", {}).get("tags", {})
            comment = next((v for k, v in tags.items() if k.lower() == "comment"), "")
            return comment == f"bridge-source:{real}"
        except Exception:
            return False

    def run(self, tl: Timeline, log_progress=None) -> None:
        """Convert every clip on selected audio tracks; sets clip.wav_path."""
        clips = [(t, c) for t in tl.audio_tracks if t.selected for c in t.clips]
        for i, (t, c) in enumerate(clips):
            if log_progress:
                log_progress(i, len(clips))
            try:
                wav, chans = self.convert(c.source_path, c.channels)
                c.wav_path = wav
                c.channels = chans
            except Exception as e:      # keep going; report at the end
                self.warnings.append(f"{t.name} / {c.name}: {e}")
                c.wav_path = None
        for t in tl.audio_tracks:
            if t.selected and t.clips and all(len(c.channels) == 1 for c in t.clips if c.wav_path):
                t.channels = 1
