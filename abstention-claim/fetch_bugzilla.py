#!/usr/bin/env python3
"""Download the Mozilla Bugzilla bug list and field histories the Bugzilla world is built from.

    python fetch_bugzilla.py --out bz-raw --plan                       # count bugs per product, print the cost
    python fetch_bugzilla.py --out bz-raw                              # everything (resumable, long)
    python fetch_bugzilla.py --out bz-raw --products "Firefox Build System,WebExtensions"   # a slice

Standard library only. Source: the public Bugzilla REST API, https://bugzilla.mozilla.org/rest , anonymous read.

Stage 1 (bugs): for each product, every bug created after 2025-01-01 and before 2026-10-01 (as the API reads those two
dates), 1,000 per request, ordered by bug id. Saved as <out>/bugs-<product>.jsonl, one slim JSON object per bug:
id, creation_time, product, component, summary, platform, op_sys, severity, type, keywords, and `owner`, the first 12
hex digits of the SHA-256 of the reporter's account name. The account name itself is never written to disk.

Stage 2 (history): for every bug whose component now is not Untriaged, General or Other, the history of the fields
product, component, summary, platform, op_sys, severity, type and keywords, 100 bugs per request
(bug/<first id>/history?ids=...). Saved as <out>/history.jsonl, one line per bug:
{"id": ..., "changes": [[when, field, removed, added], ...]}, or {"id": ..., "changes": [], "unavailable": true} for a
bug the API will not show anonymously. Nobody's name is stored. Bugs already in the file are skipped on a rerun.

The script sleeps 1 second after every request and retries a failed request with growing pauses.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://bugzilla.mozilla.org/rest"
PRODUCTS = [
    "Core",
    "DevTools",
    "Firefox",
    "Firefox Build System",
    "Firefox for Android",
    "Toolkit",
    "Web Compatibility",
    "WebExtensions",
]
SINCE, UNTIL = "2025-01-01", "2026-10-01"
FIELDS = "id,creation_time,product,component,summary,platform,op_sys,severity,type,keywords,creator"
HISTORY_FIELDS = {"product", "component", "summary", "platform", "op_sys", "severity", "type", "keywords"}
UNTRIAGED = {"Untriaged", "General", "Other"}
PAGE = 1000
BATCH = 100
SLEEP = 1


def call(path: str, params: list[tuple[str, str]]) -> dict:
    address = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    for attempt in range(5):
        try:
            with urllib.request.urlopen(address, timeout=120) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            try:  # an error answer has a JSON body too (for example, a bug that cannot be shown)
                return json.load(error)
            except ValueError:
                time.sleep(5 * (attempt + 1))
        except (OSError, ValueError):
            time.sleep(5 * (attempt + 1))
    raise SystemExit(f"giving up on {address}")


def window(product: str) -> list[tuple[str, str]]:
    return [
        ("product", product),
        ("f1", "creation_ts"), ("o1", "greaterthan"), ("v1", SINCE),
        ("f2", "creation_ts"), ("o2", "lessthan"), ("v2", UNTIL),
    ]  # fmt: skip


def count(product: str) -> int:
    return call("bug", window(product) + [("include_fields", "id"), ("count_only", "1")])["bug_count"]


def pseudonym(account: str) -> str:
    return hashlib.sha256(account.encode("utf-8")).hexdigest()[:12]


def fetch_bugs(out: Path, product: str, limit: int | None) -> None:
    path = out / f"bugs-{product}.jsonl"
    if path.exists():
        print(f"[skip] {product}", flush=True)
        return
    n = 0
    tmp = path.with_suffix(".part")
    with tmp.open("w", encoding="utf-8") as f:
        offset = 0
        while limit is None or n < limit:
            size = PAGE if limit is None else min(PAGE, limit - n)
            bugs = call("bug", window(product) + [("include_fields", FIELDS), ("order", "bug_id"), ("limit", str(size)), ("offset", str(offset))])["bugs"]
            for bug in bugs:
                bug["owner"] = pseudonym(bug.pop("creator", ""))
                bug.pop("creator_detail", None)  # the API adds the reporter's name and address; keep neither
                f.write(json.dumps({k: bug.get(k) for k in sorted(bug)}, ensure_ascii=False) + "\n")
            n += len(bugs)
            offset += len(bugs)
            print(f"  {product}: {n}", flush=True)
            time.sleep(SLEEP)
            if len(bugs) < size:
                break
    tmp.rename(path)


def histories(ids: list[int]) -> list[dict] | None:
    params = [("ids", str(i)) for i in ids[1:]]
    answer = call(f"bug/{ids[0]}/history", params)
    return None if answer.get("error") else answer.get("bugs")


def fetch_history(out: Path, products: list[str]) -> None:
    bugs: list[int] = []
    for product in products:
        for line in (out / f"bugs-{product}.jsonl").open(encoding="utf-8"):
            b = json.loads(line)
            if b["component"] not in UNTRIAGED:
                bugs.append(b["id"])
    path = out / "history.jsonl"
    seen = {json.loads(line)["id"] for line in path.open(encoding="utf-8")} if path.exists() else set()
    todo = sorted(set(bugs) - seen)
    print(f"history: {len(bugs)} bugs, {len(seen)} already here, {len(todo)} to get", flush=True)
    started = time.time()
    with path.open("a", encoding="utf-8") as f:
        for n in range(0, len(todo), BATCH):
            chunk = todo[n : n + BATCH]
            got = histories(chunk)
            if got is None:  # one bug of the batch cannot be shown: ask one by one
                got = []
                for bug in chunk:
                    one = histories([bug])
                    got += one if one is not None else [{"id": bug, "history": None}]
                    time.sleep(SLEEP)
            for bug in got:
                if bug["history"] is None:
                    f.write(json.dumps({"id": bug["id"], "changes": [], "unavailable": True}) + "\n")
                    continue
                changes = [
                    [h["when"], c["field_name"], c.get("removed", ""), c.get("added", "")]
                    for h in bug["history"]
                    for c in h["changes"]
                    if c["field_name"] in HISTORY_FIELDS
                ]
                f.write(json.dumps({"id": bug["id"], "changes": changes}, ensure_ascii=False) + "\n")
            f.flush()
            if (n // BATCH) % 20 == 0:
                print(f"  {n + len(chunk)}/{len(todo)} · {time.time() - started:.0f} s", flush=True)
            time.sleep(SLEEP)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--plan", action="store_true", help="only count the bugs and print the estimated cost")
    ap.add_argument("--products", default=",".join(PRODUCTS), help="comma-separated; default: the eight products")
    ap.add_argument("--max-bugs", type=int, help="fetch at most N bugs per product (a slice)")
    ap.add_argument("--bugs-only", action="store_true", help="stage 1 only (the bug list, without the histories)")
    a = ap.parse_args()
    products = [p for p in a.products.split(",") if p]
    for p in products:
        if p not in PRODUCTS:
            raise SystemExit(f"unknown product {p!r}")
    a.out.mkdir(parents=True, exist_ok=True)
    sizes = {}
    for p in products:
        sizes[p] = count(p)
        time.sleep(SLEEP)
    total = sum(sizes.values())
    pages = sum(-(-s // PAGE) for s in sizes.values())
    print(f"bugs per product: {sizes} = {total}")
    print(f"stage 1: {pages} requests; stage 2: about {-(-total // BATCH)} requests at most (fewer: bugs still in a "
          f"triage component are skipped); plus 1 s sleep after each")
    if a.plan:
        return
    for p in products:
        fetch_bugs(a.out, p, a.max_bugs)
    if not a.bugs_only:
        fetch_history(a.out, products)
    print("done")


if __name__ == "__main__":
    main()
