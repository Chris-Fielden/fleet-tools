"""Money helpers. Amounts are in the shop currency."""
from decimal import ROUND_HALF_EVEN, Decimal


def cents(amount):
    """Round to two places the way finance books it (banker's rounding)."""
    return Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


def invoice_total(lines):
    """Sum of price * quantity over (price, qty) lines."""
    total = 0.0
    for price, qty in lines:
        total += float(price) * qty
    return round(total, 2)


def split_payment(total, parts):
    """Split a total into equal parts; the first part takes the leftover cent."""
    share = cents(Decimal(str(total)) / parts)
    first = cents(Decimal(str(total)) - share * (parts - 1))
    return [first] + [share] * (parts - 1)
