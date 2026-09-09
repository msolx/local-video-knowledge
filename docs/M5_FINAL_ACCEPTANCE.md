# Milestone M5: Knowledge Store & Retrieval Foundation · Final Acceptance

> **Milestone Status**: `M5-06 = DONE` · **M5 overall = COMPLETE / ACCEPTED WITH KNOWN LIMITATIONS**
> **Working Branch**: `feat/m5-knowledge-store-retrieval`
> **Acceptance Method**: fully offline, deterministic, read-only against M4 canonical artifacts. No LLM, no embeddings, no reranker, no runtime, no network.

---

## 1. Milestone Scope

M5 built the **Knowledge Store & Retrieval Foundation**: a derived, rebuildable
SQLite store over the canonical M4 `knowledge_units.json` artifacts, a weighted
FTS5 lexical index, a public evidence-grounded Retrieval API with structured
filters and explainable ranking, and a golden-query evaluation harness.

M5 is **offline and deterministic**. It performs **no inference**: no LLM, no
embeddings, no reranker. Retrieval returns hits (with full evidence expansion),
never answers.

## 2. Sealed Commits (M5-00 ~ M5-05)

| Stage | Commit | Summary |
| :--- | :--- | :--- |
| M5-00 | `712dcc0` | Contract design & freeze (`docs/M5_*.md`) |
| M5-01 | `04f7991` | Canonical Knowledge Store & idempotent ingestion (`store.py`) |
| M5-02 | `6472641` | SQLite FTS5 lexical indexing (`fts.py`) |
| M5-03 | `6e1239c` | Evidence-grounded retrieval API (`retrieval.py`) |
| M5-04 | `3707412` | Deterministic retrieval ranking diagnostics (retrieval.py rank layer) |
| M5-05 | `66d6d19` | Retrieval evaluation harness & C10 golden queries (`evaluation.py`, `c10_golden_queries.json`) |

Pre-acceptance HEAD: `66d6d192a1183f0688de8b9898a271b7f7b88639` (M5-05 final baseline, sealed).

## 3. Store Architecture

- **Storage**: `data/knowledge/knowledge_store.sqlite3` (stdlib `sqlite3`, v1 schema).
- **Schema**: `knowledge-store-v1` via `PRAGMA user_version = 1` + `store_meta`
  (records `schema_version`, `schema_policy_version`, `created_at`, `rebuilt_at`,
  `store_revision`, `fts_policy_version`, `fts_tokenizer`).
- **Tables**: `store_meta`, `ingested_assets`, `knowledge_units` (with verbatim
  `canonical_payload_json`), `evidence_refs`, `entities`, `topics`, plus the
  derived FTS projection `knowledge_fts_content` and the external-content FTS5
  index `knowledge_fts`. FK `ON DELETE CASCADE`, `PRAGMA foreign_keys = ON`.
- **Single-truth projection**: every projection column is derived from
  `canonical_payload_json` in one ingestion parse; `validate_store` enforces
  projection↔payload consistency.
- **Atomic ingestion**: one asset = one `BEGIN IMMEDIATE ... COMMIT`
  (asset → units → evidence/entities/topics → FTS content row → triggers sync
  the index). Failure → ROLLBACK. Idempotent (`unchanged`) and deterministic
  replace (`replaced`) inside one transaction.
- **Rebuild**: `rebuild_store` scans `data/processed/*/knowledge/knowledge_units.json`
  (final artifacts only), builds a temp DB, validates it, then atomically
  replaces the official DB. A failed rebuild preserves the existing store.
- **Store revision**: SHA-256 over sorted `(canonical_id, source_artifact_fingerprint)`
  pairs — deterministic, independent of rowids/`ingested_at`.

## 4. Tokenizer Decision

Probe on SQLite 3.45.3 proved `unicode61` cannot match Chinese words or Latin
tokens embedded in mixed CJK+Latin text (every such query → 0 hits). The frozen
tokenizer is **`trigram`** (Decision 21): all ≥3-character queries match,
including Chinese words and mixed CN/EN text. Queries < 3 characters use the
deterministic M5-03 literal substring fallback (Decision 25).

FTS5 columns (weighted): `statement` (5.0), `entity_names` (2.0), `topics`
(2.0), `evidence_excerpts` (1.0); `bm25()` with explicit column weights.

## 5. Query / Retrieval Contract

- Public entrypoint `retrieve(db_path, query)` returning `RetrievalResult`.
- `RetrievalQuery`: `query_text`, `top_k ∈ [1, 100]`, structured filters
  `canonical_ids`, `unit_types`, `verification_statuses`, `topics`,
  `entity_names` (enum-validated; empty = unset).
