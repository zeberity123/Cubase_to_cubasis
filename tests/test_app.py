import tkinter as tk
from types import SimpleNamespace
import unittest

from song_exporter_app import App


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
                              audio_track_count=1, audio_tracks=[])
                app.loaded(SimpleNamespace(path='example.cpr', inventory=dict(
                    folders=[folder], audio_track_count=1)), (0, 0))
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


if __name__ == '__main__':
    unittest.main()
