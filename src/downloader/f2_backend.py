"""F2 In-Process Backend Adapter (DY-D03).

Implements:
1. Production DownloadBackend Port:
   - Implements DownloadBackend protocol from src.downloader.contracts.
   - Cleanly connects:
     DownloadTask -> CredentialContext -> TaskSandbox -> BackendOutputPlan
     -> F2 In-Process Backend -> Normalizer -> Validator -> Promoter.
2. In-Process Execution with Environment Isolation:
   - Runs exclusively within Dedicated Downloader Worker environment (.venv-f2).
   - Module-level lazy imports ensure importing this module in main .venv
     (which has NO F2 installed) does NOT trigger ModuleNotFoundError.
3. Strict Credential & Secret Boundary:
   - Pure in-memory credential injection from CredentialContext / reveal_for_backend().
   - Zero CLI argv (--cookie), zero os.environ secrets, zero temporary cookie files.
   - Active post-execution reference scrubbing (short lifetime, no leakage to subsequent tasks).
4. Application-Level Sandbox Containment & MAX_PATH Shield:
   - Strictly obeys BackendOutputPlan from D06 (folderize=False, flat output, short naming).
   - Sandbox-scoped working directory (cwd) ensures SQLite databases (douyin_users.db,
     douyin_videos.db) stay strictly within task sandbox.
   - Output candidates validated with verify_sandbox_containment(); outside files rejected.
   - Zero knowledge of or access to canonical archive root.
5. Media & Album Acquisition:
   - Video acquisition: PRIMARY_VIDEO (.mp4).
   - Image album acquisition: ALBUM_IMAGE (.webp/.jpg) with deterministic 1-based sequence_index,
     optional BGM_AUDIO (.mp3).
   - Bark notification suppression (enable_bark=False) preventing spurious 405 failures.
   - Dynamic msToken generation via TokenManager.gen_real_msToken() preventing stale token 403s.
6. Error Taxonomy & Structured Diagnostics:
   - Maps platform errors to QW-13 / D01 frozen DownloaderErrorCode taxonomy.
   - Ambiguous 403 mapped to DOWNLOAD_PERMISSION_DENIED (not blindly AUTH_REQUIRED).
   - All diagnostics scrubbed via scrub_secrets.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import enum
import inspect
import logging
import os
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from src.downloader.contracts import (
    BackendDownloadResult,
    DownloadBackend,
    DownloaderErrorCode,
    scrub_secrets,
)
from src.downloader.normalizer import (
    ArtifactCandidate,
    ArtifactRole,
    BackendOutputPlan,
    plan_backend_output,
    verify_sandbox_containment,
)

logger = logging.getLogger(__name__)

# Extensions considered media artifacts produced by F2
_MEDIA_EXTENSIONS = {
    ".mp4",
    ".webp",
    ".jpg",
    ".jpeg",
    ".png",
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
}

# Diagnostic and database extensions excluded from media candidates
_DIAGNOSTIC_EXTENSIONS = {
    ".db",
    ".db-journal",
    ".sqlite",
    ".txt",
    ".json",
    ".desc",
    ".log",
}


# =============================================================================
# 1. Environment & Version Helpers
# =============================================================================


def is_f2_available() -> bool:
    """Checks if the f2 package is importable in the current runtime environment."""
    try:
        import f2  # type: ignore # noqa: F401

        return True
    except (ImportError, ModuleNotFoundError):
        return False


def get_f2_version() -> str | None:
    """Returns the installed F2 version string, or None if F2 is not installed."""
    try:
        import f2  # type: ignore

        return getattr(f2, "__version__", None) or "0.0.1.7"
    except (ImportError, ModuleNotFoundError):
        return None


class F2EnvironmentError(RuntimeError):
    """Raised when F2 backend operations are attempted without F2 installed."""

    pass


# =============================================================================
# 2. Acquisition Specification & Enums
# =============================================================================


class AcquisitionMode(str, enum.Enum):
    """Execution mode for F2 backend acquisition."""

    AUTO = "AUTO"
    VIDEO = "VIDEO"
    IMAGE_ALBUM = "IMAGE_ALBUM"


@dataclass(frozen=True)
class BackendAcquisitionSpec:
    """Explicit acquisition parameter specification for F2 backend execution (D09 ready)."""

    mode: AcquisitionMode = AcquisitionMode.AUTO
    platform_content_id: str = ""
    source_url: str = ""
    download_input: dict[str, Any] = field(default_factory=dict)
    path_plan: BackendOutputPlan | None = None
    include_bgm: bool = True
    include_cover: bool = False
    include_desc: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value if isinstance(self.mode, AcquisitionMode) else str(self.mode),
            "platform_content_id": self.platform_content_id,
            "source_url": self.source_url,
            "path_plan": self.path_plan.to_dict() if self.path_plan else None,
            "include_bgm": self.include_bgm,
            "include_cover": self.include_cover,
            "include_desc": self.include_desc,
        }


# =============================================================================
# 3. Error Taxonomy Classifier
# =============================================================================


def classify_f2_error(error: Exception | str) -> tuple[DownloaderErrorCode, str, bool]:
    """Classifies F2 backend exceptions or error strings into DownloaderErrorCode.

    Returns:
        tuple[DownloaderErrorCode, clean_error_message, retryable_hint]
    """
    msg = scrub_secrets(str(error))
    msg_lower = msg.lower()

    # Rate limiting
    if "429" in msg_lower or "rate limit" in msg_lower or "too many requests" in msg_lower:
        return (DownloaderErrorCode.DOWNLOAD_RATE_LIMITED, msg, True)

    # Server errors (5xx)
    if any(code in msg_lower for code in ["500", "502", "503", "504", "internal server error", "bad gateway"]):
        return (DownloaderErrorCode.DOWNLOAD_SERVER_ERROR, msg, True)

    # Network / Timeout
    if any(k in msg_lower for k in ["timeout", "timed out", "connecttimeout", "readtimeout"]):
        return (DownloaderErrorCode.DOWNLOAD_TIMEOUT, msg, True)
    if any(k in msg_lower for k in ["connection", "connect", "dns", "unreachable", "ssl", "clientconnectorerror"]):
        return (DownloaderErrorCode.DOWNLOAD_NETWORK_ERROR, msg, True)

    # 403 HTTP status: distinguish auth challenge vs ambiguous permission denied
    if "403" in msg_lower or "forbidden" in msg_lower:
        challenge_markers = ["login", "verify", "captcha", "challenge", "risk", "security", "token expired"]
        if any(marker in msg_lower for marker in challenge_markers):
            return (DownloaderErrorCode.DOWNLOAD_AUTH_CHALLENGE, msg, False)
        return (DownloaderErrorCode.DOWNLOAD_PERMISSION_DENIED, msg, False)

    # 401 Unauthorized
    if "401" in msg_lower or "unauthorized" in msg_lower or "need login" in msg_lower:
        return (DownloaderErrorCode.DOWNLOAD_AUTH_REQUIRED, msg, False)

    # Not found / deleted
    if any(k in msg_lower for k in ["404", "not found", "deleted", "已被删除", "作品已被封禁", "private_status"]):
        return (DownloaderErrorCode.DOWNLOAD_NOT_FOUND, msg, False)

    # Missing environment / tool errors
    if isinstance(error, F2EnvironmentError) or "no module named 'f2'" in msg_lower:
        return (DownloaderErrorCode.DOWNLOAD_TOOL_ERROR, msg, False)

    # Generic tool error
    return (DownloaderErrorCode.DOWNLOAD_TOOL_ERROR, msg, False)


# =============================================================================
# 4. Helper for Running Coroutines Across Sync/Async Contexts
# =============================================================================


def _run_coroutine(coro: Any) -> Any:
    """Safely executes an async coroutine from either a synchronous thread

    or an active asyncio event loop thread without loop collision.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Active loop in current thread: execute in dedicated background thread
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(lambda: asyncio.run(coro))
            return future.result()
    else:
        return asyncio.run(coro)


