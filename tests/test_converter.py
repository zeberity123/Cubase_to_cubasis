import tempfile
import unittest
from pathlib import Path
import wave
import xml.etree.ElementTree as ET
import zipfile

from cubase_to_cubasis import Archive, ConversionError, convert


class ConverterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        media = self.folder / 'Media'
        media.mkdir()
        self.wav = media / '음성.wav'
        with wave.open(str(self.wav), 'wb') as w:
            w.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
            w.writeframes(b'\x01\x00' * 48000)
        self.xml = self.folder / 'test.xml'
        self.output = self.folder / 'test.dawproject'
        self.root = ET.fromstring('''<tracklist2>
          <list name="track"><obj class="MAudioTrackEvent" ID="track">
            <obj class="MListNode" name="Node" ID="node">
              <string name="Name" value="음성"/>
              <member name="Domain"><int name="Type" value="0"/></member>
              <list name="Events">
                <obj class="MAudioEvent" ID="a">
                  <float name="Start" value="660"/><float name="Length" value="4800"/>
                  <float name="Offset" value="9600"/><obj name="AudioClip" ID="clip"/>
                </obj>
                <obj class="MAudioEvent" ID="b">
                  <float name="Start" value="1056"/><float name="Length" value="4800"/>
                  <float name="Offset" value="24000"/><obj name="AudioClip" ID="clip"/>
                </obj>
              </list>
            </obj>
          </obj></list>
          <obj class="MTempoTrackEvent" ID="tempo">
            <float name="RehearsalTempo" value="165"/><int name="RehearsalMode" value="1"/>
            <list name="TempoEvent"><obj class="MTempoEvent"><float name="BPM" value="120"/>
              <float name="PPQ" value="0"/></obj></list>
          </obj>
          <obj class="MTimeSignatureEvent"><float name="Start" value="0"/>
            <int name="Numerator" value="4"/><int name="Denominator" value="4"/></obj>
          <obj class="PAudioClip" ID="clip">
            <member name="Domain"><int name="Type" value="10"/>
              <float name="Period" value="0.000020833333333333333"/></member>
            <obj name="Cluster" ID="cluster"/>
          </obj>
          <obj class="AudioCluster" ID="cluster">
            <list name="Substreams"><obj name="stream" ID="file"/></list>
            <list name="Segments"><item><obj name="Stream" ID="file"/>
              <int name="Offset" value="0"/><int name="Start" value="0"/>
              <int name="Length" value="48000"/></item></list>
          </obj>
          <obj class="AudioFile" ID="file"><int name="FrameCount" value="48000"/>
            <float name="Rate" value="48000"/><int name="Channels" value="1"/>
            <obj name="archivePath" ID="path"/></obj>
          <obj class="FNPath" ID="path"><string name="Name" value="음성.wav"/>
            <string name="Path" value="Z:\\old-machine\\Media\\"/></obj>
        </tracklist2>''')
        self.save()

    def save(self):
        ET.ElementTree(self.root).write(self.xml, encoding='utf-8')

    def test_gap_offsets_shared_source_and_unicode_round_trip(self):
        report = convert(self.xml, self.output, 165)
        self.assertEqual(len(report['media']), 1)
        self.assertEqual(report['tracks'][0]['name'], '음성')
        self.assertAlmostEqual(report['tracks'][0]['gaps'][1][0], .6)
        self.assertAlmostEqual(report['tracks'][0]['gaps'][1][1], .8)
        with zipfile.ZipFile(self.output) as z:
            project = ET.fromstring(z.read('project.xml'))
            arrangement = project.find('Arrangement/Lanes')
            self.assertEqual(arrangement.get('timeUnit'), 'beats')
            track_lanes = arrangement.find("Lanes[@track='track1']")
            self.assertIsNotNone(track_lanes)
            self.assertEqual(len(arrangement.findall('Clips')), 0)
            clips = track_lanes.findall('Clips/Clip')
            self.assertEqual(len(clips), 2)
            self.assertAlmostEqual(float(clips[0].get('time')) * 60 / 165, .5)
            self.assertAlmostEqual(float(clips[0].get('duration')) * 60 / 165, .1)
            self.assertEqual(clips[0].get('contentTimeUnit'), 'seconds')
            self.assertAlmostEqual(float(clips[0].get('playStart')), .2)
            self.assertAlmostEqual(float(clips[0].get('playStop')), .3)
            self.assertAlmostEqual(float(clips[1].get('time')) * 60 / 165, .8)
            self.assertAlmostEqual(float(clips[1].get('playStart')), .5)
            entry = clips[0].find('Audio/File').get('path')
            self.assertEqual(z.read(entry), self.wav.read_bytes())
            self.assertEqual(clips[1].find('Audio/File').get('path'), entry)
            self.assertTrue(entry.isascii())
            self.assertLess(z.getinfo(entry).extract_version, 45)
            self.assertTrue(all(c.find('Audio').get('id') for c in clips))
            # Optional developer validation; converter itself needs no lxml.
            try:
                from lxml import etree
            except ImportError:
                return
            reference = Path(__file__).resolve().parents[1] / 'reference'
            for document, schema in [('project.xml', 'Project.xsd'), ('metadata.xml', 'MetaData.xsd')]:
                etree.XMLSchema(etree.parse(str(reference / schema))).assertValid(
                    etree.fromstring(z.read(document)))

    def test_inactive_120_bpm_does_not_override_fixed_165(self):
        self.assertAlmostEqual(Archive(self.xml, 165).tracks()[0].clips[0].start_seconds, .5)
        with self.assertRaisesRegex(ConversionError, 'Active XML tempo'):
            Archive(self.xml, 120)

    def test_unknown_event_flags_are_rejected(self):
        event = self.root.find(".//obj[@ID='a']")
        ET.SubElement(event, 'int', name='Flags', value='2')
        self.save()
        with self.assertRaisesRegex(ConversionError, 'unsupported edits/flags'):
            convert(self.xml, self.output, 165)
        self.assertFalse(self.output.exists())

    def test_missing_media_rejected_before_writing(self):
        self.wav.unlink()
        with self.assertRaisesRegex(ConversionError, 'Missing or ambiguous'):
            convert(self.xml, self.output, 165)
        self.assertFalse(self.output.exists())

    def test_out_of_bounds_source_trim_rejected(self):
        self.root.find(".//obj[@ID='a']/float[@name='Offset']").set('value', '47999')
        self.save()
        with self.assertRaisesRegex(ConversionError, 'beyond the source'):
            convert(self.xml, self.output, 165)

    def test_existing_output_is_not_overwritten(self):
        self.output.write_bytes(b'keep me')
        with self.assertRaisesRegex(ConversionError, 'already exists'):
            convert(self.xml, self.output, 165)
        self.assertEqual(self.output.read_bytes(), b'keep me')

    def test_sample_arrangement(self):
        sample = Path(__file__).resolve().parents[1] / 'sample' / 'sample.xml'
        if not sample.exists():
            self.skipTest('Private Cubase sample is not present')
        tracks = Archive(sample, 165).tracks()
        self.assertEqual([len(t.clips) for t in tracks], [15, 1])
        first, second = tracks[0].clips[:2]
        self.assertAlmostEqual(first.start_seconds + first.duration_seconds, 17.72727317887345)
        self.assertAlmostEqual(second.start_seconds, 18.454545454545453)
        self.assertAlmostEqual(second.offset_seconds, 18.062297214949036)


if __name__ == '__main__':
    unittest.main()
