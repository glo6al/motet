"""Closed schemas and limits for the disabled-by-default attachment foundation."""
import hashlib
import json
import math
import re
import unicodedata

MIB = 1024 * 1024
DEFAULT_LIMITS = {
    'bytes': 5_000_000_000, 'file_bytes': 10 * MIB, 'draft_bytes': 20 * MIB,
    'files': 5, 'drafts': 1000, 'objects': 50_000, 'seen': 100_000,
    'uploads': 128, 'streams': 4, 'conversations': 1000,
    'requests': 10_000, 'officer_messages': 10_000, 'copies': 50_000, 'contexts': 1000,
    'database_bytes': 128 * MIB, 'text_chars': 4000, 'lines': 100_000,
}
ID = re.compile(r'[a-f0-9]{32}\Z')
TOKEN = re.compile(r'[a-f0-9]{64}\Z')
KEY = re.compile(r'[A-Za-z0-9_-]{16,128}\Z')
OPAQUE = re.compile(r'[A-Za-z0-9_.:-]{1,160}\Z')
MEDIA = frozenset(('text/plain', 'text/markdown', 'application/pdf', 'image/png', 'image/jpeg'))


class AttachmentError(Exception):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def fail(code, message, status=409):
    raise AttachmentError(code, message, status)


def integer(value, minimum=0, maximum=2**53 - 1):
    return type(value) is int and minimum <= value <= maximum


def text(value, maximum):
    return type(value) is str and len(value) <= maximum and not any(0xD800 <= ord(c) <= 0xDFFF for c in value)


def ident(value):
    if type(value) is not str or not ID.fullmatch(value):
        fail('invalid_request', 'Invalid attachment identity.', 400)
    return value


def key(value):
    if type(value) is not str or not KEY.fullmatch(value):
        fail('invalid_request', 'A bounded idempotency key is required.', 400)
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def filename(value):
    if not text(value, 255) or not value:
        fail('invalid_request', 'Use a nonempty bounded filename.', 400)
    result = ''.join('_' if c in '/\\' or unicodedata.category(c).startswith('C') else c for c in value).strip()
    if result in ('', '.', '..'):
        fail('invalid_request', 'Use a nonempty bounded filename.', 400)
    return result


def destination(value):
    fields = {'kind', 'id', 'scopeId', 'conversationId', 'modelId', 'capabilityRevision'}
    if type(value) is not dict or set(value) != fields or value['kind'] not in ('agent', 'officer'):
        fail('invalid_request', 'Invalid resolved attachment destination.', 400)
    for field in ('id', 'scopeId', 'capabilityRevision'):
        if type(value[field]) is not str or not OPAQUE.fullmatch(value[field]):
            fail('invalid_request', 'Invalid resolved attachment destination.', 400)
    if value['kind'] == 'officer':
        ident(value['conversationId'])
        if type(value['modelId']) is not str or not OPAQUE.fullmatch(value['modelId']):
            fail('invalid_request', 'Choose an exact Motet model.', 400)
    elif value['conversationId'] is not None or value['modelId'] is not None:
        fail('invalid_request', 'Agent drafts cannot adopt Motet conversation files.', 400)
    return dict(value)


def limits(overrides):
    if overrides is None:
        return dict(DEFAULT_LIMITS)
    if type(overrides) is not dict or set(overrides) - set(DEFAULT_LIMITS):
        fail('invalid_configuration', 'Invalid attachment limits.', 503)
    result = dict(DEFAULT_LIMITS, **overrides)
    if any(not integer(v, 1) for v in result.values()):
        fail('invalid_configuration', 'Invalid attachment limits.', 503)
    return result


def now(clock):
    value = clock()
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        fail('clock_uncertain', 'Attachment time could not be verified.', 503)
    return float(value)


def add_note(error, note):
    """BaseException.add_note on Python 3.11 and later, and the same __notes__ list on earlier versions."""
    if hasattr(error, 'add_note'):
        error.add_note(note)
    else:
        error.__notes__ = [*getattr(error, '__notes__', ()), note]
