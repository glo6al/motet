"""What the page is told about files: on only when the store started, with plain words when it is off.

Covers the reported state and reason (store started, store failed, Windows), the refusal code every attachment
request gets while the store is off, the host flag that keeps native delivery off on Windows, and that the attachment
modules import where fcntl does not exist. Never starts WATCH or contacts a provider.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import server
import private_storage
from attachments_types import AttachmentError

sys.path.insert(0, str(ROOT / 'tests'))
from attachment_host import POSIX, POSIX_WHY, STORE, STORE_WHY


def private_live_room(home):
    """``~/.live-room`` for a test home, private to this user: mode 0700 on POSIX; on Windows a protected access list
    owned by this user (an elevated runner's new folders can otherwise belong to Administrators)."""
    path = home / '.live-room'
    if os.name == 'nt':
        private_storage.make_private_folder(str(path))
    else:
        path.mkdir(mode=0o700)
    return path

KEY = 'fictional-owner-key-for-availability-tests'
WINDOWS = 'Sending files is not available on Windows yet. Ordinary messages still work.'
STORAGE = ('Sending files is unavailable because Motet could not open its private file storage. '
           'Restart Motet to try again. Ordinary messages still work.')


class Request:
    """Just enough of the request handler for officer_models_http and attachments_http."""
    path = '/officer/model-selection'
    headers = {'X-Live-Room-Key': KEY}

    def host_ok(self):
        return True

    def keyed(self, key):
        return key == KEY

    def send(self, code, body, kind=None, extra=()):
        return code, json.loads(body)


def model_selection(demo='0'):
    with mock.patch.object(server, 'OFFICER_MODEL', 'claude-haiku-5-5'), mock.patch.object(server, 'OFFICER_MODEL_ERROR', ''), \
            mock.patch.dict(os.environ, {'WATCH_DEMO': demo}):
        code, value = server.H.officer_models_http(Request(), 'GET')
    assert code == 200
    return value


class ReportedStateTests(unittest.TestCase):
    def test_store_running_reports_on_without_a_reason(self):
        plain, documents = mock.Mock(spec=['store']), mock.Mock(spec=['store', 'documents'])
        for service, reading in ((plain, False), (documents, True)):
            with mock.patch.object(server, 'ATTACHMENTS', service), mock.patch.object(server, 'ATTACHMENTS_OFF', None):
                value = model_selection()
            self.assertEqual(set(value), {'schemaVersion', 'selected_model', 'selection_available', 'attachments_enabled',
                                          'documents_enabled'}, 'a running store answers exactly as before')
            self.assertIs(value['attachments_enabled'], True)
            self.assertIs(value['documents_enabled'], reading, 'Motet reads files only once its policy loaded')
        with mock.patch.object(server, 'ATTACHMENTS', documents), mock.patch.object(server, 'OFFICER_DOCUMENTS_ENABLED', False):
            self.assertEqual(server.attachment_availability(), (True, False, None))

    def test_store_off_reports_off_with_plain_words(self):
        for off, words in (('attachments_windows_unavailable', WINDOWS), ('attachments_storage_unavailable', STORAGE),
                           ('attachments_disabled', 'Sending files is turned off in this Motet. Ordinary messages still work.'),
                           (None, 'Sending files is turned off in this Motet. Ordinary messages still work.')):
            with mock.patch.object(server, 'ATTACHMENTS', None), mock.patch.object(server, 'ATTACHMENTS_OFF', off):
                value = model_selection()
            self.assertIs(value['attachments_enabled'], False)
            self.assertIs(value['documents_enabled'], False)
            self.assertEqual(value['attachments_reason'], words)
        # A store left over while the rollout is off is still reported off.
        with mock.patch.object(server, 'ATTACHMENTS', mock.Mock(spec=['store', 'documents'])), \
                mock.patch.object(server, 'ATTACHMENTS_ENABLED', False), mock.patch.object(server, 'ATTACHMENTS_OFF', None):
            self.assertEqual(server.attachment_availability()[:2], (False, False))

    def test_every_refusal_is_plain_bounded_text_the_page_accepts(self):
        for words in server.ATTACHMENTS_OFF_WHY.values():
            self.assertTrue(0 < len(words.strip()) <= 240)
            self.assertNotRegex(words, r'[/\\~]', 'no paths or builder words in the reason')


@unittest.skipUnless(STORE, STORE_WHY)
class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(os.path.realpath(self.temp.name))
        self.patches = [mock.patch.object(server, 'HOME', str(self.home)), mock.patch.object(server, 'KEY', KEY),
                        mock.patch.object(server, 'ATTACHMENTS_ENABLED', True),
                        mock.patch.object(server, 'OFFICER_DOCUMENTS_ENABLED', True),
                        mock.patch.dict(os.environ, {'WATCH_DEMO': '0'}),
                        mock.patch('officer_document_transport.DirectDocumentTransport',
                                   side_effect=AssertionError('network constructed'))]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(setattr, server, 'ATTACHMENTS_OFF', 'attachments_disabled')

    def start(self):
        service = server.start_attachments()
        if service is not None:
            self.addCleanup(service.close)
        return service

    def test_started_store_reports_on_and_reading_off_without_its_policy(self):
        private_live_room(self.home)
        service = self.start()
        self.assertIsNotNone(service)
        self.assertIsNone(server.ATTACHMENTS_OFF)
        with mock.patch.object(server, 'ATTACHMENTS', service):
            value = model_selection()
        self.assertEqual((value['attachments_enabled'], value['documents_enabled'], value.get('attachments_reason')),
                         (True, False, None))
        self.assertNotIn('attachments_reason', value)

    def test_reading_turns_off_when_the_policy_is_refused_at_start(self):
        # Kept on purpose: with the store running but the reading policy missing, unreadable or out of date, the page is
        # told documents_enabled is false (2a2d902 said true), so a staged file question gets the "current setting" line.
        private_live_room(self.home)
        expired = AttachmentError('policy_unavailable', 'Fictional expired policy.', 503)
        with mock.patch('officer_document_policy.load_allowance', side_effect=expired) as load:
            service = self.start()
        load.assert_called_once()
        self.assertIsNotNone(service)
        with mock.patch.object(server, 'ATTACHMENTS', service):
            value = model_selection()
        self.assertEqual(value, {'schemaVersion': 1, 'selected_model': 'claude-haiku-5-5', 'selection_available': True,
                                 'attachments_enabled': True, 'documents_enabled': False})

    @unittest.skipUnless(POSIX, POSIX_WHY)
    def test_failed_store_on_a_mac_or_linux_reports_the_storage_reason(self):
        (self.home / '.live-room').mkdir(mode=0o755)        # not private, so the store refuses it
        with mock.patch.object(server, 'WIN', False):
            self.assertIsNone(self.start())
        self.assertEqual(server.ATTACHMENTS_OFF, 'attachments_storage_unavailable')
        with mock.patch.object(server, 'ATTACHMENTS', None):
            self.assertEqual(model_selection()['attachments_reason'], STORAGE)

    def test_windows_store_refusal_reports_the_windows_reason(self):
        private_live_room(self.home)
        # The refusal a Windows meets in PrivateFiles when the walk's system calls did not load there.
        with mock.patch.object(server, 'WIN', True), mock.patch.object(private_storage, 'folder_walk_available',
                                                                       return_value=False):
            self.assertIsNone(self.start())
        self.assertEqual(server.ATTACHMENTS_OFF, 'attachments_windows_unavailable')
        with mock.patch.object(server, 'ATTACHMENTS', None):
            value = model_selection()
        self.assertEqual((value['attachments_enabled'], value['documents_enabled'], value['attachments_reason']),
                         (False, False, WINDOWS))

    def test_other_windows_failures_are_not_called_unported(self):
        failure = AttachmentError('storage_unavailable', 'Fictional storage failure.', 503)
        with mock.patch.object(server, 'WIN', True), mock.patch('attachments_store.AttachmentStore', side_effect=failure):
            self.assertIsNone(self.start())
        self.assertEqual(server.ATTACHMENTS_OFF, 'attachments_storage_unavailable')

    def test_rollout_off_and_demo_report_turned_off(self):
        for enabled, demo in ((False, '0'), (True, '1')):
            with mock.patch.object(server, 'ATTACHMENTS_ENABLED', enabled), mock.patch.dict(os.environ, {'WATCH_DEMO': demo}):
                self.assertIsNone(self.start())
            self.assertEqual(server.ATTACHMENTS_OFF, 'attachments_disabled')


class DemoTests(unittest.TestCase):
    """The demo feeds screenshots, so it reports and refuses exactly as 2a2d902 did."""

    def test_demo_reports_the_rollout_switches_as_before(self):
        for off in ('attachments_disabled', 'attachments_windows_unavailable', 'attachments_storage_unavailable', None):
            with mock.patch.object(server, 'ATTACHMENTS', None), mock.patch.object(server, 'ATTACHMENTS_OFF', off):
                self.assertEqual(model_selection(demo='1'), {'schemaVersion': 1, 'selected_model': 'claude-haiku-5-5',
                                                             'selection_available': True, 'attachments_enabled': True,
                                                             'documents_enabled': True})
        with mock.patch.object(server, 'ATTACHMENTS', None), mock.patch.object(server, 'OFFICER_DOCUMENTS_ENABLED', False):
            value = model_selection(demo='1')
        self.assertEqual((value['attachments_enabled'], value['documents_enabled']), (True, False))
        self.assertNotIn('attachments_reason', value)

    def test_demo_requests_get_the_old_refusal(self):
        seen = []
        def handle(handler, service, key, port, **options):
            seen.append(options)
        with mock.patch.object(server, 'ATTACHMENTS', None), mock.patch.object(server, 'ATTACHMENTS_OFF', 'attachments_windows_unavailable'), \
                mock.patch.dict(os.environ, {'WATCH_DEMO': '1'}), mock.patch('attachments_http.handle_http', side_effect=handle):
            server.H.attachments_http(Request())
        self.assertEqual(seen, [{'native': None}], 'the default code and words: attachments_disabled, as before')


@unittest.skipUnless(os.name == 'nt', 'The real Windows start; simulated on POSIX by tests/test_attachments_windows_store.py')
class RealWindowsStartupTests(unittest.TestCase):
    def test_real_start_attachments_starts_the_store_and_keeps_agent_delivery_off(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(os.path.realpath(temporary))
            private_live_room(home)
            with mock.patch.multiple(server, HOME=str(home), KEY=KEY, ATTACHMENTS_ENABLED=True,
                                     OFFICER_DOCUMENTS_ENABLED=True), mock.patch.dict(os.environ, {'WATCH_DEMO': '0'}):
                service = None
                try:
                    service = server.start_attachments()
                    self.assertIsNotNone(service)
                    self.assertIsNone(server.ATTACHMENTS_OFF)
                    with mock.patch.object(server, 'ATTACHMENTS', service):
                        value = model_selection()
                    # Motet's own file reading still refuses at its policy on Windows (step 6c.3).
                    self.assertEqual((value['attachments_enabled'], value['documents_enabled']), (True, False))
                    self.assertNotIn('attachments_reason', value)
                    self.assertIs(server.NATIVE_ATTACHMENT_DELIVERY_HOST, False)
                finally:
                    if service is not None:
                        service.close()
                    server.ATTACHMENTS_OFF = 'attachments_disabled'


class RefusalCodeTests(unittest.TestCase):
    def test_requests_get_the_reason_code_while_the_store_is_off(self):
        seen = []
        def handle(handler, service, key, port, *, native=None, unavailable, **options):
            seen.append((service, unavailable))
        for off in ('attachments_windows_unavailable', 'attachments_storage_unavailable', 'attachments_disabled', None):
            with mock.patch.object(server, 'ATTACHMENTS', None), mock.patch.object(server, 'ATTACHMENTS_OFF', off), \
                    mock.patch.dict(os.environ, {'WATCH_DEMO': '0'}), mock.patch('attachments_http.handle_http', side_effect=handle):
                server.H.attachments_http(Request())
            code = off or 'attachments_disabled'
            self.assertEqual(seen.pop(), (None, (code, server.ATTACHMENTS_OFF_WHY[code])))

    def test_native_helper_is_chosen_only_where_the_host_may_deliver(self):
        seen = []
        def handle(handler, service, key, port, *, native=None, unavailable, **options):
            seen.append(native)
        helper = object()
        messaging = mock.Mock(_native_composition=mock.Mock(claude=helper))
        for host, expected in ((False, None), (True, helper)):
            with mock.patch.multiple(server, MESSAGING=messaging, ATTACHMENTS=mock.Mock(), ATTACHMENTS_OFF=None,
                                     ATTACHMENTS_ENABLED=True, NATIVE_AGENT_ATTACHMENTS_ENABLED=True,
                                     NATIVE_ATTACHMENT_DELIVERY_HOST=host), \
                    mock.patch.dict(os.environ, {'WATCH_DEMO': '0'}), mock.patch('attachments_http.handle_http', side_effect=handle):
                server.H.attachments_http(Request())
            self.assertIs(seen.pop(), expected)

    def test_http_boundary_answers_with_that_code_after_owner_checks(self):
        import attachments_http

        class Headers:
            def __init__(self, values):
                self.values = values

            def get_all(self, name, default):
                return self.values.get(name, default)

        class Handler:
            command, path = 'GET', '/api/attachments/native/selection?destinationId=fictional'
            connection = mock.Mock()

            def __init__(self, key):
                self.headers = Headers({'X-Live-Room-Key': [key], 'Host': ['127.0.0.1:18801']})
                self.sent = None

            def send(self, code, body, kind=None, extra=()):
                self.sent = code, json.loads(body)

        refused = ('attachments_windows_unavailable', WINDOWS)
        handler = Handler(KEY)
        attachments_http.handle_http(handler, None, KEY, 18801, unavailable=refused)
        self.assertEqual(handler.sent[0], 503)
        self.assertEqual(handler.sent[1]['error'], {'code': refused[0], 'message': WINDOWS, 'retryable': False})
        stranger = Handler('fictional-wrong-key')
        attachments_http.handle_http(stranger, None, KEY, 18801, unavailable=refused)
        self.assertEqual(stranger.sent[0], 401, 'the reason is told only to the owner')
        default = Handler(KEY)
        attachments_http.handle_http(default, None, KEY, 18801)
        self.assertEqual(default.sent[1]['error']['code'], 'attachments_disabled')

    def test_agent_chat_file_requests_get_the_windows_reason_while_the_store_runs(self):
        """With the store running but delivery to agent chats off (Windows), the page's first request for an agent
        chat answers with the Windows reason, which it shows as a quiet refusal, not "could not read your saved
        drafts"."""
        seen = []

        def handle(handler, service, key, port, *, native=None, unavailable, **options):
            seen.append(options)
        for host, expected in ((False, {'native_unavailable': ('attachments_windows_unavailable', WINDOWS)}), (True, {})):
            with mock.patch.multiple(server, ATTACHMENTS=mock.Mock(), ATTACHMENTS_OFF=None, ATTACHMENTS_ENABLED=True,
                                     NATIVE_AGENT_ATTACHMENTS_ENABLED=True, NATIVE_ATTACHMENT_DELIVERY_HOST=host), \
                    mock.patch.dict(os.environ, {'WATCH_DEMO': '0'}), mock.patch('attachments_http.handle_http', side_effect=handle):
                server.H.attachments_http(Request())
            self.assertEqual(seen.pop(), expected)

    def test_http_answers_agent_chat_requests_with_that_reason_and_others_from_the_store(self):
        import attachments_http

        class Headers:
            def __init__(self, values):
                self.values = values

            def get_all(self, name, default):
                return self.values.get(name, default)

            def __iter__(self):
                return iter(self.values)

        class Handler:
            command = 'GET'
            connection = mock.Mock()

            def __init__(self, path):
                self.path = '/api/attachments' + path
                self.headers = Headers({'X-Live-Room-Key': [KEY], 'Host': ['127.0.0.1:18801']})
                self.sent = None

            def send(self, code, body, kind=None, extra=()):
                self.sent = code, json.loads(body)

        refused = ('attachments_windows_unavailable', WINDOWS)
        service = mock.Mock()
        service.operation.return_value.__enter__ = mock.Mock(return_value=None)
        service.operation.return_value.__exit__ = mock.Mock(return_value=False)
        service.native_selection.side_effect = AssertionError('the store was asked about agent delivery')
        for path in ('/native/selection?destinationId=fictional', '/native/requests?destinationId=fictional&scopeId=fictional-orchard'):
            handler = Handler(path)
            attachments_http.handle_http(handler, service, KEY, 18801, native_unavailable=refused)
            self.assertEqual((handler.sent[0], handler.sent[1]['error']),
                             (503, {'code': refused[0], 'message': WINDOWS, 'retryable': False}))
        service.storage.return_value = {'fictional': True}
        handler = Handler('/storage')
        attachments_http.handle_http(handler, service, KEY, 18801, native_unavailable=refused)
        self.assertNotEqual(handler.sent[1].get('error', {}).get('code'), refused[0], 'the store still answers the rest')

    def test_page_words_each_code_exactly_as_the_server_does(self):
        client = (ROOT / 'src' / 'web' / 'native-agent-attachments-client.js').read_text(encoding='utf-8')
        page = (ROOT / 'src' / 'web' / 'native-agent-attachments-page.js').read_text(encoding='utf-8')
        passive = re.search(r"const passiveFileRefusal = s => !!s && \[([^\]]*)\]", page).group(1)
        for code in ('attachments_windows_unavailable', 'attachments_storage_unavailable'):
            self.assertIn("  %s: '%s'," % (code, server.ATTACHMENTS_OFF_WHY[code]), client)
            self.assertIn("'%s'" % code, passive, 'the agent stage shows it as a quiet refusal, with no Look again')


class HostFlagTests(unittest.TestCase):
    def test_native_delivery_is_a_named_host_capability_off_on_windows(self):
        self.assertIs(server.NATIVE_ATTACHMENT_DELIVERY_HOST, not server.WIN)
        self.assertIs(server.NATIVE_ATTACHMENT_DELIVERY_HOST, os.name != 'nt')

    def test_host_flag_off_never_installs_native_delivery_even_with_a_running_store(self):
        messaging, service = mock.Mock(_attachment_guard=None), mock.Mock(store=object())
        with mock.patch.multiple(server, ATTACHMENTS_ENABLED=True, NATIVE_AGENT_ATTACHMENTS_ENABLED=True,
                                 NATIVE_ATTACHMENT_DELIVERY_HOST=False), mock.patch.dict(os.environ, {'WATCH_DEMO': '0'}), \
                mock.patch('attachments_native_composition.bind_native_composition',
                           side_effect=AssertionError('native delivery bound')) as bind, \
                mock.patch('attachments_owner_runtime.bind_owner_runtime') as owner:
            self.assertIsNone(server.start_native_attachment_maintenance(messaging, service))
            messaging._attachment_guard = object()     # a retained guard is still maintained, never installed
            server.start_native_attachment_maintenance(messaging, service)
        bind.assert_not_called()
        owner.assert_called_once_with(messaging, allow_install=False)

    @unittest.skipIf(os.name == 'nt', 'Native delivery binding needs POSIX; this checks the flag lets a Mac bind')
    def test_host_flag_on_still_binds_native_delivery(self):
        messaging, service = mock.Mock(_attachment_guard=None), mock.Mock(store=object())
        with mock.patch.multiple(server, ATTACHMENTS_ENABLED=True, NATIVE_AGENT_ATTACHMENTS_ENABLED=True,
                                 NATIVE_ATTACHMENT_DELIVERY_HOST=True), mock.patch.dict(os.environ, {'WATCH_DEMO': '0'}), \
                mock.patch('attachments_native_composition.bind_native_composition', return_value='bound') as bind:
            self.assertEqual(server.start_native_attachment_maintenance(messaging, service), 'bound')
        bind.assert_called_once()


class ImportWithoutFcntlTests(unittest.TestCase):
    def test_attachment_modules_import_where_fcntl_does_not_exist(self):
        # Every module on the attachment path on disk, so a new one is covered without editing this list.
        modules = sorted({p.stem for pattern in ('attachments_*.py', 'officer_document*.py', 'codex_attachment*.py',
                                                 'claude_attachment*.py') for p in (ROOT / 'src').glob(pattern)})
        for name in ('attachments_native_namespace', 'attachments_claude_service', 'attachments_native_composition',
                     'officer_documents', 'codex_attachment_live_evidence', 'claude_attachment_adapter'):
            self.assertIn(name, modules)
        code = ("import importlib, sys\n"
                "sys.modules['fcntl'] = None\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "failed = []\n"
                "for name in sys.argv[2:]:\n"
                "    try:\n"
                "        importlib.import_module(name)\n"
                "    except ImportError as error:\n"
                "        failed.append(name + ': ' + str(error))\n"
                "print('\\n'.join(failed))\n")
        result = subprocess.run([sys.executable, '-c', code, str(ROOT / 'src')] + modules,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '', 'these modules need fcntl at import time')

    @unittest.skipUnless(os.name == 'posix', 'Checks the lazy import still reaches the POSIX lock')
    def test_host_fence_without_fcntl_fails_closed(self):
        from attachments_native_namespace import NamespaceError, NativeNamespaceFence
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(os.path.realpath(temporary))
            root.chmod(0o700)
            fd = os.open(root / 'managed-claude.lock', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            fence = NativeNamespaceFence(root)
            with fence.host_fence() as held:       # the lock file qualifies, so only fcntl decides below
                self.assertIsInstance(held, int)
            with mock.patch.dict(sys.modules, {'fcntl': None}), self.assertRaises(NamespaceError):
                with fence.host_fence():
                    pass


if __name__ == '__main__':
    unittest.main()
