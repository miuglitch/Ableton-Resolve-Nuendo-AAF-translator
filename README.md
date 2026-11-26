# MIUGLITCH AAF Translator

Round-trips timelines between **DaVinci Resolve** (hub), **Ableton Live 12/13**
(sound design) and **Nuendo** (mix/edit), via AAF and FCPXML.

| Direction | Status |
|---|---|
| Resolve / Nuendo → Live (AAF, FCPXML) | **v0.1: built, needs testing in Live** |
| Live → Resolve (FCPXML) | v0.5: built |
| Live → Resolve/Nuendo (AAF) | next |
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

## Live template

The app builds each set from a Live Set saved by your own Live, so the sets
match your Live version exactly:

1. In Live 12, create a new empty Live Set and delete all tracks except one audio track.
2. Recommended: drag any WAV onto that track (Arrangement or Session view) and add one locator.
3. Save it as `Bridge Template.als`.

If the template has no clip, a built-in clip definition is used instead and
the report says so. Drop the template on the app once; it is remembered.

## One click: Resolve → Live (tab "Resolve → Live")

Needs **DaVinci Resolve Studio** open with the timeline, and in Resolve:
Preferences → System → General → *External scripting using* = **Local**.
(The free version of Resolve doesn't let outside apps connect.)

1. Click **Read open timeline** to check the connection (project, timeline, shots, tracks).
2. Pick the Live template and where to save (new project, or add to an existing one).
3. **Build Live Set from Resolve.** Resolve renders the picture on its Deliver page;
   your render settings, page and timeline are put back afterwards.

Picture: footage clips decide the cuts. A title over a cut is split there and goes
with the shot underneath. One Live clip per shot, on a track named like the Resolve
video track, with a locator at each shot and the timeline's stereo mix as its sound.
*Full timeline* renders once and cuts it up (handles = neighbouring edit).
*Per shot, real source handles* also renders a temporary timeline where each shot is
extended with its own footage (grade, Fusion comps and Inspector transforms copied;
deleted afterwards). Titles appear inside the cut, handles are clean footage.

Audio: one Live track per Resolve audio track (same name), clips cut with head/tail
handles or whole files, optional normalising (as clip gain), clip gain/fades/volume
automation read from an AAF Resolve exports in the background (automation goes to a
Utility's Gain). Gain staging: tracks at -18 dB and +18 dB Utility on the Main track.
**Put one Utility (at 0 dB) on the template's audio track** so the app can copy it.

The .bridge.json next to the set records every shot's source file and source
timecode, for a later re-conform.

## Import: Resolve → Live

**In Resolve:** File → Export → Timeline… → **FCPXML** (recommended: carries
markers) or **AAF** (Fairlight export, linked media).

**In the app** (drag and drop works for every file and folder):
1. Drop or choose the timeline file; the track list appears with every track ticked.
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

**Video** (the *Video* card)
- *Timeline's video clips on their own tracks*: each video clip from Resolve is cut
  out with 1 s handles, converted to the format chosen below (H.264 all-intra
  scrubs best) and placed on a `VIDEO V1`, `VIDEO V2`… track, so you can select and
  loop a shot. Files go to `Samples\Video`.
- *Locator at every video clip*: a locator at each cut, named after the clip.
- *Handles*: seconds kept before/after each clip; *Full extent* converts whole
  source files instead (one file per source and speed).
- *Framing*: Fit to timeline frame (bars), Crop to fill, or keep the clip's aspect.
  Fit and Crop always give the timeline's aspect ratio.
- Retimed clips (timeMap) and conformed frame rates (e.g. 50 fps shot played in a
  25 fps timeline) are converted at their speed and written at the timeline's
  frame rate. Speed ramps use their average speed. Retimed audio clips get their own
  WAV at that speed. Video clips alternate cyan/white so cuts are easy to see.
- For timelines with video, export **FCPXML** from Resolve: its AAF export re-renders
  everything to DNx MXF, which can change resolution and frame rate.
- Track colours: audio from Resolve = sky blue, video clips = violet,
  picture reference = grey.

**Destination**
- *New Live project*: by default a **neighbour folder**: the project is created next
  to the folder the AAF is in (source and working folders stay separate). Untick to
  choose any folder. The new project folder gets the Ableton project icon, copied
  from the folder your Live template lives in.
- *Add to existing project*: pick the Live project folder. New audio goes into its
  `Samples\Imported`, and a new set is saved as `<Name> - <note>.als` (or
  `- added <date time>` without a note). *Build on* an existing set to keep all its
  tracks and add the new ones, or *New tracks only*. Tempo, sample rate and offset
  are taken from the project's earlier import so everything lines up.

Result, in the folder you chose:
```
<Name> Project/
  <Name>.als                 open in Live
  <Name>.bridge.json         remembers offset, tempo, frame rate, media origin (needed for export)
  <Name> - import report.txt what was imported + warnings
  Samples/Imported/*.wav       24-bit WAVs at the rate you pick (44.1–96 kHz), with original timecode (BWF)
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

## Export: Live → Resolve

1. Drop the Live Set (.als) on the *Live → Resolve / Nuendo* tab. Your own
   increments work too (e.g. `Timeline 1 02.als`); the import info is found in
   the folders above if it isn't next to the set.
2. Check the track list and the *Before exporting* notes (plug-ins, MIDI and
   sends aren't carried; freeze and flatten those tracks first).
3. **Export to Resolve.** Then in Resolve: File → Import → Timeline… → the .fcpxml.

Resolve always imports a timeline as a **new timeline**: a complete one, to check
next to the original. What comes back: Resolve's video edit (V1, V2…; or the picture
reference for projects imported before v0.6), original audio tracks you didn't bring
into Live, clips on the original timecode, trims, clip gain, fades, track volume
automation as clip keyframes (measured from the fader position), locators as
markers. The static fader level is left out unless "Add track fader level to clip
gain" is ticked. Clips still using Resolve's audio point back to Resolve's own media;
new sound design is copied into `<Name> Media` next to the export and goes on
its own `SD - ` tracks.

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
