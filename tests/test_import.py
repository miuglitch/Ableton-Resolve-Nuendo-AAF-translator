"""End-to-end import tests. Run:  python -m pytest tests  (needs ffmpeg)."""
import gzip
import os
import sys
import tempfile
from fractions import Fraction

import pytest
from lxml import etree

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bridge import importer, timecode as tc          # noqa: E402
from bridge.sidecar import Sidecar                     # noqa: E402
from tests import fixtures as fx                       # noqa: E402

FR = Fraction(24000, 1001)


@pytest.fixture(scope="module")
def env():
    d = tempfile.mkdtemp(prefix="bridge-test-")
    cam, sfx = fx.media(os.path.join(d, "media"))
    return dict(d=d, cam=cam, sfx=sfx, fcp=fx.fcpxml(d, cam, sfx),
                aaf=fx.aaf(d, cam, sfx), tpl=fx.template(d))


def frames(n):
    return Fraction(n) / FR


def test_timecode_roundtrip():
    for rate, drop in [(FR, False), (Fraction(25), False), (Fraction(30000, 1001), True)]:
        for tcs in ["01:00:00:00", "00:59:58:00", "10:23:45:12"]:
            if drop:
                tcs = tcs.replace(":", ";", 2)[:-3] + ";" + tcs[-2:]
            s = tc.tc_to_seconds(tcs, rate, drop)
            assert tc.seconds_to_tc(s, rate, drop).replace(";", ":") == tcs.replace(";", ":")
    assert tc.tc_to_frames("00:10:00;00", Fraction(30000, 1001), True) == 17982


def test_fcpxml_read(env):
    tl = importer.load(env["fcp"])
    assert tl.frame_rate == FR
    assert tl.start == frames(86400)                    # 01:00:00:00
    names = [t.name for t in tl.audio_tracks]
    # the video clip's own audio is dropped: the same media sits on lane -1
    assert names == ["A1", "A2", "A3"], names
    a2 = tl.audio_tracks[0]
    c = a2.clips[0]
    assert c.channels == [1, 2]
    assert c.start == frames(86400)
    assert c.source_in == frames(24)
    assert c.gain_db == -3.0
    assert c.fade_in == frames(12) and c.fade_out == frames(24)
    w = tl.audio_tracks[2].clips[0]
    assert w.start == frames(86640 + 24)
    # A3 carries automation (from Resolve's keyframes), so its fader setting doesn't apply
    assert [round(p.db) for p in w.volume][:1] == [-12]
    assert [p.db for p in w.volume] == [-12.0, 0.0]
    assert w.volume[0].time == 0
    assert tl.markers[0].time == frames(86424) and tl.markers[0].name == "Door slam"


def _als(path):
    with gzip.open(path) as fh:
        return etree.fromstring(fh.read())


def test_fcpxml_to_live(env):
    out = os.path.join(env["d"], "out")
    res = importer.run(env["fcp"], importer.ImportOptions(
        template=env["tpl"], out_dir=out, tempo=120.0,
        selected=["A1", "A2", "A3"], picture=env["cam"]))
    root = _als(res.als_path)
    tracks = root.findall("LiveSet/Tracks/AudioTrack")
    names = [t.find("Name/EffectiveName").get("Value") for t in tracks]
    assert names == ["PICTURE", "ORIG - A1", "ORIG - A2", "ORIG - A3"]
    assert root.find("LiveSet/Tracks/ReturnTrack") is not None
    # unique pointee ids
    ids = [e.get("Id") for e in root.iter() if e.tag.endswith("Target") and e.get("Id")]
    assert len(ids) == len(set(ids))
    assert int(root.find("LiveSet/NextPointeeId").get("Value")) > max(map(int, ids))
    # clip on A2 starts at Live 0, plays source from 1 frame*24 in
    clip = tracks[1].find(".//Events/AudioClip")
    assert float(clip.get("Time")) == 0.0
    assert abs(float(clip.find("Loop/LoopStart").get("Value")) - float(frames(24))) < 1e-9
    assert clip.find("IsWarped").get("Value") == "false"
    assert abs(float(clip.find("SampleVolume").get("Value")) - tc.db_to_lin(-3)) < 1e-6
    wav = clip.find("SampleRef/FileRef/Path").get("Value")
    assert wav.endswith("_ch1-2_48k.wav") and os.path.exists(wav)
    # whoosh: 264 frames after 01:00:00:00 -> beats at 120 BPM
    wclip = tracks[3].find(".//Events/AudioClip")
    assert abs(float(wclip.get("Time")) - float(frames(264)) * 2) < 1e-9
    env_ = tracks[3].find("AutomationEnvelopes/Envelopes/AutomationEnvelope")
    vol_id = tracks[3].find(".//Mixer/Volume/AutomationTarget").get("Id")
    assert env_.find("EnvelopeTarget/PointeeId").get("Value") == vol_id
    # tempo + locator
    assert root.find("LiveSet/MainTrack/DeviceChain/Mixer/Tempo/Manual").get("Value") == "120.0"
    loc = root.find("LiveSet/Locators/Locators/Locator")
    assert loc.find("Name").get("Value") == "Door slam"
    assert abs(float(loc.find("Time").get("Value")) - float(frames(24)) * 2) < 1e-9
    side = Sidecar.load(Sidecar.path_for(res.als_path))
    assert side.offset_s == frames(86400) and side.is_original(wav)


