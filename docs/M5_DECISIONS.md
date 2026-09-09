# Milestone M5: Knowledge Store & Retrieval Foundation · Architectural Decision Log

> **Milestone Status**: `IN_PROGRESS` (M5-00 = `DONE / SEALED`, M5-01 = `DONE`; M5-02 next)
> **Status**: APPROVED / ACTIVE
> **Context**: M4 is COMPLETE/SEALED (`knowledge_units.json` schema `knowledge-units-v1`). M5 builds a derived, rebuildable, queryable Knowledge Store with a lexical retrieval contract, offline and deterministic.

---

## Decision 1: M4 `knowledge_units.json` Is the Canonical Source of Truth
- **Context**: M4 produced per-asset canonical artifacts. A store must not become a competing authority that can silently diverge from the sealed units.
- **Decision**:
  - `M4 knowledge_units.json` == **Canonical Knowledge Source of Truth** (immutable).
  - `M5 Knowledge Store` == **Derived / Rebuildable Retrieval Store**.
  - The store can be deleted and fully rebuilt from the M4 artifacts.
  - The store has **no authority** to rewrite `statement`, change `verification_status`, change `attribution`, change `evidence`, or regenerate KU IDs.

---

## Decision 2: SQLite (with FTS5) Is the M5 v1 Storage Technology
- **Context**: The repo already uses stdlib `sqlite3` (collector/repository, downloader/job_store, media_adapter/adapter). Personal-knowledge scale does not warrant a server or vector DB.
- **Decision**:
  - Freeze **SQLite** for M5 v1: local-first, zero service, Python stdlib, portable, NAS-friendly, transactional, FTS5 built-in.
  - **Not introduced**: PostgreSQL, Elasticsearch, Qdrant, Chroma, LanceDB. Re-evaluated only when scale/quality demands (deferred).

---

## Decision 3: Store Path & Derived-Artifact Placement
- **Context**: A cross-asset store cannot live inside `data/processed/<single asset>/`; it is a derived artifact, not a source archive.
- **Decision**:
  - Stable store path: `data/knowledge/knowledge_store.sqlite3`.
  - `data/*` and `*.sqlite3*` are already gitignored; the DB is rebuildable from canonical artifacts.
  - Deleting the store never deletes `data/processed` or any M4/M3 artifact.

---

## Decision 4: Schema Versioning via `PRAGMA user_version`
- **Context**: Downstream code must know whether the on-disk schema matches the code; a silent mismatch corrupts queries.
- **Decision**:
  - `store_schema_version = "knowledge-store-v1"`; enforced via `PRAGMA user_version = 1`.
  - `store_meta` table records the human-readable schema string + policy + timestamps.
  - M5 v1 supports exactly `create`, `validate`, `rebuild`. No migration engine.
  - Incompatible `user_version` → **explicit FAIL / instruct rebuild**, never silent guess.

---

## Decision 5: Logical Tables and Over-Normalization Boundary
- **Context**: Units carry evidence, entities, topics, attribution, and lineage. Over-normalizing attribution/lineage adds schema surface without a v1 consumer.
- **Decision**:
  - Normalized tables: `store_meta`, `ingested_assets`, `knowledge_units`, `evidence_refs`, `entities`, `topics`.
  - `evidence_refs` preserves canonical order via `UNIQUE(knowledge_unit_id, ordinal)` and carries temporal/sequence coordinates.
  - **Attribution and lineage stay in `canonical_payload_json`** (not normalized) in v1; surfaced verbatim via `RetrievalHit`.

---

## Decision 6: Canonical Payload + Projection Single-Truth Rule
- **Context**: Two independent copies of a unit's truth (columns vs JSON) can diverge.
- **Decision**:
  - `knowledge_units.canonical_payload_json` is the **authoritative** unit inside the store (verbatim M4 unit dict).
  - All projection columns are **derived from** `canonical_payload_json` in a single ingestion parse — never written independently, so divergence is impossible.
  - `RetrievalHit` fields are read from `canonical_payload_json`, not from loose columns.

---

## Decision 7: Atomic Transactional Ingestion
- **Context**: A failed ingest must never leave a half-asset in the store.
- **Decision**:
  - `ingest_knowledge_document(...)`: one asset = one `BEGIN IMMEDIATE ... COMMIT`.
  - Body: validate artifact → compute fingerprint → upsert `ingested_assets` → replace units → replace evidence/entities/topics → refresh FTS → COMMIT.
  - Any failure → **ROLLBACK**; no partial asset rows, no partial FTS state.

---

## Decision 8: Idempotency & Deterministic Replace
- **Context**: Re-running ingestion must not duplicate rows; changed artifacts must not leave stale units.
- **Decision**:
  - Same `canonical_id` + same `source_artifact_fingerprint` → **NO-OP / cache hit** (no duplicate KU/EvidenceRef/Entity/Topic; `ingested_at` preserved).
  - Changed fingerprint → **deterministic replace**: delete the asset's derived rows (including FTS) and re-insert from the new artifact, all in one transaction.
  - `source_artifact_fingerprint` = canonical SHA-256 JSON of the complete `knowledge_units.json`.

---

