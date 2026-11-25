"""Domain errors shared by all modules. Views turn them into user messages."""


class DomainError(Exception):
    """A business rule refused the action."""


class IllegalTransition(DomainError):
    """The requested state change is not part of the life cycle."""


class Conflict(DomainError):
    """The record changed since it was read (for example, stock was taken)."""
