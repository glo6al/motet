"""Offline dollar admission tests. No provider, native app or production server."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import copy
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.attachments_proof.harness import (
    BILLING, MODEL, SLOTS, InputBound, OfflineProofHarness, ProofFile,
    ProofRejected, ProofRequest, load_pricing_snapshot, synthetic_bound,
)


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='watch-offline-cap-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / 'scratch'
        self.folder.mkdir()
        self.other = self.root / 'other-scratch'
        self.other.mkdir()
        self.h = OfflineProofHarness(self.root)
        self.body = {'model': MODEL, 'max_tokens': 1024, 'system': 'Offline synthetic test only.',
                     'messages': [{'role': 'user', 'content': 'Fictional analysis.'}]}

    def request(self, slot='O1', **kw):
        values = dict(slot=slot, body=self.body, input_bound=synthetic_bound(self.body),
                      billing=copy.deepcopy(BILLING), expected_folder=self.folder, actual_folder=self.folder)
        values.update(kw)
        return ProofRequest(**values)

    def file(self, name, data, mime='text/plain'):
        path = self.root / name
        path.write_bytes(data)
        return ProofFile(path, hashlib.sha256(data).hexdigest(), mime)

    def assert_no_attempt(self, request, pattern):
        with self.assertRaisesRegex((ValueError, RuntimeError), pattern):
            self.h.run(request)
        self.assertEqual(self.h.summary()['stub_attempts'], 0)
        self.assertEqual(self.h.summary()['network_transmissions'], 0)
        self.assertEqual(self.h.status('O1')['state'], 'unused')

    def test_fixed_stub_and_idempotent_result_never_need_network(self):
        with patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')), patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS forbidden')):
            result = self.h.run(self.request())
            replay = self.h.run(self.request())
        self.assertEqual(result, replay)
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(result['result']['response']['content'][0]['text'], 'OFFLINE_STUB_ONLY')
        self.assertEqual(self.h.summary()['stub_attempts'], 1)
        self.assertEqual(self.h.summary()['reserved_usd'], '0.200')
        self.assertTrue(self.h.summary()['synthetic_bounds_only'])

    def test_eight_fixed_nonrecycled_slots_and_ninth_rejection(self):
        for slot in SLOTS:
            self.assertEqual(self.h.run(self.request(slot))['state'], 'complete')
        self.assertEqual(self.h.summary()['stub_attempts'], 8)
        with self.assertRaisesRegex(ProofRejected, 'unknown_slot'):
            self.h.run(self.request('O9'))
        self.assertEqual(self.h.summary()['reserved_usd'], '0.200')
        self.assertEqual(self.h.summary()['stub_attempts'], 8)

    def test_locked_pdf_unsupported_type_recipient_and_forwarding_before_attempt(self):
        # Controlled encrypted-PDF-shaped negative; generator integration tests use an actual PDF.
        locked = self.file('locked.pdf', b'%PDF-1.4\n/Encrypt 3 0 R\n%%EOF', 'application/pdf')
        self.assert_no_attempt(self.request(files=(locked,)), 'locked_pdf')
        self.assert_no_attempt(self.request(files=(replace(locked, media_type='application/zip'),)), 'unsupported_type')
        self.assert_no_attempt(self.request(recipient='cloud'), 'unsupported_recipient')
        self.assert_no_attempt(self.request(intent='forward'), 'officer_forwarding_refused')
        self.assert_no_attempt(self.request(synthetic=False), 'synthetic_fixture_only')

    def test_folder_mismatch_symlink_and_hash_mismatch_block(self):
        self.assert_no_attempt(self.request(actual_folder=self.other), 'folder_or_worktree_mismatch')
        f = self.file('fact.txt', b'fictional source')
        self.assert_no_attempt(self.request(files=(replace(f, sha256='0' * 64),)), 'fixture_digest_mismatch')
        link = self.root / 'alias.txt'
        link.symlink_to(f.path)
        self.assert_no_attempt(self.request(files=(replace(f, path=link),)), 'symlink')

    def test_real_utf8_and_magic_mismatch_are_checked(self):
        f = self.file('not-pdf.pdf', b'fictional source', 'application/pdf')
        self.assert_no_attempt(self.request(files=(f,)), 'invalid_fixture_pdf')
        f = self.file('invalid.txt', b'\xff', 'text/plain')
        self.assert_no_attempt(self.request(files=(f,)), 'invalid_utf8')

    def test_missing_mismatched_bound_billing_and_budget_caps(self):
        self.assert_no_attempt(self.request(input_bound=None), 'input_bound_unknown')
        self.assert_no_attempt(self.request(input_bound=InputBound(100, 'conservative', '0' * 64)), 'input_bound_invalid')
        self.assert_no_attempt(self.request(input_bound=synthetic_bound(self.body, 100001)), 'input_tokens_exhausted')
        self.assert_no_attempt(self.request(billing=None), 'billing_basis_unknown')
        self.assert_no_attempt(self.request(billing={**BILLING, 'extras': True}), 'billing_basis_unknown')
        for changed, reason in [({**self.body, 'max_tokens': 1025}, 'output_exhausted'),
                                ({**self.body, 'system': 'X' * 524288}, 'input_exhausted'),
                                ({**self.body, 'model': 'other-model'}, 'unsupported_model'),
                                ({**self.body, 'tools': []}, 'unsupported_body_field')]:
            self.assert_no_attempt(self.request(body=changed, input_bound=synthetic_bound(changed)), reason)

    def test_missing_pricing_and_changed_snapshot_cannot_admit_or_reopen(self):
        root = self.root / 'missing-price'; root.mkdir()
        folder = root / 'scratch'; folder.mkdir()
        h = OfflineProofHarness(root, pricing=None)
        with self.assertRaisesRegex(RuntimeError, 'pricing_unknown'):
            h.run(self.request(expected_folder=folder, actual_folder=folder))
        self.assertEqual(h.summary()['stub_attempts'], 0)
        with self.assertRaisesRegex(ProofRejected, 'binding'):
            OfflineProofHarness(root)
        changed = load_pricing_snapshot()
        changed['models'][MODEL]['input'] = '0'
        with self.assertRaisesRegex(ProofRejected, 'pricing_snapshot_mismatch'):
            OfflineProofHarness(self.root, pricing=changed)

    def test_lost_result_keeps_claim_and_halts_remaining_slots_across_restart(self):
        request = self.request()
        lost = self.h.run(request, fault='lost_result')
        self.assertEqual(lost['state'], 'unknown')
        self.assertEqual(lost['stub_attempts'], 1)
        resumed = OfflineProofHarness(self.root)
        self.assertEqual(resumed.run(request), lost)
        with self.assertRaisesRegex(ProofRejected, 'run_halted'):
            resumed.run(self.request('O2'))
        self.assertEqual(resumed.summary()['stub_attempts'], 1)
        self.assertEqual(resumed.summary()['reserved_usd'], '0.200')

    def test_fault_after_durable_claim_has_no_stub_but_never_retries(self):
        result = self.h.run(self.request(), fault='after_claim')
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(result['stub_attempts'], 0)
        resumed = OfflineProofHarness(self.root)
        self.assertEqual(resumed.run(self.request()), result)
        with self.assertRaisesRegex(ProofRejected, 'run_halted'):
            resumed.run(self.request('O2'))

    def test_measured_usage_overrun_halts_all_remaining_slots(self):
        result = self.h.run(self.request(), fault='usage_overrun')
        self.assertEqual(result['state'], 'overrun')
        self.assertTrue(self.h.summary()['halted'])
        self.assertTrue(result['result']['budget']['exhausted'])
        with self.assertRaisesRegex(ProofRejected, 'run_halted'):
            OfflineProofHarness(self.root).run(self.request('O2'))

    def test_same_body_different_manifest_or_folder_cannot_reuse_slot(self):
        f = self.file('first.txt', b'first fictional source')
        g = self.file('second.txt', b'second fictional source')
        self.h.run(self.request(files=(f,)))
        with self.assertRaisesRegex(ProofRejected, 'fingerprint_conflict'):
            self.h.run(self.request(files=(g,)))
        with self.assertRaisesRegex(ProofRejected, 'fingerprint_conflict'):
            self.h.run(self.request(files=(f,), expected_folder=self.other, actual_folder=self.other))
        self.assertEqual(self.h.summary()['stub_attempts'], 1)

    def test_concurrent_duplicate_claim_has_one_stub_attempt(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.h.run(self.request()), range(8)))
        self.assertTrue(all(r['state'] in ('unknown', 'complete') for r in results))
        self.assertEqual(self.h.summary()['stub_attempts'], 1)
        self.assertEqual(self.h.status('O1')['state'], 'complete')

    def test_ledger_corruption_does_not_reset_capacity(self):
        self.h.run(self.request())
        with sqlite3.connect(self.h.path) as db:
            db.execute("UPDATE slots SET reserved_usd='0' WHERE slot='O1'")
        with self.assertRaises(ProofRejected):
            OfflineProofHarness(self.root)
        with self.assertRaises(ProofRejected):
            self.h.summary()

    def test_replacing_file_identity_conflicts_even_with_same_content(self):
        f = self.file('replace.txt', b'unchanged fictional bytes')
        self.h.run(self.request(files=(f,)))
        f.path.rename(self.root / 'original.txt')
        f.path.write_bytes(b'unchanged fictional bytes')
        with self.assertRaisesRegex(ProofRejected, 'fingerprint_conflict'):
            self.h.run(self.request(files=(f,)))
        self.assertEqual(self.h.summary()['stub_attempts'], 1)

    def test_replacing_folder_identity_conflicts_with_unchanged_file(self):
        f = self.file('unchanged.txt', b'unchanged fictional bytes')
        self.h.run(self.request(files=(f,)))
        self.folder.rename(self.root / 'original-scratch')
        self.folder.mkdir()
        with self.assertRaisesRegex(ProofRejected, 'fingerprint_conflict'):
            self.h.run(self.request(files=(f,)))
        self.assertEqual(self.h.summary()['stub_attempts'], 1)

    def test_changed_budget_evidence_cannot_recover_an_old_claim(self):
        self.h.run(self.request())
        with self.assertRaisesRegex(ProofRejected, 'fingerprint_conflict'):
            self.h.run(self.request(input_bound=synthetic_bound(self.body, 100)))
        self.assertEqual(self.h.summary()['stub_attempts'], 1)

    def test_replaced_or_symlinked_ledger_is_rejected_on_existing_instance(self):
        original = self.root / 'original-ledger.sqlite'
        self.h.path.rename(original)
        self.h.path.write_bytes(original.read_bytes())
        with self.assertRaisesRegex(ProofRejected, 'identity_changed'):
            self.h.summary()
        self.h.path.unlink()
        self.h.path.symlink_to(original)
        with self.assertRaisesRegex(ProofRejected, 'identity_changed'):
            self.h.run(self.request())

    def test_raw_orphan_claim_blocks_other_slots_before_and_after_reopen(self):
        self.h.run(self.request(), fault='after_claim')
        with sqlite3.connect(self.h.path) as db:
            db.execute("UPDATE slots SET state='claimed',result=NULL WHERE slot='O1'")
            db.execute('UPDATE meta SET halted=0 WHERE id=1')
        # A different active slot cannot run while a claim is unresolved.
        with self.assertRaisesRegex(ProofRejected, 'unresolved_claim'):
            self.h.run(self.request('O2'))
        resumed = OfflineProofHarness(self.root)
        self.assertEqual(resumed.status('O1')['state'], 'unknown')
        self.assertEqual(resumed.status('O1')['result']['reason'], 'unresolved_claim_on_open')
        with self.assertRaisesRegex(ProofRejected, 'run_halted'):
            resumed.run(self.request('O2'))
        self.assertEqual(resumed.summary()['stub_attempts'], 0)

    def test_lost_result_fault_calls_fixed_stub_once_then_discards_response(self):
        with patch.object(OfflineProofHarness, '_stub', wraps=OfflineProofHarness._stub) as stub:
            self.h.run(self.request(), fault='lost_result')
            self.h.run(self.request())
            self.assertEqual(stub.call_count, 1)

    def test_missing_ledger_after_unknown_does_not_reset_run(self):
        self.h.run(self.request(), fault='lost_result')
        self.h.path.unlink()
        with self.assertRaisesRegex(ProofRejected, 'incomplete_or_missing_run_state'):
            OfflineProofHarness(self.root)
        self.assertFalse(self.h.path.exists())
        self.assertTrue(self.h.marker.exists())

    def test_missing_or_changed_run_marker_fails_closed(self):
        self.h.marker.unlink()
        with self.assertRaisesRegex(ProofRejected, 'incomplete_or_missing_run_state'):
            OfflineProofHarness(self.root)
        with self.assertRaisesRegex(ProofRejected, 'identity_changed'):
            self.h.summary()

    def test_marker_only_partial_initialization_is_not_reinitialized(self):
        root = self.root / 'partial'; root.mkdir()
        marker = root / '.offline-proof-run.json'
        marker.write_text('{"version":1,"stub_only":true,"run_id":"' + 'a' * 32 + '"}')
        with self.assertRaisesRegex(ProofRejected, 'incomplete_or_missing_run_state'):
            OfflineProofHarness(root)
        self.assertFalse((root / 'offline-proof.sqlite').exists())

    def test_reopen_between_claim_and_attempt_prevents_unrecorded_stub_call(self):
        original_db = self.h._db
        opens = 0

        @contextmanager
        def reopen_before_second_transaction():
            nonlocal opens
            opens += 1
            if opens == 2:
                OfflineProofHarness(self.root)
            with original_db() as db:
                yield db

        with patch.object(self.h, '_db', reopen_before_second_transaction), patch.object(
                OfflineProofHarness, '_stub', wraps=OfflineProofHarness._stub) as stub:
            result = self.h.run(self.request())
            self.assertEqual(stub.call_count, 0)
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(result['stub_attempts'], 0)
        self.assertEqual(result['result']['reason'], 'unresolved_claim_on_open')
        self.assertEqual(self.h.summary()['stub_attempts'], 0)
        self.assertTrue(self.h.summary()['halted'])

    def test_no_transport_configuration_surface(self):
        with self.assertRaises(TypeError):
            OfflineProofHarness(self.root, transport=lambda: None)
        with self.assertRaises(TypeError):
            self.h.run(self.request(), endpoint='https://example.invalid')


if __name__ == '__main__':
    unittest.main()
