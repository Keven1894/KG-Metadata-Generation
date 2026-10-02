"""Authority linking for extracted entities (the C-standard / FAIR F+I payoff).

Resolves a free-text entity (person / organization / place / subject) to a real, persistent
authority record and returns its **URI / PID** plus the **normalized authorized label**. This is the
concrete Findable+Interoperable contribution of the `C-standard` tier (doc 26): bare LLM cards carry
free-text names; here we attach globally unique identifiers from controlled vocabularies.

Sources (all public-domain, no key required except GeoNames):
  - id.loc.gov Suggest2  -> LCNAF (PersonalName / CorporateName / Geographic), LCSH (Topic)
  - GeoNames searchJSON  -> places (better small-place coverage, e.g. "Little River"); optional,
                            needs a free GEONAMES_USERNAME; falls back to LC Geographic without it.

Design notes:
  - Stdlib-only HTTP (urllib) so this runs anywhere with no extra deps.
  - On-disk JSON cache (reproducible + polite to the APIs); identical queries are never re-fetched.
  - Conservative matching: we take the top hit and record a fuzzy score so the caller / scorer can
    threshold "link precision" later. We never fabricate a URI.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path

import config as expcfg
from common import entity_registry

# ── config ────────────────────────────────────────────────────────────────────
_CACHE_PATH = expcfg.DATA_DIR / "authority_cache.json"
_USER_AGENT = "jcdl26-miamilife-curation/0.1 (<institution> <lab>; research)"
_TIMEOUT = 12
_MIN_INTERVAL = 0.34  # be polite: ~3 req/s ceiling per host
_ENV_PATH = Path(os.getenv("<internal>_ENV_PATH", r"<path>"))


def _geonames_username() -> str:
    """GeoNames free web service needs **username only** (no password / API key in requests).

    Password in .env is for logging into geonames.org; API calls use ?username=...
    """
    for key in ("GEONAMES_USERNAME", "geonames-user", "GEONAMES_USER"):
        v = os.getenv(key, "").strip()
        if v:
            return v
    if _ENV_PATH.exists():
        try:
            from dotenv import dotenv_values

            vals = dotenv_values(_ENV_PATH)
            for key in ("GEONAMES_USERNAME", "geonames-user", "GEONAMES_USER"):
                v = (vals.get(key) or "").strip()
                if v:
                    return v
        except ImportError:
            pass
    return ""

# Minimum match score to accept a link for C-standard cards (below → free-text only).
AUTHORITY_MIN_SCORE = 0.5

# G0-GROUNDING (docs/rag-design/00 §3/§6): a verified PID is NOT a verified identity.
# Hyperlocal people are mostly absent from LCNAF, so the top Suggest2 hit is often a DIFFERENT
# real person (OCR "W. E. Ellis" -> "Ellis, Erastus W. H., 1815-1876"). Person links therefore:
#   (a) need a much higher score than places/subjects (prefer no-link over weak-link), and
#   (b) must pass a LIFESPAN sanity check against the entity's local activity window (G0
#       registry span, bounded by the corpus publication window 1927-1949).
# Every rejection is logged to the ledger (claim C2's audit trail).
PERSON_MIN_SCORE = 0.75
# Orgs have the same wrong-entity failure with no lifespan signal to catch it ("Miami Life" ->
# "Miami Life Center" (a Pilates studio); an Ocala bank -> a Nebraska bank). Fuzzy corporate
# matches are rejected outright; only near-exact names may link. Exact-name/different-entity
# collisions remain possible for orgs — that residual is the human gate's job (documented in 00 §3).
ORG_MIN_SCORE = 0.85
REJECTION_LEDGER = expcfg.DATA_DIR / "authority_rejections.jsonl"

# "Surname, Given, 1815-1876" / "…, b. 1882" / "…, 1882-" style lifespans in authority labels.
_LIFESPAN = re.compile(r"(?:\b|, ?)(1[5-9]\d\d)\s*[-–]\s*(1[5-9]\d\d|20\d\d)?\s*$")
_BORN_ONLY = re.compile(r"\bb\.?\s*(1[5-9]\d\d)\b")

_ledger_seen: set[tuple] = set()


def _log_rejection(surface: str, etype: str, uri: str, label: str, score: float, reason: str) -> None:
    key = (surface.lower(), uri, reason)
    with _state_lock:
        if key in _ledger_seen:
            return
        _ledger_seen.add(key)
        rec = {
            "surface": surface, "etype": etype, "uri": uri, "label": label,
            "score": score, "reason": reason,
            "local_context": entity_registry.local_context(surface),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        with REJECTION_LEDGER.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _lifespan_conflict(label: str, surface: str) -> str:
    """Return a rejection reason if the authority label's lifespan cannot overlap the entity's
    local activity window, else ''."""
    m = _LIFESPAN.search(label.strip())
    born = died = None
    if m:
        born = int(m.group(1))
        died = int(m.group(2)) if m.group(2) else None
    else:
        mb = _BORN_ONLY.search(label)
        if mb:
            born = int(mb.group(1))
    if born is None and died is None:
        return ""
    lo, hi = entity_registry.activity_window(surface)
    if died is not None and died < lo:
        return f"lifespan: died {died} < activity window start {lo}"
    if born is not None and born > hi:
        return f"lifespan: born {born} > activity window end {hi}"
    return ""

# Map our entity types -> (loc dataset, suggest2 rdftype). "place"/"subject" handled specially.
_LOC_ROUTING: dict[str, tuple[str, str]] = {
    "person":   ("names", "PersonalName"),
    "people":   ("names", "PersonalName"),
    "org":      ("names", "CorporateName"),
    "organization": ("names", "CorporateName"),
    "business": ("names", "CorporateName"),
    "corporate": ("names", "CorporateName"),
}


@dataclass
class AuthorityMatch:
    surface: str                 # the input free-text string
    etype: str                   # normalized entity type
    source: str                  # "lcnaf" | "lcsh" | "geonames" | "lc-geographic"
    uri: str                     # the persistent identifier (resolvable URI)
    label: str                   # authorized / normalized label
    score: float                 # fuzzy similarity surface<->label in [0,1]
    extra: dict | None = None    # source-specific (country, feature class, ...)

    def to_dict(self) -> dict:
        return asdict(self)


# ── cache + http ────────────────────────────────────────────────────────────────
# Guards _cache and _last_call: card generation now runs on a thread pool
# (common/parallel.py, docs/rag-design/05 §6e) and unlocked read-modify-write-save on a shared
# dict corrupts it across threads ("dictionary changed size during iteration" from json.dumps
# racing a concurrent write) — every access to either goes through this lock.
_state_lock = threading.Lock()
_cache: dict[str, dict] | None = None
_last_call = {"t": 0.0}


def _load_cache() -> dict[str, dict]:
    global _cache
    if _cache is None:
        if _CACHE_PATH.exists():
            try:
                _cache = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                _cache = {}
        else:
            _cache = {}
    return _cache


def _save_cache() -> None:
    if _cache is not None:
        _CACHE_PATH.write_text(json.dumps(_cache, ensure_ascii=False, indent=1), encoding="utf-8")


def _throttle() -> None:
    with _state_lock:
        dt = time.time() - _last_call["t"]
        wait = _MIN_INTERVAL - dt if dt < _MIN_INTERVAL else 0.0
        _last_call["t"] = max(time.time(), _last_call["t"] + _MIN_INTERVAL)
    if wait > 0:
        time.sleep(wait)


def _get_json(url: str) -> dict | list | None:
    with _state_lock:
        cache = _load_cache()
        if url in cache:
            return cache[url]
    _throttle()
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception:  # noqa: BLE001 — network/json errors are non-fatal; treat as "no hit"
        data = None
    with _state_lock:
        cache[url] = data
        _save_cache()
    return data


_PUNCT = str.maketrans({c: " " for c in ",.;:()[]{}'\"/-"})


def _norm(s: str) -> str:
    return " ".join(s.lower().translate(_PUNCT).split())


def _tokens(s: str) -> set[str]:
    # drop dates and 1-char noise so "Roosevelt, Franklin D. ... 1882-1945" tokenizes cleanly
    return {t for t in _norm(s).split() if len(t) > 1 and not t.isdigit()}


def _sim(a: str, b: str) -> float:
    """Token-aware similarity robust to inverted name order and trailing authority qualifiers.

    Blends (i) how much of the surface's tokens are present in the label (containment) with
    (ii) a plain sequence ratio, so 'Franklin D. Roosevelt' vs 'Roosevelt, Franklin D., 1882-1945'
    scores high, while 'Miami' vs 'Art Deco Historic District (Miami Beach, Fla.)' stays low.
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return round(SequenceMatcher(None, _norm(a), _norm(b)).ratio(), 3)
    containment = len(ta & tb) / len(ta)            # fraction of surface tokens found in label
    jaccard = len(ta & tb) / len(ta | tb)           # penalizes label carrying many extra tokens
    seq = SequenceMatcher(None, _norm(a), _norm(b)).ratio()
    return round(0.6 * containment + 0.25 * jaccard + 0.15 * seq, 3)


