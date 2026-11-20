"""Durable New chat behavior using injected fictional native identities only."""
from dataclasses import replace
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from new_chat_service import NewChatService, RETENTION
from new_chat_types import Capability, NewChatError, OpenResult, VerifiedIdentity


def wait_for(fn):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if fn():
            return
        time.sleep(.005)
    raise AssertionError('Fictional worker did not settle.')


class Clocks:
    def __init__(self):
        self.wall, self.mono = 1900000000., 1000.

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


class FakeAdapter:
    def __init__(self, root, folder):
        self.root, self.folder = root, folder
        self.preflights, self.creates, self.opens, self.verifies = 0, 0, 0, 0
        self.version, self.available = 'fixture-v1', True
        self.identities = {}
        self.create_entered, self.create_release = threading.Event(), threading.Event()
        self.create_release.set()
        self.open_result = OpenResult('accepted')
        self.after_receipt = None
        self.before_receipt = None
        self.wrong_folder = None
        self.closed = False

    def make_identity(self, folder=None):
        folder = folder or self.folder
        ident = str(uuid.uuid4())
        source = self.root / (ident + '.jsonl')
        source.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': ident, 'cwd': str(folder)}}) + '\n')
        st, directory = source.stat(), folder.stat()
        identity = VerifiedIdentity(ident, str(folder), str(source), st.st_dev, st.st_ino,
                                    self.version, directory.st_dev, directory.st_ino)
        self.identities[ident] = identity
        return identity

    def preflight(self):
        self.preflights += 1
        return Capability(self.available, 'native_defaults_unverified' if not self.available else '', version=self.version)

    def create_once(self, folder, callback):
        self.creates += 1
        self.create_entered.set()
        self.create_release.wait(3)
        if self.before_receipt:
            self.before_receipt()
        identity = self.make_identity(self.wrong_folder or Path(folder.path))
        callback(identity.native_id, identity.cwd)
        if self.after_receipt:
            self.after_receipt(identity)
        return identity

    def verify_existing(self, native_id, folder):
        self.verifies += 1
        return self.identities.get(native_id)

    def open_existing(self, identity, draft):
        self.opens += 1
        return self.open_result

    def close(self):
        self.closed = True
        self.create_release.set()


class DatabaseProbe:
    def __init__(self, db, execute=None, committed=None):
        self.db, self.before_execute, self.committed = db, execute, committed

    def __getattr__(self, name):
        return getattr(self.db, name)

    def execute(self, sql, *args):
        if self.before_execute:
            override = self.before_execute(sql, *args)
            if override is not None:
                return override
        return self.db.execute(sql, *args)

    def __enter__(self):
        self.db.__enter__()
        return self

    def __exit__(self, *args):
        result = self.db.__exit__(*args)
        if args[0] is None and self.committed:
            self.committed()
        return result


class LegacyTransactionProbe(DatabaseProbe):
    """Exercise pre-fix sqlite3 context-manager COMMIT failure semantics."""
    def __exit__(self, kind, value, traceback):
        if kind is None:
            self.db.commit()
        else:
            self.db.rollback()


