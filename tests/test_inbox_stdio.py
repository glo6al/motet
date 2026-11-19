"""Real MCP/HTTP/SQLite inbox proof; fixture state only, no provider calls."""
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import unittest

import test_peer_inbox as fixtures

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')
SDK = ROOT / 'integrations/claude/node_modules/@modelcontextprotocol/sdk/package.json'


class DropClaimHandler(fixtures.Handler):
    def send(self, status, raw, content_type, extra=()):
        fixture = self.server.fixture
        if getattr(fixture, 'kill_pid', None):
            with fixture.store.lock:
                row = fixture.store.db.execute('SELECT state FROM peer_inbox_requests WHERE request_id=?',
                                               (fixture.kill_request,)).fetchone()
            if row and row[0] == 'claimed':
                os.kill(fixture.kill_pid, signal.SIGKILL)
                fixture.kill_pid = None
                self.close_connection = True
                return
        return super().send(status, raw, content_type, extra)


@unittest.skipUnless(os.name == 'posix' and NODE and SDK.exists(), 'POSIX, Node and installed MCP SDK')
class StdioTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.InboxTests('test_round_trip_no_native_paths_or_token_leaks')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.http.RequestHandlerClass = DropClaimHandler
        directory = self.fixture.state / 'bridge-credentials'
        directory.mkdir(mode=0o700)
        self.credential_file = directory / 'fictional.json'
        self.credential_file.write_text(json.dumps(dict(self.fixture.credential, port=self.fixture.http.server_port)))
        self.credential_file.chmod(0o600)

    def data(self, **extra):
        return dict(credential_file=str(self.credential_file), inbox_file=str(self.fixture.path), **extra)

    def run_stdio(self, data):
        return subprocess.run([NODE, 'tests/inbox_stdio.mjs'], cwd=ROOT,
                              input=json.dumps(data), text=True, capture_output=True, timeout=20)

    def test_real_stdio_round_trip(self):
        result = self.run_stdio(self.data(mode='round_trip'))
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(json.loads(result.stdout), {'ok': True, 'mode': 'round_trip'})
        self.fixture.store.shared.directory_factory.assert_not_called()

    def test_claim_survives_stdio_process_loss_with_original_prepared_token(self):
        request = self.fixture.result('send_inbox_message', self.fixture.send_args(text='Fictional lost acknowledgment.'))
        token = secrets.token_hex(32)
        data = self.data(mode='claim', request_id=request['request_id'], claim_token=token)
        child = subprocess.Popen([NODE, 'tests/inbox_stdio.mjs'], cwd=ROOT, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.fixture.kill_pid, self.fixture.kill_request = child.pid, request['request_id']
        child.communicate(json.dumps(data), timeout=20)
        self.assertEqual(child.returncode, -signal.SIGKILL)
        record = self.fixture.result('get_inbox_reply', {'request_id': request['request_id']})
        self.assertEqual(record['state'], 'claimed')
        self.assertNotIn(token, json.dumps(record))
        recovered = self.run_stdio(data)
        self.assertEqual(recovered.returncode, 0, recovered.stderr[-2000:])
        self.assertEqual(json.loads(recovered.stdout), {'ok': True, 'mode': 'claim'})
        self.fixture.store.shared.directory_factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
