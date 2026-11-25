"""Build small test media, a Resolve-style FCPXML, an AAF and a stand-in
Live template, so the import can be tested without Resolve or Live.

The stand-in template only mimics the parts of a Live Set the writer touches.
Real verification needs a template saved by Live (see README)."""
from __future__ import annotations

import gzip
import os
import subprocess
from fractions import Fraction

FPS = Fraction(24000, 1001)


def ff(*args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def media(d):
    os.makedirs(d, exist_ok=True)
    cam = os.path.join(d, "A001_C003 cam.mov")
    # 20 s camera clip: video + 4 mono-ish channels, TC 10:00:00:00
    ff("-f", "lavfi", "-i", "testsrc=size=320x180:rate=24000/1001:duration=20",
       "-f", "lavfi", "-i", "sine=f=440:d=20", "-f", "lavfi", "-i", "sine=f=660:d=20",
       "-f", "lavfi", "-i", "sine=f=880:d=20", "-f", "lavfi", "-i", "sine=f=1100:d=20",
       "-filter_complex", "[1][2][3][4]amerge=inputs=4[a]", "-map", "0:v", "-map", "[a]",
       "-c:v", "mpeg4", "-c:a", "pcm_s16le", "-ar", "48000", "-timecode", "10:00:00:00", cam)
    sfx = os.path.join(d, "Whoosh.mxf")
    ff("-f", "lavfi", "-i", "sine=f=220:d=8", "-ar", "48000", "-c:a", "pcm_s24le",
       "-f", "mxf_opatom", "-timecode", "00:00:00:00", sfx)
    return cam, sfx


def url(p):
    import pathlib
    return pathlib.Path(os.path.abspath(p)).as_uri()


def fcpxml(d, cam, sfx):
    fd = "1001/24000s"
    def fr(n):  # n frames -> rational
        return f"{n * 1001}/24000s"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.10">
 <resources>
  <format id="r0" name="FFVideoFormat1080p2398" frameDuration="{fd}" width="1920" height="1080"/>
  <asset id="r1" name="A001_C003 cam" start="{fr(864000)}" duration="{fr(480)}" hasVideo="1" hasAudio="1" audioSources="1" audioChannels="4" audioRate="48000" format="r0">
   <media-rep kind="original-media" src="{url(cam)}"/>
  </asset>
  <asset id="r2" name="Whoosh" start="0s" duration="8s" hasAudio="1" audioSources="1" audioChannels="1" audioRate="48000">
   <media-rep kind="original-media" src="{url(sfx)}"/>
  </asset>
 </resources>
 <library><event name="Test"><project name="Spot 30s">
  <sequence format="r0" duration="{fr(720)}" tcStart="{fr(86400)}" tcFormat="NDF" audioLayout="stereo" audioRate="48k">
   <spine>
    <asset-clip ref="r1" offset="{fr(86400)}" name="A001_C003 cam" start="{fr(864024)}" duration="{fr(240)}" tcFormat="NDF">
     <asset-clip ref="r1" lane="-1" offset="{fr(864024)}" name="A001_C003 cam" start="{fr(864024)}" duration="{fr(240)}">
      <audio-channel-source srcCh="1, 2" role="dialogue"/>
      <adjust-volume amount="-3dB">
       <param name="amount"><fadeIn type="easeIn" duration="{fr(12)}"/><fadeOut type="easeOut" duration="{fr(24)}"/></param>
      </adjust-volume>
     </asset-clip>
     <asset-clip ref="r1" lane="-2" offset="{fr(864024)}" name="A001_C003 boom" start="{fr(864024)}" duration="{fr(240)}">
      <audio-channel-source srcCh="3"/>
     </asset-clip>
     <marker start="{fr(864048)}" duration="{fd}" value="Door slam" note="SFX here"/>
    </asset-clip>
    <gap name="Gap" offset="{fr(86640)}" start="{fr(86400)}" duration="{fr(480)}">
     <asset-clip ref="r2" lane="-3" offset="{fr(86424)}" name="Whoosh" start="2s" duration="{fr(96)}">
      <adjust-volume><param name="amount">
        <keyframe time="2s" value="-12dB"/><keyframe time="4s" value="0dB"/>
      </param></adjust-volume>
     </asset-clip>
    </gap>
   </spine>
  </sequence>
 </project></event></library>
</fcpxml>"""
    p = os.path.join(d, "Spot 30s.fcpxml")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(xml)
    return p


def aaf(d, cam, sfx):
    import aaf2
    from aaf2 import ama
    p = os.path.join(d, "Spot 30s.aaf")
    with aaf2.open(p, "w") as f:
        rate = "24000/1001"
        comp = f.create.CompositionMob("Spot 30s")
        f.content.mobs.append(comp)
        # timecode 01:00:00:00
        tc_slot = comp.create_timeline_slot(rate)
        tc_slot.segment = f.create.Timecode(24, False)
        tc_slot.segment.start = 86400
        tc_slot.segment.length = 720

        def master_for(path):
            import json
            meta = json.loads(subprocess.run(
                ["ffprobe", "-v", "error", "-of", "json", "-show_format", "-show_streams", path],
                capture_output=True, text=True, check=True).stdout)
            mobs = f.content.create_ama_link(path, meta)
            return mobs[0] if isinstance(mobs, (list, tuple)) else mobs

        cam_master = master_for(cam)
        sfx_master = master_for(sfx)
        snd_slots = [s for s in cam_master.slots if s.media_kind == "Sound"]
        names = ["A1", "A2"]
        for i in range(2):
            slot = comp.create_sound_slot(edit_rate=rate)
            slot.name = names[i]
            seq = f.create.Sequence("sound")
            filler = f.create.Filler("sound", 24)
            seq.components.append(filler)
            sc = f.create.SourceClip(media_kind="sound", length=240,
                                     mob_id=cam_master.mob_id, slot_id=snd_slots[i].slot_id)
            sc.start = 24
            seq.components.append(sc)
            slot.segment = seq
        # A3: SFX with clip gain -6 dB
        slot = comp.create_sound_slot(edit_rate=rate)
        slot.name = "SFX"
        seq = f.create.Sequence("sound")
        seq.components.append(f.create.Filler("sound", 264))
        sfx_slot = [s for s in sfx_master.slots if s.media_kind == "Sound"][0]
        sc = f.create.SourceClip(media_kind="sound", length=96,
                                 mob_id=sfx_master.mob_id, slot_id=sfx_slot.slot_id)
        sc.start = 48
        opdef = f.create.OperationDef(
            aaf2.auid.AUID("9d2ea893-0968-11d3-8a38-0050040ef7d2"), "Mono Audio Gain")
        opdef.media_kind = "sound"
        opdef["NumberInputs"].value = 1
        p_def = f.create.ParameterDef(aaf2.auid.AUID("e4962320-2267-11d3-8a4c-0050040ef7d2"),
                                      "Amplitude", "", "Rational")
        f.dictionary.register_def(p_def)
        opdef.parameters.append(p_def)
        f.dictionary.register_def(opdef)
        og = f.create.OperationGroup(opdef, 96, "sound")
        og.parameters.append(f.create.ConstantValue(p_def, Fraction(1, 2)))
        og.segments.append(sc)
        seq.components.append(og)
        slot.segment = seq
    return p


TEMPLATE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Ableton MajorVersion="5" MinorVersion="12.0_12049" SchemaChangeCount="1" Creator="Ableton Live 12.0" Revision="">
 <LiveSet>
  <NextPointeeId Value="22000"/>
  <Tracks>
   <AudioTrack Id="8">
    <LomId Value="0"/><Name><EffectiveName Value="1-Audio"/><UserName Value=""/><Annotation Value=""/></Name>
    <Color Value="4"/><TrackGroupId Value="-1"/>
    <AutomationEnvelopes><Envelopes/></AutomationEnvelopes>
    <DeviceChain>
     <Mixer><Volume><Manual Value="1"/><AutomationTarget Id="21001"><LockEnvelope Value="0"/></AutomationTarget><ModulationTarget Id="21002"/></Volume>
      <Pan><Manual Value="0"/><AutomationTarget Id="21003"/><ModulationTarget Id="21004"/></Pan></Mixer>
     <MainSequencer><Sample><ArrangerAutomation><Events>
      <AudioClip Id="0" Time="0">
       <LomId Value="0"/><CurrentStart Value="0"/><CurrentEnd Value="4"/>
       <Loop><LoopStart Value="0"/><LoopEnd Value="2"/><StartRelative Value="0"/><LoopOn Value="false"/><OutMarker Value="2"/><HiddenLoopStart Value="0"/><HiddenLoopEnd Value="2"/></Loop>
       <Name Value="x"/><Annotation Value=""/><Color Value="4"/><Disabled Value="false"/>
       <Envelopes><Envelopes/></Envelopes>
       <SampleRef><FileRef><RelativePathType Value="3"/><RelativePath Value="Samples/x.wav"/><Path Value="C:/x.wav"/><Type Value="1"/><LivePackName Value=""/><LivePackId Value=""/><OriginalFileSize Value="1"/><OriginalCrc Value="1"/></FileRef><LastModDate Value="0"/><SourceContext/><SampleUsageHint Value="0"/><DefaultDuration Value="96000"/><DefaultSampleRate Value="48000"/></SampleRef>
       <Fades><FadeInLength Value="0"/><FadeOutLength Value="0"/><ClipFadesAreInitialized Value="true"/><CrossfadeInState Value="0"/><IsDefaultFadeIn Value="true"/><IsDefaultFadeOut Value="true"/></Fades>
       <PitchCoarse Value="0"/><PitchFine Value="0"/><SampleVolume Value="1"/><WarpMode Value="0"/><IsWarped Value="true"/>
       <WarpMarkers><WarpMarker Id="0" SecTime="0" BeatTime="0"/><WarpMarker Id="1" SecTime="0.5" BeatTime="1"/></WarpMarkers>
      </AudioClip>
     </Events></ArrangerAutomation></Sample></MainSequencer>
    </DeviceChain>
   </AudioTrack>
   <ReturnTrack Id="2"><Name><EffectiveName Value="A-Reverb"/></Name></ReturnTrack>
  </Tracks>
  <MainTrack><AutomationEnvelopes><Envelopes>
    <AutomationEnvelope Id="0"><EnvelopeTarget><PointeeId Value="21010"/></EnvelopeTarget><Automation><Events><FloatEvent Id="1" Time="-63072000" Value="120"/></Events></Automation></AutomationEnvelope>
   </Envelopes></AutomationEnvelopes>
   <DeviceChain><Mixer><Tempo><Manual Value="120"/><AutomationTarget Id="21010"/></Tempo></Mixer></DeviceChain></MainTrack>
  <Locators><Locators/></Locators>
 </LiveSet>
</Ableton>"""


def template(d):
    p = os.path.join(d, "stand-in template.als")
    with gzip.open(p, "wb") as fh:
        fh.write(TEMPLATE_XML.encode())
    return p
