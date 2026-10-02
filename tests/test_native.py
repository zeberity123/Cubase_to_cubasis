import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import wave
import xml.etree.ElementTree as ET
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import soundfile as sf

from cpr_native import Reader, NativeError
from cpr_export import materialize, wav_info, filename_for_song, prepare_song, export_song, SongTrackError
from cubase_to_cubasis import Clip, Track, ConversionError, write_project


class NativeExportTests(unittest.TestCase):
    def test_excluded_error_track_is_not_decoded_and_is_reported_in_output(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            wav = folder / 'good.wav'
            sf.write(wav, np.zeros(8), 48000, subtype='PCM_16')
            source = dict(name=wav.name, directory=str(folder), frames=8, bits=16,
                          channels=1, sample_rate=48000, record_offset=10)
            clip = dict(offset=20, name='good', segments=[dict(source=source, start=0, offset=0, length=8)])
            event = dict(clip=clip, start=0, duration=8, offset=0, flags=0, gain=1,
                         domain=0, priority=0, record_offset=30, name='good')
            good = dict(name='Vocal', offset=100, path=['Song120', 'Vocal'])
            bad = dict(name='Click', offset=200, path=['Song120', 'Click'])
            song = dict(id='song', name='Song120', path=['Song120'], audio_tracks=[good, bad])

            def track_events(track):
                if track['offset'] == 200:
                    raise NativeError('unsupported event type MAudioPartEvent')
                return [event]

            reader = SimpleNamespace(path=folder / 'example.cpr', inventory=dict(
                folders=[song], source_sha256='fixture'), track_events=Mock(side_effect=track_events))
            with self.assertRaises(SongTrackError) as failure:
                prepare_song(reader, 'song', 120)
            self.assertEqual(failure.exception.track_offset, 200)
            self.assertEqual(failure.exception.track_path, bad['path'])
            reader.track_events.reset_mock()
            plan = prepare_song(reader, 'song', 120, excluded_track_offsets={200})
            reader.track_events.assert_called_once_with(good)
            self.assertEqual(plan.excluded_tracks, [bad])
            output = folder / 'result.dawproject'
            report = export_song(plan, output)
            self.assertEqual([t['name'] for t in report['tracks']], ['Vocal'])
            self.assertEqual(report['source']['excluded_tracks'], [dict(path=bad['path'], offset=200)])
            self.assertTrue(any('Tracks excluded by user' in w for w in report['warnings']))
            with zipfile.ZipFile(output) as z:
                self.assertIsNone(z.testzip())
                self.assertEqual(len(ET.fromstring(z.read('project.xml')).findall('.//Clip')), 1)
            reader.track_events.reset_mock()
            with self.assertRaisesRegex(NativeError, 'Check at least one'):
                prepare_song(reader, 'song', 120, excluded_track_offsets={100, 200})
            reader.track_events.assert_not_called()

    def test_reconstructed_segments_and_gap_are_sample_exact(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            a, b, out = [folder / n for n in ['a.wav', 'b.wav', 'out.wav']]
            sf.write(a, np.array([100, 200, 300, 400], dtype=np.int16), 48000, subtype='PCM_16')
            sf.write(b, np.array([-100, -200, -300], dtype=np.int16), 48000, subtype='PCM_16')
            resolved = {1: (a, wav_info(a)), 2: (b, wav_info(b))}
            source = dict(channels=1, sample_width=2, sample_rate=48000, subtype='PCM_16', segments=[
                dict(source={'record_offset': 1}, offset=1, length=2, start=0),
                dict(source={'record_offset': 2}, offset=0, length=3, start=3)])
            materialize(source, resolved, out, None)
            samples, rate = sf.read(out, dtype='int16')
            np.testing.assert_array_equal(samples, [200, 300, 0, -100, -200, -300])
            self.assertEqual(rate, 48000)

    def test_float_wav_header_and_original_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            source = folder / 'float.wav'
            sf.write(source, np.array([.2, 1.5, -.1]), 48000, subtype='FLOAT')
            info = wav_info(source)
            self.assertEqual(info['subtype'], 'FLOAT')
            target = folder / 'result.dawproject'
            track = Track('Float audio', [Clip('event', '1', source, 1, 3/48000, 0, 0, False)])
            write_project([track], {source: info}, target, 165)
            with zipfile.ZipFile(target) as z:
                xml = ET.fromstring(z.read('project.xml'))
                clip = xml.find('.//Clip')
                self.assertEqual(clip.get('enable'), 'false')
                self.assertEqual(z.read(clip.find('Audio/File').get('path')), source.read_bytes())

    def test_mixed_pcm_bit_depth_segments_retain_precision(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            a, b, out = [folder / n for n in ['a.wav', 'b.wav', 'out.wav']]
            sf.write(a, np.array([100, -100], dtype=np.int16), 48000, subtype='PCM_16')
            precise = np.array([256, -256], dtype=np.int32)
            sf.write(b, precise, 48000, subtype='PCM_24')
            source = dict(channels=1, sample_width=3, sample_rate=48000, subtype='PCM_24', segments=[
                dict(source={'record_offset': 1}, offset=0, length=2, start=0),
                dict(source={'record_offset': 2}, offset=0, length=2, start=2)])
            materialize(source, {1: (a, wav_info(a)), 2: (b, wav_info(b))}, out, None)
            samples, _ = sf.read(out, dtype='int32')
            np.testing.assert_array_equal(samples, [100 * 65536, -100 * 65536, 256, -256])

    def test_cancellation_removes_incomplete_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            source = folder / 'audio.wav'
            sf.write(source, np.zeros(2000000), 48000, subtype='PCM_16')
            target = folder / 'cancelled.dawproject'
            cancel = threading.Event()
            tracks = [Track('Audio', [Clip('event', '1', source, 0, 1, 0, 0)])]
            with self.assertRaisesRegex(ConversionError, 'cancelled'):
                write_project(tracks, {source: wav_info(source)}, target, 120, cancel=cancel,
                              progress=lambda *_: cancel.set())
            self.assertFalse(target.exists())
            self.assertEqual(list(folder.glob('*.partial')), [])

    def test_output_filenames_are_valid_windows_names(self):
        self.assertEqual(filename_for_song('song174/7'), 'song174_7')
        self.assertEqual(filename_for_song('CON'), '_CON')
        self.assertEqual(filename_for_song('테스트175'), '테스트175')


class PrivateNativeRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = os.environ.get('CUBASIS_PRIVATE_CPR')
        if not fixture:
            raise unittest.SkipTest('Set CUBASIS_PRIVATE_CPR to the original private regression fixture')
        source = Path(fixture)
        if not source.exists():
            raise unittest.SkipTest('Private Cubase project is not available')
        cls.reader = Reader(source)

    def test_native_voice_clip_matches_cubase_xml(self):
        sample = Path(__file__).resolve().parents[1] / 'sample' / 'sample.xml'
        if not sample.exists():
            self.skipTest('Private XML sample is not available')
        xml = ET.parse(sample).getroot()
        track_xml = next(t for t in xml.findall('./list/obj')
                         if t.find('./obj[@name="Node"]/string[@name="Name"]').get('value') == 'Vo-01')
        event_xml = track_xml.find('./obj[@name="Node"]/list[@name="Events"]/obj')
        folder = next(f for f in self.reader.inventory['folders'] if f['id'] == 'F032')
        track = next(t for t in folder['audio_tracks'] if t['name'] == 'Vo-01')
        events = self.reader.track_events(track)
        self.assertEqual(len(events), 1)
        for field in ['Start', 'Length', 'Offset']:
            key = 'duration' if field == 'Length' else field.lower()
            self.assertEqual(events[0][key], float(event_xml.find(f'./float[@name="{field}"]').get('value')))
        self.assertEqual(events[0]['clip']['segments'][0]['source']['name'], 'Vo-02.wav')

    def test_native_unchanged_splits_match_cubase_xml(self):
        sample = Path(__file__).resolve().parents[1] / 'sample' / 'sample.xml'
        if not sample.exists():
            self.skipTest('Private XML sample is not available')
        xml = ET.parse(sample).getroot()
        expected = xml.findall('./list/obj')[0].findall('./obj[@name="Node"]/list[@name="Events"]/obj')
        folder = next(f for f in self.reader.inventory['folders'] if f['id'] == 'F032')
        name = xml.findall('./list/obj')[0].find('./obj[@name="Node"]/string[@name="Name"]').get('value')
        track = next(t for t in folder['audio_tracks'] if t['name'] == name)
        actual = self.reader.track_events(track)
        self.assertEqual(len(actual), 14)  # Saved CPR predates the extra split in the XML sample.
        for event, element in zip(actual[1:], expected[2:]):
            for field, key in [('Start', 'start'), ('Length', 'duration'), ('Offset', 'offset')]:
                self.assertEqual(event[key], float(element.find(f'./float[@name="{field}"]').get('value')))

    def test_song_preflight_resolves_only_its_four_media_files(self):
        plan = prepare_song(self.reader, 'F003', 170)
        self.assertEqual(len(plan.tracks), 4)
        self.assertEqual(len(plan.resolved), 4)
        self.assertEqual(sum(len(events) for _, events in plan.tracks), 4)

    def test_linear_envelope_is_retained_for_rendering(self):
        folder = next(f for f in self.reader.inventory['folders'] if f['id'] == 'F032')
        events = [e for track in folder['audio_tracks'] for e in self.reader.track_events(track)]
        self.assertTrue(any(e.get('envelope') for e in events))


if __name__ == '__main__':
    unittest.main()
