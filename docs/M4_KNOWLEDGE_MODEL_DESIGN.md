# Milestone M4: Unified Knowledge Model · Canonical Architecture & Contract Specification

> **Milestone Status**: `STARTED` (Design Phase M4-00 `DONE`)  
> **Repository Root**: `G:\local_pc_project\personal-knowledge-pipeline`  
> **Git Branch**: `feat/m4-unified-knowledge-model`  
> **Base Anchor**: `1f1c3b9a604d2fbdb9bb63606d6392aa893c080e` (`m3-media-integration-complete`)  
> **Authoritative Design Document for Milestone M4 (Reconciled Contract v1.1)**  
> **Handoff Contract**: Cross-Agent / Cross-Harness Compatible (Gemini 3.8 Flash, OpenCode + GLM 5.3, Codex)

---

## 1. Input Boundary & Immutable Preconditions

Milestone M4 operates strictly downstream of the Milestone M3 Evidence Layer. It consumes frozen, grounded evidence artifacts and does not perform upstream media acquisition, demuxing, speech transcription, or computer vision processing.

### 1.1 Authoritative Inputs
1. **Evidence Manifest**: `data/processed/<canonical_id>/evidence_manifest.json`
   - **Schema**: `evidence-manifest-v1`
   - Provides granular, 1:1 artifact-bound `EvidenceItem` records (speech segments, OCR visual lines, VLM visual descriptions).
   - Contains immutable snapshot of collector metadata (`published_at`, `first_seen_at`, `author_name`, `author_id`, `source_url`, `tags`).
   - Contains model execution provenance for ASR (faster-whisper) and Visual (PaddleOCR, VLM).
   - Global and item-level epistemic state: `verification_status = "not_checked"`.
2. **Evidence Chunks**: `data/processed/<canonical_id>/evidence_chunks.json`
   - **Schema**: `evidence-chunks-v1`
   - Provides deterministic, bounded-overlap processing windows (`EvidenceChunk`).
   - Preserves exact references to `evidence_ids` without splitting text.
   - Envelopes exact temporal ranges for videos and 1-indexed image sequence ranges for albums.

### 1.2 Strict Boundary Invariants (Non-Negotiable)
- **Zero Raw Media Re-processing**: No reading raw MP4 video files, no re-extracting audio, no re-running faster-whisper ASR, no re-running PaddleOCR.
- **Zero Network Ingestion**: No Douyin web requests, no browser automation, no F2 worker calls.
- **Zero Database Re-parsing**: No direct queries to `data/metadata.db`; all necessary source provenance is ingested strictly through `evidence_manifest.json`.
- **Formal Archive Immutability**: `archive/` remains 100% read-only.
- **Derived Workspace Isolation**: All M4 outputs are isolated under `data/processed/<canonical_id>/knowledge/` (e.g. `knowledge_units.json`, `knowledge.md`).

---

## 2. Core M4 Objective: Evidence → Canonical Knowledge Units

The central responsibility of Milestone M4 is transforming grounded, sequential evidence into structured, typed, attributed, and deduplicated knowledge units:

```text
[M3 Grounded Evidence Layer]
  - evidence_manifest.json (Atomic Evidence Items)
  - evidence_chunks.json   (Deterministic Windows)
             │
             ▼
[M4 Knowledge Extraction Subsystem]
  - Chunk-Level Extraction (LLM with Strict Grounding Prompt)
  - Overlap-Aware Deduplication & Merging
  - Entity Mention & Topic Tag Attachment
             │
             ▼
[Canonical Knowledge Units Artifact]
  - data/processed/<canonical_id>/knowledge/knowledge_units.json (Schema: knowledge-units-v1)
  - data/processed/<canonical_id>/knowledge/knowledge.md         (Internal Audit Representation)
```