- Query planner: long terms (≥3 chars) → trigram FTS with literal AND; short
  terms (1–2 chars) → deterministic `instr()` substring fallback over
  `knowledge_fts_content`; mixed → both (AND).
- Filters applied in SQL **before** ranking/LIMIT; same-category OR,
  cross-category AND; filters are structured projections, never FTS substrings.
- `RetrievalHit`: full canonical unit (hydrated via
  `CanonicalKnowledgeUnit.from_dict`) + **always-populated** `evidence_refs`
  (id + verbatim excerpt + temporal/sequence coordinates) + `source_artifact`
  provenance + `match_info` + `ranking_diagnostics`.
- Retrieval ≠ answering: no LLM, no RAG, no citations, no answer synthesis.

## 6. Ranking Policy

Frozen `lexical-ranking-v1` (Decision 30).

- FTS / mixed path lexicographic key:
  `(evidence_only_tier, exact_statement_phrase DESC, statement_match DESC,
  entity_match DESC, topic_match DESC, term_coverage DESC, raw_bm25 ASC,
  unit_rowid ASC)`.
- Short path: `weighted_substring_score` primary (higher is better) with
  exact-phrase + coverage tie-breaks; `unit_rowid` ASC universal final tie-break.
- **BM25 lower is better** (never negated into a probability); short score
  higher is better (never compared to BM25). Retrieval score ≠ truth probability
  ≠ extraction confidence ≠ verification status.
- `extraction_confidence` and `verification_status` never boost ranking.
- `why_this_hit` diagnostics are templated strings, never LLM output.

## 7. Real Production Store Counts

Built from **all** legal M4 final artifacts discovered on disk
(`data/processed/*/knowledge/knowledge_units.json`):

| Asset (`canonical_id`) | Path | SHA-256 | Units |
| :--- | :--- | :--- | ---: |
| `douyin_7681603850364521734` | `data/processed/douyin_7681603850364521734/knowledge/knowledge_units.json` | `255b0a8bc2dfc7d4f8383185687755d56c9f82d57363090ab67dffc066faa93e` | 62 |
| `douyin_7682038498466993905` | `data/processed/douyin_7682038498466993905/knowledge/knowledge_units.json` | `361b0e82bb7a7caabf6ccd65bc1a6993893141f735573e200c724c7afe7e79c4` | 6 |

- **asset count**: 2
- **KU count**: 68
- **EvidenceRef count**: 150
- **Entity count**: 138
- **Topic count**: 103
- **FTS content count**: 68 (= KU count, 1:1) · **FTS index count**: 68
- **Store revision**: `7b604b334eaaede2f98e341a1cbeafdf3979d4643bc2b54196769e19919355d6`
  (identical between acceptance temp store and production store)
- **Production store validation**: `valid`, 0 violations

## 8. Structural Acceptance (all gates)

| Gate | Result |
| :--- | :--- |
| Source validation (all discovered artifacts pass `CanonicalKnowledgeUnitsDocument.from_dict` + domain validation) | PASS (2/2) |
| Acceptance temp store rebuild (`rebuild_store`) | PASS |
| `validate_store` | PASS, violations = 0 |
| M5 store integrity (user_version, metadata, FK, integrity_check, projection↔payload, asset unit counts, ordinal integrity, FTS content↔KU 1:1, FTS integrity, store_revision) | PASS, all checks clean |
| Round-trip audit (hydrate `canonical_payload_json` → `CanonicalKnowledgeUnit` vs original M4, all 11 canonical fields) | PASS, 68 checked / 0 mismatches |
| Rebuild determinism (2 independent temp rebuilds → identical revision + semantically identical retrieval) | PASS |
| Failure safety (synthetic invalid artifact → rebuild fail-fast, existing store preserved) | PASS |

## 9. Retrieval Acceptance

Representative queries against the production store (all paths correct, top_k
applied, filters before limit, canonical hydration, non-empty EvidenceRefs,
source artifact provenance present, ranking diagnostics complete, term-coverage
invariant = 1.0, stable ordering):

| Query | Retrieval path | Hits |
| :--- | :--- | ---: |
| `Vulkan` | `fts_trigram` | 5 |
| `RDNA` | `fts_trigram` | 1 |
| `Thinking` | `fts_trigram` | 1 |
| `27B` | `fts_trigram` | 4 |
| `logitech` | `fts_trigram` | 2 |
| `AGON` | `fts_trigram` | 4 |
| `SMILEY` | `fts_trigram` | 4 |
| `Vulkan 模型` | `fts_trigram_with_short_filter` (mixed) | 1 |

Short-query acceptance (1–2 char substring fallback):

