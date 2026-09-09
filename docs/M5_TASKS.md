# Milestone M5: Knowledge Store & Retrieval Foundation · Task Board

> **Milestone Target**: A derived, rebuildable SQLite Knowledge Store over the canonical M4 `knowledge_units.json` artifacts, plus a stable lexical retrieval contract with full evidence expansion. Offline and deterministic; no LLM, no embeddings.
> **Working Branch**: `feat/m5-knowledge-store-retrieval`
> **Status Matrix**: M5-00 = `DONE / SEALED` | M5-01 = `DONE` | M5-02 = `DONE` | M5-03 = `DONE` | M5-04 = `DONE` | M5-05 = `TODO` | M5-06 = `TODO`

---

## 1. Milestone M5 Progress Board

| Task ID | Task Title | Owner | Status | Dependencies | Target Deliverable |
| :--- | :--- | :---: | :---: | :--- | :--- |
| **M5-00** | **Contract Design** | Sealed | **`DONE / SEALED`** | M4 Acceptance | `docs/M5_*.md` (Design & Contract Freeze) |
| **M5-01** | **Canonical Knowledge Store & Idempotent Ingestion** | Complete | **`DONE`** | M5-00 | `src/knowledge/store.py`, `tests/test_knowledge_store.py` |
| **M5-02** | **SQLite FTS5 Lexical / Metadata Indexing** | Complete | **`DONE`** | M5-01 | `src/knowledge/fts.py`, `tests/test_knowledge_fts.py` |
| **M5-03** | **Retrieval API & Evidence Expansion** | Complete | **`DONE`** | M5-01, M5-02 | `src/knowledge/retrieval.py`, `tests/test_knowledge_retrieval.py` |
| **M5-04** | **Filtering, Ranking & Query Diagnostics** | Complete | **`DONE`** | M5-03 | `src/knowledge/retrieval.py` (rank layer + QueryPlan + diagnostics), `tests/test_knowledge_ranking.py` |
| **M5-05** | **Retrieval Evaluation Harness & C10 Golden Queries** | — | **`TODO`** | M5-03, M5-04 | `scripts/run_m5_05_evaluation.py`, `tests/test_m5_retrieval_evaluation.py` |
| **M5-06** | **End-to-End Acceptance** | — | **`TODO`** | M5-01 ~ M5-05 | `docs/M5_FINAL_ACCEPTANCE.md` |

---

## 2. Detailed Task Breakdown

### M5-00: Contract Design (`DONE`)
- **Objective**: Freeze the Knowledge Store schema, retrieval contract, FTS strategy, ingestion/deletion/rebuild policies, and C10 golden-query fixtures without modifying production code.
- **Deliverables**:
  - `docs/M5_KNOWLEDGE_STORE_DESIGN.md`: Authoritative design specification (v1.0).
  - `docs/M5_HANDOFF.md`: Master handoff and cross-agent protocol.
  - `docs/M5_DECISIONS.md`: Architectural decisions log (Decisions 1-23).
  - `docs/M5_TASKS.md`: Task board and progression matrix.

