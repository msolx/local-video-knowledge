# Milestone M5: Knowledge Store & Retrieval Foundation · Master Handoff Protocol

> **Milestone Status**: `IN_PROGRESS` (M5-00 = `DONE / SEALED`; M5-01 = `DONE`; M5-02 = `DONE`; M5-03 = `DONE`; M5-04 = `DONE`; M5-05 = `DONE`; M5-06 = `TODO`)
> **Source Baseline**: Milestone M4 Sealed at Tag `m4-unified-knowledge-model-complete` (`92775b9ad862bc179f041c8ad56c2ee1c1bd8e49`).
> **Working Branch**: `feat/m5-knowledge-store-retrieval`

---

## 1. Handoff Overview

Milestone M5 builds the **Knowledge Store & Retrieval Foundation**: a derived,
rebuildable SQLite store over the canonical M4 `knowledge_units.json` artifacts,
plus a stable lexical retrieval contract that returns Knowledge Units together
with full provenance (evidence refs, attribution, lineage, source artifact).

M5 is **offline and deterministic**. It performs **no inference**: no LLM, no
embeddings, no reranker. Retrieval returns hits, never answers.

### Upstream M4 Deliverables Consumed (Read-Only):
- `data/processed/<canonical_id>/knowledge/knowledge_units.json`
  (schema `knowledge-units-v1`, `CanonicalKnowledgeUnitsDocument`).

### M5-00 Deliverables Completed:
- `docs/M5_KNOWLEDGE_STORE_DESIGN.md`: authoritative design specification v1.0.
- `docs/M5_DECISIONS.md`: Decisions 1-23.
- `docs/M5_TASKS.md`: frozen task tree M5-00 ~ M5-06.
- `docs/M5_HANDOFF.md`: this protocol.

### M5-01 Deliverables Completed:
- `src/knowledge/store.py`: SQLite `knowledge-store-v1` canonical knowledge store.
  - `create_store` / `open_store` / `validate_store`; `PRAGMA user_version=1` +
    `store_meta`; incompatible schema → `StoreSchemaError` (no silent migration).
  - Tables `store_meta`, `ingested_assets`, `knowledge_units`,
    `evidence_refs`, `entities`, `topics` (FK `ON DELETE CASCADE`,
    `PRAGMA foreign_keys=ON`).
  - `ingest_knowledge_document(db_path, knowledge_units_path)`: parse +
    domain-validate via `CanonicalKnowledgeUnitsDocument.from_dict`, then one
    atomic `BEGIN IMMEDIATE ... COMMIT` per asset; failure → ROLLBACK.
  - Idempotency (`unchanged` / cache hit, `ingested_at` preserved) and
    deterministic replace (`replaced`) inside one transaction.
  - `remove_asset`, `compute_store_revision`, `rebuild_store` (temp-DB + atomic
    replace, fail-fast), minimal read API (`get_unit`,
    `list_units_for_asset`, `get_ingested_asset`, `list_ingested_assets`).
  - No FTS5 / search / ranking / filters / LLM / embeddings.
- `tests/test_knowledge_store.py`: 47 tests (create/version/tables/schema
  rejection, ingest validity, round-trips, idempotency, replace, rollback
  isolation, removal, FK/orphan/projection/ordinal validation, parameterized
  safety, unicode/multiline/coordinates, zero-unit, multi-asset, rebuild, real
  C10 video+album).
- Real C10 ingestion (temp/test DB; M4 artifacts untouched):
  - Video 62 units, Album 6 units → 2 assets / 68 units / 150 evidence refs /
    138 entities / 103 topics; validation valid.
  - Second identical ingest → `unchanged`, row counts identical.
- Targeted suite: 83 passed (36 models + 47 store). Full regression:
  **1033 passed, 10 skipped** (M4 baseline 986 + 47 new; zero regressions).