## Decision 9: Deletion Semantics
- **Context**: Store removal must not be conflated with source deletion.
- **Decision**:
  - `remove_asset(canonical_id)` deletes **only** the asset's derived store rows.
  - It never touches `data/processed`, M4 artifacts, M3 evidence, or archive media.

---

## Decision 10: Rebuild Strategy
- **Context**: The store must be fully reconstructible; rebuild behavior on invalid artifacts must be explicit.
- **Decision**:
  - `rebuild_store(...)` drops derived tables and re-ingests all discovered canonical artifacts.
  - Discovery: `data/processed/*/knowledge/knowledge_units.json` with `schema_version == knowledge-units-v1` validated via `CanonicalKnowledgeUnitsDocument`.
  - Invalid asset → **fail-fast** (abort rebuild, report offending asset). No silent skips.

---

## Decision 11: Weighted FTS5 Fields (Option B′)
- **Context**: Indexing only statements loses evidence recall; indexing everything flat lets long excerpts drown statement ranking.
- **Decision**:
  - Index four **independent weighted FTS5 columns**: `statement` (highest), `entity_names` (medium), `topics` (medium), `evidence_excerpts` (lowest).
  - External-content FTS5 (`content='knowledge_units'`, `content_rowid='unit_rowid'`), `unicode61` tokenizer.
  - Ranking via `bm25()` with explicit column weights so evidence text never swamps statement matches.

---

## Decision 12: Retrieval ≠ Answering; No Embeddings in M5
- **Context**: M5 is a retrieval foundation; answer synthesis/RAG/embeddings are a separate future milestone.
- **Decision**:
  - M5 returns **retrieval hits** only; no LLM, no RAG prompt, no citations, no answer synthesis.
  - No embedding/vector/dense/hybrid/reranker in M5 v1.
  - `retrieval_method` is a first-class field and `RetrievalBackend` abstraction reserves extension points for future dense/hybrid backends — not implemented now.

---

## Decision 13: Retrieval Contract (`RetrievalQuery` / `RetrievalHit` / `RetrievalResult`)
- **Context**: An Agent needs knowledge together with provenance, not bare strings.
- **Decision**:
  - `RetrievalQuery`: `query_text`, `top_k`, optional filters `canonical_ids`, `unit_types`, `verification_statuses`, `topics`, `entity_names`.
  - `RetrievalHit`: rank + full canonical unit fields + entities/topics/attribution + verbatim `evidence_refs` + `source_artifact` reference + `match_info` + `ranking_diagnostics`.
  - `RetrievalResult`: query, `retrieval_method`, `store_schema_version`, `result_count`, hits, optional `store_revision`/`diagnostics`.

---

## Decision 14: Score Semantics — No Fake Relevance Probability
- **Context**: Lexical scores are not probabilities and must not be conflated with confidence or verification.
- **Decision**:
  - v1 records `retrieval_method = "lexical_fts5"`, `score_components = {"lexical": <bm25>}`, and `rank`.
  - Explicit rule: retrieval score ≠ truth probability ≠ extraction confidence ≠ verification status.
  - Future hybrid retrieval extends `score_components` without changing the envelope.

---

## Decision 15: Evidence Expansion Is Mandatory in the Default Contract
- **Context**: PKP's value is `Knowledge → Evidence → Source` traceability; the Agent must not chase evidence afterward.
- **Decision**:
  - `RetrievalHit.evidence_refs` is **always populated** from canonical payload (id + verbatim excerpt + temporal/sequence coordinates).
  - A future summary-only search endpoint is allowed as an addition, never a replacement.

---

## Decision 16: Verification Semantics — Filter, Never Bias
- **Context**: Auto-boosting `verified` or penalizing `not_checked` would leak epistemic judgments into ranking.
- **Decision**:
  - Ranking never automatically favors `verified` or penalizes `not_checked`.
  - `verification_status` is only a structured filter when the caller requests it; M5 never reinterprets statuses.

---

## Decision 17: Filters Are Structured Projections, Not Text Search
- **Context**: `topic = "GPU推理优化"` must hit the topic projection, not FTS substring noise.
- **Decision**:
  - All filters (`canonical_id`, `unit_type`, `verification_status`, `topics`, `entity_names`) query structured projection columns / tables.
  - No filter is implemented as a full-text substring search.

---

## Decision 18: Index Update Atomicity
- **Context**: Rows and FTS must never disagree (new rows + stale index).
- **Decision**:
  - Structured projections and the FTS index update in the **same transaction** per asset; no intermediate state where rows and index diverge.

---

## Decision 19: Single-Writer Semantics & NAS Note
- **Context**: A future NAS 24x7 + PC GPU topology may host the store on a NAS, but unsafe multi-machine concurrent SQLite writes over a network share are prohibited.
- **Decision**:
  - M5 v1 enforces **single-writer semantics**; exactly one process writes the store at a time.
  - NAS backup uses a single writer or file-level copy of a quiescent store.
  - A multi-client service/API ownership layer is deferred; **no server is implemented in M5**.

---

## Decision 20: Performance Target & Scale Policy
- **Context**: Personal knowledge scale; no premature distributed systems.
- **Decision**:
  - SQLite + FTS5 is the v1 target and is assessed as comfortable through 100k KUs and still workable at 1M with proper indexes and bounded `top_k`.
  - No distributed database for hypothetical millions of rows; revisit only when a real consumer demands it.