"""Seed `identity_items` from the frozen E1 structured-check trace (E2 full-population audit).

Usage (from experiments/review_app/):
    python seed_identity_items.py            # insert missing items, never overwrite labels
    python seed_identity_items.py --dry-run  # print counts only

Unit: one item per unique (entity_type, lowercased surface, candidate_uri) among trace rows whose
gate_outcome is accept / score_reject / temporal_reject. no_hit and cache_error rows have no
URI and are not identity decisions, so they are excluded (they are reported in E1 only).

Order: a single seeded Fisher–Yates shuffle over the whole population (seed below). Reviewers
see items in `sort_order`, so however many they finish, the finished prefix is a simple random
sample of the population — no stratification weights needed for the partial-completion case.

Re-running is idempotent: existing (trace_run_id, entity_type, surface, candidate_uri) rows are
left untouched (so labels keep their item ids); only new rows are inserted.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import appdb  # noqa: E402

EXP_ROOT = Path(__file__).resolve().parents[1]
TRACE = EXP_ROOT / "data" / "freeze" / "e1_structured_check_trace.jsonl"
MANIFEST = EXP_ROOT / "artifacts" / "e2_identity_population_manifest.json"
SHUFFLE_SEED = 20260903
AUDITED_OUTCOMES = ("accept", "score_reject", "temporal_reject")


def load_trace() -> list[dict]:
    return [json.loads(l) for l in TRACE.read_text(encoding="utf-8").splitlines() if l.strip()]


def collapse(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("gate_outcome") not in AUDITED_OUTCOMES or not r.get("candidate_uri"):
            continue
        # MySQL's utf8mb4 general collation is case-insensitive and the E1 trace already dedupes
        # per issue on the lowercased surface, so collapse case variants ("MIAMI LIFE"/"Miami Life").
        groups[(r["entity_type"], (r.get("normalized_surface") or r["surface"]).lower(),
                r["candidate_uri"])].append(r)

    items = []
    for (etype, _norm, uri), grp in groups.items():
        grp = sorted(grp, key=lambda r: (r["doc_id"], r.get("entity_rank", 0)))
        head = grp[0]
        surface = head["surface"]
        surface_variants = sorted({r["surface"] for r in grp})
        outcomes = {r["gate_outcome"] for r in grp}
        # The deployed gate is deterministic per (surface, uri) except for the temporal check,
        # whose activity window can differ per issue when the registry has no years. Record the
        # most conservative outcome and keep the per-row detail in the manifest.
        if len(outcomes) > 1:
            outcome = ("temporal_reject" if "temporal_reject" in outcomes
                       else "score_reject" if "score_reject" in outcomes else "accept")
        else:
            outcome = head["gate_outcome"]
        years = [r["g0_year_min"] for r in grp if r.get("g0_year_min") is not None]
        years_max = [r["g0_year_max"] for r in grp if r.get("g0_year_max") is not None]
        card_fields = sorted({f for r in grp for f in (r.get("card_fields") or [])})
        items.append({
            "trace_run_id": head["trace_run_id"],
            "entity_type": etype,
            "surface": surface,
            "surface_variants": surface_variants,
            "normalized_surface": head.get("normalized_surface") or surface.lower(),
            "candidate_uri": uri,
            "candidate_label": head.get("candidate_label") or "",
            "doc_ids": sorted({r["doc_id"] for r in grp}),
            "n_rows": len(grp),
            "g0_found": any(r.get("g0_found") for r in grp),
            "g0_local_context": next((r["g0_local_context"] for r in grp if r.get("g0_local_context")), None),
            "g0_year_min": min(years) if years else None,
            "g0_year_max": max(years_max) if years_max else None,
            "g0_year_source": head.get("g0_year_source"),
            "card_fields": card_fields,
            "gate_outcome": outcome,
            "mixed_outcomes": sorted(outcomes) if len(outcomes) > 1 else None,
            "authority_source": head.get("authority_source") or "",
            "candidate_score": head.get("candidate_score"),
            "score_threshold": head.get("score_threshold"),
            "score_state": head.get("score_state"),
            "temporal_state": head.get("temporal_state"),
            "candidate_birth_year": head.get("candidate_birth_year"),
            "candidate_death_year": head.get("candidate_death_year"),
            "decision_reason": head.get("decision_reason") or "",
            "card_embedded_rows": sum(1 for r in grp if r.get("card_embedded")),
            "card_uri_occurrences": sum(int(r.get("card_uri_occurrences") or 0) for r in grp),
        })
    # deterministic base order before the seeded shuffle
    items.sort(key=lambda it: (it["entity_type"], it["surface"].lower(), it["candidate_uri"]))
    rng = random.Random(SHUFFLE_SEED)
    rng.shuffle(items)
    for i, it in enumerate(items, start=1):
        it["sort_order"] = i
    return items


def write_manifest(items: list[dict]) -> None:
    by_outcome = defaultdict(int)
    by_type = defaultdict(int)
    by_source = defaultdict(int)
    rows_by_outcome = defaultdict(int)
    for it in items:
        by_outcome[it["gate_outcome"]] += 1
        by_type[it["entity_type"]] += 1
        by_source[it["authority_source"]] += 1
        rows_by_outcome[it["gate_outcome"]] += it["n_rows"]
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps({
        "trace": str(TRACE.relative_to(EXP_ROOT)).replace("\\", "/"),
        "trace_run_id": items[0]["trace_run_id"] if items else None,
        "shuffle_seed": SHUFFLE_SEED,
        "unit": "unique (entity_type, lowercased surface, candidate_uri) with gate_outcome in accept/score_reject/temporal_reject",
        "n_items_with_case_variants": sum(1 for it in items if len(it["surface_variants"]) > 1),
        "n_items": len(items),
        "items_by_outcome": dict(by_outcome),
        "trace_rows_by_outcome": dict(rows_by_outcome),
        "items_by_entity_type": dict(by_type),
        "items_by_authority_source": dict(by_source),
        "n_items_mixed_outcomes": sum(1 for it in items if it["mixed_outcomes"]),
        "note": "sort_order is a seeded random permutation; any labelled prefix is a simple random sample.",
    }, indent=2), encoding="utf-8")


def seed(items: list[dict]) -> tuple[int, int]:
    inserted = skipped = 0
    with appdb.connect() as c:
        cur = c.cursor()
        for it in items:
            cur.execute(
                "SELECT id FROM identity_items WHERE trace_run_id=%s AND entity_type=%s "
                "AND surface=%s AND candidate_uri=%s",
                (it["trace_run_id"], it["entity_type"], it["surface"], it["candidate_uri"]),
            )
            if cur.fetchone():
                skipped += 1
                continue
            cur.execute(
                """INSERT INTO identity_items
                   (trace_run_id, sort_order, shuffle_seed, entity_type, surface, normalized_surface,
                    candidate_uri, candidate_label, doc_ids_json, n_rows, g0_found, g0_local_context,
                    g0_year_min, g0_year_max, g0_year_source, card_fields_json,
                    gate_outcome, authority_source, candidate_score, score_threshold, score_state,
                    temporal_state, candidate_birth_year, candidate_death_year, decision_reason,
                    card_embedded_rows, card_uri_occurrences)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (it["trace_run_id"], it["sort_order"], SHUFFLE_SEED, it["entity_type"], it["surface"],
                 it["normalized_surface"][:255], it["candidate_uri"], it["candidate_label"][:255],
                 json.dumps(it["doc_ids"]), it["n_rows"], int(it["g0_found"]), it["g0_local_context"],
                 it["g0_year_min"], it["g0_year_max"], it["g0_year_source"], json.dumps(it["card_fields"]),
                 it["gate_outcome"], it["authority_source"], it["candidate_score"], it["score_threshold"],
                 it["score_state"], it["temporal_state"], it["candidate_birth_year"],
                 it["candidate_death_year"], it["decision_reason"], it["card_embedded_rows"],
                 it["card_uri_occurrences"]),
            )
            inserted += 1
    return inserted, skipped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reset", action="store_true",
                    help="delete all identity_items first; refused if any identity_labels exist")
    args = ap.parse_args()
    if args.reset and not args.dry_run:
        with appdb.connect() as c:
            cur = c.cursor()
            cur.execute("SELECT COUNT(*) AS n FROM identity_labels")
            if cur.fetchone()["n"]:
                raise SystemExit("refusing --reset: identity_labels is not empty")
            cur.execute("DELETE FROM identity_items")
            print("reset: identity_items emptied")

    items = collapse(load_trace())
    write_manifest(items)
    print(json.dumps(json.loads(MANIFEST.read_text(encoding="utf-8")), indent=2))
    if args.dry_run:
        return 0
    inserted, skipped = seed(items)
    print(f"inserted={inserted} skipped_existing={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