### M5-02 Deliverables Completed:
- `src/knowledge/fts.py`: FTS5 lexical indexing module.
  - Frozen policy: `FTS_POLICY_VERSION="m5-fts-trigram-v1"`,
    `FTS_TOKENIZER="trigram"`, `FTS_COLUMNS=(statement, entity_names, topics,
    evidence_excerpts)`, `FTS_FIELD_WEIGHTS={statement:5.0, entity_names:2.0,
    topics:2.0, evidence_excerpts:1.0}`.
  - `FTS_SCHEMA_SQL`: derived materialized table `knowledge_fts_content`
    (`unit_rowid INTEGER PRIMARY KEY` 1:1 with `knowledge_units.unit_rowid`;
    `statement/entity_names/topics/evidence_excerpts TEXT NOT NULL`) +
    external-content FTS5 `knowledge_fts` (`content='knowledge_fts_content'`,
    `content_rowid='unit_rowid'`, `tokenize='trigram'`) + 3 sync triggers
    (`knowledge_fts_ai`/`_ad`/`_au`) keeping the index in the same transaction.
  - `build_fts_content_values(payload)` deterministic projection (ordinal-order
    space joins; never re-sorts, never summarizes, never attribution/verification).
  - `literal_fts_query(q)` = `"<q>"` with doubled embedded quotes (literal phrase
    — disables all FTS5 query syntax; parameterized SQL only).
  - `lexical_search_rows(conn, query_text, limit, weights=...)` low-level helper
    returning `(unit_rowid, bm25_score)` ordered best-first (NOT the M5-03 public
    Retrieval API); `fts_index_count(conn)`; `fts_integrity_check(conn)`.
- `src/knowledge/store.py` (extended for FTS, no semantic rewrite):
  - Schema now includes `knowledge_fts_content` + `knowledge_fts` + triggers;
    `_required_tables_exist` includes both; `store_meta` records
    `fts_policy_version` + `fts_tokenizer`.
  - `_insert_unit_rows` materializes the FTS content row per unit (capturing
    `unit_rowid`); `_delete_asset_rows` and `remove_asset` explicitly delete the
    asset's FTS content rows (trigger cleans the index) in the same transaction.
  - `validate_store` extended: FTS content row count == unit count, missing FTS
    content, orphan FTS content, FTS5 `integrity-check`, and `store_meta`
    `fts_policy_version` match.
- `tests/test_knowledge_fts.py`: 45 tests (FTS5/trigram availability, schema
  presence, unit_rowid mapping, statement/entity/topic/evidence indexing, field
  weight preference statement>evidence, insert/replace/remove/rollback/rebuild
  sync, validation catches missing/orphan content, repeated-ingest no dup,
  unicode + CN/EN mixed, literal quote/punctuation/injection safety, empty query,
  deterministic ranking, store revision unaffected by FTS, tokenizer policy
  persisted, short-query (<3 chars) documented limitation, real C10 video+album:
  count 68 + Vulkan/RDNA/Thinking/27B/logitech/AGON/SMILEY + real Chinese
  大模型/思考模式/任务类型 + mixed Vulkan后端/Strax Halo/AMX395).
- **Tokenizer decision**: probe on SQLite 3.45.3 proved `unicode61` cannot match
  Chinese words or Latin tokens inside mixed text (every such query → 0); frozen
  `trigram` instead (all ≥3-char queries match; <3 chars = documented known
  limitation, no fallback this round). Recorded as Decision 21 (bounded
  implementation correction, not a contract redesign).
- Real C10 FTS (temp/test DB): 2 assets / 68 units → FTS content == index == 68;
  all 10 real query smoke tests pass; validation valid.
- Targeted suite: 92 passed (47 store + 45 fts). Full regression:
  **1078 passed, 10 skipped** (M5-01 baseline 1033 + 45 new; zero regressions).

