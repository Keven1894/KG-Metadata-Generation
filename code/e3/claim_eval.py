"""FActScore-style claim-level faithfulness for the DESCRIPTIVE field group.

Copied from the literature, not invented (docs/rag-design/05 §6c):
  Min et al., "FActScore: Fine-grained Atomic Evaluation of Factual Precision in Long Form Text
  Generation" (EMNLP 2023): decompose generated text into ATOMIC CLAIMS, verify each claim
  against a knowledge source, score = fraction supported.

Why: the 3-level whole-field verdict (supported/partially/unsupported) has no resolution on the
descriptive group — every tier's subject/description/coverage is "partially", so C5 cannot be
read. Claim decomposition turns one blunt verdict into N precise ones. Our knowledge source is
the issue's own OCR (cleaner than FActScore's Wikipedia setting).

Two calls per card, mirroring FActScore's pipeline:
  1. DECOMPOSE (card only, no OCR — decomposition must not be contaminated by the source);
  2. VERIFY    (numbered claims + OCR -> supported / unsupported / contradicted each).

Outputs per tier: claim precision (supported/total, the FActScore) and supported-claim count
(FActScore's "factual recall" analogue — how much true description the tier delivers).

Usage: python eval/claim_eval.py [--tiers ...] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as expcfg
from common.parallel import run_parallel
from core.llm import chat
from eval.judge import _parse_json, _strip_authority_uris
from eval.multi_llm import JUDGES, chat_multi

DESCRIPTIVE_FIELDS = ("subject", "description", "coverage", "contributor", "relation")

DECOMPOSE_SYSTEM = (
    "You decompose library metadata into atomic factual claims. An atomic claim is one minimal, "
    "self-contained assertion about the described newspaper issue (e.g. 'The issue covers a "
    "greyhound racing track opening', 'J. J. Murphy appears in the issue as a Hialeah city "
    "council member'). Rule<path>"
    "- Cataloging FORM is not a claim: LCSH heading syntax ('Politics, Practical'), inverted name "
    "order, place qualifiers ('(Fla.)'), and punctuation carry no factual content — extract the "
    "underlying assertion only.\n"
    "- Each claim must be independently checkable against the issue's text.\n"
    "- Do not add claims the metadata does not make; do not merge distinct assertions."
)

VERIFY_SYSTEM = (
    "You are a fact checker. For each numbered claim about a 'Miami Life' newspaper issue, check "
    "it against the issue's OCR text and give a verdic<path>"
    "- supported: the OCR clearly supports it (OCR is noisy; tolerate garbled spellings of the "
    "same name/word)\n"
    "- unsupported: the OCR does not contain evidence for it\n"
    "- contradicted: the OCR contains evidence against it\n"
    "Judge the claim's substance, not its wording."
)


def decompose(fields: dict) -> dict[str, list[str]]:
    """field -> list of atomic claims (only descriptive fields, only non-empty)."""
    desc = {k: v for k in DESCRIPTIVE_FIELDS if (v := fields.get(k))}
    if not desc:
        return {}
    user = (
        "Decompose each metadata field below into atomic claims.\n"
        'Return ONLY JSON: {"<field>": ["claim", ...], ...} (empty list if a field makes no '
        "factual claims).\n\n"
        f"=== METADATA (descriptive fields of one newspaper-issue record) ===\n"
        f"{json.dumps(desc, ensure_ascii=False, indent=1)}"
    )
    raw = chat([{"role": "system", "content": DECOMPOSE_SYSTEM}, {"role": "user", "content": user}],
               model=expcfg.CHAT_MODEL, max_tokens=4000, temperature=0)
    out = _parse_json(raw)
    return {k: [c for c in v if isinstance(c, str) and c.strip()]
            for k, v in out.items() if isinstance(v, list)} if isinstance(out, dict) else {}


def verify(doc_id: str, claims_by_field: dict[str, list[str]], judge: str = "self") -> list[dict]:
    """Flat list of {field, claim, verdict}.

    `judge` selects who VERIFIES the claims (docs/rag-design/08). Decomposition (above) is always
    done by the generator model (gpt-5.2) regardless of `judge`, so every judge scores the exact
    same claim set — disagreement then reflects judgment, not different claim extraction.
    """
    from common.corpus import read_text
    flat = [(f, c) for f, cs in claims_by_field.items() for c in cs]
    if not flat:
        return []
    src = read_text(doc_id)
    if len(src) > 60_000:
        src = src[:50_000] + "\n\n[... middle omitted ...]\n\n" + src[-10_000:]
    numbered = "\n".join(f"{i+1}. {c}" for i, (_, c) in enumerate(flat))
    user = (
        f"Check each claim against the OCR.\n"
        f'Return ONLY JSON: {{"verdicts": ["supported"|"unsupported"|"contradicted", ...]}} '
        f"with exactly {len(flat)} entries, in order.\n\n"
        f"=== CLAIMS ===\n{numbered}\n\n=== OCR SOURCE ===\n{src}\n=== END ==="
    )
    raw = chat_multi([{"role": "system", "content": VERIFY_SYSTEM}, {"role": "user", "content": user}],
                      judge=judge, max_tokens=(8000 if judge == "gpt55" else 2000))
    # gpt-5.5 is a reasoning model: with max_tokens=2000 on long OCR+claim lists it often
    # returns an empty string (reasoning consumes the budget). Retry once with a larger budget
    # if that happens, and again if the JSON parse yields no usable verdicts.
    if not (raw or "").strip():
        raw = chat_multi(
            [{"role": "system", "content": VERIFY_SYSTEM}, {"role": "user", "content": user}],
            judge=judge, max_tokens=16000,
        )
    out = _parse_json(raw)
    verdicts = out.get("verdicts", []) if isinstance(out, dict) else []
    if not isinstance(verdicts, list) or len(verdicts) < len(flat):
        raw = chat_multi(
            [{"role": "system", "content": VERIFY_SYSTEM}, {"role": "user", "content": user}],
            judge=judge, max_tokens=16000,
        )
        out = _parse_json(raw)
    verdicts = out.get("verdicts", []) if isinstance(out, dict) else []
    rows = []
    for i, (f, c) in enumerate(flat):
        v = str(verdicts[i]).lower().strip() if i < len(verdicts) else "unparsed"
        if v not in ("supported", "unsupported", "contradicted"):
            v = "unparsed"
        rows.append({"field": f, "claim": c, "verdict": v})
    return rows


def _claims_by_field_from_rows(rows: list[dict]) -> dict[str, list[str]]:
    """Rebuild field -> [claim, ...] from a prior eval's flat claim rows (order preserved)."""
    out: dict[str, list[str]] = {}
    for r in rows:
        f, c = r.get("field"), r.get("claim")
        if isinstance(f, str) and isinstance(c, str) and c.strip():
            out.setdefault(f, []).append(c)
    return out


