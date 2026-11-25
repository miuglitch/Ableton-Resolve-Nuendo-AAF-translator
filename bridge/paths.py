"""Turn the many file-URL spellings found in AAF/FCPXML into real paths.

Seen in the wild:  file:///D:/a%20b.wav   file://localhost/D:/a.wav
                   file://D:\\a b.wav      file:/D:/a.wav   file://server/share/a.wav
                   D:\\a.wav               /Volumes/Media/a.wav
"""
from __future__ import annotations

import re
from urllib.parse import unquote

_DRIVE = re.compile(r"^[A-Za-z]:([\\/]|$)")


def url_to_path(url: str) -> str:
    if not url:
        return ""
    s = url.strip()
    if not s.lower().startswith("file:"):
        return unquote(s)
    s = unquote(s[5:])
    if s.lower().startswith("//localhost/"):
        s = s[len("//localhost"):]
    rest = s.lstrip("/\\")
    if _DRIVE.match(rest):                  # Windows drive letter, any slash count
        return rest
    if s.startswith("///") or not s.startswith("//"):
        return "/" + rest                   # local absolute path (macOS/Linux)
    return "//" + rest                      # //server/share
