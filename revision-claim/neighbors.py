#!/usr/bin/env python3
"""Two score-threshold tools that freeze their policy, run across a code-list revision.

    python neighbors.py <stream.jsonl.gz> [--world chicago|osha] [--slot S --boundary B] [--after N]
                        [--seeds 0,1,2,3,4]
                        [--no-refit] [--out result.json]

The stream is the output of build_world.py (one JSON record per line, in settlement order, with a `form_version` side
column). Needs: numpy, scipy, scikit-learn, `mapie==1.6.0`, `calibroute-ai==0.4.0` - see the README for the exact
command. It does not use `iyulab-gil`; the keeper arms come from carryover.py.

One text classifier supplies the confidence for every arm: a multinomial logistic regression over the word 1-2-grams
of the free text (`comments` for Chicago, `narrative` for OSHA) plus one-hot categorical columns (Chicago:
`facility_type`, `risk`, `inspection_type`; OSHA: `state`, `naics`, `hospitalized`, `amputation`, `loss_of_eye`). Records before the revision are split by
inspection (the `batch` column) at random into 50 % training, 25 % calibration and 25 % hold-out; the calibration
sample plays the "independent IID validation sample" the tools ask for. The target is a precision of 0.90 among the
answered records. Arms:

  F-CR   CalibRoute `fit_policy(max_risk=0.10, risk_method="clopper_pearson", confidence_level=0.95)`, frozen.
         A record is answered when its confidence reaches the policy's accept threshold. No shift check.
  F-CRg  The same policy through `route_batch`, in consecutive batches of 250 records, with CalibRoute's own batch
         shift check (the policy keeps `max_js_divergence=None`, so `route_batch` calibrates a threshold from the
         policy's reference histogram). A record is answered when `route_batch` accepts it.
  F-MP   MAPIE `BinaryClassificationController` with "the prediction is right" as the positive class, risk
         "precision" >= 0.90 at confidence 0.95, on the same calibration sample.
  R-CR   (unless --no-refit) retrain the classifier and refit the CalibRoute policy on the records settled after the
         revision, at 250, 500, 1,000, 2,000 and 4,000 records and then whenever the settled records have grown by
         10 % (first 20,000 records after the revision only): first 70 % of the settled records train, last 30 %
         calibrate. It answers nothing until 30 records have settled.

For each bucket of records after the revision and each arm the output gives lookups, answered, correct, the share
answered, the precision among the answered with a Wilson 95 % interval, and (F-CRg) how many batches the shift check
flagged. Pre-revision hold-out precision of each arm is printed as a check that the tools were used as intended.
"""

from __future__ import annotations

import argparse
import gzip
import itertools
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

TARGET = 0.90
BATCH = 250
MIN_ANSWERED = 30
BUCKETS = ((0, 1_000), (1_000, 5_000), (5_000, 20_000), (20_000, None))
EARLY_POINTS = (250, 500, 1_000, 2_000, 4_000)
GROWTH = 1.1
REFIT_LIMIT = 20_000
WORLDS = {
    "chicago": {
        "slot": "violation",
        "boundary": "2018",
        "text": "comments",
        "cats": ("facility_type", "risk", "inspection_type"),
        "after": 120_000,
    },
    "osha": {
        "slot": "nature",
        "boundary": "v3",
        "text": "narrative",
        "cats": ("state", "naics", "hospitalized", "amputation", "loss_of_eye"),
        "after": None,
    },
}
TEXT = WORLDS["chicago"]["text"]
CATS = WORLDS["chicago"]["cats"]


