# Milestone M4: Unified Knowledge Model · Task Board

> **Milestone Target**: Unified, typed, deterministic knowledge extraction from grounded media evidence chunks.  
> **Working Branch**: `feat/m4-unified-knowledge-model`  
> **Status Matrix**: M4-00 = `DONE / SEALED` | M4-01 = `DONE / SEALED` | M4-02 = `DONE / SEALED` | M4-03 ~ M4-06 = `TODO`

---

## 1. Milestone M4 Progress Board

| Task ID | Task Title | Owner | Status | Dependencies | Target Deliverable |
| :--- | :--- | :---: | :---: | :--- | :--- |
| **M4-00** | **Contract Design & Lineage Reconciliation** | Sealed | **`DONE / SEALED`** | M3 Acceptance | `docs/M4_*.md` (Design & Contract Freeze) |
| **M4-01** | **Canonical Model & Domain Layer** | Sealed | **`DONE / SEALED`** | M4-00 | `src/knowledge/models.py`, `tests/test_knowledge_models.py` |
| **M4-02** | **Chunk-Level Extraction Pipeline** | Sealed | **`DONE / SEALED`** | M4-01 | `src/knowledge/extractor.py`, `tests/test_knowledge_extraction.py` |
| **M4-03** | **Cross-Chunk Deduplication & Merging** | NEXT AGENT | **`TODO`** | M4-02 | `src/knowledge/merger.py`, `tests/test_knowledge_dedup.py` |
| **M4-04** | **Entity & Topic Attachment** | TBD | **`TODO`** | M4-03 | `src/knowledge/enrichment.py`, `tests/test_knowledge_enrichment.py` |
| **M4-05** | **Verification Contract & Audit Render** | TBD | **`TODO`** | M4-04 | `src/knowledge/render.py`, `tests/test_knowledge_render.py` |
| **M4-06** | **M4 End-to-End Acceptance** | TBD | **`TODO`** | M4-01 ~ M4-05 | Full offline regression & formal asset acceptance |

---

## 2. Detailed Task Breakdown

### M4-00: Contract Design & Lineage Reconciliation (`DONE`)
- **Objective**: Freeze Canonical KnowledgeUnit contract, schema, source-neutral attribution, unit-aware lineage, overlap invariance, and grounded C10 examples without modifying production code.
- **Deliverables**:
  - `docs/M4_KNOWLEDGE_MODEL_DESIGN.md`: Authoritative design specification (reconciled v1.3).
  - `docs/M4_HANDOFF.md`: Master handoff and cross-agent protocol.
  - `docs/M4_DECISIONS.md`: Architectural decisions log (Decisions 1-15).
  - `docs/M4_TASKS.md`: Task board and progression matrix.

### M4-01: Canonical Model & Domain Layer (`DONE`)
- **Objective**: Implement core domain models and validation in Python.
- **Delivered**:
  - `src/knowledge/models.py`:
    - Enums: `UnitType`, `VerificationStatus`, `AttributionStatus`.
    - Value objects: `TemporalRange`, `SequenceRange`.
    - Evidence Reference: `EvidenceRef` (decoupled from chunk identity, multidimensional coordinate support).
    - Attribution: `AttributionInfo` (source-neutral, unverified speech ASR default).
    - Lineage & Provenance: `ExtractionProvenance`, `ExtractionLineage`.
    - Knowledge Unit: `CanonicalKnowledgeUnit` (deterministic ID, strict invariants, verification question invariant).
    - Document Container: `CanonicalKnowledgeUnitsDocument`.
    - Normalization & Helpers: `normalize_statement`, `compute_knowledge_unit_id`, `create_knowledge_unit`, `validate_observation_grounding`, `adapt_legacy_point`.
  - `tests/test_knowledge_models.py`: 36 unit tests covering all structural, validation, serialization, coordinate, and deterministic ID invariants.

