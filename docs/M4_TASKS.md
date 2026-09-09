# Milestone M4: Unified Knowledge Model · Task Board

> **Milestone Status**: `STARTED`  
> **Target Goal**: `Grounded Evidence (M3 Output) -> Knowledge Extraction -> Canonical Knowledge Units (M4 Output)`  
> **Handoff Contract**: Cross-Agent / Cross-Harness Compatible (Gemini, OpenCode, GLM, Codex)

---

## 1. Task Progression Matrix

| Task ID | Task Title | Owner | Status | Dependencies | Target Deliverable |
| :--- | :--- | :---: | :---: | :--- | :--- |
| **M4-00** | **Contract Design & Reconciliation** | Current Agent | **`DONE`** | M3 Acceptance | `docs/M4_*.md` (Design & Contract Freeze) |
| **M4-01** | **Canonical Model & Domain Layer** | TBD | **`TODO`** | M4-00 | `src/knowledge/models.py`, `tests/test_knowledge_models.py` |
| **M4-02** | **Chunk-Level Extraction Pipeline** | TBD | **`TODO`** | M4-01 | `src/knowledge/extractor.py`, `tests/test_knowledge_extraction.py` |
| **M4-03** | **Cross-Chunk Deduplication & Merging** | TBD | **`TODO`** | M4-02 | `src/knowledge/merger.py`, `tests/test_knowledge_dedup.py` |
| **M4-04** | **Entity & Topic Attachment** | TBD | **`TODO`** | M4-03 | `src/knowledge/enrichment.py`, `tests/test_knowledge_enrichment.py` |
| **M4-05** | **Verification Contract & Audit Render** | TBD | **`TODO`** | M4-04 | `src/knowledge/render.py`, `tests/test_knowledge_render.py` |
| **M4-06** | **M4 End-to-End Acceptance** | TBD | **`TODO`** | M4-01 ~ M4-05 | Full offline regression & formal asset acceptance |

---

## 2. Detailed Task Breakdown

### M4-00: Contract Design & Reconciliation (`DONE`)
- **Objective**: Freeze Canonical KnowledgeUnit contract, schema, taxonomy, epistemic semantics, decoupled attribution, and task plan without modifying production code.
- **Deliverables**:
  - `docs/M4_KNOWLEDGE_MODEL_DESIGN.md`: Authoritative design specification (reconciled v1.1).
  - `docs/M4_HANDOFF.md`: Master handoff and cross-agent protocol.
  - `docs/M4_DECISIONS.md`: Architectural decisions log (Decisions 1-9).
  - `docs/M4_TASKS.md`: Task board and progression matrix.

### M4-01: Canonical Model & Domain Layer (`TODO`)
- **Objective**: Implement strongly typed domain models in `src/knowledge/models.py`.
- **Target Scope**:
  - `CanonicalKnowledgeUnit`, `KnowledgeUnitType`, `EvidenceReference`, `Attribution`, `EntityMention`, `ExtractionProvenance`.
  - Deterministic ID generator (`compute_knowledge_unit_id`).
  - Strict serialization and deserialization (`to_dict`, `from_dict`, JSON Schema validation).
  - Unit test suite: `tests/test_knowledge_models.py`.

### M4-02: Chunk-Level Extraction Pipeline (`TODO`)
- **Objective**: Implement chunk-level knowledge extraction consuming `evidence_chunks.json`.
- **Target Scope**:
  - Structured prompt engineering (`knowledge-extraction-v4.0`).
  - LLM backend driver integration (LM Studio, Ollama, OpenAI-compatible).
  - VRAM lifecycle orchestration via `src/knowledge/lifecycle.py`.
  - Cache hit and sub-second resume protocol based on chunk and prompt fingerprints.
  - Unit test suite: `tests/test_knowledge_extraction.py`.

### M4-03: Cross-Chunk Deduplication & Merging (`TODO`)
- **Objective**: Implement boundary overlap resolution and multi-chunk knowledge aggregation.
- **Target Scope**:
  - Exact evidence set deduplication.
  - Superset evidence absorption.
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
- **Objective**: Full end-to-end regression and verification against real C10 formal assets.
- **Target Scope**:
  - Full offline verification on real C10 Video and Album.
  - Regression testing across all test suites.
  - Author `docs/M4_FINAL_ACCEPTANCE.md`.