### M5-01: Canonical Knowledge Store & Idempotent Ingestion (`DONE`)
- **Objective**: Implement the SQLite `knowledge-store-v1` schema and the atomic, idempotent ingestion pipeline over M4 `knowledge_units.json` artifacts.
- **Delivered**:
  - `src/knowledge/store.py`:
    - Schema `knowledge-store-v1` via `PRAGMA user_version = 1` + `store_meta`; `create_store`, `open_store`, `validate_store`; incompatible schema → explicit `StoreSchemaError` (create/validate/rebuild only, no silent migration).
    - Tables: `store_meta`, `ingested_assets`, `knowledge_units` (with verbatim `canonical_payload_json`), `evidence_refs` (ordinal + temporal/sequence coordinates), `entities`, `topics`; FK `ON DELETE CASCADE`, `PRAGMA foreign_keys = ON`.
    - Single-truth rule: all projection columns derived deterministically from `canonical_payload_json`; `validate_store` verifies projection-vs-payload consistency and fails loudly on divergence.
    - `ingest_knowledge_document(db_path, knowledge_units_path)`: reads + parses + domain-validates via `CanonicalKnowledgeUnitsDocument.from_dict`, then persists one asset in a single `BEGIN IMMEDIATE ... COMMIT`; failure → `ROLLBACK`, no half-asset.
    - Idempotency: same `canonical_id` + same fingerprint → `IngestResult(status="unchanged")`, no row touched, `ingested_at` preserved. Changed fingerprint → deterministic replace (`status="replaced"`) inside one transaction; stale units/evidence/entities/topics fully removed.
    - `source_artifact_fingerprint` = canonical SHA-256 JSON of the real `knowledge_units.json` (project convention, never fabricated).
    - `remove_asset(canonical_id)`: deletes only derived store rows (FK cascade); missing asset → deterministic `removed=False`; never touches source artifacts.
    - Deterministic store revision: SHA-256 over sorted `(canonical_id, source_artifact_fingerprint)` pairs; independent of `ingested_at`/rowids/time.
    - `rebuild_store`: fail-fast discovery of `data/processed/*/knowledge/knowledge_units.json` only (intermediate candidates never consumed); builds into a temp DB, validates, then atomically replaces the official DB — a failed rebuild preserves the old store.
    - Minimal deterministic read API: `get_unit`, `list_units_for_asset`, `get_ingested_asset`, `list_ingested_assets` (round-trip verification; NOT retrieval).
    - No FTS5, no search, no RetrievalQuery/Hit/Result, no ranking/filters, no LLM, no embeddings.
  - `tests/test_knowledge_store.py`: 47 collected tests covering create/version/tables/incompatible-schema rejection, ingest validity + rejection, asset metadata, canonical payload storage, evidence/entity/topic order round-trips, attribution/lineage round-trips, full semantic round-trip, idempotency (no-op, ingested_at preserved, no duplicate rows), changed-fingerprint replace, stale-row removal, rollback-preserves-old-version (simulated mid-transaction failure), remove asset + nonexistent + source-file untouched, FK integrity, orphan detection, projection consistency + corruption detection, unit-count invariant, invalid-ordinal detection, validation report, parameterized/quote safety, unicode/multiline/coordinate round-trips, zero-unit document, multiple assets, same-KU stability, rebuild discovery (final artifacts only), rebuild invalid fail-fast, rebuild failure preserves old store, rebuild success, and real C10 video/album/combined ingestion.
  - Real C10 ingestion (temp/test DB only, M4 artifacts untouched):
    - Video `douyin_7681603850364521734`: 62 units ingested; album `douyin_7682038498466993905`: 6 units ingested; combined 2 assets / 68 units / 150 evidence refs / 138 entities / 103 topics; store validation valid.
    - Second identical ingest → `unchanged` (cache hit), row counts byte-identical.
  - Full regression: **1033 passed, 10 skipped** (M4 baseline 986 + 47 new store tests; zero regressions).

