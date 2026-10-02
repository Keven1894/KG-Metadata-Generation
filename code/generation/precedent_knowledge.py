"""External operational-memory retrieval for the `C-precedent` tier (老技师 / veteran technician).

C-precedent = C-standard  +  the top-k transferable ⟨problem → judgment → resolution⟩ EPISODES that
other institutions' catalogers wrote down when they hit a peculiarity. These are NOT the cataloging
rules (C-standard already encodes those) — they are the *judgment calls* a veteran applies to a
messy issue: numbering that resets in January, an editor more famous than the paper, a weekend title
that differs, a frequency with a regular exception, etc.

The store is built by build_precedent_store.py from probe-confirmed episodes (control excluded).
Retrieval is cosine top-k over an embedded "issue profile" derived from the issue's own signals, so a
new Miami Life issue pulls the handful of episodes most relevant to ITS peculiarities.
"""

from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # experiments/ on path

import config as expcfg
from core.embeddings import embed_query

STORE_DIR = expcfg.DATA_DIR / "precedent" / "store"
_EMB_PATH = STORE_DIR / "embeddings.npy"
_EP_PATH = STORE_DIR / "episodes.json"


@lru_cache(maxsize=1)
def _load() -> tuple[np.ndarray, tuple[dict, ...]]:
    if not _EMB_PATH.exists() or not _EP_PATH.exists():
        return np.zeros((0, 0), dtype=np.float32), ()
    vecs = np.load(_EMB_PATH)
    eps = tuple(json.loads(_EP_PATH.read_text(encoding="utf-8")))
    return vecs, eps


def store_available() -> bool:
    vecs, eps = _load()
    return len(eps) > 0 and vecs.shape[0] == len(eps)


def retrieve_episodes(query: str, k: int = 8) -> list[dict]:
    """Return the k episodes most similar to `query` (each with a `_score` cosine similarity)."""
    vecs, eps = _load()
    if not eps:
        return []
    q = np.asarray(embed_query(query), dtype=np.float32)
    q /= np.linalg.norm(q) + 1e-12
    sims = vecs @ q
    order = np.argsort(-sims)[: min(k, len(eps))]
    out: list[dict] = []
    for i in order:
        ep = dict(eps[int(i)])
        ep["_score"] = round(float(sims[int(i)]), 3)
        out.append(ep)
    return out


def precedent_evidence_block(episodes: list[dict]) -> str:
    """Render retrieved episodes as advisory 'veteran technician' notes for the prompt."""
    if not episodes:
        return "VETERAN-CATALOGER PRECEDENTS: (none retrieved)"
    lines = [
        "VETERAN-CATALOGER PRECEDENTS (operational memory mined from OTHER institutions' "
        "newspaper-cataloging notes — these are JUDGMENT CALLS for messy issues, not rules; apply "
        "one ONLY if this issue actually shows the described situation, and never invent data to "
        "fit a precedent):",
    ]
    for n, ep in enumerate(episodes, 1):
        src = ep.get("_source", "?").replace(".txt", "")
        lines.append(
            f"  {n}. WHEN {ep.get('problem', '').strip()}\n"
            f"     DO   {ep.get('transferable_pattern', '').strip()}"
            + (f"  (field: {ep['field_hint'].strip()})" if ep.get("field_hint", "").strip() else "")
            + f"  [src: {src}]"
        )
    return "\n".join(lines)


def issue_profile(doc_id: str, ocr_head: str) -> str:
    """Build the retrieval query: the issue's own signals (title/numbering/frequency/editor cues).

    We use the masthead/front-matter slice of the OCR (where numbering, frequency, volume, editor and
    title usually live) plus the structured row, since those are exactly the features episodes key on.
    """
    from common import db  # local import: keeps this module importable without a live DB

    row = db.issue_row(doc_id)
    a = db.raw_analysis(doc_id)
    people = [
        (x.get("name") if isinstance(x, dict) else str(x)) for x in (a.get("people") or [])[:5]
    ]
    parts = [
        "Historical newspaper issue cataloging cues:",
        f"volume/numbering/date: {row.get('volume', '')} {row.get('date', '')}".strip(),
        f"topics: {', '.join(str(t) for t in (row.get('topics') or [])[:6])}",
        f"prominent people (possible editors/publishers): {', '.join(p for p in people if p)}",
        "masthead / front matter (numbering, frequency, title, editor often here):",
        ocr_head[:1500],
    ]
    return "\n".join(p for p in parts if p.strip())


if __name__ == "__main__":
    import sys

    q = sys.argv[1] if len(sys.argv) > 1 else "newspaper volume numbering resets in January, weekly"
    print(f"store_available={store_available()}  query={q!r}\n")
    for ep in retrieve_episodes(q, k=5):
        print(f"[{ep['_score']}] {ep.get('problem', '')[:90]}  [{ep.get('_source')}]")
