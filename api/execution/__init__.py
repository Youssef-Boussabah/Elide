"""P5 payment execution and reconciliation (ADR-0012).

Drives an approved, revalidated payment intent through a supported PayPal Sandbox
operation (a payee-issued invoice the payer pays in PayPal's hosted page) and reconciles
the provider outcome into the journal. Never pays, captures or approves for the payer;
never imported by `api.ai` or `api.planning`.
"""
