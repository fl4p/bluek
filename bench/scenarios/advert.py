"""Advertisement-processing scenario.

Drives the stack's ``scan_iter`` for ``cfg.duration_s + cfg.warmup_s``
seconds, discarding the warmup window. Reports adverts/sec, time-to-first
device (post-warmup), and inter-arrival stats.

Note for the D-Bus path: BlueZ coalesces advertisements per-device; the
event rate observed there is NOT a 1:1 mirror of HCI adverts. The result's
``notes`` field records this so cross-stack comparisons of "events/sec" are
read correctly.
"""

from __future__ import annotations

from typing import Any

from bench.config import BenchConfig
from bench.measure.clock import perf_ns, summarize
from bench.measure.recorder import EventRateRecorder
from .common import base_result, make_cpu_tracker


async def run(stack: Any, cfg: BenchConfig) -> dict:
    out = base_result(cfg, scenario="advert", stack_name=getattr(stack, "NAME", type(stack).__name__))
    if out["stack"] in ("raw_dbus", "lib_bleak"):
        out["notes"].append(
            "BlueZ deduplicates advertisements per-device; events/sec are NOT 1:1 with HCI adverts."
        )

    rec = EventRateRecorder(name="adverts")
    first_device_ns: int | None = None
    warmup_deadline_ns = perf_ns() + int(cfg.warmup_s * 1_000_000_000)
    cpu = make_cpu_tracker(cfg)
    cpu_started = False

    total_duration = cfg.warmup_s + cfg.duration_s
    try:
        async for ev in stack.scan_iter(total_duration):
            if ev.t_ns < warmup_deadline_ns:
                continue  # warmup; discard
            if not cpu_started:
                cpu.start()
                cpu_started = True
            if first_device_ns is None:
                first_device_ns = ev.t_ns - warmup_deadline_ns
            rec.record(ev.t_ns)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        if cpu_started:
            cpu.stop()
            out["cpu"] = cpu.to_dict()

    summary = rec.to_dict()
    summary["time_to_first_device_ns"] = first_device_ns
    out["result"] = summary
    return out
