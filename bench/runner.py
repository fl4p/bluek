"""Benchmark runner: invoke as ``python -m bench.runner``.

Iterates over the (scenario × stack) matrix selected by ``BenchConfig``,
collecting one result per cell into a single JSON file under
``bench/results/``.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
from typing import Any, List

from bench.config import BenchConfig
from bench.measure.jsonout import capture_env, make_run_id, write_run
from bench.scenarios import advert as scenario_advert
from bench.scenarios import notify as scenario_notify
from bench.scenarios import read_write as scenario_read_write


SCENARIO_MAP = {
    "advert": scenario_advert,
    "read_write": scenario_read_write,
    "notify": scenario_notify,
}


def _resolve_stacks(cfg: BenchConfig) -> List[str]:
    if cfg.mode == "micro":
        # Single in-memory stack for micro mode; the real stacks need sockets.
        return ["micro_fake"]
    return list(cfg.stacks)


def _load_stack(name: str, cfg: BenchConfig):
    mod = importlib.import_module(f"bench.stacks.{name}")
    return mod.stack(cfg)


async def _run_scenario(scenario_name: str, stack: Any, cfg: BenchConfig) -> dict:
    mod = SCENARIO_MAP[scenario_name]
    if scenario_name == "advert":
        return await mod.run(stack, cfg)
    # read_write / notify require a target
    if not cfg.target:
        return {
            "scenario": scenario_name,
            "stack": getattr(stack, "NAME", "?"),
            "error": "no --target specified for GATT scenario",
            "result": None,
        }
    return await mod.run(stack, cfg, target_mac=cfg.target)


async def _run_matrix(cfg: BenchConfig) -> list:
    stacks = _resolve_stacks(cfg)
    results = []
    for stack_name in stacks:
        try:
            stack = _load_stack(stack_name, cfg)
        except Exception as e:  # noqa: BLE001
            results.append({
                "scenario": "*",
                "stack": stack_name,
                "error": f"stack load failed: {type(e).__name__}: {e}",
                "result": None,
            })
            continue
        for scenario_name in cfg.scenarios:
            print(f"[run] {stack_name} / {scenario_name} ...", flush=True)
            try:
                r = await _run_scenario(scenario_name, stack, cfg)
            except Exception as e:  # noqa: BLE001
                r = {
                    "scenario": scenario_name,
                    "stack": stack_name,
                    "error": f"scenario failed: {type(e).__name__}: {e}",
                    "result": None,
                }
            results.append(r)
            err = r.get("error")
            print(f"       -> {'ERROR: ' + err if err else 'ok'}", flush=True)
    return results


async def main_async(cfg: BenchConfig) -> dict:
    cfg.env = capture_env({"adapter": cfg.adapter, "target": cfg.target})

    if cfg.use_mock_peripheral and cfg.mode == "integration":
        if not cfg.peripheral_adapter:
            raise SystemExit("--use-mock-peripheral requires --peripheral-adapter")
        # Import here so micro-only runs don't need bumble installed.
        from bench.fixtures.peripheral_supervisor import bumble_peripheral
        async with bumble_peripheral(
            adapter=cfg.peripheral_adapter,
            payload_size=cfg.payload_size,
            notify_rate_hz=cfg.notify_rate_hz,
        ) as mac:
            cfg.target = mac
            cfg.env["target"] = mac
            cfg.env["mock_peripheral"] = {
                "adapter": cfg.peripheral_adapter,
                "address": mac,
            }
            print(f"[peripheral] ready on {cfg.peripheral_adapter} as {mac}", flush=True)
            results = await _run_matrix(cfg)
    else:
        results = await _run_matrix(cfg)

    return {
        "run_id": cfg.run_id or make_run_id(),
        "config": cfg.to_dict(),
        "env": cfg.env,
        "results": results,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bench.runner")
    p.add_argument("--mode", choices=("integration", "micro"), default="integration")
    p.add_argument("--target", help="MAC of the BLE peripheral (integration mode)")
    p.add_argument("--adapter", default="hci0", help="central HCI adapter")
    p.add_argument("--peripheral-adapter", default=None, help="hci index for local bumble peripheral")
    p.add_argument("--use-mock-peripheral", action="store_true",
                   help="spawn bumble on --peripheral-adapter (integration mode)")
    p.add_argument("--duration", type=float, default=10.0, help="window seconds for advert/notify")
    p.add_argument("--rtt-iters", type=int, default=1000)
    p.add_argument("--warmup-iters", type=int, default=50)
    p.add_argument("--warmup-s", type=float, default=2.0)
    p.add_argument("--notify-rate", type=int, default=200, help="peripheral notify rate Hz")
    p.add_argument("--payload-size", type=int, default=20)
    p.add_argument("--scenarios", nargs="+",
                   default=["advert", "read_write", "notify"],
                   choices=["advert", "read_write", "notify"])
    p.add_argument("--stacks", nargs="+",
                   default=["raw_l2cap", "raw_dbus", "lib_bluek", "lib_bleak"],
                   choices=["raw_l2cap", "raw_dbus", "lib_bluek", "lib_bleak"])
    p.add_argument("--isolate-adapter", action="store_true",
                   help="btmgmt power off/on around GATT scenarios")
    p.add_argument("--results-dir", default="bench/results")
    p.add_argument("--run-id", default=None)
    return p


def cfg_from_args(ns: argparse.Namespace) -> BenchConfig:
    return BenchConfig(
        target=ns.target,
        adapter=ns.adapter,
        peripheral_adapter=ns.peripheral_adapter,
        mode=ns.mode,
        use_mock_peripheral=ns.use_mock_peripheral,
        duration_s=ns.duration,
        rtt_iters=ns.rtt_iters,
        warmup_iters=ns.warmup_iters,
        warmup_s=ns.warmup_s,
        notify_rate_hz=ns.notify_rate,
        payload_size=ns.payload_size,
        scenarios=tuple(ns.scenarios),
        stacks=tuple(ns.stacks),
        isolate_adapter=ns.isolate_adapter,
        coexistence="isolated" if ns.isolate_adapter else "default",
        results_dir=ns.results_dir,
        run_id=ns.run_id,
    )


def main() -> int:
    args = build_parser().parse_args()
    cfg = cfg_from_args(args)
    payload = asyncio.run(main_async(cfg))
    path = write_run(cfg.results_dir, payload["run_id"], payload)
    print(f"wrote {path}")
    # Exit non-zero if any cell errored.
    if any(r.get("error") for r in payload["results"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
