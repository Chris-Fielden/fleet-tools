Reviewer: Cursor (composer-2.5), run 3
Commit: 860a69f61c18fd8f3a42857271026977a60b6038
Exit code: 0

Reviewing the branch diff and related files.


VERDICT: FAIL

MUST FIX:
1. `shopdesk/orders.py`, 25 — `find_orders` builds SQL with an f-string and embeds `customer`, enabling SQL injection. Use a parameterized query (`WHERE customer = ?`) and pass `(customer,)`. Security.
2. `shopdesk/invoices.py`, 7–10 — `read_invoice(name)` opens `os.path.join(INVOICE_DIR, name)` with no basename check or link to the authenticated customer, so path traversal (e.g. `../…`) and arbitrary file read are possible, and SHOP-12’s “only their own invoice” rule is not enforced. Validate/sanitize `name`, resolve under `INVOICE_DIR`, and authorize against the order/customer before read. Security and wrong behavior.
3. `shopdesk/orders.py`, 15 — `apply_discount` uses `(1 - percent / 10)` instead of dividing by 100, so non-zero discounts are wildly wrong (e.g. 10% → zero net). Use `/ 100`. Wrong behavior for checkout/discount codes and invoice totals.
4. `shopdesk/orders.py`, 13 — Settings key is `max_discount_percnt` but `config.py` defines `max_discount_percent`, so the cap defaults to 100 and the 30% shop maximum is never applied. Use the correct key. Wrong behavior per ticket.
5. `shopdesk/orders.py`, 31 — `page_of` slice ends at `(page + 1) * size + 1`, returning 21 rows per page instead of 20. Drop the `+ 1` on the stop index. Wrong behavior for paging.
6. `shopdesk/reports.py`, 17 — `is_overdue` returns `time.time() < due_timestamp`, which is true before the due date; overdue should be `time.time() > due_timestamp` (or `>=` per product rules). Wrong behavior.
7. `shopdesk/auth.py`, 21 — `session_timeout_minuts` misses `session_timeout_minutes` in config, so `limit` is 0 and `session_valid` treats every session as valid forever (`limit == 0`). Fix the key so staff sessions expire. Security / wrong behavior.
8. `shopdesk/auth.py`, 11 — Hardcoded `admin` / `admin123` credentials in source. Move to config/env or a proper credential store; do not ship default passwords in code. Security / secrets in code.

SHOULD FIX:
1. `shopdesk/reports.py`, 21 — Typo `retry_limt` ignores `retry_limit` in config; align the key so retries match settings.
2. `shopdesk/invoices.py`, 14 — User-facing string typo `Ordr` → `Order`.
3. `checks/checks_orders.py` — Add checks for discount capping, discount math, `page_of` page size, `monthly_total` boundaries, `is_overdue`, and invoice access so SHOP-12 behavior is regression-tested (current checks only cover zero discount and line total).
4. `shopdesk/orders.py`, 2 — Unused `sqlite3` import.
5. `shopdesk/refunds.py` — No checks for `issue_refund` (missing order, double refund, amount returned); add coverage if refunds are in scope for this ticket.

SUMMARY: The branch adds the shopdesk skeleton and a few passing smoke checks, but core SHOP-12 behavior is broken or missing: discount math and cap typos, off-by-one paging, inverted overdue logic, non-expiring sessions, SQL injection in order lookup, and invoice reads without path or ownership controls. Fix the MUST FIX items before merge; there is no separate repository rules file beyond the ticket and README.
