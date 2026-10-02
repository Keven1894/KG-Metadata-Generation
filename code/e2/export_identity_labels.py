"""Export the E2 identity audit (items × labels, unblinded) to the freeze directory.

Usage (from experiments/review_app/):
    python export_identity_labels.py

Writes  experiments/data/freeze/e2_identity_labels.jsonl   (gitignored; contains outcomes)
and     experiments/artifacts/e2_identity_progress.json    (committed; counts only, no per-item data)
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import identity  # noqa: E402

EXP_ROOT = Path(__file__).resolve().parents[1]
OUT = EXP_ROOT / "data" / "freeze" / "e2_identity_labels.jsonl"
PROGRESS = EXP_ROOT / "artifacts" / "e2_identity_progress.json"


def main() -> int:
    rows = identity.export_rows()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    labelled = [r for r in rows if r.get("label")]
    items = {r["id"]: r["gate_outcome"] for r in rows}
    labelled_items = {r["id"] for r in labelled}
    cell = Counter((r["gate_outcome"], r["label"]) for r in labelled)
    PROGRESS.write_text(json.dumps({
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_items": len(items),
        "n_items_labelled": len(labelled_items),
        "n_labels": len(labelled),
        "n_reviewers": len({r["reviewer_id"] for r in labelled}),
        "labelled_by_outcome": dict(Counter(r["gate_outcome"] for r in labelled)),
        "items_by_outcome": dict(Counter(items.values())),
        "outcome_x_label": {f"{o}|{l}": n for (o, l), n in sorted(cell.items())},
        "total_active_min": round(sum(int(r.get("active_ms") or 0) for r in labelled) / 60000.0, 1),
    }, indent=2), encoding="utf-8")
    print(f"wrote {OUT} ({len(rows)} rows, {len(labelled)} labels) and {PROGRESS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
