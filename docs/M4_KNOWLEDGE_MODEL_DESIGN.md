# Milestone M4: Unified Knowledge Model · Canonical Architecture & Contract Specification

> **Milestone Status**: `STARTED` (Design Phase M4-00)  
> **Repository Root**: `G:\local_pc_project\personal-knowledge-pipeline`  
> **Git Branch**: `feat/m4-unified-knowledge-model`  
> **Base Anchor**: `1f1c3b9a604d2fbdb9bb63606d6392aa893c080e` (`m3-media-integration-complete`)  
> **Authoritative Design Document for Milestone M4**  
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
  - Entity Mention & Classification Attachment
             │
             ▼
[Canonical Knowledge Units Artifact]
  - data/processed/<canonical_id>/knowledge/knowledge_units.json (Schema: knowledge-units-v1)
```

### Out-of-Scope Responsibilities (Deferred to Subsequent Milestones)
- **RAG & Vector Embeddings**: No embedding generation, chunk vectorization, or Vector DB indexing.
- **Obsidian Vault Publishing**: No syncing or writing directly into external Obsidian vaults.
- **Global Knowledge Graph Visualization**: No global Neo4j, Cypher, or network graph exports.
- **Live Fact-Checking**: No search engine queries or external ground-truth validation.
- **Web UI & Services**: No HTTP servers, FastAPI endpoints, or Docker/NAS packaging.

---

## 3. Canonical KnowledgeUnit Schema Proposal

The canonical schema represents a single, self-contained unit of extracted knowledge. It avoids the anti-pattern of an unbounded dictionary with dozens of nullable fields by organizing data into five cohesive sub-structures: **Identity & Type**, **Content Statements**, **Evidence Provenance**, **Attribution & Epistemics**, and **Graph/Classification Extensions**.

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
    "source_excerpts",
    "evidence_refs",
    "attribution",
    "confidence",
    "verification_status",
    "extraction_provenance"
  ],
  "properties": {
    "knowledge_unit_id": {
      "type": "string",
      "description": "Deterministic identifier: ku_<sha256(canonical_id + unit_type + sorted_evidence_ids + normalized_statement)[:16]>"
    },
    "canonical_id": {
      "type": "string",
      "description": "Canonical ID of the source media asset (e.g. douyin_7681603850364521734)"
    },
    "unit_type": {
      "type": "string",
      "enum": ["author_claim", "author_opinion", "observation", "procedure_step", "verification_question"],
      "description": "Strict semantic taxonomy of the knowledge unit"
    },
    "statement": {
      "type": "string",
      "description": "Normalized, concise declarative statement of the knowledge unit in plain Chinese"
    },
    "source_excerpts": {
      "type": "array",
      "items": { "type": "string" },
      "description": "Exact verbatim text segments extracted from the cited evidence items"
    },
    "evidence_refs": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": ["evidence_id", "modality"],
        "properties": {
          "evidence_id": { "type": "string" },
          "modality": { "type": "string", "enum": ["speech", "visual_text", "visual_description"] },
          "chunk_id": { "type": "string" },
          "temporal": {
            "type": "object",
            "properties": {
              "start": { "type": "number" },
              "end": { "type": "number" }
            }
          },
          "sequence": {
            "type": "object",
            "properties": {
              "sequence_index": { "type": "integer" }
            }
          }
        }
      },
      "description": "Direct, 1:N references to atomic evidence items supporting this knowledge unit"
    },
    "attribution": {
      "type": "object",
      "required": ["author_name", "attribution_status"],
      "properties": {
        "author_name": { "type": "string" },
        "author_id": { "type": ["string", "null"] },
        "attribution_status": {
          "type": "string",
          "enum": ["inferred_creator_speaking", "quoted_third_party", "unverified_speaker", "attributed_visual_text"]
        }
      },
      "description": "Source attribution accounting for absence of speaker diarization"
    },
    "confidence": {
      "type": "number",
      "minimum": 0.0,
      "maximum": 1.0,
      "description": "Model extraction confidence (syntactic/semantic extraction quality, NOT truth probability)"
    },
    "verification_status": {
      "type": "string",
      "enum": ["not_checked", "verified", "contested", "unsupported"],
      "default": "not_checked",
      "description": "Epistemic state indicating whether statement veracity has been independently checked"
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
      "description": "Named entities mentioned in the statement (structured extension point)"
    },
    "topics": {
      "type": "array",
      "items": { "type": "string" },
      "description": "Topical tags or classification labels"
    },
    "relationships": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["target_ku_id", "relation_type"],
        "properties": {
          "target_ku_id": { "type": "string" },
          "relation_type": { "type": "string", "enum": ["supports", "contradicts", "extends", "questions"] }
        }
      },
      "description": "Inter-unit knowledge graph links"
    },
    "extraction_provenance": {
      "type": "object",
      "required": ["backend", "model", "prompt_version", "generated_at", "source_manifest_fingerprint"],
      "properties": {
        "backend": { "type": "string" },
        "model": { "type": "string" },
        "prompt_version": { "type": "string" },
        "temperature": { "type": "number" },
        "generated_at": { "type": "string" },
        "source_manifest_fingerprint": { "type": "string" }
      },
      "description": "Full audit trail of the extraction process"
    }
  }
}
```

