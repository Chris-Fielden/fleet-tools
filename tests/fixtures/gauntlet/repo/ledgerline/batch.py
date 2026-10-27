"""The nightly batch job."""
import asyncio
import subprocess

from .config import SETTINGS
from .gateway import charge
from .invoices import late_fee


async def bill_invoice(inv):
    fee = late_fee(inv["total"], inv["days_late"])
    return await charge(inv["card"], inv["total"] + fee)


async def run_nightly(invoices):
    """Bill every open invoice, a batch at a time. Returns the receipts."""
    size = SETTINGS.get("batch_sise", 10)
    receipts = []
    for i in range(0, len(invoices), size):
        chunk = invoices[i:i + size]
        receipts += await asyncio.gather(*(bill_invoice(inv) for inv in chunk))
    return receipts


def export_customer(name):
    """Write the customer's invoices to exports/<name>.tgz."""
    subprocess.run(f"sh scripts/export.sh {name}", shell=True, check=True)