def test_offset_preroll(env):
    out = os.path.join(env["d"], "out2")
    res = importer.run(env["fcp"], importer.ImportOptions(
        template=env["tpl"], out_dir=out, tempo=100.0, offset_tc="00:59:58:00",
        selected=["A1"], set_name="preroll"))
    clip = _als(res.als_path).find("LiveSet/Tracks/AudioTrack//Events/AudioClip")
    assert abs(float(clip.get("Time")) - float(frames(48)) * 100 / 60) < 1e-9


def test_aaf_read_and_import(env):
    tl = importer.load(env["aaf"])
    assert tl.start == frames(86400)
    names = [t.name for t in tl.audio_tracks]
    assert names == ["A1", "SFX"], names                # A1/A2 mono pair merged to stereo
    a1 = tl.audio_tracks[0].clips[0]
    assert a1.channels == [1, 2] and a1.start == frames(86424)
    assert a1.source_in == frames(24)
    sfx = tl.audio_tracks[1].clips[0]
    assert abs(sfx.gain_db - (-6.0206)) < 0.01
    assert sfx.source_in == frames(48)
    res = importer.run(env["aaf"], importer.ImportOptions(
        template=env["tpl"], out_dir=os.path.join(env["d"], "out3")))
    assert not [w for w in res.warnings if "offline" in w.lower()], res.warnings


def test_url_spellings():
    from bridge.paths import url_to_path as u
    assert u("file:///D:/a%20b.wav") == "D:/a b.wav"
    assert u("file://localhost/D:/a.wav") == "D:/a.wav"
    assert u("file://C:\\Users\\x\\a.mxf") == "C:\\Users\\x\\a.mxf"
    assert u("file:/D:/a.wav") == "D:/a.wav"
    assert u("file://server/share/a.wav") == "//server/share/a.wav"
    assert u("file:///Volumes/Media/a.wav") == "/Volumes/Media/a.wav"
    assert u("D:\\a.wav") == "D:\\a.wav"


def test_ffmpeg_parts_join(tmp_path):
    from bridge.media import _join_parts
    target = tmp_path / "ffmpeg.exe"
    (tmp_path / "ffmpeg.exe.001").write_bytes(b"abc")
    (tmp_path / "ffmpeg.exe.002").write_bytes(b"def")
    assert _join_parts(str(target))
    assert target.read_bytes() == b"abcdef"
    assert not (tmp_path / "ffmpeg.exe.001").exists()


@pytest.mark.parametrize("codec", ["H.264 (.mp4)", "ProRes 422 Proxy (.mov)", "DNxHR LB (.mxf)"])
def test_picture_transcode(env, codec):
    from bridge import video, media
    s = video.VideoSettings(codec=codec, frame_rate="25", resolution="360p",
                            keyframe_interval=1)
    out = video.transcode(env["cam"], os.path.join(env["d"], "pic"), s, log=lambda m: None)
    v = next(x for x in media.probe(out)["streams"] if x["codec_type"] == "video")
    assert (v["width"], v["height"], v["r_frame_rate"]) == (640, 360, "25/1")


