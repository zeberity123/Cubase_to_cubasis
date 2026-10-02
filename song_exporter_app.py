"""Windows desktop song-folder exporter. All processing stays on this computer."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter import font as tkfont

from cpr_native import Reader
from cpr_export import prepare_song, export_song, unique_output


APP_NAME = 'Cubase to Cubasis — Song Exporter'
VERSION = '0.3.4'


class App:
    def __init__(self, root):
        self.root = root
        self.reader = None
        self.source_stamp = None
        self.selected = set()
        self.excluded_tracks = set()
        self.failed_songs = {}
        self.preview_tracks = {}
        self.bpms = {}
        self.by_id = {}
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.editing = None
        self.setting_bpm = False
        root.title(f'{APP_NAME} v{VERSION}')
        root.geometry('1160x820')
        root.minsize(900, 650)
        root.configure(bg='#f2f5f8')
        root.protocol('WM_DELETE_WINDOW', self.close)
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('.', font=('Segoe UI', 10), background='#f2f5f8', foreground='#1b293b')
        row_height = max(29, tkfont.Font(root, font=('Segoe UI', 10)).metrics('linespace') + 8)
        style.configure('Treeview', rowheight=row_height, background='white', fieldbackground='white', font=('Segoe UI', 10))
        style.configure('Treeview.Heading', font=('Segoe UI', 10, 'bold'), background='#e5ecf3')
        style.configure('Accent.TButton', background='#176b75', foreground='white', padding=(18, 9), font=('Segoe UI', 10, 'bold'))
        style.map('Accent.TButton', background=[('active', '#208594'), ('disabled', '#9eaeb7')])
        style.configure('TButton', padding=(10, 6))
        self.project_path = tk.StringVar()
        self.output_path = tk.StringVar()
        self.extra_path = tk.StringVar()
        self.allow_silent_tails = tk.BooleanVar(value=False)
        self.search = tk.StringVar()
        self.bpm = tk.StringVar()
        self.status = tk.StringVar(value='Open a saved Cubase project to begin.')
        self.summary = tk.StringVar(value='No project loaded')
        self.selection_label = tk.StringVar(value='0 songs selected')
        self.title = tk.StringVar(value='Select a song folder')
        self.track_label = tk.StringVar(value='Audio tracks')
        frame = ttk.Frame(root, padding=20)
        frame.pack(fill='both', expand=True)
        # Reserve the footer before the expanding list requests its space.
        # Packing it last hides the export action on small/high-DPI windows.
        footer = ttk.Frame(frame)
        footer.pack(side='bottom', fill='x')
        settings_bar = ttk.Frame(frame)
        settings_bar.pack(fill='x', pady=(0, 8))
        self.settings_button = ttk.Button(settings_bar, text='Project / output folders', command=self.toggle_settings)
        self.settings_button.pack(side='left')
        self.project_label = tk.StringVar(value='No project loaded')
        ttk.Label(settings_bar, textvariable=self.project_label).pack(side='left', padx=12)
        self.settings_frame = ttk.Frame(frame)
        self.settings_frame.pack(fill='x')
        self.buttons = []
        self.path_row(self.settings_frame, 'Cubase project', self.project_path, self.browse_project, 'Open .cpr…')
        self.path_row(self.settings_frame, 'Output folder', self.output_path, self.browse_output, 'Browse…')
        self.path_row(self.settings_frame, 'Extra audio folder', self.extra_path, self.browse_extra, 'Optional…')
        bar = ttk.Frame(frame)
        self.search_bar = bar
        bar.pack(fill='x', pady=(8, 8))
        ttk.Label(bar, text='Find folder').pack(side='left', padx=(0, 8))
        ttk.Entry(bar, textvariable=self.search).pack(side='left', fill='x', expand=True)
        self.search.trace_add('write', lambda *_: self.render())
        ttk.Button(bar, text='Expand', command=lambda: self.expand(True)).pack(side='left', padx=(8, 4))
        ttk.Button(bar, text='Collapse', command=lambda: self.expand(False)).pack(side='left')
        panes = ttk.Panedwindow(frame, orient='horizontal')
        panes.pack(fill='both', expand=True)
        left = ttk.Frame(panes)
        right = ttk.Frame(panes, padding=(16, 0, 0, 0))
        panes.add(left, weight=3)
        panes.add(right, weight=1)
        self.tree = ttk.Treeview(left, columns=('bpm', 'direct', 'total'), selectmode='browse')
        self.tree.heading('#0', text='Song folders — click a checkbox to select')
        self.tree.heading('bpm', text='BPM')
        self.tree.heading('direct', text='Direct')
        self.tree.heading('total', text='All tracks')
        self.tree.column('#0', width=435, minwidth=220)
        for col, width in [('bpm', 62), ('direct', 52), ('total', 68)]:
            self.tree.column(col, width=width, minwidth=width, stretch=False, anchor='center')
        scroll = ttk.Scrollbar(left, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True)
        self.tree.tag_configure('parent', foreground='#637386')
        self.tree.bind('<Button-1>', self.click)
        self.tree.bind('<space>', self.toggle_focused)
        self.tree.bind('<<TreeviewSelect>>', self.focus_folder)
        self.icons = [self.checkbox(False), self.checkbox(True)]
        self.song_title_label = ttk.Label(right, textvariable=self.title, font=('Segoe UI', 13, 'bold'), wraplength=280)
        self.song_title_label.pack(anchor='w')
        bpm_row = ttk.Frame(right)
        bpm_row.pack(fill='x', pady=(8, 10))
        ttk.Label(bpm_row, text='Song BPM').pack(side='left', padx=(0, 8))
        self.bpm_entry = ttk.Entry(bpm_row, textvariable=self.bpm, width=8)
        self.bpm_entry.pack(side='left')
        self.bpm.trace_add('write', self.change_bpm)
        tails = ttk.Checkbutton(right, text='Allow silent source tails', variable=self.allow_silent_tails)
        tails.pack(anchor='w', pady=(0, 8))
        self.buttons.append(tails)
        ttk.Label(right, textvariable=self.track_label, font=('Segoe UI', 10, 'bold')).pack(anchor='w')
        preview_frame = ttk.Frame(right)
        preview_frame.pack(fill='both', expand=True, pady=(5, 0))
        self.preview = ttk.Treeview(preview_frame, show='tree', selectmode='browse', height=1)
        self.preview.column('#0', width=300, minwidth=120)
        self.preview.tag_configure('failed', foreground='#b13a30')
        self.preview.tag_configure('excluded', foreground='#7a8794')
        preview_scroll = ttk.Scrollbar(preview_frame, orient='vertical', command=self.preview.yview)
        preview_horizontal = ttk.Scrollbar(preview_frame, orient='horizontal', command=self.preview.xview)
        self.preview.configure(yscrollcommand=preview_scroll.set, xscrollcommand=preview_horizontal.set)
        preview_horizontal.pack(side='bottom', fill='x')
        preview_scroll.pack(side='right', fill='y')
        self.preview.pack(fill='both', expand=True)
        self.preview.bind('<Button-1>', self.click_track)
        self.preview.bind('<space>', self.toggle_focused_track)
        bottom = ttk.Frame(footer)
        bottom.pack(fill='x', pady=(12, 7))
        ttk.Label(bottom, textvariable=self.selection_label).pack(side='left')
        self.export_button = ttk.Button(bottom, text='Export selected songs', style='Accent.TButton', command=self.export, state='disabled')
        self.export_button.pack(side='right')
        self.cancel_button = ttk.Button(bottom, text='Cancel', command=self.cancel.set, state='disabled')
        self.cancel_button.pack(side='right', padx=8)
        tools_bar = ttk.Frame(footer)
        tools_bar.pack(fill='x', pady=(0, 7))
        self.open_button = ttk.Button(tools_bar, text='Open output folder', command=self.open_output)
        self.open_button.pack(side='left')
        self.retry_button = ttk.Menubutton(tools_bar, text='Clear / select failed songs')
        retry_menu = tk.Menu(self.retry_button, tearoff=False)
        retry_menu.add_command(label='Clear song selection', command=self.clear_songs)
        retry_menu.add_command(label='Select failed songs', command=self.select_failed_songs)
        retry_menu.add_command(label='Load failed songs from summary...', command=self.load_failed_summary)
        self.retry_button.configure(menu=retry_menu)
        self.retry_button.pack(side='left', padx=8)
        self.buttons.append(self.retry_button)
        self.log_button = ttk.Button(tools_bar, text='Show log', command=self.toggle_log)
        self.log_button.pack(side='left')
        self.progress = ttk.Progressbar(footer, mode='determinate', maximum=100)
        self.progress.pack(fill='x')
        ttk.Label(footer, textvariable=self.status, wraplength=1080).pack(anchor='w', pady=(5, 2))
        self.log_frame = ttk.Frame(footer)
        self.log = tk.Text(self.log_frame, height=4, wrap='word', font=('Consolas', 9), relief='flat', bg='#e6edf3', padx=8, pady=6, state='disabled')
        self.log.pack(fill='x', pady=(5, 0))
        ttk.Label(self.log_frame, textvariable=self.summary, foreground='#65778a').pack(anchor='w', pady=(6, 0))
        root.after(100, self.poll)

    def checkbox(self, checked):
        image = tk.PhotoImage(width=18, height=18)
        image.put('#6e849b', to=(1, 1, 17, 17))
        image.put('#176b75' if checked else '#ffffff', to=(2, 2, 16, 16))
        if checked:
            for x, y in [(4, 8), (5, 9), (6, 10), (7, 11), (8, 10), (9, 9), (10, 8), (11, 7), (12, 6), (13, 5)]:
                image.put('white', to=(x, y, x+2, y+2))
        return image

    def toggle_settings(self):
        if self.settings_frame.winfo_manager():
            self.settings_frame.pack_forget()
        else:
            self.settings_frame.pack(fill='x', before=self.search_bar)

    def path_row(self, parent, label, variable, command, button_text):
        row = ttk.Frame(parent)
        row.pack(fill='x', pady=3)
        ttk.Label(row, text=label, width=19).pack(side='left')
        ttk.Entry(row, textvariable=variable, state='readonly').pack(side='left', fill='x', expand=True)
        button = ttk.Button(row, text=button_text, command=command, width=13)
        button.pack(side='left', padx=(8, 0))
        self.buttons.append(button)

    def browse_project(self):
        path = filedialog.askopenfilename(title='Open a saved Cubase project', filetypes=[('Cubase projects', '*.cpr')])
        if path:
            self.load(path)

    def browse_output(self):
        path = filedialog.askdirectory(title='Choose where to save DAWproject files')
        if path:
            self.output_path.set(path)

    def browse_extra(self):
        path = filedialog.askdirectory(title='Optional: locate an Audio folder moved since the project was saved')
        if path:
            self.extra_path.set(path)

    def load(self, path):
        if self.busy:
            return
        self.set_busy(True)
        self.status.set('Reading folder hierarchy…')
        self.progress.configure(mode='indeterminate')
        self.progress.start(10)
        def worker():
            try:
                source = Path(path)
                before = source.stat()
                reader = Reader(source)
                after = source.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError('The project changed while being read. Save it in Cubase, then open it again.')
                self.events.put(('loaded', reader, (after.st_size, after.st_mtime_ns)))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def loaded(self, reader, stamp):
        self.reader, self.source_stamp = reader, stamp
        self.project_path.set(str(reader.path))
        self.project_label.set(Path(reader.path).name)
        self.settings_frame.pack_forget()
        if not self.output_path.get():
            self.output_path.set(str(Path.home() / 'Desktop' / 'Cubasis Exports'))
        self.selected.clear()
        self.excluded_tracks.clear()
        self.failed_songs.clear()
        self.editing = None
        self.preview_tracks.clear()
        self.preview.delete(*self.preview.get_children())
        self.track_label.set('Audio tracks')
        self.title.set('Select a song folder')
        self.setting_bpm = True
        self.bpm.set('')
        self.setting_bpm = False
        self.bpm_entry.configure(state='disabled')
        self.by_id = {f['id']: f for f in reader.inventory['folders']}
        self.bpms = {f['id']: format(f['suggested_bpm'], 'g') if f['suggested_bpm'] is not None else '' for f in self.by_id.values()}
        self.render()
        self.summary.set(f"{len(self.by_id)} folders · {reader.inventory['audio_track_count']} audio tracks · v{VERSION}")
        self.status.set('Check the song folders to export. Parent folders are kept for navigation.')
        self.set_busy(False)

    def render(self):
        if not hasattr(self, 'tree') or not self.reader:
            return
        focus = self.tree.focus()
        self.tree.delete(*self.tree.get_children())
        query = self.search.get().casefold()
        visible = set()
        for folder in self.by_id.values():
            if query in ' / '.join(folder['path']).casefold():
                current = folder
                while current:
                    visible.add(current['id'])
                    current = self.by_id.get(current['parent_id'])
        for fid, f in self.by_id.items():
            if fid not in visible:
                continue
            selectable = f['direct_audio_track_count'] > 0
            self.tree.insert(f['parent_id'] or '', 'end', iid=fid, text=f" {f['name']}", open=True,
                             image=self.icons[int(fid in self.selected)] if selectable else '',
                             values=(self.bpms[fid] or ('?' if selectable else ''), f['direct_audio_track_count'], f['audio_track_count']),
                             tags=() if selectable else ('parent',))
        if focus and self.tree.exists(focus):
            self.tree.selection_set(focus)
            self.tree.focus(focus)
        self.update_count()

    def expand(self, opened):
        for fid in self.by_id:
            if self.tree.exists(fid):
                self.tree.item(fid, open=opened)

    def click(self, event):
        row = self.tree.identify_row(event.y)
        if row and 'image' in self.tree.identify_element(event.x, event.y):
            self.toggle(row)

    def toggle_focused(self, event=None):
        self.toggle(self.tree.focus())
        return 'break'

    def toggle(self, fid):
        if self.busy or fid not in self.by_id or not self.by_id[fid]['direct_audio_track_count']:
            return
        if fid in self.selected:
            self.selected.remove(fid)
        else:
            self.selected.add(fid)
        if self.tree.exists(fid):
            self.tree.item(fid, image=self.icons[int(fid in self.selected)])
        self.update_count()

    def update_count(self):
        self.selection_label.set(f'{len(self.selected)} songs selected')
        self.export_button.configure(state='normal' if self.selected and not self.busy else 'disabled')

    def focus_folder(self, event=None):
        selected = self.tree.selection()
        if not selected:
            return
        self.editing = selected[0]
        f = self.by_id[self.editing]
        self.title.set(f['name'])
        self.setting_bpm = True
        self.bpm.set(self.bpms[self.editing])
        self.setting_bpm = False
        self.bpm_entry.configure(state='normal' if f['direct_audio_track_count'] and not self.busy else 'disabled')
        self.render_tracks()

    def render_tracks(self):
        focus = self.preview.focus()
        scroll_position = self.preview.yview()[0]
        self.preview.delete(*self.preview.get_children())
        self.preview_tracks.clear()
        if self.editing not in self.by_id:
            return
        f = self.by_id[self.editing]
        failed_offsets = {failure.get('track_offset') for failure in self.failed_songs.values()}
        for t in f['audio_tracks']:
            iid = str(t['offset'])
            self.preview_tracks[iid] = t
            included = t['offset'] not in self.excluded_tracks
            failed = t['offset'] in failed_offsets
            name = ' / '.join(t['path'][len(f['path']):]) or t['name']
            self.preview.insert('', 'end', iid=iid, text=' ' + name + (' [failed]' if failed else ''),
                                image=self.icons[int(included)],
                                tags=('failed',) if failed else (() if included else ('excluded',)))
        if focus and self.preview.exists(focus):
            self.preview.focus(focus)
            self.preview.selection_set(focus)
            self.preview.yview_moveto(scroll_position)
        included_count = sum(t['offset'] not in self.excluded_tracks for t in f['audio_tracks'])
        self.track_label.set(f'Audio tracks ({included_count}/{len(f["audio_tracks"])} checked)')

    def click_track(self, event):
        row = self.preview.identify_row(event.y)
        if row and 'image' in self.preview.identify_element(event.x, event.y):
            self.toggle_track(row)

    def toggle_focused_track(self, event=None):
        self.toggle_track(self.preview.focus())
        return 'break'

    def toggle_track(self, iid):
        if self.busy or iid not in self.preview_tracks:
            return
        offset = self.preview_tracks[iid]['offset']
        self.preview.focus(iid)
        if offset in self.excluded_tracks:
            self.excluded_tracks.remove(offset)
        else:
            self.excluded_tracks.add(offset)
        self.render_tracks()

    def clear_songs(self):
        if not self.busy:
            self.selected.clear()
            self.render()

    def select_failed_songs(self):
        if self.busy or not self.reader:
            return
        self.selected = {fid for fid in self.failed_songs if fid in self.by_id
                         and self.by_id[fid]['direct_audio_track_count']}
        self.search.set('')
        self.render()
        if self.selected:
            first = next(fid for fid in self.by_id if fid in self.selected)
            self.tree.focus(first)
            self.tree.selection_set(first)
            self.tree.see(first)
            self.focus_folder()
        self.status.set(f'{len(self.selected)} failed songs selected. Uncheck tracks to exclude them before retrying.')

    def import_failed_summary(self, summary):
        if not self.reader:
            raise ValueError('Open the Cubase project before loading its export summary.')
        if not isinstance(summary, dict) or not isinstance(summary.get('failed'), list):
            raise ValueError('Choose an export-summary JSON file.')
        source = summary.get('source')
        if not isinstance(source, str) or Path(source).resolve() != self.reader.path.resolve():
            raise ValueError('This export summary belongs to a different Cubase project.')
        digest = summary.get('source_sha256')
        if digest and digest != self.reader.inventory['source_sha256']:
            raise ValueError('The project has changed since this summary was created. Export again to identify current failures.')
        failures = {}
        for failure in summary['failed']:
            if not isinstance(failure, dict) or not isinstance(failure.get('error'), str):
                raise ValueError('The export summary contains an invalid failure entry.')
            matches = [fid for fid, f in self.by_id.items() if f['name'] == failure.get('song')
                       and ('folder_path' not in failure or f['path'] == failure['folder_path'])]
            if len(matches) != 1:
                raise ValueError(f'Cannot uniquely match failed song: {failure.get("song", "(unnamed)")}')
            fid = matches[0]
            record = dict(failure)
            tracks = self.by_id[fid]['audio_tracks']
            matching_tracks = [t for t in tracks if t['offset'] == record.get('track_offset')]
            if not matching_tracks:
                matching_tracks = [t for t in tracks if record['error'].startswith(
                    f'{self.by_id[fid]["name"]} / {t["name"]}:')]
            if len(matching_tracks) == 1:
                record['track_offset'] = matching_tracks[0]['offset']
            else:
                record.pop('track_offset', None)
            failures[fid] = record
        self.failed_songs = failures
        self.render_tracks()
        self.select_failed_songs()

    def load_failed_summary(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(title='Load failed songs from an export summary',
                                         initialdir=self.output_path.get() or '.',
                                         filetypes=[('Export summaries', 'export-summary-*.json'), ('JSON files', '*.json')])
        if path:
            try:
                self.import_failed_summary(json.loads(Path(path).read_text(encoding='utf-8-sig')))
            except (ValueError, OSError) as exc:
                messagebox.showerror('Cannot load failed songs', str(exc))

    def change_bpm(self, *_):
        if self.setting_bpm or self.busy or self.editing is None:
            return
        self.bpms[self.editing] = self.bpm.get().strip()
        if self.tree.exists(self.editing):
            self.tree.set(self.editing, 'bpm', self.bpms[self.editing] or '?')

    def set_busy(self, busy):
        self.busy = busy
        for button in self.buttons:
            button.configure(state='disabled' if busy else 'normal')
        if not busy:
            self.progress.stop()
            self.progress.configure(mode='determinate')
            self.cancel_button.configure(state='disabled')
        if self.editing in self.by_id:
            self.bpm_entry.configure(state='normal' if not busy and self.by_id[self.editing]['direct_audio_track_count'] else 'disabled')
        self.update_count()

    def export(self):
        if self.busy or not self.reader or not self.selected:
            return
        songs = []
        try:
            stat = self.reader.path.stat()
            if (stat.st_size, stat.st_mtime_ns) != self.source_stamp:
                raise ValueError('The .cpr changed after it was loaded. Open it again before exporting.')
            for fid in self.by_id:
                if fid not in self.selected:
                    continue
                f = self.by_id[fid]
                try:
                    bpm = float(self.bpms[fid])
                    if not 1 <= bpm <= 1000:
                        raise ValueError()
                except ValueError:
                    raise ValueError(f'Enter a valid BPM for {f["name"]} in the right panel.')
                parent = f['parent_id']
                while parent:
                    if parent in self.selected:
                        raise ValueError(f'Select either {f["name"]} or its parent song, not both.')
                    parent = self.by_id[parent]['parent_id']
                songs.append((fid, bpm))
            if not self.output_path.get():
                raise ValueError('Choose an output folder.')
        except (ValueError, OSError) as exc:
            messagebox.showerror('Cannot export yet', str(exc))
            return
        output_dir = Path(self.output_path.get())
        extra = self.extra_path.get() or None
        allow_silent_tails = self.allow_silent_tails.get()
        self.cancel.clear()
        self.set_busy(True)
        self.cancel_button.configure(state='normal')
        self.progress['value'] = 0
        reader = self.reader
        excluded_tracks = frozenset(self.excluded_tracks)
        def worker():
            completed, failed = [], []
            try:
                output_dir.mkdir(parents=True, exist_ok=True)
                for index, (fid, bpm) in enumerate(songs):
                    if self.cancel.is_set():
                        break
                    name = self.by_id[fid]['name']
                    self.events.put(('status', f'Checking {name}…'))
                    try:
                        plan = prepare_song(reader, fid, bpm, extra, self.cancel, excluded_tracks, allow_silent_tails=allow_silent_tails)
                        output = unique_output(output_dir, name)
                        previous = [0.0]
                        def progress(done, total, media_name):
                            now = time.monotonic()
                            if now - previous[0] > .15 or done == total:
                                previous[0] = now
                                self.events.put(('progress', 100 * (index + done / max(total, 1)) / len(songs),
                                                 f'{name}: {done/1024**2:.0f} / {total/1024**2:.0f} MB'))
                        export_song(plan, output, self.cancel, progress, lambda s: self.events.put(('status', s)))
                        completed.append(str(output))
                        self.events.put(('song_succeeded', fid))
                        self.events.put(('log', f'EXPORTED  {output.name}'))
                    except Exception as exc:
                        if self.cancel.is_set():
                            break
                        failure = dict(song=name, folder_path=self.by_id[fid]['path'], error=str(exc))
                        if hasattr(exc, 'track_offset'):
                            failure.update(track_offset=exc.track_offset, track_path=exc.track_path)
                        failed.append(failure)
                        self.events.put(('song_failed', fid, failure))
                        self.events.put(('log', f'NOT EXPORTED  {name}\n  {exc}'))
                summary = dict(source=str(reader.path), source_sha256=reader.inventory['source_sha256'],
                               completed=completed, failed=failed, cancelled=self.cancel.is_set(),
                               allow_silent_tails=allow_silent_tails,
                               excluded_tracks=[dict(path=t['path'], offset=t['offset'])
                                                for fid, _ in songs for t in self.by_id[fid]['audio_tracks']
                                                if t['offset'] in excluded_tracks])
                report = output_dir / ('export-summary-' + time.strftime('%Y%m%d-%H%M%S') + f'-{time.time_ns() % 10**9:09d}.json')
                report.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
                self.events.put(('done', len(completed), len(failed), self.cancel.is_set()))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == 'loaded': self.loaded(event[1], event[2])
                elif event[0] == 'status': self.status.set(event[1])
                elif event[0] == 'progress':
                    self.progress['value'] = event[1]
                    self.status.set(event[2])
                elif event[0] == 'log': self.append_log(event[1])
                elif event[0] == 'song_failed':
                    self.failed_songs[event[1]] = event[2]
                    self.render_tracks()
                elif event[0] == 'song_succeeded':
                    self.failed_songs.pop(event[1], None)
                    self.render_tracks()
                elif event[0] == 'error':
                    self.set_busy(False)
                    self.status.set('Could not complete the operation.')
                    self.append_log(event[1])
                    messagebox.showerror('Song Exporter', event[1])
                elif event[0] == 'done':
                    self.set_busy(False)
                    self.progress['value'] = 0 if event[3] else 100
                    message = f'{event[1]} songs exported; {event[2]} could not be exported.'
                    if event[3]: message += ' Cancelled.'
                    self.status.set(message)
                    self.append_log(message)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def toggle_log(self):
        if self.log_frame.winfo_manager():
            self.log_frame.pack_forget()
            self.log_button.configure(text='Show log')
        else:
            self.log_frame.pack(fill='x', pady=(5, 0))
            self.log_button.configure(text='Hide log')

    def append_log(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', text + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def open_output(self):
        path = Path(self.output_path.get())
        if self.output_path.get() and path.is_dir():
            os.startfile(str(path))

    def close(self):
        if self.busy:
            self.cancel.set()
            self.status.set('Stopping safely… close the window again after the operation finishes.')
        else:
            self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument('--inspect', type=Path, help='Headless packaged-app verification')
    parser.add_argument('--smoke-ui', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--export', dest='export_project', type=Path)
    parser.add_argument('--folder')
    parser.add_argument('--bpm', type=float)
    parser.add_argument('--allow-silent-tails', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.inspect or args.smoke_ui:
        reader = Reader(args.inspect) if args.inspect else None
        layout_checks = []
        if args.smoke_ui:
            from types import SimpleNamespace
            root = tk.Tk()
            app = App(root)
            if reader:
                smoke_reader = reader
                stamp = reader.path.stat()
                source_stamp = (stamp.st_size, stamp.st_mtime_ns)
            else:
                smoke_folder = dict(id='smoke', parent_id=None, name='Song120', path=['Song120'],
                                    suggested_bpm=120, direct_audio_track_count=2, audio_track_count=2,
                                    audio_tracks=[dict(name=f'Track {i}', path=['Song120', f'Track {i}'], offset=i)
                                                  for i in (1, 2)])
                smoke_reader = SimpleNamespace(path=Path('smoke.cpr'), inventory=dict(
                    folders=[smoke_folder], audio_track_count=2))
                source_stamp = (0, 0)
            app.loaded(smoke_reader, source_stamp)
            first = next(f for f in smoke_reader.inventory['folders'] if f['direct_audio_track_count'])
            app.toggle(first['id'])
            assert first['id'] in app.selected
            assert app.tree.exists(first['id'])
            app.tree.selection_set(first['id'])
            app.focus_folder()
            track = first['audio_tracks'][0]
            app.toggle_track(str(track['offset']))
            assert track['offset'] in app.excluded_tracks
            app.failed_songs[first['id']] = dict(error='smoke test', track_offset=track['offset'])
            app.clear_songs()
            assert not app.selected
            app.select_failed_songs()
            assert app.selected == {first['id']}
            assert track['offset'] in app.excluded_tracks
            for size in ('1160x820', '900x650'):
                root.geometry(size + '+10000+10000')
                root.update()
                button = app.export_button
                y = button.winfo_rooty() - root.winfo_rooty()
                assert button.winfo_ismapped(), f'Export button hidden at {size}'
                assert 0 <= y < y + button.winfo_height() <= root.winfo_height()
                assert app.preview.winfo_ismapped(), f'Track checkboxes hidden at {size}'
                assert app.retry_button.winfo_ismapped(), f'Retry menu hidden at {size}'
                layout_checks.append(dict(window=size, export_button_visible=True,
                                          track_checkboxes_visible=True, retry_menu_visible=True))
            root.destroy()
        if args.report:
            report = reader.inventory if reader else dict(version=VERSION, layout_checks=layout_checks,
                                                         track_exclusion_verified=True, retry_selection_verified=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        return
    if args.export_project:
        reader = Reader(args.export_project)
        plan = prepare_song(reader, args.folder, args.bpm, allow_silent_tails=args.allow_silent_tails)
        export_song(plan, args.output)
        return
    if os.name == 'nt':
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
