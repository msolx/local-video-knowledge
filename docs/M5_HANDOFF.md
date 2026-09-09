# Milestone M5: Knowledge Store & Retrieval Foundation · Master Handoff Protocol

> **Milestone Status**: `IN_PROGRESS` (M5-00 = `DONE / SEALED`; M5-01 = `DONE`; M5-02 = `TODO`, M5-03 ~ M5-06 = `TODO`)
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
- `docs/M5_DECISIONS.md`: Decisions 1-20.
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
9. **Weighted FTS5 (Option B′)**: columns `statement` (highest), `entity_names`
   (medium), `topics` (medium), `evidence_excerpts` (lowest); external-content
   FTS5 (`content_rowid='unit_rowid'`), `unicode61` tokenizer, `bm25()` weights.
   Index and rows update in the same transaction.
10. **Retrieval ≠ answering**: no LLM, no RAG, no citations, no answer
    synthesis. No embeddings/vector/dense/hybrid/reranker in M5; `retrieval_method`
    + `RetrievalBackend` reserve extension points only.
11. **Retrieval contract**: `RetrievalQuery` (`query_text`, `top_k`, filters:
    `canonical_ids`, `unit_types`, `verification_statuses`, `topics`,
    `entity_names`), `RetrievalHit` (rank + full canonical unit + verbatim
    `evidence_refs` + `source_artifact` + `match_info` + `ranking_diagnostics`),
    `RetrievalResult` (query, `retrieval_method`, `store_schema_version`,
    `result_count`, hits).
12. **Score semantics**: `retrieval_method = "lexical_fts5"`,
    `score_components = {"lexical": <bm25>}`; retrieval score ≠ truth
    probability ≠ extraction confidence ≠ verification status.
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
- **Zero M4 code modified**: `models.py`, `extractor.py`, `merger.py`,
  `enrichment.py`, `render.py` untouched. No FTS5, no search, no LLM, no
  runtime started or probed.
- **No production DB written**: all M5-01 ingestion/validation ran on temp/test
  SQLite DBs. The official `data/knowledge/knowledge_store.sqlite3` will be
  built at milestone acceptance (M5-06) or a later explicit step.

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

- **Task**: `M5-02 · SQLite FTS5 Lexical / Metadata Indexing`
- **Objective**: Add the weighted FTS5 lexical index over `statement`,
  `entity_names`, `topics`, `evidence_excerpts` (Option B′: independent weighted
  columns, external-content FTS5 with `content_rowid='unit_rowid'`, `unicode61`
  tokenizer, explicit `bm25()` column weights), and keep it atomically in sync
  with structured rows inside the ingestion transaction. Per
  `docs/M5_KNOWLEDGE_STORE_DESIGN.md` §11, §17 and `docs/M5_DECISIONS.md`
  Decisions 11, 18.
- **Do not begin M5-03** (retrieval API), M5-04, or later tasks.
- **Hard constraints**:
  - M5-01 `src/knowledge/store.py` is the ingestion base; the FTS index update
    must live in the same transaction (rows and index never diverge).
  - M4 canonical artifacts remain read-only. No LLM, no embeddings, no runtime
    probing.
  - Commit message: `feat(m5): add sqlite fts5 lexical indexing`.