def test_import_with_picture_transcode(env):
    from bridge import video
    res = importer.run(env["fcp"], importer.ImportOptions(
        template=env["tpl"], out_dir=os.path.join(env["d"], "out4"), selected=["A1"],
        picture=env["cam"], picture_video=video.VideoSettings(resolution="360p")))
    root = _als(res.als_path)
    pic = root.find("LiveSet/Tracks/AudioTrack//Events/AudioClip")
    assert pic.find("SampleRef/FileRef/Path").get("Value").endswith("_picture.mp4")
    # picture has TC 10:00:00:00, timeline starts 01:00:00:00 -> 9 h later in Live
    assert float(pic.get("Time")) > 9 * 3600 * 2 - 100


def test_import_96k(env):
    from bridge.media import probe
    res = importer.run(env["fcp"], importer.ImportOptions(
        template=env["tpl"], out_dir=os.path.join(env["d"], "out96"), selected=["A1"],
        sample_rate=96000))
    root = _als(res.als_path)
    clip = root.find("LiveSet/Tracks/AudioTrack//Events/AudioClip")
    wav = clip.find("SampleRef/FileRef/Path").get("Value")
    a = next(s for s in probe(wav)["streams"] if s["codec_type"] == "audio")
    assert a["sample_rate"] == "96000"
    assert clip.find("SampleRef/DefaultSampleRate").get("Value") == "96000"
    # position/in-point unchanged (seconds-based)
    assert abs(float(clip.find("Loop/LoopStart").get("Value")) - float(frames(24))) < 1e-9
    tags = probe(wav)["format"]["tags"]
    assert int(tags["time_reference"]) == 36036 * 96000          # 10:00:00:00 @23.976


def test_template_without_clip(env, tmp_path):
    import re
    from tests.fixtures import TEMPLATE_XML
    xml = re.sub(r"<AudioClip .*?</AudioClip>", "", TEMPLATE_XML, flags=re.S)
    tpl = tmp_path / "noclip.als"
    tpl.write_bytes(gzip.compress(xml.encode()))
    res = importer.run(env["fcp"], importer.ImportOptions(
        template=str(tpl), out_dir=str(tmp_path / "o"), selected=["A1"]))
    clip = _als(res.als_path).find("LiveSet/Tracks/AudioTrack//Events/AudioClip")
    assert clip is not None and clip.find("IsWarped").get("Value") == "false"
    assert any("built-in clip" in w for w in res.warnings)


def test_picture_keeps_aspect(tmp_path):
    """Source with a stray pixel-aspect flag: output keeps stored proportions, square pixels, no bars."""
    from bridge import video, media
    src = tmp_path / "scope.mov"
    fx.ff("-f", "lavfi", "-i", "testsrc=size=1440x804:rate=24:duration=1",
          "-vf", "setsar=4/3", "-c:v", "mpeg4", str(src))          # displays as 1920x804
    out = video.transcode(str(src), str(tmp_path / "o"),
                          video.VideoSettings(resolution="720p", framing=video.FRAMINGS[2]), log=lambda m: None)
    v = next(x for x in media.probe(out)["streams"] if x["codec_type"] == "video")
    assert v["height"] == 720 and abs(v["width"] / v["height"] - 1440 / 804) < 0.01
    # fit / crop to a 16:9 timeline frame: always the timeline's aspect
    for fr in video.FRAMINGS[:2]:
        o = video.transcode(str(src), str(tmp_path / fr[:3]), video.VideoSettings(
            resolution="720p", framing=fr, frame_w=1920, frame_h=1080), log=lambda m: None)
        v = next(x for x in media.probe(o)["streams"] if x["codec_type"] == "video")
        assert (v["width"], v["height"]) == (1280, 720)
    assert v.get("sample_aspect_ratio", "1:1") in ("1:1", "0:1", "N/A")


