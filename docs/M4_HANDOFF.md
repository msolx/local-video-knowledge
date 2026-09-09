# Milestone M4: Unified Knowledge Model · Master Handoff Protocol

> **Milestone Status**: `STARTED` (Design Phase M4-00 Finalized)  
> **Source Baseline**: Milestone M3 Sealed at Tag `m3-media-integration-complete` (`1f1c3b9a604d2fbdb9bb63606d6392aa893c080e`).  
> **Working Branch**: `feat/m4-unified-knowledge-model`

---

## 1. Handoff Overview

Milestone M4 establishes the **Unified Knowledge Model Layer** for `personal-knowledge-pipeline`. It ingests tamper-evident evidence chunks produced by Milestone M3 and synthesizes typed, source-neutral, deterministic Knowledge Units.

### Upstream M3 Grounded Deliverables Consumed:
- `data/processed/<canonical_id>/evidence_manifest.json` (Atomic Evidence items)
- `data/processed/<canonical_id>/evidence_chunks.json` (Windowed chunk partitions)

---

## 2. Key Architecture Invariants & Contracts

1. **Zero Media Re-extraction**:
   - The M4 pipeline never re-extracts audio, never runs whisper/paddleocr, and never contacts external networks.
2. **Source-Neutral Attribution**:
   - Fields: `source_actor_name`, `source_actor_id`, `speaker_name`, `speaker_id`, `attribution_status`.
   - Generalizes across Douyin, Bilibili, YouTube, Web pages, Forums, PDF documents, and Xiaoheihe.
   - Standard undiarized speech ASR strictly defaults to `speaker_name = null`, `speaker_id = null`, and `attribution_status = "unverified_speaker"`.
3. **Observation Grounding Contract**:
   - An `observation` unit requires direct machine-perceptual evidence (`visual_text` OCR, `visual_description` VLM). Spoken descriptions alone cannot produce an `observation`. If an asset has only speech evidence, `observation` is strictly `NOT PRESENT`.
4. **Deterministic Identity**:
   - `knowledge_unit_id` is derived deterministically from SHA256 of `(schema_version, canonical_id, unit_type, canonical_ordered_eids, normalized_statement)`.
5. **Relationships Removed from v1**:
   - `relationships` field is formally **DEFERRED**; no placeholder array in schema.
6. **Internal Audit Markdown**:
   - `knowledge.md` is strictly an internal, readable audit document, not an Obsidian vault export.

---

## 3. Grounded C10 Asset Baselines (Physical Disk)

| Asset ID | Content Type | Media File Count | Evidence Item Count | Modalities Present | Grounded Knowledge Units Present |
| :--- | :--- | :---: | :---: | :--- | :--- |
| **`douyin_7681603850364521734`** | Formal Video | 1 MP4 | 184 items (`ev_seg_000001`~`ev_seg_000184`) | `speech` ONLY | `claim`, `opinion` (`observation`: NOT PRESENT) |
| **`douyin_7682038498466993905`** | Formal Album | 3 WebP images | 4 items (`ve_img_001`~`ve_img_003`, `ve_vlm_img_003`) | `visual_text`, `visual_description` | `observation` (`claim`, `opinion`: NOT PRESENT) |

---

## 4. Worktree State & Git Hygiene
- **Branch**: `feat/m4-unified-knowledge-model`
- **Zero Production Code Touched**: Files in `src/`, `tests/`, and `config/` remain completely untouched during M4-00.
