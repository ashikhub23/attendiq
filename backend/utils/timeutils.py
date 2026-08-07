"""Indian Standard Time (UTC+5:30) helpers for display."""
from datetime import datetime, timedelta

IST_OFFSET = timedelta(hours=5, minutes=30)


def utc_to_ist(dt):
    """Shift a naive UTC datetime to IST (UTC+5:30)."""
    if dt is None:
        return dt
    return dt + IST_OFFSET


def now_ist():
    return datetime.utcnow() + IST_OFFSET
