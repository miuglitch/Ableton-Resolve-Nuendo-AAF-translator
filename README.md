# MIUGLITCH AAF Translator

Round-trips timelines between **DaVinci Resolve** (hub), **Ableton Live 12/13**
(sound design) and **Nuendo** (mix/edit), via AAF and FCPXML.

| Direction | Status |
|---|---|
| Resolve / Nuendo → Live (AAF, FCPXML) | **v0.1: built, needs testing in Live** |
| Live → Resolve (FCPXML, AAF) | next |
| Live → Nuendo (AAF) | next |

## Getting the app

Ready-to-run builds live in `OUTPUT\`:
```
OUTPUT\ffmpeg\              shared by every version (delivered in pieces; the app
                              joins them into ffmpeg.exe/ffprobe.exe on first use)
OUTPUT\v0.2.0\MIUGLITCH AAF Translator.exe
```
Each new version gets its own folder (~17 MB). Old version folders can be deleted.
Every push also publishes the exe as a GitHub release (vX.Y.Z).

## One-time step: make the Live template

Live Sets contain hundreds of settings that change between Live versions, so
the app builds each set from a template saved by **your** Live:

1. In Live 12 (so the sets also open in Live 13), create a new empty Live Set.
2. Delete all tracks except **one audio track**.
3. (The template's sample rate doesn't matter — Live stores it as a preference.)
4. Drag any WAV file onto that audio track in **Arrangement View**.
5. Add one locator (Create → Add Locator).
6. Save it as `Bridge Template.als`, e.g. in the app folder.

Pick this file once in the app; it is remembered.

## Import: Resolve → Live

**In Resolve:** File → Export → Timeline… → **FCPXML** (recommended: carries
markers) or **AAF** (Fairlight export, linked media).

**In the app:**
1. Choose the timeline file; the track list appears with every track ticked.
2. Untick tracks you don't need (only ticked tracks are converted).
3. **Live 0:00 = timecode:** defaults to the timeline start (usually
   01:00:00:00). Set e.g. `00:59:58:00` for two seconds of pre-roll.
4. **Sample rate:** pick the rate your Live session runs at (e.g. 96000).
   The choice is remembered.
5. **Tempo:** any value, but keep it constant in the set; tempo changes would
   move audio against picture.
6. Optional **picture reference**. Its start timecode is read from the file
   (or typed in). Tick *Transcode picture for Live* to convert it first:
   codec (H.264 .mp4/.mov, ProRes Proxy/LT, DNxHR LB .mov/.mxf, MJPEG),
   frame rate, resolution, keyframe interval (1 = every frame, best for
   scrubbing), keep audio, burn-in timecode. It lands on a `PICTURE` track.
   The *Picture transcode* tab does the same for any video file.
7. **Create Live Set.**

Result, in the folder you chose:
```
<Name> Project/
  <Name>.als                 open in Live
  <Name>.bridge.json         remembers offset, tempo, frame rate, media origin (needed for export)
  <Name> - import report.txt what was imported + warnings
  Media/Imported/*.wav       24-bit WAVs at the rate you pick (44.1–96 kHz), with original timecode (BWF)
```

What comes across: clip positions and trims, clip gain, fades, volume
keyframes (as track volume automation), markers → locators, track names
(prefixed `ORIG - `), muted clips. Whole source files are converted, so
clips can be extended into their handles in Live.

Notes
- Stereo that arrives as two mono tracks (L/R) is combined into one stereo track.
- Sources with more than 2 channels: the channels the clip uses are kept; if a
  clip uses more than 2, the first two are kept and the report says so.
- Compound / multicam clips: decompose/flatten them in Resolve before export.

## Command line

```
python -m bridge.cli list  "Spot.fcpxml"
python -m bridge.cli import "Spot.fcpxml" --template "Bridge Template.als" --out D:\Projects --tempo 120 --offset 00:59:58:00 --tracks A1,A3
```

## Development

```
pip install -r requirements.txt pytest
python -m pytest tests        # needs ffmpeg on PATH
python app.py                 # run the app from source
```

Live-specific unit choices that must be confirmed in Live (fade length units,
volume/clip gain ranges) are collected at the top of `bridge/als/writer.py`.
