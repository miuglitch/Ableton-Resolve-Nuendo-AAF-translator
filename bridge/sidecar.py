"""The '.bridge.json' file saved next to each generated Live Set.

It remembers everything Live itself can't store: where the Resolve timeline
started, the offset and tempo used to place clips, the frame rate, and which
WAV files were converted from which Resolve media. The export step reads it
to put everything back exactly where it came from, and to tell original
audio (ORIG) apart from new sound design (SD).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from fractions import Fraction

VERSION = 1


def _f(x: Fraction) -> str:
    return f"{x.numerator}/{x.denominator}"


def _p(s: str) -> Fraction:
    n, d = s.split("/")
    return Fraction(int(n), int(d))


@dataclass
class MediaLink:
    wav: str                 # converted file, as referenced by Live
    source: str              # original media file Resolve used
    channels: list[int]      # source channels in the wav (1-based)
    wav_start_in_source: str = "0/1"   # Fraction seconds, normally 0


@dataclass
class Sidecar:
    timeline_name: str
    source_format: str
    source_file: str
    frame_rate: str                  # Fraction as 'n/d'
    drop_frame: bool
    timeline_start: str              # seconds, 'n/d'
    offset: str                      # seconds subtracted so Live starts at 0, 'n/d'
    tempo: float
    sample_rate: int = 48000             # Resolve timeline rate
    media_sample_rate: int = 48000       # rate the WAVs were converted to (Live session rate)
    media: list[MediaLink] = field(default_factory=list)
    # Resolve's edit as it was at import, so the export can rebuild a complete
    # timeline: video tracks, and audio tracks that weren't brought into Live.
    video: list[dict] = field(default_factory=list)        # {"track": "V1", "clips": [clip dicts]}
    extra_audio: list[dict] = field(default_factory=list)  # {"track": "A5", "clips": [...]}
    version: int = VERSION

    # convenience accessors
    @property
    def offset_s(self) -> Fraction:
        return _p(self.offset)

    @property
    def rate(self) -> Fraction:
        return _p(self.frame_rate)

    @property
    def start_s(self) -> Fraction:
        return _p(self.timeline_start)

    def is_original(self, wav_path: str) -> bool:
        return self.link_for(wav_path) is not None

    def link_for(self, wav_path: str):
        """Find the import record for a WAV Live plays. Falls back to the file
        name, so it still works after the project folder was moved."""
        norm = lambda p: os.path.normcase(os.path.normpath(p.replace("\\", "/")))  # noqa: E731
        key = norm(os.path.abspath(wav_path))
        for m in self.media:
            if norm(os.path.abspath(m.wav)) == key:
                return m
        base = os.path.basename(wav_path.replace("\\", "/")).lower()
        hits = [m for m in self.media if os.path.basename(m.wav.replace("\\", "/")).lower() == base]
        return hits[0] if len(hits) == 1 else None

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2)

    @classmethod
    def load(cls, path: str) -> "Sidecar":
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        media = [MediaLink(**m) for m in d.pop("media", [])]
        known = set(cls.__dataclass_fields__)
        return cls(media=media, **{k: v for k, v in d.items() if k in known})

    @staticmethod
    def path_for(als_path: str) -> str:
        base, _ = os.path.splitext(als_path)
        return base + ".bridge.json"


def clip_to_dict(c) -> dict:
    return {"name": c.name, "start": _f(c.start), "duration": _f(c.duration),
            "source": c.source_path, "source_in": _f(c.source_in), "channels": list(c.channels),
            "gain_db": c.gain_db, "fade_in": _f(c.fade_in), "fade_out": _f(c.fade_out),
            "enabled": c.enabled, "speed": _f(Fraction(c.speed or 1))}


def clip_from_dict(d):
    from .model import Clip
    return Clip(name=d.get("name", ""), start=_p(d["start"]), duration=_p(d["duration"]),
                source_path=d["source"], source_in=_p(d["source_in"]),
                channels=list(d.get("channels", [])), gain_db=d.get("gain_db", 0.0),
                fade_in=_p(d.get("fade_in", "0/1")), fade_out=_p(d.get("fade_out", "0/1")),
                enabled=d.get("enabled", True), speed=_p(d.get("speed", "1/1")))


def tracks_to_list(tracks) -> list[dict]:
    return [{"track": t.name, "clips": [clip_to_dict(c) for c in t.clips]} for t in tracks if t.clips]


def make(timeline, offset: Fraction, tempo: float) -> Sidecar:
    return Sidecar(
        timeline_name=timeline.name,
        source_format=timeline.source_format,
        source_file=timeline.source_file,
        frame_rate=_f(timeline.frame_rate),
        drop_frame=timeline.drop_frame,
        timeline_start=_f(timeline.start),
        offset=_f(offset),
        tempo=tempo,
        sample_rate=timeline.sample_rate,
    )
