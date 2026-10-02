"""E2 blinded identity audit with fixed reviewer ranges."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import appdb

APP_DIR = Path(__file__).resolve().parent
CORPUS_DIR = Path(os.getenv("CORPUS_DIR", APP_DIR.parents[2] / "miamilife_txt"))

VISIBLE_COLS = (
    "id, sort_order, entity_type, surface, normalized_surface, candidate_uri, candidate_label, "
    "doc_ids_json, n_rows, g0_found, g0_local_context, g0_year_min, g0_year_max, g0_year_source, "
    "card_fields_json"
)
LABELS = ("correct", "wrong", "true_nil", "indeterminate")
SEVERITIES = ("critical", "major", "minor")
RESIDUAL_REASONS = (
    "same_name_same_era",
    "org_alias_or_parent",
    "insufficient_context",
    "equivalent_identifier",
    "out_of_kb",
    "editorial_not_identity",
)
MAX_CONTEXT_DOCS = 6
SNIPPET_RADIUS = 220
IDENTITY_REVIEWER_RANGES = {
    "r2": (101, 200),
    "r3": (201, 300),
    "r4": (301, 400),
    "r5": (401, 500),
}


def is_identity_reviewer(user: dict) -> bool:
    return (
        user.get("role") != "admin"
        and str(user.get("username", "")).lower() in IDENTITY_REVIEWER_RANGES
    )


def can_review_identity(user: dict) -> bool:
    return user.get("role") == "admin" or is_identity_reviewer(user)


def assigned_range(user: dict) -> tuple[int, int] | None:
    """Return a reviewer's inclusive sort-order range; admins are unrestricted."""
    if user.get("role") == "admin":
        return None
    return IDENTITY_REVIEWER_RANGES.get(str(user.get("username", "")).lower())


def _ocr_text(doc_id: str) -> str | None:
    with appdb.connect() as c:
        cur = c.cursor()
        cur.execute("SELECT ocr_path FROM issues WHERE doc_id=%s", (doc_id,))
        row = cur.fetchone()
    names = [row["ocr_path"]] if row and row.get("ocr_path") else []
    names += [f"{doc_id}_pdf.txt", f"{doc_id}.txt"]
    for name in names:
        f = CORPUS_DIR / name
        if f.exists():
            return f.read_text(encoding="utf-8", errors="ignore")
    hits = sorted(CORPUS_DIR.glob(f"{doc_id}*_pdf.txt")) + sorted(
        CORPUS_DIR.glob(f"{doc_id}*.txt")
    )
    if hits:
        return hits[0].read_text(encoding="utf-8", errors="ignore")
    return None


def _surface_patterns(surface: str) -> list[re.Pattern]:
    pats = [re.compile(re.escape(surface), re.IGNORECASE)]
    toks = [t for t in re.split(r"\s+", surface.strip()) if t]
    if len(toks) > 1:
        pats.append(re.compile(r"\s+".join(re.escape(t) for t in toks), re.IGNORECASE))
        longest = max(toks, key=len)
        if len(re.sub(r"\W", "", longest)) >= 4:
            pats.append(re.compile(re.escape(longest), re.IGNORECASE))
    return pats


def ocr_snippets(doc_id: str, surface: str, max_hits: int = 3) -> dict:
    text = _ocr_text(doc_id)
    if text is None:
        return {
            "doc_id": doc_id,
            "found": False,
            "snippets": [],
            "n_hits": 0,
            "match_level": None,
        }
    for level, pat in enumerate(_surface_patterns(surface)):
        matches = list(pat.finditer(text))
        if matches:
            out = []
            for match in matches[:max_hits]:
                start = max(0, match.start() - SNIPPET_RADIUS)
                end = min(len(text), match.end() + SNIPPET_RADIUS)
                out.append(
                    {
                        "before": re.sub(r"\s+", " ", text[start:match.start()]),
                        "hit": text[match.start():match.end()],
                        "after": re.sub(r"\s+", " ", text[match.end():end]),
                        "offset": match.start(),
                    }
                )
            return {
                "doc_id": doc_id,
                "found": True,
                "snippets": out,
                "n_hits": len(matches),
                "match_level": ("exact", "whitespace", "longest_token")[level],
            }
    return {
        "doc_id": doc_id,
        "found": True,
        "snippets": [],
        "n_hits": 0,
        "match_level": None,
    }


