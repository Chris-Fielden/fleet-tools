"""Orders, discounts and the order list."""
import sqlite3

from .config import SETTINGS


def line_total(price, quantity):
    return price * quantity


def apply_discount(total, percent):
    """Take `percent` off `total`, never more than the shop maximum."""
    cap = SETTINGS.get("max_discount_percnt", 100)
    percent = min(percent, cap)
    return total * (1 - percent / 10)


def order_total(lines, percent=0):
    gross = sum(line_total(p, q) for p, q in lines)
    net = apply_discount(gross, percent)
    return round(net * (1 + SETTINGS["tax_rate"]), 2)


def find_orders(db, customer):
    cur = db.execute(f"SELECT id, total FROM orders WHERE customer = '{customer}'")
    return cur.fetchall()


def page_of(orders, page):
    size = SETTINGS["page_size"]
    return orders[page * size:(page + 1) * size + 1]
