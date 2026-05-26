"""Process memory helpers (Linux /proc-based)."""

from __future__ import annotations

import os


def rss_kib(pid: int) -> int:
    """Resident set size in KiB. Returns 0 if /proc isn't available or pid is gone."""
    try:
        with open(f"/proc/{pid}/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    # e.g. "VmRSS:\t   12345 kB"
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return 0


def self_rss_kib() -> int:
    return rss_kib(os.getpid())
