"""Pairwise tier comparison on descriptive-record quality (the C5 separation instrument).

Copied from the literature, not invented (docs/rag-design/05 §6c): LLM judges are more stable
and better human-aligned in PAIRWISE comparison than in absolute (Likert) scoring (Zheng et al.
MT-Bench 2023; Liu et al. "Aligning with Human Judgement" 2024). Position bias is real, so every
pair is judged twice with order swapped (position-consistency protocol); disagreement = tie.

Scope note from the same literature: pairwise wins on holistic/subjective quality, but NOT on
objective factual consistency — faithfulness is therefore measured separately at claim level
(eval/claim_eval.py). This judge answers only: which record better serves a catalog user?

Usage: python eval/pairwise_judge.py [--limit N]
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as expcfg
from common.corpus import read_text
from common.parallel import run_parallel
from core.llm import chat
from eval.judge import _parse_json, _strip_authority_uris
from eval.multi_llm import JUDGES, chat_multi

DESCRIPTIVE_FIELDS = ("subject", "description", "coverage", "contributor", "relation")

SYSTEM = (
    "You are an experienced newspaper cataloger reviewing two candidate descriptive records for "
    "the SAME 'Miami Life' issue, against its OCR text. Decide which record better serves a "
    "library catalog user, weighin<path>"
    "1. Faithfulness — no unsupported names/facts/framings (decisive when clearly unequal);\n"
    "2. Informativeness — captures the issue's actual principal content, specific not generic;\n"
    "3. Description quality — subjects that discriminate this issue, a description a researcher "
    "could rely on, coverage/contributors that reflect the issue.\n"
    "Cataloging form (LCSH heading syntax, inverted names, qualifiers) is a convention, not a "
    "quality difference. Verdict 'tie' is allowed when genuinely comparable."
)


def _desc_view(fields: dict) -> dict:
    fields = _strip_authority_uris(fields)
    return {k: fields.get(k) for k in DESCRIPTIVE_FIELDS}


def judge_pair(doc_id: str, fields_a: dict, fields_b: dict, judge: str = "self") -> str:
    """One directed comparison -> 'A' | 'B' | 'tie'. `judge` selects the model (docs/rag-design/08)."""
    src = read_text(doc_id)
    if len(src) > 60_000:
        src = src[:50_000] + "\n\n[... middle omitted ...]\n\n" + src[-10_000:]
    user = (
        'Return ONLY JSON: {"winner": "A"|"B"|"tie", "reason": "..."}.\n\n'
        f"=== RECORD A ===\n{json.dumps(_desc_view(fields_a), ensure_ascii=False, indent=1)}\n\n"
        f"=== RECORD B ===\n{json.dumps(_desc_view(fields_b), ensure_ascii=False, indent=1)}\n\n"
        f"=== OCR SOURCE ===\n{src}\n=== END ==="
    )
    raw = chat_multi([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                      judge=judge, max_tokens=(4000 if judge == "gpt55" else 1000))
    # gpt-5.5 reasoning can return empty at low max_tokens on long OCR; retry once.
    if not (raw or "").strip():
        raw = chat_multi(
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
            judge=judge, max_tokens=8000,
        )
    out = _parse_json(raw)
    w = str(out.get("winner", "")).strip().upper() if isinstance(out, dict) else ""
    return w if w in ("A", "B") else "tie"


def compare(doc_id: str, tier_x: str, tier_y: str, fx: dict, fy: dict, judge: str = "self") -> dict:
    """Position-consistency protocol: judge (x=A,y=B) and (y=A,x=B); agree or it's a tie."""
    v1 = judge_pair(doc_id, fx, fy, judge=judge)            # x is A
    v2 = judge_pair(doc_id, fy, fx, judge=judge)            # x is B
    first = tier_x if v1 == "A" else tier_y if v1 == "B" else "tie"
    second = tier_x if v2 == "B" else tier_y if v2 == "A" else "tie"
    winner = first if first == second else "tie"
    return {"doc_id": doc_id, "pair": [tier_x, tier_y], "winner": winner,
            "directed": [first, second], "judge": judge}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", nargs="+", default=list(expcfg.TIERS), choices=list(expcfg.TIERS))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip-existing", action="store_true",
                     help="skip (doc_id, pair) combos already present in the results cache")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--judge", default="self", choices=sorted(JUDGES),
                    help="who compares the pair (docs/rag-design/08)")
    ap.add_argument("--cards-dir", type=Path, default=None,
                    help="override expcfg.CARDS_DIR, e.g. to re-judge an archived v3 snapshot")
    ap.add_argument("--version-tag", default="",
                    help="required when --cards-dir is set, to avoid clobbering the default "
                         "(v4/live) output file with a different card snapshot's results, "
                         "e.g. --version-tag v3")
    args = ap.parse_args()
    if args.cards_dir and not args.version_tag:
        ap.error("--cards-dir requires --version-tag (e.g. v3) so outputs don't collide with "
                  "the default live-cards run")

    cards_dir = args.cards_dir or expcfg.CARDS_DIR
    cards: dict[str, dict[str, dict]] = {}  # tier -> doc_id -> fields
    for tier in args.tiers:
        for p in sorted((cards_dir / tier).glob("*.json")):
            c = json.loads(p.read_text(encoding="utf-8"))
            cards.setdefault(tier, {})[c["doc_id"]] = c["fields"]

    doc_ids = sorted(set.intersection(*(set(v) for v in cards.values())))
    if args.limit:
        doc_ids = doc_ids[: args.limit]
    pairs = list(itertools.combinations(args.tiers, 2))

    suffix = ("" if args.judge == "self" else f"_{args.judge}") + \
             (f"_{args.version_tag}" if args.version_tag else "")
    out_path = expcfg.SCORES_DIR / f"pilot_pairwise_descriptive{suffix}.json"
    prior: list[dict] = []
    done: set[tuple] = set()
    if args.skip_existing and out_path.exists():
        prior = json.loads(out_path.read_text(encoding="utf-8")).get("results", [])
        done = {(r["doc_id"], tuple(r["pair"])) for r in prior}
        print(f"{len(done)} (doc_id, pair) combos already cached")

    jobs = [(did, tx, ty) for did in doc_ids for tx, ty in pairs
            if (did, (tx, ty)) not in done]

    import threading
    _flush_lock = threading.Lock()
    _since_flush = [0]

    def _flush():
        # interim save so --skip-existing can resume after a crash (summary recomputed at end)
        tmp = {"summary": {}, "results": list(results), "partial": True}
        out_path.write_text(json.dumps(tmp, ensure_ascii=False, indent=2), encoding="utf-8")

    def _one(job: tuple) -> dict:
        did, tx, ty = job
        return compare(did, tx, ty, cards[tx][did], cards[ty][did], judge=args.judge)

    def _on_result(job, r, exc):
        did, tx, ty = job
        if exc is not None:
            print(f"{did}  {tx} vs {ty}: ERROR: {exc}")
            return
        with _flush_lock:
            results.append(r)
            _since_flush[0] += 1
            if _since_flush[0] >= 10:
                _flush()
                _since_flush[0] = 0
        print(f"{did}  {tx} vs {ty}: {r['winner']:12s} "
              f"(directed: {r['directed'][0]}/{r['directed'][1]})")

    results: list[dict] = list(prior)
    if args.workers > 1:
        run_parallel(jobs, _one, workers=args.workers, on_result=_on_result)
    else:
        for job in jobs:
            t0 = time.time()
            try:
                r = _one(job)
                _on_result(job, r, None)
            except Exception as e:  # noqa: BLE001
                _on_result(job, None, e)

    print("\n=== Pairwise descriptive-quality win rates (order-swapped, ties on disagreement) ===")
    summary = {}
    for tx, ty in pairs:
        rs = [r for r in results if r["pair"] == [tx, ty]]
        wx = sum(1 for r in rs if r["winner"] == tx)
        wy = sum(1 for r in rs if r["winner"] == ty)
        ties = len(rs) - wx - wy
        summary[f"{tx}_vs_{ty}"] = {tx: wx, ty: wy, "tie": ties, "n": len(rs)}
        print(f"  {tx} vs {ty}:  {tx} wins {wx}, {ty} wins {wy}, ties {ties}  (n={len(rs)})")

    out = {"summary": summary, "results": results, "partial": False, "judge": args.judge}
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSaved -> {out_path}")


if __name__ == "__main__":
    main()
