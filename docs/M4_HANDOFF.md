# Milestone M4: Unified Knowledge Model · Master Handoff Protocol

> **Milestone Status**: `IN_PROGRESS` (M4-00 = `DONE / SEALED`, M4-01 = `DONE / SEALED`, M4-02 = `DONE / SEALED`, M4-03 = `DONE / SEALED`, M4-04 = `DONE`, M4-05 = `DONE`, M4-06 = `TODO`)
> **Source Baseline**: Milestone M3 Sealed at Tag `m3-media-integration-complete` (`1f1c3b9a604d2fbdb9bb63606d6392aa893c080e`).  
> **Working Branch**: `feat/m4-unified-knowledge-model`

---

## 1. Handoff Overview

Milestone M4 establishes the **Unified Knowledge Model Layer** for `personal-knowledge-pipeline`. It ingests tamper-evident evidence chunks produced by Milestone M3 and synthesizes typed, source-neutral, deterministic Knowledge Units.

### Upstream M3 Grounded Deliverables Consumed:
- `data/processed/<canonical_id>/evidence_manifest.json` (Atomic Evidence items)
- `data/processed/<canonical_id>/evidence_chunks.json` (Windowed chunk partitions)

### M4 Deliverables Completed:
- `docs/M4_*.md`: Full schema, taxonomy, source-neutral attribution, observation contract, and lineage design (Decisions 1-18).
- `src/knowledge/models.py`: Canonical domain layer and JSON serialization (36 unit tests).
- `src/knowledge/extractor.py`: Grounded chunk extraction pipeline with an untrusted candidate model, usable-semantic-payload enforcement, strict observation gating, verbatim evidence resolution, cache fingerprinting, content-addressed asset run lineage, and LM Studio integration.
- `tests/test_knowledge_extraction.py`: 72 collected tests verifying extraction, grounding, empty/unresolved evidence rejection, coordinate preservation, injection defense, system-owned lineage, caching, raw-response hashing, and asset-level run identity.
- Real local model smoke test (`scripts/run_m4_02_real_smoke.py`):
  - Verified against local LM Studio `qwen/qwen3-8b` (RTX 4090).
  - C10 Video: 4 chunks, 62 accepted candidates, 0 rejections, 0 violations.
  - C10 Album: 1 chunk, 6 accepted candidates, 0 rejections, 0 violations.
  - Final reconciliation reused the saved raw responses without inference: Video 62 accepted / 0 rejected; Album 6 accepted / 0 rejected. No Album raw candidate cited `ve_img_003` or `ve_vlm_img_003`.
  - Cache hits preserve the content-addressed run ID and invoke no backend.
- `src/knowledge/enrichment.py` (M4-04): Surface-grounded entity & topic enrichment pipeline (LLM untrusted proposer, deterministic validator, bounded category vocabulary, failure isolation, deterministic cache fingerprinting, batching).
- `tests/test_knowledge_enrichment.py`: 43 collected tests covering grounding acceptance/rejection, category validity, entity/topic dedup and ordering, topic limits, malformed/empty/unknown/duplicate proposals, per-unit failure isolation, prompt-injection immutability, cache identity and invalidation, no-secret persistence, identity audit, and real C10 fixtures.
- Real local model enrichment smoke test (`scripts/run_m4_04_real_smoke.py`):
  - Verified against local LM Studio `qwen/qwen3-8b` (RTX 4090).
  - C10 Video: 62 in → 62 out; 62 units with entities (132 mentions), 62 units with topics (97 topics), 0 rejected proposals, 0 identity violations.
  - C10 Album: 6 in → 6 out; 6 units with entities (6 mentions, OCR surfaces like `logitech`, `INAMAX`, `AGON`, `SMILEY`, `lognach`, `081`), 6 units with topics, 0 rejected proposals, 0 identity violations.
  - Cache hit verified: identical config returns in <0.01s with 0 LLM calls and identical unit IDs.
- `src/knowledge/render.py` (M4-05): Deterministic finalization & audit render (verification contract validation, `CanonicalKnowledgeUnitsDocument` construction, per-unit identity audit, blockquote-based Markdown safety, finalization fingerprint/cache, filesystem pipeline writing `knowledge_units.json` + `knowledge.md` + `knowledge_finalization.json`).
- `tests/test_knowledge_render.py`: 44 collected tests covering document validity, unit count identity, all 11 canonical fields unchanged, verification render for all four states (without generating verification), source-actor-vs-speaker attribution, unknown/visual_media/system_derived rendering, temporal/sequence/both/neither coordinates, exact excerpt retention, Markdown injection safety, entity/topic render, stable unit & evidence ordering, zero-unit document, deterministic JSON & Markdown, cache hit + invalidation, invalid verification status rejection, canonical identity mismatch rejection, no-secret persistence, and real C10 fixtures.
- Real C10 finalization (M4-05, offline, 0 LLM calls):
  - C10 Video: `knowledge_units.json` 62 units, `knowledge.md` ~59 KB, 0 identity violations, all `claim`/`not_checked`.
  - C10 Album: `knowledge_units.json` 6 units, `knowledge.md` ~4.5 KB, 0 identity violations, OCR excerpts rendered verbatim.
  - Cache hit verified: repeated runs return byte-identical artifacts without rewriting `generated_at`.

