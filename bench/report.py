"""Aggregate one or more bench result JSONs into a Markdown table.

Usage:
    python -m bench.report bench/results/                  # all runs in dir
    python -m bench.report bench/results/foo.json bar.json # specific files
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterable, List


def _iter_paths(args: List[str]) -> Iterable[Path]:
    if not args:
        args = ["bench/results"]
    for a in args:
        p = Path(a)
        if p.is_dir():
            yield from sorted(p.glob("*.json"))
        elif p.is_file():
            yield p


def _row(run: dict, r: dict) -> dict:
    out = {
        "run_id": run.get("run_id", "?"),
        "host": run.get("env", {}).get("host", "?"),
        "kernel": run.get("env", {}).get("kernel", "?"),
        "mode": run.get("config", {}).get("mode", "?"),
        "scenario": r.get("scenario", "?"),
        "stack": r.get("stack", "?"),
        "p50_ns": None,
        "p95_ns": None,
        "p99_ns": None,
        "rate": None,
        "n": None,
        "py_cpu_pct": None,
        "btd_cpu_pct": None,
        "dbus_cpu_pct": None,
        "error": r.get("error"),
    }
    res = r.get("result") or {}
    if r.get("scenario") == "read_write":
        rs = res.get("read", {}).get("summary_ns", {})
        out["p50_ns"] = rs.get("p50")
        out["p95_ns"] = rs.get("p95")
        out["p99_ns"] = rs.get("p99")
        out["n"] = rs.get("n")
    elif r.get("scenario") in ("advert", "notify"):
        out["rate"] = res.get("rate_per_s")
        out["n"] = res.get("count")
        ia = res.get("inter_arrival_ns", {})
        out["p50_ns"] = ia.get("p50")
        out["p95_ns"] = ia.get("p95")
        out["p99_ns"] = ia.get("p99")

    cpu = r.get("cpu") or {}
    per_pid = cpu.get("per_pid") or {}
    out["py_cpu_pct"] = (per_pid.get("python") or {}).get("cpu_pct")
    out["btd_cpu_pct"] = (per_pid.get("bluetoothd") or {}).get("cpu_pct")
    out["dbus_cpu_pct"] = (
        (per_pid.get("dbus-broker") or {}).get("cpu_pct")
        or (per_pid.get("dbus-daemon") or {}).get("cpu_pct")
    )
    return out


def _fmt_ns(v):
    if v is None:
        return "—"
    if v < 10_000:
        return f"{v}ns"
    if v < 10_000_000:
        return f"{v/1000:.1f}us"
    return f"{v/1_000_000:.1f}ms"


def _fmt_pct(v):
    return "—" if v is None else f"{v:.2f}%"


def render(rows: List[dict]) -> str:
    header = "| scenario | stack | mode | host | p50 | p95 | p99 | rate | n | py-cpu | btd-cpu | dbus-cpu | error |"
    sep    = "|---|---|---|---|---|---|---|---|---|---|---|---|---|"
    lines = [header, sep]
    for row in rows:
        rate = "—" if row["rate"] is None else f"{row['rate']:.1f}/s"
        n = "—" if row["n"] is None else str(row["n"])
        err = (row["error"] or "").replace("|", "/")
        lines.append(
            f"| {row['scenario']} | {row['stack']} | {row['mode']} | {row['host']} | "
            f"{_fmt_ns(row['p50_ns'])} | {_fmt_ns(row['p95_ns'])} | {_fmt_ns(row['p99_ns'])} | "
            f"{rate} | {n} | "
            f"{_fmt_pct(row['py_cpu_pct'])} | {_fmt_pct(row['btd_cpu_pct'])} | {_fmt_pct(row['dbus_cpu_pct'])} | "
            f"{err} |"
        )
    return "\n".join(lines)


def main(argv: List[str]) -> int:
    rows: List[dict] = []
    for path in _iter_paths(argv):
        try:
            with open(path) as f:
                run = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"# skipping {path}: {e}", file=sys.stderr)
            continue
        for r in run.get("results", []):
            rows.append(_row(run, r))
    if not rows:
        print("# no results found", file=sys.stderr)
        return 1
    print(render(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
