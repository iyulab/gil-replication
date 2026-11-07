#!/usr/bin/env python3
"""Build the two replay streams used by the revision-claim reproduction.

    python build_world.py chicago <inspections.csv> [<inspections.csv> ...] --out chicago.stream.jsonl.gz
    python build_world.py osha <severe-injury.zip | severe-injury.csv> --out osha.stream.jsonl.gz

Standard library only. Each output line is one settled record:

    {"id": ..., "owner": ..., "context": [[name, value], ...], "targets": {slot: code},
     "date": ..., <world-specific side columns>, "form_version": ...}

`context` holds what is observed about the record, `targets` the code a person assigned, and the side columns
(`date`, `form_version`, ...) are for the harness and are never shown to the keeper. Records are ordered by
event date and then by source id; `id` counts the output lines from 1.

Chicago food inspections: one record per violation listed in an inspection. The target slot is `violation`, the
violation number the inspector wrote. The inspectors' comments have every violation title (and every comma- or
semicolon-separated piece of a title that is at least 12 characters long) blanked out, so the comment cannot
simply repeat the title of the code. `form_version` is "2010" for inspections before 2018-07-01 and "2018" from
that date on, when the city's food code revision took effect and violation numbers were rewritten.

OSHA severe injury reports: one record per report with all four OIICS codes present. The target slots are
`nature`, `part`, `event` and `source`. `form_version` is "v2" for events before 2024-01 and "v3" from 2024-01 on,
when the OIICS 3.0 edition replaced the injury codes.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import re
import sys
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path

# ----------------------------------------------------------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------------------------------------------------------


def text(value) -> str:
    """A source cell as one line of text: empty if missing, whitespace collapsed."""
    return " ".join(str(value or "").split())


def pseudonym(value) -> str:
    """First 12 hex digits of the SHA-256 of the upper-cased, whitespace-collapsed text; empty stays empty."""
    key = text(value).upper()
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12] if key else ""


def blank(value) -> bool:
    return value is None or not str(value).strip()


def build_rows(records: list[dict], observed: tuple[str, ...], judged: tuple[str, ...], require_all: bool):
    """Turn records into stream rows.

    A record is a dict with `owner`, `context` (list of (name, value)), `targets`, `order` (sort key), `key`
    (identity for duplicate removal) and `side` (extra output columns). Records are sorted by `order` (stable),
    a repeated `key` keeps only its first record, blank values are dropped, context follows the order of
    `observed`, and ids count the kept rows from 1.
    """
    rows = sorted(records, key=lambda r: r["order"])
    seen_keys: set[str] = set()
    out: list[dict] = []
    dropped: Counter = Counter()
    for r in rows:
        if r["key"] in seen_keys:
            dropped["duplicate"] += 1
            continue
        seen_keys.add(r["key"])
        targets = {name: value for name, value in r["targets"].items() if not blank(value)}
        if not targets or (require_all and len(targets) < len(judged)):
            dropped["incomplete_targets"] += 1
            continue
        values: dict[str, str] = {}
        for name, value in r["context"]:
            if name in observed and not blank(value):
                values.setdefault(name, value)
        out.append(
            {
                "id": len(out) + 1,
                "owner": r["owner"],
                "context": [[name, values[name]] for name in observed if name in values],
                "targets": {name: targets[name] for name in judged if name in targets},
                **r["side"],
            }
        )
    return out, dict(dropped)


def write_stream(rows: list[dict], path: Path) -> None:
    """Write gzip JSON lines with a fixed header (no timestamp, no name), so equal rows give equal bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with (
        open(path, "wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as packed,
        io.TextIOWrapper(packed, encoding="utf-8", newline="\n") as out,
    ):
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")


# ----------------------------------------------------------------------------------------------------------------
# Chicago food inspections
# ----------------------------------------------------------------------------------------------------------------

CHICAGO_OBSERVED = ("facility_type", "risk", "inspection_type", "zip", "comments")
CHICAGO_SINCE = "2017-01-01"
CHICAGO_BOUNDARY = "2018-07-01"  # first day of the revised violation numbering
COMMENT_CHARS = 500
LEAK_MIN_CHARS = 12
VIOLATION = re.compile(r"(\d+)\.\s*(.*?)\s*(?:-\s*Comments:\s*(.*))?$", re.DOTALL)


def split_violations(cell: str) -> list[tuple[str, str, str]]:
    """The `violations` cell as (number, title, comment) triples; a violation with no comment has comment ""."""
    out = []
    for part in (cell or "").split("|"):
        m = VIOLATION.match(part.strip())
        if m:
            out.append((m.group(1), text(m.group(2)), text(m.group(3))))
    return out


def title_pattern(titles: set[str]):
    """One case-insensitive pattern for every title and every comma/semicolon piece of one, longest first."""
    phrases = set()
    for title in titles:
        phrases.add(title)
        phrases.update(p.strip() for p in re.split(r"[;,]", title))
    phrases = sorted((p for p in phrases if len(p) >= LEAK_MIN_CHARS), key=len, reverse=True)
    if not phrases:
        return None
    return re.compile("|".join(re.escape(p) for p in phrases), re.IGNORECASE)


def chicago_records(rows: list[dict], since: str):
    counts: Counter = Counter()
    kept = []
    titles: set[str] = set()
    for r in rows:
        counts["inspections"] += 1
        date = text(r.get("inspection_date"))[:10]
        if date < since:
            counts["before_since"] += 1
            continue
        found = split_violations(r.get("violations"))
        if not found:
            counts["no_violation"] += 1
            continue
        for i, v in enumerate(found):
            titles.add(v[1])
            kept.append((r, date, i, v))
    pattern = title_pattern(titles)
    out = []
    for r, date, i, (code, _, comment) in kept:
        masked = text(pattern.sub(" ", comment)) if pattern else comment
        if masked != comment:
            counts["comment_masked"] += 1
        inspection = text(r.get("inspection_id"))
        out.append(
            {
                "owner": pseudonym(r.get("license_")),
                "context": [
                    ("facility_type", text(r.get("facility_type")).upper()),
                    ("risk", text(r.get("risk"))),
                    ("inspection_type", text(r.get("inspection_type")).upper()),
                    ("zip", text(r.get("zip"))),
                    ("comments", masked[:COMMENT_CHARS].rstrip()),
                ],
                "targets": {"violation": code},
                "order": (date, int(inspection or 0), i),
                "key": f"{inspection}:{i}",
                "side": {
                    "date": date,
                    "batch": inspection,
                    "form_version": "2010" if date < CHICAGO_BOUNDARY else "2018",
                },
            }
        )
    counts["violations"] = len(out)
    return out, counts


def build_chicago(sources: list[Path], out: Path, since: str) -> dict:
    csv.field_size_limit(sys.maxsize)
    rows: list[dict] = []
    for source in sources:
        with open(source, encoding="utf-8", newline="") as f:
            rows.extend(csv.DictReader(f))
    rows.sort(key=lambda r: (text(r.get("inspection_date"))[:10], int(r.get("inspection_id") or 0)))
    records, counts = chicago_records(rows, since)
    stream, dropped = build_rows(records, CHICAGO_OBSERVED, ("violation",), require_all=False)
    write_stream(stream, out)
    return {
        "world": "chicago",
        "counts": dict(counts),
        "dropped": dropped,
        "rows": len(stream),
        "form_versions": dict(sorted(Counter(r["form_version"] for r in stream).items())),
    }


# ----------------------------------------------------------------------------------------------------------------
# OSHA severe injury reports
# ----------------------------------------------------------------------------------------------------------------

OSHA_OBSERVED = (
    "employer",
    "state",
    "city",
    "naics",
    "hospitalized",
    "amputation",
    "loss_of_eye",
    "narrative",
)
OSHA_JUDGED = {"nature": "Nature", "part": "Part of Body", "event": "Event", "source": "Source"}
OSHA_BOUNDARY_MONTH = "2024-01"  # first month coded with OIICS 3.0


def count_text(value: str) -> str:
    """A count cell such as "1.00" as an integer string."""
    v = text(value)
    try:
        return str(int(float(v)))
    except ValueError:
        return v


def osha_records(rows: list[dict]):
    counts: Counter = Counter(rows=len(rows))
    out = []
    for r in rows:
        try:
            date = datetime.strptime(text(r["EventDate"]), "%m/%d/%Y").date().isoformat()
        except ValueError:
            counts["no_date"] += 1
            continue
        targets = {k: text(r[col]) for k, col in OSHA_JUDGED.items()}
        if not all(targets.values()):
            counts["no_code"] += 1
            continue
        employer = pseudonym(r["Employer"])
        out.append(
            {
                "owner": employer,
                "context": [
                    ("employer", employer),
                    ("state", text(r["State"]).upper()),
                    ("city", text(r["City"]).upper()),
                    ("naics", text(r["Primary NAICS"])),
                    ("hospitalized", count_text(r["Hospitalized"])),
                    ("amputation", count_text(r["Amputation"])),
                    ("loss_of_eye", count_text(r["Loss of Eye"])),
                    ("narrative", text(r["Final Narrative"])),
                ],
                "targets": targets,
                "order": (date, text(r["ID"])),
                "key": text(r["ID"]),
                "side": {
                    "date": date,
                    "year": date[:4],
                    "form_version": "v3" if date[:7] >= OSHA_BOUNDARY_MONTH else "v2",
                },
            }
        )
    counts["kept"] = len(out)
    return out, counts


def read_osha(source: Path) -> list[dict]:
    csv.field_size_limit(sys.maxsize)
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".csv")]
            if len(names) != 1:
                raise SystemExit(f"expected exactly one CSV in {source}, found {names}")
            with z.open(names[0]) as raw:
                return list(csv.DictReader(io.TextIOWrapper(raw, encoding="latin-1", newline="")))
    with open(source, encoding="latin-1", newline="") as f:
        return list(csv.DictReader(f))


