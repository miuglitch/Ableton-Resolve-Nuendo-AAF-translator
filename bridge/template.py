"""The Live set the app builds on when you haven't picked your own template.

bridge/assets/TranslatorTemplate.als was saved by Ableton Live 12.4: one audio
track at -18 dB and a Utility at +18 dB on the Main track. The app copies the
audio track/clip and the Utility from it, so no Live template has to be chosen.
"""
from __future__ import annotations

import os
import sys

NAME = "TranslatorTemplate.als"


def builtin() -> str:
    base = getattr(sys, "_MEIPASS", None)
    here = os.path.join(base, "bridge") if base else os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "assets", NAME)


def resolve(user_choice: str | None) -> str:
    """The user's template if it exists, otherwise the built-in one."""
    if user_choice and os.path.isfile(user_choice):
        return user_choice
    return builtin()