def test_add_to_existing_project(env, tmp_path):
    first = importer.run(env["fcp"], importer.ImportOptions(
        template=env["tpl"], out_dir=str(tmp_path), selected=["A1"], tempo=100.0,
        offset_tc="00:59:58:00", sample_rate=96000))
    proj = os.path.dirname(first.als_path)
    # second export from Resolve, added to the same project, building on the first set
    res = importer.run(env["fcp"], importer.ImportOptions(
        template=env["tpl"], existing_project=proj, base_set=first.als_path,
        selected=["A3"], note="SFX pickups"))
    assert os.path.basename(res.als_path) == "Spot 30s - SFX pickups.als"
    root = _als(res.als_path)
    names = [t.find("Name/EffectiveName").get("Value") for t in root.findall("LiveSet/Tracks/AudioTrack")]
    assert names == ["ORIG - A1", "ORIG - A3"]
    assert root.find("LiveSet/Tracks")[-1].tag == "ReturnTrack"
    # same offset/tempo as the first import → whoosh at 264+48 frames, 100 BPM
    w = root.findall("LiveSet/Tracks/AudioTrack")[1].find(".//Events/AudioClip")
    assert abs(float(w.get("Time")) - float(frames(312)) * 100 / 60) < 1e-6
    wav = w.find("SampleRef/FileRef/Path").get("Value")
    assert "/Samples/Imported/" in wav.replace("\\", "/") and wav.endswith("_96k.wav")
    ids = [e.get("Id") for e in root.iter() if e.tag.endswith("Target") and e.get("Id")]
    assert len(ids) == len(set(ids))
    assert len(root.findall("LiveSet/Locators/Locators/Locator")) == 1     # no duplicate
    side = Sidecar.load(Sidecar.path_for(res.als_path))
    assert len(side.media) == 2
    # new-tracks-only variant + auto name
    res2 = importer.run(env["fcp"], importer.ImportOptions(
        template=env["tpl"], existing_project=proj, selected=["A2"]))
    assert " - added " in os.path.basename(res2.als_path)


def test_project_icon_copied(env, tmp_path):
    import shutil
    ref = tmp_path / "Tpl Project"
    (ref / "Ableton Project Info").mkdir(parents=True)
    (ref / "Ableton Project Info" / "AProject.ico").write_bytes(b"ICO")
    (ref / "desktop.ini").write_text("[.ShellClassInfo]\nIconResource=Ableton Project Info\\AProject.ico,0\n")
    tpl = ref / "Tpl.als"
    shutil.copy(env["tpl"], tpl)
    res = importer.run(env["fcp"], importer.ImportOptions(
        template=str(tpl), out_dir=str(tmp_path / "out"), selected=["A1"]))
    proj = os.path.dirname(res.als_path)
    ini = open(os.path.join(proj, "desktop.ini")).read()
    assert "IconFile=Ableton Project Info\\AProject.ico" in ini and "IconIndex=0" in ini
    assert os.path.exists(os.path.join(proj, "Ableton Project Info\\AProject.ico")) or \
        os.path.exists(os.path.join(proj, "Ableton Project Info", "AProject.ico"))


