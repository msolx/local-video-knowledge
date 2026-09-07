"""Deterministic Canonical Transform Engine for Douyin Collections (DY-C07).

Transforms Douyin raw `listcollection` item payloads into frozen canonical
representations:
  - `collection-item-v1`: Content entity representation
  - `collection-observation-v1`: Discrete observation event representation

Contract & Invariant Guarantees:
  1. Purely deterministic: No network, no browser, no database/SQLite imports.
  2. No system clock dependency: `datetime.now()` and `utcnow()` are strictly forbidden.
     Timestamps come strictly from `TransformContext` and `raw_item['create_time']`.
  3. No random UUIDs: Observation ID is deterministic `obs_{sync_run_id}_{platform_content_id}`.
  4. Historical fields (`first_seen_at`, `observed_count`, `reappeared_at`, `active`)
     cannot be inferred from raw items alone; they must accept explicit `TransformContext`
     and optional `PriorCollectionState`.
  5. `create_time` is Unix seconds -> converted strictly to UTC ISO 8601 `published_at`.
     Never used as `collected_at` or `first_seen_at`.
  6. Pagination cursors reside exclusively in `CollectionObservation`, never in `CollectionItem`.
  7. Full JSON Schema Draft 2020-12 validation against canonical contracts.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jsonschema

logger = logging.getLogger(__name__)


# ============================================================================
# Exception Hierarchy
# ============================================================================


class TransformError(Exception):
    """Base exception for transform pipeline failures."""


class TransformInvalidRawError(TransformError):
    """Raised when raw payload is not a dict or lacks fundamental structure."""


class TransformMissingIdentityError(TransformError):
    """Raised when raw item lacks platform content identity (aweme_id)."""


class TransformSchemaValidationError(TransformError):
    """Raised when canonical result fails JSON schema contract validation."""


# ============================================================================
# Context & State Models
# ============================================================================


@dataclass(frozen=True)
class RawRefContext:
    """Provenance pointer to the preserved raw API response archive."""

    archive_file: str
    sha256: str
    item_index: int
    platform_raw_type: str = "douyin.listcollection.aweme_struct"

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform_raw_type": self.platform_raw_type,
            "archive_file": self.archive_file,
            "sha256": self.sha256,
            "item_index": self.item_index,
        }


@dataclass(frozen=True)
class TransformContext:
    """Execution context provided by the caller (Sync Engine / Pipeline).

    Provides the external temporal and positional coordinates that CANNOT
    be inferred from Douyin raw payloads alone.
    """

    sync_run_id: str
    observed_at: str  # ISO 8601 UTC timestamp, e.g. "2026-09-05T07:00:00Z"
    page_number: int  # 1-based page index
    position_in_page: int  # 0-based index within the page's aweme_list
    global_rank_seen: int  # 1-based overall position in the current sync run
    request_cursor: str  # Request cursor decimal string
    response_cursor: str  # Response envelope cursor decimal string
    raw_ref: RawRefContext | None = None
    is_reappearance_event: bool = False


@dataclass(frozen=True)
class PriorCollectionState:
    """Historical persistent entity state of an item known from local database.

    Does not carry transient observation events.
    """

    exists: bool
    active: bool
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    reappeared_at: str | None = None
    observed_count: int = 0
    last_seen_position: int | None = None


@dataclass(frozen=True)
class CanonicalTransformResult:
    """Result container containing canonical item and observation records."""

    item: dict[str, Any]
    observation: dict[str, Any]
    platform_content_id: str
    content_type: str
    is_first_observation: bool
    is_reappearance: bool
    validation_passed: bool = True


# ============================================================================
# Transformer Implementation
# ============================================================================


class DouyinCanonicalTransformer:
    """Deterministic transformer for Douyin listcollection payloads."""

    def __init__(
        self,
        schema_dir: str | Path | None = None,
        validate_schema: bool = True,
    ) -> None:
        self.validate_schema = validate_schema

        # Resolve schemas directory
        if schema_dir is not None:
            resolved_dir = Path(schema_dir)
        else:
            pkg_schemas = Path(__file__).resolve().parent.parent / "schemas"
            repo_schemas = Path("G:/antigravity-cli/dy/schema")
            if pkg_schemas.exists() and (pkg_schemas / "collection_item_v1.schema.json").exists():
                resolved_dir = pkg_schemas
            elif repo_schemas.exists() and (repo_schemas / "collection_item_v1.schema.json").exists():
                resolved_dir = repo_schemas
            else:
                resolved_dir = pkg_schemas

        self.schema_dir = resolved_dir
        self._item_validator: jsonschema.Draft202012Validator | None = None
        self._obs_validator: jsonschema.Draft202012Validator | None = None

        if self.validate_schema:
            self._load_validators()

    def _load_validators(self) -> None:
        item_schema_path = self.schema_dir / "collection_item_v1.schema.json"
        obs_schema_path = self.schema_dir / "collection_observation_v1.schema.json"

        if not item_schema_path.exists():
            raise FileNotFoundError(f"Item schema not found at {item_schema_path}")
        if not obs_schema_path.exists():
            raise FileNotFoundError(f"Observation schema not found at {obs_schema_path}")

        with open(item_schema_path, "r", encoding="utf-8") as f:
            item_schema = json.load(f)
        with open(obs_schema_path, "r", encoding="utf-8") as f:
            obs_schema = json.load(f)

        self._item_validator = jsonschema.Draft202012Validator(
            item_schema,
            format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
        )
        self._obs_validator = jsonschema.Draft202012Validator(
            obs_schema,
            format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
        )

    # ------------------------------------------------------------------------
    # Public Transformation API
    # ------------------------------------------------------------------------

    def transform_item(
        self,
        raw_item: dict[str, Any],
        context: TransformContext,
        prior: PriorCollectionState | None = None,
    ) -> CanonicalTransformResult:
        """Transform a single raw Douyin aweme_struct into canonical records.

        Args:
            raw_item: Raw aweme_struct dict from listcollection aweme_list.
            context: Positional and temporal context for this observation.
            prior: Prior historical state of this item in the collection, if known.

        Returns:
            CanonicalTransformResult containing validated item and observation.

        Raises:
            TransformInvalidRawError: If raw_item is invalid or missing required fields.
            TransformMissingIdentityError: If aweme_id is missing or empty.
            TransformSchemaValidationError: If generated records violate canonical schema.
        """
        if not isinstance(raw_item, dict):
            raise TransformInvalidRawError(f"Raw item must be a dict, got {type(raw_item)}")

        # 1. Identity validation
        raw_id = raw_item.get("aweme_id")
        if not raw_id:
            raise TransformMissingIdentityError("Raw item missing 'aweme_id'")
        platform_content_id = str(raw_id).strip()
        if not platform_content_id:
            raise TransformMissingIdentityError("Raw item has empty 'aweme_id'")

        # 2. Temporal extraction
        create_time = raw_item.get("create_time")
        if create_time is None:
            raise TransformInvalidRawError(f"Raw item {platform_content_id} missing 'create_time'")
        published_at = self.format_published_at(create_time)

        # 3. Content type determination
        content_type = self.determine_content_type(raw_item)

        # 4. Text and author extraction
        desc = str(raw_item.get("desc") or "")
        item_title = raw_item.get("item_title")
        clean_title = str(item_title).strip() if item_title is not None and str(item_title).strip() else None
        tags = self.extract_tags(raw_item)
        author_dict = self.build_author_dict(raw_item)

        # 5. Collection state synthesis (Case A / B / C)
        collection_dict, is_first_obs, is_reapp = self.build_collection_dict(context, prior)

        # 6. Media structure synthesis
        media_dict = self.build_media_dict(raw_item, content_type, context.observed_at)

        # 7. Chapters & Statistics
        chapters = self.extract_chapters(raw_item)
        statistics = self.extract_statistics(raw_item)

        # 8. Raw Reference provenance
        raw_ref_dict = self.build_raw_ref(context)

        # 9. Legacy compatibility projection
        legacy_compat = self.build_legacy_compatibility(
            raw_item=raw_item,
            aweme_id=platform_content_id,
            first_seen_at=collection_dict["first_seen_at"],
            author_dict=author_dict,
            item_title=clean_title,
            desc=desc,
        )

        # 10. Assemble canonical CollectionItemV1
        canonical_item: dict[str, Any] = {
            "schema_version": "collection-item-v1",
            "platform": "douyin",
            "source_type": "collection",
            "platform_content_id": platform_content_id,
            "source_url": f"https://www.douyin.com/video/{platform_content_id}",
            "content_type": content_type,
            "title": clean_title,
            "description": desc,
            "tags": tags,
            "author": author_dict,
            "published_at": published_at,
            "collection": collection_dict,
            "media": media_dict,
            "chapters": chapters,
            "statistics": statistics,
            "raw_ref": raw_ref_dict,
            "legacy_compatibility": legacy_compat,
        }

        # 11. Assemble canonical CollectionObservationV1
        observation_id = f"obs_{context.sync_run_id}_{platform_content_id}"
        canonical_observation: dict[str, Any] = {
            "schema_version": "collection-observation-v1",
            "observation_id": observation_id,
            "sync_run_id": str(context.sync_run_id),
            "platform": "douyin",
            "platform_content_id": platform_content_id,
            "observed_at": str(context.observed_at),
            "page_number": int(context.page_number),
            "position_in_page": int(context.position_in_page),
            "global_rank_seen": int(context.global_rank_seen),
            "page_request_cursor": str(context.request_cursor),
            "page_response_cursor": str(context.response_cursor),
            "is_first_observation": is_first_obs,
            "is_reappearance": is_reapp,
            "raw_ref": raw_ref_dict,
        }

        # 12. Schema validation
        if self.validate_schema:
            self.validate_item(canonical_item)
            self.validate_observation(canonical_observation)

        return CanonicalTransformResult(
            item=canonical_item,
            observation=canonical_observation,
            platform_content_id=platform_content_id,
            content_type=content_type,
            is_first_observation=is_first_obs,
            is_reappearance=is_reapp,
            validation_passed=True,
        )

    # ------------------------------------------------------------------------
    # Field Extraction & Synthesis Helpers
    # ------------------------------------------------------------------------

    @staticmethod
    def format_published_at(create_time: int | float) -> str:
        """Convert Unix timestamp (seconds or ms) to UTC ISO 8601 string."""
        ts = float(create_time)
        if ts > 1e11:  # Milliseconds detected
            ts = ts / 1000.0
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.isoformat()

    @staticmethod
    def determine_content_type(raw_item: dict[str, Any]) -> str:
        """Determine whether content is 'video' or 'image_album'."""
        aweme_type = raw_item.get("aweme_type", 0)
        media_type = raw_item.get("media_type", 4)
        raw_images = raw_item.get("images")
        if aweme_type == 68 or media_type == 2 or (raw_images and len(raw_images) > 0):
            return "image_album"
        return "video"

    @staticmethod
    def extract_tags(raw_item: dict[str, Any]) -> list[str]:
        """Extract deduplicated tags preserving order from text_extra or desc."""
        tags: list[str] = []
        text_extra = raw_item.get("text_extra") or []
        for te in text_extra:
            if isinstance(te, dict):
                tag_name = te.get("hashtag_name")
                if tag_name:
                    cleaned = str(tag_name).lstrip("#").strip()
                    if cleaned and cleaned not in tags:
                        tags.append(cleaned)
        # Fallback to hashtags in desc if text_extra was absent
        if not tags and raw_item.get("desc"):
            import re

            matches = re.findall(r"#([^\s#]+)", str(raw_item["desc"]))
            for m in matches:
                cleaned = m.strip()
                if cleaned and cleaned not in tags:
                    tags.append(cleaned)
        return tags

    @staticmethod
    def build_author_dict(raw_item: dict[str, Any]) -> dict[str, Any]:
        """Synthesize canonical Author object."""
        raw_author = raw_item.get("author") or {}
        uid = str(raw_author.get("uid") or raw_item.get("author_user_id") or "0")
        sec_uid = raw_author.get("sec_uid") or None
        unique_id = raw_author.get("unique_id") or raw_author.get("short_id") or None
        nickname = str(raw_author.get("nickname") or "Unknown Author")
        signature = raw_author.get("signature") or None

        avatar_url = None
        thumb = raw_author.get("avatar_thumb") or {}
        if isinstance(thumb, dict) and thumb.get("url_list"):
            avatar_url = thumb["url_list"][0]

        profile_url = f"https://www.douyin.com/user/{sec_uid}" if sec_uid else None

        return {
            "platform_author_id": uid,
            "sec_uid": sec_uid,
            "username": unique_id,
            "display_name": nickname,
            "avatar_url": avatar_url,
            "profile_url": profile_url,
            "signature": signature,
        }

    @staticmethod
    def build_collection_dict(
        context: TransformContext,
        prior: PriorCollectionState | None,
    ) -> tuple[dict[str, Any], bool, bool]:
        """Synthesize collection state reflecting historical invariants.

        Returns:
            (collection_dict, is_first_observation, is_reappearance)
        """
        observed_at = context.observed_at
        position = context.global_rank_seen

        # Case A: Brand-new item observation
        if prior is None or not prior.exists:
            is_first_obs = True
            is_reapp = False
            collection_dict = {
                "first_seen_at": observed_at,
                "last_seen_at": observed_at,
                "reappeared_at": None,
                "active": True,
                "observed_count": 1,
                "last_seen_position": position,
            }
            return collection_dict, is_first_obs, is_reapp

        # Item has prior existence in DB
        is_first_obs = False
        first_seen_at = prior.first_seen_at or observed_at
        observed_count = max(1, prior.observed_count + 1)

        # Case C: Reappearance (re-favorite)
        # Triggered if prior was marked inactive (item inactive in DB -> reappeared)
        # OR caller explicitly provided current reappearance event flag in TransformContext
        is_reapp_event = (not prior.active) or context.is_reappearance_event
        if is_reapp_event:
            is_reapp = True
            reappeared_at = observed_at
        else:
            # Case B: Continuing active observation
            is_reapp = False
            reappeared_at = prior.reappeared_at

        collection_dict = {
            "first_seen_at": first_seen_at,
            "last_seen_at": observed_at,
            "reappeared_at": reappeared_at,
            "active": True,
            "observed_count": observed_count,
            "last_seen_position": position,
        }
        return collection_dict, is_first_obs, is_reapp

    @staticmethod
    def build_media_dict(
        raw_item: dict[str, Any],
        content_type: str,
        observed_at: str,
    ) -> dict[str, Any]:
        """Synthesize canonical Media object adhering to stability levels."""
        raw_video = raw_item.get("video") or {}
        raw_music = raw_item.get("music") or {}

        # Audio indicators
        has_audio = bool(raw_music.get("id") or raw_video.get("play_addr"))
        audio_title = raw_music.get("title") or None
        audio_author = raw_music.get("author") or None

        # Durations & Dimensions
        duration_ms = raw_video.get("duration")
        duration_ms_val = int(duration_ms) if duration_ms is not None else None
        duration_sec_val = round(duration_ms_val / 1000.0, 3) if duration_ms_val is not None else None

        width = int(raw_video["width"]) if raw_video.get("width") else None
        height = int(raw_video["height"]) if raw_video.get("height") else None
        aspect_ratio = str(raw_video.get("ratio")) if raw_video.get("ratio") else None

        # Storage Asset IDs & Cover
        cover_obj = raw_video.get("cover") or {}
        cover_url = None
        if isinstance(cover_obj, dict) and cover_obj.get("url_list"):
            cover_url = cover_obj["url_list"][0]
        cover_asset_id = cover_obj.get("uri") or None

        play_addr = raw_video.get("play_addr") or {}
        video_asset_id = play_addr.get("uri") or None

        # Images (for image_album)
        images_list: list[dict[str, Any]] | None = None
        if content_type == "image_album" and raw_item.get("images"):
            images_list = []
            for idx, img in enumerate(raw_item["images"]):
                img_url = ""
                if img.get("url_list") and len(img["url_list"]) > 0:
                    img_url = img["url_list"][0]
                elif img.get("download_url_list") and len(img["download_url_list"]) > 0:
                    img_url = img["download_url_list"][0]
                images_list.append(
                    {
                        "index": idx,
                        "asset_id": img.get("uri") or None,
                        "url": img_url,
                        "width": int(img["width"]) if img.get("width") else None,
                        "height": int(img["height"]) if img.get("height") else None,
                    }
                )

        # Download input (transient CDN URLs)
        download_input: dict[str, Any] | None = None
        play_urls = play_addr.get("url_list") or []
        if play_urls:
            bitrate_variants = []
            for b in raw_video.get("bit_rate") or []:
                b_url = b.get("play_addr", {}).get("url_list", [None])[0]
                if b_url:
                    bitrate_variants.append(
                        {
                            "gear_name": str(b.get("gear_name", "")),
                            "quality_type": int(b.get("quality_type", 0)),
                            "bit_rate": int(b.get("bit_rate", 0)),
                            "format": str(b.get("format", "")),
                            "url": b_url,
                        }
                    )
            download_input = {
                "fetched_at": observed_at,
                "play_urls": [u for u in play_urls if u],
                "bitrate_variants": bitrate_variants,
            }

        return {
            "has_audio": has_audio,
            "duration_seconds": duration_sec_val,
            "duration_ms": duration_ms_val,
            "width": width,
            "height": height,
            "aspect_ratio": aspect_ratio,
            "cover_url": cover_url,
            "cover_asset_id": cover_asset_id,
            "video_asset_id": video_asset_id,
            "images": images_list,
            "audio_title": audio_title,
            "audio_author": audio_author,
            "download_input": download_input,
        }

    @staticmethod
    def extract_chapters(raw_item: dict[str, Any]) -> list[dict[str, Any]] | None:
        """Extract structured chapters timestamps if present."""
        raw_chapters = raw_item.get("chapter_list")
        if not raw_chapters or not isinstance(raw_chapters, list):
            return None
        chapters: list[dict[str, Any]] = []
        for ch in raw_chapters:
            if isinstance(ch, dict):
                chapters.append(
                    {
                        "timestamp_ms": int(ch.get("timestamp", 0)),
                        "title": str(ch.get("desc", "")),
                        "description": ch.get("detail") or None,
                    }
                )
        return chapters if chapters else None

    @staticmethod
    def extract_statistics(raw_item: dict[str, Any]) -> dict[str, Any] | None:
        """Extract public engagement counters at observation time."""
        raw_stats = raw_item.get("statistics")
        if not isinstance(raw_stats, dict):
            return None
        return {
            "digg_count": int(raw_stats["digg_count"]) if raw_stats.get("digg_count") is not None else None,
            "comment_count": int(raw_stats["comment_count"]) if raw_stats.get("comment_count") is not None else None,
            "share_count": int(raw_stats["share_count"]) if raw_stats.get("share_count") is not None else None,
            "collect_count": int(raw_stats["collect_count"]) if raw_stats.get("collect_count") is not None else None,
        }

    @staticmethod
    def build_raw_ref(context: TransformContext) -> dict[str, Any]:
        """Produce RawRef pointer, utilizing context or synthetic fallback."""
        if context.raw_ref is not None:
            return context.raw_ref.to_dict()
        return {
            "platform_raw_type": "douyin.listcollection.aweme_struct",
            "archive_file": "unarchived/inline.json",
            "sha256": "0" * 64,
            "item_index": context.position_in_page,
        }

    @staticmethod
    def build_legacy_compatibility(
        raw_item: dict[str, Any],
        aweme_id: str,
        first_seen_at: str,
        author_dict: dict[str, Any],
        item_title: str | None,
        desc: str,
    ) -> dict[str, Any]:
        """Project legacy local-video-knowledge compatibility layer."""
        fallback_title = item_title or (desc[:30] if desc else None)
        return {
            "collected_at": first_seen_at,
            "author_name": author_dict["display_name"],
            "author_id": author_dict["sec_uid"] or author_dict["platform_author_id"],
            "title": fallback_title,
            "original_filename": f"{aweme_id}.mp4",
        }

    # ------------------------------------------------------------------------
    # Schema Validation
    # ------------------------------------------------------------------------

    def validate_item(self, item_dict: dict[str, Any]) -> None:
        """Validate an item dict against CollectionItemV1 schema."""
        if self._item_validator is None:
            return
        errors = list(self._item_validator.iter_errors(item_dict))
        if errors:
            err_details = "; ".join(f"[{e.json_path}]: {e.message}" for e in errors)
            raise TransformSchemaValidationError(
                f"CollectionItemV1 validation failed ({len(errors)} errors): {err_details}"
            )

    def validate_observation(self, obs_dict: dict[str, Any]) -> None:
        """Validate an observation dict against CollectionObservationV1 schema."""
        if self._obs_validator is None:
            return
        errors = list(self._obs_validator.iter_errors(obs_dict))
        if errors:
            err_details = "; ".join(f"[{e.json_path}]: {e.message}" for e in errors)
            raise TransformSchemaValidationError(
                f"CollectionObservationV1 validation failed ({len(errors)} errors): {err_details}"
            )
