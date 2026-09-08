from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .storage import atomic_write_json, load_json, utc_now

logger = logging.getLogger(__name__)

SIDECAR_SUFFIX = ".source.json"
PLATFORMS = {"douyin", "bilibili", "youtube", "other"}
EVIDENCE_MANIFEST_FILENAME = "evidence_manifest.json"
EVIDENCE_SCHEMA_VERSION = "evidence-manifest-v1"


# ----------------------------------------------------------------------
# Legacy Intake / M1 Sidecar Compatibility Layer
# ----------------------------------------------------------------------

def source_sidecar_path(media_path: Path) -> Path:
    return media_path.with_name(f"{media_path.name}{SIDECAR_SUFFIX}")


def normalize_source(source: dict[str, Any]) -> dict[str, Any]:
    """Keep only objective source fields; unknown values remain null."""
    result = dict(source)
    result["platform"] = result.get("platform") if result.get("platform") in PLATFORMS else "other"
    for key in ("source_type", "source_url", "platform_content_id", "author_name", "author_id", "title", "published_at", "collected_at", "original_filename"):
        result.setdefault(key, None)
    return result


def write_source_sidecar(media_path: Path, source: dict[str, Any]) -> Path:
    path = source_sidecar_path(media_path)
    atomic_write_json(path, {"schema_version": "source-v1", "source": normalize_source(source)})
    return path


def read_source_sidecar(media_path: Path) -> dict[str, Any] | None:
    payload = load_json(source_sidecar_path(media_path), {})
    source = payload.get("source") if isinstance(payload, dict) else None
    return normalize_source(source) if isinstance(source, dict) else None


def source_for_media(*media_paths: str | Path | None) -> dict[str, Any] | None:
    """Find the first source sidecar shared by a complete asset or stream pair."""
    for value in media_paths:
        if value:
            source = read_source_sidecar(Path(value))
            if source is not None:
                return source
    return None


# ----------------------------------------------------------------------
# M3-04: Metadata & Evidence Provenance Binding Layer
# ----------------------------------------------------------------------

@dataclass
class EvidenceItem:
    """Represents a single model-observed piece of evidence.

    INVARIANT: Evidence Is Not Truth. verification_status defaults to 'not_checked'.
    """
    evidence_id: str
    modality: str  # 'speech', 'visual_text', 'visual_description'
    source_artifact: dict[str, Any]
    temporal: dict[str, Any] | None = None
    sequence: dict[str, Any] | None = None
    model_provenance: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    verification_status: str = "not_checked"

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "modality": self.modality,
            "source_artifact": self.source_artifact,
            "temporal": self.temporal,
            "sequence": self.sequence,
            "model_provenance": self.model_provenance,
            "payload": self.payload,
            "verification_status": self.verification_status,
        }


def extract_source_metadata_snapshot(
    canonical_asset: Any, processed_dir: Path | None = None
) -> dict[str, Any]:
    """Extracts an immutable source metadata snapshot from the canonical asset.

    SEMANTICS:
      - published_at: Content creator's publication timestamp on the platform.
      - first_seen_at: Time the collector first observed this item.
      - Strictly NO fake 'collected_at'.
    """
    sm = dict(getattr(canonical_asset, "source_metadata", None) or {})
    if not sm and processed_dir:
        meta_file = processed_dir / "metadata.json"
        if meta_file.is_file():
            meta = load_json(meta_file, {})
            if isinstance(meta.get("source_metadata"), dict):
                sm = dict(meta["source_metadata"])
            elif isinstance(meta.get("source"), dict):
                sm = dict(meta["source"])

    author = sm.get("author")
    author_name: str | None = None
    author_id: str | None = None
    if isinstance(author, dict):
        author_name = author.get("display_name") or author.get("username")
        author_id = author.get("platform_author_id") or author.get("sec_uid")
    elif isinstance(author, str):
        author_name = author

    if not author_name and sm.get("author_name"):
        author_name = sm.get("author_name")
    if not author_id and sm.get("author_id"):
        author_id = sm.get("author_id")

    title = sm.get("title")
    if not title and getattr(canonical_asset, "title", None) != getattr(canonical_asset, "platform_content_id", None):
        title = getattr(canonical_asset, "title", None)

    published_at = sm.get("published_at")
    first_seen_at = sm.get("first_seen_at")
    if not first_seen_at and isinstance(sm.get("collection"), dict):
        first_seen_at = sm["collection"].get("first_seen_at")

    cid = getattr(canonical_asset, "platform_content_id", "unknown")
    source_url = sm.get("source_url") or f"https://www.douyin.com/video/{cid}"
    tags = list(sm.get("tags") or [])

    is_enriched = bool(title or author_name or tags or published_at or first_seen_at)
    enrichment_status = "enriched" if is_enriched else "unenriched"

    return {
        "enrichment_status": enrichment_status,
        "title": title,
        "author_name": author_name,
        "author_id": author_id,
        "published_at": published_at,
        "first_seen_at": first_seen_at,
        "source_url": source_url,
        "tags": tags,
    }


