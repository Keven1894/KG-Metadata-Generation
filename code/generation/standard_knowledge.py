"""Lightweight cataloging-standard knowledge pack for the `C-standard` tier (doc 26, item 1).

The `C-standard` tier feeds the LLM two things the bare `C-simple` tier lacks:
  (i)  this module — externalized **cataloging-standard knowledge**: how a professional describes a
       historical-newspaper *issue* under the NDNP / MODS / Dublin Core conventions; and
  (ii) `common.authority` — **authority-linked entities** (real LCNAF/LCSH/VIAF/GeoNames URIs).

This is the "straight-A student" knowledge: the rules and field semantics a trained cataloger knows,
distilled from the LoC NDNP guidance (METS+MODS+MARC21/CONSER) into a compact prompt block. Kept
deliberately lightweight (prompt knowledge, not a full graph) per the 2026-06-24 decision; a richer
AgentLoom-style knowledge/skill/rule graph is a later upgrade.

Sources distilled: LoC NDNP "Toward a Metadata Standard for Digitized Historical Newspapers";
NDNP 2026 Digital Asset Metadata Elements; DCMI Dublin Core; CONSER newspaper-cataloging practice.
"""

from __future__ import annotations

import json

# Per-field cataloging guidance (newspaper-issue level), aligned to our DC schema in fair/schema.py.
_FIELD_RULES: dict[str, str] = {
    "title": "Use the masthead title plus the issue's date/volume (e.g. 'Miami Life, May 27, 1933'). "
             "Transcribe the title as it appears; do not modernize spelling.",
    "creator": "The creator is the SINGLE top-billed editor/publisher named in the masthead (the "
               "person MODS-style cataloging would record as chiefly responsible) — not article "
               "bylines, and not the rest of the masthead staff box. Do NOT list the business "
               "manager, advertising manager, circulation manager, or other staff as creators; if "
               "they are notable, they belong in 'contributor', not here. Record the name AS THE "
               "MASTHEAD/OCR SHOWS IT; if an authority link is given for that person, append its "
               "URI — never swap in the authority's label.",
    "publisher": "The publishing body/company (e.g. 'Miami Life Co.'). Distinct from the editor as a "
                 "person; if only a person is known, record them but flag the body/person ambiguity.",
    "date": "Issue date in ISO 8601 (YYYY-MM-DD). Encode uncertainty explicitly (YYYY or YYYY-MM) "
            "rather than guessing a day; never invent precision the evidence does not support.",
    "type": "DCMI Type = 'Text'; genre = newspaper issue. Keep constant across issues.",
    "language": "ISO 639 code ('eng' for Miami Life). One code unless multilingual content is evident.",
    "subject": "Write each subject as a SPECIFIC, discriminating LCSH-style heading, compounded "
               "with subtopic/place where that sharpens it ('Miami (Fla.)—Politics and "
               "government', 'Transportation—Bus service—Miami (Fla.)', 'Grand juries', "
               "'Judicial ethics') — never a bare generic single word ('Politics', 'Business', "
               "'Tourism', 'Crime') when a more specific heading already covers that ground; a "
               "generic term earns its own entry ONLY if nothing more specific captures that "
               "content. A long list padded with generic topics is a WORSE record than a shorter, "
               "sharper one, even if every term is technically true — a cataloger is graded on "
               "the list's discriminating power, not its length. Where a subject you list happens "
               "to have an authority link provided, append its URI; never restrict the subject "
               "list to linked topics, and never drop a specific subject because it lacks a link. "
               "Subjects describe the issue's content, not individual ads.",
    "description": "A neutral abstract of the issue's principal contents (lead stories, recurring "
                   "columns, notable coverage). Summarize; do not editorialize or quote sensational "
                   "headlines as fact.",
    "coverage": "Spatial coverage = authority-linked places (GeoNames/LCSH geographic) the issue is "
                "about (esp. Miami / Dade County localities); temporal coverage = the period covered.",
    "contributor": "Named people/organizations featured or discussed, chosen by their prominence "
                   "in the issue — NOT by whether they carry an authority link. Append the URI "
                   "when one is provided. These are subjects-of/contributors-to coverage, not the "
                   "creator.",
    "relation": "Volume/series membership and links to preceding/succeeding titles or related issues.",
    "source": "Holding source and digitization provenance (<institution> Libraries dPanther).",
    "rights": "Rights/access statement appropriate to a digitized historical newspaper.",
    "identifier": "Stable identifier / dPanther URL for the issue.",
    "format": "Digital format of the described resource (OCR text / digitized page images).",
}

