"""Differential checks for owner marker detection, with disposable SQLite only."""
import json
from pathlib import Path
import random
import sqlite3
import sys
import tracemalloc
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from attachments_owner_marker import (CLEAR, MARKER, MAX_DEPTH, UNCERTAIN, lexical_payload_state,
                                      sqlite_marker_available, sqlite_payload_state)


KEY = 'attachment_owner_version'


class ObjectPairs(list):
    """Differentiate object pairs from arrays and preserve duplicate names."""


def reject_constant(value):
    raise ValueError(value)


def reference(raw):
    if any(not (32 <= byte <= 126 or byte in (9, 10, 13)) for byte in raw):
        return UNCERTAIN
    try:
        body = json.loads(raw, object_pairs_hook=ObjectPairs, parse_constant=reject_constant)
    except (ValueError, RecursionError):
        return UNCERTAIN
    if not isinstance(body, ObjectPairs):
        return UNCERTAIN
    return MARKER if any(key == KEY for key, _ in body) else CLEAR


class MarkerTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.assertTrue(sqlite_marker_available(self.db))

    def sql(self, raw, *, storage='TEXT'):
        expression = sqlite_payload_state('sample.body')
        return self.db.execute(f'SELECT {expression} FROM (SELECT CAST(? AS {storage}) AS body) AS sample',
                               (raw,)).fetchone()[0]

    def both(self, sample, expected):
        self.assertEqual(lexical_payload_state(sample), expected)
        self.assertEqual(self.sql(sample), expected)

    def test_sql_and_fallback_all_256_single_byte_insertions(self):
        templates = (b'{"question":"a%sZ"}', b'{"a":%s1}', b'{"question":"a"}%s', b'{%s"a":0}', b'%s{"a":0}')
        for template in templates:
            for byte in range(256):
                sample = template % bytes([byte])
                with self.subTest(template=template, byte=byte):
                    self.both(sample, reference(sample))

    def test_root_marker_escaped_duplicates_null_and_nested_text(self):
        cases = (
            (b'{"attachment_owner_version":null}', MARKER),
            (br'{"\u0061ttachment_owner_versio\u006e":false}', MARKER),
            (br'{"\u0061ttachment_owner_version":null,"attachment_owner_version":0}', MARKER),
            (br'{"attachment_owner_version":null,"\u0061ttachment_owner_version":[]}', MARKER),
            (br'{"\u0061ttachment_owne\u0072_version":null,"\u0061ttachment_owner_version":false}', MARKER),
            (br'{"nested":{"attachment_owner_version":1},"question":"\"attachment_owner_version\":null"}', CLEAR),
            (br'{"attachment_owner_version\u0000":null,"other":[{},[0,{}]]}', CLEAR),
            (br'{"attachment_owner_versioN":null,"attachment_owner_versionx":1}', CLEAR),
            (br'{"attachment_owner_\uD800":0,"q":"\ud83d\ude00"}', CLEAR),
            (br'{"k":"x\/\\\"\b\f\t\n\r\u007f"}', CLEAR),
            (b'\t\r\n{ "ok": 0 }\r\n', CLEAR),
        )
        for sample, state in cases:
            with self.subTest(sample=sample):
                self.both(sample, state)

    def test_malformed_unknown_root_types_and_representation(self):
        cases = (b'', b'  ', b'{}x', b'{"a":truefalse}', b'{"a":null0}', b'{"a":false,"b":}',
                 b'{"a":1e-}', b'{"a":.1}', b'{"a":2.}', b'{"a":-}', b'{"a":01}', b'{"a":-00}',
                 br'{"bad\q":0}', br'{"a":"\uXYZ1"}', b'{"a":"\\u12"}', b'{"a":1,}', b'{"a":[1,]}',
                 b'{"a":[1}', b'[]', b'[ {"attachment_owner_version":1} ]', b'null', b'"question"', b'1',
                 b'{"a":1\0}', b'{}\0', b'{"a":"\x01"}', b'{"a":"\x7f"}', b'{"a":"\xff"}',
                 '{"a":"é"}'.encode('utf-8'), '{"question":"😀"}'.encode('utf-8'))
        for sample in cases:
            with self.subTest(sample=sample):
                self.both(sample, UNCERTAIN)
        self.assertEqual(self.sql(b'{}', storage='BLOB'), UNCERTAIN)
        self.assertEqual(self.sql(b'{"attachment_owner_version":null}', storage='BLOB'), UNCERTAIN)
        self.assertEqual(lexical_payload_state(None), UNCERTAIN)

    def test_invalid_suffix_cannot_downgrade_an_observed_marker(self):
        invalid_after = br'{"x":[{}],"\u0061ttachment_owner_version":null,"broken":'
        self.assertEqual(lexical_payload_state(invalid_after), MARKER)
        self.assertEqual(self.sql(invalid_after), UNCERTAIN)
        self.both(br'{"\u0061ttachment_owner_version" 0}', UNCERTAIN)
        self.assertEqual(lexical_payload_state(b'{"attachment_owner_version":null,"a":"\xff"}'), MARKER)
        self.assertEqual(self.sql('{"title":"é","attachment_owner_version":1}'.encode('utf-8')), MARKER)

    def test_fixed_depth_and_large_lexical_workspace(self):
        def arrays(levels):
            return b'{"a":' + b'[' * levels + b'0' + b']' * levels + b'}'
        self.assertEqual(lexical_payload_state(arrays(MAX_DEPTH - 2)), CLEAR)
        self.assertEqual(lexical_payload_state(arrays(MAX_DEPTH - 1)), UNCERTAIN)
        self.assertEqual(lexical_payload_state(arrays(MAX_DEPTH * 8)), UNCERTAIN)
        self.assertEqual(self.sql(arrays(MAX_DEPTH * 8)), UNCERTAIN)
        small = b'{"question":"' + b'x' * 1024 + b'"}'
        huge = b'{"question":"' + b'x' * 9900000 + b'","number":' + b'7' * 4000000 + b'}'
        large_key = b'{"' + b'x' * 9900000 + b'":null,"attachment_owner_version":null}'
        for body, expected in ((small, CLEAR), (huge, CLEAR), (large_key, MARKER)):
            tracemalloc.start()
            try:
                self.assertEqual(lexical_payload_state(body), expected)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertLess(peak, 32 * 1024)
        self.assertEqual(self.sql(huge), CLEAR)
        self.assertEqual(self.sql(large_key), MARKER)

    def test_seeded_valid_and_mutated_json_against_object_pairs(self):
        rng = random.Random(263)
        tokens = [None, False, True, -193, 0, 3.5e-100, '\n\u0000😀', '"\\\\', '\ud800',
                  '文字', KEY, 'attachment_owner_version: null']
        names = ['q', '', '2x', '😀', '"foo', 'a\n', KEY]

        def value(depth):
            if depth and rng.randrange(3) == 0:
                return {rng.choice(names): value(depth-1) for _ in range(rng.randrange(5))}
            if depth and rng.randrange(3) == 0:
                return [value(depth-1) for _ in range(rng.randrange(5))]
            return rng.choice(tokens)

        for _ in range(300):
            document = {rng.choice(names): value(5) for _ in range(rng.randrange(8))}
            raw = json.dumps(document, ensure_ascii=True, indent=rng.choice([None, 1])).encode('ascii')
            if rng.randrange(2) == 0:
                raw = raw.replace(b'"attachment_owner_version"', br'"\u0061ttachment_owner_version"', 1)
            self.both(raw, reference(raw))
            index = rng.randrange(len(raw))
            changed = raw[:index] + bytes([rng.randrange(256)]) + raw[index+1:]
            expected = reference(changed)
            lexical = lexical_payload_state(changed)
            if expected == UNCERTAIN:
                self.assertIn(lexical, (MARKER, UNCERTAIN))  # A prefix witness may survive damage.
                self.assertNotEqual(self.sql(changed), CLEAR)
            else:
                self.both(changed, expected)


if __name__ == '__main__':
    unittest.main()
