"""SafeDouyinDownloader Orchestration Shell (DY-D01).

Implements the 8-stage state machine:
RECEIVED -> PREFLIGHT -> SANDBOX_READY -> DOWNLOADING -> NORMALIZING -> VALIDATING -> PROMOTING -> SUCCESS
(with stage cleanup on failure).

Invariants:
1. Strict Dependency Injection: all external operations decoupled via Protocols.
2. Secret Scrubbing Firewall: 100% credential redaction across messages and logs.
3. Sandbox Isolation: Every download executes inside an isolated TaskSandbox.
4. Atomic Archive Promotion: Files only reach canonical storage after successful validation.
5. Idempotent Bypass: Existing valid canonical assets return SKIPPED without downloading.
6. Zero f2 in Main Process: No direct f2 imports or native library bindings.
"""

from __future__ import annotations

import inspect
import logging
import time
from pathlib import Path
from typing import Any

from src.collector.download_models import DownloadTask
from src.downloader.contracts import (
    ArchivePromoter,
    AssetNormalizer,
    AssetStateInspector,
    ContentRouter,
    CredentialProvider,
    DownloadBackend,
    DownloadedAsset,
    DownloaderErrorCode,
    DownloaderStatus,
    DownloadErrorPolicy,
    DownloadResultContract,
    ExecutionStage,
    MediaValidator,
    TaskSandbox,
    TaskSandboxProvider,
    ValidationResult,
    scrub_secrets,
)
from src.downloader.credentials import CredentialProviderError
from src.downloader.router import UnsupportedContentTypeError
from src.downloader.validator import ValidationProfile
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

logger = logging.getLogger(__name__)


