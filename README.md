# Gil replication kits

Two kits that rebuild measurements of [Gil](https://github.com/iyulab/gil) from public data, using only the published
[`iyulab-gil`](https://pypi.org/project/iyulab-gil/) package (0.10.0) and its public API. Each kit downloads its own
data, builds replay streams with the standard library, and prints the tables its README compares against.

| Kit | Claim | Worlds |
| --- | --- | --- |
| [`revision-claim/`](revision-claim/) | When a code list is revised so that numbers are rewritten, a keeper that carries its memory over the revision promises far more than it realizes right after it; a keeper that starts its memory at the revision keeps its promise. The kit also runs two score-threshold tools that freeze their policy across the same revision, and shows when a keeper that is never told about the revision notices it. | Chicago food inspections (2018 food code) · OSHA severe injury reports (OIICS 3.0) |
| [`abstention-claim/`](abstention-claim/) | A decision the keeper abstains on is corrected later more often than one it answers. | NVD weakness types (CWE) · Mozilla Bugzilla components |

Each kit README states the claim, the method, the commands, the results to compare against, how a later download of
the same public data may differ, and what the result does not show. Read the caveats before quoting a number.

## Note and counterexamples

[`note/note.tex`](note/note.tex) is a short note that reports the revision claim and the watch that notices it in one
place, and asks for counterexamples: a stream where carrying memory over such a revision keeps the promise, or where
the watch stays silent after one. Open an issue in this repository with the stream and the command that rebuilds it.

## Closest alternatives

Score-threshold tools such as CalibRoute and the risk-control module of MAPIE choose a confidence threshold on a
held-out sample and then keep it fixed; CalibRoute documents that its check assumes an independent sample like the data
it will see, and it ships a batch shift check. Gil's keeper works the other way round: it replays the records people
have confirmed in the order they were settled, chooses its thresholds again as confirmations arrive, and reports the
precision it realized next to the precision it promised.

We ran both tools as documented on the two revision streams above (food-inspection violations; severe-injury reports),
with one off-the-shelf text classifier supplying the confidence (`calibroute-ai` 0.4.0, `mapie` 1.6.0,
`scikit-learn` 1.9.1; five seeds). On held-out records from before the revision both met their 0.90 target. After the
revision a frozen threshold without a shift check answered 92-100% of the records, and fewer than 10% of those answers
were right. CalibRoute's own shift check stopped it in the first stream (it flagged every batch and answered none of
the next 120,000 records) but never fired in the second, where the classifier stayed as confident as before. A keeper
started fresh at the revision stayed above the 0.90 target in every range of records after the revision in which it
answered, and a classifier retrained and re-thresholded on the records confirmed after the revision stayed close to it
(between 89% and 94%), with very different coverage: in the 1,000-5,000 records after the revision the keeper answered
64% of the records at 98% precision in the first stream and 7% at 93% in the second, while the retrained threshold
answered 86% at 89% and 9% at 92%, and, from record 5,000 on in the second stream, 56% at 91%.

Gil's realized-precision watch, which needs the outcomes of the answers it shows, raised its first alarm 54 records
(first stream) and 163 records (second stream) after the revision, with no alarm before it; CalibRoute's shift check
needs no outcomes, and it fired only in the first stream.

These runs show what freezing the policy across a revision costs. They do not show that a keeper does something a
retrained threshold cannot, and they say nothing about data that stays exchangeable. Two streams, one classifier. The
tables and the commands that print them are in [`revision-claim/`](revision-claim/README.md#two-tools-that-freeze-their-policy-across-the-same-revision).

## Requirements

[`uv`](https://docs.astral.sh/uv/) and Python 3.11 or newer. Every command pins the packages it runs with; nothing
is installed by hand. Downloads are plain HTTP requests to the public sources, paced as each kit README says.

## License

Apache License 2.0 — see [LICENSE](LICENSE). Copyright iyulab.

The data each kit downloads stays under the terms of its source (City of Chicago Data Portal, OSHA, NIST NVD, Mozilla
Bugzilla); this repository contains code and text only.
