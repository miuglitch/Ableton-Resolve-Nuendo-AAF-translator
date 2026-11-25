"""Neutral timeline model shared by all readers and writers.

Times are Fraction seconds on the *source timeline clock*, i.e. a Resolve
timeline that starts at 01:00:00:00 has its first frame at t = 3600.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional


@dataclass
class VolumePoint:
    time: Fraction          # seconds, relative to clip start on the timeline
    db: float


@dataclass
class Clip:
    name: str
    start: Fraction                 # timeline position (seconds)
    duration: Fraction              # seconds
    source_path: str                # media file as referenced by the edit
    source_in: Fraction             # seconds from the start of the media file
    channels: list[int] = field(default_factory=list)  # 1-based source channels, [] = all
    gain_db: float = 0.0
    fade_in: Fraction = Fraction(0)
    fade_out: Fraction = Fraction(0)
    volume: list[VolumePoint] = field(default_factory=list)
    pan: Optional[float] = None     # -1..1, None = not set
    enabled: bool = True
    speed: Fraction = Fraction(1)   # media seconds per timeline second (0.5 = slow motion)
    color: Optional[int] = None     # Live colour for this clip (None = track colour)
    # filled by the media step: the WAV Live will actually play
    wav_path: Optional[str] = None
    wav_offset: Fraction = Fraction(0)  # seconds to add to source_in inside wav

    @property
    def end(self) -> Fraction:
        return self.start + self.duration


@dataclass
class Track:
    name: str
    kind: str = "audio"             # 'audio' or 'video'
    clips: list[Clip] = field(default_factory=list)
    channels: int = 2               # 1 = mono, 2 = stereo (as delivered to Live)
    selected: bool = True


@dataclass
class Marker:
    time: Fraction
    name: str
    note: str = ""
    duration: Fraction = Fraction(0)


@dataclass
class Timeline:
    name: str
    frame_rate: Fraction
    drop_frame: bool = False
    start: Fraction = Fraction(0)   # timeline start (seconds), e.g. 3600 for 01:00:00:00
    sample_rate: int = 48000
    tracks: list[Track] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)
    width: int = 1920               # timeline frame size
    height: int = 1080
    source_format: str = ""         # 'fcpxml' / 'aaf'
    source_file: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def audio_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.kind == "audio"]

    @property
    def end(self) -> Fraction:
        ends = [c.end for t in self.tracks for c in t.clips]
        return max(ends) if ends else self.start