def _row_to_item(row: dict) -> dict:
    row = dict(row)
    row["doc_ids"] = json.loads(row.pop("doc_ids_json") or "[]")
    row["card_fields"] = json.loads(row.pop("card_fields_json") or "[]")
    return row


def first_item_id(bounds: tuple[int, int] | None = None) -> int | None:
    with appdb.connect() as c:
        cur = c.cursor()
        if bounds:
            cur.execute(
                "SELECT id FROM identity_items WHERE sort_order BETWEEN %s AND %s "
                "ORDER BY sort_order LIMIT 1",
                bounds,
            )
        else:
            cur.execute("SELECT id FROM identity_items ORDER BY sort_order LIMIT 1")
        row = cur.fetchone()
    return row["id"] if row else None


def next_item_for(
    reviewer_id: int, bounds: tuple[int, int] | None = None
) -> dict | None:
    with appdb.connect() as c:
        cur = c.cursor()
        range_sql = ""
        params: list[int] = [reviewer_id]
        if bounds:
            range_sql = " AND i.sort_order BETWEEN %s AND %s"
            params.extend(bounds)
        cur.execute(
            f"""SELECT {VISIBLE_COLS} FROM identity_items i
                WHERE NOT EXISTS (
                    SELECT 1 FROM identity_labels l
                    WHERE l.item_id=i.id AND l.reviewer_id=%s
                )
                {range_sql}
                ORDER BY sort_order LIMIT 1""",
            tuple(params),
        )
        row = cur.fetchone()
    return _row_to_item(row) if row else None


def get_item(
    item_id: int, bounds: tuple[int, int] | None = None
) -> dict | None:
    with appdb.connect() as c:
        cur = c.cursor()
        if bounds:
            cur.execute(
                f"SELECT {VISIBLE_COLS} FROM identity_items "
                "WHERE id=%s AND sort_order BETWEEN %s AND %s",
                (item_id, *bounds),
            )
        else:
            cur.execute(f"SELECT {VISIBLE_COLS} FROM identity_items WHERE id=%s", (item_id,))
        row = cur.fetchone()
    return _row_to_item(row) if row else None


def neighbours(
    sort_order: int, bounds: tuple[int, int] | None = None
) -> dict:
    with appdb.connect() as c:
        cur = c.cursor()
        if bounds:
            cur.execute(
                "SELECT id FROM identity_items "
                "WHERE sort_order<%s AND sort_order BETWEEN %s AND %s "
                "ORDER BY sort_order DESC LIMIT 1",
                (sort_order, *bounds),
            )
        else:
            cur.execute(
                "SELECT id FROM identity_items WHERE sort_order<%s "
                "ORDER BY sort_order DESC LIMIT 1",
                (sort_order,),
            )
        previous = cur.fetchone()
        if bounds:
            cur.execute(
                "SELECT id FROM identity_items "
                "WHERE sort_order>%s AND sort_order BETWEEN %s AND %s "
                "ORDER BY sort_order LIMIT 1",
                (sort_order, *bounds),
            )
        else:
            cur.execute(
                "SELECT id FROM identity_items WHERE sort_order>%s "
                "ORDER BY sort_order LIMIT 1",
                (sort_order,),
            )
        following = cur.fetchone()
    return {
        "prev_id": previous["id"] if previous else None,
        "next_id": following["id"] if following else None,
    }


