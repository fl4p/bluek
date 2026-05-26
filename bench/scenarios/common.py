"""Shared scenario helpers."""

from __future__ import annotations

from typing import Any

from bench.config import BenchConfig
from bench.measure.cpu import CpuTracker


def make_cpu_tracker(_cfg: BenchConfig) -> CpuTracker:
    return CpuTracker.for_default_pids()


def base_result(cfg: BenchConfig, scenario: str, stack_name: str) -> dict:
    return {
        "scenario": scenario,
        "stack": stack_name,
        "mode": cfg.mode,
        "coexistence": cfg.coexistence,
        "bluetoothd_paused": cfg.isolate_adapter,
        "params": {
            "duration_s": cfg.duration_s,
            "rtt_iters": cfg.rtt_iters,
            "warmup_iters": cfg.warmup_iters,
            "warmup_s": cfg.warmup_s,
            "notify_rate_hz": cfg.notify_rate_hz,
            "payload_size": cfg.payload_size,
        },
        "result": None,
        "cpu": None,
        "notes": [],
        "error": None,
    }
