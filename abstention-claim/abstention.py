#!/usr/bin/env python3
"""Replay a world through the keeper and compare how often abstained and answered decisions are corrected later.

    python abstention.py nvd      <world dir from build_world.py> [--out result.json]
    python abstention.py bugzilla <world dir from build_world.py> [--out result.json]

Uses only the public `gil` package (iyulab-gil) and the standard library.

Replay. One gil.Keeper (target precision 0.9, at least 30 answered) goes through the stream in order. The first 5,000
records are only settled; the keeper then chooses its thresholds, and again every time the settled records have grown
by 10%. The thresholds chosen at one point decide, for the records up to the next choice, whether the keeper answers or
abstains. What each of the two layers (key, similar records) found for a record is read from the decision records that
`keeper.rechoose(True)` returns for the records it replays, which hold only the records settled before. A record is
answered when the key layer reaches its threshold (and the key is not marked as caught), or else when the similar-record
layer reaches its threshold and, for a vote, its nearest record is at least as similar as the chosen floor; otherwise the
keeper abstains. Whether the answer was right plays no part: the split is answered versus abstained.

Counting. Records from the first choice on (position 5,000) are counted. Each counted record is "corrected" if it was
corrected later (NVD: the CVE has a correction entry; Bugzilla: its component at the end differs from the first one).
Records are stratified by

    NVD        year x earlier records of the same CWE x earlier records of the same first-listed vendor
    Bugzilla   quarter x earlier records of the same component x earlier records of the same product

where "earlier" counts every earlier record of the stream (also those before position 5,000) and the count is binned
0 | 1-9 | 10-99 | 100+. The NVD analysis leaves out the CVEs touched by a bulk CWE remap. The risk ratio is the
Mantel-Haenszel ratio of the corrected share of abstained records to that of answered records over the strata that
hold both kinds, with a log-normal 95% interval (Robins-Breslow-Greenland variance).
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import gil

TARGET = 0.9
MINIMUM = 30
START = 5_000
GROWTH = 1.1
TICK = 10_000_000  # one second in the 100-nanosecond ticks of `settled_at`
BINS = (1, 10, 100)  # 0 | 1-9 | 10-99 | 100+


def load(world: Path) -> list[dict]:
    with gzip.open(world / "stream.jsonl.gz", "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f]


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
    """`settled_at` is the stream position, so settlement never goes backwards across calls."""
    items = [
        {
            "id": str(r["id"]),
            "settled_at": (offset + j + 1) * TICK,
            "values": {**dict(r["context"]), slot: r["targets"][slot]},
        }
        for j, r in enumerate(rows)
    ]
    return gil.Records.from_json_lines("\n".join(json.dumps(i) for i in items))


def choice_points(n: int) -> list[int]:
    points = [START]
    while points[-1] < n:
        points.append(max(points[-1] + 1, math.ceil(points[-1] * GROWTH)))
    return [p for p in points if p < n] + [n]


def thresholds(choice: dict, slot: str) -> dict:
    layers = choice["chosen"][slot]
    key = layers["key"]["chosen"]
    memory = layers["memory"]["chosen"]
    return {
        "key": key and key["threshold"],
        "memory": memory and memory["threshold"],
        "memory_floor": memory and memory.get("similarity_floor"),
    }


def answered(look: dict, t: dict) -> bool:
    """Whether a decision record is answered at thresholds `t`."""
    key = look.get("key")
    if key and key.get("caught"):
        return False
    if key and t["key"] is not None and key["strength"] >= t["key"]:
        return True
    vote = look.get("similar")
    return bool(
        vote
        and t["memory"] is not None
        and vote["score"] >= t["memory"]
        and not (vote.get("voted") and t["memory_floor"] is not None and vote["nearest"] < t["memory_floor"])
    )


def replay(rows: list[dict], slot: str) -> tuple[list[bool], list[int], list[dict]]:
    """Per record from position START on: did the keeper answer? Also the choice points and the thresholds chosen."""
    schema = gil.Schema.from_json(schema_json(rows, slot))
    decider = gil.Decider(schema, None, False)  # holds the records; it keeps no similar-record index of its own
    keeper = gil.Keeper(schema, TARGET, MINIMUM)
    points = choice_points(len(rows))
    looks: list[dict] = [{}] * len(rows)
    chosen: list[dict] = []
    previous = 0
    started = time.time()
    for at in points:
        keeper.settle(decider, records(rows[previous:at], slot, previous))
        choice = keeper.rechoose(True)
        by_id = {d["record"]: d for d in choice.get("decisions", [])}
        for i in range(previous, at):
            looks[i] = by_id.get(str(rows[i]["id"]), {})
        chosen.append(thresholds(choice, slot))
        previous = at
        print(f"choice at {at}: {time.time() - started:.0f} s", file=sys.stderr, flush=True)
    flags = [False] * len(rows)
    for point, nxt, t in zip(points, points[1:], chosen):
        for i in range(point, nxt):
            flags[i] = answered(looks[i], t)
    return flags, points, chosen


def bin_of(n: int) -> int:
    return sum(n >= b for b in BINS)


def mantel_haenszel(strata: dict) -> dict:
    """strata[k] = (corrected abstained, abstained, corrected answered, answered); strata with both kinds only."""
    r = s = p = 0.0
    used = corrected = answered_n = 0
    for y1, n1, y0, n0 in strata.values():
        if n1 == 0 or n0 == 0:
            continue
        n = n1 + n0
        m = y1 + y0
        r += y1 * n0 / n
        s += y0 * n1 / n
        p += (n1 * n0 * m - y1 * y0 * n) / (n * n)
        used += 1
        corrected += m
        answered_n += n0
    out = {"strata": used, "corrected": corrected, "answered": answered_n}
    if r == 0 or s == 0:
        return {"rr": None, "lo": None, "hi": None, **out}
    rr = r / s
    se = math.sqrt(p / (r * s))
    return {"rr": rr, "lo": rr * math.exp(-1.96 * se), "hi": rr * math.exp(1.96 * se), **out}


def analyse(rows, flags, start, slot, stratum_of, corrected_of, skip) -> dict:
    """Count abstained/answered records from `start` on, per stratum. `stratum_of(row, seen_slot, seen_group)`."""
    seen_slot: Counter = Counter()
    seen_group: Counter = Counter()
    group_of = stratum_of.group_of
    for i in range(start):
        seen_slot[str(rows[i]["targets"][slot])] += 1
        seen_group[group_of(rows[i])] += 1
    cells: dict[tuple, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    crude = [0, 0, 0, 0]
    counted = 0
    for i in range(start, len(rows)):
        row = rows[i]
        value = str(row["targets"][slot])
        group = group_of(row)
        if not skip(row):
            abstained = not flags[i]
            y = int(corrected_of(row))
            c = cells[stratum_of.key(row, bin_of(seen_slot[value]), bin_of(seen_group[group]))]
            c[0 if abstained else 2] += y
            c[1 if abstained else 3] += 1
            crude[0 if abstained else 2] += y
            crude[1 if abstained else 3] += 1
            counted += 1
        seen_slot[value] += 1
        seen_group[group] += 1
    stratified = mantel_haenszel({k: tuple(v) for k, v in cells.items()})
    crude_result = mantel_haenszel({"all": tuple(crude)})
    return {
        "records_counted": counted,
        "strata_total": len(cells),
        "stratified": stratified,
        "crude": {**crude_result, "abstained": {"corrected": crude[0], "n": crude[1]}, "answered": {"corrected": crude[2], "n": crude[3]}},
    }


class NvdStrata:
    @staticmethod
    def group_of(row):  # the first vendor listed
        return (dict(row["context"]).get("cpe_vendor") or "").split(" ")[0]

    @staticmethod
    def key(row, slot_bin, group_bin):
        return int(row["date"][:4]), slot_bin, group_bin


class BugzillaStrata:
    @staticmethod
    def group_of(row):
        return dict(row["context"]).get("product", "")

    @staticmethod
    def key(row, slot_bin, group_bin):
        return int(row["date"][:4]), (int(row["date"][5:7]) - 1) // 3, slot_bin, group_bin


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
    ap.add_argument("world", choices=["nvd", "bugzilla"])
    ap.add_argument("dir", type=Path, help="the --out directory of build_world.py")
    ap.add_argument("--out", type=Path, help="write the result as JSON")
    ap.add_argument("--records", type=int, help="replay only the first N records (a quick check)")
    a = ap.parse_args()

    rows = load(a.dir)
    if a.records:
        rows = rows[: a.records]
    slot = "cwe" if a.world == "nvd" else "component0"
    flags, points, chosen = replay(rows, slot)
    start = points[0]
    if a.world == "nvd":
        corrected = {json.loads(line)["cve"] for line in (a.dir / "corrections.jsonl").open(encoding="utf-8")}
        remap = set(json.loads((a.dir / "remap_cves.json").read_text(encoding="utf-8")))
        result = analyse(rows, flags, start, slot, NvdStrata, lambda r: r["cve"] in corrected, lambda r: r["cve"] in remap)
    else:
        result = analyse(rows, flags, start, slot, BugzillaStrata, lambda r: r["moved"], lambda r: False)
    result = {
        "world": a.world,
        "records": len(rows),
        "choices": len(chosen),
        "records_from_first_choice": len(rows) - start,
        "answered_from_first_choice": sum(flags[start:]),
        "answered_share": sum(flags[start:]) / (len(rows) - start),
        **result,
    }
    result["crude_over_stratified"] = (
        result["crude"]["rr"] / result["stratified"]["rr"] if result["stratified"]["rr"] and result["crude"]["rr"] else None
    )
    text = json.dumps(result, indent=1)
    if a.out:
        a.out.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