### M5-03 Deliverables Completed:
- `src/knowledge/retrieval.py`: public Evidence-Grounded Retrieval API.
  - Frozen contracts `RetrievalQuery` / `RetrievalHit` / `RetrievalResult` with
    JSON-safe `to_dict`/`from_dict`; `RetrievalQuery` validates `query_text`,
    `top_k ∈ [1, 100]`, and filter enums; `retrieve(db_path, query)` is the
    stable entrypoint (also behind `FTS5RetrievalBackend`).
  - Deterministic normalization (NFKC + strip + whitespace collapse) + literal
    term planner. Long terms (≥3 chars) → trigram FTS with literal AND; short
    terms (1–2 chars) → deterministic `instr(lower(...))` substring fallback over
    `knowledge_fts_content` (weighted short score, higher better); mixed →
    FTS + short conditions (AND). No FTS operator authority for user text.
  - Structured filters (`canonical_ids`, `unit_types`,
    `verification_statuses`, `topics`, `entity_names`) applied in SQL **before**
    ranking/LIMIT; same-category OR, cross-category AND; enum-validated.
  - Canonical hydration from `canonical_payload_json` via
    `CanonicalKnowledgeUnit.from_dict()`; **evidence expansion always on**
    (full refs, canonical order); `source_artifact` from `ingested_assets`;
    `match_info` + `ranking_diagnostics` deterministic.
  - `RetrievalResult` carries `store_schema_version` + `store_revision`;
    BM25 lower-is-better, short score higher-is-better, never comparable; ties
    break on `unit_rowid` ASC; empty results legal; no cache; no LLM fallback.
- `src/knowledge/__init__.py`: M5-03 exports added.
- `tests/test_knowledge_retrieval.py`: 56 tests (validation, normalization,
  planner, long-FTS AND, short/1-char fallback + top_k, mixed, FTS operator
  safety, all structured filters + filter-before-limit, hydration, evidence
  ordering/coordinates, attribution/lineage, source artifact, store revision,
  match info, BM25/short-score direction, deterministic ties, zero result, no
  LLM fallback, Unicode, SQL-injection safety, JSON round-trip, backend
  abstraction, real C10 golden queries).
- Real C10 retrieval (temp/test DB): `Vulkan`/`RDNA`/`Thinking`/`27B` → video
  hits with populated evidence refs; `logitech`/`AGON`/`SMILEY` → album hits;
  `模型`/`速度` (2-char) → video hits via short fallback; `推理` (absent) → 0
  hits without error; `Vulkan 模型` → mixed AND; video+`logitech` → 0 hits,
  album+`logitech` → hits; claim / not_checked filters pass.
- Targeted suite: 184 passed (36 models + 47 store + 45 fts + 56 retrieval).
  Full regression: **1134 passed, 10 skipped** (M5-02 baseline 1078 + 56 new;
  zero regressions).

### M5-04 Deliverables Completed:
- `src/knowledge/retrieval.py` (extended, M5-03 contract untouched): M5-04
  ranking + diagnostics layer.
  - **QueryPlan** (frozen, JSON-safe) + `build_query_plan(query)`:
    `original_query`, `normalized_query`, `terms`, `long_terms`, `short_terms`,
    `retrieval_path` (`fts_trigram` / `substring_short` /
    `fts_trigram_with_short_filter`), `filters`, `top_k`. Never exposes SQL.
  - **Ranking policy `lexical-ranking-v1`** (frozen; in result + per-hit
    diagnostics): FTS/mixed lexicographic key
    `(evidence_only_tier, exact_statement_phrase DESC, statement_match DESC,
    entity_match DESC, topic_match DESC, term_coverage DESC, raw_bm25 ASC,
    unit_rowid ASC)`; short path keeps `weighted_substring_score` primary
    (higher better) with exact-phrase + coverage tie-breaks; `unit_rowid` ASC
    is the universal final tie-break. BM25 stays lower-is-better, never
    negated/normalized into a fake probability.
  - **Upgraded diagnostics**: `RetrievalResult.diagnostics` now includes
    `query_plan`, `filters_applied` (caller values only), and
    `candidate_count_before_limit` (no-LIMIT candidate query; no payload pulls
    for counting) alongside `result_count`/`top_k`/`short_query_fallback`/
    `ranking_policy_version`/`limitations`.
  - **Field-match signals & invariant**: `match_info` extended with
    `statement_match`/`entity_match`/`topic_match`/`evidence_match`,
    exact-phrase flags, `matched_term_count`/`total_term_count`/`term_coverage`,
    `evidence_only_match`. `check_retrieval_invariant()` +
    `RetrievalInvariantError` enforce the AND term-coverage invariant.
  - **No bias / no penalties**: `extraction_confidence` and `verification_status`
    never boost ranking; no content-based meta-unit penalties.
  - `ranking_diagnostics` adds `ranking_policy_version`, `bm25_direction` /
    `score_direction`, `ranking_components`, `short_term_matches` (mixed), and a
    templated `why_this_hit` (never an LLM).