def _match_score(query: str, label: str, *, region: str | None = None, subject: bool = False) -> float:
    """Confidence-calibrated match score used for ranking AND reporting.

    - exact normalized equality -> 1.0
    - penalize labels carrying extra *content* tokens (catches LCSH precoordinated headings like
      'Buddhism and politics' for 'politics') — strongest for subjects
    - boost places whose label carries the expected region (e.g. 'Fla.') to fight small-place
      cross-state collisions (e.g. 'Little River (Me.)' vs the Miami district)
    """
    nq, nl = _norm(query), _norm(label)
    if nq == nl:
        return 1.0
    score = _sim(query, label)
    qt, lt = _tokens(query), _tokens(label)
    extra = lt - qt
    if extra:
        weight = 0.40 if subject else 0.20
        score -= weight * (len(extra) / max(len(lt), 1))
    if region:
        rt = _tokens(region) | {"fla", "fl"}
        if lt & rt:
            score += 0.15
    return round(max(0.0, min(1.0, score)), 3)


# ── source queries ──────────────────────────────────────────────────────────────
def _loc_suggest2(
    query: str, dataset: str, rdftype: str | None,
    *, region: str | None = None, subject: bool = False,
) -> tuple[str, str, float] | None:
    """Return (uri, authorized_label, score) of the BEST id.loc.gov Suggest2 hit, ranked by a
    confidence-calibrated score; tries left-anchored first then keyword. None if nothing found."""
    candidates: list[tuple[str, str]] = []
    for searchtype in ("leftanchored", "keyword"):
        params = {"q": query, "count": "10", "searchtype": searchtype}
        if rdftype:
            params["rdftype"] = rdftype
        url = f"https://id.loc.gov/authorities/{dataset}/suggest2/?{urllib.parse.urlencode(params)}"
        data = _get_json(url)
        if isinstance(data, dict):
            for h in (data.get("hits") or []):
                uri = (h.get("uri") or "").replace("http://", "https://")
                label = h.get("aLabel") or h.get("label") or ""
                if uri and label:
                    candidates.append((uri, label))
        if candidates:
            break  # prefer left-anchored matches when present
    if not candidates:
        return None
    uri, label = max(candidates, key=lambda c: _match_score(query, c[1], region=region, subject=subject))
    return uri, label, _match_score(query, label, region=region, subject=subject)