def test_roundtrip_export(env, tmp_path):
    """Resolve FCPXML → Live → (add sound design + automation in 'Live') → FCPXML."""
    import copy, shutil
    from bridge import exporter
    from bridge.readers.fcpxml import read as read_fcp
    from bridge.als.writer import load_als, save_als
    res = importer.run(env["fcp"], importer.ImportOptions(
        template=env["tpl"], out_dir=str(tmp_path), selected=["A1", "A3"], tempo=120.0))
    # --- "work in Live": Save As into a nested project, add an SD clip + volume automation
    nested = os.path.join(os.path.dirname(res.als_path), "Spot 02 Project")
    os.makedirs(nested)
    sfx = tmp_path / "lib" / "Boom.wav"
    sfx.parent.mkdir()
    fx.ff("-f", "lavfi", "-i", "sine=f=60:d=3", "-ar", "48000", str(sfx))
    tree = load_als(res.als_path)
    root = tree.getroot()
    tracks = root.findall("LiveSet/Tracks/AudioTrack")
    a1 = tracks[0]
    events = a1.find(".//ArrangerAutomation/Events")
    clip = copy.deepcopy(events[0])
    clip.set("Time", "4")                                 # 2 s at 120 BPM
    clip.find("CurrentStart").set("Value", "4")
    clip.find("CurrentEnd").set("Value", "6")             # 1 s long
    clip.find("Loop/LoopStart").set("Value", "0.5")
    clip.find("SampleRef/FileRef/Path").set("Value", str(sfx))
    clip.find("SampleVolume").set("Value", "0.5")
    events.append(clip)
    vol_id = a1.find(".//Mixer/Volume/AutomationTarget").get("Id")
    envs = a1.find("AutomationEnvelopes/Envelopes")
    envs.append(etree.fromstring(
        f'<AutomationEnvelope Id="9"><EnvelopeTarget><PointeeId Value="{vol_id}"/></EnvelopeTarget>'
        '<Automation><Events><FloatEvent Id="0" Time="-63072000" Value="1"/>'
        '<FloatEvent Id="1" Time="4" Value="1"/><FloatEvent Id="2" Time="6" Value="0.5"/>'
        '</Events></Automation></AutomationEnvelope>'))
    tracks[1].find(".//Mixer/Volume/Manual").set("Value", "0.0794328")   # A3 fader at -22 dB
    als2 = os.path.join(nested, "Spot 02.als")
    save_als(tree, als2)

    pre = exporter.preflight(als2)
    assert pre.side is not None                           # found in the parent folder
    out = exporter.run(als2, exporter.ExportOptions(out_dir=str(tmp_path / "exp"), log=lambda m: None), pre)
    tl = read_fcp(out.path)
    assert tl.start == frames(86400)
    names = [t.name for t in tl.audio_tracks]
    # ORIG - A1, SD - A1, ORIG - A3, + Resolve's A2 that wasn't brought into Live
    assert names == ["A1", "A2", "A3", "A4"], names
    assert tl.audio_tracks[3].clips[0].channels == [3]
    # Resolve's video edit comes back on V1 (video only, its audio not doubled)
    v = [t for t in tl.tracks if t.kind == "video"]
    assert v and v[0].clips[0].start == frames(86400) and v[0].clips[0].duration == frames(240)
    assert v[0].clips[0].source_path.endswith("A001_C003 cam.mov")
    orig = tl.audio_tracks[0].clips[0]
    assert orig.start == frames(86400) and orig.source_in == frames(24)
    assert orig.source_path.endswith("A001_C003 cam.mov") and orig.channels == [1, 2]
    # the volume ride over this clip becomes keyframes, clip gain (-3 dB) included
    assert orig.volume[0].db == pytest.approx(-3.0, abs=0.01)
    assert orig.volume[-1].db == pytest.approx(-3.0 - 6.0206, abs=0.01)
    boom = tl.audio_tracks[1].clips[0]
    assert boom.start == frames(86400) + 2 and boom.duration == 1
    assert boom.source_in == Fraction(1, 2)
    assert "Spot 02 Media" in boom.source_path and os.path.exists(boom.source_path)
    assert [round(p.db, 2) for p in boom.volume] == [round(-6.0206, 2), round(-12.0412, 2)]
    w = tl.audio_tracks[2].clips[0]
    assert w.start == frames(86640 + 24)
    # A3 carries automation (from Resolve's keyframes), so its fader setting doesn't apply
    assert [round(p.db) for p in w.volume][:1] == [-12]
    assert tl.markers and tl.markers[0].name == "Door slam" and tl.markers[0].time == frames(86424)
    # export XML names the tracks
    raw = open(out.path, encoding="utf-8").read()
    assert 'lane="-2"' in raw


