# Milestone M4: Unified Knowledge Model · Architectural Decision Log

> **Milestone Status**: `STARTED` (Design Phase M4-00)  
> **Status**: APPROVED / ACTIVE  
> **Context**: Transitioning from Grounded Evidence (M3 Output) to Structured Canonical Knowledge Units (M4 Output).

---

## Decision 1: Input Boundary & Grounding Invariant
- **Context**: Milestone M3 established grounded evidence artifacts (`evidence_manifest.json` and `evidence_chunks.json`) bound 1:1 to formal archive files and collector metadata.
- **Decision**:
  - M4 treats the M3 Evidence Layer as its **sole authoritative input**.
  - M4 will **never** re-read raw video files, re-extract audio, re-run ASR/OCR, or query the live Douyin network or `data/metadata.db`.
  - All source metadata is consumed directly from `evidence_manifest.json`.

---

## Decision 2: Source-Neutral Attribution Architecture & Decoupling from Unit Type
- **Context**: Calling a unit `author_claim` or `author_opinion` falsely assumes the creator is verified to be the speaker, whereas ASR lacks speaker diarization. Furthermore, platform-specific fields like `channel_creator` break generalization across Bilibili, YouTube, Web pages, Forums, PDF documents, Xiaoheihe, etc.
- **Decision**:
  - Knowledge semantics (`unit_type`) and attribution (`attribution`) are strictly orthogonal.
  - Canonical unit types: `claim`, `opinion`, `observation`, `procedure_step`, `verification_question`.
  - Attribution schema is **source-neutral**:
    - `source_actor_name`: Publisher / uploader / author / account identity (e.g., Douyin author, YouTube channel, forum poster, document author).
    - `source_actor_id`: Platform or account identifier of the source actor.
    - `speaker_name`: Actual speaker within media, populated **only when evidence explicitly supports it**.
    - `speaker_id`: Speaker identifier (if diarized / identified, else `null`).
    - `attribution_status`: Enum representing attribution confidence.
  - **Default ASR Rule**: Ordinary ASR without speaker diarization strictly sets `speaker_name = null`, `speaker_id = null`, and `attribution_status = "unverified_speaker"`. Knowing `source_actor_name` does **not** allow assuming the speaker is the source actor.

---

## Decision 3: EvidenceRef Canonical Identity vs Processing Window (Chunk Removal)
- **Context**: Milestone M3 established that `Evidence Chunk` is an ephemeral processing window, whereas `EvidenceItem` is the durable provenance identity. Due to overlap windowing, a single `evidence_id` can simultaneously exist in multiple chunks. Forcing a single `chunk_id` into `EvidenceRef` binds processing mechanics into evidence identity and creates false provenance conflicts across overlap windows.
- **Decision**:
  - `EvidenceRef` describes **only** the atomic `EvidenceItem`:
    - `evidence_id`: Canonical stable identity.
    - `source_excerpt`: Verbatim quote from the evidence item payload.
    - `temporal_range`: Temporal bounding interval (if temporal media).
    - `sequence_range`: Sequence bounding index (if sequential media).
  - `chunk_id` is **strictly removed** from `EvidenceRef`.
  - Chunk associations belong exclusively to the extraction lineage layer.

---

## Decision 4: Canonical Evidence Ordering Invariant
- **Context**: Sorting evidence IDs alphabetically destroys temporal and sequential narrative context.
- **Decision**:
  - `evidence_refs` strictly maintains the canonical order from `evidence_manifest.json` and `evidence_chunks.json` (temporal start ascending for media, sequence order ascending for albums/docs).
  - Lexical sorting by `evidence_id` string is strictly forbidden.

---

