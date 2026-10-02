"""Tiered FAIR-metadata card generation.

The ablation holds the generation step constant (same prompt, same schema, same model) and varies
ONLY the *knowledge layered on top of the issue OCR*. EVERY tier reads the raw OCR; what differs is
the externalized knowledge fed alongside it:

    C-simple    : raw OCR only                                        -- untrained reader (baseline)
    C-standard  : OCR + cataloging-standard knowledge pack
                  + authority-linked entities (LCNAF/LCSH/GeoNames)   -- "straight-A student" (学霸)
    C-precedent : C-standard + external operational memory (top-k transferable episodes
                  retrieved from OTHER institutions' cataloging discussions)  -- "veteran technician" (老技师)

A 4th rung — C-standard + external precedent + OUR OWN HITL gold-correction memory ("our seasoned
employee", 熟练员工) — is FUTURE WORK: it can only grow as curators submit gold in the review tool.

The earlier C-graph / C-om tiers (read a pre-extracted analysis.db blob, never the OCR) were
REFUTED as circular + entity-bleed and have been removed. Do not reintroduce them.
"""

from __future__ import annotations

import json
import re
import time

import config as expcfg
from common import authority, db
from common.corpus import read_text
from core.llm import chat
from fair.schema import DC_FIELDS, empty_card_fields, schema_prompt_block
from common.kg import graph_scoped_retrieve
from tiers.precedent_knowledge import (
    issue_profile,
    precedent_evidence_block,
    store_available,
)
from tiers.standard_knowledge import authority_evidence_block, cataloging_standard_block

SYSTEM = (
    "You are an expert archival cataloger producing FAIR / Dublin Core metadata for individual "
    "issues of the 'Miami Life' newspaper (Miami, Florida, 1927-1949). The evidence is OCR-derived "
    "and may be noisy; infer carefully and never fabricate facts not supported by the evidence."
)