def build_osha(source: Path, out: Path) -> dict:
    records, counts = osha_records(read_osha(source))
    stream, dropped = build_rows(records, OSHA_OBSERVED, tuple(OSHA_JUDGED), require_all=True)
    write_stream(stream, out)
    return {
        "world": "osha",
        "counts": dict(counts),
        "dropped": dropped,
        "rows": len(stream),
        "form_versions": dict(sorted(Counter(r["form_version"] for r in stream).items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="world", required=True)
    c = sub.add_parser("chicago", help="Chicago food inspections CSV file(s) -> stream")
    c.add_argument("source", type=Path, nargs="+", help="inspection CSV file(s), any order")
    c.add_argument("--since", default=CHICAGO_SINCE, help="keep inspections on or after this date (default %(default)s)")
    c.add_argument("--out", type=Path, default=Path("chicago.stream.jsonl.gz"))
    o = sub.add_parser("osha", help="OSHA severe injury reports (zip or CSV) -> stream")
    o.add_argument("source", type=Path, help="the downloaded zip, or the CSV inside it")
    o.add_argument("--out", type=Path, default=Path("osha.stream.jsonl.gz"))
    args = parser.parse_args()
    if args.world == "chicago":
        summary = build_chicago(args.source, args.out, args.since)
    else:
        summary = build_osha(args.source, args.out)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