def extract_formal_asset_binding(canonical_asset: Any) -> dict[str, Any]:
    """Binds formal archive manifest identity, artifacts, and M2 download provenance."""
    artifacts: list[dict[str, Any]] = []
    if getattr(canonical_asset, "is_video", False) and canonical_asset.video_path:
        artifacts.append({
            "file_name": canonical_asset.video_path.name,
            "role": "PRIMARY_VIDEO",
            "sha256": canonical_asset.video_sha256,
            "byte_size": canonical_asset.video_size_bytes,
            "sequence_index": None,
        })
    elif getattr(canonical_asset, "is_album", False) and canonical_asset.album_images:
        for img in sorted(canonical_asset.album_images, key=lambda x: x.sequence_index):
            artifacts.append({
                "file_name": img.file_name,
                "role": "ALBUM_IMAGE",
                "sha256": img.sha256,
                "byte_size": getattr(img, "size_bytes", getattr(img, "byte_size", 0)),
                "sequence_index": img.sequence_index,
            })

    if getattr(canonical_asset, "audio_path", None):
        artifacts.append({
            "file_name": canonical_asset.audio_path.name,
            "role": "AUDIO_TRACK",
            "sha256": canonical_asset.audio_sha256,
            "byte_size": canonical_asset.audio_size_bytes,
            "sequence_index": None,
        })

    content_type_val = (
        canonical_asset.content_type.value
        if hasattr(canonical_asset.content_type, "value")
        else str(canonical_asset.content_type)
    )

    return {
        "canonical_id": canonical_asset.canonical_id,
        "platform": canonical_asset.platform,
        "platform_content_id": canonical_asset.platform_content_id,
        "content_type": content_type_val,
        "asset_root": str(canonical_asset.asset_root),
        "manifest_path": str(canonical_asset.manifest_path),
        "archived_at": canonical_asset.archived_at,
        "source_provenance": dict(canonical_asset.source_provenance or {}),
        "artifacts": artifacts,
    }


