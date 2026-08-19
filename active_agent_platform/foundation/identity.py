"""Sortable production UUIDs and deterministic test UUIDs."""

import secrets
import threading
from collections.abc import Callable, Iterable
from uuid import UUID

from brain_kernel.ports import Clock


class Uuid7Generator:
    """Generate monotonic UUIDv7 values using an injected clock."""

    def __init__(self, clock: Clock, random_bits: Callable[[int], int] = secrets.randbits) -> None:
        self._clock = clock
        self._random_bits = random_bits
        self._last_millis = -1
        self._entropy = 0
        self._lock = threading.Lock()

    def new(self) -> UUID:
        wall_millis = int(self._clock.now().timestamp() * 1000)
        with self._lock:
            millis = max(wall_millis, self._last_millis)
            if millis == self._last_millis:
                self._entropy = (self._entropy + 1) & ((1 << 74) - 1)
                if self._entropy == 0:
                    millis += 1
            else:
                self._entropy = self._random_bits(74) & ((1 << 74) - 1)
            self._last_millis = millis
            entropy = self._entropy

        rand_a = entropy >> 62
        rand_b = entropy & ((1 << 62) - 1)
        value = (millis & ((1 << 48) - 1)) << 80
        value |= 0x7 << 76
        value |= rand_a << 64
        value |= 0b10 << 62
        value |= rand_b
        return UUID(int=value)


class FakeUuidGenerator:
    def __init__(self, values: Iterable[UUID]) -> None:
        self._values = iter(values)

    def new(self) -> UUID:
        try:
            return next(self._values)
        except StopIteration as error:
            raise RuntimeError("fake UUID sequence exhausted") from error
