from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import xml.etree.ElementTree as ET
import zipfile

import numpy as np
import soundfile as sf

from cpr_native import Reader, NativeError
from cpr_export import prepare_song, render_event_audio, wav_info, export_song
from cubase_to_cubasis import Clip, Track, write_project


CLASSES = {1: 'MLinearInterpolator', 2: 'MFadeIn', 3: 'MFadeOut', 4: 'MAudioPart',
           5: 'MAudioEvent', 6: 'MAudioPartEvent', 7: 'PAudioClip',
           8: 'MListNode', 9: 'MTempoTrackEvent', 10: 'MSignatureTrackEvent'}


def packet(cls, payload):
    key = next(k for k, v in CLASSES.items() if v == cls)
    return struct.pack('>II', 0x80000000 | key, len(payload)) + payload


def string(text):
    encoded = text.encode('utf-8') + b'\0'
    return struct.pack('>I', len(encoded)) + encoded


def curve(points, max_x=None):
    maximum = points[-1][0] if max_x is None else max_x
    return packet('MLinearInterpolator', struct.pack('>I', len(points))
                  + b''.join(struct.pack('>dd', *p) for p in points)
                  + struct.pack('>dddd', 0, maximum, 0, 1))


def fade(cls, points=None):
    return packet(cls, (curve(points) if points else bytes(4)) + struct.pack('>d', .5))


def event(start=0, length=8, offset=0, flags=0, envelope=None, fades=None):
    attrs = struct.pack('>I', 1) + b'vClV\x00\x22' + curve(envelope) if envelope else bytes(4)
    fade_data = b''.join(fades) if fades else bytes(8)
    payload = (struct.pack('>Hddd', flags, start, length, offset) + packet('PAudioClip', b'')
               + attrs + struct.pack('>I', 1) + fade_data + bytes(8) + string('Audio')
               + bytes(4) + struct.pack('>f', 1) + bytes(6))
    return packet('MAudioEvent', payload)


def node(cls, events):
    payload = (string('Audio') + bytes(4) + packet('MTempoTrackEvent', b'')
               + packet('MSignatureTrackEvent', b'') + struct.pack('>I', len(events))
               + b''.join(events))
    return packet(cls, payload)


def part(events, start=960, length=480, offset=0, flags=0):
    attrs = struct.pack('>I', 2) + b'enaL\x00\x01' + bytes(8) + b'treV\x00\x01' + bytes(8)
    return packet('MAudioPartEvent', struct.pack('>Hddd', flags, start, length, offset)
                  + node('MAudioPart', events) + attrs + struct.pack('>I', 1))


def reader_for(data):
    reader = Reader.__new__(Reader)
    reader.data, reader.classes = data, CLASSES
    reader.base, reader.inline_ends = 0, {}
    reader.audio_clip = Mock(return_value=dict(offset=1))
    return reader


