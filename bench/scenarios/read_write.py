"""Sequential GATT read & write RTT scenario."""

from __future__ import annotations

from typing import Any

from bench.config import BenchConfig
from bench.measure.clock import perf_ns
from bench.measure.recorder import LatencyRecorder
from .common import base_result, make_cpu_tracker


async def run(stack: Any, cfg: BenchConfig, target_mac: str) -> dict:
    out = base_result(cfg, scenario="read_write", stack_name=getattr(stack, "NAME", type(stack).__name__))

    handle = await stack.setup_gatt(target_mac)
    payload = bytes((i % 256 for i in range(cfg.payload_size)))

    try:
        # warmup (not timed)
        for _ in range(cfg.warmup_iters):
            await stack.read(handle, cfg.char_read_uuid)
            await stack.write(handle, cfg.char_write_uuid, payload)

        read_rec = LatencyRecorder("read_rtt_ns")
        write_rec = LatencyRecorder("write_rtt_ns")

        cpu = make_cpu_tracker(cfg)
        cpu.start()
        for _ in range(cfg.rtt_iters):
            t0 = perf_ns()
            await stack.read(handle, cfg.char_read_uuid)
            t1 = perf_ns()
            read_rec.record(t0, t1)

            t0 = perf_ns()
            await stack.write(handle, cfg.char_write_uuid, payload)
            t1 = perf_ns()
            write_rec.record(t0, t1)
        cpu.stop()

        out["result"] = {
            "read": read_rec.to_dict(),
            "write": write_rec.to_dict(),
        }
        out["cpu"] = cpu.to_dict()
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        try:
            await stack.teardown(handle)
        except Exception as e:  # noqa: BLE001
            out["notes"].append(f"teardown error: {type(e).__name__}: {e}")

    return out