def collect_evidence_items(
    canonical_asset: Any, processed_dir: Path
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, int]]:
    """Gathers all derived evidence items (video ASR segments or album visual/OCR items)."""
    items: list[dict[str, Any]] = []
    model_prov: dict[str, Any] = {}
    summary_counts: dict[str, int] = {
        "total_items": 0,
        "speech_segments": 0,
        "visual_ocr_items": 0,
        "visual_vlm_items": 0,
    }

    # Video ASR evidence
    if getattr(canonical_asset, "is_video", False):
        transcript_path = processed_dir / "transcript.json"
        if transcript_path.is_file():
            data = load_json(transcript_path, {})
            prov = data.get("provenance", {})
            status = data.get("status", "completed")
            model_prov["asr"] = {
                "backend": prov.get("backend", "faster_whisper"),
                "model": prov.get("model", "large-v3"),
                "compute_type": prov.get("settings", {}).get("compute_type", "float16"),
                "beam_size": prov.get("settings", {}).get("beam_size", 5),
                "status": status,
            }
            if status != "NO_AUDIO":
                raw_segments = data.get("segments", [])
                sorted_segments = sorted(
                    raw_segments, key=lambda s: (float(s.get("start", 0.0)), s.get("id", ""))
                )
                video_name = (
                    canonical_asset.video_path.name
                    if getattr(canonical_asset, "video_path", None)
                    else Path(data.get("source_video", "")).name
                )
                video_sha = canonical_asset.video_sha256 or data.get("content_hash")
                for idx, seg in enumerate(sorted_segments):
                    seg_id = seg.get("id") or f"seg_{idx+1:06d}"
                    start_t = float(seg.get("start", 0.0))
                    end_t = float(seg.get("end", 0.0))
                    item = EvidenceItem(
                        evidence_id=f"ev_{seg_id}",
                        modality="speech",
                        source_artifact={
                            "role": "PRIMARY_VIDEO",
                            "file_name": video_name,
                            "sha256": video_sha,
                        },
                        temporal={
                            "start": start_t,
                            "end": end_t,
                            "duration": round(end_t - start_t, 3),
                            "segment_id": seg_id,
                            "segment_order": idx + 1,
                        },
                        sequence=None,
                        model_provenance={
                            "stage": "asr",
                            "backend": prov.get("backend", "faster_whisper"),
                            "model": prov.get("model", "large-v3"),
                            "compute_type": prov.get("settings", {}).get("compute_type", "float16"),
                        },
                        payload={
                            "text": seg.get("text", "").strip(),
                        },
                        verification_status="not_checked",
                    )
                    items.append(item.to_dict())
                summary_counts["speech_segments"] = len(items)

    # Album Visual evidence
    if getattr(canonical_asset, "is_album", False):
        vt_path = processed_dir / "visual" / "visual_transcript.json"
        if vt_path.is_file():
            vt = load_json(vt_path, {})
            model_prov["ocr"] = {
                "engine": "PaddleOCR",
                "model": "PP-OCRv6",
                "minimum_confidence": 0.65,
            }
            model_prov["vlm"] = {
                "backend": "lm-studio",
                "model": "qwen2-vl-7b-instruct",
                "status": "unresolved_visual_reference",
            }
            raw_evidence = vt.get("visual_evidence", [])
            sorted_evidence = sorted(
                raw_evidence,
                key=lambda e: (
                    e.get("sequence_index", 0),
                    0 if e.get("source_type") == "visual_ocr" else 1,
                    e.get("id", ""),
                ),
            )
            for ev in sorted_evidence:
                source_type = ev.get("source_type", "visual_ocr")
                seq_idx = ev.get("sequence_index")
                fname = ev.get("source_image_file")
                sha = ev.get("source_image_sha256")
                ev_status = ev.get("status", "completed")

                if source_type == "visual_ocr":
                    item = EvidenceItem(
                        evidence_id=ev.get("id"),
                        modality="visual_text",
                        source_artifact={
                            "role": "ALBUM_IMAGE",
                            "file_name": fname,
                            "sha256": sha,
                            "sequence_index": seq_idx,
                            "byte_size": ev.get("byte_size"),
                        },
                        temporal=None,
                        sequence={
                            "sequence_index": seq_idx,
                        },
                        model_provenance={
                            "stage": "visual",
                            "engine": ev.get("ocr_engine", "PaddleOCR"),
                            "model": "PP-OCRv6",
                        },
                        payload={
                            "text": ev.get("text", ""),
                            "confidence": ev.get("confidence"),
                            "lines": ev.get("lines", []),
                            "ocr_status": ev_status,
                        },
                        verification_status="not_checked",
                    )
                    items.append(item.to_dict())
                    summary_counts["visual_ocr_items"] += 1
                else:
                    item = EvidenceItem(
                        evidence_id=ev.get("id"),
                        modality="visual_description",
                        source_artifact={
                            "role": "ALBUM_IMAGE",
                            "file_name": fname,
                            "sha256": sha,
                            "sequence_index": seq_idx,
                        },
                        temporal=None,
                        sequence={
                            "sequence_index": seq_idx,
                        },
                        model_provenance={
                            "stage": "visual",
                            "backend": "lm-studio",
                            "model": "qwen2-vl-7b-instruct",
                        },
                        payload={
                            "status": ev_status,
                            "description": ev.get("description"),
                            "reason": ev.get("reason", ev_status),
                        },
                        verification_status="not_checked",
                    )
                    items.append(item.to_dict())
                    summary_counts["visual_vlm_items"] += 1

    summary_counts["total_items"] = len(items)
    return items, model_prov, summary_counts


