"""Invoices: lookup, month lists, late fees."""
import sqlite3
from datetime import date

from .config import SETTINGS
from .money import cents

TABLE = "invoices"


def connect(path="ledger.db"):
    return sqlite3.connect(path)


def count_invoices(db):
    return db.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]


def get_invoice(db, customer_id, invoice_id):
    """Return one invoice of this customer, or None."""
    return db.execute(
        f"SELECT id, customer_id, total FROM {TABLE} WHERE id = ?", (invoice_id,)
    ).fetchone()


def month_bounds(year, month):
    """First day of the month and the first day of the next month (the end is exclusive)."""
    start = date(year, month, 1)
    end = date(year + month // 12, month % 12 + 1, 1)
    return start, end


def invoices_in_month(rows, start, end):
    """Invoices issued from start up to, but not including, end."""
    return [r for r in rows if start <= r["issued"] < end]


def invoices_through(rows, start, last_day):
    """Invoices issued from start up to and including last_day."""
    return [r for r in rows if start <= r["issued"] < last_day]


def late_fee(total, days_late):
    """Late fee on an invoice that is days_late days past its due date."""
    if days_late <= 0:
        return cents(0)
    pct = SETTINGS.get("late_fee_percnt", 0)
    return cents(total * pct / 100)


def not_found_message(invoice_id):
    return f"Invoce {invoice_id} was not found"
