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
| **Current Task** | **M3-01: CanonicalMediaAssetAdapter** (`DONE / FULLY RECONCILED`) |
| **Active Test Baseline** | **682 passed, 10 skipped** (Main `.venv`: 666 baseline + 16 M3 adapter tests; Worker `.venv-f2`: 55 passed, 10 deselected) |

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

### 3.2 Regression Baseline Discrepancy Reconciliation (672/4 -> 682/10)
- **M2 Checkpoint Baseline**: 672 passed, 4 skipped (Total collected: 676)
- **Current M3-01 Main Suite**: 682 passed, 10 skipped (Total collected: 692 = 676 M2 tests + 16 new M3 tests)
  - Old tests executing: 666 passed, 10 skipped
  - New M3 tests: 16 passed
  - Total: 666 + 16 = 682 passed.
- **The 4 Pre-Existing M2 Skips (Worker Environment Isolation)**:
  1. `tests/test_f2_backend.py:885`: `Requires F2 installed in worker environment (.venv-f2)`
  2. `tests/test_f2_backend.py:988`: `Requires F2 installed in worker environment (.venv-f2)`
  3. `tests/test_downloader_worker.py:1519`: `Live network acquisition requires dedicated F2 worker environment (.venv-f2) and authenticated Chrome profile.`
  4. `tests/test_downloader_worker.py:1625`: `Live network acquisition requires dedicated F2 worker environment (.venv-f2) and authenticated Chrome profile.`
- **The 6 Tests Transitioning from Passed to Skipped**:
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
Current Status : M3-01 DONE, ready for M3-02.

1. CURRENT BRANCH:
   feat/m3-media-knowledge-integration

2. CURRENT HEAD / BASE:
   Commit: fix(m3): finalize media adapter boundaries (incorporates M3-01 final reconciliation)
   Base: ffe8aa1d7e937e91a6270a042e603e0563d402e2 (main / m2-douyin-complete-r1)

3. COMPONENTS:
   - src/media_adapter/ (CanonicalMediaAsset, AlbumImageArtifact, CanonicalMediaAssetAdapter)
   - tests/test_media_adapter.py (16 unit and offline integration smoke tests)
   - docs/M3_HANDOFF.md, docs/M3_DECISIONS.md, docs/M3_TASKS.md

4. CURRENT TASK:
   M3-01: CanonicalMediaAssetAdapter -> DONE & RECONCILED.
   Next Task: M3-02 Video / ASR Integration.

5. COMPLETED WORK (M3-01):
   - Ingested M2 formal assets (video & image album) via asset_manifest.json.
   - Built CanonicalMediaAsset and AlbumImageArtifact domain models.
   - Built CanonicalMediaAssetAdapter with hash verification, 1-indexed ordering, and BGM detection.
   - Decoupled metadata.db: optional, configurable, explicit enable_metadata_enrichment toggle,
     and failure resilience (corrupt DB does not block asset loading).
   - Built 16 unit & smoke tests (all passing).
   - Reconciled 672 -> 682/10 baseline behavior (profile captcha expiration, zero regression).

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
