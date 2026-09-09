# Milestone M3: Media Knowledge Integration · Master Handoff & Continuity Guide

> **Authoritative Handoff Document for Milestone M3**  
> **Repository Root**: `G:\local_pc_project\personal-knowledge-pipeline`  
> **Milestone Status**: `COMPLETE`  
> **Current Focus**: Milestone M3 End-to-End Acceptance (`DONE`)  
> **Next Milestone**: Milestone M4: Unified Knowledge Model (`NOT STARTED` - Awaiting User Authorization)  
> **Handoff Target**: Cross-Agent / Cross-Harness Compatible (Gemini 3.8 Flash, OpenCode + GLM 5.3, Codex)

---

## 1. Executive Status Snapshot

| Property | Value |
| :--- | :--- |
| **Milestone** | **Milestone M3: Media Knowledge Integration** |
| **Status** | **COMPLETE** (User authorized 2026-09-08, Accepted 2026-09-09) |
| **Git Branch** | `feat/m3-media-knowledge-integration` |
| **Base Commit** | `ffe8aa1d7e937e91a6270a042e603e0563d402e2` (M2 recovery anchor `m2-douyin-complete-r1`) |
| **M2 Subsystem State**| **FROZEN / UNMODIFIED** (`src/collector/`, `src/downloader/` untouched) |
| **Current Task** | **M3-06: M3 End-to-End Acceptance** (`DONE`) |
| **Active Test Baseline** | **743 passed, 10 skipped** (Main `.venv`: 666 M2 baseline + 16 M3-01 + 10 M3-02 + 14 M3-03 + 17 M3-04 + 20 M3-05 tests; Worker `.venv-f2`: 55 passed, 10 deselected) |



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

