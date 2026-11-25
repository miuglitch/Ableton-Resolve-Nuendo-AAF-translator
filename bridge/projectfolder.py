"""Make a new folder a real Live project folder (Windows).

Live treats a folder as a project when it contains 'Ableton Project Info'.
Without it, 'Save As' in Live creates a second project nested inside. Live
also gives the folder its icon through a desktop.ini pointing to
'Ableton Project Info\\AProject.ico'. The icon belongs to Ableton, so the app
copies it from a project Live made on this PC (the one your template is in).
"""
from __future__ import annotations

import configparser
import glob
import os
import shutil
import sys

INFO = "Ableton Project Info"
ICON = "AProject.ico"
HIDDEN, SYSTEM, READONLY = 0x2, 0x4, 0x1

DESKTOP_INI = ("[.ShellClassInfo]\r\nConfirmFileOp=0\r\nNoSharing=0\r\n"
               "IconFile=Ableton Project Info\\AProject.ico\r\nIconIndex=0\r\n")


def _set_attrs(path: str, attrs: int) -> None:
    if sys.platform != "win32":
        return
    import ctypes
    k = ctypes.windll.kernel32
    cur = k.GetFileAttributesW(path)
    if cur not in (-1, 0xFFFFFFFF):
        k.SetFileAttributesW(path, cur | attrs)


def _find_icon(start: str, levels: int = 3) -> str | None:
    """AProject.ico in a Live project at/above `start` (e.g. the template's)."""
    d = os.path.abspath(start)
    for _ in range(levels + 1):
        for cand in glob.glob(os.path.join(glob.escape(os.path.join(d, INFO)), "*.ico")):
            return cand
        ini = os.path.join(d, "desktop.ini")
        if os.path.isfile(ini):
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                with open(ini, encoding="utf-8-sig", errors="ignore") as fh:
                    cp.read_string(fh.read())
                sec = cp[".ShellClassInfo"]
                res = (sec.get("IconFile") or sec.get("IconResource") or "").rsplit(",", 1)[0]
                p = os.path.join(d, res.replace("\\", os.sep))
                if res and os.path.isfile(p):
                    return p
            except (KeyError, configparser.Error, OSError):
                pass
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def make_live_project(new_project: str, reference: str | None, log=print) -> bool:
    """Create 'Ableton Project Info' and, when an icon is found, the folder icon.
    Returns True if the icon was set."""
    info = os.path.join(new_project, INFO)
    os.makedirs(info, exist_ok=True)
    icon = _find_icon(reference) if reference else None
    if not icon:
        return False
    dst = os.path.join(info, ICON)
    if not os.path.exists(dst):
        shutil.copy2(icon, dst)
    ini = os.path.join(new_project, "desktop.ini")
    if not os.path.exists(ini) and not os.path.exists(os.path.join(new_project, "Desktop.ini")):
        with open(ini, "w", encoding="utf-8", newline="") as fh:
            fh.write(DESKTOP_INI)
        _set_attrs(ini, HIDDEN | SYSTEM)
    _set_attrs(new_project, READONLY)       # Explorer reads desktop.ini only on such folders
    return True


apply_icon = make_live_project              # older name
