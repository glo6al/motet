"""Owner-selected calendar expiry. No scheduler, deletion, or ambient TZ override.

Only trusted server code supplies the host zone. Browser requests supply a date,
never a timezone or UTC deadline. Retained policies use their stored deadline;
calling this module again is not a migration or reinterpretation of a policy.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
from pathlib import Path
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from attachments_types import fail

DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
ZONE = re.compile(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*\Z")
UTC = timezone.utc


@dataclass(frozen=True)
class ExpiryBoundary:
    date: str
    timezone: str
    deadline_utc: int


def _zone(name):
    if type(name) is not str or len(name) > 160 or not ZONE.fullmatch(name):
        fail('timezone_unavailable', 'The Motet computer timezone could not be verified.', 503)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        fail('timezone_unavailable', 'The Motet computer timezone could not be verified.', 503)


def host_timezone():
    """Read the host's zoneinfo link; never infer an IANA zone from an offset.

    Supported hosts publish /etc/localtime as a zoneinfo symlink. A copied file,
    an unrecognized layout, or missing zone data leaves date setting unavailable.
    It does not alter no-expiry defaults or previously recorded UTC deadlines.
    """
    try:
        link = Path('/etc/localtime')
        if not link.is_symlink():
            raise ValueError()
        resolved = str(link.resolve(strict=True))
        _, separator, name = resolved.rpartition('/zoneinfo/')
        if not separator:
            raise ValueError()
        _zone(name)
        return name
    except (OSError, ValueError, RuntimeError):
        fail('timezone_unavailable', 'The Motet computer timezone could not be verified.', 503)


def _end_of_day(selected, zone):
    """First valid instant after the selected local day, to second precision.

    A valid repeated midnight uses its earlier occurrence. For a missing
    midnight (including a skipped calendar day), zoneinfo's two fold mappings
    bracket the gap. Locate the transition itself, not the shifted wall time.
    TZif transition times and Python zoneinfo offsets have second precision.
    """
    try:
        midnight = datetime.combine(selected + timedelta(days=1), datetime.min.time())
        candidates = sorted(set(int(midnight.replace(tzinfo=zone, fold=fold).timestamp())
                                for fold in (0, 1)))
        exact = [stamp for stamp in candidates
                 if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) == midnight]
        if exact:
            return min(exact)
        if len(candidates) != 2:
            raise ValueError()
        low, high = candidates
        if (high - low > 2 * 86400
                or datetime.fromtimestamp(low, zone).date() > selected
                or datetime.fromtimestamp(high, zone).date() <= selected):
            raise ValueError()
        while high - low > 1:
            middle = (low + high) // 2
            if datetime.fromtimestamp(middle, zone).date() > selected:
                high = middle
            else:
                low = middle
        if (datetime.fromtimestamp(high - 1, zone).date() > selected
                or datetime.fromtimestamp(high, zone).date() <= selected):
            raise ValueError()
        return high
    except (OverflowError, OSError, ValueError):
        fail('expiry_date_unavailable', 'That expiry date cannot be resolved on this computer.', 422)


def resolve_expiry(date_value, *, timezone_name, now):
    """Resolve an explicit owner date using a trusted host-zone snapshot.

    Today is allowed; past, malformed and out-of-range dates are rejected.
    The returned date, IANA name and UTC deadline must be persisted together.
    No date is synthesized for an attachment with no chosen expiry.
    """
    zone = _zone(timezone_name)
    if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
        fail('clock_uncertain', 'Attachment time could not be verified.', 503)
    try:
        today = datetime.fromtimestamp(now, zone).date()
    except (ValueError, OverflowError, OSError):
        fail('clock_uncertain', 'Attachment time could not be verified.', 503)
    if type(date_value) is not str or not DATE.fullmatch(date_value):
        fail('invalid_expiry_date', 'Choose a valid expiry date.', 400)
    try:
        selected = date.fromisoformat(date_value)
    except ValueError:
        fail('invalid_expiry_date', 'Choose a valid expiry date.', 400)
    if selected < today:
        fail('expiry_date_past', 'Choose today or a later expiry date.', 409)
    return ExpiryBoundary(date_value, timezone_name, _end_of_day(selected, zone))
