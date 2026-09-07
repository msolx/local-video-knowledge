"""DY-D07: Comprehensive Test Suite for Production Archive Promotion Engine.

Covers:
- Basic & multi-asset atomic promotion
- Authoritative SHA-256 computation and verification
- Formal asset_manifest.json sidecar schema & atomic commit
- Validation binding & TOCTOU mutation rejection
- Idempotency & conflict defenses (same bytes -> IDEMPOTENT_EXISTING, diff -> ARCHIVE_CONFLICT)
- Staging directory isolation & atomic final visibility
- Cross-filesystem safe fallback (mock EXDEV / copy-to-staging + atomic replace)
- Transfer corruption detection (tamper destination temp byte -> fail)
- Mid-copy & partial album failure transaction rollbacks
- Independent FinalArchivePathBudget (<240 chars)
- Sandbox preservation (promoter never deletes source files)
- Full integration with D04 Sandbox, D06 Normalizer, D05 Validator, and D01 SafeDouyinDownloader
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from src.collector.download_models import DownloadPriority, DownloadReason, DownloadTask
from src.downloader.contracts import (
    DownloadedAsset,
    DownloaderErrorCode,
    DownloaderStatus,
    ExecutionStage,
    NormalizedAsset,
    ValidationResult,
)
from src.downloader.normalizer import (
    ArtifactCandidate,
    ArtifactRole,
    NormalizedArtifact,
    ProductionAssetNormalizer,
)
from src.downloader.promoter import (
    ArchiveBudgetExceededError,
    ArchiveConflictError,
    FinalArchivePathBudget,
    InvalidPromotionArtifactError,
    ProductionArchivePromoter,
    PromotedAsset,
    PromotionContainmentError,
    PromotionCorruptionError,
    PromotionError,
    PromotionPreconditionError,
    PromotionResult,
    PromotionStatus,
    compute_file_sha256,
)
from src.downloader.safe_downloader import SafeDouyinDownloader
from src.downloader.sandbox import ProductionTaskSandboxProvider
from src.downloader.stubs import FakeContentRouter, FakeDownloadBackend, FakeMediaValidator
from src.downloader.validator import ProductionMediaValidator

FIXTURE_DIR = Path(r"G:/antigravity-cli/dy/download_matrix/normalized")


def make_artifact(
    file_path: Path,
    role: ArtifactRole = ArtifactRole.PRIMARY_VIDEO,
    sequence_index: int | None = None,
    media_kind: str = "video",
    byte_size: int | None = None,
) -> NormalizedArtifact:
    """Helper to instantiate a valid NormalizedArtifact with both NormalizedAsset and NormalizedArtifact fields."""
    size = byte_size if byte_size is not None else (file_path.stat().st_size if file_path.exists() else 0)
    return NormalizedArtifact(
        file_name=file_path.name,
        file_path=file_path,
        content_type=media_kind,
        platform_content_id=file_path.stem.split("_img_")[0].split("_bgm")[0].split("_cover")[0],
        role=str(role.value if hasattr(role, "value") else role),
        sequence_index=sequence_index,
        normalized_path=file_path,
        normalized_relative_path=f"output/{file_path.name}",
        original_candidate_reference=file_path.name,
        byte_size=size,
        media_kind=media_kind,
        extension=file_path.suffix,
        metadata={"size_bytes": size},
    )


@pytest.fixture
def archive_env(tmp_path: Path) -> tuple[Path, Path]:
    """Creates a temporary sandbox and a temporary canonical archive root."""
    sandbox_dir = tmp_path / "sandbox_d07"
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    (sandbox_dir / "output").mkdir(parents=True, exist_ok=True)

    archive_dir = tmp_path / "canonical_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    return sandbox_dir, archive_dir


@pytest.fixture
def promoter(archive_env: tuple[Path, Path]) -> ProductionArchivePromoter:
    _, archive_root = archive_env
    return ProductionArchivePromoter(archive_root=archive_root, require_validation=True)


# =============================================================================
# 1. Basic Promotion & SHA-256 Authority Tests
# =============================================================================


class TestBasicPromotionAndShaAuthority:
    def test_basic_promotion(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """Basic video promotion moves file into canonical layout with authoritative SHA-256."""
        sandbox, archive_root = archive_env
        vid_file = sandbox / "output" / "7671141177986518318.mp4"
        vid_file.write_bytes(b"sample video bytes for basic promotion test")

        norm = make_artifact(vid_file)
        val = ValidationResult(passed=True, ffprobe_verified=True, decode_smoke_verified=True)

        res = promoter.promote(
            normalized_assets=[norm],
            validation_result=val,
            platform="douyin",
            platform_content_id="7671141177986518318",
            task_id="task_001",
            sandbox_root=sandbox,
        )

        assert res.status == PromotionStatus.SUCCESS
        assert len(res.promoted_assets) == 1
        promoted = res.promoted_assets[0]

        expected_target = archive_root / "douyin" / "7671141177986518318" / "7671141177986518318.mp4"
        assert expected_target.exists()
        assert promoted.absolute_archive_path == expected_target
        assert promoted.file_name == "7671141177986518318.mp4"
        assert promoted.relative_archive_path == "douyin/7671141177986518318/7671141177986518318.mp4"

    def test_final_sha256_authority(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """D07 is the authoritative producer of Canonical Asset SHA-256."""
        sandbox, archive_root = archive_env
        data = b"authoritative sha-256 content test 12345"
        expected_sha = hashlib.sha256(data).hexdigest()

        vid_file = sandbox / "output" / "sha_test.mp4"
        vid_file.write_bytes(data)

        norm = make_artifact(vid_file)

        res = promoter.promote(
            normalized_assets=[norm],
            validation_result=ValidationResult(passed=True),
            platform="douyin",
            platform_content_id="sha_test",
            sandbox_root=sandbox,
        )

        assert res.promoted_assets[0].sha256 == expected_sha
        dest_file = res.promoted_assets[0].absolute_archive_path
        assert compute_file_sha256(dest_file) == expected_sha

    def test_relative_archive_path(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """Relative archive path strictly uses forward slashes and platform/content_id prefix."""
        sandbox, _ = archive_env
        vid_file = sandbox / "output" / "rel_test.mp4"
        vid_file.write_bytes(b"data")

        norm = make_artifact(vid_file)

        res = promoter.promote(
            normalized_assets=[norm],
            validation_result=ValidationResult(passed=True),
            platform="douyin",
            platform_content_id="rel_test",
            sandbox_root=sandbox,
        )
        rel_path = res.promoted_assets[0].relative_archive_path
        assert "\\" not in rel_path
        assert rel_path == "douyin/rel_test/rel_test.mp4"


# =============================================================================
# 2. Manifest & Security Tests
# =============================================================================


class TestManifestAndSecurity:
    def test_manifest_schema_and_creation(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """asset_manifest.json is created atomically with valid schema v1.0 and provenance."""
        sandbox, archive_root = archive_env
        vid_file = sandbox / "output" / "manifest_test.mp4"
        vid_file.write_bytes(b"manifest payload")

        norm = make_artifact(vid_file)

        res = promoter.promote(
            normalized_assets=[norm],
            validation_result=ValidationResult(passed=True, ffprobe_verified=True, decode_smoke_verified=True),
            platform="douyin",
            platform_content_id="manifest_test",
            task_id="task_dl_100",
            execution_id="exec_uuid_200",
            source_sync_run_id="run_300",
            scope_id="douyin:test_user",
            sandbox_root=sandbox,
        )

        manifest_path = res.manifest_path
        assert manifest_path.exists()
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))

        assert manifest_data["schema_version"] == "1.0"
        assert manifest_data["platform"] == "douyin"
        assert manifest_data["platform_content_id"] == "manifest_test"
        assert "archived_at" in manifest_data
        assert manifest_data["source_provenance"]["task_id"] == "task_dl_100"
        assert manifest_data["source_provenance"]["execution_id"] == "exec_uuid_200"
        assert manifest_data["source_provenance"]["scope_id"] == "douyin:test_user"
        assert len(manifest_data["assets"]) == 1
        assert manifest_data["assets"][0]["file_name"] == "manifest_test.mp4"

    def test_manifest_no_secrets(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """Manifest rigorously scrubs sensitive cookies, session tokens, and keys."""
        sandbox, _ = archive_env
        vid_file = sandbox / "output" / "secret_test.mp4"
        vid_file.write_bytes(b"data")

        norm = make_artifact(vid_file)

        res = promoter.promote(
            normalized_assets=[norm],
            validation_result=ValidationResult(passed=True),
            platform="douyin",
            platform_content_id="secret_test",
            task_id="task_123?sessionid=leak_session_token_123",
            execution_id="exec_456?bearer=leak_bearer_token_456",
            source_sync_run_id="run_789?msToken=leak_ms_token",
            sandbox_root=sandbox,
        )

        raw_manifest = res.manifest_path.read_text(encoding="utf-8")
        assert "leak_session_token_123" not in raw_manifest
        assert "leak_bearer_token_456" not in raw_manifest
        assert "leak_ms_token" not in raw_manifest
        assert "[REDACTED]" in raw_manifest

    def test_manifest_only_after_assets(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """Manifest only exists after media files are promoted successfully."""
        sandbox, archive_root = archive_env
        vid_file = sandbox / "output" / "order_test.mp4"
        vid_file.write_bytes(b"data")

        norm = make_artifact(vid_file)

        target_dir = archive_root / "douyin" / "order_test"
        manifest_path = target_dir / "asset_manifest.json"

        # Before promotion
        assert not manifest_path.exists()

        res = promoter.promote(
            normalized_assets=[norm],
            validation_result=ValidationResult(passed=True),
            platform="douyin",
            platform_content_id="order_test",
            sandbox_root=sandbox,
        )

        assert (target_dir / "order_test.mp4").exists()
        assert res.manifest_path.exists()


# =============================================================================
# 3. Precondition & Validation Binding Tests
# =============================================================================


class TestPreconditionsAndValidationBinding:
    def test_unvalidated_rejected(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """When require_validation=True, promotion without ValidationResult is rejected."""
        sandbox, _ = archive_env
        vid_file = sandbox / "output" / "unval.mp4"
        vid_file.write_bytes(b"data")

        norm = make_artifact(vid_file)

        with pytest.raises(PromotionPreconditionError) as exc_info:
            promoter.promote(normalized_assets=[norm], validation_result=None, platform_content_id="unval")
        assert exc_info.value.reason == "UNVALIDATED_ASSET"

    def test_validation_failed_rejected(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """If validation_result.passed is False, promotion is rejected unconditionally."""
        sandbox, _ = archive_env
        vid_file = sandbox / "output" / "failed_val.mp4"
        vid_file.write_bytes(b"data")

        norm = make_artifact(vid_file)
        val = ValidationResult(passed=False, error="structural container decode failed")

        with pytest.raises(PromotionPreconditionError) as exc_info:
            promoter.promote(normalized_assets=[norm], validation_result=val, platform_content_id="failed_val")
        assert exc_info.value.reason == "VALIDATION_FAILED"

    def test_file_changed_after_validation_rejected(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """TOCTOU guard: if file size changes between validation and promotion, reject."""
        sandbox, _ = archive_env
        vid_file = sandbox / "output" / "mutated.mp4"
        vid_file.write_bytes(b"original data 10 bytes")

        norm = make_artifact(vid_file, byte_size=22)

        # Mutate file on disk before promotion
        vid_file.write_bytes(b"tampered new content that has a different length!")

        with pytest.raises(PromotionPreconditionError) as exc_info:
            promoter.promote(
                normalized_assets=[norm],
                validation_result=ValidationResult(passed=True),
                platform_content_id="mutated",
                sandbox_root=sandbox,
            )
        assert exc_info.value.reason == "FILE_MUTATED_POST_VALIDATION"

    def test_source_containment_rejected(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path], tmp_path: Path) -> None:
        """Candidate file residing outside assigned sandbox root is rejected with PromotionContainmentError."""
        sandbox, _ = archive_env
        outside_file = tmp_path / "outside_sandbox" / "evil.mp4"
        outside_file.parent.mkdir(parents=True, exist_ok=True)
        outside_file.write_bytes(b"evil data")

        norm = make_artifact(outside_file)

        with pytest.raises(PromotionContainmentError) as exc_info:
            promoter.promote(
                normalized_assets=[norm],
                validation_result=ValidationResult(passed=True),
                platform_content_id="evil",
                sandbox_root=sandbox,
            )
        assert exc_info.value.reason == "SANDBOX_CONTAINMENT_VIOLATION"

    def test_temporary_extension_rejected(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """Temporary files (.part, .tmp, etc.) are strictly rejected by the promotion firewall."""
        sandbox, _ = archive_env
        part_file = sandbox / "output" / "video.mp4.part"
        part_file.write_bytes(b"partial bytes")

        norm = make_artifact(part_file)

        with pytest.raises(InvalidPromotionArtifactError) as exc_info:
            promoter.promote(
                normalized_assets=[norm],
                validation_result=ValidationResult(passed=True),
                platform_content_id="part_test",
                sandbox_root=sandbox,
            )
        assert exc_info.value.reason == "TEMPORARY_FILE_EXTENSION"

    def test_empty_asset_list_rejected(self, promoter: ProductionArchivePromoter) -> None:
        """Attempting to promote an empty list of assets raises PromotionPreconditionError."""
        with pytest.raises(PromotionPreconditionError) as exc_info:
            promoter.promote(normalized_assets=[], validation_result=ValidationResult(passed=True))
        assert exc_info.value.reason == "EMPTY_ASSET_LIST"


# =============================================================================
# 4. Idempotency & Conflict Defenses
# =============================================================================


class TestIdempotencyAndConflict:
    def test_idempotent_existing(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """Target already exists with identical SHA-256 returns IDEMPOTENT_EXISTING without rewriting."""
        sandbox, archive_root = archive_env
        data = b"stable identical content for idempotency"
        vid_file = sandbox / "output" / "idem.mp4"
        vid_file.write_bytes(data)

        norm = make_artifact(vid_file)

        # Run 1: initial promotion
        res1 = promoter.promote(
            normalized_assets=[norm],
            validation_result=ValidationResult(passed=True),
            platform_content_id="idem",
            sandbox_root=sandbox,
        )
        assert res1.status == PromotionStatus.SUCCESS
        assert res1.idempotent_existing is False

        target_file = archive_root / "douyin" / "idem" / "idem.mp4"
        mtime1 = target_file.stat().st_mtime_ns

        # Run 2: second promotion of identical content
        res2 = promoter.promote(
            normalized_assets=[norm],
            validation_result=ValidationResult(passed=True),
            platform_content_id="idem",
            sandbox_root=sandbox,
        )
        assert res2.status == PromotionStatus.IDEMPOTENT_EXISTING
        assert res2.idempotent_existing is True
        mtime2 = target_file.stat().st_mtime_ns

        # File was untouched (not re-written)
        assert mtime1 == mtime2

    def test_conflicting_existing_rejected(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """Target already exists with differing bytes raises ArchiveConflictError without overwriting."""
        sandbox, archive_root = archive_env
        vid_file = sandbox / "output" / "conflict.mp4"
        vid_file.write_bytes(b"new incoming video content")

        norm = make_artifact(vid_file)

        # Pre-seed existing target directory with differing content
        target_dir = archive_root / "douyin" / "conflict"
        target_dir.mkdir(parents=True, exist_ok=True)
        existing_target = target_dir / "conflict.mp4"
        existing_target.write_bytes(b"pre-existing completely different video content")

        with pytest.raises(ArchiveConflictError) as exc_info:
            promoter.promote(
                normalized_assets=[norm],
                validation_result=ValidationResult(passed=True),
                platform_content_id="conflict",
                sandbox_root=sandbox,
            )
        assert exc_info.value.reason == "DESTINATION_COLLISION_DIFFERENT_CONTENT"

        # Pre-existing file content was preserved (never overwritten)
        assert existing_target.read_bytes() == b"pre-existing completely different video content"


# =============================================================================
# 5. Staging, Corruption & Cross-Filesystem Tests
# =============================================================================


class TestStagingCorruptionAndCrossFs:
    def test_copy_corruption_detected(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """If destination file bytes are tampered/corrupted during transfer, reject with PromotionCorruptionError."""
        sandbox, archive_root = archive_env
        vid_file = sandbox / "output" / "corrupt_tx.mp4"
        vid_file.write_bytes(b"clean uncorrupted source media bytes")

        norm = make_artifact(vid_file)

        real_copy = shutil.copyfileobj

        def corrupt_copy(fsrc, fdst, length=128 * 1024):
            real_copy(fsrc, fdst, length)
            fdst.write(b"_corrupt_extra_tamper_byte")

        with patch("shutil.copyfileobj", side_effect=corrupt_copy):
            with pytest.raises(PromotionCorruptionError) as exc_info:
                promoter.promote(
                    normalized_assets=[norm],
                    validation_result=ValidationResult(passed=True),
                    platform_content_id="corrupt_tx",
                    sandbox_root=sandbox,
                )
            assert exc_info.value.reason == "TRANSFER_CORRUPTION"

        # Final destination does NOT exist
        final_dest = archive_root / "douyin" / "corrupt_tx" / "corrupt_tx.mp4"
        assert not final_dest.exists()

    def test_copy_failure_cleanup(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """IO error during transfer aborts transaction and removes staging directory."""
        sandbox, archive_root = archive_env
        vid_file = sandbox / "output" / "io_fail.mp4"
        vid_file.write_bytes(b"data")

        norm = make_artifact(vid_file)

        with patch("shutil.copyfileobj", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            with pytest.raises(OSError):
                promoter.promote(
                    normalized_assets=[norm],
                    validation_result=ValidationResult(passed=True),
                    platform_content_id="io_fail",
                    sandbox_root=sandbox,
                )

        # Staging directory is cleaned up
        staging_root = archive_root / "douyin" / ".staging"
        if staging_root.exists():
            assert len(list(staging_root.iterdir())) == 0

    def test_cross_filesystem_fallback(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """When os.replace between directories/mounts raises EXDEV, fallback safely publishes via temp file."""
        sandbox, archive_root = archive_env
        vid_file = sandbox / "output" / "cross_fs.mp4"
        vid_file.write_bytes(b"cross filesystem data")

        norm = make_artifact(vid_file)

        original_replace = os.replace

        def mock_replace(src, dst):
            if "cross_fs.mp4" in str(src) and ".promoting" not in str(src):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return original_replace(src, dst)

        with patch("os.replace", side_effect=mock_replace):
            res = promoter.promote(
                normalized_assets=[norm],
                validation_result=ValidationResult(passed=True),
                platform_content_id="cross_fs",
                sandbox_root=sandbox,
            )

        assert res.status == PromotionStatus.SUCCESS
        dest_file = archive_root / "douyin" / "cross_fs" / "cross_fs.mp4"
        assert dest_file.exists()
        assert dest_file.read_bytes() == b"cross filesystem data"


# =============================================================================
# 6. Multi-Asset Transaction & Album Tests
# =============================================================================


class TestMultiAssetTransactions:
    def test_image_album_promotion(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """Multi-image album with 3 images and BGM is promoted transactionally."""
        sandbox, archive_root = archive_env
        img1 = sandbox / "output" / "album_img_001.webp"
        img2 = sandbox / "output" / "album_img_002.webp"
        img3 = sandbox / "output" / "album_img_003.webp"
        bgm = sandbox / "output" / "album_bgm.mp3"

        img1.write_bytes(b"image 1 bytes")
        img2.write_bytes(b"image 2 bytes")
        img3.write_bytes(b"image 3 bytes")
        bgm.write_bytes(b"bgm audio bytes")

        norms = [
            make_artifact(img1, role=ArtifactRole.ALBUM_IMAGE, sequence_index=1, media_kind="image"),
            make_artifact(img2, role=ArtifactRole.ALBUM_IMAGE, sequence_index=2, media_kind="image"),
            make_artifact(img3, role=ArtifactRole.ALBUM_IMAGE, sequence_index=3, media_kind="image"),
            make_artifact(bgm, role=ArtifactRole.BGM_AUDIO, media_kind="audio"),
        ]

        res = promoter.promote(
            normalized_assets=norms,
            validation_result=ValidationResult(passed=True),
            platform="douyin",
            platform_content_id="album_content_123",
            sandbox_root=sandbox,
        )

        assert res.status == PromotionStatus.SUCCESS
        assert len(res.promoted_assets) == 4

        dest_dir = archive_root / "douyin" / "album_content_123"
        assert (dest_dir / "album_img_001.webp").exists()
        assert (dest_dir / "album_img_002.webp").exists()
        assert (dest_dir / "album_img_003.webp").exists()
        assert (dest_dir / "album_bgm.mp3").exists()
        assert (dest_dir / "asset_manifest.json").exists()

    def test_partial_album_failure_aborts_all(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """If promotion of 3rd image in an album fails, entire transaction is aborted with zero published files."""
        sandbox, archive_root = archive_env
        img1 = sandbox / "output" / "part_album_001.webp"
        img2 = sandbox / "output" / "part_album_002.webp"
        img3 = sandbox / "output" / "part_album_003.webp"

        img1.write_bytes(b"img1")
        img2.write_bytes(b"img2")
        img3.write_bytes(b"img3")

        norms = [
            make_artifact(img1, role=ArtifactRole.ALBUM_IMAGE, sequence_index=1, media_kind="image"),
            make_artifact(img2, role=ArtifactRole.ALBUM_IMAGE, sequence_index=2, media_kind="image"),
            make_artifact(img3, role=ArtifactRole.ALBUM_IMAGE, sequence_index=3, media_kind="image"),
        ]

        real_copy = shutil.copyfileobj

        def fail_on_img3(fsrc, fdst, length=128 * 1024):
            if "part_album_003.webp" in getattr(fdst, "name", ""):
                raise OSError(errno.EIO, "Simulated disk write error on image 3")
            return real_copy(fsrc, fdst, length)

        with patch("shutil.copyfileobj", side_effect=fail_on_img3):
            with pytest.raises(OSError):
                promoter.promote(
                    normalized_assets=norms,
                    validation_result=ValidationResult(passed=True),
                    platform="douyin",
                    platform_content_id="part_album_tx",
                    sandbox_root=sandbox,
                )

        dest_dir = archive_root / "douyin" / "part_album_tx"
        if dest_dir.exists():
            assert not (dest_dir / "asset_manifest.json").exists()
            assert not (dest_dir / "part_album_001.webp").exists()

    def test_cover_auxiliary_promotion(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """Cover image auxiliary artifact is cleanly promoted."""
        sandbox, archive_root = archive_env
        cover_file = sandbox / "output" / "vid_cover.webp"
        cover_file.write_bytes(b"cover image bytes")

        norm = make_artifact(cover_file, role=ArtifactRole.COVER_IMAGE, media_kind="image")

        res = promoter.promote(
            normalized_assets=[norm],
            validation_result=ValidationResult(passed=True),
            platform="douyin",
            platform_content_id="cover_post",
            sandbox_root=sandbox,
        )

        assert "COVER_IMAGE" in res.promoted_assets[0].role
        assert (archive_root / "douyin" / "cover_post" / "vid_cover.webp").exists()


# =============================================================================
# 7. Path Budget & Traversal Defenses
# =============================================================================


class TestPathBudgetAndTraversal:
    def test_deep_archive_root_rejected_by_budget(self, tmp_path: Path) -> None:
        """Excessively deep archive path exceeding max_path_budget raises ArchiveBudgetExceededError."""
        deep_archive = tmp_path / "deep_1" / "deep_2"
        deep_archive.mkdir(parents=True, exist_ok=True)

        strict_promoter = ProductionArchivePromoter(
            archive_root=deep_archive,
            path_budget=FinalArchivePathBudget(max_path_budget=60),
        )

        fake_file = tmp_path / "vid.mp4"
        fake_file.write_bytes(b"data")
        norm = make_artifact(fake_file)

        with pytest.raises(ArchiveBudgetExceededError) as exc_info:
            strict_promoter.promote(
                normalized_assets=[norm],
                validation_result=ValidationResult(passed=True),
                platform_content_id="7671141177986518318",
            )
        assert exc_info.value.reason == "ARCHIVE_PATH_BUDGET_EXCEEDED"

    def test_no_final_archive_traversal(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """Path traversal sequences (..) in target directory raise PromotionContainmentError."""
        sandbox, archive_root = archive_env
        vid = sandbox / "output" / "vid.mp4"
        vid.write_bytes(b"data")
        norm = make_artifact(vid)

        with pytest.raises(PromotionContainmentError) as exc_info:
            promoter.promote(
                normalized_assets=[norm],
                validation_result=ValidationResult(passed=True),
                target_directory=Path("../../evil_escape"),
                platform_content_id="traversal",
            )
        assert exc_info.value.reason == "ARCHIVE_PATH_ESCAPE"

    def test_source_files_not_deleted_by_promoter(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """Promoter strictly leaves sandbox source files intact (D04 owns sandbox GC)."""
        sandbox, _ = archive_env
        vid_file = sandbox / "output" / "keep_source.mp4"
        vid_file.write_bytes(b"source data to remain in sandbox")

        norm = make_artifact(vid_file)

        res = promoter.promote(
            normalized_assets=[norm],
            validation_result=ValidationResult(passed=True),
            platform_content_id="keep_source",
            sandbox_root=sandbox,
        )

        assert res.status == PromotionStatus.SUCCESS
        assert vid_file.exists()
        assert vid_file.read_bytes() == b"source data to remain in sandbox"


# =============================================================================
# 8. Real Media Fixture Tests & Subsystem Integration
# =============================================================================


class TestRealMediaAndSubsystemIntegration:
    def test_video_asset_promotion_real_h264(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """Real H.264 video fixture promotion with SHA-256 and byte invariance."""
        fixture_src = FIXTURE_DIR / "6611417973221494020.mp4"
        if not fixture_src.exists():
            pytest.skip("Fixture not found")

        sandbox, archive_root = archive_env
        dest = sandbox / "output" / "6611417973221494020.mp4"
        shutil.copy2(fixture_src, dest)

        orig_hash = hashlib.sha256(dest.read_bytes()).hexdigest()
        orig_size = dest.stat().st_size

        norm = make_artifact(dest)
        val = ValidationResult(passed=True, ffprobe_verified=True, decode_smoke_verified=True)

        res = promoter.promote(
            normalized_assets=[norm],
            validation_result=val,
            platform="douyin",
            platform_content_id="6611417973221494020",
            sandbox_root=sandbox,
        )

        assert res.status == PromotionStatus.SUCCESS
        promoted = res.promoted_assets[0]
        assert promoted.sha256 == orig_hash
        assert promoted.byte_size == orig_size
        assert promoted.absolute_archive_path.exists()

    def test_hevc_asset_promotion_real(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """Real HEVC video fixture promotion."""
        fixture_src = FIXTURE_DIR / "7672935264565710131.mp4"
        if not fixture_src.exists():
            pytest.skip("Fixture not found")

        sandbox, archive_root = archive_env
        dest = sandbox / "output" / "7672935264565710131.mp4"
        shutil.copy2(fixture_src, dest)

        orig_hash = hashlib.sha256(dest.read_bytes()).hexdigest()
        orig_size = dest.stat().st_size

        norm = make_artifact(dest)

        res = promoter.promote(
            normalized_assets=[norm],
            validation_result=ValidationResult(passed=True),
            platform="douyin",
            platform_content_id="7672935264565710131",
            sandbox_root=sandbox,
        )

        assert res.promoted_assets[0].sha256 == orig_hash
        assert res.promoted_assets[0].byte_size == orig_size

    def test_d01_integration_full_chain(self, tmp_path: Path) -> None:
        """Full pipeline: D04 Sandbox -> plan_output -> FakeBackend -> normalize -> validate_assets -> promote -> SUCCESS."""
        sandbox_provider = ProductionTaskSandboxProvider(base_dir=tmp_path / "sandboxes")
        normalizer = ProductionAssetNormalizer()
        validator = FakeMediaValidator(should_pass=True)
        archive_root = tmp_path / "full_chain_archive"
        promoter = ProductionArchivePromoter(archive_root=archive_root)

        router = FakeContentRouter(base_archive_dir=archive_root)
        downloader = SafeDouyinDownloader(
            sandbox_provider=sandbox_provider,
            normalizer=normalizer,
            validator=validator,
            promoter=promoter,
            router=router,
            require_auth=False,
        )

        task = DownloadTask(
            task_id="task_full_chain_001",
            platform="douyin",
            scope_id="douyin:test",
            platform_content_id="7671141177986518318",
            source_url="https://www.douyin.com/video/7671141177986518318",
            content_type="video",
            priority=DownloadPriority.NEW_COLLECTION,
            reason=DownloadReason.NEW_COLLECTION_ITEM,
            attempt_policy={},
        )

        result = downloader.execute(task)

        target_dir = router.resolve_target_directory(task.platform, task.scope_id, task.platform_content_id)
        assert (target_dir / "7671141177986518318.mp4").exists()
        assert (target_dir / "asset_manifest.json").exists()

    def test_stale_staging_cleanup_safety(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """cleanup_stale_staging removes old orphan staging dirs without touching recent ones."""
        _, archive_root = archive_env
        staging_root = archive_root / "douyin" / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True)

        old_dir = staging_root / "stale_123"
        old_dir.mkdir(parents=True, exist_ok=True)
        recent_dir = staging_root / "recent_456"
        recent_dir.mkdir(parents=True, exist_ok=True)

        import time
        # Stale directory with dead PID and expired TTL
        (staging_root / ".meta_stale_123.json").write_text(
            json.dumps({"owner_pid": 99999999, "created_at": time.time() - (2 * 86400)}),
            encoding="utf-8",
        )
        # Recent directory with active PID within TTL
        (staging_root / ".meta_recent_456.json").write_text(
            json.dumps({"owner_pid": os.getpid(), "created_at": time.time()}),
            encoding="utf-8",
        )

        cleaned = promoter.cleanup_stale_staging(platform="douyin", max_age_seconds=86400)
        assert cleaned == 1
        assert not old_dir.exists()
        assert recent_dir.exists()

    def test_generic_platform_namespace(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """Promoter correctly namespaces generic platforms (bilibili, youtube)."""
        sandbox, archive_root = archive_env
        vid_file = sandbox / "output" / "BV1xx411c7mD.mp4"
        vid_file.write_bytes(b"bilibili video data")

        norm = make_artifact(vid_file)

        res = promoter.promote(
            normalized_assets=[norm],
            validation_result=ValidationResult(passed=True),
            platform="bilibili",
            platform_content_id="BV1xx411c7mD",
            sandbox_root=sandbox,
        )

        expected_file = archive_root / "bilibili" / "BV1xx411c7mD" / "BV1xx411c7mD.mp4"
        assert expected_file.exists()
        assert res.promoted_assets[0].relative_archive_path == "bilibili/BV1xx411c7mD/BV1xx411c7mD.mp4"


# =============================================================================
# 9. Invariance, Atomicity & Integration Matrix (D07 Comprehensive)
# =============================================================================


class TestPromoterInvariantsAndAtomicity:
    def test_temp_copy_before_final_publish(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """Destination final path never sees partial bytes; file exists in staging first."""
        sandbox, archive_root = archive_env
        vid_file = sandbox / "output" / "partial_test.mp4"
        vid_file.write_bytes(b"complete media content payload")

        norm = make_artifact(vid_file)
        target_final = archive_root / "douyin" / "partial_test" / "partial_test.mp4"

        # Hook into safe_copy_and_sync: while copying, final file does NOT exist
        def assert_final_absent_during_copy(src, dst):
            assert not target_final.exists(), "Final file must not exist while staging copy is ongoing"
            shutil.copyfileobj(open(src, "rb"), open(dst, "wb"))

        with patch("src.downloader.promoter.safe_copy_and_sync", side_effect=assert_final_absent_during_copy):
            res = promoter.promote(
                normalized_assets=[norm],
                validation_result=ValidationResult(passed=True),
                platform_content_id="partial_test",
                sandbox_root=sandbox,
            )

        assert res.status == PromotionStatus.SUCCESS
        assert target_final.exists()

    def test_atomic_final_visibility(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """Final content directory appears atomically via single os.replace."""
        sandbox, archive_root = archive_env
        vid_file = sandbox / "output" / "atomic_vis.mp4"
        vid_file.write_bytes(b"atomic content")
        norm = make_artifact(vid_file)

        replace_called = False
        real_replace = os.replace

        def track_replace(src, dst):
            nonlocal replace_called
            if "atomic_vis" in str(dst) and not str(src).endswith(".tmp"):
                replace_called = True
            return real_replace(src, dst)

        with patch("os.replace", side_effect=track_replace):
            res = promoter.promote(
                normalized_assets=[norm],
                validation_result=ValidationResult(passed=True),
                platform_content_id="atomic_vis",
                sandbox_root=sandbox,
            )

        assert res.status == PromotionStatus.SUCCESS
        assert replace_called is True
        assert (archive_root / "douyin" / "atomic_vis" / "atomic_vis.mp4").exists()

    def test_same_filesystem_promotion(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """When sandbox and archive are on the same filesystem/drive, promotion executes cleanly."""
        sandbox, archive_root = archive_env
        vid_file = sandbox / "output" / "same_fs.mp4"
        vid_file.write_bytes(b"same filesystem bytes")
        norm = make_artifact(vid_file)

        res = promoter.promote(
            normalized_assets=[norm],
            validation_result=ValidationResult(passed=True),
            platform_content_id="same_fs",
            sandbox_root=sandbox,
        )
        assert res.status == PromotionStatus.SUCCESS
        assert (archive_root / "douyin" / "same_fs" / "same_fs.mp4").exists()

    def test_final_replace_failure_cleanup(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """If final replace fails, transaction is aborted and staging is cleaned up."""
        sandbox, archive_root = archive_env
        vid_file = sandbox / "output" / "replace_fail.mp4"
        vid_file.write_bytes(b"replace fail bytes")
        norm = make_artifact(vid_file)

        real_replace = os.replace

        def mock_failing_replace(src, dst):
            if "replace_fail" in str(dst) and not str(src).endswith(".tmp"):
                raise PermissionError("Access denied on final replace destination")
            return real_replace(src, dst)

        with patch("os.replace", side_effect=mock_failing_replace):
            with pytest.raises(PermissionError):
                promoter.promote(
                    normalized_assets=[norm],
                    validation_result=ValidationResult(passed=True),
                    platform_content_id="replace_fail",
                    sandbox_root=sandbox,
                )

        target_final = archive_root / "douyin" / "replace_fail" / "replace_fail.mp4"
        assert not target_final.exists()

    def test_manifest_atomic_write(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """asset_manifest.json is written to temp file first then committed atomically."""
        sandbox, archive_root = archive_env
        vid_file = sandbox / "output" / "manifest_atomic.mp4"
        vid_file.write_bytes(b"data")
        norm = make_artifact(vid_file)

        manifest_temp_observed = False
        real_replace = os.replace

        def check_manifest_temp(src, dst):
            nonlocal manifest_temp_observed
            if "asset_manifest.json" in str(dst) and ".tmp" in str(src):
                manifest_temp_observed = True
            return real_replace(src, dst)

        with patch("os.replace", side_effect=check_manifest_temp):
            res = promoter.promote(
                normalized_assets=[norm],
                validation_result=ValidationResult(passed=True),
                platform_content_id="manifest_atomic",
                sandbox_root=sandbox,
            )

        assert res.status == PromotionStatus.SUCCESS
        assert manifest_temp_observed is True
        assert res.manifest_path.exists()

    def test_bgm_auxiliary_promotion(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """BGM audio auxiliary artifact is promoted with BGM_AUDIO role."""
        sandbox, archive_root = archive_env
        bgm_file = sandbox / "output" / "vid_bgm.mp3"
        bgm_file.write_bytes(b"mp3 audio bitstream data")

        norm = make_artifact(bgm_file, role=ArtifactRole.BGM_AUDIO, media_kind="audio")

        res = promoter.promote(
            normalized_assets=[norm],
            validation_result=ValidationResult(passed=True),
            platform="douyin",
            platform_content_id="bgm_post",
            sandbox_root=sandbox,
        )

        assert res.status == PromotionStatus.SUCCESS
        assert "BGM_AUDIO" in res.promoted_assets[0].role
        assert (archive_root / "douyin" / "bgm_post" / "vid_bgm.mp3").exists()

    def test_path_budget_normal_success(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """Normal path budget checks pass without raising errors."""
        budget = FinalArchivePathBudget(max_path_budget=240, max_component_budget=64)
        valid_path = Path("G:/archive/douyin/7671141177986518318/7671141177986518318.mp4")
        budget.validate_path(valid_path, extra_buffer_len=30)

    def test_title_ignored_in_promotion(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """Titles, nicknames, and emoji descriptions in metadata have zero impact on canonical archive paths."""
        sandbox, archive_root = archive_env
        vid_file = sandbox / "output" / "ignore_title.mp4"
        vid_file.write_bytes(b"video bytes")

        norm = make_artifact(vid_file)
        # Attach extreme metadata
        norm.metadata["title"] = "超长500字标题" * 50 + "🔥🎉🚀"
        norm.metadata["nickname"] = '非法字符<>:"/\\\\|?*'

        res = promoter.promote(
            normalized_assets=[norm],
            validation_result=ValidationResult(passed=True),
            platform_content_id="ignore_title",
            sandbox_root=sandbox,
        )

        assert res.status == PromotionStatus.SUCCESS
        # Canonical file name remains pure platform_content_id
        promoted_file = res.promoted_assets[0].absolute_archive_path
        assert promoted_file.name == "ignore_title.mp4"
        assert "超长" not in str(promoted_file)

    def test_sandbox_cleanup_still_belongs_to_d04(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """Promoter does not clean up sandbox; sandbox directories remain intact for D04."""
        sandbox, _ = archive_env
        vid_file = sandbox / "output" / "d04_gc.mp4"
        vid_file.write_bytes(b"content")

        norm = make_artifact(vid_file)

        promoter.promote(
            normalized_assets=[norm],
            validation_result=ValidationResult(passed=True),
            platform_content_id="d04_gc",
            sandbox_root=sandbox,
        )

        # Output folder and source file are completely preserved
        assert (sandbox / "output").exists()
        assert vid_file.exists()

    def test_download_result_success_invariant(self, tmp_path: Path) -> None:
        """SafeDouyinDownloader returns SUCCESS ONLY when promoter succeeds. If promoter fails, result is FAILED."""
        sandbox_provider = ProductionTaskSandboxProvider(base_dir=tmp_path / "sb")
        normalizer = ProductionAssetNormalizer()
        validator = FakeMediaValidator(should_pass=True)
        archive_root = tmp_path / "fail_archive"

        # Mock promoter that simulates failure
        failing_promoter = MagicMock(spec=ProductionArchivePromoter)
        failing_promoter.promote.side_effect = PromotionError("Disk write failed during promotion", reason="PROMOTION_FAILED")

        downloader = SafeDouyinDownloader(
            sandbox_provider=sandbox_provider,
            normalizer=normalizer,
            validator=validator,
            promoter=failing_promoter,
            require_auth=False,
        )

        task = DownloadTask(
            task_id="task_fail_prom_001",
            platform="douyin",
            scope_id="douyin:test",
            platform_content_id="7671141177986518318",
            source_url="https://www.douyin.com/video/7671141177986518318",
            content_type="video",
            priority=DownloadPriority.NEW_COLLECTION,
            reason=DownloadReason.NEW_COLLECTION_ITEM,
            attempt_policy={},
        )

        result = downloader.execute(task)

        # Status must be FAILED, stage PROMOTING
        assert result.status == DownloaderStatus.FAILED
        assert result.stage == ExecutionStage.PROMOTING
        assert "promotion" in result.message.lower()

    def test_restart_repromotion_idempotency(self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]) -> None:
        """Restarting promotion after complete archive exists returns IDEMPOTENT_EXISTING without altering manifest."""
        sandbox, archive_root = archive_env
        vid_file = sandbox / "output" / "restart_idem.mp4"
        vid_file.write_bytes(b"durable media file")
        norm = make_artifact(vid_file)

        # Run 1
        res1 = promoter.promote(
            normalized_assets=[norm],
            validation_result=ValidationResult(passed=True),
            platform_content_id="restart_idem",
            sandbox_root=sandbox,
        )
        assert res1.status == PromotionStatus.SUCCESS
        orig_manifest_text = res1.manifest_path.read_text(encoding="utf-8")

        # Run 2 (worker reboot/restart simulation)
        res2 = promoter.promote(
            normalized_assets=[norm],
            validation_result=ValidationResult(passed=True),
            platform_content_id="restart_idem",
            sandbox_root=sandbox,
        )
        assert res2.status == PromotionStatus.IDEMPOTENT_EXISTING
        assert res2.idempotent_existing is True
        # Manifest text identical
        assert res2.manifest_path.read_text(encoding="utf-8") == orig_manifest_text


# =============================================================================
# 10. Formal Asset Publication Invariants (DY-D07 Strict Regressions)
# =============================================================================


class TestFormalAssetPublicationInvariants:
    def test_source_untouched_after_promotion(
        self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]
    ) -> None:
        """Requirement 1 & K: Sandbox source files remain completely untouched and identical after promotion."""
        sandbox, archive_root = archive_env
        vid_file = sandbox / "output" / "untouched.mp4"
        content_bytes = b"source file that must remain untouched in sandbox"
        vid_file.write_bytes(content_bytes)
        init_sha = compute_file_sha256(vid_file)
        init_stat = vid_file.stat()

        norm = make_artifact(vid_file)
        val = ValidationResult(passed=True, validated_sha256=init_sha)

        res = promoter.promote(
            normalized_assets=[norm],
            validation_result=val,
            platform_content_id="untouched_001",
            sandbox_root=sandbox,
        )

        assert res.status == PromotionStatus.SUCCESS
        # Source must still exist
        assert vid_file.exists(), "Source sandbox file must not be deleted by promoter"
        assert vid_file.read_bytes() == content_bytes, "Source sandbox file bytes must not be mutated"
        assert compute_file_sha256(vid_file) == init_sha, "Source sandbox file SHA-256 must match initial"
        assert vid_file.stat().st_size == init_stat.st_size, "Source sandbox size must not change"

    def test_d04_owns_final_cleanup(
        self, tmp_path: Path
    ) -> None:
        """Requirement 2 & K: D04 owns sandbox lifecycle; cleanup occurs through D04, not D07."""
        from src.downloader.sandbox import SandboxRetentionPolicy

        base_sb_dir = tmp_path / "sandboxes"
        sb_provider = ProductionTaskSandboxProvider(
            base_dir=base_sb_dir,
            retention_policy=SandboxRetentionPolicy.DELETE_ON_SUCCESS,
        )
        sandbox = sb_provider.create_sandbox("task_cleanup_test_001")

        vid_file = sandbox.path / "output" / "vid.mp4"
        vid_file.parent.mkdir(parents=True, exist_ok=True)
        vid_file.write_bytes(b"media for d04 cleanup test")
        src_sha = compute_file_sha256(vid_file)

        archive_dir = tmp_path / "archive"
        promoter = ProductionArchivePromoter(archive_root=archive_dir)

        norm = make_artifact(vid_file)
        val = ValidationResult(passed=True, validated_sha256=src_sha)

        res = promoter.promote(
            normalized_assets=[norm],
            validation_result=val,
            platform_content_id="d04_clean_001",
            sandbox_root=sandbox.path,
        )

        assert res.status == PromotionStatus.SUCCESS
        # Right after D07, sandbox still has the file
        assert vid_file.exists()
        assert sandbox.path.exists()

        # Now D04 finalizes success
        sandbox.finalize_success()
        sandbox.cleanup()

        # Now sandbox is cleaned up by D04 per retention policy
        assert not vid_file.exists()

    def test_validation_sha_binding(
        self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]
    ) -> None:
        """Requirement 3 & C: Cryptographic validation binding succeeds when hashes match."""
        sandbox, archive_root = archive_env
        vid_file = sandbox / "output" / "valid_bind.mp4"
        vid_file.write_bytes(b"binding bytes")
        valid_sha = compute_file_sha256(vid_file)

        norm = make_artifact(vid_file)
        val = ValidationResult(
            passed=True,
            validated_sha256=valid_sha,
            validated_artifacts=({
                "file_name": vid_file.name,
                "file_path": str(vid_file),
                "size_bytes": len(b"binding bytes"),
                "validated_sha256": valid_sha,
            },),
        )

        res = promoter.promote(
            normalized_assets=[norm],
            validation_result=val,
            platform_content_id="valid_bind_001",
            sandbox_root=sandbox,
        )
        assert res.status == PromotionStatus.SUCCESS
        assert (archive_root / "douyin" / "valid_bind_001" / "valid_bind.mp4").exists()

    def test_same_size_tamper_rejected(
        self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]
    ) -> None:
        """Requirement 4 & D (BLOCKER): Post-validation tamper with SAME file size is rejected by SHA binding."""
        sandbox, archive_root = archive_env
        vid_file = sandbox / "output" / "tamper_test.mp4"
        # 1. Valid video A, size = 16 bytes
        original_bytes = b"original valid A"
        vid_file.write_bytes(original_bytes)
        a_hash = compute_file_sha256(vid_file)

        norm = make_artifact(vid_file)
        # 2. Validation PASS with A_HASH
        val = ValidationResult(
            passed=True,
            validated_sha256=a_hash,
            validated_artifacts=({
                "file_name": vid_file.name,
                "file_path": str(vid_file),
                "size_bytes": len(original_bytes),
                "validated_sha256": a_hash,
            },),
        )

        # 3. Between D05 and D07: replace file with DIFFERENT bytes B, but forced same length (16 bytes)
        tampered_bytes = b"tampered byte B!"  # exactly 16 bytes
        assert len(tampered_bytes) == len(original_bytes)
        vid_file.write_bytes(tampered_bytes)
        b_hash = compute_file_sha256(vid_file)
        assert b_hash != a_hash

        # 4. D07 promotion MUST REJECT
        with pytest.raises(PromotionCorruptionError) as exc_info:
            promoter.promote(
                normalized_assets=[norm],
                validation_result=val,
                platform_content_id="tamper_001",
                sandbox_root=sandbox,
            )

        assert "VALIDATION_BINDING_MISMATCH" in str(exc_info.value)
        # Final asset must NOT exist
        final_dir = archive_root / "douyin" / "tamper_001"
        assert not final_dir.exists()

    def test_all_files_stage_before_publish(
        self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]
    ) -> None:
        """Requirement 5 & B: All files and manifest are staged in archive-local staging before directory rename."""
        sandbox, archive_root = archive_env
        img1 = sandbox / "output" / "album_001.webp"
        img2 = sandbox / "output" / "album_002.webp"
        bgm = sandbox / "output" / "album_bgm.mp3"
        img1.write_bytes(b"image 1 content")
        img2.write_bytes(b"image 2 content")
        bgm.write_bytes(b"audio bgm content")

        norm1 = make_artifact(img1, role=ArtifactRole.ALBUM_IMAGE, sequence_index=1, media_kind="image")
        norm2 = make_artifact(img2, role=ArtifactRole.ALBUM_IMAGE, sequence_index=2, media_kind="image")
        norm3 = make_artifact(bgm, role=ArtifactRole.BGM_AUDIO, media_kind="audio")

        target_dir = archive_root / "douyin" / "album_staged_first"
        observed_staged_content = False
        real_replace = os.replace

        def check_staging_before_replace(src, dst):
            nonlocal observed_staged_content
            # When directory replace is called to target_dir
            if Path(dst) == target_dir:
                src_path = Path(src)
                # Verify all 3 assets + manifest exist in staging directory right before rename
                if (src_path / "album_001.webp").exists() and \
                   (src_path / "album_002.webp").exists() and \
                   (src_path / "album_bgm.mp3").exists() and \
                   (src_path / "asset_manifest.json").exists():
                    observed_staged_content = True
            return real_replace(src, dst)

        with patch("os.replace", side_effect=check_staging_before_replace):
            res = promoter.promote(
                normalized_assets=[norm1, norm2, norm3],
                validation_result=ValidationResult(passed=True),
                platform_content_id="album_staged_first",
                target_directory=target_dir,
                sandbox_root=sandbox,
            )

        assert res.status == PromotionStatus.SUCCESS
        assert observed_staged_content is True
        assert (target_dir / "album_001.webp").exists()
        assert (target_dir / "album_002.webp").exists()
        assert (target_dir / "album_bgm.mp3").exists()
        assert (target_dir / "asset_manifest.json").exists()

    def test_partial_album_never_visible_final(
        self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]
    ) -> None:
        """Requirement 6 & L: Failure at 3rd item of 3-images + BGM leaves zero partial files in archive."""
        sandbox, archive_root = archive_env
        img1 = sandbox / "output" / "fail_001.webp"
        img2 = sandbox / "output" / "fail_002.webp"
        img3 = sandbox / "output" / "fail_003.webp"
        bgm = sandbox / "output" / "fail_bgm.mp3"
        img1.write_bytes(b"img1")
        img2.write_bytes(b"img2")
        img3.write_bytes(b"img3")
        bgm.write_bytes(b"bgm")

        norm1 = make_artifact(img1, role=ArtifactRole.ALBUM_IMAGE, sequence_index=1, media_kind="image")
        norm2 = make_artifact(img2, role=ArtifactRole.ALBUM_IMAGE, sequence_index=2, media_kind="image")
        norm3 = make_artifact(img3, role=ArtifactRole.ALBUM_IMAGE, sequence_index=3, media_kind="image")
        norm4 = make_artifact(bgm, role=ArtifactRole.BGM_AUDIO, media_kind="audio")

        target_dir = archive_root / "douyin" / "partial_album_test"

        # Inject failure during copy of 3rd item
        from src.downloader.promoter import safe_copy_and_sync
        real_sync = safe_copy_and_sync

        def failing_copy(src, dst):
            if "fail_003.webp" in str(dst):
                raise IOError("Simulated disk full or I/O failure during 3rd item copy")
            real_sync(src, dst)

        with patch("src.downloader.promoter.safe_copy_and_sync", side_effect=failing_copy):
            with pytest.raises(IOError):
                promoter.promote(
                    normalized_assets=[norm1, norm2, norm3, norm4],
                    validation_result=ValidationResult(passed=True),
                    platform_content_id="partial_album_test",
                    target_directory=target_dir,
                    sandbox_root=sandbox,
                )

        # 1. Final content directory does NOT exist
        assert not target_dir.exists(), "Final content directory must not exist after failure"
        # 2. Manifest does NOT exist
        assert not (target_dir / "asset_manifest.json").exists()
        # 3. Sandbox source files: all 4 still exist completely intact!
        assert img1.exists()
        assert img2.exists()
        assert img3.exists()
        assert bgm.exists()

    def test_crash_before_directory_publish(
        self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]
    ) -> None:
        """Requirement 7 & H Case 2: Crash after staging is ready but before directory rename leaves no formal asset."""
        sandbox, archive_root = archive_env
        vid_file = sandbox / "output" / "crash_test.mp4"
        vid_file.write_bytes(b"crash test bytes")
        norm = make_artifact(vid_file)

        target_dir = archive_root / "douyin" / "crash_test"
        real_replace = os.replace

        def crash_before_rename(src, dst):
            if Path(dst) == target_dir:
                raise RuntimeError("Simulated process crash or power interruption before directory rename")
            return real_replace(src, dst)

        with patch("os.replace", side_effect=crash_before_rename):
            with pytest.raises(RuntimeError):
                promoter.promote(
                    normalized_assets=[norm],
                    validation_result=ValidationResult(passed=True),
                    platform_content_id="crash_test",
                    target_directory=target_dir,
                    sandbox_root=sandbox,
                )

        assert not target_dir.exists(), "Final directory must not exist if crash occurs before publish"

    def test_crash_after_publish_retry_idempotent(
        self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]
    ) -> None:
        """Requirement 8 & H Case 3: Crash after directory publish; retry with same task succeeds via EXISTING_IDENTICAL."""
        sandbox, archive_root = archive_env
        vid_file = sandbox / "output" / "crash_after.mp4"
        vid_file.write_bytes(b"durable committed content")
        norm = make_artifact(vid_file)

        # First run succeeds
        res1 = promoter.promote(
            normalized_assets=[norm],
            validation_result=ValidationResult(passed=True),
            platform_content_id="crash_after",
            sandbox_root=sandbox,
        )
        assert res1.status == PromotionStatus.SUCCESS

        # Subsequent retry (e.g. after worker crash before recording DownloadResult)
        res2 = promoter.promote(
            normalized_assets=[norm],
            validation_result=ValidationResult(passed=True),
            platform_content_id="crash_after",
            sandbox_root=sandbox,
        )
        assert res2.status == PromotionStatus.IDEMPOTENT_EXISTING
        assert res2.idempotent_existing is True
        # Verify formal asset is completely valid
        from src.downloader.promoter import verify_archived_asset
        verify_res = verify_archived_asset(res2.target_directory)
        assert verify_res.valid is True

    def test_existing_identical_not_deleted_on_rollback(
        self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]
    ) -> None:
        """Requirement 9 & M: Rollback of a failing transaction never touches pre-existing identical assets."""
        sandbox, archive_root = archive_env
        vid_file1 = sandbox / "output" / "existing_a.mp4"
        vid_file1.write_bytes(b"existing durable asset A bytes")
        norm1 = make_artifact(vid_file1)

        # Asset A promoted successfully
        res_a = promoter.promote(
            normalized_assets=[norm1],
            validation_result=ValidationResult(passed=True),
            platform_content_id="existing_a",
            sandbox_root=sandbox,
        )
        assert res_a.status == PromotionStatus.SUCCESS
        target_a = archive_root / "douyin" / "existing_a"
        assert (target_a / "existing_a.mp4").exists()

        # In a separate transaction for asset B, an error occurs
        vid_file2 = sandbox / "output" / "failing_b.mp4"
        vid_file2.write_bytes(b"failing asset B")
        norm2 = make_artifact(vid_file2)

        with patch("src.downloader.promoter.safe_copy_and_sync", side_effect=IOError("Disk write error")):
            with pytest.raises(IOError):
                promoter.promote(
                    normalized_assets=[norm2],
                    validation_result=ValidationResult(passed=True),
                    platform_content_id="failing_b",
                    sandbox_root=sandbox,
                )

        # Asset A must remain 100% untouched and intact!
        assert (target_a / "existing_a.mp4").exists()
        assert (target_a / "asset_manifest.json").exists()
        from src.downloader.promoter import verify_archived_asset
        assert verify_archived_asset(target_a).valid is True

    def test_existing_conflicting_set_rejected(
        self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]
    ) -> None:
        """Requirement 10 & G Case 2: Existing target directory with conflicting hash or asset set is rejected."""
        sandbox, archive_root = archive_env
        vid_file = sandbox / "output" / "conflict_target.mp4"
        vid_file.write_bytes(b"version 1 content")
        norm1 = make_artifact(vid_file)

        # Promote version 1
        res1 = promoter.promote(
            normalized_assets=[norm1],
            validation_result=ValidationResult(passed=True),
            platform_content_id="conflict_target",
            sandbox_root=sandbox,
        )
        assert res1.status == PromotionStatus.SUCCESS

        # Now attempt to promote version 2 with different bytes to the same content ID
        vid_file2 = sandbox / "output" / "conflict_target.mp4"
        vid_file2.write_bytes(b"version 2 DIFFERENT content")
        norm2 = make_artifact(vid_file2)

        with pytest.raises(ArchiveConflictError):
            promoter.promote(
                normalized_assets=[norm2],
                validation_result=ValidationResult(passed=True),
                platform_content_id="conflict_target",
                sandbox_root=sandbox,
            )

        # Existing version 1 remains intact
        assert (archive_root / "douyin" / "conflict_target" / "conflict_target.mp4").read_bytes() == b"version 1 content"

    def test_manifest_is_commit_marker(
        self, archive_env: tuple[Path, Path]
    ) -> None:
        """Requirement 11 & F: Content directory is not a formal asset if asset_manifest.json is missing."""
        _, archive_root = archive_env
        incomplete_dir = archive_root / "douyin" / "incomplete_item"
        incomplete_dir.mkdir(parents=True, exist_ok=True)
        (incomplete_dir / "incomplete_item.mp4").write_bytes(b"some media")

        # Without manifest, verify_archived_asset must return valid=False
        from src.downloader.promoter import verify_archived_asset
        ver = verify_archived_asset(incomplete_dir)
        assert ver.valid is False
        assert "Commit marker" in (ver.error or "") or "missing" in (ver.error or "")

    def test_verify_archived_asset_comprehensive(
        self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]
    ) -> None:
        """Requirement 12 & F: Comprehensive verification of valid, corrupt, and missing archived assets."""
        sandbox, archive_root = archive_env
        vid_file = sandbox / "output" / "verify_full.mp4"
        vid_file.write_bytes(b"full verification media data")
        norm = make_artifact(vid_file)

        res = promoter.promote(
            normalized_assets=[norm],
            validation_result=ValidationResult(passed=True),
            platform_content_id="verify_full",
            sandbox_root=sandbox,
        )
        assert res.status == PromotionStatus.SUCCESS

        from src.downloader.promoter import verify_archived_asset
        target_dir = res.target_directory
        # 1. Fully valid asset
        assert verify_archived_asset(target_dir).valid is True

        # 2. Corrupt one file by tampering byte
        media_file = target_dir / "verify_full.mp4"
        media_file.write_bytes(b"tampered content!")
        corrupt_ver = verify_archived_asset(target_dir)
        assert corrupt_ver.valid is False
        assert "hash mismatch" in (corrupt_ver.error or "")

        # 3. Non-existent directory
        assert verify_archived_asset(archive_root / "non_existent_dir").valid is False

    def test_live_staging_never_age_gc(
        self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]
    ) -> None:
        """Requirement 13 & I: Staging directory whose owner PID is alive is NEVER deleted even if TTL expired."""
        _, archive_root = archive_env
        staging_root = archive_root / "douyin" / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True)

        live_dir = staging_root / "live_staged_trans"
        live_dir.mkdir(parents=True, exist_ok=True)
        (live_dir / "partial.mp4").write_bytes(b"active writing")

        import time
        # Meta points to CURRENT process PID (which is alive!) and old timestamp (2 days ago)
        meta_file = staging_root / ".meta_live_staged_trans.json"
        meta_file.write_text(
            json.dumps({
                "owner_pid": os.getpid(),
                "created_at": time.time() - (2 * 86400),
                "state": "STAGING",
            }),
            encoding="utf-8",
        )

        gc_res = promoter.cleanup_stale_staging(platform="douyin", max_age_seconds=86400)
        assert gc_res.cleaned == 0
        assert gc_res.kept >= 1
        assert live_dir.exists(), "Live staging directory must NEVER be deleted by age GC"

    def test_uncertain_owner_preserved(
        self, promoter: ProductionArchivePromoter, archive_env: tuple[Path, Path]
    ) -> None:
        """Requirement 14 & I: Staging directory with uncertain ownership (missing or corrupt metadata) is PRESERVED."""
        _, archive_root = archive_env
        staging_root = archive_root / "douyin" / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True)

        # 1. Missing metadata directory
        no_meta_dir = staging_root / "no_meta_trans"
        no_meta_dir.mkdir(parents=True, exist_ok=True)

        # 2. Corrupted metadata directory
        corrupt_meta_dir = staging_root / "corrupt_meta_trans"
        corrupt_meta_dir.mkdir(parents=True, exist_ok=True)
        (staging_root / ".meta_corrupt_meta_trans.json").write_text("NOT_JSON{!!", encoding="utf-8")

        gc_res = promoter.cleanup_stale_staging(platform="douyin", max_age_seconds=86400)
        assert gc_res.cleaned == 0
        assert no_meta_dir.exists(), "Uncertain ownership directory without meta must be preserved"
        assert corrupt_meta_dir.exists(), "Uncertain ownership directory with corrupt meta must be preserved"
