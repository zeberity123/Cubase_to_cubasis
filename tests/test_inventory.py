import json
import os
from pathlib import Path
import tempfile
import unittest

from cpr_inventory import InventoryError, inspect_project, suggest_tempo, write_selector


class InventoryTests(unittest.TestCase):
    def test_tempo_suffix_and_decimals(self):
        self.assertEqual(suggest_tempo('song165'), (165.0, 'From folder name'))
        self.assertEqual(suggest_tempo('Song137.5'), (137.5, 'From folder name'))
        self.assertEqual(suggest_tempo('39_175')[0], 175)
        self.assertEqual(suggest_tempo('テスト4/137')[0], 137)

    def test_dates_and_ambiguous_tempos_are_not_guessed(self):
        self.assertIsNone(suggest_tempo('テスト2019')[0])
        self.assertIsNone(suggest_tempo('250913テスト')[0])
        self.assertIsNone(suggest_tempo('song120-140')[0])
        self.assertEqual(suggest_tempo('song190/3bar4'), (190.0, 'Review annotation'))

    def test_invalid_container_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'bad.cpr'
            p.write_bytes(b'not a Cubase file')
            with self.assertRaises(InventoryError):
                inspect_project(p)

    def test_selector_escapes_script_end_in_names(self):
        inventory = {'folders': [{'name': '</script><script>alert(1)</script>'}]}
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'selector.html'
            write_selector(inventory, p)
            page = p.read_text(encoding='utf-8')
            self.assertNotIn('</script><script>alert(1)', page)
            payload = page.split('type="application/json">')[1].split('</script>')[0]
            self.assertEqual(json.loads(payload), inventory)

    def test_private_project_hierarchy(self):
        fixture = os.environ.get('CUBASIS_PRIVATE_CPR')
        if not fixture:
            self.skipTest('Set CUBASIS_PRIVATE_CPR to the original private regression fixture')
        source = Path(fixture)
        if not source.exists():
            self.skipTest('Private source project is unavailable')
        # inspect_project verifies every decoded folder's child count itself.
        inventory = inspect_project(source)
        self.assertEqual(inventory['folder_count'], 137)
        self.assertEqual(inventory['audio_track_count'], 1435)
        sample_song = next(f for f in inventory['folders'] if f['id'] == 'F032')
        self.assertEqual(sample_song['suggested_bpm'], 165)
        self.assertEqual(sample_song['audio_track_count'], 14)
        self.assertIn('Vo-01', [t['name'] for t in sample_song['audio_tracks']])


if __name__ == '__main__':
    unittest.main()
