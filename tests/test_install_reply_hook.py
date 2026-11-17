"""Owner installer uses private temporary HOME only; never native hooks or trust state."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import install_reply_hook as installer


@unittest.skipIf(os.name=='nt','POSIX private user hook installation')
class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.home=Path(self.tmp.name).resolve()
        self.state=self.home/'.live-room';self.state.mkdir(mode=0o700)
        self.store=self.state/'reply-hook';self.store.mkdir(mode=0o700)
        self.paths={'codex':self.home/'.codex/hooks.json','claude':self.home/'.claude/settings.json'}
        self.original={}
        for provider,path in self.paths.items():
            path.parent.mkdir(mode=0o700)
            raw=json.dumps({'unrelated':provider,'hooks':{'PermissionRequest':[{'hooks':[{'type':'command','command':'fictional existing permission'}]}],
                 'PostToolUse':[{'matcher':'^other_tool$','hooks':[{'type':'command','command':'fictional other observer'}]}]}}).encode()
            path.write_bytes(raw);path.chmod(0o600);self.original[provider]=json.loads(raw)
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
        import peer_reply_delivery
        self.backend=peer_reply_delivery
        self.credential=str(self.state/'fictional-credential.json')
        Path(self.credential).write_text('{}')
        db=sqlite3.connect(self.state/'officer-messages.sqlite3')
        self.backend.create_schema(db)
        db.execute('INSERT INTO peer_reply_hook_credentials VALUES (?,?,1,?,?)',('fictional-id','fictional-secret','["codex","claude"]',1))
        db.commit();db.close();(self.state/'officer-messages.sqlite3').chmod(0o600)
    def seed_legacy(self):
        for provider,path in self.paths.items():
            owned={'matcher':'^'+installer.hook.TOOLS[provider]+'$','hooks':[{'type':'command','command':'fictional legacy hook #'+installer.MARK}]}
            value=json.loads(path.read_bytes());value['hooks']['PostToolUse'].append(owned);path.write_text(json.dumps(value))
            (self.store/('install-'+provider+'.json')).write_text(json.dumps(dict(version=1,provider=provider,path=str(path),owned=owned,enabled=True)))
        (self.store/'watch_reply_hook.py').write_text('# fictional legacy helper')
    def tearDown(self):self.tmp.cleanup()
    def main(self,*extra):
        args=['install_reply_hook.py','--provider','both',*extra]
        with patch.object(sys,'argv',args),patch.object(installer.Path,'home',return_value=self.home),contextlib.redirect_stdout(io.StringIO()):installer.main()
    def test_install_setup_and_custom_credential_rejected_before_any_write(self):
        before={str(p):p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        for args in [('--setup','--accept-native-parent-return'),('--credential-file',self.credential,'--accept-native-parent-return'),('--remove','--setup')]:
            with self.assertRaisesRegex(ValueError,'removed'):self.main(*args)
        with self.assertRaisesRegex(ValueError,'removed'):
            installer.configure('codex',self.home/'absent/hooks.json',self.home/'absent-store',self.credential,accepted=True)
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.home.rglob('*') if p.is_file()})
        self.assertFalse((self.home/'absent').exists());self.assertFalse((self.home/'absent-store').exists())

    def test_remove_is_idempotent_and_preserves_unrelated_hooks(self):
        self.seed_legacy();self.main('--remove');self.main('--remove')
        for provider,path in self.paths.items():self.assertEqual(json.loads(path.read_bytes()),self.original[provider])
        self.assertFalse((self.home/'.codex/config.toml').exists())

    def test_offline_remove_revoke_is_atomic_with_configuration_failure(self):
        self.seed_legacy()
        tracked=list(self.paths.values())+[self.store/'install-codex.json',self.store/'install-claude.json',self.store/'watch_reply_hook.py']
        before={p:p.read_bytes() for p in tracked};configure=installer.configure
        def fail_second(provider,*args,**kwargs):
            if provider=='claude':raise ValueError('fictional removal failure')
            return configure(provider,*args,**kwargs)
        with patch.object(installer,'configure',side_effect=fail_second):
            with self.assertRaises(ValueError):self.main('--remove','--revoke')
        self.assertEqual(before,{p:p.read_bytes() for p in tracked})
        db=sqlite3.connect(self.state/'officer-messages.sqlite3')
        try:self.assertEqual(db.execute('SELECT COUNT(*) FROM peer_reply_hook_credentials WHERE active=1').fetchone()[0],1)
        finally:db.close()
        self.main('--remove','--revoke')
        db=sqlite3.connect(self.state/'officer-messages.sqlite3')
        try:self.assertEqual(db.execute('SELECT COUNT(*) FROM peer_reply_hook_credentials WHERE active=1').fetchone()[0],0)
        finally:db.close()
        for provider,path in self.paths.items():self.assertEqual(json.loads(path.read_bytes()),self.original[provider])

    def test_post_callback_commit_failure_restores_setup_and_revoke(self):
        self.seed_legacy()
        tracked=list(self.paths.values())+[self.store/'install-codex.json',self.store/'install-claude.json',self.store/'watch_reply_hook.py']
        before={p:p.read_bytes() for p in tracked};transaction=self.backend.p.transaction;reached=[]
        @contextlib.contextmanager
        def failed_commit(db,*args,**kwargs):
            with transaction(db,*args,**kwargs):
                yield
                if any(p.read_bytes()!=before[p] for p in self.paths.values()):
                    reached.append(True)
                    raise OSError('fictional final commit failure after install callback')
        for operation in [('--remove','--revoke')]:
            with patch.object(self.backend.p,'transaction',failed_commit):
                with self.assertRaises(OSError):self.main(*operation)
            self.assertEqual(before,{p:p.read_bytes() for p in tracked})
            db=sqlite3.connect(self.state/'officer-messages.sqlite3')
            try:self.assertEqual(db.execute('SELECT id FROM peer_reply_hook_credentials WHERE active=1').fetchall(),[('fictional-id',)])
            finally:db.close()
        self.assertEqual(reached,[True],'failure must occur after actual config replacement')

    def test_concurrent_edit_symlink_and_private_backup_guards(self):
        self.seed_legacy()
        replace=installer.replace;target=self.paths['codex'];replacement=b'{"unrelated":"concurrent owner edit"}'
        def edit(path,*args):
            if Path(path)==target:target.write_bytes(replacement)
            return replace(path,*args)
        with patch.object(installer,'replace',side_effect=edit):
            with self.assertRaises(ValueError):self.main('--remove')
        self.assertEqual(target.read_bytes(),replacement)
        target.unlink();victim=self.home/'victim';victim.write_text('unchanged');target.symlink_to(victim)
        with self.assertRaises(OSError):self.main('--remove')
        self.assertEqual(victim.read_text(),'unchanged')
        for path in self.store.glob('backup-*.json'):self.assertEqual(path.stat().st_mode&0o777,0o600)


if __name__=='__main__':unittest.main()
