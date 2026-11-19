"""Explicit local smoke check using an already installed optional runtime/model.

No microphone, download, recording or external request. Ordinary CI does not run
this check. The bundled sample is fed faster than real time; timing is backend
finalization only, not word-to-screen latency.
"""
import argparse
import http.client
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integrations.dictation.assets import model_info
from integrations.dictation.helper import HelperServer, make_service
from dictation_auth import request_headers
from integrations.dictation.state import PrivateState


def check(python, source):
    sample_script = (
        "import wave,struct,sys;from pathlib import Path;from moonshine_voice import get_assets_path;"
        "w=wave.open(str(Path(get_assets_path())/'beckett.wav'));"
        "assert (w.getnchannels(),w.getframerate(),w.getsampwidth())==(1,16000,2);"
        "raw=w.readframes(w.getnframes());"
        "values=[x[0]/32768 for x in struct.iter_unpack('<h',raw)];"
        "sys.stdout.buffer.write(struct.pack('<'+'f'*len(values),*values))"
    )
    sample = subprocess.run([str(python), '-I', '-B', '-c', sample_script],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
    if sample.returncode or not sample.stdout:
        raise RuntimeError('The installed runtime could not provide its bundled sample.')
    with tempfile.TemporaryDirectory(prefix='dictation-native-') as directory:
        home = Path(directory).resolve()
        state = PrivateState(home)
        key = state.key(create=True)
        model = state.root / 'model'
        model.mkdir(mode=0o700)
        for item in model_info()['files']:
            target = model / item['filename']
            shutil.copyfile(source / item['filename'], target)
            target.chmod(0o600)
        service = make_service(python, model)
        server = HelperServer(0, key, service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(path, value=None, audio_headers=None):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=20)
            headers = {}
            if isinstance(value, dict):
                value = json.dumps(value).encode()
                headers['Content-Type'] = 'application/json'
            if audio_headers:
                headers.update(audio_headers)
                headers['Content-Type'] = 'application/octet-stream'
            verb = 'POST' if value is not None else 'GET'
            headers.update(request_headers(key, server.server_port, verb, path, value, extra_headers=headers))
            connection.request(verb, path, value, headers)
            response = connection.getresponse()
            result = json.loads(response.read())
            connection.close()
            if response.status != 200:
                raise RuntimeError('Native helper check failed: ' + result.get('error', {}).get('code', 'unavailable'))
            return result
        try:
            service.load()
            service.loading_thread.join(20)
            status = request('/v1/status')
            assert status['state'] == 'ready', status['state']
            ident = request('/v1/start', {'intervalSeconds': .25})['sessionId']
            for sequence, offset in enumerate(range(0, len(sample.stdout), 6400)):
                request('/v1/audio', sample.stdout[offset:offset + 6400],
                        {'X-Dictation-Session': ident, 'X-Dictation-Sequence': str(sequence)})
            began = time.monotonic()
            stop = request('/v1/stop', {'sessionId': ident})
            stop_ms = (time.monotonic() - began) * 1000
            events = request('/v1/events', {'sessionId': ident, 'after': 0})
            lines = [event for event in events['events'] if event['type'] == 'line']
            partials = sum(bool(event['text']) and not event['complete'] for event in lines)
            finals = sum(event['complete'] for event in lines)
            assert partials > 0 and finals > 0 and events['ended']
            return {'modelLoadMs': status['loadMs'], 'stopHttpRoundTripMs': round(stop_ms, 3),
                    'helperFinalizeMs': stop['finalizeMs'], 'partialEvents': partials,
                    'completedEvents': finals, 'audioSeconds': len(sample.stdout) / 64000,
                    'basis': 'bundled sample, faster than real time; backend only; no microphone'}
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(check(args.python.absolute(), args.model.resolve()), indent=2))
