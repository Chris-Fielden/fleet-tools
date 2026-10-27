Reviewer: Claude (opus)
Commit: 860a69f61c18fd8f3a42857271026977a60b6038

VERDICT: FAIL

MUST FIX:
1. `shopdesk/orders.py:15`. `apply_discount` divides by 10 instead of 100. A 10% code takes the whole order to 0, and anything above 10% gives a negative total. **Fix:** use `total * (1 - percent / 100)`. **Reason:** WRONG BEHAVIOR.
2. `shopdesk/orders.py:13`. The settings key is misspelled as `"max_discount_percnt"`, so the lookup always falls back to 100 and the 30% shop maximum the ticket asks for is never applied. Negative percents are also not rejected. **Fix:** use `SETTINGS["max_discount_percent"]` (indexing, so a typo raises an error instead of falling back silently) and clamp to `max(0, min(percent, cap))`. **Reason:** WRONG BEHAVIOR.
3. `shopdesk/orders.py:25`. `find_orders` puts `customer` into the SQL with an f-string, which is an SQL injection hole. For example, `customer = "x' OR '1'='1"` returns every customer's orders. **Fix:** `db.execute("SELECT id, total FROM orders WHERE customer = ?", (customer,))`. **Reason:** SECURITY.
4. `shopdesk/invoices.py:7-10`. `read_invoice` joins a caller-supplied name onto `INVOICE_DIR` with no checks, so `../../etc/passwd` or an absolute path reads any file. It also never checks that the invoice belongs to the customer asking, which the ticket requires ("a customer must only be able to download their own invoice"). **Fix:** take the customer and order id rather than a file name, check ownership in the DB, build the file name on the server, and confirm the resolved path stays inside `INVOICE_DIR`. **Reason:** SECURITY.
5. `shopdesk/auth.py:11`. The admin login `admin`/`admin123` is written into the source. That is a secret in the code and a default login anyone can guess. **Fix:** load staff logins from a user store or environment, and store and compare password hashes (e.g. `hmac.compare_digest` on a salted hash). **Reason:** SECURITY.
6. `shopdesk/auth.py:21`. The key is misspelled as `"session_timeout_minuts"`, so the limit comes out as 0, which the code treats as "no limit". Staff sessions never expire, even though the config sets 30 minutes. **Fix:** use `SETTINGS["session_timeout_minutes"]`. **Reason:** SECURITY.
7. `shopdesk/orders.py:31`. `page_of` slices to `(page + 1) * size + 1`, so each page returns 21 orders and the last order of one page shows up again on the next. **Fix:** drop the `+ 1`. **Reason:** WRONG BEHAVIOR.
8. `shopdesk/reports.py:17`. `is_overdue` is the wrong way round: it returns True before the due time and False after it. **Fix:** `return time.time() >= due_timestamp`. **Reason:** WRONG BEHAVIOR.
9. `shopdesk/reports.py:21`. The key is misspelled as `"retry_limt"`, so it falls back to 0 and `fetch_with_retry` tries only once instead of the configured 3 retries. **Fix:** use `SETTINGS["retry_limit"]`. **Reason:** WRONG BEHAVIOR.

SHOULD FIX:
1. `shopdesk/refunds.py:6-14`. The read and the update are separate steps, so two refunds running at the same moment can both see `refunded = 0` and both return the total, paying it out twice. Use `UPDATE orders SET refunded = 1 WHERE id = ? AND refunded = 0` and check `rowcount`, or wrap both steps in one transaction.
2. `shopdesk/refunds.py:4`. Nothing checks that the caller is signed-in staff. The ticket says only staff may issue refunds, so the HTTP layer or this function needs to call `auth.session_valid`.
3. `checks/checks_orders.py`. The checks cover almost none of the new features. There is nothing for discounts (including the cap), paging, invoice ownership or path traversal, the monthly total, refunds, session expiry or `is_overdue`. Checks for any of these would have caught MUST FIX items 1, 2, 4 and 6–9. Nothing existing was weakened, but please add them.
4. The config keys are read with `.get()` plus a fallback all over the code, which hid three typos. Index `SETTINGS[...]` directly, or define the keys as constants.
5. `shopdesk/reports.py:28`. If `attempts` ever ends up 0, `raise last` would raise `None` (a `TypeError`). Guard against that.
6. `shopdesk/auth.py:6`. Sessions live in an in-memory dict keyed only by user name. There is no session token, they are lost on restart, and they don't work with more than one process.
7. `shopdesk/invoices.py:14`. The customer-facing message has a typo: "Ordr" should be "Order". README.md also has "seperate".
8. Environment note: the brief mentions `CLAUDE.md` and `scripts/check.sh`, but neither exists in this repo or on the branch, so I couldn't check the changes against any CLAUDE.md rules. The README points to `python -m checks.checks_orders` instead. As instructed, I did not run any checks. There is no Kubernetes YAML in the diff.

SUMMARY: The branch adds discount codes, invoice download, refunds, paging and reports, but several core features don't work as the ticket asks. The discount maths is wrong and the cap is never applied, paging returns 21 orders a page, and `is_overdue` is inverted. There are also serious security holes: SQL injection in `find_orders`, path traversal and no ownership check on invoice download, a hardcoded admin password, and staff sessions that never expire. The checks barely cover the new code, which is how these got through `check.sh`.
