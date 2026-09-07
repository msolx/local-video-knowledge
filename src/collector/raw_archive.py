"""Raw Response Archiver (DY-C06).

Persists verbatim signed JSON responses returned by SourceClient BEFORE any
canonical transformation occurs (Raw Before Transform principle).

Architectural Principles & Guarantees:
  1. Pure Evidence Storage: No browser, no auth, no pagination, no schema
     transformation, no database writes, no downloader involvement.
  2. Immutability: Once written, raw archive pages are never edited or overwritten.
  3. Deterministic UTF-8 Serialization: Compact, sorted keys, consistent byte representation.
  4. Byte-level SHA-256 Integrity: Hash computed directly on disk-written bytes.
  5. Atomic Writes: Temp-file write -> fsync -> os.replace prevents corrupted half-JSONs.
  6. Idempotency vs Conflict:
     - Identical payload to existing page -> IDEMPOTENT_EXISTING (returns existing ref).
     - Conflicting payload to existing page -> RAW_ARCHIVE_CONFLICT (strictly rejected).
  7. Sensitive Field Scan: Blocks persisting credentials (sessionid, cookies, auth headers).
  8. Path Safety: Strict whitelist on platform and sync_run_id to prevent path traversal.
  9. Run Manifest: Atomically maintained manifest.json recording per-run page inventory.
 10. Verification & Replay: Full verify() and read_page() capabilities.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ============================================================================
# Exception Hierarchy
# ============================================================================


class RawArchiveError(Exception):
    """Base exception for raw archiver failures."""


class RawArchiveInvalidInputError(RawArchiveError):
    """Raised when parameters or payload are invalid."""


class RawArchiveIOError(RawArchiveError):
    """Raised on underlying file system read/write errors."""


class RawArchiveConflictError(RawArchiveError):
    """Raised when attempting to overwrite an existing page with different payload."""


class RawArchiveHashMismatchError(RawArchiveError):
    """Raised when checksum validation detects data corruption."""


class RawArchiveSensitiveFieldError(RawArchiveError):
    """Raised when sensitive credential keys are detected in raw payload."""


class RawArchiveNotFoundError(RawArchiveError):
    """Raised when requested archive file does not exist."""


class RawArchiveCorruptError(RawArchiveError):
    """Raised when archive file is corrupted or cannot be parsed as JSON."""


class RawArchiveUnknownError(RawArchiveError):
    """Raised for unexpected internal errors."""


# ============================================================================
# Data Models
# ============================================================================


@dataclass(frozen=True)
class RawArchiveRef:
    """Strong-typed immutable reference to an archived raw API response page."""

    platform: str
    sync_run_id: str
    page_number: int
    path: str  # Standard relative path, e.g. "data/raw/douyin/run_01/page_000001.json"
    sha256: str  # 64-char lowercase hex hash of written bytes
    byte_size: int
    archived_at: str  # ISO 8601 UTC timestamp of storage event
    request_cursor: str
    response_cursor: str
    fetched_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "sync_run_id": self.sync_run_id,
            "page_number": self.page_number,
            "path": self.path,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
            "archived_at": self.archived_at,
            "request_cursor": self.request_cursor,
            "response_cursor": self.response_cursor,
            "fetched_at": self.fetched_at,
        }

    def to_raw_ref_dict(
        self,
        item_index: int,
        platform_raw_type: str = "douyin.listcollection.aweme_struct",
    ) -> dict[str, Any]:
        """Construct raw_ref dictionary conforming to collection_item_v1 and collection_observation_v1."""
        return {
            "platform_raw_type": platform_raw_type,
            "archive_file": self.path,
            "sha256": self.sha256,
            "item_index": item_index,
        }


# ============================================================================
# Security & Sanitization
# ============================================================================

SENSITIVE_KEY_NAMES = frozenset(
    {
        "cookie",
        "cookies",
        "authorization",
        "sessionid",
        "sid_guard",
        "passport_auth",
        "passport_assist",
        "passport_csrf_token",
        "password",
        "secret_key",
        "access_token",
        "refresh_token",
    }
)

SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def scan_for_sensitive_fields(payload: Any) -> list[str]:
    """Recursively scan payload for prohibited sensitive credential keys."""
    detected: list[str] = []

    def _walk(val: Any) -> None:
        if isinstance(val, dict):
            for k, v in val.items():
                lower_k = str(k).lower()
                if lower_k in SENSITIVE_KEY_NAMES:
                    detected.append(str(k))
                _walk(v)
        elif isinstance(val, list):
            for elem in val:
                _walk(elem)

    _walk(payload)
    return detected


def validate_identifier(value: str, field_name: str) -> None:
    """Validate identifier to ensure path safety and prevent directory traversal."""
    if not value or not isinstance(value, str):
        raise RawArchiveInvalidInputError(f"{field_name} must be a non-empty string")
    if not SAFE_IDENTIFIER_PATTERN.match(value):
        raise RawArchiveInvalidInputError(
            f"Invalid {field_name}: '{value}'. Must contain only alphanumeric characters, underscores, and dashes."
        )
    if ".." in value or "/" in value or "\\" in value or ":" in value:
        raise RawArchiveInvalidInputError(f"Path traversal characters detected in {field_name}: '{value}'")


# ============================================================================
# Serialization & Atomic I/O
# ============================================================================


def serialize_raw_payload(payload: dict[str, Any]) -> bytes:
    """Serialize raw payload to deterministic UTF-8 bytes."""
    try:
        json_str = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return json_str.encode("utf-8")
    except Exception as exc:
        raise RawArchiveInvalidInputError(f"Failed to serialize payload to JSON: {exc}") from exc


def atomic_write_bytes(target_path: Path, data: bytes) -> None:
    """Write data bytes to target_path atomically using a temp file in the same dir."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = f".{target_path.name}.tmp.{os.getpid()}_{uuid.uuid4().hex[:8]}"
    temp_path = target_path.parent / temp_name
    try:
        with open(temp_path, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, target_path)
    except Exception as exc:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise RawArchiveIOError(f"Atomic write failed for {target_path}: {exc}") from exc


