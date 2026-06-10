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


class PolicyBlocked(InkfootError):
    """Raised from the call path when an enforcement decision refuses
    an LLM call outright. The originating SDK call is *not* made.

    Carries enough structured context for the caller to understand
    which budget clause tripped and by how much, without parsing the
    message string:

    * ``clause`` — the budget clause name that was breached
      (e.g. ``"max_nanodollars"``).
    * ``projected`` — the projected value that triggered the block.
    * ``threshold`` — the clause's configured ceiling.
    """

    def __init__(
        self,
        message: str,
        *,
        clause: str | None = None,
        projected: float | None = None,
        threshold: float | None = None,
    ) -> None:
        super().__init__(message)
        self.clause = clause
        self.projected = projected
        self.threshold = threshold