- `src/knowledge/__init__.py`: M5-04 exports added (paths, `RANKING_POLICY_VERSION`,
  `QueryPlan`, `build_query_plan`, `RetrievalInvariantError`,
  `check_retrieval_invariant`).
- `tests/test_knowledge_ranking.py`: 45 tests (query plan long/short/mixed,
  filter diagnostics, candidate/result counts, policy version, field-match
  signals, evidence-only, exact phrase, term coverage + invariant, BM25/short
  directions, statement>entity>topic>evidence relative priority, tie-break,
  stability, confidence/verification non-bias, AND semantics, no fuzzy/no
  rewrite, JSON safety, SQL-free diagnostics, hit compatibility, zero-result,
  real C10 ranking audits).
- Real C10 ranking audit (temp DB, 9 golden queries): explainable ordering —
  statement/entity/topic hits rank above evidence-only hits
  (`evidence_only_tier=1` + `evidence_only_match=true`), short path ranks by
  weighted score with `candidate_count_before_limit` > `result_count` where
  applicable; term coverage 1.0 everywhere; `Vulkan` 5 candidates / 5 hits,
  `Thinking` evidence-only flagged, `模型` 24 candidates / 5 hits (top_k=5).
- Targeted suite: 101 passed (56 retrieval + 45 ranking). Full regression:
  **1179 passed, 10 skipped** (M5-03 baseline 1134 + 45 new; zero regressions).
  M5-01 store / M5-02 FTS schema / M5-03 retrieval contracts untouched; M4 untouched.

### M5-05 Deliverables Completed:
- `src/knowledge/evaluation.py` (new, consumer-only): `GoldenQuery` /
  `GoldenSuite` / `load_golden_suite` / `load_golden_queries` /
  `evaluate_query` / `evaluate_suite` / `EvaluationSummary` /
  `write_evaluation_report`. `evaluate_query` computes Hit@K, MRR,
  first_relevant_rank, Precision@K/Recall@K/F1@K (exhaustive judgment only),
  required/forbidden hit checks, expected/forbidden canonical checks,
  zero-result gates, max_first_relevant_rank bounds, filter_correct,
  retrieval_path_correct, evidence/provenance completeness,
  term_coverage_valid. Aggregates never mix denominators
  (`exhaustive_query_count` / `partial_query_count` reported).
- `evaluation/m5/c10_golden_queries.json` (tracked, `m5-c10-golden-v1`):
  17 queries bound to the C10 corpus fingerprint (`sha256` of canonical
  `[{canonical_id, sha256}]` of the two real final M4 artifacts). Categories:
  long trigram (`Vulkan`/`RDNA`/`Thinking`/`27B`), album entity terms
  (`logitech`/`AGON`/`SMILEY`), short Chinese fallback (`模型`/`速度` + absent
  `推理` negative), mixed (`Vulkan 模型`), structured filters (album/video),
  topic filter, entity filter, verification_status filter, and a deterministic
  random-absent zero-result query. Golden KU IDs come only from real disk
  artifacts; stale corpus fingerprint fails every query.
