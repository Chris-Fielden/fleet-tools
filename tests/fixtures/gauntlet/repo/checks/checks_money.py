"""Quick checks. Run: python -m checks.checks_money"""
from datetime import date

from ledgerline.invoices import invoices_through
from ledgerline.money import invoice_total, split_payment


def check_total():
    assert invoice_total([(10.0, 2), (5.5, 1)]) == 25.5


def check_last_day_of_january():
    rows = [{"issued": date(2026, 1, 31)}]
    assert invoices_through(rows, date(2026, 1, 1), date(2026, 1, 31)) == []


def check_split():
    assert sum(split_payment(10, 3)) == 10


if __name__ == "__main__":
    check_total()
    check_last_day_of_january()
    check_split()
    print("ok")
