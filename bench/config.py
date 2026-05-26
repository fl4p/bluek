"""Benchmark configuration.

Single dataclass owning every knob the suite respects. Constructed once in
``runner.py`` from CLI args and passed down to scenarios and stacks.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


# UUIDs for the benchmark peripheral's GATT db. Picked from the unassigned
# 16-bit range so we don't collide with any standard service in scans.
SVC_UUID = "0000fbe0-0000-1000-8000-00805f9b34fb"
CHR_READ_UUID = "0000fbe1-0000-1000-8000-00805f9b34fb"
CHR_WRITE_UUID = "0000fbe2-0000-1000-8000-00805f9b34fb"
CHR_NOTIFY_UUID = "0000fbe3-0000-1000-8000-00805f9b34fb"


@dataclass
class BenchConfig:
    # --- targets ---
    target: Optional[str] = None              # MAC of the BLE peripheral
    adapter: str = "hci0"                     # central adapter (hciN)
    peripheral_adapter: Optional[str] = None  # hciN for the local bumble peripheral, if any

    # --- mode selection ---
    mode: str = "integration"                 # "integration" | "micro"
    use_mock_peripheral: bool = False         # spawn bumble on peripheral_adapter

    # --- iteration counts ---
    duration_s: float = 10.0                  # advert / notify window
    rtt_iters: int = 1000                     # read/write samples
    warmup_iters: int = 50
    warmup_s: float = 2.0
    notify_rate_hz: int = 200                 # peripheral firing rate
    payload_size: int = 20                    # bytes for read/write/notify char

    # --- scenario / stack selection ---
    scenarios: tuple = ("advert", "read_write", "notify")
    stacks: tuple = ("raw_l2cap", "raw_dbus", "lib_bluek", "lib_bleak")

    # --- bluetoothd coordination ---
    isolate_adapter: bool = False             # btmgmt power off/on around GATT runs
    coexistence: str = "default"              # "default" | "isolated"

    # --- output ---
    results_dir: str = "bench/results"
    run_id: Optional[str] = None              # default: ISO timestamp + host + kernel

    # --- characteristic UUIDs (overridable for non-bumble targets) ---
    char_read_uuid: str = CHR_READ_UUID
    char_write_uuid: str = CHR_WRITE_UUID
    char_notify_uuid: str = CHR_NOTIFY_UUID

    # --- env capture, populated by jsonout ---
    env: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)
