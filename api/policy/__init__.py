"""Merchant policy drafts (AI-assisted) and their deterministic validation.

Flow: merchant text -> model `PolicyExtraction` (untrusted) -> `validate_policy` (pure,
deterministic) -> `PolicyDraft` (never active) -> `confirm` with the exact draft digest by
the same merchant -> `ConfirmedPolicy` -> `apply_confirmed_policy` to a planning network.
Only a `ConfirmedPolicy` can change planning inputs; a draft can never mutate an invoice,
reserve cash or authorize a payment.
"""
