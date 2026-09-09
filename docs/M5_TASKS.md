# Milestone M5: Knowledge Store & Retrieval Foundation · Task Board

> **Milestone Target**: A derived, rebuildable SQLite Knowledge Store over the canonical M4 `knowledge_units.json` artifacts, plus a stable lexical retrieval contract with full evidence expansion. Offline and deterministic; no LLM, no embeddings.
> **Working Branch**: `feat/m5-knowledge-store-retrieval`
> **Status Matrix**: M5-00 = `DONE / SEALED` | M5-01 = `TODO` | M5-02 = `TODO` | M5-03 = `TODO` | M5-04 = `TODO` | M5-05 = `TODO` | M5-06 = `TODO`

---

## 1. Milestone M5 Progress Board

| Task ID | Task Title | Owner | Status | Dependencies | Target Deliverable |
| :--- | :--- | :---: | :---: | :--- | :--- |
| **M5-00** | **Contract Design** | Sealed | **`DONE / SEALED`** | M4 Acceptance | `docs/M5_*.md` (Design & Contract Freeze) |
| **M5-01** | **Canonical Knowledge Store & Idempotent Ingestion** | — | **`TODO`** | M5-00 | `src/knowledge/store.py`, `tests/test_knowledge_store.py` |
| **M5-02** | **SQLite FTS5 Lexical / Metadata Indexing** | — | **`TODO`** | M5-01 | `src/knowledge/indexing.py`, `tests/test_knowledge_indexing.py` |
| **M5-03** | **Retrieval API & Evidence Expansion** | — | **`TODO`** | M5-01, M5-02 | `src/knowledge/retrieval.py`, `tests/test_knowledge_retrieval.py` |
| **M5-04** | **Filtering, Ranking & Query Diagnostics** | — | **`TODO`** | M5-03 | `src/knowledge/retrieval.py` (filter/rank layer), `tests/test_knowledge_retrieval_filters.py` |
| **M5-05** | **Retrieval Evaluation Harness & C10 Golden Queries** | — | **`TODO`** | M5-03, M5-04 | `scripts/run_m5_05_evaluation.py`, `tests/test_m5_retrieval_evaluation.py` |
| **M5-06** | **End-to-End Acceptance** | — | **`TODO`** | M5-01 ~ M5-05 | `docs/M5_FINAL_ACCEPTANCE.md` |

---

## 2. Detailed Task Breakdown

### M5-00: Contract Design (`DONE`)
- **Objective**: Freeze the Knowledge Store schema, retrieval contract, FTS strategy, ingestion/deletion/rebuild policies, and C10 golden-query fixtures without modifying production code.
- **Deliverables**:
  - `docs/M5_KNOWLEDGE_STORE_DESIGN.md`: Authoritative design specification (v1.0).
  - `docs/M5_HANDOFF.md`: Master handoff and cross-agent protocol.
  - `docs/M5_DECISIONS.md`: Architectural decisions log (Decisions 1-20).
  - `docs/M5_TASKS.md`: Task board and progression matrix.

### M5-01: Canonical Knowledge Store & Idempotent Ingestion (`TODO`)
- **Objective**: Implement the SQLite `knowledge-store-v1` schema and the atomic, idempotent ingestion pipeline over M4 `knowledge_units.json` artifacts.
- **Target Scope**:
  - `src/knowledge/store.py`: `create_store`, `validate_store`, `rebuild_store`, `ingest_knowledge_document`, `remove_asset`, store path `data/knowledge/knowledge_store.sqlite3`.
  - Tables: `store_meta`, `ingested_assets`, `knowledge_units`, `evidence_refs`, `entities`, `topics`; `canonical_payload_json` single-truth rule.
  - Atomic `BEGIN IMMEDIATE` asset transactions; failure → ROLLBACK, no partial asset.
  - Idempotency (same fingerprint → NO-OP) and deterministic replace (changed fingerprint → delete + re-insert same transaction).
  - Deletion semantics (derived rows only) and fail-fast rebuild.
- **Deliverable**: `src/knowledge/store.py`, `tests/test_knowledge_store.py`.

### M5-02: SQLite FTS5 Lexical / Metadata Indexing (`TODO`)
- **Objective**: Add the weighted FTS5 lexical index and keep it atomically in sync with structured rows.
- **Target Scope**:
  - `src/knowledge/indexing.py`: external-content `units_fts` over `statement`, `entity_names`, `topics`, `evidence_excerpts` with explicit `bm25()` column weights (statement highest, evidence lowest).
  - Atomic index refresh inside the ingestion transaction; no rows-vs-index divergence.
  - Metadata projections/indexes for `canonical_id`, `unit_type`, `verification_status`.
- **Deliverable**: `src/knowledge/indexing.py`, `tests/test_knowledge_indexing.py`.

### M5-03: Retrieval API & Evidence Expansion (`TODO`)
- **Objective**: Implement the retrieval contract with mandatory full evidence expansion.
- **Target Scope**:
  - `src/knowledge/retrieval.py`: `RetrievalQuery`, `RetrievalHit`, `RetrievalResult`, `RetrievalBackend` abstraction.
  - `FTS5RetrievalBackend` (`retrieval_method = "lexical_fts5"`); extension points reserved for future dense/hybrid.
  - `RetrievalHit` carries verbatim canonical units (from `canonical_payload_json`), attribution, entities, topics, evidence refs, `source_artifact`, `match_info`, `ranking_diagnostics`.
  - Retrieval ≠ answering: no LLM, no RAG, no citations, no answer synthesis.
- **Deliverable**: `src/knowledge/retrieval.py`, `tests/test_knowledge_retrieval.py`.

### M5-04: Filtering, Ranking & Query Diagnostics (`TODO`)
- **Objective**: Structured filters, BM25 ranking + score components, and diagnostics with the no-verification-bias rule.
- **Target Scope**:
  - Filters `canonical_ids`, `unit_types`, `verification_statuses`, `topics`, `entity_names` — all against structured projection columns, never FTS substring search.
  - `score_components = {"lexical": <bm25>}`; explicit non-equivalence of retrieval score vs confidence vs verification.
  - Ranking never auto-boosts `verified` or penalizes `not_checked`.
  - `top_k` bound; diagnostics (candidates scanned, filter stats).
- **Deliverable**: `src/knowledge/retrieval.py` (filter/rank layer), `tests/test_knowledge_retrieval_filters.py`.

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