- `scripts/run_m5_05_retrieval_eval.py`: disposable temp store (never
  production), ingests both C10 final artifacts, verifies 2 assets / 68 KU /
  fingerprint, runs suite, prints concise report, writes machine-readable
  `evaluation/m5/reports/c10_retrieval_evaluation.json` (gitignored).
- `tests/test_knowledge_evaluation.py`: 56 tests (fixture parse/validation,
  corpus fingerprint + stale detection, partial vs exhaustive semantics,
  Hit@K / MRR / P/R/F1, required/forbidden/filter/zero-result gates, retrieval
  path success/failure, evidence/provenance/ranking-diagnostics completeness,
  determinism, aggregate denominators, all 17 real C10 golden queries,
  full-suite pass, report writing).
- **Real C10 result**: 17/17 golden queries pass. Aggregate mean Hit@K =
  0.8235, mean MRR = 0.8235; exhaustive mean Precision@K = 0.6467,
  Recall@K = 0.9417, F1@K = 0.7144; filter_accuracy = 1.0,
  retrieval_path_accuracy = 1.0, evidence_completeness = 1.0,
  provenance_completeness = 1.0, term_coverage_valid_rate = 1.0.
- Targeted suite: 157 passed (56 retrieval + 45 ranking + 56 evaluation). Full
  regression: **1235 passed, 10 skipped** (M5-04 baseline 1179 + 56 new; zero
  regressions). M5-01/02/03/04 sealed files untouched; no retrieval bug surfaced
  (no STOP/HOLD); M4 untouched.

---

## 2. Key Architecture Invariants & Contracts

1. **Source-of-Truth Invariant**:
   - `M4 knowledge_units.json` == **Canonical Knowledge Source of Truth**.
   - `M5 Knowledge Store` == **Derived / Rebuildable Retrieval Store**.
   - Store deletion → rebuildable from canonical artifacts. Store never edits
     canonical unit semantics (statement / verification / attribution /
     evidence / KU ID).
2. **SQLite v1**: `data/knowledge/knowledge_store.sqlite3`; stdlib `sqlite3`;
   FTS5 for lexical search. No PostgreSQL/ES/Qdrant/Chroma/LanceDB.
3. **Schema versioning**: `store_schema_version = "knowledge-store-v1"` via
   `PRAGMA user_version = 1` + `store_meta`. v1 supports `create`, `validate`,
   `rebuild` only; incompatible schema → explicit fail/rebuild.
4. **Single-truth projection**: `knowledge_units.canonical_payload_json` is
   authoritative inside the store; every projection column is derived from it in
   one ingestion parse; `RetrievalHit` fields are read from the canonical JSON.
5. **Atomic ingestion**: one asset = one `BEGIN IMMEDIATE ... COMMIT`
   (upsert asset → replace units → replace evidence/entities/topics → refresh
   FTS). Failure → ROLLBACK, no half-asset.
6. **Idempotency**: same `canonical_id` + same `source_artifact_fingerprint` →
   NO-OP/cache hit (no duplicates, `ingested_at` preserved). Changed fingerprint
   → deterministic replace in the same transaction.
7. **Deletion semantics**: `remove_asset(canonical_id)` deletes only derived
   store rows; never touches `data/processed` / M4 / M3 / archive media.
8. **Rebuild**: `rebuild_store` scans
   `data/processed/*/knowledge/knowledge_units.json`, validates each via
   `CanonicalKnowledgeUnitsDocument`, **fails fast** on invalid artifacts.
9. **Weighted FTS5 (Option B′ + Decision 21/22)**: columns `statement`
    (highest), `entity_names` (medium), `topics` (medium), `evidence_excerpts`
    (lowest); external-content FTS5 over the derived `knowledge_fts_content`
    projection (`content_rowid='unit_rowid'`), **`trigram` tokenizer** (probe
    proved `unicode61` unusable for the Chinese-dominant mixed corpus), `bm25()`
    weights `(5.0, 2.0, 2.0, 1.0)`. Index and rows update in the same
    transaction via triggers. Short (<3 char) queries use the deterministic
    M5-03 substring fallback (Decision 25).
