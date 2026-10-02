# E3 pairwise reanalysis (existing v4 scores; no new API calls)

**Date:** 2026-08-31. **Script:** `experiments/eval/e3_reanalyze_pairwise.py`.  
**JSON:** `experiments/artifacts/e3_pairwise_reanalysis.json`.

Unconditional W/T/L for C-simple vs C-standard matches `latex/tables/tab_pairwise_binomial.tex`.

## Swap consistency

`directed` is the two orderings. A **tie** is almost always a swap disagreement
(`directed[0] != directed[1]`). Swap-consistent count equals decided (non-tie) votes.

| Judge | Simple vs standard W/T/L | Swap-consistent |
|-------|--------------------------|-----------------|
| self | 25 / 160 / 18 | 43/203 (21%) |
| Opus | 119 / 69 / 15 | 134/203 (66%) |
| GPT-5.5 | 136 / 29 / 38 | 174/203 (86%) |

The self-judge’s 160 ties are not “both records equally good after two consistent looks”; they
are **order reversals**. Independent judges reverse less often. That is the S3 wording to keep:
self vs non-generator judges do not support the same descriptive conclusion.

G2 (standard vs precedent) remains a coin-flip on decided votes (Opus 56–53 with 94 ties;
GPT-5.5 65–65 with 73 ties).

## OCR truncation

`pairwise_judge.py` keeps head+tail when OCR length > 60,000. **182 / 203** issues exceed that.
Treat pairwise (and claim) numbers as judgments over a truncated OCR window, not the full issue
text. Do not start a new judge run this week.

## GPT-5.5 G0→G1 claim Δ

From `latex/tables/claim_delta_ci.csv`: mean bootstrap CI **includes 0**; Wilcoxon **p ≈ 0.0099**.
Different estimands (mean vs paired rank). Report both; do not call them a contradiction.