# ============================================================================
# RawArchiver Implementation
# ============================================================================


class DiskRawArchiver:
    """Production implementation of RawArchiver storing immutable raw pages on disk."""

    def __init__(self, root_dir: str | Path = "data/raw") -> None:
        self.root_dir = Path(root_dir).resolve()

    def get_run_dir(self, platform: str, sync_run_id: str) -> Path:
        """Resolve storage directory for a specific sync run."""
        validate_identifier(platform, "platform")
        validate_identifier(sync_run_id, "sync_run_id")
        return self.root_dir / platform / sync_run_id

    def get_page_path(self, platform: str, sync_run_id: str, page_number: int) -> Path:
        """Resolve target path for a specific page."""
        if not isinstance(page_number, int) or page_number < 1:
            raise RawArchiveInvalidInputError(f"page_number must be an integer >= 1, got {page_number}")
        run_dir = self.get_run_dir(platform, sync_run_id)
        filename = f"page_{page_number:06d}.json"
        return run_dir / filename

    def _relative_path(self, abs_path: Path) -> str:
        """Get path relative to workspace or root_dir with forward slashes."""
        try:
            rel = abs_path.relative_to(Path.cwd())
        except ValueError:
            try:
                rel = abs_path.relative_to(self.root_dir.parent.parent)
            except ValueError:
                rel = abs_path.relative_to(self.root_dir)
        return str(rel).replace("\\", "/")

    def archive_page(
        self,
        sync_run_id: str,
        platform: str,
        page_number: int,
        request_cursor: str,
        response_cursor: str,
        raw_response: dict[str, Any],
        fetched_at: str,
    ) -> RawArchiveRef:
        """Archive a raw collection page payload with integrity hashing and atomic write.

        Args:
            sync_run_id: ID of current sync session.
            platform: Originating platform (e.g. "douyin").
            page_number: 1-based page number within the sync session.
            request_cursor: Parameter sent to request this page.
            response_cursor: next_cursor returned by platform.
            raw_response: Verbatim dict response from platform.
            fetched_at: UTC ISO timestamp when response was received by client.

        Returns:
            RawArchiveRef pointer describing persisted file.

        Raises:
            RawArchiveInvalidInputError: If inputs are invalid.
            RawArchiveSensitiveFieldError: If credentials detected in payload.
            RawArchiveConflictError: If page already exists with different content.
            RawArchiveIOError: If disk write fails.
        """
        if not isinstance(raw_response, dict):
            raise RawArchiveInvalidInputError(f"raw_response must be a dict, got {type(raw_response)}")
        if not fetched_at or not isinstance(fetched_at, str):
            raise RawArchiveInvalidInputError("fetched_at must be a non-empty ISO 8601 timestamp string")

        # 1. Audit for prohibited credentials
        sensitive_keys = scan_for_sensitive_fields(raw_response)
        if sensitive_keys:
            raise RawArchiveSensitiveFieldError(
                f"Prohibited sensitive credential fields detected in raw payload: {sensitive_keys}"
            )

        # 2. Serialize payload deterministically
        raw_bytes = serialize_raw_payload(raw_response)
        byte_size = len(raw_bytes)
        sha256_hash = hashlib.sha256(raw_bytes).hexdigest()

        # 3. Resolve target file path
        target_path = self.get_page_path(platform, sync_run_id, page_number)
        rel_path = self._relative_path(target_path)
        archived_at = datetime.now(timezone.utc).isoformat()

        # 4. Check for existing file (Idempotency vs Conflict)
        if target_path.exists():
            try:
                with open(target_path, "rb") as f:
                    existing_bytes = f.read()
                existing_sha = hashlib.sha256(existing_bytes).hexdigest()
            except Exception as exc:
                raise RawArchiveIOError(f"Failed to read existing page at {target_path}: {exc}") from exc

            if existing_sha == sha256_hash:
                logger.info("Raw page already archived with identical hash: %s", target_path)
                return RawArchiveRef(
                    platform=platform,
                    sync_run_id=sync_run_id,
                    page_number=page_number,
                    path=rel_path,
                    sha256=sha256_hash,
                    byte_size=byte_size,
                    archived_at=archived_at,
                    request_cursor=str(request_cursor),
                    response_cursor=str(response_cursor),
                    fetched_at=fetched_at,
                )
            else:
                raise RawArchiveConflictError(
                    f"Page {page_number} for {sync_run_id} already exists with different hash "
                    f"({existing_sha} != {sha256_hash}). Refusing to overwrite immutable raw evidence."
                )

        # 5. Atomically persist to disk
        atomic_write_bytes(target_path, raw_bytes)

        ref = RawArchiveRef(
            platform=platform,
            sync_run_id=sync_run_id,
            page_number=page_number,
            path=rel_path,
            sha256=sha256_hash,
            byte_size=byte_size,
            archived_at=archived_at,
            request_cursor=str(request_cursor),
            response_cursor=str(response_cursor),
            fetched_at=fetched_at,
        )

        # 6. Update manifest
        self._update_manifest(ref)
        return ref

    def _update_manifest(self, ref: RawArchiveRef) -> None:
        """Atomically record page in sync run manifest.json."""
        run_dir = self.get_run_dir(ref.platform, ref.sync_run_id)
        manifest_path = run_dir / "manifest.json"

        manifest_data: dict[str, Any] = {
            "sync_run_id": ref.sync_run_id,
            "platform": ref.platform,
            "created_at": ref.archived_at,
            "pages": [],
        }

        if manifest_path.exists():
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        manifest_data = loaded
            except Exception as exc:
                logger.warning("Manifest corrupted at %s, recreating: %s", manifest_path, exc)

        pages = manifest_data.setdefault("pages", [])
        # Deduplicate by page_number
        filtered = [p for p in pages if p.get("page_number") != ref.page_number]
        filtered.append(ref.to_dict())
        filtered.sort(key=lambda p: p.get("page_number", 0))
        manifest_data["pages"] = filtered

        manifest_bytes = serialize_raw_payload(manifest_data)
        atomic_write_bytes(manifest_path, manifest_bytes)

    def verify(self, ref: RawArchiveRef) -> bool:
        """Verify the integrity of an archived raw page against its reference hash."""
        target_path = self.get_page_path(ref.platform, ref.sync_run_id, ref.page_number)
        if not target_path.exists():
            raise RawArchiveNotFoundError(f"Archive file not found: {target_path}")

        try:
            with open(target_path, "rb") as f:
                content = f.read()
        except Exception as exc:
            raise RawArchiveIOError(f"Failed to read archive file {target_path}: {exc}") from exc

        if len(content) != ref.byte_size:
            raise RawArchiveHashMismatchError(
                f"Byte size mismatch for {target_path}: expected {ref.byte_size}, got {len(content)}"
            )

        actual_hash = hashlib.sha256(content).hexdigest()
        if actual_hash != ref.sha256:
            raise RawArchiveHashMismatchError(
                f"SHA-256 mismatch for {target_path}: expected {ref.sha256}, got {actual_hash}"
            )
        return True

    def read_page(self, ref: RawArchiveRef) -> dict[str, Any]:
        """Read and parse the raw JSON payload from disk."""
        target_path = self.get_page_path(ref.platform, ref.sync_run_id, ref.page_number)
        if not target_path.exists():
            raise RawArchiveNotFoundError(f"Archive file not found: {target_path}")

        try:
            with open(target_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            raise RawArchiveCorruptError(f"Failed to decode JSON from {target_path}: {exc}") from exc
        except Exception as exc:
            raise RawArchiveIOError(f"Failed to read {target_path}: {exc}") from exc

        if not isinstance(data, dict):
            raise RawArchiveCorruptError(f"Expected dict from {target_path}, got {type(data)}")
        return data

    def discover_run(self, sync_run_id: str, platform: str = "douyin") -> list[RawArchiveRef]:
        """Discover all archived page references for a given sync run."""
        run_dir = self.get_run_dir(platform, sync_run_id)
        if not run_dir.exists():
            return []

        manifest_path = run_dir / "manifest.json"
        if manifest_path.exists():
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                refs: list[RawArchiveRef] = []
                for p in manifest.get("pages", []):
                    refs.append(
                        RawArchiveRef(
                            platform=p["platform"],
                            sync_run_id=p["sync_run_id"],
                            page_number=p["page_number"],
                            path=p["path"],
                            sha256=p["sha256"],
                            byte_size=p["byte_size"],
                            archived_at=p["archived_at"],
                            request_cursor=p.get("request_cursor", "0"),
                            response_cursor=p.get("response_cursor", "0"),
                            fetched_at=p.get("fetched_at", p["archived_at"]),
                        )
                    )
                return refs
            except Exception as exc:
                logger.warning("Failed to parse manifest for %s, falling back to disk glob: %s", sync_run_id, exc)

        # Fallback: scan page files directly
        discovered: list[RawArchiveRef] = []
        for page_file in sorted(run_dir.glob("page_*.json")):
            m = re.match(r"page_(\d+)\.json", page_file.name)
            if not m:
                continue
            page_num = int(m.group(1))
            try:
                with open(page_file, "rb") as f:
                    raw_bytes = f.read()
                h = hashlib.sha256(raw_bytes).hexdigest()
                stat = page_file.stat()
                archived_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
                discovered.append(
                    RawArchiveRef(
                        platform=platform,
                        sync_run_id=sync_run_id,
                        page_number=page_num,
                        path=self._relative_path(page_file),
                        sha256=h,
                        byte_size=len(raw_bytes),
                        archived_at=archived_at,
                        request_cursor="0",
                        response_cursor="0",
                        fetched_at=archived_at,
                    )
                )
            except Exception as exc:
                logger.warning("Error reading %s during discovery: %s", page_file, exc)
        return discovered

    # ------------------------------------------------------------------------
    # Backward Compatibility with C01 RawArchiver Protocol
    # ------------------------------------------------------------------------

    def archive_batch(self, batch: Any) -> Path:
        """Adapter for C01 RawArchiveBatch Protocol."""
        sync_run_id = getattr(batch, "run_id", "default_run")
        page_index = getattr(batch, "page_index", 1)
        raw_payload = getattr(batch, "raw_payload", {})
        cursor = str(getattr(batch, "cursor", "0"))
        archived_at = getattr(batch, "archived_at", datetime.now(timezone.utc).isoformat())

        ref = self.archive_page(
            sync_run_id=sync_run_id,
            platform="douyin",
            page_number=page_index,
            request_cursor=cursor,
            response_cursor="0",
            raw_response=raw_payload,
            fetched_at=archived_at,
        )
        return self.get_page_path("douyin", sync_run_id, page_index)
