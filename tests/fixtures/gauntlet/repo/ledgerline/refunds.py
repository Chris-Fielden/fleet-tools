"""Refunds and fee waivers, done by staff."""
from .money import cents


def issue_refund(db, invoice_id, amount, staff):
    """Refund part of an invoice. Staff must be signed in."""
    if not staff.get("signed_in"):
        raise PermissionError("sign in first")
    db.execute("UPDATE invoices SET refunded = refunded + ? WHERE id = ?", (str(cents(amount)), invoice_id))
    db.commit()


def waive_late_fee(db, invoice_id, staff):
    """Set the late fee of an invoice to zero."""
    if not staff.get("signed_in"):
        raise PermissionError("sign in first")
    db.execute("UPDATE invoices SET late_fee = 0 WHERE id = ?", (invoice_id,))
    db.commit()
