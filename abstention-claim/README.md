# Abstained decisions are corrected later more often: a reproduction kit

**Claim.** A decision the keeper abstains on is corrected later more often than one it answers.

The kit rebuilds two replay streams from public data, replays each through the published
[`iyulab-gil`](https://pypi.org/project/iyulab-gil/) 0.10.0 package using its public API only, splits the decisions
into answered and abstained, and compares how often each kind is corrected afterwards:

| File | What it does |
| --- | --- |
| `fetch_nvd.py` | Downloads the NVD change-history pages (standard library only, resumable, 7 s between requests). |
| `fetch_bugzilla.py` | Downloads Mozilla Bugzilla bugs and field histories (standard library only, resumable, 1 s between requests). |
| `build_world.py` | Turns the downloaded files into replay streams (standard library only). |
| `abstention.py` | Replays a stream through `gil.Keeper`, splits answered from abstained, and prints the stratified risk ratio (needs `iyulab-gil` only). |

The two worlds:

* **NVD.** A record is a CVE. The decision is the weakness type (CWE) that NVD's first "Initial Analysis" entry put on
  it; it counts as corrected if a later "Reanalysis" or "Modified Analysis" entry removes a CWE and adds a different,
  non-placeholder one.
* **Bugzilla (Mozilla).** A record is a bug. The decision is the component the bug had when it was filed; it counts as
  corrected ("moved") if its component at the end of the history is a different one.

## How the replay works

One `gil.Keeper(schema, 0.9, 30)` (target precision 0.9, at least 30 answered) goes through the stream in order. The
first 5,000 records are settled and the keeper chooses its thresholds; it chooses again every time the settled records
have grown by 10%. The thresholds chosen at one point decide the records up to the next choice. What the two layers
found for a record is read from the decision records that `keeper.rechoose(True)` returns, which hold only records
settled before it. A record is **answered** when the key layer reaches its threshold (and the key is not marked as
caught) or the similar-record layer reaches its threshold (and, for a vote, its nearest record passes the chosen
similarity floor); otherwise the keeper **abstains**. Whether an answer was right plays no part: the split is
answered versus abstained, then corrected later versus not.

Records from position 5,000 on are counted and stratified:

* NVD: year x number of earlier records with the same CWE x number of earlier records with the same vendor (the
  alphabetically first vendor of the record's CPE configuration), each count binned 0 | 1-9 | 10-99 | 100+. CVEs touched
  by one of the seven bulk "CWE Remap" days (a change of the CWE code list, not a per-record correction) are left out.
* Bugzilla: quarter x earlier records with the same component x earlier records with the same product, binned alike.

"Earlier" counts every earlier record of the stream, also those before position 5,000. The risk ratio is the
Mantel-Haenszel ratio of the corrected share among abstained records to that among answered records, over the strata
that contain both kinds, with a log-normal 95% interval (Robins-Breslow-Greenland variance).

## Setup

You need [`uv`](https://docs.astral.sh/uv/). `build_world.py` and the fetch scripts run on any Python 3.9 or newer;
`abstention.py` needs Python 3.11 or newer, which `uv` provides together with the pinned package:

```sh
uv run --no-project --isolated --with iyulab-gil==0.10.0 python abstention.py --help
```

## Get the data

### NVD

Source: NVD API 2.0 change history, <https://services.nvd.nist.gov/rest/json/cvehistory/2.0>, no API key. The world
needs four feeds: `Initial Analysis` (pages with startIndex 76000 to 224000: 150,000 entries, event dates 2016-03 to
2024-01), and all of `Reanalysis`, `Modified Analysis` and `CWE Remap`.

```sh
uv run --no-project python fetch_nvd.py --out nvd-raw --plan     # counts the feeds, prints the cost, downloads nothing (4 requests)
uv run --no-project python fetch_nvd.py --out nvd-raw            # the download; skips pages already present, so it can be restarted
```

Measured cost, 2026-11-01: 126 requests (75 + 8 + 31 + 12) of 2,000 entries each, 445 MB (444,892,536 bytes) in
total. With the 7 s pause the full run took 1,823 s (30 min 23 s; 14.5 s per request including transfer). Wall time
depends on the connection; the pause alone is 15 min.

**Does a new download match the data behind the numbers below?** All 126 pages were downloaded again on 2026-11-01 and compared
with the pages fetched on 2026-10-25 that the numbers below come from, entry by entry (keyed by the entry's `cveChangeId`):

| Feed | Entries then | Entries now | Same entries | Only now | Content of the common entries |
| --- | ---: | ---: | ---: | ---: | --- |
| Initial Analysis (window) | 150,000 | 150,000 | 150,000 | 0 | identical |
| Reanalysis | 15,555 | 15,565 | 15,555 | 10 | identical |
| Modified Analysis | 61,337 | 61,361 | 61,337 | 24 | identical |
| CWE Remap | 23,129 | 23,129 | 23,129 | 0 | identical |

The 34 new entries were created between 2026-10-26 and 2026-10-30, i.e. after the earlier download. The feeds are
ordered by event time and the index window did not move. So the snapshot **can be pinned**: `build_world.py nvd
--cutoff 2026-10-25T06:30:00` ignores every entry created after that time, and the world built that way from the new
download is byte-for-byte the world built from the old one (stream, corrections, remap list). Built without
`--cutoff` and replayed, it gives the same 3,043 correction entries on 3,000 CVEs and the same table below. That may
change as NVD keeps adding entries; use `--cutoff` to be sure.
The ordering of the feed and the stability of old entries are observed behaviours, not documented guarantees.

### Bugzilla

Source: the public Mozilla Bugzilla REST API, <https://bugzilla.mozilla.org/rest>, anonymous read. Bugs created
after 2025-01-01 and before 2026-10-01 in eight products (Core, DevTools, Firefox, Firefox Build System, Firefox for
Android, Toolkit, Web Compatibility, WebExtensions), plus, for every bug not sitting in a triage component
(Untriaged, General, Other), the history of eight fields (product, component, summary, platform, op_sys, severity,
type, keywords).

```sh
uv run --no-project python fetch_bugzilla.py --out bz-raw --plan       # counts bugs per product and requests, downloads nothing
uv run --no-project python fetch_bugzilla.py --out bz-raw              # the full download (long, resumable)
uv run --no-project python fetch_bugzilla.py --out bz-raw --products "Firefox Build System,WebExtensions"   # a slice
```

The script never writes a reporter's name or address to disk: the reporter becomes the first 12 hex digits of a SHA-256
hash, used only for the rule that sets aside intake queues (see below).

**Cost, measured 2026-11-01.** Counts: 95,375 bugs in the eight products. Stage 1 (bug list) is 100 requests of 1,000
bugs: run in full (`--bugs-only`), 391 s, 32 MB. Stage 2 (histories) needs 90,740 bugs (those not in a triage
component), 100 per request, 908 requests. It was not run in full. A slice of two products (3,188 bugs, 1,740
histories, 4 + 18 requests) took 56 s in total, about 2.3 s per request; 908 requests at that rate come to about 35
minutes, so the full download is about 40 minutes plus whatever your connection adds. The slice option exists for a quick look at the format before committing to the full download.

**Does a new download match the data behind the numbers below? Only partly, and the snapshot cannot be fully
pinned.** The bug lists behind the numbers were fetched on 2026-10-04 (UTC), the histories on 2026-10-23. Comparing
the bug list fetched on 2026-11-01 with the earlier one:

* 95,120 bugs then, 95,375 now; 95,105 in both, 15 only then, 270 only now. The 270 are mostly bugs that moved into
  one of the eight products; the 15 left them.
* Of the 69,540 bugs of the world below, 8 are not in the new list (they changed product or became invisible).
* Of the bugs in both lists, some fields differ because people edit bugs: component 389, product 223, severity 924,
  keywords 245, summary 133, type 71, platform 17, op_sys 16, reporter hash 354 (accounts renamed).
* 65 of the 69,540 world bugs have a component that differs from the one on 2026-10-04, so the `moved` flag of those
  could differ.

`build_world.py bugzilla --as-of 2026-10-04T20:05:00Z` rewinds component and product of every bug to that time, using
the history, so bugs that were not yet in one of the eight products, or sat in a triage component, at that time are
left out, and `moved` counts only moves up to then. It cannot bring back the bugs that have since left the eight
products (8 of 69,540 in the comparison above), and it only works for bugs that have a history file entry. `--as-of` was tested
on the data behind the numbers (the stream is byte-for-byte the same with and without it) and on the
two-product slice
(1,455 bugs in both: one differs, in a keywords value; moves and components agree). The full chain
(full download, build, replay) was not run on new data, so how much the Bugzilla result moves with a new download is not known. An
exact reproduction of the Bugzilla numbers needs the 2026 snapshot, which this kit does not ship; a new download is an
approximation (8 of 69,540 bugs missing, a few dozen components and fields changed).

## Build the streams

```sh
uv run --no-project python build_world.py nvd nvd-raw --out nvd-world --cutoff 2026-10-25T06:30:00
uv run --no-project python build_world.py bugzilla bz-raw --out bz-world --as-of 2026-10-04T20:05:00Z
uv run --no-project python build_world.py bugzilla bz-raw --out bz-world-with-intake --as-of 2026-10-04T20:05:00Z --keep-intake
```

The NVD build takes about 5 s. Of the 150,000 "Initial Analysis" entries it leaves out 755 later entries of a CVE
already seen, 21,216 first entries without a CWE, 13,862 with a placeholder only and 1,215 with several CWEs; that
yields 112,952 records, and 3,043 correction entries on 3,000 CVEs (`build.json` has the counts). The Bugzilla build yields 69,540 records (4,838 moved) and sets
aside the intake queue described under Caveats; with `--keep-intake` it yields 77,660 records (12,958 moved).

## Run the harness

```sh
uv run --no-project --isolated --with iyulab-gil==0.10.0 python abstention.py nvd nvd-world --out nvd.result.json
uv run --no-project --isolated --with iyulab-gil==0.10.0 python abstention.py bugzilla bz-world --out bz.result.json
uv run --no-project --isolated --with iyulab-gil==0.10.0 python abstention.py bugzilla bz-world-with-intake --out bz-with-intake.result.json
```

`--records N` replays only the first N records (a quick check; the counted part starts at 5,000). Progress goes to
stderr. Time on a 30-core Apple-silicon Mac with 96 GB: 1 min 5 s (NVD), 1 min 10 s (Bugzilla), 1 min 14 s
(Bugzilla with the intake queue); memory was not measured.

## Results to compare against

Produced from the data of the snapshots described above (NVD pages fetched 2026-10-25; Bugzilla bug lists fetched
2026-10-04 and histories 2026-10-23) with the commands above and `iyulab-gil` 0.10.0. The NVD run is repeatable to the
last printed digit from a fresh download (checked 2026-11-06: 3.514117 [2.008565, 6.148179]); the Bugzilla runs are
not (see the note above on how the data moves; a download on 2026-11-06 printed 1.67 against 0.23). For Bugzilla
"reproduced" therefore means: the ratio is above 1 with the intake queue set aside and below 1 with it kept, and the
printed pair is within 0.05 of 1.70 and 0.22.

| | NVD, first CWE | Bugzilla, first component (intake queue set aside) | Bugzilla (intake queue kept) |
| --- | ---: | ---: | ---: |
| Records in the stream | 112,952 | 69,540 | 77,660 |
| Counted (from position 5,000; NVD without remap CVEs) | 86,203 | 64,540 | 72,660 |
| Answered by the keeper | 1,309 (1.5%) | 15,213 (23.6%) | 23,554 (32.4%) |
| Corrected, abstained | 2,584 of 84,894 (3.0%) | 3,884 of 49,327 (7.9%) | 3,897 of 49,106 (7.9%) |
| Corrected, answered | 12 of 1,309 (0.9%) | 664 of 15,213 (4.4%) | 8,282 of 23,554 (35.2%) |
| Crude ratio [95% interval] | 3.32 [1.89, 5.84] | 1.80 [1.66, 1.95] | 0.23 [0.22, 0.23] |
| Strata (with both kinds / all) | 27 / 115 | 34 / 86 | 33 / not printed |
| Stratified ratio [95% interval] | **3.51 [2.01, 6.15]** | **1.70 [1.57, 1.85]** | **0.22 [0.21, 0.23]** |

Unrounded: NVD 3.514117 [2.008565, 6.148179]; Bugzilla 1.704069 [1.570443, 1.849065]; with the intake queue
0.220839 [0.212363, 0.229653]. The crude over stratified ratio is 0.94 (NVD) and 1.06 (Bugzilla). The strata that hold both
kinds, which the ratio uses, contain 2,246 corrected CVEs and 1,309 answered CVEs (NVD), and 4,379 moved bugs and
15,212 answered bugs (Bugzilla). Over the 107,952 NVD records from position 5,000 on, the keeper answered 2,453
(2.3%); 1,309 of those are in the counted records, the others are CVEs of the remap days.

## Caveats

* **The Bugzilla ratio reverses if the intake queue is kept.** In the first Bugzilla run the ratio was 0.22: answered
  bugs were moved *more* often. The cause is one component, `web-platform-tests`, to which a single account filed all
  of its 8,120 bugs; every one of them was moved later, and keeping them raises the number of answered bugs from 15,213
  to 23,554 (their texts are alike, so the keeper answers them). The rule that sets such components aside (at least 100 bugs, at least 90% from one
  reporter) was written **after** this first result was seen; it catches only that component, and it does not use
  whether bugs were moved. Judge the 1.70 with that in mind. Other components with systematic moves remain (for
  example `Security`, 998 bugs of which 82% were moved) and were not controlled.
* **The interval for NVD is wide and rests on 12 corrections.** Only 1,309 of 86,203 counted CVEs were answered (1.5%),
  and 12 of those were corrected later. A year or a stratum can move the point estimate; a fair summary is "the lower end
  of the interval is about 2", not "three times".
* **Only a difference between two groups, not a review order.** The result says abstained decisions are corrected more
  often than answered ones. It says nothing about ranking abstained decisions against each other, and nothing about
  whether a person looking at the abstained ones first would find more errors than another order would: the abstained
  group is 98% of the NVD records and 76% of the Bugzilla records.
* **Derived, not human-confirmed.** The first value is whatever the source lists at first; "corrected" means the source
  later lists another value. Nobody judged which one was right, whether a Bugzilla move was a correction or a tidy-up
  (renamed components count), or whether an NVD change was a more specific code (a parent replaced by a child) or a
  contradicting one. The NVD rule also leaves out the first analyses with no or several CWEs, which is a selection.
* **Stratification is partial.** Only counts of the same code and of the same vendor/product, and the year/quarter, are
  held fixed. Records that are hard to analyse for other reasons (large configurations, missing scores) are not
  controlled, so abstention may stand for "hard record" rather than anything about the keeper's own uncertainty. The
  crude ratios (3.32, 1.80) are close to the stratified ones, so the counts did not account for the difference, but
  that does not rule out other confounders.
* **Time of observation.** Corrections appear over time, so records near the end of a stream have had less time to be
  corrected; the year/quarter strata compare like with like. A later download changes the share corrected.
* **Data drift** is described in the fetch sections: NVD can be pinned with `--cutoff`; Bugzilla cannot be pinned
  exactly.
* **What is not covered.** Two worlds only, one setting (target 0.9, 5,000 records before the first choice, choices at
  +10%). Other thresholds, other worlds and the alternative of ranking by the keeper's scores are not tested here. The
  "CWE Remap" days and the Bugzilla reporter-concentration rule are readings of the data. Worlds built with
  `--keep-intake` or without `--cutoff` are variants, not the numbers above. A secondary comparison by kind of change (more specific
  versus sideways in NVD; within versus across products in Bugzilla) was done elsewhere and is not part of this kit.
* **Schema.** The keeper's attributes are the context names seen in the first 1,000 records. The early NVD records
  carry CVSS v2 components only, so v3.1 components are not declared attributes for that world.
* **Python.** `iyulab-gil` 0.10.0 requires Python 3.11 or newer; `numpy` is not needed.
