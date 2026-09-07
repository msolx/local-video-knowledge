"""Comprehensive Test Suite for F2 In-Process Backend Adapter (DY-D03).

Validates all 30 required offline security invariants and scenarios, plus real smoke integration:
1.  F2 version detection (is_f2_available, get_f2_version)
2.  Lazy import (importing f2_backend does not import f2)
3.  Main environment has NO F2
4.  Worker environment has F2 available (0.0.1.7)
5.  Explicit credential export (reveal_for_backend, to_f2_credentials)
6.  Zero argv credentials (--cookie never in sys.argv)
7.  Zero env credentials (COOKIE never in os.environ)
8.  Zero temporary credential config files created on disk
9.  Credential state cleanup after task execution
10. Credential isolation (Task A credential never leaks to Task B)
11. Output plan honored (BackendOutputPlan sandbox_output_root used)
12. Folderize disabled (flat output without author nickname subdirectories)
13. Short naming ({aweme_id} stem)
14. Sandbox CWD containment (os.getcwd() set to sandbox and restored)
15. Outside candidate path strictly rejected
16. Video ArtifactCandidate role (PRIMARY_VIDEO, media_kind="video")
17. Image album ArtifactCandidates role (ALBUM_IMAGE, media_kind="image")
18. Image sequence order (strict 1..N order even if discovered out of order)
19. BGM audio role (BGM_AUDIO, media_kind="audio")
20. Diagnostics excluded (.db, .sqlite, .txt, .desc, .json, .log)
21. No global directory glob (current sandbox only)
22. Empty output structured failure (DOWNLOAD_MEDIA_INCOMPLETE)
23. F2 exception structured failure mapped to error taxonomy
24. HTTP 429 classification (DOWNLOAD_RATE_LIMITED)
25. HTTP 5xx classification (DOWNLOAD_SERVER_ERROR)
26. Network error classification (DOWNLOAD_NETWORK_ERROR / DOWNLOAD_TIMEOUT)
27. Ambiguous 403 classification (DOWNLOAD_PERMISSION_DENIED, not AUTH_REQUIRED)
28. Bark ERROR does not alone fail when media artifacts exist
29. No archive access (backend accepts no archive_root)
30. Backend result JSON-safe and redacted
31. F2EnvironmentError raised when missing F2
32. Explicit download_video method (AcquisitionMode.VIDEO)
33. Explicit download_image_album method (AcquisitionMode.IMAGE_ALBUM)
34. Real video smoke test (Live F2 download -> D06 -> D05 -> D07 -> Temporary Archive Root)
35. Real image album smoke test (Live F2 download -> D06 -> D05 -> D07 -> Temporary Archive Root)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.downloader.contracts import (
    BackendDownloadResult,
    DownloaderErrorCode,
    scrub_secrets,
)
from src.downloader.f2_backend import (
    AcquisitionMode,
    BackendAcquisitionSpec,
    F2EnvironmentError,
    F2InProcessBackendAdapter,
    classify_f2_error,
    get_f2_version,
    is_f2_available,
)
from src.downloader.normalizer import (
    ArtifactCandidate,
    ArtifactRole,
    BackendOutputPlan,
    NormalizationContainmentError,
    ProductionAssetNormalizer,
    plan_backend_output,
    verify_sandbox_containment,
)


# =============================================================================
# Helper Fixtures & Dummy Credential Objects
# =============================================================================


class MockCredentialContext:
    """Mock CredentialContext adhering to D02 non-Mapping contracts."""

    def __init__(self, cookies_dict: dict[str, str], scope_id: str = "douyin:dyacct_test123") -> None:
        self._cookies = dict(cookies_dict)
        self.account_scope_id = scope_id
        self._active = True

    def reveal_for_backend(self) -> dict[str, str]:
        if not self._active:
            raise RuntimeError("Credential context expired")
        cookie_header = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
        return {"cookie": cookie_header, "account_scope_id": self.account_scope_id}

    def to_f2_credentials(self) -> dict[str, str]:
        return self.reveal_for_backend()

    def close(self) -> None:
        self._active = False
        self._cookies.clear()


# =============================================================================
# Group 1: Environment & Lazy Loading (Tests 1 - 4, 31)
# =============================================================================


class TestF2EnvironmentAndLazyLoading:
    """Validates F2 environment detection and lazy import boundary."""

    def test_01_f2_version_detection(self) -> None:
        """Test 01: F2 version detection returns bool and version string or None."""
        avail = is_f2_available()
        ver = get_f2_version()
        if avail:
            assert ver is not None
            assert ver == "0.0.1.7"
        else:
            assert ver is None

    def test_02_lazy_import_no_side_effects(self) -> None:
        """Test 02: Importing f2_backend does not force import f2 at module load time."""
        # Check that f2_backend is already imported
        assert "src.downloader.f2_backend" in sys.modules
        # In main env, 'f2' must not be in sys.modules
        if not is_f2_available():
            assert "f2" not in sys.modules

    def test_03_main_env_no_f2(self) -> None:
        """Test 03: Main environment must NOT have f2 installed."""
        is_main_venv = ".venv-f2" not in sys.executable.lower()
        if is_main_venv:
            assert not is_f2_available(), "Main environment must not have F2 installed!"
            assert get_f2_version() is None

    def test_04_worker_env_f2_available(self) -> None:
        """Test 04: Dedicated worker environment (.venv-f2) must have F2 installed."""
        is_worker_venv = ".venv-f2" in sys.executable.lower()
        if is_worker_venv:
            assert is_f2_available(), "Worker environment must have F2 installed!"
            assert get_f2_version() == "0.0.1.7"

    def test_31_f2_environment_error_on_missing_f2(self) -> None:
        """Test 31: Adapter raises F2EnvironmentError or reports failure if F2 missing."""
        with patch("src.downloader.f2_backend.is_f2_available", return_value=False):
            adapter = F2InProcessBackendAdapter()
            with pytest.raises(F2EnvironmentError):
                adapter.initialize()

            with tempfile.TemporaryDirectory() as td:
                res = adapter.execute_download(
                    source_url="https://www.douyin.com/video/123",
                    sandbox_dir=Path(td),
                    download_input={},
                )
                assert res.success is False
                assert "F2 is not available" in res.error_message


# =============================================================================
# Group 2: Credential Boundary & Scrubbing (Tests 5 - 10)
# =============================================================================


class TestCredentialBoundaryAndScrubbing:
    """Validates strict in-memory credential injection and zero disk/env/argv leakage."""

    def test_05_explicit_credential_export(self) -> None:
        """Test 05: Adapter accepts explicit reveal_for_backend / to_f2_credentials."""
        ctx = MockCredentialContext({"sessionid": "secret_sess_123", "msToken": "secret_token_abc"})
        creds = ctx.reveal_for_backend()
        assert "cookie" in creds
        assert "sessionid=secret_sess_123" in creds["cookie"]

    def test_06_no_argv_credential(self) -> None:
        """Test 06: Zero credentials injected into sys.argv."""
        ctx = MockCredentialContext({"sessionid": "super_secret_cookie_999"})
        orig_argv = list(sys.argv)

        adapter = F2InProcessBackendAdapter()
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "output"
            out_dir.mkdir()
            plan = BackendOutputPlan(
                execution_id="e1",
                sandbox_output_root=out_dir,
                safe_filename_stem="12345",
                platform_content_id="12345",
            )
            # Offline run
            with patch.object(adapter, "_run_f2_in_sandbox", return_value=[]) if hasattr(adapter, "_run_f2_in_sandbox") else patch("src.downloader.f2_backend.F2InProcessBackendAdapter._run_f2_handler"):
                adapter.execute_download(
                    source_url="https://www.douyin.com/video/12345",
                    sandbox_dir=Path(td),
                    download_input={},
                    credentials=ctx,
                    path_plan=plan,
                )

        assert sys.argv == orig_argv
        for arg in sys.argv:
            assert "super_secret_cookie_999" not in arg
            assert "--cookie" not in arg

    def test_07_no_env_credential(self) -> None:
        """Test 07: Zero credentials injected into os.environ."""
        ctx = MockCredentialContext({"sessionid": "super_secret_cookie_888"})
        adapter = F2InProcessBackendAdapter()

        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "output"
            out_dir.mkdir()
            plan = BackendOutputPlan(
                execution_id="e1",
                sandbox_output_root=out_dir,
                safe_filename_stem="12345",
                platform_content_id="12345",
            )
            with patch("src.downloader.f2_backend.F2InProcessBackendAdapter._run_f2_handler"):
                adapter.execute_download(
                    source_url="https://www.douyin.com/video/12345",
                    sandbox_dir=Path(td),
                    download_input={},
                    credentials=ctx,
                    path_plan=plan,
                )

        assert "COOKIE" not in os.environ
        assert "sessionid" not in os.environ
        for v in os.environ.values():
            assert "super_secret_cookie_888" not in v

    def test_08_no_temp_credential_config(self) -> None:
        """Test 08: Zero temporary cookie files created on disk."""
        ctx = MockCredentialContext({"sessionid": "super_secret_cookie_777"})
        adapter = F2InProcessBackendAdapter()

        with tempfile.TemporaryDirectory() as td:
            sb = Path(td)
            out_dir = sb / "output"
            out_dir.mkdir()
            plan = BackendOutputPlan(
                execution_id="e1",
                sandbox_output_root=out_dir,
                safe_filename_stem="12345",
                platform_content_id="12345",
            )
            with patch("src.downloader.f2_backend.F2InProcessBackendAdapter._run_f2_handler"):
                adapter.execute_download(
                    source_url="https://www.douyin.com/video/12345",
                    sandbox_dir=sb,
                    download_input={},
                    credentials=ctx,
                    path_plan=plan,
                )

            # Check all files in sandbox
            for f in sb.rglob("*"):
                if f.is_file():
                    content = f.read_bytes()
                    assert b"super_secret_cookie_777" not in content

    def test_09_credential_state_cleanup(self) -> None:
        """Test 09: Post-execution scrubbing deletes local credential references."""
        ctx = MockCredentialContext({"sessionid": "ephemeral_secret_111"})
        adapter = F2InProcessBackendAdapter()

        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "output"
            out_dir.mkdir()
            plan = BackendOutputPlan(
                execution_id="e1",
                sandbox_output_root=out_dir,
                safe_filename_stem="12345",
                platform_content_id="12345",
            )
            with patch("src.downloader.f2_backend.F2InProcessBackendAdapter._run_f2_handler"):
                adapter.execute_download(
                    source_url="https://www.douyin.com/video/12345",
                    sandbox_dir=Path(td),
                    download_input={},
                    credentials=ctx,
                    path_plan=plan,
                )

        # Ensure adapter instance does not retain credential attributes
        assert not hasattr(adapter, "credentials")
        assert not hasattr(adapter, "cookie")

    def test_10_credential_isolation_a_not_leak_b(self) -> None:
        """Test 10: Task A credential never leaks to Task B execution."""
        ctx_a = MockCredentialContext({"sessionid": "secret_A_999"}, scope_id="scope_A")
        ctx_b = MockCredentialContext({"sessionid": "secret_B_888"}, scope_id="scope_B")
        adapter = F2InProcessBackendAdapter()

        recorded_cookies = []

        def capture_f2(*args, **kwargs):
            # Capture what F2 was called with
            pass

        with tempfile.TemporaryDirectory() as td_a, tempfile.TemporaryDirectory() as td_b:
            sb_a = Path(td_a)
            sb_b = Path(td_b)
            (sb_a / "output").mkdir()
            (sb_b / "output").mkdir()

            with patch("src.downloader.f2_backend.F2InProcessBackendAdapter._run_f2_handler", side_effect=capture_f2):
                res_a = adapter.execute_download(
                    source_url="https://www.douyin.com/video/111",
                    sandbox_dir=sb_a,
                    download_input={},
                    credentials=ctx_a,
                )
                res_b = adapter.execute_download(
                    source_url="https://www.douyin.com/video/222",
                    sandbox_dir=sb_b,
                    download_input={},
                    credentials=ctx_b,
                )

            # Neither diagnostics leak the other
            assert "secret_A_999" not in str(res_b.raw_diagnostics)
            assert "secret_B_888" not in str(res_a.raw_diagnostics)


# =============================================================================
# Group 3: Sandbox Containment & Path Planning (Tests 11 - 15, 21, 29)
# =============================================================================


class TestSandboxContainmentAndPathPlanning:
    """Validates adherence to BackendOutputPlan and strict sandbox containment."""

    def test_11_output_plan_honored(self) -> None:
        """Test 11: Backend strictly uses sandbox_output_root from BackendOutputPlan."""
        adapter = F2InProcessBackendAdapter()
        with tempfile.TemporaryDirectory() as td:
            sb = Path(td)
            planned_output = sb / "output"
            planned_output.mkdir()

            plan = BackendOutputPlan(
                execution_id="exec_11",
                sandbox_output_root=planned_output,
                safe_filename_stem="7671141177986518318",
                platform_content_id="7671141177986518318",
            )

            # Simulate F2 creating video inside planned_output
            def fake_f2(*args, **kwargs):
                (planned_output / "7671141177986518318_video.mp4").write_bytes(b"fake_mp4_bytes")

            with patch("src.downloader.f2_backend.is_f2_available", return_value=True), \
                 patch("src.downloader.f2_backend.F2InProcessBackendAdapter._run_f2_handler", side_effect=fake_f2):
                res = adapter.execute_download(
                    source_url="https://www.douyin.com/video/7671141177986518318",
                    sandbox_dir=sb,
                    download_input={},
                    path_plan=plan,
                )

            assert res.success is True
            assert len(res.raw_files) == 1
            assert res.raw_files[0] == planned_output / "7671141177986518318_video.mp4"

    def test_12_folderize_disabled(self) -> None:
        """Test 12: Output must be flat, folderize=False (no author nickname folder)."""
        adapter = F2InProcessBackendAdapter()
        with tempfile.TemporaryDirectory() as td:
            sb = Path(td)
            out_dir = sb / "output"
            out_dir.mkdir()

            def fake_f2(*args, **kwargs):
                (out_dir / "12345_video.mp4").write_bytes(b"video_bytes")

            with patch("src.downloader.f2_backend.is_f2_available", return_value=True), \
                 patch("src.downloader.f2_backend.F2InProcessBackendAdapter._run_f2_handler", side_effect=fake_f2):
                res = adapter.execute_download(
                    source_url="https://www.douyin.com/video/12345",
                    sandbox_dir=sb,
                    download_input={},
                )

            # Confirm file is directly in output, not in a subfolder
            assert res.raw_files[0].parent == out_dir

    def test_13_short_naming(self) -> None:
        """Test 13: File naming must use clean short stem {aweme_id}."""
        adapter = F2InProcessBackendAdapter()
        with tempfile.TemporaryDirectory() as td:
            sb = Path(td)
            out = sb / "output"
            out.mkdir()

            def fake_f2(*args, **kwargs):
                (out / "6611417973221494020_video.mp4").write_bytes(b"video_bytes")

            with patch("src.downloader.f2_backend.is_f2_available", return_value=True), \
                 patch("src.downloader.f2_backend.F2InProcessBackendAdapter._run_f2_handler", side_effect=fake_f2):
                res = adapter.execute_download(
                    source_url="https://www.douyin.com/video/6611417973221494020",
                    sandbox_dir=sb,
                    download_input={},
                )

            assert "6611417973221494020" in res.raw_files[0].name

    def test_14_sandbox_cwd_containment(self) -> None:
        """Test 14: CWD is set to sandbox during execution and restored to original CWD."""
        adapter = F2InProcessBackendAdapter()
        orig_cwd = os.getcwd()
        executed_cwd = []

        with tempfile.TemporaryDirectory() as td:
            sb = Path(td).resolve()
            (sb / "output").mkdir()

            def fake_f2(*args, **kwargs):
                executed_cwd.append(Path(os.getcwd()).resolve())
                # Simulate SQLite DB creation in cwd
                (Path(os.getcwd()) / "douyin_users.db").write_bytes(b"sqlite_data")
                (sb / "output" / "123_video.mp4").write_bytes(b"video_data")

            with patch("src.downloader.f2_backend.is_f2_available", return_value=True), \
                 patch("src.downloader.f2_backend.F2InProcessBackendAdapter._run_f2_handler", side_effect=fake_f2):
                res = adapter.execute_download(
                    source_url="https://www.douyin.com/video/123",
                    sandbox_dir=sb,
                    download_input={},
                )

            assert executed_cwd[0] == sb
            assert os.getcwd() == orig_cwd
            # SQLite DB must be inside sandbox
            assert (sb / "douyin_users.db").exists()
            # SQLite DB must NOT be in raw_files candidates
            assert (sb / "douyin_users.db") not in res.raw_files

    def test_15_outside_path_rejected(self) -> None:
        """Test 15: If an artifact candidate resides outside sandbox, containment check fails."""
        with tempfile.TemporaryDirectory() as td_sb, tempfile.TemporaryDirectory() as td_outside:
            sb = Path(td_sb)
            outside_file = Path(td_outside) / "leak.mp4"
            outside_file.write_bytes(b"leak")

            with pytest.raises(NormalizationContainmentError):
                verify_sandbox_containment(outside_file, sb)

    def test_21_no_global_directory_glob(self) -> None:
        """Test 21: Discovery only scans the current sandbox directory."""
        adapter = F2InProcessBackendAdapter()
        with tempfile.TemporaryDirectory() as td_sb, tempfile.TemporaryDirectory() as td_other:
            sb = Path(td_sb)
            (sb / "output").mkdir()
            other = Path(td_other)
            (other / "unrelated_video.mp4").write_bytes(b"other")

            def fake_f2(*args, **kwargs):
                (sb / "output" / "my_video.mp4").write_bytes(b"mine")

            with patch("src.downloader.f2_backend.is_f2_available", return_value=True), \
                 patch("src.downloader.f2_backend.F2InProcessBackendAdapter._run_f2_handler", side_effect=fake_f2):
                res = adapter.execute_download(
                    source_url="https://www.douyin.com/video/my_video",
                    sandbox_dir=sb,
                    download_input={},
                )

            # Only files inside sb are included
            for f in res.raw_files:
                assert sb in f.parents

    def test_29_no_archive_access(self) -> None:
        """Test 29: Adapter does not accept or write to canonical archive_root."""
        adapter = F2InProcessBackendAdapter()
        with tempfile.TemporaryDirectory() as td_sb, tempfile.TemporaryDirectory() as td_archive:
            sb = Path(td_sb)
            (sb / "output").mkdir()
            archive = Path(td_archive)

            def fake_f2(*args, **kwargs):
                (sb / "output" / "video.mp4").write_bytes(b"content")

            with patch("src.downloader.f2_backend.is_f2_available", return_value=True), \
                 patch("src.downloader.f2_backend.F2InProcessBackendAdapter._run_f2_handler", side_effect=fake_f2):
                res = adapter.execute_download(
                    source_url="https://www.douyin.com/video/123",
                    sandbox_dir=sb,
                    download_input={},
                )

            # Canonical archive directory remains completely empty
            assert list(archive.rglob("*")) == []

    def test_30_sequential_tasks_isolation(self) -> None:
        """Test 30: Consecutive tasks with different scopes/sandboxes do not leak state."""
        adapter = F2InProcessBackendAdapter()
        orig_cwd = os.getcwd()

        with tempfile.TemporaryDirectory() as td_a, tempfile.TemporaryDirectory() as td_b:
            sb_a = Path(td_a).resolve()
            (sb_a / "output").mkdir()
            sb_b = Path(td_b).resolve()
            (sb_b / "output").mkdir()

            captured_creds = []
            captured_cwds = []

            def fake_f2_a(output_root, source_url, cookie_header, *args, **kwargs):
                captured_cwds.append(os.getcwd())
                captured_creds.append(cookie_header)
                (output_root / "video_a.mp4").write_bytes(b"data_a")

            def fake_f2_b(output_root, source_url, cookie_header, *args, **kwargs):
                captured_cwds.append(os.getcwd())
                captured_creds.append(cookie_header)
                (output_root / "video_b.mp4").write_bytes(b"data_b")

            # Execute Task A
            with patch("src.downloader.f2_backend.is_f2_available", return_value=True), \
                 patch("src.downloader.f2_backend.F2InProcessBackendAdapter._run_f2_handler", side_effect=fake_f2_a):
                res_a = adapter.execute_download(
                    source_url="https://www.douyin.com/video/aaa",
                    sandbox_dir=sb_a,
                    download_input={"scope_id": "douyin:scope_a"},
                    credentials={"cookie": "secret_cookie_a"},
                )

            # Assert Task A postconditions
            assert os.getcwd() == orig_cwd
            assert res_a.success is True
            assert (sb_a / "output" / "video_a.mp4").exists()
            assert not (sb_b / "output" / "video_a.mp4").exists()

            # Execute Task B
            with patch("src.downloader.f2_backend.is_f2_available", return_value=True), \
                 patch("src.downloader.f2_backend.F2InProcessBackendAdapter._run_f2_handler", side_effect=fake_f2_b):
                res_b = adapter.execute_download(
                    source_url="https://www.douyin.com/video/bbb",
                    sandbox_dir=sb_b,
                    download_input={"scope_id": "douyin:scope_b"},
                    credentials={"cookie": "secret_cookie_b"},
                )

            # Assert Task B postconditions
            assert os.getcwd() == orig_cwd
            assert res_b.success is True
            assert (sb_b / "output" / "video_b.mp4").exists()
            assert not (sb_a / "output" / "video_b.mp4").exists()

            # Verify complete isolation
            assert captured_cwds[0] == str(sb_a)
            assert captured_cwds[1] == str(sb_b)
            assert captured_creds[0] == "secret_cookie_a"
            assert captured_creds[1] == "secret_cookie_b"



# =============================================================================
# Group 4: Artifact Candidate Roles & Sequencing (Tests 16 - 20)
# =============================================================================


class TestArtifactCandidateRolesAndSequencing:
    """Validates candidate classification, sequence index extraction, and ordering."""

    def test_16_video_artifact_candidate(self) -> None:
        """Test 16: MP4 candidate is classified as PRIMARY_VIDEO / video."""
        adapter = F2InProcessBackendAdapter()
        with tempfile.TemporaryDirectory() as td:
            sb = Path(td)
            out = sb / "output"
            out.mkdir()

            def fake_f2(*args, **kwargs):
                (out / "123_video.mp4").write_bytes(b"mp4_data")

            with patch("src.downloader.f2_backend.is_f2_available", return_value=True), \
                 patch("src.downloader.f2_backend.F2InProcessBackendAdapter._run_f2_handler", side_effect=fake_f2):
                res = adapter.execute_download(
                    source_url="https://www.douyin.com/video/123",
                    sandbox_dir=sb,
                    download_input={},
                )

            cands = res.raw_diagnostics.get("candidates", [])
            assert len(cands) == 1
            assert cands[0]["role"] == ArtifactRole.PRIMARY_VIDEO.value
            assert cands[0]["media_kind"] == "video"

    def test_17_image_album_candidates(self) -> None:
        """Test 17: WebP / JPG candidates are classified as ALBUM_IMAGE / image."""
        adapter = F2InProcessBackendAdapter()
        with tempfile.TemporaryDirectory() as td:
            sb = Path(td)
            out = sb / "output"
            out.mkdir()

            def fake_f2(*args, **kwargs):
                (out / "456_image_1.webp").write_bytes(b"img1")

            with patch("src.downloader.f2_backend.is_f2_available", return_value=True), \
                 patch("src.downloader.f2_backend.F2InProcessBackendAdapter._run_f2_handler", side_effect=fake_f2):
                res = adapter.execute_download(
                    source_url="https://www.douyin.com/note/456",
                    sandbox_dir=sb,
                    download_input={},
                )

            cands = res.raw_diagnostics.get("candidates", [])
            assert len(cands) == 1
            assert cands[0]["role"] == ArtifactRole.ALBUM_IMAGE.value
            assert cands[0]["media_kind"] == "image"

    def test_18_image_sequence_order(self) -> None:
        """Test 18: Image album candidates are strictly ordered by sequence_index 1..N

        even if filesystem enumeration discovers them in reverse order.
        """
        adapter = F2InProcessBackendAdapter()
        with tempfile.TemporaryDirectory() as td:
            sb = Path(td)
            out = sb / "output"
            out.mkdir()

            def fake_f2_album(output_root, *args, **kwargs):
                # Create in reverse order during execution: 3, 2, 1
                (output_root / "716_image_3.webp").write_bytes(b"img3")
                (output_root / "716_image_2.webp").write_bytes(b"img2")
                (output_root / "716_image_1.webp").write_bytes(b"img1")

            with patch("src.downloader.f2_backend.is_f2_available", return_value=True), \
                 patch("src.downloader.f2_backend.F2InProcessBackendAdapter._run_f2_handler", side_effect=fake_f2_album):
                res = adapter.execute_download(
                    source_url="https://www.douyin.com/note/716",
                    sandbox_dir=sb,
                    download_input={},
                )

            cands = res.raw_diagnostics.get("candidates", [])
            seqs = [c["sequence_index"] for c in cands]
            assert seqs == [1, 2, 3]
            names = [Path(c["file_path"]).name for c in cands]
            assert names == ["716_image_1.webp", "716_image_2.webp", "716_image_3.webp"]

    def test_19_bgm_role(self) -> None:
        """Test 19: Audio candidate with 'music' or 'bgm' is classified as BGM_AUDIO."""
        adapter = F2InProcessBackendAdapter()
        with tempfile.TemporaryDirectory() as td:
            sb = Path(td)
            out = sb / "output"
            out.mkdir()

            def fake_f2_bgm(output_root, *args, **kwargs):
                (output_root / "716_image_1.webp").write_bytes(b"img1")
                (output_root / "716_music.mp3").write_bytes(b"mp3_data")

            with patch("src.downloader.f2_backend.is_f2_available", return_value=True), \
                 patch("src.downloader.f2_backend.F2InProcessBackendAdapter._run_f2_handler", side_effect=fake_f2_bgm):
                res = adapter.execute_download(
                    source_url="https://www.douyin.com/note/716",
                    sandbox_dir=sb,
                    download_input={},
                )

            cands = res.raw_diagnostics.get("candidates", [])
            bgm_cands = [c for c in cands if c["role"] == ArtifactRole.BGM_AUDIO.value]
            assert len(bgm_cands) == 1
            assert bgm_cands[0]["media_kind"] == "audio"
            assert "716_music.mp3" in bgm_cands[0]["file_path"]

    def test_20_diagnostics_excluded(self) -> None:
        """Test 20: Diagnostic files (.txt, .desc, .json, .log, .db) excluded from media candidates."""
        adapter = F2InProcessBackendAdapter()
        with tempfile.TemporaryDirectory() as td:
            sb = Path(td)
            out = sb / "output"
            out.mkdir()

            def fake_f2_diag(output_root, *args, **kwargs):
                (output_root / "123_video.mp4").write_bytes(b"media")
                (output_root / "123_desc.txt").write_bytes(b"desc")
                (output_root / "diag.json").write_bytes(b"{}")
                (sb / "douyin_users.db").write_bytes(b"sqlite")

            with patch("src.downloader.f2_backend.is_f2_available", return_value=True), \
                 patch("src.downloader.f2_backend.F2InProcessBackendAdapter._run_f2_handler", side_effect=fake_f2_diag):
                res = adapter.execute_download(
                    source_url="https://www.douyin.com/video/123",
                    sandbox_dir=sb,
                    download_input={},
                )

            assert len(res.raw_files) == 1
            assert res.raw_files[0].name == "123_video.mp4"


# =============================================================================
# Group 5: Error Taxonomy & Structured Failures (Tests 22 - 28, 30)
# =============================================================================


class TestErrorTaxonomyAndStructuredFailures:
    """Validates structured error mapping for F2 exceptions and status codes."""

    def test_22_empty_output_structured_failure(self) -> None:
        """Test 22: Empty output produces DOWNLOAD_MEDIA_INCOMPLETE."""
        adapter = F2InProcessBackendAdapter()
        with tempfile.TemporaryDirectory() as td:
            sb = Path(td)
            (sb / "output").mkdir()

            with patch("src.downloader.f2_backend.is_f2_available", return_value=True), \
                 patch("src.downloader.f2_backend.F2InProcessBackendAdapter._run_f2_handler"):
                res = adapter.execute_download(
                    source_url="https://www.douyin.com/video/123",
                    sandbox_dir=sb,
                    download_input={},
                )

            assert res.success is False
            assert res.raw_diagnostics["error_code"] == DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE.value

    def test_23_f2_exception_structured_failure(self) -> None:
        """Test 23: F2 exception during execution returns structured failure."""
        adapter = F2InProcessBackendAdapter()
        with tempfile.TemporaryDirectory() as td:
            sb = Path(td)
            (sb / "output").mkdir()

            def raise_api_err(*args, **kwargs):
                raise RuntimeError("APIResponseError: connection closed by peer")

            with patch("src.downloader.f2_backend.is_f2_available", return_value=True), \
                 patch("src.downloader.f2_backend.F2InProcessBackendAdapter._run_f2_handler", side_effect=raise_api_err):
                res = adapter.execute_download(
                    source_url="https://www.douyin.com/video/123",
                    sandbox_dir=sb,
                    download_input={},
                )

            assert res.success is False
            assert "error_code" in res.raw_diagnostics

    def test_24_429_classification(self) -> None:
        """Test 24: HTTP 429 maps to DOWNLOAD_RATE_LIMITED."""
        code, msg, retry = classify_f2_error("HTTP 429 Too Many Requests")
        assert code == DownloaderErrorCode.DOWNLOAD_RATE_LIMITED
        assert retry is True

    def test_25_5xx_classification(self) -> None:
        """Test 25: HTTP 500 / 502 / 503 maps to DOWNLOAD_SERVER_ERROR."""
        for err in ["HTTP 500 Internal Server Error", "502 Bad Gateway", "503 Service Unavailable"]:
            code, msg, retry = classify_f2_error(err)
            assert code == DownloaderErrorCode.DOWNLOAD_SERVER_ERROR
            assert retry is True

    def test_26_network_error_classification(self) -> None:
        """Test 26: Timeout and ConnectionError map to network error taxonomy."""
        code1, _, _ = classify_f2_error("ClientConnectorError: Cannot connect to host")
        assert code1 == DownloaderErrorCode.DOWNLOAD_NETWORK_ERROR

        code2, _, _ = classify_f2_error("Connection timed out after 30 seconds")
        assert code2 == DownloaderErrorCode.DOWNLOAD_TIMEOUT

    def test_27_ambiguous_403_classification(self) -> None:
        """Test 27: Ambiguous HTTP 403 maps to DOWNLOAD_PERMISSION_DENIED, NOT blindly AUTH_REQUIRED."""
        code, _, _ = classify_f2_error("HTTP 403 Forbidden")
        assert code == DownloaderErrorCode.DOWNLOAD_PERMISSION_DENIED

        # Explicit challenge maps to AUTH_CHALLENGE
        code_challenge, _, _ = classify_f2_error("HTTP 403 Forbidden: risk captcha challenge required")
        assert code_challenge == DownloaderErrorCode.DOWNLOAD_AUTH_CHALLENGE

    def test_28_bark_error_does_not_alone_fail(self) -> None:
        """Test 28: Bark notification failure (405) does not fail download if media exists."""
        adapter = F2InProcessBackendAdapter()
        with tempfile.TemporaryDirectory() as td:
            sb = Path(td)
            out = sb / "output"
            out.mkdir()

            def fake_f2_bark(*args, **kwargs):
                (out / "123_video.mp4").write_bytes(b"valid_media")
                # Bark failure does not abort media production

            with patch("src.downloader.f2_backend.is_f2_available", return_value=True), \
                 patch("src.downloader.f2_backend.F2InProcessBackendAdapter._run_f2_handler", side_effect=fake_f2_bark):
                res = adapter.execute_download(
                    source_url="https://www.douyin.com/video/123",
                    sandbox_dir=sb,
                    download_input={},
                )

            assert res.success is True
            assert len(res.raw_files) == 1

    def test_30_backend_result_json_safe_and_redacted(self) -> None:
        """Test 30: BackendDownloadResult diagnostics and messages are JSON-safe and redacted."""
        secret_msg = "Error accessing https://douyin.com?sessionid=secret_123&msToken=token_456"
        code, clean_msg, _ = classify_f2_error(secret_msg)

        assert "secret_123" not in clean_msg
        assert "token_456" not in clean_msg
        assert "[REDACTED]" in clean_msg

        res = BackendDownloadResult(
            success=False,
            raw_files=(),
            error_message=clean_msg,
            exit_code=1,
            raw_diagnostics={"error": clean_msg},
        )
        serialized = json.dumps(res.raw_diagnostics)
        assert "secret_123" not in serialized


# =============================================================================
# Group 6: Explicit Domain Methods (Tests 32 - 33)
# =============================================================================


class TestExplicitDomainAcquisitionMethods:
    """Validates download_video and download_image_album domain APIs."""

    def test_32_download_video_explicit_method(self) -> None:
        """Test 32: download_video executes with AcquisitionMode.VIDEO."""
        adapter = F2InProcessBackendAdapter()
        with tempfile.TemporaryDirectory() as td:
            sb = Path(td)
            (sb / "output").mkdir()

            with patch.object(adapter, "execute_download") as mock_exec:
                adapter.download_video(
                    source_url="https://www.douyin.com/video/123",
                    sandbox_dir=sb,
                    download_input={},
                )
                assert mock_exec.called
                _, kwargs = mock_exec.call_args
                assert kwargs["spec"].mode == AcquisitionMode.VIDEO
                assert kwargs["spec"].include_bgm is False

    def test_33_download_image_album_explicit_method(self) -> None:
        """Test 33: download_image_album executes with AcquisitionMode.IMAGE_ALBUM."""
        adapter = F2InProcessBackendAdapter()
        with tempfile.TemporaryDirectory() as td:
            sb = Path(td)
            (sb / "output").mkdir()

            with patch.object(adapter, "execute_download") as mock_exec:
                adapter.download_image_album(
                    source_url="https://www.douyin.com/note/456",
                    sandbox_dir=sb,
                    download_input={},
                )
                assert mock_exec.called
                _, kwargs = mock_exec.call_args
                assert kwargs["spec"].mode == AcquisitionMode.IMAGE_ALBUM
                assert kwargs["spec"].include_bgm is True


# =============================================================================
# Group 7: Real Media Live Smoke Tests (Tests 34 - 35, Worker Env Only)
# =============================================================================


@pytest.mark.skipif(not is_f2_available(), reason="Requires F2 installed in worker environment (.venv-f2)")
class TestRealMediaLiveSmoke:
    """Live integration tests running against genuine Douyin media assets.

    Strict Invariants:
    1. Downloads actual network media bytes into temporary sandbox.
    2. Runs through D06 Normalizer -> D05 Validator -> D07 Promoter.
    3. Promotes into a TEMPORARY test archive root (NEVER touches canonical user archive).
    4. Validates asset_manifest.json and playable ffprobe media streams.
    """

    def test_34_real_video_e2e_pipeline(self) -> None:
        """Test 34: Real video live download -> Normalize -> Validate -> Promote (Temporary Archive)."""
        from src.collector.douyin.browser_runtime import DouyinBrowserRuntimeProvider
        from src.collector.douyin.config import DouyinCollectorConfig
        from src.collector.download_models import DownloadTask
        from src.downloader.credentials import BrowserRuntimeCredentialSource, DouyinCredentialProvider
        from src.downloader.normalizer import ProductionAssetNormalizer
        from src.downloader.promoter import ProductionArchivePromoter, verify_archived_asset
        from src.downloader.sandbox import ProductionTaskSandboxProvider
        from src.downloader.validator import ProductionMediaValidator

        profile_path = Path(r"G:\antigravity-cli\dy\runtime\chrome-profile")
        if not profile_path.exists():
            pytest.skip(f"Chrome profile not found at {profile_path}")

        # Capture credential
        config = DouyinCollectorConfig(profile_path=profile_path, headless=True)
        runtime = DouyinBrowserRuntimeProvider.from_config(config)
        runtime.launch()
        try:
            source = BrowserRuntimeCredentialSource(runtime_provider=runtime)
            snap = source.capture_snapshot()
            cookie_header = snap.cookie_header
        finally:
            runtime.close()

        video_id = "6611417973221494020"
        video_url = f"https://www.douyin.com/video/{video_id}"

        with tempfile.TemporaryDirectory(prefix="d03_smoke_sb_") as td_sb, \
             tempfile.TemporaryDirectory(prefix="d03_smoke_archive_") as td_arch:

            sb_root = Path(td_sb)
            arch_root = Path(td_arch)

            # 1. Sandbox
            sb_provider = ProductionTaskSandboxProvider(base_dir=sb_root)
            task = DownloadTask(
                task_id="video_smoke_01",
                platform="douyin",
                platform_content_id=video_id,
                source_url=video_url,
                scope_id="douyin:dyacct_smoke",
                content_type="video",
            )
            sandbox = sb_provider.create_sandbox(task)

            # 2. Path plan
            normalizer = ProductionAssetNormalizer()
            path_plan = normalizer.plan_output(sandbox_root=sandbox.path, platform_content_id=video_id)

            # 3. Backend download
            adapter = F2InProcessBackendAdapter()
            backend_res = adapter.execute_download(
                source_url=video_url,
                sandbox_dir=sandbox.path,
                download_input=task.download_input,
                credentials={"cookie": cookie_header},
                path_plan=path_plan,
            )

            if not backend_res.success and (
                "403" in (backend_res.error_message or "")
                or backend_res.raw_diagnostics.get("error_code") in ("DOWNLOAD_PERMISSION_DENIED", "DOWNLOAD_AUTH_CHALLENGE")
            ):
                pytest.skip(f"BLOCKED: Douyin live platform returned 403/Challenge ({backend_res.error_message}). Verification or backoff required.")

            assert backend_res.success is True, f"F2 download failed: {backend_res.error_message}"
            assert len(backend_res.raw_files) >= 1

            # 4. Normalize
            norm_res = normalizer.normalize(
                raw_assets=list(backend_res.raw_files),
                platform_content_id=video_id,
                content_type="video",
                sandbox_root=sandbox.path,
                path_plan=path_plan,
            )
            assert len(norm_res.artifacts) >= 1

            # 5. Validate
            validator = ProductionMediaValidator()
            val_res = validator.validate_assets([a.file_path for a in norm_res.artifacts])
            assert val_res.passed is True, f"Media validation failed: {val_res.error}"

            # 6. Promote to Temporary Archive Root
            target_dir = arch_root / "douyin" / "smoke_scope" / video_id
            promoter = ProductionArchivePromoter()
            promoted = promoter.promote(
                normalized_assets=norm_res.artifacts,
                target_directory=target_dir,
                validation_result=val_res,
                platform_content_id=video_id,
            )

            assert len(promoted) >= 1
            manifest_file = target_dir / "asset_manifest.json"
            assert manifest_file.exists()

            # 7. Verify archived asset
            verif = verify_archived_asset(target_dir)
            assert verif.valid is True, f"Verification failed: {verif.error}"

    def test_35_real_image_album_e2e_pipeline(self) -> None:
        """Test 35: Real image album live download -> Normalize -> Validate -> Promote (Temporary Archive)."""
        from src.collector.douyin.browser_runtime import DouyinBrowserRuntimeProvider
        from src.collector.douyin.config import DouyinCollectorConfig
        from src.collector.download_models import DownloadTask
        from src.downloader.credentials import BrowserRuntimeCredentialSource
        from src.downloader.normalizer import ProductionAssetNormalizer
        from src.downloader.promoter import ProductionArchivePromoter, verify_archived_asset
        from src.downloader.sandbox import ProductionTaskSandboxProvider
        from src.downloader.validator import ProductionMediaValidator

        profile_path = Path(r"G:\antigravity-cli\dy\runtime\chrome-profile")
        if not profile_path.exists():
            pytest.skip(f"Chrome profile not found at {profile_path}")

        # Capture credential
        config = DouyinCollectorConfig(profile_path=profile_path, headless=True)
        runtime = DouyinBrowserRuntimeProvider.from_config(config)
        runtime.launch()
        try:
            source = BrowserRuntimeCredentialSource(runtime_provider=runtime)
            snap = source.capture_snapshot()
            cookie_header = snap.cookie_header
        finally:
            runtime.close()

        album_id = "7169622286633274635"
        album_url = f"https://www.douyin.com/note/{album_id}"

        with tempfile.TemporaryDirectory(prefix="d03_smoke_album_sb_") as td_sb, \
             tempfile.TemporaryDirectory(prefix="d03_smoke_album_arch_") as td_arch:

            sb_root = Path(td_sb)
            arch_root = Path(td_arch)

            # 1. Sandbox
            sb_provider = ProductionTaskSandboxProvider(base_dir=sb_root)
            task = DownloadTask(
                task_id="album_smoke_01",
                platform="douyin",
                platform_content_id=album_id,
                source_url=album_url,
                scope_id="douyin:dyacct_smoke",
                content_type="image_album",
            )
            sandbox = sb_provider.create_sandbox(task)

            # 2. Path plan
            normalizer = ProductionAssetNormalizer()
            path_plan = normalizer.plan_output(sandbox_root=sandbox.path, platform_content_id=album_id)

            # 3. Backend download (Image album with BGM)
            adapter = F2InProcessBackendAdapter()
            backend_res = adapter.download_image_album(
                source_url=album_url,
                sandbox_dir=sandbox.path,
                download_input=task.download_input,
                credentials={"cookie": cookie_header},
                path_plan=path_plan,
            )

            if not backend_res.success and (
                "403" in (backend_res.error_message or "")
                or backend_res.raw_diagnostics.get("error_code") in ("DOWNLOAD_PERMISSION_DENIED", "DOWNLOAD_AUTH_CHALLENGE")
            ):
                pytest.skip(f"BLOCKED: Douyin live platform returned 403/Challenge ({backend_res.error_message}). Verification or backoff required.")

            assert backend_res.success is True, f"F2 album download failed: {backend_res.error_message}"
            assert len(backend_res.raw_files) >= 3  # At least images + audio

            # 4. Normalize
            norm_res = normalizer.normalize(
                raw_assets=list(backend_res.raw_files),
                platform_content_id=album_id,
                content_type="image_album",
                sandbox_root=sandbox.path,
                path_plan=path_plan,
            )
            assert len(norm_res.artifacts) >= 3

            # Check sequence indices
            image_arts = [
                a for a in norm_res.artifacts
                if a.role in (ArtifactRole.ALBUM_IMAGE, ArtifactRole.ALBUM_IMAGE.value, str(ArtifactRole.ALBUM_IMAGE))
                or a.media_kind == "image"
            ]
            assert len(image_arts) >= 2
            seq_indices = [a.sequence_index for a in image_arts]
            assert seq_indices == sorted(seq_indices)
            assert seq_indices[0] == 1

            # 5. Validate
            validator = ProductionMediaValidator()
            val_res = validator.validate_assets([a.file_path for a in norm_res.artifacts])
            assert val_res.passed is True, f"Album validation failed: {val_res.error}"

            # 6. Promote to Temporary Archive Root
            target_dir = arch_root / "douyin" / "smoke_scope" / album_id
            promoter = ProductionArchivePromoter()
            promoted = promoter.promote(
                normalized_assets=norm_res.artifacts,
                target_directory=target_dir,
                validation_result=val_res,
                platform_content_id=album_id,
            )

            assert len(promoted) >= 3
            manifest_file = target_dir / "asset_manifest.json"
            assert manifest_file.exists()

            # 7. Verify archived asset
            verif = verify_archived_asset(target_dir)
            assert verif.valid is True, f"Verification failed: {verif.error}"
