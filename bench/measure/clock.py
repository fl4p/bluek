"""Wall-clock timing helpers."""

from __future__ import annotations

import time
from typing import Sequence


def perf_ns() -> int:
    return time.perf_counter_ns()


def percentile(samples: Sequence[int], p: float) -> int:
    """Nearest-rank percentile. `p` is 0..100. Empty input returns 0."""
    if not samples:
        return 0
    if p <= 0:
        return min(samples)
    if p >= 100:
        return max(samples)
    s = sorted(samples)
    # nearest-rank: ceil(p/100 * n) - 1, clamped
    rank = max(0, min(len(s) - 1, int((p / 100.0) * len(s)) - (1 if p == 100 else 0)))
    return s[rank]


def summarize(samples: Sequence[int]) -> dict:
    if not samples:
        return {"n": 0, "min": 0, "p50": 0, "p95": 0, "p99": 0, "max": 0, "mean": 0}
    n = len(samples)
    return {
        "n": n,
        "min": min(samples),
        "p50": percentile(samples, 50),
        "p95": percentile(samples, 95),
        "p99": percentile(samples, 99),
        "max": max(samples),
        "mean": sum(samples) // n,
    }
