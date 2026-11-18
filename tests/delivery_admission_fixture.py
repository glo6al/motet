"""Disposable real-HTTP owner-delivery fixture; no model or native application.

The ordinary production handler, messaging store, attachment guard and Claude
plugin protocol remain in use. Only the model and native host are fictional.
Run this file to expose the same fixture for browser checks; stdin EOF stops it.
"""
from contextlib import ExitStack
import http.client
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import attachments_owner_runtime
from attachments_owner_transport import LedgerError
import officer_messaging
import server

NATIVE = '11111111-1111-4111-8111-111111111111'
AGENT = 'c:' + NATIVE
OWNER_KEY = 'fictional-delivery-fixture-owner-key'


class HeldWorker:
    """No background maintenance can hide the intent-write/admission race."""
    def __init__(self, checkpoint, stopped):
        self.checkpoint, self.stopped = checkpoint, stopped
        self.started = False

    def start(self):
        self.started = True

    def is_alive(self):
        return self.started and not self.stopped.is_set()

    def join(self):
        self.started = False


class ObservedConnection(sqlite3.Connection):
    """Count actual first SQL insertions, excluding later receipt updates."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.request_insertions = []
        self.lose_request_commit = False
        self._new_request_pending = False

    def execute(self, sql, parameters=()):
        normalized = ' '.join(sql.lower().split())
        new_id = None
        if (normalized.startswith(('insert into requests ', 'insert or replace into requests '))
                and parameters and not super().execute(
                    'SELECT 1 FROM requests WHERE id=?', (parameters[0],)).fetchone()):
            new_id = parameters[0]
        result = super().execute(sql, parameters)
        if new_id is not None:
            self.request_insertions.append(new_id)
            self._new_request_pending = True
        return result

    def commit(self):
        lose = self.lose_request_commit and self._new_request_pending
        super().commit()
        self._new_request_pending = False
        if lose:
            self.lose_request_commit = False
            raise sqlite3.OperationalError('Fictional lost response after committing the request.')


class DeliveryFixture:
    def __enter__(self):
        self.stack = ExitStack()
        self.stack.__enter__()
        try:
            return self._start()
        except BaseException:
            self.stack.close()
            raise

    def _start(self):
        self.model_calls = 0
        self.stale_decisions = []
        self.refuse_preflight = False
        temp = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='motet-delivery-fixture-'))
        self.project = os.path.realpath(str(Path(temp) / 'Orchard'))
        Path(self.project).mkdir()
        self.stack.enter_context(patch.multiple(
            server, HOME=temp, KEY=OWNER_KEY, OFFICER_KEY='fictional-model-key',
            OFFICER_MODEL='claude-fictional-fixture', OFFICER_MODEL_ERROR='',
            OFFICER_CATALOG={'key': '', 'at': 0, 'models': []},
            HISTORY=None, OFFICER_USAGE=None, CLAUDE_MANAGED=None,
            CLAUDE_MANAGED_ENABLED=False, CLAUDE_MANAGED_FAILED=False))
        # This patches only server.HOME: never redirect the process's HOME or
        # any provider tool to a disposable or real owner configuration.
        saved_agents, saved_asks = dict(server.AGENTS), list(server.OFFICER_ASKS)
        self.stack.callback(lambda: (server.AGENTS.clear(), server.AGENTS.update(saved_agents)))
        self.stack.callback(lambda: server.OFFICER_ASKS.__setitem__(slice(None), saved_asks))
        server.AGENTS.clear()
        server.OFFICER_ASKS.clear()
        server.AGENTS[AGENT] = dict(runtime='Claude', name='Atlas', cwd=self.project, started=time.time())
        connect = sqlite3.connect
        with patch.object(sqlite3, 'connect', side_effect=lambda *a, **kw: connect(*a, factory=ObservedConnection, **kw)):
            self.store = officer_messaging.MessagingStore(str(Path(temp) / 'state'), str(Path(temp) / 'sessions'))
        self.stack.callback(self.store.close)
        self.store.codex_available = False
        self.stack.enter_context(patch.object(server, 'MESSAGING', self.store))
        self.runtime = attachments_owner_runtime.bind_owner_runtime(
            self.store, allow_install=True, scheduler=HeldWorker)
        if self.runtime is None:
            raise RuntimeError('Fixture needs SQLite JSON marker support and a bound owner runtime.')
        self.runtime.refresh_directory()
        if not self.runtime.ledger.directory.ready:
            raise AssertionError('Initial owner directory did not become ready.')
        original_decide = self.store.decide_ask

        def decide(request_id, decision):
            # Establish a current directory immediately before the real intent
            # UPDATE. The held scheduler cannot repair the resulting epoch.
            refusal = self.refuse_preflight
            self.refuse_preflight = False
            try:
                self.runtime.refresh_directory()
            finally:
                self.refuse_preflight = refusal
            directory = self.runtime.ledger.directory
            if not directory.matches(directory.epoch()):
                raise AssertionError('Directory was not current before deciding.')
            result = original_decide(request_id, decision)
            if directory.matches(directory.epoch()):
                raise AssertionError('Real decide_ask did not stale the directory.')
            self.stale_decisions.append(request_id)
            return result

        self.stack.enter_context(patch.object(self.store, 'decide_ask', side_effect=decide))
        original_step = self.runtime.ledger.directory.rebuild_step

        def rebuild():
            if self.refuse_preflight:
                raise LedgerError('fictional_preflight_refusal')
            return original_step()

        self.stack.enter_context(patch.object(self.runtime.ledger.directory, 'rebuild_step', side_effect=rebuild))
        real_ask = server.officer_ask
        self.stack.enter_context(patch.object(server, 'officer_ask', side_effect=lambda turns, **kw:
                                             real_ask(turns, call=self.model, **kw)))
        # Any accidental fallback must fail locally instead of calling an API.
        self.stack.enter_context(patch.object(server, 'anthropic_request', side_effect=AssertionError('Unexpected model network call.')))
        fixture = self

        class Handler(server.H):
            def do_POST(self):
                if self.path.startswith('/fixture/'):
                    if not self.keyed(self.headers.get('X-Live-Room-Key', '')):
                        return self.send(403, b'forbidden')
                    body = self.body()
                    if self.path == '/fixture/fault' and body in ({'refuse': True}, {'refuse': False}):
                        fixture.refuse_preflight = body['refuse']
                        fixture.runtime.ledger.directory.invalidate()
                        return self.send(200, b'{"ok":true}', 'application/json')
                    if self.path == '/fixture/fault' and body == {'lost_commit': True}:
                        fixture.store.db.lose_request_commit = True
                        return self.send(200, b'{"ok":true}', 'application/json')
                    if self.path == '/fixture/state' and body == {}:
                        return self.send(200, json.dumps(fixture.state()).encode(), 'application/json')
                    return self.send(400, b'{"error":"Invalid fixture control."}', 'application/json')
                return super().do_POST()

        self.httpd = server.Server(('127.0.0.1', 0), Handler)
        self.stack.callback(self.httpd.server_close)
        self.stack.enter_context(patch.object(server, 'PORT', self.httpd.server_port))
        self.port = self.httpd.server_port
        self.thread = threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=.01), daemon=True)
        self.thread.start()
        self.stack.callback(self._stop_http)
        return self

    def _stop_http(self):
        self.httpd.shutdown()
        self.thread.join(5)
        if self.thread.is_alive():
            raise AssertionError('Fixture HTTP server did not stop.')

    def model(self, body, **kwargs):
        self.model_calls += 1
        text = body['messages'][-1]['content']
        if 'Atlas' not in text:
            raise AssertionError('Fixture model only handles explicit fictional Atlas requests.')
        question = text.partition(':')[2].strip() or 'Report fictional progress.'
        return {'content': [{'type': 'tool_use', 'name': 'relay_question',
                             'input': {'agent': 'Atlas', 'question': question}}]}, ''

    def state(self):
        with self.store.lock:
            rows = self.store.snapshot()['requests']
            return dict(model_calls=self.model_calls, stale_decisions=self.stale_decisions,
                        request_insertions=list(self.store.db.request_insertions), requests=rows,
                        bound_runtime=self.store._native_owner_runtime is self.runtime)

    def request(self, route, body):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            conn.request('POST', route, json.dumps(body), {'Content-Type': 'application/json',
                                                        'X-Live-Room-Key': OWNER_KEY})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def __exit__(self, *args):
        return self.stack.__exit__(*args)


if __name__ == '__main__':
    with DeliveryFixture() as fixture:
        print(json.dumps(dict(port=fixture.port, owner_key=OWNER_KEY, native=NATIVE,
                              project=fixture.project)), flush=True)
        for _ in sys.stdin:
            pass
