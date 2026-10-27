"""Settings for the billing service. Read once at start-up."""

SETTINGS = {
    "currency": "EUR",
    "invoice_due_days": 30,
    "late_fee_percent": 2,
    "max_retries": 3,
    "batch_size": 50,
    "tier_discount_percent": 10,
    "tier_min_units": 10,
}
