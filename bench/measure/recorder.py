"""Latency / event recorders."""

from __future__ import annotations

from typing import List

from .clock import summarize


class LatencyRecorder:
    """Records per-call latency samples in nanoseconds."""

    def __init__(self, name: str):
        self.name = name
        self.samples: List[int] = []

    def record(self, t_start_ns: int, t_end_ns: int) -> None:
        self.samples.append(t_end_ns - t_start_ns)

    def add(self, latency_ns: int) -> None:
        self.samples.append(latency_ns)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "summary_ns": summarize(self.samples),
            "samples_ns": self.samples,
        }


class EventRateRecorder:
    """Records timestamps of events to derive rate + inter-arrival stats."""

    def __init__(self, name: str):
        self.name = name
        self.timestamps_ns: List[int] = []

    def record(self, t_ns: int) -> None:
        self.timestamps_ns.append(t_ns)

    def to_dict(self) -> dict:
        ts = self.timestamps_ns
        if len(ts) < 2:
            return {
                "name": self.name,
                "count": len(ts),
                "duration_ns": 0,
                "rate_per_s": 0.0,
                "inter_arrival_ns": summarize([]),
            }
        duration_ns = ts[-1] - ts[0]
        rate = (len(ts) - 1) * 1_000_000_000 / duration_ns if duration_ns > 0 else 0.0
        intervals = [ts[i] - ts[i - 1] for i in range(1, len(ts))]
        return {
            "name": self.name,
            "count": len(ts),
            "duration_ns": duration_ns,
            "rate_per_s": round(rate, 3),
            "inter_arrival_ns": summarize(intervals),
        }
