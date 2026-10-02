"""E3: reanalyze existing v4 pairwise files. No LLM calls.

Usage (from experiments/):
    python eval/e3_reanalyze_pairwise.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as expcfg
from common.corpus import read_text

ARTIFACT_DIR = Path(__file__).resolve().parents[1] / "artifacts"
OCR_TRUNC = 60_000

JUDGE_FILES = {
    "self": expcfg.SCORES_DIR / "pilot_pairwise_descriptive.json",
    "opus": expcfg.SCORES_DIR / "pilot_pairwise_descriptive_opus.json",
    "gpt55": expcfg.SCORES_DIR / "pilot_pairwise_descriptive_gpt55.json",
}


def pair_key(pair) -> str:
    a, b = pair
    return f"{a}_vs_{b}"


def analyze_judge(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    results = data.get("results") or []
    by_pair: dict[str, list] = defaultdict(list)
    for r in results:
        by_pair[pair_key(r["pair"])].append(r)

    out = {"n_results": len(results), "by_pair": {}, "summary_file": data.get("summary")}
    for pk, rows in sorted(by_pair.items()):
        wins = Counter()
        swap_agree = 0
        n = len(rows)
        for r in rows:
            w = r.get("winner") or "tie"
            wins[w] += 1
            d = r.get("directed") or []
            if len(d) == 2 and d[0] == d[1]:
                swap_agree += 1
        out["by_pair"][pk] = {
            "n": n,
            "unconditional_counts": dict(wins),
            "swap_consistent": swap_agree,
            "swap_consistent_pct": round(100 * swap_agree / n, 2) if n else None,
            "n_ties": wins.get("tie", 0),
        }
    return out


def ocr_truncation() -> dict:
    n = over = missing = 0
    over_ids = []
    for p in sorted((expcfg.CARDS_DIR / "C-standard").glob("*.json")):
        n += 1
        doc_id = p.stem
        try:
            text = read_text(doc_id)
        except Exception:
            missing += 1
            continue
        if len(text) > OCR_TRUNC:
            over += 1
            over_ids.append(doc_id)
    return {
        "n_issues": n,
        "ocr_chars_gt_60000": over,
        "ocr_unreadable": missing,
        "truncation_rule": "pairwise_judge.py / claim eval keep head+tail when len>60000",
        "over_ids_sample": over_ids[:15],
    }


def main() -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    judges = {name: analyze_judge(path) for name, path in JUDGE_FILES.items()}

    delta_csv = Path(__file__).resolve().parents[2] / "latex" / "tables" / "claim_delta_ci.csv"
    gpt55_note = (
        "GPT-5.5 G0→G1: Wilcoxon p≈0.0099 but bootstrap CI on the mean delta includes 0 "
        "(claim_delta_ci.csv). These are different estimands; do not treat as a conflict."
    )
    payload = {
        "instrument": "pairwise_v4_reanalysis",
        "no_new_api_calls": True,
        "judges": judges,
        "ocr_truncation": ocr_truncation(),
        "gpt55_mean_vs_wilcoxon": gpt55_note,
        "latex_binomial_table": "latex/tables/tab_pairwise_binomial.tex already matches unconditional W/T/L",
    }
    dest = ARTIFACT_DIR / "e3_pairwise_reanalysis.json"
    dest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps({
        "wrote": str(dest),
        "ocr_truncation": payload["ocr_truncation"],
        "self_simple_vs_standard": judges["self"]["by_pair"].get("C-simple_vs_C-standard"),
        "opus_simple_vs_standard": judges["opus"]["by_pair"].get("C-simple_vs_C-standard"),
        "gpt55_simple_vs_standard": judges["gpt55"]["by_pair"].get("C-simple_vs_C-standard"),
    }, indent=2))


if __name__ == "__main__":
    main()
