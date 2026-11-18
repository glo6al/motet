"""Real HTTP -> bound owner guard -> official Claude plugin -> exact reply."""
import json
import os
import shutil
import subprocess
import unittest

from delivery_admission_fixture import AGENT, DeliveryFixture, NATIVE, OWNER_KEY, ROOT


@unittest.skipIf(os.name == 'nt' or not shutil.which('node'), 'Requires POSIX private storage and Node')
class DeliveryAdmissionE2E(unittest.TestCase):
    def test_direct_relay_replay_refusal_and_lost_commit_response(self):
        with DeliveryFixture() as fixture:
            process = subprocess.run(
                ['node', str(ROOT / 'tests' / 'delivery_admission_runtime.mjs')],
                input=json.dumps(dict(port=fixture.port, owner_key=OWNER_KEY,
                                      native=NATIVE, project=fixture.project)),
                text=True, capture_output=True, timeout=40)
            self.assertEqual(process.returncode, 0, process.stdout + '\n' + process.stderr)
            result = json.loads(process.stdout)
            self.assertEqual(len(result['native_submissions']), 3)
            self.assertEqual(len(set(result['native_submissions'])), 3)
            self.assertEqual(sorted(result['native_submissions']), sorted(result['request_insertions']))
            self.assertEqual(len(result['stale_decisions']), 3)
            self.assertEqual(result['model_calls'], 3)
            self.assertTrue(all(row['agent'] == AGENT and row['state'] == 'answered'
                                and row['reply'] == 'Fictional Atlas reply.' for row in result['answered']))
            self.assertEqual(fixture.store.db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 3)


if __name__ == '__main__':
    unittest.main()
