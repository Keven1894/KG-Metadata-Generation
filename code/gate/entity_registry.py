"""G0 — the base knowledge graph: a corpus-level ENTITY REGISTRY read from the collection itself.

This is the substrate of the graph-growth framework (docs/rag-design/00): before any external
knowledge is layered on, we READ all 203 issues and record who/what/where the collection is
actually about — every person/business/place surface from analysis.db, aggregated across issues
with occurrence counts, year spans, and role/context snippets.

G0 is used on the VALIDATION side only (red line #2 in doc 00): it grounds/disambiguates external
authority links (G1) — e.g. rejecting an LCNAF match whose lifespan cannot overlap the entity's
local activity window — and keys retrieval. It NEVER injects cross-issue facts into a card.

Build:  python common/entity_registry.py            -> data/entity_registry.json
Query:  from common.entity_registry import lookup, registry_stats
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # experiments/ on path

import config as expcfg

REGISTRY_PATH = expcfg.DATA_DIR / "entity_registry.json"

# The collection's publication window — the widest activity window any local entity can have.
CORPUS_YEAR_MIN, CORPUS_YEAR_MAX = 1927, 1949

_PUNCT = str.maketrans({c: " " for c in ",.;:()[]{}'\"/-"})


def norm_key(name: str) -> str:
    """Normalization used as the registry key (case/punct/space-insensitive)."""
    return " ".join((name or "").lower().translate(_PUNCT).split())


def _issue_years(conn: sqlite3.Connection) -> dict[str, int]:
    """Issue year per doc_id. analysis.db only dates ~100/203 issues; librarian MODS dates all
    203, so it fills the gaps (dates are bibliographic facts from the catalog, not AI output)."""
    out: dict[str, int] = {}
    for doc_id, year in conn.execute("SELECT doc_id, year FROM issues"):
        y = str(year or "")
        if y.isdigit():
            out[doc_id] = int(y)
        else:
            p = expcfg.LIBRARIAN_DIR / f"{doc_id}.json"
            if p.exists():
                try:
                    d = json.loads(p.read_text(encoding="utf-8")).get("fields", {}).get("date", "")
                except json.JSONDecodeError:
                    d = ""
                if str(d)[:4].isdigit():
                    out[doc_id] = int(str(d)[:4])
    return out


def build() -> dict:
    """Aggregate people/businesses/places across all issues into the registry."""
    conn = sqlite3.connect(str(expcfg.ANALYSIS_DB))
    years = _issue_years(conn)

    reg: dict[str, dict] = {}

    def add(name: str, etype: str, doc_id: str, attr: str, context: str) -> None:
        key = norm_key(name)
        if len(key) < 3:
            return
        e = reg.setdefault(key, {
            "name": name, "etype": etype, "n_mentions": 0,
            "doc_ids": [], "years": [], "attrs": {}, "contexts": [],
        })
        e["n_mentions"] += 1
        if doc_id not in e["doc_ids"]:
            e["doc_ids"].append(doc_id)
        y = years.get(doc_id)
        if y and y not in e["years"]:
            e["years"].append(y)
        if attr:
            e["attrs"][attr] = e["attrs"].get(attr, 0) + 1
        if context and len(e["contexts"]) < 5 and context not in e["contexts"]:
            e["contexts"].append(context[:160])

    for name, role, ctx, doc_id in conn.execute(
            "SELECT name, role, context, doc_id FROM people"):
        add(name, "person", doc_id, (role or "").strip(), (ctx or "").strip())
    for name, btype, ctx, doc_id in conn.execute(
            "SELECT name, type, context, doc_id FROM businesses"):
        add(name, "org", doc_id, (btype or "").strip(), (ctx or "").strip())
    for name, ptype, ctx, doc_id in conn.execute(
            "SELECT name, type, context, doc_id FROM places"):
        add(name, "place", doc_id, (ptype or "").strip(), (ctx or "").strip())
    conn.close()

    # compact: sort years, keep top attrs, cap doc_ids
    for e in reg.values():
        e["years"] = sorted(e["years"])
        e["n_issues"] = len(e["doc_ids"])
        e["doc_ids"] = e["doc_ids"][:20]
        e["attrs"] = dict(sorted(e["attrs"].items(), key=lambda kv: -kv[1])[:4])
    return reg


@lru_cache(maxsize=1)
def _load() -> dict[str, dict]:
    if not REGISTRY_PATH.exists():
        return {}
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def available() -> bool:
    return bool(_load())


def lookup(surface: str) -> dict | None:
    """Exact (normalized) registry entry for a surface form, else None."""
    return _load().get(norm_key(surface))


def surname_entries(surface: str) -> list[dict]:
    """All person entries sharing the (heuristic) surname — for partial-name grounding."""
    toks = norm_key(surface).split()
    if not toks:
        return []
    sur = toks[-1]
    return [e for k, e in _load().items()
            if e.get("etype") == "person" and k.split() and k.split()[-1] == sur]


def activity_window(surface: str) -> tuple[int, int]:
    """The local activity window for an entity: its observed year span in the collection,
    padded by a human-plausibility margin; falls back to the corpus window."""
    e = lookup(surface)
    years = (e or {}).get("years") or []
    if years:
        return min(years), max(years)
    return CORPUS_YEAR_MIN, CORPUS_YEAR_MAX


def local_context(surface: str) -> str:
    """One-line local identity summary (for prompts/ledgers): roles + span + spread."""
    e = lookup(surface)
    if not e:
        return ""
    attrs = ", ".join(e.get("attrs") or [])
    yrs = e.get("years") or []
    span = f"{min(yrs)}-{max(yrs)}" if yrs else "?"
    return f"{e['name']} [{e['etype']}] {attrs or 'no role recorded'}; seen in {e['n_issues']} issue(s), {span}"


def registry_stats() -> dict:
    reg = _load()
    by = {}
    for e in reg.values():
        by[e["etype"]] = by.get(e["etype"], 0) + 1
    return {"entities": len(reg), "by_type": by,
            "multi_issue": sum(1 for e in reg.values() if e["n_issues"] > 1)}


def main() -> int:
    reg = build()
    REGISTRY_PATH.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
    _load.cache_clear()
    s = registry_stats()
    print(f"G0 entity registry -> {REGISTRY_PATH}")
    print(f"  entities   : {s['entities']}  {s['by_type']}")
    print(f"  multi-issue: {s['multi_issue']}")
    for probe in ("W. E. Ellis", "Alexander Robbins", "Reubin Clein", "Little River"):
        print(f"  lookup({probe!r}): {local_context(probe) or '(absent)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
