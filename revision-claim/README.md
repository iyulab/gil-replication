# Carrying memory over a code-list revision: a reproduction kit

**Claim.** When a form's code list is revised so that numbers are rewritten, a keeper that carries its memory over the
revision (arm C) promises far more than it realizes in the first 1,000 decisions after the revision, while a keeper that
starts its memory at the revision (arm N) keeps its promise.

The kit rebuilds two replay streams from public data and replays each of them through the published
[`iyulab-gil`](https://pypi.org/project/iyulab-gil/) 0.10.0 package, using its public API only:

| File | What it does |
| --- | --- |
| `build_world.py` | Turns the raw public CSV files into replay streams (standard library only). |
| `carryover.py` | Runs arms N and C over a stream cut at the revision and prints the table (needs `iyulab-gil` only). |
| `neighbors.py` | Runs two score-threshold tools that freeze their policy (CalibRoute, MAPIE) over the Chicago stream cut at the revision (needs `scikit-learn` and the two tools, versions pinned below; does not use `iyulab-gil`). |

The two worlds:

* **Chicago food inspections.** Every violation in an inspection is one record; the decided attribute is `violation`,
  the violation number the inspector wrote. The city's food code revision took effect on 2018-07-01 and rewrote the
  violation numbers (before it the numbers run 1-70, from it 1-64; 45 numbers occur in both editions).
* **OSHA severe injury reports.** Every report is one record; the decided attribute is `nature` (nature of injury, one
  of four OIICS codes on each report). Events from 2024-01 on carry codes of the OIICS 3.0 edition. The four most
  common pre-2024 codes (`111`, `1311`, `1972`, `132`) make up 0.73-0.77 of the reports in 2023-10 to 2023-12 and
  0.05 of those in 2024-01 to 2024-03.

## How the replay works

`carryover.py` cuts the stream where the side column `form_version` first takes the post-revision value. The records
after the cut are identical for both arms. The arms differ only in what the keeper remembers when the revision
arrives: N is given nothing, C is given every record before the revision with its code unchanged.

Each arm is one `gil.Keeper(schema, 0.9, 30)` (target precision 0.9, at least 30 answered). It chooses its thresholds
at the revision (arm N has nothing settled yet and answers nothing), after 250, 500, 1,000, 2,000 and 4,000 records
following the revision, and from there every time the settled records have grown by 10%. The thresholds chosen at one
point answer the records up to the next choice. What each layer found for a record is read from the decision records
that `keeper.rechoose(True)` returns, which hold only the records settled before it.

Per bucket of records after the revision and per arm the table gives the answered count, the precision (share of
answers that were right), the promise (mean of what the answering layer promised) and delta = promise - precision with
a Wilson 95% interval. A keeper that keeps its promise has a delta near zero or below.

## Setup

You need [`uv`](https://docs.astral.sh/uv/) (any Python 3.11+ works for `build_world.py`). Nothing else is installed
by hand; `uv` fetches the pinned package for each command:

```sh
uv run --no-project --isolated --with iyulab-gil==0.10.0 python carryover.py --help
```

## Get the data

### Chicago food inspections

City of Chicago Data Portal, dataset `4ijn-s7e5` (Food Inspections,
<https://data.cityofchicago.org/Health-Human-Services/Food-Inspections/4ijn-s7e5>). The live dataset covers 2010 to the
present. One request fetches every inspection from 2017-01-01 on (about 200 MB):

```sh
curl -G 'https://data.cityofchicago.org/resource/4ijn-s7e5.csv' \
  --data-urlencode "\$where=inspection_date >= '2017-01-01T00:00:00'" \
  --data-urlencode '$order=inspection_date,inspection_id' \
  --data-urlencode '$limit=1000000' \
  -o inspections-2017-.csv
```

Run on 2026-11-01 without an app token, exactly this request returned HTTP 200 and 175,995 inspections.

### OSHA severe injury reports

The full Severe Injury Reports data set, <https://www.osha.gov/severe-injury-reports> (also reachable as
<https://www.osha.gov/severeinjury>). The page links one zip file with all reports. The numbers below use
`January2015toNovember2025.zip` (events 2015-01-01 to 2025-11-30; SHA-256
`a3f7f434e200fb956131f12277378e592993a25db3f328716fbece106f846bb0`; 16,224,511 bytes).

Download the file by hand in a web browser; the site's CDN refuses scripted requests such as a plain `curl` with HTTP 403,
and this kit does not work around that:

1. Open <https://www.osha.gov/severe-injury-reports> in a browser.
2. Follow the link to the zip file with all reports, `January2015toNovember2025.zip`
   (the page may list a different file name today; see the note below).
3. Check the file you saved:

```sh
shasum -a 256 January2015toNovember2025.zip   # a3f7f434e200fb956131f12277378e592993a25db3f328716fbece106f846bb0
```

On 2026-11-01 this file had that SHA-256. OSHA updates the data set periodically and names the file after its
date range, so a later visit will list a different file name and a later end date; how long the old file name stays
online was not verified. If the page no longer offers this file, download the one it links and use the numbers below
only as an approximate comparison.

## Build the streams

```sh
uv run --no-project python build_world.py chicago inspections-2017-.csv --out chicago.stream.jsonl.gz
uv run --no-project python build_world.py osha January2015toNovember2025.zip --out osha.stream.jsonl.gz
```

`osha` accepts the zip or the CSV inside it. The Chicago build takes about two minutes (it blanks violation titles out of
the inspector comments with one large pattern). On the data the numbers below were produced from, the OSHA build yields
105,991 records (105,996 rows, 5 repeated report ids dropped; 88,245 before the revision, 17,746 after) and the Chicago
build 596,527 records (90,842 before the revision, 505,685 after).

## Run the harness

```sh
# Chicago: all 90,842 pre-revision records, first 120,000 after
uv run --no-project --isolated --with iyulab-gil==0.10.0 python carryover.py chicago.stream.jsonl.gz \
  --slot violation --boundary 2018 --after 120000 --arms N,C --out chicago.result.json

# OSHA: all 88,245 pre-revision records, all 17,746 after
uv run --no-project --isolated --with iyulab-gil==0.10.0 python carryover.py osha.stream.jsonl.gz \
  --slot nature --boundary v3 --arms N,C --out osha.result.json
```

Arguments: `--slot` the decided attribute, `--boundary` the `form_version` value after the revision (`2018` for Chicago,
`v3` for OSHA), `--before N` / `--after N` keep only the last N records before / the first N after the revision,
`--arms` any of `N`, `C`. A quick check: `--after 3000` on Chicago takes a few minutes and reproduces the first
bucket of each arm below (N: 356 answered, precision 0.975; C: 655 answered, precision 0.441). On a download made later
than the data behind the tables the answered counts move by a record or two (357 and 656 on 2026-11-06) and the precisions
stay; see "Portal data keeps changing" under Caveats. The portal moves, so for Chicago "reproduced" means: the same
pattern (arm C promises about 0.94 and is right on about 0.44; arm N keeps its promise), every printed count within
0.5 % of the table below and every printed rate within 0.002. The OSHA run is fixed by the file's SHA-256 and prints the
table to the last digit.

Time: the first choice of arm C replays every pre-revision record and dominates. In the runs behind the tables below
the choices took about 10.5 minutes in total for the Chicago run and about 3.7 minutes for the OSHA run, on a 30-core
workstation; expect longer on a laptop. An end-to-end run of the Chicago command above, with `iyulab-gil` 0.10.0
from PyPI and other jobs running on the same 30-core machine, took about 20 minutes.
Progress goes to stderr.

## Results to compare against

Produced from the data described above with the commands above (`iyulab-gil` 0.10.0). "Records after" counts records
after the revision; `lookups` is the number of records in the bucket; `answered` of them were answered.

### Chicago food inspections (`violation`, boundary 2018-07-01; 90,842 records before, first 120,000 after)

| Arm | Records after | Lookups | Answered | Precision | Promise | Delta | Delta 95% |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| N | 0-1,000 | 1,000 | 356 | 0.975 | 0.969 | -0.006 | [-0.018, +0.016] |
| N | 1,000-5,000 | 4,000 | 2,570 | 0.982 | 0.975 | -0.007 | [-0.011, -0.001] |
| N | 5,000-20,000 | 15,000 | 12,016 | 0.985 | 0.983 | -0.002 | [-0.004, +0.001] |
| N | 20,000-120,000 | 100,000 | 81,289 | 0.986 | 0.983 | -0.003 | [-0.003, -0.002] |
| C | 0-1,000 | 1,000 | 655 | 0.441 | 0.940 | +0.499 | [+0.461, +0.537] |
| C | 1,000-5,000 | 4,000 | 2,296 | 0.611 | 0.802 | +0.191 | [+0.172, +0.212] |
| C | 5,000-20,000 | 15,000 | 8,203 | 0.918 | 0.859 | -0.060 | [-0.065, -0.053] |
| C | 20,000-120,000 | 100,000 | 51,728 | 0.972 | 0.967 | -0.005 | [-0.006, -0.004] |

### OSHA severe injury reports (`nature`, boundary 2024-01; 88,245 records before, 17,746 after)

| Arm | Records after | Lookups | Answered | Precision | Promise | Delta | Delta 95% |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| N | 0-1,000 | 1,000 | 0 | - | - | - | - |
| N | 1,000-5,000 | 4,000 | 294 | 0.929 | 0.910 | -0.018 | [-0.043, +0.017] |
| N | 5,000-20,000 | 12,746 | 965 | 0.905 | 0.905 | +0.000 | [-0.017, +0.020] |
| C | 0-1,000 | 1,000 | 153 | 0.007 | 0.869 | +0.862 | [+0.833, +0.868] |
| C | 1,000-5,000 | 4,000 | 182 | 0.038 | 0.536 | +0.497 | [+0.459, +0.517] |
| C | 5,000-20,000 | 12,746 | 66 | 0.121 | 0.210 | +0.089 | [-0.011, +0.147] |

Reading the first buckets: in both worlds arm C promised 0.94 (Chicago) and 0.87 (OSHA) for the first 1,000 records after
the revision and was right on 0.44 and 0.007 of what it answered. Arm N's delta is at or below zero in
every Chicago bucket (the interval of the first bucket includes zero) and, in the OSHA run, from the second bucket on, where it
answered. In the OSHA run arm N answered nothing in the first 1,000 records after the revision, so there the comparison
is between a keeper that answered nothing and one that answered and was wrong.

Records after the revision whose code the arm's memory had never seen (`new_value` in the result file): Chicago, N 45 of
the first 1,000 and 63 of 120,000; C 14 and 18. OSHA, N 49 and 119 of 17,746; C 19 and 49.

## Two tools that freeze their policy, across the same revision

[CalibRoute](https://pypi.org/project/calibroute-ai/) (`calibroute-ai` 0.4.0) and the risk-control module of
[MAPIE](https://pypi.org/project/mapie/) (1.6.0) choose a confidence threshold for a precision target on a held-out
sample and then keep it. CalibRoute's documentation states that its check is meant for an independent, identically
distributed validation sample and does not cover distribution shift, and it ships a batch shift check of its own.
`neighbors.py` runs both on the Chicago stream and on the OSHA stream (`--world chicago`, the default, or
`--world osha`), with one off-the-shelf text classifier supplying the confidence (a multinomial logistic regression over the
word 1-2-grams of the free text plus one-hot categorical columns: `comments` with `facility_type`, `risk` and
`inspection_type` for Chicago; `narrative` with `state`, `naics`, `hospitalized`, `amputation` and `loss_of_eye` for OSHA). The classifier is trained on 50 % of the pre-revision inspections; 25 % calibrate the tools
and 25 % are held out, split at random by inspection (seeds 0-4). The target is a precision of 0.90 among the answered
records. Arms: `F-CR` (CalibRoute's `fit_policy`, frozen, no shift check), `F-CRg` (the same policy through
`route_batch` in batches of 250 records, with CalibRoute's own shift check), `F-MP` (MAPIE's
`BinaryClassificationController`, risk `precision`, "the prediction is right" as the positive class) and `R-CR`
(retrain the classifier and refit the CalibRoute policy on the records settled after the revision, at the same choice
points as the keeper, first 20,000 records only).

```sh
uv run --no-project --isolated --with numpy==2.5.4 --with scipy==1.18.1 --with scikit-learn==1.9.1 \
  --with mapie==1.6.0 --with calibroute-ai==0.4.0 \
  python neighbors.py chicago.stream.jsonl.gz --slot violation --boundary 2018 --after 120000 --out neighbors.result.json
```

Time: about 2.5 minutes per seed. Add `--seeds 0` for one seed, `--no-refit` to skip `R-CR`.

Results over the five seeds (`Share` = share of the records in the bucket that were answered; `Precision` = share of
the answered that were right):

| Arm | Records after | Share | Precision |
| --- | --- | ---: | ---: |
| F-CR | 0-1,000 | 0.994 | 0.004 |
| F-CR | 1,000-5,000 | 0.996 | 0.007 |
| F-CR | 5,000-20,000 | 0.996 | 0.005 |
| F-CR | 20,000-120,000 | 0.996 | 0.006 |
| F-CRg | all four buckets | 0 | - |
| F-MP | 0-1,000 | 1.000 | 0.004 |
| F-MP | 1,000-5,000 | 1.000 | 0.007 |
| F-MP | 5,000-20,000 | 1.000 | 0.005 |
| F-MP | 20,000-120,000 | 1.000 | 0.006 |
| R-CR | 0-1,000 | 0.353 | 0.941 |
| R-CR | 1,000-5,000 | 0.859 | 0.890 |
| R-CR | 5,000-20,000 | 0.995 | 0.923 |

For comparison, arm N above answered 0.356 / 0.642 / 0.801 / 0.813 of the four buckets at precision 0.975 / 0.982 /
0.985 / 0.986.

Checks that the tools were used as intended: on the pre-revision hold-out, `F-CR`, `F-CRg` and `F-MP` all reached a
precision of 0.949-0.952 among the answered (target 0.90) in every seed; CalibRoute's shift check flagged none of the
91-92 hold-out batches of 250 records, and flagged all 480 batches after the revision in every seed. The classifier
was right on 0.948-0.952 of the pre-revision calibration and hold-out records and on 0.55-0.59 % of the first
120,000 records after the revision. Because the classifier is so accurate before the revision, the thresholds the tools
pick (0.09-0.12) accept nearly every record, and after the revision the confidence stays high while the codes it
points to have been renumbered.

What this does not show: it is one world and one classifier, the tools are used as documented and not tuned, MAPIE is
used through a binary reduction (the precision among the answered, not per class), and CalibRoute's shift check reacts
to a change in the distribution of the confidences, which happened here; a change of meaning that leaves that
distribution alone would not trip it. Nothing here concerns data that stays exchangeable. The numbers depend on the
`scikit-learn` version used for the classifier (1.9.1 here).

### The same runs on the OSHA stream

```sh
uv run --no-project --isolated --with numpy==2.5.4 --with scipy==1.18.1 --with scikit-learn==1.9.1 \
  --with mapie==1.6.0 --with calibroute-ai==0.4.0 \
  python neighbors.py osha.stream.jsonl.gz --world osha --out neighbors-osha.result.json
```

Time: about 12 minutes per seed. Results over the five seeds (88,245 records before the revision, all 17,746 after it):

| Arm | Records after | Share | Precision |
| --- | --- | ---: | ---: |
| F-CR | 0-1,000 | 0.947 | 0.064 |
| F-CR | 1,000-5,000 | 0.932 | 0.085 |
| F-CR | 5,000-17,746 | 0.931 | 0.088 |
| F-CRg | 0-1,000 | 0.947 | 0.064 |
| F-CRg | 1,000-5,000 | 0.932 | 0.085 |
| F-CRg | 5,000-17,746 | 0.931 | 0.088 |
| F-MP | 0-1,000 | 0.938 | 0.064 |
| F-MP | 1,000-5,000 | 0.924 | 0.084 |
| F-MP | 5,000-17,746 | 0.923 | 0.088 |
| R-CR | 0-1,000 | 0 | - |
| R-CR | 1,000-5,000 | 0.088 | 0.917 |
| R-CR | 5,000-17,746 | 0.561 | 0.907 |

For comparison, arm N above answered 0 / 0.073 / 0.076 of the three buckets at precision - / 0.929 / 0.905.

Checks: on the pre-revision hold-out, `F-CR`, `F-CRg` and `F-MP` reached a precision of 0.901-0.913 among the answered in
every seed (target 0.90), at accept thresholds of 0.37-0.41. The classifier was right on 0.852-0.863 of the
pre-revision calibration records, 0.856-0.862 of the hold-out records and 8.4-8.6 % of the records after the revision.
CalibRoute's shift check flagged one of the 89 hold-out batches of 250 records in one seed and none in the others, and
flagged **none of the 71 batches after the revision in any seed** (shift score about 0.005 against a threshold of
0.02): the classifier stayed as confident as before although the codes it points to had been renumbered, so the check had
nothing to see. `R-CR` could not fit a policy in 4 of its 20 refits (too few settled records) and answered nothing in the
first 1,000 records.

Read together with the Chicago table: a policy frozen before the revision answered nearly everything and was right on
fewer than one in ten answers in both streams; CalibRoute's shift check stopped it where the confidences moved (Chicago)
and not where they did not (OSHA); the two ways of choosing again after the revision, the keeper started fresh and the
retrained and re-thresholded classifier, both stayed near the 0.90 target, with different coverage.

## Caveats

* **Portal data keeps changing.** The Chicago portal is updated continuously, so a download made later holds more
  inspections and may differ in details. A build from the 2026-11-01 download was compared with the build behind the
  numbers above (which ended at 2026-10-20): the 90,842 records before the boundary and the first 120,000 after it are
  the same set of records (same codes, comments and context), and the boundary position is the same. Some records
  are in a different order within their inspection (87 of the 90,842 before; 2,579 of the 120,000 after), because the
  order of the violations listed in an inspection differs between the two downloads. The record ids and the counts
  after the first 120,000 differ (506,899 records after the boundary instead of 505,685). Expect your table to agree with
  the one above closely but not necessarily to the last digit. The short check (`--after 3000`) was rerun on the newer download: the first bucket of arm C is identical (655
  answered, precision 0.441, promise 0.940) and arm N answered 357 of 1,000 instead of 356 (same precision and promise to
  three digits). The full run was repeated on a download made on 2026-11-06 (176,155 inspections, 507,289 records after
  the boundary): arm N, first 1,000 records, answered 357 (precision 0.975, promise 0.969), arm C answered 656 (precision
  0.441, promise 0.940); later buckets differ by a few records in the answered counts and by at most 0.002 in the
  rates. The reading is the same, the last digits are not. The same holds for `neighbors.py` on that download: the
  frozen policies answer 1,000 of the first 1,000 records (CalibRoute) instead of 996, with precision 0.004 in both
  runs. Run on the stream behind the table above, both scripts print the table above to the last digit; the stream
  that was used is not part of the kit, because the portal serves only its current data.
* **The worlds are derived, not human-confirmed.** A record counts as settled with the code the source lists, in event
  order. Nobody confirmed or corrected suggestions made by the keeper; the codes are the ones inspectors and OSHA
  coders assigned. The replay measures what the keeper would have promised and realized on that history.
* **Two worlds only.** The claim is about code lists whose numbers are rewritten, and these are the two revisions
  tested. The result does not say what happens for a revision that changes no code, or for other worlds.
* **The boundary is the one in the data.** For Chicago it is the date the revised food code took effect. For OSHA it is
  the first month in which the `nature` distribution changes (the monthly figures above); it is taken to be the start of the
  OIICS 3.0 edition, which was not checked against OSHA documentation.
* **The first bucket is small.** 1,000 lookups; the OSHA arm C answered 153 of them. The intervals above are Wilson
  intervals on the answered records, with the promise treated as fixed.
* **Time.** See above: minutes on a many-core workstation, longer on a laptop. Memory was not measured.
* **Python.** `iyulab-gil` 0.10.0 requires Python 3.11 or newer; `numpy` is not needed.

## Does the watch notice the revision?

**Claim.** A keeper that is never told about the revision still notices it from what people do with its answers. Made
with `watch_realized=True` (experimental), `keeper.stale()` says `{"realized": ...}` when, of the last 100 answers whose
rightness came back, fewer than the target less 0.15 (0.75 at a target of 0.9) were right. On the two worlds above this
happened 54 records (Chicago) and 163 records (OSHA) after the revision, with no alarm before it.

**Method.** `alarm.py` plays the part of an application. It uses only the public `gil` package and the standard
library, and it never tells the keeper about the edition (`form_version` is read only to place the revision). One
`gil.Keeper(schema, 0.9, 30, watch_realized=True)` and one `gil.Decider` go through the stream in settlement order:

* The first 5,000 records are settled as typed (`Enter`) and the keeper chooses its thresholds; it chooses again each
  time the settled records have grown by 10%.
* Each later record is first answered by the decider, with the thresholds the keeper holds and only the record's
  observed values. If an answer is trusted it counts as shown: the record is then settled with the code the source
  lists, as `Accept` when that is the answer shown and `Correct` when it is not, carrying the answer in `suggested`. A
  record with no trusted answer is settled as `Enter`.
* After each judged settlement the script reads `keeper.stale()`.

Counting alarms is a rule of the script, not of the package, whose reason stays set for as long as the last 100 judged
answers stay low. An alarm is counted at the first judged settlement where the `realized` reason is present, provided
at least 5,000 records have passed since the previous alarm and at least 100 answers were judged since then. Alarms
before the first record of the new edition are false alarms. The delay counts records from the first record of the
new edition (position 0) to the first alarm: a delay of 54 means the alarm came with the 55th record of the new
edition.

**Run it** on the streams built above (it needs `iyulab-gil` only):

```sh
# OSHA: all 105,991 records (revision at 88,245)
uv run --no-project --isolated --with iyulab-gil==0.10.0 python alarm.py osha.stream.jsonl.gz \
  --slot nature --boundary v3 --out osha.alarm.json

# Chicago: all 90,842 records before the revision, the first 120,000 after it
uv run --no-project --isolated --with iyulab-gil==0.10.0 python alarm.py chicago.stream.jsonl.gz \
  --slot violation --boundary 2018 --after 120000 --out chicago.alarm.json
```

`--trace file.jsonl` also writes one line per record after the first 5,000 (position, id, whether an answer was shown,
whether it was right). Progress goes to stderr. Time, with `iyulab-gil` 0.10.0 from PyPI on a 30-core workstation: 7
minutes 16 seconds for OSHA and 17 minutes 12 seconds for Chicago, the answers of the decider being the larger part.
Memory was not measured.

**What the kit produced** (the Chicago stream built from the 2026-10-20 download described under Caveats above, the OSHA
stream from the zip file named above):

| World | Records followed | Revision at | Answers shown (share right) | Alarms | False alarms | Delay | Alarms after the first |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Chicago food inspections | 210,842 | 90,842 | 127,638 (0.958) | 2 | 0 | 54 | +5,055 (57 of 100 right) |
| OSHA severe injury reports | 105,991 | 88,245 | 18,563 (0.878) | 3 | 0 | 163 | +5,169 (7 of 100), +10,822 (5 of 100) |

At the first alarm the window held 74 right of 100 in both worlds. The later alarms are the memory still answering with
old codes more than 5,000 records on: this script does nothing with an alarm, so the keeper carries on with the memory
it has.

These are the numbers of the claim (2 and 3 alarms, none before the revision, 54 and 163 records). What you can
repeat is the table above.

**What is not covered, and caveats**

* **The thirteen streams without a revision are not replayed here.** The kit has no builder for them. The only
  evidence of "no alarm without a revision" the kit produces is the part of the two streams before the revision (83,245
  and 85,842 records after the first 5,000, 0 alarms); that is two streams, not thirteen. The package README states that on thirteen public
  streams with no revision the watch never fired, and that on public device recalls with no revision it said so once,
  on a nine-day run of recalls from one firm that the memory answered wrongly. Both sentences were checked against the
  package README text only, not by replaying those streams.
* **The answers and their rightness are derived.** Nobody accepted or corrected anything: an answer counts as right
  when it equals the code the source lists, and `Accept` / `Correct` are set from that. In an application, people may
  accept wrong answers or correct right ones, and only some answers may be checked; the watch is as good as the
  settlements it is given.
* **Two worlds, one setting.** Target 0.9, a window of 100 and a margin of 0.15 (the package's constants), the first
  choice after 5,000 records, a revision that arrives after 88,000 or more records. The alarm needs 100 judged answers
  after the revision to fill its window; a stream that answers little alarms later in records than one that answers
  often (after the revision the keeper showed an answer for 964 of the 17,746 OSHA records, 0.054, and for 0.528 of the
  120,000 Chicago records).
* **How far the stream is followed matters for the counts, not for the delay.** The alarm counts depend on the
  5,000-record pause of the counting rule and on how many records after the revision are followed (120,000 for
  Chicago, all 17,746 for OSHA). The delay is the first alarm only.
* **The Chicago build.** The numbers come from a Chicago stream whose first 120,000 records after the revision are the
  ones described under "Portal data keeps changing" above. `alarm.py` was not rerun on a stream built from a later
  download, where some records are ordered differently within their inspection; the alarm position can move by a few
  records with such a reordering.
* **The watch is experimental in the package** and its reason is a signal to look at what the window holds, not a
  statement that a code list was revised.
