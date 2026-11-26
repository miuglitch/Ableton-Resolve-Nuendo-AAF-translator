"""Resolve → Live with a simulated Resolve. Run: python -m pytest tests"""
import gzip
import json
import os
import subprocess
import sys
import tempfile
from fractions import Fraction

import pytest
from lxml import etree

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bridge import resolve_api, resolve_live          # noqa: E402
from bridge.video import VideoSettings                # noqa: E402
from tests import fixtures as fx                      # noqa: E402
from tests import fake_resolve as fr                  # noqa: E402

UTIL = ('<DeviceChain><Devices><StereoGain Id="0"><On><Manual Value="true"/></On>'
        '<Gain><LomId Value="0"/><Manual Value="0"/><MidiControllerRange><Min Value="-35"/>'
        '<Max Value="35"/></MidiControllerRange><AutomationTarget Id="21020"><LockEnvelope Value="0"/>'
        '</AutomationTarget><ModulationTarget Id="21021"/></Gain></StereoGain></Devices></DeviceChain>')


def template(d, with_utility=True):
    xml = fx.TEMPLATE_XML
    if with_utility:
        xml = xml.replace("<MainSequencer>", UTIL + "<MainSequencer>", 1)
        xml = xml.replace("<DeviceChain><Mixer><Tempo>", "<DeviceChain><DeviceChain><Devices/></DeviceChain><Mixer><Tempo>")
    p = os.path.join(d, f"tpl{'_u' if with_utility else ''}.als")
    with gzip.open(p, "wb") as fh:
        fh.write(xml.encode())
    return p


def clip_media(d, name):
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25:duration=20",
                    "-f", "lavfi", "-i", "sine=f=330:d=20", "-ac", "2", "-c:v", "mpeg4", "-c:a", "pcm_s16le",
                    "-ar", "48000", "-timecode", "10:00:00:00", "-f", "mov", p], check=True)
    return p


def make_timeline(d):
    """Like the test project: snow shot, cut to GX010030, a Text+ over the cut."""
    snow = clip_media(d, "snow.mov")
    gx = clip_media(d, "GX010030.MP4")
    m_snow, m_gx = fr.MPI(snow, tc="10:00:00:00"), fr.MPI(gx, tc="10:00:00:00")
    tl = fr.FakeTimeline("Edit 1")
    v1 = [fr.Item("snow", 90000, 90100, m_snow, src_in_s=5.0),
          fr.Item("GX010030.MP4", 90100, 90250, m_gx, src_in_s=3.0)]
    v2 = [fr.Item("Text+", 90060, 90160)]                 # title over the cut
    tl.tracks["video"] = [("Video 1", v1), ("Titles", v2)]
    tl.tracks["audio"] = [("Dialogue", [fr.Item("GX010030.MP4", 90100, 90250, m_gx, src_in_s=3.0,
                                                 chmap={"track_mapping": {"1": {"channel_idx": [1, 2], "mute": False}}})]),
                          ("Ambience", [fr.Item("snow", 90000, 90100, m_snow, src_in_s=5.0)])]
    tl.markers = {30: {"name": "Hit", "note": "", "color": "Blue", "duration": 1}}
    return tl


@pytest.fixture(scope="module")
def env():
    d = tempfile.mkdtemp(prefix="resolve-live-")
    return dict(d=d, tpl=template(d), tpl_plain=template(d, False))


def pixel(path, t):
    out = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.4f}", "-i", path, "-frames:v", "1",
                          "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    return tuple(out[:3])


def near(c, want, tol=40):
    return all(abs(a - b) <= tol for a, b in zip(c, want))


def duration(path):
    import json as _j
    r = subprocess.run(["ffprobe", "-v", "error", "-of", "json", "-show_format", path],
                       capture_output=True, text=True, check=True)
    return float(_j.loads(r.stdout)["format"]["duration"])


def test_read_and_plan(env):
    rv = fr.Resolve(make_timeline(os.path.join(env["d"], "m0")))
    rt = resolve_api.read(rv)
    assert rt.fps == 25 and rt.start == 90000 and rt.end == 90250
    assert rt.video_tracks == {1: "Video 1", 2: "Titles"}
    assert rt.audio_tracks == {1: "Dialogue", 2: "Ambience"}
    shots = resolve_live.plan_shots(rt)
    # the title spanning the cut never makes a cut of its own
    assert [(s.start, s.end, s.name) for s in shots] == [(90000, 90100, "snow"), (90100, 90250, "GX010030")]
    assert [o.name for o in shots[0].overlays] == ["Text+"] and [o.name for o in shots[1].overlays] == ["Text+"]
    assert shots[1].source_in == 3
    assert resolve_live.shot_tc(rt, shots[1]) == "10:00:03:00"


def test_title_alone_is_a_shot():
    tl = fr.FakeTimeline("t")
    m = fr.MPI("/x/a.mov")
    tl.tracks["video"] = [("V1", [fr.Item("a", 0, 50, m)]), ("V2", [fr.Item("Title", 40, 80)])]
    rt = resolve_api.read(fr.Resolve(tl))
    shots = resolve_live.plan_shots(rt)
    assert [(s.start, s.end, s.name) for s in shots] == [(0, 50, "a"), (50, 80, "Title")]


