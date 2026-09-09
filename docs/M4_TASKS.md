# Milestone M4: Unified Knowledge Model · Task Board

> **Milestone Target**: Unified, typed, deterministic knowledge extraction from grounded media evidence chunks.  
> **Working Branch**: `feat/m4-unified-knowledge-model`  
> **Status Matrix**: M4-00 = `DONE` | M4-01 = `DONE` | M4-02 ~ M4-06 = `TODO`

---

## 1. Milestone M4 Progress Board

| Task ID | Task Title | Owner | Status | Dependencies | Target Deliverable |
| :--- | :--- | :---: | :---: | :--- | :--- |
| **M4-00** | **Contract Design & Lineage Reconciliation** | Sealed | **`DONE`** | M3 Acceptance | `docs/M4_*.md` (Design & Contract Freeze) |
| **M4-01** | **Canonical Model & Domain Layer** | Current Agent | **`DONE`** | M4-00 | `src/knowledge/models.py`, `tests/test_knowledge_models.py` |
| **M4-02** | **Chunk-Level Extraction Pipeline** | NEXT AGENT | **`TODO`** | M4-01 | `src/knowledge/extractor.py`, `tests/test_knowledge_extraction.py` |
| **M4-03** | **Cross-Chunk Deduplication & Merging** | TBD | **`TODO`** | M4-02 | `src/knowledge/merger.py`, `tests/test_knowledge_dedup.py` |
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
  - `docs/M4_DECISIONS.md`: Architectural decisions log (Decisions 1-11).
  - `docs/M4_TASKS.md`: Task board and progression matrix.

### M4-01: Canonical Model & Domain Layer (`DONE`)
- **Objective**: Implement core domain models and validation in Python.
- **Delivered**:
  - `src/knowledge/models.py`:
    - Enums: `UnitType`, `VerificationStatus`, `AttributionStatus`.
    - Value objects: `TemporalRange`, `SequenceRange`.
    - Evidence Reference: `EvidenceRef` (decoupled from chunk identity).
    - Attribution: `AttributionInfo` (source-neutral, unverified speech ASR default).
    - Lineage & Provenance: `ExtractionProvenance`, `ExtractionLineage`.
    - Knowledge Unit: `CanonicalKnowledgeUnit` (deterministic ID, strict invariants, verification question invariant).
    - Document Container: `CanonicalKnowledgeUnitsDocument`.
    - Normalization & Helpers: `normalize_statement`, `compute_knowledge_unit_id`, `create_knowledge_unit`, `validate_observation_grounding`, `adapt_legacy_point`.
  - `tests/test_knowledge_models.py`: 35 unit tests covering all structural, validation, serialization, and deterministic ID invariants.

### M4-02: Chunk-Level Extraction Pipeline (`TODO` · NEXT AGENT START HERE)
- **Objective**: Implement LLM-based structured knowledge extraction per chunk with unit-aware lineage.
- **Target Scope**:
  - Ingest `evidence_chunks.json`.
  - Structured extraction prompt enforcing `knowledge-units-v1`.
  - Source-neutral attribution mapping and default `unverified_speaker` logic.
  - Unit lineage population (`input_chunk_ids: [chunk_id]`, `candidate_id`).
  - Integration with local LM Studio worker.
  - Unit test suite: `tests/test_knowledge_extraction.py`.

### M4-03: Cross-Chunk Deduplication & Merging (`TODO`)
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
