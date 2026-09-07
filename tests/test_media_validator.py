"""Test Suite for Multi-Step Media Asset Validation Engine (DY-D05).

Verifies:
1. Tool Discovery & Availability (ffprobe/ffmpeg detection, missing tool handling).
2. File Sanity Checks (existence, regular file, forbidden extensions, 0-byte, min size).
3. Sandbox Containment Checks (path traversal / containment violation detection).
4. Structural Probe & Corruption Detection (random bytes, truncated containers, timeouts, parse errors).
5. Real Media Video Validation (H.264 portrait/landscape, HEVC 4K/1080p, short/long videos, tail smoke).
6. Stream Requirements & Expectations (mandatory vs optional audio, missing video stream, dimensions, duration).
7. Decode Smoke Testing (real decode to null sink, failure handling, timeout handling, tail smoke).
8. TOCTOU Concurrent Modification Detection.
9. Subprocess Safety (credential scrubbing in stderr, bounded capture).
10. Image & Image Set Validation (WebP single/album, count mismatch, corrupt members, Pillow decode smoke).
11. Audio Validation (MP3 decode smoke, corrupt audio rejection).
12. Downloader Integration (SafeDouyinDownloader + ProductionMediaValidator: success vs corrupt media blocking promoter).
13. Invariant: Validator is strictly READ-ONLY (no mutation, rename, deletion, or promotion).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.collector.download_models import DownloadPriority, DownloadReason, DownloadTask
from src.downloader.contracts import (
    ArchivePromoter,
    BackendDownloadResult,
    DownloadedAsset,
    DownloaderErrorCode,
    DownloaderStatus,
    ExecutionStage,
    NormalizedAsset,
    ValidationResult,
)
from src.downloader.safe_downloader import SafeDouyinDownloader
from src.downloader.sandbox import ProductionTaskSandboxProvider
from src.downloader.stubs import FakeArchivePromoter, FakeAssetNormalizer, FakeContentRouter
from src.downloader.validator import (
    MediaValidationResult,
    ProductionMediaValidator,
    ValidationExpectations,
    ValidationProfile,
)

FIXTURE_DIR = Path(r"G:/antigravity-cli/dy/download_matrix/normalized")


@pytest.fixture
def validator() -> ProductionMediaValidator:
    """Returns a ProductionMediaValidator using system ffmpeg and ffprobe."""
    return ProductionMediaValidator()


@pytest.fixture
def sandbox_env(tmp_path: Path) -> Path:
    """Prepares an isolated temporary sandbox directory."""
    sb = tmp_path / "sandbox_d05"
    sb.mkdir(parents=True, exist_ok=True)
    return sb


# =============================================================================
# 1. Tool Discovery & Availability
# =============================================================================


class TestToolDiscoveryAndAvailability:
    def test_tool_discovery_real_system(self, validator: ProductionMediaValidator) -> None:
        """Verifies system ffprobe and ffmpeg are discovered with valid version strings."""
        versions = validator.get_tool_versions()
        assert "ffprobe" in versions
        assert "ffmpeg" in versions
        assert versions["ffprobe"] != "NOT_FOUND"
        assert versions["ffmpeg"] != "NOT_FOUND"
        assert "ffprobe version" in versions["ffprobe"] or "ffmpeg version" in versions["ffprobe"]

    def test_missing_ffprobe_reports_tool_error(self, tmp_path: Path) -> None:
        """Missing ffprobe executable returns TOOL_ERROR and FFPROBE_NOT_AVAILABLE."""
        dummy_file = tmp_path / "dummy.mp4"
        dummy_file.write_bytes(b"content" * 100)

        bad_val = ProductionMediaValidator(ffprobe_executable="non_existent_ffprobe_bin")
        res = bad_val.validate_video(dummy_file)

        assert not res.passed
        assert res.failed_check == "FFPROBE_NOT_AVAILABLE"
        assert res.error_code == DownloaderErrorCode.DOWNLOAD_TOOL_ERROR.value
        assert "ffprobe executable not found" in str(res.error)

    def test_missing_ffmpeg_reports_tool_error(self, tmp_path: Path) -> None:
        """Missing ffmpeg executable returns TOOL_ERROR and FFMPEG_NOT_AVAILABLE."""
        dummy_file = tmp_path / "dummy.mp4"
        dummy_file.write_bytes(b"content" * 100)

        bad_val = ProductionMediaValidator(ffmpeg_executable="non_existent_ffmpeg_bin")
        res = bad_val.validate_video(dummy_file)

        assert not res.passed
        assert res.failed_check == "FFMPEG_NOT_AVAILABLE"
        assert res.error_code == DownloaderErrorCode.DOWNLOAD_TOOL_ERROR.value
        assert "ffmpeg executable not found" in str(res.error)


# =============================================================================
# 2. File Sanity Checks
# =============================================================================


class TestFileSanityChecks:
    def test_empty_asset_list_rejected(self, validator: ProductionMediaValidator) -> None:
        """Empty candidate asset list returns failure with NO_ASSETS_PROVIDED."""
        res = validator.validate_assets([])
        assert not res.passed
        assert res.failed_check == "NO_ASSETS_PROVIDED"
        assert res.file_count == 0

    def test_non_existent_file_rejected(self, validator: ProductionMediaValidator, tmp_path: Path) -> None:
        """Non-existent file path returns failure with FILE_NOT_FOUND."""
        non_existent = tmp_path / "ghost.mp4"
        res = validator.validate_video(non_existent)
        assert not res.passed
        assert res.failed_check == "FILE_NOT_FOUND"
        assert res.error_code == DownloaderErrorCode.DOWNLOAD_NOT_FOUND.value

    def test_directory_rejected(self, validator: ProductionMediaValidator, tmp_path: Path) -> None:
        """Directory path passed as candidate file returns NOT_A_REGULAR_FILE."""
        sub_dir = tmp_path / "fake_dir"
        sub_dir.mkdir()
        res = validator.validate_video(sub_dir)
        assert not res.passed
        assert res.failed_check == "NOT_A_REGULAR_FILE"

    def test_forbidden_extensions_part(self, validator: ProductionMediaValidator, tmp_path: Path) -> None:
        """.part file extension returns TEMPORARY_FILE_EXTENSION."""
        part_file = tmp_path / "video.mp4.part"
        part_file.write_bytes(b"downloading incomplete stream")
        res = validator.validate_video(part_file)
        assert not res.passed
        assert res.failed_check == "TEMPORARY_FILE_EXTENSION"

    @pytest.mark.parametrize("ext", [".tmp", ".crdownload", ".download", ".incomplete"])
    def test_forbidden_extensions_all(self, validator: ProductionMediaValidator, tmp_path: Path, ext: str) -> None:
        """All temporary browser / downloader extensions are rejected."""
        temp_file = tmp_path / f"video{ext}"
        temp_file.write_bytes(b"temp incomplete file")
        res = validator.validate_video(temp_file)
        assert not res.passed
        assert res.failed_check == "TEMPORARY_FILE_EXTENSION"

    def test_zero_byte_file_rejected(self, validator: ProductionMediaValidator, tmp_path: Path) -> None:
        """0-byte file returns ZERO_BYTE_FILE with DOWNLOAD_MEDIA_INCOMPLETE."""
        zero_file = tmp_path / "empty.mp4"
        zero_file.touch()
        res = validator.validate_video(zero_file)
        assert not res.passed
        assert res.failed_check == "ZERO_BYTE_FILE"
        assert res.error_code == DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE.value

    def test_sub_minimum_file_size_rejected(self, validator: ProductionMediaValidator, tmp_path: Path) -> None:
        """File smaller than min_file_size_bytes returns FILE_SIZE_BELOW_MINIMUM with DOWNLOAD_MEDIA_INCOMPLETE."""
        small_file = tmp_path / "tiny.mp4"
        small_file.write_bytes(b"small")
        exp = ValidationExpectations(min_file_size_bytes=500)
        res = validator.validate_video(small_file, expectations=exp)
        assert not res.passed
        assert res.failed_check == "FILE_SIZE_BELOW_MINIMUM"
        assert res.error_code == DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE.value

    def test_small_valid_media_regression(self, validator: ProductionMediaValidator, tmp_path: Path) -> None:
        """Confirms small media (e.g. 50 bytes) is not rejected by default size check (only 0-byte rejected)."""
        small_file = tmp_path / "small.mp4"
        small_file.write_bytes(b"X" * 50)
        sanity = validator._check_file_sanity(small_file, sandbox_root=None, min_bytes=ValidationExpectations().min_file_size_bytes)
        assert sanity["passed"] is True


# =============================================================================
# 3. Sandbox Containment Checks
# =============================================================================


class TestSandboxContainment:
    def test_sandbox_containment_violation_rejected(
        self, validator: ProductionMediaValidator, tmp_path: Path
    ) -> None:
        """File located outside sandbox root is rejected with SANDBOX_CONTAINMENT_VIOLATION."""
        sandbox_root = tmp_path / "allowed_sandbox"
        sandbox_root.mkdir()
        outside_file = tmp_path / "outside_evil.mp4"
        outside_file.write_bytes(b"outside content" * 100)

        res = validator.validate_video(outside_file, sandbox_root=sandbox_root)
        assert not res.passed
        assert res.failed_check == "SANDBOX_CONTAINMENT_VIOLATION"
        assert res.error_code == DownloaderErrorCode.SANDBOX_PATH_ESCAPE.value

    def test_sandbox_containment_pass(
        self, validator: ProductionMediaValidator, tmp_path: Path
    ) -> None:
        """File strictly inside sandbox root passes containment check."""
        sandbox_root = tmp_path / "allowed_sandbox"
        sandbox_root.mkdir()
        inside_file = sandbox_root / "test.mp4"

        # Copy real fixture
        fixture_src = FIXTURE_DIR / "6611417973221494020.mp4"
        if fixture_src.exists():
            shutil.copy2(fixture_src, inside_file)
            res = validator.validate_video(inside_file, sandbox_root=sandbox_root)
            assert res.passed is True


# =============================================================================
# 4. Structural Probe & Corruption Detection
# =============================================================================


class TestStructuralProbeAndCorruption:
    def test_random_bytes_corruption_rejected(
        self, validator: ProductionMediaValidator, tmp_path: Path
    ) -> None:
        """Corrupted file containing random garbage bytes fails structural probe."""
        corrupt_file = tmp_path / "corrupt_garbage.mp4"
        corrupt_file.write_bytes(os.urandom(8192))

        res = validator.validate_video(corrupt_file)
        assert not res.passed
        assert res.failed_check in ("FFPROBE_PROBE_FAILURE", "DECODE_SMOKE_FAILURE")

    def test_truncated_mp4_rejected(
        self, validator: ProductionMediaValidator, tmp_path: Path
    ) -> None:
        """Truncated MP4 containing only headers / partial moov fails probe or decode."""
        fixture_src = FIXTURE_DIR / "6611417973221494020.mp4"
        if not fixture_src.exists():
            pytest.skip("Fixture not found")

        truncated_file = tmp_path / "truncated.mp4"
        with open(fixture_src, "rb") as f_in:
            head_bytes = f_in.read(1024)  # First 1KB only
        truncated_file.write_bytes(head_bytes)

        res = validator.validate_video(truncated_file)
        assert not res.passed

    def test_ffprobe_timeout_handled(
        self, validator: ProductionMediaValidator, tmp_path: Path
    ) -> None:
        """ffprobe timeout returns FFPROBE_TIMEOUT and DOWNLOAD_TIMEOUT."""
        dummy_file = tmp_path / "dummy.mp4"
        dummy_file.write_bytes(b"dummy video content" * 50)

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["ffprobe"], timeout=1.0)):
            res = validator.validate_video(dummy_file)
            assert not res.passed
            assert res.failed_check == "FFPROBE_TIMEOUT"
            assert res.error_code == DownloaderErrorCode.DOWNLOAD_TOOL_ERROR.value

    def test_ffprobe_malformed_json_handled(
        self, validator: ProductionMediaValidator, tmp_path: Path
    ) -> None:
        """Corrupted / unparseable JSON from ffprobe returns FFPROBE_JSON_PARSE_ERROR."""
        dummy_file = tmp_path / "dummy.mp4"
        dummy_file.write_bytes(b"dummy video content" * 50)

        fake_res = MagicMock()
        fake_res.returncode = 0
        fake_res.stdout = "{incomplete_json_error"

        with patch("subprocess.run", return_value=fake_res):
            res = validator.validate_video(dummy_file)
            assert not res.passed
            assert res.failed_check == "FFPROBE_JSON_PARSE_ERROR"


# =============================================================================
# 5. Real Media Video Validation (H.264, HEVC, Portrait, Landscape, 4K)
# =============================================================================


class TestRealMediaVideoValidation:
    def test_real_h264_portrait_video(
        self, validator: ProductionMediaValidator, sandbox_env: Path
    ) -> None:
        """Validates real Douyin H.264 portrait video (720x1280)."""
        src = FIXTURE_DIR / "6611417973221494020.mp4"
        if not src.exists():
            pytest.skip(f"Fixture {src} not found")

        dest = sandbox_env / src.name
        shutil.copy2(src, dest)

        res = validator.validate_video(dest, sandbox_root=sandbox_env)
        assert res.passed is True
        assert res.ffprobe_verified is True
        assert res.decode_smoke_verified is True
        assert res.width == 720
        assert res.height == 1280
        assert res.duration_sec is not None and res.duration_sec > 0
        assert "h264" in (res.codec_summary or "").lower()

    def test_real_h264_landscape_video(
        self, validator: ProductionMediaValidator, sandbox_env: Path
    ) -> None:
        """Validates real Douyin H.264 landscape video (1920x1080)."""
        src = FIXTURE_DIR / "7047407240084557059.mp4"
        if not src.exists():
            pytest.skip(f"Fixture {src} not found")

        dest = sandbox_env / src.name
        shutil.copy2(src, dest)

        res = validator.validate_video(dest, sandbox_root=sandbox_env)
        assert res.passed is True
        assert res.width == 1920
        assert res.height == 1080

    def test_real_hevc_4k_video(
        self, validator: ProductionMediaValidator, sandbox_env: Path
    ) -> None:
        """Validates real Douyin HEVC 4K video (3870x2160)."""
        src = FIXTURE_DIR / "7672935264565710131.mp4"
        if not src.exists():
            pytest.skip(f"Fixture {src} not found")

        dest = sandbox_env / src.name
        shutil.copy2(src, dest)

        res = validator.validate_video(dest, sandbox_root=sandbox_env)
        assert res.passed is True
        assert res.width == 3870
        assert res.height == 2160
        assert "hevc" in (res.codec_summary or "").lower()

    def test_real_hevc_1080p_video(
        self, validator: ProductionMediaValidator, sandbox_env: Path
    ) -> None:
        """Validates real Douyin HEVC 1080p video (1920x1080)."""
        src = FIXTURE_DIR / "7681627509745519918.mp4"
        if not src.exists():
            pytest.skip(f"Fixture {src} not found")

        dest = sandbox_env / src.name
        shutil.copy2(src, dest)

        res = validator.validate_video(dest, sandbox_root=sandbox_env)
        assert res.passed is True
        assert res.width == 1920
        assert res.height == 1080
        assert "hevc" in (res.codec_summary or "").lower()

    def test_real_short_video(
        self, validator: ProductionMediaValidator, sandbox_env: Path
    ) -> None:
        """Validates real Douyin short duration video."""
        src = FIXTURE_DIR / "7043656151740714271.mp4"
        if not src.exists():
            pytest.skip(f"Fixture {src} not found")

        dest = sandbox_env / src.name
        shutil.copy2(src, dest)

        res = validator.validate_video(dest, sandbox_root=sandbox_env)
        assert res.passed is True
        assert res.duration_sec is not None and res.duration_sec > 0

    def test_real_long_video_with_tail_smoke(
        self, validator: ProductionMediaValidator, sandbox_env: Path
    ) -> None:
        """Validates longer video executing both head and tail decode smoke."""
        src = FIXTURE_DIR / "7681615068294843684.mp4"
        if not src.exists():
            pytest.skip(f"Fixture {src} not found")

        dest = sandbox_env / src.name
        shutil.copy2(src, dest)

        exp = ValidationExpectations(enable_tail_smoke=True, tail_smoke_seconds=2.0)
        res = validator.validate_video(dest, expectations=exp, sandbox_root=sandbox_env)
        assert res.passed is True
        assert res.decode_smoke_verified is True


# =============================================================================
# 6. Stream Requirements & Expectations
# =============================================================================


class TestStreamRequirements:
    def test_missing_video_stream_rejected(
        self, validator: ProductionMediaValidator, sandbox_env: Path
    ) -> None:
        """File with no video streams validated with require_video_stream=True fails."""
        audio_src = FIXTURE_DIR / "7169622286633274635" / "7169622286633274635_music.mp3"
        if not audio_src.exists():
            pytest.skip(f"Fixture {audio_src} not found")

        dest = sandbox_env / "fake_video_actually_audio.mp4"
        shutil.copy2(audio_src, dest)

        res = validator.validate_video(dest, expectations=ValidationExpectations(require_video_stream=True))
        assert not res.passed
        assert res.failed_check == "MISSING_VIDEO_STREAM"
        assert res.error_code == DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE.value

    def test_audio_stream_mandatory_present(
        self, validator: ProductionMediaValidator, sandbox_env: Path
    ) -> None:
        """Video with audio stream passes when expect_audio_stream=True."""
        src = FIXTURE_DIR / "6611417973221494020.mp4"
        if not src.exists():
            pytest.skip(f"Fixture {src} not found")

        dest = sandbox_env / src.name
        shutil.copy2(src, dest)

        res = validator.validate_video(dest, expectations=ValidationExpectations(expect_audio_stream=True))
        assert res.passed is True

    def test_audio_stream_mandatory_missing(
        self, validator: ProductionMediaValidator, tmp_path: Path
    ) -> None:
        """Video missing audio stream fails when expect_audio_stream=True."""
        dummy = tmp_path / "silent.mp4"
        dummy.write_bytes(b"dummy content" * 50)

        fake_probe = {
            "passed": True,
            "metadata": {
                "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920, "duration": "10.0"}],
                "format": {"duration": "10.0"},
            },
        }
        with patch.object(validator, "_run_ffprobe", return_value=fake_probe):
            res = validator.validate_video(dummy, expectations=ValidationExpectations(expect_audio_stream=True))
            assert not res.passed
            assert res.failed_check == "MISSING_AUDIO_STREAM"
            assert res.error_code == DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE.value

    def test_audio_stream_optional_missing_passes(
        self, validator: ProductionMediaValidator, tmp_path: Path
    ) -> None:
        """Video missing audio stream passes when expect_audio_stream is None or False."""
        dummy = tmp_path / "silent.mp4"
        dummy.write_bytes(b"dummy content" * 50)

        fake_probe = {
            "passed": True,
            "metadata": {
                "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920, "duration": "10.0"}],
                "format": {"duration": "10.0"},
            },
        }
        with patch.object(validator, "_run_ffprobe", return_value=fake_probe), \
             patch.object(validator, "_run_ffmpeg_decode_smoke", return_value={"passed": True}):
            res = validator.validate_video(dummy, expectations=ValidationExpectations(expect_audio_stream=None))
            assert res.passed is True

    def test_invalid_dimensions_rejected(
        self, validator: ProductionMediaValidator, tmp_path: Path
    ) -> None:
        """Probed video with width=0 or height=0 fails INVALID_DIMENSIONS."""
        dummy = tmp_path / "bad_dim.mp4"
        dummy.write_bytes(b"dummy content" * 50)

        fake_probe = {
            "passed": True,
            "metadata": {
                "streams": [{"codec_type": "video", "codec_name": "h264", "width": 0, "height": 1080}],
                "format": {"duration": "10.0"},
            },
        }
        with patch.object(validator, "_run_ffprobe", return_value=fake_probe):
            res = validator.validate_video(dummy)
            assert not res.passed
            assert res.failed_check == "INVALID_DIMENSIONS"

    def test_invalid_duration_rejected(
        self, validator: ProductionMediaValidator, tmp_path: Path
    ) -> None:
        """Probed video with duration=0 fails INVALID_DURATION."""
        dummy = tmp_path / "zero_dur.mp4"
        dummy.write_bytes(b"dummy content" * 50)

        fake_probe = {
            "passed": True,
            "metadata": {
                "streams": [{"codec_type": "video", "codec_name": "h264", "width": 720, "height": 1280}],
                "format": {"duration": "0.0"},
            },
        }
        with patch.object(validator, "_run_ffprobe", return_value=fake_probe):
            res = validator.validate_video(dummy)
            assert not res.passed
            assert res.failed_check == "INVALID_DURATION"

    def test_duration_expectation_warning(
        self, validator: ProductionMediaValidator, sandbox_env: Path
    ) -> None:
        """Duration mismatch with expected value exceeds tolerance and generates warning."""
        src = FIXTURE_DIR / "6611417973221494020.mp4"
        if not src.exists():
            pytest.skip(f"Fixture {src} not found")

        dest = sandbox_env / src.name
        shutil.copy2(src, dest)

        # Expected duration 5000ms (5s) whereas fixture is ~59s
        exp = ValidationExpectations(expected_duration_ms=5000)
        res = validator.validate_video(dest, expectations=exp, sandbox_root=sandbox_env)
        assert res.passed is True
        assert len(res.warnings) > 0
        assert any("Duration mismatch" in w for w in res.warnings)


# =============================================================================
# 7. Decode Smoke Testing
# =============================================================================


class TestDecodeSmoke:
    def test_decode_smoke_failure_handled(
        self, validator: ProductionMediaValidator, tmp_path: Path
    ) -> None:
        """ffmpeg decode smoke failure (exit code != 0) returns DECODE_SMOKE_FAILURE."""
        dummy = tmp_path / "corrupt_decode.mp4"
        dummy.write_bytes(b"dummy content" * 50)

        fake_probe = {
            "passed": True,
            "metadata": {
                "streams": [{"codec_type": "video", "codec_name": "h264", "width": 720, "height": 1280, "duration": "10.0"}],
                "format": {"duration": "10.0"},
            },
        }
        with patch.object(validator, "_run_ffprobe", return_value=fake_probe), \
             patch.object(validator, "_run_ffmpeg_decode_smoke", return_value={"passed": False, "failed_check": "DECODE_SMOKE_FAILURE", "error": "Invalid NAL unit"}):
            res = validator.validate_video(dummy)
            assert not res.passed
            assert res.failed_check == "DECODE_SMOKE_FAILURE"
            assert res.decode_smoke_verified is False

    def test_decode_smoke_timeout_handled(
        self, validator: ProductionMediaValidator, tmp_path: Path
    ) -> None:
        """ffmpeg decode smoke timeout returns FFMPEG_DECODE_TIMEOUT and DOWNLOAD_TOOL_ERROR."""
        dummy = tmp_path / "timeout_decode.mp4"
        dummy.write_bytes(b"dummy content" * 50)

        fake_probe = {
            "passed": True,
            "metadata": {
                "streams": [{"codec_type": "video", "codec_name": "h264", "width": 720, "height": 1280, "duration": "10.0"}],
                "format": {"duration": "10.0"},
            },
        }
        with patch.object(validator, "_run_ffprobe", return_value=fake_probe), \
             patch.object(validator, "_run_ffmpeg_decode_smoke", return_value={"passed": False, "failed_check": "FFMPEG_DECODE_TIMEOUT", "error_code": DownloaderErrorCode.DOWNLOAD_TOOL_ERROR.value, "error": "ffmpeg decode smoke timed out"}):
            res = validator.validate_video(dummy)
            assert not res.passed
            assert res.failed_check == "FFMPEG_DECODE_TIMEOUT"
            assert res.error_code == DownloaderErrorCode.DOWNLOAD_TOOL_ERROR.value

    def test_tail_smoke_truncation_detection(
        self, validator: ProductionMediaValidator, tmp_path: Path
    ) -> None:
        """Simulated tail decode smoke failure returns DECODE_TAIL_SMOKE_FAILURE."""
        dummy = tmp_path / "tail_corrupt.mp4"
        dummy.write_bytes(b"dummy content" * 50)

        fake_probe = {
            "passed": True,
            "metadata": {
                "streams": [{"codec_type": "video", "codec_name": "h264", "width": 720, "height": 1280, "duration": "30.0"}],
                "format": {"duration": "30.0"},
            },
        }

        # Head smoke passes, tail smoke fails
        def fake_smoke(file_path: Path, duration_sec: float, seek_sec: float | None = None) -> dict[str, Any]:
            if seek_sec is not None and seek_sec > 0:
                return {"passed": False, "failed_check": "DECODE_SMOKE_FAILURE", "error": "End of file error"}
            return {"passed": True}

        with patch.object(validator, "_run_ffprobe", return_value=fake_probe), \
             patch.object(validator, "_run_ffmpeg_decode_smoke", side_effect=fake_smoke):
            exp = ValidationExpectations(enable_tail_smoke=True)
            res = validator.validate_video(dummy, expectations=exp)
            assert not res.passed
            assert res.failed_check == "DECODE_TAIL_SMOKE_FAILURE"


# =============================================================================
# 8. TOCTOU Verification
# =============================================================================


class TestToctouVerification:
    def test_toctou_mutation_detected(
        self, validator: ProductionMediaValidator, sandbox_env: Path
    ) -> None:
        """File size or mtime modification during validation triggers TOCTOU_CONCURRENT_MUTATION."""
        src = FIXTURE_DIR / "6611417973221494020.mp4"
        if not src.exists():
            pytest.skip(f"Fixture {src} not found")

        dest = sandbox_env / src.name
        shutil.copy2(src, dest)

        # Mutate file during decode smoke check
        orig_decode = validator._run_ffmpeg_decode_smoke

        def mutating_smoke(file_path: Path, duration_sec: float, seek_sec: float | None = None) -> dict[str, Any]:
            res = orig_decode(file_path, duration_sec, seek_sec)
            # Append 1 byte to violate TOCTOU size
            with open(file_path, "ab") as f:
                f.write(b"X")
            return res

        with patch.object(validator, "_run_ffmpeg_decode_smoke", side_effect=mutating_smoke):
            res = validator.validate_video(dest, sandbox_root=sandbox_env)
            assert not res.passed
            assert res.failed_check == "TOCTOU_CONCURRENT_MUTATION"


# =============================================================================
# 9. Subprocess Safety
# =============================================================================


class TestSubprocessSafety:
    def test_secret_scrubbing_in_error_message(
        self, validator: ProductionMediaValidator, tmp_path: Path
    ) -> None:
        """Sensitive cookies or credentials leaking into stderr are scrubbed in validation result."""
        dummy = tmp_path / "dummy.mp4"
        dummy.write_bytes(b"dummy content" * 50)

        leak_stderr = "Error reading stream: sessionid=secret_session_12345; passport_csrf_token=csrf_tok_abc;"
        fake_res = MagicMock()
        fake_res.returncode = 1
        fake_res.stderr = leak_stderr

        with patch("subprocess.run", return_value=fake_res):
            res = validator.validate_video(dummy)
            assert not res.passed
            assert "secret_session_12345" not in str(res.error)
            assert "csrf_tok_abc" not in str(res.error)
            assert "[REDACTED]" in str(res.error)

    def test_bounded_stderr_capture(
        self, tmp_path: Path
    ) -> None:
        """Massive stderr output is capped to max_capture_bytes."""
        dummy = tmp_path / "dummy.mp4"
        dummy.write_bytes(b"dummy content" * 50)

        val = ProductionMediaValidator(max_capture_bytes=200)
        huge_stderr = "A" * 10000
        fake_res = MagicMock()
        fake_res.returncode = 1
        fake_res.stderr = huge_stderr

        with patch("subprocess.run", return_value=fake_res):
            res = val.validate_video(dummy)
            assert not res.passed
            assert len(str(res.error)) < 500


# =============================================================================
# 10. Image & Image Set Validation
# =============================================================================


class TestImageAndImageSetValidation:
    def test_real_single_image_webp(
        self, validator: ProductionMediaValidator, sandbox_env: Path
    ) -> None:
        """Validates real Douyin WebP image asset through Pillow decode smoke."""
        img_src = FIXTURE_DIR / "7169622286633274635" / "7169622286633274635_image_1.webp"
        if not img_src.exists():
            pytest.skip(f"Fixture {img_src} not found")

        dest = sandbox_env / img_src.name
        shutil.copy2(img_src, dest)

        res = validator.validate_image(dest, sandbox_root=sandbox_env)
        assert res.passed is True
        assert res.profile == ValidationProfile.IMAGE.value
        assert res.width is not None and res.width > 0
        assert res.height is not None and res.height > 0
        assert res.codec_summary == "image/webp"

    def test_real_image_set_webp_album(
        self, validator: ProductionMediaValidator, sandbox_env: Path
    ) -> None:
        """Validates album of multiple real WebP images."""
        album_dir = FIXTURE_DIR / "7169622286633274635"
        if not album_dir.exists():
            pytest.skip(f"Album fixture {album_dir} not found")

        webp_files = sorted(album_dir.glob("*.webp"))
        if len(webp_files) < 3:
            pytest.skip("Expected at least 3 webp fixtures")

        copied_paths = []
        for p in webp_files:
            dest = sandbox_env / p.name
            shutil.copy2(p, dest)
            copied_paths.append(dest)

        res = validator.validate_image_set(copied_paths, sandbox_root=sandbox_env)
        assert res.passed is True
        assert res.profile == ValidationProfile.IMAGE_SET.value
        assert res.file_count == len(copied_paths)

    def test_image_set_minimum_count_violation(
        self, validator: ProductionMediaValidator, sandbox_env: Path
    ) -> None:
        """Image set below minimum_image_count fails IMAGE_COUNT_BELOW_MINIMUM."""
        img_src = FIXTURE_DIR / "7169622286633274635" / "7169622286633274635_image_1.webp"
        if not img_src.exists():
            pytest.skip(f"Fixture {img_src} not found")

        dest = sandbox_env / img_src.name
        shutil.copy2(img_src, dest)

        exp = ValidationExpectations(minimum_image_count=3)
        res = validator.validate_image_set([dest], expectations=exp, sandbox_root=sandbox_env)
        assert not res.passed
        assert res.failed_check == "IMAGE_COUNT_BELOW_MINIMUM"

    def test_image_set_corrupted_member(
        self, validator: ProductionMediaValidator, sandbox_env: Path
    ) -> None:
        """Image album with one corrupted member fails with IMAGE_SET_CORRUPT_MEMBER."""
        img_src = FIXTURE_DIR / "7169622286633274635" / "7169622286633274635_image_1.webp"
        if not img_src.exists():
            pytest.skip(f"Fixture {img_src} not found")

        valid_dest = sandbox_env / "valid.webp"
        shutil.copy2(img_src, valid_dest)

        corrupt_dest = sandbox_env / "corrupt.webp"
        corrupt_dest.write_bytes(os.urandom(2048))

        res = validator.validate_image_set([valid_dest, corrupt_dest], sandbox_root=sandbox_env)
        assert not res.passed
        assert "IMAGE_SET_CORRUPT_MEMBER" in (res.failed_check or "")

    def test_corrupt_single_image_rejected(
        self, validator: ProductionMediaValidator, tmp_path: Path
    ) -> None:
        """File with .webp extension containing non-image bytes fails IMAGE_DECODE_FAILURE."""
        bad_img = tmp_path / "bad.webp"
        bad_img.write_bytes(b"not an image at all" * 50)

        res = validator.validate_image(bad_img)
        assert not res.passed
        assert res.failed_check == "IMAGE_DECODE_FAILURE"


# =============================================================================
# 11. Audio Validation
# =============================================================================


class TestAudioValidation:
    def test_real_mp3_audio(
        self, validator: ProductionMediaValidator, sandbox_env: Path
    ) -> None:
        """Validates real Douyin BGM MP3 audio asset."""
        audio_src = FIXTURE_DIR / "7169622286633274635" / "7169622286633274635_music.mp3"
        if not audio_src.exists():
            pytest.skip(f"Fixture {audio_src} not found")

        dest = sandbox_env / audio_src.name
        shutil.copy2(audio_src, dest)

        res = validator.validate_audio(dest, sandbox_root=sandbox_env)
        assert res.passed is True
        assert res.profile == ValidationProfile.AUDIO.value
        assert "mp3" in (res.codec_summary or "").lower()

    def test_corrupt_audio_rejected(
        self, validator: ProductionMediaValidator, tmp_path: Path
    ) -> None:
        """Random bytes with .mp3 extension fails structural probe or decode."""
        bad_audio = tmp_path / "bad.mp3"
        bad_audio.write_bytes(os.urandom(4096))

        res = validator.validate_audio(bad_audio)
        assert not res.passed


# =============================================================================
# 12. SafeDouyinDownloader Integration (D01 + D05)
# =============================================================================


class TestSafeDownloaderIntegration:
    def test_safe_downloader_with_production_validator_success(
        self, validator: ProductionMediaValidator, tmp_path: Path
    ) -> None:
        """Integration test: SafeDouyinDownloader executes backend, production validator passes, and promoter promotes."""
        fixture_src = FIXTURE_DIR / "6611417973221494020.mp4"
        if not fixture_src.exists():
            pytest.skip(f"Fixture {fixture_src} not found")

        # Mock backend that copies real fixture to sandbox output
        backend_mock = MagicMock()

        def fake_download(source_url: str, sandbox_dir: Path, download_input: dict[str, Any], credentials: Any = None) -> BackendDownloadResult:
            out_file = sandbox_dir / "output" / "video_6611417973221494020.mp4"
            out_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fixture_src, out_file)
            return BackendDownloadResult(success=True, raw_files=(out_file,), exit_code=0)

        backend_mock.execute_download.side_effect = fake_download

        # Promoter mock to verify promotion occurs
        promoter_mock = MagicMock(spec=ArchivePromoter)
        promoter_mock.promote.side_effect = lambda normalized_assets, target_directory: [
            DownloadedAsset(
                file_name=na.file_name,
                relative_path=na.file_name,
                size_bytes=na.file_path.stat().st_size,
                content_type=na.content_type,
            )
            for na in normalized_assets
        ]

        sandbox_root = tmp_path / "downloader_sandboxes"
        sandbox_provider = ProductionTaskSandboxProvider(base_dir=sandbox_root)

        downloader = SafeDouyinDownloader(
            backend=backend_mock,
            sandbox_provider=sandbox_provider,
            validator=validator,
            promoter=promoter_mock,
        )

        task = DownloadTask(
            task_id="task_int_val_001",
            source_url="https://www.douyin.com/video/6611417973221494020",
            platform="douyin",
            platform_content_id="6611417973221494020",
            content_type="video",
            priority=DownloadPriority.NEW_COLLECTION,
            reason=DownloadReason.NEW_COLLECTION_ITEM,
            scope_id="douyin:test",
            created_at="2026-09-05T00:00:00Z",
        )

        res = downloader.execute(task)

        assert res.status == DownloaderStatus.SUCCESS
        assert res.validation["passed"] is True
        assert res.validation["ffprobe_verified"] is True
        assert res.validation["decode_smoke_verified"] is True
        assert promoter_mock.promote.call_count == 1

    def test_safe_downloader_with_corrupt_media_blocks_promotion(
        self, validator: ProductionMediaValidator, tmp_path: Path
    ) -> None:
        """Integration test: Backend reports exit code 0 but outputs 0-byte file; validator FAILS and ArchivePromoter is NEVER called."""
        backend_mock = MagicMock()

        def fake_download_corrupt(source_url: str, sandbox_dir: Path, download_input: dict[str, Any], credentials: Any = None) -> BackendDownloadResult:
            out_file = sandbox_dir / "output" / "corrupt_0byte.mp4"
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.touch()  # 0-byte file
            return BackendDownloadResult(success=True, raw_files=(out_file,), exit_code=0)

        backend_mock.execute_download.side_effect = fake_download_corrupt

        promoter_mock = MagicMock(spec=ArchivePromoter)
        sandbox_root = tmp_path / "downloader_sandboxes"
        sandbox_provider = ProductionTaskSandboxProvider(base_dir=sandbox_root)

        downloader = SafeDouyinDownloader(
            backend=backend_mock,
            sandbox_provider=sandbox_provider,
            validator=validator,
            promoter=promoter_mock,
        )

        task = DownloadTask(
            task_id="task_int_corrupt_002",
            source_url="https://www.douyin.com/video/7047407240084557059",
            platform="douyin",
            platform_content_id="7047407240084557059",
            content_type="video",
            priority=DownloadPriority.NEW_COLLECTION,
            reason=DownloadReason.NEW_COLLECTION_ITEM,
            scope_id="douyin:test",
            created_at="2026-09-05T00:00:00Z",
        )

        res = downloader.execute(task)

        assert res.status == DownloaderStatus.FAILED
        assert res.stage == ExecutionStage.VALIDATING
        assert "empty (0 bytes)" in str(res.message) or "ZERO_BYTE_FILE" in str(res.message)
        # CRITICAL INVARIANT: Promoter MUST NOT be called if validator fails!
        assert promoter_mock.promote.call_count == 0


# =============================================================================
# 13. Read-Only Invariant
# =============================================================================


class TestReadOnlyInvariant:
    def test_validator_is_strictly_read_only(
        self, validator: ProductionMediaValidator, sandbox_env: Path
    ) -> None:
        """Validating a media file does not modify, rename, delete, or create any file."""
        src = FIXTURE_DIR / "6611417973221494020.mp4"
        if not src.exists():
            pytest.skip(f"Fixture {src} not found")

        dest = sandbox_env / src.name
        shutil.copy2(src, dest)

        # Record pre-validation file state
        pre_stat = dest.stat()
        pre_hash = hashlib.sha256(dest.read_bytes()).hexdigest()
        pre_dir_contents = sorted(p.name for p in sandbox_env.iterdir())

        # Execute validation
        res = validator.validate_video(dest, sandbox_root=sandbox_env)
        assert res.passed is True

        # Record post-validation file state
        post_stat = dest.stat()
        post_hash = hashlib.sha256(dest.read_bytes()).hexdigest()
        post_dir_contents = sorted(p.name for p in sandbox_env.iterdir())

        assert pre_hash == post_hash
        assert pre_stat.st_size == post_stat.st_size
        assert pre_stat.st_mtime == post_stat.st_mtime
        assert pre_dir_contents == post_dir_contents
