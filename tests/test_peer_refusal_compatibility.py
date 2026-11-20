"""Current server denials remain explicit refusals in the older signed adapter."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

import test_peer_progress_projection as fixtures
import test_peer_progress as ledger_fixtures


@unittest.skipIf(os.name == 'nt', 'POSIX private state')
class RefusalCompatibilityTests(unittest.TestCase):
    @contextmanager
    def store(self):
        fixture = fixtures.ProjectionTests()
        fixture.setUp()
        try:
            yield fixture
        finally:
            fixture.tearDown()

    def test_every_compact_denial_is_understood_by_the_frozen_adapter(self):
        root = Path(__file__).resolve().parents[1]
        node = shutil.which('node')
        if not node or not (root / 'integrations/claude/node_modules/@modelcontextprotocol/sdk').is_dir():
            self.skipTest('Optional offline MCP SDK is not installed.')
        reasons = []
        for state, bound, observation, age in (
                ('awaiting_reply', 'fictional-turn', 'started', 1200),
                ('awaiting_reply', 'fictional-turn', None, 1200),
                ('queued', '', 'accepted', 1200),
                ('queued', '', None, 1200),
                ('awaiting_reply', 'fictional-turn', 'started', 250000000000)):
            with self.store() as fixture:
                _, row = fixture.codex(state=state, bound=bound)
                if observation:
                    fixture.observe(row, observation, 1)
                now = row['created'] + age if age < 100000 else age
                with patch.object(fixtures.peer_bridge.time, 'time', return_value=now):
                    reasons.append(fixture.bridge._recipient_capacity(row['request_id'], row['agent'], 'running' if bound else 'submitted'))
        with self.store() as fixture:
            _, row = fixture.codex(state='awaiting_reply', bound='fictional-turn')
            row.update(state='unknown', tracking_stopped=True)
            fixture.store._save(row)
            reasons.append(fixture.bridge._recipient_capacity(row['request_id'], row['agent'], 'uncertain'))
        for started, claimed in ((False, True), (False, False), (True, True)):
            for age in (1200, fixtures.peer_bridge.RETENTION + 1):
                with self.store() as fixture:
                    rid, tid = ledger_fixtures.uid(), ledger_fixtures.uid()
                    agent = 'c:' + ledger_fixtures.uid()
                    created = fixtures.peer_bridge.time.time() - 10
                    target = dict(agent_id=agent, provider='claude', name='Fictional worker',
                                  project_label='Fictional project', identity_revision='a'*64, project_id='b'*64)
                    row = dict(origin='peer', bridge_version=2, request_id=tid, agent=agent, created=created,
                               state='queued', runtime='claude', transport='claude_mod', launched=True,
                               identity_revision='a'*64, mod_enrollment=ledger_fixtures.uid(),
                               mod_generation=ledger_fixtures.uid(), mod_attempt_id=ledger_fixtures.uid(),
                               mod_project='/fictional/project', target_project='/fictional/project')
                    fixture.store.db.execute('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,?,?)',
                                             (rid, agent, json.dumps(target), 'c'*64, tid,
                                              fixture.credential['credential_id'], created, created, 'Fictional preview'))
                    fixture.store._save(row)
                    if claimed:
                        fixture.observe(row, 'claim_issued', 1)
                    if started:
                        row.update(state='awaiting_reply', mod_turn='fictional-turn')
                        fixture.store._save(row)
                        fixture.observe(row, 'started', 2)
                    with patch.object(fixtures.peer_bridge.time, 'time', return_value=created + age):
                        reasons.append(fixture.bridge._recipient_capacity(tid, agent, 'running' if started else 'submitted'))
        ledger = ledger_fixtures.ProgressTests();ledger.setUp()
        try:
            for _ in range(4):
                ledger.admit(launch=False)
            reasons.append(fixtures.peer_lifecycle.capacity_error(ledger.db, 'x:' + ledger_fixtures.uid(), ledger_fixtures.CREATED + 1, 900))
        finally:
            ledger.doCleanups()
        result = subprocess.run([node, str(root/'integrations/claude/refusal-compat.mjs')], input=json.dumps(reasons),
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Legacy adapter confirmed', result.stdout)


if __name__ == '__main__':
    unittest.main()
