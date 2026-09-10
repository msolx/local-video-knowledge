# Milestone M4: Unified Knowledge Model · Task Board

> **Milestone Target**: Unified, typed, deterministic knowledge extraction from grounded media evidence chunks.  
> **Working Branch**: `feat/m4-unified-knowledge-model`  
> **Status Matrix**: M4-00 = `DONE / SEALED` | M4-01 = `DONE / SEALED` | M4-02 = `DONE / SEALED` | M4-03 = `DONE / SEALED` | M4-04 = `DONE` | M4-05 = `DONE` | M4-06 = `DONE`
> **C10 Historical Fixture Status**: `RECOVERED_WITH_INTERMEDIATE_PROVENANCE_LOSS` (2026-09-10; see `docs/M4_C10_ARTIFACT_INCIDENT_20260910.md`; canonical final preserved, historical M4 intermediate execution artifacts irreversibly lost, M4 code semantics SEALED)

---

## 1. Milestone M4 Progress Board

| Task ID | Task Title | Owner | Status | Dependencies | Target Deliverable |
| :--- | :--- | :---: | :---: | :--- | :--- |
| **M4-00** | **Contract Design & Lineage Reconciliation** | Sealed | **`DONE / SEALED`** | M3 Acceptance | `docs/M4_*.md` (Design & Contract Freeze) |
| **M4-01** | **Canonical Model & Domain Layer** | Sealed | **`DONE / SEALED`** | M4-00 | `src/knowledge/models.py`, `tests/test_knowledge_models.py` |
| **M4-02** | **Chunk-Level Extraction Pipeline** | Sealed | **`DONE / SEALED`** | M4-01 | `src/knowledge/extractor.py`, `tests/test_knowledge_extraction.py` |
| **M4-03** | **Cross-Chunk Deduplication & Merging** | Sealed | **`DONE / SEALED`** | M4-02 | `src/knowledge/merger.py`, `tests/test_knowledge_dedup.py` |
| **M4-04** | **Entity & Topic Attachment** | Complete | **`DONE`** | M4-03 | `src/knowledge/enrichment.py`, `tests/test_knowledge_enrichment.py` |
| **M4-05** | **Verification Contract & Audit Render** | Complete | **`DONE`** | M4-04 | `src/knowledge/render.py`, `tests/test_knowledge_render.py` |
| **M4-06** | **M4 End-to-End Acceptance** | Complete | **`DONE`** | M4-01 ~ M4-05 | `docs/M4_FINAL_ACCEPTANCE.md`, `scripts/run_m4_06_acceptance.py`, `tests/test_m4_acceptance.py` |

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

### M4-03: Cross-Chunk Deduplication & Merging (`DONE`)
- **Objective**: Deduplicate and merge knowledge units extracted across chunk boundaries, recording merge lineage.
- **Target Scope**:
  - Exact KU-ID boundary-overlap duplicate resolution, preserving complete lineage.
  - Deterministic `merged_knowledge_candidates.json` cache keyed by source artifact content, policy version, and knowledge schema version.
  - Same-ID canonical conflicts audited and excluded; non-exact/superset/statement merges deferred.
  - Offline unit test suite: `tests/test_knowledge_dedup.py`.

### M4-04: Entity & Topic Attachment (`DONE`)
- **Objective**: Attach named entity mentions and topic tags to extracted knowledge units.
- **Delivered**:
  - `src/knowledge/enrichment.py`:
    - `EnrichmentConfig`, `RawEnrichmentProposal`, `UnitEnrichmentResult`.
    - `GroundedEnrichmentInputBuilder`: untrusted-data-boundary system prompt with surface-grounding, bounded category vocabulary, topic policy, `input_ref` routing, and `/no_think` reasoning suppression.
    - Deterministic normalization (`normalize_surface`: NFKC + casefold + whitespace collapse) and `is_entity_surface_grounded` (substring support in statement or any cited excerpt; no fuzzy/embedding/alias/external completion).
    - `validate_entity` / `validate_topic` / `validate_proposal_against_unit`: category vocabulary enforcement, in-unit entity dedup by normalized name, topic length/whitespace/dedup policy (0–5, 2–32 chars).
    - `process_enrichment_batch`: input_ref routing (never response order), unknown/duplicate ref rejection, per-unit failure isolation.
    - `compute_enrichment_fingerprint` / `compute_merged_artifact_fingerprint`: deterministic cache identity covering merged artifact content, exact unit IDs, backend/model, prompt & policy versions, knowledge schema, response schema, temperature, generation config.
    - `enrich_units` / `enrich_merged_candidates_artifact` / `enrich_knowledge_candidates`: batching (default 10 units/request), mock/OpenAI-compatible backend reuse, intermediate artifact `enriched_knowledge_candidates.json` (schema `m4-enriched-candidates-v1`).
    - `audit_identity_preservation`: programmatic before/after verification that only `entities`/`topics` may change.
  - `tests/test_knowledge_enrichment.py`: 43 collected tests (grounding accept/reject, category validity, dedup/order, topic limits, malformed/empty/unknown/duplicate proposals, failure isolation, prompt-injection immutability, cache identity + invalidation, no-secret persistence, identity audit, real C10 video & album fixtures).
  - Real local model smoke test (`scripts/run_m4_04_real_smoke.py`):
    - LM Studio `qwen/qwen3-8b`: C10 Video 62 → 62 units (132 entity mentions, 97 topics, 0 rejected, 0 identity violations, 7 LLM calls); C10 Album 6 → 6 units (6 entity mentions incl. OCR surfaces `logitech`/`INAMAX`/`AGON`/`SMILEY`, 6 topics, 0 rejected, 0 identity violations, 1 LLM call).
    - Cache hit verified with 0 LLM calls and identical unit IDs.

