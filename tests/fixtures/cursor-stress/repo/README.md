# shopdesk

A tiny order desk: orders, discount codes, invoices and refunds.

Run the checks with `python -m checks.checks_orders`.
Settings live in `shopdesk/config.py`; the order and customer data are kept in
seperate tables of one SQLite file.
