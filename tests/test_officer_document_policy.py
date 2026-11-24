"""Dated immutable qualification, exact tier/default billing; no network."""
from dataclasses import replace
from decimal import localcontext
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import officer_document_policy as p
import officer_document_ledger as ledger
import officer_documents as documents
from attachments_types import AttachmentError
import test_attachments_officer_storage as fixtures
from attachment_host import POSIX, POSIX_WHY, STORE, STORE_WHY
from test_officer_documents import response, wire


def usage(n=100,geo='global',**kw):
    return documents.DocumentUsage(n,10,0,0,0,0,0,'standard',geo,'standard',**kw)


class PolicyTests(unittest.TestCase):
    def test_exact_profile_and_worst_both_default_geographies(self):
        profile=p.input_profile();documents._profile(profile)
        self.assertEqual(profile.model_id,'claude-haiku-5-5')
        self.assertEqual(profile.strict_budget_evidence.max_input_tokens,1_000_000)
        self.assertEqual(p.reserve_units(),1_102_816_000)
        self.assertEqual(profile.strict_budget_evidence.usd_ceiling,'1.11')
        p.validate_pricing(profile,p.document_pricing())
        for change in ({'max_output_tokens':1025},{'model_id':'other-model'},
                       {'strict_budget_evidence':replace(profile.strict_budget_evidence,max_input_tokens=100)}):
            with self.subTest(change=change),self.assertRaises(AttachmentError):
                p.validate_pricing(replace(profile,**change),p.document_pricing())
        for change in ({'input_rate':1},{'qualification_id':'invented'},{'inference_geo':'global'}):
            with self.subTest(change=change),self.assertRaises(AttachmentError):
                p.validate_pricing(profile,replace(p.document_pricing(),**change))

    def test_renewed_window_and_evidence(self):
        """The 2026-11-24 renewal: a new month-long window, a new evidence id, the same prices. The old window is not
        accepted, nor is the end of the new one, nor a policy or registered budget from the old qualification."""
        import datetime
        def epoch(day): return int(datetime.datetime(*day, tzinfo=datetime.timezone.utc).timestamp())
        self.assertEqual((p.VALID_FROM, p.VALID_UNTIL), (epoch((2026, 11, 24)), epoch((2026, 12, 24))))
        self.assertEqual((p.EVIDENCE_ID, p.PRICING_REVISION), ('haiku-pdf-png-2026-11-24', 'haiku-tiered-2026-11-24'))
        self.assertEqual((p.HIGH, p.LOW, p.LOW_THRESHOLD, p.MAX_INPUT, p.PER_CALL_USD),
                         ((500, 2500, 50, 625, 1000), (100, 500, 10, 125, 200), 100_000, 1_000_000, '1.11'))
        old_from, old_until = epoch((2026, 11, 15)), epoch((2026, 11, 22))
        for stamp in (old_from, old_until - 1, old_until, p.VALID_UNTIL, p.VALID_UNTIL + 1):
            with self.subTest(stamp=stamp), self.assertRaises(AttachmentError):
                p.current(lambda: stamp)
        p.current(lambda: p.VALID_FROM)
        p.current(lambda: p.VALID_UNTIL - 1)
        with self.assertRaises(AttachmentError):
            p.HostAllowance('haiku-pdf-png-2026-11-15', 'fictional-scope', 'v1', '2.22').validate(lambda: p.VALID_FROM + 1)
        self.assertEqual(p.HostAllowance(p.EVIDENCE_ID, 'fictional-scope', 'v1', '2.22').validate(lambda: p.VALID_FROM + 1).usd_limit,
                         '2.22')

    def test_half_open_validity_no_clock_reset_or_extension(self):
        for stamp in (p.VALID_FROM,p.VALID_UNTIL-0.001):p.current(lambda:stamp)
        for stamp in (p.VALID_FROM-1,p.VALID_UNTIL,float('nan'),float('inf'),True,'today'):
            with self.subTest(stamp=stamp),self.assertRaises(AttachmentError):p.current(lambda:stamp)

    def test_tier_boundary_and_us_estimate_exact_integer_ceiling(self):
        self.assertEqual(p.usage_units(usage(100000)),100000*100+10*500)
        self.assertEqual(p.usage_units(usage(100001)),100001*500+10*2500)
        self.assertEqual(p.usage_units(usage(100000,'us')),(100000*100+10*500)*11//10)
        partial=documents.DocumentUsage(0,0,1,0,1,0,0,'standard','us','standard')
        self.assertEqual(p.usage_units(partial),138)
        with localcontext() as context:
            context.prec=2
            self.assertEqual(p.usage_units(partial),138)
        for invalid in (replace(partial,cache_creation_input_tokens=2),replace(partial,inference_geo='elsewhere'),
                        replace(partial,input_tokens=True),replace(partial,speed='fast'),usage(1000001),
                        replace(partial,output_tokens=1025)):
            with self.subTest(invalid=invalid),self.assertRaises(AttachmentError):p.usage_units(invalid)

    def test_gate_and_demo_do_not_read_policy_or_construct_store(self):
        with mock.patch.object(os,'open',side_effect=AssertionError('must not open')):
            for enabled,demo in ((False,False),(True,True),(1,False),(True,0)):
                self.assertIsNone(p.load_allowance('fictional-not-opened',enabled=enabled,demo=demo))

    @unittest.skipUnless(POSIX, POSIX_WHY)       # Windows: WindowsPolicyTests and the simulated tests below
    def test_closed_private_allowance_file_and_no_evidence_override(self):
        with tempfile.TemporaryDirectory() as raw:
            parent=Path(os.path.realpath(raw)); path=parent/p.POLICY_FILENAME
            data={'version':1,'evidenceId':p.EVIDENCE_ID,'scopeId':'fictional-scope','scopeRevision':'v1','usdLimit':'2.22'}
            def write(value):
                path.write_text(value if type(value) is str else json.dumps(value));path.chmod(0o600)
            def load():return p.load_allowance(parent,enabled=True,clock=lambda:p.VALID_FROM+1)
            write(data);self.assertEqual(load().usd_limit,'2.22')
            for invalid in (dict(data,rate=1),dict(data,evidenceId='invented'),dict(data,usdLimit='0.01'),
                            dict(data,usdLimit='NaN'),dict(data,version=True),dict(data,validUntil=9999999999),
                            '{"version":1,"version":1}'):
                write(invalid)
                with self.subTest(invalid=invalid),self.assertRaises(AttachmentError):load()
            write(data);path.chmod(0o644)
            with self.assertRaises(AttachmentError):load()
            path.chmod(0o600); link=parent/'duplicate';os.link(path,link)
            with self.assertRaises(AttachmentError):load()
            link.unlink();path.rename(link);path.symlink_to(link.name)
            with self.assertRaises(AttachmentError):load()
            path.unlink();link.rename(path)
            old_read=os.read
            def change(fd,size):
                result=old_read(fd,size);path.write_text(json.dumps(dict(data,usdLimit='3.33')));return result
            with mock.patch.object(os,'read',side_effect=change),self.assertRaises(AttachmentError):load()


DATA = {'version': 1, 'evidenceId': p.EVIDENCE_ID, 'scopeId': 'fictional-scope', 'scopeRevision': 'v1', 'usdLimit': '2.22'}
NOW = p.VALID_FROM + 1


@unittest.skipIf(os.name == 'nt', 'the simulation answers Windows calls with the POSIX walk')
class SimulatedWindowsPolicyTests(unittest.TestCase):
    """load_allowance's Windows branch under tests/windows_simulation.py: the walk and handle checks are answered by
    POSIX, and os.getuid and fcntl fail loudly, so a POSIX-only call left in the Windows branch shows up."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from windows_simulation import simulated_windows
        self.simulated_windows = simulated_windows
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(os.path.realpath(temporary.name)) / 'state'
        os.mkdir(self.parent, 0o700)
        self.path = self.parent / p.POLICY_FILENAME

    def write(self, value, mode=0o600):
        if self.path.exists() or self.path.is_symlink():
            self.path.unlink()
        self.path.write_bytes((value if type(value) is str else json.dumps(value)).encode('utf-8'))
        self.path.chmod(mode)

    def load(self):
        with self.simulated_windows() as calls:
            result = p.load_allowance(self.parent, enabled=True, clock=lambda: NOW)
        return result, calls

    def refused(self):
        with self.assertRaises(AttachmentError) as raised:
            self.load()
        self.assertEqual(raised.exception.code, 'document_policy_unavailable')

    def test_a_private_policy_loads_through_the_walk_and_handles(self):
        self.write(DATA)
        allowance, calls = self.load()
        self.assertEqual((allowance.scope_id, allowance.usd_limit), ('fictional-scope', '2.22'))
        for name in ('open_folder', 'file_identity'):
            self.assertIn(name, calls)

    def test_every_content_check_still_applies(self):
        for invalid in (dict(DATA, rate=1), dict(DATA, evidenceId='invented'), dict(DATA, usdLimit='0.01'),
                        dict(DATA, usdLimit='NaN'), dict(DATA, version=True), '{"version":1,"version":1}', '',
                        'x' * 4097):
            with self.subTest(invalid=str(invalid)[:40]):
                self.write(invalid)
                with self.assertRaises(AttachmentError):     # as on POSIX: an unreadable limit has its own code
                    self.load()

    def test_what_is_not_a_private_file_with_one_name_is_refused(self):
        self.write(DATA, 0o644)
        self.refused()
        self.write(DATA)
        os.link(self.path, self.parent / 'second-name')
        self.refused()
        os.unlink(self.parent / 'second-name')
        self.path.rename(self.parent / 'elsewhere.json')
        os.symlink('elsewhere.json', self.path)
        self.refused()
        self.path.unlink()
        self.refused()                                     # missing
        os.chmod(self.parent, 0o755)
        (self.parent / 'elsewhere.json').rename(self.path)
        self.refused()                                     # the folder is not private
        os.chmod(self.parent, 0o700)
        self.assertEqual(self.load()[0].usd_limit, '2.22')

    def test_a_policy_changed_while_it_is_read_is_refused(self):
        self.write(DATA)
        old_read = os.read

        def change(fd, size):
            result = old_read(fd, size)
            self.path.write_bytes(json.dumps(dict(DATA, usdLimit='3.33')).encode('utf-8'))
            return result
        with mock.patch.object(os, 'read', side_effect=change):
            self.refused()

    def test_the_dates_and_gates_still_come_first(self):
        self.write(DATA)
        with self.simulated_windows() as calls, self.assertRaises(AttachmentError):
            p.load_allowance(self.parent, enabled=True, clock=lambda: p.VALID_UNTIL)
        self.assertNotIn('open_folder', calls, 'an expired qualification reads nothing')
        with self.simulated_windows() as calls:
            self.assertIsNone(p.load_allowance(self.parent, enabled=False))
        self.assertEqual(calls, [])


def icacls(*args):
    import subprocess
    subprocess.run(['icacls'] + list(args), check=True, capture_output=True, timeout=30)


@unittest.skipUnless(os.name == 'nt', 'requires real Windows access lists and junctions')
class WindowsPolicyTests(unittest.TestCase):
    """The policy file on a real NTFS volume, in a state folder made private and owned by this user (the runner is
    elevated, so a plain new folder would belong to Administrators)."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(os.path.realpath(temporary.name))
        self.parent = self.base / 'state'
        import private_storage
        private_storage.make_private_folder(str(self.parent))
        self.path = self.parent / p.POLICY_FILENAME

    def load(self):
        return p.load_allowance(self.parent, enabled=True, clock=lambda: NOW)

    def refused(self):
        with self.assertRaises(AttachmentError) as raised:
            self.load()
        self.assertEqual(raised.exception.code, 'document_policy_unavailable')

    def private_policy(self, value=DATA):
        import private_storage
        fd = private_storage.create_private_file(str(self.path))       # protected: this user and SYSTEM only
        try:
            os.write(fd, json.dumps(value).encode('utf-8'))
        finally:
            os.close(fd)

    def test_a_private_policy_loads(self):
        self.private_policy()
        self.assertEqual(self.load().usd_limit, '2.22')

    def refused_for(self, reason):
        """Refused, and for ``reason`` (checked separately on the file, since the policy's refusal is one code)."""
        import private_storage
        self.refused()
        with self.assertRaises(private_storage.PrivateStorageError) as raised:
            private_storage.windows_security(str(self.path))
        self.assertEqual(raised.exception.reason, reason)

    def make_mine(self):
        import subprocess
        subprocess.run('icacls "%s" /setowner "%%USERDOMAIN%%\\%%USERNAME%%"' % self.path, shell=True, check=True,
                       capture_output=True, timeout=30)

    def test_a_policy_that_follows_the_folder_access_list_is_refused(self):
        self.path.write_bytes(json.dumps(DATA).encode('utf-8'))       # inherited, not protected
        self.make_mine()             # the elevated runner made it Administrators'; the owner is not what is tested
        self.refused_for('access')

    def test_a_protected_policy_that_also_names_administrators_is_refused(self):
        """A protected policy owned by this user, with an explicit Administrators grant added to the file (as icacls
        /grant would add it): still not private, since a file may name only this user and SYSTEM."""
        self.private_policy()
        icacls(str(self.path), '/grant', '*S-1-5-32-544:(R)')
        self.refused_for('access')

    def test_the_documented_icacls_command_makes_it_load(self):
        self.path.write_bytes(json.dumps(DATA).encode('utf-8'))
        import subprocess
        subprocess.run('icacls "%s" /setowner "%%USERDOMAIN%%\\%%USERNAME%%"' % self.path, shell=True, check=True,
                       capture_output=True, timeout=30)
        subprocess.run('icacls "%s" /inheritance:r /grant:r "%%USERDOMAIN%%\\%%USERNAME%%:F"' % self.path, shell=True,
                       check=True, capture_output=True, timeout=30)
        self.assertEqual(self.load().usd_limit, '2.22')

    def test_a_policy_readable_by_others_is_refused(self):
        self.private_policy()
        icacls(str(self.path), '/grant', '*S-1-1-0:(R)')
        self.refused()

    def test_a_junction_in_place_of_the_policy_or_its_folder_is_refused(self):
        import subprocess
        elsewhere = self.base / 'elsewhere'
        elsewhere.mkdir()
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(self.path), str(elsewhere)], check=True, capture_output=True,
                       timeout=30)
        self.refused()
        os.rmdir(self.path)
        self.private_policy()
        moved = self.base / 'state-moved'
        os.rename(self.parent, moved)
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(self.parent), str(moved)], check=True, capture_output=True,
                       timeout=30)
        self.refused()
        os.rmdir(self.parent)
        os.rename(moved, self.parent)
        self.assertEqual(self.load().usd_limit, '2.22')

    def test_a_second_name_is_refused(self):
        self.private_policy()
        os.link(self.path, self.parent / 'second-name')
        self.refused()