def eval_card(
    doc_id: str,
    fields: dict,
    judge: str = "self",
    prior_claim_rows: list[dict] | None = None,
) -> dict:
    """Score one card. If `prior_claim_rows` is given, skip decompose and re-verify those claims
    only — required for cross-judge agreement (docs/rag-design/08): every judge must score the
    identical claim set produced by the generator-side decompose step.
    """
    fields = _strip_authority_uris(fields)  # PIDs are linker-verified, not textual claims
    if prior_claim_rows is not None:
        claims = _claims_by_field_from_rows(prior_claim_rows)
    else:
        claims = decompose(fields)
    rows = verify(doc_id, claims, judge=judge)
    n = len(rows)
    ns = sum(1 for r in rows if r["verdict"] == "supported")
    nc = sum(1 for r in rows if r["verdict"] == "contradicted")
    per_field = {}
    for f in DESCRIPTIVE_FIELDS:
        fr = [r for r in rows if r["field"] == f]
        if fr:
            per_field[f] = {"n": len(fr),
                            "supported": sum(1 for r in fr if r["verdict"] == "supported")}
    return {
        "doc_id": doc_id,
        "judge": judge,
        "n_claims": n,
        "n_supported": ns,
        "n_contradicted": nc,
        "claim_precision": round(ns / n, 4) if n else None,  # the FActScore
        "per_field": per_field,
        "claims": rows,
        "reused_claims": prior_claim_rows is not None,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", nargs="+", default=list(expcfg.TIERS), choices=list(expcfg.TIERS))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip-existing", action="store_true")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--judge", default="self", choices=sorted(JUDGES),
                    help="who VERIFIES claims (docs/rag-design/08); decomposition always uses "
                         "the generator model so judges score identical claim sets")
    ap.add_argument("--cards-dir", type=Path, default=None,
                    help="override expcfg.CARDS_DIR, e.g. to re-judge an archived v3 snapshot")
    ap.add_argument("--version-tag", default="",
                    help="required when --cards-dir is set, to avoid clobbering the default "
                         "(v4/live) output dir with a different card snapshot's results, "
                         "e.g. --version-tag v3")
    ap.add_argument("--reuse-claims-from", type=Path, default=None,
                    help="directory of prior claim JSON (tier__doc_id.json) whose claim TEXTS "
                         "are re-verified by --judge (skip decompose). Default when "
                         "--judge != self: scores/claims (v4) or the matching archived "
                         "claims dir when --version-tag v3.")
    args = ap.parse_args()
    if args.cards_dir and not args.version_tag:
        ap.error("--cards-dir requires --version-tag (e.g. v3) so outputs don't collide with "
                  "the default live-cards run")

    cards_dir = args.cards_dir or expcfg.CARDS_DIR
    suffix = ("" if args.judge == "self" else f"_{args.judge}") + \
             (f"_{args.version_tag}" if args.version_tag else "")
    out_dir = expcfg.SCORES_DIR / f"claims{suffix}"
    out_dir.mkdir(parents=True, exist_ok=True)

    reuse_dir = args.reuse_claims_from
    if reuse_dir is None and args.judge != "self":
        if args.version_tag == "v3":
            reuse_dir = expcfg.DATA_DIR / "_run3_full_2026-07-04" / "scores" / "claims"
        else:
            reuse_dir = expcfg.SCORES_DIR / "claims"
    if args.judge != "self":
        if reuse_dir is None or not reuse_dir.is_dir():
            ap.error(f"--judge {args.judge} needs a prior claim set to re-verify "
                     f"(reuse dir missing: {reuse_dir})")
        print(f"Reusing claim texts from {reuse_dir} (verify-only with judge={args.judge})")

    per_tier: dict[str, list[dict]] = {t: [] for t in args.tiers}
    for tier in args.tiers:
        paths = sorted((cards_dir / tier).glob("*.json"))
        if args.limit:
            paths = paths[: args.limit]
        cards = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
        if args.skip_existing:
            before = len(cards)
            cards = [c for c in cards if not (out_dir / f"{tier}__{c['doc_id']}.json").exists()]
            print(f"[{tier}] {before - len(cards)} already on disk, evaluating {len(cards)}")

        def _one(card: dict, tier: str = tier) -> dict:
            prior = None
            if reuse_dir is not None:
                prior_path = reuse_dir / f"{tier}__{card['doc_id']}.json"
                if not prior_path.exists():
                    raise FileNotFoundError(f"missing prior claims: {prior_path}")
                prior = json.loads(prior_path.read_text(encoding="utf-8")).get("claims") or []
            res = eval_card(card["doc_id"], card["fields"], judge=args.judge,
                            prior_claim_rows=prior)
            res["tier"] = tier
            return res

        def _on_result(card, res, exc, tier=tier):
            did = card["doc_id"]
            if exc is not None:
                print(f"[{tier:11s}] {did}  ERROR: {exc}")
                return
            per_tier[tier].append(res)
            (out_dir / f"{tier}__{did}.json").write_text(
                json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
            cp = res["claim_precision"]
            print(f"[{tier:11s}] {did}  claims={res['n_claims']:3d} "
                  f"supported={res['n_supported']:3d} precision="
                  f"{cp if cp is not None else '--'}")

        if args.workers > 1:
            run_parallel(cards, _one, workers=args.workers, on_result=_on_result)
        else:
            for card in cards:
                t0 = time.time()
                try:
                    res = _one(card)
                    _on_result(card, res, None)
                except Exception as e:  # noqa: BLE001
                    _on_result(card, None, e)

    # rebuild per_tier from ALL files on disk (not just this run's) so --skip-existing summaries
    # reflect the full accumulated set, not only newly-evaluated cards.
    per_tier = {t: [] for t in args.tiers}
    for tier in args.tiers:
        for p in sorted(out_dir.glob(f"{tier}__*.json")):
            per_tier[tier].append(json.loads(p.read_text(encoding="utf-8")))

    print("\n=== Descriptive claim-level faithfulness (FActScore-style) ===")
    print(f"{'tier':11s} {'precision':>10s} {'claims/card':>12s} {'supported/card':>15s} "
          f"{'contradicted':>13s} {'n':>3s}")
    summary = {}
    for tier in args.tiers:
        rs = [r for r in per_tier[tier] if r["n_claims"]]
        if not rs:
            continue
        n = len(rs)
        prec = sum(r["claim_precision"] for r in rs) / n
        summary[tier] = {
            "claim_precision": round(prec, 4),
            "claims_per_card": round(sum(r["n_claims"] for r in rs) / n, 2),
            "supported_per_card": round(sum(r["n_supported"] for r in rs) / n, 2),
            "contradicted_total": sum(r["n_contradicted"] for r in rs),
            "n": n,
        }
        s = summary[tier]
        print(f"{tier:11s} {s['claim_precision']:10.3f} {s['claims_per_card']:12.1f} "
              f"{s['supported_per_card']:15.1f} {s['contradicted_total']:13d} {n:3d}")

    summary_path = expcfg.SCORES_DIR / f"pilot_claim_faithfulness{suffix}.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nSaved -> {summary_path}")


if __name__ == "__main__":
    main()