---

## 2. Key Architecture Invariants & Contracts

1. **Zero Media Re-extraction**:
   - The M4 pipeline never re-extracts audio, never runs whisper/paddleocr, and never contacts external networks.
2. **Untrusted LLM Security Boundary**:
   - The LLM acts solely as a candidate proposer. It is strictly barred from setting canonical IDs, coordinates, source excerpts, author identity, or verification status.
   - All canonical properties are synthesized and validated deterministically by application code.
3. **Source-Neutral Attribution**:
   - Fields: `source_actor_name`, `source_actor_id`, `speaker_name`, `speaker_id`, `attribution_status`.
   - Generalizes across Douyin, Bilibili, YouTube, Web pages, Forums, PDF documents, and Xiaoheihe.
   - Standard undiarized speech ASR strictly defaults to `speaker_name = null`, `speaker_id = null`, and `attribution_status = "unverified_speaker"`.
4. **EvidenceRef Decoupled from Chunk ID**:
   - `EvidenceRef` contains only `evidence_id`, `source_excerpt`, `temporal_range`, and `sequence_range`.
   - `chunk_id` is removed from `EvidenceRef` because chunks are processing windows, not evidence identities.
5. **Two-Tier Lineage Architecture**:
   - Document-level `extraction_provenance`: shared model, prompt, backend, and manifest fingerprints.
   - Unit-level `extraction_lineage`: `extraction_run_id`, `input_chunk_ids`, `candidate_id`, `source_candidate_ids`, `merge_strategy`.
6. **Observation Grounding Contract**:
   - Every candidate citation requires a real non-empty semantic payload. Empty OCR and unresolved/null/whitespace visual descriptions cannot ground any unit type.
   - Every citation supporting an `observation` must be usable direct machine-perceptual evidence (`visual_text`, resolved `visual_description`, supported `perceptual_metric`). Speech-only and mixed speech+visual observations are rejected.
7. **Intermediate Artifacts Isolation**:
   - M4-02 writes per-chunk raw responses to `raw_extractions/<chunk_id>.json` and intermediate candidates to `knowledge_candidates.json`.
   - Final `knowledge_units.json` is deferred to M4-05 after M4-03 deduplication/merging and M4-04 enrichment.
8. **Relationships Removed from v1**:
   - `relationships` field is formally **DEFERRED**; no placeholder array in schema.
9. **Cache and Run Identity Separation**:
   - Cache fingerprint selects a reusable inference artifact from exact inputs/config and includes `knowledge_schema_version`.
   - Asset `extraction_run_id` addresses the configuration fingerprint plus canonical ordered `(chunk_id, raw_response_sha256)` pairs; all chunks share it, and `generated_at` is excluded.
   - Per-chunk raw artifacts persist `cache_fingerprint`, `raw_response`, `raw_response_sha256`, first-generation time, and non-secret config references.
10. **M4-03 Exact Identity Merge**:
    - `merged_knowledge_candidates.json` is a deterministic intermediate artifact. Same `knowledge_unit_id` candidates merge only when all frozen canonical fields agree; their chunk and source-candidate lineage is unioned in first-appearance order.
    - Non-exact candidates remain separate. Same-ID conflicts are audited and excluded rather than silently resolved. M4-03 does not invoke an LLM or enrich entities/topics.
 11. **M4-04 Surface-Grounded Enrichment**:
    - The LLM only proposes `entities`/`topics`, keyed by batch-local `input_ref`; the application owns the KU→`input_ref` mapping and never lets the model touch `knowledge_unit_id`.
    - `entity_name` must have direct textual support in the unit statement or a cited excerpt (NFKC + casefold + whitespace collapse). No fuzzy/embedding/alias/external-knowledge completion.
    - Only `entities` and `topics` may change; `verification_status` stays `not_checked`, all frozen fields byte-identical. Per-unit failures preserve the original unit and are recorded in the wrapper audit.
    - `enriched_knowledge_candidates.json` (schema `m4-enriched-candidates-v1`) is an intermediate artifact, not the M4-05 `knowledge_units.json`.
12. **M4-05 Finalization & Audit Render**:
    - `knowledge_units.json` (schema `knowledge-units-v1`) is constructed via `CanonicalKnowledgeUnitsDocument` with units byte-identical to the enriched artifact; only wrapper/document metadata may change.
    - M4-05 performs no fact checking and never changes `verification_status`; legal states are only `not_checked`/`verified`/`contested`/`unsupported`, and the input state is preserved verbatim.
    - `knowledge.md` is an internal audit representation (NOT Obsidian publishing); it renders excerpts verbatim with deterministic escaping/blockquote safety, distinguishes source actor from speaker, and never implies evidence presence equals factual verification.
    - Finalization fingerprint = SHA-256 over enriched artifact content fingerprint + `knowledge_schema_version` + render policy version; cache hits are byte-identical and never rewrite `generated_at`. No LLM/runtime is started or probed.

