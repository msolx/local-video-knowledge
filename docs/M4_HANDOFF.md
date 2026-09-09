# Milestone M4: Unified Knowledge Model · Master Handoff Protocol

> **Milestone Status**: `IN_PROGRESS` (M4-00 = `DONE`, M4-01 = `DONE`, M4-02 = `DONE`, M4-03 = `TODO`)  
> **Source Baseline**: Milestone M3 Sealed at Tag `m3-media-integration-complete` (`1f1c3b9a604d2fbdb9bb63606d6392aa893c080e`).  
> **Working Branch**: `feat/m4-unified-knowledge-model`

---

## 1. Handoff Overview

Milestone M4 establishes the **Unified Knowledge Model Layer** for `personal-knowledge-pipeline`. It ingests tamper-evident evidence chunks produced by Milestone M3 and synthesizes typed, source-neutral, deterministic Knowledge Units.

### Upstream M3 Grounded Deliverables Consumed:
- `data/processed/<canonical_id>/evidence_manifest.json` (Atomic Evidence items)
- `data/processed/<canonical_id>/evidence_chunks.json` (Windowed chunk partitions)

### M4 Deliverables Completed:
- `docs/M4_*.md`: Full schema, taxonomy, source-neutral attribution, observation contract, and lineage design (Decisions 1-15).
- `src/knowledge/models.py`: Canonical domain layer and JSON serialization (36 unit tests).
- `src/knowledge/extractor.py`: Grounded chunk extraction pipeline with untrusted candidate model, strict candidate validator, verbatim evidence resolver, cache fingerprinting, and LM Studio integration.
- `tests/test_knowledge_extraction.py`: 46 unit tests verifying all extraction, grounding, coordinate preservation, injection defense, and caching invariants.
- Real local model smoke test (`scripts/run_m4_02_real_smoke.py`):
  - Verified against local LM Studio `qwen/qwen3-8b` (RTX 4090).
  - C10 Video: 4 chunks, 62 accepted candidates, 0 rejections, 0 violations.
  - C10 Album: 1 chunk, 6 accepted candidates, 0 rejections, 0 violations.
  - Caching / idempotency verified (<0.02s on cache hit with identical IDs).

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
   - An `observation` unit strictly requires direct machine-perceptual evidence (`visual_text` OCR, `visual_description` VLM). Spoken descriptions alone cannot produce an `observation`. If an asset has only speech evidence, `observation` is strictly `NOT PRESENT`.
7. **Intermediate Artifacts Isolation**:
   - M4-02 writes per-chunk raw responses to `raw_extractions/<chunk_id>.json` and intermediate candidates to `knowledge_candidates.json`.
   - Final `knowledge_units.json` is deferred to M4-05 after M4-03 deduplication/merging and M4-04 enrichment.
8. **Relationships Removed from v1**:
   - `relationships` field is formally **DEFERRED**; no placeholder array in schema.

---

## 3. Grounded C10 Asset Baselines & Extraction Results

| Asset ID | Content Type | Chunks | Real Smoke Status | Accepted Candidates | Rejections | Audit Violations | Cache Hit Duration |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **`douyin_7681603850364521734`** | Formal Video | 4 chunks | SUCCESS | 62 | 0 | 0 | 0.013s |
| **`douyin_7682038498466993905`** | Formal Album | 1 chunk | SUCCESS | 6 | 0 | 0 | <0.010s |

---

## 4. Worktree State & Git Hygiene
- **Branch**: `feat/m4-unified-knowledge-model`
- **M4-02 Additions**:
  - `src/knowledge/extractor.py`
  - `src/knowledge/__init__.py`
  - `tests/test_knowledge_extraction.py`
  - `scripts/run_m4_02_real_smoke.py`
- **Zero M2/M3 Code Touched**: Files in `src/collector/`, `src/downloader/`, `src/media_adapter/`, `src/visual/`, `src/chunking/`, `src/provenance.py` remain completely untouched.
- **Regression Suite**: 825 passed, 10 skipped in full regression; 82/82 knowledge domain & extraction tests passed; 55/55 Worker tests passed.

---

## 5. NEXT_AGENT_START_HERE
- **Task**: `M4-03 · Cross-Chunk Deduplication & Merging`
- **Objective**: Ingest `knowledge_candidates.json` across chunk boundaries, deduplicate identical/overlapping candidates, merge normalized statements, and construct merged unit-level lineage (`input_chunk_ids: [chk1, chk2]`, `source_candidate_ids: [cand1, cand2]`).
- **Entry Points**:
  - `data/processed/<canonical_id>/knowledge/knowledge_candidates.json` (authoritative M4-02 candidate input)
  - `src/knowledge/models.py` (canonical domain definitions)
  - `src/knowledge/merger.py` (to be implemented in M4-03)
  - `tests/test_knowledge_dedup.py` (to be implemented in M4-03)