def _geonames(query: str, *, admin1: str = "FL", country: str = "US") -> tuple[str, str, dict] | None:
    """Return (uri, name, extra) from GeoNames, or None. Requires GEONAMES_USERNAME.

    Biased to Florida/US by default (the corpus is hyperlocal Miami) so small places like
    'Little River' resolve to the Dade County locality, not a same-named place out of state.
    """
    if not _geonames_username():
        return None
    params = {
        "q": query, "maxRows": "5", "username": _geonames_username(), "style": "MEDIUM",
        "orderby": "relevance",
    }
    if country:
        params["country"] = country
    if admin1:
        params["adminCode1"] = admin1
    url = f"http://api.geonames.org/searchJSON?{urllib.parse.urlencode(params)}"
    data = _get_json(url)
    if not isinstance(data, dict):
        return None
    if err := data.get("status"):
        msg = err.get("message", "")
        # Common: value 10 = "user does not exist" — register at geonames.org (webservice enable).
        if "does not exist" in msg.lower() or "not enabled" in msg.lower():
            print(f"[authority] GeoNames: {msg} (username={_geonames_username()!r})")
        return None
    rows = data.get("geonames") or []
    if not rows:
        return None
    g = max(rows, key=lambda r: _sim(query, r.get("name", "")))
    gid = g.get("geonameId")
    if not gid:
        return None
    extra = {
        "country": g.get("countryName", ""),
        "admin1": g.get("adminName1", ""),
        "feature": g.get("fcodeName", ""),
        "lat": g.get("lat", ""),
        "lng": g.get("lng", ""),
    }
    return f"https://www.geonames.org/{gid}", g.get("name", query), extra


