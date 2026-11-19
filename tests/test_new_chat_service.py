"""Durable New chat behavior using injected fictional native identities only."""
from dataclasses import replace
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
        self.assertTrue(context['canRegisterFolders'])
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
                choice = dict(provider='codex', folderId=healthy['id'], folderRevision=healthy['revision'])
                self.assertIn('requestId', s.prepare(choice))
                self.assertEqual(s.db.execute('SELECT CAST(id AS BLOB),CAST(data AS BLOB) FROM registered_folders ORDER BY id').fetchall(), before)
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
