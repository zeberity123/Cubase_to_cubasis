"""Bounded, read-only decoder for the Cubase 12 audio structures observed here."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import math
import re
import struct

from cpr_inventory import inspect_project


class NativeError(ValueError):
    pass


@dataclass(frozen=True)
class Object:
    cls: str
    payload: int
    end: int
    next: int


class Reader:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.data = self.path.read_bytes()
        self.inventory = inspect_project(self.path, self.data)
        tag = b'\xff\xff\xff\xfe\x00\x00\x00\x0aGDocument\x00'
        self.base = self.data.find(tag) + 8
        self.classes = {}
        self.inline_ends = {}
        self.versions = {}
        pattern = rb'\xff\xff\xff[\xfe\xff](\x00\x00\x00[\x02-\x60])([A-Za-z][A-Za-z0-9_ ]*)\x00'
        for match in re.finditer(pattern, self.data):
            if self.u32(match.start() + 4) != len(match.group(2)) + 1:
                continue
            cls = match.group(2).decode('ascii')
            self.classes[match.start() + 8 - self.base] = cls
            self.inline_ends[match.end()] = cls
            self.versions[cls] = self.u16(match.end())
        self.clips = {}
        self.files = {}

    def require(self, pos, length, limit=None):
        if pos < 0 or length < 0 or pos + length > min(len(self.data), limit or len(self.data)):
            raise NativeError(f'Truncated or invalid record at byte {pos}')

    def unpack(self, fmt, pos):
        self.require(pos, struct.calcsize(fmt))
        return struct.unpack_from(fmt, self.data, pos)[0]

    def u16(self, pos): return self.unpack('>H', pos)
    def u32(self, pos): return self.unpack('>I', pos)
    def u64(self, pos): return self.unpack('>Q', pos)
    def f32(self, pos): return self.unpack('>f', pos)
    def f64(self, pos): return self.unpack('>d', pos)

    def string(self, pos, limit=None):
        length = self.u32(pos)
        if length > 1024 * 1024:
            raise NativeError(f'Invalid string length at byte {pos}')
        self.require(pos + 4, length, limit)
        raw = self.data[pos + 4:pos + 4 + length]
        if raw.endswith(b'\x00\xef\xbb\xbf'):
            raw = raw[:-4]
        else:
            raw = raw.rstrip(b'\x00')
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise NativeError(f'Unsupported string encoding at byte {pos}') from exc
        return text, pos + 4 + length

    def obj(self, pos):
        original = pos
        ref = self.u32(pos)
        if ref in (0xfffffffe, 0xffffffff):
            for _ in range(30):
                tag = self.u32(pos)
                if tag not in (0xfffffffe, 0xffffffff):
                    raise NativeError(f'Invalid class declaration at byte {pos}')
                cls, end = self.string(pos + 4)
                pos = end + 2
                if tag == 0xffffffff:
                    payload, end = pos + 4, pos + 4 + self.u32(pos)
                    break
            else:
                raise NativeError('Class inheritance is too deep')
            next_pos = end
        elif ref & 0x80000000:
            cls = self.classes.get(ref & 0x7fffffff)
            payload = pos + 8
            end = payload + self.u32(pos + 4)
            next_pos = end
        elif ref:
            payload = self.base + ref - 4
            self.require(payload - 8, 8)
            header = self.u32(payload - 8)
            cls = self.classes.get(header & 0x7fffffff) if header & 0x80000000 else None
            cls = cls or self.inline_ends.get(payload - 6)
            end = payload + self.u32(payload - 4)
            next_pos = pos + 4
        else:
            raise NativeError(f'Unexpected null object at byte {pos}')
        if not cls:
            raise NativeError(f'Unknown object class at byte {original}')
        self.require(payload, end - payload)
        return Object(cls, payload, end, next_pos)

    def attributes(self, pos, limit):
        count = self.u32(pos)
        pos += 4
        if count > 10000:
            raise NativeError('Invalid attribute count')
        names = []
        for _ in range(count):
            name, pos = self.string(pos, limit)
            names.append(name)
            kind = self.u16(pos)
            pos += 2
            if kind & 0x7f in (1, 4):
                pos += 8
            elif kind & 0x7f == 8:
                _, pos = self.string(pos, limit)
            elif kind & 0x7f in (0x22, 0x12, 0x02):
                pos = self.obj(pos).next
            else:
                raise NativeError(f'Unsupported source attribute {name} (0x{kind:x})')
            self.require(pos, 0, limit)
        return names, pos

    def file_path(self, record):
        if record.cls != 'FNPath':
            raise NativeError(f'Expected audio path, found {record.cls}')
        name, pos = self.string(record.payload, record.end)
        pos += 4  # Macintosh file type
        for _ in range(3):  # DOS type, Unix type, display description
            _, pos = self.string(pos, record.end)
        pos += 6
        directory, pos = self.string(pos, record.end)
        if pos != record.end:
            raise NativeError('Unrecognized audio path trailer')
        return dict(name=name, directory=directory)

    def audio_file(self, record):
        if record.cls != 'AudioFile':
            raise NativeError(f'Unsupported audio stream {record.cls}')
        if record.payload in self.files:
            return self.files[record.payload]
        p = record.payload
        frames, bits, frame_size, channels, rate = (self.u64(p), self.u16(p+8), self.u16(p+10),
                                                  self.u16(p+12), self.f32(p+14))
        path = self.obj(p + 18)
        source = self.file_path(path)
        if channels not in (1, 2) or bits not in (16, 24, 32) or frame_size != channels * bits // 8:
            raise NativeError(f'Unsupported audio layout: {source["name"]}')
        if not math.isfinite(rate) or rate < 8000 or rate > 384000:
            raise NativeError('Invalid source sample rate')
        source.update(frames=frames, bits=bits, frame_size=frame_size, channels=channels, sample_rate=int(rate),
                      record_offset=p, trailer=self.data[path.next:record.end].hex())
        self.files[p] = source
        return source

    def cluster(self, record):
        if record.cls != 'AudioCluster':
            raise NativeError(f'Unsupported audio cluster {record.cls}')
        p = self.obj(record.payload).next  # source path
        version = self.u16(p)
        count = self.u32(p + 2)
        p += 6
        if version != 2 or not 1 <= count <= 10000:
            raise NativeError('Unsupported cluster layout')
        files = {}
        for _ in range(count):
            file = self.obj(p)
            files[file.payload] = self.audio_file(file)
            p = file.next
        groups = self.u16(p)
        count = self.u32(p + 2)
        p += 6
        if groups != 1 or not 1 <= count <= 100000:
            raise NativeError('Layered/multi-channel audio clusters are unsupported')
        segments = []
        for _ in range(count):
            item = self.obj(p)
            if item.cls != 'AClusterSegment':
                raise NativeError('Unsupported cluster segment')
            stream = self.obj(item.payload)
            source = self.audio_file(stream)
            q = stream.next
            self.require(q, 24, item.end)
            offset, length, start = self.u64(q), self.u64(q+8), self.u64(q+16)
            if q + 24 != item.end or offset + length > source['frames']:
                raise NativeError('Invalid audio segment bounds')
            segments.append(dict(source=source, offset=offset, length=length, start=start))
            p = item.next
        if p != record.end:
            raise NativeError('Unrecognized audio cluster trailer')
        return segments

    def audio_clip(self, record):
        if record.cls != 'PAudioClip':
            raise NativeError(f'Unsupported clip type {record.cls}')
        if record.payload in self.clips:
            return self.clips[record.payload]
        name, p = self.string(record.payload, record.end)
        domain, period, count = self.u32(p), self.f64(p+4), self.u32(p+12)
        p += 16
        if domain != 10 or not math.isfinite(period) or period <= 0 or count > 1000000:
            raise NativeError('Unsupported source time domain/hitpoint list')
        for _ in range(count):
            hit = self.obj(p)
            if hit.cls != 'MHitPointEvent':
                raise NativeError(f'Unsupported source event {hit.cls}')
            p = hit.next
        path = self.obj(p)
        source_path = self.file_path(path)
        _, p = self.string(path.next, record.end)  # asset identifier
        attributes, p = self.attributes(p + 14, record.end)
        cluster = self.obj(p)
        segments = self.cluster(cluster)
        rates = {s['source']['sample_rate'] for s in segments}
        if len(rates) != 1 or not math.isclose(period, 1 / next(iter(rates)), rel_tol=1e-8):
            raise NativeError('Audio source sample periods disagree')
        result = dict(name=name, period=period, segments=segments, path=source_path,
                      attributes=attributes, offset=record.payload)
        self.clips[record.payload] = result
        return result

    def track_events(self, track):
        node = self.obj(track['offset'])
        name, p = self.string(node.payload, node.end)
        domain = self.u32(p)
        p += 4
        if domain == 0:
            p = self.obj(p).next  # tempo track, possibly first inline declaration
            p = self.obj(p).next  # signature track
        elif domain == 1:
            if self.f64(p) != 1:
                raise NativeError('Unsupported linear track period')
            p += 8
        else:
            raise NativeError(f'Unsupported track timebase {domain}')
        count = self.u32(p)
        p += 4
        if count > 1000000:
            raise NativeError('Invalid event count')
        result = []
        for _ in range(count):
            record = self.obj(p)
            if record.cls != 'MAudioEvent':
                raise NativeError(f'{name}: unsupported event type {record.cls}')
            q = record.payload
            flags = self.u16(q)
            start, duration, offset = self.f64(q+2), self.f64(q+10), self.f64(q+18)
            clip_record = self.obj(q + 26)
            clip = self.audio_clip(clip_record)
            q = clip_record.next
            # Compact event attributes use four-byte tags, unlike source attrs.
            count_attrs = self.u32(q)
            if count_attrs:
                raise NativeError(f'{name}: clip envelopes or additional event processing are not supported yet')
            self.require(q, 28, record.end)
            header = self.data[q:q+24]
            priority = self.u32(q+4)
            if self.u32(q+8) or self.u32(q+12) or self.f64(q+16):
                raise NativeError(f'{name}: fades/crossfades or additional event timing are not supported yet')
            description, after_name = self.string(q+24, record.end)
            tail = self.data[after_name:record.end]
            if len(tail) != 14 or tail[:4] != bytes(4) or tail[8:] != bytes(6):
                raise NativeError(f'{name}: pitch shift, inversion or additional event edits are not supported yet')
            gain = struct.unpack_from('>f', tail, 4)[0]
            if not math.isfinite(gain) or gain < 0:
                raise NativeError('Invalid clip gain')
            if flags & ~2:
                raise NativeError(f'{name}: unsupported event flags 0x{flags:x}')
            if not all(math.isfinite(v) for v in (start, duration, offset)) or start < 0 or duration <= 0 or offset < 0:
                raise NativeError(f'{name}: invalid clip timing')
            result.append(dict(name=description, flags=flags, start=start, duration=duration, offset=offset,
                               clip=clip, priority=priority, header=header.hex(), tail=tail.hex(),
                               record_offset=record.payload, domain=domain, gain=gain))
            p = record.next
        if p != node.end:
            raise NativeError(f'{name}: trailing data after the declared event list ({node.end-p} bytes)')
        return result
