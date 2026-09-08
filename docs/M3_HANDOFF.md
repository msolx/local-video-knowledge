# Milestone M3: Media Knowledge Integration · Master Handoff & Continuity Guide

> **Authoritative Handoff Document for Milestone M3**  
> **Repository Root**: `G:\local_pc_project\personal-knowledge-pipeline`  
> **Milestone Status**: `STARTED / IN_PROGRESS`  
> **Current Focus**: Task M3-02 Video / ASR Integration (`DONE`)  
> **Next Focus**: Task M3-03 Image Album OCR/VLM Integration (`TODO`)  
> **Handoff Target**: Cross-Agent / Cross-Harness Compatible (Gemini 3.8 Flash, OpenCode + GLM 5.3, Codex)

---

## 1. Executive Status Snapshot

| Property | Value |
| :--- | :--- |
| **Milestone** | **Milestone M3: Media Knowledge Integration** |
| **Status** | **STARTED** (User formally authorized 2026-09-08) |
| **Git Branch** | `feat/m3-media-knowledge-integration` |
| **Base Commit** | `ffe8aa1d7e937e91a6270a042e603e0563d402e2` (M2 recovery anchor `m2-douyin-complete-r1`) |
| **M2 Subsystem State**| **FROZEN / UNMODIFIED** (`src/collector/`, `src/downloader/` untouched) |
| **Current Task** | **M3-02: Video / ASR Integration** (`DONE`) |
| **Active Test Baseline** | **692 passed, 10 skipped** (Main `.venv`: 666 M2 baseline + 16 M3-01 tests + 10 M3-02 tests; Worker `.venv-f2`: 55 passed, 10 deselected) |

---

## 2. Implemented Components & Architecture (M3-01)

