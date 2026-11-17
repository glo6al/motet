"""Nonce delivery seam without importing or starting the application server."""
import ast
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import unittest


class EmbedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'web').mkdir()
        self.html = self.root / 'web' / 'index.html'
        self.pending = self.root / 'web' / 'officer-document-pending.js'
        self.client = self.root / 'web' / 'officer-documents-client.js'
        self.html.write_text('<html><script>const app = true;</script></html>')
        self.pending.write_text('function createOfficerDocumentPending() { return {}; }')
        self.client.write_text('function createOfficerDocumentsClient() { return {}; }')
        source = Path(__file__).resolve().parents[1] / 'src' / 'server.py'
        module = ast.parse(source.read_text())
        method = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == 'page_with_nonce')
        scope = {'os': os, 'secrets': secrets, 'HERE': str(self.root), 'UI_CSP': "script-src 'nonce-%s'"}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), scope)
        self.render = scope['page_with_nonce']

    def tearDown(self):
        self.temp.cleanup()

    def test_one_nonce_covers_both_fixed_modules_and_page_with_fresh_response(self):
        first, policy = self.render()
        self.assertEqual(first.count(b'<script nonce='), 1)
        self.assertEqual(first.count(b'</script>'), 1)
        nonce = first.split(b'nonce="')[1].split(b'"')[0].decode()
        expected = (b'<script nonce="%s">\n' % nonce.encode() + self.pending.read_bytes() + b'\n;\n' +
                    self.client.read_bytes() + b'\n;\n')
        self.assertEqual(first, self.html.read_bytes().replace(b'<script>', expected))
        self.assertIn(nonce, policy)
        self.assertNotEqual(first, self.render()[0])

    def test_invalid_module_or_page_fails_closed(self):
        for module in (self.pending, self.client):
            valid = module.read_bytes()
            for invalid in (b'// </script>', b'// </ScRiPt>', b'x' * 200001):
                with self.subTest(module=module.name, size=len(invalid)):
                    module.write_bytes(invalid)
                    with self.assertRaises(RuntimeError):
                        self.render()
            module.write_bytes(valid)
        self.html.write_text('<script></script><script></script>')
        with self.assertRaises(RuntimeError): self.render()

    def test_native_agent_modules_are_opt_in_under_the_same_nonce(self):
        names = ('journal', 'client', 'page')
        modules = [self.html.parent / f'native-agent-attachments-{name}.js' for name in names]
        for name, module in zip(names, modules):
            module.write_text(f'// native {name} sentinel')
        default_page, _ = self.render()
        native_page, policy = self.render(native_agent_modules=True)
        self.assertEqual(native_page.count(b'<script nonce='), 1)
        nonce = native_page.split(b'nonce="')[1].split(b'"')[0].decode()
        self.assertIn(nonce, policy)
        markers = [module.read_bytes() for module in modules]
        self.assertTrue(all(marker not in default_page for marker in markers))
        self.assertIn(b'\n;\n'.join(markers), native_page)
        modules[-1].write_bytes(b'// </script>')
        with self.assertRaises(RuntimeError): self.render(native_agent_modules=True)
        self.assertNotIn(markers[-1], self.render()[0])

    def test_saved_model_discovery_never_loads_catalogue_or_changes_selection(self):
        source = Path(__file__).resolve().parents[1] / 'src' / 'server.py'
        module = ast.parse(source.read_text())
        handler = next(n for n in module.body if isinstance(n, ast.ClassDef) and n.name == 'H')
        method = next(n for n in handler.body if isinstance(n, ast.FunctionDef) and n.name == 'officer_models_http')
        def forbidden_call(*args): raise AssertionError('No catalogue or selection mutation permitted')
        scope = {'json': json, 'OFFICER_LOCK': threading.RLock(), 'OFFICER_MODEL': 'claude-haiku-5-5',
                 'OFFICER_MODEL_ERROR': '', 'ATTACHMENTS_ENABLED': False, 'OFFICER_DOCUMENTS_ENABLED': False,
                 'officer_catalog': forbidden_call, 'select_officer_model': forbidden_call}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), scope)
        class Request:
            path = '/officer/model-selection'
            headers = {'X-Live-Room-Key': 'fictional-owner'}
            allowed = True
            def host_ok(self): return self.allowed
            def keyed(self, key): return self.allowed and key == 'fictional-owner'
            def send(self, code, body, kind=None, extra=()): return code, body, extra
        req = Request()
        code, body, extra = scope['officer_models_http'](req, 'GET')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body), {'schemaVersion': 1, 'selected_model': 'claude-haiku-5-5',
                                          'selection_available': True, 'attachments_enabled': False, 'documents_enabled': False})
        self.assertIn(('Cache-Control', 'no-store'), extra)
        req.allowed = False
        self.assertEqual(scope['officer_models_http'](req, 'GET')[0], 403)
        self.assertEqual(scope['OFFICER_MODEL'], 'claude-haiku-5-5')


if __name__ == '__main__': unittest.main()