---

## 4. Knowledge Unit Types & Semantics

To avoid taxonomy bloat while capturing critical epistemological differences, M4 adopts a compact set of **5 core unit types**:

| Unit Type | Semantic Definition | Key Distinguishing Criteria | Verification Eligibility |
| :--- | :--- | :--- | :---: |
| **`author_claim`** | The author/speaker asserts a statement about the objective external world that possesses a truth value (true or false) in principle. | Empirical claims, benchmark results, performance measurements, causal assertions, architectural mechanisms. | **Yes** (Primary candidate for fact-checking) |
| **`author_opinion`** | The author/speaker expresses subjective evaluation, personal preferences, qualitative judgment, or speculative advice. | Value judgments, purchase recommendations, sentiment, personal satisfaction, aesthetic assessments. | **No** (Subjective, non-falsifiable) |
| **`observation`** | Direct, sensory recording of visible text, objects, or environmental conditions without analytical assertion. | OCR text on video frames / image slides, visible logos, layout structures, hardware labeling. | **Yes** (Verifiable against artifact images) |
| **`procedure_step`** | Concrete operational instruction, CLI command, configuration parameter, or sequential procedural step. | Tutorial actions, software deployment commands, recipe instructions, reproducible steps. | **Yes** (Verifiable through execution) |
| **`verification_question`** | An unresolved question, discrepancy, or critical inquiry triggered by the evidence that warrants verification. | Speaker-admitted uncertainties, suspicious metrics, controversial claims requiring independent audit. | **Yes** (Formulates fact-check query) |

### 4.1 Claim vs Opinion Invariant
- **Strict Distinction**: A statement is an `author_claim` **only if** it makes an assertion of empirical fact that could theoretically be proven false by counter-evidence (e.g., *"Vulkan backend does not use RDNA 3.5 cooperative matrix"*).
- **Tone Defense**: Even if an opinion is stated with high confidence or aggressive authority (e.g., *"This is definitely the worst GPU ever designed"*), it remains an **`author_opinion`**, because it represents a qualitative appraisal, not an empirical truth statement.

---

## 5. Evidence Reference Contract & Provenance Chain

Every `KnowledgeUnit` must be anchored to concrete evidence. **Unanchored knowledge units are strictly prohibited.**

```text
KnowledgeUnit (ku_...)
      │  references (1:N)
      ▼
EvidenceItem (ev_seg_... / ve_img_...)
      │  bound to (1:1)
      ▼
Formal Artifact (7681603850364521734.mp4 / 7682038498466993905_img_001.webp)
      │  stored in
      ▼
Formal Archive (archive/douyin/<cid>/)
```

### 5.1 Evidence Reference Properties
1. **Direct Granularity**: Cites exact atomic `evidence_id`s (e.g. `["ev_seg_000046", "ev_seg_000047", "ev_seg_000048"]`). It is forbidden to cite only the coarse `canonical_id`.
2. **Stable Sorting**: `evidence_refs` are sorted strictly by chronological temporal start (for speech) or sequence index (for image albums).
3. **Multi-Segment Support**: A knowledge unit may synthesize a claim spanning contiguous ASR segments or multiple related album images, preserving all cited IDs in sequence.
4. **Verbatim Dual Representation**:
   - `statement`: Normalized, grammatically clean declarative sentence produced by the model.
   - `source_excerpts`: Array of raw verbatim excerpts copied directly from the cited evidence items, allowing users to immediately audit model hallucination or distortion.

---

## 6. Chunk Processing vs Provenance Invariant

A fundamental architectural principle of Milestone M4:
> **Evidence Chunk is a processing window, NOT a provenance entity.**

