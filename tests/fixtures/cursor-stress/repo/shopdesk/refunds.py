"""Refunds for whole orders."""


def issue_refund(db, order_id):
    """Mark the order as refunded and return the amount to pay back."""
    row = db.execute("SELECT total, refunded FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        raise KeyError(order_id)
    total, refunded = row
    if refunded:
        return 0
    db.execute("UPDATE orders SET refunded = 1 WHERE id = ?", (order_id,))
    db.commit()
    return total
