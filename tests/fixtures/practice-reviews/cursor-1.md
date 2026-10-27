Reviewer: Cursor (composer-2.5), run 1
Commit: 860a69f61c18fd8f3a42857271026977a60b6038
Exit code: 0

Reviewing the branch diff and related files.


VERDICT: FAIL

MUST FIX:
1. `shopdesk/orders.py`, line 15 — Discount uses `percent / 10` instead of `/ 100`, so e.g. 10% off wipes the total and checkout math is wrong. Use `percent / 100` (and add a check that asserts discounted totals).
2. `shopdesk/orders.py`, line 13 — Settings key is `max_discount_percnt`; config defines `max_discount_percent`, so the 30% cap is never applied and `min(percent, 100)` allows excessive discounts. Use `max_discount_percent` and the configured cap.
3. `shopdesk/orders.py`, line 25 — Customer is interpolated into SQL (`f"... '{customer}'"`), enabling SQL injection. Use a parameterized query (`?`) with `(customer,)`.
4. `shopdesk/orders.py`, line 31 — `page_of` slice ends at `(page + 1) * size + 1`, returning 21 rows per page instead of 20. End the slice at `(page + 1) * size`.
5. `shopdesk/invoices.py`, line 9 — `read_invoice` joins `name` without sanitization, so paths like `../` can read files outside `INVOICE_DIR`. Reject `..`, separators, and absolute paths; resolve under `INVOICE_DIR` only.
6. `shopdesk/invoices.py` (module) — TICKET requires customers to download only their own invoices; there is no ownership check (only raw `read_invoice`). Add an authorized download path that verifies the order belongs to the caller before reading.
7. `shopdesk/auth.py`, line 11 — Hardcoded `admin` / `admin123` credentials in source. Load secrets from config/env and hash passwords; do not commit plaintext passwords.
8. `shopdesk/auth.py`, line 21 — `session_timeout_minuts` misses `session_timeout_minutes`, so timeout is 0 and `session_valid` treats every session as valid forever (`limit == 0`). Use the correct settings key so sessions expire per config.

SHOULD FIX:
1. `shopdesk/reports.py`, line 17 — `is_overdue` uses `time.time() < due_timestamp`, so future due dates read as “overdue”; invert to `time.time() > due_timestamp`.
2. `shopdesk/reports.py`, line 21 — `retry_limt` should be `retry_limit` so configured retries are honored.
3. `shopdesk/reports.py`, lines 9–10 — Month-end via `year + month // 12` is brittle; use `datetime`/`calendar` for the next month’s start.
4. `checks/checks_orders.py` — No checks for discount cap, paging size, invoice access, monthly totals, or refunds; add coverage for SHOP-12 behavior.
5. `shopdesk/orders.py`, line 2 — `sqlite3` is imported but unused.
6. `shopdesk/invoices.py`, line 14 — User-facing typo “Ordr” → “Order”.
7. `README.md` — “seperate” → “separate”.

SUMMARY: The branch adds the SHOP-12 skeleton but core ticket behavior is broken or missing: discount math and caps, 20-item paging, session expiry, and invoice authorization are wrong or absent, and `find_orders` plus `read_invoice` introduce injection/path traversal risk alongside hardcoded admin credentials. Existing checks only cover the no-discount order total and a failed login, so they would still pass while these defects remain. No repository rules file was present in the tree reviewed.