### 6.1 Rationale
- Chunks (`EvidenceChunk`) exist solely to divide continuous media streams into LLM-manageable token context windows (`max_tokens: 1000`, `max_duration: 120s`).
- Chunks have intentional overlaps (`overlap_segments: 2`) to ensure context continuity at boundaries.
- If a KnowledgeUnit only referenced `chunk_id`, moving or re-parameterizing chunks would invalidate knowledge provenance.
- Therefore, the **canonical provenance unit is strictly `EvidenceItem`**. The `chunk_id` in `evidence_refs` is recorded solely as execution telemetry, not as primary identity.

---

## 7. Cross-Chunk Deduplication Strategy

Because chunks overlap at boundaries, extractors running independently on Chunk $K$ and Chunk $K+1$ will frequently observe the same boundary evidence segments (e.g. segments 47 and 48) and may generate identical or near-identical knowledge units.

M4 establishes a three-tier deduplication protocol:

```text
Extraction from Chunk K ──► Candidate KU A
                                  │
                                  ├──► 1. Exact Evidence Set Match ──► Merge/Discard Duplicate
                                  │
Extraction from Chunk K+1 ─► Candidate KU B
                                  ├──► 2. Overlapping Evidence + High Similarity ──► Statement Unification
                                  │
                                  └──► 3. Disjoint Evidence ──► Distinct Knowledge Units
```

### 7.1 Deduplication Rules
1. **Exact Evidence Set Duplication**: If Candidate A and Candidate B cite the exact same set of `evidence_ids` and possess the same `unit_type`:
   - Keep the unit with higher model extraction confidence or more complete normalized statement.
   - Discard the redundant copy.
2. **Superset Overlap Resolution**: If Candidate B cites `[ev_46, ev_47, ev_48]` while Candidate A (from previous chunk) only cited `[ev_47, ev_48]`, and their normalized statements align semantically:
   - Candidate B is recognized as the broader realization; Candidate A is subsumed into Candidate B.
3. **Normalized Semantic Key Collapsing**: Units sharing identical canonical ID, unit type, and core entity/predicate components are merged, combining their `evidence_refs` into a unified list without duplicate IDs.

---

## 8. Deterministic Identity Strategy (`knowledge_unit_id`)

Knowledge units must not receive random UUIDs. Random identifiers prevent idempotent re-runs and complicate cache validation.

### 8.1 Hash Formulation
```python
def compute_knowledge_unit_id(
    canonical_id: str,
    unit_type: str,
    evidence_ids: list[str],
    statement: str,
) -> str:
    sorted_eids = sorted(evidence_ids)
    normalized_stmt = statement.strip().lower()
    payload = f"{canonical_id}|{unit_type}|{','.join(sorted_eids)}|{normalized_stmt}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"ku_{digest}"
```
- **Invariance**: Re-running the pipeline with identical inputs and identical model outputs yields bit-for-bit identical `knowledge_unit_id`s.

---

## 9. Epistemic Semantics: "Extraction Confidence is NOT Truth"

M4 strictly preserves the epistemic integrity established in M3:

1. **Default State**:
   ```json
   "verification_status": "not_checked"
   ```
   Every newly extracted `KnowledgeUnit` is initialized to `"not_checked"`.
2. **Confidence vs Truth**:
   - `confidence`: Measures extraction quality (e.g. how clearly the statement reflects the source evidence and adheres to extraction schemas).
   - High confidence (e.g. `0.98`) means *"The model is very certain the author claimed this"*, **NEVER** *"This claim is 98% factually true"*.
3. **Verification States (Reserved Contract for Future Fact-Checking)**:
   - `"not_checked"`: Statement extracted as asserted by author; veracity unexamined.
   - `"verified"`: Cross-verified against authoritative external ground truth.
   - `"contested"`: Disputed or refuted by conflicting authoritative evidence.
   - `"unsupported"`: Checked and found to lack objective empirical basis.

---

## 10. Author Attribution Semantics & Diarization Defense

In informal short-form video and social media content, the channel owner (`author_name`) is not necessarily the sole speaker in the video (e.g. interviews, street vlogs, movie dubs, guest appearances).

