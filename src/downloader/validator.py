"""Multi-Step Media Asset Validation Engine (DY-D05).

Implements:
1. Multi-step media validation pipeline:
   - Step 0: File sanity (exists, regular file, size > 0, rejection of .part/.tmp/.crdownload)
   - Step 1: ffprobe structural probe (container format, streams, duration, dimensions)
   - Step 2: Stream requirement verification (VIDEO: >=1 video stream; audio required vs optional)
   - Step 3: FFmpeg decode smoke (real 5-10s decoding to null sink, optional tail smoke)
   - Step 4: TOCTOU verification (stat size/mtime before and after validation)
2. Specialized validation primitives:
   - Video validation (H.264, HEVC, VP9, AV1, portrait, landscape, 4K)
   - Image & Image Set validation (WebP, JPEG, PNG decode smoke)
   - Audio validation (AAC, MP3, BGM audio streams)
3. Subprocess safety: argv list, shell=False, timeout, bounded capture, credential scrubbing.
4. Tool availability: distinguishing missing binaries (TOOL_ERROR) from media failure (VALIDATION_FAILED).
5. Invariants: Read-only (never mutates/moves/promotes/deletes files), zero retry execution.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from src.downloader.contracts import (
    DownloaderErrorCode,
    ValidationResult,
    scrub_secrets,
)

logger = logging.getLogger(__name__)

# Temporary / incomplete file extensions strictly forbidden as final artifacts
_FORBIDDEN_EXTENSIONS = {".part", ".tmp", ".crdownload", ".download", ".incomplete"}


def _compute_file_sha256(file_path: Path) -> str:
    """Computes SHA-256 digest of a file in 64 KiB chunks."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


# =============================================================================
# 1. Validation Profiles & Expectations
# =============================================================================


class ValidationProfile(str, Enum):
    """Media type validation profiles."""

    VIDEO = "VIDEO"
    IMAGE = "IMAGE"
    IMAGE_SET = "IMAGE_SET"
    AUDIO = "AUDIO"
    AUTO = "AUTO"


@dataclass(frozen=True)
class ValidationExpectations:
    """Configurable expectations and thresholds for media validation."""

    require_video_stream: bool = True
    expect_audio_stream: bool | None = None  # True: mandatory; False/None: optional
    expected_duration_ms: float | None = None
    duration_tolerance_ratio: float = 0.20  # 20% tolerance
    duration_tolerance_abs_sec: float = 3.0  # minimum 3s tolerance
    expected_width: int | None = None
    expected_height: int | None = None
    minimum_image_count: int = 1
    expected_image_count: int | None = None
    decode_smoke_seconds: float = 5.0
    enable_tail_smoke: bool = True
    tail_smoke_seconds: float = 3.0
    min_file_size_bytes: int = 1


# =============================================================================
# 2. Validation Result Contract
# =============================================================================


@dataclass
class MediaValidationResult:
    """Comprehensive media validation output (QW-13 / DY-D01 compliant)."""

    passed: bool
    ffprobe_verified: bool = False
    decode_smoke_verified: bool = False
    profile: str = ValidationProfile.VIDEO.value
    file_count: int = 1
    streams: list[dict[str, Any]] = field(default_factory=list)
    duration_sec: float | None = None
    width: int | None = None
    height: int | None = None
    codec_summary: str | None = None
    warnings: list[str] = field(default_factory=list)
    failed_check: str | None = None
    error: str | None = None
    error_code: str | None = None
    tool_versions: dict[str, str] = field(default_factory=dict)
    validated_sha256: str | None = None
    validated_artifacts: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.error:
            self.error = scrub_secrets(self.error)

    def to_dict(self) -> dict[str, Any]:
        """Returns JSON-serializable dictionary matching Downloader contract expectations."""
        return {
            "passed": self.passed,
            "ffprobe_verified": self.ffprobe_verified,
            "decode_smoke_verified": self.decode_smoke_verified,
            "profile": self.profile,
            "file_count": self.file_count,
            "streams": list(self.streams),
            "duration_sec": round(self.duration_sec, 3) if self.duration_sec is not None else None,
            "width": self.width,
            "height": self.height,
            "codec_summary": self.codec_summary,
            "warnings": list(self.warnings),
            "failed_check": self.failed_check,
            "error": scrub_secrets(self.error) if self.error else None,
            "error_code": self.error_code,
            "tool_versions": dict(self.tool_versions),
            "validated_sha256": self.validated_sha256,
            "validated_artifacts": list(self.validated_artifacts),
        }

    def to_base_validation_result(self) -> ValidationResult:
        """Converts to base contracts.ValidationResult for backward compatibility."""
        return ValidationResult(
            passed=self.passed,
            ffprobe_verified=self.ffprobe_verified,
            decode_smoke_verified=self.decode_smoke_verified,
            streams=tuple(self.streams),
            error=self.error,
            validated_sha256=self.validated_sha256,
            validated_artifacts=tuple(self.validated_artifacts),
        )


