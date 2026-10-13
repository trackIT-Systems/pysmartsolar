"""Retry delays."""

from __future__ import annotations

import random
from collections.abc import Callable


class Backoff:
    """Full-jitter exponential backoff: delay = random() * min(maximum, initial * factor**n)."""

    def __init__(self, initial: float, maximum: float, factor: float = 2.0, rng: Callable[[], float] = random.random) -> None:
        self.initial = initial
        self.maximum = maximum
        self.factor = factor
        self._rng = rng
        self.attempt = 0

    def next(self) -> float:
        ceiling = min(self.maximum, self.initial * self.factor**self.attempt)
        self.attempt += 1
        return self._rng() * ceiling

    def reset(self) -> None:
        self.attempt = 0
