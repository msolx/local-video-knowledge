# Milestone M3: Media Knowledge Integration · Architectural Decision Log

> **Context**: Transitioning from Milestone M2 (Douyin Ingestion -> Formal Local Asset) to Milestone M3 (Formal Local Asset -> Media Processing -> Grounded Evidence).  
> **Status**: APPROVED / ACTIVE

---

## Decision 1: Boundary of `CanonicalMediaAssetAdapter`
- **Context**: The existing media pipeline (`src/pipeline.py`) was designed in M1 to scan an incoming manual folder (`data/incoming/manual`) and probe/mux raw input files. M2 D07 introduced formal archive directories with `asset_manifest.json`.
- **Decision**: Create an explicit, deterministic adapter layer (`src/media_adapter/`) that parses and verifies `asset_manifest.json` from formal archive roots and yields a domain model `CanonicalMediaAsset`.
- **Boundary Invariants**:
  - The adapter is strictly **read-only** and **deterministic**.
  - **No network calls** (no Douyin API, no F2, no browser automation, no external scraping).
  - **No ML inference** in the adapter (no ASR, OCR, VLM, LLM, or RAG in the adapter itself).
  - **Zero credential dependencies**.
- **Rejected Alternatives**:
  - *Symlinking / copying formal assets into `data/incoming/manual`*: Violates storage efficiency, creates duplicate copies of large 4K videos, loses structured manifest metadata.
  - *Embedding ML pipeline calls directly inside the adapter*: Violates single responsibility principle and complicates offline testing.

---

## Decision 2: `CanonicalMediaAsset` Contract Design
- **Context**: Need a lightweight, strongly-typed domain model representing both video and image album assets without bloat.
- **Decision**:
  - `CanonicalMediaAsset` models:
    - Identity: `platform`, `platform_content_id`, `canonical_id`, `content_type` (`video` vs `image_album`).
    - Paths: `asset_root`, `manifest_path`.
    - Payloads:
      - Video: `video_path`, `video_sha256`, `video_size_bytes` (populated for `video`).
      - Image Album: `album_images` (strictly ordered list of `AlbumImageArtifact` with `sequence_index`, `path`, `sha256`, `size_bytes`).
      - Audio: `audio_path`, `audio_sha256` (optional standalone BGM track).
    - Provenance: `archived_at`, `source_provenance` (task_id, scope_id, etc.), `source_metadata` (title, author, published_at, etc.).
  - Projection to legacy pipeline:
    - Provides `.to_pipeline_media_asset(config)` for video assets to directly feed into `pipeline.process_asset()` without re-muxing or manual copying.
- **Rejected Alternatives**:
  - *Forcing Image Album into a fake single-video container*: Image albums are inherently multi-image; synthesizing a slideshow video causes loss of original high-res image quality and complicates OCR.

---

## Decision 3: M2 Codebase Freeze Boundary
- **Context**: M2 (DY-C01 ~ C10, DY-D01 ~ D10) is 100% complete and frozen under tag `m2-douyin-complete-r1`.
- **Decision**:
  - Subsystems `src/collector/` and `src/downloader/` are **strictly frozen**. M3 code MUST NOT modify M2 business logic, models, or schemas.
  - M3 resides in new modular packages (`src/media_adapter/`, etc.) that read M2 outputs (`archive/`, `metadata.db`) as stable external contracts.
  - If any M2 contract modification is deemed necessary in the future, the agent must STOP, record justification in `docs/M3_HANDOFF.md`, and obtain explicit user authorization.

---

## Decision 4: Video vs Image Album Formal Asset Handling
- **Context**: M2 formal video assets consist of a primary `.mp4` video (e.g. C10 173MB 4K video) and `asset_manifest.json`. Image album assets consist of ordered `.webp` images (e.g. C10 3 images) and `asset_manifest.json`, with optional audio BGM.
- **Decision**:
  - Role parsing: Manifest roles may appear as `"ArtifactRole.PRIMARY_VIDEO"` or `"PRIMARY_VIDEO"`, `"ArtifactRole.ALBUM_IMAGE"` or `"ALBUM_IMAGE"`. The adapter normalizes both representations cleanly.
  - Sequence ordering: Album images are strictly sorted by `sequence_index` (1, 2, 3...).
  - Optional BGM: Never assume an image album contains a BGM track. If present (e.g. role `AUDIO_TRACK` or `BGM`), it is exposed via `audio_path`; otherwise `audio_path` is `None`.

---

## Decision 5: Provenance and Collector Metadata Sourcing
- **Context**: `asset_manifest.json` contains runtime download provenance (`task_id`, `scope_id`, `execution_id`, `source_sync_run_id`), but high-level semantic metadata (`title`, `author`, `published_at`, `source_url`, `tags`) is maintained in `metadata.db` by the collector.
- **Decision**:
  - `CanonicalMediaAssetAdapter` supports optional read-only enrichment from `data/metadata.db`.
  - When `metadata_db_path` is provided and valid, the adapter performs a read-only query on `collection_items` for the `platform_content_id` to populate `source_metadata`.
  - If `metadata.db` is not present (e.g. in isolated unit test harnesses), `source_metadata` gracefully defaults to empty or manifest-derived metadata.

