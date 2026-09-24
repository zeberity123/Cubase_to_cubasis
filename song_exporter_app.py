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

from cpr_native import Reader
from cpr_export import prepare_song, export_song, unique_output


APP_NAME = 'Cubase to Cubasis — Song Exporter'
VERSION = '0.3.0'


class App:
    def __init__(self, root):
        self.root = root
        self.reader = None
        self.source_stamp = None
        self.selected = set()
        self.bpms = {}
        self.by_id = {}
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.editing = None
        self.setting_bpm = False
        root.title(APP_NAME)
        root.geometry('1160x820')
        root.minsize(900, 650)
        root.configure(bg='#f2f5f8')
        root.protocol('WM_DELETE_WINDOW', self.close)
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('.', font=('Segoe UI', 10), background='#f2f5f8', foreground='#1b293b')
        style.configure('Treeview', rowheight=29, background='white', fieldbackground='white', font=('Segoe UI', 10))
        style.configure('Treeview.Heading', font=('Segoe UI', 10, 'bold'), background='#e5ecf3')
        style.configure('Accent.TButton', background='#176b75', foreground='white', padding=(18, 9), font=('Segoe UI', 10, 'bold'))
        style.map('Accent.TButton', background=[('active', '#208594'), ('disabled', '#9eaeb7')])
        style.configure('TButton', padding=(10, 6))
        self.project_path = tk.StringVar()
        self.output_path = tk.StringVar()
        self.extra_path = tk.StringVar()
        self.search = tk.StringVar()
        self.bpm = tk.StringVar()
        self.status = tk.StringVar(value='Open a saved Cubase project to begin.')
        self.summary = tk.StringVar(value='No project loaded')
        self.selection_label = tk.StringVar(value='0 songs selected')
        self.title = tk.StringVar(value='Select a song folder')
        self.detail = tk.StringVar(value='Parent folders stay visible in the tree.')
        frame = ttk.Frame(root, padding=20)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Cubase → Cubasis', font=('Segoe UI', 23, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='Export selected song folders as separate projects', font=('Segoe UI', 11)).pack(anchor='w', pady=(1, 14))
        self.buttons = []
        self.path_row(frame, 'Cubase project', self.project_path, self.browse_project, 'Open .cpr…')
        self.path_row(frame, 'Output folder', self.output_path, self.browse_output, 'Browse…')
        self.path_row(frame, 'Extra audio folder', self.extra_path, self.browse_extra, 'Optional…')
        ttk.Label(frame, text='Audio only · Original clip positions · 4/4 · No mixer settings, plug-ins or MIDI', foreground='#526578').pack(anchor='w', pady=(8, 4))
        ttk.Label(frame, text='This version stops a song if it contains unsupported fades, envelopes or audio parts; it never silently drops those clips.',
                  foreground='#805421', wraplength=1050).pack(anchor='w', pady=(0, 8))
        bar = ttk.Frame(frame)
        bar.pack(fill='x', pady=(0, 8))
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
        ttk.Label(right, textvariable=self.title, font=('Segoe UI', 13, 'bold'), wraplength=280).pack(anchor='w')
        ttk.Label(right, textvariable=self.detail, wraplength=280, foreground='#536579').pack(anchor='w', pady=(6, 12))
        ttk.Label(right, text='Song BPM').pack(anchor='w')
        self.bpm_entry = ttk.Entry(right, textvariable=self.bpm, width=16)
        self.bpm_entry.pack(anchor='w', pady=(4, 6))
        self.bpm.trace_add('write', self.change_bpm)
        ttk.Label(right, text='Check values inferred from folder names.\nA selected song includes its nested audio tracks.', wraplength=280).pack(anchor='w', pady=(0, 12))
        ttk.Label(right, text='Audio tracks', font=('Segoe UI', 10, 'bold')).pack(anchor='w')
        self.preview = tk.Text(right, width=30, height=10, wrap='word', relief='flat', bg='white',
                               fg='#263b4f', font=('Segoe UI', 9), padx=8, pady=8, state='disabled')
        self.preview.pack(fill='both', expand=True, pady=(5, 0))
        bottom = ttk.Frame(frame)
        bottom.pack(fill='x', pady=(12, 7))
        ttk.Label(bottom, textvariable=self.selection_label).pack(side='left')
        self.export_button = ttk.Button(bottom, text='Export selected songs', style='Accent.TButton', command=self.export, state='disabled')
        self.export_button.pack(side='right')
        self.cancel_button = ttk.Button(bottom, text='Cancel', command=self.cancel.set, state='disabled')
        self.cancel_button.pack(side='right', padx=8)
        self.open_button = ttk.Button(bottom, text='Open output folder', command=self.open_output)
        self.open_button.pack(side='right')
        self.progress = ttk.Progressbar(frame, mode='determinate', maximum=100)
        self.progress.pack(fill='x')
        ttk.Label(frame, textvariable=self.status, wraplength=1080).pack(anchor='w', pady=(5, 2))
        self.log = tk.Text(frame, height=5, wrap='word', font=('Consolas', 9), relief='flat', bg='#e6edf3', padx=8, pady=6, state='disabled')
        self.log.pack(fill='x', pady=(5, 0))
        ttk.Label(frame, textvariable=self.summary, foreground='#65778a').pack(anchor='w', pady=(6, 0))
        root.after(100, self.poll)

    def checkbox(self, checked):
        image = tk.PhotoImage(width=18, height=18)
        image.put('#6e849b', to=(1, 1, 17, 17))
        image.put('#176b75' if checked else '#ffffff', to=(2, 2, 16, 16))
        if checked:
            for x, y in [(4, 8), (5, 9), (6, 10), (7, 11), (8, 10), (9, 9), (10, 8), (11, 7), (12, 6), (13, 5)]:
                image.put('white', to=(x, y, x+2, y+2))
        return image

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
        if not self.output_path.get():
            self.output_path.set(str(Path.home() / 'Desktop' / 'Cubasis Exports'))
        self.selected.clear()
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
        self.detail.set(' / '.join(f['path'][:-1]) + '\n' + f['bpm_note'])
        self.setting_bpm = True
        self.bpm.set(self.bpms[self.editing])
        self.setting_bpm = False
        self.bpm_entry.configure(state='normal' if f['direct_audio_track_count'] and not self.busy else 'disabled')
        self.preview.configure(state='normal')
        self.preview.delete('1.0', 'end')
        for t in f['audio_tracks']:
            self.preview.insert('end', ' / '.join(t['path'][len(f['path']):]) + '\n')
        self.preview.configure(state='disabled')

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
        self.cancel.clear()
        self.set_busy(True)
        self.cancel_button.configure(state='normal')
        self.progress['value'] = 0
        reader = self.reader
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
                        plan = prepare_song(reader, fid, bpm, extra, self.cancel)
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
                        self.events.put(('log', f'EXPORTED  {output.name}'))
                    except Exception as exc:
                        if self.cancel.is_set():
                            break
                        failed.append(dict(song=name, error=str(exc)))
                        self.events.put(('log', f'NOT EXPORTED  {name}\n  {exc}'))
                summary = dict(source=str(reader.path), completed=completed, failed=failed, cancelled=self.cancel.is_set())
                report = output_dir / ('export-summary-' + time.strftime('%Y%m%d-%H%M%S') + '.json')
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
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.inspect:
        reader = Reader(args.inspect)
        if args.smoke_ui:
            root = tk.Tk()
            root.withdraw()
            app = App(root)
            stamp = reader.path.stat()
            app.loaded(reader, (stamp.st_size, stamp.st_mtime_ns))
            first = next(f for f in reader.inventory['folders'] if f['direct_audio_track_count'])
            app.toggle(first['id'])
            assert first['id'] in app.selected
            assert app.tree.exists(first['id'])
            root.update_idletasks()
            root.destroy()
        if args.report:
            args.report.write_text(json.dumps(reader.inventory, ensure_ascii=False, indent=2), encoding='utf-8')
        return
    if args.export_project:
        reader = Reader(args.export_project)
        plan = prepare_song(reader, args.folder, args.bpm)
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
