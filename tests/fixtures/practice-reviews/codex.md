Reviewer: Codex
Commit: 860a69f61c18fd8f3a42857271026977a60b6038

VERDICT: FAIL

MUST FIX:
1. `shopdesk/orders.py:13–15` — Discounts use `/ 10` instead of `/ 100`, and the misspelled setting bypasses the configured 30% cap. A 20% discount produces a negative total. Fix the setting key to `max_discount_percent` and divide by 100. **WRONG BEHAVIOR**
2. `shopdesk/orders.py:25` — Interpolating `customer` into SQL permits injection; a value such as `' OR 1=1 --` returns other customers’ orders. Use a bound SQL parameter. **SECURITY**
3. `shopdesk/invoices.py:7–10` — Invoice reads have neither ownership checks nor path confinement. Callers can request another customer’s invoice or escape the invoice directory using `../` or an absolute path. Resolve invoices through an order owned by the authenticated customer and enforce directory confinement. **SECURITY**
4. `shopdesk/auth.py:11` — A hardcoded, predictable administrator password grants staff access. Replace it with securely provisioned credentials and password-hash verification. **SECURITY**
5. `shopdesk/auth.py:21` — The misspelled `session_timeout_minuts` setting defaults to zero, making sessions valid indefinitely despite the configured 30-minute timeout. Read `session_timeout_minutes`. **SECURITY**
6. `shopdesk/orders.py:31` — Pagination returns 21 orders and overlaps adjacent pages. Remove the extra `+ 1` from the slice endpoint. **WRONG BEHAVIOR**
7. `shopdesk/reports.py:17` — The overdue predicate is reversed: future invoices are overdue and past-due invoices are not. Compare the current time with `>` instead. **WRONG BEHAVIOR**
8. `shopdesk/reports.py:21` — The misspelled `retry_limt` key causes only one attempt despite `retry_limit=3`. Use the configured key so transient failures receive the intended retries. **WRONG BEHAVIOR**

SHOULD FIX:
1. `shopdesk/refunds.py:6–14` — Concurrent refund requests can both read `refunded=0` and return the full amount. Use an atomic conditional update or transaction that serializes the eligibility check and update.
2. `checks/checks_orders.py:5–14` — Add coverage for capped discounts, invoice access restrictions, session expiry, pagination boundaries, overdue dates, and retries. The three existing checks leave these behaviors untested.
3. Repository rules — No `CLAUDE.md` was present in the repository or inspected ancestor directories, so compliance with its rules could not be verified.

SUMMARY: The branch introduces security vulnerabilities and incorrect behavior in discounts, pagination, sessions, and reporting. All changed files were read; no checks or tests were run, as requested. No Kubernetes manifests, dependency changes, or weakened existing tests were present.
