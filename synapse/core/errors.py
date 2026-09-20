"""Errors raised by deterministic Synapse Core validation."""


class InvariantViolation(ValueError):
    """Raised when a Canonical State rule is violated."""