---

## Decision 6: Decoupled Metadata DB Architecture & Formal Asset Independence
- **Context**: Audit whether `CanonicalMediaAssetAdapter` couples hardcodedly to `data/metadata.db` or allows completely standalone operation.
- **Decision**:
  - **Zero Hardcoded Coupling**: `CanonicalMediaAssetAdapter` never hardcodes the path `data/metadata.db`. `metadata_db_path` defaults to `None`.
  - **Standalone Core Ingestion**: Formal asset loading (`load_from_dir`) relies solely on the asset directory and `asset_manifest.json`. File integrity, ordering, type discrimination, and path extraction function 100% independently without any SQLite database present.
  - **Explicit Enrichment Toggle**: Introduced `enable_metadata_enrichment: bool = True` in `CanonicalMediaAssetAdapter.__init__`. Callers can explicitly disable DB queries even if a database path is supplied.
  - **Non-blocking Resilience**: SQLite access is strictly read-only (`?mode=ro`). Any SQLite error (file missing, schema mismatch, lock, corruption, invalid JSON) is caught and handled gracefully: `source_metadata` safely defaults to `{}` without failing or blocking asset loading.
  - **Zero Network**: Adapter operations remain entirely local and offline.

---

## Decision 7: Video / ASR Integration Pipeline Architecture (M3-02)
- **Context**: Milestone M2 stores formal video assets in `archive/douyin/<content_id>/` with `asset_manifest.json`. M1 media pipeline (`src/pipeline.py`) was designed for incoming manual directory drops and performed 6 full stages (`source`, `audio`, `asr`, `visual`, `knowledge`, `publish_media`). M3-02 requires integrating formal video assets directly into audio extraction and ASR without unnecessary re-ingest, secondary copying, or premature visual/knowledge processing.
- **Decision**:
  - **Direct Streamline via `CanonicalMediaAsset`**:
    - `CanonicalMediaAsset` is projected to `MediaAsset` via `to_pipeline_media_asset(config)`, binding canonical provenance (`canonical_id`, `platform`, `platform_content_id`).
    - Introduced `process_canonical_asset(config, canonical_asset, force=False, stop_after="asr")` in `src/pipeline.py` and `CanonicalMediaAsset.process_asr(config, force=False)` in `src/media_adapter/models.py`.
    - Formal assets are never copied to `data/incoming/manual`. Raw originals are linked/referenced directly by absolute path.
  - **Stage Cutoff Support (`stop_after`)**:
    - Added `stop_after: str | None = None` parameter to `pipeline.process_asset()`, allowing execution to stop cleanly after any stage (`"source"`, `"audio"`, `"asr"`, `"visual"`, `"knowledge"`).
    - For M3-02, default is `stop_after="asr"`.
  - **Strict Immutability of Formal Assets**:
    - Processing outputs are written solely to `data/processed/<video_id>/` (`manifest.json`, `audio.wav`, `transcript.json`, `transcript.md`).
    - The source `archive/` directory is **strictly read-only**. Zero files are written to, modified in, or deleted from `archive/`.
  - **Audio Extraction Source Invariant**:
    - Audio extraction uses strictly `PRIMARY_VIDEO` as input. FFmpeg extracts 16kHz mono PCM WAV.
    - External URLs or synthetic tracks are never used as speech source.
  - **Explicit `NO_AUDIO` State Handling**:
    - If a video has no audio track (`not asset.probe.audio`), `extract_audio` records `{"status": "skipped", "reason": "NO_AUDIO", "artifacts": []}`.
    - `run_asr` handles `NO_AUDIO` gracefully with empty segments, `status: "NO_AUDIO"`, and a clear markdown notice. No fake empty transcripts are hallucinated.
    - Pipeline stages check `previous.get("status") in ("completed", "skipped")` to preserve skip semantics.
  - **Idempotency and Resume Protocol**:
    - Before running ASR, the pipeline verifies whether `transcript.json` exists with matching `source_video_hash` (or matching video SHA-256) and identical ASR model settings (`model_name`, `backend`, `compute_type`, `beam_size`, etc.).
    - When matching, ASR inference is skipped, achieving sub-second resume without GPU load.
    - `force=True` bypasses cached artifacts when explicit re-transcription is requested.
  - **Provenance Preservation**:
    - `transcript.json` is enriched with canonical provenance (`canonical_id`, `platform`, `platform_content_id`, `source_video`, `content_hash`).
- **Rejected Alternatives**:
  - *Symlinking or copying formal video into `data/incoming/`*: Violates storage efficiency and creates orphan files.
  - *Writing transcripts or audio directly into `archive/`*: Violates M2 formal archive immutability and separation of raw assets from processed intelligence.
  - *Running visual OCR/VLM or knowledge summarization during M3-02*: Violates task staging boundaries (M3-03 and M3-04 handle visual inspection and knowledge synthesis).