## Decision 5: Deterministic KnowledgeUnit ID Strategy & Overlap Invariance
- **Context**: Random UUIDs prevent idempotent re-execution and break cache validation. If chunk IDs leak into the unit ID hash, identical extractions from overlap regions produce conflicting IDs.
- **Decision**:
  - `knowledge_unit_id` is computed deterministically:
    `ku_<sha256(schema_version + '|' + canonical_id + '|' + unit_type + '|' + canonical_ordered_eids + '|' + normalized_statement)[:16]>`.
  - **Overlap Invariance**: Chunk information is never included in the ID hash. If extraction on Chunk 1 and Chunk 2 yields identical unit_type, canonical ordered evidence IDs, and normalized statement, both produce the identical `knowledge_unit_id`.

---

## Decision 6: Extraction Confidence vs Factual Verification
- **Context**: Users and downstream systems may confuse extraction confidence with factual truth probability.
- **Decision**:
  - Rename `confidence` to `extraction_confidence`.
  - Define it strictly as model certainty in text parsing and formatting adherence.
  - `verification_status` defaults to `"not_checked"` and remains completely decoupled.

---

## Decision 7: System-Derived Nature of Verification Questions
- **Context**: `verification_question` is not an assertion made by the author/speaker.
- **Decision**:
  - `verification_question` is formally categorized as a system-derived follow-up inquiry.
  - Requires `attribution.attribution_status = "system_derived"` and `verification_status = "not_checked"`.
  - Must cite grounded `evidence_refs` and retain full unit-level `extraction_lineage`.

---

## Decision 8: Removal of Relationships from KnowledgeUnit v1 (DEFERRED)
- **Context**: Knowledge graph edge modeling has no active consumer or schema defined in M4.
- **Decision**:
  - Completely **REMOVE** `relationships` from KnowledgeUnit v1 proposal.
  - Retain only `entities` and `topics`.
  - Inter-unit relationships and cross-asset links are formally **DEFERRED** to a future Knowledge Graph milestone.
  - Do NOT leave a placeholder `relationships: []` field.

---

## Decision 9: Legacy Knowledge Code Reconciliation & Migration
- **Context**: Legacy codebase in `src/knowledge/` and `src/backends/llm.py` contains M1 extraction logic.
- **Decision**:
  - `src/knowledge/chunker.py`: **DEPRECATE** (superseded by `src/chunking/`).
  - `src/knowledge/lifecycle.py`: **KEEP** (VRAM orchestration for LM Studio).
  - `src/knowledge/service.py`: **ADAPT** into `src/knowledge/extractor.py` and `merger.py`.
  - `src/backends/llm.py`: **ADAPT** prompt and output schema to `knowledge-units-v1`.
  - `src/render.py`: **ADAPT** to render typed KnowledgeUnits in `knowledge.md` as an internal audit representation (NOT Obsidian).
  - Legacy `author_claim` / `author_opinion` map to canonical `claim` / `opinion`. Historical author attribution is migrated to `source_actor_explicit_speaker` only if evidence explicitly proves it, otherwise defaulting to `unverified_speaker`.

---

