"""Native CPR audio export, using the Cubasis-tested DAWproject writer."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path, PureWindowsPath
import re
import shutil
import tempfile
import numpy as np
import soundfile as sf

from cpr_native import NativeError, Reader
from cubase_to_cubasis import Clip, Track, ConversionError, write_project


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise ConversionError('Export cancelled')


def wav_info(path):
    try:
        with sf.SoundFile(str(path)) as wav:
            widths = {'PCM_16': 2, 'PCM_24': 3, 'PCM_32': 4, 'FLOAT': 4}
            if wav.format not in ('WAV', 'WAVEX', 'RF64') or wav.subtype not in widths:
                raise NativeError(f'Unsupported audio format: {wav.format}/{wav.subtype}')
            return dict(sample_rate=wav.samplerate, channels=wav.channels, frames=wav.frames,
                        sample_width=widths[wav.subtype], subtype=wav.subtype)
    except (OSError, RuntimeError) as exc:
        raise NativeError(f'Cannot read PCM WAV {path.name}: {exc}') from exc


def locate_source(source, project_dir, extra_media=None):
    name = source['name']
    if PureWindowsPath(name).name != name or '/' in name:
        raise NativeError(f'Invalid audio filename: {name}')
    candidates = [Path(source['directory']) / name, project_dir / 'Audio' / name,
                  project_dir / 'Edits' / name, project_dir / name]
    if extra_media:
        candidates.extend([Path(extra_media) / name, Path(extra_media) / 'Audio' / name,
                           Path(extra_media) / 'Edits' / name])
    seen = set()
    mismatches = []
    for candidate in candidates:
        path = candidate.resolve()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        info = wav_info(path)
        if (info['frames'] == source['frames'] and info['sample_rate'] == source['sample_rate']
                and info['channels'] == source['channels'] and info['sample_width'] * 8 == source['bits']):
            return path, info
        mismatches.append(str(path))
    if mismatches:
        raise NativeError(f'Audio does not match saved project metadata: {name}')
    raise NativeError(f'Missing audio: {source["directory"]}{name}')


@dataclass
class SongPlan:
    reader: Reader
    folder: dict
    bpm: float
    tracks: list
    resolved: dict
    sources: dict
    warnings: list
    excluded_tracks: list = field(default_factory=list)


class SongTrackError(NativeError):
    def __init__(self, folder, track, cause):
        super().__init__(f'{folder["name"]} / {track["name"]}: {cause}')
        self.track_offset = track['offset']
        self.track_path = track['path']


def prepare_song(reader, folder_id, bpm, extra_media=None, cancel=None, excluded_track_offsets=None):
    if not math.isfinite(bpm) or not 1 <= bpm <= 1000:
        raise NativeError('Enter a BPM between 1 and 1000')
    folder = next((f for f in reader.inventory['folders'] if f['id'] == folder_id), None)
    if folder is None:
        raise NativeError('Folder is not in the loaded project')
    tracks, sources, resolved = [], {}, {}
    warnings = []
    excluded = set(excluded_track_offsets or ())
    excluded_tracks = [track for track in folder['audio_tracks'] if track['offset'] in excluded]
    for track in folder['audio_tracks']:
        check_cancel(cancel)
        if track['offset'] in excluded:
            continue
        try:
            events = reader.track_events(track)
            for event in events:
                clip = event['clip']
                if clip['offset'] in sources:
                    continue
                segments = sorted(clip['segments'], key=lambda s: s['start'])
                last_end, layout = 0, None
                for segment in segments:
                    check_cancel(cancel)
                    source = segment['source']
                    key = source['record_offset']
                    if key not in resolved:
                        resolved[key] = locate_source(source, reader.path.parent, extra_media)
                    info = resolved[key][1]
                    current_layout = (info['sample_rate'], info['channels'], info['sample_width'], info['subtype'])
                    if layout and current_layout[:2] != layout[:2]:
                        raise NativeError('Source segments have different sample rates or channel counts')
                    if layout and current_layout[3] != layout[3]:
                        if 'FLOAT' in (layout[3], current_layout[3]):
                            raise NativeError('Mixed floating-point and integer source segments are not supported yet')
                        # PCM 16/24/32 can be promoted without losing any samples.
                        width = max(layout[2], current_layout[2])
                        layout = (*layout[:2], width, f'PCM_{width * 8}')
                    else:
                        layout = current_layout
                    if segment['start'] < last_end:
                        raise NativeError('Overlapping source segments are not supported yet')
                    last_end = segment['start'] + segment['length']
                sources[clip['offset']] = dict(clip=clip, segments=segments, frames=last_end,
                                               sample_rate=layout[0], channels=layout[1], sample_width=layout[2], subtype=layout[3])
            for event in events:
                maximum = sources[event['clip']['offset']]['frames']
                if event['offset'] + event['duration'] > maximum + 1:
                    raise NativeError(f'{track["name"]}: an audio event extends beyond its source; time-stretched or otherwise unsupported timing needs a Cubase-rendered source '
                                      f'(offset={event["offset"]:g}, length={event["duration"]:g}, source={maximum} samples)')
            tracks.append((track, events))
        except NativeError as exc:
            raise SongTrackError(folder, track, exc) from exc
    if not tracks or not any(events for _, events in tracks):
        raise NativeError('The selected tracks have no audio clips. Check at least one audio track with clips.')
    if excluded_tracks:
        warnings.append('Tracks excluded by user: ' + ', '.join(' / '.join(t['path']) for t in excluded_tracks))
    if any(e['flags'] & 2 for _, events in tracks for e in events):
        warnings.append('Event mute flag 0x0002 is exported as disabled clips. Track/folder mixer mute and solo are not transferred.')
    if any(not math.isclose(e['gain'], 1.0, abs_tol=1e-7) for _, events in tracks for e in events):
        warnings.append('Clip gain is baked into separate trimmed 32-bit float WAVs.')
    return SongPlan(reader, folder, bpm, tracks, resolved, sources, warnings, excluded_tracks)


def copy_frames(src, dst, count, frame_size, cancel, gain=1):
    remaining = count
    while remaining:
        check_cancel(cancel)
        take = min(remaining, max(1, 1024 * 1024 // frame_size))
        block = src.read(take, dtype='float64', always_2d=True)
        if len(block) != take:
            raise NativeError('Unexpected end of source audio')
        if gain != 1:
            block *= gain
        dst.write(block)
        remaining -= take


def materialize(source, resolved, destination, cancel):
    segments = source['segments']
    first = segments[0]
    original, info = resolved[first['source']['record_offset']]
    if len(segments) == 1 and first['offset'] == 0 and first['start'] == 0 and first['length'] == info['frames']:
        return original
    frame_size = source['channels'] * source['sample_width']
    with sf.SoundFile(str(destination), 'w', samplerate=source['sample_rate'], channels=source['channels'], subtype=source['subtype']) as dst:
        cursor = 0
        for segment in segments:
            gap = segment['start'] - cursor
            while gap:
                check_cancel(cancel)
                count = min(gap, 65536)
                dst.write(np.zeros((count, source['channels']), dtype='float64'))
                gap -= count
            path, _ = resolved[segment['source']['record_offset']]
            with sf.SoundFile(str(path)) as src:
                src.seek(segment['offset'])
                copy_frames(src, dst, segment['length'], frame_size, cancel)
            cursor = segment['start'] + segment['length']
    return destination


def filename_for_song(name):
    result = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .')[:120].rstrip(' .') or 'Song'
    if result.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *[f'COM{i}' for i in range(1, 10)], *[f'LPT{i}' for i in range(1, 10)]}:
        result = '_' + result
    return result


def unique_output(folder, song_name):
    name = filename_for_song(song_name)
    for i in range(1, 10000):
        path = folder / (name + (f' ({i})' if i > 1 else '') + '.dawproject')
        if not path.exists() and not path.with_suffix('.report.json').exists():
            return path
    raise NativeError('Too many outputs with this name')


def export_song(plan, output, cancel=None, progress=None, status=None):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Conservative space estimate: package + reconstructed sources/gain clips.
    needed = sum(s['frames'] * s['channels'] * s['sample_width'] for s in plan.sources.values()) * 3
    if shutil.disk_usage(output.parent).free < needed + 64 * 1024 * 1024:
        raise NativeError(f'Not enough free space (allow about {needed / 1024**3:.1f} GB for this song)')
    with tempfile.TemporaryDirectory(prefix='.cubasis-audio-', dir=output.parent) as folder:
        temporary = Path(folder)
        source_paths, media, tracks = {}, {}, []
        for key, source in plan.sources.items():
            check_cancel(cancel)
            if status:
                status(f'Preparing audio: {source["clip"]["name"]}')
            path = materialize(source, plan.resolved, temporary / f'source_{key}.wav', cancel)
            source_paths[key] = path
        for track, events in plan.tracks:
            clips = []
            for event in events:
                check_cancel(cancel)
                source = plan.sources[event['clip']['offset']]
                path = source_paths[event['clip']['offset']]
                info = {key: source[key] for key in ('frames', 'sample_rate', 'channels', 'sample_width', 'subtype')}
                offset = event['offset'] / info['sample_rate']
                duration = event['duration'] / info['sample_rate']
                if not math.isclose(event['gain'], 1.0, abs_tol=1e-7):
                    adjusted = temporary / f'gain_{event["record_offset"]}.wav'
                    start_frame = round(event['offset'])
                    count = min(round(event['duration']), info['frames'] - start_frame)
                    with sf.SoundFile(str(path)) as src, sf.SoundFile(str(adjusted), 'w', samplerate=info['sample_rate'], channels=info['channels'], subtype='FLOAT') as dst:
                        src.seek(start_frame)
                        copy_frames(src, dst, count, info['channels'] * info['sample_width'], cancel,
                                    event['gain'])
                    path, offset = adjusted, 0
                    info = dict(info, frames=count, sample_width=4, subtype='FLOAT')
                media[path] = info
                start = event['start'] * 60 / (480 * plan.bpm) if event['domain'] == 0 else event['start']
                clips.append(Clip(event['name'], str(event['record_offset']), path, start, duration,
                                  offset, event['priority'], not bool(event['flags'] & 2)))
            relative = track['path'][len(plan.folder['path']):]
            tracks.append(Track(' / '.join(relative), clips))
        source_info = dict(cpr=str(plan.reader.path), sha256=plan.reader.inventory['source_sha256'],
                           folder=plan.folder['path'], positions='Original track positions; musical positions evaluated at selected song BPM',
                           excluded_tracks=[dict(path=t['path'], offset=t['offset']) for t in plan.excluded_tracks],
                           input_audio=[str(path) for path, _ in plan.resolved.values()])
        return write_project(tracks, media, output, plan.bpm, warnings=plan.warnings,
                             progress=progress, cancel=cancel, source_info=source_info)