### Out-of-Scope Responsibilities (Deferred to Subsequent Milestones)
- **RAG & Vector Embeddings**: No embedding generation, chunk vectorization, or Vector DB indexing.
- **Obsidian Vault Publishing**: No syncing or writing directly into external Obsidian vaults.
- **Global Knowledge Graph Visualization**: No global Neo4j, Cypher, or network graph exports.
- **Live Fact-Checking**: No search engine queries or external ground-truth validation.
- **Web UI & Services**: No HTTP servers, FastAPI endpoints, or Docker/NAS packaging.

---

## 3. Canonical KnowledgeUnit Schema Specification

The canonical schema represents a single, self-contained unit of extracted knowledge. It enforces **strict decoupling between knowledge semantics and speaker attribution**, binds excerpts directly to each evidence reference, and eliminates unneeded relationship fields from v1.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "CanonicalKnowledgeUnit",
  "type": "object",
  "required": [
    "knowledge_unit_id",
    "canonical_id",
    "unit_type",
    "statement",
    "evidence_refs",
    "attribution",
    "extraction_confidence",
    "verification_status",
    "extraction_provenance"
  ],
  "properties": {
    "knowledge_unit_id": {
      "type": "string",
      "description": "Deterministic identifier: ku_<sha256(schema_version + '|' + canonical_id + '|' + unit_type + '|' + canonical_ordered_eids + '|' + normalized_statement)[:16]>"
    },
    "canonical_id": {
      "type": "string",
      "description": "Canonical ID of the source media asset (e.g. douyin_7681603850364521734)"
    },
    "unit_type": {
      "type": "string",
      "enum": ["claim", "opinion", "observation", "procedure_step", "verification_question"],
      "description": "Semantic nature of the statement (decoupled from speaker/author attribution)"
    },
    "statement": {
      "type": "string",
      "description": "Normalized, concise declarative statement in plain Chinese"
    },
    "evidence_refs": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": ["evidence_id", "modality", "source_excerpt"],
        "properties": {
          "evidence_id": { "type": "string" },
          "modality": { "type": "string", "enum": ["speech", "visual_text", "visual_description"] },
          "chunk_id": { "type": ["string", "null"] },
          "source_excerpt": {
            "type": "string",
            "description": "Exact verbatim text excerpt copied directly from this cited evidence item"
          },
          "temporal": {
            "type": ["object", "null"],
            "properties": {
              "start": { "type": "number" },
              "end": { "type": "number" }
            }
          },
          "sequence": {
            "type": ["object", "null"],
            "properties": {
              "sequence_index": { "type": "integer" }
            }
          }
        }
      },
      "description": "Ordered references to atomic evidence items, with verbatim source excerpt bound per reference"
    },
    "attribution": {
      "type": "object",
      "required": ["attribution_status"],
      "properties": {
        "channel_creator": { "type": ["string", "null"] },
        "channel_creator_id": { "type": ["string", "null"] },
        "speaker_name": { "type": ["string", "null"] },
        "attribution_status": {
          "type": "string",
          "enum": [
            "source_author_explicit",
            "named_speaker",
            "quoted_third_party",
            "unverified_speaker",
            "visual_media",
            "system_derived"
          ]
        }
      },
      "description": "Source and speaker attribution decoupled from unit type, accounting for lack of diarization"
    },
    "extraction_confidence": {
      "type": "number",
      "minimum": 0.0,
      "maximum": 1.0,
      "description": "Extractor certainty in parsing, formulation, and schema adherence (NEVER truth probability)"
    },
    "verification_status": {
      "type": "string",
      "enum": ["not_checked", "verified", "contested", "unsupported"],
      "default": "not_checked",
      "description": "Epistemic verification state against real-world truth (default: not_checked)"
    },
    "entities": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["name", "category"],
        "properties": {
          "name": { "type": "string" },
          "category": { "type": "string" },
          "uri": { "type": ["string", "null"] }
        }
      },
      "description": "Named entity mentions attached to the unit (v1 extension point)"
    },
    "topics": {
      "type": "array",
      "items": { "type": "string" },
      "description": "Topical tags or classification labels (v1 extension point)"
    },
    "extraction_provenance": {
      "type": "object",
      "required": [
        "backend",
        "model",
        "prompt_version",
        "knowledge_schema_version",
        "temperature",
        "generated_at",
        "evidence_manifest_fingerprint",
        "evidence_chunks_fingerprint",
        "input_chunk_ids"
      ],
      "properties": {
        "backend": { "type": "string" },
        "model": { "type": "string" },
        "prompt_version": { "type": "string" },
        "knowledge_schema_version": { "type": "string" },
        "temperature": { "type": "number" },
        "generated_at": { "type": "string" },
        "evidence_manifest_fingerprint": { "type": "string" },
        "evidence_chunks_fingerprint": { "type": "string" },
        "input_chunk_ids": { "type": "array", "items": { "type": "string" } }
      },
      "description": "Complete operational audit trail of the extraction run"
    }
  }
}
```

---

## 4. Semantic Taxonomy & Decoupled Attribution

### 4.1 Knowledge Semantics (What the Statement Is)
M4 defines a compact set of **5 canonical unit types**:
1. **`claim`**: A declarative assertion about the objective external world that has truth conditions and can in principle be verified or refuted (e.g. *"Strix Halo Vulkan backend uses generic operators"*).
2. **`opinion`**: A subjective judgment, preference, attitude, qualitative appraisal, advice, or speculative prediction (e.g. *"Thinking mode should always be inspected before comparing benchmark speeds"*).
3. **`observation`**: A direct, sensory fact observed in the media evidence without argumentative interpretation (e.g. OCR text detected on an image, visible logo, or media structure fact).
4. **`procedure_step`**: Sequential operational instructions or executable commands.
5. **`verification_question`**: A **system-derived** inquiry highlighting an uncertainty, controversial claim, or unverified metric that warrants downstream fact-checking.

### 4.2 Speaker & Author Attribution (Who Said / Created It)
Knowledge type and speaker attribution are completely orthogonal dimensions:
- **`source_author_explicit`**: Speaker is explicitly proven / confirmed to be the channel creator (e.g. verified talking-head video with creator self-introduction).
- **`named_speaker`**: A specific named third party speaking in the content (e.g. a guest speaker introduced by name).
- **`quoted_third_party`**: The statement is a quotation attributed by the speaker to an external entity (e.g. *"AMD stated in their whitepaper that..."*).
- **`unverified_speaker`**: Default for speech ASR when speaker diarization is absent. We know the channel creator, but cannot guarantee the speaker is the creator.
- **`visual_media`**: Applicable to OCR text or visual graphics where no spoken voice is involved.
- **`system_derived`**: Applicable to `verification_question`, explicitly stating the question is synthesized by the pipeline, NOT asserted by the speaker.

### 4.3 Claim vs Opinion Invariant
- **Falsifiability Criterion**: A unit is a `claim` **if and only if** it asserts an empirical proposition that can be proven true or false by objective counter-evidence.
- **Tone Defense**: Even if an opinion is expressed with emphatic conviction (e.g. *"This is unquestionably the greatest GPU in history"*), it remains an **`opinion`** because it reflects a subjective value assessment.

---

## 5. Evidence Reference Contract & Per-Reference Excerpts

### 5.1 Elimination of Parallel Arrays
In legacy designs, `source_excerpts[]` and `evidence_refs[]` were parallel arrays, relying on fragile index synchronization.
M4 enforces **atomic encapsulation**: every `EvidenceRef` embeds its exact `source_excerpt`:

```json
"evidence_refs": [
  {
    "evidence_id": "ev_seg_000046",
    "modality": "speech",
    "chunk_id": "chk_000001",
    "source_excerpt": "Vulkan后端在StructHalo上量化矩阵走的是通用算子",
    "temporal": { "start": 109.42, "end": 112.42 }
  },
  {
    "evidence_id": "ev_seg_000047",
    "modality": "speech",
    "chunk_id": "chk_000001",
    "source_excerpt": "没有吃到RDNA的3.5协作矩阵的红利",
    "temporal": { "start": 112.42, "end": 115.12 }
  }
]
```

### 5.2 Canonical Evidence Ordering Invariant
- **Semantic Sequence**: Speech segments and album image sequences possess inherent temporal and sequential meaning.
- **No Arbitrary Lexical Sorting**: The order of `evidence_refs` **must strictly follow the canonical order** of `evidence_manifest.json` and `evidence_chunks.json`. Sorting evidence IDs alphabetically is strictly forbidden.

---

## 6. Deterministic Identity Strategy (`knowledge_unit_id`)

Knowledge units must not receive random UUIDs. Identifiers must be stable across repeated runs.

### 6.1 Payload Formulation
```python
def compute_knowledge_unit_id(
    schema_version: str,
    canonical_id: str,
    unit_type: str,
    canonical_ordered_evidence_ids: list[str],
    normalized_statement: str,
) -> str:
    # Preserve canonical order; do NOT sort lexically
    eids_str = ",".join(canonical_ordered_evidence_ids)
    cleaned_stmt = normalized_statement.strip().lower()
    payload = f"{schema_version}|{canonical_id}|{unit_type}|{eids_str}|{cleaned_stmt}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"ku_{digest}"
