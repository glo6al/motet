"""Owner-only attachment orchestration; no native or provider transport."""
from contextlib import contextmanager
from datetime import datetime, timezone
from attachments_owner_marker import SQLITE_UNAVAILABLE_MESSAGE, LEGACY_MANAGED_UNAVAILABLE_MESSAGE
import threading
import time

from attachments_types import OPAQUE, destination, fail, integer, now
from attachments_relay import RELAY_FORMATS
from attachments_catalogue import Catalogue, HistorySelection

FENCE = {'revision', 'writerGeneration', 'writerNonce'}
SELECTION = {'destinationId', 'scopeId', 'modelId', 'conversationId', 'capabilityRevision'}


def closed(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        fail('invalid_request', 'Invalid attachment request fields.', 400)
    return value


def opaque(value):
    if type(value) is not str or not OPAQUE.fullmatch(value):
        fail('invalid_request', 'Choose an exact registered attachment context.', 400)
    return value


class UnqualifiedResolver:
    """No runtime proof registry is installed. Branding is not qualification."""
    def resolve_scope(self, scope_id):
        fail('destination_unavailable', 'Attachment conversations are unavailable on this host.', 503)

    def resolve_destination(self, destination_id, scope_id, model_id):
        return {'available': False}


class AttachmentService:
    def __init__(self, store, *, resolver, clock=time.time, officer_deletions=True):
        self.store, self.resolver, self.clock = store, resolver, clock
        self.native_sqlite_available = True
        self._native_legacy_managed_storage = None
        self.catalogue = Catalogue(store)
        self.condition = threading.Condition()
        self.accepting, self.active = True, 0
        self.deletions = None
        if officer_deletions and getattr(store, 'enabled', False) is True and not store.closed:
            from officer_document_deletion import OfficerDeletion, DeletionWorker
            self.deletions = OfficerDeletion(store)
            if getattr(store, '_deletion_worker', None) is None:
                store._deletion_worker = DeletionWorker(self.deletions)

    @contextmanager
    def operation(self):
        with self.condition:
            if not self.accepting:
                fail('attachments_unavailable', 'Attachments are shutting down.', 503)
            self.active += 1
        try:
            yield
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()

    def close(self, timeout=35):
        with self.condition:
            self.accepting = False
            if not self.condition.wait_for(lambda: self.active == 0, timeout):
                fail('upload_busy', 'Attachment operations are still active.')
            self.store.close()

    def envelope(self, value):
        return dict(value, schemaVersion=1, fixture=False,
                    serverTime=datetime.fromtimestamp(now(self.clock), timezone.utc).isoformat().replace('+00:00', 'Z'))

    def capabilities(self, destination_id, scope_id, model_id):
        opaque(destination_id); opaque(scope_id)
        if model_id is not None:
            opaque(model_id)
        resolved = self.resolver.resolve_destination(destination_id, scope_id, model_id)
        if type(resolved) is not dict or resolved.get('available') is not True:
            return {'available': False, 'reason': 'Attachment support is not qualified for this destination.',
                    'destinationId': destination_id, 'scopeId': scope_id, 'modelId': model_id}
        fields = {'available', 'kind', 'id', 'scopeId', 'modelId', 'capabilityRevision', 'formats', 'limits'}
        if set(resolved) not in (fields, fields | {'originalRelayFormats'}):
            fail('destination_unavailable', 'Attachment context could not be verified.', 503)
        if (resolved['id'], resolved['scopeId'], resolved['modelId']) != (destination_id, scope_id, model_id):
            fail('destination_unavailable', 'Attachment context could not be verified.', 503)
        if resolved['kind'] not in ('agent', 'officer') or not isinstance(resolved['formats'], (tuple, list)):
            fail('destination_unavailable', 'Attachment context could not be verified.', 503)
        if (resolved['kind'] == 'agent' and model_id is not None or resolved['kind'] == 'officer' and model_id is None):
            fail('invalid_request', 'Choose the exact destination model.', 400)
        opaque(resolved['capabilityRevision'])
        relay_formats = resolved.get('originalRelayFormats', [])
        if (type(relay_formats) not in (tuple, list) or len(relay_formats) > 3
                or any(type(f) is not str or f not in RELAY_FORMATS for f in relay_formats)
                or len(set(relay_formats)) != len(relay_formats)
                or any(f not in resolved['formats'] for f in relay_formats)
                or relay_formats and resolved['kind'] != 'agent'):
            fail('destination_unavailable', 'Original-file relay is unavailable for this destination.', 503)
        public_limits = self.store.limits_view()
        closed(resolved['limits'], {'fileBytes', 'draftBytes', 'fileCount'})
        for field, value in resolved['limits'].items():
            if not integer(value, 1, public_limits[field]):
                fail('destination_unavailable', 'Attachment limits could not be verified.', 503)
            public_limits[field] = value
        private_formats = public_limits['preparationFormats']
        formats = [f for f in resolved['formats'] if f in private_formats or f in relay_formats]
        return {'available': True, 'kind': resolved['kind'], 'destinationId': destination_id,
                'scopeId': scope_id, 'modelId': model_id, 'capabilityRevision': resolved['capabilityRevision'],
                'formats': formats, 'limits': public_limits,
                'preparationModes': {f: 'original_relay' if f in relay_formats else 'text' for f in formats}}

    def _selection(self, selection):
        closed(selection, SELECTION)
        cap = self.capabilities(selection['destinationId'], selection['scopeId'], selection['modelId'])
        if not cap['available']:
            fail('destination_unavailable', 'Attachment support is not qualified for this destination.', 503)
        if selection['capabilityRevision'] != cap['capabilityRevision']:
            fail('capability_changed', 'This attachment destination changed. Choose it again.')
        binding = destination({'kind': cap['kind'], 'id': cap['destinationId'], 'scopeId': cap['scopeId'],
                            'modelId': cap['modelId'], 'conversationId': selection['conversationId'],
                            'capabilityRevision': cap['capabilityRevision']})
        return binding, cap

    def binding(self, selection):
        return self._selection(selection)[0]

    def create_conversation(self, data, command_key):
        closed(data, {'scopeId'}); opaque(data['scopeId'])
        scope = self.resolver.resolve_scope(data['scopeId'])
        if type(scope) is not dict or scope.get('id') != data['scopeId']:
            fail('forbidden', 'The Motet scope could not be authorized.', 403)
        return self.store.create_conversation(data['scopeId'], command_key)

    def current_conversation(self, scope_id, *, create=False):
        opaque(scope_id)
        current = self.store.current_conversation(scope_id)
        if current is None and create:
            scope = self.resolver.resolve_scope(scope_id)
            if type(scope) is not dict or scope.get('id') != scope_id:
                fail('forbidden', 'The Motet scope could not be authorized.', 403)
            current = self.store.current_conversation(scope_id, create=True)
        return {'conversation': current}

    def create_draft(self, data, command_key):
        return self.store.create_draft(self.binding(data), command_key)

    def native_selection(self, destination_id):
        if self._native_legacy_managed_storage is not None and self._native_legacy_managed_storage():
            fail('legacy_managed_storage', LEGACY_MANAGED_UNAVAILABLE_MESSAGE, 503)
        if not self.native_sqlite_available:
            fail('sqlite_unavailable', SQLITE_UNAVAILABLE_MESSAGE, 503)
        fail('attachments_disabled', 'Native agent attachments are unavailable.', 503)

    def native_receipt(self, kind, destination_id, scope_id, command_digest, draft_id=None):
        fail('attachments_disabled', 'Native agent attachments are unavailable.', 503)

    def list_drafts(self, *, destination_id, scope_id, cursor=None):
        return self.store.list_drafts(destination_id=destination_id, scope_id=scope_id, cursor=cursor)

    def get_upload(self, draft_id, capability, upload_id):
        return self.store.get_upload(draft_id, capability, upload_id)

    def upload_transport_info(self, draft_id, capability, upload_id):
        return self.store.upload_transport_info(draft_id, capability, upload_id)

    def receive_upload(self, draft_id, capability, fence, upload_id, fingerprint, reader):
        return self.store.receive_upload(draft_id, capability, fence, upload_id, fingerprint, reader)

    def _reserve_upload(self, draft_id, capability, fence, metadata, command_key, **options):
        return self.store.reserve_upload(draft_id, capability, fence, metadata, command_key, **options)

    def mutate(self, operation, draft_id, capability, data, *, command_key=None, file_id=None):
        extra = {'text'} if operation == 'edit' else {'name', 'mediaType', 'bytes', 'digest'} if operation == 'reserve' else set()
        closed(data, FENCE | extra)
        fence = {k: data[k] for k in FENCE}
        if operation == 'edit':
            return self.store.edit_text(draft_id, capability, fence, data['text'])
        if operation == 'discard':
            return self.store.discard_draft(draft_id, capability, fence, command_key)
        if operation == 'reserve':
            draft = self.store.get_draft(draft_id, capability)
            b = draft['destination']
            cap, qualified = self._selection({'destinationId': b['id'], 'scopeId': b['scopeId'], 'modelId': b['modelId'],
                                'conversationId': b['conversationId'], 'capabilityRevision': b['capabilityRevision']})
            if data['mediaType'] not in qualified['formats']:
                fail('preparation_unavailable', 'This attachment format is not qualified for this destination.', 503)
            return self._reserve_upload(draft_id, capability, fence, {k: data[k] for k in extra}, command_key,
                                             admission_limits={k: qualified['limits'][k] for k in ('fileBytes', 'draftBytes', 'fileCount')},
                                             preparation_kind=qualified['preparationModes'][data['mediaType']])
        if operation == 'retry':
            return self.store.retry_preparation(draft_id, capability, fence, file_id)
        if operation == 'remove':
            return self.store.remove_file(draft_id, capability, fence, file_id)
        fail('unavailable', 'This attachment operation is not implemented.', 503)

    def recover(self, draft_id, data):
        closed(data, {'writerGeneration'})
        self.store.draft_summary(draft_id)
        return self.store.recover_draft(draft_id, data['writerGeneration'])

    def resume(self, draft_id, capability, data):
        closed(data, {'revision', 'writerGeneration'})
        return self.store.resume_draft(draft_id, capability, data['revision'], data['writerGeneration'])

    def history(self, destination_id, scope_id, conversation_id, *, cursor=None):
        selection = HistorySelection('officer' if conversation_id is not None else 'agent', destination_id, scope_id, conversation_id)
        return self.catalogue.history(selection, cursor=cursor)

    def get_draft(self, draft_id, capability):
        return self.catalogue.draft(draft_id, capability)

    def storage_files(self, *, cursor=None):
        return self.catalogue.storage_files(cursor=cursor)

    def management(self, conversation_id, destination_id, scope_id, cursor=None):
        if self.deletions is None:
            fail('attachments_disabled', 'Attachments are unavailable.', 503)
        return self.deletions.management(destination_id, scope_id, conversation_id, cursor)

    def deletion_prepare(self, file_id, data, command_key):
        closed(data, {'destinationId', 'scopeId', 'conversationId', 'managementGeneration'})
        return self.deletions.prepare(file_id, data['destinationId'], data['scopeId'], data['conversationId'],
                                      data['managementGeneration'], command_key)

    def deletion_confirm(self, operation_id, capability, data, command_key):
        closed(data, {'fingerprint'})
        return self.deletions.confirm(operation_id, capability, data['fingerprint'], command_key)

    def deletion_status(self, operation_id, capability):
        return self.deletions.status(operation_id, capability)

    def preview(self, file_id, capability, location, publish):
        snapshot = self.catalogue.preview_snapshot(capability, file_id, location)
        return self.catalogue.publish_preview(snapshot, publish)