### 10.1 Attribution Taxonomy
To prevent attributing guest statements to the channel creator, M4 enforces explicit attribution labeling:
- **`inferred_creator_speaking`**: Default when the video is a solo presentation / talking-head monologue by the creator.
- **`quoted_third_party`**: The statement is explicitly quoted by the author from another entity (e.g., *"AMD officially claimed that..."*).
- **`unverified_speaker`**: Used when multiple speakers or background dialogue exist and speech diarization is unavailable.
- **`attributed_visual_text`**: For OCR-derived statements (e.g. brand labels on equipment).

The schema requires recording both `author_name` (from channel metadata) and `attribution_status`.

---

## 11. Extraction Provenance Contract

Every extracted knowledge document and individual unit records complete operational provenance:
- `backend`: Inference runtime (e.g. `"lm_studio"`, `"ollama"`, `"openai_compatible"`).
- `model`: Exact model identifier (e.g. `"qwen2.5-7b-instruct"`, `"qwen3.8-27b"`).
- `prompt_version`: Prompt contract identifier (e.g. `"knowledge-extraction-v4.0"`).
- `temperature`: Sampling temperature used.
- `generated_at`: ISO-8601 UTC timestamp of execution.
- `source_manifest_fingerprint`: SHA-256 fingerprint of the input `evidence_manifest.json`.

---

## 12. Historical Knowledge Code Reconciliation Matrix

An audit of existing legacy code in `src/knowledge/`, `src/backends/llm.py`, and `src/render.py` reveals the following status:

| File / Component | Legacy State | M4 Status | Rationale & Action Plan |
| :--- | :--- | :---: | :--- |
| **`src/knowledge/chunker.py`** | Transcript-only heuristic token chunker (`chunk_transcript`) | **`DEPRECATE`** | Completely superseded by `src/chunking/` (`ChunkingPolicy`, `EvidenceChunk`, `evidence_chunks.json`). Will be phased out. |
| **`src/knowledge/lifecycle.py`** | GPU VRAM manager for LM Studio (`lms ps`, `lms load`, `lms unload`) | **`KEEP`** | Highly valuable for local GPU orchestration on RTX 4090. Preserves zero-VRAM-competition with Whisper. |
| **`src/knowledge/service.py`** | Legacy `build_knowledge()`, manual source schema, local-to-global merge | **`ADAPT`** | Refactor into `src/knowledge/extractor.py`. Replace manual metadata ingestion with M3 `evidence_manifest.json` and legacy chunking with `evidence_chunks.json`. |
| **`src/backends/llm.py`** | v2.3 prompt with `ALLOWED_TYPES = {"author_claim", "author_opinion", "verification_question"}` | **`ADAPT`** | Update schema and prompt to `knowledge-units-v1`. Retain underlying OpenAI-compatible and Ollama HTTP drivers. |
| **`src/render.py`** | Markdown renderer (`render_markdown`) producing `knowledge.md` | **`ADAPT`** | Adapt section renderers to consume `CanonicalKnowledgeUnit` structures and link evidence IDs to formal artifacts. |
| **`src/pipeline.py`** (knowledge stage) | Ingests raw `transcript.json` and `visual_transcript.json` directly | **`ADAPT`** | Wire knowledge stage to consume `CanonicalMediaAsset.evidence_manifest` and `evidence_chunks`. |

---

## 13. Grounded C10 Real Evidence Examples

Based strictly on physical inspection of real M3 output files:
- Video: `data/processed/douyin_7681603850364521734/evidence_manifest.json`
- Album: `data/processed/douyin_7682038498466993905/evidence_manifest.json`

