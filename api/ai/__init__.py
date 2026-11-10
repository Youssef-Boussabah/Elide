"""Bounded LLM adapter (07_AI_ML_INTELLIGENCE.md).

The LLM interprets merchant policy text and drafts proposals as typed JSON. It has no
payment credentials, no PayPal access, no tools, and cannot mark funds available or
record consent. Every output is schema-validated and then checked by deterministic code.
"""

from api.ai.adapter import LLMAdapter, LLMUnavailableError, UnconfiguredLLMAdapter, get_llm_adapter

__all__ = ["LLMAdapter", "LLMUnavailableError", "UnconfiguredLLMAdapter", "get_llm_adapter"]