@unittest.skipIf(os.name == 'nt', 'New chat uses private POSIX storage; Windows native launch is unavailable.')
class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.folder = self.root / 'fictional-project'
        self.folder.mkdir()
        self.adapter = FakeAdapter(self.root, self.folder)
        self.seed = self.adapter.make_identity()
        self.clocks = Clocks()
        self.services = []
        self.addCleanup(self.cleanup_services)

    def cleanup_services(self):
        for service in self.services:
            service.close()

    def service(self, **kwargs):
        s = NewChatService(self.root / 'private-state', self.adapter, lambda: [self.seed], home=self.root,
                           enabled=kwargs.pop('enabled', True), clock=lambda: self.clocks.mono,
                           wall=lambda: self.clocks.wall, **kwargs)
        self.services.append(s)
        return s

    def request(self, service):
        folder = service.context()['folders'][0]
        selection = {'provider': 'codex', 'folderId': folder['id'], 'folderRevision': folder['revision']}
        intent = service.prepare(selection)
        return dict(selection, requestId=intent['requestId'])

    def settle(self, s):
        wait_for(lambda: not s.jobs)

    def test_disabled_and_demo_short_circuit_all_native_reads_and_creation(self):
        for kwargs in ({'enabled': False}, {'demo': True}):
            s = self.service(**kwargs)
            self.assertEqual(s.context()['folders'], [])
            self.assertFalse(s.context()['providers'][0]['available'])
            with self.assertRaises(NewChatError):
                s.prepare({'provider': 'codex', 'folderId': 'fake', 'folderRevision': 'fake'})
            with self.assertRaises(NewChatError) as refused:
                s.register_folder({'path': str(self.folder)})
            self.assertIn(refused.exception.code, ('disabled', 'demo_unavailable'))
            s.close()
        self.assertEqual((self.adapter.preflights, self.adapter.verifies, self.adapter.creates), (0, 0, 0))

    def test_registered_folder_survives_restart_and_discovery_failure(self):
        s = self.service()
        new = self.root / 'first-project'
        new.mkdir()
        alias = self.root / 'alias'
        alias.symlink_to(new, target_is_directory=True)
        saved = s.register_folder({'path': str(alias)})['folder']
        self.assertEqual(saved['displayPath'], str(new))
        self.assertEqual(s.register_folder({'path': str(new)})['folder'], saved)
        self.assertIn(saved, s.context()['folders'])
        self.assertEqual(self.adapter.creates, 0)
        s.close()

        s = self.service()
        def offline():
            raise OSError('fixture discovery unavailable')
        s.inventory.source = offline
        self.assertEqual(s.context()['folders'], [saved])
        selection = {'provider': 'codex', 'folderId': saved['id'], 'folderRevision': saved['revision']}
        intent = s.prepare(selection)
        new.rename(self.root / 'old-project')
        new.mkdir()
        self.assertEqual(s.context()['folders'], [])
        self.assertEqual(s.db.execute('SELECT count(*) FROM registered_folders').fetchone()[0], 1)
        with self.assertRaises(NewChatError) as failed:
            s.open(dict(selection, requestId=intent['requestId']))
        self.assertEqual(failed.exception.code, 'folder_changed')
        self.assertEqual(self.adapter.creates, 0)
        replaced = s.register_folder({'path': str(new)})['folder']
        self.assertEqual(replaced['id'], saved['id'])
        self.assertNotEqual(replaced['revision'], saved['revision'])
        with self.assertRaises(NewChatError):
            s.prepare(selection)

    def test_registration_rejects_invalid_directory_selections(self):
        s = self.service()
        file = self.root / 'example.txt'
        file.touch()
        for value in (str(file), str(self.root / 'missing'), 'relative', '/', '/fixture\x00', None):
            with self.subTest(value=value), self.assertRaises(NewChatError) as failed:
                s.register_folder({'path': value})
            self.assertEqual(failed.exception.code, 'invalid_folder')
        self.assertEqual(len(s.context()['folders']), 1)

    def test_registration_does_not_call_adapters_prepare_or_start_attempts(self):
        s = self.service()
        self.adapter.available = False
        before = self.folder.stat().st_mode
        with patch.object(self.adapter, 'preflight') as preflight, \
                patch.object(self.adapter, 'create_once') as create, \
                patch.object(self.adapter, 'open_existing') as open_native, \
                patch.object(self.adapter, 'verify_existing') as verify, \
                patch.object(s, 'prepare') as prepare, patch.object(s, 'open') as attempt, \
                patch.object(s.inventory, 'source') as source:
            saved = s.register_folder({'path': str(self.folder)})
            for operation in (preflight, create, open_native, verify, prepare, attempt, source):
                operation.assert_not_called()
        self.assertEqual(saved['schemaVersion'], 1)
        self.assertTrue(saved['folder']['registered'])
        self.assertEqual(self.folder.stat().st_mode, before)
        self.assertEqual((s.root.stat().st_mode & 0o777, (s.root / 'journal.sqlite').stat().st_mode & 0o777),
                         (0o700, 0o600))
        self.assertEqual(s.db.execute('SELECT count(*) FROM intents').fetchone()[0], 0)
        self.assertEqual(s.db.execute('SELECT count(*) FROM attempts').fetchone()[0], 0)
        self.assertEqual(s.jobs, {})
        context = s.context()
        self.assertNotIn('canRegisterFolders', context)
        self.assertFalse(context['canChooseFolders'])
        self.assertIn(saved['folder'], context['folders'])
        self.assertFalse(context['providers'][0]['available'])
        self.assertEqual((self.adapter.creates, self.adapter.opens, self.adapter.verifies), (0, 0, 0))
        self.assertEqual(s.db.execute('SELECT count(*) FROM intents').fetchone()[0], 0)
        self.assertEqual(s.db.execute('SELECT count(*) FROM attempts').fetchone()[0], 0)

    def test_registration_requires_one_path_and_does_not_probe_invalid_shapes(self):
        s = self.service()
        with patch.object(s.inventory, 'register') as register:
            for data in (None, [], str(self.folder), {}, {'path': str(self.folder), 'launch': True},
                         {'folder': str(self.folder)}):
                with self.subTest(data=data), self.assertRaises(NewChatError) as caught:
                    s.register_folder(data)
                self.assertEqual((caught.exception.status, caught.exception.code), (400, 'invalid_request'))
            register.assert_not_called()
        self.assertEqual(s.db.execute('SELECT count(*) FROM registered_folders').fetchone()[0], 0)

    def test_saved_capacity_is_256_and_duplicate_is_allowed_at_limit(self):
        s = self.service()
        self.assertEqual(s.inventory.maximum, 256)
        saved = None
        for index in range(256):
            folder = self.root / ('registered-%03d' % index)
            folder.mkdir()
            saved = s.register_folder({'path': str(folder)})['folder']
        self.assertEqual(s.register_folder({'path': saved['displayPath']})['folder'], saved)
        extra = self.root / 'overflow-project'
        extra.mkdir()
        with self.assertRaises(NewChatError) as caught:
            s.register_folder({'path': str(extra)})
        self.assertEqual((caught.exception.status, caught.exception.code), (429, 'capacity'))
        self.assertEqual(s.db.execute('SELECT count(*) FROM registered_folders').fetchone()[0], 256)
        # Native discovery has its own separate bound and remains available.
        self.assertEqual(len(s.context()['folders']), 257)
        self.assertEqual(self.adapter.creates, 0)

    def test_replacement_during_registration_does_not_save_stale_authority(self):
        s = self.service()
        register = s.inventory.register

        def replace_after_check(path):
            folder = register(path)
            self.folder.rename(self.root / 'old-project')
            self.folder.mkdir()
            return folder

        with patch.object(s.inventory, 'register', side_effect=replace_after_check):
            with self.assertRaises(NewChatError) as caught:
                s.register_folder({'path': str(self.folder)})
        self.assertEqual(caught.exception.code, 'folder_changed')
        self.assertEqual(s.db.execute('SELECT count(*) FROM registered_folders').fetchone()[0], 0)
        self.assertEqual(self.adapter.creates, 0)

    def test_bad_saved_rows_do_not_poison_healthy_rows_or_repair_the_registry(self):
        s = self.service()
        healthy = s.register_folder({'path': str(self.folder)})['folder']
        other = self.root / 'other-project'
        other.mkdir()
        damaged = s.register_folder({'path': str(other)})['folder']
        original = json.loads(s.db.execute('SELECT data FROM registered_folders WHERE id=?', (damaged['id'],)).fetchone()[0])
        bad = ['{', 'null', '[]', '"fictional"', '[' * 2000 + ']' * 2000, b'not-text', ' ' * 65537 + '{}']
        for changes in ({'id': []}, {'revision': '0' * 64}, {'path': 'relative'}, {'device': True},
                        {'inode': -1}, {'label': 7}, {'label': ''}, {'extra': 'unknown'}, {'path': '/fictional\ud800'}):
            bad.append(json.dumps(dict(original, **changes)))
        missing = dict(original)
        missing.pop('inode')
        bad.append(json.dumps(missing))
        bad.append(json.dumps(original)[:-1] + ',"id":' + json.dumps(original['id']) + '}')
        for index, raw in enumerate(bad):
            with self.subTest(index=index):
                with s.db:
                    s.db.execute('UPDATE registered_folders SET data=? WHERE id=?', (raw, damaged['id']))
                before = s.db.execute('SELECT id,data FROM registered_folders ORDER BY id').fetchall()
                self.assertEqual(s.context()['folders'], [healthy])
                self.assertEqual(s.saved_folders(), {'schemaVersion': 1,
                    'savedFolders': [{key: healthy[key] for key in ('id', 'revision', 'label', 'displayPath')}],
                    'complete': False, 'unreadableCount': 1})
                self.assertEqual(s.db.execute('SELECT id,data FROM registered_folders ORDER BY id').fetchall(), before)
        with s.db:
            s.db.execute('UPDATE registered_folders SET id=?,data=? WHERE id=?',
                         ('f' * 64, json.dumps(original), damaged['id']))
        self.assertEqual(s.context()['folders'], [healthy])
        forged = dict(original, id='f' * 64)
        with s.db:
            s.db.execute('UPDATE registered_folders SET data=? WHERE id=?', (json.dumps(forged), 'f' * 64))
            s.db.execute('INSERT INTO registered_folders VALUES (?,?)', ('0' * 64, '{'))
        self.assertEqual(s.context()['folders'], [healthy])
        self.assertEqual(s.db.execute('SELECT count(*) FROM registered_folders').fetchone()[0], 3)
        self.assertEqual(self.adapter.creates, 0)

    def test_saved_loader_is_filesystem_free_and_query_failure_is_not_hidden(self):
        s = self.service()
        saved = s.register_folder({'path': str(self.folder)})['folder']
        with patch('new_chat_inventory.os.stat', side_effect=AssertionError('No filesystem reads in loader.')), \
                patch('new_chat_inventory._physical_path', side_effect=AssertionError('No policy I/O in loader.')):
            loaded = s._registered_folders()
        self.assertEqual([folder.id for folder in loaded], [saved['id']])
        with s.db:
            s.db.execute('DROP TABLE registered_folders')
        with self.assertRaises(sqlite3.Error):
            s.context()

    def test_invalid_utf8_saved_text_is_skipped_without_poisoning_healthy_rows(self):
        s = self.service()
        healthy = s.register_folder({'path': str(self.folder)})['folder']
        other = self.root / 'other-project'
        other.mkdir()
        damaged = s.register_folder({'path': str(other)})['folder']
        original = s.db.execute('SELECT data FROM registered_folders WHERE id=?', (damaged['id'],)).fetchone()[0]
        for kind in ('data', 'id-before', 'id-after'):
            with self.subTest(kind=kind):
                with s.db:
                    s.db.execute('DELETE FROM registered_folders WHERE id!=?', (healthy['id'],))
                    s.db.execute('INSERT INTO registered_folders VALUES (?,?)', (damaged['id'], original))
                    if kind == 'data':
                        s.db.execute("UPDATE registered_folders SET data=CAST(x'7bff7d' AS TEXT) WHERE id=?", (damaged['id'],))
                    else:
                        invalid = (b'0' if kind == 'id-before' else b'f') * 63 + b'\xff'
                        s.db.execute('UPDATE registered_folders SET id=CAST(? AS TEXT) WHERE id=?', (invalid, damaged['id']))
                before = s.db.execute('SELECT CAST(id AS BLOB),CAST(data AS BLOB) FROM registered_folders ORDER BY id').fetchall()
                self.assertEqual(s.context()['folders'], [healthy])
                listing = s.saved_folders()
                self.assertEqual((listing['complete'], listing['unreadableCount']), (False, 1))
                self.assertEqual([row['id'] for row in listing['savedFolders']], [healthy['id']])
                choice = dict(provider='codex', folderId=healthy['id'], folderRevision=healthy['revision'])
                self.assertIn('requestId', s.prepare(choice))
                self.assertEqual(s.db.execute('SELECT CAST(id AS BLOB),CAST(data AS BLOB) FROM registered_folders ORDER BY id').fetchall(), before)
        self.assertEqual((self.adapter.creates, self.adapter.opens), (0, 0))

    def test_saved_management_uses_only_stored_metadata_for_unavailable_targets(self):
        s = self.service()
        saved = [s.register_folder({'path': str(self.folder)})['folder']]
        missing, replaced = self.root / 'missing-project', self.root / 'replaced-project'
        for path in (missing, replaced):
            path.mkdir()
            saved.append(s.register_folder({'path': str(path)})['folder'])
        missing.rmdir()
        replaced.rename(self.root / 'previous-project')
        replaced.mkdir()
        private = self.root / '.claude'
        private.mkdir()
        frozen = s.inventory._folder(str(private), private.stat())
        with s.db:
            s.db.execute('INSERT INTO registered_folders VALUES (?,?)', (frozen.id, json.dumps(vars(frozen))))
        saved.append(dict(id=frozen.id, revision=frozen.revision, label=frozen.label, displayPath=frozen.path))
        protected = self.folder / 'kept.txt'
        protected.write_text('Fictional owner file stays in place.')
        self.adapter.available = False
        with ExitStack() as stack:
            for obj, name in ((s, 'context'), (s, '_capability'), (s, '_chooser'),
                              (s, 'prepare'), (s, 'open'), (s.inventory, 'source'),
                              (s.inventory, 'require_allowed'), (self.adapter, 'preflight'),
                              (self.adapter, 'create_once'), (self.adapter, 'open_existing'),
                              (self.adapter, 'verify_existing')):
                stack.enter_context(patch.object(obj, name, side_effect=AssertionError('Management reached a launch effect.')))
            stack.enter_context(patch('new_chat_inventory.os.stat', side_effect=AssertionError('Management probed a target.')))
            stack.enter_context(patch('new_chat_inventory._physical_path', side_effect=AssertionError('Management resolved a target.')))
            stack.enter_context(patch('new_chat_service.validate_folder', side_effect=AssertionError('Management validated a target.')))
            listing = s.saved_folders()
            self.assertEqual((listing['schemaVersion'], listing['complete'], listing['unreadableCount']), (1, True, 0))
            self.assertEqual({row['id'] for row in listing['savedFolders']}, {row['id'] for row in saved})
            self.assertTrue(all(set(row) == {'id', 'revision', 'label', 'displayPath'} for row in listing['savedFolders']))
            for row in saved:
                self.assertEqual(s.remove_saved_folder(row['id'], {'revision': row['revision']}),
                                 {'schemaVersion': 1, 'removed': {'id': row['id'], 'revision': row['revision']}})
            self.assertEqual(s.saved_folders(), {'schemaVersion': 1, 'savedFolders': [], 'complete': True, 'unreadableCount': 0})
        self.assertEqual(protected.read_text(), 'Fictional owner file stays in place.')
        self.assertFalse(missing.exists())
        self.assertTrue(replaced.is_dir())
        self.assertTrue(private.is_dir())
        self.assertEqual(s.db.execute('SELECT count(*) FROM intents').fetchone()[0], 0)
        self.assertEqual(s.db.execute('SELECT count(*) FROM attempts').fetchone()[0], 0)
        self.assertEqual(s.jobs, {})

    def test_saved_remove_refuses_unknown_discovered_stale_corrupt_and_invalid_requests(self):
        s = self.service()
        discovered = s.context()['folders'][0]
        with self.assertRaises(NewChatError) as failed:
            s.remove_saved_folder(discovered['id'], {'revision': discovered['revision']})
        self.assertEqual((failed.exception.status, failed.exception.code, failed.exception.message),
                         (404, 'unknown_saved_folder', 'This folder is no longer saved in Motet.'))
        saved = s.register_folder({'path': str(self.folder)})['folder']
        before = s.db.execute('SELECT id,data FROM registered_folders').fetchall()
        invalid = [(value, {'revision': saved['revision']}) for value in (None, True, 'a' * 63, 'A' * 64, 'z' * 64)]
        invalid += [(saved['id'], value) for value in (None, [], {}, {'revision': True}, {'revision': 'A' * 64},
                     {'revision': 'a' * 63}, {'revision': saved['revision'], 'path': str(self.folder)})]
        for ident, data in invalid:
            with self.subTest(ident=ident, data=data):
                with self.assertRaises(NewChatError) as failed:
                    s.remove_saved_folder(ident, data)
                self.assertEqual((failed.exception.status, failed.exception.code), (400, 'invalid_request'))
        with self.assertRaises(NewChatError) as failed:
            s.remove_saved_folder(saved['id'], {'revision': '0' * 64})
        self.assertEqual((failed.exception.status, failed.exception.code, failed.exception.message),
                         (409, 'saved_folder_changed',
                          'This saved project folder changed. Refresh the list before removing it.'))
        self.assertEqual(s.db.execute('SELECT id,data FROM registered_folders').fetchall(), before)
        with s.db:
            s.db.execute('UPDATE registered_folders SET data=?', ('{',))
        with self.assertRaises(NewChatError) as failed:
            s.remove_saved_folder(saved['id'], {'revision': saved['revision']})
        self.assertEqual((failed.exception.status, failed.exception.code, failed.exception.message),
                         (409, 'unreadable_saved_folder', 'This saved project folder could not be verified. Nothing was removed.'))
        self.assertEqual(s.db.execute('SELECT data FROM registered_folders').fetchone()[0], '{')

    def test_saved_remove_old_revision_cannot_remove_replacement_registration(self):
        s = self.service()
        old = s.register_folder({'path': str(self.folder)})['folder']
        self.folder.rename(self.root / 'previous-project')
        self.folder.mkdir()
        current = s.register_folder({'path': str(self.folder)})['folder']
        self.assertEqual(current['id'], old['id'])
        self.assertNotEqual(current['revision'], old['revision'])
        with self.assertRaises(NewChatError) as failed:
            s.remove_saved_folder(old['id'], {'revision': old['revision']})
        self.assertEqual(failed.exception.code, 'saved_folder_changed')
        self.assertEqual(s.saved_folders()['savedFolders'][0]['revision'], current['revision'])

    def test_saved_remove_raw_compare_and_one_row_check_protect_concurrent_changes(self):
        s = self.service()
        for action in ('replace', 'blob', 'delete'):
            saved = s.register_folder({'path': str(self.folder)})['folder']
            original = s.db.execute('SELECT data FROM registered_folders').fetchone()[0]
            changed = json.dumps(dict(json.loads(original), label='Fictional concurrent label'))
            def interfere(sql, *args):
                if sql.startswith('DELETE FROM registered_folders'):
                    with closing(sqlite3.connect(s.root / 'journal.sqlite')) as other, other:
                        if action == 'replace':
                            other.execute('UPDATE registered_folders SET data=? WHERE id=?', (changed, saved['id']))
                        elif action == 'blob':
                            other.execute('UPDATE registered_folders SET data=? WHERE id=?',
                                          (sqlite3.Binary(original.encode()), saved['id']))
                        else:
                            other.execute('DELETE FROM registered_folders WHERE id=?', (saved['id'],))
            with self.subTest(action=action), patch.object(s, 'db', DatabaseProbe(s.db, execute=interfere)):
                with self.assertRaises(NewChatError) as failed:
                    s.remove_saved_folder(saved['id'], {'revision': saved['revision']})
                self.assertEqual((failed.exception.status, failed.exception.code, failed.exception.message),
                                 (409, 'saved_folder_changed',
                                  'This saved project folder changed. Refresh the list before removing it.'))
            remaining = s.db.execute('SELECT data FROM registered_folders').fetchone()
            self.assertEqual(remaining, (changed,) if action == 'replace' else (original.encode(),) if action == 'blob' else None)

    def test_saved_database_failures_are_unavailable_and_committed_response_can_be_uncertain(self):
        s = self.service()
        saved = s.register_folder({'path': str(self.folder)})['folder']
        unavailable = (503, 'saved_folders_unavailable',
                       'Saved project folders are unavailable. Refresh the list to check.')
        def broken(sql, *args):
            raise sqlite3.OperationalError('/fictional/private database failure')
        for operation in (lambda: s.saved_folders(), lambda: s.remove_saved_folder(saved['id'], {'revision': saved['revision']})):
            with patch.object(s, 'db', DatabaseProbe(s.db, execute=broken)), self.assertRaises(NewChatError) as failed:
                operation()
            self.assertEqual((failed.exception.status, failed.exception.code, failed.exception.message), unavailable)
        class InterruptedRead:
            def __iter__(self):
                yield tuple(value.encode() for value in s.db.db.execute('SELECT id,data FROM registered_folders').fetchone())
                raise sqlite3.DatabaseError('/fictional/interrupted read')
        with patch.object(s, 'db', DatabaseProbe(s.db, execute=lambda *args: InterruptedRead())), self.assertRaises(NewChatError) as failed:
            s.saved_folders()
        self.assertEqual((failed.exception.status, failed.exception.code, failed.exception.message), unavailable)
        def delete_failed(sql, *args):
            if sql.startswith('DELETE FROM registered_folders'):
                raise sqlite3.OperationalError('/fictional/delete failed')
        with patch.object(s, 'db', DatabaseProbe(s.db, execute=delete_failed)), self.assertRaises(NewChatError) as failed:
            s.remove_saved_folder(saved['id'], {'revision': saved['revision']})
        self.assertEqual((failed.exception.status, failed.exception.code, failed.exception.message), unavailable)
        self.assertEqual(s.db.execute('SELECT count(*) FROM registered_folders').fetchone()[0], 1)
        def lost_commit_response():
            raise sqlite3.OperationalError('/fictional/lost commit response')
        with patch.object(s, 'db', DatabaseProbe(s.db, committed=lost_commit_response)), self.assertRaises(NewChatError) as failed:
            s.remove_saved_folder(saved['id'], {'revision': saved['revision']})
        self.assertEqual((failed.exception.status, failed.exception.code, failed.exception.message), unavailable)
        self.assertEqual(s.saved_folders()['savedFolders'], [])

    def test_saved_busy_commit_rolls_back_before_another_operation_can_commit(self):
        for legacy in (False, True):
            with self.subTest(legacy_context=legacy):
                s = self.service()
                saved = s.register_folder({'path': str(self.folder)})['folder']
                database = s.db
                database.execute('PRAGMA busy_timeout=1')
                reader = sqlite3.connect(s.root / 'journal.sqlite', timeout=.01)
                self.addCleanup(reader.close)
                reader.execute('BEGIN')
                reader.execute('SELECT * FROM registered_folders').fetchall()
                rollbacks = []
                class Probe(LegacyTransactionProbe):
                    def rollback(probe):
                        rollbacks.append(s.lock._is_owned())
                        return database.rollback()
                try:
                    if legacy:
                        s.db = Probe(database)
                    with self.assertRaises(NewChatError) as failed:
                        s.remove_saved_folder(saved['id'], {'revision': saved['revision']})
                    self.assertEqual((failed.exception.status, failed.exception.code, failed.exception.message),
                                     (503, 'saved_folders_unavailable',
                                      'Saved project folders are unavailable. Refresh the list to check.'))
                    self.assertFalse(database.in_transaction)
                    self.assertEqual(len(s.saved_folders()['savedFolders']), 1)
                    self.assertEqual(reader.execute('SELECT count(*) FROM registered_folders').fetchone()[0], 1)
                    if legacy:
                        self.assertEqual(rollbacks, [True], 'rollback completes under the service lock')
                    reader.rollback()
                    with s.db:
                        s.db.execute('SELECT 1')
                    self.assertEqual(reader.execute('SELECT count(*) FROM registered_folders').fetchone()[0], 1,
                                     'an unrelated transaction cannot commit the refused delete')
                finally:
                    reader.rollback()
                    s.db = database
                    s.close()

    def test_saved_rollback_failure_quarantines_journal_without_cancelling_claimed_work(self):
        s = self.service()
        saved = s.register_folder({'path': str(self.folder)})['folder']
        request = self.request(s)
        entered, release = threading.Event(), threading.Event()
        def held_create(folder, callback):
            self.adapter.creates += 1
            entered.set()
            release.wait()
            identity = self.adapter.make_identity()
            callback(identity.native_id, identity.cwd)
            return identity
        self.adapter.create_once = held_create
        _, attempt = s.open(request)
        self.assertTrue(entered.wait(3))
        database, file_lock = s.db, s._file_lock
        calls = (self.adapter.preflights, self.adapter.verifies, self.adapter.creates, self.adapter.opens)
        class Poison(LegacyTransactionProbe):
            def __exit__(probe, *args):
                raise sqlite3.OperationalError('Fictional COMMIT failure')
            def rollback(probe):
                self.assertTrue(s.lock._is_owned())
                raise sqlite3.OperationalError('Fictional rollback failure')
            def close(probe):
                self.assertTrue(s.lock._is_owned())
                self.assertTrue(s._journal_unavailable, 'quarantine precedes the close attempt')
                database.close()
                raise sqlite3.OperationalError('Fictional close response failure')
        s.db = Poison(database)
        try:
            with self.assertRaises(NewChatError) as failed:
                s.remove_saved_folder(saved['id'], {'revision': saved['revision']})
            self.assertEqual((failed.exception.status, failed.exception.code, failed.exception.message),
                             (503, 'saved_folders_unavailable',
                              'Saved project folders are unavailable. Restart Motet before trying again.'))
            self.assertIsNone(s.db, 'even a failed close cannot leave a reusable dirty journal')
            self.assertEqual(s._file_lock, file_lock)
            os.fstat(file_lock)
            self.assertFalse(s.closing or self.adapter.closed)
            self.assertIn(attempt['attemptId'], s.jobs)
            self.assertTrue(s.jobs[attempt['attemptId']].is_alive())
            for operation in (s.context, s.saved_folders,
                              lambda: s.remove_saved_folder(saved['id'], {'revision': saved['revision']}),
                              lambda: s.register_folder({'path': str(self.folder)}),
                              lambda: s.prepare({key: request[key] for key in ('provider', 'folderId', 'folderRevision')}),
                              lambda: s.open(request), lambda: s.request_status(request['requestId']),
                              lambda: s.status(attempt['attemptId']),
                              lambda: s.observe(attempt['attemptId'], {'action': 'stop'})):
                with self.assertRaises(NewChatError) as unavailable:
                    operation()
                self.assertEqual((unavailable.exception.status, unavailable.exception.code, unavailable.exception.message),
                                 (503, 'saved_folders_unavailable',
                                  'Saved project folders are unavailable. Restart Motet before trying again.'))
            s.process_admissions(lambda *args: self.fail('A poisoned journal cannot admit a native session'))
            self.assertEqual((self.adapter.preflights, self.adapter.verifies,
                              self.adapter.creates, self.adapter.opens), calls)
            reader = sqlite3.connect(s.root / 'journal.sqlite')
            self.addCleanup(reader.close)
            with reader:
                self.assertEqual(reader.execute('SELECT count(*) FROM registered_folders').fetchone()[0], 1)
        finally:
            release.set()
            self.settle(s)
        self.assertEqual((self.adapter.creates, self.adapter.opens), (1, 0))
        self.assertFalse(self.adapter.closed)

    def test_saved_close_failure_cannot_expose_the_quarantined_pending_delete(self):
        s = self.service()
        saved = s.register_folder({'path': str(self.folder)})['folder']
        database = s.db
        class Poison(LegacyTransactionProbe):
            def __exit__(probe, *args):
                raise sqlite3.OperationalError('Fictional COMMIT failure')
            def rollback(probe):
                raise sqlite3.OperationalError('Fictional rollback failure')
            def close(probe):
                raise sqlite3.OperationalError('Fictional close failure')
        s.db = Poison(database)
        try:
            with self.assertRaises(NewChatError):
                s.remove_saved_folder(saved['id'], {'revision': saved['revision']})
            self.assertIsNone(s.db)
            self.assertTrue(database.in_transaction, 'the fault fixture really leaves its connection dirty')
            for operation in (s.context, s.saved_folders, lambda: s.request_status(str(uuid.uuid4()))):
                with self.assertRaises(NewChatError) as failed:
                    operation()
                self.assertEqual(failed.exception.status, 503)
            reader = sqlite3.connect(s.root / 'journal.sqlite')
            self.addCleanup(reader.close)
            with reader:
                self.assertEqual(reader.execute('SELECT count(*) FROM registered_folders').fetchone()[0], 1)
        finally:
            database.close()

    def poison_saved_journal(self, service, saved):
        class Poison(LegacyTransactionProbe):
            def __exit__(probe, *args):
                raise sqlite3.OperationalError('Fictional COMMIT failure')
            def rollback(probe):
                raise sqlite3.OperationalError('Fictional rollback failure')
        service.db = Poison(service.db)
        with self.assertRaises(NewChatError) as failed:
            service.remove_saved_folder(saved['id'], {'revision': saved['revision']})
        self.assertEqual((failed.exception.status, failed.exception.code), (503, 'saved_folders_unavailable'))
        self.assertIsNone(service.db)

    def test_saved_quarantine_wins_before_late_register_prepare_and_open_entries(self):
        for action in ('register', 'prepare', 'open'):
            with self.subTest(action=action), closing(self.service()) as s:
                saved = s.register_folder({'path': str(self.folder)})['folder']
                request = self.request(s)
                selection = {key: request[key] for key in ('provider', 'folderId', 'folderRevision')}
                paused, release = threading.Event(), threading.Event()
                results, reads, guard = [], [], s._guard
                def unexpected_read(*args, **kwargs):
                    reads.append(True)
                    raise AssertionError('Quarantined work cannot inspect project folders')
                def pause_after_first_guard():
                    guard()
                    if threading.current_thread().name == 'fictional-late-entry' and not paused.is_set():
                        paused.set()
                        release.wait()
                def invoke():
                    try:
                        results.append({'register': lambda: s.register_folder({'path': str(self.folder)}),
                                        'prepare': lambda: s.prepare(selection),
                                        'open': lambda: s.open(request)}[action]())
                    except Exception as error:
                        results.append(error)
                thread = threading.Thread(name='fictional-late-entry', target=invoke)
                calls = (self.adapter.preflights, self.adapter.creates, self.adapter.opens, self.adapter.verifies)
                with patch.object(s, '_guard', side_effect=pause_after_first_guard), ExitStack() as stack:
                    try:
                        thread.start()
                        self.assertTrue(paused.wait(3))
                        self.poison_saved_journal(s, saved)
                        for name in ('register', 'refresh', 'get', 'require_allowed'):
                            stack.enter_context(patch.object(s.inventory, name,
                                side_effect=unexpected_read))
                    finally:
                        release.set()
                        thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(results), 1)
                self.assertIsInstance(results[0], NewChatError)
                self.assertEqual((results[0].status, results[0].code), (503, 'saved_folders_unavailable'))
                self.assertEqual((self.adapter.preflights, self.adapter.creates,
                                  self.adapter.opens, self.adapter.verifies), calls)
                self.assertEqual(reads, [], 'even a caught physical-read failure is forbidden after quarantine')
                self.assertEqual(s.jobs, {})

    def test_saved_quarantine_between_context_and_admission_read_phases(self):
        for phase in ('inventory', 'provider', 'admission'):
            with self.subTest(phase=phase), closing(self.service()) as s:
                saved = s.register_folder({'path': str(self.folder)})['folder']
                if phase == 'admission':
                    s.open(self.request(s))
                    self.settle(s)
                paused, release = threading.Event(), threading.Event()
                results, reads = [], []
                def unexpected_read(*args, **kwargs):
                    reads.append(True)
                    raise AssertionError('No project read after quarantine')
                def pause():
                    if threading.current_thread().name == 'fictional-late-read' and not paused.is_set():
                        paused.set()
                        release.wait()
                inventory, capability, loads = s._inventory, s._capability, json.loads
                def late_inventory(*args):
                    pause()
                    return inventory(*args)
                def late_capability(*args):
                    pause()
                    return capability(*args)
                class Snapshot(dict):
                    def __getitem__(row, key):
                        if key == 'observation' and not s.lock._is_owned():
                            pause()
                        return super().__getitem__(key)
                def snapshots(*args, **kwargs):
                    value = loads(*args, **kwargs)
                    return Snapshot(value) if isinstance(value, dict) and 'creation_attempts' in value else value
                def invoke():
                    try:
                        results.append(s.process_admissions(lambda *args: self.fail('No admission after quarantine'))
                                       if phase == 'admission' else s.context())
                    except Exception as error:
                        results.append(error)
                thread = threading.Thread(name='fictional-late-read', target=invoke)
                with ExitStack() as stack:
                    if phase == 'inventory':
                        stack.enter_context(patch.object(s, '_inventory', side_effect=late_inventory))
                    elif phase == 'provider':
                        stack.enter_context(patch.object(s, '_capability', side_effect=late_capability))
                    else:
                        stack.enter_context(patch('new_chat_service.json.loads', side_effect=snapshots))
                    try:
                        thread.start()
                        self.assertTrue(paused.wait(3))
                        calls = (self.adapter.preflights, self.adapter.verifies, self.adapter.creates, self.adapter.opens)
                        self.poison_saved_journal(s, saved)
                        for name in ('refresh', 'get', 'require_allowed'):
                            stack.enter_context(patch.object(s.inventory, name,
                                side_effect=unexpected_read))
                    finally:
                        release.set()
                        thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(results), 1)
                if phase == 'admission':
                    self.assertEqual(results, [None])
                else:
                    self.assertIsInstance(results[0], NewChatError)
                    self.assertEqual((results[0].status, results[0].code), (503, 'saved_folders_unavailable'))
                self.assertEqual((self.adapter.preflights, self.adapter.verifies,
                                  self.adapter.creates, self.adapter.opens), calls)
                self.assertEqual(reads, [], 'caught policy errors must not hide a post-quarantine read')

    def test_saved_management_preserves_guards_and_shutdown_race(self):
        s = self.service()
        saved = s.register_folder({'path': str(self.folder)})['folder']
        for flag, code in (('enabled', 'disabled'), ('demo', 'demo_unavailable')):
            value = False if flag == 'enabled' else True
            with patch.object(s, flag, value):
                for operation in (s.saved_folders, lambda: s.remove_saved_folder(saved['id'], {'revision': saved['revision']})):
                    with self.assertRaises(NewChatError) as failed:
                        operation()
                    self.assertEqual((failed.exception.status, failed.exception.code), (503, code))
        guard = s._guard
        def close_after_initial_guard():
            guard()
            s.close()
        with patch.object(s, '_guard', side_effect=close_after_initial_guard), self.assertRaises(NewChatError) as failed:
            s.remove_saved_folder(saved['id'], {'revision': saved['revision']})
        self.assertEqual((failed.exception.status, failed.exception.code), (503, 'service_closed'))

    def test_saved_removal_keeps_discovery_and_claimed_work_but_blocks_manual_preclaim(self):
        s = self.service()
        saved = s.register_folder({'path': str(self.folder)})['folder']
        s.remove_saved_folder(saved['id'], {'revision': saved['revision']})
        discovered = s.context()['folders'][0]
        self.assertEqual(discovered['id'], saved['id'])
        self.assertNotIn('registered', discovered)
        manual = self.root / 'manual-project'
        manual.mkdir()
        saved = s.register_folder({'path': str(manual)})['folder']
        choice = dict(provider='codex', folderId=saved['id'], folderRevision=saved['revision'])
        intent = s.prepare(choice)
        s.remove_saved_folder(saved['id'], {'revision': saved['revision']})
        with self.assertRaises(NewChatError) as failed:
            s.open(dict(choice, requestId=intent['requestId']))
        self.assertEqual(failed.exception.code, 'folder_changed')
        self.assertEqual(s.db.execute('SELECT count(*) FROM attempts').fetchone()[0], 0)
        s.register_folder({'path': str(manual)})
        request = dict(choice, requestId=s.prepare(choice)['requestId'])
        entered, release = threading.Event(), threading.Event()
        create = self.adapter.create_once
        def held(folder, callback):
            entered.set()
            release.wait()
            return create(folder, callback)
        with patch.object(self.adapter, 'create_once', side_effect=held):
            try:
                _, attempt = s.open(request)
                self.assertTrue(entered.wait(3))
                tables = ('intents', 'attempts', 'native_receipts', 'intent_issues', 'folders', 'meta')
                before = {table: s.db.execute('SELECT * FROM ' + table).fetchall() for table in tables}
                s.remove_saved_folder(saved['id'], {'revision': saved['revision']})
                self.assertEqual({table: s.db.execute('SELECT * FROM ' + table).fetchall() for table in tables}, before)
            finally:
                release.set()
                self.settle(s)
        self.assertEqual((self.adapter.creates, self.adapter.opens), (1, 1))
        s.process_admissions(lambda identity, folder, permit: 'x:' + identity.native_id if permit() else None)
        self.assertEqual(s.status(attempt['attemptId'])['state'], 'added')
        self.assertEqual(s.open(request)[0], 200)
        self.assertEqual((self.adapter.creates, self.adapter.opens), (1, 1))

    def test_remove_and_preclaim_open_share_the_same_service_lock(self):
        s = self.service()
        manual = self.root / 'manual-project'
        manual.mkdir()
        saved = s.register_folder({'path': str(manual)})['folder']
        choice = dict(provider='codex', folderId=saved['id'], folderRevision=saved['revision'])
        request = dict(choice, requestId=s.prepare(choice)['requestId'])
        entered, release, open_at_lock = threading.Event(), threading.Event(), threading.Event()
        outputs, failures = {}, {}
        class TrackedLock:
            def __init__(self, lock):
                self.lock, self.owners = lock, []
            def __enter__(self):
                if threading.current_thread().name == 'fictional-open':
                    open_at_lock.set()
                self.lock.acquire()
                self.owners.append(threading.current_thread())
                return self
            def __exit__(self, *args):
                self.owners.pop()
                self.lock.release()
        tracked = TrackedLock(s.lock)
        decode = s._decode_registered_folder
        def held_decode(*args):
            if threading.current_thread().name == 'fictional-remove':
                entered.set()
                self.assertIs(tracked.owners[-1] if tracked.owners else None, threading.current_thread())
                release.wait()
            return decode(*args)
        def invoke(name, operation):
            try:
                outputs[name] = operation()
            except BaseException as error:
                failures[name] = error
        remove = threading.Thread(name='fictional-remove', target=invoke,
            args=('remove', lambda: s.remove_saved_folder(saved['id'], {'revision': saved['revision']})))
        launch = threading.Thread(name='fictional-open', target=invoke, args=('open', lambda: s.open(request)))
        with patch.object(s, 'lock', tracked), patch.object(s, '_decode_registered_folder', side_effect=held_decode):
            try:
                remove.start()
                self.assertTrue(entered.wait(3))
                launch.start()
                self.assertTrue(open_at_lock.wait(3))
                self.assertNotIn('open', outputs)
                self.assertEqual((self.adapter.creates, self.adapter.opens), (0, 0))
            finally:
                release.set()
                remove.join(5)
                if launch.ident is not None:
                    launch.join(5)
        self.assertFalse(remove.is_alive() or launch.is_alive())
        self.assertNotIn('remove', failures)
        self.assertEqual(outputs['remove']['removed'], {'id': saved['id'], 'revision': saved['revision']})
        self.assertIsInstance(failures.get('open'), NewChatError)
        self.assertEqual(failures['open'].code, 'folder_changed')
        self.assertEqual(s.db.execute('SELECT count(*) FROM attempts').fetchone()[0], 0)
        self.assertEqual((self.adapter.creates, self.adapter.opens), (0, 0))

    def test_codex_worktree_allows_fake_creation_only_after_an_explicit_attempt(self):
        s = self.service()
        project = self.root / '.codex' / 'worktrees' / 'orchard'
        project.mkdir(parents=True)
        saved = s.register_folder({'path': str(project)})['folder']
        self.seed = self.adapter.make_identity(project)
        self.assertEqual(s.context()['folders'], [saved])
        choice = dict(provider='codex', folderId=saved['id'], folderRevision=saved['revision'])
        intent = s.prepare(choice)
        self.assertEqual((self.adapter.creates, self.adapter.opens), (0, 0))
        _, attempt = s.open(dict(choice, requestId=intent['requestId']))
        self.settle(s)
        self.assertEqual((self.adapter.creates, self.adapter.opens), (1, 1))
        s.process_admissions(lambda identity, folder, permit: 'x:' + identity.native_id if permit() else None)
        self.assertEqual(s.status(attempt['attemptId'])['state'], 'added')

    def test_old_saved_worktrees_root_stays_stored_but_cannot_prepare_a_chat(self):
        s = self.service()
        worktrees = self.root / '.codex' / 'worktrees'
        worktrees.mkdir(parents=True)
        frozen = s.inventory._folder(str(worktrees), worktrees.stat())
        with s.db:
            s.db.execute('INSERT INTO registered_folders VALUES (?,?)',
                         (frozen.id, json.dumps(vars(frozen), ensure_ascii=True)))
        self.seed = self.adapter.make_identity(worktrees)
        before = s.db.execute('SELECT id,data FROM registered_folders').fetchall()
        self.assertEqual([folder.id for folder in s._registered_folders()], [frozen.id])
        self.assertEqual(s.context()['folders'], [])
        with self.assertRaises(NewChatError):
            s.prepare(dict(provider='codex', folderId=frozen.id, folderRevision=frozen.revision))
        with self.assertRaises(NewChatError):
            s.register_folder({'path': str(worktrees)})
        self.assertEqual(s.db.execute('SELECT id,data FROM registered_folders').fetchall(), before)
        self.assertEqual(s.db.execute('SELECT count(*) FROM intents').fetchone()[0], 0)
        self.assertEqual((self.adapter.creates, self.adapter.opens), (0, 0))

    def test_corrupt_saved_revision_cannot_override_independent_discovery(self):
        s = self.service()
        saved = s.register_folder({'path': str(self.folder)})['folder']
        raw = json.loads(s.db.execute('SELECT data FROM registered_folders').fetchone()[0])
        raw['revision'] = '0' * 64
        with s.db:
            s.db.execute('UPDATE registered_folders SET data=?', (json.dumps(raw),))
        current = s.context()['folders'][0]
        self.assertEqual(current['revision'], saved['revision'])
        self.assertNotIn('registered', current)
        with self.assertRaises(NewChatError):
            s.prepare(dict(provider='codex', folderId=current['id'], folderRevision=raw['revision']))
        self.assertEqual(self.adapter.creates, 0)

    def test_private_discovery_and_saved_rows_are_unavailable_without_deletion(self):
        s = self.service()
        private = self.root / '.claude'
        private.mkdir()
        proof = self.adapter.make_identity(private)
        s.inventory.source = lambda: [proof]
        folder = s.inventory._folder(str(private), private.stat())
        with s.db:
            s.db.execute('INSERT INTO registered_folders VALUES (?,?)',
                         (folder.id, json.dumps(vars(folder))))
        self.assertEqual(s.context()['folders'], [])
        with self.assertRaises(NewChatError):
            s.prepare(dict(provider='codex', folderId=folder.id, folderRevision=folder.revision))
        self.assertEqual(s.db.execute('SELECT count(*) FROM registered_folders').fetchone()[0], 1)
        self.assertEqual((self.adapter.creates, self.adapter.opens), (0, 0))

    def test_actual_state_root_is_injected_and_refused(self):
        s = self.service()
        for path in (s.root.parent, s.root):
            with self.subTest(path=path), self.assertRaises(NewChatError) as caught:
                s.register_folder({'path': str(path)})
            self.assertEqual(caught.exception.message, 'Motet cannot use that folder.')
        self.assertEqual(self.adapter.creates, 0)

    @unittest.skipUnless(sys.platform == 'darwin', 'Physical directory spelling is a macOS capability.')
    def test_private_case_alias_cannot_prepare_through_discovery(self):
        s = self.service()
        private = self.root / '.claude'
        private.mkdir()
        alias = self.root / '.CLAUDE'
        if not alias.exists():
            self.skipTest('This volume is case-sensitive.')
        proof = replace(self.adapter.make_identity(private), cwd=str(alias))
        s.inventory.source = lambda: [proof]
        folder = s.inventory._folder(str(alias), alias.stat())
        self.assertEqual(s.context()['folders'], [])
        with self.assertRaises(NewChatError):
            s.prepare(dict(provider='codex', folderId=folder.id, folderRevision=folder.revision))
        self.assertEqual(s.db.execute('SELECT count(*) FROM intents').fetchone()[0], 0)
        self.assertEqual(self.adapter.creates, 0)

    def test_concurrent_registration_keeps_private_refusals_and_duplicate_capacity(self):
        s = self.service()
        private = self.root / '.codex'
        private.mkdir()
        results, errors = [], []

        def register(index):
            try:
                results.append(s.register_folder({'path': str(private if index % 2 else self.folder)}))
            except BaseException as error:
                errors.append(error)

        callers = [threading.Thread(target=register, args=(index,)) for index in range(40)]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(10)
        self.assertTrue(all(not caller.is_alive() for caller in callers))
        self.assertEqual(len(results), 20)
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(len(errors), 20)
        self.assertTrue(all(isinstance(error, NewChatError) and error.message == 'Motet cannot use that folder.' for error in errors))
        self.assertEqual(s.db.execute('SELECT count(*) FROM registered_folders').fetchone()[0], 1)
        self.assertEqual(s.db.execute('SELECT count(*) FROM attempts').fetchone()[0], 0)
        self.assertEqual(self.adapter.creates, 0)

    def test_private_anchor_retarget_between_verification_and_save_is_refused(self):
        s = self.service()
        register = s.inventory.register

        def retarget(path):
            folder = register(path)
            (self.root / '.codex').symlink_to(self.folder, target_is_directory=True)
            return folder

        with patch.object(s.inventory, 'register', side_effect=retarget):
            with self.assertRaises(NewChatError) as caught:
                s.register_folder({'path': str(self.folder)})
        self.assertEqual(caught.exception.message, 'Motet cannot use that folder.')
        self.assertEqual(s.db.execute('SELECT count(*) FROM registered_folders').fetchone()[0], 0)

    def test_private_anchor_retarget_before_worker_creation_has_no_native_effect(self):
        s = self.service()
        request = self.request(s)
        get = s.inventory.get

        def retarget(*args):
            folder = get(*args)
            (self.root / '.codex').symlink_to(self.folder, target_is_directory=True)
            return folder

        with patch.object(s.inventory, 'get', side_effect=retarget):
            _, attempt = s.open(request)
        self.settle(s)
        final = s.status(attempt['attemptId'])
        self.assertEqual(final['creation']['outcome'], 'not_attempted')
        self.assertEqual((self.adapter.creates, self.adapter.opens), (0, 0))

    def test_normal_identity_proof_still_works_then_private_retarget_prevents_open_and_admission(self):
        s = self.service()
        _, attempt = s.open(self.request(s))
        self.settle(s)
        with s.lock:
            row = s._load(attempt['attemptId'])
        identity = self.adapter.identities[row['native_id']]
        self.assertEqual(s._checked_identity(identity, row), identity)
        (self.root / '.codex').symlink_to(self.folder, target_is_directory=True)
        with self.assertRaises(NewChatError):
            s._checked_identity(identity, row)
        admissions = []
        verifies = self.adapter.verifies
        s.process_admissions(lambda *args: admissions.append(args))
        self.assertEqual(admissions, [])
        self.assertEqual(self.adapter.verifies, verifies)

    def test_private_anchor_retarget_after_creation_prevents_ui_open(self):
        s = self.service()
        self.adapter.after_receipt = lambda _: (self.root / '.codex').symlink_to(self.folder, target_is_directory=True)
        _, attempt = s.open(self.request(s))
        self.settle(s)
        self.assertEqual((self.adapter.creates, self.adapter.opens), (1, 0))
        self.assertIsNone(s.status(attempt['attemptId'])['session'])

    @unittest.skipUnless(sys.platform == 'darwin', 'Physical directory spelling is a macOS capability.')
    def test_registration_uses_physical_case_for_the_launch_path(self):
        s = self.service()
        alternate = self.folder.with_name(self.folder.name.upper())
        if not alternate.exists():
            self.skipTest('This volume is case-sensitive.')
        actual = s.register_folder({'path': str(self.folder)})['folder']
        self.assertEqual(s.register_folder({'path': str(alternate)})['folder'], actual)
        self.assertEqual(actual['displayPath'], str(self.folder))

    def test_async_one_creation_and_duplicate_click_replay(self):
        s = self.service()
        request = self.request(s)
        self.adapter.create_release.clear()
        status, result = s.open(request)
        self.assertEqual(status, 202)
        self.assertEqual(result['state'], 'waiting')
        self.assertTrue(self.adapter.create_entered.wait(1))
        results = []
        threads = [threading.Thread(target=lambda: results.append(s.open(request))) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(1)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(code == 200 and out['attemptId'] == result['attemptId'] for code, out in results))
        self.assertEqual(self.adapter.creates, 1)
        self.adapter.create_release.set()
        self.settle(s)

    def test_omitted_empty_draft_replay_same_but_changed_draft_conflicts(self):
        s = self.service()
        request = self.request(s)
        s.open(request)
        self.assertEqual(s.open(dict(request, draft=''))[0], 200)
        with self.assertRaises(NewChatError) as caught:
            s.open(dict(request, draft='different'))
        self.assertEqual(caught.exception.status, 409)
        self.settle(s)

    def test_receipt_durable_before_adapter_next_mutation_and_no_draft_in_store(self):
        s = self.service()
        request = self.request(s)
        observations = []
        def after_receipt(identity):
            db = sqlite3.connect(s.root / 'journal.sqlite')
            raw = db.execute('SELECT data FROM attempts').fetchone()[0]
            db.close()
            observations.append(json.loads(raw)['native_id'])
        self.adapter.after_receipt = after_receipt
        _, result = s.open(dict(request, draft='PRIVATE FICTIONAL DRAFT'))
        self.settle(s)
        out = s.status(result['attemptId'])
        self.assertEqual(observations, [out['existingSession']['id']])
        self.assertNotIn('PRIVATE FICTIONAL DRAFT', (s.root / 'journal.sqlite').read_bytes().decode('latin1'))
        self.assertEqual(out['draft']['disposition'], 'prefill_requested')

    def test_success_only_after_exact_blank_room_acknowledgement(self):
        s = self.service()
        _, result = s.open(self.request(s))
        self.settle(s)
        self.assertIsNone(s.status(result['attemptId'])['session'])
        s.process_admissions(lambda identity, folder, permit: 'wrong-room-id' if permit() else None)
        self.assertEqual(s.status(result['attemptId'])['state'], 'waiting')
        s.process_admissions(lambda identity, folder, permit: 'x:' + identity.native_id if permit() else None)
        out = s.status(result['attemptId'])
        self.assertEqual(out['state'], 'added')
        self.assertEqual(out['session']['roomAgentId'], 'x:' + out['existingSession']['id'])
        calls = []
        s.process_admissions(lambda *args: calls.append(args))
        self.assertEqual(calls, [])

    def test_creation_success_ui_failure_has_existing_session_and_no_admission(self):
        s = self.service()
        self.adapter.open_result = OpenResult('rejected')
        _, result = s.open(self.request(s))
        self.settle(s)
        out = s.status(result['attemptId'])
        self.assertEqual((out['state'], out['creation']['outcome'], out['uiOpen']['outcome']), ('failed', 'verified', 'rejected'))
        self.assertEqual(out['reason']['code'], 'session_created_ui_open_failed')
        self.assertIsNotNone(out['existingSession'])
        self.assertIsNone(out['session'])
        self.assertFalse(out['capabilities']['openExisting'])
        self.assertEqual(s.observe(result['attemptId'], {'action': 'stop'}), out)
        calls = []
        s.process_admissions(lambda *args: calls.append(args))
        self.assertEqual(calls, [])

    def test_lost_creation_response_never_repeated_after_restart(self):
        s = self.service()
        self.adapter.before_receipt = lambda: (_ for _ in ()).throw(RuntimeError('PRIVATE RAW ERROR'))
        request = self.request(s)
        _, result = s.open(request)
        self.settle(s)
        s.close()
        self.adapter.closed = False
        restored = self.service()
        self.assertEqual(restored.open(request)[0], 200)
        self.assertEqual(self.adapter.creates, 1)
        self.assertNotIn('PRIVATE RAW ERROR', json.dumps(restored.status(result['attemptId'])))
        self.assertEqual(restored.status(result['attemptId'])['creation']['outcome'], 'unknown')

    def test_process_loss_after_native_creation_before_receipt_has_no_recovery_guess(self):
        s = self.service()
        # SystemExit terminates only the fictional worker, preserving precisely
        # the journal at this boundary instead of running exception recovery.
        s._receipt = lambda *args: (_ for _ in ()).throw(SystemExit())
        request = self.request(s)
        _, result = s.open(request)
        self.settle(s)
        self.assertEqual(len(self.adapter.identities), 2)  # seed + unrecorded native creation
        s.close()
        restored = self.service()
        restored.process_admissions(lambda *args: self.fail('An unrecorded native ID was guessed'))
        self.assertEqual(restored.open(request)[0], 200)
        self.assertIsNone(restored.status(result['attemptId'])['existingSession'])
        self.assertEqual((self.adapter.creates, self.adapter.opens), (1, 0))

    def test_process_loss_after_receipt_recovery_does_not_repeat_naming_or_open(self):
        s = self.service()
        self.adapter.after_receipt = lambda _: (_ for _ in ()).throw(SystemExit())
        _, result = s.open(self.request(s))
        self.settle(s)
        s.close()
        restored = self.service()
        restored.process_admissions(lambda *args: self.fail('A never-opened chat was admitted'))
        self.assertIsNotNone(restored.status(result['attemptId'])['existingSession'])
        self.assertEqual((self.adapter.creates, self.adapter.opens), (1, 0))

    def test_process_loss_after_ui_claim_never_reopens_on_recovery(self):
        s = self.service()
        self.adapter.open_existing = lambda *args: (_ for _ in ()).throw(SystemExit())
        request = self.request(s)
        _, result = s.open(request)
        self.settle(s)
        s.close()
        restored = self.service()
        restored.process_admissions(lambda *args: self.fail('Unknown UI handoff was treated as accepted'))
        restored.open(request)
        out = restored.status(result['attemptId'])
        self.assertEqual(out['uiOpen'], {'attempts': 1, 'outcome': 'unknown'})
        self.assertEqual(out['reason']['code'], 'ui_open_outcome_unknown')
        self.assertIsNotNone(out['existingSession'])
        self.assertEqual(self.adapter.creates, 1)

    def test_recorded_receipt_after_creation_exception_recovers_only_by_read(self):
        s = self.service()
        self.adapter.after_receipt = lambda _: (_ for _ in ()).throw(RuntimeError())
        request = self.request(s)
        _, result = s.open(request)
        self.settle(s)
        s.close()
        restored = self.service()
        restored.process_admissions(lambda *args: self.fail('UI was not opened; no admission'))
        self.assertIsNotNone(restored.status(result['attemptId'])['existingSession'])
        self.assertEqual((self.adapter.creates, self.adapter.opens), (1, 0))

    def test_late_receipt_after_stop_is_retained_but_never_associated(self):
        s = self.service()
        self.adapter.create_release.clear()
        _, result = s.open(self.request(s))
        self.assertTrue(self.adapter.create_entered.wait(1))
        stopped = s.observe(result['attemptId'], {'action': 'stop'})
        self.assertEqual(stopped['observation'], 'stopped')
        self.adapter.create_release.set()
        self.settle(s)
        calls = []
        s.process_admissions(lambda *args: calls.append(args))
        out = s.status(result['attemptId'])
        self.assertEqual(calls, [])
        self.assertEqual(out['observation'], 'stopped')
        self.assertIsNotNone(out['existingSession'])

    def test_admission_and_stop_linearize(self):
        s = self.service()
        _, result = s.open(self.request(s))
        self.settle(s)
        entered, release = threading.Event(), threading.Event()
        def admit(identity, folder, permit):
            entered.set()
            release.wait(2)
            return 'x:' + identity.native_id if permit() else None
        worker = threading.Thread(target=lambda: s.process_admissions(admit))
        worker.start()
        self.assertTrue(entered.wait(1))
        outputs = []
        stopper = threading.Thread(target=lambda: outputs.append(s.observe(result['attemptId'], {'action': 'stop'})))
        stopper.start()
        release.set()
        worker.join(2)
        stopper.join(2)
        self.assertEqual(outputs[0]['state'], 'added')

    def test_timeout_continue_stop_no_native_retry_and_no_window_extension_on_restart(self):
        s = self.service()
        _, result = s.open(self.request(s))
        self.settle(s)
        self.clocks.advance(120)
        self.assertEqual(s.status(result['attemptId'])['state'], 'timeout')
        continued = s.observe(result['attemptId'], {'action': 'continue'})
        deadline = continued['deadlineAt']
        self.clocks.advance(25)
        s.close()
        restored = self.service()
        self.assertEqual(restored.status(result['attemptId'])['deadlineAt'], deadline)
        restored.observe(result['attemptId'], {'action': 'stop'})
        self.clocks.advance(120)
        restored.process_admissions(lambda *args: self.fail('Stopped request admitted'))
        self.assertEqual(self.adapter.creates, 1)

    def test_keep_waiting_is_offered_only_where_continue_can_run(self):
        s = self.service()
        _, result = s.open(self.request(s))
        self.settle(s)
        self.clocks.advance(120)
        view = s.status(result['attemptId'])
        self.assertEqual(view['state'], 'timeout')
        self.assertTrue(view['capabilities']['keepWaiting'])
        s.close()
        off = self.service(enabled=False)
        view = off.status(result['attemptId'])
        self.assertEqual(view['state'], 'timeout')
        self.assertFalse(view['capabilities']['keepWaiting'], 'a disabled host must not advertise Keep waiting')
        with self.assertRaises(NewChatError):
            off.observe(result['attemptId'], {'action': 'continue'})
        self.assertEqual(off.observe(result['attemptId'], {'action': 'stop'})['observation'], 'stopped', 'Stop stays local')
        off.close()
        on = self.service()
        self.assertFalse(on.status(result['attemptId'])['capabilities']['keepWaiting'], 'stopped stays stopped')
        self.assertEqual(self.adapter.creates, 1)

    def test_expired_intent_cannot_authorize_and_expired_details_redacted(self):
        s = self.service()
        request = self.request(s)
        self.clocks.advance(301)
        with self.assertRaises(NewChatError):
            s.open(request)
        self.assertEqual(self.adapter.creates, 0)
        request = self.request(s)
        _, result = s.open(request)
        self.settle(s)
        self.clocks.advance(RETENTION)
        with self.assertRaises(NewChatError) as caught:
            s.status(result['attemptId'])
        self.assertEqual(caught.exception.status, 410)
        self.assertEqual(s.db.execute('SELECT data FROM attempts').fetchone()[0], '{"expired":true}')
        with self.assertRaises(NewChatError):
            s.open(request)
        self.clocks.advance(RETENTION)
        s.context()
        with self.assertRaises(NewChatError) as caught:
            s.status(result['attemptId'])
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(self.adapter.creates, 1)

    def test_capacity_cannot_be_bypassed_by_stopping_blocked_worker(self):
        s = self.service(max_active=1)
        self.adapter.create_release.clear()
        _, result = s.open(self.request(s))
        self.assertTrue(self.adapter.create_entered.wait(1))
        s.observe(result['attemptId'], {'action': 'stop'})
        another = self.request(s)
        with self.assertRaises(NewChatError) as caught:
            s.open(another)
        self.assertEqual(caught.exception.status, 429)
        self.adapter.create_release.set()
        self.settle(s)

    def test_unknown_observe_ids_do_not_allocate_unbounded_gates(self):
        s = self.service()
        for _ in range(20):
            with self.assertRaises(NewChatError):
                s.observe(str(uuid.uuid4()), {'action': 'stop'})
        self.assertEqual(s.gates, {})

    def test_wrong_folder_receipt_requires_independent_proof_for_mismatch(self):
        s = self.service()
        other = self.root / 'another-project'
        other.mkdir()
        self.adapter.wrong_folder = other
        _, result = s.open(self.request(s))
        self.settle(s)
        out = s.status(result['attemptId'])
        self.assertEqual(out['state'], 'mismatch')
        self.assertEqual(out['mismatch']['displayPath'], str(other))
        self.assertIsNone(out['existingSession'])
        self.assertIsNone(out['session'])
        self.assertEqual(self.adapter.opens, 0)

    def test_wrong_folder_receipt_without_persisted_proof_remains_uncertain(self):
        s = self.service()
        other = self.root / 'another-project'
        other.mkdir()
        self.adapter.wrong_folder = other
        self.adapter.verify_existing = lambda *args: None
        _, result = s.open(self.request(s))
        self.settle(s)
        out = s.status(result['attemptId'])
        self.assertEqual(out['state'], 'waiting')
        self.assertIsNone(out['mismatch'])
        self.assertEqual(self.adapter.opens, 0)

    def test_late_wrong_folder_identity_cannot_override_timeout(self):
        s = self.service()
        other = self.root / 'another-project'
        other.mkdir()
        self.adapter.wrong_folder = other
        self.adapter.create_release.clear()
        _, result = s.open(self.request(s))
        self.assertTrue(self.adapter.create_entered.wait(1))
        self.clocks.advance(120)
        self.adapter.create_release.set()
        self.settle(s)
        out = s.status(result['attemptId'])
        self.assertEqual(out['state'], 'timeout')
        self.assertIsNone(out['mismatch'])
        self.assertIsNone(out['existingSession'])

    def test_native_id_cannot_bind_a_second_attempt(self):
        s = self.service()
        _, first = s.open(self.request(s))
        self.settle(s)
        original = s.status(first['attemptId'])['existingSession']['id']
        s.observe(first['attemptId'], {'action': 'stop'})
        identity = self.adapter.identities[original]
        self.adapter.make_identity = lambda folder=None: identity
        _, second = s.open(self.request(s))
        self.settle(s)
        out = s.status(second['attemptId'])
        self.assertIsNone(out['existingSession'])
        self.assertEqual(self.adapter.opens, 1)
        with s.lock:
            self.assertIsNone(s._load(second['attemptId'])['native_id'])
        self.assertEqual(s.status(first['attemptId'])['existingSession']['id'], original)

    def test_wall_clock_discontinuity_stops_observation(self):
        s = self.service()
        _, result = s.open(self.request(s))
        self.settle(s)
        self.clocks.wall -= 30
        out = s.status(result['attemptId'])
        self.assertEqual((out['state'], out['reason']['code']), ('timeout', 'clock_uncertain'))

    def test_receipt_write_failure_prevents_later_adapter_mutations(self):
        s = self.service()
        s._receipt = lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError('fictional disk full'))
        named = []
        self.adapter.after_receipt = lambda identity: named.append(identity)
        _, result = s.open(self.request(s))
        self.settle(s)
        self.assertEqual(named, [])
        self.assertEqual(self.adapter.opens, 0)
        self.assertEqual(s.status(result['attemptId'])['creation']['outcome'], 'unknown')

    def test_final_publication_permit_checks_deadline_before_side_effect(self):
        s = self.service()
        _, result = s.open(self.request(s))
        self.settle(s)
        publications = []
        def admit(identity, folder, permit):
            self.clocks.advance(120)
            if not permit():
                return None
            publications.append(identity.native_id)
            return 'x:' + identity.native_id
        s.process_admissions(admit)
        self.assertEqual(s.status(result['attemptId'])['state'], 'timeout')
        self.assertEqual(publications, [])

    def test_permitted_publication_is_not_retroactively_expired_after_callback(self):
        s = self.service()
        _, result = s.open(self.request(s))
        self.settle(s)
        def admit(identity, folder, permit):
            self.assertTrue(permit())
            self.clocks.advance(120)
            return 'x:' + identity.native_id
        s.process_admissions(admit)
        self.assertEqual(s.status(result['attemptId'])['state'], 'added')

    def test_folder_replacement_between_prepare_and_open_rejects_before_native(self):
        s = self.service()
        request = self.request(s)
        self.folder.rename(self.root / 'previous-project')
        self.folder.mkdir()
        with self.assertRaises(NewChatError):
            s.open(request)
        self.assertEqual(self.adapter.creates, 0)

    def test_two_process_writers_are_rejected_and_hmac_survives_restart(self):
        s = self.service()
        key = s.key
        with self.assertRaises((OSError, ValueError)):
            self.service()
        s.close()
        restored = self.service()
        self.assertEqual(restored.key, key)

    def test_adapter_unavailable_settings_reject_before_any_claim(self):
        s = self.service()
        self.adapter.available = False
        folder = s.context()['folders'][0]
        with self.assertRaises(NewChatError) as caught:
            s.prepare({'provider': 'codex', 'folderId': folder['id'], 'folderRevision': folder['revision']})
        self.assertEqual(caught.exception.code, 'native_defaults_unverified')
        self.assertEqual(self.adapter.creates, 0)


