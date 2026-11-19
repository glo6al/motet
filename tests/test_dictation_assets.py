"""Model setup uses small in-memory fixtures, never downloads a real model."""
import hashlib
import importlib.util
import io
import os
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'integrations' / 'dictation'))
import assets


class Response(io.BytesIO):
    def __init__(self, data, url):
        super().__init__(data)
        self.url = url
        self.request_sizes = []

    def geturl(self):
        return self.url

    def read(self, size=-1):
        if not 0 < size <= assets.CHUNK_BYTES:
            raise AssertionError('A model transfer must use bounded reads')
        self.request_sizes.append(size)
        return super().read(size)


class Opener:
    def __init__(self, data, bad_file=None, final_url=None):
        self.data = data
        self.bad_file = bad_file
        self.final_url = final_url
        self.calls = []
        self.responses = []

    def open(self, request, timeout):
        name = request.full_url.rsplit('/', 1)[-1]
        self.calls.append(name)
        if name == self.bad_file:
            raise OSError('sensitive local details must not be repeated')
        if request.get_header('User-agent') != 'moonshine-voice/0.1.6':
            raise AssertionError('The explicit model download identifies its package')
        response = Response(self.data[name], self.final_url or request.full_url)
        self.responses.append(response)
        return response


class AssetsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        os.chmod(str(self.root), 0o700)
        self.model = self.root / 'model'
        self.data = {'adapter.ort': b'first-model-file', 'encoder.ort': b'second-model-file'}
        self.info = {
            'schemaVersion': 1, 'engine': 'Moonshine Small Streaming English',
            'packageVersion': '0.1.6', 'modelId': 'small-streaming-en/quantized_26_11_02',
            'bytes': sum(len(value) for value in self.data.values()), 'licence': 'MIT',
            'files': [{'filename': name, 'size': len(value), 'sha256': hashlib.sha256(value).hexdigest(),
                       'url': assets._MODEL_URL + name, 'licence': 'MIT'} for name, value in self.data.items()]}
        self.manifest = mock.patch.object(assets, 'model_info', return_value=self.info)
        self.manifest.start()

    def tearDown(self):
        self.manifest.stop()
        self.temp.cleanup()

    def write(self, name, data):
        self.model.mkdir(mode=0o700, exist_ok=True)
        target = self.model / name
        target.write_bytes(data)
        os.chmod(str(target), 0o600)
        return target

    def assert_error(self, code, callback):
        with self.assertRaises(assets.AssetsError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(str(self.root), caught.exception.message)
        return caught.exception

    def test_committed_manifest_exact_model_version_and_size(self):
        self.manifest.stop()
        with mock.patch.object(urllib.request, 'urlopen', side_effect=AssertionError('No network')):
            info = assets.model_info()
        self.assertEqual(info['bytes'], assets.MODEL_BYTES)
        self.assertEqual(info['bytes'], 142465862)
        self.assertEqual(len(info['files']), 8)
        self.assertEqual(info['packageVersion'], '0.1.6')
        self.assertEqual(info['modelId'], 'small-streaming-en/quantized_26_11_02')
        self.assertEqual(info['licence'], 'MIT')
        self.assertEqual(sum(item['size'] for item in info['files']), info['bytes'])
        self.assertTrue(all(item['url'] == assets._MODEL_URL + item['filename'] for item in info['files']))

    def test_confirmation_is_checked_before_any_path_or_network_access(self):
        opener = Opener(self.data)
        for value in (None, True, str(self.info['bytes']), self.info['bytes'] - 1, self.info['bytes'] + 1):
            with mock.patch.object(assets, '_path', side_effect=AssertionError('Touched a path')):
                self.assert_error('confirmation_required', lambda: assets.download_model(self.model, value, opener=opener))
        self.assertFalse(opener.calls)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_first_download_publishes_only_after_full_verification(self):
        opener = Opener(self.data)
        progress = []

        def observe(event):
            progress.append(event)
            if event['phase'] != 'complete':
                self.assertFalse(self.model.exists())
                self.assertLess(event['completedBytes'], event['totalBytes'])

        result = assets.download_model(self.model, self.info['bytes'], progress=observe, opener=opener)
        self.assertTrue(assets.validate_model(self.model))
        self.assertEqual(result['downloadedBytes'], self.info['bytes'])
        self.assertEqual(opener.calls, list(self.data))
        self.assertEqual(progress[-1]['phase'], 'complete')
        self.assertEqual(progress[-1]['completedBytes'], self.info['bytes'])
        self.assertFalse(list(self.root.glob('.model.staging-*')))
        self.assertTrue(all(response.request_sizes for response in opener.responses))
        if os.name == 'posix':
            self.assertEqual(self.model.stat().st_mode & 0o777, 0o700)
            self.assertTrue(all((path.stat().st_mode & 0o777) == 0o600 for path in self.model.iterdir()))

    def test_valid_model_performs_no_network_or_replacement(self):
        for name, data in self.data.items():
            self.write(name, data)
        before = self.model.stat().st_ino
        opener = Opener(self.data)
        result = assets.download_model(self.model, self.info['bytes'], opener=opener)
        self.assertEqual(result['downloadedBytes'], 0)
        self.assertFalse(opener.calls)
        self.assertEqual(self.model.stat().st_ino, before)

    def test_incomplete_model_reuses_verified_files(self):
        self.write('adapter.ort', self.data['adapter.ort'])
        opener = Opener(self.data)
        result = assets.download_model(self.model, self.info['bytes'], opener=opener)
        self.assertEqual(opener.calls, ['encoder.ort'])
        self.assertEqual(result['downloadedBytes'], len(self.data['encoder.ort']))
        self.assertTrue(assets.validate_model(self.model))
        self.assertFalse(list(self.root.glob('.model.previous-*')))

    def test_failed_transfer_preserves_existing_files_and_releases_lock(self):
        first = self.write('adapter.ort', self.data['adapter.ort'])
        self.assert_error('download_failed', lambda: assets.download_model(
            self.model, self.info['bytes'], opener=Opener(self.data, bad_file='encoder.ort')))
        self.assertEqual(first.read_bytes(), self.data['adapter.ort'])
        self.assertEqual([item.name for item in self.model.iterdir()], ['adapter.ort'])
        self.assertFalse(list(self.root.glob('.model.staging-*')))
        self.assertTrue(assets.download_model(self.model, self.info['bytes'], opener=Opener(self.data)))

    def test_wrong_hash_truncated_and_oversize_transfers_never_publish(self):
        for wrong in (b'x' * len(self.data['adapter.ort']), b'x', self.data['adapter.ort'] + b'extra'):
            with self.subTest(length=len(wrong)):
                bad = dict(self.data, **{'adapter.ort': wrong})
                self.assert_error('download_integrity', lambda: assets.download_model(
                    self.model, self.info['bytes'], opener=Opener(bad)))
                self.assertFalse(self.model.exists())
                self.assertFalse(list(self.root.glob('.model.staging-*')))

    def test_validate_requires_hash_and_exact_size(self):
        self.assert_error('model_missing', lambda: assets.validate_model(self.model))
        for name, data in self.data.items():
            self.write(name, data)
        self.assertTrue(assets.validate_model(self.model))
        self.write('adapter.ort', b'x' * len(self.data['adapter.ort']))
        self.assert_error('model_integrity', lambda: assets.validate_model(self.model))

    def test_replacement_failure_restores_previous_folder(self):
        first = self.write('adapter.ort', self.data['adapter.ort'])
        real_replace = os.replace

        def fail_activation(source, destination):
            if '.staging-' in str(source):
                raise OSError('activation failed')
            return real_replace(source, destination)

        with mock.patch.object(os, 'replace', side_effect=fail_activation):
            self.assert_error('download_failed', lambda: assets.download_model(
                self.model, self.info['bytes'], opener=Opener(self.data)))
        self.assertEqual(first.read_bytes(), self.data['adapter.ort'])
        self.assertFalse((self.model / 'encoder.ort').exists())
        self.assertFalse(list(self.root.glob('.model.previous-*')))
        self.assertFalse(list(self.root.glob('.model.staging-*')))

    def test_concurrent_download_is_refused_without_network(self):
        with assets._download_lock(self.model):
            opener = Opener(self.data)
            self.assert_error('download_busy', lambda: assets.download_model(
                self.model, self.info['bytes'], opener=opener))
            self.assertFalse(opener.calls)

    def test_foreign_response_location_is_refused(self):
        for url in ('http://download.moonshine.ai/file', 'https://other.invalid/file'):
            self.assert_error('download_failed', lambda: assets.download_model(
                self.model, self.info['bytes'], opener=Opener(self.data, final_url=url)))
        self.assertFalse(self.model.exists())

    def test_redirect_handler_refuses_cross_host_and_plain_http(self):
        handler = assets._SameOriginRedirect()
        request = urllib.request.Request(assets._MODEL_URL + 'adapter.ort')
        for url in ('http://download.moonshine.ai/file', 'https://other.invalid/file',
                    'https://user:password@download.moonshine.ai/file'):
            self.assert_error('download_failed', lambda: handler.redirect_request(
                request, None, 302, 'Found', {}, url))
        allowed = handler.redirect_request(request, None, 302, 'Found', {}, assets._MODEL_URL + 'encoder.ort')
        self.assertEqual(allowed.full_url, assets._MODEL_URL + 'encoder.ort')

    def test_malformed_manifest_is_refused_before_touching_model_path(self):
        for field, value in (('filename', '../outside'), ('filename', '/outside'),
                             ('url', 'https://other.invalid/adapter.ort'), ('size', -1),
                             ('sha256', 'not-a-hash')):
            bad = copy.deepcopy(self.info)
            bad['files'][0][field] = value
            with mock.patch.object(assets, 'model_info', return_value=bad), \
                    mock.patch.object(assets, '_path', side_effect=AssertionError('Invalid manifest touched state')):
                self.assert_error('invalid_manifest', lambda: assets.download_model(
                    self.model, bad['bytes'], opener=Opener(self.data)))

    def test_unrelated_existing_files_are_never_removed(self):
        file = self.write('unrelated.txt', b'keep this')
        self.assert_error('unsafe_model_path', lambda: assets.download_model(
            self.model, self.info['bytes'], opener=Opener(self.data)))
        self.assertEqual(file.read_bytes(), b'keep this')

    @unittest.skipUnless(os.name == 'posix', 'POSIX private permissions')
    def test_world_readable_parent_or_model_file_is_rejected(self):
        os.chmod(str(self.root), 0o755)
        self.assert_error('unsafe_model_path', lambda: assets.download_model(
            self.model, self.info['bytes'], opener=Opener(self.data)))
        os.chmod(str(self.root), 0o700)
        target = self.write('adapter.ort', self.data['adapter.ort'])
        os.chmod(str(target), 0o644)
        self.assert_error('unsafe_model_path', lambda: assets.validate_model(self.model))

    @unittest.skipUnless(hasattr(os, 'symlink'), 'Symlink support required')
    def test_model_file_folder_and_lock_symlinks_are_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir(mode=0o700)
        self.model.symlink_to(outside, target_is_directory=True)
        self.assert_error('unsafe_model_path', lambda: assets.download_model(
            self.model, self.info['bytes'], opener=Opener(self.data)))
        self.model.unlink()
        self.model.mkdir(mode=0o700)
        file = outside / 'adapter.ort'
        file.write_bytes(self.data['adapter.ort'])
        os.chmod(str(file), 0o600)
        (self.model / 'adapter.ort').symlink_to(file)
        self.assert_error('unsafe_model_path', lambda: assets.validate_model(self.model))
        (self.model / 'adapter.ort').unlink()
        (self.root / '.model.download.lock').symlink_to(file)
        self.assert_error('unsafe_model_path', lambda: assets.download_model(
            self.model, self.info['bytes'], opener=Opener(self.data)))
        self.assertEqual(file.read_bytes(), self.data['adapter.ort'])

    @unittest.skipUnless(os.name == 'posix', 'POSIX hardlink handling')
    def test_hardlinked_model_files_are_rejected(self):
        target = self.write('adapter.ort', self.data['adapter.ort'])
        os.link(str(target), str(self.root / 'other-link'))
        self.assert_error('unsafe_model_path', lambda: assets.validate_model(self.model))

    def test_import_does_not_read_manifest_or_open_network(self):
        spec = importlib.util.spec_from_file_location('isolated_assets', assets.__file__)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.object(Path, 'read_text', side_effect=AssertionError('Import read a file')), \
                mock.patch.object(urllib.request, 'urlopen', side_effect=AssertionError('Import contacted network')):
            spec.loader.exec_module(module)
        self.assertEqual(module.MODEL_BYTES, 142465862)


if __name__ == '__main__':
    unittest.main()