### M4-05: Verification Contract & Audit Render (`DONE`)
- **Objective**: Serialize canonical knowledge artifacts to disk and provide human-readable audit representation.
- **Delivered**:
  - `src/knowledge/render.py`:
    - `RenderConfig`, `compute_enriched_artifact_fingerprint`, `compute_finalization_fingerprint`: deterministic cache identity over enriched artifact content, `knowledge_schema_version`, and render policy version.
    - `validate_verification_status`: only `not_checked`/`verified`/`contested`/`unsupported` accepted; illegal values fail validation. M4-05 never generates or recomputes verification states.
    - `build_final_document`: constructs `knowledge_units.json` (schema `knowledge-units-v1`) strictly via `CanonicalKnowledgeUnitsDocument`; units carried verbatim from M4-04; real M4-02 `extraction_provenance` copied verbatim (never fabricated).
    - `audit_finalization_identity`: programmatic per-unit comparison of ALL canonical fields (including `entities`/`topics`/`lineage`) between enriched and final; `identity_violation_count == 0` required.
    - `escape_source_excerpt` (blockquote + backslash/backtick/HTML escaping) and `render_audit_markdown` (`# Knowledge Audit` → Asset → Finalization → Summary (type & verification counts) → per-KU sections with type/statement/verification/confidence/source-actor-vs-speaker/entities/topics/evidence-excerpts/coordinates/lineage).
    - `finalize_knowledge_document`: filesystem pipeline writing `knowledge_units.json`, `knowledge.md`, and `knowledge_finalization.json` (wrapper/cache metadata); idempotent cache hit never rewrites `generated_at` and never re-invokes extractor/merger/enrichment/LLM. No model runtime is ever started or probed.
  - `tests/test_knowledge_render.py`: 44 collected tests (document validity, unit count identity, all 11 canonical fields unchanged, verification render for all four states without generating verification, source-actor vs speaker distinction, unknown/visual_media/system_derived attribution, temporal/sequence/both/neither coordinates, exact excerpt retention, Markdown injection safety, entity/topic render, stable unit & evidence ordering, zero-unit document, deterministic JSON & Markdown, cache hit, enriched-fingerprint and render-policy invalidation, invalid verification status rejection, canonical identity mismatch rejection, no-secret persistence, real C10 video & album fixtures).
  - Real C10 finalization (offline, no LLM):
    - C10 Video: 62 → 62 units, 0 identity violations, all `claim` / `not_checked`, 62 units with entities (132 mentions, 104 distinct), 62 units with topics (97 total, 89 distinct).
    - C10 Album: 6 → 6 units, 0 identity violations, all `claim` / `not_checked`, 6 entities (`logitech`, `INAMAX`, `lognach`, `AGON`, `SMILEY`, `081`), 6 topics (`brand mention`, `text mention`); OCR excerpts rendered verbatim as blockquotes.
    - Cache hit verified: repeated `finalize_knowledge_document` returns byte-identical `knowledge_units.json` / `knowledge.md`.

### M4-06: M4 End-to-End Acceptance (`DONE`)
- **Objective**: Execute end-to-end regression across all formal test assets (C10 Video & C10 Album).
- **Target Scope**:
  - Validate JSON schema conformance (`knowledge-units-v1`).
  - Verify deterministic IDs, canonical evidence ordering, unit-aware lineage, and audit rendering.
  - Deliverable: `docs/M4_FINAL_ACCEPTANCE.md`.
- **Delivered**:
  - `scripts/run_m4_06_acceptance.py`: read-only audit runner covering chain counts, fingerprint chain (candidates → merged → enriched → final), KU ID recomputation, full evidence grounding (excerpt/coords/order, unresolved-visual rejection), attribution (modality-based), observation gate, verification counts, entity surface grounding, topic policy, lineage traceability, cross-stage identity (M4-03→04 / M4-04→05), Markdown/JSON parity, qualitative samples, and classification markers. Writes a machine-readable summary to gitignored `data/acceptance/m4_06_acceptance_summary.json` (`knowledge_layer: false`).
  - `tests/test_m4_acceptance.py`: 33 tests covering the full acceptance contract (chain counts, fingerprint chain, stale/tampered artifact detection, KU ID recomputation, excerpt/coordinate grounding, unresolved-visual rejection, attribution, observation gate, entity grounding + alias rejection, topic bounds, lineage traceability, finalization identity, Markdown/JSON parity, zero-unit pipeline, and both real C10 fixtures).
  - Real C10 acceptance: Video 62→62→62→62 (fingerprint chain intact, 68/68 KU IDs recomputed with 0 mismatches, 144 evidence refs 0 violations, 132/132 entities grounded, 97 topics 0 violations, 0 orphan lineage); Album 6→6→6→6 (6 evidence refs 0 violations, 6/6 entities grounded, 6 topics). Identity audits 0 violations; Markdown parity 62/62 + 6/6.
  - Qualitative sample audit: 15 video units → A=13, B=2, C=0, D=0, E=0; 4/62 occasional advisory/procedural phrasing typed as `claim` (known limitation, extractor untouched); album 6/6 grounded with no brand-relationship inference.
  - Full regression: 986 passed, 10 skipped (M3 baseline 953 + 33 new, zero regressions).
  - Full report: `docs/M4_FINAL_ACCEPTANCE.md` → **ACCEPT (M4 COMPLETE with known limitations)**.
