"""The NWS climate day: midnight to midnight local STANDARD time, all year round.

During daylight saving time that is 1:00 AM to 1:00 AM on the clock, so a 12:30 AM
DST reading belongs to the previous climate day.
"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def standard_offset(tz: ZoneInfo, day: date) -> timedelta:
    """UTC offset of the zone's standard time around `day` (DST removed)."""
    noon = datetime.combine(day, time(12), tzinfo=tz)
    return noon.utcoffset() - (noon.dst() or timedelta(0))


def climate_day_window(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """UTC start and end of `day`'s climate day."""
    lst = timezone(standard_offset(tz, day))
    start = datetime.combine(day, time(0), tzinfo=lst)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def climate_date(moment: datetime, tz: ZoneInfo) -> date:
    """The climate day that an aware datetime falls in."""
    day = moment.astimezone(tz).date()
    for candidate in (day, day - timedelta(days=1), day + timedelta(days=1)):
        start, end = climate_day_window(candidate, tz)
        if start <= moment < end:
            return candidate
    raise ValueError(f"no climate day contains {moment}")
