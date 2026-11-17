"""Bounded plain-source preparation. No parser, executable markup or provider calls."""
from attachments_types import AttachmentError, fail


def prepare(data, media_type, *, max_bytes, max_lines):
    if type(data) is not bytes or len(data) > max_bytes:
        fail('file_too_large', 'The attachment exceeds the file limit.', 413)
    if media_type not in ('text/plain', 'text/markdown'):
        fail('preparation_unavailable', 'This file type needs a qualified isolated parser.', 503)
    try:
        value = data.decode('utf-8', errors='strict')
    except UnicodeError:
        fail('invalid_text', 'This file is not valid UTF-8 text.', 422)
    if '\x00' in value:
        fail('invalid_text', 'This text file contains unsupported null characters.', 422)
    # Check line separators without allocating an unbounded list first. CRLF is
    # one separator under splitlines(), so count that pair once.
    separators = value.count('\n') + value.count('\r') - value.count('\r\n')
    separators += sum(value.count(c) for c in ('\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029'))
    if separators > max_lines:
        fail('preparation_limit', 'This file has too many text lines.', 422)
    lines = value.splitlines()
    if len(lines) > max_lines:
        fail('preparation_limit', 'This file has too many text lines.', 422)
    # Preserve the exact source. A renderer must treat these values as text, never HTML.
    return {'kind': 'text', 'mediaType': media_type, 'text': value,
            'lineCount': len(lines), 'lines': [{'number': i + 1, 'text': line} for i, line in enumerate(lines)]}