### 13.1 Real C10 Video Example 1: `author_claim`
```json
{
  "knowledge_unit_id": "ku_3f91b7e408d2c19a",
  "canonical_id": "douyin_7681603850364521734",
  "unit_type": "author_claim",
  "statement": "Vulkan后端引擎在Strix Halo平台运行27B模型时采用通用矩阵算子，未利用RDNA 3.5协作矩阵（Cooperative Matrix）加速，导致显存推理速度受限在十几Token左右。",
  "source_excerpts": [
    "用主线Vulkan版本的引擎来跑27B",
    "看到的十几Token的速度",
    "别急着骂硬件",
    "那其实是引擎的电话板",
    "不是芯片的",
    "Vulkan后端在StructHalo上量化矩阵走的是通用算子",
    "没有吃到RDNA的3.5协作矩阵的红利",
    "所以速度自然起不来"
  ],
  "evidence_refs": [
    { "evidence_id": "ev_seg_000041", "modality": "speech", "chunk_id": "chk_000001", "temporal": { "start": 101.58, "end": 104.12 } },
    { "evidence_id": "ev_seg_000042", "modality": "speech", "chunk_id": "chk_000001", "temporal": { "start": 104.12, "end": 105.82 } },
    { "evidence_id": "ev_seg_000046", "modality": "speech", "chunk_id": "chk_000001", "temporal": { "start": 109.42, "end": 112.42 } },
    { "evidence_id": "ev_seg_000047", "modality": "speech", "chunk_id": "chk_000001", "temporal": { "start": 112.42, "end": 115.12 } },
    { "evidence_id": "ev_seg_000048", "modality": "speech", "chunk_id": "chk_000001", "temporal": { "start": 115.60, "end": 117.52 } }
  ],
  "attribution": {
    "author_name": "老林说",
    "author_id": "7615965445866783802",
    "attribution_status": "inferred_creator_speaking"
  },
  "confidence": 0.95,
  "verification_status": "not_checked",
  "entities": [
    { "name": "Vulkan", "category": "software_engine", "uri": null },
    { "name": "Strix Halo", "category": "hardware_architecture", "uri": null },
    { "name": "RDNA 3.5", "category": "gpu_architecture", "uri": null }
  ],
  "topics": ["本地部署大模型", "统一内存", "AIMAX395", "strixhalo"],
  "relationships": [],
  "extraction_provenance": {
    "backend": "mock_extractor",
    "model": "qwen2.5-7b-instruct",
    "prompt_version": "knowledge-extraction-v4.0",
    "temperature": 0.1,
    "generated_at": "2026-09-09T12:00:00Z",
    "source_manifest_fingerprint": "1a13943..."
  }
}
```

### 13.2 Real C10 Video Example 2: `author_opinion`
```json
{
  "knowledge_unit_id": "ku_8d40a1b2c9e7f531",
  "canonical_id": "douyin_7681603850364521734",
  "unit_type": "author_opinion",
  "statement": "评估大模型端侧推理性能时不应仅看单一速度数值，应综合考察推理引擎类型、任务场景以及是否开启思考模式（Thinking），这三个变量可导致同模型速度产生数倍差异。",
  "source_excerpts": [
    "所以再看到任何的评测",
    "先问三个问题",
    "它用的是什么引擎",
    "用的是什么任务类型",
    "然后Thinking有没有开",
    "这三个变量",
    "能让同一个模型",
    "的速度差出好几倍"
  ],
  "evidence_refs": [
    { "evidence_id": "ev_seg_000154", "modality": "speech", "chunk_id": "chk_000004", "temporal": { "start": 314.64, "end": 317.22 } },
    { "evidence_id": "ev_seg_000155", "modality": "speech", "chunk_id": "chk_000004", "temporal": { "start": 317.22, "end": 318.40 } },
    { "evidence_id": "ev_seg_000156", "modality": "speech", "chunk_id": "chk_000004", "temporal": { "start": 318.40, "end": 319.98 } },
    { "evidence_id": "ev_seg_000157", "modality": "speech", "chunk_id": "chk_000004", "temporal": { "start": 319.98, "end": 321.30 } },
    { "evidence_id": "ev_seg_000158", "modality": "speech", "chunk_id": "chk_000004", "temporal": { "start": 321.30, "end": 323.10 } },
    { "evidence_id": "ev_seg_000161", "modality": "speech", "chunk_id": "chk_000004", "temporal": { "start": 324.66, "end": 326.10 } }
  ],
  "attribution": {
    "author_name": "老林说",
    "author_id": "7615965445866783802",
    "attribution_status": "inferred_creator_speaking"
  },
  "confidence": 0.92,
  "verification_status": "not_checked",
  "entities": [
    { "name": "Thinking模式", "category": "model_feature", "uri": null }
  ],
  "topics": ["本地部署大模型", "评测标准"],
  "relationships": [],
  "extraction_provenance": {
    "backend": "mock_extractor",
    "model": "qwen2.5-7b-instruct",
    "prompt_version": "knowledge-extraction-v4.0",
    "temperature": 0.1,
    "generated_at": "2026-09-09T12:00:00Z",
    "source_manifest_fingerprint": "1a13943..."
  }
}
```