```
- **Stability**: Identical inputs and identical model outputs yield bit-for-bit identical `knowledge_unit_id`s.
- **Variation Handling**: If the normalized statement changes slightly across models, distinct candidate IDs are generated, which M4-03 merger will reconcile.

---

## 7. Epistemic Semantics & Extraction Confidence

M4 strictly preserves epistemic clarity:
1. **`extraction_confidence`**:
   - Quantifies model certainty that the statement is accurately extracted and categorized from the text.
   - **Does NOT represent real-world truth probability.**
2. **`verification_status`**:
   - Strictly defaults to `"not_checked"` for all newly extracted units.
   - Decoupled from extraction: only an external fact-checking process (out of scope for M4) can alter this status.
3. **`verification_question` Semantics**:
   - Not an author-asserted fact; represents an audit question generated by the system.
   - Enforces `attribution.attribution_status = "system_derived"` and `verification_status = "not_checked"`.

---

## 8. Observation Semantics (Tightened Scope)
- **`observation`** is strictly reserved for direct media-level observations (e.g. OCR text strings, visual layout elements, slide boundaries).
- Spoken statements within speech transcripts must be classified as `claim` or `opinion`. They must **not** be labeled as `observation` to bypass the epistemic verification contract.

---

## 9. Entity, Topic, and Relationship Boundaries
- **`entities`**: Retained as a structured v1 extension point (`list[EntityMention]`).
- **`topics`**: Retained as a structured v1 extension point (`list[str]`).
- **`relationships`**: **REMOVED from KnowledgeUnit v1**. Because M4 does not implement an inter-unit graph consumer, removing `relationships` avoids an undefined, unvalidated field. Inter-unit graph models belong to a future Knowledge Graph milestone.

---

## 10. Chunk Processing vs Provenance Invariant

> **An Evidence Chunk is a processing window, NOT a provenance entity.**

- Chunks exist solely to accommodate LLM token limits (`max_tokens: 1000`, `max_duration: 120s`).
- Chunks have boundary overlaps (`overlap_segments: 2`).
- Canonical provenance is anchored **strictly to `EvidenceItem.evidence_id`**. The `chunk_id` in `evidence_refs` is recorded purely as execution telemetry.

---

## 11. Cross-Chunk Deduplication Strategy

Because chunks overlap, extractors running on adjacent chunks will observe identical boundary segments.
M4 establishes a three-tier deduplication protocol in M4-03:
1. **Exact Evidence Set Match**: If Candidate A (Chunk $K$) and Candidate B (Chunk $K+1$) cite the exact same canonical `evidence_id`s with matching `unit_type`: retain the version with higher `extraction_confidence` or more complete statement, and discard the duplicate.
2. **Superset Overlap Absorption**: If Candidate B cites `[ev_46, ev_47, ev_48]` while Candidate A cited only `[ev_47, ev_48]`, and statements align, Candidate A is subsumed into Candidate B.
3. **Normalized Semantic Key Collapsing**: Units sharing identical canonical ID, unit type, and core entity/predicate components are collapsed into a single unit, merging their `evidence_refs` into a unified canonical sequence.

---

## 12. Extraction Provenance Contract

Every knowledge document records complete audit metadata:
```json
"extraction_provenance": {
  "backend": "lm_studio",
  "model": "qwen2.5-7b-instruct",
  "prompt_version": "knowledge-extraction-v4.0",
  "knowledge_schema_version": "knowledge-units-v1",
  "temperature": 0.1,
  "generated_at": "2026-09-09T12:00:00Z",
  "evidence_manifest_fingerprint": "943bac2c855632f796ac0db6c918ae9531d72722e5b5cd8ca6221f2a16899738",
  "evidence_chunks_fingerprint": "5b4004cf45f8703bb743caca5c5b77a46d6fb0eee2505fb42e00e7b5d553717a",
  "input_chunk_ids": ["chk_000001", "chk_000002", "chk_000003", "chk_000004"]
}
```
- Distinguishes manifest fingerprint from chunks fingerprint.
- Tracks specific `input_chunk_ids`.

---

## 13. Historical Legacy Code Reconciliation Matrix

| Component | Legacy State | M4 Status | Migration & Reconciliation Plan |
| :--- | :--- | :---: | :--- |
| **`src/knowledge/chunker.py`** | Transcript-only heuristic token chunker (`chunk_transcript`) | **`DEPRECATE`** | Completely superseded by `src/chunking/` (`evidence_chunks.json`). Phase out in M4. |
| **`src/knowledge/lifecycle.py`** | GPU VRAM manager for LM Studio (`lms ps`, `lms load`, `lms unload`) | **`KEEP`** | Retained for local RTX 4090 GPU orchestration, preventing VRAM competition with Whisper. |
| **`src/knowledge/service.py`** | Legacy `build_knowledge()`, manual metadata ingestion | **`ADAPT`** | Refactor into `src/knowledge/extractor.py` and `merger.py`, wired to M3 evidence artifacts. |
| **`src/backends/llm.py`** | Prompt v2.3 with legacy types (`author_claim`, `author_opinion`) | **`ADAPT`** | Upgrade prompt to `knowledge-units-v1` with decoupled types (`claim`, `opinion`). |
| **`src/render.py`** | Legacy `render_markdown` producing `knowledge.md` | **`ADAPT`** | Refactor to render `knowledge.md` as an internal audit document displaying typed KnowledgeUnits and evidence links. |
| **Legacy `author_claim`** | Legacy schema field in M1/M2 | **`MIGRATED`** | Maps to: `unit_type = "claim"` + `attribution.speaker_name = author` + `attribution.attribution_status = "source_author_explicit"`. |
| **Legacy `author_opinion`** | Legacy schema field in M1/M2 | **`MIGRATED`** | Maps to: `unit_type = "opinion"` + `attribution.speaker_name = author` + `attribution.attribution_status = "source_author_explicit"`. |

---

## 14. Real C10 Grounded Evidence Examples

Grounded strictly in physical disk artifacts:
- Video Manifest: `data/processed/douyin_7681603850364521734/evidence_manifest.json` (Fingerprint: `943bac2c855632f7...`)
- Video Chunks: `data/processed/douyin_7681603850364521734/evidence_chunks.json` (Fingerprint: `5b4004cf45f8703b...`)
- Album Manifest: `data/processed/douyin_7682038498466993905/evidence_manifest.json` (Fingerprint: `734713343813cb6e...`)
- Album Chunks: `data/processed/douyin_7682038498466993905/evidence_chunks.json` (Fingerprint: `bfb831b18620fb37...`)

### 14.1 Real C10 Video Example 1: `claim`
```json
{
  "knowledge_unit_id": "ku_3f91b7e408d2c19a",
  "canonical_id": "douyin_7681603850364521734",
  "unit_type": "claim",
  "statement": "Vulkan后端引擎在Strix Halo平台运行27B模型时采用通用矩阵算子，未利用RDNA 3.5协作矩阵加速，导致推理速度受限在十几Token左右。",
  "evidence_refs": [
    {
      "evidence_id": "ev_seg_000041",
      "modality": "speech",
      "chunk_id": "chk_000001",
      "source_excerpt": "用主线Vulkan版本的引擎来跑27B",
      "temporal": { "start": 101.58, "end": 104.12 },
      "sequence": null
    },
    {
      "evidence_id": "ev_seg_000042",
      "modality": "speech",
      "chunk_id": "chk_000001",
      "source_excerpt": "看到的十几Token的速度",
      "temporal": { "start": 104.12, "end": 105.82 },
      "sequence": null
    },
    {
      "evidence_id": "ev_seg_000046",
      "modality": "speech",
      "chunk_id": "chk_000001",
      "source_excerpt": "Vulkan后端在StructHalo上量化矩阵走的是通用算子",
      "temporal": { "start": 109.42, "end": 112.42 },
      "sequence": null
    },
    {
      "evidence_id": "ev_seg_000047",
      "modality": "speech",
      "chunk_id": "chk_000001",
      "source_excerpt": "没有吃到RDNA的3.5协作矩阵的红利",
      "temporal": { "start": 112.42, "end": 115.12 },
      "sequence": null
    },
    {
      "evidence_id": "ev_seg_000048",
      "modality": "speech",
      "chunk_id": "chk_000001",
      "source_excerpt": "所以速度自然起不来",
      "temporal": { "start": 115.60, "end": 117.52 },
      "sequence": null
    }
  ],
  "attribution": {
    "channel_creator": "老林说",
    "channel_creator_id": "7615965445866783802",
    "speaker_name": null,
    "attribution_status": "unverified_speaker"
  },
  "extraction_confidence": 0.95,
  "verification_status": "not_checked",
  "entities": [
    { "name": "Vulkan", "category": "software_engine", "uri": null },
    { "name": "Strix Halo", "category": "hardware_architecture", "uri": null },
    { "name": "RDNA 3.5", "category": "gpu_architecture", "uri": null }
  ],
  "topics": ["本地部署大模型", "统一内存", "AIMAX395", "strixhalo"],
  "extraction_provenance": {
    "backend": "mock_extractor",
    "model": "qwen2.5-7b-instruct",
    "prompt_version": "knowledge-extraction-v4.0",
    "knowledge_schema_version": "knowledge-units-v1",
    "temperature": 0.1,
    "generated_at": "2026-09-09T12:00:00Z",
    "evidence_manifest_fingerprint": "943bac2c855632f796ac0db6c918ae9531d72722e5b5cd8ca6221f2a16899738",
    "evidence_chunks_fingerprint": "5b4004cf45f8703bb743caca5c5b77a46d6fb0eee2505fb42e00e7b5d553717a",
    "input_chunk_ids": ["chk_000001"]
  }
}
```

### 14.2 Real C10 Video Example 2: `opinion`
```json
{
  "knowledge_unit_id": "ku_8d40a1b2c9e7f531",
  "canonical_id": "douyin_7681603850364521734",
  "unit_type": "opinion",
  "statement": "评估大模型端侧推理性能时不应仅看单一速度数值，应综合考察推理引擎类型、任务场景以及是否开启思考模式（Thinking），这三个变量可导致同模型速度产生数倍差异。",
  "evidence_refs": [
    {
      "evidence_id": "ev_seg_000154",
      "modality": "speech",
      "chunk_id": "chk_000004",
      "source_excerpt": "所以再看到任何的评测",
      "temporal": { "start": 314.64, "end": 317.22 },
      "sequence": null
    },
    {
      "evidence_id": "ev_seg_000155",
      "modality": "speech",
      "chunk_id": "chk_000004",
      "source_excerpt": "先问三个问题",
      "temporal": { "start": 317.22, "end": 318.40 },
      "sequence": null
    },
    {
      "evidence_id": "ev_seg_000156",
      "modality": "speech",
      "chunk_id": "chk_000004",
      "source_excerpt": "它用的是什么引擎",
      "temporal": { "start": 318.40, "end": 319.98 },
      "sequence": null
    },
    {
      "evidence_id": "ev_seg_000157",
      "modality": "speech",
      "chunk_id": "chk_000004",
      "source_excerpt": "用的是什么任务类型",
      "temporal": { "start": 319.98, "end": 321.30 },
      "sequence": null
    },
    {
      "evidence_id": "ev_seg_000158",
      "modality": "speech",
      "chunk_id": "chk_000004",
      "source_excerpt": "然后Thinking有没有开",
      "temporal": { "start": 321.30, "end": 323.10 },
      "sequence": null
    },
    {
      "evidence_id": "ev_seg_000161",
      "modality": "speech",
      "chunk_id": "chk_000004",
      "source_excerpt": "的速度差出好几倍",
      "temporal": { "start": 324.66, "end": 326.10 },
      "sequence": null
    }
  ],
  "attribution": {
    "channel_creator": "老林说",
    "channel_creator_id": "7615965445866783802",
    "speaker_name": null,
    "attribution_status": "unverified_speaker"
  },
  "extraction_confidence": 0.92,
  "verification_status": "not_checked",
  "entities": [
    { "name": "Thinking模式", "category": "model_feature", "uri": null }
  ],
  "topics": ["本地部署大模型", "评测标准"],
  "extraction_provenance": {
    "backend": "mock_extractor",
    "model": "qwen2.5-7b-instruct",
    "prompt_version": "knowledge-extraction-v4.0",
    "knowledge_schema_version": "knowledge-units-v1",
    "temperature": 0.1,
    "generated_at": "2026-09-09T12:00:00Z",
    "evidence_manifest_fingerprint": "943bac2c855632f796ac0db6c918ae9531d72722e5b5cd8ca6221f2a16899738",
    "evidence_chunks_fingerprint": "5b4004cf45f8703bb743caca5c5b77a46d6fb0eee2505fb42e00e7b5d553717a",
    "input_chunk_ids": ["chk_000004"]
  }
}
```

### 14.3 Real C10 Album Grounded Analysis (Visual `observation` & NOT PRESENT)
Physical inspection of C10 Image Album (`douyin_7682038498466993905`):
- Contains 3 WebP images with esports team graphics and brand labels (`logitech`, `INAMAX`, `AGON`, `SMILEY`).
- **`claim`**: **NOT PRESENT** in evidence (no declarative statements made in images).
- **`opinion`**: **NOT PRESENT** in evidence (no subjective viewpoints expressed).
- Valid visual `observation`:
```json
{
  "knowledge_unit_id": "ku_b2e59a1140df38c7",
  "canonical_id": "douyin_7682038498466993905",
  "unit_type": "observation",
  "statement": "图集第1张与第2张图片包含赞助商与品牌标识文字，经OCR识别包含'logitech'、'INAMAX'、'AGON'及'SMILEY'。",
  "evidence_refs": [
    {
      "evidence_id": "ve_img_001",
      "modality": "visual_text",
      "chunk_id": "chk_000001",
      "source_excerpt": "logitech\nINAMAX",
      "temporal": null,
      "sequence": { "sequence_index": 1 }
    },
    {
      "evidence_id": "ve_img_002",
      "modality": "visual_text",
      "chunk_id": "chk_000001",
      "source_excerpt": "lognach\nAGON\nSMILEY\n081",
      "temporal": null,
      "sequence": { "sequence_index": 2 }
    }
  ],
  "attribution": {
    "channel_creator": "姑妈有神王",
    "channel_creator_id": "1295683635130569",
    "speaker_name": null,
    "attribution_status": "visual_media"
  },
  "extraction_confidence": 0.98,
  "verification_status": "not_checked",
  "entities": [
    { "name": "Logitech", "category": "brand", "uri": null },
    { "name": "AGON", "category": "brand", "uri": null }
  ],
  "topics": ["英雄联盟", "g2"],
  "extraction_provenance": {
    "backend": "mock_extractor",
    "model": "qwen2.5-7b-instruct",
    "prompt_version": "knowledge-extraction-v4.0",
    "knowledge_schema_version": "knowledge-units-v1",
    "temperature": 0.1,
    "generated_at": "2026-09-09T12:00:00Z",
    "evidence_manifest_fingerprint": "734713343813cb6ef69db6e3d528c06c3367627e598bb48f480859e222d0be7b",
    "evidence_chunks_fingerprint": "bfb831b18620fb375f2075d1818e50356328e67bd0cdca4f43cc896ad1d2c71e",
    "input_chunk_ids": ["chk_000001"]
  }
}
```

---

## 15. Corrected Milestone M4 Task Breakdown

| Task ID | Task Title | Core Objective | Scope Boundary | Target Deliverables |
| :--- | :--- | :--- | :--- | :--- |
| **M4-00** | **Contract Design** | Freeze Canonical KnowledgeUnit contract, schema, and taxonomy | Docs only, no production code | `docs/M4_*.md` |
| **M4-01** | **Canonical Model & Domain Layer** | Implement `CanonicalKnowledgeUnit` domain dataclasses and serialization | Pydantic/dataclass schema, validation, deterministic ID calculation | `src/knowledge/models.py`, `tests/test_knowledge_models.py` |
| **M4-02** | **Chunk-Level Extraction Pipeline** | Extract structured knowledge units from `evidence_chunks.json` | LLM backend prompts, structured output parser, LM Studio lifecycle integration | `src/knowledge/extractor.py`, `tests/test_knowledge_extraction.py` |
| **M4-03** | **Cross-Chunk Deduplication & Merging** | Resolve boundary overlap duplicates and merge continuous units | Exact match dedup, superset resolution, normalized statement merge | `src/knowledge/merger.py`, `tests/test_knowledge_dedup.py` |
| **M4-04** | **Entity & Topic Attachment** | Attach recognized entity mentions and topic tags to units | Structured metadata attachment, vocabulary normalization | `src/knowledge/enrichment.py`, `tests/test_knowledge_enrichment.py` |
| **M4-05** | **Verification Contract & Audit Render** | Output `knowledge_units.json` and human-readable audit `knowledge.md` | Verification state contracts, human-readable audit render (NOT Obsidian) | `src/knowledge/render.py`, `tests/test_knowledge_render.py` |
| **M4-06** | **End-to-End Acceptance** | Full offline regression and C10 verification | End-to-end verification, regression baselines, freeze audit | `docs/M4_FINAL_ACCEPTANCE.md` |

---

## 16. Explicit Non-Goals in Milestone M4

1. **No External RAG Engine**: Embedding generation and vector databases (Chroma, Qdrant, LanceDB) are deferred to a dedicated retrieval milestone.
2. **No Obsidian Vault Synchronization**: `knowledge.md` is strictly an internal, human-readable audit representation; publishing to Obsidian vaults is deferred.
3. **No External Fact-Checking**: Network querying to Wikipedia, Google, or Baidu is forbidden. All units remain `verification_status = "not_checked"`.
4. **No Full Global Entity Knowledge Graph**: Graph database storage (Neo4j) and inter-unit graph relationships are out of scope.
