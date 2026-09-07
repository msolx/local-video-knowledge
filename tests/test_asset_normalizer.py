"""Comprehensive Test Suite for Asset Normalizer & MAX_PATH Shield (DY-D06).

Verifies:
1. Pre-Download Path Planning & MAX_PATH Budget Shield.
2. Deterministic, Sanitized Naming (Video, Image Album, BGM, Cover).
3. Image Album Strict Sequence Ordering (never filesystem/mtime order).
4. Extension Sanitization, Preservation, and Rejection of Temporary Files.
5. Windows Reserved Device Name Protection and Character Sanitization.
6. Path Containment & Escape Detection.
7. Collision Detection & Idempotency.
8. Atomic Manifest Persistence with Zero Secrets.
9. Immutability of Media Bytes & Zero Archive Write Boundary.
10. Integration with SafeDouyinDownloader (D01), TaskSandbox (D04), and MediaValidator (D05).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.collector.download_models import DownloadPriority, DownloadReason, DownloadTask
from src.downloader.contracts import (
    ArchivePromoter,
    AssetNormalizer,
    BackendDownloadResult,
    DownloadedAsset,
    DownloaderErrorCode,
    DownloaderStatus,
    ExecutionStage,
    NormalizedAsset,
)
from src.downloader.normalizer import (
    ArtifactCandidate,
    ArtifactRole,
    BackendOutputPlan,
    InvalidArtifactExtensionError,
    NormalizationCollisionError,
    NormalizationContainmentError,
    NormalizationError,
    NormalizationResult,
    NormalizedArtifact,
    PathBudgetExceededError,
    ProductionAssetNormalizer,
    sanitize_extension,
    sanitize_filename_component,
)
from src.downloader.safe_downloader import SafeDouyinDownloader
from src.downloader.sandbox import ProductionTaskSandboxProvider
from src.downloader.validator import ProductionMediaValidator

FIXTURE_DIR = Path(r"G:/antigravity-cli/dy/download_matrix/normalized")


@pytest.fixture
def normalizer() -> ProductionAssetNormalizer:
    """Returns a default ProductionAssetNormalizer."""
    return ProductionAssetNormalizer(max_path_budget=240, max_component_budget=255)


@pytest.fixture
def sandbox_env(tmp_path: Path) -> Path:
    """Creates a temporary sandbox environment."""
    sb = tmp_path / "sandbox_d06"
    sb.mkdir(parents=True, exist_ok=True)
    (sb / "output").mkdir(parents=True, exist_ok=True)
    return sb


# =============================================================================
# 1. Video Naming & Extension Policy Tests
# =============================================================================


class TestVideoNamingAndExtensions:
    def test_video_deterministic_naming(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path) -> None:
        """PRIMARY_VIDEO candidate normalizes deterministically to <platform_content_id>.<ext>."""
        raw_video = sandbox_env / "output" / "messy_backend_filename_12345.mp4"
        raw_video.write_bytes(b"dummy mp4 stream")

        cand = ArtifactCandidate(
            file_path=raw_video,
            role=ArtifactRole.PRIMARY_VIDEO,
            media_kind="video",
        )

        res = normalizer.normalize(
            raw_assets=[cand],
            platform_content_id="7671141177986518318",
            content_type="video",
            sandbox_root=sandbox_env,
        )

        assert len(res.artifacts) == 1
        art = res.artifacts[0]
        assert art.file_name == "7671141177986518318.mp4"
        assert art.normalized_path == sandbox_env / "output" / "7671141177986518318.mp4"
        assert art.normalized_path.exists()
        assert not raw_video.exists()  # Renamed within sandbox

    def test_h264_mp4_naming(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path) -> None:
        """H.264 MP4 fixture normalizes to content_id.mp4."""
        src = FIXTURE_DIR / "6611417973221494020.mp4"
        if not src.exists():
            pytest.skip("Fixture not found")

        dest = sandbox_env / "output" / "raw_h264.mp4"
        shutil.copy2(src, dest)

        res = normalizer.normalize(
            raw_assets=[dest],
            platform_content_id="6611417973221494020",
            content_type="video",
            sandbox_root=sandbox_env,
        )
        assert res.artifacts[0].file_name == "6611417973221494020.mp4"

    def test_hevc_mp4_naming(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path) -> None:
        """HEVC MP4 fixture normalizes to content_id.mp4."""
        src = FIXTURE_DIR / "7672935264565710131.mp4"
        if not src.exists():
            pytest.skip("Fixture not found")

        dest = sandbox_env / "output" / "raw_hevc.mp4"
        shutil.copy2(src, dest)

        res = normalizer.normalize(
            raw_assets=[dest],
            platform_content_id="7672935264565710131",
            content_type="video",
            sandbox_root=sandbox_env,
        )
        assert res.artifacts[0].file_name == "7672935264565710131.mp4"

    def test_extension_preserved(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path) -> None:
        """Original safe extensions like .mov or .mkv are preserved."""
        raw_mov = sandbox_env / "output" / "video.mov"
        raw_mov.write_bytes(b"quicktime mov")

        res = normalizer.normalize(
            raw_assets=[raw_mov],
            platform_content_id="123456789",
            sandbox_root=sandbox_env,
        )
        assert res.artifacts[0].file_name == "123456789.mov"

    def test_extension_not_blindly_changed_to_mp4(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path) -> None:
        """WebM extension is preserved, never blindly renamed to .mp4 without transcoding."""
        raw_webm = sandbox_env / "output" / "video.webm"
        raw_webm.write_bytes(b"webm container bytes")

        res = normalizer.normalize(
            raw_assets=[raw_webm],
            platform_content_id="987654321",
            content_type="video",
            sandbox_root=sandbox_env,
        )
        assert res.artifacts[0].file_name == "987654321.webm"


# =============================================================================
# 2. Image Album & Ordering Tests
# =============================================================================


class TestImageAlbumNormalization:
    def test_image_album_naming(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path) -> None:
        """Image album candidates normalize to <content_id>_img_001.webp, etc."""
        img1 = sandbox_env / "output" / "temp_1.webp"
        img2 = sandbox_env / "output" / "temp_2.webp"
        img1.write_bytes(b"webp1")
        img2.write_bytes(b"webp2")

        cands = [
            ArtifactCandidate(file_path=img1, role=ArtifactRole.ALBUM_IMAGE, media_kind="image", sequence_index=1),
            ArtifactCandidate(file_path=img2, role=ArtifactRole.ALBUM_IMAGE, media_kind="image", sequence_index=2),
        ]

        res = normalizer.normalize(
            raw_assets=cands,
            platform_content_id="7169622286633274635",
            content_type="image_album",
            sandbox_root=sandbox_env,
        )

        assert len(res.artifacts) == 2
        assert res.artifacts[0].file_name == "7169622286633274635_img_001.webp"
        assert res.artifacts[1].file_name == "7169622286633274635_img_002.webp"

    def test_image_stable_sequence_order(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path) -> None:
        """CRITICAL INVARIANT (Section N & AG): Output order strictly follows sequence_index, regardless of filesystem order."""
        # Filesystem created in reversed order
        img3 = sandbox_env / "output" / "alpha_third.webp"
        img1 = sandbox_env / "output" / "beta_first.webp"
        img2 = sandbox_env / "output" / "gamma_second.webp"
        img3.write_bytes(b"3")
        img1.write_bytes(b"1")
        img2.write_bytes(b"2")

        cands = [
            ArtifactCandidate(file_path=img3, role=ArtifactRole.ALBUM_IMAGE, media_kind="image", sequence_index=3),
            ArtifactCandidate(file_path=img1, role=ArtifactRole.ALBUM_IMAGE, media_kind="image", sequence_index=1),
            ArtifactCandidate(file_path=img2, role=ArtifactRole.ALBUM_IMAGE, media_kind="image", sequence_index=2),
        ]

        res = normalizer.normalize(
            raw_assets=cands,
            platform_content_id="7169622286633274635",
            content_type="image_album",
            sandbox_root=sandbox_env,
        )

        names = [a.file_name for a in res.artifacts]
        assert names == [
            "7169622286633274635_img_001.webp",
            "7169622286633274635_img_002.webp",
            "7169622286633274635_img_003.webp",
        ]
        # Verify content matches sequence_index, not creation order
        assert (sandbox_env / "output" / names[0]).read_bytes() == b"1"
        assert (sandbox_env / "output" / names[1]).read_bytes() == b"2"
        assert (sandbox_env / "output" / names[2]).read_bytes() == b"3"

    def test_missing_sequence_index_rejected(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path) -> None:
        """Album candidate missing sequence_index raises NormalizationError with MISSING_SEQUENCE_INDEX."""
        img = sandbox_env / "output" / "no_idx.webp"
        img.write_bytes(b"img")

        cand = ArtifactCandidate(file_path=img, role=ArtifactRole.ALBUM_IMAGE, media_kind="image", sequence_index=None)

        with pytest.raises(NormalizationError) as exc_info:
            normalizer.normalize(raw_assets=[cand], platform_content_id="111", sandbox_root=sandbox_env)
        assert exc_info.value.reason == "MISSING_SEQUENCE_INDEX"


# =============================================================================
# 3. BGM, Cover & Diagnostic Roles Tests
# =============================================================================


class TestAuxiliaryRoles:
    def test_bgm_naming(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path) -> None:
        """Candidate with role BGM_AUDIO normalizes to <content_id>_bgm.<ext>."""
        bgm_file = sandbox_env / "output" / "music_stream_99.mp3"
        bgm_file.write_bytes(b"mp3 audio stream")

        cand = ArtifactCandidate(file_path=bgm_file, role=ArtifactRole.BGM_AUDIO, media_kind="audio")
        res = normalizer.normalize(raw_assets=[cand], platform_content_id="7169622286633274635", sandbox_root=sandbox_env)

        assert res.artifacts[0].file_name == "7169622286633274635_bgm.mp3"

    def test_cover_naming(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path) -> None:
        """Candidate with role COVER_IMAGE normalizes to <content_id>_cover.<ext>."""
        cover_file = sandbox_env / "output" / "poster.jpg"
        cover_file.write_bytes(b"jpeg bytes")

        cand = ArtifactCandidate(file_path=cover_file, role=ArtifactRole.COVER_IMAGE, media_kind="image")
        res = normalizer.normalize(raw_assets=[cand], platform_content_id="7169622286633274635", sandbox_root=sandbox_env)

        assert res.artifacts[0].file_name == "7169622286633274635_cover.jpg"

    def test_diagnostic_file_excluded(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path) -> None:
        """Diagnostic files (.txt, .json, .log) are excluded from normalized media artifacts."""
        video_file = sandbox_env / "output" / "vid.mp4"
        video_file.write_bytes(b"video")
        desc_file = sandbox_env / "output" / "desc.txt"
        desc_file.write_bytes(b"video description")
        log_file = sandbox_env / "output" / "backend.log"
        log_file.write_bytes(b"backend log")

        cands = [
            ArtifactCandidate(file_path=video_file, role=ArtifactRole.PRIMARY_VIDEO, media_kind="video"),
            ArtifactCandidate(file_path=desc_file, role=ArtifactRole.OTHER_DIAGNOSTIC, media_kind="diagnostic"),
            ArtifactCandidate(file_path=log_file, role=ArtifactRole.OTHER_DIAGNOSTIC, media_kind="diagnostic"),
        ]

        res = normalizer.normalize(raw_assets=cands, platform_content_id="123", sandbox_root=sandbox_env)

        assert len(res.artifacts) == 1
        assert res.artifacts[0].file_name == "123.mp4"
        # Diagnostics remain in sandbox for D04 retention without being promoted
        assert desc_file.exists()
        assert log_file.exists()


# =============================================================================
# 4. Sanitization & Rejection Tests
# =============================================================================


class TestSanitizationAndRejection:
    def test_temp_file_rejected_part(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path) -> None:
        """.part file extension raises InvalidArtifactExtensionError."""
        part_file = sandbox_env / "output" / "video.mp4.part"
        part_file.write_bytes(b"incomplete")

        with pytest.raises(InvalidArtifactExtensionError):
            normalizer.normalize(raw_assets=[part_file], platform_content_id="123", sandbox_root=sandbox_env)

    @pytest.mark.parametrize("ext", [".tmp", ".crdownload", ".download", ".incomplete"])
    def test_temp_file_rejected_all(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path, ext: str) -> None:
        """All temporary browser / downloader extensions are rejected."""
        t_file = sandbox_env / "output" / f"video{ext}"
        t_file.write_bytes(b"temp")

        with pytest.raises(InvalidArtifactExtensionError):
            normalizer.normalize(raw_assets=[t_file], platform_content_id="123", sandbox_root=sandbox_env)

    def test_outside_source_rejected(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path, tmp_path: Path) -> None:
        """Candidate file outside sandbox root raises NormalizationContainmentError."""
        outside = tmp_path / "outside_sandbox.mp4"
        outside.write_bytes(b"outside")

        with pytest.raises(NormalizationContainmentError) as exc_info:
            normalizer.normalize(raw_assets=[outside], platform_content_id="123", sandbox_root=sandbox_env)
        assert exc_info.value.reason == "NORMALIZATION_PATH_ESCAPE"

    def test_relative_paths_computed(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path) -> None:
        """NormalizedArtifact records clean relative path using forward slashes."""
        vid = sandbox_env / "output" / "raw.mp4"
        vid.write_bytes(b"data")

        res = normalizer.normalize(raw_assets=[vid], platform_content_id="456", sandbox_root=sandbox_env)
        art = res.artifacts[0]
        assert art.normalized_relative_path == "output/456.mp4"

    def test_long_title_ignored(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path) -> None:
        """500+ character Unicode title in metadata_hint has zero impact on normalized filename."""
        vid = sandbox_env / "output" / "vid.mp4"
        vid.write_bytes(b"data")

        huge_title = "测试" * 250  # 500 characters
        res = normalizer.normalize(
            raw_assets=[vid],
            platform_content_id="7671141177986518318",
            metadata_hint={"title": huge_title, "nickname": "超长用户名"},
            sandbox_root=sandbox_env,
        )

        art = res.artifacts[0]
        assert art.file_name == "7671141177986518318.mp4"
        assert len(art.file_name) == len("7671141177986518318.mp4")

    def test_emoji_title_ignored(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path) -> None:
        """Emoji-heavy title in metadata has zero impact on filename."""
        vid = sandbox_env / "output" / "vid.mp4"
        vid.write_bytes(b"data")

        res = normalizer.normalize(
            raw_assets=[vid],
            platform_content_id="7671141177986518318",
            metadata_hint={"title": "🔥🎉🚀🐱‍👤🌟✨💎"},
            sandbox_root=sandbox_env,
        )
        assert res.artifacts[0].file_name == "7671141177986518318.mp4"

    def test_illegal_title_chars_ignored(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path) -> None:
        """Windows illegal filename chars in metadata do not corrupt filenames."""
        vid = sandbox_env / "output" / "vid.mp4"
        vid.write_bytes(b"data")

        res = normalizer.normalize(
            raw_assets=[vid],
            platform_content_id="7671141177986518318",
            metadata_hint={"title": '<>:"/\\|?*'},
            sandbox_root=sandbox_env,
        )
        assert res.artifacts[0].file_name == "7671141177986518318.mp4"

    @pytest.mark.parametrize("reserved", ["CON", "PRN", "AUX", "NUL", "COM1", "LPT1"])
    def test_windows_reserved_names_sanitizer(self, reserved: str) -> None:
        """Windows reserved device names are prefixed with underscore by sanitizer."""
        sanitized = sanitize_filename_component(reserved)
        assert sanitized == f"_{reserved}"

    def test_trailing_dot_space_sanitizer(self) -> None:
        """Trailing dots and spaces are stripped."""
        assert sanitize_filename_component("my_file...  ") == "my_file"

    def test_extension_sanitizer_rules(self) -> None:
        """Extension sanitizer converts to lowercase and handles non-standard characters."""
        ext, warns = sanitize_extension(".MP4")
        assert ext == ".mp4"

        ext, warns = sanitize_extension(".webm;foo=bar")
        assert ext == ".webmfoobar"
        assert len(warns) > 0

        with pytest.raises(InvalidArtifactExtensionError):
            sanitize_extension("../../etc/passwd")


# =============================================================================
# 5. MAX_PATH Shield & Budget Tests
# =============================================================================


class TestMaxPathShield:
    def test_backend_output_plan_creation(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path) -> None:
        """plan_output returns safe BackendOutputPlan with flat output root."""
        plan = normalizer.plan_output(
            sandbox_root=sandbox_env,
            platform_content_id="7671141177986518318",
            execution_id="exec_uuid_123",
        )
        assert isinstance(plan, BackendOutputPlan)
        assert plan.sandbox_output_root == sandbox_env / "output"
        assert plan.safe_filename_stem == "7671141177986518318"
        assert plan.max_path_budget == 240
        assert plan.allow_subdirectories is False

    def test_d03_handoff_serialization(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path) -> None:
        """BackendOutputPlan serializes cleanly to JSON dictionary for D03 consumption."""
        plan = normalizer.plan_output(sandbox_root=sandbox_env, platform_content_id="7671141177986518318")
        d = plan.to_dict()
        assert "sandbox_output_root" in d
        assert "safe_filename_stem" in d
        assert "max_path_budget" in d
        # Assert JSON serializable
        assert json.dumps(d)

    def test_full_path_budget_exceeded(self, sandbox_env: Path) -> None:
        """Target path exceeding max_path_budget raises PathBudgetExceededError."""
        strict_norm = ProductionAssetNormalizer(max_path_budget=50)  # Very low budget
        vid = sandbox_env / "output" / "vid.mp4"
        vid.write_bytes(b"data")

        with pytest.raises(PathBudgetExceededError) as exc_info:
            strict_norm.normalize(raw_assets=[vid], platform_content_id="1234567890", sandbox_root=sandbox_env)
        assert exc_info.value.reason == "PATH_BUDGET_EXCEEDED"

    def test_deep_sandbox_root_rejected_at_planning(self, tmp_path: Path) -> None:
        """Excessively deep sandbox root is rejected during pre-download planning."""
        deep_root = tmp_path / "deep_nested_directory_level_1" / "deep_nested_directory_level_2"
        deep_root.mkdir(parents=True, exist_ok=True)

        strict_norm = ProductionAssetNormalizer(max_path_budget=100)
        with pytest.raises(PathBudgetExceededError):
            strict_norm.plan_output(sandbox_root=deep_root, platform_content_id="7671141177986518318")


# =============================================================================
# 6. Collision & Idempotency Tests
# =============================================================================


class TestCollisionAndIdempotency:
    def test_collision_multiple_primary_videos(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path) -> None:
        """Multiple PRIMARY_VIDEO candidates raise NormalizationCollisionError."""
        vid1 = sandbox_env / "output" / "vid1.mp4"
        vid2 = sandbox_env / "output" / "vid2.mp4"
        vid1.write_bytes(b"v1")
        vid2.write_bytes(b"v2")

        cands = [
            ArtifactCandidate(file_path=vid1, role=ArtifactRole.PRIMARY_VIDEO),
            ArtifactCandidate(file_path=vid2, role=ArtifactRole.PRIMARY_VIDEO),
        ]

        with pytest.raises(NormalizationCollisionError) as exc_info:
            normalizer.normalize(raw_assets=cands, platform_content_id="123", sandbox_root=sandbox_env)
        assert exc_info.value.reason == "NORMALIZATION_COLLISION"

    def test_duplicate_image_index(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path) -> None:
        """Duplicate sequence_index in image album candidates raises NormalizationCollisionError."""
        img1 = sandbox_env / "output" / "img1.webp"
        img2 = sandbox_env / "output" / "img2.webp"
        img1.write_bytes(b"i1")
        img2.write_bytes(b"i2")

        cands = [
            ArtifactCandidate(file_path=img1, role=ArtifactRole.ALBUM_IMAGE, sequence_index=1),
            ArtifactCandidate(file_path=img2, role=ArtifactRole.ALBUM_IMAGE, sequence_index=1),
        ]

        with pytest.raises(NormalizationCollisionError) as exc_info:
            normalizer.normalize(raw_assets=cands, platform_content_id="123", sandbox_root=sandbox_env)
        assert exc_info.value.reason == "NORMALIZATION_COLLISION"

    def test_idempotent_normalization(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path) -> None:
        """Normalizing twice within the same sandbox is idempotent."""
        vid = sandbox_env / "output" / "raw.mp4"
        vid.write_bytes(b"my video")

        res1 = normalizer.normalize(raw_assets=[vid], platform_content_id="123", sandbox_root=sandbox_env)
        norm_path = res1.artifacts[0].normalized_path
        assert norm_path.exists()

        # Call again on normalized path
        res2 = normalizer.normalize(raw_assets=[norm_path], platform_content_id="123", sandbox_root=sandbox_env)
        assert len(res2.artifacts) == 1
        assert res2.artifacts[0].normalized_path == norm_path
        assert norm_path.exists()

    def test_conflicting_existing_target(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path) -> None:
        """If target exists with different content from source, raise NormalizationCollisionError."""
        target = sandbox_env / "output" / "123.mp4"
        target.write_bytes(b"already existing conflicting target content")

        source = sandbox_env / "output" / "different_source.mp4"
        source.write_bytes(b"different new source content")

        with pytest.raises(NormalizationCollisionError):
            normalizer.normalize(raw_assets=[source], platform_content_id="123", sandbox_root=sandbox_env)


# =============================================================================
# 7. Manifest & Invariance Tests
# =============================================================================


class TestManifestAndInvariants:
    def test_manifest_creation_and_no_secrets(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path) -> None:
        """normalization_manifest.json is created atomically in sandbox with relative paths and no secrets."""
        vid = sandbox_env / "output" / "vid.mp4"
        vid.write_bytes(b"data")

        res = normalizer.normalize(
            raw_assets=[vid],
            platform_content_id="7671141177986518318",
            task_id="task_dl_123?sessionid=secret_session_token",
            execution_id="exec_uuid_456?bearer=secret_bearer",
            sandbox_root=sandbox_env,
        )

        assert res.manifest_path is not None
        assert res.manifest_path.exists()

        manifest_content = json.loads(res.manifest_path.read_text(encoding="utf-8"))
        assert manifest_content["schema_version"] == "1.0"
        assert manifest_content["platform_content_id"] == "7671141177986518318"
        assert manifest_content["artifact_count"] == 1

        # Assert no absolute machine paths
        rel_path = manifest_content["artifacts"][0]["normalized_relative_path"]
        assert not rel_path.startswith("C:") and not rel_path.startswith("G:") and not rel_path.startswith("/")

        # Assert 100% secret scrubbing in manifest
        raw_manifest_text = res.manifest_path.read_text(encoding="utf-8")
        assert "secret_session_token" not in raw_manifest_text
        assert "secret_bearer" not in raw_manifest_text
        assert "[REDACTED]" in raw_manifest_text

    def test_no_media_byte_mutation(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path) -> None:
        """Normalizer renames/moves files but never modifies a single media byte."""
        src = FIXTURE_DIR / "6611417973221494020.mp4"
        if not src.exists():
            pytest.skip("Fixture not found")

        dest = sandbox_env / "output" / "raw_before_norm.mp4"
        shutil.copy2(src, dest)

        orig_hash = hashlib.sha256(dest.read_bytes()).hexdigest()
        orig_size = dest.stat().st_size

        res = normalizer.normalize(raw_assets=[dest], platform_content_id="6611417973221494020", sandbox_root=sandbox_env)
        norm_file = res.artifacts[0].normalized_path

        post_hash = hashlib.sha256(norm_file.read_bytes()).hexdigest()
        post_size = norm_file.stat().st_size

        assert orig_hash == post_hash
        assert orig_size == post_size

    def test_no_final_archive_access(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path, tmp_path: Path) -> None:
        """Normalizer strictly writes within sandbox, never touching canonical archive storage."""
        canonical_archive = tmp_path / "canonical_archive"
        canonical_archive.mkdir(parents=True, exist_ok=True)

        vid = sandbox_env / "output" / "vid.mp4"
        vid.write_bytes(b"data")

        res = normalizer.normalize(raw_assets=[vid], platform_content_id="123", sandbox_root=sandbox_env)
        assert len(list(canonical_archive.iterdir())) == 0


# =============================================================================
# 8. End-to-End Subsystem Integration Tests (D01 + D04 + D06 + D05)
# =============================================================================


class TestDownloaderSubsystemIntegration:
    def test_d05_integration_with_d06_output(self, normalizer: ProductionAssetNormalizer, sandbox_env: Path) -> None:
        """D05 MediaValidator validates the output directly produced by D06 Normalizer."""
        src = FIXTURE_DIR / "6611417973221494020.mp4"
        if not src.exists():
            pytest.skip("Fixture not found")

        raw_copy = sandbox_env / "output" / "raw_download_6611.mp4"
        shutil.copy2(src, raw_copy)

        # Stage 5: Normalization
        norm_res = normalizer.normalize(
            raw_assets=[raw_copy],
            platform_content_id="6611417973221494020",
            sandbox_root=sandbox_env,
        )
        assert len(norm_res.artifacts) == 1
        normalized_file = norm_res.artifacts[0].normalized_path

        # Stage 6: Validation
        validator = ProductionMediaValidator()
        val_res = validator.validate_assets([normalized_file], sandbox_root=sandbox_env)

        assert val_res.passed is True
        assert val_res.ffprobe_verified is True
        assert val_res.decode_smoke_verified is True

    def test_d01_integration_full_chain_success(self, tmp_path: Path) -> None:
        """Full pipeline integration: Production D04 + D06 + D05 with SafeDouyinDownloader."""
        fixture_src = FIXTURE_DIR / "6611417973221494020.mp4"
        if not fixture_src.exists():
            pytest.skip("Fixture not found")

        # Mock backend that uses plan and outputs candidate
        backend_mock = MagicMock()

        def fake_download(source_url: str, sandbox_dir: Path, download_input: dict[str, Any], credentials: Any = None) -> BackendDownloadResult:
            out_file = sandbox_dir / "output" / "backend_messy_name.mp4"
            out_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fixture_src, out_file)
            return BackendDownloadResult(success=True, raw_files=(out_file,), exit_code=0)

        backend_mock.execute_download.side_effect = fake_download

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

        sandbox_root = tmp_path / "sandboxes"
        sandbox_provider = ProductionTaskSandboxProvider(base_dir=sandbox_root)
        normalizer = ProductionAssetNormalizer()
        validator = ProductionMediaValidator()

        downloader = SafeDouyinDownloader(
            backend=backend_mock,
            sandbox_provider=sandbox_provider,
            normalizer=normalizer,
            validator=validator,
            promoter=promoter_mock,
        )

        task = DownloadTask(
            task_id="task_e2e_d06_001",
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
        assert promoter_mock.promote.call_count == 1
        # Check promoted asset name matches D06 normalization
        promoted = res.assets[0]
        assert promoted.file_name == "6611417973221494020.mp4"

    def test_main_env_no_f2(self) -> None:
        """Verifies normalizer.py does not import f2 anywhere."""
        normalizer_code = Path("src/downloader/normalizer.py").read_text(encoding="utf-8")
        assert "import f2" not in normalizer_code
        assert "from f2" not in normalizer_code