def load(
    stream: Path, slot: str, boundary: str, after: int | None
) -> tuple[list[dict], list[dict]]:
    rows = []
    with gzip.open(stream, "rt", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if slot in r.get("targets", {}):
                rows.append(r)
    at = next(i for i, r in enumerate(rows) if r["form_version"] == boundary)
    late = sum(r["form_version"] != boundary for r in rows[at:])
    if late:
        raise SystemExit(f"{late} records of an earlier edition after the boundary")
    pre, post = rows[:at], rows[at:]
    return pre, post[:after] if after else post


def wilson(correct: int, n: int, z: float = 1.96) -> tuple[float, float]:
    p = correct / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / (1 + z * z / n), (centre + margin) / (1 + z * z / n)


class Scorer:
    """Multinomial logistic regression over comment n-grams and three categorical columns."""

    def __init__(self, rows: list[dict], slot: str):
        from scipy.sparse import hstack
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import OneHotEncoder

        self.slot = slot
        self.tf = TfidfVectorizer(ngram_range=(1, 2), min_df=2, max_features=100_000)
        self.oh = OneHotEncoder(handle_unknown="ignore")
        x = hstack(
            [
                self.tf.fit_transform(self._texts(rows)),
                self.oh.fit_transform(self._cats(rows)),
            ]
        ).tocsr()
        y = [str(r["targets"][slot]) for r in rows]
        if len(set(y)) < 2:
            raise ValueError("fewer than two classes")
        self.clf = LogisticRegression(C=10, max_iter=300)
        self.clf.fit(x, y)

    @staticmethod
    def _texts(rows):
        return [dict(r["context"]).get(TEXT, "") for r in rows]

    @staticmethod
    def _cats(rows):
        return [[dict(r["context"]).get(k, "") for k in CATS] for r in rows]

    def confidence(self, rows: list[dict]) -> tuple[np.ndarray, np.ndarray]:
        """(highest class probability, whether that class is the recorded value) per record."""
        from scipy.sparse import hstack

        x = hstack(
            [self.tf.transform(self._texts(rows)), self.oh.transform(self._cats(rows))]
        ).tocsr()
        proba = self.clf.predict_proba(x)
        top = proba.argmax(axis=1)
        conf = proba[np.arange(len(rows)), top]
        pred = self.clf.classes_[top]
        ok = np.array([str(r["targets"][self.slot]) == p for r, p in zip(rows, pred)])
        return conf, ok


def records(conf, ok=None):
    from calibroute import PredictionRecord

    return [
        PredictionRecord(
            record_id=str(i),
            confidence=float(c),
            correct=None if ok is None else bool(ok[i]),
        )
        for i, c in enumerate(conf)
    ]


def calibroute_policy(conf, ok):
    from calibroute import fit_policy

    try:
        return fit_policy(
            records(conf, ok),
            max_risk=1 - TARGET,
            risk_method="clopper_pearson",
            confidence_level=0.95,
        ), None
    except Exception as e:  # noqa: BLE001 - the tool refuses; keep the reason
        return None, f"{type(e).__name__}: {e}"[:200]


def mapie_answers(cal_conf, cal_ok, conf):
    from mapie.risk_control import BinaryClassificationController

    def two_columns(x):
        x = np.asarray(x, dtype=float).ravel()
        return np.column_stack([1.0 - x, x])

    try:
        ctrl = BinaryClassificationController(
            predict_function=two_columns,
            risk="precision",
            target_level=TARGET,
            confidence_level=0.95,
        )
        ctrl.calibrate(np.asarray(cal_conf, dtype=float), np.asarray(cal_ok, dtype=int))
        valid = getattr(ctrl, "valid_predict_params", None)
        if valid is None or len(valid) == 0:
            return np.zeros(len(conf), dtype=bool), "no valid threshold"
        return np.asarray(ctrl.predict(np.asarray(conf, dtype=float))) == 1, None
    except Exception as e:  # noqa: BLE001 - the tool refuses; keep the reason
        return np.zeros(len(conf), dtype=bool), f"{type(e).__name__}: {e}"[:200]


def routed_answers(policy, conf):
    """Consecutive batches of BATCH records through route_batch. -> (answered, flagged flag per batch)"""
    from calibroute import Action, route_batch

    answered = np.zeros(len(conf), dtype=bool)
    flagged = []
    for s in range(0, len(conf), BATCH):
        decisions, summary = route_batch(records(conf[s : s + BATCH]), policy)
        for j, d in enumerate(decisions):
            answered[s + j] = d.action == Action.ACCEPT
        flagged.append(bool(summary.get("severe_shift")))
    return answered, flagged


def refit_points(limit: int) -> list[int]:
    points = [0] + [p for p in EARLY_POINTS if p < limit]
    while points[-1] < limit:
        points.append(min(limit, max(points[-1] + 1, math.ceil(points[-1] * GROWTH))))
    return points


def refit_answers(post: list[dict], slot: str):
    limit = min(REFIT_LIMIT, len(post))
    answered = np.zeros(limit, dtype=bool)
    right = np.zeros(limit, dtype=bool)
    for p, q in itertools.pairwise(refit_points(limit)):
        if p < MIN_ANSWERED:
            continue
        t = int(p * 0.7)
        try:
            scorer = Scorer(post[:t], slot)
            cal_conf, cal_ok = scorer.confidence(post[t:p])
        except ValueError:
            continue
        policy, _ = calibroute_policy(cal_conf, cal_ok)
        if policy is None:
            continue
        conf, ok = scorer.confidence(post[p:q])
        answered[p:q] = conf >= policy.accept_threshold
        right[p:q] = ok
    return answered, right


def tally(answered, right, flagged=None) -> list[dict]:
    out = []
    for lo, hi in BUCKETS:
        top = len(answered) if hi is None else min(hi, len(answered))
        if lo >= top:
            out.append(
                {"from": lo, "until": hi, "lookups": 0, "answered": 0, "correct": 0}
            )
            continue
        a = answered[lo:top]
        row = {
            "from": lo,
            "until": hi,
            "lookups": int(top - lo),
            "answered": int(a.sum()),
            "correct": int((a & right[lo:top]).sum()),
        }
        if flagged is not None:
            idx = [k for k in range(len(flagged)) if lo <= k * BATCH < top]
            row["batches"] = len(idx)
            row["flagged_batches"] = int(sum(flagged[k] for k in idx))
        out.append(row)
    return out


def hold_out_check(answered, right) -> dict:
    n = int(answered.sum())
    c = int((answered & right).sum())
    return {
        "answered": n,
        "correct": c,
        "precision": (c / n) if n else None,
        "lookups": len(answered),
    }


def run_seed(
    seed: int, pre: list[dict], post: list[dict], slot: str, refit: bool
) -> dict:
    ids = sorted({str(r.get("batch") or r["id"]) for r in pre})
    order = np.random.default_rng(seed).permutation(len(ids))
    a, b = int(len(ids) * 0.5), int(len(ids) * 0.75)
    role = {
        ids[j]: ("train" if k < a else "cal" if k < b else "hold")
        for k, j in enumerate(order)
    }
    part: dict[str, list[dict]] = {"train": [], "cal": [], "hold": []}
    for r in pre:
        part[role[str(r.get("batch") or r["id"])]].append(r)

    scorer = Scorer(part["train"], slot)
    cal_conf, cal_ok = scorer.confidence(part["cal"])
    hold_conf, hold_ok = scorer.confidence(part["hold"])
    conf, ok = scorer.confidence(post)
    out: dict = {
        "seed": seed,
        "sizes": {k: len(v) for k, v in part.items()},
        "classifier_accuracy": {
            "calibration": float(cal_ok.mean()),
            "hold_out": float(hold_ok.mean()),
            "after_revision": float(ok.mean()),
        },
        "notes": {},
        "hold_out": {},
        "arms": {},
    }

    policy, note = calibroute_policy(cal_conf, cal_ok)
    out["notes"]["calibroute"] = note
    if policy is not None:
        out["notes"]["accept_threshold"] = policy.accept_threshold
        out["hold_out"]["F-CR"] = hold_out_check(
            hold_conf >= policy.accept_threshold, hold_ok
        )
        out["arms"]["F-CR"] = tally(conf >= policy.accept_threshold, ok)
        _, hold_flags = routed_answers(policy, hold_conf)
        out["notes"]["shift_check_flags_on_hold_out"] = {
            "batches": len(hold_flags),
            "flagged": int(sum(hold_flags)),
        }
        routed, flags = routed_answers(policy, conf)
        out["arms"]["F-CRg"] = tally(routed, ok, flags)

    hold_mp, note = mapie_answers(cal_conf, cal_ok, hold_conf)
    out["notes"]["mapie"] = note
    out["hold_out"]["F-MP"] = hold_out_check(hold_mp, hold_ok)
    post_mp, _ = mapie_answers(cal_conf, cal_ok, conf)
    out["arms"]["F-MP"] = tally(post_mp, ok)

    if refit:
        answered, right = refit_answers(post, slot)
        out["arms"]["R-CR"] = tally(answered, right)
    return out


def table(result: dict) -> str:
    lines = [
        (
            f"stream {result['stream']}  before {result['before']}  after {result['after']}  "
            f"mapie {result['mapie']}  calibroute-ai {result['calibroute_ai']}  scikit-learn {result['scikit_learn']}"
        )
    ]
    for seed, s in result["seeds"].items():
        acc = s["classifier_accuracy"]
        lines.append(
            f"seed {seed}: classifier accuracy calibration {acc['calibration']:.3f}, hold-out {acc['hold_out']:.3f}, "
            f"after the revision {acc['after_revision']:.4f}; hold-out precision of the answered: "
            + ", ".join(
                f"{k} {v['precision']:.3f}"
                for k, v in s["hold_out"].items()
                if v["precision"] is not None
            )
            + f"; shift check flagged {s['notes'].get('shift_check_flags_on_hold_out')} hold-out batches"
        )
    lines.append(
        f"{'arm':<7}{'records after':<16}{'seeds':>6}{'answered':>10}{'share':>8}{'precision':>10}  Wilson 95%   flagged batches"
    )
    arms = {a for s in result["seeds"].values() for a in s["arms"]}
    for arm in ("F-CR", "F-CRg", "F-MP", "R-CR"):
        if arm not in arms:
            continue
        for i, (lo, hi) in enumerate(BUCKETS):
            cells = [
                s["arms"][arm][i]
                for s in result["seeds"].values()
                if arm in s["arms"] and s["arms"][arm][i]["lookups"]
            ]
            if not cells:
                continue
            n = sum(c["lookups"] for c in cells)
            answered = sum(c["answered"] for c in cells)
            correct = sum(c["correct"] for c in cells)
            span = f"{lo:,}-" + ("end" if hi is None else f"{hi:,}")
            if answered:
                lo95, hi95 = wilson(correct, answered)
                prec = f"{correct / answered:>10.3f}  [{lo95:.3f}, {hi95:.3f}]"
            else:
                prec = f"{'-':>10}"
            flagged = ""
            if "flagged_batches" in cells[0]:
                flagged = f"  {sum(c['flagged_batches'] for c in cells)}/{sum(c['batches'] for c in cells)}"
            lines.append(
                f"{arm:<7}{span:<16}{len(cells):>6}{answered:>10}{answered / n:>8.3f}{prec}{flagged}"
            )
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
    require_pins(**{"numpy": "2.5.4", "scipy": "1.18.1", "scikit-learn": "1.9.1", "mapie": "1.6.0", "calibroute-ai": "0.4.0"})
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("stream", type=Path)
    ap.add_argument(
        "--world",
        choices=sorted(WORLDS),
        default="chicago",
        help="which stream's columns to use (default chicago)",
    )
    ap.add_argument(
        "--slot",
        help="the decided attribute (default: the world's - violation, nature)",
    )
    ap.add_argument(
        "--boundary",
        help="the form_version after the revision (default: the world's - 2018, v3)",
    )
    ap.add_argument(
        "--after",
        type=int,
        help="keep only the first N records after the revision (default: the world's - 120,000 for Chicago, all for OSHA)",
    )
    ap.add_argument(
        "--seeds",
        default="0,1,2,3,4",
        help="seeds of the pre-revision split (default 0,1,2,3,4)",
    )
    ap.add_argument("--no-refit", action="store_true", help="skip the R-CR arm")
    ap.add_argument("--out", type=Path, help="write the result as JSON")
    a = ap.parse_args()

    from importlib.metadata import version

    global TEXT, CATS
    world = WORLDS[a.world]
    TEXT, CATS = world["text"], world["cats"]
    a.slot = a.slot or world["slot"]
    a.boundary = a.boundary or world["boundary"]
    a.after = a.after if a.after is not None else world["after"]
    pre, post = load(a.stream, a.slot, a.boundary, a.after)
    result = {
        "stream": a.stream.name,
        "before": len(pre),
        "after": len(post),
        "mapie": version("mapie"),
        "calibroute_ai": version("calibroute-ai"),
        "scikit_learn": version("scikit-learn"),
        "seeds": {},
    }
    for seed in (int(s) for s in a.seeds.split(",")):
        t0 = time.perf_counter()
        result["seeds"][str(seed)] = run_seed(
            seed, pre, post, a.slot, refit=not a.no_refit
        )
        print(
            f"seed {seed}: {time.perf_counter() - t0:.0f} s",
            file=sys.stderr,
            flush=True,
        )
    if a.out:
        a.out.write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    print(table(result))


if __name__ == "__main__":
    main()
