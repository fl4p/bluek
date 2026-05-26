"""Notification-throughput scenario.

Assumes the peripheral is firing notifications on ``cfg.char_notify_uuid``
at ``cfg.notify_rate_hz``. Reports observed rate, inter-arrival stats, and
total count. Compare client-observed rate against the peripheral target to
detect backpressure.
"""

from __future__ import annotations

from typing import Any

from bench.config import BenchConfig
from bench.measure.clock import perf_ns
from bench.measure.recorder import EventRateRecorder
from .common import base_result, make_cpu_tracker


async def run(stack: Any, cfg: BenchConfig, target_mac: str) -> dict:
    out = base_result(cfg, scenario="notify", stack_name=getattr(stack, "NAME", type(stack).__name__))
    out["params"]["target_rate_hz"] = cfg.notify_rate_hz

    handle = await stack.setup_gatt(target_mac)
    rec = EventRateRecorder("notify")
    warmup_deadline_ns = perf_ns() + int(cfg.warmup_s * 1_000_000_000)
    cpu = make_cpu_tracker(cfg)
    cpu_started = False

    try:
        total_duration = cfg.warmup_s + cfg.duration_s
        async for ev in stack.notify_iter(handle, cfg.char_notify_uuid, total_duration):
            if ev.t_ns < warmup_deadline_ns:
                continue
            if not cpu_started:
                cpu.start()
                cpu_started = True
            rec.record(ev.t_ns)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        if cpu_started:
            cpu.stop()
            out["cpu"] = cpu.to_dict()
        try:
            await stack.teardown(handle)
        except Exception as e:  # noqa: BLE001
            out["notes"].append(f"teardown error: {type(e).__name__}: {e}")

    out["result"] = rec.to_dict()
    return out
