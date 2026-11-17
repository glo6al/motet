"""Opaque original-file relay eligibility, never document validation or preview.

Only a trusted native-agent qualification may select this preparation mode.
This module cannot qualify a recipient or offer a file to Motet. It checks
a bounded byte buffer and a superficial signature, without decoding its format.
"""
import hashlib

from attachments_types import fail, integer

SIGNATURES = {
    'application/pdf': b'%PDF-',
    'image/png': b'\x89PNG\r\n\x1a\n',
    'image/jpeg': b'\xff\xd8\xff',
}
RELAY_FORMATS = frozenset(SIGNATURES)


def original_relay(data, media_type, *, max_bytes):
    """Return metadata for unchanged bytes; malformed documents can pass.

    Matching a prefix is not encryption, structure, dimension, safety or
    readability validation. A recipient's own parser may reject the file.
    No extracted content, page count, decoded pixels or original bytes return.
    """
    if not integer(max_bytes, 1, 10 * 1024 * 1024):
        fail('preparation_unavailable', 'Original-file relay limits are unavailable.', 503)
    if type(data) is not bytes or len(data) > max_bytes:
        fail('file_too_large', 'The attachment exceeds the file limit.', 413)
    if type(media_type) is not str or media_type not in RELAY_FORMATS:
        fail('preparation_unavailable', 'This type is unavailable for original-file relay.', 503)
    if not data.startswith(SIGNATURES[media_type]):
        fail('type_signature_mismatch', 'The file prefix does not match its selected type.', 422)
    return {'kind': 'original_relay', 'mediaType': media_type, 'bytes': len(data),
            'sha256': hashlib.sha256(data).hexdigest(),
            'preview': {'available': False, 'reason': 'Original-file relay has no preview in Motet.'}}