def label_for(item_id: int, reviewer_id: int) -> dict | None:
    with appdb.connect() as c:
        cur = c.cursor()
        cur.execute(
            "SELECT label, severity, varies_by_issue, residual_reason, note, active_ms, "
            "reviewed_at FROM identity_labels WHERE item_id=%s AND reviewer_id=%s",
            (item_id, reviewer_id),
        )
        return cur.fetchone()


def review_payload(
    reviewer_id: int,
    item_id: int | None = None,
    bounds: tuple[int, int] | None = None,
) -> dict | None:
    item = get_item(item_id, bounds) if item_id else next_item_for(reviewer_id, bounds)
    if not item:
        return None
    contexts = [ocr_snippets(doc, item["surface"]) for doc in item["doc_ids"][:MAX_CONTEXT_DOCS]]
    return {
        "item": item,
        "contexts": contexts,
        "n_more_docs": max(0, len(item["doc_ids"]) - MAX_CONTEXT_DOCS),
        "existing": label_for(item["id"], reviewer_id),
        "nav": neighbours(item["sort_order"], bounds),
        "progress": reviewer_progress(reviewer_id, bounds),
        "assigned_range": bounds,
        "labels": LABELS,
        "severities": SEVERITIES,
        "residual_reasons": RESIDUAL_REASONS,
    }


def item_in_range(item_id: int, bounds: tuple[int, int] | None) -> bool:
    return get_item(item_id, bounds) is not None


def save_label(
    item_id: int,
    reviewer_id: int,
    label: str,
    severity: str | None,
    varies_by_issue: bool,
    residual_reason: str | None,
    note: str,
    active_ms: int,
) -> None:
    if label not in LABELS:
        raise ValueError(f"bad label {label!r}")
    if label != "wrong":
        severity = None
    elif severity not in SEVERITIES:
        raise ValueError("severity required when label is 'wrong'")
    if residual_reason and residual_reason not in RESIDUAL_REASONS:
        raise ValueError(f"bad residual_reason {residual_reason!r}")
    with appdb.connect() as c:
        cur = c.cursor()
        cur.execute(
            """INSERT INTO identity_labels
               (item_id, reviewer_id, label, severity, varies_by_issue, residual_reason, note,
                active_ms, first_opened_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())
               ON DUPLICATE KEY UPDATE
                 label=VALUES(label), severity=VALUES(severity),
                 varies_by_issue=VALUES(varies_by_issue), residual_reason=VALUES(residual_reason),
                 note=VALUES(note), active_ms=active_ms+VALUES(active_ms)""",
            (
                item_id,
                reviewer_id,
                label,
                severity,
                int(bool(varies_by_issue)),
                residual_reason or None,
                note or "",
                max(0, int(active_ms or 0)),
            ),
        )


