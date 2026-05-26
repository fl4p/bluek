"""Per-process CPU time tracker.

Walks /proc to find PIDs by comm name (``bluetoothd``, ``dbus-broker`` etc.),
snapshots ``utime+stime`` at start/stop, reports deltas in nanoseconds.

On non-Linux hosts the tracker is a no-op so the same code paths can run for
smoke tests on macOS.
"""

from __future__ import annotations

import os
import time
from typing import Dict, Optional


def _hz() -> int:
    try:
        return int(os.sysconf("SC_CLK_TCK"))
    except (ValueError, OSError, AttributeError):
        return 100  # conservative default; only used if sysconf fails


def _ticks_to_ns(ticks: int) -> int:
    return ticks * 1_000_000_000 // _hz()


def resolve_pid_by_comm(comm: str) -> Optional[int]:
    """Find a single PID whose /proc/{pid}/comm equals ``comm``. Returns the
    lowest match (typically the daemon parent, not a worker)."""
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    matches = []
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/comm", "r") as f:
                if f.read().strip() == comm:
                    matches.append(int(entry))
        except OSError:
            continue
    return min(matches) if matches else None


def read_proc_cpu_ns(pid: int) -> Optional[int]:
    """utime+stime of pid in ns, or None if unavailable."""
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            data = f.read()
    except OSError:
        return None
    # the comm field may contain spaces and parens; everything after the LAST ')'
    # is the regular space-separated fields starting at field 3 (state).
    close = data.rfind(")")
    if close < 0:
        return None
    fields = data[close + 1:].split()
    # Fields after comm: state(0), ppid(1), pgrp(2), session(3), tty_nr(4),
    # tpgid(5), flags(6), minflt(7), cminflt(8), majflt(9), cmajflt(10),
    # utime(11), stime(12), ...
    try:
        utime = int(fields[11])
        stime = int(fields[12])
    except (IndexError, ValueError):
        return None
    return _ticks_to_ns(utime + stime)


class CpuTracker:
    """Snapshots CPU time for one or more PIDs across a measurement window.

    Usage::

        tracker = CpuTracker.for_default_pids()
        tracker.start()
        ... run scenario ...
        tracker.stop()
        result = tracker.to_dict()
    """

    DEFAULT_COMMS = ("bluetoothd", "dbus-broker", "dbus-daemon")

    def __init__(self, pids: Dict[str, int]):
        # name -> pid; the Python process is added automatically
        self._pids: Dict[str, int] = dict(pids)
        self._pids.setdefault("python", os.getpid())
        self._start_ns: Dict[str, Optional[int]] = {}
        self._stop_ns: Dict[str, Optional[int]] = {}
        self._wall_start: int = 0
        self._wall_stop: int = 0

    @classmethod
    def for_default_pids(cls) -> "CpuTracker":
        pids: Dict[str, int] = {}
        for comm in cls.DEFAULT_COMMS:
            pid = resolve_pid_by_comm(comm)
            if pid is not None:
                pids[comm] = pid
        return cls(pids)

    def start(self) -> None:
        self._wall_start = time.perf_counter_ns()
        self._start_ns = {name: read_proc_cpu_ns(pid) for name, pid in self._pids.items()}

    def stop(self) -> None:
        self._wall_stop = time.perf_counter_ns()
        self._stop_ns = {name: read_proc_cpu_ns(pid) for name, pid in self._pids.items()}

    def to_dict(self) -> dict:
        wall_ns = self._wall_stop - self._wall_start
        per_pid = {}
        for name, pid in self._pids.items():
            start = self._start_ns.get(name)
            stop = self._stop_ns.get(name)
            if start is None or stop is None:
                per_pid[name] = {"pid": pid, "cpu_ns": None, "cpu_pct": None}
                continue
            cpu_ns = stop - start
            cpu_pct = (100.0 * cpu_ns / wall_ns) if wall_ns > 0 else 0.0
            per_pid[name] = {"pid": pid, "cpu_ns": cpu_ns, "cpu_pct": round(cpu_pct, 3)}
        return {"wall_ns": wall_ns, "per_pid": per_pid}
