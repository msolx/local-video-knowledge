"""Douyin-specific collector configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import CollectorConfig


@dataclass
class DouyinCollectorConfig(CollectorConfig):
    """Configuration specific to Douyin Collection Ingestion."""
    platform: str = "douyin"
    aid: int = 6383
    device_platform: str = "webapp"
    channel: str = "channel_pc_web"
    consecutive_known_threshold: int = 3

    @classmethod
    def from_dict(cls, raw: dict[str, Any], base_path: Path | None = None) -> DouyinCollectorConfig:
        base_cfg = super().from_dict(raw, base_path=base_path)
        extra = base_cfg.extra or {}
        return cls(
            platform="douyin",
            runtime_root=base_cfg.runtime_root,
            raw_archive_root=base_cfg.raw_archive_root,
            canonical_root=base_cfg.canonical_root,
            database_path=base_cfg.database_path,
            log_level=base_cfg.log_level,
            lock_timeout_sec=base_cfg.lock_timeout_sec,
            page_size=base_cfg.page_size,
            headless=base_cfg.headless,
            profile_path=base_cfg.profile_path,
            aid=int(extra.get("aid", 6383)),
            device_platform=str(extra.get("device_platform", "webapp")),
            channel=str(extra.get("channel", "channel_pc_web")),
            consecutive_known_threshold=int(extra.get("consecutive_known_threshold", 3)),
            extra=extra,
        )