### M5-02: SQLite FTS5 Lexical / Metadata Indexing (`DONE`)
- **Objective**: Add the weighted FTS5 lexical index and keep it atomically in sync with structured rows.
- **Delivered**:
  - `src/knowledge/fts.py`:
    - Frozen policy: `FTS_POLICY_VERSION = "m5-fts-trigram-v1"`,
      `FTS_TOKENIZER = "trigram"`, `FTS_COLUMNS = (statement, entity_names,
      topics, evidence_excerpts)`, `FTS_FIELD_WEIGHTS = {statement: 5.0,
      entity_names: 2.0, topics: 2.0, evidence_excerpts: 1.0}`.
    - `FTS_SCHEMA_SQL`: derived materialized table `knowledge_fts_content`
      (`unit_rowid INTEGER PRIMARY KEY` 1:1 with `knowledge_units.unit_rowid`;
      `statement/entity_names/topics/evidence_excerpts TEXT NOT NULL`) +
      external-content FTS5 `knowledge_fts` (`content='knowledge_fts_content'`,
      `content_rowid='unit_rowid'`, `tokenize='trigram'`) + sync triggers
      `knowledge_fts_ai` / `_ad` / `_au` (index updated in the same transaction).
    - `build_fts_content_values(payload)`: deterministic projection — statement
      verbatim; entity/topic/evidence joined in canonical ordinal order; never
      re-sorted, never summarized, never attribution/verification/confidence.
    - `literal_fts_query(q)`: entire input wrapped in double quotes with embedded
      quotes doubled → literal FTS5 phrase; disables all query-language syntax;
      always parameterized (never spliced into SQL).
    - `lexical_search_rows(conn, query_text, limit, weights=...)`: low-level
      internal helper returning `(unit_rowid, bm25_score)` ordered best-first;
      `fts_index_count`; `fts_integrity_check`. Public Retrieval API is M5-03.
  - `src/knowledge/store.py` (extended, M5-01 semantics untouched): schema now
    includes FTS tables + triggers; `store_meta` records `fts_policy_version` +
    `fts_tokenizer`; `_insert_unit_rows` materializes FTS content rows;
    `_delete_asset_rows`/`remove_asset` explicitly delete FTS content rows in the
    same transaction; `validate_store` checks content==unit counts, missing /
    orphan FTS content, FTS5 `integrity-check`, and policy version.
  - **Tokenizer decision**: pre-flight probe on SQLite 3.45.3 showed `unicode61`
    tokenizes each CJK(+Latin) run as one token, so Chinese words and Latin
    tokens in mixed text (Vulkan/27B/RDNA/Thinking) cannot be matched. Frozen
    `trigram` instead (all ≥3-char queries match). Short (<3 char) queries are a
    documented known limitation with no fallback this round. Recorded as
    Decision 21 (bounded implementation correction, not a contract redesign).
  - `tests/test_knowledge_fts.py`: 45 collected tests (FTS5/trigram availability,
    schema presence, unit_rowid mapping, statement/entity/topic/evidence
    indexing, field-weight preference statement>evidence, insert/replace/remove/
    rollback/rebuild sync, validation catches missing/orphan FTS content,
    repeated-ingest no dup, unicode + CN/EN mixed, literal quote/punctuation/SQL
    injection safety, empty query, deterministic ranking, store revision
    unaffected by FTS, tokenizer policy persisted, short-query limitation, real
    C10 video+album golden queries incl. real Chinese/mixed terms).
  - Real C10 FTS (temp/test DB only): 2 assets / 68 units → FTS content == index
    == 68; Vulkan/RDNA/Thinking/27B/logitech/AGON/SMILEY/大模型/思考模式/任务类型/
    Vulkan后端/Strax Halo/AMX395 all return hits from the correct asset;
    validation valid.
  - Full regression: **1078 passed, 10 skipped** (M5-01 baseline 1033 + 45 new
    FTS tests; zero regressions).

