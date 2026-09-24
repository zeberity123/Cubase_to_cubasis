"""Convert a supported Cubase Pro XML track archive to audio-only DAWproject.

Initial, deliberately narrow implementation, validated against a Cubase 12 sample.
Uses Python 3.10+ standard library; no installation or network access required.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, asdict
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import sys
import tempfile
import wave
import xml.etree.ElementTree as ET
import zipfile


class ConversionError(ValueError):
    pass


def value(node, name, default=None):
    child = node.find(f"./*[@name='{name}']") if node is not None else None
    return child.get('value', default) if child is not None else default


def number(node, name, default=None):
    raw = value(node, name, default)
    if raw is None:
        raise ConversionError(f"Missing {name}")
    result = float(raw)
    if not math.isfinite(result):
        raise ConversionError(f"Non-finite {name}")
    return result


def fmt(n):
    return format(n, '.15g')


@dataclass
class Clip:
    name: str
    event_id: str
    source: Path
    start_seconds: float
    duration_seconds: float
    offset_seconds: float
    priority: int
    enabled: bool = True


@dataclass
class Track:
    name: str
    clips: list[Clip]


class Archive:
    def __init__(self, xml_path: Path, tempo: float):
        self.path = xml_path.resolve()
        if not math.isfinite(tempo) or not 1 <= tempo <= 1000:
            raise ConversionError('Tempo must be between 1 and 1000 BPM')
        self.tempo = tempo
        self.root = ET.parse(self.path).getroot()
        if self.root.tag not in ('tracklist', 'tracklist2'):
            raise ConversionError('Expected a Cubase Selected Tracks XML archive')
        # Cubase emits shared objects once, then references them by ID.
        self.objects = {e.get('ID'): e for e in self.root.iter('obj')
                        if e.get('ID') and e.get('class')}
        self.media = {}
        self.warnings = []
        self._check_tempo()
        signatures = self.root.findall(".//obj[@class='MTimeSignatureEvent']")
        if len(signatures) != 1 or number(signatures[0], 'Start') != 0:
            raise ConversionError('This prototype requires one constant time signature')
        self.signature = (int(number(signatures[0], 'Numerator')),
                          int(number(signatures[0], 'Denominator')))

    def resolve(self, node):
        if node is None:
            raise ConversionError('Missing object reference')
        if node.get('class'):
            return node
        try:
            return self.objects[node.get('ID')]
        except KeyError:
            raise ConversionError(f"Unresolved object ID {node.get('ID')}") from None

    def _check_tempo(self):
        for tempo in self.root.findall(".//obj[@class='MTempoTrackEvent']"):
            if number(tempo, 'RehearsalMode', 0) == 1:
                bpm = number(tempo, 'RehearsalTempo')
            else:
                events = tempo.findall("./list[@name='TempoEvent']/obj")
                if len(events) != 1 or number(events[0], 'PPQ') != 0:
                    raise ConversionError('Changing tempo is not supported yet')
                bpm = number(events[0], 'BPM')
            if not math.isclose(bpm, self.tempo, abs_tol=1e-6):
                raise ConversionError(f'Active XML tempo is {bpm}, not {self.tempo}')

    def audio(self, clip):
        domain = clip.find("./member[@name='Domain']")
        if number(domain, 'Type') != 10:
            raise ConversionError('Unsupported audio source time domain')
        period = number(domain, 'Period')
        cluster = self.resolve(clip.find("./obj[@name='Cluster']"))
        streams = cluster.findall("./list[@name='Substreams']/obj")
        segments = cluster.findall("./list[@name='Segments']/item")
        if len(streams) != 1 or len(segments) != 1:
            raise ConversionError('Processed/multi-segment audio sources are not supported yet')
        stream = self.resolve(streams[0])
        segment = segments[0]
        if (number(segment, 'Start') != 0 or number(segment, 'Offset') != 0
                or self.resolve(segment.find("./obj[@name='Stream']")) is not stream
                or number(segment, 'Length') != number(stream, 'FrameCount')):
            raise ConversionError('Nontrivial audio source segment mapping is not supported yet')
        path_ref = stream.find("./obj[@name='archivePath']")
        if path_ref is None:
            path_ref = stream.find("./obj[@name='FPath']")
        name = value(self.resolve(path_ref), 'Name')
        if not name or PureWindowsPath(name).name != name or '/' in name:
            raise ConversionError('Expected a media filename without directory components')
        # Only use copied media alongside the archive, never old absolute paths.
        candidates = [p for folder in (self.path.parent / 'Media', self.path.parent / 'media', self.path.parent)
                      if folder.is_dir() for p in folder.iterdir()
                      if p.is_file() and p.name.casefold() == name.casefold()]
        candidates = list(dict.fromkeys(p.resolve() for p in candidates))
        if len(candidates) != 1:
            raise ConversionError(f'Missing or ambiguous copied media: {name}')
        path = candidates[0]
        if path not in self.media:
            try:
                with wave.open(str(path), 'rb') as wav:
                    self.media[path] = dict(sample_rate=wav.getframerate(), channels=wav.getnchannels(),
                                            frames=wav.getnframes(), sample_width=wav.getsampwidth())
            except (wave.Error, EOFError) as exc:
                raise ConversionError(f'Unsupported PCM WAV {name}: {exc}') from exc
        info = self.media[path]
        if info['channels'] not in (1, 2):
            raise ConversionError('Only mono and stereo audio are supported')
        if (info['frames'] != number(stream, 'FrameCount')
                or info['sample_rate'] != number(stream, 'Rate')
                or info['channels'] != number(stream, 'Channels')
                or not math.isclose(period, 1 / info['sample_rate'], rel_tol=1e-9)):
            raise ConversionError(f'WAV and XML audio metadata disagree: {name}')
        return path, period

    def tracks(self):
        result = []
        for raw in self.root.findall("./list[@name='track']/obj"):
            if raw.get('class') != 'MAudioTrackEvent':
                self.warnings.append(f"Skipped non-audio track: {raw.get('class')}")
                continue
            if number(raw, 'Start', 0) != 0:
                raise ConversionError('Nonzero track container start is unsupported')
            node = self.resolve(raw.find("./obj[@name='Node']"))
            domain = node.find("./member[@name='Domain']")
            kind = number(domain, 'Type')
            if kind == 0:
                # Musical position uses 480 PPQ; source lengths/offsets use samples.
                start_scale = 60 / (480 * self.tempo)
            elif kind == 1 and number(domain, 'Period', 1) == 1:
                start_scale = 1
            else:
                raise ConversionError(f'Unsupported track time domain {kind}')
            track = Track(value(node, 'Name', 'Audio'), [])
            for event in node.findall("./list[@name='Events']/obj"):
                if event.get('class') != 'MAudioEvent':
                    raise ConversionError('Nested audio parts or other event types are unsupported')
                allowed = {'Start', 'Length', 'Offset', 'Priority', 'Description', 'AudioClip', 'Inverted', 'Flags'}
                unknown = {e.get('name', e.tag) for e in event} - allowed
                if unknown or number(event, 'Inverted', 0) != 0 or number(event, 'Flags', 0) != 0:
                    raise ConversionError(f"Event {event.get('ID')}: unsupported edits/flags {sorted(unknown)}")
                source, period = self.audio(self.resolve(event.find("./obj[@name='AudioClip']")))
                clip = Clip(value(event, 'Description', track.name), event.get('ID'), source,
                            number(event, 'Start') * start_scale,
                            number(event, 'Length') * period, number(event, 'Offset', 0) * period,
                            int(number(event, 'Priority', 0)))
                info = self.media[source]
                if clip.start_seconds < 0 or clip.offset_seconds < 0 or clip.duration_seconds <= 0:
                    raise ConversionError('Negative position/offset or nonpositive clip length')
                if clip.offset_seconds + clip.duration_seconds > (info['frames'] + 1) / info['sample_rate']:
                    raise ConversionError('Clip extends beyond the source audio file')
                track.clips.append(clip)
            result.append(track)
        if not result or not any(t.clips for t in result):
            raise ConversionError('No supported audio clips found')
        return result


def xml_bytes(root):
    ET.indent(root)
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def convert(xml_path: Path, output: Path, tempo: float):
    archive = Archive(xml_path, tempo)
    tracks = archive.tracks()
    archive.warnings.append('No explicit event-mute flags were present in this supported XML input. '
                            'Other event flags/edits are rejected, not guessed.')
    return write_project(tracks, archive.media, output, tempo, archive.signature, archive.warnings)


def write_project(tracks, media, output, tempo, signature=(4, 4), warnings=None,
                  progress=None, cancel=None, source_info=None):
    warnings = list(warnings or [])
    def check_cancel():
        if cancel is not None and cancel.is_set():
            raise ConversionError('Export cancelled')
    check_cancel()
    output = output.resolve()
    report_path = output.with_suffix('.report.json')
    if output.suffix.lower() != '.dawproject':
        raise ConversionError('Output must have the .dawproject extension')
    if output.exists() or report_path.exists():
        raise ConversionError('Output or report already exists; choose a new output name')
    # Original Unicode names remain on tracks/clips and in the report.
    embedded = {p: f'audio/audio_{i:03d}.wav' for i, p in enumerate(media, 1)}
    project = ET.Element('Project', version='1.0')
    ET.SubElement(project, 'Application', name='Cubase Audio Archive Converter', version='0.2.0')
    transport = ET.SubElement(project, 'Transport')
    ET.SubElement(transport, 'Tempo', unit='bpm', value=fmt(tempo), id='tempo')
    ET.SubElement(transport, 'TimeSignature', numerator=str(signature[0]),
                  denominator=str(signature[1]), id='signature')
    structure = ET.SubElement(project, 'Structure')
    arrangement = ET.SubElement(project, 'Arrangement', id='arrangement')
    # Follow the published arrangement -> track Lanes -> Clips hierarchy.
    # Arrangement coordinates are beats; source offsets remain seconds.
    lanes = ET.SubElement(arrangement, 'Lanes', id='lanes', timeUnit='beats')
    beats_per_second = tempo / 60
    report = dict(tempo=tempo, time_signature=signature, tracks=[], media=[],
                  warnings=warnings, validation='Native exports need a playback check in Cubasis',
                  source=source_info)
    for i, track in enumerate(tracks, 1):
        track_id = f'track{i}'
        tr = ET.SubElement(structure, 'Track', id=track_id, name=track.name, contentType='audio', loaded='true')
        channel = ET.SubElement(tr, 'Channel', id=f'channel{i}', audioChannels='2', role='regular',
                                destination='masterChannel', solo='false')
        ET.SubElement(channel, 'Mute', value='false')
        ET.SubElement(channel, 'Pan', value='0.5', unit='normalized')
        ET.SubElement(channel, 'Volume', value='1', unit='linear')
        track_lanes = ET.SubElement(lanes, 'Lanes', id=f'trackLanes{i}', track=track_id)
        clips = ET.SubElement(track_lanes, 'Clips', id=f'clips{i}')
        track_report = dict(name=track.name, clips=[], gaps=[], overlaps=[])
        covered_end = 0
        for clip_index, clip in enumerate(sorted(track.clips, key=lambda c: c.start_seconds), 1):
            info = media[clip.source]
            element = ET.SubElement(clips, 'Clip', name=clip.name,
                                    time=fmt(clip.start_seconds * beats_per_second),
                                    duration=fmt(clip.duration_seconds * beats_per_second), contentTimeUnit='seconds',
                                    playStart=fmt(clip.offset_seconds),
                                    playStop=fmt(clip.offset_seconds + clip.duration_seconds),
                                    enable='true' if clip.enabled else 'false')
            audio = ET.SubElement(element, 'Audio', id=f'audio{i}_{clip_index}', timeUnit='seconds',
                                  duration=fmt(info['frames'] / info['sample_rate']),
                                  sampleRate=str(info['sample_rate']), channels=str(info['channels']))
            ET.SubElement(audio, 'File', path=embedded[clip.source], external='false')
            end = clip.start_seconds + clip.duration_seconds
            tolerance = 1 / info['sample_rate']
            if clip.start_seconds > covered_end + tolerance:
                track_report['gaps'].append([covered_end, clip.start_seconds])
            elif clip.start_seconds < covered_end - tolerance:
                track_report['overlaps'].append([clip.start_seconds, min(covered_end, end)])
            covered_end = max(covered_end, end)
            item = asdict(clip)
            item['source'] = clip.source.name
            item['end_seconds'] = end
            track_report['clips'].append(item)
        if track_report['overlaps']:
            warnings.append(f'{track.name}: overlapping clips retained. Cubase playback priority '
                                    'is not mapped; compare overlap playback in Cubasis.')
        report['tracks'].append(track_report)
    master = ET.SubElement(structure, 'Track', id='master', name='Master', contentType='audio', loaded='true')
    channel = ET.SubElement(master, 'Channel', id='masterChannel', role='master', audioChannels='2', solo='false')
    ET.SubElement(channel, 'Mute', value='false')
    ET.SubElement(channel, 'Volume', value='1', unit='linear')
    metadata = ET.Element('MetaData')
    ET.SubElement(metadata, 'Title').text = output.stem
    warnings.append('Mixer settings, automation, effects and instruments are omitted. '
                    'Audio tracks use unity volume and center pan.')
    output.parent.mkdir(parents=True, exist_ok=True)
    total = sum(p.stat().st_size for p in embedded)
    done = 0
    fd, temporary = tempfile.mkstemp(prefix='.cubasis-', suffix='.partial', dir=output.parent)
    os.close(fd)
    temporary = Path(temporary)
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.writestr('project.xml', xml_bytes(project))
            z.writestr('metadata.xml', xml_bytes(metadata))
            for path, entry in embedded.items():
                check_cancel()
                digest = hashlib.sha256()
                zip_info = zipfile.ZipInfo(entry)
                zip_info.compress_type = zipfile.ZIP_DEFLATED
                zip_info.file_size = path.stat().st_size
                with path.open('rb') as src, z.open(zip_info, 'w') as dst:
                    while chunk := src.read(1024 * 1024):
                        check_cancel()
                        digest.update(chunk)
                        dst.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total, path.name)
                report['media'].append(dict(file=path.name, archive_path=entry, sha256=digest.hexdigest(),
                                            **media[path]))
        check_cancel()
        # Atomic create without overwriting a file another process just created.
        if os.name == 'nt':
            os.rename(temporary, output)  # Windows rename fails if destination exists; works on exFAT too.
        else:
            os.link(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--tempo', type=float, required=True, help='Constant project tempo in BPM')
    args = parser.parse_args()
    try:
        report = convert(args.archive, args.output, args.tempo)
    except (ConversionError, OSError, ET.ParseError, ValueError) as exc:
        parser.exit(1, f'Conversion failed: {exc}\n')
    print(f"Created {args.output}: {len(report['tracks'])} tracks, "
          f"{sum(len(t['clips']) for t in report['tracks'])} clips")
    for warning in report['warnings']:
        print(f'Note: {warning}')


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    main()