## Decision 10: Observation Grounding Contract & Speech Boundary
- **Context**: Conflating speech claims with empirical observations causes hallucinations (e.g. treating spoken phrase "这里可以看到 X" as proof of X's physical presence).
- **Decision**:
  - An `observation` unit **MUST** originate from direct perceptual / machine-observed evidence:
    - OCR visible text (`visual_text`)
    - VLM visual description (`visual_description`)
    - Structured log / benchmark output
    - Direct measurable media property
  - Spoken evidence ("这里可以看到 X") only proves that the speaker claimed/described X; it cannot alone support an `observation` that X actually exists.
  - If an asset contains only speech evidence (like C10 Video), `observation` is strictly **`NOT PRESENT`**.
  - For image albums where only OCR text is available, observations must strictly describe detected text ("第 N 张图 OCR 检测到文本 X") without hallucinating semantic categories ("赞助商", "战队", "海报") unless supported by VLM evidence.

---

## Decision 11: Two-Layer Extraction Provenance & Unit-Aware Lineage
- **Context**: Storing full LLM backend, model, prompt, and fingerprint metadata inside every KnowledgeUnit duplicates kilobytes of redundant text dozens of times. Conversely, omitting unit-level input chunk and candidate tracking makes it impossible to audit which chunk window or candidate generated a given unit after cross-chunk deduplication and merging.
- **Decision**:
  - Split lineage into two explicit tiers:
    1. **Document-Level `extraction_provenance`**: Preserves shared execution metadata (backend, model, prompt_version, knowledge_schema_version, temperature, fingerprints).
    2. **Unit-Level `extraction_lineage`**: Tracks the exact execution context for each unit (`extraction_run_id`, `input_chunk_ids`, `candidate_id`, `source_candidate_ids`, `merge_strategy`).
  - **Merge Lineage Contract (M4-03 Extension Point)**: When duplicate candidates from overlapping chunks are merged, the canonical unit retains the union of all `input_chunk_ids` and records all original `source_candidate_ids`, ensuring full traceability without losing extractor origin.

---

## Decision 12: Untrusted LLM Candidate Security Model & Deterministic System Synthesis
- **Context**: Relying on an LLM to directly output canonical IDs, exact timestamps, coordinates, verification status, or attribution invites hallucinations, prompt injection vulnerabilities, and coordinate drift.
- **Decision**:
  - The LLM acts strictly as an untrusted candidate proposer. It is only permitted to propose: `unit_type`, `statement`, `evidence_ids`, and `extraction_confidence`.
  - The application layer strictly owns and deterministically synthesizes all canonical attributes:
    - `knowledge_unit_id`: computed via canonical formula.
    - `source_excerpt`: copied verbatim from authoritative `EvidenceItem` payloads in `evidence_manifest.json`.
    - `temporal_range` & `sequence_range`: copied verbatim from manifest.
    - `attribution`: derived from asset metadata and cited evidence modality (ordinary ASR speech strictly defaults to `unverified_speaker`).
    - `verification_status`: hardcoded to `"not_checked"`.
    - `entities` & `topics`: empty lists `[]` (deferred to M4-04).
  - Any model proposal containing forbidden canonical fields is strictly rejected.

---

## Decision 13: Observation Perceptual Evidence Gate & Boundary Validation
- **Context**: Models frequently hallucinate `observation` units from spoken dialogue ("这里可以看到..."), violating the grounding contract.
- **Decision**:
  - Deterministic application-side validator enforces that if `unit_type == "observation"`, at least one cited evidence item must possess a perceptual modality (`visual_text`, `visual_description`, `perceptual_metric`).
  - Candidates citing only `speech` evidence with `unit_type == "observation"` are rejected with reason `observation_without_perceptual_evidence`.

---

## Decision 14: Intermediate Extraction Artifacts vs Final Canonical Artifacts
- **Context**: M4-02 extracts chunk-level candidates before cross-chunk deduplication and merging (which belongs to M4-03). Overwrite or premature emission of `knowledge_units.json` breaks milestone isolation.
- **Decision**:
  - M4-02 outputs intermediate auditable artifacts:
    - Per-chunk raw extractions: `data/processed/<canonical_id>/knowledge/raw_extractions/<chunk_id>.json`.
    - Intermediate candidates artifact: `data/processed/<canonical_id>/knowledge/knowledge_candidates.json` (schema: `m4-candidates-v1`).
  - Final `knowledge_units.json` (schema: `knowledge-units-v1`) is strictly deferred to M4-05.
  - Overlap duplicate units across chunk boundaries are preserved in `knowledge_candidates.json` for M4-03 deduplication.

---

## Decision 15: Cache Fingerprinting, Determinism & Local Offline Execution
- **Context**: LLM inference is computationally expensive and nondeterministic if unconstrained.
- **Decision**:
  - Chunk extraction is cached using a deterministic SHA-256 fingerprint over manifest fingerprint, chunks fingerprint, chunk evidence IDs, backend, model, prompt version, temperature, and token limit.
  - Subsequent executions with matching fingerprints return cached results in <0.02s without invoking LLM inference.
  - Zero external network requests are permitted; execution utilizes local LM Studio / OpenAI-compatible runtime or deterministic mock backend.