def compute_manifest_fingerprint(
    canonical_id: str,
    source_metadata: dict[str, Any],
    formal_asset: dict[str, Any],
    evidence_items: list[dict[str, Any]],
    model_provenance: dict[str, Any],
) -> str:
    """Computes a deterministic SHA-256 fingerprint over identity, metadata, artifacts, and evidence."""
    hasher = hashlib.sha256()
    hasher.update(canonical_id.encode("utf-8"))
    hasher.update(json.dumps(source_metadata, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    hasher.update(json.dumps(formal_asset.get("artifacts", []), sort_keys=True, ensure_ascii=False).encode("utf-8"))
    hasher.update(json.dumps(model_provenance, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    for item in evidence_items:
        hasher.update(json.dumps(item, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return hasher.hexdigest()


def build_evidence_manifest(
    canonical_asset: Any,
    config: Any | None = None,
    processed_dir: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Constructs the canonical evidence manifest dictionary with deterministic fingerprinting.

    If a valid evidence_manifest.json with matching fingerprint already exists and force=False,
    returns the cached manifest immediately (sub-second resume < 1ms).
    """
    pdir = processed_dir or (
        config.data_root / "processed" / canonical_asset.canonical_id
        if config and hasattr(config, "data_root")
        else Path("data/processed") / canonical_asset.canonical_id
    )

    manifest_file = pdir / EVIDENCE_MANIFEST_FILENAME
    source_meta = extract_source_metadata_snapshot(canonical_asset, pdir)
    formal_binding = extract_formal_asset_binding(canonical_asset)
    evidence_items, model_prov, summary_counts = collect_evidence_items(canonical_asset, pdir)

    proc_file = pdir / "processing.json"
    proc_state = load_json(proc_file, {}) if proc_file.is_file() else {}
    stages_executed = [
        st for st, val in proc_state.get("stages", {}).items()
        if val.get("status") in ("completed", "skipped")
    ]

    processing_provenance = {
        "processed_dir": str(pdir.resolve()),
        "config_fingerprint": (config.fingerprint if config and hasattr(config, "fingerprint") else None) or proc_state.get("config_fingerprint"),
        "stages_executed": stages_executed,
        "model_provenance": model_prov,
    }

    manifest_fp = compute_manifest_fingerprint(
        canonical_asset.canonical_id,
        source_meta,
        formal_binding,
        evidence_items,
        model_prov,
    )

    if manifest_file.is_file() and not force:
        cached = load_json(manifest_file, {})
        if isinstance(cached, dict) and cached.get("manifest_fingerprint") == manifest_fp:
            return cached

    manifest = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "manifest_fingerprint": manifest_fp,
        "generated_at": utc_now(),
        "canonical_id": canonical_asset.canonical_id,
        "platform": canonical_asset.platform,
        "platform_content_id": canonical_asset.platform_content_id,
        "content_type": formal_binding["content_type"],
        "source_metadata": source_meta,
        "formal_asset": formal_binding,
        "processing_provenance": processing_provenance,
        "evidence_summary": {
            **summary_counts,
            "verification_status": "not_checked",
        },
        "evidence_items": evidence_items,
    }
    return manifest


def write_evidence_manifest(
    canonical_asset: Any,
    config: Any | None = None,
    processed_dir: Path | None = None,
    force: bool = False,
) -> Path:
    """Builds and atomically writes data/processed/<canonical_id>/evidence_manifest.json."""
    pdir = processed_dir or (
        config.data_root / "processed" / canonical_asset.canonical_id
        if config and hasattr(config, "data_root")
        else Path("data/processed") / canonical_asset.canonical_id
    )
    pdir.mkdir(parents=True, exist_ok=True)
    manifest_file = pdir / EVIDENCE_MANIFEST_FILENAME

    manifest = build_evidence_manifest(
        canonical_asset, config=config, processed_dir=pdir, force=force
    )

    # Check if existing file is already identical
    if manifest_file.is_file() and not force:
        cached = load_json(manifest_file, {})
        if isinstance(cached, dict) and cached.get("manifest_fingerprint") == manifest["manifest_fingerprint"]:
            return manifest_file

    atomic_write_json(manifest_file, manifest)
    logger.info("Saved evidence manifest: %s (fingerprint: %s)", manifest_file, manifest["manifest_fingerprint"])
    return manifest_file


def load_evidence_manifest(processed_dir_or_file: Path | str) -> dict[str, Any] | None:
    path = Path(processed_dir_or_file)
    target = path if path.is_file() else path / EVIDENCE_MANIFEST_FILENAME
    if not target.is_file():
        return None
    data = load_json(target, {})
    return data if isinstance(data, dict) and data.get("schema_version") == EVIDENCE_SCHEMA_VERSION else None


def verify_evidence_manifest(manifest_or_file: dict[str, Any] | Path | str) -> bool:
    """Validates structural integrity, fingerprint consistency, and invariant checks."""
    if isinstance(manifest_or_file, (Path, str)):
        manifest = load_evidence_manifest(manifest_or_file)
        if not manifest:
            return False
    else:
        manifest = manifest_or_file

    if manifest.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        return False

    # Check verification status invariant
    if manifest.get("evidence_summary", {}).get("verification_status") != "not_checked":
        return False

    for item in manifest.get("evidence_items", []):
        if item.get("verification_status") != "not_checked":
            return False

    # Check fingerprint determinism
    expected_fp = compute_manifest_fingerprint(
        manifest.get("canonical_id", ""),
        manifest.get("source_metadata", {}),
        manifest.get("formal_asset", {}),
        manifest.get("evidence_items", []),
        manifest.get("processing_provenance", {}).get("model_provenance", {}),
    )
    return manifest.get("manifest_fingerprint") == expected_fp