### 13.3 Real C10 Album Example: Visual `observation` & NOT PRESENT Analysis
Physical inspection of C10 Image Album (`douyin_7682038498466993905`):
- Contains 3 WebP images with esports team jersey graphics and sponsor brand marks (`logitech`, `INAMAX`, `AGON`, `SMILEY`).
- **`author_claim`**: **NOT PRESENT** in source evidence (no declarative statements made by author).
- **`author_opinion`**: **NOT PRESENT** in source evidence (no subjective viewpoints expressed).
- Valid visual `observation`:
```json
{
  "knowledge_unit_id": "ku_b2e59a1140df38c7",
  "canonical_id": "douyin_7682038498466993905",
  "unit_type": "observation",
  "statement": "图集第1张与第2张图片包含赞助商与品牌标识文字，经OCR识别包含'logitech'、'INAMAX'、'AGON'及'SMILEY'。",
  "source_excerpts": [
    "logitech",
    "INAMAX",
    "AGON",
    "SMILEY"
  ],
  "evidence_refs": [
    { "evidence_id": "ve_img_001", "modality": "visual_text", "chunk_id": "chk_000001", "sequence": { "sequence_index": 1 } },
    { "evidence_id": "ve_img_002", "modality": "visual_text", "chunk_id": "chk_000001", "sequence": { "sequence_index": 2 } }
  ],
  "attribution": {
    "author_name": "姑妈有神王",
    "author_id": "1295683635130569",
    "attribution_status": "attributed_visual_text"
  },
  "confidence": 0.98,
  "verification_status": "not_checked",
  "entities": [
    { "name": "Logitech", "category": "brand", "uri": null },
    { "name": "AGON", "category": "brand", "uri": null }
  ],
  "topics": ["英雄联盟", "g2"],
  "relationships": [],
  "extraction_provenance": {
    "backend": "mock_extractor",
    "model": "qwen2.5-7b-instruct",
    "prompt_version": "knowledge-extraction-v4.0",
    "temperature": 0.1,
    "generated_at": "2026-09-09T12:00:00Z",
    "source_manifest_fingerprint": "22cd298..."
  }
}
```

---

## 14. Milestone M4 Task Breakdown

| Task ID | Task Title | Core Objective | Scope Boundary | Target Deliverables |
| :--- | :--- | :--- | :--- | :--- |
| **M4-00** | **Contract Design** | Freeze Canonical KnowledgeUnit contract, schema, and taxonomy | Docs only, no production code | `docs/M4_*.md` |
| **M4-01** | **Canonical Model & Domain Layer** | Implement `CanonicalKnowledgeUnit` domain dataclasses and serialization | Pydantic/dataclass schema, validation, deterministic ID calculation | `src/knowledge/models.py`, `tests/test_knowledge_models.py` |
| **M4-02** | **Chunk-Level Extraction Pipeline** | Extract structured knowledge units from `evidence_chunks.json` | LLM backend prompts, structured output parser, LM Studio lifecycle integration | `src/knowledge/extractor.py`, `tests/test_knowledge_extraction.py` |
| **M4-03** | **Cross-Chunk Deduplication & Merging** | Resolve boundary overlap duplicates and merge continuous units | Exact match dedup, superset resolution, normalized statement merge | `src/knowledge/merger.py`, `tests/test_knowledge_dedup.py` |
| **M4-04** | **Entity & Classification Attachment** | Attach recognized entity mentions and topic tags to units | Structured metadata attachment, vocabulary normalization | `src/knowledge/enrichment.py`, `tests/test_knowledge_enrichment.py` |
| **M4-05** | **Knowledge Document & Markdown Render** | Output `knowledge_units.json` and human-readable `knowledge.md` | JSON serialization, clean markdown generation with evidence links | `src/knowledge/render.py`, `tests/test_knowledge_render.py` |
| **M4-06** | **End-to-End Acceptance** | Full offline regression and C10 verification | End-to-end verification, regression baselines, freeze audit | `docs/M4_FINAL_ACCEPTANCE.md` |

---

## 15. Summary of Explicit Non-Goals in Milestone M4

To maintain development velocity and isolation of responsibilities, the following areas are formally designated **NON-GOALS** for Milestone M4:
1. **No External RAG Engine**: Embedding generation and vector databases (Chroma, Qdrant, LanceDB) will be designed in a future retrieval milestone.
2. **No Obsidian Vault Synchronization**: Writing directly into user desktop Obsidian vaults is deferred to a dedicated publishing milestone.
3. **No External Fact-Checking**: Network querying to Wikipedia, Google, or Baidu is forbidden. All units remain `verification_status = "not_checked"`.
4. **No Full Global Entity Knowledge Graph**: Graph database storage (Neo4j) is out of scope; M4 only captures local inter-unit relationships within the same media asset.
