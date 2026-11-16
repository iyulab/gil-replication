#!/usr/bin/env python3
"""Re-count the realized-precision monitor for other windows and margins, from one trace written by alarm.py.

    python window_margin.py <alarm result.json> <alarm trace.jsonl> [--world NAME] [--out cells.json]

`alarm.py` takes no action on an alarm, so the answered records' right/wrong column of its trace is enough to apply the
rule again: raise an alarm when, among the last W answered records, the share that was right is below
(target - margin). The counting rules are alarm.py's own: after an alarm the monitor is quiet for the next 5,000 records,
and a new alarm needs at least W answered records since the previous one.

Before any other cell is counted, the reference cell (W = 100, margin 0.15) is replayed and must give exactly the alarm
list alarm.py wrote. If it does not, the script stops without counting ("replica check failed"): the rule here would not
be the rule that ran.

Per cell it reports the alarms, the false alarms (alarms before the revision), the delay (records from the revision to
the first alarm after it) and a label: `false alarm` (any alarm before the revision), `on time` (first alarm less than
1,000 records after the revision), `tie` (exactly 1,000), `late` (later, or none).

Standard library only.
"""

from __future__ import annotations

import argparse
import json
from collections import deque
from pathlib import Path

QUIET = 5_000
WINDOWS = (50, 100, 200)
MARGINS = (0.10, 0.15, 0.20)
DELAY_LINE = 1_000


def replay(trace: list[dict], boundary: int, window: int, margin: float, target: float) -> dict:
    line = target - margin
    win: deque[int] = deque(maxlen=window)
    judged_since = 0
    quiet_until = -1
    alarms = []
    min_pre = None  # lowest window share seen before the revision once the window was full (for reading, not for the label)
    for r in trace:
        if not r["answered"]:
            continue
        win.append(1 if r["right"] else 0)
        judged_since += 1
        if len(win) < window:
            continue
        share = sum(win) / window
        if r["at"] < boundary:
            min_pre = share if min_pre is None else min(min_pre, share)
        if share < line and r["at"] > quiet_until and judged_since >= window:
            alarms.append({"at": r["at"], "after_boundary": r["at"] - boundary, "share": share})
            quiet_until, judged_since = r["at"] + QUIET, 0
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
        "window": window,
        "margin": margin,
        "line": round(line, 4),
        "alarms": len(alarms),
        "false_alarms": false_alarms,
        "delay": delay,
        "min_pre_window_share": min_pre,
        "label": label,
        "alarm_list": alarms,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("result", type=Path, help="the --out file of alarm.py")
    ap.add_argument("trace", type=Path, help="the --trace file of alarm.py")
    ap.add_argument("--world", default="", help="a name for the output (e.g. chicago, osha)")
    ap.add_argument("--target", type=float, default=0.9, help="the target alarm.py ran with (default 0.9)")
    ap.add_argument("--out", type=Path, help="write the cells as JSON")
    a = ap.parse_args()

    result = json.loads(a.result.read_text(encoding="utf-8"))
    trace = [json.loads(x) for x in a.trace.read_text(encoding="utf-8").splitlines()]
    boundary = result["boundary"]
    check = replay(trace, boundary, 100, 0.15, a.target)
    want = [(x["at"], x["after_boundary"]) for x in result["alarm_list"]]
    got = [(x["at"], x["after_boundary"]) for x in check["alarm_list"]]
    verified = want == got
    out = {
        "world": a.world,
        "result": a.result.name,
        "trace": a.trace.name,
        "boundary": boundary,
        "records": len(trace),
        "validation": {"verified": verified, "alarm_py": want, "replica": got},
        "cells": [],
    }
    if verified:
        for w in WINDOWS:
            for m in MARGINS:
                cell = replay(trace, boundary, w, m, a.target)
                cell["is_reference_cell"] = w == 100 and m == 0.15
                out["cells"].append(cell)
    if a.out:
        a.out.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{a.world}: replica check {'passed' if verified else 'failed'}  alarm.py {want}  replica {got}")
    for c in out["cells"]:
        print(
            f"  W={c['window']:<4} margin={c['margin']:.2f} line={c['line']:.2f}  {c['label']:<11} "
            f"alarms {c['alarms']} false {c['false_alarms']} delay {c['delay']}  "
            f"min pre-revision window share {c['min_pre_window_share']}"
        )


if __name__ == "__main__":
    main()
