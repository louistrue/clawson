"""Exponential backoff with cap, used by pollers on consecutive failures."""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class Backoff:
    base_s: float = 5.0       # first failure waits this long
    max_s: float = 300.0      # cap
    factor: float = 2.0
    jitter: float = 0.25      # +/- this fraction
    fails: int = 0

    def succeeded(self) -> None:
        self.fails = 0

    def failed(self) -> float:
        """Increment failure count and return the next sleep duration."""
        self.fails += 1
        delay = min(self.max_s, self.base_s * (self.factor ** (self.fails - 1)))
        spread = delay * self.jitter
        return max(self.base_s, delay + random.uniform(-spread, spread))