| Query | Retrieval path | Hits |
| :--- | :--- | ---: |
| `模型` | `substring_short` | 10 |
| `速度` | `substring_short` | 6 |
| `模` (1-char) | `substring_short` | no error, top_k respected |

Structured-filter acceptance: `canonical_id` (album + logitech → hits; **video +
logitech → 0** negative gate), `unit_type` (claim), `verification_status`
(not_checked), `topic` (模型优化), `entity` (logitech); same-category OR +
cross-category AND verified.

Ranking acceptance: statement-match ranks above evidence-only
(`27B`: statement-hit #1 vs evidence-only #4, `evidence_only_tier` = 1);
BM25 lower-is-better; short weighted score higher-is-better; no truth
probability / unified fake relevance score observed.

## 10. C10 Golden Baseline

Frozen suite `m5-c10-golden-v1` (17 queries) bound to the C10 corpus
fingerprint `27e87387805ea21159fae443cafbf2b0dac71b0e13f7b8d1a344d43603949b92`
(sha256 of canonical `[{canonical_id, sha256}]` of the two real final M4
artifacts). Runner: `scripts/run_m5_05_retrieval_eval.py` (disposable temp
store).

**Result: 17/17 PASS** (corpus fingerprint OK, no staleness; failed = 0).

| Metric | Value |
| :--- | :--- |
| Golden queries | 17 |
| Exhaustive | 10 |
| Partial | 7 |
| Mean Hit@K | **0.8235** |
| Mean MRR | **0.8235** |
| Exhaustive Precision@K | **0.6467** |
| Exhaustive Recall@K | **0.9417** |
| Exhaustive F1@K | **0.7144** |
| Filter accuracy | 1.0 |
| Retrieval path accuracy | 1.0 |
| Evidence completeness | 1.0 |
| Provenance completeness | 1.0 |
| Term-coverage valid rate | 1.0 |

Baseline preserved vs M5-05 frozen baseline (Hit@K 0.8235, MRR 0.8235,
Precision@K 0.6467, Recall@K 0.9417, F1@K 0.7144). Golden fixture NOT rewritten;
ranking untouched.

Determinism: golden evaluation run twice on the same temp store → results,
KU ordering, ranking, paths, metrics, and store revision identical
(only `generated_at` differs).

## 11. Production Store Creation

All gates passed before any production write. `data/knowledge/knowledge_store.sqlite3`
was created via `rebuild_store` (temp rebuild → validate → atomic replace; no
ad-hoc writes on an old DB). Re-opened after creation and re-validated:
`valid`, 0 violations, revision identical to the acceptance temp store.
Representative queries and the full golden suite run against the production
store: 17/17.

The production DB is **gitignored** (not committed). The SQLite store remains a
**derived / rebuildable artifact**; M4 `knowledge_units.json` remains the
Canonical Knowledge Source of Truth.

## 12. Known Limitations

1. **trigram lexical retrieval ≠ semantic retrieval**: term matching only; no
   synonym/embedding semantics.
2. **Short 1–2 char queries use a substring scan** (`instr` over
   `knowledge_fts_content`); a scalability limitation at larger corpus sizes.
3. **Exact topic/entity filters only**: no fuzzy/alias/substring matching for
   structured filter values.
4. **Current quality benchmark is C10**: 68 KU / 2 assets. If the production
   store later contains more assets, the golden benchmark still covers only C10.
5. **Baseline quality**: Precision@K ≈ 0.6467, Recall@K ≈ 0.9417 (mean over
   the 10 exhaustive queries).
6. **No learned/dense reranking**: ranking is the frozen lexical policy.
7. **SQLite single-writer semantics**: one writer process at a time; no
   multi-writer service in M5.
8. **M4 factual statuses are generally `not_checked`**: retrieval ranks
   irrespective of verification (filter-only by design).

None of these are defects discovered by acceptance; they are documented,
accepted characteristics of the M5 v1 design.

## 13. Regression

- Targeted (store + fts + retrieval + ranking + evaluation + acceptance):
  **279 passed**.
- Full regression `pytest tests -q`: **1265 passed, 10 skipped**
  (M5-05 baseline 1235 + 30 new; zero regressions).
- M4 code (`models.py`, `extractor.py`, `merger.py`, `enrichment.py`,
  `render.py`) untouched. M4 knowledge artifacts untouched. M5-01~05 sealed
  modules untouched. No LLM/runtime/network used.

## 14. Final Decision

All structural gates PASS, 17/17 golden PASS, baseline metrics preserved,
production store validated, full regression PASS.

**M5-06 = DONE. M5 overall = COMPLETE / ACCEPTED WITH KNOWN LIMITATIONS.**

The M5 retrieval foundation is ready for downstream use (future milestone)
with the documented limitations above.