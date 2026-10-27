# ledgerline

Order and billing service for a small web shop. It takes orders, reserves stock, charges
cards, issues invoices and runs a nightly batch job that bills open accounts.

Run the checks with `python -m checks.checks_money`. The legacy import and the old reports
live in seperate modules and are not used by the nightly job.
