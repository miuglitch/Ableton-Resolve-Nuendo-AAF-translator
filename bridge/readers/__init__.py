import os


def read_timeline(path: str):
    """Pick the reader from the file type."""
    ext = os.path.splitext(path.rstrip("/\\"))[1].lower()
    if ext in (".fcpxml", ".fcpxmld", ".xml"):
        from .fcpxml import read
        return read(path)
    if ext == ".aaf":
        from .aaf import read
        return read(path)
    raise ValueError(f"Unsupported file type: {ext} (use .aaf, .fcpxml or .fcpxmld)")
