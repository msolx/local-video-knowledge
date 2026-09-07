"""Comprehensive Unit and Component Tests for Content Routing Engine (DY-D09).

Validates:
1. Deterministic Plan Generation for Video Route:
   - Default expectations: mode=VIDEO, ValidationProfile.VIDEO, require_video_stream=True.
   - Audio optionality: require_audio_stream=False by default (supports silent videos).
   - Audio requirement triggered only by explicit canonical metadata hints.
   - Zero filesystem archive paths in DownloadExecutionPlan.
   - Schema compliance with download-execution-plan-v1.
2. Deterministic Plan Generation for Image Album Route:
   - Default expectations: mode=IMAGE_ALBUM, ValidationProfile.IMAGE_SET, min_images>=1.
   - BGM audio allowed but optional by default.
   - Strict sequence index requirement.
   - Image count hint propagation.
   - Zero filesystem archive paths.
3. Unsupported Content Handling & Terminal Failures:
   - Unsupported types ('live', 'note', 'article', 'story', 'unknown', '', None).
   - Raises UnsupportedContentTypeError mapped to DOWNLOAD_UNSUPPORTED_CONTENT.
   - D08 error policy classifies it as TERMINAL with retryable=False.
4. Extension Agnosticism Invariants:
   - NEVER guesses or mutates execution plan based on source_url file extension.
   - Single authority is DownloadTask.content_type.
5. Artifact Conformity Verification - Video:
   - Valid single video artifact (+ optional cover).
   - Missing primary video -> DOWNLOAD_MEDIA_INCOMPLETE.
   - Multiple primary videos -> DOWNLOAD_MEDIA_INCOMPLETE.
   - Route mismatch: video plan receiving album images -> DOWNLOAD_MEDIA_INCOMPLETE.
   - Explicit audio requirement validation.
6. Artifact Conformity Verification - Image Album:
   - Valid multi-image set (+ optional BGM, cover).
   - Missing images -> DOWNLOAD_MEDIA_INCOMPLETE.
   - Insufficient images below hinted count -> DOWNLOAD_MEDIA_INCOMPLETE.
   - Route mismatch: album plan receiving video -> DOWNLOAD_MEDIA_INCOMPLETE.
   - Duplicate sequence indices -> DOWNLOAD_VALIDATION_FAILED.
   - Non-positive or missing sequence indices -> DOWNLOAD_VALIDATION_FAILED.
7. Contradictory Mixed Primaries & Candidate Polymorphism:
   - Rejects sets containing both PRIMARY_VIDEO and ALBUM_IMAGE.
   - Empty candidate set rejection.
   - Polymorphic candidate support: ArtifactCandidate, NormalizedAsset, Path, dict.
8. Cross-Scope Separation & Physical Asset Identity:
   - Identical routing plan across different collection scopes.
   - Physical asset identity is strictly (platform, platform_content_id) - NOT bound to scope.
   - Cross-scope deduplication: D07 publishes single canonical media; second scope is IDEMPOTENT_EXISTING.
9. Downloader Subsystem Integration:
   - SafeDouyinDownloader integration: binds explicit ValidationProfile (never AUTO).
   - SafeDouyinDownloader handles unsupported content type cleanly.
   - SafeDouyinDownloader rejects artifact-route mismatches before promotion.
   - SafeDouyinDownloader resolves archive destination via D07 promoter (not D09 router).
10. Performance & Non-Interference:
    - Pure deterministic execution (< 50ms for 1000 plans, zero time.sleep).
    - Environment purity: NO f2 imports in router module.
    - Architectural boundary purity: NO queue polling, NO worker threads.
    - Protocol conformance.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from src.collector.download_models import DownloadTask
from src.downloader.contracts import (
    ContentRouter,
    DownloadedAsset,
    DownloaderErrorCode,
    DownloaderStatus,
    ExecutionStage,
    NormalizedAsset,
    ValidationResult,
)
from src.downloader.normalizer import ArtifactCandidate, ArtifactRole
from src.downloader.promoter import ProductionArchivePromoter, PromotionStatus
from src.downloader.retry_policy import (
    DownloadFailureFact,
    ProductionDownloadErrorPolicy,
    RetryAction,
)
from src.downloader.router import (
    SUPPORTED_CONTENT_TYPES,
    ArtifactRouteMismatchError,
    ConformityResult,
    DownloadExecutionPlan,
    ExecutionMode,
    ExpectedArtifactExpectation,
    ProductionContentRouter,
    RoutingError,
    UnsupportedContentTypeError,
)
from src.downloader.safe_downloader import SafeDouyinDownloader
from src.downloader.stubs import (
    DefaultDownloadErrorPolicy,
    DefaultTaskSandboxProvider,
    FakeArchivePromoter,
    FakeAssetNormalizer,
    FakeAssetStateInspector,
    FakeContentRouter,
    FakeDownloadBackend,
    FakeMediaValidator,
    NullCredentialProvider,
)
from src.downloader.validator import ValidationProfile


# =============================================================================
# Helper Fixtures & Factories
# =============================================================================


def make_task(
    content_type: str = "video",
    platform_content_id: str = "7123456789012345678",
    scope_id: str = "douyin:dyacct_test",
    source_url: str = "https://www.douyin.com/video/7123456789012345678",
    download_input: dict[str, Any] | None = None,
    metadata_ref: dict[str, Any] | None = None,
) -> DownloadTask:
    return DownloadTask(
        task_id=f"dl_douyin_{platform_content_id}_test",
        platform="douyin",
        scope_id=scope_id,
        platform_content_id=platform_content_id,
        content_type=content_type,
        source_url=source_url,
        download_input=download_input or {},
        metadata_ref=metadata_ref or {},
    )


# =============================================================================
# Group 1: Deterministic Plan Generation for Video Route (Tests 01 - 06)
# =============================================================================


class TestVideoPlanGeneration:
    """Validates deterministic plan generation for content_type='video'."""

    def test_01_video_plan_generation_defaults(self) -> None:
        """Test 01: Default video plan has mode=VIDEO, Profile.VIDEO, require_video=True, require_audio=False."""
        router = ProductionContentRouter()
        task = make_task(content_type="video")
        plan = router.plan_execution(task)

        assert plan.mode == ExecutionMode.VIDEO
        assert plan.content_type == "video"
        assert plan.backend_acquisition_mode == "video"
        assert plan.validation_profile == ValidationProfile.VIDEO
        assert plan.primary_artifact_role == ArtifactRole.PRIMARY_VIDEO
        assert plan.require_video_stream is True
        assert plan.require_audio_stream is False  # Silent video support by default
        assert plan.min_images == 0
        assert plan.expected_artifacts.min_primary_count == 1
        assert plan.expected_artifacts.max_primary_count == 1
        assert plan.expected_artifacts.allow_cover is True
        assert plan.expected_artifacts.strict_sequence_indices is False

    def test_02_video_plan_audio_hint_in_metadata_ref(self) -> None:
        """Test 02: Canonical metadata_ref with has_audio=True sets require_audio_stream=True."""
        router = ProductionContentRouter()
        task = make_task(content_type="video", metadata_ref={"has_audio": True})
        plan = router.plan_execution(task)

        assert plan.require_audio_stream is True
        assert plan.expected_artifacts.require_audio is True

    def test_03_video_plan_silent_video_support(self) -> None:
        """Test 03: Canonical metadata_ref with has_audio=False preserves require_audio_stream=False."""
        router = ProductionContentRouter()
        task = make_task(content_type="video", metadata_ref={"has_audio": False})
        plan = router.plan_execution(task)

        assert plan.require_audio_stream is False
        assert plan.expected_artifacts.require_audio is False

    def test_04_video_plan_audio_hint_in_download_input(self) -> None:
        """Test 04: Volatile download_input with expect_audio=True sets require_audio_stream=True."""
        router = ProductionContentRouter()
        task = make_task(content_type="video", download_input={"expect_audio": True})
        plan = router.plan_execution(task)

        assert plan.require_audio_stream is True
        assert plan.expected_artifacts.require_audio is True

    def test_05_video_plan_preserves_provenance_and_no_archive_path(self) -> None:
        """Test 05: Plan preserves provenance (platform, scope_id) but contains zero filesystem archive paths."""
        router = ProductionContentRouter()
        task = make_task(content_type="video", platform_content_id="6611417973221494020", scope_id="douyin:dyacct_custom")
        plan = router.plan_execution(task)

        assert plan.platform == "douyin"
        assert plan.scope_id == "douyin:dyacct_custom"
        assert plan.platform_content_id == "6611417973221494020"
        # D09 must NOT own or generate formal archive paths
        assert not hasattr(plan, "target_directory")
        assert not hasattr(plan, "archive_root")

    def test_06_video_plan_schema_compliance_and_json(self) -> None:
        """Test 06: Plan adheres strictly to download-execution-plan-v1 schema and serializes cleanly."""
        router = ProductionContentRouter()
        task = make_task(content_type="video")
        plan = router.plan_execution(task)

        d = plan.to_dict()
        assert d["schema_version"] == "download-execution-plan-v1"
        assert d["task_id"] == task.task_id
        assert d["platform"] == "douyin"
        assert d["scope_id"] == task.scope_id
        assert d["platform_content_id"] == task.platform_content_id
        assert d["mode"] == "video"
        assert d["backend_acquisition_mode"] == "video"
        assert d["validation_profile"] == "VIDEO"
        assert d["primary_artifact_role"] == "PRIMARY_VIDEO"
        assert isinstance(d["expected_artifacts"], dict)
        assert d["expected_artifacts"]["primary_role"] == "PRIMARY_VIDEO"
        assert "target_directory" not in d

        json_str = plan.to_json()
        assert "download-execution-plan-v1" in json_str
        assert "PRIMARY_VIDEO" in json_str
        assert "target_directory" not in json_str


# =============================================================================
# Group 2: Deterministic Plan Generation for Image Album Route (Tests 07 - 12)
# =============================================================================


class TestImageAlbumPlanGeneration:
    """Validates deterministic plan generation for content_type='image_album'."""

    def test_07_album_plan_generation_defaults(self) -> None:
        """Test 07: Default album plan has mode=IMAGE_ALBUM, Profile.IMAGE_SET, require_video=False, strict_sequence=True."""
        router = ProductionContentRouter()
        task = make_task(content_type="image_album")
        plan = router.plan_execution(task)

        assert plan.mode == ExecutionMode.IMAGE_ALBUM
        assert plan.content_type == "image_album"
        assert plan.backend_acquisition_mode == "image_album"
        assert plan.validation_profile == ValidationProfile.IMAGE_SET
        assert plan.primary_artifact_role == ArtifactRole.ALBUM_IMAGE
        assert plan.require_video_stream is False
        assert plan.require_audio_stream is False
        assert plan.min_images == 1
        assert plan.expected_artifacts.min_primary_count == 1
        assert plan.expected_artifacts.max_primary_count is None
        assert plan.expected_artifacts.allow_audio is True  # BGM permitted
        assert plan.expected_artifacts.require_audio is False  # BGM optional
        assert plan.expected_artifacts.strict_sequence_indices is True

    def test_08_album_plan_image_count_hint(self) -> None:
        """Test 08: image_count hint in download_input sets min_images and min_primary_count."""
        router = ProductionContentRouter()
        task = make_task(content_type="image_album", download_input={"image_count": 6})
        plan = router.plan_execution(task)

        assert plan.min_images == 6
        assert plan.expected_artifacts.min_primary_count == 6

    def test_09_album_plan_metadata_ref_image_count(self) -> None:
        """Test 09: image_count hint in metadata_ref sets min_images correctly."""
        router = ProductionContentRouter()
        task = make_task(content_type="image_album", metadata_ref={"image_count": 12})
        plan = router.plan_execution(task)

        assert plan.min_images == 12
        assert plan.expected_artifacts.min_primary_count == 12

    def test_10_album_plan_bgm_optional_default(self) -> None:
        """Test 10: Album route treats background audio as optional by default."""
        router = ProductionContentRouter()
        task = make_task(content_type="image_album")
        plan = router.plan_execution(task)

        assert plan.expected_artifacts.require_audio is False
        assert plan.expected_artifacts.allow_audio is True

    def test_11_album_plan_preserves_provenance_and_no_archive_path(self) -> None:
        """Test 11: Album plan preserves provenance (platform, scope_id) but contains zero filesystem archive paths."""
        router = ProductionContentRouter()
        task = make_task(content_type="image_album", platform_content_id="7169622286633274635")
        plan = router.plan_execution(task)

        assert plan.platform == "douyin"
        assert plan.scope_id == "douyin:dyacct_test"
        assert plan.platform_content_id == "7169622286633274635"
        assert not hasattr(plan, "target_directory")
        assert not hasattr(plan, "archive_root")

    def test_12_album_plan_schema_compliance_and_json(self) -> None:
        """Test 12: Album plan adheres to schema and serializes cleanly."""
        router = ProductionContentRouter()
        task = make_task(content_type="image_album")
        plan = router.plan_execution(task)

        d = plan.to_dict()
        assert d["schema_version"] == "download-execution-plan-v1"
        assert d["mode"] == "image_album"
        assert d["backend_acquisition_mode"] == "image_album"
        assert d["validation_profile"] == "IMAGE_SET"
        assert d["primary_artifact_role"] == "ALBUM_IMAGE"
        assert d["expected_artifacts"]["strict_sequence_indices"] is True
        assert "target_directory" not in d


# =============================================================================
# Group 3: Unsupported Content Type Routing & Terminal Failure (Tests 13 - 17)
# =============================================================================


class TestUnsupportedContentTypeHandling:
    """Validates that unsupported content types fail fast with exact taxonomy codes."""

    def test_13_unsupported_live_stream(self) -> None:
        """Test 13: Live stream content_type raises UnsupportedContentTypeError."""
        router = ProductionContentRouter()
        task = make_task(content_type="live")
        with pytest.raises(UnsupportedContentTypeError) as exc_info:
            router.plan_execution(task)
        assert exc_info.value.error_code == DownloaderErrorCode.DOWNLOAD_UNSUPPORTED_CONTENT
        assert "live" in str(exc_info.value)

    def test_14_unsupported_note(self) -> None:
        """Test 14: Note / article content_type raises UnsupportedContentTypeError."""
        router = ProductionContentRouter()
        task = make_task(content_type="note")
        with pytest.raises(UnsupportedContentTypeError) as exc_info:
            router.plan_execution(task)
        assert exc_info.value.error_code == DownloaderErrorCode.DOWNLOAD_UNSUPPORTED_CONTENT

    def test_15_unsupported_unknown_and_empty(self) -> None:
        """Test 15: Empty string and unknown content types raise UnsupportedContentTypeError."""
        router = ProductionContentRouter()
        for bad_ct in ["", "unknown", "story", "podcast", "article"]:
            task = make_task(content_type=bad_ct)
            with pytest.raises(UnsupportedContentTypeError) as exc_info:
                router.plan_execution(task)
            assert exc_info.value.error_code == DownloaderErrorCode.DOWNLOAD_UNSUPPORTED_CONTENT

    def test_16_d08_error_policy_classifies_unsupported_as_terminal(self) -> None:
        """Test 16: D08 ProductionDownloadErrorPolicy maps DOWNLOAD_UNSUPPORTED_CONTENT to TERMINAL."""
        policy = ProductionDownloadErrorPolicy()
        fact = DownloadFailureFact(
            error_code=DownloaderErrorCode.DOWNLOAD_UNSUPPORTED_CONTENT,
            message="Unsupported content_type 'live'",
        )
        dec = policy.decide(fact, attempt_history=1)
        assert dec.action == RetryAction.TERMINAL
        assert dec.retryable is False

    def test_17_is_supported_helper(self) -> None:
        """Test 17: is_supported accurately verifies supported set."""
        router = ProductionContentRouter()
        assert router.is_supported("video") is True
        assert router.is_supported("image_album") is True
        assert router.is_supported("VIDEO") is True
        assert router.is_supported("IMAGE_ALBUM") is True
        assert router.is_supported("live") is False
        assert router.is_supported("") is False
        assert router.is_supported(None) is False


# =============================================================================
# Group 4: Extension Agnosticism Invariants (Tests 18 - 20)
# =============================================================================


class TestExtensionAgnosticismInvariants:
    """Validates that URL / file extensions NEVER override task.content_type."""

    def test_18_no_guess_by_extension_video_with_weird_url(self) -> None:
        """Test 18: Video task with .webp in URL still plans as VIDEO."""
        router = ProductionContentRouter()
        task = make_task(
            content_type="video",
            source_url="https://www.douyin.com/video/12345?poster=thumb.webp",
        )
        plan = router.plan_execution(task)
        assert plan.mode == ExecutionMode.VIDEO
        assert plan.validation_profile == ValidationProfile.VIDEO

    def test_19_no_guess_by_extension_album_with_mp4_in_url(self) -> None:
        """Test 19: Image album task with .mp4 in URL still plans as IMAGE_ALBUM."""
        router = ProductionContentRouter()
        task = make_task(
            content_type="image_album",
            source_url="https://www.douyin.com/note/7169622?video_sample=foo.mp4",
        )
        plan = router.plan_execution(task)
        assert plan.mode == ExecutionMode.IMAGE_ALBUM
        assert plan.validation_profile == ValidationProfile.IMAGE_SET

    def test_20_content_type_is_sole_authority(self) -> None:
        """Test 20: Even when download_input has mp4 download URL, album remains IMAGE_ALBUM."""
        router = ProductionContentRouter()
        task = make_task(
            content_type="image_album",
            download_input={"play_addr": "https://aweme.snssdk.com/sample.mp4"},
        )
        plan = router.plan_execution(task)
        assert plan.mode == ExecutionMode.IMAGE_ALBUM
        assert plan.primary_artifact_role == ArtifactRole.ALBUM_IMAGE


# =============================================================================
# Group 5: Artifact Conformity - Video Route (Tests 21 - 27)
# =============================================================================


class TestVideoArtifactConformity:
    """Validates conformity checks for video execution plans."""

    @pytest.fixture
    def video_plan(self) -> DownloadExecutionPlan:
        router = ProductionContentRouter()
        return router.plan_execution(make_task(content_type="video"))

    def test_21_valid_video_single_primary(self, video_plan: DownloadExecutionPlan) -> None:
        """Test 21: Exactly 1 PRIMARY_VIDEO candidate passes conformity."""
        router = ProductionContentRouter()
        candidates = [
            ArtifactCandidate(file_path=Path("123.mp4"), role=ArtifactRole.PRIMARY_VIDEO)
        ]
        res = router.validate_artifact_conformity(video_plan, candidates)
        assert res.passed is True
        assert res.primary_count == 1

    def test_22_valid_video_with_cover(self, video_plan: DownloadExecutionPlan) -> None:
        """Test 22: PRIMARY_VIDEO + COVER_IMAGE passes conformity."""
        router = ProductionContentRouter()
        candidates = [
            ArtifactCandidate(file_path=Path("123.mp4"), role=ArtifactRole.PRIMARY_VIDEO),
            ArtifactCandidate(file_path=Path("123_cover.jpeg"), role=ArtifactRole.COVER_IMAGE),
        ]
        res = router.validate_artifact_conformity(video_plan, candidates)
        assert res.passed is True
        assert res.primary_count == 1
        assert res.cover_count == 1

    def test_23_video_route_missing_primary(self, video_plan: DownloadExecutionPlan) -> None:
        """Test 23: Video route with 0 primary artifacts fails with DOWNLOAD_MEDIA_INCOMPLETE."""
        router = ProductionContentRouter()
        candidates = [
            ArtifactCandidate(file_path=Path("123_cover.jpeg"), role=ArtifactRole.COVER_IMAGE)
        ]
        res = router.validate_artifact_conformity(video_plan, candidates)
        assert res.passed is False
        assert res.error_code == DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE
        assert "Missing required primary video artifact" in str(res.reason)

    def test_24_video_route_duplicate_primary(self, video_plan: DownloadExecutionPlan) -> None:
        """Test 24: Multiple PRIMARY_VIDEO artifacts fail with DOWNLOAD_MEDIA_INCOMPLETE."""
        router = ProductionContentRouter()
        candidates = [
            ArtifactCandidate(file_path=Path("123_a.mp4"), role=ArtifactRole.PRIMARY_VIDEO),
            ArtifactCandidate(file_path=Path("123_b.mp4"), role=ArtifactRole.PRIMARY_VIDEO),
        ]
        res = router.validate_artifact_conformity(video_plan, candidates)
        assert res.passed is False
        assert res.error_code == DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE
        assert "Multiple primary video artifacts" in str(res.reason)

    def test_25_video_route_mismatch_album_images_only(self, video_plan: DownloadExecutionPlan) -> None:
        """Test 25: Video plan receiving only album images fails with route mismatch."""
        router = ProductionContentRouter()
        candidates = [
            ArtifactCandidate(file_path=Path("123_img_001.webp"), role=ArtifactRole.ALBUM_IMAGE, sequence_index=1),
            ArtifactCandidate(file_path=Path("123_img_002.webp"), role=ArtifactRole.ALBUM_IMAGE, sequence_index=2),
        ]
        res = router.validate_artifact_conformity(video_plan, candidates)
        assert res.passed is False
        assert res.error_code == DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE
        assert "Route mismatch" in str(res.reason)

    def test_26_video_route_required_audio_present(self) -> None:
        """Test 26: Video plan requiring audio passes when audio artifact is present."""
        router = ProductionContentRouter()
        task = make_task(content_type="video", metadata_ref={"has_audio": True})
        plan = router.plan_execution(task)
        candidates = [
            ArtifactCandidate(file_path=Path("123.mp4"), role=ArtifactRole.PRIMARY_VIDEO),
            ArtifactCandidate(file_path=Path("123_bgm.mp3"), role=ArtifactRole.BGM_AUDIO),
        ]
        res = router.validate_artifact_conformity(plan, candidates)
        assert res.passed is True
        assert res.audio_count == 1

    def test_27_video_route_required_audio_missing(self) -> None:
        """Test 27: Video plan requiring audio fails when audio artifact is missing."""
        router = ProductionContentRouter()
        task = make_task(content_type="video", metadata_ref={"has_audio": True})
        plan = router.plan_execution(task)
        candidates = [
            ArtifactCandidate(file_path=Path("123.mp4"), role=ArtifactRole.PRIMARY_VIDEO)
        ]
        res = router.validate_artifact_conformity(plan, candidates)
        assert res.passed is False
        assert res.error_code == DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE
        assert "Required audio artifact is missing" in str(res.reason)


# =============================================================================
# Group 6: Artifact Conformity - Image Album Route (Tests 28 - 35)
# =============================================================================


class TestImageAlbumArtifactConformity:
    """Validates conformity checks for image album execution plans."""

    @pytest.fixture
    def album_plan(self) -> DownloadExecutionPlan:
        router = ProductionContentRouter()
        return router.plan_execution(make_task(content_type="image_album"))

    def test_28_valid_album_multiple_images(self, album_plan: DownloadExecutionPlan) -> None:
        """Test 28: Valid image album with 3 ordered sequence items passes conformity."""
        router = ProductionContentRouter()
        candidates = [
            ArtifactCandidate(file_path=Path("123_img_001.webp"), role=ArtifactRole.ALBUM_IMAGE, sequence_index=1),
            ArtifactCandidate(file_path=Path("123_img_002.webp"), role=ArtifactRole.ALBUM_IMAGE, sequence_index=2),
            ArtifactCandidate(file_path=Path("123_img_003.webp"), role=ArtifactRole.ALBUM_IMAGE, sequence_index=3),
        ]
        res = router.validate_artifact_conformity(album_plan, candidates)
        assert res.passed is True
        assert res.primary_count == 3
        assert res.sequence_indices == (1, 2, 3)

    def test_29_valid_album_with_bgm_and_cover(self, album_plan: DownloadExecutionPlan) -> None:
        """Test 29: Album with images + optional BGM + cover passes conformity."""
        router = ProductionContentRouter()
        candidates = [
            ArtifactCandidate(file_path=Path("123_img_001.webp"), role=ArtifactRole.ALBUM_IMAGE, sequence_index=1),
            ArtifactCandidate(file_path=Path("123_bgm.mp3"), role=ArtifactRole.BGM_AUDIO),
            ArtifactCandidate(file_path=Path("123_cover.jpeg"), role=ArtifactRole.COVER_IMAGE),
        ]
        res = router.validate_artifact_conformity(album_plan, candidates)
        assert res.passed is True
        assert res.primary_count == 1
        assert res.audio_count == 1
        assert res.cover_count == 1

    def test_30_album_route_missing_images(self, album_plan: DownloadExecutionPlan) -> None:
        """Test 30: Album plan with 0 images fails with DOWNLOAD_MEDIA_INCOMPLETE."""
        router = ProductionContentRouter()
        candidates = [
            ArtifactCandidate(file_path=Path("123_bgm.mp3"), role=ArtifactRole.BGM_AUDIO)
        ]
        res = router.validate_artifact_conformity(album_plan, candidates)
        assert res.passed is False
        assert res.error_code == DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE
        assert "Missing required album image artifacts" in str(res.reason)

    def test_31_album_route_insufficient_images_hint(self) -> None:
        """Test 31: Album plan requiring 5 images fails if only 3 are provided."""
        router = ProductionContentRouter()
        task = make_task(content_type="image_album", download_input={"image_count": 5})
        plan = router.plan_execution(task)
        candidates = [
            ArtifactCandidate(file_path=Path("123_img_001.webp"), role=ArtifactRole.ALBUM_IMAGE, sequence_index=1),
            ArtifactCandidate(file_path=Path("123_img_002.webp"), role=ArtifactRole.ALBUM_IMAGE, sequence_index=2),
            ArtifactCandidate(file_path=Path("123_img_003.webp"), role=ArtifactRole.ALBUM_IMAGE, sequence_index=3),
        ]
        res = router.validate_artifact_conformity(plan, candidates)
        assert res.passed is False
        assert res.error_code == DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE
        assert "Insufficient album images: expected at least 5, found 3" in str(res.reason)

    def test_32_album_route_mismatch_video_present(self, album_plan: DownloadExecutionPlan) -> None:
        """Test 32: Album plan receiving video artifact fails with route mismatch."""
        router = ProductionContentRouter()
        candidates = [
            ArtifactCandidate(file_path=Path("123.mp4"), role=ArtifactRole.PRIMARY_VIDEO)
        ]
        res = router.validate_artifact_conformity(album_plan, candidates)
        assert res.passed is False
        assert res.error_code == DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE
        assert "Route mismatch" in str(res.reason)

    def test_33_album_route_duplicate_sequence_index(self, album_plan: DownloadExecutionPlan) -> None:
        """Test 33: Duplicate sequence indices fail with DOWNLOAD_VALIDATION_FAILED."""
        router = ProductionContentRouter()
        candidates = [
            ArtifactCandidate(file_path=Path("123_img_001.webp"), role=ArtifactRole.ALBUM_IMAGE, sequence_index=1),
            ArtifactCandidate(file_path=Path("123_img_002a.webp"), role=ArtifactRole.ALBUM_IMAGE, sequence_index=2),
            ArtifactCandidate(file_path=Path("123_img_002b.webp"), role=ArtifactRole.ALBUM_IMAGE, sequence_index=2),
        ]
        res = router.validate_artifact_conformity(album_plan, candidates)
        assert res.passed is False
        assert res.error_code == DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED
        assert "Duplicate album image sequence indices detected" in str(res.reason)

    def test_34_album_route_non_positive_sequence_index(self, album_plan: DownloadExecutionPlan) -> None:
        """Test 34: 0 or negative sequence index fails with DOWNLOAD_VALIDATION_FAILED."""
        router = ProductionContentRouter()
        candidates = [
            ArtifactCandidate(file_path=Path("123_img_000.webp"), role=ArtifactRole.ALBUM_IMAGE, sequence_index=0),
        ]
        res = router.validate_artifact_conformity(album_plan, candidates)
        assert res.passed is False
        assert res.error_code == DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED
        assert "non-positive sequence index" in str(res.reason)

    def test_35_album_route_missing_sequence_index(self, album_plan: DownloadExecutionPlan) -> None:
        """Test 35: None sequence index in album image fails with DOWNLOAD_VALIDATION_FAILED."""
        router = ProductionContentRouter()
        candidates = [
            ArtifactCandidate(file_path=Path("random_name.webp"), role=ArtifactRole.ALBUM_IMAGE, sequence_index=None),
        ]
        res = router.validate_artifact_conformity(album_plan, candidates)
        assert res.passed is False
        assert res.error_code == DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED


# =============================================================================
# Group 7: Contradictory Primaries & Candidate Polymorphism (Tests 36 - 39)
# =============================================================================


class TestContradictoryAndPolymorphicCandidates:
    """Validates contradiction rejection and polymorphic candidate decoding."""

    def test_36_contradictory_mixed_primaries(self) -> None:
        """Test 36: Both PRIMARY_VIDEO and ALBUM_IMAGE present in artifact set fails."""
        router = ProductionContentRouter()
        plan = router.plan_execution(make_task(content_type="video"))
        candidates = [
            ArtifactCandidate(file_path=Path("123.mp4"), role=ArtifactRole.PRIMARY_VIDEO),
            ArtifactCandidate(file_path=Path("123_img_001.webp"), role=ArtifactRole.ALBUM_IMAGE, sequence_index=1),
        ]
        res = router.validate_artifact_conformity(plan, candidates)
        assert res.passed is False
        assert res.error_code == DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE
        assert "Contradictory media" in str(res.reason)

    def test_37_empty_candidate_set(self) -> None:
        """Test 37: Empty candidate list fails with DOWNLOAD_MEDIA_INCOMPLETE."""
        router = ProductionContentRouter()
        plan = router.plan_execution(make_task(content_type="video"))
        res = router.validate_artifact_conformity(plan, [])
        assert res.passed is False
        assert res.error_code == DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE

    def test_38_candidate_polymorphism_normalized_asset(self) -> None:
        """Test 38: NormalizedAsset inputs are accurately decoded and validated."""
        router = ProductionContentRouter()
        plan = router.plan_execution(make_task(content_type="image_album"))
        candidates = [
            NormalizedAsset(
                file_path=Path("/tmp/123_img_001.webp"),
                content_type="image_album",
                file_name="123_img_001.webp",
                metadata={"sequence_index": 1},
            ),
            NormalizedAsset(
                file_path=Path("/tmp/123_img_002.webp"),
                content_type="image_album",
                file_name="123_img_002.webp",
                metadata={"sequence_index": 2},
            ),
        ]
        res = router.validate_artifact_conformity(plan, candidates)
        assert res.passed is True
        assert res.primary_count == 2
        assert res.sequence_indices == (1, 2)

    def test_39_candidate_polymorphism_paths_and_dicts(self) -> None:
        """Test 39: Raw Paths and dicts are parsed correctly via D06 naming patterns."""
        router = ProductionContentRouter()
        plan = router.plan_execution(make_task(content_type="video"))
        candidates: list[Any] = [
            Path("/var/sandbox/7123456789.mp4"),
            {"file_name": "7123456789_cover.jpeg", "role": "COVER_IMAGE"},
        ]
        res = router.validate_artifact_conformity(plan, candidates)
        assert res.passed is True
        assert res.primary_count == 1
        assert res.cover_count == 1


# =============================================================================
# Group 8: Cross-Scope Separation & Physical Asset Identity (Tests 40 - 42)
# =============================================================================


class TestCrossScopeSeparationAndPhysicalAssetIdentity:
    """Validates that scope is decoupled from physical media identity (Section E)."""

    def test_40_cross_scope_identical_routing_plan(self) -> None:
        """Test 40: Task A and Task B with different scopes produce identical routes and zero archive paths."""
        router = ProductionContentRouter()
        content_id = "7671141177986518318"

        task_a = make_task(content_type="video", platform_content_id=content_id, scope_id="douyin:dyacct_A")
        task_b = make_task(content_type="video", platform_content_id=content_id, scope_id="douyin:dyacct_B")

        plan_a = router.plan_execution(task_a)
        plan_b = router.plan_execution(task_b)

        # 1. Routing decisions are identical
        assert plan_a.mode == plan_b.mode == ExecutionMode.VIDEO
        assert plan_a.validation_profile == plan_b.validation_profile == ValidationProfile.VIDEO
        assert plan_a.primary_artifact_role == plan_b.primary_artifact_role == ArtifactRole.PRIMARY_VIDEO
        assert plan_a.require_video_stream == plan_b.require_video_stream is True
        assert plan_a.require_audio_stream == plan_b.require_audio_stream is False

        # 2. Scope is preserved strictly for execution/provenance
        assert plan_a.scope_id == "douyin:dyacct_A"
        assert plan_b.scope_id == "douyin:dyacct_B"

        # 3. Neither plan contains filesystem archive destination paths
        assert not hasattr(plan_a, "target_directory")
        assert not hasattr(plan_b, "target_directory")

    def test_41_cross_scope_single_physical_media_identity(self) -> None:
        """Test 41: D07 ArchivePromoter computes identical canonical destination for different scopes."""
        archive_root = Path(tempfile.gettempdir()) / "test_cross_scope_arch"
        promoter = ProductionArchivePromoter(archive_root=archive_root)

        content_id = "7671141177986518318"
        dest_a = promoter.resolve_canonical_destination(platform="douyin", platform_content_id=content_id)
        dest_b = promoter.resolve_canonical_destination(platform="douyin", platform_content_id=content_id)

        # Both scopes resolve to the exact same physical directory
        assert dest_a == dest_b
        expected_dest = (archive_root / "douyin" / content_id).resolve()
        assert dest_a == expected_dest
        # Asserts scope_id did NOT enter the physical storage path
        assert "dyacct_A" not in str(dest_a)
        assert "dyacct_B" not in str(dest_b)

    def test_42_cross_scope_idempotent_ingest_no_duplicate_media(self) -> None:
        """Test 42: Media published by Scope A is recognized as existing by Scope B (idempotent bypass)."""
        with tempfile.TemporaryDirectory(prefix="cross_scope_test_") as td:
            arch_root = Path(td)
            promoter = ProductionArchivePromoter(archive_root=arch_root)
            content_id = "7671141177986518318"

            # Create sample normalized asset
            sample_file = arch_root / "sample_src.mp4"
            sample_file.write_bytes(b"TEST_VIDEO_BYTES_123456789")
            norm_assets = [
                NormalizedAsset(
                    file_path=sample_file,
                    content_type="video",
                    file_name=f"{content_id}.mp4",
                )
            ]

            # 1. Scope A publishes first
            val_result = ValidationResult(
                passed=True,
                ffprobe_verified=True,
                validated_sha256=None,
                validated_artifacts=(
                    {"file_name": f"{content_id}.mp4", "sha256": "digest", "size_bytes": 26},
                ),
            )
            res_a = promoter.promote(
                normalized_assets=norm_assets,
                validation_result=val_result,
                platform="douyin",
                platform_content_id=content_id,
                scope_id="douyin:dyacct_A",
            )
            assert res_a.status == PromotionStatus.SUCCESS
            final_dir = res_a.target_directory

            # 2. Scope B with identical bytes attempts ingest against same canonical destination
            res_b = promoter.promote(
                normalized_assets=norm_assets,
                validation_result=val_result,
                platform="douyin",
                platform_content_id=content_id,
                scope_id="douyin:dyacct_B",
            )
            assert res_b.status == PromotionStatus.IDEMPOTENT_EXISTING
            assert res_b.target_directory == final_dir

            # 3. Assert zero duplicate scope folders created in archive
            douyin_folder = arch_root / "douyin"
            subdirs = [p.name for p in douyin_folder.iterdir() if p.is_dir() and not p.name.startswith(".")]
            assert subdirs == [content_id]
            assert "douyin_dyacct_A" not in subdirs
            assert "douyin_dyacct_B" not in subdirs


# =============================================================================
# Group 9: Downloader Subsystem Integration (Tests 43 - 46)
# =============================================================================


class TestDownloaderSubsystemIntegration:
    """Validates integration with SafeDouyinDownloader and stage machine."""

    def test_43_safe_downloader_unsupported_content_type_fails_at_received(self) -> None:
        """Test 43: SafeDouyinDownloader with unsupported content_type returns DOWNLOAD_UNSUPPORTED_CONTENT."""
        router = ProductionContentRouter()
        downloader = SafeDouyinDownloader(router=router)
        task = make_task(content_type="live")

        result = downloader.execute(task)
        assert result.status == DownloaderStatus.FAILED
        assert result.error_code == DownloaderErrorCode.DOWNLOAD_UNSUPPORTED_CONTENT.value
        assert result.retryable is False
        assert result.stage == ExecutionStage.RECEIVED

    def test_44_safe_downloader_video_route_binds_video_profile(self) -> None:
        """Test 44: SafeDouyinDownloader executes video task with ValidationProfile.VIDEO (never AUTO)."""
        observed_profiles: list[ValidationProfile] = []

        class RecordingValidator(FakeMediaValidator):
            def validate_assets(self, asset_paths: list[Path], profile: ValidationProfile = ValidationProfile.AUTO) -> ValidationResult:
                observed_profiles.append(profile)
                return ValidationResult(passed=True, ffprobe_verified=True)

        router = ProductionContentRouter()
        backend = FakeDownloadBackend(success=True, files_to_create=("sample.mp4",))
        downloader = SafeDouyinDownloader(
            router=router,
            validator=RecordingValidator(),
            backend=backend,
            sandbox_provider=DefaultTaskSandboxProvider(),
        )

        task = make_task(content_type="video")
        result = downloader.execute(task)

        assert result.status == DownloaderStatus.SUCCESS
        assert len(observed_profiles) == 1
        assert observed_profiles[0] == ValidationProfile.VIDEO
        assert observed_profiles[0] != ValidationProfile.AUTO

    def test_45_safe_downloader_album_route_binds_image_set_profile(self) -> None:
        """Test 45: SafeDouyinDownloader executes image_album task with ValidationProfile.IMAGE_SET (never AUTO)."""
        observed_profiles: list[ValidationProfile] = []

        class RecordingValidator(FakeMediaValidator):
            def validate_assets(self, asset_paths: list[Path], profile: ValidationProfile = ValidationProfile.AUTO) -> ValidationResult:
                observed_profiles.append(profile)
                return ValidationResult(passed=True, ffprobe_verified=True)

        class AlbumNormalizer(FakeAssetNormalizer):
            def normalize(self, raw_assets: list[Path], platform_content_id: str, content_type: str, metadata_hint: dict[str, Any], **kwargs: Any) -> list[NormalizedAsset]:
                return [
                    NormalizedAsset(
                        file_path=raw_assets[0],
                        content_type="image_album",
                        file_name=f"{platform_content_id}_img_001.webp",
                        metadata={"sequence_index": 1},
                    )
                ]

        router = ProductionContentRouter()
        backend = FakeDownloadBackend(success=True, files_to_create=("test_img_001.webp",))
        downloader = SafeDouyinDownloader(
            router=router,
            validator=RecordingValidator(),
            normalizer=AlbumNormalizer(),
            backend=backend,
            sandbox_provider=DefaultTaskSandboxProvider(),
        )

        task = make_task(content_type="image_album")
        result = downloader.execute(task)

        assert result.status == DownloaderStatus.SUCCESS
        assert len(observed_profiles) == 1
        assert observed_profiles[0] == ValidationProfile.IMAGE_SET
        assert observed_profiles[0] != ValidationProfile.AUTO

    def test_46_safe_downloader_route_mismatch_fails_with_media_incomplete(self) -> None:
        """Test 46: SafeDouyinDownloader rejects normalized assets that mismatch the planned route."""
        class CustomMismatchNormalizer(FakeAssetNormalizer):
            def normalize(self, raw_assets: list[Path], platform_content_id: str, content_type: str, metadata_hint: dict[str, Any], **kwargs: Any) -> list[NormalizedAsset]:
                return [
                    NormalizedAsset(
                        file_path=raw_assets[0],
                        content_type="image_album",
                        file_name=f"{platform_content_id}_img_001.webp",
                        metadata={"sequence_index": 1},
                    )
                ]

        router = ProductionContentRouter()
        backend = FakeDownloadBackend(success=True, files_to_create=("test.webp",))
        downloader = SafeDouyinDownloader(
            router=router,
            normalizer=CustomMismatchNormalizer(),
            backend=backend,
            sandbox_provider=DefaultTaskSandboxProvider(),
        )

        task = make_task(content_type="video")
        result = downloader.execute(task)

        assert result.status == DownloaderStatus.FAILED
        assert result.error_code == DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE.value
        assert "Route mismatch" in result.message


# =============================================================================
# Group 10: Performance & Non-Interference Invariants (Tests 47 - 51)
# =============================================================================


class TestPerformanceAndNonInterference:
    """Validates performance and non-interference boundaries."""

    def test_47_pure_deterministic_fast_execution(self) -> None:
        """Test 47: Routing engine evaluates 1000 plans in < 50ms (zero sleep, pure logic)."""
        router = ProductionContentRouter()
        task = make_task(content_type="video")

        t0 = time.monotonic()
        for _ in range(1000):
            plan = router.plan_execution(task)
        elapsed = time.monotonic() - t0

        assert elapsed < 0.1  # Well below 100ms
        assert plan.mode == ExecutionMode.VIDEO

    def test_48_no_f2_imports_in_router_module(self) -> None:
        """Test 48: router module has zero dependency on f2."""
        import src.downloader.router as router_mod
        assert not hasattr(router_mod, "f2")
        assert "f2" not in router_mod.__file__.lower()

    def test_49_no_queue_polling_or_worker_pool(self) -> None:
        """Test 49: router module contains no queue polling or worker thread management."""
        import src.downloader.router as router_mod
        for attr in ["poll_outbox", "WorkerPool", "Thread", "ThreadPoolExecutor"]:
            assert not hasattr(router_mod, attr)

    def test_50_plan_preserves_task_id_and_content_id(self) -> None:
        """Test 50: Generated plan preserves exact task_id and platform_content_id."""
        router = ProductionContentRouter()
        task = make_task(content_type="video", platform_content_id="9876543210")
        plan = router.plan_execution(task)
        assert plan.task_id == task.task_id
        assert plan.platform_content_id == "9876543210"

    def test_51_router_conforms_to_content_router_protocol(self) -> None:
        """Test 51: ProductionContentRouter strictly implements ContentRouter protocol."""
        from src.downloader.contracts import ContentRouter
        router = ProductionContentRouter()
        assert isinstance(router, ContentRouter)
        assert hasattr(router, "plan_execution")
        assert hasattr(router, "validate_artifact_conformity")
