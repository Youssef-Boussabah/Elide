"""Bounded negotiation and plan explanation around the deterministic planner.

The model may only phrase things inside limits computed here; every model output is
checked by `guards` and by the P2 solver/verifier before it is shown. Drafts never become
authorized terms, approvals or payments.
"""
