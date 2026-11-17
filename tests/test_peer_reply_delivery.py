"""Automatic notice contracts. Fictional metadata; native admission is mocked."""
import copy
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import officer_messaging as messaging
import peer_bridge as bridge
import peer_reply_delivery as replies
import server


def uid():
    return str(uuid.uuid4())


class Directory:
    def __init__(self):
        self.lock, self._entries = threading.RLock(), {}

    def resolve(self, aid, revision, require_reachable=True):
        value = self._entries[aid]
        if value['identity_revision'] != revision or require_reachable and value['capability'] != 'reachable':
            raise bridge.PeerError(409, 'Changed fictional metadata.')
        return copy.deepcopy(value)


class PortableContracts(unittest.TestCase):
    def test_canonical_digest(self):
        data = dict(agent_id='x:'+uid(), identity_revision='a'*64, text='Fictional 😀\ntext')
        expected = bridge.digest(json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode())
        self.assertEqual(bridge.receipt_fingerprint(**data), expected)
        data['declared_sender'] = 'Fictional sender'
        self.assertEqual(bridge.receipt_fingerprint(**data), bridge.digest(json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()))
        self.assertNotEqual(bridge.receipt_fingerprint(**data), expected)


    def test_strict_duplicate_and_metadata_fields(self):
        manager = replies.ReplyDelivery.__new__(replies.ReplyDelivery)
        data = dict(version=1, provider='codex', native_id=uid(), source_scope='native_parent', tool_use_id='fictional-call',
                    request_id=uid(), target_agent_id='c:'+uid(), target_identity_revision='a'*64, created_at=time.time(),
                    args_sha256='b'*64, observed_at=time.time(), duplicate=False)
        manager.validate(data)
        for changes in [dict(duplicate=True), dict(duplicate=0), dict(version=True), dict(provider='other'), dict(source_scope='worker'),
                        dict(tool_use_id='bad\ncall'), dict(turn_id=''), dict(args_sha256=True), dict(observed_at=float('nan')),
                        dict(cwd='/fictional'), dict(answer='must not accept body'), dict(created_at=True)]:
            with self.subTest(changes=changes), self.assertRaises(bridge.PeerError):manager.validate(dict(data, **changes))


