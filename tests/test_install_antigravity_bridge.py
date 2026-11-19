#!/usr/bin/env python3
"""Antigravity outbound plugin registration in fixture homes only."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import install_antigravity_bridge as installer


@unittest.skipUnless(installer.SUPPORTED, 'macOS and Linux private plugin installation')
class InstallTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        self.config = self.home / '.gemini' / 'config'
        self.plugin = self.config / 'plugins' / installer.PLUGIN
        self.repo = self.home / 'checkout with "quotes" $(literal)'
        self.adapter = self.repo / 'integrations' / 'claude' / 'peer.mjs'
        for directory in (self.config.parent, self.config, self.repo, self.repo / 'integrations', self.adapter.parent):
            directory.mkdir(mode=0o755)
        self.write_launch_file(self.adapter, '// fixture adapter\n')
        self.node = self.home / 'node with spaces ; literal'
        self.node.write_text('#!/bin/sh\nexit 42\n')
        self.node.chmod(0o700)
        self.state = self.home / 'motet fixture'
        self.state.mkdir(mode=0o700)
        credential_dir = self.state / 'credential files'
        credential_dir.mkdir(mode=0o700)
        self.credential = credential_dir / 'bridge "fixture".json'
        self.secret = 'c' * 64
        self.credential.write_text(json.dumps({'version': 2, 'credential_id': str(uuid.uuid4()),
                                               'secret': self.secret, 'port': 8793}))
        self.credential.chmod(0o600)

    def write_launch_file(self, path, text):
        path.write_text(text)
        path.chmod(0o644)

    def run_cli(self, action, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = installer.main([action, '--home', str(self.home), *map(str, args)])
        result = output.getvalue()
        self.assertNotIn(self.secret, result)
        self.assertNotIn(str(self.credential), result)
        self.last_result = json.loads(result)
        return code, self.last_result['state']

    def install(self, credential=None, node=None):
        return self.run_cli('install', '--credential', credential or self.credential,
                            '--node', node or self.node, '--repo', self.repo)

    def interrupt_rename(self, action, when):
        code = '''
import os
import signal
import sys
import install_antigravity_bridge as installer

rename = installer.rename_exclusive
phase = sys.argv[1]

def interrupt(*args):
    if phase == 'after':
        rename(*args)
    os.kill(os.getpid(), signal.SIGKILL)

installer.rename_exclusive = interrupt
sys.exit(installer.main(sys.argv[2:]))
'''
        args = [action, '--home', str(self.home)]
        if action == 'install':
            args.extend(['--credential', str(self.credential), '--node', str(self.node), '--repo', str(self.repo)])
        result = subprocess.run([sys.executable, '-c', code, when, *args],
                                cwd=installer.ROOT / 'scripts', capture_output=True, timeout=10)
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stdout + result.stderr)

    def test_register_idempotently_and_remove_only_managed_files(self):
        others = {self.config / 'config.json': b'{"plugins":{"example":{"enabled":false}}}',
                  self.config / 'mcp_config.json': b'{"mcpServers":{"example":{}}}',
                  self.plugin.parent / 'neighbor' / 'plugin.json': b'{"name":"neighbor"}'}
        self.plugin.parent.mkdir(mode=0o700)
        for path, raw in others.items():
            path.parent.mkdir(mode=0o755, exist_ok=True)
            path.write_bytes(raw)
        original = self.credential.read_bytes()
        self.assertEqual(self.run_cli('status'), (0, 'absent'))
        self.assertEqual(self.install(), (0, 'registered'))
        manifest = json.loads((self.plugin / 'plugin.json').read_text())
        self.assertEqual(manifest['name'], installer.PLUGIN)
        self.assertFalse(manifest.get('disabled', False))
        config = self.plugin / 'mcp_config.json'
        settings = json.loads(config.read_text())['mcpServers'][installer.PLUGIN]
        self.assertEqual(settings, {'command': str(self.node),
                                    'args': [str(self.adapter), '--credential', str(self.credential)],
                                    'cwd': str(self.adapter.parent)})
        self.assertNotIn(self.secret, config.read_text())
        self.assertEqual(self.run_cli('status'), (0, 'registered'))
        inode = config.stat().st_ino
        self.assertEqual(self.install(), (0, 'registered'))
        self.assertEqual(config.stat().st_ino, inode)
        for directory in (self.plugin, self.plugin / 'rules'):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for path in self.plugin.rglob('*'):
            if path.is_file():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(self.run_cli('remove'), (0, 'absent'))
        self.assertEqual(self.run_cli('remove'), (0, 'absent'))
        self.assertFalse(self.plugin.exists())
        self.assertEqual(self.credential.read_bytes(), original)
        self.assertEqual({path: path.read_bytes() for path in others}, others)

    def test_install_refuses_existing_unmanaged_plugin(self):
        self.plugin.parent.mkdir(mode=0o700)
        self.plugin.mkdir(mode=0o700)
        unrelated = self.plugin / 'readme.txt'
        unrelated.write_bytes(b'foreign')
        for cli in (self.install, lambda: self.run_cli('remove')):
            self.assertEqual(cli(), (1, 'unavailable'))
        self.assertEqual(unrelated.read_bytes(), b'foreign')
        self.assertEqual(list(self.plugin.iterdir()), [unrelated])

    def test_optional_inbox_receiver_keeps_only_private_capability_path(self):
        inbox = self.state / 'antigravity-inbox.json'
        receiver_key = 'd' * 64
        inbox.write_text(json.dumps(dict(version=1, inbox_id=str(uuid.uuid4()),
                                        label='Antigravity inbox', receiver_key=receiver_key)))
        inbox.chmod(0o600)
        self.write_launch_file(self.adapter.parent / 'inbox.mjs', '// fixture receiver\n')
        self.write_launch_file(self.adapter.parent / 'bridge-capabilities.json', '{"shared_pull_inbox_receiver":1}')
        args = ('--credential', self.credential, '--node', self.node, '--repo', self.repo,
                '--inbox-credential', inbox)
        self.assertEqual(self.run_cli('install', *args), (0, 'registered'))
        config = json.loads((self.plugin / 'mcp_config.json').read_text())
        self.assertEqual(config['mcpServers']['motet-bridge']['args'][-2:],
                         ['--inbox-credential', str(inbox)])
        for file in self.plugin.rglob('*'):
            if file.is_file():
                self.assertNotIn(receiver_key, file.read_text())
        self.assertEqual(self.run_cli('remove'), (0, 'absent'))
        self.assertTrue(inbox.exists())
        inbox.chmod(0o644)
        self.assertEqual(self.run_cli('install', *args), (1, 'unavailable'))
        self.assertNotIn(str(inbox), json.dumps(self.last_result))
        self.assertNotIn(receiver_key, json.dumps(self.last_result))
        self.assertFalse(self.plugin.exists())

    def test_receiver_refuses_outbound_only_checkout_before_registration(self):
        inbox = self.state / 'antigravity-inbox.json'
        inbox.write_text(json.dumps(dict(version=1, inbox_id=str(uuid.uuid4()),
                                        label='Antigravity inbox', receiver_key='e' * 64)))
        inbox.chmod(0o600)
        args = ('--credential', self.credential, '--node', self.node, '--repo', self.repo,
                '--inbox-credential', inbox)
        self.assertEqual(self.run_cli('install', *args), (1, 'unavailable'))
        self.assertFalse(self.plugin.exists())
        self.write_launch_file(self.adapter.parent / 'inbox.mjs', '// receiver fixture\n')
        self.assertEqual(self.run_cli('install', *args), (1, 'unavailable'))
        self.assertFalse(self.plugin.exists())
        self.assertEqual(self.install(), (0, 'registered'))

    def test_symlink_target_and_ancestors_are_preserved(self):
        outside = self.home / 'destination'
        outside.mkdir(mode=0o700)
        self.plugin.parent.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.install(), (1, 'unavailable'))
        self.assertFalse(list(outside.iterdir()))
        self.plugin.parent.unlink()
        self.plugin.parent.mkdir(mode=0o700)
        self.plugin.symlink_to(outside, target_is_directory=True)
        for cli in (self.install, lambda: self.run_cli('remove')):
            self.assertEqual(cli(), (1, 'unavailable'))
        self.assertTrue(self.plugin.is_symlink())
        self.assertFalse(list(outside.iterdir()))

    def test_refuses_changed_content_foreign_entries_and_hardlinks(self):
        self.assertEqual(self.install(), (0, 'registered'))
        manifest = self.plugin / 'plugin.json'
        config = self.plugin / 'mcp_config.json'
        original = config.read_bytes()
        config.write_bytes(original + b'\n')
        self.assertEqual(self.run_cli('status'), (1, 'unavailable'))
        self.assertEqual(self.run_cli('remove'), (1, 'unavailable'))
        self.assertTrue(manifest.exists())
        config.write_bytes(original)
        foreign = self.plugin / 'rules' / 'unrelated.md'
        foreign.write_text('unrelated')
        self.assertEqual(self.install(), (1, 'unavailable'))
        self.assertEqual(self.run_cli('remove'), (1, 'unavailable'))
        self.assertEqual(foreign.read_text(), 'unrelated')
        foreign.unlink()
        linked = self.home / 'hardlink'
        os.link(config, linked)
        self.assertEqual(self.run_cli('remove'), (1, 'unavailable'))
        self.assertEqual(linked.read_bytes(), original)
        self.assertTrue(manifest.exists())

    def test_new_configuration_requires_explicit_remove(self):
        self.assertEqual(self.install(), (0, 'registered'))
        new_node = self.home / 'node-2'
        new_node.write_bytes(self.node.read_bytes())
        new_node.chmod(0o700)
        original = (self.plugin / 'mcp_config.json').read_bytes()
        self.assertEqual(self.install(node=new_node), (1, 'unavailable'))
        self.assertEqual((self.plugin / 'mcp_config.json').read_bytes(), original)

    def test_unsafe_credentials_and_non_executable_node_leave_home_unchanged(self):
        link = self.credential.with_suffix('.link')
        link.symlink_to(self.credential)
        self.assertEqual(self.install(credential=link), (1, 'unavailable'))
        self.credential.chmod(0o400)
        self.assertEqual(self.install(), (1, 'unavailable'))
        self.credential.chmod(0o600)
        self.node.chmod(0o600)
        self.assertEqual(self.install(), (1, 'unavailable'))
        self.node.chmod(0o700)
        self.credential.write_text(json.dumps({'version': 1, 'endpoint_id': str(uuid.uuid4()),
                                               'secret': self.secret, 'port': 8793, 'project': str(self.repo)}))
        self.assertEqual(self.install(), (1, 'unavailable'))
        self.assertFalse(self.plugin.parent.exists())

    def test_refuses_untrusted_node_and_adapter_paths_before_writing(self):
        tool_dir = self.home / 'node installation'
        tool_dir.mkdir(mode=0o700)
        moved_node = tool_dir / 'node'
        self.node.rename(moved_node)
        self.node.symlink_to(moved_node)
        modules = self.adapter.parent / 'node_modules'
        modules.mkdir(mode=0o755)
        for target in (moved_node, tool_dir, self.adapter, self.adapter.parent, self.repo, modules):
            with self.subTest(name=target.name):
                mode = stat.S_IMODE(target.stat().st_mode)
                target.chmod(mode | 0o020)
                try:
                    self.assertEqual(self.install(), (1, 'unavailable'))
                    self.assertIn(str(target), self.last_result['message'])
                    self.assertFalse(self.plugin.parent.exists())
                finally:
                    target.chmod(mode)
        self.assertEqual(self.install(), (0, 'registered'))

    def test_registration_reports_the_refused_antigravity_config_directory(self):
        self.config.chmod(0o775)
        self.assertEqual(self.install(), (1, 'unavailable'))
        self.assertIn(str(self.config), self.last_result['message'])
        self.assertFalse(self.plugin.parent.exists())

    def test_registration_checks_sdk_root_without_recursing_into_dist(self):
        modules = self.adapter.parent / 'node_modules'
        scope = modules / '@modelcontextprotocol'
        sdk = scope / 'sdk'
        dist = sdk / 'dist'
        for directory in (modules, scope, sdk, dist):
            directory.mkdir(mode=0o755)
        self.write_launch_file(sdk / 'package.json', '{}')
        dist.chmod(0o777)
        self.assertEqual(self.install(), (0, 'registered'))
        self.assertEqual(stat.S_IMODE(dist.stat().st_mode), 0o777)
        self.assertEqual(self.run_cli('remove'), (0, 'absent'))
        sdk.chmod(0o775)
        self.assertEqual(self.install(), (1, 'unavailable'))
        self.assertIn(str(sdk), self.last_result['message'])
        self.assertFalse(self.plugin.exists())

    def test_keeps_trusted_stable_node_link_after_retarget(self):
        old = self.home / 'node old version'
        new = self.home / 'node new version'
        self.node.rename(old)
        new.write_text('#!/bin/sh\nexit 0\n')
        new.chmod(0o700)
        self.node.symlink_to(old.name)
        self.assertEqual(self.install(), (0, 'registered'))
        command = json.loads((self.plugin / 'mcp_config.json').read_text())['mcpServers'][installer.PLUGIN]['command']
        self.assertEqual(command, str(self.node))
        self.node.unlink()
        self.node.symlink_to(new.name)
        old.unlink()
        self.assertEqual(subprocess.run([command], timeout=5).returncode, 0)
        self.assertEqual(self.install(), (0, 'registered'))

    def test_accepts_root_managed_executable_path(self):
        executable = Path('/usr/bin/true')
        self.assertTrue(executable.exists())
        self.assertEqual(self.install(node=executable), (0, 'registered'))

    def test_failed_install_cleans_up_created_assets_and_allows_retry(self):
        original = installer.write_asset

        def fail_at_manifest(directory, name, raw):
            if name == 'plugin.json':
                raise OSError('fixture write failure')
            original(directory, name, raw)

        with patch.object(installer, 'write_asset', side_effect=fail_at_manifest):
            self.assertEqual(self.install(), (1, 'unavailable'))
        self.assertFalse(self.plugin.exists())
        self.assertEqual(self.install(), (0, 'registered'))

    def test_failed_removal_rename_preserves_managed_files_and_allows_retry(self):
        self.assertEqual(self.install(), (0, 'registered'))
        with patch.object(installer, 'rename_exclusive', side_effect=OSError('fixture rename failure')):
            self.assertEqual(self.run_cli('remove'), (1, 'unavailable'))
        self.assertEqual(self.run_cli('status'), (0, 'registered'))
        self.assertEqual(self.run_cli('remove'), (0, 'absent'))

    def test_publish_refuses_target_created_during_staging(self):
        rename = installer.rename_exclusive

        def make_foreign_directory(source_dir, source, target_dir, target):
            self.plugin.mkdir(mode=0o700)
            foreign_inode = self.plugin.stat().st_ino
            try:
                rename(source_dir, source, target_dir, target)
            finally:
                self.assertEqual(self.plugin.stat().st_ino, foreign_inode)

        with patch.object(installer, 'rename_exclusive', side_effect=make_foreign_directory):
            self.assertEqual(self.install(), (1, 'unavailable'))
        self.assertEqual(list(self.plugin.parent.iterdir()), [self.plugin])
        self.assertEqual(list(self.plugin.iterdir()), [])

    def test_sigkill_at_install_publication_boundary_is_retryable(self):
        self.interrupt_rename('install', 'before')
        self.assertEqual(self.run_cli('status'), (0, 'absent'))
        self.assertEqual(list(self.plugin.parent.glob('*/plugin.json')), [])
        self.interrupt_rename('install', 'after')
        self.assertEqual(self.run_cli('status'), (0, 'registered'))
        self.assertEqual(self.install(), (0, 'registered'))

    def test_sigkill_at_remove_publication_boundary_is_retryable(self):
        self.assertEqual(self.install(), (0, 'registered'))
        self.interrupt_rename('remove', 'before')
        self.assertEqual(self.run_cli('status'), (0, 'registered'))
        self.interrupt_rename('remove', 'after')
        self.assertEqual(self.run_cli('status'), (0, 'absent'))
        self.assertEqual(list(self.plugin.parent.glob('*/plugin.json')), [])
        self.assertEqual(self.run_cli('remove'), (0, 'absent'))
        self.assertEqual(self.install(), (0, 'registered'))


if __name__ == '__main__':
    unittest.main()