### 2.1 Package: `src/media_adapter/`
- **[`src/media_adapter/models.py`](file:///G:/local_pc_project/personal-knowledge-pipeline/src/media_adapter/models.py)**:
  - `CanonicalMediaAsset`: Intermediate domain entity encapsulating:
    - Platform identity (`platform`, `platform_content_id`, `canonical_id`)
    - Type discrimination (`CanonicalMediaType.VIDEO` vs `CanonicalMediaType.IMAGE_ALBUM`)
    - Media payloads (`video_path`, `album_images` sequence, `audio_path`)
    - Integrity fingerprints (`video_sha256`, `audio_sha256`, image hashes)
    - Provenance (`source_provenance`, `source_metadata`)
    - Direct projection: `.to_pipeline_media_asset(config)` to bridge video directly into `pipeline.process_asset()` without copying or remuxing.
  - `AlbumImageArtifact`: Frozen dataclass for strictly-ordered image album frames.
  - Domain error taxonomy: `ManifestNotFoundError`, `ManifestInvalidError`, `MediaFileNotFoundError`, `MediaHashMismatchError`, `UnsupportedContentTypeError`, `InvalidMediaAssetError`.
- **[`src/media_adapter/adapter.py`](file:///G:/local_pc_project/personal-knowledge-pipeline/src/media_adapter/adapter.py)**:
  - `CanonicalMediaAssetAdapter`: Deterministic, read-only adapter.
  - Loads and verifies M2 D07 `asset_manifest.json` from formal archive directories (`archive/<platform>/<content_id>`).
  - Supports `load_from_dir(path)`, `load_from_content_id(cid, platform)`, `load_all(platform)`.
  - Performs strict file existence and SHA-256 integrity validation.
  - Sorts image album sequences strictly by 1-indexed `sequence_index`.
  - Discovers optional standalone BGM audio tracks without assuming BGM exists.
  - **Decoupled Metadata DB Architecture**:
    - `metadata_db_path` defaults to `None` (no hardcoding of `data/metadata.db`).
    - Core formal asset ingestion works 100% independently without `metadata.db`.
    - Explicit toggle `enable_metadata_enrichment: bool = True` in `__init__`.
    - Non-blocking SQLite read-only access (`?mode=ro`); if the DB is missing, corrupted, or locked, queries return `{}` and asset loading continues smoothly without interruption.
    - Zero network dependencies.
- **[`src/media_adapter/__init__.py`](file:///G:/local_pc_project/personal-knowledge-pipeline/src/media_adapter/__init__.py)**: Exported module symbols.

### 2.2 Pipeline & ASR Integration (M3-02)
- **[`src/pipeline.py`](file:///G:/local_pc_project/personal-knowledge-pipeline/src/pipeline.py)**:
  - **Stage Cutoff Control (`stop_after`)**:
    - Added parameter `stop_after: str | None = None` to `process_asset()`.
    - Enables halting cleanly after any stage (`"source"`, `"audio"`, `"asr"`, `"visual"`, `"knowledge"`). Downstream stages are cleanly skipped without error.
    - Added `--stop-after` CLI flag in both `create_parser()` and `main.py`.
  - **Canonical Asset Ingestion (`process_canonical_asset`)**:
    - Direct invocation via `process_canonical_asset(config, canonical_asset, force=False, stop_after="asr") -> Path`.
    - Automatically projects `CanonicalMediaAsset` to legacy `MediaAsset` with fully bound provenance.
    - Creates video processing workspace under `data/processed/<video_id>/` without requiring folder copying or intake dropping.
  - **Archive Immutability Invariant**:
    - `archive/` is strictly read-only. All intermediate and final artifacts (`manifest.json`, `audio.wav`, `transcript.json`, `transcript.md`) are saved under `data/processed/<video_id>/`.
    - Verified zero modifications to source files during testing.
  - **Audio Extraction Source Invariant**:
    - Audio is extracted strictly from `PRIMARY_VIDEO` via FFmpeg into 16kHz mono PCM WAV.
    - No reliance on secondary BGM metadata or synthetic audio tracks for speech transcription.
  - **Explicit `NO_AUDIO` State Handling**:
    - Videos lacking an audio stream (`not asset.probe.audio`) are detected in `extract_audio` and marked `{"status": "skipped", "reason": "NO_AUDIO", "artifacts": []}`.
    - `run_asr` handles this cleanly by generating `status: "NO_AUDIO"` with empty segments and a descriptive markdown notice: `_No speech audio track present in source video (NO_AUDIO)._`.
    - Preserves skip semantics in `_stage()` via `previous.get("status") in ("completed", "skipped")`.
  - **Idempotency & Resume Protocol**:
    - Before calling faster-whisper inference, checks if `transcript.json` already exists and verifies both `source_video_hash` (or SHA-256) and ASR configuration settings (`model_name`, `backend`, `compute_type`, `beam_size`).
    - Skips inference on cache hits, yielding sub-second turnaround (~0.03s).
    - `force=True` safely bypasses the cache to regenerate transcripts.
  - **Enriched Provenance Binding**:
    - Writes canonical provenance into `transcript.json`: `canonical_id`, `platform`, `platform_content_id`, `source_video`, `content_hash`, `source`, `provenance`.
- **[`src/media_adapter/models.py`](file:///G:/local_pc_project/personal-knowledge-pipeline/src/media_adapter/models.py)**:
  - Extended `to_pipeline_media_asset` to populate `canonical_id`, `platform`, `platform_content_id` in `media_record`.
  - Added method `CanonicalMediaAsset.process_asr(config, force=False) -> Path`.
- **[`main.py`](file:///G:/local_pc_project/personal-knowledge-pipeline/main.py)**:
  - Added CLI routing for `--canonical-id` and `--stop-after`.

---

## 3. Test Suite & Regression Reconciliation

### 3.1 Unit & Integration Test Suite (`tests/test_media_adapter.py`)
- **16/16 tests passing** in 0.44s.
- Covers:
  - Formal video asset ingestion & validation
  - Image album asset ingestion & 1-indexed sequence ordering
  - Optional BGM audio track discovery
  - Shuffled sequence ordering defense
  - Missing manifest detection (`ManifestNotFoundError`)
  - Corrupt JSON detection (`ManifestInvalidError`)
  - Missing referenced media detection (`MediaFileNotFoundError`)
  - SHA-256 hash mismatch detection & toggle (`MediaHashMismatchError`)
  - Zero-byte media rejection (`InvalidMediaAssetError`)
  - Unknown content type rejection (`UnsupportedContentTypeError`)
  - Optional SQLite metadata enrichment (`collection_items` read-only query)
  - Multi-asset discovery via `archive_root` (`load_from_content_id`, `load_all`)
  - Rejection of image album projection to single-video `MediaAsset`
  - Real C10 4K HEVC video offline integration smoke test (`7681603850364521734`)
  - Real C10 3-image WebP album offline integration smoke test (`7682038498466993905`)
  - Explicit metadata enrichment toggle (`enable_metadata_enrichment=False`) & database failure resilience (corrupt DB non-blocking)

### 3.2 Video / ASR Integration Test Suite (`tests/test_video_asr_pipeline.py`)
- **10/10 tests passing** in 1.88s.
- Covers:
  1. `test_01_canonical_video_asset_projection`: Verifies `CanonicalMediaAsset.to_pipeline_media_asset()` projection and identity binding.
  2. `test_02_formal_asset_archive_immutability`: Verifies `archive/` remains 100% immutable before, during, and after processing.
  3. `test_03_no_manual_incoming_copy`: Confirms processing occurs directly without copying to `data/incoming/manual`.
  4. `test_04_audio_extraction_from_primary_video`: Confirms audio is extracted strictly from `PRIMARY_VIDEO` to 16kHz mono WAV.
  5. `test_05_no_audio_video_explicit_status`: Tests videos without audio track produce explicit `NO_AUDIO` status without crashing.
  6. `test_06_asr_idempotency_and_resume`: Verifies unchanged hash + settings skips ASR, and `force=True` re-runs ASR.
  7. `test_07_transcript_artifact_structure`: Verifies `transcript.json` and `transcript.md` structure, segment timestamps, and provenance fields.
  8. `test_08_image_album_rejection_for_video_asr`: Verifies image album assets are rejected from video ASR flow with clear domain exception.
  9. `test_09_pipeline_stop_after_cutoff`: Confirms `stop_after="asr"` terminates pipeline cleanly before `visual` or `knowledge` stages.
  10. `test_10_real_c10_video_offline_asr_smoke`: Full offline integration test verifying real C10 video (`7681603850364521734`) processes through adapter and pipeline to valid transcripts.

### 3.3 Regression Baseline Tracking (672/4 -> 682/10 -> 692/10)
- **M2 Checkpoint Baseline**: 672 passed, 4 skipped (Total collected: 676)
- **M3-01 Main Suite**: 682 passed, 10 skipped (Total collected: 692 = 676 M2 tests + 16 M3-01 tests)
- **Current M3-02 Main Suite**: **692 passed, 10 skipped** (Total collected: 702 = 676 M2 tests + 16 M3-01 tests + 10 M3-02 tests)
  - Old tests executing: 666 passed, 10 skipped
  - New M3-01 tests: 16 passed
  - New M3-02 tests: 10 passed
  - Total: 666 + 16 + 10 = 692 passed in 76.08s.
- **Worker Suite (`.venv-f2`)**: 55 passed, 10 deselected in 3.51s.
- **The 4 Pre-Existing M2 Skips (Worker Environment Isolation)**:
  1. `tests/test_f2_backend.py:885`: `Requires F2 installed in worker environment (.venv-f2)`
  2. `tests/test_f2_backend.py:988`: `Requires F2 installed in worker environment (.venv-f2)`
  3. `tests/test_downloader_worker.py:1519`: `Live network acquisition requires dedicated F2 worker environment (.venv-f2) and authenticated Chrome profile.`
  4. `tests/test_downloader_worker.py:1625`: `Live network acquisition requires dedicated F2 worker environment (.venv-f2) and authenticated Chrome profile.`
- **The 6 Tests Transitioning from Passed to Skipped (Captive Portal / Challenge Backoff)**:
  1. `tests/test_credentials.py::test_32_real_profile_smoke_read_only` (line 804)
  2. `tests/test_credentials.py::test_33_real_profile_cold_restart` (line 854)
  3. `tests/test_credentials.py::test_34_real_profile_scope_mismatch` (line 897)
  4. `tests/test_douyin_auth_state.py::test_case_21_real_dedicated_profile_preflight_smoke` (line 739)
  5. `tests/test_douyin_auth_state.py::test_case_22_real_dedicated_profile_restart_preflight` (line 780)
  6. `tests/test_douyin_source_client.py::test_23_live_collection_fetch_smoke` (line 560)
- **Exact Skip Reasons**:
  - Tests 1–5: `SKIPPED: BLOCKED: Dedicated profile currently requires human captcha verification or WAF backoff (INTERACTIVE_CHALLENGE)`
  - Test 6: `SKIPPED: BLOCKED: Dedicated profile currently requires manual human captcha verification (AUTH_CHALLENGE_REQUIRES_USER_ACTION)`
- **Root Cause Analysis**:
  - During C10 acceptance testing, the dedicated Chrome profile (`G:\antigravity-cli\dy\runtime\chrome-profile`) had an active human-solved captcha cookie state.
  - Subsequently (>24h later), the Douyin web platform naturally presented a slider challenge / WAF captcha for the profile.
  - Per explicit user mandate: **"不再访问抖音线上，不要再次浏览器挑战测试，现有 C10 live evidence 保持有效，FINAL STOP"**, no agent or human solves captchas on the live platform.
  - The test fixtures for these 6 tests are specifically engineered with preflight challenge detection: when `INTERACTIVE_CHALLENGE` is encountered, they invoke `pytest.skip()` rather than failing.
  - **Zero Code Regression**: None of the 6 skips were caused by M3 code changes, dependency changes, or workspace relocation. Zero tests were dropped from collection. All 676 M2 tests remain intact and collected.

---

## 4. Known Limitations & Non-Goals in M3-02

1. **Task Scope Boundary**: M3-02 strictly addressed video audio extraction and ASR transcription. Visual frame OCR/VLM and knowledge summarization were cleanly stopped via `stop_after="asr"`.
2. **Image Album Processing**: Image albums are intentionally rejected by `process_canonical_asset()` during M3-02. M3-03 will implement multi-image OCR and VLM processing for `asset.album_images`.
3. **No Network**: Zero network requests or live Douyin API calls.

---

## 5. Next Agent Protocol (NEXT_AGENT_START_HERE)

```text
================================================================================
                    NEXT_AGENT_START_HERE (CROSS-AGENT PROTOCOL)
================================================================================
Target Audience: Any LLM / Agent (Gemini, OpenCode + GLM 5.3, Codex)
Current Status : M3-02 DONE, ready for M3-03.

1. CURRENT BRANCH:
   feat/m3-media-knowledge-integration

2. CURRENT HEAD / BASE:
   Commit: feat(m3): integrate formal video assets with ASR
   Base: ffe8aa1d7e937e91a6270a042e603e0563d402e2 (main / m2-douyin-complete-r1)

3. COMPONENTS:
   - src/media_adapter/ (CanonicalMediaAsset, AlbumImageArtifact, CanonicalMediaAssetAdapter)
   - src/pipeline.py (process_canonical_asset, stop_after stage cutoff, NO_AUDIO contract, resume)
   - tests/test_media_adapter.py (16 tests)
   - tests/test_video_asr_pipeline.py (10 tests)
   - docs/M3_HANDOFF.md, docs/M3_DECISIONS.md, docs/M3_TASKS.md

4. CURRENT TASK:
   M3-02: Video / ASR Integration -> DONE.
   Next Task: M3-03 Image Album OCR/VLM Integration.

5. COMPLETED WORK:
   - M3-01: CanonicalMediaAssetAdapter (manifest ingestion, formal validation, decoupled metadata DB).
   - M3-02: Video / ASR Integration:
     * Added stop_after cutoff support in pipeline.py ("source", "audio", "asr").
     * Direct streaming from CanonicalMediaAsset into pipeline without incoming/manual copying.
     * Strict archive immutability (archive/ is 100% read-only).
     * Audio extraction strictly from PRIMARY_VIDEO (16kHz mono WAV).
     * Explicit NO_AUDIO status handling without hallucinating transcripts.
     * Idempotent resume: sub-second turnaround (0.03s) on cached transcripts.
     * Real C10 4K HEVC video verified on RTX 4090 GPU offline (184 segments, 370.58s).
     * 10/10 tests in tests/test_video_asr_pipeline.py passed.
     * Full regression: 692 passed, 10 skipped.

6. REMAINING WORK (M3-03+):
   - M3-03: Implement image album visual OCR/VLM inspection for album_images (PP-OCRv6 + VLM).
   - M3-04: Bind grounded provenance and collector metadata to final knowledge evidence.
   - M3-05: Dynamic chunking of long transcripts and image batches with hierarchical summary merging.
   - M3-06: M3 End-to-End Acceptance.

7. EXACT NEXT COMMANDS TO RUN:
   # Step A: Run M3 unit suites
   .\.venv\Scripts\python.exe -m pytest tests/test_media_adapter.py tests/test_video_asr_pipeline.py -v

   # Step B: Run full test regression
   .\.venv\Scripts\python.exe -m pytest tests -q

8. KEY FILES TO READ:
   - docs/M3_HANDOFF.md (this document)
   - docs/M3_DECISIONS.md (architectural boundaries, Decisions 1-7)
   - docs/M3_TASKS.md (task progression board)
   - src/media_adapter/models.py
   - src/media_adapter/adapter.py
   - src/pipeline.py
   - tests/test_video_asr_pipeline.py

9. CORE INVARIANTS (DO NOT BREAK):
   - DO NOT modify src/collector/ or src/downloader/ (M2 is frozen).
   - DO NOT make network calls or live Douyin requests.
   - DO NOT touch, mutate, or delete M2 runtime data (data/metadata.db, archive/douyin/).
   - Formal asset archive files must remain strictly read-only.
================================================================================
```

