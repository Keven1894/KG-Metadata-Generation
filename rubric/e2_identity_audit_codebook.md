# E2 identity-audit codebook (one page, frozen 2026-08-31)

**Source:** [`../rag-design/13-evaluation-methods-literature-review.md`](../rag-design/13-evaluation-methods-literature-review.md) §3.2.  
**Do not redesign.** This page is the freeze. Full metrics stay in `13`.  
**Sample (2026-09-03):** full population of the E1 structured-check trace, reviewed in the portal
at `/identity` in seeded random order — see the last section. The 20-item pilot sheet
`experiments/artifacts/e2_pilot20_blind.csv` is calibration only.

## Unit

One item = one unique `(entity_type, lowercased surface, candidate_uri)` decision from
`experiments/data/freeze/e1_structured_check_trace.jsonl`, shown with the OCR context of every
issue in which it occurred. (Superseded: one row per `(run_id, doc_id, surface, candidate_uri)`
from `gate_decisions.jsonl`, which lacked `doc_id` for two ledger pairs and had no trace evidence.)

## What the annotator sees

- surface form;
- candidate URI (open the LoC / GeoNames record);
- issue `doc_id` and OCR / PDF for that issue;
- card field text when present.

## What is hidden until after the label

- gate outcome, score, rejection reason, authority source;
- the other annotator’s label.

Shown in addition (evidence, not a machine decision): the authority label cached with the
candidate, and the collection context line derived from the corpus itself (role phrases, issues
seen, observed years).

## Labels (exactly these)

| Code | Meaning |
|------|---------|
| `correct` | This URI is the right identity for this mention in this issue. |
| `wrong` | Resolvable or not, it is the wrong identity. |
| `true_nil` | No suitable record in the consulted vocabularies; refusing (or not linking) is right. |
| `indeterminate` | Evidence in OCR + authority record is not enough to decide. |

## Severity (only if `wrong`)

| Code | Meaning |
|------|---------|
| `critical` | Wrong person/org that a catalog user would treat as fact (homonym, anachronism). |
| `major` | Wrong but related (broader place, parent body, near-name). |
| `minor` | Harmless variant / equivalent ID / punctuation-level mismatch. |

## Refusal items

For ledger rows, answer: *if a human had to accept or refuse this candidate, would the
machine’s hidden decision be right?* After unblinding, map:

- `correct` on an accepted row → true positive identity;
- `wrong` on an accepted row → false accept;
- `true_nil` on a refused row → correct refusal;
- `correct` on a refused row → false refusal (the URI was actually right).

Do not infer severity from the machine score.

## Sampling: superseded 2026-09-03 by full-population review in the portal

The stratified ~200/37/80–120 draw is **not used**. Decision (<author>, 2026-09-03): put the whole
population into the review portal, label as much as time allows, report whatever is completed,
and let the portal measure the real workload.

- **Population:** every unique `(entity_type, lowercased surface, candidate_uri)` in the E1
  structured-check trace with outcome accept / score_reject / temporal_reject:
  **917 items** = 561 accepted + 319 score-refused + 37 temporal-refused
  (collapsed from 2,948 + 827 + 55 trace rows). `no_hit` rows have no URI and are excluded.
  Manifest: `experiments/artifacts/e2_identity_population_manifest.json`.
- **Order and allocation (implemented 2026-09-04):** one seeded shuffle (seed 20260903) over
  the whole population. Admin reviews from sort order 1. Four identity-only accounts have
  fixed, non-overlapping blocks: R2 101–200, R3 201–300, R4 301–400, and R5
  401–500. Each account receives the lowest-numbered unlabelled item inside its block and
  cannot browse or save outside that block.
- **Stopping rule:** each student works for a predeclared two-hour wall-clock session, in order,
  without selective skipping. Report the completed contiguous prefix within each assigned
  block, reviewer-specific `n`, and the union of completed items. Because completion count can
  depend on item effort, the time-limited union is not claimed to be an exact simple random
  sample; Wilson intervals are descriptive/approximate and must be accompanied by block-level
  sensitivity results. If fixed blocks are completed in full, their seeded positions are a
  probability sample without design weights.
- **Unit change:** one label per unique pair, shown with OCR snippets from every issue where it
  occurred (up to six inline, the rest linked). If identity really differs across issues, tick
  *identity differs across the listed issues*, label the common case, explain in the note.
- **Where:** portal page `/identity` (nav: *Identity audit*). Student accounts are identity-only.
  Hotkeys 1–4 label, Q/W/E severity, and Enter save-and-next; student skipping is disabled.
  Focused seconds per item are recorded (`active_ms`), but the two-hour workload measure uses
  wall-clock session time because external authority-record tabs are not counted as focused time.
- **Blinding:** the page never renders gate outcome, score, threshold, check states, reason, or
  authority source. Unblinded strata and the machine-outcome × human-label table are only at
  `/R1/identity`; do not open it while still labelling.
- **Multi-reviewer:** labels are per (item, reviewer). The current fixed blocks measure
  independent review throughput and broaden coverage; they do not themselves create a
  double-coded agreement subset. Any later agreement study must declare shared items in advance.
- **Export:** `python review_app/export_identity_labels.py` →
  `experiments/data/freeze/e2_identity_labels.jsonl` (gitignored) +
  `experiments/artifacts/e2_identity_progress.json` (counts only).

The 20-item pilot (`e2_pilot20_blind.csv`, drawn from the old `gate_decisions.jsonl`) remains
useful only for codebook calibration; it is not the E2 sample.
