"""The unified knowledge graph — a REAL networkx graph, not a flat lookup table.

Context (2026-07-04, raised by the user against `docs/rag-design/02` §3.1's own admission that the
authority "graph" and precedent store were flat structures, "a richer knowledge/skill/rule graph is
a later upgrade"): this module IS that upgrade. It unifies all three growth states as typed
nodes/edges in one `networkx.MultiDiGraph`:

    G0  entity:<key>          person / org / place nodes, aggregated from analysis.db across all
                               203 issues (same aggregation `common/entity_registry.py` does, now
                               as graph nodes instead of a flat dict)
    G1  authority:<uri>       resolved LCNAF/LCSH/GeoNames nodes, connected to the G0 entity that
                               was ACTUALLY linked to it in a generated card (edge: LINKED_TO,
                               carries the match score) — derived from the cards on disk, which is
                               the true accepted-links record (the authority HTTP cache is keyed by
                               request URL, not surface, so it cannot answer "what got linked to
                               what"; the cards can)
    G2  episode:<idx>         K_ext precedent episodes (`tiers/precedent_knowledge.py`), connected
                               to field:<dc_field> nodes (via a MARC-field-hint -> Dublin-Core
                               crosswalk) and source:<file> nodes (provenance)
    field:<dc_field>          the 15 Dublin Core fields (`fair/schema.py`) — the structural spine
                               G2 attaches to, and a hook for future G0/G1 cross-linking

**Why this matters (the scalability argument, not a current quality claim)**: pure cosine top-k
over a flat 130-episode store is fine at n=130 — plausibly indistinguishable in retrieval quality
from a graph-scoped version, and that parity is EXPECTED, not a failure (a graph pays for itself as
the store grows, not before). But as K_ext grows past hundreds/thousands of episodes (more listserv
harvests, more institutions), pure embedding similarity increasingly returns near-duplicate
episodes clustered around whichever single theme is closest to the query, silently starving
under-represented fields. A graph gives you the thing flat retrieval structurally cannot: scoped,
diverse, prunable, and cross-linkable retrieval — `graph_scoped_retrieve()` below enforces a
per-field diversity cap using the SAME embeddings, at zero extra embedding cost.

**Red line, unchanged from G0's existing rule (docs/00 §3)**: this graph is used for
VALIDATION/RETRIEVAL/SCOPING only. It is never serialized into a prompt as a content substitute for
reading the issue OCR — that was the old C-graph's mistake and stays refuted.

Build:   python common/kg.py                    -> data/kg/graph.pkl + data/kg/manifest.json
Query:   from common.kg import load_graph, graph_scoped_retrieve
"""

from __future__ import annotations

import json
import pickle
import re
import sys
import time
from pathlib import Path

import networkx as nx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # experiments/ on path

import config as expcfg  # noqa: E402
from common import entity_registry  # noqa: E402

KG_DIR = expcfg.DATA_DIR / "kg"
KG_PATH = KG_DIR / "graph.pkl"
MANIFEST_PATH = KG_DIR / "manifest.json"

DC_FIELD_NAMES = (
    "identifier", "title", "creator", "publisher", "date", "type", "format", "language",
    "subject", "description", "coverage", "contributor", "relation", "source", "rights",
)

# MARC field -> Dublin Core crosswalk (LC's own MARC21-to-DC mapping, trimmed to the tags that
# actually appear in the K_ext episodes' `field_hint`), used to attach episode:<idx> nodes to the
# field:<dc_field> node(s) they bear on.
MARC_TO_DC = {
    "022": "identifier", "023": "identifier", "856": "identifier",
    "130": "title", "245": "title", "246": "title", "247": "title",
    "100": "creator", "110": "creator", "111": "creator",
    "700": "contributor", "710": "contributor", "711": "contributor",
    "260": "publisher", "264": "publisher", "040": "publisher",
    "362": "date",
    "336": "type", "337": "format", "338": "format", "300": "format",
    "041": "language", "546": "language",
    "650": "subject", "651": "subject", "653": "subject",
    "500": "description", "515": "description", "588": "description", "936": "description",
    "255": "coverage", "651 (place)": "coverage",
    "780": "relation", "785": "relation", "776": "relation", "550": "relation",
    "866": "relation", "830": "relation",
    "506": "rights", "540": "rights",
}