### 2.3 Package: `src/visual/album.py` and Image Album OCR/VLM Integration (M3-03)
- **[`src/visual/album.py`](file:///G:/local_pc_project/personal-knowledge-pipeline/src/visual/album.py)**:
  - `build_album_visual_evidence(canonical_asset, visual_dir, config, force=False)`:
    - Sorts album images strictly by 1-indexed `sequence_index` (1..N).
    - Checks cache hit via `album_visual_pipeline_fingerprint()`; resumes in sub-second time (<1ms) if unchanged.
    - Dispatches OCR across all ordered images using `_run_album_ocr()`.
    - Extracts detailed per-line OCR text, confidence scores, 4-point polygons, and bounding boxes.
    - Isolates single-image failures: wrapped in per-image try/except; if 1/N fails, remaining N-1 are captured and overall status is `"partial"`.
    - Evaluates OCR text against `minimum_confidence` (default 0.65); flags `insufficient_ocr` when text is missing or below threshold.
    - Optional VLM: attempts visual description via `LMStudioVLMBackend` or `OpenAICompatibleVLMBackend`; if server is offline or disabled, logs `unresolved_visual_reference` gracefully without failing.
    - Writes standard artifacts into `data/processed/<canonical_id>/visual/`:
      * `visual_transcript.json` (schema `"visual-evidence-v1"`)
      * `ocr.json` (detailed per-line polygons, boxes, and confidence)
      * `requests.json` (VLM requests and statuses)
      * `visual.md` (human-readable markdown visual summary)
  - `album_visual_pipeline_fingerprint(album_images, config)`: Deterministic SHA-256 over image sequence indices, image SHA-256s, and visual OCR/VLM settings.
  - `render_album_visual_markdown(evidence, summary)`: Formats visual summary report.
- **[`src/visual/service.py`](file:///G:/local_pc_project/personal-knowledge-pipeline/src/visual/service.py)**:
  - Enhanced `PaddleOCRBackend` with `read_detail(frame)` returning `texts`, `scores`, `polygons`, `boxes`, `inference_seconds`.
  - Fixed Windows subprocess pipe decoding: changed `text=True` to byte I/O with `.decode("utf-8", errors="replace")` in `_run_gpu_worker()`.
- **[`scripts/ocr_gpu_worker.py`](file:///G:/local_pc_project/personal-knowledge-pipeline/scripts/ocr_gpu_worker.py)**:
  - Calls `backend.read_detail(frame)` and wraps each frame in try/except for per-image failure isolation.
- **[`src/visual/vlm.py`](file:///G:/local_pc_project/personal-knowledge-pipeline/src/visual/vlm.py)**:
  - Fixed Windows subprocess pipe decoding in `LMStudioVLMBackend` to eliminate GBK decode crashes.
- **[`src/pipeline.py`](file:///G:/local_pc_project/personal-knowledge-pipeline/src/pipeline.py)**:
  - Added `process_canonical_album(config, canonical_asset, force=False, stop_after=None) -> Path`.
  - Updated `run()` to route `canonical_asset.is_album` to `process_canonical_album`.
  - Automated visual cache invalidation: re-runs visual stage if visual config or album content fingerprint changes.
- **[`src/media_adapter/models.py`](file:///G:/local_pc_project/personal-knowledge-pipeline/src/media_adapter/models.py)**:
  - Added `@property def size_bytes(self) -> int: return self.byte_size` to `AlbumImageArtifact`.
  - Added `CanonicalMediaAsset.process_visual(config, force=False, stop_after=None) -> Path`.
  - Added `CanonicalMediaAsset.bind_evidence(config, force=False, processed_dir=None) -> Path`.

### 2.4 Package: `src/provenance.py` and Metadata & Evidence Provenance Binding (M3-04)
- **[`src/provenance.py`](file:///G:/local_pc_project/personal-knowledge-pipeline/src/provenance.py)**:
  - `EvidenceItem`: Lightweight dataclass representing an observed piece of evidence.
    - Fields: `evidence_id`, `modality` (`speech`, `visual_text`, `visual_description`), `source_artifact`, `temporal`, `sequence`, `model_provenance`, `payload`, `verification_status`.
    - Invariant: `verification_status: "not_checked"`.
  - `extract_source_metadata_snapshot(canonical_asset, processed_dir)`:
    - Extracts `title`, `author_name`, `author_id`, `published_at`, `first_seen_at`, `source_url`, `tags`, and `enrichment_status`.
    - Distinguishes creator publication time (`published_at`) from collector observation time (`first_seen_at`).
    - Strictly NO fake `collected_at`.
  - `extract_formal_asset_binding(canonical_asset)`:
    - Binds formal asset metadata: `canonical_id`, `platform`, `platform_content_id`, `content_type`, `asset_root`, `manifest_path`, `archived_at`, `source_provenance`, and ordered `artifacts` (`PRIMARY_VIDEO`, `ALBUM_IMAGE`, `AUDIO_TRACK`).
  - `collect_evidence_items(canonical_asset, processed_dir)`:
    - Video ASR segments: maps each speech segment to 1-indexed `segment_order`, exact `start`/`end`/`duration`, bound to `PRIMARY_VIDEO` and video SHA-256.
    - Album visual frames: maps each OCR frame to 1-indexed `sequence_index`, bound to `ALBUM_IMAGE` WebP file and SHA-256, with confidence, lines, polygons, and boxes.
    - Unresolved VLM references: records `modality: "visual_description"` with status `unresolved_visual_reference`.
    - Handles `NO_AUDIO` video gracefully with 0 segments and explicit status.
  - `compute_manifest_fingerprint(canonical_id, source_meta, formal_asset, evidence_items, model_prov)`:
    - Computes deterministic SHA-256 over identity, metadata snapshot, formal artifacts, evidence items, and model settings.
  - `build_evidence_manifest(canonical_asset, config, processed_dir, force=False)`:
    - Assembles unified evidence manifest.
    - Returns cached manifest instantly (< 2ms) if fingerprint matches and `force=False`.
  - `write_evidence_manifest(canonical_asset, config, processed_dir, force=False)`:
    - Atomically writes `data/processed/<canonical_id>/evidence_manifest.json`.
  - `load_evidence_manifest(path)` and `verify_evidence_manifest(path)`:
    - Verification and loading helpers.
- **[`src/pipeline.py`](file:///G:/local_pc_project/personal-knowledge-pipeline/src/pipeline.py)**:
  - Added `--stop-after evidence` to CLI parser.
  - `process_canonical_asset` and `process_canonical_album` automatically invoke `write_evidence_manifest` when completing ASR or visual stages.

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

### 3.3 Image Album Visual / OCR Test Suite (`tests/test_album_visual_pipeline.py`)
- **14/14 tests passing** in 0.69s.
- Covers:
  1. `test_01_canonical_album_enters_visual_pipeline`: Album enters visual pipeline directly without manual drops; verifies artifact directory structure.
  2. `test_02_sequence_order_preserved`: Verifies images are processed in strict sequence order (1..N) even if shuffled in manifest.
  3. `test_03_archive_immutable`: Verifies `archive/` remains 100% untouched before and after processing.
  4. `test_04_ocr_evidence_tied_to_exact_image`: Verifies provenance binding (`canonical_id`, `platform`, `sequence_index`, `source_image_file`, `source_image_sha256`) and per-line polygons/boxes/confidence.
  5. `test_05_ocr_empty_text_behavior`: Verifies images with empty or low-confidence text produce `insufficient_ocr` without crashing.
  6. `test_06_ocr_backend_unavailable_behavior`: Verifies missing OCR binary/env raises clear, informative error.
  7. `test_07_single_image_failure_partial_behavior`: Verifies failure isolation (1/N failing produces overall `"partial"` status while preserving other images).
  8. `test_08_optional_vlm_disabled`: Verifies VLM disabled mode completes OCR cleanly.
  9. `test_09_optional_vlm_success_mock`: Verifies successful VLM response produces `"resolved"` visual reference.
  10. `test_10_repeated_processing_and_cache`: Verifies idempotent sub-second cache hit on repeated execution (<1ms).
  11. `test_11_image_hash_or_config_change_invalidates_cache`: Verifies modifying OCR config invalidates cache and triggers re-run.
  12. `test_12_video_canonical_asset_rejected`: Verifies video asset is rejected by album visual pipeline with `UnsupportedContentTypeError`.
  13. `test_13_optional_bgm_does_not_affect_ocr`: Verifies optional BGM is preserved in metadata/media while audio/ASR stages are skipped.
  14. `test_14_real_c10_album_offline_smoke`: Integration smoke test with real C10 formal album (`7682038498466993905`: 3 WebP images) on GPU.

### 3.4 Evidence & Provenance Binding Test Suite (`tests/test_evidence_provenance.py`)
- **17/17 tests passing** in 0.42s.
- Covers all 17 mandatory requirements:
  1. `test_01_video_asr_evidence_provenance`: Video ASR evidence contains complete model provenance (backend, model, compute_type, beam_size, status).
  2. `test_02_asr_segment_exact_source_binding`: Video segment evidence binds to PRIMARY_VIDEO, file name, SHA-256, temporal start/end/duration, and 1-indexed segment order.
  3. `test_03_album_ocr_image_level_provenance`: Album OCR items bind to ALBUM_IMAGE, WebP filename, image SHA-256, byte size, confidence, lines (polygons/boxes), and sequence index.
  4. `test_04_sequence_index_preserved`: Album images maintain 1-indexed order even if input sequence is shuffled.
  5. `test_05_vlm_unresolved_provenance`: Unresolved VLM references produce modality `visual_description` with `unresolved_visual_reference`, `verification_status: "not_checked"`.
  6. `test_06_metadata_enrichment_binding`: When `metadata.db` is present, `source_metadata` is snapshot-bound with `enrichment_status: "enriched"`, `title`, `author_name`, `author_id`, `published_at`, `first_seen_at`, `source_url`, `tags`.
  7. `test_07_metadata_db_absent_resilience`: When `metadata.db` is absent, `source_metadata` gracefully falls back with `enrichment_status: "unenriched"` without failing.
  8. `test_08_published_at_vs_first_seen_at_semantics`: Creator publication timestamp (`published_at`) and collector observation timestamp (`first_seen_at`) have distinct values and semantics.
  9. `test_09_no_fake_collected_at`: `source_metadata` in `evidence_manifest.json` does NOT contain any fabricated `collected_at` field.
  10. `test_10_no_audio_video_evidence`: Videos without audio produce 0 speech items, with `status: "NO_AUDIO"` in model provenance.
  11. `test_11_partial_album_evidence`: Album with partial OCR failure captures all items with corresponding status (`failed` or `completed`).
  12. `test_12_deterministic_ordering`: Multiple calls produce bit-for-bit identical evidence item ordering and identical fingerprints.
  13. `test_13_repeated_binding_idempotent`: Repeated invocation with unchanged inputs returns cached file in sub-second time (< 2ms).
  14. `test_14_evidence_or_config_change_invalidates_fingerprint`: Altering an evidence item or model setting alters the fingerprint and triggers re-generation.
  15. `test_15_archive_immutability`: Formal archive files in `archive/` are never written to, modified, or deleted.
  16. `test_16_real_c10_video_offline_smoke`: Offline integration smoke test with real C10 video (`7681603850364521734`) verifying full provenance chain: Douyin CID -> Formal Video MP4 -> SHA-256 `3959...` -> exact 184 segments -> ASR model faster-whisper large-v3 -> `evidence_manifest.json`.
  17. `test_17_real_c10_album_offline_smoke`: Offline integration smoke test with real C10 album (`7682038498466993905`) verifying full provenance chain: Douyin CID -> Formal Album WebP images -> SHA-256s -> sequence indices 1..3 -> PaddleOCR text/polygons -> unresolved VLM -> `evidence_manifest.json`.

### 3.5 Long Media Chunking Test Suite (`tests/test_evidence_chunking.py`)
- **20/20 tests passing** in 0.38s.
- Covers:
  1. `test_01_video_evidence_manifest_to_chunks`: Validates chunking video evidence manifest into deterministic speech chunks.
  2. `test_02_all_speech_evidence_covered`: Guarantees 100% unique evidence coverage (all speech segments referenced).
  3. `test_03_deterministic_chunk_ordering`: Verifies chunk ordering is strictly increasing by 1-indexed `chunk_index`.
  4. `test_04_deterministic_chunk_ids`: Confirms deterministic naming `chk_000001`, `chk_000002`, etc.
  5. `test_05_temporal_range_correct`: Checks temporal range (`start`, `end`, `duration`) envelopes referenced evidence.
  6. `test_06_bounded_overlap`: Verifies overlap segments are properly tracked and explicitly listed in `overlap_evidence_ids`.
  7. `test_07_no_segment_text_splitting`: Ensures segments are referenced as atomic units without text splitting.
  8. `test_08_config_change_invalidates_cache`: Tests that altering chunk policy thresholds invalidates cached chunks.
  9. `test_09_evidence_manifest_change_invalidates_cache`: Tests that changing source manifest fingerprint invalidates cache.
  10. `test_10_second_run_idempotent_cache_hit`: Confirms sub-second cache hit (< 3ms) on repeated execution.
  11. `test_11_no_audio_video_no_fake_speech_chunk`: Tests that NO_AUDIO video produces 0 speech chunks.
  12. `test_12_album_evidence_to_chunks`: Tests batching album visual evidence into sequence-bounded chunks.
  13. `test_13_album_sequence_preserved`: Confirms album image sequences are strictly 1-indexed without time distortion.
  14. `test_14_same_image_ocr_vlm_grouping`: Groups OCR and VLM evidence for the same image together.
  15. `test_15_unresolved_vlm_preserved`: Preserves unresolved VLM evidence references within chunks.
  16. `test_16_partial_evidence_preserved`: Preserves partial/error evidence within chunk references.
  17. `test_17_large_synthetic_album_batching`: Validates multi-chunk batching across large image albums.
  18. `test_18_archive_immutability`: Confirms formal archive in `archive/` remains 100% read-only.
  19. `test_19_real_c10_video_smoke`: Offline integration smoke test on real C10 video (4 chunks, 184/184 unique segments).
  20. `test_20_real_c10_album_smoke`: Offline integration smoke test on real C10 album (1 chunk, 3 images / 4 items).

### 3.6 Regression Baseline Tracking (672/4 -> 682/10 -> 692/10 -> 706/10 -> 723/10 -> 743/10)
- **M2 Checkpoint Baseline**: 672 passed, 4 skipped (Total collected: 676)
- **M3-01 Main Suite**: 682 passed, 10 skipped (Total collected: 692 = 676 M2 tests + 16 M3-01 tests)
- **M3-02 Main Suite**: 692 passed, 10 skipped (Total collected: 702 = 676 M2 tests + 16 M3-01 tests + 10 M3-02 tests)
- **M3-03 Main Suite**: 706 passed, 10 skipped (Total collected: 716 = 676 M2 tests + 16 M3-01 tests + 10 M3-02 tests + 14 M3-03 tests)
- **M3-04 Main Suite**: 723 passed, 10 skipped (Total collected: 733 = 676 M2 tests + 16 M3-01 tests + 10 M3-02 tests + 14 M3-03 tests + 17 M3-04 tests)
- **Final M3-06 Main Suite**: **743 passed, 10 skipped** (Total collected: 753 = 676 M2 tests + 16 M3-01 + 10 M3-02 + 14 M3-03 + 17 M3-04 + 20 M3-05 tests)
  - Old tests executing: 666 passed, 10 skipped
  - New M3-01 tests: 16 passed
  - New M3-02 tests: 10 passed
  - New M3-03 tests: 14 passed
  - New M3-04 tests: 17 passed
  - New M3-05 tests: 20 passed
  - Total: 666 + 16 + 10 + 14 + 17 + 20 = 743 passed in 54.55s.
- **Worker Suite (`.venv-f2`)**: 55 passed, 10 deselected in 5.08s.
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

## 4. Known Limitations & Non-Goals in M3-04

1. **Epistemic Invariant (Evidence Is Not Truth)**: M3-04 strictly binds model-observed evidence (ASR speech segments and OCR image frame text/polygons). It does NOT assert ground truth or factual veracity (`verification_status: "not_checked"` across all items).
2. **Task Scope Boundary (Zero Knowledge Extraction)**: M3-04 strictly stops after evidence binding. No claim extraction, no opinion extraction, no summarization, no entity linking, and no Obsidian/RAG notes are generated.
3. **No Network**: Zero network requests or live Douyin API calls.
4. **Archive Immutability**: Formal archive files in `archive/` remain 100% read-only and unmodified.

---

## 5. Next Agent Protocol (NEXT_AGENT_START_HERE)

```text
================================================================================
                    NEXT_AGENT_START_HERE (CROSS-AGENT PROTOCOL)
================================================================================
Target Audience: Any LLM / Agent (Gemini 3.8 Flash, OpenCode + GLM 5.3, Codex)
Current Status : Milestone M3 is COMPLETE.
Next Milestone : Milestone M4: Unified Knowledge Model (NOT STARTED - Awaiting User Authorization)

1. CURRENT BRANCH:
   feat/m3-media-knowledge-integration

2. CURRENT BASE:
   Base Commit: ffe8aa1d7e937e91a6270a042e603e0563d402e2 (main / m2-douyin-complete-r1)
   Milestone M3 Deliverables:
     - src/media_adapter/ (CanonicalMediaAssetAdapter, CanonicalMediaAsset, models.py)
     - src/visual/album.py (Image Album OCR/VLM processing, sequence preservation)
     - src/provenance.py (EvidenceItem, Evidence Manifest, 1:1 artifact binding)
     - src/chunking/ (EvidenceChunk, ChunkingPolicy, deterministic windowing)
     - data/processed/<canonical_id>/evidence_manifest.json (schema: media-evidence-manifest-v1)
     - data/processed/<canonical_id>/evidence_chunks.json (schema: evidence-chunks-v1)
     - docs/M3_FINAL_ACCEPTANCE.md (authoritative signoff document)

3. REGRESSION STATUS:
   - 77/77 M3 targeted unit/integration tests passing in 3.21s
   - 743 passed, 10 skipped in 54.55s on main test suite (666 M2 baseline + 77 M3 tests)
   - 55 passed, 10 deselected in 5.08s on F2 worker test suite (.venv-f2)
   - 0 failures, 0 regressions, 0 archive mutations, 0 M2 code changes

4. MILESTONE M3 STATUS SUMMARY:
   - M3-01: CanonicalMediaAssetAdapter -> DONE
   - M3-02: Video / ASR Integration -> DONE
   - M3-03: Image Album OCR/VLM Integration -> DONE
   - M3-04: Metadata & Provenance Binding -> DONE
   - M3-05: Long Media Chunking -> DONE
   - M3-06: M3 End-to-End Acceptance -> DONE
   - MILESTONE M3 -> COMPLETE

5. MILESTONE M4 BOUNDARY & PREREQUISITES:
   - Milestone M4 encompasses the Unified Knowledge Model:
     * Semantic summarization of Evidence Chunks via LLM
     * Knowledge extraction (claims, opinions, entities) bound to EvidenceItem IDs
     * Multi-modal knowledge graph and Markdown note generation
   - DO NOT start M4 until the user explicitly issues authorization and instructions for M4.
   - When M4 begins, read:
     * docs/M3_FINAL_ACCEPTANCE.md
     * docs/M3_HANDOFF.md
     * docs/M3_DECISIONS.md
     * data/processed/<canonical_id>/evidence_chunks.json

6. CORE INVARIANTS (DO NOT BREAK):
   - DO NOT modify src/collector/ or src/downloader/ (M2 is frozen).
   - DO NOT make network calls or live Douyin requests.
   - DO NOT touch, mutate, or delete M2 runtime data (data/metadata.db, archive/douyin/).
   - Formal asset archive files must remain strictly read-only.
   - Evidence is model observation, NOT truth (verification_status: "not_checked").
================================================================================
```



