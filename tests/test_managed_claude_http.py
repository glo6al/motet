"""Owner HTTP contract: status never launches, decisions never gain extra authority."""
import json
import sqlite3
import threading
from types import SimpleNamespace
import unittest
from test_new_chat_http import Handler, NATIVE
from managed_claude_http import handle_http

RUN = '/api/managed-claude/runs/' + NATIVE
PERMISSION = '22222222-2222-4222-8222-222222222222'


class Manager:
    enabled, demo = True, False

    def __init__(self):
        self.calls = []

    def status(self, ident):
        self.calls.append(('status', ident))
        return {'runId': ident, 'state': 'needs_attention', 'cwd': '/fictional/private',
                'token': 'fictional-secret', 'nativeId': 'not-public', 'messages': ['not-public'],
                'serverTime': 1900000030, 'timeoutSeconds': 120, 'evidenceAvailable': True,
                'permissions': [{'id': PERMISSION, 'tool': 'Write', 'input': {'file_path': '/fictional/action'},
                                 'state': 'pending', 'canAllow': True, 'canDeny': True,
                                 'createdAt': 1900000000, 'expiresAt': 1900000120,
                                 'decision': None, 'decisionState': None, 'resolutionReason': None,
                                 'resolvedAt': None, 'inputAvailable': True,
                                 'nativeRequestId': 'not-public'}]}

    def decide(self, run, permission, request, decision):
        self.calls.append(('decision', run, permission, request, decision))
        return dict(requestId=request, permissionId=permission, decision=decision, state='written',
                    decisionState='written', resolutionReason='owner', resolvedAt=1900000040, token='not-public')


class ManagedHttpTests(unittest.TestCase):
    def call(self, h, manager):
        handle_http(h, manager, 'fictional-key', 18801)
        self.assertTrue(h.close_connection)
        return h.result

    def post(self, raw=None):
        raw = raw or json.dumps({'requestId': NATIVE, 'decision': 'allow'}).encode()
        return Handler(RUN + '/permissions/' + PERMISSION, 'POST', raw,
                       [('Content-Type', 'application/json'), ('Content-Length', str(len(raw)))])

    def test_status_allowlists_only_owner_action_evidence(self):
        m = Manager()
        code, result = self.call(Handler(RUN), m)
        self.assertEqual(code, 200)
        self.assertEqual(m.calls, [('status', NATIVE)])
        self.assertNotIn('not-public', json.dumps(result))
        self.assertNotIn('fictional-secret', json.dumps(result))
        self.assertEqual(result['permissions'][0]['input']['file_path'], '/fictional/action')
        self.assertEqual({k: result[k] for k in ('serverTime', 'timeoutSeconds', 'evidenceAvailable')},
                         dict(serverTime=1900000030, timeoutSeconds=120, evidenceAvailable=True))
        item = result['permissions'][0]
        self.assertEqual(item['createdAt'], 1900000000)
        self.assertEqual(item['expiresAt'], 1900000120)
        self.assertTrue(item['inputAvailable'])
        for key in ('decision', 'decisionState', 'resolutionReason', 'resolvedAt'):
            self.assertIn(key, item)
            self.assertIsNone(item[key])

    def test_permission_exact_route_body_and_receipt(self):
        m = Manager()
        code, result = self.call(self.post(), m)
        self.assertEqual(code, 200)
        self.assertEqual(m.calls, [('decision', NATIVE, PERMISSION, NATIVE, 'allow')])
        self.assertNotIn('token', result)
        self.assertEqual(result['decisionState'], 'written')
        self.assertEqual(result['resolutionReason'], 'owner')
        self.assertEqual(result['resolvedAt'], 1900000040)

    def test_status_preserves_outcomes_and_disables_unreadable_evidence(self):
        m = Manager()
        raw = m.status(NATIVE)
        raw['permissions'][0].update(state='expired', decision='deny', resolutionReason='timeout',
            decisionState='unknown', resolvedAt=1900000120, canAllow=False, canDeny=False)
        m.status = lambda ident: raw
        code, result = self.call(Handler(RUN), m)
        self.assertEqual(code, 200)
        for key in ('decision', 'resolutionReason', 'decisionState', 'resolvedAt'):
            self.assertEqual(result['permissions'][0][key], raw['permissions'][0][key])
        raw.update(state='uncertain', evidenceAvailable=False, permissions=[])
        code, result = self.call(Handler(RUN), m)
        self.assertEqual(code, 200)
        self.assertFalse(result['evidenceAvailable'])
        self.assertFalse(result['capabilities']['permissions'])
        self.assertEqual(result['permissions'], [])

    def test_permission_extra_payload_cannot_change_scope(self):
        m = Manager()
        for body in ({'requestId': NATIVE, 'decision': 'allow', 'updatedPermissions': []},
                     {'requestId': NATIVE, 'decision': 'always_allow'}, {}):
            m.calls.clear()
            self.assertEqual(self.call(self.post(json.dumps(body).encode()), m)[0], 400)
            self.assertFalse(m.calls)

    def test_disabled_demo_and_auth_make_zero_manager_calls(self):
        for enabled, demo in ((False, False), (True, True)):
            m = Manager(); m.enabled, m.demo = enabled, demo
            for h in (Handler(RUN), self.post()):
                self.assertEqual(self.call(h, m)[0], 503)
            self.assertFalse(m.calls)
        for extra in [('Host', '127.0.0.1:18801'), ('Origin', 'https://fictional.invalid'),
                      ('X-Live-Room-Key', 'other')]:
            m = Manager()
            self.assertEqual(self.call(Handler(RUN, headers=[extra]), m)[0], 403)
            self.assertFalse(m.calls)
        h = self.post(); h.headers.replace_header('X-Live-Room-Key', 'wrong')
        class Unreadable:
            def read1(self, n):
                raise AssertionError('Read before auth')
        h.rfile = Unreadable()
        self.assertEqual(self.call(h, Manager())[0], 403)

    def test_permission_id_cannot_reuse_legacy_operation(self):
        db = sqlite3.connect(':memory:')
        try:
            for table in ('requests', 'controls', 'expired_controls'):
                db.execute('CREATE TABLE '+table+' (id TEXT)')
            for invalid in ([], {}, 3, None, 'not-a-uuid'):
                h, manager = self.post(json.dumps({'requestId': invalid, 'decision': 'allow'}).encode()), Manager()
                handle_http(h, manager, 'fictional-key', 18801, SimpleNamespace(db=db, lock=threading.RLock()))
                self.assertEqual(h.result[0], 400)
                self.assertFalse(manager.calls)
            for table in ('requests', 'controls', 'expired_controls'):
                db.execute('INSERT INTO '+table+' VALUES(?)', (NATIVE,))
                h, manager = self.post(), Manager()
                handle_http(h, manager, 'fictional-key', 18801, SimpleNamespace(db=db, lock=threading.RLock()))
                self.assertEqual(h.result[0], 409)
                self.assertFalse(manager.calls)
                db.execute('DELETE FROM '+table)
        finally:
            db.close()

    def test_ambiguous_bodies_and_routes_do_not_decide(self):
        m = Manager()
        for raw in (b'{"decision":"allow","decision":"deny"}', b'{"bad":1e999}', b'[]', b'{'):
            self.assertEqual(self.call(self.post(raw), m)[0], 400)
        for path in (RUN + '?x=1', RUN + '?', RUN + '#x', RUN.replace('/runs/', '/%72uns/')):
            self.assertEqual(self.call(Handler(path), m)[0], 400)
        self.assertFalse(m.calls)


if __name__ == '__main__':
    unittest.main()