---

## 3. Grounded C10 Asset Baselines & Extraction Results

| Asset ID | Content Type | Chunks | Real Smoke Status | Accepted Candidates | Rejections | Audit Violations | Cache Hit Duration |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **`douyin_7681603850364521734`** | Formal Video | 4 chunks | SUCCESS | 62 | 0 | 0 | 0.013s |
| **`douyin_7682038498466993905`** | Formal Album | 1 chunk | SUCCESS | 6 | 0 | 0 | <0.010s |

### M4-04 Enrichment Baselines (same local `qwen/qwen3-8b`, RTX 4090)

| Asset ID | Merged Units | Enriched Units | Units w/ Entities | Total Entity Mentions | Units w/ Topics | Total Topics | Rejected Proposals | Identity Violations | LLM Calls | Cache Hit |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`douyin_7681603850364521734`** | 62 | 62 | 62 | 132 | 62 | 97 | 0 | 0 | 7 | <0.01s |
| **`douyin_7682038498466993905`** | 6 | 6 | 6 | 6 | 6 | 6 | 0 | 0 | 1 | <0.01s |

### M4-05 Finalization Baselines (offline, deterministic, 0 LLM calls)

| Asset ID | Enriched Units | Final Units | Identity Violations | Unit Types | Verification Statuses | `knowledge.md` Size |
| :--- | :---: | :---: | :---: | :--- | :--- | :---: |
| **`douyin_7681603850364521734`** | 62 | 62 | 0 | claim × 62 | not_checked × 62 | ~59 KB |
| **`douyin_7682038498466993905`** | 6 | 6 | 0 | claim × 6 | not_checked × 6 | ~4.5 KB |

---

## 4. Worktree State & Git Hygiene
- **Branch**: `feat/m4-unified-knowledge-model`
- **M4-02 Additions**:
  - `src/knowledge/extractor.py`
  - `src/knowledge/__init__.py`
  - `tests/test_knowledge_extraction.py`
  - `scripts/run_m4_02_real_smoke.py`
- **M4-04 Additions**:
  - `src/knowledge/enrichment.py`
  - `tests/test_knowledge_enrichment.py`
  - `scripts/run_m4_04_real_smoke.py`
- **M4-05 Additions**:
  - `src/knowledge/render.py`
  - `tests/test_knowledge_render.py`
- **Zero M2/M3 Code Touched**: Files in `src/collector/`, `src/downloader/`, `src/media_adapter/`, `src/visual/`, `src/chunking/`, `src/provenance.py` remain completely untouched.
- **Final Reconciliation Targeted Suite**: 210 passed (`test_knowledge_models.py` + `test_knowledge_extraction.py` + `test_knowledge_dedup.py` + `test_knowledge_enrichment.py` + `test_knowledge_render.py`).
- **Full Regression**: 953 passed, 10 skipped; M2/M3 implementation remained untouched.

### Operator Note: Local LLM Runtime Preference

Future local LLM runtime preference:

1. Prefer llama.cpp at: `G:\llama.cpp`
2. Local model storage: `D:\LMmodel`
3. LM Studio is no longer the default runtime.
4. Tasks that do not require LLM inference must not start or probe either runtime.

This is an operator/runtime note only; it does not modify any M4 schema.

---

## 5. NEXT_AGENT_START_HERE
- **Task**: `M4-06 · M4 End-to-End Acceptance`
- **Objective**: Execute end-to-end regression across all formal test assets (C10 Video & C10 Album) and finalize the milestone with `docs/M4_FINAL_ACCEPTANCE.md`.
- **Entry Points**:
  - `data/processed/douyin_7681603850364521734/knowledge/knowledge_units.json` + `knowledge.md` (final canonical outputs)
  - `data/processed/douyin_7682038498466993905/knowledge/knowledge_units.json` + `knowledge.md`
  - `src/knowledge/render.py` (M4-05 finalization & audit render)
  - `docs/M4_KNOWLEDGE_MODEL_DESIGN.md` (sealed schema contract)
- **Scope**:
  - Validate JSON schema conformance (`knowledge-units-v1`).
  - Verify deterministic IDs, canonical evidence ordering, unit-aware lineage, and audit rendering.
  - Deliverable: `docs/M4_FINAL_ACCEPTANCE.md`.
- **Operator Note**: Future local LLM runtime preference is llama.cpp (`G:\llama.cpp`) with models in `D:\LMmodel`; LM Studio is no longer the default. Inference-free tasks must not start or probe either runtime.