_PRINCIPLES = [
    "Describe the ISSUE as a whole (issue-level cataloging), not each article.",
    "Faithfulness over completeness: record only what the evidence supports; leave a field empty "
    "rather than fabricate. OCR is noisy — do not 'repair' names into plausible but unverified forms.",
    "Names come from the ISSUE, identifiers come from the AUTHORITY: keep every entity's "
    "OCR/masthead surface form as its name, and attach the provided authority URI/PID beside it "
    "when one is given (this is the Findable + Interoperable / FAIR payoff). NEVER replace a name "
    "with the authority's label — authority labels can carry another person's dates/expansions.",
    "Authority links are OPTIONAL DECORATIONS on content, never a filter on it: decide WHAT to "
    "describe from the issue alone (as if no links existed), THEN attach URIs to whatever happens "
    "to be linked. A record that only mentions linked entities/topics is an impoverished record.",
    "Specificity beats volume in every list field (subject/contributor/coverage): a shorter list "
    "of sharp, discriminating entries is a BETTER record than a longer list padded with generic "
    "restatements of the same ground. Before adding an entry, check it is not a broader/vaguer "
    "duplicate of one already listed.",
    "Normalize dates to ISO 8601 and express uncertainty rather than false precision.",
    "Keep controlled fields (type, language) consistent across the run.",
]


def cataloging_standard_block() -> str:
    """Compact, model-readable cataloging-standard knowledge for the C-standard prompt."""
    lines = [
        "CATALOGING STANDARD (how a professional describes a historical-newspaper issue — follow it):",
        "Conventions: NDNP / MODS / MARC21 (CONSER) issue-level description, mapped to Dublin Core.",
        "",
        "Principles:",
    ]
    lines += [f"  {i+1}. {p}" for i, p in enumerate(_PRINCIPLES)]
    lines += ["", "Per-field rules:"]
    lines += [f"  - {k}: {v}" for k, v in _FIELD_RULES.items()]
    return "\n".join(lines)


def authority_evidence_block(linked_entities: list[dict]) -> str:
    """Render authority-linked entities into the evidence: OCR surface form stays the name; the
    authority contributes ONLY the URI (label shown for verification, never for substitution).

    `linked_entities` items look like {surface, etype, authority: {label, uri, source, score}|None}.
    """
    have = [e for e in linked_entities if e.get("authority")]
    if not have:
        return "AUTHORITY-LINKED ENTITIES: (none resolved)"
    by_type: dict[str, list[str]] = {}
    for e in have:
        a = e["authority"]
        by_type.setdefault(a.get("etype") or e.get("etype") or "entity", []).append(
            f"{e.get('surface','')!r} — URI: {a.get('uri','')}  "
            f"[{a.get('source','')}; authority label for verification only: {a.get('label','')!r}]"
        )
    lines = ["AUTHORITY-LINKED ENTITIES (write the entity's NAME exactly as the issue shows it — "
             "the first quoted form below; append the URI beside it in the matching DC field: "
             "creator/contributor for people/orgs, subject for topics, coverage for places. Do NOT "
             "copy the bracketed authority label or its dates into the card. This list is NOT a "
             "content filter: describe the issue as fully as the evidence supports, then decorate "
             "whatever happens to appear below with its URI):"]
    for t, rows in sorted(by_type.items()):
        lines.append(f"  {t}:")
        lines += [f"    - {r}" for r in rows]
    return "\n".join(lines)


if __name__ == "__main__":
    print(cataloging_standard_block())
    print()
    demo = [
        {"surface": "Florida East Coast Railway", "etype": "org",
         "authority": {"label": "Florida East Coast Railway Company", "source": "lcnaf",
                       "uri": "https://id.loc.gov/authorities/names/n85000000", "etype": "org", "score": 0.9}},
        {"surface": "Miami", "etype": "place", "authority": None},
    ]
    print(authority_evidence_block(demo))
    print()
    print(json.dumps({"fields": list(_FIELD_RULES)}, indent=1))
