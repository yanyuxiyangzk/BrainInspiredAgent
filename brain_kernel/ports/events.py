"""Minimal immutable message shape understood by the event transport."""

from typing import Protocol


class BusMessage(Protocol):
    @property
    def msg_id(self) -> str: ...

    @property
    def msg_type(self) -> str: ...

    @property
    def source(self) -> str: ...

    @property
    def priority(self) -> int: ...
