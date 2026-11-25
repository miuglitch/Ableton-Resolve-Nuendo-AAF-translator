"""Give new project folders the Ableton project icon (Windows).

Live marks a project folder with a desktop.ini that points to an icon inside
the folder. The icon belongs to Ableton, so instead of shipping it the app
copies the setup from the project folder your Live template lives in (Live
created that one), and falls back to any .ico in its 'Ableton Project Info'.
"""
from __future__ import annotations

import configparser
import glob
import os
import shutil
import sys

INFO = "Ableton Project Info"


def _set_attrs(path: str, attrs: int) -> None:
    if sys.platform != "win32":
        return
    import ctypes
    cur = ctypes.windll.kernel32.GetFileAttributesW(path)
    if cur != -1 and cur != 0xFFFFFFFF:
        ctypes.windll.kernel32.SetFileAttributesW(path, cur | attrs)


HIDDEN, SYSTEM, READONLY = 0x2, 0x4, 0x1


def _icon_from_ini(ini_path: str):
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        with open(ini_path, encoding="utf-8-sig", errors="ignore") as fh:
            cp.read_string(fh.read())
    except (OSError, configparser.Error):
        return None
    sec = cp[".ShellClassInfo"] if cp.has_section(".ShellClassInfo") else None
    if sec is None:
        return None
    res = sec.get("IconResource") or sec.get("IconFile")
    if not res:
        return None
    rel = res.rsplit(",", 1)[0].strip()
    index = res.rsplit(",", 1)[1].strip() if "," in res else sec.get("IconIndex", "0")
    return rel, index


def apply_icon(new_project: str, reference_project: str, log=print) -> bool:
    """Copy the project-folder icon from reference_project to new_project."""
    if not reference_project or not os.path.isdir(reference_project):
        return False
    rel, index = None, "0"
    ini = os.path.join(reference_project, "desktop.ini")
    if os.path.isfile(ini):
        found = _icon_from_ini(ini)
        if found:
            rel, index = found
    if rel is None:
        icos = glob.glob(os.path.join(glob.escape(os.path.join(reference_project, INFO)), "*.ico"))
        if not icos:
            return False
        rel = os.path.join(INFO, os.path.basename(icos[0]))
    rel_fs = rel.replace("\\", os.sep).replace("/", os.sep)
    rel = rel.replace("/", "\\")                 # desktop.ini uses Windows separators
    src = rel_fs if os.path.isabs(rel_fs) else os.path.join(reference_project, rel_fs)
    if not os.path.isfile(src):
        return False
    if not os.path.isabs(rel_fs):
        dst = os.path.join(new_project, rel_fs)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if not os.path.exists(dst):
            shutil.copy2(src, dst)
    new_ini = os.path.join(new_project, "desktop.ini")
    if os.path.exists(new_ini):
        return True
    with open(new_ini, "w", encoding="utf-8") as fh:
        fh.write(f"[.ShellClassInfo]\nIconResource={rel},{index}\n")
    _set_attrs(new_ini, HIDDEN | SYSTEM)
    _set_attrs(new_project, READONLY)     # Explorer only reads desktop.ini on such folders
    return True
