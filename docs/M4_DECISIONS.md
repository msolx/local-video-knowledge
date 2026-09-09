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

## Decision 3: Encapsulation of Source Excerpts in Evidence References
- **Context**: Parallel arrays `source_excerpts[]` and `evidence_refs[]` introduce fragility if array order gets misaligned.
- **Decision**:
  - `source_excerpt` is directly embedded in each `EvidenceRef` object alongside `evidence_id`, `chunk_id`, and `temporal` / `sequence` bounds.
  - Eliminates parallel arrays and guarantees 1:1 binding between cited evidence and raw quote.

---

## Decision 4: Canonical Evidence Ordering Invariant
- **Context**: Sorting evidence IDs alphabetically destroys temporal and sequential narrative context.
- **Decision**:
  - `evidence_refs` strictly maintains the canonical order from `evidence_manifest.json` and `evidence_chunks.json` (temporal start ascending for media, sequence order ascending for albums/docs).
  - Lexical sorting by `evidence_id` string is strictly forbidden.

---

## Decision 5: Deterministic KnowledgeUnit ID Strategy
- **Context**: Random UUIDs prevent idempotent re-execution and break cache validation.
- **Decision**:
  - `knowledge_unit_id` is computed deterministically:
    `ku_<sha256(schema_version + '|' + canonical_id + '|' + unit_type + '|' + canonical_ordered_eids + '|' + normalized_statement)[:16]>`.
  - Repeated extraction on unchanged inputs generates identical IDs.

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