10. **Retrieval ≠ answering**: no LLM, no RAG, no citations, no answer
    synthesis. No embeddings/vector/dense/hybrid/reranker in M5; `retrieval_method`
    + `RetrievalBackend` reserve extension points only.
11. **Retrieval contract**: `RetrievalQuery` (`query_text`, `top_k ∈ [1, 100]`,
    filters: `canonical_ids`, `unit_types`, `verification_statuses`, `topics`,
    `entity_names`), `RetrievalHit` (rank + full canonical unit + verbatim
    `evidence_refs` + `source_artifact` + `match_info` + `ranking_diagnostics`),
    `RetrievalResult` (query, `retrieval_method`, `store_schema_version`,
    `store_revision`, `result_count`, hits). Public entrypoint `retrieve()`.
    Query planner: long terms (≥3 chars) → trigram FTS literal AND; short terms
    (1–2 chars) → deterministic `instr()` substring fallback; mixed → both.
12. **Score semantics**: methods `lexical_fts5_trigram`,
    `lexical_substring_short`, `lexical_fts5_trigram_with_short_filter`. FTS
    path: `raw_bm25`, **lower is better** (`ORDER BY bm25 ASC`, ties by
    `unit_rowid` ASC), never negated into a probability. Short path:
    `weighted_substring_score`, **higher is better**, never comparable to BM25.
    Retrieval score ≠ truth probability ≠ extraction confidence ≠ verification
    status.
13. **Evidence expansion is mandatory**: `RetrievalHit.evidence_refs` always
    populated (id + verbatim excerpt + temporal/sequence coordinates).
14. **Verification semantics**: ranking never auto-boosts `verified` or
    penalizes `not_checked`; status is filter-only.
15. **Filters are structured projections**, never FTS substring search.
16. **Single-writer semantics**: exactly one process writes the store at a time;
    no unsafe multi-machine concurrent writes over network shares. No server in
    M5.
17. **Scale policy**: SQLite + FTS5 is the v1 target (personal KB scale);
    comfortable through 100k KUs, workable at 1M with proper indexes. No
    distributed DB in M5.

---

## 3. M5-01 Entry Points (for the next task)

- **Authoritative design**: `docs/M5_KNOWLEDGE_STORE_DESIGN.md`.
- **Real C10 inputs** (read-only):
  - `data/processed/douyin_7681603850364521734/knowledge/knowledge_units.json`
    (62 units, claim × 62, not_checked × 62).
  - `data/processed/douyin_7682038498466993905/knowledge/knowledge_units.json`
    (6 units, claim × 6, not_checked × 6).
- **Sealed model layer**: `src/knowledge/models.py`
  (`CanonicalKnowledgeUnitsDocument`, `CanonicalKnowledgeUnit`,
  `EvidenceRef`, `EntityMention`, `AttributionInfo`, `ExtractionLineage`).
- **Sealed fingerprint conventions**: canonical SHA-256 JSON
  (`sort_keys=True, ensure_ascii=False, separators=(",", ":")`), as used by
  `src/knowledge/merger.py` / `render.py` helpers.
- **Existing SQLite conventions**: `src/collector/repository.py`,
  `src/downloader/job_store.py`, `src/media_adapter/adapter.py` (stdlib
  `sqlite3`, `BEGIN IMMEDIATE`, prepared statements).
- **Atomic file helpers**: `src/storage.py`
  (`atomic_write_json`, `atomic_write_text`, `utc_now`, `load_json`).

---

## 4. Worktree State & Git Hygiene

- **Branch**: `feat/m5-knowledge-store-retrieval` (created from `main` at
  `92775b9ad862bc179f041c8ad56c2ee1c1bd8e49`).