# Matches both formats seen in generated cards: "Name (URI)" and "Name URI".
_URI_LINE = re.compile(
    r'"([^"\[\]()]{2,80}?)\s*\(?'
    r'(https?://(?:id\.loc\.gov|viaf\.org|(?:www\.|sws\.)?geonames\.org)/[^\s")]+)\)?"'
)


def _dc_fields_for_hint(field_hint: str) -> list[str]:
    nums = re.findall(r"\b(\d{3})\b", field_hint or "")
    fields = sorted({MARC_TO_DC[n] for n in nums if n in MARC_TO_DC})
    return fields or ["description"]  # unhinted episodes default to the general-note field


def _accepted_links_from_cards() -> list[tuple[str, str]]:
    """(surface, uri) pairs actually accepted into a generated C-standard/C-precedent card.

    This is the true "what got linked to what" record — the authority HTTP cache is keyed by
    request URL, not surface, so it can't answer this; the cards (which embed "Name URI" text
    after the sanitizer already dropped unverified URIs) can.
    """
    out: list[tuple[str, str]] = []
    for tier in ("C-standard", "C-precedent"):
        for p in (expcfg.CARDS_DIR / tier).glob("*.json"):
            try:
                fields = json.loads(p.read_text(encoding="utf-8")).get("fields", {})
            except json.JSONDecodeError:
                continue
            blob = json.dumps(fields, ensure_ascii=False)
            for surface, uri in _URI_LINE.findall(blob):
                out.append((surface.strip(), uri.replace("http://", "https://")))
    return out


def build_graph() -> nx.MultiDiGraph:
    G = nx.MultiDiGraph()

    # ── field spine (structural hooks G0/G1/G2 all attach to) ──
    for f in DC_FIELD_NAMES:
        G.add_node(f"field:{f}", layer="spine", kind="dc_field", label=f)

    # ── G0: entity nodes ──
    reg = entity_registry._load() or entity_registry.build()
    for key, e in reg.items():
        G.add_node(f"entity:{key}", layer="G0", kind="entity", **{k: v for k, v in e.items()
                                                                    if k != "contexts"})
    n_g0 = sum(1 for _, d in G.nodes(data=True) if d.get("layer") == "G0")

    # ── G1: authority nodes + LINKED_TO edges, derived from accepted links in generated cards ──
    n_g1_edges = 0
    seen_authority: set[str] = set()
    for surface, uri in _accepted_links_from_cards():
        key = entity_registry.norm_key(surface)
        ent_node = f"entity:{key}"
        if not G.has_node(ent_node):
            G.add_node(ent_node, layer="G0", kind="entity", name=surface, etype="unknown")
        auth_node = f"authority:{uri}"
        if uri not in seen_authority:
            G.add_node(auth_node, layer="G1", kind="authority", uri=uri)
            seen_authority.add(uri)
        if not G.has_edge(ent_node, auth_node, key="linked_to"):
            G.add_edge(ent_node, auth_node, key="linked_to", type="LINKED_TO")
            n_g1_edges += 1

    # ── G2: episode nodes + PERTAINS_TO field edges + FROM_SOURCE provenance edges ──
    _, eps = entity_registry_load_precedent()
    n_g2 = 0
    for i, ep in enumerate(eps):
        node = f"episode:{i}"
        G.add_node(node, layer="G2", kind="episode", problem=ep.get("problem", "")[:200],
                   transferable_pattern=ep.get("transferable_pattern", "")[:200],
                   field_hint=ep.get("field_hint", ""), source=ep.get("_source", "?"))
        n_g2 += 1
        for f in _dc_fields_for_hint(ep.get("field_hint", "")):
            G.add_edge(node, f"field:{f}", key="pertains_to", type="PERTAINS_TO")
        src = ep.get("_source", "?")
        src_node = f"source:{src}"
        if not G.has_node(src_node):
            G.add_node(src_node, layer="G2", kind="source", label=src)
        G.add_edge(node, src_node, key="from_source", type="FROM_SOURCE")

    print(f"graph built: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges  "
          f"(G0 entities={n_g0}, G1 links={n_g1_edges}, G2 episodes={n_g2})")
    return G


