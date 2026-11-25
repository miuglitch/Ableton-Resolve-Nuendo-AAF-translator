# MIUGLITCH AAF Translator

Round-trips timelines between **DaVinci Resolve** (hub), **Ableton Live 12/13**
(sound design) and **Nuendo** (mix/edit), via AAF and FCPXML.

| Direction | Status |
|---|---|
| Resolve / Nuendo → Live (AAF, FCPXML) | **v0.1: built, needs testing in Live** |
| Live → Resolve (FCPXML, AAF) | next |
| Live → Nuendo (AAF) | next |

## Getting the app

Each push builds a Windows app automatically:
GitHub → **Actions** → latest "Build Windows app" run → download
**MIUGLITCH-AAF-Translator-windows**. Unzip it and run
`MIUGLITCH AAF Translator.exe`. ffmpeg is included.

## One-time step: make the Live template

Live Sets contain hundreds of settings that change between Live versions, so
the app builds each set from a template saved by **your** Live:

1. In Live 12 (so the sets also open in Live 13), create a new empty Live Set.
2. Delete all tracks except **one audio track**.
3. Set **Preferences → Audio → Sample Rate to 48000**.
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
4. **Tempo:** any value, but keep it constant in the set; tempo changes would
   move audio against picture.
5. Optional **picture reference** (an H.264 .mov/.mp4 export from Resolve) and
   its start timecode. It lands on a `PICTURE` track.
6. **Create Live Set.**

Result, in the folder you chose:
```
<Name> Project/
  <Name>.als                 open in Live
  <Name>.bridge.json         remembers offset, tempo, frame rate, media origin (needed for export)
  <Name> - import report.txt what was imported + warnings
  Media/Imported/*.wav       48 kHz/24-bit WAVs with the original timecode (BWF)
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
