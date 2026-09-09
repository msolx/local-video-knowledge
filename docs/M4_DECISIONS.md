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

## Decision 2: Canonical KnowledgeUnit Schema & Five-Pillar Organization
- **Context**: Avoid creating an unmanageable dictionary with dozens of sparse nullable fields while accommodating diverse knowledge types.
- **Decision**:
  - KnowledgeUnit schema is organized into five cohesive pillars:
    1. **Identity & Taxonomy**: `knowledge_unit_id`, `canonical_id`, `unit_type`.
    2. **Content Statements**: `statement` (normalized), `source_excerpts` (verbatim raw text).
    3. **Evidence Provenance**: `evidence_refs` (array of 1:N atomic `EvidenceItem` references).
    4. **Attribution & Epistemics**: `attribution` (author & status), `confidence`, `verification_status`.
    5. **Extensions**: `entities`, `topics`, `relationships`, `extraction_provenance`.

---

## Decision 3: Compact 5-Type Taxonomy & Strict Claim vs Opinion Boundary
- **Context**: Need a clear taxonomy distinguishing verifiable assertions from subjective commentary.
- **Decision**:
  - Adopt a minimal, robust 5-type taxonomy:
    * `author_claim`: Falsifiable assertions about the objective external world.
    * `author_opinion`: Subjective evaluations, preferences, recommendations, and predictions.
    * `observation`: Direct sensory recordings (OCR text, visual scene descriptions).
    * `procedure_step`: Sequential executable commands or tutorial actions.
    * `verification_question`: Critical inquiries or admitted uncertainties warranting fact-checking.
  - **Tone Defense**: Authoritativeness or confidence of speaker delivery does **not** convert an opinion into a claim. Subjective assessments remain `author_opinion`.

---

## Decision 4: Deterministic KnowledgeUnit ID Strategy
- **Context**: Random UUIDs prevent idempotent re-execution and break cache validation.
- **Decision**:
  - `knowledge_unit_id` is computed deterministically:
    `ku_<sha256(canonical_id + unit_type + sorted_evidence_ids + normalized_statement)[:16]>`.
  - Repeated extraction on unchanged inputs generates identical IDs.

---

## Decision 5: Separation of Processing Chunk vs Provenance Entity
- **Context**: Evidence chunks have window boundaries and overlap segments that may change if chunking policy is re-tuned.
- **Decision**:
  - Chunks (`EvidenceChunk`) are strictly **processing windows**, not provenance entities.
  - KnowledgeUnits bind directly to **`EvidenceItem.evidence_id`**.
  - `chunk_id` is recorded in `evidence_refs` solely as runtime processing telemetry.

---

## Decision 6: Multi-tier Overlap Deduplication & Merging Architecture
- **Context**: Overlapping boundary segments between Chunk $K$ and Chunk $K+1$ lead to duplicate extraction.
- **Decision**:
  - Implement a three-tier deduplication protocol:
    1. Exact evidence set duplicate removal.
    2. Superset/subset overlap resolution.
    3. Normalized semantic key collapsing.

---

## Decision 7: Author Attribution & Diarization Defense
- **Context**: ASR lacks speaker diarization; the channel creator (`author_name`) may not be the sole speaker in the video.
- **Decision**:
  - Enforce explicit `attribution_status`:
    * `inferred_creator_speaking` (default for creator solo videos).
    * `quoted_third_party` (author explicitly cites external entity).
    * `unverified_speaker` (multi-speaker dialogue without diarization).
    * `attributed_visual_text` (OCR text from visuals).
  - Never fabricate speaker identity.

---

## Decision 8: Decoupling of Extraction from Fact Verification & Graph Indexing
- **Context**: Milestone M4 focuses on local knowledge extraction from individual media assets.
- **Decision**:
  - All extracted units enforce `verification_status: "not_checked"`.
  - External search engine fact-checking, global graph databases (Neo4j), and vector databases (RAG) are explicitly deferred beyond M4.

---

## Decision 9: Legacy Knowledge Code Reconciliation
- **Context**: The existing `src/knowledge/` and `src/backends/llm.py` contain legacy M1 extraction logic.
- **Decision**:
  - `src/knowledge/chunker.py`: **DEPRECATE** (superseded by `src/chunking/`).
  - `src/knowledge/lifecycle.py`: **KEEP** (VRAM orchestration for LM Studio).
  - `src/knowledge/service.py`: **ADAPT** into `src/knowledge/extractor.py` and `merger.py`.
  - `src/backends/llm.py`: **ADAPT** prompt and output schema to `knowledge-units-v1`.
  - `src/render.py`: **ADAPT** to render typed KnowledgeUnits in `knowledge.md`.
