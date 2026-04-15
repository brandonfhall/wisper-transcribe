"""LLM client error types."""
from __future__ import annotations


class LLMUnavailableError(RuntimeError):
    """Provider SDK is not installed, endpoint unreachable, or API key missing.

    Raised as a recoverable error so callers can soft-fail with a warning and
    skip the LLM step rather than aborting the whole pipeline.
    """


class LLMResponseError(RuntimeError):
    """The LLM returned a response that could not be parsed or did not conform
    to the requested schema."""
