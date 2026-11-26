"""Scoped Claude peer claims over production HTTP/SQLite; fictional local fixtures."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import chat_directory
import claude_mod as mod
import peer_bridge
import server
import test_claude_mod as owner_fixture

uid = owner_fixture.uid


class ClaudePeerBackendTests(unittest.TestCase):
    http = owner_fixture.ClaudeModTests.http
    register_global = owner_fixture.ClaudeModTests.register_global
    global_poll = owner_fixture.ClaudeModTests.global_poll
    request = owner_fixture.ClaudeModTests.request
    enroll = owner_fixture.ClaudeModTests.enroll

    def setUp(self):
        owner_fixture.ClaudeModTests.setUp(self)
        root = self.base/'claude-projects'/'fictional'; root.mkdir(parents=True)
        self.transcript = root/(self.sid+'.jsonl')
        self.transcript.write_text(json.dumps(dict(type='custom-title', customTitle='Fictional',
                                                   sessionId=self.sid, cwd=self.project))+'\n')
        codex = self.base/'codex'; (codex/'sessions').mkdir(parents=True)
        self.directory = chat_directory.ChatDirectory(str(codex), str(root.parent), policy={'history_days':'all'},
            codex_sessions=str(codex/'sessions'), live_snapshot=lambda:server.shared_directory_snapshot(self.store))
        self.store.shared.directory = self.directory
        with self.store.lock:
            credential = peer_bridge.initialize_service(self.store.db, {'history_days':'all'})
            self.credential = peer_bridge.service_credential(self.store.db, credential['credential_id'])

    def tearDown(self):
        owner_fixture.ClaudeModTests.tearDown(self)

    def setup_global(self, consent=True, protocol=1):
        status, setup = self.http('/claude-mod-setup', {'peer_receive':True} if consent else {}, owner=True)
        self.assertEqual(status,200,setup); self.assertIs(setup['peer_receive'],consent)
        status, self.grant = self.http('/claude-mod/setup', {'code':setup['pair_code']})
        self.assertEqual(status,200,self.grant)
        extra = {} if protocol is None else {'peer_protocol':protocol}
        status, self.registration, self.body = self.register_global(self.grant, **extra)
        self.assertEqual(status,200,self.registration)
        return self.registration

    def target(self):
        for _ in range(30):
            if self.directory.refresh(force=True)['complete']:
                break
        return self.directory.list_agents()['agents'][0]

    def send(self, text='Report the fictional status.', request_id=None):
        target = self.target()
        return self.store.shared.send(self.credential,target['agent_id'],target['identity_revision'],text,
                                      request_id or uid(),'Fictional requester')

    def peer_row(self, request_id):
        tid = self.store.db.execute('SELECT transport_id FROM shared_bridge_requests WHERE client_id=?',(request_id,)).fetchone()[0]
        return next(r for r in self.store._rows() if r['request_id']==tid)

    def folder_poll(self, cwd, accept=True, project=None):
        # Folder protocol 1 (plugin 0.6.4): the poll reports where the chat is working.
        return self.http('/claude-mod/poll', dict(generation=self.generation, native_id=self.sid, client_id=self.body['client_id'],
                                                  project=project or self.project, cwd=cwd, accept=accept), token=self.token)

    def event(self, claim, kind='answered', **extras):
        data = dict(generation=self.generation,native_id=self.sid,client_id=self.body['client_id'],
                    request_id=claim['request_id'],attempt_id=claim['attempt_id'],kind=kind)
        if kind == 'answered':data.update(turn_id='fictional-turn',answer='WATCH_REPLY '+claim['request_id']+'\nFictional reply.')
        data.update(extras)
        return self.http('/claude-mod/event',data,token=self.token)

    def enable_lifecycle(self):
        self.setup_global()
        self.body = dict(self.body, connection_seq=1, lifecycle_protocol=2)
        status, registration = self.http('/claude-mod/register', self.body, token=self.grant['grant'])
        self.assertEqual(status, 200, registration)
        self.generation, self.token = registration['generation'], registration['token']
        self.assertEqual(registration['supported_lifecycle_protocol'], 2)
        self.assertEqual(registration['lifecycle_protocol'], 2)

    def completion(self, claim, outcome='unusable', **extra):
        fields = dict(turn_id='fictional-turn', outcome=outcome, answer='', event_id=uid(), occurred_at=100000)
        if outcome == 'answered': fields['answer'] = 'WATCH_REPLY '+claim['request_id']+'\nFictional reply.'
        fields.update(extra)
        return self.event(claim, 'completed', **fields)

    def install_owner_guards(self):
        from attachments_owner_guards import OwnerAttachmentGuard, install_for_test
        from attachments_owner_transport import OwnerAttachmentLedger
        OwnerAttachmentLedger(self.store.db, self.store.lock)
        with peer_bridge.transaction(self.store.db, self.store.lock):
            install_for_test(self.store.db)
        self.store._attachment_guard = OwnerAttachmentGuard.probe(self.store.db, lock=self.store.lock)

    def check_held_completion(self, *, installed):
        import peer_lifecycle as life
        self.enable_lifecycle()
        sent = self.send()
        claim = self.global_poll(self.body['client_id'])[1]['claim']
        self.assertEqual(self.event(claim, 'started', turn_id='fictional-turn')[0], 200)
        identity = claim['request_id']
        row = self.peer_row(sent['request_id'])
        damaged = json.dumps(dict(row, question='Fictional café.'), ensure_ascii=False)
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE requests SET data=? WHERE id=?', (damaged, identity))
        if installed:
            self.install_owner_guards()
        db = self.store.db
        before = tuple(db.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
                       for table in ('shared_bridge_requests', 'peer_execution', 'peer_execution_events'))
        self.assertEqual(self.store._peer_payload(identity), ('uncertain', None))
        status, response = self.completion(claim, 'withheld')
        self.assertEqual(status, 409, response)
        self.assertIn('does not match its original claim', response['error'])
        self.assertEqual(tuple(db.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
                               for table in ('shared_bridge_requests', 'peer_execution', 'peer_execution_events')), before)
        self.assertEqual(db.execute('SELECT data FROM requests WHERE id=?', (identity,)).fetchone(), (damaged,))
        self.assertEqual([entry[:2] for entry in life.blockers(db, self.aid)], [(identity, 'running')])
        public = self.store.shared.result(sent['request_id'])
        self.assertEqual((public['state'], public['reply']), ('unknown', ''))
        with self.assertRaises(peer_bridge.PeerError) as blocker:
            self.send('Fictional follow-up must remain blocked.')
        self.assertEqual(blocker.exception.status, 429)
        self.assertNotIn(identity, str(blocker.exception))
        self.assertIn('The new request was not accepted', str(blocker.exception))
        self.assertEqual(db.execute('SELECT count(*) FROM shared_bridge_requests').fetchone(), (1,))

    def test_held_body_cannot_be_reconstructed_as_pruned_uninstalled(self):
        self.check_held_completion(installed=False)

    def test_held_body_cannot_be_reconstructed_as_pruned_installed(self):
        self.check_held_completion(installed=True)

    def test_installed_genuinely_pruned_completion_keeps_existing_receipt_path(self):
        import peer_lifecycle as life
        self.install_owner_guards()
        self.enable_lifecycle()
        sent = self.send()
        claim = self.global_poll(self.body['client_id'])[1]['claim']
        self.assertEqual(self.event(claim, 'started', turn_id='fictional-turn')[0], 200)
        identity = claim['request_id']
        with self.store.lock, self.store.db:
            self.store.db.execute('DELETE FROM requests WHERE id=?', (identity,))
        self.assertEqual(self.store._peer_payload(identity), ('clear', None))
        status, response = self.completion(claim, 'withheld')
        self.assertEqual(status, 200, response)
        self.assertEqual(self.store._peer_payload(identity), ('clear', None))
        self.assertEqual(life.get(self.store.db, identity)['execution'], 'completed')
        self.assertEqual(life.blockers(self.store.db, self.aid), [])
        public = self.store.shared.result(sent['request_id'])
        self.assertEqual((public['state'], public['reply']), ('unknown', ''))

    def test_owner_retirement_v1_late_answer_is_acknowledged_without_capture_or_reclaim(self):
        import peer_lifecycle as life
        self.setup_global(); sent = self.send()
        claim = self.global_poll(self.body['client_id'])[1]['claim']
        self.assertEqual(self.event(claim, 'started', turn_id='fictional-turn')[0], 200)
        with self.store.lock, self.store.db:
            retired = life.retire(self.store.db, claim['request_id'], life.RETIRE_REASON)
        before = self.store.db.execute('SELECT count(*) FROM shared_bridge_requests').fetchone()[0]
        for _ in range(2):
            status, response = self.event(claim)
            self.assertEqual(status, 200, response); self.assertTrue(response['retired'])
            self.assertIn('Provider outcome remains unknown', response['message'])
        self.assertEqual(life.get(self.store.db, claim['request_id']), retired)
        result = self.store.shared.result(sent['request_id'])
        self.assertEqual(result['state'], 'unknown'); self.assertEqual(result['reply'], '')
        self.assertIn('retired by the owner', result['error'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM shared_bridge_requests').fetchone()[0], before)
        # Native turn/attempt authentication remains mandatory after retirement.
        self.assertEqual(self.event(claim, turn_id='different-turn')[0], 409)
        self.assertEqual(self.event(dict(claim, attempt_id=uid()))[0], 409)
        followup = self.send('A separately authorized fictional follow-up.')
        new_claim = self.global_poll(self.body['client_id'])[1]['claim']
        self.assertEqual(new_claim['request_id'], self.peer_row(followup['request_id'])['request_id'])
        self.assertNotEqual(new_claim['request_id'], claim['request_id'])
        self.assertIsNone(self.global_poll(self.body['client_id'])[1]['claim'])

    def test_owner_retirement_v2_pruned_completion_remains_unknown_withheld(self):
        import peer_lifecycle as life
        self.enable_lifecycle(); sent = self.send()
        claim = self.global_poll(self.body['client_id'])[1]['claim']
        self.assertEqual(self.event(claim, 'started', turn_id='fictional-turn')[0], 200)
        with self.store.lock, self.store.db:
            retired = life.retire(self.store.db, claim['request_id'], life.RETIRE_REASON)
            self.store.db.execute('DELETE FROM requests WHERE id=?', (claim['request_id'],))
        event_id = uid()
        for _ in range(2):
            status, response = self.completion(claim, 'answered', event_id=event_id)
            self.assertEqual(status, 200, response); self.assertTrue(response['retired'])
        self.assertEqual(life.get(self.store.db, claim['request_id']), retired)
        self.assertIsNone(self.store.db.execute('SELECT data FROM requests WHERE id=?', (claim['request_id'],)).fetchone())
        result = self.store.shared.result(sent['request_id'])
        self.assertEqual(result['state'], 'unknown'); self.assertEqual(result['reply'], '')
        self.assertIn('retired by the owner', result['error'])
        self.assertEqual(self.event(claim, 'unknown')[0], 200)
        self.assertEqual(self.event(claim, 'dropped')[0], 200)
        self.assertEqual(life.get(self.store.db, claim['request_id']), retired)

    def test_v2_terminal_execution_releases_capacity_and_cannot_regress(self):
        import peer_lifecycle
        self.enable_lifecycle(); sent = self.send()
        claim = self.global_poll(self.body['client_id'])[1]['claim']
        self.assertEqual(claim['lifecycle_protocol'], 2)
        self.assertEqual(self.event(claim, 'started', turn_id='fictional-turn')[0], 200)
        self.assertEqual(self.completion(claim)[0], 200)
        row = self.peer_row(sent['request_id'])
        self.assertEqual(row['state'], 'unknown'); self.assertTrue(row['settled'])
        followup = self.send('A separately authorized follow-up.')
        for kind, extra in [('unknown', {}), ('started', {'turn_id':'fictional-turn'})]:
            self.assertEqual(self.event(claim, kind, **extra)[0], 200)
        self.assertEqual(peer_lifecycle.get(self.store.db, claim['request_id'])['execution'], 'completed')
        self.assertEqual(self.peer_row(sent['request_id'])['state'], 'unknown')
        self.assertEqual(self.global_poll(self.body['client_id'])[1]['claim']['request_id'], self.peer_row(followup['request_id'])['request_id'])

    def test_v2_answer_ack_idempotency_conflict_and_withholding(self):
        self.enable_lifecycle(); sent = self.send(); claim = self.global_poll(self.body['client_id'])[1]['claim']
        event_id = uid()
        self.assertEqual(self.completion(claim, 'answered', event_id=event_id)[0], 200)
        self.assertEqual(self.completion(claim, 'answered', event_id=event_id)[0], 200)
        self.assertEqual(self.completion(claim, 'aborted', event_id=event_id)[0], 409)
        with self.store.lock, self.store.db:
            self.store.db.execute('DELETE FROM peer_execution_events')
        self.assertEqual(self.completion(claim, 'aborted', event_id=event_id)[0], 409)
        self.assertEqual(self.store.shared.result(sent['request_id'])['reply'], 'Fictional reply.')
        other = self.send(); claim = self.global_poll(self.body['client_id'])[1]['claim']
        self.assertEqual(self.completion(claim, 'withheld')[0], 200)
        self.assertEqual(self.event(claim)[0], 200)
        result = self.store.shared.result(other['request_id'])
        self.assertEqual(result['state'], 'unknown'); self.assertEqual(result['reply'], '')
        self.assertIn('withheld', result['error'])

    def test_v2_invalid_answer_settles_original_execution_without_capturing(self):
        import peer_lifecycle
        self.enable_lifecycle()
        for suffix in ('', '\n \t\x1f\u00a0\u2009\u3000', '\nMentions WATCH_REPLY in prose.',
                       '\n\x00', '\n\ud800', '\nx'*16000):
            sent = self.send()
            claim = self.global_poll(self.body['client_id'])[1]['claim']
            self.assertEqual(self.event(claim, 'started', turn_id='fictional-turn')[0], 200)
            data = dict(outcome='answered', answer='WATCH_REPLY '+claim['request_id']+suffix, event_id=uid())
            self.assertEqual(self.completion(claim, **data, turn_id='another-turn')[0], 409)
            self.assertEqual(self.completion(dict(claim, attempt_id=uid()), **data)[0], 409)
            self.assertEqual(self.store.shared.result(sent['request_id'])['state'], 'awaiting_reply')
            self.assertEqual(self.completion(claim, **data)[0], 200)
            self.assertEqual(self.completion(claim, **data)[0], 200)
            self.assertEqual(self.completion(claim, **dict(data, answer='changed'))[0], 409)
            result = self.store.shared.result(sent['request_id'])
            self.assertEqual((result['state'], result['reply']), ('unknown', ''))
            self.assertIn('turn completed', result['error'])
            lifecycle = peer_lifecycle.get(self.store.db, claim['request_id'])
            self.assertEqual((lifecycle['execution'], lifecycle['capture']), ('completed', 'unusable'))
            self.assertEqual(peer_lifecycle.blockers(self.store.db, self.aid), [])

    def test_recoverable_turn_proof_comes_from_original_execution_and_survives_pruning(self):
        self.enable_lifecycle()
        sent = self.send()
        claim = self.global_poll(self.body['client_id'])[1]['claim']
        self.assertEqual(self.global_poll(self.body['client_id'])[1]['recoverable_turns'], [])
        self.assertEqual(self.event(claim, 'started', turn_id='fictional-turn')[0], 200)
        expected = dict(request_id=claim['request_id'], attempt_id=claim['attempt_id'],
                        generation=self.generation, turn_id='fictional-turn')
        old_generation = self.generation
        self.body = dict(self.body, connection_seq=2)
        status, registration = self.http('/claude-mod/register', self.body, token=self.grant['grant'])
        self.assertEqual(status, 200)
        self.generation = registration['generation']
        self.assertNotEqual(self.generation, old_generation)
        self.assertEqual(self.global_poll(self.body['client_id'])[1]['recoverable_turns'], [expected])
        with self.store.lock, self.store.db:
            row = self.peer_row(sent['request_id'])
            row['created'] -= 86410
            self.store.db.execute('UPDATE shared_bridge_requests SET created=? WHERE client_id=?',
                                  (row['created'], sent['request_id']))
            self.store.mods.put(row)
            self.store._prune(self.store._rows(), time.time())
        self.assertEqual(self.global_poll(self.body['client_id'])[1]['recoverable_turns'], [expected])
        self.assertEqual(self.completion(claim, 'withheld', generation=old_generation)[0], 409)
        self.assertEqual(self.completion(claim, 'withheld')[0], 200)
        self.assertEqual(self.global_poll(self.body['client_id'])[1]['recoverable_turns'], [])

    def test_v2_original_completion_after_payload_pruning_releases_without_republishing(self):
        import peer_lifecycle
        self.enable_lifecycle(); sent = self.send(); claim = self.global_poll(self.body['client_id'])[1]['claim']
        self.assertEqual(self.event(claim, 'started', turn_id='fictional-turn')[0], 200)
        with self.store.lock, self.store.db:
            row = self.peer_row(sent['request_id']); row['created'] -= 86410
            self.store.db.execute('UPDATE shared_bridge_requests SET created=? WHERE client_id=?', (row['created'], sent['request_id']))
            self.store.mods.put(row)
            self.store._prune(self.store._rows(), time.time())
        self.assertIsNone(self.store.db.execute('SELECT data FROM requests WHERE id=?', (claim['request_id'],)).fetchone())
        # Content expiry does not invalidate the retained exact execution binding.
        with self.assertRaisesRegex(peer_bridge.PeerError, 'Busy with request'):
            self.send()
        self.assertEqual(self.completion(dict(claim, attempt_id=uid()))[0], 409)
        self.assertEqual(self.completion(claim, 'answered')[0], 200)
        self.assertEqual(peer_lifecycle.get(self.store.db, claim['request_id'])['execution'], 'completed')
        self.assertIsNone(self.store.db.execute('SELECT data FROM requests WHERE id=?', (claim['request_id'],)).fetchone())
        self.assertEqual(self.store.shared.result(sent['request_id'])['reply'], '')
        self.send()

    def test_pruned_claude_claim_with_compatible_bound_turn_is_excluded_from_codex_hold(self):
        import peer_lifecycle as life

        self.enable_lifecycle()
        sent = self.send()
        public_id = sent['request_id']
        claim = self.global_poll(self.body['client_id'])[1]['claim']
        transport_id = claim['request_id']
        turn_id = 'fictional-turn'
        self.assertEqual(self.event(claim, 'started', turn_id=turn_id)[0], 200)

        now = time.time()
        with self.store.lock, self.store.db:
            row = self.peer_row(public_id)
            self.assertEqual(row['request_id'], transport_id)
            self.assertEqual(row['agent'], self.aid)
            self.assertEqual(row['mod_turn'], turn_id)
            row['created'] = now - peer_bridge.RETENTION - 1
            # The persisted payload schema allows both turn fields. Exercise that
            # compatibility so the bound-turn check cannot mask a missing provider filter.
            row['bound_turn'] = turn_id
            self.store.db.execute('UPDATE shared_bridge_requests SET created=? WHERE client_id=?',
                                  (row['created'], public_id))
            self.store.mods.put(row)
            before = life.get(self.store.db, transport_id)
            self.assertEqual((before['execution'], before['capture']), ('running', 'pending'))
            self.assertEqual(before['evidence']['bound_turn'], turn_id)
            self.assertIs(before['evidence']['launched'], True)
            self.store._prune(self.store._rows(), now)

        retained = life.get(self.store.db, transport_id)
        self.assertNotIn('codex_retention_hold', retained['evidence'], life.CODEX_RETENTION_HOLD)
        self.assertEqual((retained['execution'], retained['capture']), ('running', 'pending'))
        self.assertIsNone(self.store.db.execute('SELECT 1 FROM requests WHERE id=?', (transport_id,)).fetchone())
        with self.assertRaisesRegex(peer_bridge.PeerError, 'Busy with request'):
            self.send('Fictional follow-up.')

        status, receipt = self.completion(claim, 'answered', turn_id=turn_id)
        self.assertEqual(status, 200, receipt)
        self.assertEqual((receipt['request_id'], receipt['turn_id']), (transport_id, turn_id))
        completed = life.get(self.store.db, transport_id)
        self.assertEqual((completed['execution'], completed['capture']), ('completed', 'unusable'))
        self.assertIsNone(self.store.db.execute('SELECT 1 FROM requests WHERE id=?', (transport_id,)).fetchone())
        result = self.store.shared.result(public_id)
        self.assertEqual((result['state'], result['text_preview'], result['reply']), ('expired', '', ''))
        followup = self.send('Fictional follow-up.')
        next_claim = self.global_poll(self.body['client_id'])[1]['claim']
        self.assertEqual(next_claim['request_id'], self.peer_row(followup['request_id'])['request_id'])

    def test_dropped_execution_rejects_v1_and_v2_contradictory_answers(self):
        import peer_lifecycle
        self.enable_lifecycle(); sent = self.send()
        claim = self.global_poll(self.body['client_id'])[1]['claim']
        self.assertEqual(self.event(claim, 'dropped')[0], 200)
        self.assertEqual(self.event(claim)[0], 409)
        self.assertEqual(self.completion(claim, 'answered')[0], 409)
        record = peer_lifecycle.get(self.store.db, claim['request_id'])
        self.assertEqual(record['execution'], 'dropped')
        self.assertNotEqual(record['capture'], 'captured')
        self.assertEqual(self.store.shared.result(sent['request_id'])['state'], 'failed')
        self.assertEqual(self.store.shared.result(sent['request_id'])['reply'], '')

    def test_drop_after_started_then_unknown_is_rejected_using_original_turn(self):
        import peer_lifecycle as life
        self.enable_lifecycle(); sent = self.send()
        claim = self.global_poll(self.body['client_id'])[1]['claim']
        self.assertEqual(self.event(claim, 'started', turn_id='fictional-turn')[0], 200)
        self.assertEqual(self.event(claim, 'unknown')[0], 200)
        self.assertEqual(self.peer_row(sent['request_id'])['state'], 'unknown')
        self.assertEqual(self.event(claim, 'dropped')[0], 409)
        self.assertEqual(life.get(self.store.db, claim['request_id'])['execution'], 'uncertain')
        with self.assertRaises(peer_bridge.PeerError): self.send()
        self.assertEqual(self.completion(claim, 'withheld')[0], 200)
        self.assertEqual(self.event(claim, 'dropped')[0], 409)
        self.assertEqual(life.get(self.store.db, claim['request_id'])['execution'], 'completed')

    def test_dropped_late_ack_preserves_full_payload_on_v1_v2_and_restart(self):
        import peer_lifecycle as life
        for v2 in (False, True):
            with self.subTest(v2=v2):
                if v2: self.enable_lifecycle()
                else: self.setup_global()
                sent = self.send(); claim = self.global_poll(self.body['client_id'])[1]['claim']
                original = self.peer_row(sent['request_id'])
                self.assertEqual(self.event(claim, 'dropped')[0], 200)
                preserved = self.peer_row(sent['request_id'])
                for field in ('question', 'envelope', 'name', 'identity_revision', 'target_identity', 'mod_peer_envelope_limit'):
                    self.assertIn(field, original)
                    self.assertEqual(preserved[field], original[field])
                for kind in ('dropped', 'dropped', 'submitted', 'unknown'):
                    self.assertEqual(self.event(claim, kind)[0], 200)
                    self.assertEqual(self.peer_row(sent['request_id']), preserved)
                # Startup migration may project terminal state, but must preserve
                # every original request field used for exact identity/correlation.
                with self.store.lock, self.store.db:
                    life.migrate(self.store.db)
                restarted = self.peer_row(sent['request_id'])
                for field in original:
                    if field not in ('state', 'reply', 'error'):
                        self.assertEqual(restarted[field], original[field], field)
                for kind in ('unknown', 'submitted', 'dropped'):
                    self.assertEqual(self.event(claim, kind)[0], 200)
                    self.assertEqual(self.peer_row(sent['request_id']), restarted)
                self.assertEqual(self.store.shared.result(sent['request_id'])['state'], 'failed')
                self.assertEqual(life.get(self.store.db, claim['request_id'])['execution'], 'dropped')

    def test_authentic_prestart_drop_after_expiry_or_prune_releases_without_resubmit(self):
        import peer_lifecycle as life
        self.enable_lifecycle()
        for pruned in (False, True):
            with self.subTest(pruned=pruned):
                sent = self.send(); claim = self.global_poll(self.body['client_id'])[1]['claim']
                with self.store.lock, self.store.db:
                    row = self.peer_row(sent['request_id']); row['created'] -= 86410 if pruned else 1000
                    self.store.db.execute('UPDATE shared_bridge_requests SET created=? WHERE client_id=?',
                                          (row['created'], sent['request_id']))
                    row['state'] = 'expired'; self.store.mods.put(row)
                    if pruned: self.store._prune(self.store._rows(), time.time())
                with self.assertRaises(peer_bridge.PeerError): self.send()
                self.assertEqual(self.event(dict(claim, attempt_id=uid()), 'dropped')[0], 409)
                self.assertTrue(life.blockers(self.store.db, self.aid))
                for _ in range(2):
                    status, response = self.event(claim, 'dropped')
                    self.assertEqual(status, 200, response); self.assertEqual(response['state'], 'failed')
                record = life.get(self.store.db, claim['request_id'])
                self.assertEqual((record['execution'], record['capture']), ('dropped', 'withheld'))
                self.assertFalse(life.blockers(self.store.db, self.aid))
                self.assertEqual(self.event(claim, 'started', turn_id='fictional-turn')[0], 409)
                self.assertEqual(self.event(claim)[0], 409)
                self.assertEqual(self.completion(claim)[0], 409)
                result = self.store.shared.result(sent['request_id'])
                self.assertEqual(result['state'], 'failed'); self.assertEqual(result['reply'], '')
                self.assertIn('dropped before it started', result['error'])
                count = self.store.db.execute('SELECT count(*) FROM shared_bridge_requests').fetchone()[0]
                duplicate = self.send(request_id=sent['request_id'])
                self.assertTrue(duplicate['duplicate']); self.assertEqual(duplicate['state'], 'failed')
                self.assertEqual(self.store.db.execute('SELECT count(*) FROM shared_bridge_requests').fetchone()[0], count)
                self.assertIsNone(self.global_poll(self.body['client_id'])[1]['claim'])
                if pruned:
                    self.assertIsNone(self.store.db.execute('SELECT data FROM requests WHERE id=?', (claim['request_id'],)).fetchone())

    def test_dropped_receipt_overrules_stale_tick_and_public_payload(self):
        import peer_lifecycle as life
        self.setup_global(); sent = self.send(); claim = self.global_poll(self.body['client_id'])[1]['claim']
        stale = self.peer_row(sent['request_id'])
        self.assertEqual(self.event(claim, 'dropped')[0], 200)
        # Simulate an already-running tracker persisting its old copied row.
        with self.store.lock:
            self.store._save(dict(stale, state='quarantined', candidate_reply='Fictional late reply'))
        saved = self.peer_row(sent['request_id'])
        self.assertEqual(saved['state'], 'failed'); self.assertTrue(saved['settled'])
        self.assertEqual(saved['candidate_reply'], '')
        # Public reads also obey the durable terminal even if external/stale JSON
        # bypassed the normal saver. This must not recapture or reclaim the input.
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE requests SET data=? WHERE id=?',
                                  (json.dumps(dict(stale, state='answered', mod_turn='fictional-stale', reply='Fictional leak')), claim['request_id']))
        self.assertEqual(self.store.shared.result(sent['request_id'])['state'], 'failed')
        self.assertEqual(self.store.shared.result(sent['request_id'])['reply'], '')
        for kind in ('submitted', 'unknown', 'dropped'):
            self.assertEqual(self.event(claim, kind)[0], 200)
        self.assertEqual(life.get(self.store.db, claim['request_id'])['capture'], 'withheld')
        followup = self.send('A distinct fictional request.')
        next_claim = self.global_poll(self.body['client_id'])[1]['claim']
        self.assertEqual(next_claim['request_id'], self.peer_row(followup['request_id'])['request_id'])
        self.assertNotEqual(next_claim['request_id'], claim['request_id'])

    def test_v2_protocol_requires_negotiation_and_downgrade_preflight(self):
        import peer_lifecycle
        self.setup_global(); sent = self.send(); claim = self.global_poll(self.body['client_id'])[1]['claim']
        self.assertEqual(self.completion(claim)[0], 409)
        self.assertEqual(self.event(claim)[0], 200)
        self.enable_lifecycle(); self.send(); claim = self.global_poll(self.body['client_id'])[1]['claim']
        self.assertFalse(peer_lifecycle.downgrade_ready(self.store.db))
        self.assertEqual(self.completion(claim)[0], 200)
        self.assertTrue(peer_lifecycle.downgrade_ready(self.store.db))

    def test_consent_protocol_and_old_manual_or_global_clients_are_independent(self):
        self.enroll()
        self.assertEqual(self.store.mods.authenticate(self.token)['peer_protocol'],0)
        self.assertFalse(self.store.mods.peer_capability(self.aid,self.agents[self.aid])[0])
        with self.store.lock,self.store.db:
            self.store.db.execute('UPDATE claude_mod_enrollments SET active=0,generation=NULL')
        self.setup_global(consent=False)
        self.assertEqual(self.registration['peer_protocol'],1)
        self.assertEqual(self.target()['capability'],'unavailable')
        self.assertFalse(self.store.owner_capabilities(self.aid,self.agents)['claude_setup']['peer_receive'])
        self.setup_global(protocol=None)
        self.assertEqual(self.registration['peer_protocol'],0)
        self.assertEqual(self.target()['capability'],'unavailable')
        old = dict(self.body)
        self.assertEqual(self.http('/claude-mod/register',dict(old,peer_protocol=1),token=self.grant['grant'])[0],409)
        status, upgraded = self.http('/claude-mod/register',dict(old,connection_seq=1,peer_protocol=1),token=self.grant['grant'])
        self.assertEqual(status,200,upgraded)
        self.assertEqual(self.target()['capability'],'reachable')
        status, downgraded = self.http('/claude-mod/register',dict(old,connection_seq=2),token=self.grant['grant'])
        self.assertEqual(status,200,downgraded)
        self.assertEqual(downgraded['peer_protocol'],0)
        self.assertEqual(self.target()['capability'],'unavailable')
        for protocol in (True,2,-1,'1'):
            self.assertEqual(self.http('/claude-mod/register',dict(old,peer_protocol=protocol),token=self.grant['grant'])[0],400)
        for body in ({'peer_receive':False},{'peer_receive':1},{'peer_receive':True,'other':1}):
            self.assertEqual(self.http('/claude-mod-setup',body,owner=True)[0],400)

    def test_production_snapshot_explains_owner_only_mod_without_claiming_readiness(self):
        self.setup_global(consent=False)
        facts=server.shared_directory_snapshot(self.store)[self.aid]
        self.assertEqual(facts['capability'],'unavailable')
        self.assertEqual(facts['supported_transport'],'claude_mod')
        self.assertEqual(facts['mod_enrollment'],self.registration['enrollment_id'])
        self.assertEqual(facts['project'],self.project)
        self.assertIn('explicit Motet setup',facts['reason'])
        target=self.target();self.assertEqual(target['supported_transport'],'claude_mod')
        self.assertEqual(target['capability'],'unavailable')
        self.assertIn('compatible live official plugin',target['reason'])

    def test_exact_peer_claim_answer_dedupe_private_owner_view_and_influence_warning(self):
        self.setup_global(); sent=self.send(); row=self.peer_row(sent['request_id'])
        self.assertIn('Origin: peer.',row['envelope']);self.assertIn('UNVERIFIED',row['envelope'])
        self.assertIn('Do not call send_message',row['envelope']);self.assertNotIn('channel reply tool',row['envelope'])
        self.assertEqual(self.store.snapshot()['requests'],[])
        status,result=self.global_poll(self.body['client_id']);self.assertEqual(status,200,result)
        claim=result['claim'];self.assertEqual(claim['origin'],'peer')
        self.assertEqual(claim['request_id'],row['request_id'])
        self.assertIsNotNone(self.store.db.execute('SELECT launched FROM shared_bridge_requests').fetchone()[0])
        self.assertEqual(self.event(dict(claim,attempt_id=uid()))[0],409)
        self.assertEqual(self.event(claim,answer='Uncorrelated')[0],409)
        self.assertEqual(self.event(claim)[0],200)
        self.assertEqual(self.event(claim)[0],200)
        self.assertEqual(self.event(claim,answer='WATCH_REPLY '+row['request_id']+'\nDifferent')[0],409)
        self.assertEqual(self.store.shared.result(sent['request_id'])['reply'],'Fictional reply.')
        self.assertTrue(self.send(request_id=sent['request_id'])['duplicate'])
        self.assertEqual(self.store.snapshot()['requests'],[])
        owner,_=self.request('Owner question after the peer.')
        claim=self.global_poll(self.body['client_id'])[1]['claim']
        self.assertNotIn('origin',claim)
        self.assertEqual(self.event(claim)[0],200)
        answer=self.store.snapshot()['requests'][0]
        self.assertEqual(answer['request_id'],owner['request_id'])
        self.assertIn(peer_bridge.PEER_WARNING,answer['delivery_warning'])

    def test_compact_escaped_envelope_exact_4000_and_4001_before_reservation(self):
        self.setup_global()
        target=self.target();rid=uid();sender='Fictional requester'
        overhead=len(peer_bridge.shared_envelope(uid(),sender,'','claude','claude_mod'))
        # Each control character expands to six ASCII codepoints; literal padding fills the exact bound.
        controls=(4000-overhead)//6
        text='\x01'*controls+'a'*((4000-overhead)%6)
        self.assertLess(len(text),2000)
        self.assertEqual(len(peer_bridge.shared_envelope(uid(),sender,text,'claude','claude_mod')),4000)
        with self.assertRaisesRegex(peer_bridge.PeerError,'4000'):
            self.store.shared.send(self.credential,target['agent_id'],target['identity_revision'],text+'a',rid,sender)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM shared_bridge_requests').fetchone()[0],0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM requests').fetchone()[0],0)
        sent=self.store.shared.send(self.credential,target['agent_id'],target['identity_revision'],text,rid,sender)
        row=self.peer_row(sent['request_id'])
        self.assertEqual(len(row['envelope']),4000)
        self.assertEqual(json.loads(row['envelope'].splitlines()[1][len('Message: '):]),text)
        self.assertEqual(self.global_poll(self.body['client_id'])[1]['claim']['envelope'],row['envelope'])

    def test_unicode_envelope_rejected_before_reservation_without_consuming_id(self):
        self.setup_global();rid=uid()
        for text in ('😀'*4000,'\x01'*1000):
            with self.assertRaisesRegex(peer_bridge.PeerError,'4000'):
                self.send(text,rid)
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM shared_bridge_requests').fetchone()[0],0)
        self.assertEqual(self.send('Short fictional message.',rid)['request_id'],rid)

    def large_peer_registration(self, limit=100000):
        self.body = dict(self.body, connection_seq=self.body['connection_seq']+1, peer_envelope_limit=limit)
        status, registration = self.http('/claude-mod/register', self.body, token=self.grant['grant'])
        self.assertEqual(status, 200, registration)
        self.generation, self.token = registration['generation'], registration['token']
        self.assertEqual(registration['peer_envelope_limit'], limit)
        return registration

    def test_peer_capacity_is_explicit_persisted_and_strictly_negotiated(self):
        self.setup_global()
        self.assertEqual(self.registration['supported_peer_envelope_limit'], 100000)
        self.assertEqual(self.registration['peer_envelope_limit'], 4000)
        for limit in (True, '100000', 99999, 100001, None):
            self.assertEqual(self.http('/claude-mod/register', dict(self.body, connection_seq=1,
                peer_envelope_limit=limit), token=self.grant['grant'])[0], 400)
        self.assertEqual(self.http('/claude-mod/register', dict(self.body,
            peer_envelope_limit=100000), token=self.grant['grant'])[0], 409)
        registration = self.large_peer_registration()
        self.assertEqual(self.store.mods.get(registration['enrollment_id'])['peer_envelope_limit'], 100000)

    def test_large_peer_claim_is_exact_and_owner_limit_is_unchanged(self):
        self.setup_global(); self.large_peer_registration()
        for text in ('a'*16000, '😀'*16000, '\x01'*16000):
            with self.subTest(kind=text[0]):
                sent = self.send(text)
                row = self.peer_row(sent['request_id'])
                self.assertEqual(row['question'], text)
                self.assertLessEqual(len(row['envelope'].encode('utf-8')), 100000)
                status, data = self.global_poll(self.body['client_id'])
                self.assertEqual(status, 200)
                self.assertEqual(data['claim']['envelope'], row['envelope'])
                self.assertEqual(json.loads(data['claim']['envelope'].splitlines()[1][9:]), text)
                self.assertEqual(self.event(data['claim'])[0], 200)
        # The owner endpoint retains its existing raw input bound.
        self.assertEqual(self.http('/agent-message', dict(agent=self.aid, question='a'*4001, request_id=uid()), owner=True)[0], 400)

    def test_oversized_peer_rejected_before_any_reservation(self):
        self.setup_global(); self.large_peer_registration(); rid=uid()
        for text in ('a'*16001, '😀'*16001):
            with self.assertRaisesRegex(peer_bridge.PeerError,
                    'Message too long: 16001 characters; maximum is 16000. Nothing was sent.'):
                self.send(text, rid)
        for table in ('requests', 'shared_bridge_requests', 'peer_execution', 'peer_reply_admissions'):
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0], 0)
        self.assertEqual(self.send('Valid follow-up.', rid)['request_id'], rid)

    def test_capacity_downgrade_before_reservation_creates_nothing(self):
        self.setup_global(); self.large_peer_registration()
        original=self.store.shared._preflight
        def downgrade(target):
            result=original(target)
            self.large_peer_registration(4000)
            return result
        with patch.object(self.store.shared, '_preflight', side_effect=downgrade):
            with self.assertRaisesRegex(peer_bridge.PeerError, 'capacity changed before reservation'):
                self.send('a'*16000)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM shared_bridge_requests').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM peer_execution').fetchone()[0], 0)

    def test_capacity_downgrade_after_reservation_cannot_claim_large_message(self):
        self.setup_global(); self.large_peer_registration()
        sent=self.send('a'*16000)
        self.large_peer_registration(4000)
        status,data=self.global_poll(self.body['client_id'])
        self.assertEqual(status,200);self.assertIsNone(data['claim'])
        row=self.peer_row(sent['request_id'])
        self.assertFalse(row['launched']);self.assertEqual(row['state'],'failed')
        self.assertIsNone(self.store.db.execute('SELECT launched FROM shared_bridge_requests WHERE client_id=?',
            (sent['request_id'],)).fetchone()[0])

    def test_reconnect_during_claim_preparation_cannot_use_stale_capacity(self):
        self.setup_global(); self.large_peer_registration()
        sent=self.send('a'*16000)
        original=self.store.shared.prepare_mod_claim
        def downgrade(row):
            prepared=original(row)
            self.large_peer_registration(4000)
            return prepared
        with patch.object(self.store.shared,'prepare_mod_claim',side_effect=downgrade):
            status,data=self.global_poll(self.body['client_id'])
        self.assertEqual(status,409)
        row=self.peer_row(sent['request_id'])
        self.assertFalse(row['launched']);self.assertNotIn('mod_attempt_id',row)
        self.assertIsNone(self.store.db.execute('SELECT launched FROM shared_bridge_requests WHERE client_id=?',
            (sent['request_id'],)).fetchone()[0])

    def test_double_poll_and_busy_owner_share_one_atomic_claim(self):
        self.setup_global();sent=self.send();owner,_=self.request('A distinct owner question.')
        barrier=threading.Barrier(2);prepare=self.store.shared.prepare_mod_claim
        def together(row):
            value=prepare(row);barrier.wait(timeout=3);return value
        with patch.object(self.store.shared,'prepare_mod_claim',side_effect=together):
            with ThreadPoolExecutor(2) as pool:
                responses=list(pool.map(lambda _:self.global_poll(self.body['client_id']),range(2)))
        self.assertTrue(all(status==200 for status,_ in responses),responses)
        claims=[value['claim'] for _,value in responses if value['claim']]
        self.assertEqual(len(claims),1);self.assertEqual(claims[0]['request_id'],self.peer_row(sent['request_id'])['request_id'])
        self.assertIsNone(self.global_poll(self.body['client_id'])[1]['claim'])
        self.assertFalse(next(r for r in self.store.mods.rows() if r['request_id']==owner['request_id'])['launched'])
        self.event(claims[0]);self.assertEqual(self.global_poll(self.body['client_id'])[1]['claim']['request_id'],owner['request_id'])

    def test_ledger_and_attempt_write_rollback_together_then_single_retry_claim(self):
        self.setup_global();sent=self.send();put=self.store.mods.put
        def fail_attempt(row):
            if row.get('origin')=='peer' and row.get('mod_attempt_id'):
                raise sqlite3.OperationalError('Fictional persistence fault')
            return put(row)
        with patch.object(self.store.mods,'put',side_effect=fail_attempt):
            self.assertEqual(self.global_poll(self.body['client_id'])[0],503)
        self.assertIsNone(self.store.db.execute('SELECT launched FROM shared_bridge_requests').fetchone()[0])
        row=self.peer_row(sent['request_id']);self.assertFalse(row['launched']);self.assertNotIn('mod_attempt_id',row)
        self.assertEqual(self.global_poll(self.body['client_id'])[1]['claim']['request_id'],row['request_id'])
        self.assertIsNone(self.global_poll(self.body['client_id'])[1]['claim'])

    def test_preparation_outside_lock_and_transaction_revoked_credential_fails_row(self):
        self.setup_global();sent=self.send();prepare=self.store.shared.prepare_mod_claim
        def revoke(row):
            self.assertFalse(self.store.lock._is_owned());self.assertFalse(self.store.db.in_transaction)
            value=prepare(row)
            with self.store.lock,self.store.db:
                self.store.db.execute('UPDATE shared_bridge_credentials SET active=0 WHERE id=?',(self.credential['id'],))
            return value
        with patch.object(self.store.shared,'prepare_mod_claim',side_effect=revoke):
            status,result=self.global_poll(self.body['client_id'])
        self.assertEqual(status,200,result);self.assertIsNone(result['claim'])
        row=self.peer_row(sent['request_id']);self.assertEqual(row['state'],'failed');self.assertFalse(row['launched'])
        self.assertIsNone(self.store.db.execute('SELECT launched FROM shared_bridge_requests').fetchone()[0])

    def test_reconnect_during_preparation_rejects_stale_poll_then_new_generation_can_claim(self):
        self.setup_global();sent=self.send();prepare=self.store.shared.prepare_mod_claim
        def reconnect(row):
            value=prepare(row)
            status,new=self.http('/claude-mod/register',dict(self.body,connection_seq=1),token=self.grant['grant'])
            self.assertEqual(status,200,new);self.next_generation=new['generation']
            return value
        with patch.object(self.store.shared,'prepare_mod_claim',side_effect=reconnect):
            self.assertEqual(self.global_poll(self.body['client_id'])[0],409)
        self.assertFalse(self.peer_row(sent['request_id'])['launched'])
        self.generation=self.next_generation
        self.assertIsNotNone(self.global_poll(self.body['client_id'])[1]['claim'])

    def test_owner_claim_during_peer_preparation_wins_without_second_claim(self):
        self.setup_global();owner,_=self.request('Owner queued before the peer.');sent=self.send()
        prepare=self.store.shared.prepare_mod_claim;entered=False;nested=[]
        def owner_claim(row):
            nonlocal entered
            value=prepare(row)
            if not entered:
                entered=True
                nested.append(self.global_poll(self.body['client_id']))
            return value
        with patch.object(self.store.shared,'prepare_mod_claim',side_effect=owner_claim):
            status,result=self.global_poll(self.body['client_id'])
        self.assertEqual(status,200,result);self.assertIsNone(result['claim'])
        self.assertEqual(nested[0][0],200,nested)
        self.assertEqual(nested[0][1]['claim']['request_id'],owner['request_id'])
        self.assertFalse(self.peer_row(sent['request_id'])['launched'])
        self.assertIsNone(self.store.db.execute('SELECT launched FROM shared_bridge_requests').fetchone()[0])

    def test_grant_revoke_during_preparation_invalidates_poll_and_fails_unclaimed(self):
        self.setup_global();sent=self.send();prepare=self.store.shared.prepare_mod_claim
        def revoke(row):
            value=prepare(row)
            self.assertEqual(self.http('/claude-mod-revoke',{},owner=True)[0],200)
            return value
        with patch.object(self.store.shared,'prepare_mod_claim',side_effect=revoke):
            self.assertEqual(self.global_poll(self.body['client_id'])[0],403)
        row=self.peer_row(sent['request_id']);self.assertEqual(row['state'],'failed');self.assertFalse(row['launched'])
        self.assertIsNone(self.store.db.execute('SELECT launched FROM shared_bridge_requests').fetchone()[0])

    def test_two_same_project_sessions_project_drift_and_peer_lease(self):
        self.setup_global();sent=self.send()
        other=uid();server.AGENTS['c:'+other]=dict(self.agents[self.aid])
        status,enrollment=self.http('/claude-mod/register',dict(self.body,native_id=other,client_id=uid()),token=self.grant['grant'])
        self.assertEqual(status,200,enrollment)
        status,result=self.http('/claude-mod/poll',dict(native_id=other,project=self.project,
            generation=enrollment['generation'],client_id=enrollment['client_id'],accept=True),token=enrollment['token'])
        self.assertEqual(status,200,result);self.assertIsNone(result['claim']);self.assertEqual(result['requests'],[])
        server.AGENTS[self.aid]['cwd']='/fictional/different-project'
        self.assertFalse(self.store.mods.peer_capability(self.aid,server.AGENTS[self.aid])[0])
        self.assertIsNone(self.global_poll(self.body['client_id'])[1]['claim'])
        server.AGENTS[self.aid]['cwd']=self.project
        with self.store.lock,self.store.db:
            self.store.db.execute('UPDATE claude_mod_enrollments SET last_seen=0 WHERE agent=?',(self.aid,))
        self.assertFalse(self.store.mods.peer_capability(self.aid,server.AGENTS[self.aid])[0])
        self.assertEqual(self.global_poll(self.body['client_id'])[0],409)
        self.assertFalse(self.peer_row(sent['request_id'])['launched'])

    def test_failed_identity_preflight_does_not_break_poll_or_route_to_owner(self):
        self.setup_global();sent=self.send();self.transcript.unlink()
        status,result=self.global_poll(self.body['client_id'])
        self.assertEqual(status,200,result);self.assertIsNone(result['claim'])
        self.assertEqual(self.peer_row(sent['request_id'])['state'],'failed')
        self.assertEqual(self.store.snapshot()['requests'],[])

    def test_lost_claim_reconnect_retained_answer_and_generation_stable_directory_identity(self):
        self.setup_global();first=self.target();sent=self.send();claim=self.global_poll(self.body['client_id'])[1]['claim']
        status,new=self.http('/claude-mod/register',dict(self.body,connection_seq=1),token=self.grant['grant'])
        self.assertEqual(status,200,new);self.generation=new['generation']
        self.assertEqual(self.target()['identity_revision'],first['identity_revision'])
        self.assertIsNone(self.global_poll(self.body['client_id'])[1]['claim'])
        self.assertEqual(self.peer_row(sent['request_id'])['state'],'unknown')
        self.assertEqual(self.event(claim)[0],200)
        self.assertEqual(self.store.shared.result(sent['request_id'])['reply'],'Fictional reply.')

    def test_immutable_ledger_selection_must_match_prepared_claim(self):
        self.setup_global();sent=self.send()
        with self.store.lock,self.store.db:
            raw=self.store.db.execute('SELECT target_snapshot FROM shared_bridge_requests').fetchone()[0]
            snapshot=json.loads(raw);snapshot['identity_revision']='0'*64
            self.store.db.execute('UPDATE shared_bridge_requests SET target_snapshot=?',(json.dumps(snapshot),))
        status,result=self.global_poll(self.body['client_id'])
        self.assertEqual(status,200,result);self.assertIsNone(result['claim'])
        row=self.peer_row(sent['request_id']);self.assertEqual(row['state'],'failed');self.assertFalse(row['launched'])
        self.assertIsNone(self.store.db.execute('SELECT launched FROM shared_bridge_requests').fetchone()[0])

    def test_unbacked_and_legacy_peer_rows_never_claim_or_accept_event(self):
        self.setup_global();sent=self.send();row=self.peer_row(sent['request_id'])
        with self.store.lock,self.store.db:
            self.store.db.execute('DELETE FROM shared_bridge_requests')
        self.assertIsNone(self.global_poll(self.body['client_id'])[1]['claim'])
        self.assertEqual(self.event(dict(request_id=row['request_id'],attempt_id=uid()))[0],409)
        with self.store.lock,self.store.db:
            row.update(bridge_version=1,mod_attempt_id=uid(),launched=True,state='queued')
            self.store.mods.put(row)
        self.assertEqual(self.store.mods.rows(),[])
        self.assertEqual(self.event(dict(request_id=row['request_id'],attempt_id=row['mod_attempt_id']))[0],409)


    def test_idle_registration_owner_peer_and_status_without_room(self):
        self.target();server.AGENTS.clear()
        self.setup_global()
        self.assertEqual(server.AGENTS,{})
        target=self.target()
        self.assertEqual((target['supported_transport'],target['capability']),('claude_mod','reachable'))
        self.assertEqual(self.store.mods.setup_status({})['connected_chats'],1)
        result=self.send();poll=self.global_poll(self.body['client_id'])[1]
        self.assertIsNotNone(poll['claim'],'verified idle peer must claim without a room member')
        self.assertEqual(self.event(poll['claim'])[0],200)
        payload,owner=self.request('Fictional owner idle question')
        claim=self.global_poll(self.body['client_id'])[1]['claim']
        self.assertIsNotNone(claim,'verified idle owner request must claim without a room member')
        self.assertEqual(server.AGENTS,{})
        self.assertEqual(self.event(claim)[0],200)
        self.assertEqual(self.store.shared.result(result['request_id'])['state'],'answered')
        self.assertEqual(self.store.mods.setup_status({})['connected_chats'],1)
        entry=self.store.mods.get(self.registration['enrollment_id'])
        self.assertFalse(self.store.mods.peer_ready(dict(entry,last_seen=time.time()-mod.LEASE-1)),'an expired lease cannot be peer-ready')
        self.store.db.execute('UPDATE claude_mod_enrollments SET last_seen=0');self.store.db.commit()
        self.assertEqual(self.store.mods.setup_status({})['connected_chats'],0)
        self.assertEqual(self.target()['capability'],'unavailable')

    def test_idle_auth_filesystem_boundaries_and_revocation_during_prepare(self):
        self.target();server.AGENTS.clear()
        status,setup=self.http('/claude-mod-setup',{'peer_receive':True},owner=True)
        _,grant=self.http('/claude-mod/setup',{'code':setup['pair_code']})
        original=self.store.shared.metadata_target
        realpath=chat_directory.os.path.realpath
        def outside(path,*args,**kwargs):
            self.assertFalse(self.store.lock._is_owned(),'filesystem must be outside store lock')
            self.assertFalse(self.store.db.in_transaction,'filesystem must be outside transaction')
            return realpath(path,*args,**kwargs)
        with patch.object(self.store.shared,'metadata_target',side_effect=AssertionError('unauthenticated discovery')):
            self.assertEqual(self.register_global(dict(grant,grant='0'*64))[0],403)
        with patch.object(chat_directory.os.path,'realpath',side_effect=outside):
            self.assertEqual(self.register_global(grant)[0],200)
        # A fresh client cannot commit against a grant revoked after its pinned read.
        with self.store.lock,self.store.db:
            self.store.db.execute('UPDATE claude_mod_enrollments SET active=0,generation=NULL')
        def revoke(*args):
            proof=original(*args)
            self.store.mods.revoke()
            return proof
        with patch.object(self.store.shared,'metadata_target',side_effect=revoke):
            self.assertEqual(self.register_global(grant)[0],403)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM claude_mod_enrollments WHERE active=1').fetchone()[0],0)

    def test_idle_registration_preparation_rechecks_host_room_and_metadata(self):
        self.target();server.AGENTS.clear()
        _,setup=self.http('/claude-mod-setup',{'peer_receive':True},owner=True)
        _,grant=self.http('/claude-mod/setup',{'code':setup['pair_code']})
        original=self.store.shared.metadata_target
        def competing(*args):
            proof=original(*args)
            body=dict(native_id=self.sid,project=self.project,client_id=uid(),connection_seq=0,peer_protocol=1)
            with self.store.lock,self.store.db:
                self.store.mods.register(body,grant['grant'],{},proof)
            return proof
        with patch.object(self.store.shared,'metadata_target',side_effect=competing):
            status,value,_=self.register_global(grant,peer_protocol=1)
            self.assertEqual((status,value['error_code']),(409,'host_conflict'))
        with self.store.lock,self.store.db:
            self.store.db.execute('UPDATE claude_mod_enrollments SET active=0,generation=NULL')
        def room_change(*args):
            proof=original(*args)
            server.AGENTS[self.aid]=dict(self.agents[self.aid],sub=True)
            return proof
        with patch.object(self.store.shared,'metadata_target',side_effect=room_change):
            status,value,_=self.register_global(grant,peer_protocol=1)
            self.assertEqual((status,value['error_code']),(409,'identity_rejected'))
        server.AGENTS.clear()
        def metadata_change(*args):
            proof=original(*args)
            replacement=self.transcript.with_suffix('.replacement')
            replacement.write_text(json.dumps(dict(sessionId=self.sid,cwd=self.project,isSidechain=True))+'\n')
            os.replace(replacement,self.transcript)
            with self.assertRaises(chat_directory.DirectoryError): self.directory.claude_metadata(self.aid,self.project)
            return proof
        with patch.object(self.store.shared,'metadata_target',side_effect=metadata_change):
            status,value,_=self.register_global(grant,peer_protocol=1)
            self.assertEqual((status,value['error_code']),(409,'not_observed'))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM claude_mod_enrollments WHERE active=1').fetchone()[0],0)

    def test_working_folder_inside_project_keeps_receiving_and_outside_is_vetoed(self):
        # The room records the folder of each transcript record; a cd inside the project must not hide the chat.
        self.setup_global()
        inside = Path(self.project, 'archive', 'notes'); inside.mkdir(parents=True)
        def receiving(cwd):
            server.AGENTS[self.aid] = dict(self.agents[self.aid], cwd=cwd)
            self.store.mods.peer_capability(self.aid, server.AGENTS[self.aid])     # queues the folder's resolution
            self.assertTrue(mod.RESOLVER.settle())
            ready = self.store.mods.peer_capability(self.aid, server.AGENTS[self.aid])[0]
            listed = self.target()['capability'] == 'reachable'
            connected = self.store.mods.setup_status(dict(server.AGENTS))['connected_chats'] == 1
            self.assertEqual(ready, listed, cwd); self.assertEqual(ready, connected, cwd)
            return ready
        self.assertTrue(receiving(str(inside)))
        outside = [self.project + '-fictional-sibling', os.path.dirname(self.project),
                   os.path.join(str(inside), '..', '..', '..', 'fictional-other')]
        if os.name != 'nt':
            elsewhere = tempfile.TemporaryDirectory(); self.addCleanup(elsewhere.cleanup)
            escape = Path(self.project, 'escape'); escape.symlink_to(os.path.realpath(elsewhere.name), target_is_directory=True)
            alias = Path(self.project, 'alias'); alias.symlink_to(inside, target_is_directory=True)
            self.assertTrue(receiving(str(alias)))
            outside += [str(escape), str(escape / 'deeper')]
        for cwd in outside:
            self.assertFalse(receiving(cwd), cwd)
            self.assertIsNone(self.folder_poll(cwd)[1]['claim'], cwd)
        receiving(str(inside))
        sent = self.send()
        self.assertIsNone(self.global_poll(self.body['client_id'])[1]['claim'])   # an older plugin: project only
        claim = self.folder_poll(str(inside))[1]['claim']
        self.assertIsNotNone(claim)
        self.assertEqual(self.peer_row(sent['request_id'])['request_id'], claim['request_id'])

    def test_wider_project_needs_the_recorded_launch_folder(self):
        # The transcript's first cwd is the project. A plugin naming '/' or a parent folder is refused, both when
        # pairing and when registering, even though that folder holds the chat's working folder.
        inside = Path(self.project, 'archive'); inside.mkdir()
        server.AGENTS[self.aid] = dict(self.agents[self.aid], cwd=str(inside))
        self.target()
        status, pair = self.http('/claude-mod-pair', {'agent': self.aid}, owner=True)
        self.assertEqual(status, 200, pair)
        for wider in ('/', os.path.dirname(self.project)):
            body = {'code': pair['pair_code'], 'native_id': self.sid, 'project': wider}
            self.assertEqual(self.http('/claude-mod/enroll', body)[0], 403, wider)
        status, enrollment = self.http('/claude-mod/enroll', {'code': pair['pair_code'], 'native_id': self.sid,
                                                              'project': self.project})
        self.assertEqual(status, 200, enrollment)
        self.assertEqual(self.store.mods.get(enrollment['enrollment_id'])['project'], self.project)
        self.assertEqual(self.http('/claude-mod-revoke', {}, owner=True)[0], 200)
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE claude_mod_enrollments SET active=0')
        status, setup = self.http('/claude-mod-setup', {'peer_receive': True}, owner=True)
        status, self.grant = self.http('/claude-mod/setup', {'code': setup['pair_code']})
        self.assertEqual(status, 200, self.grant)
        for wider in ('/', os.path.dirname(self.project)):
            status, value, _ = self.register_global(self.grant, project=wider)
            self.assertEqual((status, value.get('error_code')), (409, 'not_observed'), wider)
        status, value, _ = self.register_global(self.grant)
        self.assertEqual(status, 200, value)
        projects = {row[0] for row in self.store.db.execute('SELECT project FROM claude_mod_enrollments WHERE active=1')}
        self.assertEqual(projects, {self.project})

    def subfolder_enrollment(self):
        # An enrollment made before plugin 0.6.3 while the chat sat in a subfolder names that subfolder.
        self.setup_global()
        inside = Path(self.project, 'archive'); inside.mkdir(exist_ok=True)
        self.move_enrollment(inside)
        server.AGENTS[self.aid] = dict(self.agents[self.aid], cwd=str(inside))
        self.target()
        return inside

    def move_enrollment(self, folder):
        # As registration stores it: the service token is derived from the grant, the chat and its project.
        payload = json.dumps(['watch-claude-session-v1', self.sid, str(folder)], separators=(',', ':'), ensure_ascii=True)
        service = hmac.new(self.grant['grant'].encode('ascii'), payload.encode('utf-8'), hashlib.sha256).hexdigest()
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE claude_mod_enrollments SET project=?,token_hash=? WHERE agent=? AND active=1',
                                  (str(folder), mod.digest(service), self.aid))
        self.token = service

    def reregister(self, project):
        body = dict(self.body, project=project, connection_seq=self.body['connection_seq'] + 1)
        return self.http('/claude-mod/register', body, token=self.grant['grant'])

    def active_projects(self):
        return [row[0] for row in self.store.db.execute(
            'SELECT project FROM claude_mod_enrollments WHERE agent=? AND active=1', (self.aid,))]

    def enrollment_facts(self):
        return self.store.db.execute('SELECT id,generation,token_hash,connection_seq,active,project '
                                     'FROM claude_mod_enrollments WHERE agent=? ORDER BY active DESC', (self.aid,)).fetchall()

    def register_at(self, project, seq, **extra):
        body = dict(self.body, project=project, connection_seq=seq, **extra)
        return self.http('/claude-mod/register', body, token=self.grant['grant'])

    def test_plugin_upgrade_checks_episode_order_before_any_change(self):
        inside = self.subfolder_enrollment()
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE claude_mod_enrollments SET connection_seq=9 WHERE agent=? AND active=1', (self.aid,))
        def child_poll(accept=True):        # the older plugin names the subfolder it enrolled from
            return self.http('/claude-mod/poll', dict(generation=self.generation, native_id=self.sid, project=str(inside),
                                                      accept=accept, client_id=self.body['client_id']), token=self.token)
        # An owner request claimed on the subfolder enrollment (its folder is that enrollment's project).
        _, owner = self.request('Fictional owner question')
        claim = child_poll()[1]['claim']
        self.assertEqual(claim['request_id'], owner['request_id'])
        def stored():
            return next(r for r in self.store._rows() if r['request_id'] == owner['request_id'])
        before, row = self.enrollment_facts(), stored()
        self.assertTrue(row['launched'])
        protocol = self.body.get('peer_protocol', 0)
        for seq, extra in ((8, {}), (9, {}), (9, {'peer_protocol': 1 - protocol})):
            with self.subTest(seq=seq, extra=extra):
                status, value = self.register_at(self.project, seq, **extra)
                self.assertEqual((status, value.get('error_code')), (409, 'stale_connection'), value)
                self.assertEqual(self.enrollment_facts(), before)
                self.assertEqual(stored(), row)                               # the claimed request is untouched
                self.assertEqual(child_poll(accept=False)[0], 200)            # the connection still works
        # A newer episode still may not move the chat while that claimed request is open.
        status, value = self.register_at(self.project, 10)
        self.assertEqual((status, value.get('error_code')), (409, 'move_pending'), value)
        self.assertEqual(self.enrollment_facts(), before)
        self.assertEqual(stored(), row)
        answered = dict(generation=self.generation, native_id=self.sid, client_id=self.body['client_id'],
                        request_id=claim['request_id'], attempt_id=claim['attempt_id'], kind='answered',
                        turn_id='fictional-turn', answer='WATCH_REPLY ' + claim['request_id'] + '\nFictional reply.')
        self.assertEqual(self.http('/claude-mod/event', answered, token=self.token)[0], 200)
        status, value = self.register_at(self.project, 11)
        self.assertEqual(status, 200, value)
        self.assertEqual(self.active_projects(), [self.project])

    def current_seq(self):
        return self.store.db.execute('SELECT connection_seq FROM claude_mod_enrollments WHERE agent=? AND active=1',
                                     (self.aid,)).fetchone()[0]

    def derived_receipts_then_move(self, expired):
        # An earlier plugin claimed an owner question from the subfolder it enrolled in; its reply is buffered.
        inside = self.subfolder_enrollment()
        child = lambda accept=True, **extra: self.http('/claude-mod/poll', dict(
            generation=self.generation, native_id=self.sid, project=str(inside), accept=accept,
            client_id=self.body['client_id'], **extra), token=self.token)
        _, owner = self.request('Fictional owner question')
        claim = child()[1]['claim']
        self.assertEqual(claim['request_id'], owner['request_id'])
        row = lambda: next(r for r in self.store._rows() if r['request_id'] == owner['request_id'])
        if expired:
            with self.store.lock, self.store.db:
                held = row(); held['created'] = time.time() - mod.TTL - 1; self.store.mods.put(held)
                self.store.mods.maintain()
            self.assertEqual(row()['state'], 'expired')
        # The 0.6.4 plugin reloads in the project folder: the move waits for that open request.
        server.AGENTS[self.aid] = dict(self.agents[self.aid], cwd=self.project)
        seq = self.current_seq()
        status, value = self.register_at(self.project, seq + 1)
        self.assertEqual((status, value.get('error_code')), (409, 'move_pending'), value)
        # Receipts only, for the unchanged earlier enrollment: no claim, but the original outcome is accepted.
        status, value = self.register_at(str(inside), seq + 2, receipt_only=True)
        self.assertEqual(status, 200, value)
        self.token, self.generation = value['token'], value['generation']
        # Recorded on the enrollment itself (durable), so it holds whatever the plugin sends in its polls.
        self.assertEqual(self.store.db.execute('SELECT receipt_only FROM claude_mod_enrollments WHERE agent=? AND active=1',
                                               (self.aid,)).fetchone()[0], 1)
        # The chat is not receivable in its earlier folder: no new question is queued there.
        later = dict(agent=self.aid, question='Fictional later owner question', request_id=uid())
        self.assertEqual(self.http('/agent-message', later, owner=True)[0], 503)
        self.assertIsNone(child()[1]['claim'])
        self.assertIsNone(child(cwd=str(inside))[1]['claim'])
        answered = dict(generation=self.generation, native_id=self.sid, client_id=self.body['client_id'],
                        request_id=claim['request_id'], attempt_id=claim['attempt_id'], kind='answered',
                        turn_id='fictional-turn', answer='WATCH_REPLY ' + claim['request_id'] + '\nFictional reply.')
        self.assertEqual(self.http('/claude-mod/event', answered, token=self.token)[0], 200)
        self.assertTrue(row().get('settled'))
        # Then the move.
        status, value = self.register_at(self.project, seq + 3)
        self.assertEqual(status, 200, value)
        self.assertEqual(self.active_projects(), [self.project])
        self.assertEqual(self.store.db.execute('SELECT receipt_only FROM claude_mod_enrollments WHERE agent=? AND active=1',
                                               (self.aid,)).fetchone()[0], 0)

    def test_a_receipt_only_connection_admits_no_new_work(self):
        # The stage still shows the earlier folder, so only the receipt-only flag tells this chat apart.
        inside = self.subfolder_enrollment()
        child = lambda accept=True: self.http('/claude-mod/poll', dict(
            generation=self.generation, native_id=self.sid, project=str(inside), accept=accept,
            client_id=self.body['client_id']), token=self.token)
        _, owner = self.request('Fictional owner question before the upgrade')
        claim = child()[1]['claim']
        self.assertEqual(claim['request_id'], owner['request_id'])
        seq = self.current_seq()
        status, value = self.register_at(str(inside), seq + 1, receipt_only=True)
        self.assertEqual(status, 200, value)
        self.token, self.generation = value['token'], value['generation']
        item = server.AGENTS[self.aid]
        rows = len(self.store._rows())
        self.assertFalse(self.store.mods.capability(self.aid, item)[0])
        self.assertFalse(self.store.mods.peer_capability(self.aid, item)[0])
        self.assertIsNone(self.store.mods.attachment_entry(self.aid, item))
        self.assertEqual(self.store.mods.setup_status(dict(server.AGENTS))['connected_chats'], 0)
        self.assertFalse(self.store.mods.any_ready())
        self.assertEqual(self.target()['capability'], 'unavailable')
        question = dict(agent=self.aid, question='Fictional question for a receipt-only chat', request_id=uid())
        self.assertEqual(self.http('/agent-message', question, owner=True)[0], 503)
        with self.assertRaises(peer_bridge.PeerError):
            self.send('Fictional peer question for a receipt-only chat')
        self.assertEqual(len(self.store._rows()), rows)                       # no new pending work
        # Its receipts still go through.
        answered = dict(generation=self.generation, native_id=self.sid, client_id=self.body['client_id'],
                        request_id=claim['request_id'], attempt_id=claim['attempt_id'], kind='answered',
                        turn_id='fictional-turn', answer='WATCH_REPLY ' + claim['request_id'] + '\nFictional reply.')
        self.assertEqual(self.http('/claude-mod/event', answered, token=self.token)[0], 200)
        # The move to the project folder clears the flag and admission resumes.
        server.AGENTS[self.aid] = dict(self.agents[self.aid], cwd=self.project)
        status, value = self.register_at(self.project, seq + 2)
        self.assertEqual(status, 200, value)
        self.assertTrue(self.store.mods.capability(self.aid, server.AGENTS[self.aid])[0])
        self.assertTrue(self.store.mods.peer_capability(self.aid, server.AGENTS[self.aid])[0])
        self.assertEqual(self.store.mods.setup_status(dict(server.AGENTS))['connected_chats'], 1)

    def test_derived_move_waits_for_receipts_reported_from_the_earlier_folder(self):
        self.derived_receipts_then_move(expired=False)

    @unittest.skipIf(os.name == 'nt', 'POSIX symbolic links')
    def test_receipts_from_a_renamed_earlier_folder_are_still_reported(self):
        # The earlier folder was renamed and its old name now links outside the project: the receipt context is
        # historical evidence, matched exactly, so the original outcome is still reported and the chat moves.
        inside = self.subfolder_enrollment()
        child = lambda accept=True: self.http('/claude-mod/poll', dict(
            generation=self.generation, native_id=self.sid, project=str(inside), accept=accept,
            client_id=self.body['client_id']), token=self.token)
        _, owner = self.request('Fictional owner question from the earlier folder')
        claim = child()[1]['claim']
        self.assertEqual(claim['request_id'], owner['request_id'])
        inside.rename(Path(self.project, 'archive-renamed'))
        elsewhere = tempfile.TemporaryDirectory(); self.addCleanup(elsewhere.cleanup)
        inside.symlink_to(os.path.realpath(elsewhere.name), target_is_directory=True)
        self.assertFalse(mod.project(str(inside)))
        server.AGENTS[self.aid] = dict(self.agents[self.aid], cwd=self.project)
        seq = self.current_seq()
        self.assertEqual(self.register_at(self.project, seq + 1)[1].get('error_code'), 'move_pending')
        # A context that is not exactly this chat's earlier enrollment still fails.
        status, value = self.register_at(str(Path(self.project, 'archive-renamed')), seq + 2, receipt_only=True)
        self.assertEqual((status, value.get('error_code')), (409, 'receipt_context_missing'))
        foreign = dict(self.body, native_id=uid(), project=str(inside), connection_seq=seq + 3, receipt_only=True)
        self.assertEqual(self.http('/claude-mod/register', foreign, token=self.grant['grant'])[1].get('error_code'),
                         'identity_rejected')
        stranger = dict(self.body, project=str(inside), connection_seq=seq + 4, receipt_only=True)
        self.assertEqual(self.http('/claude-mod/register', stranger, token='f' * 64)[0], 403)
        # The exact earlier context: receipts only.
        status, value = self.register_at(str(inside), seq + 5, receipt_only=True)
        self.assertEqual(status, 200, value)
        self.token, self.generation = value['token'], value['generation']
        self.assertIsNone(child()[1]['claim'])
        answered = dict(generation=self.generation, native_id=self.sid, client_id=self.body['client_id'],
                        request_id=claim['request_id'], attempt_id=claim['attempt_id'], kind='answered',
                        turn_id='fictional-turn', answer='WATCH_REPLY ' + claim['request_id'] + '\nFictional reply.')
        self.assertEqual(self.http('/claude-mod/event', answered, token=self.token)[0], 200)
        row = next(r for r in self.store._rows() if r['request_id'] == owner['request_id'])
        self.assertTrue(row.get('settled'))
        status, value = self.register_at(self.project, seq + 6)
        self.assertEqual(status, 200, value)
        self.assertEqual(self.active_projects(), [self.project])
        # Only the one original claim was ever handed out.
        self.assertEqual(sum(1 for r in self.store._rows() if r.get('launched')), 1)

    def test_derived_move_reports_an_already_expired_request_first(self):
        self.derived_receipts_then_move(expired=True)

    def test_receipt_only_registration_needs_the_unchanged_earlier_enrollment(self):
        inside = self.subfolder_enrollment()
        seq = self.current_seq()
        for folder in (str(Path(inside, 'other')), self.project):
            status, value = self.register_at(folder, seq + 1, receipt_only=True)
            self.assertEqual((status, value.get('error_code')), (409, 'receipt_context_missing'), folder)
        self.assertEqual(self.register_at(str(inside), seq + 1, receipt_only=False)[0], 400)

    def test_manual_move_waits_for_receipts_reported_from_the_earlier_folder(self):
        for expired in (False, True):
            with self.subTest(expired=expired):
                self.manual_receipts_then_move(expired)
                with self.store.lock, self.store.db:
                    self.store.db.execute('DELETE FROM claude_mod_enrollments')

    def manual_receipts_then_move(self, expired):
        inside, enrollment = self.manual_child()
        _, owner = self.request('Fictional manual owner question')
        claim = self.manual_poll(str(inside))[1]['claim']
        self.assertEqual(claim['request_id'], owner['request_id'])
        row = lambda: next(r for r in self.store._rows() if r['request_id'] == owner['request_id'])
        if expired:
            with self.store.lock, self.store.db:
                held = row(); held['created'] = time.time() - mod.TTL - 1; self.store.mods.put(held)
                self.store.mods.maintain()
        server.AGENTS[self.aid] = dict(self.agents[self.aid], cwd=self.project)
        connect = lambda project, **extra: self.http('/claude-mod/connect', dict(native_id=self.sid, project=project, **extra),
                                                     token=self.token)
        self.assertEqual(connect(self.project)[1].get('error_code'), 'move_pending')
        self.assertEqual(connect(self.project, receipt_only=True)[1].get('error_code'), 'receipt_context_missing')
        status, value = connect(str(inside), receipt_only=True)
        self.assertEqual(status, 200, value)
        self.generation = value['generation']
        self.assertIsNone(self.manual_poll(str(inside))[1]['claim'])
        answered = dict(generation=self.generation, native_id=self.sid, request_id=claim['request_id'],
                        attempt_id=claim['attempt_id'], kind='answered', turn_id='fictional-turn',
                        answer='WATCH_REPLY ' + claim['request_id'] + '\nFictional reply.')
        self.assertEqual(self.http('/claude-mod/event', answered, token=self.token)[0], 200)
        self.assertTrue(row().get('settled'))
        status, value = connect(self.project)
        self.assertEqual(status, 200, value)
        self.assertEqual(self.store.db.execute('SELECT project,grant_id FROM claude_mod_enrollments WHERE id=?',
                                               (enrollment['enrollment_id'],)).fetchone(), (self.project, None))

    def test_unresolved_move_evidence_is_retryable_not_a_rejection(self):
        inside = self.subfolder_enrollment()
        _, owner = self.request('Fictional question waiting for the move')
        server.AGENTS[self.aid] = dict(self.agents[self.aid], cwd=self.project)
        seq = self.current_seq()
        with patch.object(self.store.shared, 'metadata_target',
                          side_effect=peer_bridge.PeerError(409, 'Fictional directory scan not finished.')):
            status, value = self.register_at(self.project, seq + 1)
        self.assertEqual((status, value.get('error_code')), (409, 'not_observed'), value)
        # A stage folder not resolved yet is retryable too.
        gate = threading.Event(); self.addCleanup(gate.set)
        hanging = mod.FolderResolver(resolve=lambda cwd, root: gate.wait(10) and mod.folder_inside(cwd, root))
        server.AGENTS[self.aid] = dict(self.agents[self.aid], cwd=str(inside))
        with patch.object(mod, 'RESOLVER', hanging), patch.object(mod.FolderResolver, 'prime',
                                                                lambda self, cwd, root, timeout=2.0: False):
            status, value = self.register_at(self.project, seq + 2)
        self.assertEqual((status, value.get('error_code')), (409, 'not_observed'), value)
        row = next(r for r in self.store._rows() if r['request_id'] == owner['request_id'])
        self.assertFalse(row.get('launched'))                                 # no claim before the proof is ready
        gate.set()
        server.AGENTS[self.aid] = dict(self.agents[self.aid], cwd=self.project)
        status, value = self.register_at(self.project, seq + 3)
        self.assertEqual(status, 200, value)
        row = next(r for r in self.store._rows() if r['request_id'] == owner['request_id'])
        self.assertEqual((row['state'], row.get('launched')), ('failed', False))     # never claimed: said so

    @unittest.skipIf(os.name == 'nt', 'POSIX symbolic links')
    def test_claim_needs_a_fresh_folder_resolution_after_a_link_moves(self):
        self.setup_global()
        inside = Path(self.project, 'inside'); inside.mkdir()
        alias = Path(self.project, 'alias'); alias.symlink_to(inside, target_is_directory=True)
        elsewhere = tempfile.TemporaryDirectory(); self.addCleanup(elsewhere.cleanup)
        server.AGENTS[self.aid] = dict(self.agents[self.aid], cwd=str(alias))
        # Warm the cache while the link points inside the project.
        mod.within_project(str(alias), self.project); self.assertTrue(mod.RESOLVER.settle())
        self.assertTrue(mod.within_project(str(alias), self.project))
        sent = self.send()
        alias.unlink(); alias.symlink_to(os.path.realpath(elsewhere.name), target_is_directory=True)
        self.assertTrue(mod.within_project(str(alias), self.project))        # the cached answer is now stale
        self.assertFalse(mod.folder_inside(str(alias), self.project))
        self.assertIsNone(self.folder_poll(str(alias))[1]['claim'])
        self.assertFalse(self.peer_row(sent['request_id'])['launched'])
        # A slow refresh never lets the earlier positive answer through: no fresh result in time, no claim.
        alias.unlink(); alias.symlink_to(inside, target_is_directory=True)
        gate = threading.Event(); self.addCleanup(gate.set)
        def slow(cwd, root):
            gate.wait(10)
            return mod.folder_inside(cwd, root)
        resolver = mod.FolderResolver(resolve=slow); resolver.REVALIDATE = 0.3
        # Warm this resolver's cache directly, as an earlier lookup would have left it.
        with resolver.changed:
            resolver._keep((str(alias), self.project), True, resolver.clock(), 0)
        with patch.object(mod, 'RESOLVER', resolver):
            self.assertIsNone(self.folder_poll(str(alias))[1]['claim'])
            self.assertFalse(self.peer_row(sent['request_id'])['launched'])
            gate.set()
            self.assertTrue(resolver.settle())
            claim = self.folder_poll(str(alias))[1]['claim']               # a fresh inside answer: claimed
        self.assertEqual(self.peer_row(sent['request_id'])['request_id'], claim['request_id'])

    def test_manual_subfolder_pairing_moves_to_its_launch_folder_on_reconnect(self):
        # A manual pairing made by an earlier plugin in a subfolder names that subfolder. On reconnect the plugin
        # offers the project root on the same manual credential; it moves only to the proven launch folder and
        # stays manual (owner-only).
        inside = Path(self.project, 'archive'); inside.mkdir()
        server.AGENTS[self.aid] = dict(self.agents[self.aid], cwd=str(inside)); self.target()
        status, pair = self.http('/claude-mod-pair', {'agent': self.aid}, owner=True)
        self.assertEqual(status, 200, pair)
        status, enrollment = self.http('/claude-mod/enroll', {'code': pair['pair_code'], 'native_id': self.sid,
                                                              'project': str(inside)})
        self.assertEqual(status, 200, enrollment)
        token, eid = enrollment['token'], enrollment['enrollment_id']
        def connect(project):
            return self.http('/claude-mod/connect', dict(native_id=self.sid, project=project), token=token)
        def facts():
            return self.store.db.execute('SELECT project,grant_id,active,generation FROM claude_mod_enrollments '
                                         'WHERE id=?', (eid,)).fetchone()
        before = facts()
        with patch.object(self.store.shared, 'metadata_target',
                          side_effect=peer_bridge.PeerError(409, 'Fictional missing metadata.')):
            self.assertEqual(connect(self.project)[0], 409)
        for wider in ('/', os.path.dirname(self.project)):
            self.assertEqual(connect(wider)[0], 409, wider)
        self.assertEqual(facts(), before)
        status, value = connect(self.project)
        self.assertEqual(status, 200, value)
        project, grant, active, generation = facts()
        self.assertEqual((project, grant, active), (self.project, None, 1))
        self.assertNotEqual(generation, before[3])
        self.assertEqual(connect(self.project)[0], 200)                       # an ordinary reconnect afterwards

    def test_plugin_upgrade_moves_a_subfolder_enrollment_to_the_proven_launch_folder(self):
        inside = self.subfolder_enrollment()
        old, old_hash = self.store.db.execute('SELECT id,token_hash FROM claude_mod_enrollments WHERE agent=? AND active=1',
                                              (self.aid,)).fetchone()
        status, value = self.reregister(self.project)
        self.assertEqual(status, 200, value)
        # The retired enrollment keeps its own token hash, so receipts retained for it can still be recovered.
        self.assertEqual(self.store.db.execute('SELECT token_hash FROM claude_mod_enrollments WHERE id=?',
                                               (old,)).fetchone()[0], old_hash)
        self.assertEqual(self.active_projects(), [self.project])
        self.assertNotEqual(value['enrollment_id'], old)
        self.assertEqual(self.store.db.execute('SELECT active FROM claude_mod_enrollments WHERE id=?',
                                               (old,)).fetchone()[0], 0)
        self.token, self.generation = value['token'], value['generation']
        self.assertTrue(self.store.mods.peer_capability(self.aid, server.AGENTS[self.aid])[0])
        sent = self.send()
        claim = self.folder_poll(str(inside))[1]['claim']
        self.assertEqual(self.peer_row(sent['request_id'])['request_id'], claim['request_id'])

    def test_plugin_upgrade_without_launch_folder_proof_keeps_the_old_enrollment(self):
        inside = self.subfolder_enrollment()
        with patch.object(self.store.shared, 'metadata_target',
                          side_effect=peer_bridge.PeerError(409, 'Fictional missing metadata.')):
            status, value = self.reregister(self.project)
        self.assertEqual(status, 409, value)
        self.assertEqual(self.active_projects(), [str(inside)])
        # A wider folder that is not the launch folder is never accepted either.
        deeper = Path(inside, 'deeper'); deeper.mkdir()
        self.move_enrollment(deeper)
        server.AGENTS[self.aid] = dict(self.agents[self.aid], cwd=str(deeper))
        for wider in (str(inside), '/'):
            status, value = self.reregister(wider)
            self.assertEqual(status, 409, (wider, value))
            self.assertEqual(self.active_projects(), [str(deeper)])

    def test_fixture_lease_does_not_depend_on_runner_speed(self):
        # A slow runner (one Windows CI run took 140 s for this suite) may spend more than the product's 12-second
        # lease between setup and the first poll. The fixture's lease covers that; the product lease would not.
        self.assertEqual(owner_fixture.PRODUCT_LEASE, 12)
        self.assertEqual(mod.LEASE, owner_fixture.TEST_LEASE)
        self.setup_global(); sent = self.send()
        later = time.time() + owner_fixture.PRODUCT_LEASE + 1
        with patch.object(mod, 'time', wraps=time) as clock:
            clock.time.return_value = later
            status, result = self.global_poll(self.body['client_id'])
            self.assertEqual(status, 200, result)
            self.assertEqual(self.peer_row(sent['request_id'])['request_id'], result['claim']['request_id'])
            # The same delay under the product lease is the CI failure: the enrollment lapses before the poll.
            clock.time.return_value = later + owner_fixture.PRODUCT_LEASE + 1
            with patch.object(mod, 'LEASE', owner_fixture.PRODUCT_LEASE):
                status, result = self.global_poll(self.body['client_id'])
        self.assertEqual((status, result.get('error_code')), (409, 'stale_connection'), result)

    @unittest.skipIf(os.name == 'nt', 'POSIX symbolic links')
    def test_claims_follow_the_host_folder_not_launch_metadata_or_a_stale_stage(self):
        # Launch metadata says where the chat began and the stage shows its last folder; neither is where the host
        # is now. A claim in a subfolder needs the host's own report, resolved for that poll.
        self.setup_global()
        inside = Path(self.project, 'inside'); inside.mkdir()
        elsewhere = tempfile.TemporaryDirectory(); self.addCleanup(elsewhere.cleanup)
        alias = Path(self.project, 'alias'); alias.symlink_to(os.path.realpath(elsewhere.name), target_is_directory=True)
        missing = Path(self.project, 'not-there')
        sent = self.send()
        for stage in ('absent', 'stale root'):
            with self.subTest(stage=stage):
                if stage == 'absent':
                    server.AGENTS.pop(self.aid, None)
                else:
                    server.AGENTS[self.aid] = dict(self.agents[self.aid], cwd=self.project)
                for cwd in (str(alias), str(alias / 'deeper'), str(missing)):
                    self.assertIsNone(self.folder_poll(cwd)[1]['claim'], cwd)
                    self.assertFalse(self.peer_row(sent['request_id'])['launched'])
        # Idle at the project folder still receives, from either plugin.
        server.AGENTS.pop(self.aid, None)
        claim = self.folder_poll(self.project)[1]['claim']
        self.assertEqual(self.peer_row(sent['request_id'])['request_id'], claim['request_id'])
        self.assertEqual(self.event(claim)[0], 200)
        second = self.send('Report the second fictional status.')
        claim = self.global_poll(self.body['client_id'])[1]['claim']
        self.assertEqual(self.peer_row(second['request_id'])['request_id'], claim['request_id'])

    def test_an_older_plugin_in_a_subfolder_gets_no_claim(self):
        self.setup_global()
        inside = Path(self.project, 'inside'); inside.mkdir()
        server.AGENTS[self.aid] = dict(self.agents[self.aid], cwd=str(inside))
        sent = self.send()
        self.assertIsNone(self.global_poll(self.body['client_id'])[1]['claim'])     # no folder report: project only
        self.assertFalse(self.peer_row(sent['request_id'])['launched'])
        self.assertEqual(self.registration['folder_protocol'], 1)

    def test_derived_move_fails_unclaimed_requests_truthfully(self):
        inside = self.subfolder_enrollment()
        def child_poll(accept=True):
            return self.http('/claude-mod/poll', dict(generation=self.generation, native_id=self.sid, project=str(inside),
                                                      accept=accept, client_id=self.body['client_id']), token=self.token)
        self.assertEqual(child_poll(accept=False)[0], 200)
        _, owner = self.request('Fictional owner question left unclaimed')
        status, value = self.reregister(self.project)
        self.assertEqual(status, 200, value)
        row = next(r for r in self.store._rows() if r['request_id'] == owner['request_id'])
        self.assertEqual(row['state'], 'failed')
        self.assertFalse(row.get('launched'))
        self.assertIn('Nothing was submitted', row['error'])

    def manual_child(self):
        inside = Path(self.project, 'archive'); inside.mkdir(exist_ok=True)
        server.AGENTS[self.aid] = dict(self.agents[self.aid], cwd=str(inside)); self.target()
        status, pair = self.http('/claude-mod-pair', {'agent': self.aid}, owner=True)
        self.assertEqual(status, 200, pair)
        status, enrollment = self.http('/claude-mod/enroll', {'code': pair['pair_code'], 'native_id': self.sid,
                                                              'project': str(inside)})
        self.assertEqual(status, 200, enrollment)
        self.token, self.generation = enrollment['token'], enrollment['generation']
        return inside, enrollment

    def manual_poll(self, project, accept=True, **extra):
        return self.http('/claude-mod/poll', dict(generation=self.generation, native_id=self.sid, project=project,
                                                  accept=accept, **extra), token=self.token)

    def test_manual_move_fails_queued_work_and_waits_for_claimed_work(self):
        inside, enrollment = self.manual_child()
        # A claimed request on the child pairing: the move waits; nothing changes.
        _, claimed = self.request('Fictional claimed owner question')
        claim = self.manual_poll(str(inside))[1]['claim']
        self.assertEqual(claim['request_id'], claimed['request_id'])
        connect = lambda project: self.http('/claude-mod/connect', dict(native_id=self.sid, project=project),
                                            token=self.token)
        before = self.store.db.execute('SELECT project,generation FROM claude_mod_enrollments WHERE id=?',
                                       (enrollment['enrollment_id'],)).fetchone()
        status, value = connect(self.project)
        self.assertEqual((status, value.get('error_code')), (409, 'move_pending'), value)
        self.assertEqual(self.store.db.execute('SELECT project,generation FROM claude_mod_enrollments WHERE id=?',
                                               (enrollment['enrollment_id'],)).fetchone(), before)
        answered = dict(generation=self.generation, native_id=self.sid, request_id=claim['request_id'],
                        attempt_id=claim['attempt_id'], kind='answered', turn_id='fictional-turn',
                        answer='WATCH_REPLY ' + claim['request_id'] + '\nFictional reply.')
        self.assertEqual(self.http('/claude-mod/event', answered, token=self.token)[0], 200)
        # A queued, never-claimed request on the child pairing fails truthfully when the pairing moves.
        _, queued = self.request('Fictional queued owner question')
        status, value = connect(self.project)
        self.assertEqual(status, 200, value)
        self.generation = value['generation']
        row = next(r for r in self.store._rows() if r['request_id'] == queued['request_id'])
        self.assertEqual((row['state'], row.get('launched')), ('failed', False))
        self.assertIn('Nothing was submitted', row['error'])
        # New work after the move reports its status normally.
        server.AGENTS[self.aid] = dict(self.agents[self.aid], cwd=self.project)
        _, fresh = self.request('Fictional owner question after the move')
        claim = self.manual_poll(self.project)[1]['claim']
        self.assertEqual(claim['request_id'], fresh['request_id'])
        submitted = dict(generation=self.generation, native_id=self.sid, request_id=claim['request_id'],
                         attempt_id=claim['attempt_id'], kind='submitted')
        self.assertEqual(self.http('/claude-mod/event', submitted, token=self.token)[0], 200)

    def test_manual_connect_authenticates_before_any_discovery(self):
        inside, enrollment = self.manual_child()
        calls = []
        real = self.store.shared.metadata_target
        def counted(*args):
            calls.append(args)
            return real(*args)
        with patch.object(self.store.shared, 'metadata_target', side_effect=counted), \
                patch.object(mod, 'project', wraps=mod.project) as canonical:
            self.assertEqual(self.http('/claude-mod/connect', dict(native_id=self.sid, project=self.project),
                                       token='f' * 64)[0], 403)
            self.assertEqual(self.http('/claude-mod/enroll', {'code': 'fictional-wrong-code-123', 'native_id': self.sid,
                                                              'project': self.project})[0], 403)
            self.assertEqual((calls, canonical.call_count), ([], 0))
            self.assertEqual(self.http('/claude-mod/connect', dict(native_id=self.sid, project=str(inside)),
                                       token=self.token)[0], 200)
            self.assertEqual((calls, canonical.call_count), ([], 0))          # an ordinary reconnect: no discovery
            held = []
            def under_lock(value):
                held.append(self.store.lock.locked() if hasattr(self.store.lock, 'locked') else None)
                return mod.os.path.realpath(value) == value
            canonical.side_effect = under_lock
            self.assertEqual(self.http('/claude-mod/connect', dict(native_id=self.sid, project=self.project),
                                       token=self.token)[0], 200)
            self.assertEqual(len(calls), 1)
            self.assertNotIn(True, held)

    def test_idle_present_room_vetoes_and_prepare_race(self):
        self.target();server.AGENTS.clear();self.setup_global()
        result=self.send()
        original=self.store.shared.prepare_mod_claim
        def veto(row):
            prepared=original(row)
            server.AGENTS[self.aid]=dict(self.agents[self.aid],parent='c:'+uid())
            return prepared
        with patch.object(self.store.shared,'prepare_mod_claim',side_effect=veto):
            self.assertIsNone(self.global_poll(self.body['client_id'])[1]['claim'])
        self.assertIsNone(self.store.db.execute('SELECT launched FROM shared_bridge_requests WHERE client_id=?',(result['request_id'],)).fetchone()[0])
        self.assertEqual(self.target()['capability'],'unavailable')
        self.assertEqual(self.store.mods.setup_status(dict(server.AGENTS))['connected_chats'],0)
        with self.assertRaises(peer_bridge.PeerError): self.send('Another fictional question')
        status,_=self.http('/agent-message',dict(agent=self.aid,question='Owner veto',request_id=uid()),owner=True)
        self.assertEqual(status,503)
        server.AGENTS.clear()
        for flags in [dict(runtime='Codex'),dict(sub=True),dict(observed='process'),dict(cwd='/fictional/wrong')]:
            server.AGENTS[self.aid]=dict(self.agents[self.aid],**flags)
            self.assertEqual(self.store.mods.setup_status(dict(server.AGENTS))['connected_chats'],0)
            self.assertEqual(self.target()['capability'],'unavailable')
        server.AGENTS.clear()

    def test_idle_owner_only_membership_never_grants_peer_receiving(self):
        self.target();server.AGENTS.clear();self.setup_global(consent=False,protocol=0)
        self.assertEqual(self.store.mods.setup_status({})['connected_chats'],1)
        self.assertEqual(self.target()['capability'],'unavailable')
        with self.assertRaises(peer_bridge.PeerError): self.send()
        self.request('Fictional owner-only question')
        self.assertIsNotNone(self.global_poll(self.body['client_id'])[1]['claim'])

    def test_idle_missing_receiver_is_truthful_and_metadata_is_not_liveness(self):
        target=self.target();server.AGENTS.clear()
        self.assertEqual(target['capability'],'unavailable')
        self.assertIn('official plugin',target['reason'])
        self.assertNotIn('identity can change',target['reason'])
        with self.assertRaises(peer_bridge.PeerError): self.send()
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM shared_bridge_requests').fetchone()[0],0)


if __name__=='__main__':unittest.main()
