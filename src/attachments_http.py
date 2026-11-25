"""Strict framed owner HTTP boundary. No lazy initialization or provider calls."""
from contextlib import ExitStack
from datetime import datetime, timezone
import hmac
import json
import math
import re
import time
import urllib.parse

from attachments_types import AttachmentError, TOKEN, fail, integer

PREFIX = '/api/attachments'
ID = r'([0-9a-f]{32})'
NATIVE_RECEIPT = r'/native/receipts/(draft_create|upload_reserve|draft_discard|send_prepare)'
NATIVE_PREPARE = r'/native/drafts/' + ID + r'/send-intent'
NATIVE_REQUEST = r'/native/requests/' + ID
NATIVE_SEND = NATIVE_REQUEST + r'/send'
SENSITIVE = ('X-Attachment-Draft-Capability', 'X-Attachment-Writer-Nonce',
             'X-Attachment-Writer-Generation', 'X-Attachment-Draft-Revision',
             'X-Attachment-Upload-Fingerprint', 'Idempotency-Key', 'X-Attachment-Source-Capability',
             'X-Attachment-Request-Capability', 'X-Attachment-Deletion-Capability')
ROUTES = ((r'/capabilities', ('GET',)), (r'/conversations', ('POST',)),
          (r'/native/selection', ('GET',)), (NATIVE_RECEIPT, ('GET',)),
          (NATIVE_PREPARE, ('POST',)), (NATIVE_SEND, ('POST',)),
          (r'/native/requests', ('GET',)), (NATIVE_REQUEST, ('GET',)),
          (r'/conversations/current', ('GET', 'POST')),
          (r'/drafts', ('GET', 'POST')), (r'/storage', ('GET',)),
          (r'/files', ('GET',)), (r'/storage/files', ('GET',)),
          (r'/files/' + ID + r'/preview', ('GET',)),
          (r'/files/' + ID + r'/deletion-intent', ('POST',)),
          (r'/deletions/' + ID, ('GET',)),
          (r'/deletions/' + ID + r'/confirm', ('POST',)),
          (r'/conversations/' + ID + r'/management', ('GET',)),
          (r'/requests/' + ID + r'/recover', ('POST',)),
          (r'/requests/' + ID, ('GET',)), (r'/requests/' + ID + r'/send', ('POST',)),
          (r'/requests/' + ID + r'/ask', ('POST',)),
          (r'/requests/' + ID + r'/abandon', ('POST',)),
          (r'/answers/' + ID, ('GET',)),
          (r'/conversations/' + ID + r'/ask', ('POST',)),
          (r'/conversations/' + ID + r'/question-intent', ('POST',)),
          (r'/conversations/' + ID + r'/requests/by-prepare', ('GET',)),
          (r'/conversations/' + ID + r'/requests', ('GET',)),
          (r'/drafts/' + ID, ('GET', 'PATCH', 'DELETE')),
          (r'/drafts/' + ID + r'/(recover|resume|upload-intents|send-intent)', ('POST',)),
          (r'/drafts/' + ID + r'/uploads/' + ID, ('GET', 'PUT')),
          (r'/drafts/' + ID + r'/files/' + ID + r'/retry', ('POST',)),
          (r'/drafts/' + ID + r'/files/' + ID, ('DELETE',)))


class LimitedReader:
    def __init__(self, handler, length, seconds, monotonic=time.monotonic):
        self.handler, self.remaining, self.monotonic = handler, length, monotonic
        self.deadline = monotonic() + seconds

    def read(self, size):
        if not self.remaining:
            return b''
        remaining_time = self.deadline - self.monotonic()
        if remaining_time <= 0:
            fail('upload_timeout', 'The attachment request timed out.', 422)
        self.handler.connection.settimeout(remaining_time)
        try:
            chunk = self.handler.rfile.read1(min(size, self.remaining, 65536))
        except (TimeoutError, OSError):
            fail('upload_timeout', 'The attachment request timed out.', 422)
        if self.monotonic() > self.deadline:
            fail('upload_timeout', 'The attachment request timed out.', 422)
        if not isinstance(chunk, bytes) or len(chunk) > min(size, self.remaining, 65536):
            fail('invalid_request', 'Invalid attachment request body.', 400)
        self.remaining -= len(chunk)
        return chunk


