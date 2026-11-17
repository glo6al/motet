"""Durable read-only Motet records, sharing Deck Log's private SQLite lifecycle.

No model, transport, filesystem lookup, or executable receipt is exposed here.
"""
import hashlib
import hmac
import json
import math
import re
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta

ID = re.compile(r'[0-9a-f]{64}\Z')
STATES = frozenset(('pending', 'answered', 'failed', 'rejected', 'queued', 'delivered',
                    'delivery_unknown', 'awaiting_reply', 'expired', 'acknowledged',
                    'interrupted', 'no_op', 'suggested'))
MAX_ENTRIES, MAX_SEEN = 5000, 100000


def day_bounds(day):
    if not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
        raise ValueError('Invalid UTC day.')
    try:
        start = datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        return start.timestamp(), end.timestamp()
    except (ValueError, OverflowError, OSError):
        raise ValueError('Invalid UTC day.') from None


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')


class OfficerHistoryMixin:
    def _officer_schema(self):
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS officer_entries (
          id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL,
          started REAL NOT NULL, updated REAL NOT NULL, epoch TEXT NOT NULL,
          generation INTEGER NOT NULL, payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS officer_started ON officer_entries(started,id);
        CREATE TABLE IF NOT EXISTS officer_seen (
          request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS officer_gaps (
          id INTEGER PRIMARY KEY, started REAL NOT NULL, ended REAL NOT NULL, reason TEXT NOT NULL);
        ''')
        self.db.execute("UPDATE officer_entries SET epoch='' WHERE epoch<>''")
        self._officer_gap('watch_started_or_restarted')
        # In-flight requests are observations with unknown completion after restart.
        for row in self.db.execute('SELECT id,payload FROM officer_entries').fetchall():
            data = json.loads(row['payload'])
            for action in data.get('actions', []):
                action['observation_status'] = 'before_restart'
            data['restart_qualification'] = 'Earlier outcome observations are not current delivery or completion verification.'
            if data.get('state') == 'pending':
                data.update(state='delivery_unknown', qualification='Motet restarted before completion was recorded.')
            self.db.execute('UPDATE officer_entries SET payload=? WHERE id=?', (json.dumps(data), row['id']))

    def _officer_gap(self, reason, start=None, end=None):
        now = self.clock()
        self.db.execute('INSERT INTO officer_gaps(started,ended,reason) VALUES (?,?,?)',
                        (now if start is None else start, now if end is None else end, reason))
        self.db.execute('DELETE FROM officer_gaps WHERE id NOT IN (SELECT id FROM officer_gaps ORDER BY id DESC LIMIT 100)')

    def _officer_erase(self, reason):
        row = self.db.execute('SELECT MIN(started),MAX(updated) FROM officer_entries').fetchone()
        if row and row[0] is not None:
            self._officer_gap(reason, row[0], max(row[1], self.clock()))
        self.db.execute('DELETE FROM officer_entries')

    def _officer_cleanup_pending(self):
        row = self.db.execute("SELECT value FROM settings WHERE key='officer_cleanup_pending'").fetchone() if self.db else None
        return bool(row and json.loads(row[0]))

    def _officer_prose_readable(self):
        return bool(not self.settings['excluded_chats'] and not self.settings['excluded_projects'] and not self._officer_cleanup_pending())

    def _officer_allowed(self):
        return bool(self.active and self.available and not self.closed and not self.reason and
                    self.settings['enabled'] and not self.settings['paused'] and
                    self._officer_prose_readable())

    def officer_begin(self, request_id, question, scope, *, fingerprint_text=None):
        """Claim once; returned duplicate records never execute or authorize anything."""
        from deck_log import _prose
        if not isinstance(request_id, str):
            raise ValueError('Invalid request identifier.')
        try:
            if str(uuid.UUID(request_id)) != request_id:
                raise ValueError()
        except (ValueError, AttributeError):
            raise ValueError('Invalid request identifier.') from None
        if not isinstance(question, str) or not question or len(question) > 16384:
            raise ValueError('Invalid Motet question.')
        if not isinstance(scope, dict) or scope.get('scope') not in ('room', 'station'):
            raise ValueError('Invalid Motet scope.')
        safe_scope = {'scope': scope['scope']}
        if scope['scope'] == 'station':
            agent = scope.get('agent')
            if not isinstance(agent, str) or not re.fullmatch(r'[xckaep]:[A-Za-z0-9_.:-]{1,196}', agent):
                raise ValueError('Invalid Motet station.')
            safe_scope['agent'] = agent
        raw = json.dumps([fingerprint_text if fingerprint_text is not None else question, safe_scope], ensure_ascii=True, sort_keys=True).encode()
        fingerprint = hmac.new(self.key, raw, hashlib.sha256).hexdigest()
        with self.lock:
            if self.db is None or not self.available:
                return dict(status='not_recorded', reason='storage_unavailable')
            self._verify()
            self._purge()
            old = self.db.execute('SELECT fingerprint FROM officer_seen WHERE request_id=?', (request_id,)).fetchone()
            if old:
                if old[0] != fingerprint:
                    return dict(status='conflict')
                row = self.db.execute('SELECT * FROM officer_entries WHERE request_id=?', (request_id,)).fetchone()
                return dict(status='duplicate', entry=self._officer_entry(row) if row and self._officer_prose_readable() else None)
            if not self._officer_allowed():
                return dict(status='not_recorded', reason='excluded_scope' if self.settings['excluded_chats'] or self.settings['excluded_projects'] else 'recording_off_or_paused')
            self._purge()
            if self.db.execute('SELECT COUNT(*) FROM officer_seen').fetchone()[0] >= MAX_SEEN or self.db.execute('SELECT COUNT(*) FROM officer_entries').fetchone()[0] >= MAX_ENTRIES:
                self._officer_gap('officer_history_full'); self.db.commit()
                return dict(status='unavailable', reason='officer_history_full')
            now = self.clock()
            clean, redacted = _prose(question, 4096)
            generation = json.loads(self.db.execute("SELECT value FROM settings WHERE key='erasure_generation'").fetchone()[0])
            ident = hashlib.sha256(('officer:' + request_id).encode()).hexdigest()
            payload = dict(question=clean, answer=None, state='pending', scope=safe_scope,
                           selected_model=None, actual_model=None, actions=[], sources=[],
                           redacted=redacted, qualification='Recorded request; completion is not yet known.')
            with self.db:
                self.db.execute('INSERT INTO officer_seen VALUES (?,?)', (request_id, fingerprint))
                self.db.execute('INSERT INTO officer_entries VALUES (?,?,?,?,?,?,?,?)',
                                (ident, request_id, fingerprint, now, now, self.epoch, generation, json.dumps(payload)))
                if not self.db.execute("SELECT 1 FROM settings WHERE key='officer_history_started'").fetchone():
                    self._save_setting('officer_history_started', now)
            return dict(status='recorded', id=ident, request_id=request_id, epoch=self.epoch, generation=generation)

    @staticmethod
    def _officer_actions(result):
        out = []
        for kind, field in (('message', 'messages'), ('control', 'controls'), ('screen_suggestion', 'actions')):
            items = result.get(field, [])
            for item in items[:32] if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                state = item.get('state', 'suggested' if kind == 'screen_suggestion' else 'delivery_unknown')
                state = {'sending': 'delivery_unknown', 'unknown': 'delivery_unknown', 'answered': 'answered',
                         'quarantined': 'delivery_unknown'}.get(state, state)
                if state not in STATES:
                    state = 'delivery_unknown'
                ident = item.get('request_id')
                try:
                    ident = ident if isinstance(ident, str) and str(uuid.UUID(ident)) == ident else None
                except ValueError:
                    ident = None
                agent = item.get('agent')
                agent = agent if isinstance(agent, str) and re.fullmatch(r'[xckaep]:[A-Za-z0-9_.:-]{1,196}', agent) else None
                action = item.get('action') if item.get('action') in ('steer', 'interrupt', 'queue') else None
                out.append(dict(kind=kind, request_id=ident, agent=agent, action=action, outcome=state,
                                qualification='Historical record only. Queue acceptance and acknowledgement do not establish that work was completed.'))
        return out[:32]

    def officer_finish(self, token, result, actual_model=None):
        from deck_log import _prose, _text
        if token.get('status') != 'recorded' or not isinstance(result, dict):
            return False
        with self.lock:
            if not self._officer_allowed() or token['epoch'] != self.epoch:
                return False
            self._verify()
            row = self.db.execute('SELECT * FROM officer_entries WHERE id=? AND generation=?', (token['id'], token['generation'])).fetchone()
            if row is None:
                return False
            data = json.loads(row['payload'])
            text = result.get('text') or result.get('error') or ''
            data['answer'], changed = _prose(text, 8192)
            data['redacted'] |= changed
            prior_actions = data.get('actions', [])
            data.update(state='failed' if result.get('error') else 'answered', selected_model=_text(result.get('model'), 120) or None,
                        actual_model=_text(actual_model, 120) or None, actions=self._officer_actions(result),
                        qualification='Historical untrusted text. Evidence and outcomes may be partial; no permission or executable actions are retained.')
            for item in data['actions']:
                item['observed_at'] = stamp(self.clock())
            if prior_actions:
                data['actions'] = prior_actions
            data['partial'] = result.get('partial') is True
            data['warnings'] = [_prose(value, 512)[0] for value in result.get('warnings', [])[:8] if isinstance(value, str)] if isinstance(result.get('warnings'), list) else []
            usage = result.get('usage_tracking', {})
            data['usage_tracking'] = {key: usage[key] for key in ('enabled', 'tracking_incomplete', 'recorded_calls') if isinstance(usage, dict) and key in usage and type(usage[key]) in (bool, int)}
            # Only source identities and fingerprints, never signed receipt or source bodies.
            receipt = result.get('receipt', {})
            payload = receipt.get('payload', {}) if isinstance(receipt, dict) else {}
            evidence = payload.get('evidence', {}) if isinstance(payload, dict) else {}
            generation = payload.get('generation', {}) if isinstance(payload, dict) else {}
            refs = evidence.get('refs', []) if isinstance(evidence, dict) else []
            data['sources'] = [dict(ref=ref, status='historical_unverified') for ref in refs[:100]
                               if isinstance(ref, str) and (re.fullmatch(r'deck:[0-9a-f]{64}', ref) or re.fullmatch(r'(?:live|fact):[A-Za-z0-9_.:-]{1,400}', ref))]
            proofs = evidence.get('fingerprints', {}) if isinstance(evidence, dict) else {}
            for source in data['sources']:
                value = proofs.get(source['ref']) if isinstance(proofs, dict) else None
                source['fingerprint'] = value if isinstance(value, str) and ID.fullmatch(value) else None
            observed = evidence.get('observed_at') if isinstance(evidence, dict) else None
            data['evidence_observed_at'] = stamp(observed) if type(observed) in (int, float) and math.isfinite(observed) and 0 < observed < 253402214400 else None
            data['omitted_source_refs'] = max(0, len(refs) - len(data['sources'])) if isinstance(refs, list) else None
            data['source_store'] = generation.get('store') if isinstance(generation, dict) and ID.fullmatch(str(generation.get('store', ''))) else None
            for source in data['sources']:
                source.update(source_at=None, observed_at=None, certainty='unknown')
                if source['ref'].startswith('deck:'):
                    current = self.evidence_current([source['ref'][5:]], data['source_store'])
                    record = current.get('records', {}).get(source['ref'][5:])
                    if record:
                        for original, target in (('source_ts', 'source_at'), ('observed_ts', 'observed_at')):
                            value = record.get(original)
                            if type(value) in (int, float) and math.isfinite(value) and 0 < value < 253402214400:
                                source[target] = stamp(value)
                        source['certainty'] = record.get('certainty') if record.get('certainty') in ('recorded', 'inferred', 'probably') else 'unknown'
            data['verification'] = result.get('verification') if result.get('verification') in ('supported', 'filtered', 'stale', 'unavailable') else 'not_recorded'
            with self.db:
                self.db.execute('UPDATE officer_entries SET updated=?,payload=? WHERE id=?', (self.clock(), json.dumps(data), token['id']))
            return True

    def officer_outcome(self, row, kind='message'):
        """Update only an already-recorded owner request. Never import old messages."""
        if not isinstance(row, dict) or row.get('origin') == 'peer':
            return
        with self.lock:
            if not self._officer_allowed() or self.db is None:
                return
            self._verify()
            found = self.db.execute('SELECT * FROM officer_entries WHERE request_id=?', (row.get('request_id'),)).fetchone()
            generation = json.loads(self.db.execute("SELECT value FROM settings WHERE key='erasure_generation'").fetchone()[0])
            if found is None or found['generation'] != generation:
                return
            data = json.loads(found['payload'])
            actions = self._officer_actions({'controls' if kind == 'control' else 'messages': [row]})
            previous = [{key: value for key, value in item.items()
                         if key not in ('observed_at', 'observation_status')}
                        for item in data.get('actions', [])]
            if actions == previous:
                return  # Queue writeback alone is not a fresh observation.
            now = self.clock()
            for item in actions:
                item['observed_at'] = stamp(now)
            data['actions'] = actions
            with self.db:
                self.db.execute('UPDATE officer_entries SET updated=?,payload=? WHERE id=?', (now, json.dumps(data), found['id']))

    def _officer_entry(self, row):
        if row is None:
            return None
        data = json.loads(row['payload'])
        for source in data.get('sources', []):
            if source['ref'].startswith('deck:'):
                current = self.evidence_current([source['ref'][5:]], data.get('source_store'))
                source['status'] = 'retained_identity_exists' if current.get('records') else 'removed' if current.get('available') else 'unavailable'
            else:
                source['status'] = 'stale'
        if any(source['status'] == 'removed' for source in data.get('sources', [])):
            data['answer'] = None
            data['verification'] = 'removed'
        data.pop('source_store', None)
        data.update(id=row['id'], request_id=row['request_id'], started_at=stamp(row['started']), updated_at=stamp(row['updated']),
                    historical=True, read_only=True, certainty='recorded',
                    evidence_qualification='Source identity checks do not verify the meaning of this earlier answer. Historical receipts are never current action proof.')
        return data

    def officer_read(self, *, day=None, limit=50, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Limit must be one to 100.')
        bounds = day_bounds(day) if day is not None else None
        if cursor is not None and (not isinstance(cursor, str) or not (ID.fullmatch(cursor) if bounds else re.fullmatch(r'\d{4}-\d{2}-\d{2}', cursor))):
            raise ValueError('Invalid Motet history cursor.')
        with self.lock:
            out = dict(timezone='UTC', day=day, entries=[] if bounds else None, days=[] if not bounds else None,
                       cursor=None, complete=False, status='unavailable', coverage=[],
                       qualification='Only retained recorded requests. Gaps and unrecorded periods are unknown, never zero activity.',
                       overview=dict(recorded_questions=None, daily_officer_estimated_usd=None, skills=None))
            if self.closed:
                return out   # shutting down, including after a close that timed out with the archive still open
            if self.available and self.db is None and self._fresh_storage():
                out['status'] = 'not_recorded'   # private storage Memory could open, and it has never created its archive
                return out
            if not self._readable():
                return out
            if not self._officer_prose_readable():
                out['status'] = 'unavailable' if self._officer_cleanup_pending() else 'not_recorded'
                out['reason'] = 'privacy_cleanup_pending' if self._officer_cleanup_pending() else 'excluded_scope'
                return out
            started = self.db.execute("SELECT value FROM settings WHERE key='officer_history_started'").fetchone()
            first = json.loads(started[0]) if started else None
            out['recording_started_at'] = stamp(first) if first else None
            out['coverage'] = [dict(r) for r in self.db.execute('SELECT started,ended,reason FROM officer_gaps ORDER BY id DESC LIMIT 100')]
            out['recording_now'] = self._officer_allowed()
            out['status'] = 'partial' if first else 'not_recorded'
            if bounds:
                start, end = bounds
                out.update(start_at=stamp(start), end_at=stamp(end))
                if end <= self.clock() - self.settings['retention_days'] * 86400:
                    out['status'] = 'expired'
                elif first is None or end <= first:
                    out['status'] = 'not_recorded'
                clauses, args = ['started>=?', 'started<?'], [start, end]
                if cursor:
                    found = self.db.execute('SELECT started FROM officer_entries WHERE id=? AND started>=? AND started<?', (cursor,start,end)).fetchone()
                    if found is None:
                        raise ValueError('Expired or mismatched Motet history cursor.')
                    clauses.append('(started<? OR (started=? AND id<?))'); args.extend((found[0],found[0],cursor))
                rows = self.db.execute('SELECT * FROM officer_entries WHERE '+' AND '.join(clauses)+' ORDER BY started DESC,id DESC LIMIT ?',args+[limit+1]).fetchall()
                out['entries'] = [self._officer_entry(row) for row in rows[:limit]]
                out['cursor'] = rows[limit-1]['id'] if len(rows)>limit else None
                out['page_complete'] = len(rows) <= limit
                out['overview']['recorded_questions'] = self.db.execute('SELECT COUNT(*) FROM officer_entries WHERE started>=? AND started<?',bounds).fetchone()[0] if rows else None
            else:
                if cursor:
                    day_bounds(cursor)
                rows = self.db.execute("SELECT strftime('%Y-%m-%d',started,'unixepoch') AS day,COUNT(*) AS recorded_questions FROM officer_entries GROUP BY day HAVING day<? ORDER BY day DESC LIMIT ?", (cursor or '9999-12-31',limit+1)).fetchall()
                out['days'] = [dict(row) for row in rows[:limit]]
                out['cursor'] = rows[limit-1]['day'] if len(rows)>limit else None
                out['page_complete'] = len(rows) <= limit
            return out
