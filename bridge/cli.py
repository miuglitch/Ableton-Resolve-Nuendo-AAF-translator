"""Command line:  python -m bridge.cli import <timeline> --template T.als --out DIR"""
from __future__ import annotations

import argparse
import sys

from . import importer
from .timecode import seconds_to_tc


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bridge")
    sub = ap.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("import", help="AAF/FCPXML -> Ableton Live Set")
    i.add_argument("timeline")
    i.add_argument("--template", required=True, help="Live template .als")
    i.add_argument("--out", required=True)
    i.add_argument("--name", default="")
    i.add_argument("--tempo", type=float, default=120.0)
    i.add_argument("--offset", default=None, help="timecode that becomes 0:00 in Live")
    i.add_argument("--tracks", default=None, help="comma-separated track names (default all)")
    i.add_argument("--picture", default=None)
    i.add_argument("--picture-tc", default=None)
    i.add_argument("--prefix", default=importer.ORIG_PREFIX)

    l = sub.add_parser("list", help="show tracks in a timeline")
    l.add_argument("timeline")

    a = ap.parse_args(argv)
    if a.cmd == "list":
        tl = importer.load(a.timeline)
        print(f"{tl.name}  {float(tl.frame_rate):.3f} fps  start {seconds_to_tc(tl.start, tl.frame_rate, tl.drop_frame)}")
        for t in tl.tracks:
            print(f"  [{t.kind}] {t.name}: {len(t.clips)} clips")
        print(f"  markers: {len(tl.markers)}")
        for w in tl.warnings:
            print("  ! " + w)
        return 0
    opt = importer.ImportOptions(
        template=a.template, out_dir=a.out, set_name=a.name, tempo=a.tempo,
        offset_tc=a.offset, selected=a.tracks.split(",") if a.tracks else None,
        picture=a.picture, picture_tc=a.picture_tc, prefix=a.prefix)
    importer.run(a.timeline, opt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