def reviewer_progress(
    reviewer_id: int, bounds: tuple[int, int] | None = None
) -> dict:
    with appdb.connect() as c:
        cur = c.cursor()
        if bounds:
            cur.execute(
                "SELECT COUNT(*) AS n FROM identity_items WHERE sort_order BETWEEN %s AND %s",
                bounds,
            )
        else:
            cur.execute("SELECT COUNT(*) AS n FROM identity_items")
        total = int(cur.fetchone()["n"])
        range_sql = ""
        params: list[int] = [reviewer_id]
        if bounds:
            range_sql = " AND i.sort_order BETWEEN %s AND %s"
            params.extend(bounds)
        cur.execute(
            """SELECT COUNT(*) AS n, COALESCE(SUM(active_ms),0) AS ms,
                      SUM(label='correct') AS n_correct, SUM(label='wrong') AS n_wrong,
                      SUM(label='true_nil') AS n_true_nil,
                      SUM(label='indeterminate') AS n_indeterminate
               FROM identity_labels l
               JOIN identity_items i ON i.id=l.item_id
               WHERE l.reviewer_id=%s""" + range_sql,
            tuple(params),
        )
        row = cur.fetchone()
        cur.execute(
            """SELECT l.active_ms FROM identity_labels l
               JOIN identity_items i ON i.id=l.item_id
               WHERE l.reviewer_id=%s AND l.active_ms>0""" + range_sql
            + " ORDER BY l.active_ms",
            tuple(params),
        )
        sorted_ms = [int(value["active_ms"]) for value in cur.fetchall()]
    done = int(row["n"] or 0)
    median_ms = sorted_ms[len(sorted_ms) // 2] if sorted_ms else 0
    remaining = total - done
    return {
        "total": total,
        "done": done,
        "pct": round(100.0 * done / total, 1) if total else 0.0,
        "active_min": round(int(row["ms"] or 0) / 60000.0, 1),
        "median_sec": round(median_ms / 1000.0, 1),
        "eta_hours": round(remaining * median_ms / 3.6e6, 1) if median_ms else None,
        "n_correct": int(row["n_correct"] or 0),
        "n_wrong": int(row["n_wrong"] or 0),
        "n_true_nil": int(row["n_true_nil"] or 0),
        "n_indeterminate": int(row["n_indeterminate"] or 0),
    }


def all_reviewers_progress() -> list[dict]:
    with appdb.connect() as c:
        cur = c.cursor()
        cur.execute(
            """SELECT u.id, u.username, u.display_name, COUNT(l.id) AS done,
                      COALESCE(SUM(l.active_ms),0) AS ms
               FROM users u JOIN identity_labels l ON l.reviewer_id=u.id
               GROUP BY u.id, u.username, u.display_name ORDER BY done DESC"""
        )
        return cur.fetchall()


def admin_progress() -> dict:
    with appdb.connect() as c:
        cur = c.cursor()
        cur.execute(
            """SELECT i.gate_outcome, i.entity_type, COUNT(DISTINCT i.id) AS n_items,
                      COUNT(DISTINCT l.item_id) AS n_labelled
               FROM identity_items i
               LEFT JOIN identity_labels l ON l.item_id=i.id
               GROUP BY i.gate_outcome, i.entity_type
               ORDER BY i.gate_outcome, i.entity_type"""
        )
        by_stratum = cur.fetchall()
        cur.execute(
            """SELECT i.gate_outcome, l.label, COUNT(*) AS n
               FROM identity_labels l JOIN identity_items i ON i.id=l.item_id
               GROUP BY i.gate_outcome, l.label ORDER BY i.gate_outcome, l.label"""
        )
        confusion = cur.fetchall()
        cur.execute(
            """SELECT l.severity, COUNT(*) AS n
               FROM identity_labels l JOIN identity_items i ON i.id=l.item_id
               WHERE l.label='wrong' AND i.gate_outcome='accept'
               GROUP BY l.severity"""
        )
        severity = cur.fetchall()
    return {
        "by_stratum": by_stratum,
        "confusion": confusion,
        "severity_false_accepts": severity,
        "reviewers": all_reviewers_progress(),
    }


def export_rows() -> list[dict]:
    with appdb.connect() as c:
        cur = c.cursor()
        cur.execute(
            """SELECT i.*, l.reviewer_id, u.username AS reviewer, l.label, l.severity,
                      l.varies_by_issue, l.residual_reason, l.note, l.active_ms, l.reviewed_at
               FROM identity_items i
               LEFT JOIN identity_labels l ON l.item_id=i.id
               LEFT JOIN users u ON u.id=l.reviewer_id
               ORDER BY i.sort_order, l.reviewer_id"""
        )
        rows = cur.fetchall()
    output = []
    for row in rows:
        row = dict(row)
        row["doc_ids"] = json.loads(row.pop("doc_ids_json") or "[]")
        row["card_fields"] = json.loads(row.pop("card_fields_json") or "[]")
        for key, value in list(row.items()):
            if hasattr(value, "isoformat"):
                row[key] = value.isoformat()
            elif type(value).__name__ == "Decimal":
                row[key] = float(value)
        output.append(row)
    return output
