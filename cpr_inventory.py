"""Read-only song-folder inventory for the observed Cubase 12 CPR layout.

This is NOT an audio-event decoder. Node byte ranges establish containment;
declared child counts and enclosing track object types must agree before the
inventory is accepted. Source projects and media are never modified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct


class InventoryError(ValueError):
    pass


def suggest_tempo(name):
    """Conservative name hint; never infer a BPM from the tail of a year/date."""
    tokens = list(re.finditer(r'\d+(?:\.\d+)?', name))
    plausible = [m for m in tokens if 40 <= float(m.group()) <= 300]
    if len(plausible) != 1:
        return None, 'Enter BPM' if not plausible else 'Multiple possible BPM values'
    candidate = plausible[0]
    suffix = name[candidate.end():].strip()
    return float(candidate.group()), 'Review annotation' if suffix else 'From folder name'


def inspect_project(path: Path, data: bytes | None = None):
    path = path.resolve()
    if data is None:
        data = path.read_bytes()
    if data[:4] != b'RIFF' or data[8:12] != b'NUND':
        raise InventoryError('Not a recognized Cubase CPR container')
    # Select the ARCH holding the arrangement, not plug-in or UI archives.
    declaration = b'\xff\xff\xff\xfe\x00\x00\x00\x0aGDocument\x00'
    declaration_pos = data.find(declaration)
    arch = declaration_pos - 8
    if declaration_pos < 8 or data[arch:arch + 4] != b'ARCH':
        raise InventoryError('Cannot identify the arrangement archive')
    arch_end = arch + 8 + struct.unpack_from('>I', data, arch + 4)[0]
    base = declaration_pos + 8
    if arch_end > len(data):
        raise InventoryError('Truncated arrangement archive')
    # Object references encode the declared class-name byte offset from base.
    declarations = {}
    inline_ends = {}
    pattern = rb'\xff\xff\xff[\xfe\xff](\x00\x00\x00[\x02-\x60])([A-Za-z][A-Za-z0-9_ ]*)\x00'
    for match in re.finditer(pattern, data[arch:arch_end]):
        raw = match.group(2)
        if struct.unpack('>I', match.group(1))[0] != len(raw) + 1:
            continue
        start = arch + match.start() + 8
        name = raw.decode('ascii')
        declarations[start - base] = name
        inline_ends[arch + match.end()] = name
    class_offsets = {}
    for name in ('MListNode', 'MTrackList'):
        found = [offset for offset, cls in declarations.items() if cls == name]
        if len(found) != 1:
            raise InventoryError(f'Expected exactly one {name} declaration')
        class_offsets[name] = found[0]

    nodes = []
    for node_class, kind in [('MTrackList', 'folder'), ('MListNode', 'track')]:
        marker = struct.pack('>I', 0x80000000 | class_offsets[node_class])
        for match in re.finditer(re.escape(marker), data[arch:arch_end]):
            pos = arch + match.start()
            if pos < 34 or pos + 12 > arch_end:
                continue
            size, name_size = struct.unpack_from('>II', data, pos + 4)
            end = pos + 8 + size
            if not 4 <= name_size <= 4096 or end > arch_end or size < name_size + 4:
                continue
            encoded = data[pos + 12:pos + 12 + name_size]
            if not encoded.endswith(b'\x00\xef\xbb\xbf'):
                continue
            try:
                name = encoded[:-4].decode('utf-8')
            except UnicodeDecodeError:
                continue
            ref = struct.unpack_from('>I', data, pos - 34)[0]
            enclosing = declarations.get(ref & 0x7fffffff) if ref & 0x80000000 else None
            if enclosing is None:
                enclosing = inline_ends.get(pos - 32)
            if enclosing is None or (enclosing != 'MFolderTrack' and not enclosing.endswith('TrackEvent')):
                continue
            if (kind == 'folder') != (enclosing == 'MFolderTrack'):
                raise InventoryError(f'Unexpected node class for {name}')
            outer_size = struct.unpack_from('>I', data, pos - 30)[0]
            if pos - 26 + outer_size < end or pos - 26 + outer_size > arch_end:
                raise InventoryError(f'Invalid containing object size: {name}')
            node = dict(name=name, kind=kind, track_class=enclosing, offset=pos, end=end)
            if kind == 'folder':
                count_pos = pos + 12 + name_size + 16
                if count_pos + 2 > end:
                    raise InventoryError('Truncated folder header')
                node['declared_children'] = struct.unpack_from('>H', data, count_pos)[0]
            nodes.append(node)
    nodes.sort(key=lambda n: n['offset'])
    folders = [n for n in nodes if n['kind'] == 'folder']
    if not folders:
        raise InventoryError('No supported folder records found')
    for index, folder in enumerate(folders, 1):
        folder['id'] = f'F{index:03d}'
    for node in nodes:
        parents = [f for f in folders if f['offset'] < node['offset'] < f['end']]
        if any(node['end'] > f['end'] for f in parents):
            raise InventoryError(f'Crossing folder boundaries: {node["name"]}')
        node['parent_id'] = parents[-1]['id'] if parents else None
        node['path'] = [f['name'] for f in parents] + [node['name']]
    for folder in folders:
        children = [n for n in nodes if n['parent_id'] == folder['id']]
        if len(children) != folder['declared_children']:
            raise InventoryError(f'{folder["name"]}: decoded {len(children)} children, '
                                 f'expected {folder["declared_children"]}')
        descendants = [n for n in nodes if folder['offset'] < n['offset'] < folder['end']]
        folder['audio_tracks'] = [dict(name=n['name'], path=n['path'], offset=n['offset'])
                                  for n in descendants if n['track_class'] == 'MAudioTrackEvent']
        folder['audio_track_count'] = len(folder['audio_tracks'])
        folder['direct_audio_tracks'] = [dict(name=n['name'], offset=n['offset'])
                                         for n in children if n['track_class'] == 'MAudioTrackEvent']
        folder['direct_audio_track_count'] = len(folder['direct_audio_tracks'])
        folder['child_folder_count'] = sum(n['kind'] == 'folder' for n in children)
        folder['suggested_bpm'], folder['bpm_note'] = suggest_tempo(folder['name'])
    return dict(source=str(path), source_size=len(data), source_sha256=hashlib.sha256(data).hexdigest(),
                folder_count=len(folders), audio_track_count=sum(n['track_class'] == 'MAudioTrackEvent' for n in nodes),
                status='Folder boundaries and immediate child counts checked. Audio events not decoded.',
                folders=folders)


def write_selector(inventory, output):
    payload = json.dumps(inventory, ensure_ascii=False).replace('<', '\\u003c').replace('&', '\\u0026')
    page = r'''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cubase song selection</title><style>
body{font:15px system-ui,sans-serif;background:#111724;color:#e6ecf6;margin:0;padding:28px}
main{max-width:1250px;margin:auto}h1{margin:0 0 8px;font-size:28px}p{color:#b8c5db;line-height:1.5}
.tools{position:sticky;top:0;background:#111724;padding:16px 0;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
input,select,button{font:inherit;padding:9px;border-radius:5px;border:1px solid #475a78;background:#202d43;color:inherit}
input[type=search]{flex:1;min-width:250px}button{cursor:pointer;background:#34569b}button:disabled{opacity:.4}
small{color:#a8b7cd}.bpm{width:78px}.warn{color:#f4bc73}.branch{margin-left:24px;padding-left:12px;border-left:1px solid #40516e}
.folder>summary{padding:10px 4px;cursor:pointer;border-bottom:1px solid #26354d}.folder>summary strong{margin:0 12px}
.controls{display:inline-flex;gap:9px;align-items:center;margin-left:12px}.tracks{color:#b8c5db;margin:10px 0 10px 30px}
input[type=checkbox]{width:19px;height:19px;vertical-align:middle}li{margin:4px 0}#message{color:#f4bc73;white-space:pre-line}
</style><main><h1>Select songs from Cubase</h1>
<p id="summary"></p><p>Parent folders stay visible as branches. Select a folder containing audio tracks directly, then check its BPM. A selected song also includes its nested audio tracks. Original project positions will be kept. Save your selection to send back for conversion; this page does not yet create audio projects.</p>
<div class="tools"><input id="search" type="search" placeholder="Search song or parent folder" aria-label="Search folders">
<button id="expand">Expand folders</button><button id="collapse">Collapse folders</button>
<button id="save">Save selection</button><span id="count"></span></div><p id="message" role="status"></p>
<div id="rows" aria-label="Project folder tree"></div>
<script id="data" type="application/json">PAYLOAD</script><script>
const inventory=JSON.parse(document.getElementById('data').textContent), rows=document.getElementById('rows');
const chosen=new Set(), tempos=new Map(inventory.folders.map(f=>[f.id,f.suggested_bpm??'']));
const byId=new Map(inventory.folders.map(f=>[f.id,f])),children=new Map();
for(const f of inventory.folders){if(!children.has(f.parent_id))children.set(f.parent_id,[]);children.get(f.parent_id).push(f)}
document.getElementById('summary').textContent=`${inventory.folder_count} folders · ${inventory.audio_track_count} audio tracks · ${inventory.source}`;
function render(){rows.replaceChildren();const search=document.getElementById('search').value.toLowerCase(),visible=new Set();
for(const f of inventory.folders){if(f.path.join(' / ').toLowerCase().includes(search)){let current=f;while(current){visible.add(current.id);current=byId.get(current.parent_id)}}}
function branch(parent,container){for(const f of children.get(parent)||[]){if(!visible.has(f.id))continue;
const details=document.createElement('details'),summary=document.createElement('summary'),title=document.createElement('strong'),count=document.createElement('small');
details.className='folder';details.open=true;details.dataset.id=f.id;title.textContent=`${f.id}  ${f.name}`;
count.textContent=`${f.direct_audio_track_count} direct audio tracks`+(f.audio_track_count!==f.direct_audio_track_count?` · ${f.audio_track_count} including subfolders`:'');
if(f.direct_audio_track_count){const check=document.createElement('input');check.type='checkbox';check.checked=chosen.has(f.id);check.setAttribute('aria-label',`Select ${f.path.join(' / ')}`);
check.onclick=e=>e.stopPropagation();check.onchange=()=>{check.checked?chosen.add(f.id):chosen.delete(f.id);update()};summary.append(check)}
summary.append(title,count);
if(f.direct_audio_track_count){const controls=document.createElement('span'),input=document.createElement('input'),note=document.createElement('small');controls.className='controls';
input.type='number';input.min='1';input.max='1000';input.step='any';input.className='bpm';input.value=tempos.get(f.id);input.setAttribute('aria-label',`BPM for ${f.name}`);
input.oninput=()=>tempos.set(f.id,input.value);input.onclick=e=>e.stopPropagation();note.textContent='BPM · '+f.bpm_note;note.className=f.bpm_note==='From folder name'?'':'warn';controls.append(input,note);summary.append(controls)}
details.append(summary);
if(f.direct_audio_track_count){const tracks=document.createElement('details'),caption=document.createElement('summary'),list=document.createElement('ul');tracks.className='tracks';caption.textContent='Show direct audio tracks';
for(const t of f.direct_audio_tracks){const li=document.createElement('li');li.textContent=t.name;list.append(li)}tracks.append(caption,list);details.append(tracks)}
const nested=document.createElement('div');nested.className='branch';branch(f.id,nested);details.append(nested);container.append(details)}}
branch(null,rows);update()}
function update(){document.getElementById('count').textContent=`${chosen.size} selected`;document.getElementById('save').disabled=!chosen.size}
document.getElementById('search').oninput=render;
document.getElementById('expand').onclick=()=>rows.querySelectorAll('details.folder').forEach(d=>d.open=true);
document.getElementById('collapse').onclick=()=>rows.querySelectorAll('details.folder').forEach(d=>d.open=false);
document.getElementById('save').onclick=()=>{const folders=inventory.folders.filter(f=>chosen.has(f.id)), errors=[];
for(const f of folders){const n=Number(tempos.get(f.id));if(!Number.isFinite(n)||n<1||n>1000)errors.push(`Enter a BPM for ${f.name}`);
let parent=f.parent_id;while(parent){if(chosen.has(parent))errors.push(`Select either ${f.name} or its parent, not both`);parent=inventory.folders.find(p=>p.id===parent).parent_id}
}
document.getElementById('message').textContent=errors.join('\n');if(errors.length)return;
const plan={version:1,source:inventory.source,source_sha256:inventory.source_sha256,position:'preserve',
songs:folders.map(f=>({id:f.id,path:f.path,bpm:Number(tempos.get(f.id)),audio_track_count:f.audio_track_count}))};
const url=URL.createObjectURL(new Blob([JSON.stringify(plan,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='song-selection.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
render();</script></main></html>'''.replace('PAYLOAD', payload)
    output.write_text(page, encoding='utf-8')


def tree_text(inventory):
    lines = [Path(inventory['source']).name, 'Numbers in brackets are direct audio-track counts; parent folders are retained.', '']
    children = {}
    for folder in inventory['folders']:
        children.setdefault(folder['parent_id'], []).append(folder)
    def visit(parent, prefix):
        siblings = children.get(parent, [])
        for i, folder in enumerate(siblings):
            last = i == len(siblings) - 1
            bpm = folder['suggested_bpm']
            suffix = f"{bpm:g} BPM" if bpm is not None else 'BPM needs entry'
            label = f" [{folder['direct_audio_track_count']} audio; {suffix}]" if folder['direct_audio_track_count'] else ' [parent]'
            lines.append(prefix + ('└─ ' if last else '├─ ') + folder['id'] + ' ' + folder['name'] + label)
            visit(folder['id'], prefix + ('   ' if last else '│  '))
    visit(None, '')
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project', type=Path)
    parser.add_argument('--output', type=Path, default=Path('output/song-folders'))
    args = parser.parse_args()
    inventory = inspect_project(args.project)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix('.json').write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding='utf-8')
    write_selector(inventory, args.output.with_suffix('.html'))
    args.output.with_suffix('.txt').write_text(tree_text(inventory), encoding='utf-8')
    print(f"Read {inventory['folder_count']} folders and {inventory['audio_track_count']} audio tracks; all folder child counts match.")
    print(f'Selector: {args.output.with_suffix(".html")}')


if __name__ == '__main__':
    main()
