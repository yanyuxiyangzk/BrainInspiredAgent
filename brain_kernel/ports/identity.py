"""Identifier generation abstraction."""

from typing import Protocol
from uuid import UUID


class UuidGenerator(Protocol):
    """Create UUID values without coupling callers to randomness or wall time."""

    def new(self) -> UUID:
        """Return the next unique identifier."""