# ── public API ────────────────────────────────────────────────────────────────
def link_entity(surface: str, etype: str, *, region: str = "Florida") -> AuthorityMatch | None:
    """Resolve one entity to an authority record. Returns the best match or None.

    `region` biases place disambiguation toward the corpus locality (Miami / Florida).
    """
    surface = (surface or "").strip()
    if len(surface) < 2:
        return None
    et = (etype or "person").lower().strip()

    # places --------------------------------------------------------------------
    if et in ("place", "location", "geographic"):
        geo = _geonames(surface)
        if geo:
            uri, label, extra = geo
            score = _match_score(surface, label, region=region)
            return AuthorityMatch(surface, "place", "geonames", uri, label, score, extra)
        # fallback: LC geographic (names dataset Geographic, then LCSH geographic), region-biased
        for dataset, rdftype, src in (("names", "Geographic", "lc-geographic"),
                                      ("subjects", "Geographic", "lcsh")):
            hit = _loc_suggest2(surface, dataset, rdftype, region=region)
            if hit:
                uri, label, score = hit
                return AuthorityMatch(surface, "place", src, uri, label, score)
        return None

    # subjects / topics ----------------------------------------------------------
    if et in ("subject", "topic", "topics", "tag"):
        hit = _loc_suggest2(surface, "subjects", "Topic", subject=True)
        if hit:
            uri, label, score = hit
            return AuthorityMatch(surface, "subject", "lcsh", uri, label, score)
        return None

    # people / orgs (LCNAF) -------------------------------------------------------
    dataset, rdftype = _LOC_ROUTING.get(et, ("names", "PersonalName"))
    hit = _loc_suggest2(surface, dataset, rdftype)
    if hit:
        uri, label, score = hit
        norm_type = "org" if rdftype == "CorporateName" else "person"
        if norm_type == "person" and score < 0.9:
            # Lifespan gate applies only to non-exact name matches: a NEAR-EXACT name whose
            # bearer died earlier is a plausible historical mention (e.g. FDR in a 1949 issue);
            # a fuzzy match with an impossible lifespan is a wrong-person link (the Ellis case).
            reason = _lifespan_conflict(label, surface)
            if reason:
                _log_rejection(surface, norm_type, uri, label, score, reason)
                return None
        return AuthorityMatch(surface, norm_type, "lcnaf", uri, label, score)
    return None


def link_many(
    entities: list[dict],
    *,
    region: str = "Florida",
    min_score: float = AUTHORITY_MIN_SCORE,
) -> list[dict]:
    """Link a list of {surface, etype} dicts; attach 'authority' only when the match clears the
    per-type threshold (persons use PERSON_MIN_SCORE — see G0-grounding note above)."""
    out = []
    for e in entities:
        surface = e.get("surface") or e.get("normalized") or ""
        m = link_entity(surface, e.get("etype") or "person", region=region)
        row = dict(e)
        threshold = ({"person": PERSON_MIN_SCORE, "org": ORG_MIN_SCORE}.get(m.etype, min_score)
                     if m else min_score)
        if m and m.score >= threshold:
            row["authority"] = m.to_dict()
        else:
            if m:  # found but below threshold — ledger it (C2 audit trail)
                _log_rejection(surface, m.etype, m.uri, m.label, m.score,
                               f"score {m.score} < threshold {threshold}")
            row["authority"] = None
        out.append(row)
    return out


def coverage(linked: list[dict], threshold: float = 0.0) -> dict:
    """Summarize authority-link rate / PID coverage for a validation harness."""
    n = len(linked)
    hits = [r for r in linked if r.get("authority")]
    strong = [r for r in hits if (r["authority"]["score"] >= threshold)]
    by_type: dict[str, list[int]] = {}
    for r in linked:
        t = (r.get("etype") or "?").lower()
        by_type.setdefault(t, [0, 0])
        by_type[t][1] += 1
        if r.get("authority"):
            by_type[t][0] += 1
    return {
        "n": n,
        "linked": len(hits),
        "link_rate": round(len(hits) / n, 3) if n else 0.0,
        "strong_at_threshold": len(strong),
        "by_type": {t: {"linked": a, "total": b, "rate": round(a / b, 3) if b else 0.0}
                    for t, (a, b) in sorted(by_type.items())},
    }


if __name__ == "__main__":
    import sys

    samples = [
        ("Florida East Coast Railway", "org"),
        ("Miami", "place"),
        ("Little River", "place"),
        ("Franklin D. Roosevelt", "person"),
        ("Prohibition", "subject"),
    ]
    if len(sys.argv) > 2:
        samples = [(sys.argv[1], sys.argv[2])]
    print(f"GEONAMES_USERNAME: {_geonames_username() or '(not set)'}")
    print("(GeoNames API uses username only — password is for geonames.org login, not API calls.)\n")
    for surf, et in samples:
        m = link_entity(surf, et)
        if m:
            print(f"[{et:8}] {surf!r:40} -> {m.label!r}  ({m.source}, score={m.score})\n"
                  f"{'':12}{m.uri}")
        else:
            print(f"[{et:8}] {surf!r:40} -> (no authority match)")
