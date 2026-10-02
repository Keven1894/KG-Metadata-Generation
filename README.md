# Layered Assurance for Knowledge-Graph-Mediated Newspaper Metadata

Data, code, and analysis notebooks for

> Boyuan Guan, Jamie Rogers, Hailu Xu, Wencong Cui, and Rebecca Bakker. *Layered Assurance for
> Knowledge-Graph-Mediated Newspaper Metadata: Traceable Checks, Identity Audit, and Evaluator
> Dependence.* 2nd Workshop on Smarter Extraction of Scholarly Metadata using Knowledge Graphs,
> Language Models and Agents (SESAME 2026), co-located with JCDL 2026. CEUR Workshop Proceedings.

The paper PDF is in [`paper/`](paper/). The study covers 203 issues of the historical
newspaper *Miami Life* (1927–1949), held by FIU Libraries and openly viewable in
[dPanther](https://dpanther.fiu.edu/), FIU Libraries' digital repository. It evaluates three
assurance layers for LLM-drafted, knowledge-graph-mediated metadata: E1 traceable gate checks,
E2 a blinded human identity audit, and E3 evaluator dependence among LLM judges.

## What this repository supports

- **Verifying every number in the paper** from frozen artifacts and per-item data
  (`artifacts/`, `data/`, `notebooks/`).
- **Inspecting the method**: the authority gate, entity registry, knowledge graph, record
  drafting, judging, and the identity-audit interface (`code/`).
- **Re-checking E1 and E3 without model calls**: the trace records every field the gate read
  and every outcome, and the E3 reanalysis (`code/e3/e3_reanalyze_pairwise.py`) runs on the
  released judge outputs. A full E1 replay also needs the entity registry, which is not
  released (see Data access).

It is not a standalone execution environment: the copied modules import internal helpers
(`config`, `core.llm`, `common.*`) that belong to the production system, and the newspaper OCR
and page images are not redistributed (see Data access).

## Contents

| Path | What it is |
|---|---|
| `paper/` | Camera-ready paper. |
| `rubric/e2_identity_audit_codebook.md` | Written rubric given to all five reviewers. |
| `artifacts/` | Frozen E1, E2, and E3 analysis outputs used for every number in the paper. |
| `data/miami_life_issues.csv` | The 203 analyzed issues: identifier, date, title, catalog search URL. |
| `data/e1/e1_structured_check_trace.jsonl` | The 6,401-row E1 replay trace (SHA-256 matches `artifacts/e1_structured_check_manifest.json`). |
| `data/e1/authority_cache_redacted.json` | Cached Library of Congress and GeoNames responses used by the replay (redacted; see below). |
| `data/e2_identity_labels_anonymized.jsonl` | Per-decision E2 export (917 decisions; 406 labeled at the analysis freeze). Reviewers are pseudonymized R1–R5; free-text notes removed. |
| `data/e3/records/<condition>/` | The 203 × 3 generated Dublin Core records judged in E3. |
| `data/e3/pairwise/<judge>.json` | Pairwise judge outputs in both presentation orders (self = `gpt-5.2`, opus = `claude-opus-4-8`, gpt55 = `gpt-5.5`). |
| `data/e3/claims/<judge>/` | Per-issue atomic-claim verdicts for each judge and condition. |
| `data/e3_claim_precision_per_issue.csv` | Per-issue claim precision summary. |
| `data/precedent/episodes.json` | The frozen peer-cataloging episode store used by C-precedent. |
| `notebooks/` | Analysis notebooks for the E1, E2, and E3 tables and figures. |
| `code/` | Gate, registry, KG, generation, evaluation, and review-portal code. |

## Data access

The OCR text, page images, and MODS records belong to FIU Libraries and are not
redistributed here. `data/miami_life_issues.csv` lists every analyzed issue with a catalog
link. The entity registry (8,413 normalized entity surfaces with OCR context snippets) is
likewise not released; E1 can be checked from the trace, which carries every field the gate
read.

## Redactions

- **Authority cache.** The GeoNames account name in cached request URLs is replaced by
  `REDACTED`, and 12 email addresses inside cached authority records are replaced
  by `<email>`. The released file therefore differs from the hashed original
  (`685e7acc8e6ba69e…`); code reading it must set `GEONAMES_USERNAME=REDACTED`
  so cache keys match.
- **Supplement files** (`artifacts/`, `code/`, `notebooks/`, `rubric/`) are the versions
  provided to reviewers: reviewer identities, internal host names, and local paths are
  replaced by `<...>` placeholders.

## Scope and provenance

The paper's results are E1 traceability, the 406-decision E2 analysis freeze, and the E3
evaluator-dependence analysis; the remaining 511 E2 decisions were not labeled. The E1
accounting notebook also retains a historical 41% unique-candidate refusal calculation, and the
E3 notebook retains legacy G0/G1/G2 condition labels (G0 = C-simple, G1 = C-standard,
G2 = C-precedent). These are analysis provenance, not headline claims.

The frozen (v4) C-precedent records were generated with plain cosine top-k retrieval over the
peer-episode store (`code/generation/precedent_knowledge.py`). `graph_scoped_retrieve()` in
`code/graph/kg.py` was added after that run and is not evaluated in the paper.

## Knowledge-graph node semantics and upstream extraction

KG entity nodes are normalized entity surfaces, not resolved real-world identities. The key is
the case-folded, punctuation-stripped, whitespace-collapsed surface (`norm_key()` in
`code/gate/entity_registry.py`); the type recorded is the first observed type for that surface,
mention counts, issue memberships, activity years, roles, and context snippets are accumulated
per key, and no coreference or consolidation beyond this normalization is performed. The 8,413
nodes (4,939 people, 2,467 organizations, 1,007 places) count these normalized surfaces.

The upstream per-issue extraction (people, businesses, places, topics, and other fields) is
the collection's existing production pipeline, run with `gpt-5.2`; its prompt belongs to that
system and is not part of this study or this package. The manuscript does not evaluate its
correctness or recall.

Subject candidates are a separate route: each issue's extracted topic list (not KG entity
nodes) is queried against LCSH (`_gather_entities()` in `code/generation/cards.py`). The 19
subject decisions in the E2 frame come from this route and have `g0_found = 0` in the trace.

