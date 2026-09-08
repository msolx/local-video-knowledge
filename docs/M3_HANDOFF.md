# Milestone M3: Media Knowledge Integration · Master Handoff & Continuity Guide

> **Authoritative Handoff Document for Milestone M3**  
> **Repository Root**: `G:\local_pc_project\personal-knowledge-pipeline`  
> **Milestone Status**: `STARTED / IN_PROGRESS`  
> **Current Focus**: Task M3-01 CanonicalMediaAssetAdapter (`DONE`)  
> **Next Focus**: Task M3-02 Video / ASR Integration (`TODO`)  
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
| **Current Task** | **M3-01: CanonicalMediaAssetAdapter** (`COMPLETED / READY TO COMMIT`) |
| **Active Test Baseline** | **681 passed, 10 skipped** (Main `.venv`: 666 baseline + 15 M3 adapter tests) |

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
  - Queries `data/metadata.db` (`collection_items.canonical_json`) in read-only mode to bind collector metadata (title, author, tags, etc.) when available.
- **[`src/media_adapter/__init__.py`](file:///G:/local_pc_project/personal-knowledge-pipeline/src/media_adapter/__init__.py)**: Exported module symbols.

---

## 3. Test Suite & Verification Results

- **Unit Test File**: [`tests/test_media_adapter.py`](file:///G:/local_pc_project/personal-knowledge-pipeline/tests/test_media_adapter.py)
  - 15/15 tests passing in 0.39s.
  - Covers video formal assets, image album assets, optional BGM, shuffled sequence ordering defense, missing manifest, corrupt JSON, missing media file, hash mismatch detection, 0-byte file rejection, unknown content type rejection, SQLite provenance enrichment, and archive root discovery.
- **Offline Integration Smoke**:
  - `test_14_real_c10_video_integration_smoke`: Successfully ingested actual C10 173MB 4K HEVC video (`7681603850364521734`), verified SHA-256, enriched title/author from `metadata.db`, and projected into `pipeline.MediaAsset` with valid stream probe (duration 371.7s, 2160x3840 HEVC, 44.1kHz AAC).
  - `test_15_real_c10_album_integration_smoke`: Successfully ingested actual C10 3-image WebP album (`7682038498466993905`), verified 1-indexed ordering and physical integrity.

---

## 4. Known Limitations & Non-Goals in M3-01

1. **Adapter Scope Boundary**: The adapter only handles formal asset reading and domain projection. It deliberately does not execute ASR, OCR, or LLM inference.
2. **Image Album Pipeline Entry**: Legacy `pipeline.py` currently only accepts audio+video inputs (`MediaAsset`). M3-03 will implement the visual OCR/VLM processor specifically designed to consume `asset.album_images`.
3. **No Network**: Zero network requests or live Douyin API calls.

---

## 5. Next Agent Protocol (NEXT_AGENT_START_HERE)

```text
================================================================================
                    NEXT_AGENT_START_HERE (CROSS-AGENT PROTOCOL)
================================================================================
Target Audience: Any LLM / Agent (Gemini, OpenCode + GLM 5.3, Codex)
Current Status : M3-01 DONE, ready to commit.

1. CURRENT BRANCH:
   feat/m3-media-knowledge-integration

2. CURRENT HEAD / BASE:
   ffe8aa1d7e937e91a6270a042e603e0563d402e2 (main / m2-douyin-complete-r1)

3. UNCOMMITTED DIFF STATUS:
   - src/media_adapter/ (new package: models.py, adapter.py, __init__.py)
   - tests/test_media_adapter.py (15 new unit and smoke tests)
   - docs/M3_HANDOFF.md, docs/M3_DECISIONS.md, docs/M3_TASKS.md
   - 4 pre-existing path decoupling updates from repo relocation:
     docs/M2_DOUYIN_COMPLETE_HANDOFF.md, src/downloader/credentials.py,
     tests/test_downloader_worker.py, tests/test_raw_archiver.py

4. CURRENT TASK:
   M3-01: CanonicalMediaAssetAdapter -> COMPLETED.
   Next Task: M3-02 Video / ASR Integration.

5. COMPLETED WORK:
   - Researched D07 asset_manifest.json format and C10 formal video/album assets.
   - Built CanonicalMediaAsset and AlbumImageArtifact domain models.
   - Built CanonicalMediaAssetAdapter with hash validation and metadata.db enrichment.
   - Built 15 comprehensive unit & smoke tests in tests/test_media_adapter.py.
   - Created M3 documentation triad (M3_TASKS.md, M3_DECISIONS.md, M3_HANDOFF.md).

6. REMAINING WORK (M3-02+):
   - M3-02: Adapt pipeline.py so that video CanonicalMediaAsset streams into ASR
     without requiring manual intake folder drops or redundant remuxing.
   - M3-03: Implement image album visual OCR/VLM inspection for album_images.
   - M3-04: Bind grounded provenance and collector metadata to final knowledge evidence.

7. EXACT NEXT COMMANDS TO RUN:
   # Step A: Run M3 adapter tests
   .\.venv\Scripts\python.exe -m pytest tests/test_media_adapter.py -v

   # Step B: Run full test regression
   .\.venv\Scripts\python.exe -m pytest tests -q

8. KEY FILES TO READ:
   - docs/M3_HANDOFF.md (this document)
   - docs/M3_DECISIONS.md (architectural boundaries)
   - docs/M3_TASKS.md (task progression board)
   - src/media_adapter/models.py
   - src/media_adapter/adapter.py
   - src/pipeline.py

9. CORE INVARIANTS (DO NOT BREAK):
   - DO NOT modify src/collector/ or src/downloader/ (M2 is frozen).
   - DO NOT make network calls or live Douyin requests.
   - DO NOT touch or delete M2 runtime data (data/metadata.db, archive/douyin/).
   - Keep CanonicalMediaAssetAdapter strictly read-only and deterministic.
================================================================================
```