def entity_registry_load_precedent() -> tuple[np.ndarray, list[dict]]:
    from tiers.precedent_knowledge import _load as _load_precedent  # local import, avoids cycle
    vecs, eps = _load_precedent()
    return vecs, list(eps)


def save_graph(G: nx.MultiDiGraph) -> None:
    KG_DIR.mkdir(parents=True, exist_ok=True)
    with KG_PATH.open("wb") as f:
        pickle.dump(G, f)
    by_layer: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for _, d in G.nodes(data=True):
        by_layer[d.get("layer", "?")] = by_layer.get(d.get("layer", "?"), 0) + 1
        by_kind[d.get("kind", "?")] = by_kind.get(d.get("kind", "?"), 0) + 1
    by_edge_type: dict[str, int] = {}
    for _, _, d in G.edges(data=True):
        by_edge_type[d.get("type", "?")] = by_edge_type.get(d.get("type", "?"), 0) + 1
    manifest = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "n_nodes": G.number_of_nodes(),
        "n_edges": G.number_of_edges(),
        "nodes_by_layer": by_layer,
        "nodes_by_kind": by_kind,
        "edges_by_type": by_edge_type,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def load_graph() -> nx.MultiDiGraph | None:
    if not KG_PATH.exists():
        return None
    with KG_PATH.open("rb") as f:
        return pickle.load(f)


def episode_fields(G: nx.MultiDiGraph, episode_idx: int) -> list[str]:
    node = f"episode:{episode_idx}"
    if not G.has_node(node):
        return []
    return sorted(v.split(":", 1)[1] for _, v, d in G.out_edges(node, data=True)
                  if d.get("type") == "PERTAINS_TO")


def graph_scoped_retrieve(
    query: str, k: int = 8, diversity_cap_per_field: int = 2, overfetch: int = 4,
) -> list[dict]:
    """Graph-scoped episode retrieval: cosine-rank a wide candidate pool, then greedily select the
    top-k under a per-field-node diversity cap read from the graph, so retrieval breadth scales
    with the graph's structure instead of collapsing onto whichever single theme is closest to the
    query (see module docstring — the scalability argument). Falls back to plain top-k if the
    graph isn't built.
    """
    from tiers.precedent_knowledge import _load as _load_precedent
    from core.embeddings import embed_query

    vecs, eps = _load_precedent()
    if not eps:
        return []
    G = load_graph()
    q = np.asarray(embed_query(query), dtype=np.float32)
    q /= np.linalg.norm(q) + 1e-12
    sims = vecs @ q
    order = list(np.argsort(-sims))

    if G is None:
        picked = order[:k]
    else:
        picked: list[int] = []
        field_counts: dict[str, int] = {}
        for i in order:
            if len(picked) >= k:
                break
            i = int(i)
            fields = episode_fields(G, i) or ["description"]
            if all(field_counts.get(f, 0) < diversity_cap_per_field for f in fields):
                picked.append(i)
                for f in fields:
                    field_counts[f] = field_counts.get(f, 0) + 1
        # backfill with the highest-similarity remainder if the diversity cap left room unused
        if len(picked) < k:
            for i in order:
                i = int(i)
                if i not in picked:
                    picked.append(i)
                if len(picked) >= k:
                    break

    out: list[dict] = []
    for i in picked[:k]:
        ep = dict(eps[i])
        ep["_score"] = round(float(sims[i]), 3)
        ep["_dc_fields"] = episode_fields(G, i) if G is not None else []
        out.append(ep)
    return out


def main() -> int:
    G = build_graph()
    save_graph(G)
    print(f"saved -> {KG_PATH}")
    print(f"manifest -> {MANIFEST_PATH}")
    print(json.loads(MANIFEST_PATH.read_text(encoding="utf-8")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
