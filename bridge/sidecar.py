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
        key = os.path.normcase(os.path.abspath(wav_path))
        for m in self.media:
            if os.path.normcase(os.path.abspath(m.wav)) == key:
                return m
        return None

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2)

    @classmethod
    def load(cls, path: str) -> "Sidecar":
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        media = [MediaLink(**m) for m in d.pop("media", [])]
        return cls(media=media, **d)

    @staticmethod
    def path_for(als_path: str) -> str:
        base, _ = os.path.splitext(als_path)
        return base + ".bridge.json"


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