def _run(env, mode, sub, tpl=None, **kw):
    d = os.path.join(env["d"], sub)
    rv = fr.Resolve(make_timeline(os.path.join(d, "media")))
    rt = resolve_api.read(rv)
    kw.setdefault("video_settings", VideoSettings(codec="H.264 (.mp4)", resolution="360p"))
    opt = resolve_live.Options(template=tpl or env["tpl"], out_dir=d, render_mode=mode, video_head=1.0,
                               video_tail=1.0, audio_head=0.5, audio_tail=0.5,
                               log=lambda m: None, **kw)
    res = resolve_live.run(rv, rt, opt)
    return rv, rt, res


def load(path):
    with gzip.open(path) as fh:
        return etree.fromstring(fh.read())


def test_full_timeline_mode(env):
    rv, rt, res = _run(env, resolve_live.MODE_FULL, "full")
    root = load(res.als_path)
    names = [t.find("Name/EffectiveName").get("Value") for t in root.iter("AudioTrack")]
    assert names[:3] == ["Video 1", "Dialogue", "Ambience"], names      # Resolve names, no title track
    side = json.load(open(res.als_path[:-4] + ".bridge.json"))
    shots = side["shots"]
    assert [s["name"] for s in shots] == ["snow", "GX010030"]
    assert shots[1]["source_tc"] == "10:00:03:00" and shots[1]["source"].endswith("GX010030.MP4")
    assert shots[0]["overlays"] == ["Text+"]
    vid = [s["live_file"] for s in shots]
    # shot 1: no head (timeline start), body 4 s + 1 s tail from the next shot
    assert abs(duration(vid[0]) - 5.0) < 0.1
    assert near(pixel(vid[0], 0.5), fr.color_of("snow"))
    assert near(pixel(vid[0], 4.5), fr.color_of("GX010030.MP4"))    # tail = neighbour in the edit
    # shot 2: 1 s head from the snow shot, 6 s body, no tail (timeline end)
    assert abs(duration(vid[1]) - 7.0) < 0.1
    assert near(pixel(vid[1], 0.5), fr.color_of("snow"))
    assert near(pixel(vid[1], 1.5), fr.color_of("GX010030.MP4"))
    # Live clip skips the head: clip starts 1 s into the file
    vt = next(t for t in root.iter("AudioTrack") if t.find("Name/EffectiveName").get("Value") == "Video 1")
    clips = list(vt.iter("AudioClip"))
    assert [float(c.find("Loop/LoopStart").get("Value")) for c in clips] == [0.0, 1.0]
    # gain staging: tracks at -18 dB, +18 dB Utility on Main
    vol = float(vt.find("DeviceChain/Mixer/Volume/Manual").get("Value"))
    assert abs(vol - 10 ** (-18 / 20)) < 1e-4
    main = root.find("LiveSet/MainTrack/DeviceChain/DeviceChain/Devices/StereoGain/Gain/Manual")
    assert float(main.get("Value")) == 18.0
    locs = [l.find("Name").get("Value") for l in root.iter("Locator")]
    assert set(locs) == {"Hit", "snow", "GX010030"}


def test_real_handles_mode(env):
    rv, rt, res = _run(env, resolve_live.MODE_HANDLES, "handles")
    side = json.load(open(res.als_path[:-4] + ".bridge.json"))
    shots = side["shots"]
    assert [s["handles"] for s in shots] == ["source", "source"]
    v1, v2 = [s["live_file"] for s in shots]
    # 1 s head + body + 1 s tail, all of it the shot's own footage
    assert abs(duration(v1) - 6.0) < 0.1 and abs(duration(v2) - 8.0) < 0.1
    assert near(pixel(v1, 0.5), fr.color_of("snow", dim=True))          # head: handles render
    assert near(pixel(v1, 2.0), fr.color_of("snow"))                    # body: real timeline
    assert near(pixel(v1, 5.5), fr.color_of("snow", dim=True))          # tail: own footage, not the next shot
    assert near(pixel(v2, 0.5), fr.color_of("GX010030.MP4", dim=True))
    # the temporary timeline is gone and Resolve is back where it was
    assert len(rv.project.timelines) == 1 and rv.project.current is rv.project.timelines[0]
    assert rv.page == "edit" and rv.project.jobs == {}


def test_audio_handles_and_names(env):
    rv, rt, res = _run(env, resolve_live.MODE_FULL, "audio", video=False, normalize=True)
    root = load(res.als_path)
    tracks = {t.find("Name/EffectiveName").get("Value"): t for t in root.iter("AudioTrack")}
    assert "Dialogue" in tracks and "Ambience" in tracks
    c = next(tracks["Dialogue"].iter("AudioClip"))
    # wav holds 0.5 s before the clip: clip plays from 0.5 s into it
    assert abs(float(c.find("Loop/LoopStart").get("Value")) - 0.5) < 1e-6
    # Utility on each audio track
    assert tracks["Dialogue"].find("DeviceChain/DeviceChain/Devices/StereoGain") is not None
    side = json.load(open(res.als_path[:-4] + ".bridge.json"))
    link = next(m for m in side["media"] if "Dialogue" in os.path.basename(m["wav"]))
    assert link["wav_start_in_source"] == "5/2"        # 3 s in, minus 0.5 s handle
    assert float(c.find("SampleVolume").get("Value")) > 1.0      # normalised up


