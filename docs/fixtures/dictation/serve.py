#!/usr/bin/env python3
"""Serve the actual page from an explicit preview home, without real agents.

The default /?demo is inert. /review uses a fictional owner credential and
fixture responses. --synthetic exercises the real audio worklet, helper protocol
and server proxy with generated audio and a deterministic, in-memory recognizer.
"""
import argparse
import json
from pathlib import Path
import signal
import sys
import threading
import time
import urllib.parse

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

OWNER = 'fictional-dictation-preview-only'


class FakeRuntime:
    """Scripted text from packet counts. PCM is neither decoded nor retained."""
    load_ms = 100
    PHRASES = (
        'Which agent changed the pricing file today?',
        'And is anything still waiting on me?',
        'Keep this draft local while I make a correction.',
    )

    def __init__(self):
        self.epoch = 1
        self.active = False
        self.closed = False
        self.audio_packets = 0
        self.starts = 0
        self.resets = 0
        self.packet = 0
        self.line = 0
        self.last = None
        self.interval_packets = 3

    def start(self):
        # Exercise the service's real loading -> ready transition without assets.
        time.sleep(self.load_ms / 1000)

    def call(self, action, **payload):
        if self.closed:
            raise RuntimeError('Fixture runtime closed')
        events = []
        if action == 'start':
            self.active = True
            self.epoch, self.packet, self.line, self.last = 1, 0, 0, None
            self.interval_packets = 3 if payload.get('interval') == .25 else 5
            self.starts += 1
        elif action == 'reset':
            old = self.epoch
            self.epoch += 1
            self.packet, self.line, self.last = 0, 0, None
            self.resets += 1
            # The real service must retire this deliberately stale event.
            events.append({'type': 'line', 'epoch': old, 'lineId': 'retired',
                           'text': 'Retired fixture text must never appear.', 'complete': True})
        elif action == 'audio':
            if not self.active:
                raise RuntimeError('Fixture take is inactive')
            self.audio_packets += 1
            self.packet += 1
            words = self.PHRASES[self.line % len(self.PHRASES)].split()
            count = min(len(words), self.packet // self.interval_packets)
            if count:
                text = ' '.join(words[:count])
                complete = count == len(words)
                event = {'type': 'line', 'epoch': self.epoch,
                         'lineId': 'fixture-%d-%d' % (self.epoch, self.line),
                         'text': text, 'complete': complete,
                         'audioSeconds': round(self.audio_packets / 10, 2), 'decodeMs': 0}
                if self.last is None or (self.last['lineId'], self.last['text'], self.last['complete']) != (event['lineId'], text, complete):
                    events.append(event)
                    self.last = event
                if complete:
                    self.line += 1
                    self.packet, self.last = 0, None
        elif action == 'stop':
            if self.last:
                events.append(dict(self.last, complete=True))
            self.active, self.last = False, None
        elif action == 'cancel':
            self.active, self.last = False, None
        else:
            raise RuntimeError('Unknown fixture command')
        return {'epoch': self.epoch, 'events': events, 'commandMs': 0}

    def close(self):
        self.active, self.closed = False, True
        self.last = None


def start_synthetic(home):
    """Start the actual authenticated helper with a fake runtime, never native ASR."""
    from integrations.dictation.helper import HelperServer
    from integrations.dictation.service import DictationService
    from integrations.dictation.state import PrivateState
    runtime = FakeRuntime()
    service = DictationService(lambda: runtime, lambda: None)
    key = PrivateState(home).key(create=True)
    helper = HelperServer(0, key, service)
    service.load()
    thread = threading.Thread(target=helper.serve_forever, kwargs={'poll_interval': .1}, daemon=True)
    thread.start()
    return helper, thread, runtime


def review_bootstrap(synthetic):
    """Trusted fixture-only code, embedded beneath the real page's script nonce."""
    start = """(function(){
  // A demo query must not touch credentials, capture, storage or helper routes.
  if(/[?&]demo\\b/.test(location.search))return;
  localStorage.setItem('liveRoomKey','fictional-dictation-preview-only');
"""
    if not synthetic:
        return (start + '})();\n').encode()
    generated = """
  const label=document.createElement('div');label.id='dictationSyntheticLabel';
  label.textContent='Simulated audio · microphone not used';label.setAttribute('role','note');
  label.style.cssText='position:fixed;top:74px;left:50%;transform:translateX(-50%);z-index:70;max-width:calc(100vw - 24px);padding:8px 14px;border:1px solid #d8c690;border-radius:8px;background:#fff7da;color:#685223;font:500 13px/18px system-ui;text-align:center;pointer-events:none';
  document.body.appendChild(label);
  const active=new Set();
  const closeAll=()=>{for(const close of [...active])close()};
  document.addEventListener('visibilitychange',()=>{if(document.hidden)closeAll()});
  addEventListener('pagehide',closeAll);
  // This replacement is installed only on /review. It never calls the original
  // getUserMedia and never connects an audio node to a speaker destination.
  if(!navigator.mediaDevices)Object.defineProperty(navigator,'mediaDevices',{configurable:true,value:{}});
  Object.defineProperty(navigator.mediaDevices,'getUserMedia',{configurable:true,value:async function(constraints){
    if(!constraints||!constraints.audio||constraints.video||document.hidden)throw new DOMException('Fixture audio is unavailable.','NotAllowedError');
    const context=new AudioContext(),destination=context.createMediaStreamDestination(),oscillator=context.createOscillator(),gain=context.createGain();
    oscillator.frequency.value=220;gain.gain.value=.12;
    oscillator.connect(gain);gain.connect(destination);
    const stream=destination.stream,stops=stream.getTracks().map(track=>track.stop.bind(track));
    let closed=false;
    const close=()=>{if(closed)return;closed=true;active.delete(close);try{oscillator.stop()}catch(error){}oscillator.disconnect();gain.disconnect();destination.disconnect();for(const stop of stops)stop();if(context.state!=='closed')context.close().catch(()=>{})};
    active.add(close);
    try{
      for(const track of stream.getTracks()){
        Object.defineProperty(track,'stop',{configurable:true,value:close});
        track.addEventListener('ended',close,{once:true});
      }
      stream.addEventListener('inactive',close,{once:true});
      await context.resume();
      if(closed)throw new DOMException('Fixture audio was stopped.','AbortError');
      oscillator.start();return stream;
    }catch(error){close();throw error}
  }});
})();
"""
    return (start + generated).encode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True, help='Existing empty private preview home')
    parser.add_argument('--port', type=int, default=18881)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--helper-port', type=int, help='Explicit existing helper in this same preview home')
    mode.add_argument('--synthetic', action='store_true', help='Generate audio; no microphone or native recognizer')
    args = parser.parse_args()
    home = args.home.resolve(strict=True)
    if not home.is_dir() or args.port == 8793 or not 0 <= args.port <= 65535:
        parser.error('Choose an existing preview home and a separate port.')
    if args.helper_port is not None and (not 1 <= args.helper_port <= 65535 or args.helper_port == 8793):
        parser.error('Choose a separate existing helper port.')
    # A dedicated preview home can hold its helper only; never use a real home.
    if any(path.name != '.live-room' for path in home.iterdir()):
        parser.error('Use a dedicated empty preview home.')
    import server
    from dictation_proxy import DictationProxy
    server.HOME = str(home)
    server.KEY = OWNER
    server.ATTACHMENTS_ENABLED = False
    server.OFFICER_DOCUMENTS_ENABLED = False
    server.NATIVE_AGENT_ATTACHMENTS_ENABLED = False
    synthetic = start_synthetic(home) if args.synthetic else None
    helper_port = synthetic[0].server_port if synthetic else args.helper_port
    server.DICTATION_PROXY = DictationProxy(home, helper_port or 18879, enabled=bool(helper_port))
    counters = {'blockedSends': 0, 'dictationRequests': {}}
    count_lock = threading.Lock()
    stopped = threading.Event()

    def count(path):
        action = path.rsplit('/', 1)[-1]
        if action in ('status', 'start', 'audio', 'events', 'stop', 'cancel', 'reset'):
            with count_lock:
                routes = counters['dictationRequests']
                routes[action] = routes.get(action, 0) + 1

    class Preview(server.H):
        def log_message(self, *args):
            pass  # No keys, audio, transcript text or request bodies in logs.

        def json(self, value, status=200):
            return self.send(status, json.dumps(value).encode(), 'application/json; charset=utf-8')

        def do_GET(self):
            if not self.host_ok():
                return self.send(403, b'forbidden')
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == '/review':
                body, csp = server.page_with_nonce(officer_document_modules=True, native_agent_modules=False)
                marker = b'/* ---------- demo decided first:'
                body = body.replace(marker, review_bootstrap(args.synthetic) + marker, 1)
                return self.send(200, body, 'text/html; charset=utf-8', (('Content-Security-Policy', csp),))
            if parsed.path == '/check':
                return self.send(200, b'ok')
            if parsed.path == '/events':
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                value = {'kind': 'hello', 'agent': '', 'seq': 1, 'boot': 'dictation-fixture',
                         'pkey': 'fictional-preview', 'window': 1200, 'names': {}, 'watch': {}, 'win': False}
                try:
                    self.wfile.write(('data: ' + json.dumps(value) + '\n\n').encode()); self.wfile.flush()
                    while not stopped.wait(2):
                        self.wfile.write(b': fixture heartbeat\n\n'); self.wfile.flush()
                except OSError:
                    pass
                return
            if parsed.path in ('/officer/models', '/officer/model-selection'):
                return self.json({'schemaVersion': 1, 'available': True, 'complete': True,
                                  'selected_model': 'automatic', 'selected_name': 'Automatic',
                                  'selection_available': True, 'attachments_enabled': False, 'documents_enabled': False,
                                  'models': [{'id': 'automatic', 'display_name': 'Automatic'}]})
            if parsed.path == '/agent-messages':
                return self.json({'available': False, 'reason': 'Preview only; no agents are connected.', 'requests': [], 'controls': []})
            if parsed.path == '/fixture-status':
                with count_lock:
                    value = {'fixture': True, 'synthetic': args.synthetic, 'agentMessages': 0,
                             'blockedSends': counters['blockedSends'],
                             'dictationRequests': dict(counters['dictationRequests'])}
                if synthetic:
                    value.update({'audioPackets': synthetic[2].audio_packets,
                                  'takesStarted': synthetic[2].starts, 'resets': synthetic[2].resets})
                return self.json(value)
            if parsed.path.startswith('/api/dictation/'):
                count(parsed.path)
                return super().do_GET()
            if parsed.path in ('/', '/dictation-worklet.js', '/dictation-help') or parsed.path.startswith('/static/'):
                return super().do_GET()
            return self.json({'error': 'Unavailable in this isolated preview.'}, 503)

        def do_POST(self):
            if not self.host_ok():
                return self.send(403, b'forbidden')
            if self.path.startswith('/api/dictation/'):
                count(self.path)
                return super().do_POST()
            # Count only; never read or retain an attempted product-send body.
            if self.path in ('/ask', '/agent-message', '/agent-control'):
                with count_lock:
                    counters['blockedSends'] += 1
            self.close_connection = True
            return self.json({'error': 'Preview only. Nothing was sent to an agent or model.'}, 409)

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST
        do_OPTIONS = do_POST

    http = None
    try:
        http = server.Server(('127.0.0.1', args.port), Preview)
        server.PORT = http.server_port
        print('Isolated demo: http://127.0.0.1:%d/?demo' % http.server_port, flush=True)
        print('Isolated capture review: http://127.0.0.1:%d/review' % http.server_port, flush=True)
        if args.synthetic:
            print('Simulated audio · microphone not used. Scripted fixture text, not ASR results.', flush=True)
        print('No observers, provider calls or real messaging. Ctrl+C stops this fixture.', flush=True)

        def stop(signum, frame):
            raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM, stop)
        http.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        if http is not None:
            http.server_close()
        if synthetic:
            helper, thread, _ = synthetic
            helper.shutdown(); helper.server_close(); thread.join(2)


if __name__ == '__main__':
    main()
