class InkfootError(Exception):
    """Base for all Inkfoot-raised exceptions."""


class PolicyNotSupported(InkfootError):
    """Raised at instrument() time when a policy is registered against
    an integration pattern that doesn't support it (ADR-0-3). Surfacing
    here is intentional and load-bearing — silent degradation would let
    a user think they have enforcement when they don't."""


class StorageError(InkfootError):
    """Raised by the storage layer for migration or invariant failures
    that cannot be recovered from at the call site."""