### M4-02: Chunk-Level Extraction Pipeline (`DONE / SEALED`)
- **Objective**: Implement LLM-based structured knowledge extraction per chunk with unit-aware lineage.
- **Delivered**:
  - `src/knowledge/extractor.py`:
    - `RawKnowledgeCandidate`: dataclass for untrusted model proposals.
    - `GroundedChunkInputBuilder`: formats evidence items into strictly grounded prompts with untrusted-data boundary.
    - `CandidateValidator`: validates candidate existence in manifest, chunk boundaries, usable semantic payload, duplicate handling, canonical ordering restoration, and strict all-citations observation perceptual gating.
    - `EvidenceResolver`: copies `source_excerpt`, `temporal_range`, `sequence_range` verbatim from manifest.
    - `ChunkExtractionResult` & `ExtractionConfig`: caching, retry, candidate rejection audit, raw-response SHA-256, and non-secret configuration references.
    - `extract_chunk_candidates` & `extract_knowledge_candidates`: pipeline entries producing `knowledge_candidates.json` and `raw_extractions/<chunk_id>.json`.
    - Cache fingerprint includes the knowledge schema and all output-affecting prompt/generation contract inputs; it remains distinct from the asset extraction run ID.
    - Asset `extraction_run_id` is content-addressed from configuration plus canonical ordered chunk raw-response hashes and is shared by every chunk candidate.
  - `tests/test_knowledge_extraction.py`: 72 collected tests covering mock backend, semantic grounding, unresolved/empty evidence rejection, injection defense, coordinates, system-owned lineage, cache identity, run identity, and structural fixtures.
  - Real local model smoke test (`scripts/run_m4_02_real_smoke.py`):
    - LM Studio `qwen/qwen3-8b` (identifier `qwen3-8b`):
      - C10 Video: 4 chunks, 62 accepted candidates, 0 rejections, 0 violations, audit valid.
      - C10 Album: 1 chunk, 6 accepted candidates, 0 rejections, 0 violations, audit valid.
    - Final reconciliation reused existing raw caches without LLM calls: Video 62 accepted / 0 rejected; Album 6 accepted / 0 rejected.
    - Cache hit verified with zero backend calls and identical content-addressed run IDs.

### M4-03: Cross-Chunk Deduplication & Merging (`TODO` · NEXT AGENT START HERE)
- **Objective**: Deduplicate and merge knowledge units extracted across chunk boundaries, recording merge lineage.
- **Target Scope**:
  - Boundary overlap duplicate resolution (preserving overlap identity).

  - Merge lineage tracking (`input_chunk_ids: [chk1, chk2]`, `source_candidate_ids`).
  - Normalized statement merge.
  - Unit test suite: `tests/test_knowledge_dedup.py`.

### M4-04: Entity & Topic Attachment (`TODO`)
- **Objective**: Attach named entity mentions and topic tags to extracted knowledge units.
- **Target Scope**:
  - Extract entities mentioned in statements.
  - Inherit and normalize topics from source metadata.
  - Unit test suite: `tests/test_knowledge_enrichment.py`.

### M4-05: Verification Contract & Audit Render (`TODO`)
- **Objective**: Serialize canonical knowledge artifacts to disk and provide human-readable audit representation.
- **Target Scope**:
  - Verification state contract (`not_checked`, `verified`, `contested`, `unsupported`).
  - Generate `data/processed/<canonical_id>/knowledge/knowledge_units.json` (schema: `knowledge-units-v1`).
  - Generate internal audit markdown `data/processed/<canonical_id>/knowledge/knowledge.md` linking claims to exact evidence excerpts (explicit non-goal: NOT Obsidian publishing).
  - Unit test suite: `tests/test_knowledge_render.py`.

### M4-06: M4 End-to-End Acceptance (`TODO`)
- **Objective**: Execute end-to-end regression across all formal test assets (C10 Video & C10 Album).
- **Target Scope**:
  - Validate JSON schema conformance (`knowledge-units-v1`).
  - Verify deterministic IDs, canonical evidence ordering, unit-aware lineage, and audit rendering.
  - Deliverable: `docs/M4_FINAL_ACCEPTANCE.md`.