def test_no_utility_in_template(env):
    rv, rt, res = _run(env, resolve_live.MODE_FULL, "plain", tpl=env["tpl_plain"], video=False)
    assert any("Utility" in w for w in res.warnings)
    root = load(res.als_path)
    t = next(root.iter("AudioTrack"))
    assert float(t.find("DeviceChain/Mixer/Volume/Manual").get("Value")) == 1.0


def test_utility_automation(env):
    from bridge.als.writer import AlsBuilder
    from bridge.model import Clip, VolumePoint
    b = AlsBuilder(env["tpl"])
    c = Clip(name="x", start=Fraction(1), duration=Fraction(2), source_path="x.wav", source_in=Fraction(0))
    c.wav_path = os.path.join(env["d"], "x.wav")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=d=3", c.wav_path], check=True)
    c.volume = [VolumePoint(Fraction(0), -6.0), VolumePoint(Fraction(1), 3.0)]
    b.add_track("A", [c], Fraction(0), volume_db=-18.0, utility=True)
    t = b.tracks_el.find("AudioTrack")
    tgt = t.find("DeviceChain/DeviceChain/Devices/StereoGain/Gain/AutomationTarget").get("Id")
    env_ = t.find("AutomationEnvelopes/Envelopes/AutomationEnvelope")
    assert env_.find("EnvelopeTarget/PointeeId").get("Value") == tgt
    vals = [float(e.get("Value")) for e in env_.iter("FloatEvent")]
    assert -6.0 in vals and 3.0 in vals and vals[0] == 0.0


def test_builtin_template(env):
    """The Live 12 set shipped inside the app: master already has +18 dB."""
    from bridge import template
    tpl = template.builtin()
    assert os.path.isfile(tpl)
    rv, rt, res = _run(env, resolve_live.MODE_FULL, "builtin", tpl=tpl)
    root = load(res.als_path)
    main = root.find("LiveSet/MainTrack/DeviceChain/DeviceChain/Devices")
    assert len(main.findall("StereoGain")) == 1                     # not doubled
    tracks = {t.find("Name/EffectiveName").get("Value"): t for t in root.iter("AudioTrack")}
    assert set(tracks) == {"Video 1", "Dialogue", "Ambience"}       # template's own track is gone
    u = tracks["Dialogue"].find("DeviceChain/DeviceChain/Devices/StereoGain")
    assert abs(float(u.find("Gain/Manual").get("Value")) - 1.0) < 1e-6   # linear 0 dB in Live 12
    ids = [e.get("Id") for e in root.iter() if e.get("Id") and (e.tag.endswith("Target"))]
    assert len(ids) == len(set(ids))                                # pointee ids stay unique
    assert not res.warnings or all("AAF" in w for w in res.warnings)


def test_resolve_renders_final_clips(env):
    """No intermediate: one Resolve job per shot, straight into the Live format."""
    seen = []
    orig = fr._render

    def spy(job):
        seen.append((job["fmt"]["format"], job["fmt"]["codec"], job["settings"]["MarkIn"],
                     job["settings"]["MarkOut"], job["settings"].get("FormatHeight")))
        orig(job)
    fr._render = spy
    try:
        rv, rt, res = _run(env, resolve_live.MODE_FULL, "direct", audio=False, mix_track=True,
                           video_settings=VideoSettings(codec="ProRes 422 Proxy (.mov)", resolution="540p"))
    finally:
        fr._render = orig
    video_jobs = [j for j in seen if j[0] == "mov"]
    assert video_jobs == [("mov", "ProRes422Proxy", 90000, 90124, 540), ("mov", "ProRes422Proxy", 90075, 90249, 540)]
    assert any(j[0] == "wav" for j in seen)                         # stereo mix track
    side = json.load(open(res.als_path[:-4] + ".bridge.json"))
    assert all(s["live_file"].endswith(".mov") and "_parts" not in s["live_file"] for s in side["shots"])
    root = load(res.als_path)
    assert "STEREO MIX" in [t.find("Name/EffectiveName").get("Value") for t in root.iter("AudioTrack")]


def test_render_preset(env):
    rv, rt, res = _run(env, resolve_live.MODE_FULL, "preset", audio=False, render_preset="Live H264")
    side = json.load(open(res.als_path[:-4] + ".bridge.json"))
    assert all(s["live_file"].endswith(".mp4") for s in side["shots"])
    with pytest.raises(resolve_api.ResolveError):
        _run(env, resolve_live.MODE_FULL, "preset2", audio=False, render_preset="Nope")


def test_codec_not_available():
    class P:
        def GetRenderFormats(self): return {"MP4": "mp4"}
        def GetRenderCodecs(self, f): return {"H.265": "H265"}
    with pytest.raises(resolve_api.ResolveError):
        resolve_live.resolve_format(P(), "H.264 (.mp4)")
