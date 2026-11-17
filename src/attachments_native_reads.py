"""Read-stage native attachment service, with no production selection source."""
from types import MappingProxyType

from attachments_native_receipts import NativeReadReceipts, draft_binding
from attachments_relay import RELAY_FORMATS
from attachments_service import AttachmentService, SELECTION, UnqualifiedResolver, closed, opaque
from attachments_types import AttachmentError, ID, TOKEN, fail, filename, ident, integer


NATIVE_RECEIPT_KINDS = ('draft_create', 'upload_reserve', 'draft_discard')
LIMITS = {'fileBytes', 'draftBytes', 'fileCount'}
MEMBER_STATES = ('uploading', 'receiving', 'ready', 'failed')
CLEANUP_STATES = ('cleanup_pending', 'cleanup_failed', 'deleted')
FILE_ERRORS = (
    'upload_interrupted', 'upload_failed', 'upload_timeout', 'invalid_upload', 'invalid_request',
    'upload_size', 'upload_digest', 'source_changed', 'source_unavailable', 'draft_conflict',
    'type_signature_mismatch', 'file_too_large', 'preparation_unavailable', 'storage_unavailable',
    'clock_uncertain', 'identity_changed',
)


def unavailable(*args, **kwargs):
    fail('unavailable', 'This native attachment operation is unavailable.', 503)


def unclassified():
    fail('storage_unavailable', 'Native attachment file state could not be verified.', 503)


def relay_row(row, states):
    state, error = row['state'], row['error']
    needs_error = state in ('failed', 'cleanup_failed')
    if (row['preparationKind'] != 'original_relay' or row['previewAvailable'] is not False
            or row['expiresAt'] is not None or state not in states
            or error is not None and error not in FILE_ERRORS
            or state != 'cleanup_pending' and needs_error != (error is not None)):
        unclassified()
    return row


class _NativeReadResolver(UnqualifiedResolver):
    def __init__(self, store, selections):
        self.store = store
        entries = {}
        if type(selections) not in (list, tuple) or len(selections) > store.limits['contexts']:
            unavailable()
        public_limits = store.limits_view()
        for item in selections:
            mode = item.get('mode') if type(item) is dict else None
            fields = {'mode', 'destinationId', 'scopeId'}
            if mode == 'current':
                closed(item, fields | {'capabilityRevision', 'formats', 'limits'})
                opaque(item['capabilityRevision'])
                formats = item['formats']
                if (type(formats) not in (list, tuple) or not 1 <= len(formats) <= len(RELAY_FORMATS)
                        or any(type(f) is not str or f not in RELAY_FORMATS for f in formats)
                        or len(set(formats)) != len(formats)):
                    unavailable()
                limits = closed(item['limits'], LIMITS)
                if any(not integer(limits[k], 1, public_limits[k]) for k in LIMITS):
                    unavailable()
                item = dict(item, formats=tuple(formats), limits=MappingProxyType(dict(limits)))
            elif mode == 'retained_only':
                closed(item, fields | {'anchorDraftId'})
                ident(item['anchorDraftId'])
            else:
                unavailable()
            did = opaque(item['destinationId'])
            opaque(item['scopeId'])
            if did == 'officer' or did in entries:
                unavailable()
            entries[did] = MappingProxyType(dict(item))
        self.entries = MappingProxyType(entries)

    def selected(self, destination_id):
        entry = self.entries.get(opaque(destination_id))
        if entry is not None and entry['mode'] == 'retained_only':
            with self.store._gate():
                row = self.store.db.execute('SELECT * FROM drafts WHERE id=?', (entry['anchorDraftId'],)).fetchone()
                if row is None:
                    return None
                binding = draft_binding(row)
                if (binding['kind'], binding['id'], binding['scopeId']) != ('agent', destination_id, entry['scopeId']):
                    return None
        return entry

    def resolve_destination(self, destination_id, scope_id, model_id):
        entry = self.selected(destination_id)
        if entry is None or entry['mode'] != 'current' or scope_id != entry['scopeId'] or model_id is not None:
            return {'available': False}
        return {'available': True, 'kind': 'agent', 'id': destination_id, 'scopeId': scope_id, 'modelId': None,
                'capabilityRevision': entry['capabilityRevision'], 'formats': entry['formats'],
                'originalRelayFormats': entry['formats'], 'limits': dict(entry['limits'])}


