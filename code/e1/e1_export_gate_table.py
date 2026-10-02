"""E1: freeze counts + reconstruct a common-unit gate decision table from v4 artifacts.

Does not re-call LoC/GeoNames. Does not invent ranked lists or mention_ids.

Usage (from experiments/):
    python eval/e1_export_gate_table.py
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as expcfg
from common import entity_registry

RUN_ID = "v4-c-standard-2026-07"
URI_RE = re.compile(r"https?://(?:id\.loc\.gov|www\.geonames\.org)[^\s()\"]+")
# "Surface (https://...)" as in generated cards
SURF_URI = re.compile(
    r"([^\"\[\]()]{2,120}?)\s*\(\s*(https?://(?:id\.loc\.gov|www\.geonames\.org)[^\s()\"]+)\s*\)"
)

FREEZE_DIR = expcfg.DATA_DIR / "freeze"
ARTIFACT_DIR = Path(__file__).resolve().parents[1] / "artifacts"


def _sha256(path: Path, max_bytes: int = 0) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        if max_bytes:
            h.update(f.read(max_bytes))
        else:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


def _file_info(path: Path) -> dict | None:
    if not path.exists():
        return None
    st = path.stat()
    return {
        "path": str(path.as_posix()),
        "bytes": st.st_size,
        "mtime_utc": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        "sha256": _sha256(path),
    }


def walk_uris(obj):
    if isinstance(obj, str):
        yield from URI_RE.findall(obj)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_uris(v)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from walk_uris(v)


def extract_accepted(tier: str = "C-standard") -> list[dict]:
    rows = []
    for p in sorted((expcfg.CARDS_DIR / tier).glob("*.json")):
        card = json.loads(p.read_text(encoding="utf-8"))
        doc_id = card.get("doc_id") or p.stem
        fields = card.get("fields") or {}
        for field, val in fields.items():
            items = val if isinstance(val, list) else [val]
            for i, item in enumerate(items):
                if not isinstance(item, str):
                    continue
                found = list(SURF_URI.finditer(item))
                if found:
                    for j, m in enumerate(found):
                        rows.append({
                            "run_id": RUN_ID,
                            "doc_id": doc_id,
                            "dc_field": field,
                            "slot": i if j == 0 else f"{i}.{j}",
                            "surface": m.group(1).strip(),
                            "entity_type": "",
                            "candidate_uri": m.group(2).rstrip(".,;"),
                            "gate_outcome": "accepted",
                            "reason": "",
                            "score": None,
                            "source": "v4_card_embed",
                        })
                    continue
                for j, uri in enumerate(URI_RE.findall(item)):
                    rows.append({
                        "run_id": RUN_ID,
                        "doc_id": doc_id,
                        "dc_field": field,
                        "slot": i if j == 0 else f"{i}.{j}",
                        "surface": "",
                        "entity_type": "",
                        "candidate_uri": uri.rstrip(".,;"),
                        "gate_outcome": "accepted",
                        "reason": "",
                        "score": None,
                        "source": "v4_card_embed",
                    })
    return rows


def extract_refusals(reg: dict) -> list[dict]:
    ledger = expcfg.DATA_DIR / "authority_rejections.jsonl"
    uniq: dict[tuple[str, str], dict] = {}
    for line in ledger.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        key = (r.get("surface", "").strip(), r.get("uri", "").strip())
        uniq[key] = r  # last write wins; unique pair as in the notebook
    rows = []
    unlocalized = 0
    for (surface, uri), r in uniq.items():
        reason = str(r.get("reason") or "")
        outcome = "temporal_rejected" if reason.lower().startswith("lifespan") else "score_rejected"
        key = entity_registry.norm_key(surface)
        rec = reg.get(key) or {}
        doc_ids = list(rec.get("doc_ids") or [])
        etype = r.get("etype") or rec.get("etype") or ""
        if not doc_ids:
            unlocalized += 1
            rows.append({
                "run_id": RUN_ID,
                "doc_id": "",
                "dc_field": "",
                "slot": None,
                "surface": surface,
                "entity_type": etype,
                "candidate_uri": uri,
                "gate_outcome": outcome,
                "reason": reason,
                "score": r.get("score"),
                "source": "ledger_unlocalized",
            })
            continue
        for doc_id in doc_ids:
            rows.append({
                "run_id": RUN_ID,
                "doc_id": doc_id,
                "dc_field": "",
                "slot": None,
                "surface": surface,
                "entity_type": etype,
                "candidate_uri": uri,
                "gate_outcome": outcome,
                "reason": reason,
                "score": r.get("score"),
                "source": "ledger_x_registry",
            })
    return rows, len(uniq), unlocalized


def cache_query_outcomes() -> dict:
    path = expcfg.DATA_DIR / "authority_cache.json"
    cache = json.loads(path.read_text(encoding="utf-8"))
    n = len(cache)
    no_hit = api_error = ok = other = 0
    for _url, payload in cache.items():
        if payload is None:
            api_error += 1
            continue
        if not isinstance(payload, dict):
            other += 1
            continue
        if payload.get("status") and "hits" not in payload and "geonames" not in payload:
            api_error += 1
            continue
        hits = payload.get("hits")
        geonames = payload.get("geonames")
        if isinstance(hits, list):
            if hits:
                ok += 1
            else:
                no_hit += 1
        elif isinstance(geonames, list):
            if geonames:
                ok += 1
            else:
                no_hit += 1
        else:
            other += 1
    return {
        "n_cache_keys": n,
        "query_ok": ok,
        "query_no_hit": no_hit,
        "query_api_error": api_error,
        "query_other": other,
        "note": "Query-level only; cannot join to (doc_id, mention) without re-running the linker.",
    }


def snapshot() -> dict:
    cards = {
        t: len(list((expcfg.CARDS_DIR / t).glob("*.json")))
        for t in ("C-simple", "C-standard", "C-precedent")
    }
    lib = list(expcfg.LIBRARIAN_DIR.glob("*.json"))
    pdfs = list(expcfg.PDF_DIR.glob("*.pdf")) if expcfg.PDF_DIR.exists() else []
    info = {
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "run_id": RUN_ID,
        "git_hint": "see repo HEAD; artifacts gitignored",
        "cards_per_tier": cards,
        "librarian_json": len(lib),
        "pdfs": len(pdfs),
        "files": {
            name: info
            for name, info in {
                "authority_rejections.jsonl": _file_info(expcfg.DATA_DIR / "authority_rejections.jsonl"),
                "entity_registry.json": _file_info(expcfg.DATA_DIR / "entity_registry.json"),
                "authority_cache.json": _file_info(expcfg.DATA_DIR / "authority_cache.json"),
                "kg_manifest.json": _file_info(expcfg.DATA_DIR / "kg" / "manifest.json"),
                "judge_agreement.json": _file_info(expcfg.SCORES_DIR / "judge_agreement.json"),
            }.items()
            if info
        },
    }
    return info


def main() -> None:
    FREEZE_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    snap = snapshot()
    (FREEZE_DIR / "2026-08-31-v4-manifest.json").write_text(
        json.dumps(snap, indent=2), encoding="utf-8"
    )

    accepted = extract_accepted("C-standard")
    uniq_acc = {r["candidate_uri"] for r in accepted}
    occ_loc = sum(1 for r in accepted if "id.loc.gov" in r["candidate_uri"])
    occ_gn = sum(1 for r in accepted if "geonames.org" in r["candidate_uri"])

    reg = entity_registry._load() or {}
    refusals, n_uniq_rej, n_unloc = extract_refusals(reg)
    n_temp = len({(r["surface"], r["candidate_uri"]) for r in refusals
                  if r["gate_outcome"] == "temporal_rejected"})
    n_score = len({(r["surface"], r["candidate_uri"]) for r in refusals
                   if r["gate_outcome"] == "score_rejected"})

    cache = cache_query_outcomes()

    acc_uris = {r["candidate_uri"] for r in accepted}
    rej_uris = {r["candidate_uri"] for r in refusals if r["candidate_uri"]}
    unloc_surfaces = sorted({
        r["surface"] for r in refusals if r["source"] == "ledger_unlocalized"
    })

    out_path = FREEZE_DIR / "gate_decisions.jsonl"
    with out_path.open("w", encoding="utf-8") as f:
        for row in accepted + refusals:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    summary = {
        "run_id": RUN_ID,
        "unit": "(doc_id, dc_field, surface, candidate_uri) for accepted card embeds; "
                "(doc_id from registry or empty, surface, candidate_uri) for ledger refusals",
        "accepted_occurrences": len(accepted),
        "accepted_unique_uris": len(uniq_acc),
        "accepted_occ_loc": occ_loc,
        "accepted_occ_geonames": occ_gn,
        "unique_refusal_pairs": n_uniq_rej,
        "unique_score_refusals": n_score,
        "unique_temporal_refusals": n_temp,
        "refusal_rows_after_registry_expand": len(refusals),
        "refusal_pairs_unlocalized": n_unloc,
        "unlocalized_surfaces": unloc_surfaces,
        "uris_in_both_accepted_and_ledger": len(acc_uris & rej_uris),
        "invalid_mixed_unit_rate_do_not_quote": round(
            n_uniq_rej / (len(uniq_acc) + n_uniq_rej), 4
        ) if uniq_acc else None,
        "cache_queries": cache,
        "cannot_reconstruct": [
            "ranked_candidates",
            "extractor mention_id",
            "mention-level no_hit/api_error",
            "candidates accepted by gate but omitted by the generator",
        ],
        "snapshot": {
            "cards_per_tier": snap["cards_per_tier"],
            "ledger_sha256": (snap["files"].get("authority_rejections.jsonl") or {}).get("sha256"),
            "registry_sha256": (snap["files"].get("entity_registry.json") or {}).get("sha256"),
            "cache_sha256": (snap["files"].get("authority_cache.json") or {}).get("sha256"),
        },
        "table_path": str(out_path),
        "n_table_rows": len(accepted) + len(refusals),
    }
    art = ARTIFACT_DIR / "e1_gate_summary.json"
    art.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"wrote {out_path} ({summary['n_table_rows']} rows)")
    print(f"wrote {art}")


if __name__ == "__main__":
    main()
