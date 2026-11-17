"""Dated immutable qualification, exact tier/default billing; no network."""
from dataclasses import replace
from decimal import localcontext
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

    @unittest.skipUnless(os.name=='posix','Private allowance requires POSIX.')
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


@unittest.skipUnless(os.name=='posix','Private store requires POSIX.')
class QualifiedLedgerTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DirectStoreTests();self.f.setUp();self.store=self.f.store
        self.store.clock=lambda:p.VALID_FROM+1
        self.f.binding['modelId']=documents.MODEL
        self.l=ledger.OfficerDocumentLedger(self.store)
        self.profile=p.input_profile();self.price=p.document_pricing()
        self.scope=ledger.DocumentBudgetScope('fictional-qualified','v1','2.22')
    def tearDown(self):self.f.tearDown()
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