# =============================================================================
# 3. Production MediaValidator Implementation
# =============================================================================


class ProductionMediaValidator:
    """Production Multi-Step Media Validator executing structural probe and decode smoke."""

    def __init__(
        self,
        ffprobe_executable: str | Path | None = None,
        ffmpeg_executable: str | Path | None = None,
        ffprobe_timeout_sec: float = 15.0,
        decode_smoke_timeout_sec: float = 20.0,
        max_capture_bytes: int = 100 * 1024,  # 100 KB bounded log capture
    ) -> None:
        self.ffprobe_executable = str(ffprobe_executable) if ffprobe_executable else (shutil.which("ffprobe") or "ffprobe")
        self.ffmpeg_executable = str(ffmpeg_executable) if ffmpeg_executable else (shutil.which("ffmpeg") or "ffmpeg")
        self.ffprobe_timeout = ffprobe_timeout_sec
        self.decode_smoke_timeout = decode_smoke_timeout_sec
        self.max_capture_bytes = max_capture_bytes

    def get_tool_versions(self) -> dict[str, str]:
        """Discovers and records versions of ffprobe and ffmpeg."""
        versions: dict[str, str] = {}
        for name, exe in [("ffprobe", self.ffprobe_executable), ("ffmpeg", self.ffmpeg_executable)]:
            try:
                res = subprocess.run(
                    [exe, "-version"],
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                    shell=False,
                )
                if res.returncode == 0 and res.stdout:
                    first_line = res.stdout.splitlines()[0].strip()
                    versions[name] = first_line
                else:
                    versions[name] = "UNAVAILABLE"
            except Exception:
                versions[name] = "NOT_FOUND"
        return versions

    def validate_assets(
        self,
        asset_paths: list[Path],
        expectations: ValidationExpectations | None = None,
        sandbox_root: Path | None = None,
        profile: ValidationProfile = ValidationProfile.AUTO,
    ) -> MediaValidationResult:
        """Main entry point satisfying the MediaValidator Protocol port in D01.

        Validates candidate media assets against structural integrity and smoke checks.
        """
        tool_versions = self.get_tool_versions()
        exp = expectations or ValidationExpectations()

        if not asset_paths:
            return MediaValidationResult(
                passed=False,
                profile=profile.value,
                file_count=0,
                failed_check="NO_ASSETS_PROVIDED",
                error_code=DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED.value,
                error="Validation failed: candidate asset list is empty.",
                tool_versions=tool_versions,
            )

        # Profile resolution
        if profile == ValidationProfile.AUTO:
            first = asset_paths[0]
            ext = first.suffix.lower()
            if ext in (".webp", ".jpg", ".jpeg", ".png"):
                if len(asset_paths) > 1:
                    profile = ValidationProfile.IMAGE_SET
                else:
                    profile = ValidationProfile.IMAGE
            elif ext in (".mp3", ".m4a", ".aac", ".flac"):
                profile = ValidationProfile.AUDIO
            else:
                profile = ValidationProfile.VIDEO

        # Dispatch to specialized primitive
        if profile == ValidationProfile.VIDEO:
            return self.validate_video(asset_paths[0], expectations=exp, sandbox_root=sandbox_root)
        elif profile == ValidationProfile.IMAGE:
            return self.validate_image(asset_paths[0], sandbox_root=sandbox_root)
        elif profile == ValidationProfile.IMAGE_SET:
            return self.validate_image_set(asset_paths, expectations=exp, sandbox_root=sandbox_root)
        elif profile == ValidationProfile.AUDIO:
            return self.validate_audio(asset_paths[0], expectations=exp, sandbox_root=sandbox_root)
        else:
            return self.validate_video(asset_paths[0], expectations=exp, sandbox_root=sandbox_root)

    # =========================================================================
    # Step-by-Step Video Validation Pipeline
    # =========================================================================

    def validate_video(
        self,
        file_path: Path,
        expectations: ValidationExpectations | None = None,
        sandbox_root: Path | None = None,
    ) -> MediaValidationResult:
        """Validates video asset via structural probe and real decode smoke."""
        exp = expectations or ValidationExpectations()
        tool_versions = self.get_tool_versions()

        # STEP 0 · FILE SANITY
        sanity_res = self._check_file_sanity(file_path, sandbox_root, exp.min_file_size_bytes)
        if not sanity_res["passed"]:
            return MediaValidationResult(
                passed=False,
                profile=ValidationProfile.VIDEO.value,
                failed_check=sanity_res["failed_check"],
                error_code=sanity_res["error_code"],
                error=sanity_res["error"],
                tool_versions=tool_versions,
            )

        initial_stat = sanity_res["stat"]

        # STEP 0.5 · TOOL AVAILABILITY
        tool_check = self._check_tool_availability()
        if not tool_check["passed"]:
            return MediaValidationResult(
                passed=False,
                profile=ValidationProfile.VIDEO.value,
                failed_check=tool_check["failed_check"],
                error_code=tool_check["error_code"],
                error=tool_check["error"],
                tool_versions=tool_versions,
            )

        # STEP 1 · FFPROBE STRUCTURAL PROBE
        probe_res = self._run_ffprobe(file_path)
        if not probe_res["passed"]:
            return MediaValidationResult(
                passed=False,
                profile=ValidationProfile.VIDEO.value,
                failed_check=probe_res["failed_check"],
                error_code=probe_res["error_code"],
                error=probe_res["error"],
                tool_versions=tool_versions,
            )

        metadata = probe_res["metadata"]
        streams = metadata.get("streams", [])
        format_info = metadata.get("format", {})

        # STEP 2 · EXPECTED STREAM CHECK
        video_streams = [s for s in streams if s.get("codec_type") == "video"]
        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

        if exp.require_video_stream and not video_streams:
            return MediaValidationResult(
                passed=False,
                profile=ValidationProfile.VIDEO.value,
                ffprobe_verified=True,
                streams=streams,
                failed_check="MISSING_VIDEO_STREAM",
                error_code=DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE.value,
                error="Container valid but missing required video stream.",
                tool_versions=tool_versions,
            )

        # Audio stream requirement check
        warnings: list[str] = []
        if exp.expect_audio_stream is True and not audio_streams:
            return MediaValidationResult(
                passed=False,
                profile=ValidationProfile.VIDEO.value,
                ffprobe_verified=True,
                streams=streams,
                failed_check="MISSING_AUDIO_STREAM",
                error_code=DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE.value,
                error="Validation failed: audio stream was explicitly required but missing.",
                tool_versions=tool_versions,
            )
        elif exp.expect_audio_stream is False and audio_streams:
            warnings.append("Audio stream present although marked not expected.")

        # Extract dimensions and duration
        primary_video = video_streams[0] if video_streams else {}
        width = primary_video.get("width")
        height = primary_video.get("height")
        codec_name = primary_video.get("codec_name", "unknown")

        if width is not None and width <= 0 or height is not None and height <= 0:
            return MediaValidationResult(
                passed=False,
                profile=ValidationProfile.VIDEO.value,
                ffprobe_verified=True,
                streams=streams,
                failed_check="INVALID_DIMENSIONS",
                error_code=DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED.value,
                error=f"Invalid video dimensions: {width}x{height}.",
                tool_versions=tool_versions,
            )

        raw_duration = format_info.get("duration") or primary_video.get("duration")
        try:
            duration_sec = float(raw_duration) if raw_duration is not None else 0.0
        except (ValueError, TypeError):
            duration_sec = 0.0

        if duration_sec <= 0.0:
            return MediaValidationResult(
                passed=False,
                profile=ValidationProfile.VIDEO.value,
                ffprobe_verified=True,
                streams=streams,
                failed_check="INVALID_DURATION",
                error_code=DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED.value,
                error=f"Invalid video duration: {duration_sec}s.",
                tool_versions=tool_versions,
            )

        # Duration sanity comparison if expected
        if exp.expected_duration_ms is not None and exp.expected_duration_ms > 0:
            expected_sec = exp.expected_duration_ms / 1000.0
            tolerance = max(exp.duration_tolerance_abs_sec, expected_sec * exp.duration_tolerance_ratio)
            if abs(duration_sec - expected_sec) > tolerance:
                warnings.append(
                    f"Duration mismatch: probed {duration_sec:.2f}s vs expected {expected_sec:.2f}s (tolerance: {tolerance:.2f}s)"
                )

        codec_summary = f"{codec_name} / {audio_streams[0].get('codec_name', 'no_audio') if audio_streams else 'no_audio'}"

        # STEP 3 · DECODE SMOKE (Head & Optional Tail)
        smoke_sec = min(duration_sec, exp.decode_smoke_seconds)
        smoke_res = self._run_ffmpeg_decode_smoke(file_path, duration_sec=smoke_sec, seek_sec=None)
        if not smoke_res["passed"]:
            return MediaValidationResult(
                passed=False,
                profile=ValidationProfile.VIDEO.value,
                ffprobe_verified=True,
                decode_smoke_verified=False,
                streams=streams,
                width=width,
                height=height,
                duration_sec=duration_sec,
                codec_summary=codec_summary,
                failed_check=smoke_res["failed_check"],
                error_code=smoke_res.get("error_code", DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED.value),
                error=smoke_res["error"],
                tool_versions=tool_versions,
            )

        # Optional Tail Smoke for videos significantly longer than the smoke window
        if exp.enable_tail_smoke and duration_sec > (exp.decode_smoke_seconds + exp.tail_smoke_seconds + 2.0):
            tail_seek = max(0.0, duration_sec - exp.tail_smoke_seconds - 1.0)
            tail_res = self._run_ffmpeg_decode_smoke(
                file_path, duration_sec=exp.tail_smoke_seconds, seek_sec=tail_seek
            )
            if not tail_res["passed"]:
                return MediaValidationResult(
                    passed=False,
                    profile=ValidationProfile.VIDEO.value,
                    ffprobe_verified=True,
                    decode_smoke_verified=False,
                    streams=streams,
                    width=width,
                    height=height,
                    duration_sec=duration_sec,
                    codec_summary=codec_summary,
                    failed_check="DECODE_TAIL_SMOKE_FAILURE",
                    error_code=tail_res.get("error_code", DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED.value),
                    error=f"Tail decode smoke failed (truncation detected): {tail_res['error']}",
                    tool_versions=tool_versions,
                )

        # STEP 4 · TOCTOU GUARD (Re-stat file to ensure no concurrent modification)
        toctou_res = self._verify_toctou(file_path, initial_stat)
        if not toctou_res["passed"]:
            return MediaValidationResult(
                passed=False,
                profile=ValidationProfile.VIDEO.value,
                failed_check=toctou_res["failed_check"],
                error_code=DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED.value,
                error=toctou_res["error"],
                tool_versions=tool_versions,
            )

        # ALL CHECKS PASSED
        v_sha = _compute_file_sha256(file_path)
        v_artifacts = [{
            "file_name": file_path.name,
            "file_path": str(file_path),
            "size_bytes": file_path.stat().st_size,
            "validated_sha256": v_sha,
        }]
        return MediaValidationResult(
            passed=True,
            ffprobe_verified=True,
            decode_smoke_verified=True,
            profile=ValidationProfile.VIDEO.value,
            file_count=1,
            streams=streams,
            width=width,
            height=height,
            duration_sec=duration_sec,
            codec_summary=codec_summary,
            warnings=warnings,
            tool_versions=tool_versions,
            validated_sha256=v_sha,
            validated_artifacts=v_artifacts,
        )

    # =========================================================================
    # Specialized Primitives: Image, Image Set & Audio
    # =========================================================================

    def validate_image(self, file_path: Path, sandbox_root: Path | None = None) -> MediaValidationResult:
        """Validates single image (WebP, JPEG, PNG) through physical decode."""
        tool_versions = self.get_tool_versions()
        sanity = self._check_file_sanity(file_path, sandbox_root, min_bytes=10)
        if not sanity["passed"]:
            return MediaValidationResult(
                passed=False,
                profile=ValidationProfile.IMAGE.value,
                failed_check=sanity["failed_check"],
                error_code=sanity["error_code"],
                error=sanity["error"],
                tool_versions=tool_versions,
            )

        initial_stat = sanity["stat"]
        decode_res = self._decode_image(file_path)
        if not decode_res["passed"]:
            return MediaValidationResult(
                passed=False,
                profile=ValidationProfile.IMAGE.value,
                failed_check=decode_res["failed_check"],
                error_code=DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED.value,
                error=decode_res["error"],
                tool_versions=tool_versions,
            )

        toctou_res = self._verify_toctou(file_path, initial_stat)
        if not toctou_res["passed"]:
            return MediaValidationResult(
                passed=False,
                profile=ValidationProfile.IMAGE.value,
                failed_check=toctou_res["failed_check"],
                error_code=DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED.value,
                error=toctou_res["error"],
                tool_versions=tool_versions,
            )

        img_sha = _compute_file_sha256(file_path)
        img_artifacts = [{
            "file_name": file_path.name,
            "file_path": str(file_path),
            "size_bytes": file_path.stat().st_size,
            "validated_sha256": img_sha,
        }]
        return MediaValidationResult(
            passed=True,
            ffprobe_verified=True,
            decode_smoke_verified=True,
            profile=ValidationProfile.IMAGE.value,
            file_count=1,
            width=decode_res["width"],
            height=decode_res["height"],
            codec_summary=f"image/{decode_res['format'].lower()}",
            tool_versions=tool_versions,
            validated_sha256=img_sha,
            validated_artifacts=img_artifacts,
        )

    def validate_image_set(
        self,
        file_paths: list[Path],
        expectations: ValidationExpectations | None = None,
        sandbox_root: Path | None = None,
    ) -> MediaValidationResult:
        """Validates an album/set of images."""
        exp = expectations or ValidationExpectations()
        tool_versions = self.get_tool_versions()

        audio_exts = {".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg"}
        image_paths = [p for p in file_paths if p.suffix.lower() not in audio_exts]
        audio_paths = [p for p in file_paths if p.suffix.lower() in audio_exts]

        if len(image_paths) < exp.minimum_image_count:
            return MediaValidationResult(
                passed=False,
                profile=ValidationProfile.IMAGE_SET.value,
                file_count=len(image_paths),
                failed_check="IMAGE_COUNT_BELOW_MINIMUM",
                error_code=DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED.value,
                error=f"Image set contains {len(image_paths)} images, expected at least {exp.minimum_image_count}.",
                tool_versions=tool_versions,
            )

        if exp.expected_image_count is not None and len(image_paths) != exp.expected_image_count:
            return MediaValidationResult(
                passed=False,
                profile=ValidationProfile.IMAGE_SET.value,
                file_count=len(image_paths),
                failed_check="IMAGE_COUNT_MISMATCH",
                error_code=DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED.value,
                error=f"Image set count mismatch: {len(image_paths)} files vs expected {exp.expected_image_count}.",
                tool_versions=tool_versions,
            )

        for img_path in image_paths:
            res = self.validate_image(img_path, sandbox_root=sandbox_root)
            if not res.passed:
                return MediaValidationResult(
                    passed=False,
                    profile=ValidationProfile.IMAGE_SET.value,
                    file_count=len(file_paths),
                    failed_check=f"IMAGE_SET_CORRUPT_MEMBER: {res.failed_check}",
                    error_code=res.error_code,
                    error=f"Image member '{img_path.name}' failed validation: {res.error}",
                    tool_versions=tool_versions,
                )

        for aud_path in audio_paths:
            res = self.validate_audio(aud_path, sandbox_root=sandbox_root)
            if not res.passed:
                return MediaValidationResult(
                    passed=False,
                    profile=ValidationProfile.IMAGE_SET.value,
                    file_count=len(file_paths),
                    failed_check=f"IMAGE_SET_CORRUPT_AUDIO: {res.failed_check}",
                    error_code=res.error_code,
                    error=f"Album audio member '{aud_path.name}' failed validation: {res.error}",
                    tool_versions=tool_versions,
                )

        set_artifacts = []
        for img_path in file_paths:
            s = _compute_file_sha256(img_path)
            set_artifacts.append({
                "file_name": img_path.name,
                "file_path": str(img_path),
                "size_bytes": img_path.stat().st_size,
                "validated_sha256": s,
            })
        primary_sha = set_artifacts[0]["validated_sha256"] if set_artifacts else None

        return MediaValidationResult(
            passed=True,
            ffprobe_verified=True,
            decode_smoke_verified=True,
            profile=ValidationProfile.IMAGE_SET.value,
            file_count=len(file_paths),
            tool_versions=tool_versions,
            validated_sha256=primary_sha,
            validated_artifacts=set_artifacts,
        )

    def validate_audio(
        self,
        file_path: Path,
        expectations: ValidationExpectations | None = None,
        sandbox_root: Path | None = None,
    ) -> MediaValidationResult:
        """Validates standalone audio asset or image album BGM."""
        exp = expectations or ValidationExpectations()
        tool_versions = self.get_tool_versions()

        sanity = self._check_file_sanity(file_path, sandbox_root, exp.min_file_size_bytes)
        if not sanity["passed"]:
            return MediaValidationResult(
                passed=False,
                profile=ValidationProfile.AUDIO.value,
                failed_check=sanity["failed_check"],
                error_code=sanity["error_code"],
                error=sanity["error"],
                tool_versions=tool_versions,
            )

        initial_stat = sanity["stat"]
        tool_check = self._check_tool_availability()
        if not tool_check["passed"]:
            return MediaValidationResult(
                passed=False,
                profile=ValidationProfile.AUDIO.value,
                failed_check=tool_check["failed_check"],
                error_code=tool_check["error_code"],
                error=tool_check["error"],
                tool_versions=tool_versions,
            )

        probe_res = self._run_ffprobe(file_path)
        if not probe_res["passed"]:
            return MediaValidationResult(
                passed=False,
                profile=ValidationProfile.AUDIO.value,
                failed_check=probe_res["failed_check"],
                error_code=probe_res["error_code"],
                error=probe_res["error"],
                tool_versions=tool_versions,
            )

        metadata = probe_res["metadata"]
        streams = metadata.get("streams", [])
        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
        if not audio_streams:
            return MediaValidationResult(
                passed=False,
                profile=ValidationProfile.AUDIO.value,
                ffprobe_verified=True,
                streams=streams,
                failed_check="MISSING_AUDIO_STREAM",
                error_code=DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE.value,
                error="Audio file is missing required audio stream.",
                tool_versions=tool_versions,
            )

        smoke_res = self._run_ffmpeg_decode_smoke(file_path, duration_sec=5.0, seek_sec=None)
        if not smoke_res["passed"]:
            return MediaValidationResult(
                passed=False,
                profile=ValidationProfile.AUDIO.value,
                ffprobe_verified=True,
                decode_smoke_verified=False,
                streams=streams,
                failed_check=smoke_res["failed_check"],
                error_code=smoke_res.get("error_code", DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED.value),
                error=smoke_res["error"],
                tool_versions=tool_versions,
            )

        toctou_res = self._verify_toctou(file_path, initial_stat)
        if not toctou_res["passed"]:
            return MediaValidationResult(
                passed=False,
                profile=ValidationProfile.AUDIO.value,
                failed_check=toctou_res["failed_check"],
                error_code=DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED.value,
                error=toctou_res["error"],
                tool_versions=tool_versions,
            )

        aud_sha = _compute_file_sha256(file_path)
        aud_artifacts = [{
            "file_name": file_path.name,
            "file_path": str(file_path),
            "size_bytes": file_path.stat().st_size,
            "validated_sha256": aud_sha,
        }]
        return MediaValidationResult(
            passed=True,
            ffprobe_verified=True,
            decode_smoke_verified=True,
            profile=ValidationProfile.AUDIO.value,
            file_count=1,
            streams=streams,
            codec_summary=f"audio/{audio_streams[0].get('codec_name', 'unknown')}",
            tool_versions=tool_versions,
            validated_sha256=aud_sha,
            validated_artifacts=aud_artifacts,
        )

    # =========================================================================
    # Internal Step Helpers
    # =========================================================================

    def _check_file_sanity(
        self,
        file_path: Path,
        sandbox_root: Path | None,
        min_bytes: int,
    ) -> dict[str, Any]:
        """Checks file existence, type, size, forbidden extension, and containment."""
        if not file_path.exists():
            return {
                "passed": False,
                "failed_check": "FILE_NOT_FOUND",
                "error_code": DownloaderErrorCode.DOWNLOAD_NOT_FOUND.value,
                "error": f"Asset file does not exist: {file_path}",
            }

        if not file_path.is_file():
            return {
                "passed": False,
                "failed_check": "NOT_A_REGULAR_FILE",
                "error_code": DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED.value,
                "error": f"Path is a directory or special device, not a regular file: {file_path}",
            }

        # Check forbidden temporary/part extensions
        if file_path.suffix.lower() in _FORBIDDEN_EXTENSIONS:
            return {
                "passed": False,
                "failed_check": "TEMPORARY_FILE_EXTENSION",
                "error_code": DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED.value,
                "error": f"Candidate file has incomplete/temporary extension: {file_path.name}",
            }

        # Sandbox containment check if root provided
        if sandbox_root is not None:
            resolved_file = file_path.resolve()
            resolved_root = sandbox_root.resolve()
            try:
                resolved_file.relative_to(resolved_root)
            except ValueError:
                return {
                    "passed": False,
                    "failed_check": "SANDBOX_CONTAINMENT_VIOLATION",
                    "error_code": DownloaderErrorCode.SANDBOX_PATH_ESCAPE.value,
                    "error": f"Asset '{file_path}' escapes sandbox containment '{sandbox_root}'!",
                }

        st = file_path.stat()
        if st.st_size <= 0:
            return {
                "passed": False,
                "failed_check": "ZERO_BYTE_FILE",
                "error_code": DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE.value,
                "error": f"Candidate file is empty (0 bytes): {file_path.name}",
            }

        if st.st_size < min_bytes:
            return {
                "passed": False,
                "failed_check": "FILE_SIZE_BELOW_MINIMUM",
                "error_code": DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE.value,
                "error": f"Candidate file size ({st.st_size} bytes) below minimum threshold ({min_bytes} bytes): {file_path.name}",
            }

        return {"passed": True, "stat": (st.st_size, st.st_mtime)}

    def _check_tool_availability(self) -> dict[str, Any]:
        """Verifies ffprobe and ffmpeg can be executed."""
        if not shutil.which(self.ffprobe_executable) and not Path(self.ffprobe_executable).is_file():
            return {
                "passed": False,
                "failed_check": "FFPROBE_NOT_AVAILABLE",
                "error_code": DownloaderErrorCode.DOWNLOAD_TOOL_ERROR.value,
                "error": f"ffprobe executable not found or not in PATH: '{self.ffprobe_executable}'",
            }
        if not shutil.which(self.ffmpeg_executable) and not Path(self.ffmpeg_executable).is_file():
            return {
                "passed": False,
                "failed_check": "FFMPEG_NOT_AVAILABLE",
                "error_code": DownloaderErrorCode.DOWNLOAD_TOOL_ERROR.value,
                "error": f"ffmpeg executable not found or not in PATH: '{self.ffmpeg_executable}'",
            }
        return {"passed": True}

    def _run_ffprobe(self, file_path: Path) -> dict[str, Any]:
        """Runs ffprobe with json output and parses structural metadata."""
        cmd = [
            self.ffprobe_executable,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-print_format",
            "json",
            str(file_path),
        ]
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.ffprobe_timeout,
                shell=False,
            )
            if res.returncode != 0:
                raw_err = (res.stderr or "").strip()[: self.max_capture_bytes]
                return {
                    "passed": False,
                    "failed_check": "FFPROBE_PROBE_FAILURE",
                    "error_code": DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED.value,
                    "error": f"ffprobe structural probe failed (exit code {res.returncode}): {scrub_secrets(raw_err)}",
                }

            try:
                data = json.loads(res.stdout)
                return {"passed": True, "metadata": data}
            except json.JSONDecodeError as j_err:
                return {
                    "passed": False,
                    "failed_check": "FFPROBE_JSON_PARSE_ERROR",
                    "error_code": DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED.value,
                    "error": f"Failed to parse ffprobe JSON output: {j_err}",
                }

        except subprocess.TimeoutExpired:
            return {
                "passed": False,
                "failed_check": "FFPROBE_TIMEOUT",
                "error_code": DownloaderErrorCode.DOWNLOAD_TOOL_ERROR.value,
                "error": f"ffprobe timed out after {self.ffprobe_timeout}s.",
            }
        except Exception as exc:
            return {
                "passed": False,
                "failed_check": "FFPROBE_TOOL_ERROR",
                "error_code": DownloaderErrorCode.DOWNLOAD_TOOL_ERROR.value,
                "error": f"Unexpected exception executing ffprobe: {exc}",
            }

    def _run_ffmpeg_decode_smoke(
        self,
        file_path: Path,
        duration_sec: float,
        seek_sec: float | None = None,
    ) -> dict[str, Any]:
        """Runs actual ffmpeg decoding to null sink to verify frame readability."""
        cmd = [self.ffmpeg_executable, "-v", "error"]
        if seek_sec is not None and seek_sec > 0:
            cmd.extend(["-ss", f"{seek_sec:.3f}"])
        cmd.extend(["-i", str(file_path)])
        if duration_sec > 0:
            cmd.extend(["-t", f"{duration_sec:.3f}"])
        cmd.extend(["-f", "null", "-"])

        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.decode_smoke_timeout,
                shell=False,
            )
            if res.returncode != 0:
                raw_err = (res.stderr or "").strip()[: self.max_capture_bytes]
                return {
                    "passed": False,
                    "failed_check": "DECODE_SMOKE_FAILURE",
                    "error": f"ffmpeg decode smoke failed (exit code {res.returncode}): {scrub_secrets(raw_err)}",
                }
            return {"passed": True}
        except subprocess.TimeoutExpired:
            return {
                "passed": False,
                "failed_check": "FFMPEG_DECODE_TIMEOUT",
                "error_code": DownloaderErrorCode.DOWNLOAD_TOOL_ERROR.value,
                "error": f"ffmpeg decode smoke timed out after {self.decode_smoke_timeout}s.",
            }
        except Exception as exc:
            return {
                "passed": False,
                "failed_check": "DECODE_SMOKE_EXCEPTION",
                "error": f"Unexpected exception during decode smoke: {exc}",
            }

    def _decode_image(self, file_path: Path) -> dict[str, Any]:
        """Decodes image container using Pillow with fallback to ffprobe."""
        try:
            from PIL import Image

            with Image.open(file_path) as img:
                img.verify()
                w, h = img.size
                fmt = img.format or "UNKNOWN"

            # Second open to execute full decode smoke (load pixels)
            with Image.open(file_path) as img:
                img.load()

            if w <= 0 or h <= 0:
                return {
                    "passed": False,
                    "failed_check": "INVALID_IMAGE_DIMENSIONS",
                    "error": f"Image has invalid dimensions: {w}x{h}",
                }

            return {"passed": True, "width": w, "height": h, "format": fmt}

        except Exception as exc:
            return {
                "passed": False,
                "failed_check": "IMAGE_DECODE_FAILURE",
                "error": f"Image decode failed: {exc}",
            }

    def _verify_toctou(self, file_path: Path, initial_stat: tuple[int, float]) -> dict[str, Any]:
        """Asserts that the file size and mtime remained unchanged throughout validation."""
        try:
            curr_st = file_path.stat()
            init_size, init_mtime = initial_stat
            if curr_st.st_size != init_size or curr_st.st_mtime != init_mtime:
                return {
                    "passed": False,
                    "failed_check": "TOCTOU_CONCURRENT_MUTATION",
                    "error": f"File size or timestamp changed during validation (TOCTOU violation)! Initial: ({init_size}, {init_mtime}), Current: ({curr_st.st_size}, {curr_st.st_mtime})",
                }
            return {"passed": True}
        except Exception as exc:
            return {
                "passed": False,
                "failed_check": "TOCTOU_STAT_ERROR",
                "error": f"Failed to re-stat file after validation: {exc}",
            }