- **M5-00 Additions**: `docs/M5_KNOWLEDGE_STORE_DESIGN.md`,
  `docs/M5_DECISIONS.md`, `docs/M5_TASKS.md`, `docs/M5_HANDOFF.md`.
- **M5-01 Additions**: `src/knowledge/store.py`,
  `tests/test_knowledge_store.py`; `src/knowledge/__init__.py` extended with
  M5-01 exports.
- **M5-02 Additions**: `src/knowledge/fts.py`,
  `tests/test_knowledge_fts.py`; `src/knowledge/store.py` extended (FTS schema,
  sync, validation) without rewriting M5-01 ingestion semantics;
  `src/knowledge/__init__.py` extended with M5-02 exports.
- **M5-03 Additions**: `src/knowledge/retrieval.py`,
  `tests/test_knowledge_retrieval.py`; `src/knowledge/__init__.py` extended with
  M5-03 exports. `store.py` / `fts.py` / `models.py` untouched by M5-03.
- **M5-04 Additions**: `src/knowledge/retrieval.py` extended (QueryPlan +
  `lexical-ranking-v1` ranking + upgraded diagnostics + term-coverage invariant);
  `tests/test_knowledge_ranking.py` (45 tests); `src/knowledge/__init__.py`
  extended with M5-04 exports. `store.py` / `fts.py` / `models.py` untouched by
  M5-04.
- **M5-05 Additions**: `src/knowledge/evaluation.py` (evaluation harness +
  golden-query model); `evaluation/m5/c10_golden_queries.json` (17 golden
  queries bound to the C10 corpus fingerprint); `scripts/run_m5_05_retrieval_eval.py`
  (disposable-store runner writing `evaluation/m5/reports/c10_retrieval_evaluation.json`,
  gitignored); `tests/test_knowledge_evaluation.py` (56 tests);
  `src/knowledge/__init__.py` extended with M5-05 exports.
  `retrieval.py` / `fts.py` / `store.py` / `models.py` untouched by M5-05
  (evaluation is a pure consumer; no retrieval bug surfaced, so no STOP/HOLD).
- **Zero M4 code modified**: `models.py`, `extractor.py`, `merger.py`,
  `enrichment.py`, `render.py` untouched. No FTS5, no search, no LLM, no
  runtime started or probed.
- **No production DB written**: all M5-01/M5-02/M5-03 ingestion/retrieval ran on
  temp/test SQLite DBs. The official `data/knowledge/knowledge_store.sqlite3`
  will be built at milestone acceptance (M5-06) or a later explicit step.

### Operator Note: Local LLM Runtime Preference (carried from M4)

Future local LLM runtime preference:

1. Prefer llama.cpp at: `G:\llama.cpp`
2. Local model storage: `D:\LMmodel`
3. LM Studio is no longer the default runtime.
4. Tasks that do not require LLM inference must not start or probe either runtime.

This is an operator/runtime note only; it does not modify any M5 schema. M5 does
not require any runtime.

---

## 5. NEXT_AGENT_START_HERE

- **Task**: `M5-06 · End-to-End Acceptance`
- **Objective**: Offline full regression, real C10 store build + query
  acceptance, and the final M5 acceptance document:
  - Build the official store from both C10 final artifacts (62 + 6 = 68 KU),
    run the golden suite, verify counts/evidence/filters, zero mutation of M4
    artifacts, capture regression baselines.
  - Deliver `docs/M5_FINAL_ACCEPTANCE.md`.
- **Do not begin any post-M5 task**.
- **Hard constraints**:
  - M5-01 store + M5-02 FTS + M5-03/04 retrieval + M5-05 evaluation
    (`src/knowledge/evaluation.py`, `evaluation/m5/c10_golden_queries.json`)
    contracts are sealed; read-only.
  - M4 canonical artifacts remain read-only. No LLM, no embeddings, no
    reranker, no runtime probing, no network.
  - Retrieval ≠ answering: no RAG, no citations, no answer synthesis.
  - No production DB write until the acceptance step explicitly requires it.