### M5-03: Retrieval API & Evidence Expansion (`DONE`)
- **Objective**: Implement the retrieval contract with mandatory full evidence expansion.
- **Delivered**:
  - `src/knowledge/retrieval.py`:
    - Frozen public contracts `RetrievalQuery` / `RetrievalHit` / `RetrievalResult` with JSON-safe `to_dict`/`from_dict` (enums as canonical strings; no Row/Path/Connection leakage).
    - Stable entrypoint `retrieve(db_path, query)`; `FTS5RetrievalBackend` implements the design's `RetrievalBackend` abstraction (`retrieval_method="lexical_fts5_trigram"`). Callers never touch the sqlite Connection.
    - `RetrievalQuery` validates `query_text` (non-empty after strip), `top_k ∈ [1, 100]`, `unit_types` / `verification_statuses` against canonical enums (illegal → `ValueError`). Empty filter collections = unset.
    - Deterministic query normalization: NFKC + strip + collapse whitespace only (no stemming/synonyms/segmentation). `plan_literal_terms` splits by whitespace into literal terms.
    - **Long-term path** (≥3 codepoints): trigram FTS with literal AND semantics via `literal_fts_query`; `raw_bm25` ranked `ORDER BY bm25 ASC`.
    - **Short-term fallback** (1–2 codepoints): deterministic literal substring over `knowledge_fts_content` using parameterized `instr(lower(col), lower(?)) > 0` (no `LIKE`); `lexical_substring_short` weighted score (statement 5 / entity_names 2 / topics 2 / evidence_excerpts 1), higher is better; one-character queries allowed with strict `top_k`.
    - **Mixed long+short**: FTS constrains long terms, structured substring conditions require every short term too (AND).
    - **Structured filters** applied in SQL before ranking/LIMIT: `canonical_ids`/`unit_types`/`verification_statuses` on projection columns; `topics`/`entity_names` via `EXISTS` on structured child tables. Same-category values OR'd, categories AND'd.
    - **Canonical hydration** from `canonical_payload_json` via `CanonicalKnowledgeUnit.from_dict()` (never reassembled from projections); **evidence expansion always on** (full refs in canonical order, id + verbatim excerpt + temporal/sequence coordinates).
    - `source_artifact = {path, fingerprint}` from `ingested_assets`; `match_info` (`matched_on`/`matched_terms`) via deterministic field-content checks; `ranking_diagnostics` with method + `raw_bm25` / `weighted_substring_score` / long/short terms + `field_weights` + `rank`.
    - `RetrievalResult` carries `store_schema_version` + deterministic `store_revision`; empty results are legal; no retrieval cache; no LLM fallback; deterministic tie-break on `unit_rowid` ASC.
  - `src/knowledge/__init__.py` extended with M5-03 exports.
  - `tests/test_knowledge_retrieval.py`: 56 collected tests covering query validation/normalization, long-term FTS, multiple-long-term AND, 2-char & 1-char fallback + top_k, mixed long+short, FTS operator safety, all structured filters (incl. filter-before-limit), hydration, evidence ordering + both coordinate types, attribution/lineage, source artifact path/fingerprint, store revision, match info, BM25 lower-is-better, short-score higher-is-better, deterministic ties, zero result, no-LLM fallback, Unicode queries, SQL-injection safety, JSON round-trip, backend abstraction, and real C10 golden queries.
  - Real C10 retrieval (temp/test DB only): `Vulkan`/`RDNA`/`Thinking`/`27B` → video-only hits with populated evidence refs; `logitech`/`AGON`/`SMILEY` → album-only hits; `模型`/`速度` (2-char) → video hits via short fallback; `推理` (2-char, absent from corpus) → fallback runs with 0 hits; `Vulkan 模型` → mixed path requires both; structured filters behave correctly (video+`logitech` → 0 hits; album+`logitech` → hits; claim / not_checked filters pass through).
  - Full regression: **1134 passed, 10 skipped** (M5-02 baseline 1078 + 56 new retrieval tests; zero regressions).

