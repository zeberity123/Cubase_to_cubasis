import tkinter as tk
from tkinter import ttk
import tempfile
import json
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from song_exporter_app import App
from cpr_export import SongTrackError


class AppLayoutTests(unittest.TestCase):
    def test_export_visible_and_selectable_at_small_sizes_and_display_scales(self):
        root = tk.Tk()
        try:
            for scaling in (1.33, 2.0, 2.67):
                root.tk.call('tk', 'scaling', scaling)
                app = App(root)
                folder = dict(id='song', parent_id=None, name='Song120',
                              path=['Song120'], suggested_bpm=120,
                              bpm_note='From folder name', direct_audio_track_count=1,
                              audio_track_count=1, audio_tracks=[dict(
                                  name='Vocal', path=['Song120', 'Vocal'], offset=1)])
                app.loaded(SimpleNamespace(path='example.cpr', inventory=dict(
                    folders=[folder], audio_track_count=1)), (0, 0))
                app.tree.selection_set('song')
                app.focus_folder()
                for size in ('1160x820', '900x650'):
                    with self.subTest(scaling=scaling, size=size):
                        root.geometry(size + '+10000+10000')
                        root.update()
                        button = app.export_button
                        self.assertTrue(button.winfo_ismapped())
                        x = button.winfo_rootx() - root.winfo_rootx()
                        y = button.winfo_rooty() - root.winfo_rooty()
                        self.assertGreaterEqual(x, 0)
                        self.assertGreaterEqual(y, 0)
                        self.assertLessEqual(x + button.winfo_width(), root.winfo_width())
                        self.assertLessEqual(y + button.winfo_height(), root.winfo_height())
                        self.assertTrue(app.retry_button.winfo_ismapped())
                        self.assertTrue(app.preview.winfo_ismapped())
                        self.assertGreaterEqual(app.preview.winfo_height(),
                                                int(ttk.Style(root).lookup('Treeview', 'rowheight')) + 2)
                        self.assertTrue(button.instate(['disabled']))
                        app.toggle('song')
                        self.assertTrue(button.instate(['!disabled']))
                        app.set_busy(True)
                        self.assertTrue(button.instate(['disabled']))
                        app.set_busy(False)
                        app.toggle('song')
                for after_id in root.tk.call('after', 'info'):
                    root.after_cancel(after_id)
                for widget in root.winfo_children():
                    widget.destroy()
        finally:
            root.destroy()


class AppRetryTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.tk.call('tk', 'scaling', 1.33)
        self.root.geometry('1160x820+10000+10000')
        self.app = App(self.root)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / 'example.cpr'
        self.source.write_bytes(b'fixture')
        self.folders = []
        for i in range(2):
            name = f'Song{i}120'
            self.folders.append(dict(id=f'song{i}', parent_id=None, name=name,
                path=[name], suggested_bpm=120, bpm_note='From folder name',
                direct_audio_track_count=2, audio_track_count=2,
                audio_tracks=[dict(name='Audio', path=[name, f'Audio{j}'], offset=i*10+j)
                              for j in range(2)]))
        self.reader = SimpleNamespace(path=self.source, inventory=dict(
            folders=self.folders, audio_track_count=4, source_sha256='fixture'))
        stamp = self.source.stat()
        self.app.loaded(self.reader, (stamp.st_size, stamp.st_mtime_ns))
        self.app.output_path.set(self.temp.name)
        self.focus('song0')
        self.root.update()

    def tearDown(self):
        for after_id in self.root.tk.call('after', 'info'):
            self.root.after_cancel(after_id)
        self.root.destroy()

    def focus(self, fid):
        self.app.tree.focus(fid)
        self.app.tree.selection_set(fid)
        self.app.focus_folder()

    def test_track_checks_persist_across_folders_and_busy_blocks_changes(self):
        self.app.toggle_track('1')
        self.assertEqual(self.app.excluded_tracks, {1})
        self.focus('song1')
        self.app.toggle_track('11')
        self.focus('song0')
        self.assertEqual(self.app.preview.item('1', 'image')[0], str(self.app.icons[0]))
        self.app.set_busy(True)
        self.app.toggle_track('1')
        self.assertEqual(self.app.excluded_tracks, {1, 11})
        self.app.set_busy(False)
        self.app.toggle_track('1')
        self.assertEqual(self.app.excluded_tracks, {11})

    def test_import_old_summary_clears_selection_and_selects_failed_songs(self):
        self.app.toggle('song0')
        self.app.toggle_track('1')
        self.app.search.set('Song0')
        summary = dict(source=str(self.source), failed=[dict(song=self.folders[1]['name'],
                       error=f'{self.folders[1]["name"]} / Audio: unsupported event type MAudioPartEvent')])
        self.app.import_failed_summary(summary)
        self.assertEqual(self.app.selected, {'song1'})
        self.assertEqual(self.app.search.get(), '')
        self.assertEqual(self.app.excluded_tracks, {1})
        # Duplicate track names must not cause an arbitrary track to be marked.
        self.assertNotIn('track_offset', self.app.failed_songs['song1'])
        self.app.clear_songs()
        self.assertEqual(self.app.selected, set())
        self.app.select_failed_songs()
        self.assertEqual(self.app.selected, {'song1'})

    def test_import_new_summary_marks_exact_track_and_rejects_wrong_source(self):
        failure = dict(song=self.folders[0]['name'], folder_path=self.folders[0]['path'],
                       track_offset=1, error='unsupported event type')
        summary = dict(source=str(self.source), source_sha256='fixture', failed=[failure])
        self.app.import_failed_summary(summary)
        self.assertIn('failed', self.app.preview.item('1', 'tags'))
        self.assertNotIn('failed', self.app.preview.item('0', 'tags'))
        for updates in (dict(source=str(self.source.with_name('other.cpr'))), dict(source_sha256='changed')):
            with self.assertRaises(ValueError):
                self.app.import_failed_summary(dict(summary, **updates))
            self.assertEqual(self.app.selected, {'song0'})

    def test_export_snapshots_exclusions_tracks_failures_and_clears_successful_retry(self):
        self.app.toggle('song0')
        self.app.toggle_track('1')
        failure = SongTrackError(self.folders[0], self.folders[0]['audio_tracks'][0], 'unsupported edit')
        with patch('song_exporter_app.prepare_song', side_effect=failure) as prepare:
            self.app.export()
            self.wait_until_idle()
            self.assertEqual(prepare.call_args.args[-1], frozenset({1}))
        self.assertEqual(self.app.failed_songs['song0']['track_offset'], 0)
        self.assertIn('failed', self.app.preview.item('0', 'tags'))
        report = json.loads(next(Path(self.temp.name).glob('export-summary-*.json')).read_text(encoding='utf-8'))
        self.assertEqual(report['failed'][0]['track_offset'], 0)
        self.assertEqual(report['excluded_tracks'][0]['offset'], 1)
        self.app.select_failed_songs()
        with patch('song_exporter_app.prepare_song', return_value=object()), patch('song_exporter_app.export_song'):
            self.app.export()
            self.wait_until_idle()
        self.assertEqual(self.app.failed_songs, {})
        self.assertNotIn('failed', self.app.preview.item('0', 'tags'))

    def wait_until_idle(self):
        deadline = time.monotonic() + 5
        while self.app.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertFalse(self.app.busy)


if __name__ == '__main__':
    unittest.main()
