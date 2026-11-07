#!/usr/bin/env python3
"""Download the NVD change-history pages the NVD world is built from.

    python fetch_nvd.py --out nvd-raw --plan            # ask each feed for its size, print the cost, download nothing
    python fetch_nvd.py --out nvd-raw                   # download everything (resumable)
    python fetch_nvd.py --out nvd-raw --max-pages 2     # a small slice of every feed

Standard library only. Source: NVD API 2.0 change history (no API key),
https://services.nvd.nist.gov/rest/json/cvehistory/2.0 , one request per page of 2,000 events:

    initial   eventName=Initial Analysis   pages with startIndex 76000 .. 224000 only (about 150,000 events,
                                           2016-03 to early 2024); the feed is ordered by event time
    reanalysis, modified, remap            eventName=Reanalysis | Modified Analysis | CWE Remap, every page

Every page is saved as <out>/<kind>-<startIndex>.json, byte for byte as served. A page that already exists is skipped,
so an interrupted run continues where it stopped. The script sleeps 7 seconds after every request (the public
rate limit without a key is 5 requests per 30 seconds) and retries a failed request up to 5 times with a 30 second
pause.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://services.nvd.nist.gov/rest/json/cvehistory/2.0"
PAGE = 2000
SLEEP = 7
INITIAL_FIRST, INITIAL_LAST = 76_000, 224_000  # startIndex of the first and the last page of the initial window
KINDS = {
    "initial": "Initial Analysis",
    "reanalysis": "Reanalysis",
    "modified": "Modified Analysis",
    "remap": "CWE Remap",
}


def url(kind: str, start: int, per_page: int = PAGE) -> str:
    query = urllib.parse.urlencode({"eventName": KINDS[kind], "resultsPerPage": per_page, "startIndex": start})
    return f"{API}?{query}"


def get(address: str) -> bytes:
    for attempt in range(5):
        try:
            with urllib.request.urlopen(address, timeout=120) as response:
                body = response.read()
            json.loads(body)
            return body
        except (OSError, ValueError) as error:
            print(f"  retry {attempt + 1}/5: {error}", file=sys.stderr, flush=True)
            time.sleep(30)
    raise SystemExit(f"giving up on {address}")


def total(kind: str) -> int:
    count = json.loads(get(url(kind, 0, 1)))["totalResults"]
    time.sleep(SLEEP)
    return count


def starts(kind: str, size: int) -> list[int]:
    if kind == "initial":
        return list(range(INITIAL_FIRST, min(INITIAL_LAST, size - 1) + 1, PAGE))
    return list(range(0, size, PAGE))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--plan", action="store_true", help="only count the pages and print the estimated cost")
    ap.add_argument("--max-pages", type=int, help="download at most N pages of every feed (a slice)")
    ap.add_argument("--kinds", default=",".join(KINDS), help="comma-separated subset of: " + ", ".join(KINDS))
    a = ap.parse_args()
    kinds = [k for k in a.kinds.split(",") if k]
    for k in kinds:
        if k not in KINDS:
            raise SystemExit(f"unknown kind {k!r}")
    a.out.mkdir(parents=True, exist_ok=True)

    sizes = {k: total(k) for k in kinds}
    plan = {k: starts(k, sizes[k]) for k in kinds}
    pages = sum(len(v) for v in plan.values())
    print(f"events per feed: {sizes}")
    print(f"pages: { {k: len(v) for k, v in plan.items()} } = {pages} requests; "
          f"sleep alone {pages * SLEEP / 60:.0f} min, about {pages * 11 / 60:.0f} min with transfer time (11 s per page seen)")
    if a.plan:
        return
    done = 0
    for k in kinds:
        for start in plan[k][: a.max_pages]:
            path = a.out / f"{k}-{start}.json"
            if path.exists():
                continue
            body = get(url(k, start))
            tmp = path.with_suffix(".part")
            tmp.write_bytes(body)
            tmp.rename(path)
            done += 1
            print(f"{k} {start}: {len(body):,} bytes", flush=True)
            time.sleep(SLEEP)
    print(f"downloaded {done} pages into {a.out}")


if __name__ == "__main__":
    main()