@unittest.skipIf(os.name == 'nt', 'POSIX private state and lifetime lock')
class ReplyRetirementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.base = Path(self.temp.name).resolve()
        self.sessions = self.base/'sessions'; self.sessions.mkdir()
        self.store = messaging.MessagingStore(str(self.base/'state'), str(self.sessions))

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def legacy(self, state, age=0):
        row = dict(request_id=uid(), notice_id=uid(), hook_id=uid(), receipt_hash='a'*64,
                   state=state, created=time.time()-age, source={'agent_id':'c:'+uid(),'project':'fictional'},
                   mod_enrollment=uid(), attempt_id=uid() if state not in replies.LIVE else None,
                   attempted_at=time.time() if state not in replies.LIVE else None)
        with self.store.lock, self.store.db:
            self.store.db.execute('INSERT INTO peer_reply_hook_credentials VALUES (?,?,1,?,?)',
                                 (row['hook_id'],'fictional-secret','["codex","claude"]',time.time()))
            self.store.db.execute('INSERT INTO peer_reply_deliveries (request_id,notice_id,state,created,checked,data) VALUES (?,?,?,?,0,?)',
                                 (row['request_id'],row['notice_id'],state,row['created'],json.dumps(row)))
        return row

    def test_setup_observe_and_restored_credential_cannot_reenable(self):
        row=self.legacy('pending'); delivery=self.store.reply_delivery
        before=self.store.db.total_changes
        for operation in [lambda:delivery.setup({},8793),lambda:delivery.observe({},{}),lambda:delivery.credential(row['hook_id'])]:
            with self.assertRaises(bridge.PeerError) as error:operation()
            self.assertEqual(error.exception.status,410)
            self.assertIn('removed',str(error.exception))
        self.assertEqual(before,self.store.db.total_changes)
        self.assertFalse(delivery.status()['enabled'])
        self.assertTrue(delivery.status()['retired'])
        new=self.base/'never-created'
        install=Mock()
        with self.assertRaisesRegex(ValueError,'removed'):replies.provision(str(new),['codex'],install=install)
        self.assertFalse(new.exists());install.assert_not_called()

    def test_startup_and_tick_retire_legacy_rows_without_native_submission(self):
        for restart in (False,True):
            rows=[self.legacy(state) for state in ['waiting_reply','pending','dispatching','queued','unknown']]
            with patch.object(self.store,'_queue_codex') as launch, patch.object(self.store.reply_delivery,'prepare') as prepare:
                if restart:
                    self.store.reply_delivery=replies.ReplyDelivery(self.store)
                self.store.reply_delivery.tick()
                launch.assert_not_called();prepare.assert_not_called()
            for row,expected in zip(rows,['failed','failed','unknown','queued','unknown']):
                actual=self.store.reply_delivery.get(row['request_id'])
                self.assertEqual(actual['state'],expected)
                self.assertEqual(actual['attempt_id'],row['attempt_id'])
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM peer_reply_hook_credentials WHERE active=1').fetchone()[0],0)
            # Even a pending row restored after startup cannot be claimed or block work.
            row=self.legacy('dispatching')
            self.assertFalse(self.store.reply_delivery.busy(row['mod_enrollment']))
            self.assertEqual(self.store.reply_delivery.candidates(row['source']['agent_id']),[])
            self.assertIsNone(self.store.reply_delivery.claim_in_transaction({},{}))

    def test_legacy_ack_is_idempotent_without_recreating_or_resubmitting_notice(self):
        row=self.legacy('dispatching');d=self.store.reply_delivery;d.tick()
        entry=dict(id=row['mod_enrollment'],grant_id='',agent=row['source']['agent_id'],project='fictional')
        data=dict(generation=uid(),native_id=entry['agent'][2:],request_id=row['notice_id'],attempt_id=row['attempt_id'],kind='submitted')
        with self.store.lock,self.store.db:
            first=d.event_in_transaction(entry,data);second=d.event_in_transaction(entry,data)
        self.assertEqual(first,second);self.assertEqual(first['state'],'queued')
        with patch.object(self.store,'_queue_codex') as launch:d.tick();launch.assert_not_called()
        self.assertEqual(len(d.statuses(entry)),1)
        bad=dict(data,attempt_id=uid())
        with self.assertRaises(bridge.PeerError):d.event_in_transaction(entry,bad)

    def test_busy_retirement_maintenance_retries_without_emitting(self):
        row=self.legacy('pending');d=self.store.reply_delivery
        writer=sqlite3.connect(self.base/'state'/'officer-messages.sqlite3')
        self.store.db.execute('PRAGMA busy_timeout=10')
        try:
            writer.execute('BEGIN IMMEDIATE')
            with patch.object(self.store,'_queue_codex') as launch:
                d.tick();launch.assert_not_called()
            self.assertEqual(d.get(row['request_id'])['state'],'pending')
            self.assertIsNone(d.claim_in_transaction({},{}))
            self.assertFalse(d.status()['enabled'])
        finally:writer.rollback();writer.close()
        d.tick();self.assertEqual(d.get(row['request_id'])['state'],'failed')

    def test_expired_legacy_record_is_scrubbed_but_ack_tombstone_survives(self):
        row=self.legacy('dispatching',age=bridge.RETENTION+1);d=self.store.reply_delivery;d.tick()
        stored=d.get(row['request_id'])
        self.assertEqual(stored['state'],'unknown');self.assertNotIn('source',stored)
        self.assertNotIn('mod_enrollment',stored);self.assertIn('ack_binding',stored)
        self.assertEqual(stored['notice_id'],row['notice_id'])

    def test_owner_http_setup_is_gone_and_revoke_status_still_work(self):
        import http.server
        httpd=server.Server(('127.0.0.1',0),server.H)
        legacy=self.legacy('pending')
        thread=threading.Thread(target=httpd.serve_forever,daemon=True)
        with patch.multiple(server,MESSAGING=self.store,KEY='fictional-key',PORT=httpd.server_port):
            thread.start()
            try:
                for path,expected in [('/peer-reply-setup',410),('/peer-reply-revoke',200),('/peer-reply-status',200)]:
                    c=http.client.HTTPConnection('127.0.0.1',httpd.server_port)
                    c.request('POST',path,'{}',{'X-Live-Room-Key':'fictional-key','Content-Type':'application/json'})
                    r=c.getresponse();body=json.loads(r.read());c.close()
                    self.assertEqual(r.status,expected,body)
                    if path.endswith('status'):self.assertFalse(body['enabled'])
                c=http.client.HTTPConnection('127.0.0.1',httpd.server_port)
                challenge=dict(credential_id=legacy['hook_id'],nonce='b'*64)
                c.request('POST','/peer-reply/v1/challenge',json.dumps(challenge),{'Content-Type':'application/json'})
                r=c.getresponse();body=json.loads(r.read());c.close()
                self.assertEqual(r.status,410,body)
            finally:httpd.shutdown();httpd.server_close();thread.join(5)


if __name__ == '__main__':unittest.main()
