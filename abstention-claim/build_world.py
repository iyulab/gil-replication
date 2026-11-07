#!/usr/bin/env python3
"""Build the two replay streams used by the abstention reproduction.

    python build_world.py nvd <dir from fetch_nvd.py> --out nvd-world [--cutoff 2026-10-25T06:30:00]
    python build_world.py bugzilla <dir from fetch_bugzilla.py> --out bz-world [--as-of 2026-10-04T20:05:00Z] [--keep-intake]

Standard library only. Each output stream line is one record, in the order the decisions were made:

    {"id": ..., "context": [[name, value], ...], "targets": {slot: code}, "date": ..., <world columns>}

`context` is what is observed about the record, `targets` the value that was assigned when the record was first
decided, and the other columns are kept for the analysis and never shown to the keeper.

NVD world (writes stream.jsonl.gz, corrections.jsonl, remap_cves.json, build.json)
    A record is a CVE. Its decision is the weakness type (CWE) put on it by the first "Initial Analysis" change
    entry that lists CWEs. Records whose first such entry has no CWE, or more than one, or only a placeholder
    (`NVD-CWE-noinfo`, `NVD-CWE-Other`), are left out, and so is every later "Initial Analysis" entry of a CVE already
    seen. Observed: the vendor and the vendor:product pairs of the CPE configuration of that entry, and the CVSS v3.1
    (else v2) vector components. Order: event time, then CVE id.
    A CVE counts as corrected (corrections.jsonl) if a later "Reanalysis" or "Modified Analysis" entry removes a CWE and
    adds another one that is not a placeholder. CVEs touched by a "CWE Remap" entry (a bulk change of the CWE code
    list on seven days) are listed in remap_cves.json; the analysis leaves them out.
    --cutoff drops every entry created after the given time, which pins the correction history to a snapshot date.

Bugzilla world (writes stream.jsonl.gz, build.json)
    A record is a bug. Its decision is the component the bug had when it was first filed (the `removed` value of its
    first component change, else its current component); its observed fields are rewound to the same moment (first
    `removed` value of each field). Bugs whose first component is Untriaged, General or Other are left out.
    `moved` says the bug's component at the end of the history differs from the first one. A component to which one
    reporter filed 90% or more of at least 100 bugs is an intake queue, not a decision, and is left out unless
    --keep-intake is given. Order: creation time, then bug id.
    --as-of rewinds the component and product of every bug to that time (using the history), so that `moved` counts
    only moves up to then and bugs that were not in an included product (or were in a triage component) then are
    left out. Bugs that have since left the eight products cannot be recovered.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import re
from collections import Counter, defaultdict
from pathlib import Path


def write_stream(rows: list[dict], path: Path) -> None:
    """A gzip stream with no time or name in the header: the same rows give the same bytes."""
    with (
        open(path, "wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as packed,
        io.TextIOWrapper(packed, encoding="utf-8", newline="\n") as out,
    ):
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")


def blank(value) -> bool:
    return value is None or not str(value).strip()


# ---------------------------------------------------------------------------------------------------------------------
# NVD
# ---------------------------------------------------------------------------------------------------------------------

V3 = ("AV", "AC", "PR", "UI", "S", "C", "I", "A")
V2 = ("AV", "AC", "Au", "C", "I", "A")
NVD_OBSERVED = ("cpe_vendor", "cpe_product", *(f"v3_{k}" for k in V3), *(f"v2_{k}" for k in V2))
CPE = re.compile(r"cpe:2\.3:([aho]):([^:]+):([^:]+):")


def pages(raw: Path, kind: str, cutoff: str | None):
    """Change entries of one feed, page by page in start-index order."""
    files = sorted(raw.glob(f"{kind}-*.json"), key=lambda p: int(re.search(r"-(\d+)\.json$", p.name).group(1)))
    if not files:
        raise SystemExit(f"no {kind}-*.json files in {raw}")
    for path in files:
        for entry in json.loads(path.read_text(encoding="utf-8"))["cveChanges"]:
            if cutoff is None or entry["change"]["created"] <= cutoff:
                yield entry["change"]


def cwes(details: list[dict], action: str) -> list[str]:
    key = "newValue" if action == "Added" else "oldValue"
    return [d[key].split(" ", 1)[-1] for d in details if d["type"] == "CWE" and d["action"] == action]


def vector(details: list[dict], kind: str, keys: tuple[str, ...], prefix: str) -> dict[str, str]:
    for d in details:
        if d["type"] == kind and d["action"] == "Added":
            text = d["newValue"].split(" ", 1)[-1].strip("()")
            parts = dict(p.split(":", 1) for p in text.split("/") if ":" in p)
            return {f"{prefix}_{k}": parts.get(k, "") for k in keys}
    return {}


def build_nvd(raw: Path, out: Path, cutoff: str | None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    dropped: Counter = Counter()
    records = []
    for change in pages(raw, "initial", cutoff):
        cve = change["cveId"]
        if cve in seen:
            dropped["later_initial"] += 1
            continue
        seen.add(cve)
        added = cwes(change["details"], "Added")
        if not added:
            dropped["no_cwe"] += 1
            continue
        if len(added) > 1:
            dropped["several_cwe"] += 1
            continue
        if added[0].startswith("NVD-CWE-"):
            dropped["placeholder"] += 1
            continue
        observed = vector(change["details"], "CVSS V3.1", V3, "v3") | vector(change["details"], "CVSS V2", V2, "v2")
        vendors, products = set(), set()
        for d in change["details"]:
            if d["type"] == "CPE Configuration":
                for _part, vendor, product in CPE.findall(d["newValue"]):
                    vendors.add(vendor)
                    products.add(f"{vendor}:{product}")
        observed |= {"cpe_vendor": " ".join(sorted(vendors)), "cpe_product": " ".join(sorted(products))}
        context = [[name, observed[name]] for name in NVD_OBSERVED if not blank(observed.get(name))]
        records.append((change["created"], cve, context, added[0]))
    records.sort(key=lambda r: (r[0], r[1]))
    rows = [
        {"id": i, "context": context, "targets": {"cwe": cwe}, "cve": cve, "date": created}
        for i, (created, cve, context, cwe) in enumerate(records, 1)
    ]
    write_stream(rows, out / "stream.jsonl.gz")

    kept = {r["cve"] for r in rows}
    corrections = []
    for kind in ("reanalysis", "modified"):
        for change in pages(raw, kind, cutoff):
            old, new = cwes(change["details"], "Removed"), cwes(change["details"], "Added")
            if old and new and change["cveId"] in kept and not all(n.startswith("NVD-CWE-") for n in new):
                corrections.append(
                    {"cve": change["cveId"], "date": change["created"], "event": change["eventName"], "old": old, "new": new}
                )
    corrections.sort(key=lambda r: (r["date"], r["cve"]))
    with (out / "corrections.jsonl").open("w", encoding="utf-8") as f:
        for row in corrections:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    remap = sorted({c["cveId"] for c in pages(raw, "remap", cutoff)})
    (out / "remap_cves.json").write_text(json.dumps(remap), encoding="utf-8")
    summary = {
        "documents": len(rows),
        "initial_entries_seen": len(seen) + dropped["later_initial"],
        "dropped": dict(dropped),
        "corrections": len(corrections),
        "corrected_cves": len({c["cve"] for c in corrections}),
        "remap_cves": len(remap),
        "first_date": rows[0]["date"],
        "last_date": rows[-1]["date"],
        "cutoff": cutoff,
    }
    (out / "build.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))


# ---------------------------------------------------------------------------------------------------------------------
# Bugzilla
# ---------------------------------------------------------------------------------------------------------------------

UNTRIAGED = {"Untriaged", "General", "Other"}
BZ_OBSERVED = ("product", "summary", "platform", "op_sys", "severity", "type", "keywords")
INTAKE_MIN_BUGS = 100
INTAKE_SHARE = 0.9


def first_value(history: list[list[str]], field: str, current: str) -> str:
    """The value a field had before its first change, else the current value."""
    for _when, name, removed, _added in history:
        if name == field:
            return removed
    return current


def value_at(history: list[list[str]], field: str, current: str, when: str | None) -> str:
    """The value a field had at `when` (an ISO time); `None` means now."""
    if when is None:
        return current
    for changed, name, removed, _added in history:
        if name == field and changed > when:
            return removed
    return current


def build_bugzilla(raw: Path, out: Path, as_of: str | None, keep_intake: bool) -> None:
    out.mkdir(parents=True, exist_ok=True)
    history: dict[int, list[list[str]]] = {}
    unavailable = 0
    for line in (raw / "history.jsonl").open(encoding="utf-8"):
        h = json.loads(line)
        history[h["id"]] = h["changes"]
        unavailable += bool(h.get("unavailable"))
    bugs = {}
    for path in sorted(raw.glob("bugs-*.jsonl")):
        for line in path.open(encoding="utf-8"):
            b = json.loads(line)
            bugs[b["id"]] = b
    products = {b["product"] for b in bugs.values()}
    dropped: Counter = Counter()
    records = []
    for b in sorted(bugs.values(), key=lambda b: (b["creation_time"], b["id"])):
        hist = history.get(b["id"])
        final = b["component"]
        if as_of is not None:
            if hist is None:
                dropped["no_history"] += 1
                continue
            final = value_at(hist, "component", final, as_of)
            if value_at(hist, "product", b["product"], as_of) not in products:
                dropped["other_product_then"] += 1
                continue
        if final in UNTRIAGED:
            dropped["triage_component_at_end"] += 1
            continue
        if hist is None:
            dropped["no_history"] += 1
            continue
        first = first_value(hist, "component", final)
        if first in UNTRIAGED:
            dropped["triage_component_first"] += 1
            continue
        current = {
            "product": b["product"],
            "summary": b["summary"],
            "platform": b.get("platform") or "",
            "op_sys": b.get("op_sys") or "",
            "severity": b.get("severity") or "",
            "type": b.get("type") or "",
            "keywords": " ".join(sorted(b.get("keywords") or [])),
        }
        context = []
        for name in BZ_OBSERVED:
            value = first_value(hist, name, current[name])
            if not blank(value):
                context.append([name, value])
        records.append(
            {
                "owner": b["owner"],
                "context": context,
                "targets": {"component0": first},
                "bug": b["id"],
                "date": b["creation_time"],
                "moved": final != first,
                "final": final,
            }
        )
    intake: list[str] = []
    if not keep_intake:
        by_component: dict[str, Counter] = defaultdict(Counter)
        for r in records:
            by_component[r["targets"]["component0"]][r["owner"]] += 1
        intake = sorted(
            c
            for c, owners in by_component.items()
            if sum(owners.values()) >= INTAKE_MIN_BUGS and owners.most_common(1)[0][1] / sum(owners.values()) >= INTAKE_SHARE
        )
        records = [r for r in records if r["targets"]["component0"] not in set(intake)]
    rows = [{"id": i, **{k: v for k, v in r.items() if k != "owner"}} for i, r in enumerate(records, 1)]
    write_stream(rows, out / "stream.jsonl.gz")
    summary = {
        "documents": len(rows),
        "moved": sum(r["moved"] for r in rows),
        "intake_components_left_out": intake,
        "dropped": dict(dropped),
        "unavailable_histories": unavailable,
        "as_of": as_of,
    }
    (out / "build.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="world", required=True)
    n = sub.add_parser("nvd")
    n.add_argument("raw", type=Path)
    n.add_argument("--out", type=Path, required=True)
    n.add_argument("--cutoff", help="ignore entries created after this time, e.g. 2026-10-25T06:30:00")
    b = sub.add_parser("bugzilla")
    b.add_argument("raw", type=Path)
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--as-of", help="rewind components and products to this time, e.g. 2026-10-04T20:05:00Z")
    b.add_argument("--keep-intake", action="store_true", help="keep intake-queue components")
    a = ap.parse_args()
    if a.world == "nvd":
        build_nvd(a.raw, a.out, a.cutoff)
    else:
        build_bugzilla(a.raw, a.out, a.as_of, a.keep_intake)


if __name__ == "__main__":
    main()