def test_moved_project_still_maps_orig(env, tmp_path):
    import shutil
    from bridge import exporter
    res = importer.run(env["fcp"], importer.ImportOptions(
        template=env["tpl"], out_dir=str(tmp_path / "a"), selected=["A3"]))
    moved = tmp_path / "moved"
    shutil.move(os.path.dirname(res.als_path), moved)
    als = moved / os.path.basename(res.als_path)
    # the set still has the old absolute paths; Live finds files via relative paths
    root = _als(str(als))
    for fr in root.iter("FileRef"):
        p = fr.find("Path").get("Value")
        fr.find("RelativePath").set("Value", "Samples/Imported/" + os.path.basename(p))
        fr.find("RelativePathType").set("Value", "1")
    from bridge.als.writer import save_als
    save_als(etree.ElementTree(root), str(als))
    out = exporter.run(str(als), exporter.ExportOptions(out_dir=str(tmp_path / "e"), log=lambda m: None))
    raw = open(out.path, encoding="utf-8").read()
    assert "Whoosh.mxf" in raw and "Media" not in os.listdir(tmp_path / "e")


def test_fader_not_folded_by_default(env, tmp_path):
    from bridge import exporter
    from bridge.readers.fcpxml import read as read_fcp
    from bridge.als.writer import save_als
    res = importer.run(env["fcp"], importer.ImportOptions(
        template=env["tpl"], out_dir=str(tmp_path), selected=["A1"]))
    root = _als(res.als_path)
    trk = [t for t in root.findall("LiveSet/Tracks/AudioTrack")
           if t.find("Name/EffectiveName").get("Value") == "ORIG - A1"][0]
    trk.find(".//Mixer/Volume/Manual").set("Value", "0.0794328")          # -22 dB
    save_als(etree.ElementTree(root), res.als_path)
    for fader, want in ((False, -3.0), (True, -25.0)):
        out = exporter.run(res.als_path, exporter.ExportOptions(
            out_dir=str(tmp_path / f"e{fader}"), include_fader=fader, log=lambda m: None))
        c = read_fcp(out.path).audio_tracks[0].clips[0]
        assert c.gain_db == pytest.approx(want, abs=0.01)


def test_video_clips_and_markers(env, tmp_path):
    from bridge import video, media
    res = importer.run(env["fcp"], importer.ImportOptions(
        template=env["tpl"], out_dir=str(tmp_path), selected=["A1"], video_markers=True,
        video_clips=video.VideoSettings(resolution="360p", codec="H.264 (.mov)")))
    root = _als(res.als_path)
    tracks = root.findall("LiveSet/Tracks/AudioTrack")
    names = [t.find("Name/EffectiveName").get("Value") for t in tracks]
    assert names[0] == "VIDEO V1" and names[1] == "ORIG - A1"
    assert tracks[0].find("Color").get("Value") == "24" and tracks[1].find("Color").get("Value") == "8"
    clip = tracks[0].find(".//Events/AudioClip")
    path = clip.find("SampleRef/FileRef/Path").get("Value")
    assert "Samples/Video/V1_001_A001_C003 cam" in path.replace("\\", "/") and path.endswith(".mov")
    assert float(clip.get("Time")) == 0.0
    assert abs(float(clip.find("Loop/LoopStart").get("Value")) - 1.0) < 1e-6      # 1 s handle
    info = media.probe(path)
    kinds = sorted(s["codec_type"] for s in info["streams"])
    assert kinds == ["audio", "video"], kinds
    assert 11.5 < float(info["format"]["duration"]) < 12.5                       # 10 s + 2 × 1 s
    locs = [(l.find("Name").get("Value"), float(l.find("Time").get("Value")))
            for l in root.findall("LiveSet/Locators/Locators/Locator")]
    assert ("A001_C003 cam", 0.0) in locs and any(n == "Door slam" for n, _ in locs)


def test_retimed_and_conformed_video(tmp_path):
    """50 fps clip conformed into a 25 fps timeline (half speed) and a 200 % retime."""
    from bridge import video, media
    src = tmp_path / "fifty.mov"
    fx.ff("-f", "lavfi", "-i", "testsrc=size=640x360:rate=50:duration=12", "-c:v", "mpeg4",
          "-timecode", "12:00:00:00", str(src))
    sfx = tmp_path / "s.wav"
    fx.ff("-f", "lavfi", "-i", "sine=d=12", "-ar", "48000", str(sfx))
    import pathlib
    u = pathlib.Path(src).as_uri()
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.9"><resources>
 <format id="r0" frameDuration="1/25s" width="1920" height="1080"/>
 <format id="r9" frameDuration="1/50s" width="640" height="360"/>
 <asset id="r1" name="fifty" start="43200s" duration="12s" hasVideo="1" format="r9"><media-rep kind="original-media" src="{u}"/></asset>
