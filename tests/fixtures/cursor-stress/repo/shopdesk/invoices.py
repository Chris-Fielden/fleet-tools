"""Invoice files."""
import os

from .config import INVOICE_DIR


def read_invoice(name):
    """Return the text of the invoice file called `name`."""
    with open(os.path.join(INVOICE_DIR, name)) as fh:
        return fh.read()


def invoice_missing_message(order_id):
    return f"Ordr {order_id} has no invoice yet"
