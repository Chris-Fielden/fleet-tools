"""Plain checks for orders and sign-in. Run: python -m checks.checks_orders"""
from shopdesk import auth, orders


def check_line_total():
    assert orders.line_total(5, 3) == 15


def check_order_total_without_discount():
    assert orders.order_total([(10, 2)]) == 24.0


def check_sign_in_wrong_password():
    assert auth.sign_in("admin", "nope") is False


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("check_"):
            fn()
    print("ok")