def handle_http(handler, service, owner_key, port, *, native=None,
                unavailable=('attachments_disabled', 'Attachments are unavailable.'), native_unavailable=None,
                native_refusal=None):
    """``unavailable`` is the (code, words) every request gets while ``service`` is None: why the store is not running.
    ``native_unavailable`` is the (code, words) requests for files sent to agent chats get while the store runs but
    this host cannot deliver them (``native`` is None there). ``native_refusal(destination_id)`` gives (code, words)
    when this host cannot deliver to that one chat (on Windows a Codex chat, or Git for Windows missing), else None;
    it answers only a selection with nothing to show (no current or retained context), so receipts, status, history
    and recovery of earlier sends stay readable, as where delivery runs."""
    handler.close_connection = True
    def headers(name):
        return handler.headers.get_all(name, [])
    def one(name, required=True):
        values = headers(name)
        if (required and len(values) != 1) or (not required and len(values) > 1):
            fail('invalid_request', 'Invalid attachment request headers.', 400)
        return values[0] if values else None
    def reply(status, value, allow=None):
        envelope = dict(value, schemaVersion=1, fixture=False,
                        serverTime=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'))
        body = json.dumps(envelope, allow_nan=False).encode('utf-8')
        if len(body) > 262144:
            fail('response_limit', 'Attachment response exceeds its safe limit.', 503)
        handler.connection.settimeout(2)
        extra = [('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff'), ('Referrer-Policy', 'no-referrer'), ('Connection', 'close')]
        if allow:
            extra.append(('Allow', ', '.join(allow)))
        try:
            return handler.send(status, body, 'application/json; charset=utf-8', extra=extra)
        except (OSError, TimeoutError):
            return None
    def error(status, code, message, allow=None):
        return reply(status, {'error': {'code': code, 'message': message, 'retryable': False}}, allow)
    def length(maximum):
        value = one('Content-Length')
        if headers('Transfer-Encoding') or not re.fullmatch(r'[0-9]{1,8}', value):
            fail('invalid_request', 'Use a bounded Content-Length.', 400)
        if int(value) > maximum:
            fail('body_too_large', 'The attachment request is too large.', 413)
        return int(value)
    def json_body():
        count = length(65536)
        if one('Content-Type').lower().replace(' ', '') not in ('application/json', 'application/json;charset=utf-8'):
            fail('unsupported_media', 'Use an application/json request body.', 415)
        reader, raw = LimitedReader(handler, count, 2), bytearray()
        while reader.remaining:
            chunk = reader.read(reader.remaining)
            if not chunk:
                fail('invalid_request', 'Incomplete attachment request body.', 400)
            raw.extend(chunk)
        def pairs(items):
            out = {}
            for k, v in items:
                if k in out:
                    raise ValueError()
                out[k] = v
            return out
        def reject(_):
            raise ValueError()
        def finite(value):
            out = float(value)
            if not math.isfinite(out):
                raise ValueError()
            return out
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=reject, parse_float=finite)
        def unicode_valid(value):
            if isinstance(value, str):
                value.encode('utf-8', 'strict')
            elif isinstance(value, dict):
                for k, v in value.items():
                    unicode_valid(k); unicode_valid(v)
            elif isinstance(value, list):
                for v in value:
                    unicode_valid(v)
        unicode_valid(data)
        if type(data) is not dict:
            fail('invalid_request', 'Use an attachment request object.', 400)
        return data
    try:
        local = ['127.0.0.1:%d' % port, 'localhost:%d' % port]
        hosts, origins, keys = headers('Host'), headers('Origin'), headers('X-Live-Room-Key')
        if len(keys) != 1 or type(owner_key) is not str or not owner_key or not hmac.compare_digest(keys[0].encode(), owner_key.encode()):
            return error(401, 'unauthorized', 'Owner authentication is required.')
        if len(hosts) != 1 or hosts[0] not in local or len(origins) > 1 or origins and origins[0] not in ['http://' + h for h in local]:
            return error(403, 'forbidden', 'This attachment origin is not authorized.')
        for name in SENSITIVE:
            one(name, required=False)
        if service is None:
            return error(503, *unavailable)
        parsed = urllib.parse.urlsplit(handler.path)
        if (parsed.scheme or parsed.netloc or parsed.fragment or '#' in handler.path or '%' in parsed.path
                or not parsed.path.startswith(PREFIX + '/') or re.search(r'%(?![0-9a-fA-F]{2})', parsed.query)):
            fail('invalid_request', 'Invalid attachment route.', 400)
        route = parsed.path[len(PREFIX):]
        query = {}
        # Python 3.9's strict parser rejects an empty query string; later versions return no fields.
        for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, encoding='utf-8', errors='strict', max_num_fields=5) if parsed.query else ():
            if k in query:
                fail('invalid_request', 'Duplicate attachment query field.', 400)
            query[k] = v
        matched = next(((re.fullmatch(pattern, route), methods) for pattern, methods in ROUTES if re.fullmatch(pattern, route)), None)
        if matched is None:
            if re.match(r'/(requests|files|answers|expiry)(/|$)', route) or route.endswith('/send'):
                fail('unavailable', 'This attachment operation is not implemented.', 503)
            fail('unknown_route', 'Unknown attachment route.', 404)
        match, methods = matched
        if handler.command not in methods:
            return error(405, 'method_not_allowed', 'Unsupported attachment method.', methods)
        expected = {'destinationId', 'scopeId', 'modelId'} if route == '/capabilities' else {'destinationId', 'scopeId'} if route == '/drafts' and handler.command == 'GET' else set()
        required = expected
        native_receipt = re.fullmatch(NATIVE_RECEIPT, route)
        native_request = route == '/native/requests' or re.fullmatch(NATIVE_REQUEST, route)
        native_send, native_prepare = re.fullmatch(NATIVE_SEND, route), re.fullmatch(NATIVE_PREPARE, route)
        if route == '/native/selection' or native_receipt or native_request:
            if (any(name.lower().startswith('x-attachment-') for name in handler.headers)
                    or headers('Idempotency-Key') or headers('Range') or headers('Content-Range')):
                fail('invalid_request', 'Invalid native attachment read headers.', 400)
            required = {'destinationId'}
            if native_receipt:
                required |= {'scopeId', 'commandDigest'}
                if native_receipt[1] != 'draft_create':
                    required.add('draftId')
            if native_request:
                required |= {'scopeId'}
            expected = required | ({'cursor'} if route == '/native/requests' else set())
        if route == '/conversations/current' and handler.command == 'GET':
            required = expected = {'scopeId'}
        if route == '/drafts' and handler.command == 'GET':
            expected = expected | {'cursor'}
        if route == '/files':
            required = {'destinationId', 'scopeId', 'conversationId'}
            expected = required | {'cursor','requestId'}
        elif route == '/storage/files':
            required, expected = set(), {'cursor'}
        elif re.fullmatch(r'/files/' + ID + r'/preview', route):
            required, expected = set(), {'location'}
        if re.fullmatch(r'/answers/'+ID,route):
            required=expected={'requestId'}
        elif re.fullmatch(r'/conversations/'+ID+r'/requests',route):
            required={'destinationId','scopeId'};expected=required|{'cursor'}
        elif re.fullmatch(r'/conversations/'+ID+r'/requests/by-prepare',route):
            required=expected={'destinationId','scopeId','prepareDigest'}
        elif re.fullmatch(r'/conversations/'+ID+r'/management',route):
            required={'destinationId','scopeId'};expected=required|{'cursor'}
        if set(query) - expected or not required <= set(query):
            fail('invalid_request', 'Invalid attachment query fields.', 400)
        if handler.command == 'GET':
            lengths = headers('Content-Length')
            if headers('Transfer-Encoding') or len(lengths) > 1 or lengths and lengths != ['0']:
                fail('invalid_request', 'GET requests cannot contain a body.', 400)
        if native_send or native_prepare:
            expected_capability = 'x-attachment-request-capability' if native_send else 'x-attachment-draft-capability'
            capabilities = {name.lower() for name in handler.headers if name.lower().startswith('x-attachment-')}
            if capabilities != {expected_capability} or headers('Range') or headers('Content-Range'):
                fail('invalid_request', 'Invalid native attachment Send headers.', 400)
        if (native is None and native_unavailable is not None
                and (route == '/native/selection' or native_receipt or native_request or native_send or native_prepare)):
            return error(503, *native_unavailable)
        if native_request or native_send or native_prepare or native_receipt and native_receipt[1] == 'send_prepare':
            if native is None:
                fail('attachments_disabled', 'Native agent attachments are unavailable.', 503)
            with native.operation():
                if native_prepare:
                    return reply(201, native.prepare(native_prepare[1], one('X-Attachment-Draft-Capability'),
                                                     json_body(), one('Idempotency-Key')))
                if native_send:
                    return reply(200, native.send(native_send[1], one('X-Attachment-Request-Capability'),
                                                  json_body(), one('Idempotency-Key')))
                selected = query['destinationId'], query['scopeId']
                if native_receipt:
                    return reply(200, native.receipt(*selected, query['draftId'], query['commandDigest']))
                if route == '/native/requests':
                    return reply(200, native.history(*selected, query.get('cursor')))
                return reply(200, native.status(native_request[1], *selected))
        if native is not None and (route == '/native/selection' or native_receipt
                                   or route in ('/capabilities', '/drafts') and handler.command == 'GET'
                                   and query['destinationId'].startswith(('c:', 'x:'))):
            with native.operation():
                destination_id = query['destinationId']
                if route == '/native/selection':
                    selection = native.native_selection(destination_id)
                    if selection.get('context') is None and native_refusal is not None:
                        refused = native_refusal(destination_id)
                        if refused is not None:
                            return error(503, *refused)
                    return reply(200, selection)
                reads, _, _ = native.scoped_reads(destination_id, query['scopeId'])
                if native_receipt:
                    return reply(200, reads.native_receipt(native_receipt[1], destination_id, query['scopeId'],
                                                          query['commandDigest'], query.get('draftId')))
                if route == '/capabilities':
                    return reply(200, reads.capabilities(destination_id, query['scopeId'], query['modelId'] or None))
                return reply(200, reads.list_drafts(destination_id=destination_id, scope_id=query['scopeId'],
                                                  cursor=query.get('cursor')))
        with ExitStack() as scope:
            scope.enter_context(service.operation())
            if native is not None and route.startswith('/drafts/') and not route.endswith('/send-intent'):
                # Classify from the retained draft, never from requester-chosen headers.
                draft_id = route.split('/')[2]
                if native.draft_target(draft_id)['kind'] == 'agent':
                    scope.enter_context(native.operation())
                    service = native.native_draft(draft_id)
            if route == '/native/selection':
                return reply(200, service.native_selection(query['destinationId']))
            if native_receipt:
                return reply(200, service.native_receipt(native_receipt[1], query['destinationId'], query['scopeId'],
                                                         query['commandDigest'], query.get('draftId')))
            if re.fullmatch(r'/conversations/'+ID+r'/management',route):
                return reply(200,service.management(route.split('/')[2],query['destinationId'],query['scopeId'],query.get('cursor')))
            if re.fullmatch(r'/files/'+ID+r'/deletion-intent',route):
                return reply(201,service.deletion_prepare(route.split('/')[2],json_body(),one('Idempotency-Key')))
            if re.fullmatch(r'/deletions/'+ID+r'(/confirm)?',route):
                operation_id=route.split('/')[2]
                cap=one('X-Attachment-Deletion-Capability')
                if route.endswith('/confirm'):
                    return reply(200,service.deletion_confirm(operation_id,cap,json_body(),one('Idempotency-Key')))
                return reply(200,service.deletion_status(operation_id,cap))
            if route == '/conversations/current':
                if handler.command == 'GET':
                    return reply(200,service.current_conversation(query['scopeId']))
                from attachments_service import closed
                data=closed(json_body(),{'scopeId'})
                return reply(200,service.current_conversation(data['scopeId'],create=True))
            if re.fullmatch(r'/requests/'+ID+r'(/(recover|send|ask|abandon))?',route) or re.fullmatch(r'/answers/'+ID,route) or re.fullmatch(r'/conversations/'+ID+r'/(ask|requests(/by-prepare)?|question-intent)',route) or route.endswith('/send-intent'):
                if not hasattr(service,'documents'):
                    fail('unavailable','Motet document requests are unavailable.',503)
                ids=route.strip('/').split('/')
                if ids[0]=='answers':
                    return service.document_answer(ids[1],query['requestId'],one('X-Attachment-Request-Capability'),lambda value:reply(200,value))
                if ids[0]=='conversations':
                    if len(ids)==4:
                        return reply(200,service.document_by_prepare(ids[1],query['destinationId'],query['scopeId'],query['prepareDigest']))
                    if ids[2]=='requests':return reply(200,service.document_history(ids[1],query['destinationId'],query['scopeId'],query.get('cursor')))
                    return reply(200,service.document_ask(ids[1],json_body(),one('Idempotency-Key'),prepare_only=ids[2]=='question-intent'))
                if ids[0]=='drafts':
                    return reply(201,service.document_prepare(ids[1],one('X-Attachment-Draft-Capability'),json_body(),one('Idempotency-Key')))
                if len(ids)==2:
                    return reply(200,service.document_status(ids[1],one('X-Attachment-Request-Capability')))
                if ids[2]=='recover':return reply(200,service.document_recover(ids[1],json_body()))
                if ids[2]=='ask':return reply(200,service.document_send_question(ids[1],one('X-Attachment-Request-Capability'),json_body(),one('Idempotency-Key')))
                if ids[2]=='abandon':return reply(200,service.document_abandon(ids[1],one('X-Attachment-Request-Capability'),json_body(),one('Idempotency-Key')))
                return reply(200,service.document_send(ids[1],one('X-Attachment-Request-Capability'),one('X-Attachment-Draft-Capability'),json_body(),one('Idempotency-Key')))
            if route == '/capabilities':
                return reply(200, service.capabilities(query['destinationId'], query['scopeId'], query['modelId'] or None))
            if route == '/files':
                options={'cursor':query.get('cursor')}
                if 'requestId' in query:
                    if not hasattr(service,'documents') or not query['conversationId']:
                        fail('invalid_request','Choose a Motet document request.',400)
                    options['request_id']=query['requestId']
                return reply(200, service.history(query['destinationId'], query['scopeId'], query['conversationId'] or None, **options))
            if route == '/storage/files':
                return reply(200, service.storage_files(cursor=query.get('cursor')))
            if re.fullmatch(r'/files/' + ID + r'/preview', route):
                cap = one('X-Attachment-Source-Capability')
                location = None
                if 'location' in query:
                    # Compact closed grammar rather than unbounded JSON in a URL.
                    match_location = re.fullmatch(r'text_lines:([1-9][0-9]{0,5}):([1-9][0-9]{0,2})', query['location'])
                    if match_location:
                        location = {'kind': 'text_lines', 'start': int(match_location[1]), 'count': int(match_location[2])}
                    elif re.fullmatch(r'pdf_page:([1-9][0-9]{0,4}|100000)',query['location']):
                        location={'page':int(query['location'].split(':')[1])}
                    elif query['location']=='whole_image':location={'image':True}
                    else:fail('invalid_location','Choose a bounded source citation location.',400)
                return service.preview(route.split('/')[2], cap, location, lambda value: reply(200, value))
            if re.fullmatch(r'/requests/' + ID + r'/recover', route):
                fail('unavailable', 'Exact attachment request recovery is not implemented.', 503)
            if route == '/storage':
                return reply(200, service.store.storage())
            if route == '/conversations':
                command_key = one('Idempotency-Key')
                return reply(201, service.create_conversation(json_body(), command_key))
            if route == '/drafts':
                if handler.command == 'GET':
                    return reply(200, service.list_drafts(destination_id=query['destinationId'], scope_id=query['scopeId'], cursor=query.get('cursor')))
                command_key = one('Idempotency-Key')
                data = json_body()
                if native is not None and isinstance(data.get('destinationId'), str) and data['destinationId'].startswith(('c:', 'x:')):
                    scope.enter_context(native.operation())
                    reads, _, _ = native.scoped_reads(data['destinationId'], data.get('scopeId'))
                    return reply(201, reads.create_draft(data, command_key))
                return reply(201, service.create_draft(data, command_key))
            parts = route.strip('/').split('/')
            did = parts[1]
            if len(parts) == 3 and parts[2] == 'recover':
                return reply(200, service.recover(did, json_body()))
            capability = one('X-Attachment-Draft-Capability')
            if len(parts) == 2:
                if handler.command == 'GET':
                    return reply(200, service.get_draft(did, capability))
                command_key = one('Idempotency-Key') if handler.command == 'DELETE' else None
                return reply(200, service.mutate('discard' if handler.command == 'DELETE' else 'edit', did, capability, json_body(), command_key=command_key))
            if parts[2] == 'resume':
                return reply(200, service.resume(did, capability, json_body()))
            if parts[2] == 'upload-intents':
                command_key = one('Idempotency-Key')
                return reply(201, service.mutate('reserve', did, capability, json_body(), command_key=command_key))
            if parts[2] == 'files':
                return reply(200, service.mutate('retry' if len(parts) == 5 else 'remove', did, capability, json_body(), file_id=parts[3]))
            uid = parts[3]
            if handler.command == 'GET':
                return reply(200, service.get_upload(did, capability, uid))
            if headers('Range') or headers('Content-Range'):
                fail('invalid_request', 'Partial attachment uploads are not supported.', 400)
            count = length(10 * 1024 * 1024)
            if one('Content-Type') != 'application/octet-stream':
                fail('unsupported_media', 'Use an application/octet-stream upload.', 415)
            nonce, generation, revision, fingerprint = [one(n) for n in SENSITIVE[1:5]]
            if not re.fullmatch(r'[1-9][0-9]{0,15}', generation) or not re.fullmatch(r'[1-9][0-9]{0,15}', revision):
                fail('invalid_request', 'Invalid attachment writer fence.', 400)
            if not integer(int(generation), 1) or not integer(int(revision), 1) or not TOKEN.fullmatch(nonce) or not TOKEN.fullmatch(fingerprint):
                fail('invalid_request', 'Invalid attachment writer fence.', 400)
            info = service.upload_transport_info(did, capability, uid)
            if count != info['bytes'] or fingerprint != info['fingerprint']:
                fail('request_conflict', 'The upload does not match its reservation.')
            fence = {'revision': int(revision), 'writerGeneration': int(generation), 'writerNonce': nonce}
            return reply(200, service.receive_upload(did, capability, fence, uid, fingerprint, LimitedReader(handler, count, 30)))
    except AttachmentError as exc:
        return error(exc.status, exc.code, exc.message)
    except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError):
        return error(400, 'invalid_request', 'Invalid attachment request.')
    except Exception:
        return error(503, 'attachments_unavailable', 'Attachment storage is unavailable.')
