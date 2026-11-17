"""Calendar boundaries use actual zoneinfo; no provider, files or deletion."""
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from attachments_expiry_time import resolve_expiry, host_timezone
from attachments_types import AttachmentError


def stamp(value):
    return int(datetime.fromisoformat(value).timestamp())


class CalendarExpiryTests(unittest.TestCase):
    def resolve(self, day, zone='UTC', now='2030-01-01T12:00:00+00:00'):
        return resolve_expiry(day, timezone_name=zone, now=stamp(now))

    def assert_error(self, code, fn):
        with self.assertRaises(AttachmentError) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)

    def test_today_allowed_and_deadline_is_next_midnight(self):
        result = self.resolve('2030-01-01')
        self.assertEqual((result.date, result.timezone, result.deadline_utc),
                         ('2030-01-01', 'UTC', stamp('2030-01-02T00:00:00+00:00')))
        with self.assertRaises(AttributeError):
            result.deadline_utc = 0

    def test_local_today_not_utc_today(self):
        result = self.resolve('2029-12-31', 'America/Los_Angeles', '2030-01-01T01:00:00+00:00')
        self.assertEqual(result.deadline_utc, stamp('2030-01-01T08:00:00+00:00'))
        self.assert_error('expiry_date_past', lambda: self.resolve(
            '2029-12-31', 'Asia/Tokyo', '2030-01-01T01:00:00+00:00'))

    def test_dst_days_are_not_fixed_24_hours(self):
        spring = self.resolve('2030-03-10', 'America/New_York')
        before = self.resolve('2030-03-09', 'America/New_York')
        self.assertEqual(spring.deadline_utc - before.deadline_utc, 23 * 3600)
        autumn = self.resolve('2030-11-03', 'America/New_York')
        before = self.resolve('2030-11-02', 'America/New_York')
        self.assertEqual(autumn.deadline_utc - before.deadline_utc, 25 * 3600)

    def test_missing_midnight_uses_actual_transition(self):
        result = self.resolve('2018-11-03', 'America/Sao_Paulo', '2018-01-01T12:00:00+00:00')
        self.assertEqual(result.deadline_utc, stamp('2018-11-04T03:00:00+00:00'))

    def test_repeated_midnight_uses_first_occurrence(self):
        result = self.resolve('2020-10-31', 'America/Havana', '2020-01-01T12:00:00+00:00')
        self.assertEqual(result.deadline_utc, stamp('2020-11-01T04:00:00+00:00'))

    def test_skipped_day_uses_first_instant_after_selected_day(self):
        result = self.resolve('2011-12-29', 'Pacific/Apia', '2011-01-01T12:00:00+00:00')
        self.assertEqual(result.deadline_utc, stamp('2011-12-30T10:00:00+00:00'))
        skipped = self.resolve('2011-12-30', 'Pacific/Apia', '2011-01-01T12:00:00+00:00')
        self.assertEqual(skipped.deadline_utc, result.deadline_utc)

    def test_invalid_dates_and_zones_are_closed(self):
        for value in (None, True, 20300101, '', '2030-1-01', '2030-02-30', '2030-01-01\n',
                      '2030-01-01T00:00:00Z', '0000-01-01'):
            with self.subTest(value=value):
                self.assert_error('invalid_expiry_date', lambda: self.resolve(value))
        self.assert_error('expiry_date_past', lambda: self.resolve('2029-12-31'))
        self.assert_error('expiry_date_unavailable', lambda: self.resolve('9999-12-31'))
        for zone in (None, '', '../UTC', '/etc/localtime', 'Unknown/Fictional', 'UTC\n'):
            with self.subTest(zone=zone):
                self.assert_error('timezone_unavailable', lambda: self.resolve('2030-01-01', zone))

    def test_invalid_clock_and_no_implicit_default(self):
        for now in (None, True, -1, float('nan'), float('inf'), 1e30):
            with self.subTest(now=now):
                self.assert_error('clock_uncertain', lambda: resolve_expiry(
                    '2030-01-01', timezone_name='UTC', now=now))
        self.assert_error('invalid_expiry_date', lambda: self.resolve(None))

    def test_missing_timezone_database_is_unavailable(self):
        with patch('attachments_expiry_time.ZoneInfo', side_effect=ZoneInfoNotFoundError('unavailable')):
            self.assert_error('timezone_unavailable', lambda: self.resolve('2030-01-01'))

    def test_host_zone_does_not_use_environment_or_guess_offset(self):
        with patch.dict('os.environ', {'TZ': 'Asia/Tokyo'}), \
                patch.object(Path, 'is_symlink', return_value=True), \
                patch.object(Path, 'resolve', return_value=Path('/usr/share/zoneinfo/America/New_York')):
            self.assertEqual(host_timezone(), 'America/New_York')
        with patch.object(Path, 'is_symlink', return_value=False):
            self.assert_error('timezone_unavailable', host_timezone)
        with patch.object(Path, 'is_symlink', return_value=True), \
                patch.object(Path, 'resolve', return_value=Path('/private/unknown')):
            self.assert_error('timezone_unavailable', host_timezone)

    def test_retained_deadline_is_not_reinterpreted(self):
        existing = self.resolve('2030-01-02', 'America/New_York')
        other = self.resolve('2030-01-02', 'Asia/Tokyo')
        self.assertNotEqual(existing.deadline_utc, other.deadline_utc)
        self.assertEqual(existing.timezone, 'America/New_York')
        self.assertEqual(existing.deadline_utc, stamp('2030-01-03T05:00:00+00:00'))


if __name__ == '__main__':
    unittest.main()