## Peer-episode store (C-precedent)

The peer cataloging episodes were mined from publicly available external cataloging sources
(a cataloging listserv archive and publicly posted cataloging practice notes from other
libraries); no Miami Life material and no non-public communication is in the store. The store
is frozen; the mining and judging code is in `code/generation/precedent_knowledge.py`.

## Run identifiers (E1)

- trace_run_id: `e1-trace-replay-2026-09-03`
- trace SHA-256: `16fce8227539bac35ff9963f823b300249ade5065c0baac170f79d68ae45405b`
- authority cache SHA-256: `685e7acc8e6ba69e56bc166e0b9136fc18526db90a712459a918c8da86f2f09f`
- entity registry SHA-256: `2ca5ee6a2ed77ca8140f56adde2243a12b310e339bc1f7d0d532dbb2bade27f7`
- code commit: `b138aed7d141c26a6217b413638e3e7c464e4b6a`
- offline_only=True, llm_calls=0, card_generation=False, ledger_writes=False

## Models

| Role | Provider model identifier | Sampling |
|---|---|---|
| Generator (all three conditions) | `gpt-5.2` | temperature 0 |
| Claim decomposition | `gpt-5.2` (shared by all judges) | temperature 0 |
| Self-judge | `gpt-5.2` | temperature 0 |
| Cross-provider judge | `claude-opus-4-8` | temperature omitted (API rejects the parameter) |
| Same-provider non-generator judge | `gpt-5.5` | provider fixes temperature at 1 |

Snapshots are not pinned by the providers; "self-judge" therefore means the same model
identifier as the generator. Max-token settings and retry policy are in `code/e3/multi_llm.py`,
`pairwise_judge.py`, and `claim_eval.py`. Pairwise judging truncates OCR to a head+tail window
when an issue exceeds 60,000 characters (182 of 203 issues); the same window is used for all
conditions.

## Candidate selection before the gate

Queries are routed by entity type (LCNAF PersonalName / CorporateName, LCSH Topic, GeoNames with
`country=US, adminCode1=FL` and an LC Geographic fallback). LoC Suggest2 is queried with
`count=10`, left-anchored first and keyword as fallback; GeoNames with `maxRows=5`. The single
candidate with the highest lexical score is retained and is the only candidate the gate
evaluates. See `code/gate/authority.py`.

## Reviewer blocks (E2)

R1 from position 1, R2 101–200, R3 201–300, R4 301–400, R5 401–500 in the seeded order
(`shuffle_seed` in the population manifest). Reporting set = longest contiguous prefix per
block; at the freeze R1=45, R2=100, R3=61, R4=100, R5=100.

## Licenses

Code is released under the MIT License (`LICENSE`). Data and documentation are released under
CC BY 4.0 (`LICENSE-DATA.md`). Cached authority records remain subject to the terms of their
sources (Library of Congress, GeoNames).

## Citation

See `CITATION.cff`.
