"""Trusted local Motet original storage, not model-reading qualification.

No provider, parser, rendering, runtime resolver or call-budget authority.
"""
from dataclasses import dataclass
import hashlib

from attachments_types import OPAQUE, fail, integer

DIRECT_MEDIA = ('application/pdf', 'image/png')
SIGNATURES = {'application/pdf': b'%PDF-', 'image/png': b'\x89PNG\r\n\x1a\n'}


@dataclass(frozen=True)
class OfficerDirectRetentionProfile:
    id: str
    revision: str
    purpose: str
    capability_revision: str
    model_id: str
    media_types: tuple = DIRECT_MEDIA
    file_count: int = 5
    file_bytes: int = 10 * 1024 * 1024
    draft_bytes: int = 20 * 1024 * 1024

    def __post_init__(self):
        if (any(type(v) is not str or not OPAQUE.fullmatch(v) for v in
                (self.id, self.revision, self.capability_revision, self.model_id))
                or self.purpose != 'local_retention' or type(self.media_types) is not tuple
                or not 1 <= len(self.media_types) <= 2 or len(set(self.media_types)) != len(self.media_types)
                or any(v not in DIRECT_MEDIA for v in self.media_types)
                or not integer(self.file_count, 1, 5) or not integer(self.file_bytes, 1, 10 * 1024 * 1024)
                or not integer(self.draft_bytes, 1, 20 * 1024 * 1024)):
            fail('profile_unavailable', 'Motet original-file retention is unavailable.', 503)


def validate_binding(profile, binding):
    if type(profile) is not OfficerDirectRetentionProfile:
        fail('profile_unavailable', 'Motet original-file retention needs an exact profile.', 503)
    if (binding['kind'] != 'officer' or binding['modelId'] != profile.model_id
            or binding['capabilityRevision'] != profile.capability_revision):
        fail('capability_changed', 'The Motet retention context changed.', 409)


def prepare_original(data, media_type, *, max_bytes):
    """Signature/size/digest only; matching bytes may be malformed or unreadable."""
    if not integer(max_bytes, 1, 10 * 1024 * 1024):
        fail('preparation_unavailable', 'Motet original-file limits are unavailable.', 503)
    if media_type not in DIRECT_MEDIA:
        fail('preparation_unavailable', 'This original-file type is unavailable to Motet.', 503)
    if type(data) is not bytes or len(data) > max_bytes:
        fail('file_too_large', 'The attachment exceeds the file limit.', 413)
    if not data.startswith(SIGNATURES[media_type]):
        fail('type_signature_mismatch', 'The file does not match its declared type.', 422)
    return {'kind': 'officer_direct', 'mediaType': media_type, 'bytes': len(data),
            'sha256': hashlib.sha256(data).hexdigest(), 'previewAvailable': False}