class WorkAdapter(FakeAdapter):
    def __init__(self, *args):
        super().__init__(*args)
        self.submissions = 0
        self.turn_id = str(uuid.uuid4())
        self.submit_entered, self.submit_release = threading.Event(), threading.Event()
        self.submit_release.set()
        self.lost_reply = False
        self.completed = False
        self.owner_missing = False

    def preflight(self):
        return Capability(True, version=self.version, supports_work=True)

    def start_work_once(self, identity, instruction, claim, receipt):
        from new_chat_types import ExecutionResult
        self.submit_entered.set()
        self.submit_release.wait(3)
        if self.owner_missing:
            return ExecutionResult('not_attempted', code='owner_unavailable')
        try:
            claim()
        except NewChatError:
            return ExecutionResult('not_attempted')
        self.submissions += 1
        if self.lost_reply:
            return ExecutionResult('unknown', code='execution_unknown')
        receipt(self.turn_id)
        return ExecutionResult('accepted', self.turn_id)

    def execution_status(self, identity, turn_id):
        from new_chat_types import ExecutionResult
        return ExecutionResult('completed', turn_id) if self.completed else None


@unittest.skipIf(os.name == 'nt', 'New chat uses private POSIX storage; Windows native launch is unavailable.')
class WorkingServiceTests(unittest.TestCase):
    setUp = ServiceTests.setUp
    cleanup_services = ServiceTests.cleanup_services
    service = ServiceTests.service

    def working(self):
        self.adapter = WorkAdapter(self.root, self.folder)
        s = self.service()
        folder = s.context()['folders'][0]
        choice = dict(provider='codex', folderId=folder['id'], folderRevision=folder['revision'], mode='work')
        intent = s.prepare(choice)
        return s, dict(choice, requestId=intent['requestId'], instruction='FICTIONAL_PRIVATE_INSTRUCTION')

    def settled(self, service, attempt_id):
        wait_for(lambda: attempt_id not in service.jobs)
        return service.status(attempt_id)

    def test_private_anchor_retarget_before_instruction_claim_prevents_submission(self):
        service, request = self.working()
        self.adapter.submit_release.clear()
        try:
            _, attempt = service.open(request)
            self.assertTrue(self.adapter.submit_entered.wait(3))
            (self.root / '.codex').symlink_to(self.folder, target_is_directory=True)
        finally:
            self.adapter.submit_release.set()
        final = self.settled(service, attempt['attemptId'])
        self.assertEqual(final['execution']['attempts'], 0)
        self.assertEqual(self.adapter.submissions, 0)

    def test_explicit_mode_instruction_validation_and_backwards_compatible_blank(self):
        service, request = self.working()
        for change in ({'mode': 'other'}, {'instruction': ''}, {'instruction': '  '},
                       {'instruction': 'x' * 8001}, {'draft': 'not an instruction'}, {'instruction': None}):
            with self.assertRaises(NewChatError):
                service.open(dict(request, **change))
        with self.assertRaises(NewChatError) as caught:
            service.open(dict(request, mode='blank', instruction='fictional'))
        self.assertEqual(caught.exception.code, 'invalid_request')
        self.assertEqual(self.adapter.creates, 0)

    def test_work_mode_is_frozen_before_any_mutation_and_not_a_draft(self):
        service, request = self.working()
        blank = dict(request, mode='blank')
        blank.pop('instruction')
        with self.assertRaises(NewChatError) as caught:
            service.open(blank)
        self.assertEqual(caught.exception.code, 'intent_conflict')
        self.assertEqual(self.adapter.creates, 0)
        _, initial = service.open(request)
        status = self.settled(service, initial['attemptId'])
        self.assertEqual(status['mode'], 'work')
        self.assertEqual(status['draft']['disposition'], 'not_provided')
        self.assertEqual(status['execution'], {'attempts': 1, 'outcome': 'accepted',
                         'turnId': self.adapter.turn_id, 'receiptKind': 'native_turn', 'reason': None, 'pending': False})
        self.assertEqual((self.adapter.creates, self.adapter.submissions, self.adapter.opens), (1, 1, 1))
        raw = service.db.execute('SELECT data FROM attempts').fetchone()[0]
        self.assertNotIn('PRIVATE_INSTRUCTION', raw)
        self.assertNotIn('PRIVATE_INSTRUCTION', json.dumps(status))

    def test_recovery_by_request_is_read_only_before_and_after_lost_post(self):
        service, request = self.working()
        recovered = service.request_status(request['requestId'])
        self.assertEqual((recovered['status'], recovered['attempt']), ('prepared', None))
        self.assertEqual((self.adapter.creates, self.adapter.submissions), (0, 0))
        _, initial = service.open(request)
        final = self.settled(service, initial['attemptId'])
        recovered = service.request_status(request['requestId'])
        self.assertEqual(recovered['status'], 'attempted')
        self.assertEqual(recovered['attempt'], final)
        for _ in range(3):
            code, same = service.open(request)
            self.assertEqual((code, same['attemptId']), (200, initial['attemptId']))
        self.assertEqual((self.adapter.creates, self.adapter.submissions), (1, 1))
        with self.assertRaises(NewChatError) as caught:
            service.open(dict(request, instruction='different task'))
        self.assertEqual(caught.exception.code, 'replay_conflict')

    def test_lost_submission_reply_and_restart_never_resend_or_recreate(self):
        service, request = self.working()
        self.adapter.lost_reply = True
        _, first = service.open(request)
        final = self.settled(service, first['attemptId'])
        self.assertEqual(final['execution']['outcome'], 'unknown')
        self.assertIsNone(final['execution']['turnId'])
        service.close()
        restarted = self.service()
        recovered = restarted.request_status(request['requestId'])['attempt']
        self.assertEqual(recovered['execution'], final['execution'])
        restarted.open(request)
        self.assertEqual((self.adapter.creates, self.adapter.submissions), (1, 1))

    def test_execution_survives_observation_timeout_stop_and_restart(self):
        service, request = self.working()
        _, first = service.open(request)
        final = self.settled(service, first['attemptId'])
        self.clocks.advance(121)
        final = service.status(first['attemptId'])
        self.assertEqual(final['state'], 'timeout')
        self.assertEqual(final['execution']['outcome'], 'accepted')
        service.observe(first['attemptId'], {'action': 'stop'})
        service.close()
        restarted = self.service()
        self.assertEqual(restarted.status(first['attemptId'])['execution']['outcome'], 'unknown')
        self.adapter.completed = True
        final = restarted.status(first['attemptId'])
        self.assertEqual(final['execution']['outcome'], 'completed')
        self.assertEqual(final['observation'], 'stopped')
        self.assertEqual((self.adapter.creates, self.adapter.submissions), (1, 1))

    def test_no_owner_means_blank_exists_instruction_not_submitted(self):
        service, request = self.working()
        self.adapter.owner_missing = True
        _, first = service.open(request)
        final = self.settled(service, first['attemptId'])
        self.assertEqual(final['creation']['outcome'], 'verified')
        self.assertEqual(final['execution']['attempts'], 0)
        self.assertEqual(final['execution']['outcome'], 'not_attempted')
        self.assertEqual(final['execution']['reason']['code'], 'owner_unavailable')
        self.assertEqual(self.adapter.submissions, 0)

    def test_folder_mismatch_never_submits_first_turn(self):
        service, request = self.working()
        wrong = self.root / 'other-fictional-project'
        wrong.mkdir()
        self.adapter.wrong_folder = wrong
        _, first = service.open(request)
        final = self.settled(service, first['attemptId'])
        self.assertEqual(final['state'], 'mismatch')
        self.assertEqual(final['execution']['attempts'], 0)
        self.assertEqual((self.adapter.opens, self.adapter.submissions), (0, 0))

    def test_deadline_before_first_turn_claim_prevents_late_submission(self):
        service, request = self.working()
        self.adapter.submit_release.clear()
        _, first = service.open(request)
        self.assertTrue(self.adapter.submit_entered.wait(1))
        self.clocks.advance(121)
        self.adapter.submit_release.set()
        final = self.settled(service, first['attemptId'])
        self.assertEqual(final['state'], 'timeout')
        self.assertEqual(final['execution']['attempts'], 0)
        self.assertEqual(self.adapter.submissions, 0)

    def test_stop_waiting_does_not_cancel_requested_first_turn(self):
        service, request = self.working()
        self.adapter.submit_release.clear()
        _, first = service.open(request)
        self.assertTrue(self.adapter.submit_entered.wait(1))
        service.observe(first['attemptId'], {'action': 'stop'})
        self.adapter.submit_release.set()
        final = self.settled(service, first['attemptId'])
        self.assertEqual(final['execution']['attempts'], 1)
        self.assertEqual(final['observation'], 'stopped')
        self.assertEqual(self.adapter.submissions, 1)

    def test_admission_before_desktop_dispatch_does_not_skip_requested_turn(self):
        service, request = self.working()
        self.adapter.submit_release.clear()
        _, first = service.open(request)
        self.assertTrue(self.adapter.submit_entered.wait(1))
        service.process_admissions(lambda identity, folder, permit: 'x:' + identity.native_id if permit() else None)
        added = service.status(first['attemptId'])
        self.assertEqual(added['state'], 'added')
        self.assertTrue(added['execution']['pending'])
        self.adapter.submit_release.set()
        final = self.settled(service, first['attemptId'])
        self.assertEqual(final['state'], 'added')
        self.assertEqual(final['execution']['attempts'], 1)
        self.assertEqual(self.adapter.submissions, 1)

    def test_concurrent_work_duplicates_share_creation_and_first_turn_claim(self):
        service, request = self.working()
        self.adapter.create_release.clear()
        _, first = service.open(request)
        self.assertTrue(self.adapter.create_entered.wait(1))
        results = []
        workers = [threading.Thread(target=lambda: results.append(service.open(request))) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(1)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(code == 200 and row['attemptId'] == first['attemptId'] for code, row in results))
        self.adapter.create_release.set()
        self.settled(service, first['attemptId'])
        self.assertEqual((self.adapter.creates, self.adapter.submissions), (1, 1))


if __name__ == '__main__':
    unittest.main()
