#!/usr/bin/env python3
"""Carry memory over a code-list revision, or start it at the revision: two keepers, one stream.

    python carryover.py <stream.jsonl.gz> --slot <decided attribute> --boundary <form_version after the revision>
                        [--before N] [--after N] [--arms N,C] [--out result.json]

Uses only the public `gil` package (iyulab-gil) and the standard library. The stream is the output of
build_world.py: one JSON record per line, in settlement order, with a `form_version` side column.

The stream is cut where `form_version` first equals `--boundary`. The records after the cut are the same for both
arms; the arms differ in what the keeper remembers when the revision arrives:

  N  starts at the revision: the records before it are not given to the keeper.
  C  carries over: the records before it are given as they were, codes unchanged.

Each arm has one gil.Keeper (target precision 0.9, at least 30 answered). The keeper chooses its thresholds once at
the revision (arm N has nothing to choose from yet and answers nothing until it has settled records), again after
250, 500, 1,000, 2,000 and 4,000 records following the revision, and from there every time the settled records have
grown by 10%. The thresholds chosen at one point answer the records up to the next choice. What the key layer and the
similar-record layer found for a record is read from the decision records of the choice that replays it, which hold
only the records settled before it.

For each bucket of records after the revision, per arm: answered, precision (share of answers that were right),
promise (mean of what the answering layer promised for the thresholds in force) and delta = promise - precision,
with a Wilson 95% interval.
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
EARLY_POINTS = (250, 500, 1_000, 2_000, 4_000)
BUCKETS = ((0, 1_000), (1_000, 5_000), (5_000, 20_000), (20_000, None))


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


def records(rows: list[dict], slot: str, offset: int) -> gil.Records:
    """Settled records for the keeper. `offset` is the position of the first row in the stream: a record's
    `settled_at` is its stream position, so settlement never goes backwards across calls."""
    items = []
    for j, r in enumerate(rows):
        item = {
            "id": str(r["id"]),
            "settled_at": (offset + j + 1) * TICK,
            "values": {**dict(r["context"]), slot: r["targets"][slot]},
        }
        if r.get("batch"):
            item["batch"] = str(r["batch"])
        items.append(item)
    return gil.Records.from_json_lines("\n".join(json.dumps(i) for i in items))


def thresholds(choice: dict, slot: str) -> dict:
    layers = choice["chosen"][slot]
    key = layers["key"]["chosen"]
    memory = layers["memory"]["chosen"]
    return {
        "key": key and key["threshold"],
        "key_promise": key and key["promise"],
        "memory": memory and memory["threshold"],
        "memory_floor": memory and memory.get("similarity_floor"),
        "memory_promise": memory and memory["promise"],
    }


def answer(look: dict, t: dict) -> tuple[bool, float] | None:
    """Whether a decision record is answered at thresholds `t`, and if so (was it right, what the layer promised).

    The key layer answers first when its strength reaches its threshold (and the key is not marked as caught);
    otherwise the similar-record layer answers when its score reaches its threshold and, for a vote, its nearest
    record is at least as similar as the chosen similarity floor.
    """
    key = look.get("key")
    if key and key.get("caught"):
        return None
    if key and t["key"] is not None and key["strength"] >= t["key"]:
        return bool(key["correct"]), t["key_promise"]
    vote = look.get("similar")
    if (
        vote
        and t["memory"] is not None
        and vote["score"] >= t["memory"]
        and not (vote.get("voted") and t["memory_floor"] is not None and vote["nearest"] < t["memory_floor"])
    ):
        return bool(vote["correct"]), t["memory_promise"]
    return None


def wilson(correct: int, n: int, z: float = 1.96) -> tuple[float, float]:
    p = correct / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / (1 + z * z / n), (centre + margin) / (1 + z * z / n)


def refit_points(after: int) -> list[int]:
    """Positions (records after the revision) where thresholds are chosen: 0, the early points, then +10% steps."""
    points = [p for p in EARLY_POINTS if p < after]
    while points[-1] < after:
        points.append(max(points[-1] + 1, math.ceil(points[-1] * GROWTH)))
    return [0] + [p for p in points if p < after] + [after]


def split(rows: list[dict], boundary: str) -> tuple[list[dict], list[dict]]:
    """Cut where `form_version` first equals `boundary`; no earlier-edition record may follow."""
    at = next(i for i, r in enumerate(rows) if r["form_version"] == boundary)
    late = sum(r["form_version"] != boundary for r in rows[at:])
    if late:
        raise SystemExit(f"{late} records of an earlier edition after the boundary")
    return rows[:at], rows[at:]


class Arm:
    def __init__(self, schema_text: str, slot: str, memory: list[dict]):
        schema = gil.Schema.from_json(schema_text)
        self.slot = slot
        self.memory = memory
        # The keeper replays its own records; the decider only has to hold them, so it keeps no similar-record index.
        self.decider = gil.Decider(schema, None, False)
        self.keeper = gil.Keeper(schema, TARGET, MINIMUM)
        self.known = {str(r["targets"][slot]) for r in memory}
        self.settled = 0

    def choose(self, rows: list[dict]) -> tuple[dict | None, list[dict]]:
        """Settle `rows`, choose again, and return (thresholds, decision records of the records replayed)."""
        if rows:
            self.keeper.settle(self.decider, records(rows, self.slot, self.settled))
            self.settled += len(rows)
        if self.settled == 0:
            return None, []
        choice = self.keeper.rechoose(True)
        return thresholds(choice, self.slot), choice.get("decisions", [])


def run(stream: Path, slot: str, boundary: str, before_n, after_n, arms: tuple[str, ...]) -> dict:
    rows = load(stream, slot)
    before, after = split(rows, boundary)
    if before_n:
        before = before[-before_n:]
    if after_n:
        after = after[:after_n]
    schema_text = schema_json(before + after, slot)
    keep = {a: Arm(schema_text, slot, before if a == "C" else []) for a in arms}
    out: dict = {
        "stream": stream.name,
        "boundary": boundary,
        "before": len(before),
        "after": len(after),
        "carried": {a: len(k.memory) for a, k in keep.items()},
    }

    # How often a record after the revision carries a code the arm's memory has not seen.
    new_value = {a: [0, 0] for a in arms}  # [within the first 1,000, all]
    for a, k in keep.items():
        known = set(k.known)
        for i, r in enumerate(after):
            v = str(r["targets"][slot])
            if v not in known:
                new_value[a][1] += 1
                new_value[a][0] += i < 1_000
            known.add(v)
    out["new_value"] = {a: {"first_1000": v[0], "all": v[1]} for a, v in new_value.items()}

    points = refit_points(len(after))
    chosen: dict[str, dict | None] = {}
    seen: dict[str, dict[str, dict]] = {a: {} for a in arms}
    segments = []
    for k, at in enumerate(points):
        t0 = time.perf_counter()
        prev = points[k - 1] if k else 0
        for a in arms:
            fresh = (keep[a].memory if k == 0 else []) + after[prev:at]
            th, looked = keep[a].choose(fresh)
            chosen[a] = th
            for d in looked:
                seen[a][d["record"]] = d
        if k + 1 < len(points):
            segments.append((at, points[k + 1], dict(chosen)))
        print(f"choice {k + 1}/{len(points)} at +{at}: {time.perf_counter() - t0:.1f} s", file=sys.stderr, flush=True)

    tally = {a: [[0, 0, 0.0, 0] for _ in BUCKETS] for a in arms}  # answered, correct, promise sum, lookups
    for begin, end, th in segments:
        for i in range(begin, end):
            b = next(j for j, (lo, hi) in enumerate(BUCKETS) if i >= lo and (hi is None or i < hi))
            rid = str(after[i]["id"])
            for a in arms:
                s = tally[a][b]
                s[3] += 1
                look = seen[a].get(rid)
                if th[a] is None or look is None:
                    continue
                got = answer(look, th[a])
                if got is None:
                    continue
                s[0] += 1
                s[1] += got[0]
                s[2] += got[1]
    out["arms"] = {}
    for a in arms:
        rows_out = []
        for (lo, hi), (n, c, p, lookups) in zip(BUCKETS, tally[a]):
            row = {"from": lo, "until": hi, "lookups": lookups, "answered": n, "answer_rate": n / lookups if lookups else None}
            if n:
                lo95, hi95 = wilson(c, n)
                row.update(
                    {
                        "precision": c / n,
                        "promise": p / n,
                        "delta": p / n - c / n,
                        "delta_95": [p / n - hi95, p / n - lo95],
                    }
                )
            rows_out.append(row)
        out["arms"][a] = rows_out
    return out


def table(result: dict) -> str:
    lines = [
        f"stream {result['stream']}  boundary {result['boundary']}  before {result['before']}  after {result['after']}",
        f"{'arm':<4}{'records after':<16}{'lookups':>8}{'answered':>9}{'answer rate':>12}{'precision':>10}{'promise':>9}{'delta':>8}  delta 95%",
    ]
    for a, buckets in result["arms"].items():
        for b in buckets:
            if not b["lookups"]:
                continue
            span = f"{b['from']:,}-" + ("end" if b["until"] is None else f"{b['until']:,}")
            if b["answered"]:
                tail = f"{b['precision']:>10.3f}{b['promise']:>9.3f}{b['delta']:>+8.3f}  [{b['delta_95'][0]:+.3f}, {b['delta_95'][1]:+.3f}]"
            else:
                tail = f"{'-':>10}{'-':>9}{'-':>8}"
            lines.append(f"{a:<4}{span:<16}{b['lookups']:>8}{b['answered']:>9}{b['answer_rate']:>12.3f}{tail}")
    return "\n".join(lines)


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
    ap.add_argument("--before", type=int, help="keep only the last N records before the revision")
    ap.add_argument("--after", type=int, help="keep only the first N records after the revision")
    ap.add_argument("--arms", default="N,C", help="comma-separated, from N and C (default N,C)")
    ap.add_argument("--out", type=Path, help="write the result as JSON")
    a = ap.parse_args()
    arms = tuple(x for x in a.arms.split(",") if x in ("N", "C"))
    if not arms:
        raise SystemExit("--arms must name N and/or C")
    result = run(a.stream, a.slot, a.boundary, a.before, a.after, arms)
    if a.out:
        a.out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(table(result))


if __name__ == "__main__":
    main()
