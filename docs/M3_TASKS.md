# Milestone M3: Media Knowledge Integration · Task Board

> **Milestone Status**: `STARTED`  
> **Target Goal**: `Formal Local Asset (M2 Output) -> Media Processing -> Grounded Evidence (M3 Output)`  
> **Handoff Contract**: Cross-Agent / Cross-Harness Compatible (Gemini, OpenCode, GLM, Codex)

---

## 1. Task Progression Matrix

| Task ID | Task Title | Owner | Status | Dependencies | Target Deliverable |
| :--- | :--- | :--- | :---: | :--- | :--- |
| **M3-01** | **CanonicalMediaAssetAdapter** | Current Agent | **`DONE`** | M2 D07 Manifest Contract | `src/media_adapter/`, Unit Tests, Integration Smoke |
| **M3-02** | **Video / ASR Integration** | Current Agent | **`DONE`** | M3-01 | Adapter -> `pipeline.py` Audio/ASR flow without muxing |
| **M3-03** | **Image Album OCR/VLM Integration** | Current Agent | **`DONE`** | M3-01 | Multi-image visual inspection pipeline |
| **M3-04** | Metadata & Provenance Binding | TBD | **`TODO`** | M3-01, M3-02, M3-03 | Grounded provenance binding across media + collector DB |
| **M3-05** | Long Media Chunking | TBD | **`TODO`** | M3-02 | Hierarchical segment chunking & merge validation |
| **M3-06** | M3 End-to-End Acceptance | TBD | **`TODO`** | M3-01 ~ M3-05 | Full offline regression & formal asset acceptance |

---

## 2. Detailed Task Breakdown

### M3-01: CanonicalMediaAssetAdapter (`DONE`)
- **Objective**: Provide a deterministic, read-only adapter that loads M2 D07 `asset_manifest.json` from formal archive directories and constructs a canonical in-memory domain representation (`CanonicalMediaAsset`) consumable by downstream media processors.
- **In Scope**:
  - Deterministic parsing of video and image album formal assets.
  - Strict file existence and optional SHA-256 validation against manifest commitments.
  - Ordered album image sequences (1-indexed sequence sorting).
  - Optional BGM / audio track discovery (never assuming BGM is present).
  - Source provenance retention (`task_id`, `scope_id`, `source_sync_run_id`, `platform`, `platform_content_id`).
  - Read-only collector metadata binding via `data/metadata.db` when available.
  - Projection to legacy `MediaAsset` for seamless entry into `pipeline.process_asset()`.
  - Comprehensive unit test suite (`tests/test_media_adapter.py`).
  - Offline smoke test with actual C10 video and album assets.
- **Out of Scope**:
  - ASR / OCR / VLM model execution.
  - Direct Douyin network access or live F2 calls.
  - Modification of frozen M2 code (`src/collector/`, `src/downloader/`).

### M3-02: Video / ASR Integration (`DONE`)
- **Objective**: Stream formal M2 video assets into `pipeline.py` audio extraction and ASR transcription without copying to `data/incoming/manual`, redundant remuxing, or premature visual/knowledge execution.
- **Completed Scope**:
  - Implemented `stop_after: str | None = None` in `pipeline.process_asset()` to halt cleanly after `"asr"`.
  - Implemented `process_canonical_asset(config, canonical_asset, force=False, stop_after="asr")` in `src/pipeline.py`.
  - Implemented `CanonicalMediaAsset.process_asr(config, force=False)` in `src/media_adapter/models.py`.
  - CLI support: `--canonical-id` and `--stop-after` in `main.py` and `pipeline.py`.
  - Strict formal archive immutability: zero bytes modified in `archive/`. Outputs isolated in `data/processed/<video_id>/`.
  - Audio extraction invariant: strictly `PRIMARY_VIDEO` -> 16kHz mono WAV.
  - Explicit `NO_AUDIO` status handling: skips audio extraction without failing, logs `NO_AUDIO` transcript with empty segments and clear notice.
  - Idempotency & resume: skips ASR if existing `transcript.json` matches video SHA and ASR model settings; verified sub-second resume (0.03s).
  - Real C10 4K HEVC video verified offline on GPU (RTX 4090 + faster-whisper large-v3, 184 segments, 370.58s duration, 0 mutations to archive).
  - 10 unit tests in `tests/test_video_asr_pipeline.py` covering all 10 requirements.

### M3-03: Image Album OCR/VLM Integration (`DONE`)
- **Objective**: Integrate formal M2 image album assets into visual understanding pipeline (PaddleOCR PP-OCRv6 + optional VLM), guaranteeing strict sequence order, archive immutability, detailed polygon/bounding box extraction, failure isolation, and idempotent sub-second resume.
- **Completed Scope**:
  - Implemented `src/visual/album.py`: `build_album_visual_evidence()`, `album_visual_pipeline_fingerprint()`, `_run_album_ocr()`, `render_album_visual_markdown()`.
  - Added `process_canonical_album(config, canonical_asset, force=False, stop_after=None)` in `src/pipeline.py` and `CanonicalMediaAsset.process_visual(config, force=False, stop_after=None)` in `src/media_adapter/models.py`.
  - Enhanced `PaddleOCRBackend` in `src/visual/service.py` and `scripts/ocr_gpu_worker.py` with `read_detail(frame)` extracting text, confidence, polygon, and bounding boxes.
  - Fixed Windows subprocess pipe encoding: changed `text=True` to raw byte stream with `.decode("utf-8", errors="replace")` in `src/visual/service.py` and `src/visual/vlm.py`.
  - Per-image failure isolation: wrapped individual image OCR in try/except; single image failures yield `"partial"` album status rather than fatal crash.
  - Strict 1-indexed sequential image ordering (`sequence_index: 1..N`) with per-image provenance binding.
  - Archive immutability: formal assets in `archive/` remain 100% untouched. Outputs isolated in `data/processed/<canonical_id>/visual/` (`visual_transcript.json`, `ocr.json`, `requests.json`, `visual.md`).
  - Optional VLM: gracefully falls back when VLM backend is disabled or local server is offline; logs `unresolved_visual_reference` without failing.
  - Idempotency: verified sub-second resume (< 1ms) when image hashes and visual config are unchanged; invalidates cache upon config/image modification.
  - Verified against real C10 formal album (`7682038498466993905`: 3 WebP images) offline on GPU.
  - 14 comprehensive unit/integration tests in `tests/test_album_visual_pipeline.py`.

### M3-04: Metadata & Provenance Binding (`TODO`)
- Traceable knowledge evidence linking extracted claims/visual points to M2 collector records and original platform items.

### M3-05: Long Media Chunking (`TODO`)
- Dynamic chunking of long transcripts and image batches with hierarchical summary merging.

### M3-06: M3 End-to-End Acceptance (`TODO`)
- Final closure audit for M3: formal local assets -> evidence generation.