# =============================================================================
# 5. Production F2 In-Process Backend Adapter
# =============================================================================


class F2InProcessBackendAdapter(DownloadBackend):
    """Production F2 In-Process Download Backend Adapter (DY-D03).

    Satisfies DownloadBackend protocol:
    execute_download(source_url, sandbox_dir, download_input, credentials) -> BackendDownloadResult
    """

    # Adapter-level execution lock to protect single-process F2 concurrency state
    _lock = threading.Lock()

    def __init__(self, auto_initialize: bool = False) -> None:
        self._initialized: bool = False
        if auto_initialize:
            self.initialize()

    def initialize(self) -> None:
        """Verifies F2 availability and prewarms imports if inside worker environment."""
        if not is_f2_available():
            raise F2EnvironmentError(
                "F2 backend cannot initialize: 'f2' package is not installed in current environment. "
                "F2 must run inside dedicated worker environment (e.g., .venv-f2)."
            )
        self._initialized = True

    # -------------------------------------------------------------------------
    # Core Download Port Implementation
    # -------------------------------------------------------------------------

    def execute_download(
        self,
        source_url: str,
        sandbox_dir: Path,
        download_input: dict[str, Any],
        credentials: dict[str, str] | Any = None,
        path_plan: BackendOutputPlan | None = None,
        spec: BackendAcquisitionSpec | None = None,
    ) -> BackendDownloadResult:
        """Executes in-process F2 download strictly inside the provided sandbox_dir.

        Guarantees:
        - Safe lazy-loading of F2 modules.
        - Strict sandbox containment: no file created outside sandbox_dir.
        - Zero knowledge of final archive root.
        - Pure in-memory credential injection (zero CLI argv, zero temp files).
        - Secret scrubbing of all diagnostics and logs.
        - Returns structured BackendDownloadResult with candidate artifact roles.
        """
        t0 = time.monotonic()

        # 1. Environment Preflight
        if not is_f2_available():
            err_msg = (
                "F2 is not available in the current Python environment. "
                "F2InProcessBackendAdapter must execute within the dedicated F2 worker environment."
            )
            return BackendDownloadResult(
                success=False,
                raw_files=(),
                error_message=err_msg,
                exit_code=1,
                raw_diagnostics={
                    "error_code": DownloaderErrorCode.DOWNLOAD_TOOL_ERROR.value,
                    "elapsed_sec": round(time.monotonic() - t0, 4),
                },
            )

        # 2. Validate Sandbox Directory
        sandbox_root = Path(sandbox_dir).resolve()
        if not sandbox_root.exists() or not sandbox_root.is_dir():
            return BackendDownloadResult(
                success=False,
                raw_files=(),
                error_message=f"Provided sandbox_dir '{sandbox_dir}' does not exist or is not a directory.",
                exit_code=1,
                raw_diagnostics={
                    "error_code": DownloaderErrorCode.DOWNLOAD_INVALID_INPUT.value,
                    "elapsed_sec": round(time.monotonic() - t0, 4),
                },
            )

        # 3. Resolve Content ID & Acquisition Plan
        content_id = ""
        if spec and spec.platform_content_id:
            content_id = spec.platform_content_id
        elif download_input and download_input.get("platform_content_id"):
            content_id = str(download_input["platform_content_id"])
        else:
            match = re.search(r"/(?:video|note)/(\d+)", source_url)
            if match:
                content_id = match.group(1)

        # Pre-download path plan (D06)
        active_plan = path_plan
        if active_plan is None and spec and spec.path_plan:
            active_plan = spec.path_plan
        if active_plan is None and download_input and isinstance(download_input.get("path_plan"), BackendOutputPlan):
            active_plan = download_input["path_plan"]
        if active_plan is None:
            active_plan = plan_backend_output(
                platform_content_id=content_id or "unnamed",
                sandbox_root=sandbox_root,
                allow_subdirectories=False,
            )

        output_root = active_plan.sandbox_output_root.resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        verify_sandbox_containment(output_root, sandbox_root)

        # 4. Resolve Credentials
        cookie_header = ""
        if credentials:
            if hasattr(credentials, "reveal_for_backend"):
                raw_creds = credentials.reveal_for_backend()
            elif hasattr(credentials, "to_f2_credentials"):
                raw_creds = credentials.to_f2_credentials()
            elif isinstance(credentials, dict):
                raw_creds = credentials
            else:
                raw_creds = {}

            if isinstance(raw_creds, dict):
                cookie_header = raw_creds.get("cookie", "") or raw_creds.get("Cookie", "")
                if not cookie_header and "cookies" in raw_creds:
                    # Construct cookie string if given cookie dict
                    cookie_items = raw_creds["cookies"]
                    if isinstance(cookie_items, dict):
                        cookie_header = "; ".join(f"{k}={v}" for k, v in cookie_items.items())
                    elif isinstance(cookie_items, list):
                        cookie_header = "; ".join(
                            f"{c.get('name')}={c.get('value')}" for c in cookie_items if "name" in c and "value" in c
                        )

        # 5. Snapshot Pre-Download Files in Sandbox
        pre_files = set(sandbox_root.rglob("*"))

        # 6. Execute F2 In-Process Under Adapter Lock & CWD Containment
        with self._lock:
            orig_cwd = os.getcwd()
            last_errors: list[Exception] = []

            try:
                # Direct CWD containment: SQLite databases stay inside task sandbox
                os.chdir(sandbox_root)

                # Determine Acquisition Mode
                is_album = False
                if spec and spec.mode == AcquisitionMode.IMAGE_ALBUM:
                    is_album = True
                elif spec and spec.mode == AcquisitionMode.VIDEO:
                    is_album = False
                elif "/note/" in source_url or download_input.get("content_type") == "image_album":
                    is_album = True

                try:
                    self._run_f2_handler(
                        output_root=output_root,
                        source_url=source_url,
                        cookie_header=cookie_header,
                        is_album=is_album,
                        spec=spec,
                        last_errors=last_errors,
                    )
                except Exception as exc:
                    last_errors.append(exc)
                    logger.warning("F2 handler execution raised unhandled exception: %s", exc)
            finally:
                # Restore original CWD
                os.chdir(orig_cwd)

                # Active secret scrubbing: delete local secret references immediately
                del cookie_header

                # Release any FileHandlers attached by F2 to prevent Windows file lock during sandbox cleanup
                try:
                    for name in list(logging.root.manager.loggerDict.keys()) + ["f2", ""]:
                        log = logging.getLogger(name)
                        for h in list(getattr(log, "handlers", [])):
                            if isinstance(h, logging.FileHandler):
                                try:
                                    h.close()
                                    log.removeHandler(h)
                                except Exception:
                                    pass
                except Exception:
                    pass

        # 7. Discover and Contain Newly Created Files
        post_files = set(sandbox_root.rglob("*"))
        new_files = [f for f in (post_files - pre_files) if f.is_file()]

        # Strict containment verification on ALL newly created files
        for f in new_files:
            verify_sandbox_containment(f, sandbox_root)

        # 8. Separate Media Candidates vs Diagnostics/Databases
        media_files: list[Path] = []
        diagnostic_files: list[Path] = []

        for f in new_files:
            ext = f.suffix.lower()
            if ext in _MEDIA_EXTENSIONS:
                media_files.append(f)
            else:
                diagnostic_files.append(f)

        # 9. Classify Candidate Roles and Sequence Indices
        candidates: list[ArtifactCandidate] = []
        raw_image_candidates: list[tuple[int, ArtifactCandidate]] = []

        for f in media_files:
            ext = f.suffix.lower()
            if ext in (".webp", ".jpg", ".jpeg", ".png"):
                # Image album candidate
                # Extract sequence index from F2 pattern e.g. <id>_image_1.webp or <id>_1.webp
                seq_idx = 1
                seq_match = re.search(r"[-_](?:image[-_])?(\d+)\.[a-zA-Z0-9]+$", f.name, re.IGNORECASE)
                if seq_match:
                    seq_idx = int(seq_match.group(1))
                cand = ArtifactCandidate(
                    file_path=f,
                    role=ArtifactRole.ALBUM_IMAGE,
                    media_kind="image",
                    sequence_index=seq_idx,
                    source_extension=ext,
                    backend_metadata={"aweme_id": content_id},
                )
                raw_image_candidates.append((seq_idx, cand))

            elif ext in (".mp3", ".m4a", ".aac", ".flac"):
                # Audio candidate (BGM or standalone audio)
                cand_role = ArtifactRole.BGM_AUDIO if ("music" in f.name.lower() or is_album) else ArtifactRole.PRIMARY_VIDEO
                candidates.append(
                    ArtifactCandidate(
                        file_path=f,
                        role=cand_role,
                        media_kind="audio",
                        sequence_index=None,
                        source_extension=ext,
                        backend_metadata={"aweme_id": content_id},
                    )
                )

            elif ext == ".mp4":
                # Video candidate
                candidates.append(
                    ArtifactCandidate(
                        file_path=f,
                        role=ArtifactRole.PRIMARY_VIDEO,
                        media_kind="video",
                        sequence_index=None,
                        source_extension=ext,
                        backend_metadata={"aweme_id": content_id},
                    )
                )

        # Sort image candidates strictly by sequence_index (Section V invariant!)
        raw_image_candidates.sort(key=lambda item: item[0])
        for _, cand in raw_image_candidates:
            candidates.append(cand)

        elapsed = round(time.monotonic() - t0, 4)

        # 10. Check Success Conditions
        if not candidates:
            # Failure: no valid media files produced
            if last_errors:
                primary_err = last_errors[-1]
                err_code, err_msg, retryable = classify_f2_error(primary_err)
            else:
                err_code = DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE
                err_msg = (
                    f"F2 execution completed without producing any media files in sandbox output. "
                    f"Discovered non-media files: {[f.name for f in diagnostic_files]}"
                )
                retryable = True

            return BackendDownloadResult(
                success=False,
                raw_files=(),
                error_message=scrub_secrets(err_msg),
                exit_code=1,
                raw_diagnostics={
                    "error_code": err_code.value,
                    "retryable": retryable,
                    "elapsed_sec": elapsed,
                    "diagnostic_files": [str(d.relative_to(sandbox_root)) for d in diagnostic_files],
                },
            )

        # Success: valid candidate artifacts produced
        ordered_files = tuple(c.file_path for c in candidates)
        return BackendDownloadResult(
            success=True,
            raw_files=ordered_files,
            error_message=None,
            exit_code=0,
            raw_diagnostics={
                "candidates": [
                    {
                        "file_path": str(c.file_path),
                        "role": str(c.role.value if isinstance(c.role, ArtifactRole) else c.role),
                        "media_kind": c.media_kind,
                        "sequence_index": c.sequence_index,
                        "source_extension": c.source_extension,
                    }
                    for c in candidates
                ],
                "candidate_count": len(candidates),
                "is_album": is_album,
                "elapsed_sec": elapsed,
            },
        )

    def _run_f2_handler(
        self,
        output_root: Path,
        source_url: str,
        cookie_header: str,
        is_album: bool,
        spec: BackendAcquisitionSpec | None,
        last_errors: list[Exception],
    ) -> None:
        """Executes F2 DouyinHandler in-process with dynamic token and flat output patches."""
        try:
            from f2.apps.douyin.crawler import DouyinCrawler  # type: ignore
            from f2.apps.douyin.filter import PostDetailFilter  # type: ignore
            from f2.apps.douyin.handler import DouyinHandler  # type: ignore
            from f2.apps.douyin.model import PostDetail  # type: ignore
            from f2.apps.douyin.utils import (  # type: ignore
                AwemeIdFetcher,
                ClientConfManager as DouyinClientConfManager,
                TokenManager,
            )
            from f2.exceptions import APIResponseError  # type: ignore
        except (ImportError, ModuleNotFoundError) as imp_err:
            last_errors.append(F2EnvironmentError(f"Cannot import F2 modules: {imp_err}"))
            return

        # Patch 1: Enforce flat sandbox output without user folder nesting
        async def _flat_user_data(self_h: Any, kwargs_h: dict, sec_user_id: str, db: Any) -> Path:
            target = Path(kwargs_h.get("path", str(output_root))).resolve()
            target.mkdir(parents=True, exist_ok=True)
            return target

        # Patch 2: Dynamic msToken generation per attempt + exception tracking
        async def _dynamic_fetch_one_video(self_h: Any, aweme_id: str) -> PostDetailFilter:
            logger.info("F2 executing dynamic fetch_one_video for aweme_id: %s", aweme_id)
            try:
                async with DouyinCrawler(self_h.kwargs) as crawler:
                    fresh_token = TokenManager.gen_real_msToken()
                    params = PostDetail(aweme_id=aweme_id, msToken=fresh_token)
                    response = await crawler.fetch_post_detail(params)
                    video_filter = PostDetailFilter(response)

                    if video_filter.nickname is None:
                        err = APIResponseError(
                            f"F2 fetch_one_video received empty detail response for aweme_id {aweme_id}. "
                            f"Filter list: {response.get('filter_list')}"
                        )
                        last_errors.append(err)
                        raise err
                    return video_filter
            except Exception as exc:
                last_errors.append(exc)
                raise

        # Patch 3: Direct regex Aweme ID resolution without unauthenticated HTTP redirect probes
        orig_get_aweme_id = getattr(AwemeIdFetcher, "get_aweme_id", None)

        @classmethod
        async def _direct_get_aweme_id(cls_h: Any, url_val: str) -> str:
            m = re.search(r"/(?:video|note)/(\d+)", str(url_val))
            if m:
                return m.group(1)
            if orig_get_aweme_id is not None:
                return await orig_get_aweme_id(url_val)
            return str(url_val)

        orig_get_user_data = getattr(DouyinHandler, "get_or_add_user_data", None)
        orig_fetch_one_video = getattr(DouyinHandler, "fetch_one_video", None)

        try:
            DouyinHandler.get_or_add_user_data = _flat_user_data
            DouyinHandler.fetch_one_video = _dynamic_fetch_one_video
            if orig_get_aweme_id is not None:
                AwemeIdFetcher.get_aweme_id = _direct_get_aweme_id

            include_bgm = spec.include_bgm if spec else is_album
            include_cover = spec.include_cover if spec else False
            include_desc = spec.include_desc if spec else False

            kwargs = {
                "app_name": "douyin",
                "url": source_url,
                "mode": "one",
                "path": str(output_root),
                "folderize": False,
                "music": include_bgm,
                "cover": include_cover,
                "desc": include_desc,
                "cookie": cookie_header,
                "headers": {
                    "User-Agent": DouyinClientConfManager.user_agent(),
                    "Referer": DouyinClientConfManager.referer(),
                },
                "naming": "{aweme_id}",
            }

            handler = DouyinHandler(kwargs)
            handler.enable_bark = False

            try:
                _run_coroutine(handler.handle_one_video())
            except Exception as exc:
                last_errors.append(exc)
                logger.warning("F2 handler execution raised exception: %s", exc)
            finally:
                if hasattr(handler, "kwargs") and isinstance(handler.kwargs, dict):
                    handler.kwargs.clear()
                kwargs.clear()
        finally:
            if orig_get_user_data is not None:
                DouyinHandler.get_or_add_user_data = orig_get_user_data
            if orig_fetch_one_video is not None:
                DouyinHandler.fetch_one_video = orig_fetch_one_video
            if orig_get_aweme_id is not None:
                AwemeIdFetcher.get_aweme_id = orig_get_aweme_id

    # -------------------------------------------------------------------------
    # Explicit Domain Acquisition Methods (D09 Ready)
    # -------------------------------------------------------------------------

    def download_video(
        self,
        source_url: str,
        sandbox_dir: Path,
        download_input: dict[str, Any],
        credentials: dict[str, str] | Any = None,
        path_plan: BackendOutputPlan | None = None,
    ) -> BackendDownloadResult:
        """Explicitly downloads a single video content item."""
        spec = BackendAcquisitionSpec(
            mode=AcquisitionMode.VIDEO,
            source_url=source_url,
            download_input=download_input,
            path_plan=path_plan,
            include_bgm=False,
            include_cover=False,
        )
        return self.execute_download(
            source_url=source_url,
            sandbox_dir=sandbox_dir,
            download_input=download_input,
            credentials=credentials,
            path_plan=path_plan,
            spec=spec,
        )

    def download_image_album(
        self,
        source_url: str,
        sandbox_dir: Path,
        download_input: dict[str, Any],
        credentials: dict[str, str] | Any = None,
        path_plan: BackendOutputPlan | None = None,
    ) -> BackendDownloadResult:
        """Explicitly downloads an image album with ordered images and BGM."""
        spec = BackendAcquisitionSpec(
            mode=AcquisitionMode.IMAGE_ALBUM,
            source_url=source_url,
            download_input=download_input,
            path_plan=path_plan,
            include_bgm=True,
            include_cover=False,
        )
        return self.execute_download(
            source_url=source_url,
            sandbox_dir=sandbox_dir,
            download_input=download_input,
            credentials=credentials,
            path_plan=path_plan,
            spec=spec,
        )
