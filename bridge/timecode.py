"""Exact time handling.

All times inside the tool are kept as fractions.Fraction seconds so that
fractional frame rates (23.976 = 24000/1001, 29.97 = 30000/1001) never drift.
Rounding only happens when a value is written to a file.
"""
from __future__ import annotations

import re
from fractions import Fraction

SAMPLE_RATE = 48000

# Nominal rate -> exact rate
KNOWN_RATES = {
    "23.976": Fraction(24000, 1001),
    "23.98": Fraction(24000, 1001),
    "24": Fraction(24),
    "25": Fraction(25),
    "29.97": Fraction(30000, 1001),
    "30": Fraction(30),
    "47.952": Fraction(48000, 1001),
    "48": Fraction(48),
    "50": Fraction(50),
    "59.94": Fraction(60000, 1001),
    "60": Fraction(60),
}


def parse_rate(value) -> Fraction:
    """Accept '23.976', 23.976, Fraction, '24000/1001'."""
    if isinstance(value, Fraction):
        return value
    s = str(value).strip()
    if "/" in s:
        n, d = s.split("/")
        return Fraction(int(n), int(d))
    if s in KNOWN_RATES:
        return KNOWN_RATES[s]
    f = Fraction(s).limit_denominator(1001)
    # snap near-NTSC values
    for exact in KNOWN_RATES.values():
        if abs(float(f) - float(exact)) < 0.002:
            return exact
    return f


def nominal_fps(rate: Fraction) -> int:
    """Timecode counting base: 23.976 counts as 24, 29.97 as 30."""
    return int(round(float(rate)))


def parse_fcp_time(s: str | None) -> Fraction:
    """FCPXML rational time: '3600s', '1001/24000s', '0s'."""
    if not s:
        return Fraction(0)
    s = s.strip()
    if s.endswith("s"):
        s = s[:-1]
    if "/" in s:
        n, d = s.split("/")
        return Fraction(int(n), int(d))
    return Fraction(s)


def fcp_time(t: Fraction, rate: Fraction | None = None) -> str:
    """Write FCPXML rational time. If a frame rate is given and the value
    lands on a frame, write it in frame units (what Resolve expects)."""
    t = Fraction(t)
    if t == 0:
        return "0s"
    if rate:
        frames = t * rate
        if frames.denominator == 1:
            fd = 1 / rate
            return f"{frames.numerator * fd.numerator}/{fd.denominator}s"
    if t.denominator == 1:
        return f"{t.numerator}s"
    return f"{t.numerator}/{t.denominator}s"


def tc_to_frames(tc: str, rate: Fraction, drop: bool | None = None) -> int:
    m = re.match(r"^(\d+)[:;.](\d+)[:;.](\d+)([:;.])(\d+)$", tc.strip())
    if not m:
        raise ValueError(f"Not a timecode: {tc!r}")
    h, mi, se, sep, fr = m.groups()
    h, mi, se, fr = int(h), int(mi), int(se), int(fr)
    if drop is None:
        drop = sep == ";"
    fps = nominal_fps(rate)
    total = ((h * 60 + mi) * 60 + se) * fps + fr
    if drop:
        dropn = 2 if fps == 30 else 4
        total_minutes = h * 60 + mi
        total -= dropn * (total_minutes - total_minutes // 10)
    return total


def frames_to_tc(frames: int, rate: Fraction, drop: bool = False) -> str:
    fps = nominal_fps(rate)
    neg = frames < 0
    frames = abs(frames)
    if drop:
        dropn = 2 if fps == 30 else 4
        per10 = fps * 600 - dropn * 9
        per1 = fps * 60 - dropn
        d, m = divmod(frames, per10)
        if m > dropn:
            frames += dropn * 9 * d + dropn * ((m - dropn) // per1)
        else:
            frames += dropn * 9 * d
    fr = frames % fps
    s = (frames // fps) % 60
    mi = (frames // (fps * 60)) % 60
    h = frames // (fps * 3600)
    sep = ";" if drop else ":"
    return f"{'-' if neg else ''}{h:02d}:{mi:02d}:{s:02d}{sep}{fr:02d}"


def tc_to_seconds(tc: str, rate: Fraction, drop: bool | None = None) -> Fraction:
    """Timecode label -> real seconds (frame count / exact rate)."""
    return Fraction(tc_to_frames(tc, rate, drop)) / rate


def seconds_to_tc(t: Fraction, rate: Fraction, drop: bool = False) -> str:
    return frames_to_tc(int(round(t * rate)), rate, drop)


def seconds_to_samples(t: Fraction, sr: int = SAMPLE_RATE) -> int:
    return int(round(t * sr))


def db_to_lin(db: float) -> float:
    return 10 ** (db / 20.0)


def lin_to_db(lin: float) -> float:
    import math
    if lin <= 0:
        return -144.0
    return 20 * math.log10(lin)
