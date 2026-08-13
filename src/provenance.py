from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .storage import atomic_write_json, load_json


SIDECAR_SUFFIX = ".source.json"
PLATFORMS = {"douyin", "bilibili", "youtube", "other"}


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
