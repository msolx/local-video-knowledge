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
  - All source metadata (title, author, published_at, first_seen_at) is consumed directly from `evidence_manifest.json`.

---

## Decision 2: Decoupling of Unit Type from Speaker Attribution
- **Context**: Calling a unit `author_claim` or `author_opinion` falsely assumes the channel creator is verified to be the speaker, whereas ASR lacks speaker diarization.
- **Decision**:
  - Knowledge semantics (`unit_type`) and speaker attribution (`attribution`) are strictly orthogonal.
  - Canonical unit types: `claim`, `opinion`, `observation`, `procedure_step`, `verification_question`.
  - Attribution captures: `channel_creator`, `channel_creator_id`, `speaker_name`, and `attribution_status`.
  - Without diarization, speech defaults to `attribution_status = "unverified_speaker"`.

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
  - `evidence_refs` strictly maintains the canonical order from `evidence_manifest.json` and `evidence_chunks.json`. Lexical sorting is forbidden.

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

## Decision 8: Removal of Relationships from KnowledgeUnit v1
- **Context**: Knowledge graph edge modeling has no active consumer or schema defined in M4.
- **Decision**:
  - Remove `relationships` from KnowledgeUnit v1 to avoid sparse, undefined fields.
  - Keep `entities` and `topics` as structured extension points. Inter-unit graph linking is deferred to future work.

---

## Decision 9: Legacy Knowledge Code Reconciliation & Migration
- **Context**: Legacy codebase in `src/knowledge/` and `src/backends/llm.py` contains M1 extraction logic.
- **Decision**:
  - `src/knowledge/chunker.py`: **DEPRECATE** (superseded by `src/chunking/`).
  - `src/knowledge/lifecycle.py`: **KEEP** (VRAM orchestration for LM Studio).
  - `src/knowledge/service.py`: **ADAPT** into `src/knowledge/extractor.py` and `merger.py`.
  - `src/backends/llm.py`: **ADAPT** prompt and output schema to `knowledge-units-v1`.
  - `src/render.py`: **ADAPT** to render typed KnowledgeUnits in `knowledge.md` as an internal audit representation (NOT Obsidian).
  - Legacy `author_claim` / `author_opinion` map to canonical `claim` / `opinion` with explicit author attribution.