</resources><library><event><project name="Mix"><sequence format="r0" duration="10s" tcStart="3600s" tcFormat="NDF" audioRate="48k"><spine>
 <asset-clip ref="r1" offset="3600s" name="slow" start="43202s" duration="4s"><conform-rate srcFrameRate="50"/></asset-clip>
 <clip offset="3604s" name="fast" start="0s" duration="2s">
  <timeMap><timept time="0s" value="43206s" interp="linear"/><timept time="2s" value="43210s" interp="linear"/></timeMap>
  <video ref="r1" offset="0s" start="43206s" duration="2s"/>
 </clip>
</spine></sequence></project></event></library></fcpxml>"""
    p = tmp_path / "mix.fcpxml"
    p.write_text(xml)
    tl = importer.load(str(p))
    assert (tl.width, tl.height) == (1920, 1080)
    v1 = [t for t in tl.tracks if t.kind == "video"][0].clips
    assert v1[0].speed == Fraction(1, 2) and v1[0].source_in == 2
    assert v1[1].speed == 2 and v1[1].source_in == 6 and v1[1].start == 3604
    tpl = fx.template(str(tmp_path))
    res = importer.run(str(p), importer.ImportOptions(
        template=tpl, out_dir=str(tmp_path / "o"), selected=[], video_handles=0.5,
        video_clips=video.VideoSettings(resolution="360p")))
    root = _als(res.als_path)
    clips = root.find("LiveSet/Tracks/AudioTrack").findall(".//Events/AudioClip")
    assert [c.find("Color").get("Value") for c in clips] == ["21", "13"]
    for c, want in zip(clips, (5.0, 3.0)):                 # 4 s + 2×0.5 s, 2 s + 2×0.5 s
        path = c.find("SampleRef/FileRef/Path").get("Value")
        info = media.probe(path)
        vs = next(x for x in info["streams"] if x["codec_type"] == "video")
        assert vs["r_frame_rate"] == "25/1" and (vs["width"], vs["height"]) == (640, 360)
        assert abs(float(info["format"]["duration"]) - want) < 0.1, (path, info["format"]["duration"])
        assert abs(float(c.find("Loop/LoopStart").get("Value")) - 0.5) < 1e-6
    # full extent: one file for the whole source per speed
    res2 = importer.run(str(p), importer.ImportOptions(
        template=tpl, out_dir=str(tmp_path / "o2"), selected=[], video_full_extent=True,
        video_clips=video.VideoSettings(resolution="360p")))
    clips = _als(res2.als_path).find("LiveSet/Tracks/AudioTrack").findall(".//Events/AudioClip")
    p0 = clips[0].find("SampleRef/FileRef/Path").get("Value")
    assert p0.endswith("_50pct_full.mp4")
    assert abs(float(media.probe(p0)["format"]["duration"]) - 24) < 0.2   # 12 s at half speed
    assert abs(float(clips[0].find("Loop/LoopStart").get("Value")) - 4.0) < 1e-6


def test_retimed_audio(env, tmp_path):
    from bridge import media
    tl = importer.load(env["fcp"])
    c = tl.audio_tracks[0].clips[0]
    c.speed = Fraction(1, 2)                               # half speed: 10 s clip uses 5 s of media
    res = importer.run(env["fcp"], importer.ImportOptions(
        template=env["tpl"], out_dir=str(tmp_path), selected=["A1"]), tl=tl)
    clip = _als(res.als_path).find("LiveSet/Tracks/AudioTrack//Events/AudioClip")
    wav = clip.find("SampleRef/FileRef/Path").get("Value")
    assert "_retime001_50pct_" in wav
    assert abs(float(media.probe(wav)["format"]["duration"]) - float(c.duration)) < 0.05
    assert abs(float(clip.find("Loop/LoopStart").get("Value"))) < 1e-9
