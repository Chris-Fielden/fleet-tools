Reviewer: Cursor (composer-2.5), run 2
Commit: 860a69f61c18fd8f3a42857271026977a60b6038
Exit code: 0

Reviewing the diff and related files for bugs, security, and rule violations.


VERDICT: FAIL

MUST FIX:
1. `shopdesk/orders.py`, 25 — `find_orders` builds SQL with an f-string and embeds `customer` directly, enabling SQL injection. Use a parameterized query (`?` placeholder and bound parameters). Reason: SECURITY.
2. `shopdesk/invoices.py`, 7–10 — `read_invoice(name)` joins `INVOICE_DIR` with unsanitized `name` (path traversal via `../`) and does not verify the caller owns the order (SHOP-12). Validate/normalize the path under `INVOICE_DIR` and enforce per-customer authorization before read. Reason: SECURITY.
3. `shopdesk/auth.py`, 11 — Hardcoded staff password `admin123` in source. Load credentials from configuration or a secret store and hash passwords; do not commit plaintext secrets. Reason: SECURITY.
4. `shopdesk/orders.py`, 13–15 — `apply_discount` reads `max_discount_percnt` (typo; config key is `max_discount_percent`) and applies `percent / 10` instead of `percent / 100`, so discounts and the shop cap are wrong whenever a code is used. Fix the key and divide by 100. Reason: WRONG BEHAVIOR (core ticket behavior).
5. `shopdesk/orders.py`, 31 — `page_of` slice uses `(page + 1) * size + 1`, returning 21 rows per page when `page_size` is 20. Use `(page + 1) * size` only. Reason: WRONG BEHAVIOR (paging requirement).
6. `shopdesk/auth.py`, 21 — `session_valid` uses `session_timeout_minuts` (typo); missing key defaults to 0, so `limit == 0` makes every session always valid and ignores `session_timeout_minutes: 30`. Use the correct key. Reason: WRONG BEHAVIOR.
7. `shopdesk/reports.py`, 17 — `is_overdue` returns `time.time() < due_timestamp` (not yet due); comment says overdue after the due time passes. Use `time.time() > due_timestamp` (or `>=`). Reason: WRONG BEHAVIOR.
8. `shopdesk/reports.py`, 21 — `fetch_with_retry` uses `retry_limt` (typo); config `retry_limit: 3` is ignored and only one attempt runs. Use `retry_limit`. Reason: WRONG BEHAVIOR.

SHOULD FIX:
1. `shopdesk/orders.py`, 2 — Unused `import sqlite3`.
2. `shopdesk/invoices.py`, 7–10 — No handling for missing/unreadable files (`FileNotFoundError`, `OSError`).
3. `shopdesk/reports.py`, 9–10 — Month-end via `year + month // 12` and `month % 12 + 1` is brittle; prefer `datetime`/`calendar` for the next month boundary.
4. `shopdesk/invoices.py`, 14 — User-facing typo `Ordr` in `invoice_missing_message`.
5. `checks/checks_orders.py` — No checks for capped discounts, discounted `order_total`, paging size, `monthly_total`, session expiry, retries, or invoice access; existing checks would still pass with the bugs above.
6. `README.md`, 6 — Typo `seperate` → `separate`.
7. `shopdesk/refunds.py`, 10–11 — Already-refunded orders return `0` with no signal; consider a distinct result or error so staff do not treat it as a successful refund.

SUMMARY: The branch adds the SHOP-12 surface area but ships serious security gaps (SQL injection, path traversal, no invoice ownership checks, hardcoded password) and several config-key typos that break discounts, paging, sessions, and retries. The bundled checks only cover the no-discount path and a failed login, so they do not catch the main functional regressions; the change should not merge until the MUST FIX items are addressed.