@unittest.skipUnless(STORE, STORE_WHY)
class QualifiedLedgerTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DirectStoreTests();self.f.setUp();self.store=self.f.store
        self.store.clock=lambda:p.VALID_FROM+1
        self.f.binding['modelId']=documents.MODEL
        self.l=ledger.OfficerDocumentLedger(self.store)
        self.profile=p.input_profile();self.price=p.document_pricing()
        self.scope=ledger.DocumentBudgetScope('fictional-qualified','v1','2.22')
    def tearDown(self):self.f.tearDown()
    OLD = dict(EVIDENCE_ID='haiku-pdf-png-2026-11-15', VALID_FROM=1794700800, VALID_UNTIL=1795305600,
               PRICING_REVISION='haiku-tiered-2026-11-15')

    def register_old(self, scope=None, **old):
        """Register a budget as the 2026-11-15 build did: its constants, its window, its clock."""
        with mock.patch.multiple(p, **dict(self.OLD, **old)):
            self.store.clock = lambda: p.VALID_FROM + 1
            self.l.register_scope(scope or self.scope, p.input_profile(), p.document_pricing())
        self.store.clock = lambda: p.VALID_FROM + 1

    def stored(self):
        return [tuple(row) for row in self.store.db.execute('SELECT * FROM officer_document_scopes')]

    def answer(self, request, profile):
        """Claim, consume and settle one fictional question; the fixture response uses 100 input, 80 output tokens."""
        state, permit, prepared = self.l.claim(self.f.selection, request['id'])
        self.assertTrue(self.l.consume(permit, self.f.selection, request['id'], prepared.body_digest, state['profileDigest']))
        raw = wire(response('Fictional answer.', citations=False))
        candidate = documents.validate_response(profile, prepared, raw)
        facts = ledger.ResultFacts(request['id'], state['claimNonce'], prepared.body_digest, state['profileDigest'],
                                   'accepted', hashlib.sha256(raw).hexdigest(), candidate.message_id, documents.MODEL,
                                   p.BILLING_REVISION, candidate.usage_facts)
        return self.l.record_result(self.f.selection, facts)

    def test_a_budget_from_the_earlier_qualification_carries_forward_with_its_charges(self):
        """(a) The 2026-11-15 budget keeps working under the renewal: the same row, its settled charge and its held
        reservation still count, and new questions draw on the same remaining amount."""
        scope = ledger.DocumentBudgetScope('fictional-qualified', 'v1', '3.33')
        d = self.f.draft(); self.f.upload(d); self.f.admit(d)
        with mock.patch.multiple(p, **self.OLD):
            self.store.clock = lambda: p.VALID_FROM + 1
            old_profile = p.input_profile()
            self.l.register_scope(scope, old_profile, p.document_pricing())
            first = self.l.prepare(self.f.selection, old_profile, scope.id, self.f.key(), 'Read the fictional file.')
            spent = self.answer(first, old_profile)
            held = self.l.prepare(self.f.selection, old_profile, scope.id, self.f.key(), 'Read it again.')
            self.l.claim(self.f.selection, held['id'])     # left unsettled: its full reservation stays charged
        self.store.clock = lambda: p.VALID_FROM + 1
        self.assertEqual((spent['chargedUnits'], spent['settled']), (100 * 100 + 80 * 500, True))
        before = self.stored()
        self.l.register_scope(scope, self.profile, self.price)       # the renewed build starting: carried forward
        self.assertEqual(self.stored(), before, 'carrying forward writes nothing')
        self.assertEqual(self.l.status(self.f.selection, first['id'])['chargedUnits'], spent['chargedUnits'])
        self.assertEqual(self.l.status(self.f.selection, held['id'])['chargedUnits'], 1_110_000_000)
        # 3.33 - 0.00005 spent - 1.11 held leaves room for exactly one more 1.11 reservation.
        new = self.l.prepare(self.f.selection, self.profile, scope.id, self.f.key(), 'A renewed question.')
        self.assertEqual(self.l.claim(self.f.selection, new['id'])[0]['chargedUnits'], 1_110_000_000)
        extra = self.l.prepare(self.f.selection, self.profile, scope.id, self.f.key(), 'One question too many.')
        with self.assertRaises(AttachmentError) as raised:
            self.l.claim(self.f.selection, extra['id'])
        self.assertEqual(raised.exception.code, 'document_budget_exhausted')
        # The old held question settles at today's prices, which equal the ones it was reserved under.
        view = self.l.status(self.f.selection, held['id'])
        facts = ledger.ResultFacts(held['id'], view['claimNonce'], view['bodyDigest'], view['profileDigest'],
                                   'accepted', 'a' * 64, 'msg_fictional_held', documents.MODEL, p.BILLING_REVISION, usage(100))
        settled = self.l.record_result(self.f.selection, facts)
        self.assertEqual((settled['settled'], settled['chargedUnits']), (True, p.usage_units(usage(100))))
        config, halted = self.l._scope(scope.id)
        self.assertEqual((config['limit'], halted), (3_330_000_000, False))
        self.assertEqual(self.l.claim(self.f.selection, extra['id'])[0]['state'], 'claimed')

    def test_a_carried_budget_with_another_scope_or_limit_is_still_refused(self):
        """(b) Only the same budget carries forward: another scope id (rotation), revision or limit is refused."""
        self.register_old()
        for scope in (ledger.DocumentBudgetScope('fictional-renewed', 'v1', '2.22'),
                      ledger.DocumentBudgetScope(self.scope.id, 'v1', '4.44'),
                      ledger.DocumentBudgetScope(self.scope.id, 'v2', '2.22')):
            with self.subTest(scope=scope), self.assertRaises(AttachmentError) as raised:
                self.l.register_scope(scope, self.profile, self.price)
            self.assertEqual(raised.exception.code, 'document_budget_conflict')
        self.assertEqual(len(self.stored()), 1)

    def refused_after(self, **old):
        """(c) Only an exact listed predecessor whose terms equal today's carries forward: this one is refused."""
        self.register_old(**old)
        with self.assertRaises(AttachmentError) as raised:
            self.l.register_scope(self.scope, self.profile, self.price)
        self.assertEqual(raised.exception.code, 'document_budget_conflict')
        d = self.f.draft(); self.f.upload(d); self.f.admit(d)
        with self.assertRaises(AttachmentError) as raised:
            self.l.prepare(self.f.selection, self.profile, self.scope.id, self.f.key(), 'Refused.')
        self.assertEqual(raised.exception.code, 'document_budget_conflict')

    def test_an_unlisted_earlier_qualification_is_still_refused(self):
        self.refused_after(EVIDENCE_ID='haiku-pdf-png-2026-11-08', PRICING_REVISION='haiku-tiered-2026-11-08')

    def test_a_listed_evidence_id_with_another_pricing_revision_is_still_refused(self):
        self.refused_after(PRICING_REVISION='haiku-tiered-2026-11-08')

    def test_a_listed_qualification_registered_at_other_prices_is_still_refused(self):
        self.refused_after(HIGH=(500, 2500, 50, 625, 999))

    def test_a_listed_predecessor_whose_terms_differ_from_today_drops_out(self):
        self.assertEqual([pricing.qualification_id for _, pricing in p.predecessor_terms()], ['haiku-pdf-png-2026-11-15'])
        for change in (dict(LOW=(100, 500, 10, 125, 199)), dict(LOW_THRESHOLD=200_000), dict(HIGH=(500, 2500, 50, 625, 999)),
                       dict(PER_CALL_USD='1.12'), dict(MAX_INPUT=900_000), dict(EVIDENCE_ID='haiku-pdf-png-2026-11-15')):
            with self.subTest(change=change), mock.patch.multiple(p, **change):
                self.assertEqual(p.predecessor_terms(), ())

    def test_carrying_forward_again_after_a_restart_is_idempotent(self):
        """(d) Each start of the renewed build registers again; every time the same stored row carries forward."""
        self.register_old()
        before = self.stored()
        for _ in range(3):
            self.l = ledger.OfficerDocumentLedger(self.store)
            self.l.register_scope(self.scope, self.profile, self.price)
            self.assertEqual(self.stored(), before)

    def test_requests_keep_the_qualification_they_were_prepared_under(self):
        """(e) The ledger shows no budget history of its own (no view lists budgets); the trail is each request's
        immutable record, which names the qualification it was prepared under, old or renewed."""
        d = self.f.draft(); self.f.upload(d); self.f.admit(d)
        with mock.patch.multiple(p, **self.OLD):
            self.store.clock = lambda: p.VALID_FROM + 1
            self.l.register_scope(self.scope, p.input_profile(), p.document_pricing())
            old = self.l.prepare(self.f.selection, p.input_profile(), self.scope.id, self.f.key(), 'Before.')
        self.store.clock = lambda: p.VALID_FROM + 1
        self.l.register_scope(self.scope, self.profile, self.price)
        new = self.l.prepare(self.f.selection, self.profile, self.scope.id, self.f.key(), 'After.')
        evidence = {row['id']: json.loads(row['record'])['profile']['strict_budget_evidence']['token_bound_revision']
                    for row in self.store.db.execute('SELECT id,record FROM officer_document_requests')}
        self.assertEqual(evidence, {old['id']: 'haiku-pdf-png-2026-11-15', new['id']: p.EVIDENCE_ID})
        config, _ = self.l._scope(self.scope.id)
        self.assertEqual(config['pricing']['qualification_id'], 'haiku-pdf-png-2026-11-15')   # the stored budget is unchanged

    def test_closed_registration_and_full_both_geo_reservation(self):
        self.l.register_scope(self.scope,self.profile,self.price)
        config,_=self.l._scope(self.scope.id)
        self.assertEqual(config['ceiling'],1_110_000_000)
        for geo in ('global','us'):
            self.assertEqual(self.l._cost(usage(100000,geo),config),p.usage_units(usage(100000,geo)))
        for invalid in (replace(self.price,cache_1h_rate=1),replace(self.price,qualification_id=None)):
            with self.assertRaises(AttachmentError):self.l.register_scope(self.scope,self.profile,invalid)
        bad=replace(self.profile,strict_budget_evidence=replace(self.profile.strict_budget_evidence,usd_ceiling='1.01'))
        with self.assertRaises(AttachmentError):self.l.register_scope(self.scope,bad,self.price)
    def test_expired_registered_policy_cannot_prepare_claim_or_consume(self):
        self.l.register_scope(self.scope,self.profile,self.price)
        d=self.f.draft();self.f.upload(d);self.f.admit(d)
        prepared=self.l.prepare(self.f.selection,self.profile,self.scope.id,self.f.key(),'Read the fictional file.')
        self.store.clock=lambda:p.VALID_UNTIL
        with self.assertRaises(AttachmentError):self.l.prepare(self.f.selection,self.profile,self.scope.id,self.f.key(),'New question')
        with self.assertRaises(AttachmentError):self.l.claim(self.f.selection,prepared['id'])
        self.store.clock=lambda:p.VALID_UNTIL-1
        state,permit,body=self.l.claim(self.f.selection,prepared['id'])
        self.store.clock=lambda:p.VALID_UNTIL
        with self.assertRaises(AttachmentError):
            self.l.consume(permit,self.f.selection,prepared['id'],body.body_digest,state['profileDigest'])
        self.assertEqual(self.l.status(self.f.selection,prepared['id'])['state'],'claimed')
        # Historical policy remains readable for settlement/recovery; no remint.
        self.assertEqual(self.l._scope(self.scope.id)[0]['ceiling'],1_110_000_000)

    def test_original_single_geo_configuration_identity_is_unchanged(self):
        from test_officer_documents import profile
        old=ledger.DocumentPricing('fictional-prices-v1','fictional-billing-v1','global',1000,10000,500,1250,2000)
        self.l.register_scope(ledger.DocumentBudgetScope('fictional-old','v1','1'),profile(),old)
        config,_=self.l._scope('fictional-old')
        self.assertNotIn('qualification_id',config['pricing'])


if __name__=='__main__':unittest.main()
