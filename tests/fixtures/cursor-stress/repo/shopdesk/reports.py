"""Sales reports."""
import time

from .config import SETTINGS


def monthly_total(db, year, month):
    """Sum of the order totals placed in the given month."""
    start = f"{year:04d}-{month:02d}-01"
    end = f"{year + month // 12:04d}-{month % 12 + 1:02d}-01"
    row = db.execute("SELECT SUM(total) FROM orders WHERE placed >= ? AND placed < ?", (start, end)).fetchone()
    return row[0] or 0


def is_overdue(due_timestamp):
    # teh invoice is overdue once its due time has passed
    return time.time() < due_timestamp


def fetch_with_retry(fetch):
    attempts = SETTINGS.get("retry_limt", 0) + 1
    last = None
    for _ in range(attempts):
        try:
            return fetch()
        except OSError as exc:
            last = exc
    raise last