class NativeEventProcessingTests(unittest.TestCase):
    def test_linear_curve_rounding_and_invalid_points(self):
        data = curve([(0, 0), (100, 1)], max_x=np.nextafter(100.0, 0))
        r = reader_for(data)
        self.assertEqual(r.linear_curve(r.obj(0))['points'], [(0, 0), (100, 1)])
        for points in ([(0, 0), (0, 1)], [(0, 0), (100, float('nan'))], [(0, -1), (100, 1)]):
            r = reader_for(curve(points))
            with self.assertRaises(NativeError):
                r.linear_curve(r.obj(0))

    def test_envelope_fades_and_empty_fades_are_decoded(self):
        data = event(envelope=[(0, .5), (8, 1)],
                     fades=[fade('MFadeIn', [(0, 0), (2, 1)]), fade('MFadeOut', [(0, 1), (2, 0)])])
        r = reader_for(data)
        e = r.event(r.obj(0), 0, 'Audio')[0]
        self.assertEqual(e['envelope']['points'], [(0, .5), (8, 1)])
        self.assertEqual(e['fade_in']['points'][-1], (2, 1))
        self.assertEqual(e['fade_out']['points'][-1], (2, 0))
        r = reader_for(event(fades=[fade('MFadeIn'), fade('MFadeOut')]))
        e = r.event(r.obj(0), 0, 'Audio')[0]
        self.assertIsNone(e['fade_in'])
        self.assertIsNone(e['fade_out'])

    def test_unknown_processing_is_still_rejected(self):
        data = event(envelope=[(0, .5), (8, 1)]).replace(b'vClV', b'????')
        r = reader_for(data)
        with self.assertRaisesRegex(NativeError, 'Unsupported event processing attribute'):
            r.event(r.obj(0), 0, 'Audio')

    def test_nested_audio_parts_preserve_offsets_windows_and_disabled_flags(self):
        data = node('MListNode', [part([part([event(start=12)], start=120, length=60, offset=4)],
                                    start=960, length=480, offset=8, flags=2)])
        r = reader_for(data)
        _, events = r.node_events(r.obj(0), 'Audio')
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['start'], 12 + 120 - 4 + 960 - 8)
        self.assertEqual(events[0]['flags'], 2)
        self.assertEqual([w['start'] for w in events[0]['part_windows']], [120 + 960 - 8, 960])
        self.assertEqual([w['duration'] for w in events[0]['part_windows']], [60, 480])

    def test_negative_arrangement_start_is_preserved_but_invalid_source_timing_fails(self):
        r = reader_for(event(start=-120))
        self.assertEqual(r.event(r.obj(0), 0, 'Audio')[0]['start'], -120)
        for kwargs in (dict(offset=-1), dict(length=0), dict(start=float('nan'))):
            r = reader_for(event(**kwargs))
            with self.assertRaisesRegex(NativeError, 'invalid clip timing'):
                r.event(r.obj(0), 0, 'Audio')

    def test_rendered_samples_include_envelope_both_fades_and_clip_gain(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            wav, rendered = folder / 'source.wav', folder / 'rendered.wav'
            sf.write(wav, np.ones((8, 2)), 48000, subtype='FLOAT')
            r = reader_for(event(envelope=[(0, .5), (8, 1)],
                fades=[fade('MFadeIn', [(0, 0), (2, 1)]), fade('MFadeOut', [(0, 1), (2, 0)])]))
            e = r.event(r.obj(0), 0, 'Audio')[0]
            e['gain'] = .5
            count = render_event_audio(wav, rendered, e, wav_info(wav))
            self.assertEqual(count, 8)
            samples, _ = sf.read(rendered, always_2d=True)
            expected = [0, .140625, .3125, .34375, .375, .40625, .4375, .234375]
            np.testing.assert_array_equal(samples[:, 0], expected)
            np.testing.assert_array_equal(samples[:, 1], expected)
            # Trimming an event must keep envelope coordinates in the source.
            e.update(offset=2, duration=4, processing_offset=0, processing_duration=8)
            render_event_audio(wav, rendered, e, wav_info(wav))
            samples, _ = sf.read(rendered, always_2d=True)
            np.testing.assert_array_equal(samples[:, 0], expected[2:6])

    def test_audio_part_bounds_trim_source_and_keep_processing_origin(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            wav = folder / 'source.wav'
            sf.write(wav, np.ones(96), 48000, subtype='FLOAT')
            source = dict(name=wav.name, directory=str(folder), frames=96, bits=32,
                          channels=1, sample_rate=48000, record_offset=10)
            clip = dict(offset=20, segments=[dict(source=source, start=0, offset=0, length=96)])
            e = dict(clip=clip, start=.99975, duration=48, offset=0, domain=1, flags=0, gain=1,
                     part_windows=[dict(start=1, duration=.0005, domain=1)])
            track = dict(name='Audio', path=['Song', 'Audio'], offset=1)
            song = dict(id='song', name='Song', path=['Song'], audio_tracks=[track])
            reader = SimpleNamespace(path=folder/'source.cpr', inventory=dict(folders=[song]),
                                     track_events=lambda _: [e])
            plan = prepare_song(reader, 'song', 120)
            adjusted = plan.tracks[0][1][0]
            self.assertAlmostEqual(adjusted['start'], 1)
            self.assertAlmostEqual(adjusted['offset'], 12)
            self.assertAlmostEqual(adjusted['duration'], 24)
            self.assertEqual(adjusted['processing_offset'], 0)
            self.assertEqual(adjusted['processing_duration'], 48)

    def test_source_overrun_requires_opt_in_and_pads_exact_silence(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            wav = folder / 'source.wav'
            samples = np.array([.25, .5, -.25, -.5])
            sf.write(wav, samples, 48000, subtype='FLOAT')
            original = wav.read_bytes()
            source = dict(name=wav.name, directory=str(folder), frames=4, bits=32,
                          channels=1, sample_rate=48000, record_offset=10)
            clip = dict(offset=20, name='Source', segments=[dict(source=source, start=0, offset=0, length=4)])
            track = dict(name='Audio', path=['Song', 'Audio'], offset=1)
            song = dict(id='song', name='Song', path=['Song'], audio_tracks=[track])
            def events(_):
                return [dict(clip=clip, name='Extended', record_offset=30, priority=0,
                             start=0, duration=8, offset=0, domain=1, flags=0, gain=1),
                        dict(clip=clip, name='Past source', record_offset=31, priority=0,
                             start=1, duration=4, offset=8, domain=1, flags=0, gain=.5)]
            reader = SimpleNamespace(path=folder/'source.cpr', inventory=dict(folders=[song], source_sha256='fixture'),
                                     track_events=events)
            with self.assertRaisesRegex(NativeError, 'extends beyond its source'):
                prepare_song(reader, 'song', 120)
            plan = prepare_song(reader, 'song', 120, allow_silent_tails=True)
            self.assertEqual(plan.sources[20]['frames'], 12)
            output = folder/'test.dawproject'
            report = export_song(plan, output)
            self.assertTrue(any('filled with silence' in w for w in report['warnings']))
            self.assertEqual(len(report['source']['silent_source_tails']), 2)
            with zipfile.ZipFile(output) as archive:
                project = ET.fromstring(archive.read('project.xml'))
                clips = project.findall('.//Clip')
                with archive.open(clips[0].find('Audio/File').get('path')) as audio:
                    actual, _ = sf.read(audio)
                    np.testing.assert_array_equal(actual, np.r_[samples, np.zeros(8)])
                with archive.open(clips[1].find('Audio/File').get('path')) as audio:
                    actual, _ = sf.read(audio)
                    np.testing.assert_array_equal(actual, np.zeros(4))
            self.assertEqual(wav.read_bytes(), original)

    def test_negative_clip_positions_pass_schema_without_false_overlaps(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            wav = folder / 'source.wav'
            sf.write(wav, np.ones(48000), 48000, subtype='PCM_16')
            track = Track('Audio', [Clip('Early', '1', wav, -3, 1, 0, 0),
                                    Clip('Later', '2', wav, -1, 1, 0, 0)])
            output = folder / 'test.dawproject'
            report = write_project([track], {wav: wav_info(wav)}, output, 120)
            self.assertEqual(report['tracks'][0]['overlaps'], [])
            with zipfile.ZipFile(output) as z:
                project = ET.fromstring(z.read('project.xml'))
                self.assertEqual([float(c.get('time')) for c in project.findall('.//Clip')], [-6, -2])
                from lxml import etree
                schema = Path(__file__).resolve().parents[1] / 'reference' / 'Project.xsd'
                etree.XMLSchema(etree.parse(str(schema))).assertValid(etree.fromstring(z.read('project.xml')))


if __name__ == '__main__':
    unittest.main()
