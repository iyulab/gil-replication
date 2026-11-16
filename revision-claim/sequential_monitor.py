#!/usr/bin/env python3
"""The fixed-window monitor against a one-sided Bernoulli CUSUM on the same traces.

    python sequential_monitor.py calibrate calibration.json
    python sequential_monitor.py run calibration.json cells.json \
        --world osha <alarm result.json> <alarm trace.jsonl> --world chicago <alarm result.json> <alarm trace.jsonl>

Two sub-commands, in this order:

  calibrate  Chooses the CUSUM threshold h from synthetic i.i.d. data only (the traces are not read): for each arm and
             each length L, the smallest h on a 0.1 grid whose estimated average run length before a false alarm,
             counted in answered records, is at least L (400 chains per estimate, chains that reach twice L count as
             2L, so the estimate is a lower bound). Write the table down before the next step.
  run        Reads that table and, on the trace of each world, counts the fixed-window rule R (the rule of alarm.py:
             the last 100 answered records, margin 0.15, as replayed by window_margin.py) and the CUSUM cells. R is first
             replayed and must give exactly the alarm list alarm.py wrote; if not, nothing is counted.

The CUSUM adds, for each answered record, log(p1/p0) if it was right and log((1-p1)/(1-p0)) if it was wrong (a test of a
drop from p0 to p1), keeps the sum at or above zero, and alarms when the sum reaches h. The arms are
  Cp  p0 = 0.90   p1 = 0.75   (in-control level = the promise)
  Cm  p0 = 0.825  p1 = 0.75   (in-control level in the middle between the promise and the alarm line)
After an alarm the sum is reset and the monitor is quiet for 5,000 records, as for the fixed window.

A CUSUM cell is compared with R on the same world: `better` when it is on time and R is not, or both are on time and its
delay is at most half of R's; `worse` in the mirror case (R on time and the cell not, or its delay at least twice R's);
otherwise `tie`. `on time` means: no alarm before the revision and a first alarm less than 1,000 records after it.

Needs numpy. The calibration is seeded and reproducible.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from window_margin import DELAY_LINE, QUIET, replay  # the fixed-window rule is window_margin's, unchanged

TARGET = 0.9
SEED = 20261115
CHAINS = 400
ARMS = {"Cp": (0.90, 0.75), "Cm": (0.825, 0.75)}
LENGTHS = (10_000, 50_000, 250_000)  # L: the lower bound on the average run length before a false alarm
H_STEP = 0.1


def increments(p0: float, p1: float) -> tuple[float, float]:
    """The log-likelihood-ratio increment (when right, when wrong) of a drop from p0 to p1."""
    return math.log(p1 / p0), math.log((1 - p1) / (1 - p0))


def arl0(p0: float, p1: float, h: float, cap: int, key: tuple[int, ...]) -> float:
    """Mean length until S reaches h under i.i.d. Bernoulli(p0). Chains that hit the cap count as cap (a lower bound)."""
    zr, zw = increments(p0, p1)
    rng = np.random.default_rng([SEED, *key])
    s = np.zeros(CHAINS)
    run = np.full(CHAINS, cap, dtype=np.int64)
    alive = np.ones(CHAINS, dtype=bool)
    step = 0
    block = 4096
    while step < cap and alive.any():
        n = min(block, cap - step)
        draws = rng.random((n, CHAINS)) < p0
        for i in range(n):
            s = np.maximum(0.0, s + np.where(draws[i], zr, zw))
            hit = alive & (s >= h)
            if hit.any():
                run[hit] = step + i + 1
                alive[hit] = False
        step += n
    return float(run.mean())


def calibrate(out_path: Path) -> None:
    table = []
    for arm, (p0, p1) in ARMS.items():
        for length in LENGTHS:
            cap = 2 * length

            def ok(k: int, p0=p0, p1=p1, cap=cap, length=length) -> tuple[bool, float]:
                est = arl0(p0, p1, k * H_STEP, cap, (round(p0 * 1000), length, k))
                return est >= length, est

            lo, hi = 0, 1
            while not ok(hi)[0]:
                lo, hi = hi, hi * 2
                if hi > 4096:
                    raise SystemExit(f"h grows without bound: {arm} L={length}")
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if ok(mid)[0]:
                    hi = mid
                else:
                    lo = mid
            _, est = ok(hi)
            row = {
                "arm": arm,
                "p0": p0,
                "p1": p1,
                "L": length,
                "h": round(hi * H_STEP, 1),
                "arl0_estimate": est,
                "chains": CHAINS,
                "cap": cap,
                "arl0_estimate_below_h": ok(hi - 1)[1] if hi > 1 else None,
            }
            table.append(row)
            print(row, flush=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"seed": SEED, "table": table}, ensure_ascii=False, indent=1), encoding="utf-8")


def cusum(trace: list[dict], boundary: int, p0: float, p1: float, h: float) -> dict:
    zr, zw = increments(p0, p1)
    s = 0.0
    quiet_until = -1
    alarms = []
    max_pre = 0.0
    for r in trace:
        if not r["answered"]:
            continue
        s = max(0.0, s + (zr if r["right"] else zw))
        if r["at"] < boundary:
            max_pre = max(max_pre, s)
        if s >= h and r["at"] > quiet_until:
            alarms.append({"at": r["at"], "after_boundary": r["at"] - boundary, "S": s})
            quiet_until = r["at"] + QUIET
            s = 0.0
    false_alarms = sum(a["after_boundary"] < 0 for a in alarms)
    after = [a["after_boundary"] for a in alarms if a["after_boundary"] >= 0]
    delay = after[0] if after else None
    if false_alarms:
        label = "false alarm"
    elif delay is not None and delay < DELAY_LINE:
        label = "on time"
    elif delay is not None and delay == DELAY_LINE:
        label = "tie"
    else:
        label = "late"
    return {
        "alarms": len(alarms),
        "false_alarms": false_alarms,
        "delay": delay,
        "label": label,
        "max_pre_boundary_S": max_pre,
        "alarm_list": alarms,
    }


def compare(cell: dict, ref: dict) -> str:
    """better / worse / tie against R on the same world."""
    c, r = cell["label"], ref["label"]
    cd, rd = cell["delay"], ref["delay"]
    if c == "on time" and (r != "on time" or (cd is not None and rd is not None and cd <= rd / 2)):
        return "better"
    if r == "on time" and (c != "on time" or (cd is not None and rd is not None and cd >= rd * 2)):
        return "worse"
    return "tie"


def run(cal_path: Path, out_path: Path, worlds: list[list[str]]) -> None:
    cal = json.loads(cal_path.read_text(encoding="utf-8"))["table"]
    out: dict = {"validation": {}, "worlds": {}, "arm_headlines": {}}
    traces = {}
    for world, result_path, trace_path in worlds:
        result = json.loads(Path(result_path).read_text(encoding="utf-8"))
        trace = [json.loads(x) for x in Path(trace_path).read_text(encoding="utf-8").splitlines()]
        boundary = result["boundary"]
        ref = replay(trace, boundary, 100, 0.15, TARGET)
        want = [(a["at"], a["after_boundary"]) for a in result["alarm_list"]]
        got = [(a["at"], a["after_boundary"]) for a in ref["alarm_list"]]
        out["validation"][world] = {"verified": want == got, "alarm_py": want, "replica": got}
        if want != got:
            out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
            raise SystemExit(f"replica check failed ({world}): the fixed-window rule here is not the rule alarm.py ran")
        traces[world] = (trace, boundary, ref)
    verdicts: dict[str, list[str]] = {arm: [] for arm in ARMS}
    for world, (trace, boundary, ref) in traces.items():
        cells = []
        for row in cal:
            res = cusum(trace, boundary, row["p0"], row["p1"], row["h"])
            res.update(arm=row["arm"], L=row["L"], h=row["h"])
            res["vs_R"] = compare(res, ref)
            verdicts[row["arm"]].append(res["vs_R"])
            cells.append(res)
        out["worlds"][world] = {"boundary": boundary, "R": ref, "cells": cells}
    for arm, v in verdicts.items():
        out["arm_headlines"][arm] = (
            "better" if all(x == "better" for x in v) else "worse" if all(x == "worse" for x in v) else "split"
        )
        out["arm_headlines"][arm + "_cells"] = v
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    for world, w in out["worlds"].items():
        r = w["R"]
        print(f"{world}: R  {r['label']:<11} alarms {r['alarms']} false {r['false_alarms']} delay {r['delay']}")
        for c in w["cells"]:
            print(
                f"  {c['arm']} L={c['L']:<7} h={c['h']:<5} {c['label']:<11} alarms {c['alarms']} "
                f"false {c['false_alarms']} delay {c['delay']} vs R {c['vs_R']}  max pre S {c['max_pre_boundary_S']:.2f}"
            )
    print("headline:", {k: v for k, v in out["arm_headlines"].items() if not k.endswith("_cells")})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    c = sub.add_parser("calibrate", help="choose the CUSUM thresholds from synthetic data")
    c.add_argument("out", type=Path)
    r = sub.add_parser("run", help="count R and the CUSUM cells on the traces")
    r.add_argument("calibration", type=Path)
    r.add_argument("out", type=Path)
    r.add_argument(
        "--world",
        nargs=3,
        action="append",
        metavar=("NAME", "RESULT", "TRACE"),
        required=True,
        help="a world's name, the --out file and the --trace file of alarm.py (repeat for each world)",
    )
    a = ap.parse_args()
    if a.command == "calibrate":
        calibrate(a.out)
    else:
        run(a.calibration, a.out, a.world)


if __name__ == "__main__":
    main()
