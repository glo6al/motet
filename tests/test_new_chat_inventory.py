"""Fictional metadata/directory proof boundaries; no provider operations."""
from dataclasses import replace
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from new_chat_inventory import KnownFolderInventory, validate_folder
from new_chat_types import NewChatError, VerifiedIdentity

NATIVE = '11111111-1111-4111-8111-111111111111'


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.folder = self.root / 'fictional-project'
        self.folder.mkdir()
        self.source = self.root / 'native.jsonl'
        self.source.write_text('{}\n')
        st = self.source.stat()
        self.identity = VerifiedIdentity(NATIVE, str(self.folder), str(self.source), st.st_dev, st.st_ino, 'fixture-v1')
        self.rows = [self.identity]
        self.inventory = KnownFolderInventory(lambda: self.rows, b'a' * 32)

    def test_only_verified_identity_type_and_pinned_regular_source(self):
        self.rows = [{'cwd': str(self.folder)}, replace(self.identity, source_inode=1),
                     replace(self.identity, native_id='not-a-native-id'), self.identity]
        rows = self.inventory.refresh()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].path, str(self.folder))
        self.assertEqual(self.inventory.snapshot()[0]['provenance'][0]['native_id'], NATIVE)

    def test_distinct_same_named_folders_have_distinct_ids(self):
        other = self.root / 'other' / self.folder.name
        other.mkdir(parents=True)
        self.rows.append(replace(self.identity, cwd=str(other)))
        rows = self.inventory.refresh()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].label, rows[1].label)
        self.assertNotEqual(rows[0].id, rows[1].id)

    def test_stable_ids_and_revision_not_random_per_refresh(self):
        first = self.inventory.refresh()[0]
        self.assertEqual(self.inventory.refresh()[0], first)
        self.assertEqual(self.inventory.get(first.id, first.revision), first)

    def test_replacement_rejects_prepared_folder(self):
        first = self.inventory.refresh()[0]
        self.folder.rename(self.root / 'old-project')
        self.folder.mkdir()
        with self.assertRaises(NewChatError):
            validate_folder(first)
        with self.assertRaises(NewChatError):
            self.inventory.get(first.id, first.revision)

    @unittest.skipIf(os.name == 'nt', 'Creating directory symlinks requires Windows privileges.')
    def test_symlink_resolves_to_shown_destination_and_retarget_retires_entry(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.folder, target_is_directory=True)
        self.rows = [replace(self.identity, cwd=str(alias))]
        first = self.inventory.refresh()[0]
        self.assertEqual(first.path, str(self.folder))
        other = self.root / 'other'
        other.mkdir()
        alias.unlink()
        alias.symlink_to(other, target_is_directory=True)
        with self.assertRaises(NewChatError):
            self.inventory.get(first.id, first.revision)

    @unittest.skipIf(os.name == 'nt', 'Creating file symlinks requires Windows privileges.')
    def test_source_replacement_or_symlink_cannot_preserve_inventory(self):
        self.inventory.refresh()
        self.source.rename(self.root / 'old-native.jsonl')
        self.source.symlink_to(self.root / 'old-native.jsonl')
        self.assertEqual(self.inventory.refresh(), [])

    def test_incomplete_oversize_or_failed_source_retires_cached_entries(self):
        self.inventory.maximum = 1
        self.assertEqual(len(self.inventory.refresh()), 1)
        self.rows = [self.identity, self.identity]
        self.assertEqual(self.inventory.refresh(), [])
        self.inventory.source = lambda: iter([self.identity, self.identity])
        self.assertEqual(self.inventory.refresh(), [])


if __name__ == '__main__':
    unittest.main()
