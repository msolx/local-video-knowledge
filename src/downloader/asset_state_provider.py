"""Formal Archive Asset State Provider (DY-D10).

Adapter connecting Collector C09 Queue Producer to D07 Formal Local Asset verifier.
Enables Collector to recognize existing, valid formal assets across sync runs and avoid
redundant download task generation.

Key Invariants:
1. Strict Read-Only Semantics:
   - Queries file system and manifest commitments without mutating Collector DB
     or attempting repairs.
2. Canonical Decoupled Identity:
   - Resolves canonical destination using (platform, platform_content_id) via D07.
   - Physical asset identity is decoupled from scope_id.
3. Cryptographic Verification:
   - Returns True only when destination directory exists, contains asset_manifest.json,
     and passes verify_archived_asset validation.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.collector.download_models import AssetStateProvider
from src.downloader.promoter import verify_archived_asset

logger = logging.getLogger(__name__)


class FormalArchiveAssetStateProvider(AssetStateProvider):
    """Production read-only adapter satisfying C09 AssetStateProvider protocol."""

    def __init__(self, archive_root: str | Path) -> None:
        self.archive_root = Path(archive_root)

    def resolve_canonical_destination(self, platform: str, platform_content_id: str) -> Path:
        """Resolves authoritative storage destination for physical media identity."""
        return self.archive_root / platform / platform_content_id

    def has_valid_asset(
        self,
        platform: str,
        scope_id: str,
        platform_content_id: str,
        content_type: str,
    ) -> bool:
        """Determines whether a formal, verified local asset already exists.

        Returns True (AVAILABLE_VALID) if:
        1. Destination folder exists.
        2. Manifest commitment exists.
        3. verify_archived_asset passes verification.
        Returns False if absent, corrupt, or truncated.
        """
        dest = self.resolve_canonical_destination(platform, platform_content_id)
        if not dest.exists() or not dest.is_dir():
            return False

        manifest_file = dest / "asset_manifest.json"
        if not manifest_file.exists() or not manifest_file.is_file():
            return False

        try:
            res = verify_archived_asset(dest)
            if res.valid:
                logger.debug(
                    "Formal asset verified for (%s, %s): %d promoted assets in %s",
                    platform,
                    platform_content_id,
                    len(res.assets),
                    dest,
                )
                return True
            else:
                logger.warning(
                    "Formal asset invalid for (%s, %s) in %s: %s",
                    platform,
                    platform_content_id,
                    dest,
                    res.error,
                )
                return False
        except Exception as exc:
            logger.warning(
                "Exception verifying formal asset (%s, %s) in %s: %s",
                platform,
                platform_content_id,
                dest,
                exc,
            )
            return False
