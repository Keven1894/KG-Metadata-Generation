"""E1 prospective offline trace: structured evidence -> independent checks -> decision.

This is a replay over the frozen analysis database, entity registry, authority HTTP cache,
and existing C-standard cards. It performs no HTTP requests, no LLM calls, no card generation,
and no writes to the rejection ledger.

Run from experiments/:
    python eval/e1_trace_structured_checks.py
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as expcfg  # noqa: E402
from common import authority, entity_registry  # noqa: E402
from tiers.cards import _gather_entities  # noqa: E402

TRACE_RUN_ID = "e1-trace-replay-2026-09-03"
FREEZE_DIR = expcfg.DATA_DIR / "freeze"
TRACE_PATH = FREEZE_DIR / "e1_structured_check_trace.jsonl"
ARTIFACT_DIR = Path(__file__).resolve().parents[1] / "artifacts"
MANIFEST_PATH = ARTIFACT_DIR / "e1_structured_check_manifest.json"
CARD_URI_RE = re.compile(
    r"https?://(?:id\.loc\.gov|www\.geonames\.org)[^\s()\"]+"
)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def normalize_uri(uri: str) -> str:
    return (uri or "").replace("http://", "https://").rstrip(".,;")


class OfflineAuthorityCache:
    """Index frozen HTTP responses by semantic query fields; never performs I/O beyond JSON."""

    def __init__(self, path: Path):
        self.path = path
        self.raw = json.loads(path.read_text(encoding="utf-8"))
        self.loc: dict[tuple[str, str, str, str], object] = {}
        self.geo: dict[tuple[str, str, str], object] = {}
        for url, payload in self.raw.items():
            parsed = urlparse(url)
            q = parse_qs(parsed.query)
            query = (q.get("q") or [""])[0]
            if "id.loc.gov" in parsed.netloc and parsed.path.endswith("/suggest2/"):
                parts = parsed.path.strip("/").split("/")
                dataset = parts[1] if len(parts) >= 3 else ""
                searchtype = (q.get("searchtype") or [""])[0]
                rdftype = (q.get("rdftype") or [""])[0]
                self.loc[(dataset, query, searchtype, rdftype)] = payload
            elif "geonames.org" in parsed.netloc and parsed.path.endswith("/searchJSON"):
                country = (q.get("country") or [""])[0]
                admin1 = (q.get("adminCode1") or [""])[0]
                self.geo[(query, country, admin1)] = payload

    @staticmethod
    def _payload_state(payload: object) -> str:
        if payload is None:
            return "cache_error"
        if not isinstance(payload, dict):
            return "cache_error"
        if payload.get("status") and "hits" not in payload and "geonames" not in payload:
            return "cache_error"
        return "ok"

    def loc_candidate(
        self,
        surface: str,
        dataset: str,
        rdftype: str,
        *,
        region: str | None = None,
        subject: bool = False,
    ) -> dict:
        query_states = []
        for searchtype in ("leftanchored", "keyword"):
            key = (dataset, surface, searchtype, rdftype)
            if key not in self.loc:
                query_states.append(f"{searchtype}:cache_missing")
                return {
                    "status": "cache_missing",
                    "query_states": query_states,
                    "candidate": None,
                }
            payload = self.loc[key]
            state = self._payload_state(payload)
            query_states.append(f"{searchtype}:{state}")
            if state != "ok":
                return {"status": state, "query_states": query_states, "candidate": None}
            candidates = []
            for hit in payload.get("hits") or []:
                uri = normalize_uri(hit.get("uri") or "")
                label = hit.get("aLabel") or hit.get("label") or ""
                if uri and label:
                    score = authority._match_score(
                        surface, label, region=region, subject=subject
                    )
                    candidates.append((score, uri, label))
            if candidates:
                score, uri, label = max(candidates, key=lambda x: x[0])
                return {
                    "status": "candidate",
                    "query_states": query_states,
                    "candidate": {
                        "uri": uri,
                        "label": label,
                        "score": score,
                        "source": (
                            "lcsh" if dataset == "subjects"
                            else "lc-geographic" if rdftype == "Geographic"
                            else "lcnaf"
                        ),
                    },
                }
        return {"status": "no_hit", "query_states": query_states, "candidate": None}

    def geonames_candidate(self, surface: str) -> dict:
        key = (surface, "US", "FL")
        if key not in self.geo:
            return {
                "status": "cache_missing",
                "query_states": ["geonames:cache_missing"],
                "candidate": None,
            }
        payload = self.geo[key]
        state = self._payload_state(payload)
        if state != "ok":
            return {
                "status": state,
                "query_states": [f"geonames:{state}"],
                "candidate": None,
            }
        rows = payload.get("geonames") or []
        if not rows:
            return {
                "status": "no_hit",
                "query_states": ["geonames:ok_empty"],
                "candidate": None,
            }
        row = max(rows, key=lambda r: authority._sim(surface, r.get("name", "")))
        gid = row.get("geonameId")
        if not gid:
            return {
                "status": "no_hit",
                "query_states": ["geonames:ok_unusable"],
                "candidate": None,
            }
        label = row.get("name") or surface
        return {
            "status": "candidate",
            "query_states": ["geonames:ok"],
            "candidate": {
                "uri": f"https://www.geonames.org/{gid}",
                "label": label,
                "score": authority._match_score(surface, label, region="Florida"),
                "source": "geonames",
            },
        }

    def candidate(self, surface: str, entity_type: str) -> dict:
        etype = (entity_type or "person").lower()
        if etype == "place":
            geo = self.geonames_candidate(surface)
            if geo["status"] == "candidate":
                return geo
            states = list(geo["query_states"])
            # A missing frozen GeoNames response is unknown, not evidence of no hit.
            if geo["status"] == "cache_missing":
                return geo
            for dataset in ("names", "subjects"):
                loc = self.loc_candidate(
                    surface, dataset, "Geographic", region="Florida"
                )
                states.extend(loc["query_states"])
                if loc["status"] == "candidate":
                    loc["query_states"] = states
                    return loc
                if loc["status"] in ("cache_missing", "cache_error"):
                    loc["query_states"] = states
                    return loc
            return {"status": "no_hit", "query_states": states, "candidate": None}
        if etype == "subject":
            return self.loc_candidate(
                surface, "subjects", "Topic", subject=True
            )
        rdftype = "CorporateName" if etype == "org" else "PersonalName"
        return self.loc_candidate(surface, "names", rdftype)


def threshold_for(entity_type: str) -> float:
    return {
        "person": authority.PERSON_MIN_SCORE,
        "org": authority.ORG_MIN_SCORE,
    }.get(entity_type, authority.AUTHORITY_MIN_SCORE)


def parse_lifespan(label: str) -> tuple[int | None, int | None]:
    match = authority._LIFESPAN.search((label or "").strip())
    if match:
        return int(match.group(1)), int(match.group(2)) if match.group(2) else None
    born = authority._BORN_ONLY.search(label or "")
    return (int(born.group(1)), None) if born else (None, None)


def temporal_check(
    entity_type: str,
    score: float,
    label: str,
    year_min: int,
    year_max: int,
) -> dict:
    if entity_type != "person":
        return {
            "state": "not_applicable",
            "birth_year": None,
            "death_year": None,
            "reason": "",
        }
    if score >= 0.9:
        return {
            "state": "skipped_high_score",
            "birth_year": None,
            "death_year": None,
            "reason": "deployed code skips lifespan check for score >= 0.9",
        }
    born, died = parse_lifespan(label)
    if born is None and died is None:
        return {
            "state": "unknown",
            "birth_year": None,
            "death_year": None,
            "reason": "no parsable lifespan in authority label",
        }
    if died is not None and died < year_min:
        return {
            "state": "fail",
            "birth_year": born,
            "death_year": died,
            "reason": f"lifespan: died {died} < activity window start {year_min}",
        }
    if born is not None and born > year_max:
        return {
            "state": "fail",
            "birth_year": born,
            "death_year": died,
            "reason": f"lifespan: born {born} > activity window end {year_max}",
        }
    return {
        "state": "pass",
        "birth_year": born,
        "death_year": died,
        "reason": "",
    }


def card_uri_locations(doc_id: str, uri: str) -> tuple[int, list[str]]:
    path = expcfg.CARDS_DIR / "C-standard" / f"{doc_id}.json"
    if not path.exists() or not uri:
        return 0, []
    fields = json.loads(path.read_text(encoding="utf-8")).get("fields") or {}
    n = 0
    found = []
    for field, value in fields.items():
        text = json.dumps(value, ensure_ascii=False)
        count = text.count(uri) + text.count(uri.replace("https://", "http://"))
        if count:
            n += count
            found.append(field)
    return n, sorted(found)


def make_row(
    doc_id: str,
    entity_rank: int,
    entity: dict,
    cache: OfflineAuthorityCache,
    commit: str,
    cache_hash: str,
    registry_hash: str,
) -> dict:
    surface = entity.get("surface") or ""
    entity_type = entity.get("etype") or "person"
    registry_key = entity_registry.norm_key(surface)
    registry_row = entity_registry.lookup(surface) or {}
    years = registry_row.get("years") or []
    g0_found = bool(registry_row)
    year_source = "entity_observed_years" if years else "corpus_fallback"
    year_min = min(years) if years else entity_registry.CORPUS_YEAR_MIN
    year_max = max(years) if years else entity_registry.CORPUS_YEAR_MAX

    lookup = cache.candidate(surface, entity_type)
    candidate = lookup.get("candidate")
    base = {
        "trace_run_id": TRACE_RUN_ID,
        "doc_id": doc_id,
        "entity_rank": entity_rank,
        "surface": surface,
        "normalized_surface": registry_key,
        "entity_type": entity_type,
        "g0_registry_key": registry_key if g0_found else "",
        "g0_found": g0_found,
        "g0_doc_ids": registry_row.get("doc_ids") or [],
        "g0_year_min": year_min,
        "g0_year_max": year_max,
        "g0_year_source": year_source,
        "g0_local_context": entity_registry.local_context(surface),
        "cache_query_states": lookup.get("query_states") or [],
        "code_commit": commit,
        "cache_sha256": cache_hash,
        "registry_sha256": registry_hash,
    }
    if not candidate:
        outcome = (
            "no_hit" if lookup["status"] == "no_hit"
            else "cache_error"
        )
        return {
            **base,
            "candidate_uri": "",
            "candidate_label": "",
            "authority_source": "",
            "candidate_score": None,
            "score_threshold": threshold_for(entity_type),
            "score_state": "not_applicable",
            "candidate_birth_year": None,
            "candidate_death_year": None,
            "temporal_state": "not_applicable",
            "gate_outcome": outcome,
            "decision_reason": lookup["status"],
            "card_embedded": False,
            "card_uri_occurrences": 0,
            "card_fields": [],
        }

    score = candidate["score"]
    threshold = threshold_for(entity_type)
    score_state = "pass" if score >= threshold else "fail"
    temporal = temporal_check(
        entity_type, score, candidate["label"], year_min, year_max
    )
    if temporal["state"] == "fail":
        outcome = "temporal_reject"
        reason = temporal["reason"]
    elif score_state == "fail":
        outcome = "score_reject"
        reason = f"score {score} < threshold {threshold}"
    else:
        outcome = "accept"
        reason = ""
    occurrences, fields = card_uri_locations(
        doc_id, normalize_uri(candidate["uri"])
    )
    return {
        **base,
        "candidate_uri": normalize_uri(candidate["uri"]),
        "candidate_label": candidate["label"],
        "authority_source": candidate["source"],
        "candidate_score": score,
        "score_threshold": threshold,
        "score_state": score_state,
        "candidate_birth_year": temporal["birth_year"],
        "candidate_death_year": temporal["death_year"],
        "temporal_state": temporal["state"],
        "gate_outcome": outcome,
        "decision_reason": reason,
        "card_embedded": occurrences > 0,
        "card_uri_occurrences": occurrences,
        "card_fields": fields,
    }


def card_occurrences(doc_ids: list[str]) -> list[tuple[str, str]]:
    out = []
    for doc_id in doc_ids:
        path = expcfg.CARDS_DIR / "C-standard" / f"{doc_id}.json"
        fields = json.loads(path.read_text(encoding="utf-8")).get("fields") or {}
        for uri in CARD_URI_RE.findall(json.dumps(fields, ensure_ascii=False)):
            out.append((doc_id, normalize_uri(uri)))
    return out


def summarize(rows: list[dict], source_doc_ids: list[str]) -> dict:
    outcomes = Counter(r["gate_outcome"] for r in rows)
    types = Counter(r["entity_type"] for r in rows)
    g0 = Counter(
        "found" if r["g0_found"] else "not_found"
        for r in rows
    )
    year_sources = Counter(r["g0_year_source"] for r in rows)
    score_states = Counter(r["score_state"] for r in rows)
    temporal_states = Counter(r["temporal_state"] for r in rows)
    incremental_temporal = [
        r for r in rows
        if r["gate_outcome"] == "temporal_reject" and r["score_state"] == "pass"
    ]
    score_temporal_overlap = [
        r for r in rows
        if r["temporal_state"] == "fail" and r["score_state"] == "fail"
    ]
    accepts = [r for r in rows if r["gate_outcome"] == "accept"]
    accepted_doc_uris = {
        (r["doc_id"], r["candidate_uri"]) for r in accepts
    }
    card_uris = card_occurrences(source_doc_ids)
    covered_card_uris = [
        pair for pair in card_uris if pair in accepted_doc_uris
    ]
    uncovered_card_uris = [
        pair for pair in card_uris if pair not in accepted_doc_uris
    ]
    row_doc_ids = {r["doc_id"] for r in rows}
    temporal_pairs = {
        (r["surface"], r["candidate_uri"])
        for r in rows if r["gate_outcome"] == "temporal_reject"
    }
    by_type_outcome: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        by_type_outcome[row["entity_type"]][row["gate_outcome"]] += 1
    return {
        "n_rows": len(rows),
        "n_source_issues": len(source_doc_ids),
        "n_issues_with_selected_entities": len(row_doc_ids),
        "issues_with_no_selected_entities": sorted(set(source_doc_ids) - row_doc_ids),
        "by_entity_type": dict(sorted(types.items())),
        "g0_registry": dict(sorted(g0.items())),
        "g0_year_source": dict(sorted(year_sources.items())),
        "score_state": dict(sorted(score_states.items())),
        "temporal_state": dict(sorted(temporal_states.items())),
        "gate_outcome": dict(sorted(outcomes.items())),
        "by_entity_type_and_outcome": {
            entity_type: dict(sorted(counts.items()))
            for entity_type, counts in sorted(by_type_outcome.items())
        },
        "incremental_temporal_catches": len(incremental_temporal),
        "score_and_temporal_fail_overlap": len(score_temporal_overlap),
        "temporal_reject_issue_entity_rows": outcomes["temporal_reject"],
        "temporal_reject_unique_surface_uri_pairs": len(temporal_pairs),
        "accepted_candidates": len(accepts),
        "accepted_candidates_embedded_in_card": sum(
            r["card_embedded"] for r in accepts
        ),
        "accepted_unique_doc_uri_pairs": len(accepted_doc_uris),
        "existing_card_uri_occurrences": len(card_uris),
        "existing_card_uri_occurrences_covered_by_replay_accepts": len(
            covered_card_uris
        ),
        "existing_card_uri_occurrences_not_covered_by_replay_accepts": len(
            uncovered_card_uris
        ),
        "uncovered_card_doc_uri_sample": [
            {"doc_id": doc_id, "uri": uri}
            for doc_id, uri in uncovered_card_uris[:20]
        ],
        "note": (
            "Rows are prospective offline replay decisions over frozen cache, not the "
            "original July execution log. Card embedding is downstream of gate acceptance."
        ),
    }


def main() -> None:
    FREEZE_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = expcfg.DATA_DIR / "authority_cache.json"
    registry_path = expcfg.DATA_DIR / "entity_registry.json"
    cache = OfflineAuthorityCache(cache_path)
    commit = git_commit()
    cache_hash = sha256(cache_path)
    registry_hash = sha256(registry_path)
    doc_ids = sorted(p.stem for p in (expcfg.CARDS_DIR / "C-standard").glob("*.json"))

    rows = []
    for doc_id in doc_ids:
        for rank, entity in enumerate(_gather_entities(doc_id), start=1):
            rows.append(
                make_row(
                    doc_id,
                    rank,
                    entity,
                    cache,
                    commit,
                    cache_hash,
                    registry_hash,
                )
            )

    with TRACE_PATH.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    manifest = {
        "trace_run_id": TRACE_RUN_ID,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "trace_path": str(TRACE_PATH),
        "trace_sha256": sha256(TRACE_PATH),
        "code_commit": commit,
        "cache_sha256": cache_hash,
        "registry_sha256": registry_hash,
        "offline_only": True,
        "llm_calls": 0,
        "card_generation": False,
        "ledger_writes": False,
        "summary": summarize(rows, doc_ids),
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