class NativeAgentReadService(AttachmentService):
    def __init__(self, store, *, selections, _receipts=None):
        resolver = _NativeReadResolver(store, selections)
        super().__init__(store, resolver=resolver, clock=store.clock, officer_deletions=False)
        self._borrowed = _receipts is not None
        if _receipts is None:
            self.receipts = NativeReadReceipts(store)
        else:
            if type(_receipts) is not NativeReadReceipts:
                unavailable()
            with store._gate():
                _receipts.require(store)
            self.receipts = _receipts

    def close(self, timeout=35):
        if not self._borrowed:
            return super().close(timeout)
        with self.condition:
            self.accepting = False
            if not self.condition.wait_for(lambda: self.active == 0, timeout):
                fail('upload_busy', 'Attachment operations are still active.')

    def native_selection(self, destination_id):
        entry = self.resolver.selected(destination_id)
        context = None if entry is None else {'kind': 'agent', 'destinationId': destination_id,
                                              'scopeId': entry['scopeId'], 'modelId': None, 'conversationId': None}
        return {'context': context, 'features': {'drafts': entry is not None, 'send': False,
                                                 'files': False, 'deletion': False}}

    def _scope(self, destination_id, scope_id):
        opaque(scope_id)
        entry = self.resolver.selected(destination_id)
        if entry is None or entry['scopeId'] != scope_id:
            fail('forbidden', 'The native attachment scope could not be authorized.', 403)

    def native_receipt(self, kind, destination_id, scope_id, command_digest, draft_id=None):
        if (kind not in NATIVE_RECEIPT_KINDS or type(command_digest) is not str
                or not TOKEN.fullmatch(command_digest) or kind == 'draft_create' and draft_id is not None):
            fail('invalid_request', 'Invalid native attachment receipt selection.', 400)
        if kind != 'draft_create':
            ident(draft_id)
        self._scope(destination_id, scope_id)
        return {'match': self.receipts.lookup(kind, destination_id, scope_id, command_digest, draft_id)}

    def _bound_draft(self, draft_id):
        with self.store._gate():
            row = self.store._draft(draft_id)
            binding = draft_binding(row)
            if binding['kind'] != 'agent':
                fail('forbidden', 'The native attachment draft could not be authorized.', 403)
            self._scope(binding['id'], binding['scopeId'])
            self.store._writable(row)
            return binding

    def list_drafts(self, *, destination_id, scope_id, cursor=None):
        self._scope(destination_id, scope_id)
        page = super().list_drafts(destination_id=destination_id, scope_id=scope_id, cursor=cursor)
        for item in page['items']:
            self._bound_draft(item['id'])
        return page

    @staticmethod
    def _relay_draft(draft):
        for file in draft['files']:
            relay_row(file, MEMBER_STATES)
            try:
                safe_name = filename(file['name'])
            except AttachmentError:
                unclassified()
            if (file['mediaType'] not in RELAY_FORMATS or safe_name != file['name']
                    or type(file['id']) is not str or not ID.fullmatch(file['id'])
                    or not integer(file['sourceGeneration'], 1)
                    or not integer(file['bytes'], 0, 10 * 1024 * 1024)):
                unclassified()
        return draft

    def get_draft(self, draft_id, capability):
        self._bound_draft(draft_id)
        draft = self._relay_draft(self.store.get_draft(draft_id, capability))
        for file in draft['files']:
            file['sourceCapability'] = None
        return draft

    def create_draft(self, data, command_key):
        binding = self.binding(data)
        return self._relay_draft(self.store.create_draft(binding, command_key, native_receipts=self.receipts))

    def _reserve_upload(self, draft_id, capability, fence, metadata, command_key, **options):
        return self.store.reserve_upload(draft_id, capability, fence, metadata, command_key,
                                         native_receipts=self.receipts, **options)

    def mutate(self, operation, draft_id, capability, data, *, command_key=None, file_id=None):
        if operation not in ('reserve', 'edit', 'remove', 'discard'):
            unavailable()
        with self.store._gate():
            self._bound_draft(draft_id)
            if operation in ('edit', 'reserve'):
                self._relay_draft(self.store.get_draft(draft_id, capability))
            result = super().mutate(operation, draft_id, capability, data, command_key=command_key, file_id=file_id)
            if operation == 'reserve':
                return relay_row(result, MEMBER_STATES + CLEANUP_STATES)
            return self._relay_draft(result) if operation != 'discard' else result

    def recover(self, draft_id, data):
        self._bound_draft(draft_id)
        return super().recover(draft_id, data)

    def resume(self, draft_id, capability, data):
        with self.store._gate():
            self._bound_draft(draft_id)
            self._relay_draft(self.store._snapshot(self.store._draft(draft_id)))
            return super().resume(draft_id, capability, data)

    def get_upload(self, draft_id, capability, upload_id):
        self._bound_draft(draft_id)
        return relay_row(super().get_upload(draft_id, capability, upload_id), MEMBER_STATES + CLEANUP_STATES)

    def _put_context(self, draft_id, capability, upload_id):
        binding = self._bound_draft(draft_id)
        selection = {key: binding[key] for key in SELECTION - {'destinationId'}}
        selection['destinationId'] = binding['id']
        _, qualification = self._selection(selection)
        upload = self.get_upload(draft_id, capability, upload_id)
        draft = self._relay_draft(self.store.get_draft(draft_id, capability))
        member = next((f for f in draft['files'] if f['id'] == upload['fileId']), None)
        if member is None or member['mediaType'] not in qualification['formats']:
            fail('forbidden', 'The native attachment upload could not be authorized.', 403)

    def upload_transport_info(self, draft_id, capability, upload_id):
        self._put_context(draft_id, capability, upload_id)
        return super().upload_transport_info(draft_id, capability, upload_id)

    def receive_upload(self, draft_id, capability, fence, upload_id, fingerprint, reader):
        self._put_context(draft_id, capability, upload_id)
        result = super().receive_upload(draft_id, capability, fence, upload_id, fingerprint, reader)
        return relay_row(result, MEMBER_STATES)

    # These routes have no qualified Stage 1 projection or mutation.
    create_conversation = current_conversation = history = storage_files = preview = unavailable
    management = deletion_prepare = deletion_confirm = deletion_status = unavailable
