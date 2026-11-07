#!/usr/bin/env python3
"""Does the realized-precision watch notice a revision nobody named? One keeper, one stream, no edition declared.

    python alarm.py <stream.jsonl.gz> --slot <decided attribute> --boundary <form_version after the revision>
                    [--after N] [--trace trace.jsonl] [--out result.json]

Uses only the public `gil` package (iyulab-gil) and the standard library. The stream is the output of
build_world.py: one JSON record per line, in settlement order, with a `form_version` side column. Unlike
carryover.py, this script never tells the keeper about the revision: `form_version` is read only to say where the
revision is when the alarms are counted.

The replay is what an application would do. One `gil.Keeper(schema, 0.9, 30, watch_realized=True)` and one
`gil.Decider` (with its similar-record memory) go through the records in settlement order:

  1. The first 5,000 records are settled as typed by a person (`Enter`), then the keeper chooses its thresholds.
     It chooses again whenever the settled records have grown by 10% (5,000, 5,500, 6,051, ...).
  2. Every later record is first answered by the decider with the schema the keeper holds, the thresholds of its last
     choice, and only the record's observed values (its context, not the code). If the answer is trusted it is
     "shown": the record is then settled with the code the source lists, as `Accept` if that equals the answer shown
     and `Correct` if not, with the answer in `suggested`. If nothing is answered the record is settled as `Enter`.
  3. After each judged settlement (`Accept` or `Correct`) the script asks `keeper.stale()`. With `watch_realized` the
     keeper says `{"realized": {"attribute", "right", "judged"}}` when, of the attribute's last 100 answers whose
     rightness came back, fewer than (target - 0.15) = 0.75 were right.

Counting alarms is a rule of this script, not of the package: the package's reason stays set for as long as the last
100 judged answers stay low. An alarm is counted at the first judged settlement where the reason is present, provided
at least 5,000 records have passed since the previous alarm and at least 100 answers were judged since then (so that
the window holds only answers given after the previous alarm). Alarms before the first record of the new edition are
false alarms. The delay is the number of records from the first record of the new edition (position 0) to the first
alarm at or after it; a delay of 54 means the alarm came with the 55th record of the new edition.

The records are positions over the rows that carry the decided attribute. `--after N` keeps only the first N records
of the new edition (a Chicago stream runs far past the revision; the alarm counts below depend on how far it is
followed). `--trace` writes one line per record after the first 5,000: its position, id, whether an answer was shown
and whether it was right.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
import time
from pathlib import Path

import gil

TICK = 10_000_000  # one second in the 100-nanosecond ticks of `settled_at`
TARGET = 0.9
MINIMUM = 30
GROWTH = 1.1
START = 5_000  # records settled before the first choice; the watch starts here
QUIET = 5_000  # records after an alarm during which no new alarm is counted


def load(stream: Path, slot: str) -> list[dict]:
    rows = []
    with gzip.open(stream, "rt", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if slot in r.get("targets", {}):
                rows.append(r)
    return rows


def schema_json(rows: list[dict], slot: str) -> str:
    """Observed attributes = the context names seen in the first 1,000 records; the slot depends on all of them."""
    observed = list(dict.fromkeys(k for r in rows[:1000] for k, _ in r["context"]))
    return json.dumps(
        {
            "name": "world",
            "attributes": [{"name": k, "role": "observed"} for k in observed]
            + [{"name": slot, "role": "decided", "depends_on": observed}],
        }
    )


def choice_points(n: int) -> set[int]:
    """Settled counts at which the keeper chooses again: START, then +10% each time (rounded up)."""
    points, at = set(), START
    while at < n:
        points.add(at)
        at = max(at + 1, math.ceil(at * GROWTH))
    return points


def record_line(r: dict, slot: str, position: int, kind: str, shown: str | None, source: str | None, promise) -> dict:
    item = {
        "id": str(r["id"]),
        "settled_at": (position + 1) * TICK,
        "values": {**dict(r["context"]), slot: r["targets"][slot]},
        "settlements": {slot: kind},
    }
    if r.get("batch"):
        item["batch"] = str(r["batch"])
    if shown is not None:
        item["suggested"] = {slot: shown}
        item["sources"] = {slot: source}
        if promise is not None:
            item["promises"] = {slot: promise}
    return item


def run(stream: Path, slot: str, boundary: str, after: int | None, trace: Path | None) -> dict:
    rows = load(stream, slot)
    at_boundary = next(i for i, r in enumerate(rows) if r["form_version"] == boundary)
    late = sum(r["form_version"] != boundary for r in rows[at_boundary:])
    if late:
        raise SystemExit(f"{late} records of an earlier edition after the boundary")
    if after is not None:
        rows = rows[: at_boundary + after]
    if len(rows) <= START or at_boundary < START:
        raise SystemExit("the stream needs more than 5,000 records, and the revision after the 5,000th")

    schema = gil.Schema.from_json(schema_json(rows, slot))
    decider = gil.Decider(schema)  # with the similar-record memory: the decider answers here
    keeper = gil.Keeper(schema, TARGET, MINIMUM, watch_realized=True)

    keeper.settle(decider, gil.records(record_line(r, slot, i, "Enter", None, None, None) for i, r in enumerate(rows[:START])))
    keeper.rechoose()
    points = choice_points(len(rows))
    promises: dict[str, float | None] = {}

    def promise_of(source: str) -> float | None:
        if source not in promises:
            promises[source] = gil.promise_of(keeper.tuning, slot, source)
        return promises[source]

    out_trace = trace.open("w", encoding="utf-8") if trace else None
    alarms: list[dict] = []
    answered = correct = judged_since = 0
    quiet_until = -1
    started = time.perf_counter()
    for i in range(START, len(rows)):
        r = rows[i]
        values = dict(r["context"])
        answer = decider.answer(keeper.schema, str(r["id"]), slot, values)
        if answer["answered"]:
            shown, source = answer["candidates"][0]["value"], answer["source"]
            right = shown == str(r["targets"][slot])
            kind, promise = ("Accept" if right else "Correct"), promise_of(source)
        else:
            shown = source = promise = right = None
            kind = "Enter"
        keeper.settle(decider, gil.records([record_line(r, slot, i, kind, shown, source, promise)]))
        if out_trace:
            out_trace.write(json.dumps({"at": i, "id": str(r["id"]), "answered": shown is not None, "right": right}) + "\n")
        if shown is not None:
            answered += 1
            correct += right
            judged_since += 1
            reason = keeper.stale()
            if isinstance(reason, dict) and "realized" in reason and i > quiet_until and judged_since >= 100:
                alarms.append({"at": i, "after_boundary": i - at_boundary, **reason["realized"]})
                quiet_until, judged_since = i + QUIET, 0
        if i + 1 in points:
            keeper.rechoose()
            promises.clear()
            print(f"choice at {i + 1}: {time.perf_counter() - started:.0f} s, {len(alarms)} alarms", file=sys.stderr, flush=True)
    if out_trace:
        out_trace.close()

    after_alarms = [a["after_boundary"] for a in alarms if a["after_boundary"] >= 0]
    return {
        "stream": stream.name,
        "records": len(rows),
        "start": START,
        "boundary": at_boundary,
        "answered": answered,
        "precision": correct / answered if answered else None,
        "alarms": len(alarms),
        "false_alarms": sum(a["after_boundary"] < 0 for a in alarms),
        "delay": after_alarms[0] if after_alarms else None,
        "alarm_list": alarms,
    }


def require_pins(**pins: str) -> None:
    """Stop unless every named package is installed in exactly the pinned version."""
    from importlib.metadata import PackageNotFoundError, version

    wrong = []
    for name, want in pins.items():
        try:
            have = version(name)
        except PackageNotFoundError:
            have = "not installed"
        if have != want:
            wrong.append(f"{name}=={want} (found {have})")
    if wrong:
        raise SystemExit(
            "This kit reproduces numbers from exact package versions; run it as the README shows. Needed: "
            + ", ".join(wrong)
        )


def main() -> None:
    require_pins(**{"iyulab-gil": "0.10.0"})
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("stream", type=Path)
    ap.add_argument("--slot", required=True, help="the decided attribute (violation for Chicago, nature for OSHA)")
    ap.add_argument("--boundary", required=True, help="the form_version after the revision (2018 for Chicago, v3 for OSHA)")
    ap.add_argument("--after", type=int, help="follow only the first N records after the revision")
    ap.add_argument("--trace", type=Path, help="write one JSON line per record after the first 5,000")
    ap.add_argument("--out", type=Path, help="write the result as JSON")
    a = ap.parse_args()
    result = run(a.stream, a.slot, a.boundary, a.after, a.trace)
    if a.out:
        a.out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        f"stream {result['stream']}  records {result['records']}  revision at {result['boundary']}  "
        f"answered {result['answered']}  precision {result['precision']:.3f}"
    )
    print(f"alarms {result['alarms']}  false alarms (before the revision) {result['false_alarms']}  delay {result['delay']}")
    for al in result["alarm_list"]:
        print(f"  alarm at {al['at']} ({al['after_boundary']:+d} from the revision): {al['right']} right of {al['judged']}")


if __name__ == "__main__":
    main()