class SafeDouyinDownloader:
    """Production orchestration shell for Douyin media downloads (DY-D01)."""

    def __init__(
        self,
        credential_provider: CredentialProvider | None = None,
        backend: DownloadBackend | None = None,
        sandbox_provider: TaskSandboxProvider | None = None,
        validator: MediaValidator | None = None,
        normalizer: AssetNormalizer | None = None,
        promoter: ArchivePromoter | None = None,
        error_policy: DownloadErrorPolicy | None = None,
        router: ContentRouter | None = None,
        asset_inspector: AssetStateInspector | None = None,
        require_auth: bool = False,
    ) -> None:
        self.credential_provider = credential_provider or NullCredentialProvider()
        self.backend = backend or FakeDownloadBackend()
        self.sandbox_provider = sandbox_provider or DefaultTaskSandboxProvider()
        self.validator = validator or FakeMediaValidator()
        self.normalizer = normalizer or FakeAssetNormalizer()
        self.promoter = promoter or FakeArchivePromoter()
        self.error_policy = error_policy or DefaultDownloadErrorPolicy()
        self.router = router or FakeContentRouter()
        self.asset_inspector = asset_inspector or FakeAssetStateInspector()
        self.require_auth = require_auth

    def execute(self, task: DownloadTask) -> DownloadResultContract:
        """Executes a DownloadTask through the 8-stage state machine."""
        t0 = time.monotonic()
        stage = ExecutionStage.RECEIVED
        sandbox: TaskSandbox | None = None

        try:
            # =================================================================
            # Stage 1: RECEIVED - Input & Idempotency Validation
            # =================================================================
            stage = ExecutionStage.RECEIVED

            # 1.1 Platform validation
            if not task.platform or task.platform.lower() != "douyin":
                return self._build_failure(
                    task=task,
                    stage=stage,
                    error_code=DownloaderErrorCode.DOWNLOAD_INVALID_INPUT,
                    message=f"Unsupported platform '{task.platform}'. Expected 'douyin'.",
                    retryable=False,
                    t0=t0,
                )

            # 1.2 URL validation
            if not task.source_url or not any(d in task.source_url.lower() for d in ["douyin.com", "iesdouyin.com"]):
                return self._build_failure(
                    task=task,
                    stage=stage,
                    error_code=DownloaderErrorCode.DOWNLOAD_INVALID_INPUT,
                    message=f"Invalid or illegal Douyin URL: '{task.source_url}'.",
                    retryable=False,
                    t0=t0,
                )

            # 1.3 Content ID validation
            if not task.platform_content_id or not task.platform_content_id.isalnum():
                return self._build_failure(
                    task=task,
                    stage=stage,
                    error_code=DownloaderErrorCode.DOWNLOAD_INVALID_INPUT,
                    message=f"Invalid platform_content_id: '{task.platform_content_id}'.",
                    retryable=False,
                    t0=t0,
                )

            # 1.4 Route & Execution Planning (DY-D09)
            plan = None
            if hasattr(self.router, "plan_execution"):
                try:
                    plan = self.router.plan_execution(task)
                except UnsupportedContentTypeError as uce:
                    return self._build_failure(
                        task=task,
                        stage=stage,
                        error_code=DownloaderErrorCode.DOWNLOAD_UNSUPPORTED_CONTENT,
                        message=str(uce),
                        retryable=False,
                        t0=t0,
                    )
            elif getattr(task, "content_type", None) not in ("video", "image_album", None):
                return self._build_failure(
                    task=task,
                    stage=stage,
                    error_code=DownloaderErrorCode.DOWNLOAD_UNSUPPORTED_CONTENT,
                    message=f"Unsupported content_type '{task.content_type}'.",
                    retryable=False,
                    t0=t0,
                )

            # 1.5 Target Directory Resolution & Idempotency Bypass
            # D07 ArchivePromoter is the sole authority for formal archive destination layout.
            # Physical asset identity is (platform, platform_content_id) - NOT bound to scope.
            if hasattr(self.promoter, "resolve_canonical_destination"):
                target_dir = self.promoter.resolve_canonical_destination(
                    platform=task.platform,
                    platform_content_id=task.platform_content_id,
                )
            elif hasattr(self.router, "resolve_target_directory"):
                # Backward compatibility with D01 test stubs (e.g. FakeContentRouter)
                target_dir = self.router.resolve_target_directory(
                    platform=task.platform,
                    scope_id=task.scope_id,
                    platform_content_id=task.platform_content_id,
                )
            else:
                target_dir = Path("archive") / task.platform / task.platform_content_id
            is_valid, existing_assets = self.asset_inspector.inspect_assets(
                target_directory=target_dir,
                platform_content_id=task.platform_content_id,
            )
            if is_valid and existing_assets:
                elapsed = time.monotonic() - t0
                return DownloadResultContract(
                    source_url=task.source_url,
                    platform_content_id=task.platform_content_id,
                    task_id=task.task_id,
                    scope_id=task.scope_id,
                    status=DownloaderStatus.SKIPPED,
                    message="Target asset already exists in canonical storage and passed validation (idempotent bypass).",
                    assets=existing_assets,
                    validation={"passed": True, "cached": True},
                    elapsed_sec=elapsed,
                    stage=ExecutionStage.SUCCESS,
                )

            # =================================================================
            # Stage 2: PREFLIGHT - Authentication Check
            # =================================================================
            stage = ExecutionStage.PREFLIGHT
            cred_ctx: Any = None
            credentials: dict[str, Any] | None = None

            if hasattr(self.credential_provider, "acquire") and not isinstance(
                self.credential_provider, NullCredentialProvider
            ):
                try:
                    cred_ctx = self.credential_provider.acquire(task)
                    credentials = cred_ctx.reveal_for_backend() if hasattr(cred_ctx, "reveal_for_backend") else cred_ctx.get_credentials()
                except CredentialProviderError as cpe:
                    if self.require_auth:
                        return self._build_failure(
                            task=task,
                            stage=stage,
                            error_code=cpe.error_code,
                            message=str(cpe),
                            retryable=False,
                            t0=t0,
                        )
                except Exception as exc:
                    if self.require_auth:
                        return self._build_failure(
                            task=task,
                            stage=stage,
                            error_code=DownloaderErrorCode.DOWNLOAD_CREDENTIAL_BRIDGE_FAILED,
                            message=f"Credential acquisition error: {exc}",
                            retryable=False,
                            t0=t0,
                        )
            else:
                credentials = self.credential_provider.get_credentials(task.scope_id)

            if self.require_auth and not credentials:
                return self._build_failure(
                    task=task,
                    stage=stage,
                    error_code=DownloaderErrorCode.DOWNLOAD_AUTH_REQUIRED,
                    message=f"Pre-flight authentication check failed: no credentials available for scope '{task.scope_id}'.",
                    retryable=False,
                    t0=t0,
                )

            # =================================================================
            # Stage 3: SANDBOX_READY - Isolated Workspace Provisioning
            # =================================================================
            stage = ExecutionStage.SANDBOX_READY
            sandbox = self.sandbox_provider.create_sandbox(task)

            # Pre-download path planning (D06 -> D03)
            path_plan = None
            if hasattr(self.normalizer, "plan_output"):
                try:
                    path_plan = self.normalizer.plan_output(
                        sandbox_root=sandbox.path,
                        platform_content_id=task.platform_content_id,
                        execution_id=getattr(sandbox, "execution_id", task.task_id or ""),
                    )
                except Exception:
                    path_plan = None

            # =================================================================
            # Stage 4: DOWNLOADING - Driver Subprocess Execution
            # =================================================================
            stage = ExecutionStage.DOWNLOADING
            try:
                backend_kwargs: dict[str, Any] = {
                    "source_url": task.source_url,
                    "sandbox_dir": sandbox.path,
                    "download_input": task.download_input,
                    "credentials": credentials,
                }
                backend_fn = getattr(self.backend.execute_download, "side_effect", None) or self.backend.execute_download
                try:
                    b_sig = inspect.signature(backend_fn)
                    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in b_sig.parameters.values()) or "path_plan" in b_sig.parameters:
                        backend_kwargs["path_plan"] = path_plan
                    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in b_sig.parameters.values()) or "spec" in b_sig.parameters:
                        if plan and hasattr(plan, "backend_spec") and plan.backend_spec:
                            backend_kwargs["spec"] = plan.backend_spec
                except (ValueError, TypeError):
                    pass

                backend_res = self.backend.execute_download(**backend_kwargs)
            finally:
                if cred_ctx is not None and hasattr(cred_ctx, "close"):
                    cred_ctx.close()

            if not backend_res.success:
                raw_err = backend_res.error_message or f"Backend process failed with exit code {backend_res.exit_code}"
                classification = self.error_policy.classify_error(raw_err)
                return self._build_failure(
                    task=task,
                    stage=stage,
                    error_code=classification.error_code,
                    message=raw_err,
                    retryable=classification.retryable,
                    retry_after=classification.retry_after,
                    t0=t0,
                )

            if not backend_res.raw_files:
                classification = self.error_policy.classify_error("zero files produced by download backend")
                return self._build_failure(
                    task=task,
                    stage=stage,
                    error_code=classification.error_code,
                    message="Download backend reported success but produced 0 files.",
                    retryable=classification.retryable,
                    retry_after=classification.retry_after,
                    t0=t0,
                )

            # =================================================================
            # Stage 5: NORMALIZING - Asset Naming & Sidecar Formatting
            # =================================================================
            stage = ExecutionStage.NORMALIZING
            norm_kwargs: dict[str, Any] = {
                "raw_assets": list(backend_res.raw_files),
                "platform_content_id": task.platform_content_id,
                "content_type": task.content_type,
                "metadata_hint": task.download_input,
            }
            norm_fn = getattr(self.normalizer.normalize, "side_effect", None) or self.normalizer.normalize
            try:
                n_sig = inspect.signature(norm_fn)
                if "sandbox_root" in n_sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in n_sig.parameters.values()):
                    norm_kwargs["sandbox_root"] = sandbox.path
                if "path_plan" in n_sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in n_sig.parameters.values()):
                    norm_kwargs["path_plan"] = path_plan
            except (ValueError, TypeError):
                pass

            normalized_assets = self.normalizer.normalize(**norm_kwargs)

            if not normalized_assets:
                return self._build_failure(
                    task=task,
                    stage=stage,
                    error_code=DownloaderErrorCode.DOWNLOAD_TOOL_ERROR,
                    message="Asset normalization yielded empty asset list.",
                    retryable=False,
                    t0=t0,
                )

            # =================================================================
            # Stage 6: VALIDATING - Integrity & Container Verification
            # =================================================================
            stage = ExecutionStage.VALIDATING

            # DY-D09: Pre-validation artifact conformity check
            if plan is not None and hasattr(self.router, "validate_artifact_conformity"):
                conformity = self.router.validate_artifact_conformity(plan, normalized_assets)
                if not conformity.passed:
                    return self._build_failure(
                        task=task,
                        stage=stage,
                        error_code=conformity.error_code or DownloaderErrorCode.DOWNLOAD_MEDIA_INCOMPLETE,
                        message=conformity.reason or "Artifact-route conformity check failed.",
                        retryable=False,
                        t0=t0,
                    )

            # DY-D09: Bind explicit validation profile from plan (never AUTO in production)
            val_profile = plan.validation_profile if plan else ValidationProfile.VIDEO
            try:
                val_result = self.validator.validate_assets(
                    [na.file_path for na in normalized_assets],
                    profile=val_profile,
                )
            except TypeError:
                val_result = self.validator.validate_assets([na.file_path for na in normalized_assets])
            if not val_result.passed:
                val_err = val_result.error or "Media validation failed: corrupt container or missing required streams"
                classification = self.error_policy.classify_error(val_err)
                return self._build_failure(
                    task=task,
                    stage=stage,
                    error_code=classification.error_code,
                    message=val_err,
                    retryable=classification.retryable,
                    retry_after=classification.retry_after,
                    validation=val_result.to_dict(),
                    t0=t0,
                )

            # =================================================================
            # Stage 7: PROMOTING - Atomic Filesystem Rename to Archive
            # =================================================================
            stage = ExecutionStage.PROMOTING
            target_fn = getattr(self.promoter.promote, "side_effect", None) or self.promoter.promote
            accepts_extra = False
            try:
                sig = inspect.signature(target_fn)
                accepts_extra = (
                    any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
                    or "validation_result" in sig.parameters
                )
            except (ValueError, TypeError):
                accepts_extra = False

            if accepts_extra:
                promoted_assets = self.promoter.promote(
                    normalized_assets=normalized_assets,
                    target_directory=target_dir,
                    validation_result=val_result,
                    platform=task.platform,
                    platform_content_id=task.platform_content_id,
                    task_id=task.task_id,
                    scope_id=task.scope_id,
                    sandbox_root=sandbox.path,
                )
            else:
                promoted_assets = self.promoter.promote(
                    normalized_assets=normalized_assets,
                    target_directory=target_dir,
                )

            if not promoted_assets:
                return self._build_failure(
                    task=task,
                    stage=stage,
                    error_code=DownloaderErrorCode.DOWNLOAD_VALIDATION_FAILED,
                    message="Archive promotion yielded empty asset list.",
                    retryable=False,
                    t0=t0,
                )

            # =================================================================
            # Stage 8: SUCCESS - Contract Finalization
            # =================================================================
            stage = ExecutionStage.SUCCESS
            elapsed = time.monotonic() - t0
            return DownloadResultContract(
                source_url=task.source_url,
                platform_content_id=task.platform_content_id,
                task_id=task.task_id,
                scope_id=task.scope_id,
                status=DownloaderStatus.SUCCESS,
                message="Download, validation, and archive promotion succeeded.",
                assets=promoted_assets,
                validation=val_result.to_dict(),
                elapsed_sec=elapsed,
                stage=stage,
            )

        except Exception as exc:
            classification = self.error_policy.classify_error(exc)
            return self._build_failure(
                task=task,
                stage=stage,
                error_code=classification.error_code,
                message=f"Unexpected exception during {stage.value}: {exc}",
                retryable=classification.retryable,
                retry_after=classification.retry_after,
                t0=t0,
            )

        finally:
            if sandbox is not None:
                try:
                    if stage == ExecutionStage.SUCCESS:
                        if hasattr(sandbox, "finalize_success"):
                            sandbox.finalize_success()
                    else:
                        if hasattr(sandbox, "finalize_failure"):
                            sandbox.finalize_failure(reason=f"Failed at {stage.value}")
                except Exception as fin_err:
                    logger.warning("Failed to finalize sandbox at %s: %s", getattr(sandbox, "path", sandbox), fin_err)
                try:
                    sandbox.cleanup()
                except Exception as clean_err:
                    logger.warning("Failed to clean up sandbox at %s: %s", getattr(sandbox, "path", sandbox), clean_err)

    def _build_failure(
        self,
        task: DownloadTask,
        stage: ExecutionStage,
        error_code: DownloaderErrorCode,
        message: str,
        retryable: bool,
        t0: float,
        retry_after: int | None = None,
        validation: dict[str, Any] | None = None,
    ) -> DownloadResultContract:
        """Constructs a sanitized failure contract."""
        elapsed = time.monotonic() - t0
        return DownloadResultContract(
            source_url=task.source_url,
            platform_content_id=task.platform_content_id,
            task_id=task.task_id,
            scope_id=task.scope_id,
            status=DownloaderStatus.FAILED,
            error_code=error_code.value,
            retryable=retryable,
            retry_after=retry_after,
            message=scrub_secrets(message),
            validation=validation or {},
            elapsed_sec=elapsed,
            stage=stage,
        )
