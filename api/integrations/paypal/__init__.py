"""PayPal provider adapter (Sandbox only).

See docs/project-sources/10_PAYPAL_INTEGRATION_RUNBOOK.md.

PayPal owns external payment facts. A PAID invoice status is provider confirmation only;
it is never treated as proof that funds are spendable (decision D006).
"""