### M5-04: Filtering, Ranking & Query Diagnostics (`DONE`)
- **Objective**: Deterministic, explainable ranking policy (`lexical-ranking-v1`), hardened query diagnostics, and ranking explainability — without a reranker and without changing M5-03 filter semantics.
- **Delivered**:
  - **QueryPlan** (frozen, JSON-safe `to_dict`/`from_dict`, `build_query_plan()`): `original_query`, `normalized_query`, `terms`, `long_terms`, `short_terms`, `retrieval_path` (`fts_trigram` / `substring_short` / `fts_trigram_with_short_filter`), `filters` (caller-provided only), `top_k`. Never exposes SQL.
  - **Ranking policy `lexical-ranking-v1`**: lexicographic key. FTS/mixed ASC `(evidence_only_tier, exact_statement_phrase DESC, statement_match DESC, entity_match DESC, topic_match DESC, term_coverage DESC, raw_bm25 ASC, unit_rowid ASC)`; short path keeps `weighted_substring_score` primary (higher better) with exact-phrase + coverage tie-breaks, `unit_rowid` ASC final. BM25 stays lower-is-better, never negated/normalized. Components preserved per-hit in `ranking_components`.
  - **Upgraded `RetrievalResult.diagnostics`**: `query_plan`, `filters_applied` (exact values, never inferred), `candidate_count_before_limit` (from a no-LIMIT candidate query, no payload pulls), `result_count`, `top_k`, `short_query_fallback`, `ranking_policy_version`, `limitations`.
  - **Field-match signals & term-coverage invariant**: per-hit `match_info` extended with `statement_match` / `entity_match` / `topic_match` / `evidence_match`, exact-phrase flags, `matched_term_count` / `total_term_count` / `term_coverage`, `evidence_only_match`. `check_retrieval_invariant()` + `RetrievalInvariantError` enforce the AND coverage invariant.
  - **No penalties / no bias**: no content-based meta-unit penalties; `extraction_confidence` and `verification_status` never boost ranking.
  - **Tests**: `tests/test_knowledge_ranking.py` (45 tests) covering the 38 spec areas (query plan long/short/mixed, filter diagnostics, candidate count, policy version, statement/entity/topic/evidence matches, evidence-only, exact phrase, term coverage, BM25/short directions, relative priority, tie-break, stability, confidence/verification non-bias, AND semantics, no fuzzy/no rewrite, JSON safety, compatibility, zero-result, real C10 audits). Real C10 audit confirms explainable ordering (statement/entity hits first, evidence-only last).
  - Full regression: **1179 passed, 10 skipped** (M5-03 baseline 1134 + 45 new ranking tests; zero regressions). M5-01/M5-02/FTS schema untouched; M4 untouched.

### M5-05: Retrieval Evaluation Harness & C10 Golden Queries (`TODO`)
- **Objective**: Deterministic golden-query fixtures over the real C10 assets and an evaluation runner.
- **Target Scope**:
  - Video queries: `Vulkan`, `RDNA`, `Thinking`, `27B` → video asset only, relevant KUs, EvidenceRefs attached.
  - Album queries: `logitech`, `AGON`, `SMILEY` → album asset only, correct KU IDs, no cross-asset corruption.
  - Assertions: correct canonical asset, relevant KU, verbatim evidence, filters work, deterministic ordering.
- **Deliverable**: `scripts/run_m5_05_evaluation.py`, `tests/test_m5_retrieval_evaluation.py`.

### M5-06: End-to-End Acceptance (`TODO`)
- **Objective**: Offline full regression, real C10 store build + query acceptance.
- **Target Scope**: build store from both C10 artifacts, run golden queries, verify counts/evidence/filters, no output mutation of M4 artifacts, regression baselines.
- **Deliverable**: `docs/M5_FINAL_ACCEPTANCE.md`.

---

## 3. Explicit Non-Goals for Milestone M5

1. **No LLM / answering**: retrieval hits only; no RAG, no citation generation, no answer synthesis.
2. **No embeddings / vector / dense / hybrid / reranker**: deferred to a future milestone; only the backend extension points are reserved.
3. **No M4 artifact modification**: canonical `knowledge_units.json` and all M4/M3 artifacts are read-only.
4. **No external engines**: PostgreSQL, Elasticsearch, Qdrant, Chroma, LanceDB are not introduced.
5. **No Obsidian / Markdown publishing**.
6. **No cross-machine concurrent writes**: single-writer semantics only.