_MAX_OCR_CHARS = 60_000  # mirror HistoryChat's analyze_issue truncation behavior


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _parse_json(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw[raw.index("\n") + 1:] if "\n" in raw else raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Some OCR-derived text carries stray ASCII control bytes (e.g. a mis-encoded apostrophe)
    # that the model copies verbatim into a string value — illegal per the JSON spec even
    # though python-side the raw text is otherwise well-formed. Strip and retry once.
    try:
        return json.loads(_CONTROL_CHARS.sub("", raw))
    except json.JSONDecodeError:
        return {}


def _normalize(fields: dict) -> dict:
    """Coerce to the schema: ensure every DC field present with the right container type."""
    out = empty_card_fields()
    for k, spec in DC_FIELDS.items():
        v = fields.get(k)
        if v is None:
            continue
        if spec["multi"]:
            if isinstance(v, str):
                v = [v] if v.strip() else []
            elif isinstance(v, list):
                v = [str(x).strip() for x in v if str(x).strip()]
            else:
                v = [str(v)]
        else:
            v = "" if v is None else str(v).strip()
        out[k] = v
    return out


# ── context builders ─────────────────────────────────────────────────────────

def context_simple(doc_id: str) -> str:
    text = read_text(doc_id)
    if len(text) > _MAX_OCR_CHARS:
        text = text[:50_000] + "\n\n[... middle omitted ...]\n\n" + text[-10_000:]
    return f"RAW OCR TEXT OF THE ISSU<path>"


def _gather_entities(doc_id: str, cap: int = 8) -> list[dict]:
    """Collect this issue's top entities (people/orgs/places) + topics for authority linking."""
    a = db.raw_analysis(doc_id)
    row = db.issue_row(doc_id)

    def _name(x) -> str:
        return (x.get("name") if isinstance(x, dict) else str(x or "")).strip()

    ents: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(name: str, etype: str) -> None:
        key = (etype, name.lower())
        if name and len(name) > 2 and key not in seen:
            seen.add(key)
            ents.append({"surface": name, "etype": etype})

    for x in (a.get("people") or [])[:cap]:
        add(_name(x), "person")
    for x in (a.get("businesses") or [])[:cap]:
        add(_name(x), "org")
    for x in (a.get("places") or [])[:cap]:
        add(_name(x), "place")
    for t in (a.get("topics") or row.get("topics") or [])[:cap]:
        add(_name(t), "subject")
    return ents


def _truncate_ocr(text: str) -> str:
    if len(text) > _MAX_OCR_CHARS:
        return text[:50_000] + "\n\n[... middle omitted ...]\n\n" + text[-10_000:]
    return text


def _standard_blocks(doc_id: str) -> tuple[str, str]:
    """Shared C-standard evidence: returns (knowledge+authority preamble, truncated OCR)."""
    text = _truncate_ocr(read_text(doc_id))
    linked = authority.link_many(_gather_entities(doc_id))
    preamble = (
        f"{cataloging_standard_block()}\n\n"
        f"{authority_evidence_block(linked)}\n\n"
        "AUTHORITY RULE: Only attach an authority URI that appears verbatim in the "
        "AUTHORITY-LINKED ENTITIES list above. NEVER invent, guess, or recall an id.loc.gov / "
        "VIAF / GeoNames identifier from memory — an unverified identifier is worse than none. "
        "If an entity has no provided URI, record its name without any identifier."
    )
    return preamble, text


def context_standard(doc_id: str) -> str:
    """C-standard (doc 26): OCR + externalized cataloging-standard knowledge + authority-linked
    entities (LCNAF/LCSH/GeoNames URIs). This is the 'straight-A student' tier: it knows the
    cataloging rules and can resolve entities to real persistent identifiers (the FAIR F+I payoff).
    """
    preamble, text = _standard_blocks(doc_id)
    return f"{preamble}\n\nRAW OCR TEXT OF THE ISSU<path>"


def context_precedent(doc_id: str, k: int = 8) -> str:
    """C-precedent ('veteran technician' / 老技师): C-standard + external OPERATIONAL MEMORY.

    On top of the C-standard pack we retrieve the top-k transferable ⟨problem→judgment→resolution⟩
    episodes (mined from OTHER institutions' cataloging notes) most relevant to THIS issue's signals,
    and feed them as advisory judgment calls. The OCR and authority discipline are unchanged — the
    only added evidence is borrowed experience, not new facts about the issue.

    Retrieval is GRAPH-SCOPED (`common/kg.py`), not plain cosine top-k: candidates are ranked by
    embedding similarity, then selected under a per-Dublin-Core-field diversity cap read from the
    unified knowledge graph (G2 episode -> field node edges), so the k slots can't collapse onto one
    dominant theme as the store grows. Falls back to plain top-k automatically if the graph isn't
    built (`common.kg.load_graph() is None`).
    """
    preamble, text = _standard_blocks(doc_id)
    episodes = graph_scoped_retrieve(issue_profile(doc_id, text), k=k) if store_available() else []
    return (
        f"{preamble}\n\n"
        f"{precedent_evidence_block(episodes)}\n\n"
        f"RAW OCR TEXT OF THE ISSU<path>"
    )


CONTEXT_BUILDERS = {
    "C-simple": context_simple,
    "C-standard": context_standard,
    "C-precedent": context_precedent,
}


# ── generation ───────────────────────────────────────────────────────────────

_AUTH_URI = re.compile(
    r"\s*(?:[\(\[]\s*(?:LCNAF|LCSH|VIAF|GeoNames|LC-Geographic)\s*[\)\]])?\s*"
    r"[\(\[]?"
    r"(https?://(?:id\.loc\.gov|viaf\.org|www\.geonames\.org|sws\.geonames\.org)/[^\s\)\]]+)"
    r"[\)\]]?",
    re.IGNORECASE,
)


def _sanitize_authorities(fields: dict, verified_uris: set[str]) -> tuple[dict, int]:
    """Strip any authority URI the LLM emitted that is NOT in our verified linker set.

    LLMs hallucinate plausible-looking id.loc.gov / VIAF identifiers even when instructed not to;
    an unverified PID is worse than none for FAIR. We keep the entity label, drop the bogus URI,
    and count removals for provenance.
    """
    removed = 0

    def clean(s: str) -> str:
        nonlocal removed
        def repl(m: re.Match) -> str:
            nonlocal removed
            url = m.group(1).rstrip(".,;")
            if url.replace("http://", "https://") in verified_uris:
                return m.group(0)  # keep verified
            removed += 1
            return ""  # drop hallucinated URI + its source tag
        return _AUTH_URI.sub(repl, s).strip()

    out = {}
    for k, v in fields.items():
        if isinstance(v, list):
            out[k] = [clean(x) if isinstance(x, str) else x for x in v]
        elif isinstance(v, str):
            out[k] = clean(v)
        else:
            out[k] = v
    return out, removed


def generate_card(doc_id: str, tier: str, model: str | None = None) -> dict:
    context = CONTEXT_BUILDERS[tier](doc_id)
    user = (
        f"Create one Dublin Core metadata card for this Miami Life issue.\n"
        f"Document ID: {doc_id}\n\n"
        f"{schema_prompt_block()}\n\n"
        f"=== EVIDENCE ===\n{context}\n=== END EVIDENCE ==="
    )
    t0 = time.time()
    raw = chat(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        model=model or expcfg.CHAT_MODEL,
        max_tokens=8000,
        temperature=0.1,
    )
    latency = round(time.time() - t0, 2)
    fields = _normalize(_parse_json(raw))

    hallucinated = 0
    if tier in ("C-standard", "C-precedent"):
        verified = {
            m["authority"]["uri"].replace("http://", "https://")
            for m in authority.link_many(_gather_entities(doc_id))
            if m.get("authority")
        }
        fields, hallucinated = _sanitize_authorities(fields, verified)

    return {
        "doc_id": doc_id,
        "tier": tier,
        "fields": fields,
        "provenance": {
            "latency_s": latency,
            "context_chars": len(context),
            "model": model or expcfg.CHAT_MODEL,
            "parse_ok": bool(_parse_json(raw)),
            "hallucinated_uris_removed": hallucinated,
        },
    }


if __name__ == "__main__":
    import sys
    did = sys.argv[1] if len(sys.argv) > 1 else "FI21052700_00002"
    tier = sys.argv[2] if len(sys.argv) > 2 else "C-precedent"
    print(json.dumps(generate_card(did, tier), ensure_ascii=False, indent=2))
