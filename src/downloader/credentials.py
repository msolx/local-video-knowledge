"""In-Process Ephemeral Credential Provider for Douyin Downloader (DY-D02).

Authoritative Security Chain:
Dedicated Authenticated Chromium
        │
        ▼
Credential Snapshot (Allowlisted Browser RPC / Sidecar)
        │
        ▼
D02 DouyinCredentialProvider (Scope Binding & Auth Gating)
        │
        ▼
Ephemeral In-Memory CredentialContext (Zero Persist, Scoped Lifecycle)
        │
        ▼
D03 F2 In-Process Backend (Direct Python kwargs Injection)
        │
        ▼
Discard (Reference Clearing & Ephemeral Invalidation)

Core Invariants:
1. Main environment (G:/local_pc_project/.venv) remains 100% F2-independent.
2. download-task-v1 remains strictly secret-free (zero credential fields in DownloadTask).
3. Primary extraction path is live authenticated browser context/sidecar; no direct SQLite Cookie DB reading.
4. Zero plaintext persistence: no temp cookie files, YAML/JSON dumps, SQLite cache, or secret logging.
5. No --cookie in argv and no COOKIE in environment variables.
6. Safe __repr__ and __str__ suppressing cookie values; non-serializable by default.
7. Strict account scope binding: task.scope_id == credential.account_scope_id reusing C03 fingerprint derivation.
8. Auth readiness gating: AUTH_VALID required; AUTH_REQUIRED, AUTH_CHALLENGE, AUTH_UNCERTAIN rejected.
"""

from __future__ import annotations

import collections.abc
import datetime
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Protocol, runtime_checkable

from ..collector.douyin.auth_state import (
    AuthState,
    DouyinAuthStateDetector,
    generate_account_scope_id,
)
from ..collector.download_models import DownloadTask
from .contracts import (
    CredentialProvider,
    DownloaderErrorCode,
    scrub_secrets,
)

logger = logging.getLogger("downloader.credentials")


class DownloaderError(Exception):
    """Base exception for downloader subsystem errors."""
    pass

# Allowlisted base domains strictly permitted for Douyin credentials
ALLOWED_DOUYIN_DOMAINS: frozenset[str] = frozenset({
    "douyin.com",
    "iesdouyin.com",
})


def is_allowed_douyin_domain(domain: str | None) -> bool:
    """Verifies that a cookie domain strictly belongs to Douyin allowlisted domains."""
    if not domain:
        return False
    clean = domain.lower().strip()
    if clean.startswith("."):
        clean = clean[1:]
    return any(clean == allowed or clean.endswith("." + allowed) for allowed in ALLOWED_DOUYIN_DOMAINS)


# =============================================================================
# 1. Error Taxonomy
# =============================================================================


class CredentialProviderError(DownloaderError):
    """Base exception for all credential provider operations."""

    def __init__(
        self,
        message: str,
        error_code: DownloaderErrorCode = DownloaderErrorCode.DOWNLOAD_CREDENTIAL_BRIDGE_FAILED,
        subreason: str = "CREDENTIAL_ERROR",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(scrub_secrets(message))
        self.error_code = error_code
        self.subreason = subreason
        self.details = details or {}


class CredentialScopeMismatchError(CredentialProviderError):
    """Raised when credential account scope does not match the requested task scope."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code=DownloaderErrorCode.DOWNLOAD_AUTH_REQUIRED,
            subreason="ACCOUNT_SCOPE_MISMATCH",
            details=details,
        )


class CredentialAuthInvalidError(CredentialProviderError):
    """Raised when authentication state is not AUTH_VALID (e.g. login required or captcha challenge)."""

    def __init__(
        self,
        message: str,
        auth_state: str,
        error_code: DownloaderErrorCode = DownloaderErrorCode.DOWNLOAD_AUTH_REQUIRED,
        subreason: str = "AUTH_STATE_INVALID",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message=message,
            error_code=error_code,
            subreason=subreason,
            details=details,
        )
        self.auth_state = auth_state


class CredentialSourceUnavailableError(CredentialProviderError):
    """Raised when browser runtime or sidecar credential source is unreachable or inactive."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code=DownloaderErrorCode.DOWNLOAD_CREDENTIAL_BRIDGE_FAILED,
            subreason="CREDENTIAL_SOURCE_UNAVAILABLE",
            details=details,
        )


class CredentialEmptyError(CredentialProviderError):
    """Raised when credential snapshot returns zero valid allowlisted cookies."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code=DownloaderErrorCode.DOWNLOAD_CREDENTIAL_BRIDGE_FAILED,
            subreason="EMPTY_CREDENTIAL_SET",
            details=details,
        )


class CredentialExpiredError(CredentialProviderError):
    """Raised when accessing an ephemeral CredentialContext after it has been closed."""

    def __init__(
        self,
        message: str = "CredentialContext has expired or been closed",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message=message,
            error_code=DownloaderErrorCode.DOWNLOAD_CREDENTIAL_BRIDGE_FAILED,
            subreason="CREDENTIAL_CONTEXT_EXPIRED",
            details=details,
        )


# =============================================================================
# 2. Credential Cookie Model
# =============================================================================


class CredentialCookie:
    """Immutable individual cookie entity with strict secret masking."""

    __slots__ = (
        "_name",
        "_value",
        "_domain",
        "_path",
        "_expires",
        "_http_only",
        "_secure",
        "_same_site",
    )

    def __init__(
        self,
        name: str,
        value: str,
        domain: str,
        path: str = "/",
        expires: float | None = None,
        http_only: bool = False,
        secure: bool = False,
        same_site: str | None = None,
    ) -> None:
        self._name = str(name).strip()
        self._value = str(value)
        self._domain = str(domain).strip().lower()
        self._path = str(path).strip() or "/"
        self._expires = float(expires) if expires is not None else None
        self._http_only = bool(http_only)
        self._secure = bool(secure)
        self._same_site = str(same_site) if same_site else None

    @property
    def name(self) -> str:
        return self._name

    @property
    def value(self) -> str:
        """Raw secret value. Strictly accessible in-memory only."""
        return self._value

    @property
    def domain(self) -> str:
        return self._domain

    @property
    def path(self) -> str:
        return self._path

    @property
    def expires(self) -> float | None:
        return self._expires

    @property
    def http_only(self) -> bool:
        return self._http_only

    @property
    def secure(self) -> bool:
        return self._secure

    @property
    def same_site(self) -> str | None:
        return self._same_site

    @property
    def is_expired(self) -> bool:
        if self._expires is None:
            return False
        return time.time() > self._expires

    def to_dict(self, include_secret: bool = False) -> dict[str, Any]:
        """Convert to dict with secret redacted by default."""
        return {
            "name": self._name,
            "domain": self._domain,
            "path": self._path,
            "expires": self._expires,
            "http_only": self._http_only,
            "secure": self._secure,
            "same_site": self._same_site,
            "value": self._value if include_secret else "[REDACTED]",
        }

    def __repr__(self) -> str:
        return (
            f"CredentialCookie(name={self._name!r}, domain={self._domain!r}, "
            f"path={self._path!r}, http_only={self._http_only}, secure={self._secure}, "
            f"expires={self._expires}, value='[REDACTED]')"
        )

    def __str__(self) -> str:
        return f"{self._name}=[REDACTED]@{self._domain}"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CredentialCookie):
            return False
        return (
            self._name == other._name
            and self._domain == other._domain
            and self._path == other._path
            and self._value == other._value
        )

    def __hash__(self) -> int:
        return hash((self._name, self._domain, self._path))


# =============================================================================
# 3. Credential Snapshot
# =============================================================================


class CredentialSnapshot:
    """Strongly-typed snapshot of authenticated credential material extracted from runtime.

    Non-serializable by default to prevent accidental plaintext dumps into logs,
    JSON files, or persistent SQLite records.
    """

    __slots__ = (
        "_account_scope_id",
        "_auth_state",
        "_acquired_at",
        "_source",
        "_metadata",
        "_cookies",
        "_cookie_map",
        "_cookie_header",
    )

    def __init__(
        self,
        account_scope_id: str,
        auth_state: str,
        cookies: Iterable[CredentialCookie],
        acquired_at: str | None = None,
        source: str = "browser_runtime",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._account_scope_id = str(account_scope_id).strip()
        self._auth_state = str(auth_state).strip()
        self._acquired_at = acquired_at or datetime.datetime.now(datetime.timezone.utc).isoformat()
        self._source = str(source).strip()
        self._metadata = dict(metadata or {})

        # Domain filtering: Strictly exclude non-Douyin cookies
        filtered: list[CredentialCookie] = [
            c for c in cookies if is_allowed_douyin_domain(c.domain)
        ]
        # Sort for deterministic representation
        filtered.sort(key=lambda c: (c.name, c.domain))
        self._cookies: tuple[CredentialCookie, ...] = tuple(filtered)
        self._cookie_map: dict[str, str] = {c.name: c.value for c in self._cookies}
        self._cookie_header: str = "; ".join(f"{c.name}={c.value}" for c in self._cookies)

    @property
    def account_scope_id(self) -> str:
        return self._account_scope_id

    @property
    def auth_state(self) -> str:
        return self._auth_state

    @property
    def acquired_at(self) -> str:
        return self._acquired_at

    @property
    def source(self) -> str:
        return self._source

    @property
    def metadata(self) -> dict[str, Any]:
        return dict(self._metadata)

    @property
    def cookies(self) -> tuple[CredentialCookie, ...]:
        return self._cookies

    @property
    def cookie_map(self) -> dict[str, str]:
        """In-memory dictionary mapping cookie name to raw value."""
        return dict(self._cookie_map)

    @property
    def cookie_header(self) -> str:
        """Combined Cookie header string for HTTP transmission."""
        return self._cookie_header

    @property
    def is_valid(self) -> bool:
        return self._auth_state == AuthState.AUTH_VALID.value and len(self._cookies) > 0

    def summary(self) -> dict[str, Any]:
        """Safe, non-secret summary suitable for audit logs and telemetry."""
        domains = sorted(list({c.domain for c in self._cookies}))
        names = [c.name for c in self._cookies]
        expirations = [c.expires for c in self._cookies if c.expires is not None]
        min_exp = min(expirations) if expirations else None
        max_exp = max(expirations) if expirations else None
        return {
            "account_scope_id": self._account_scope_id,
            "auth_state": self._auth_state,
            "acquired_at": self._acquired_at,
            "source": self._source,
            "cookie_count": len(self._cookies),
            "cookie_names": names,
            "domains": domains,
            "min_expiry_epoch": min_exp,
            "max_expiry_epoch": max_exp,
            "metadata": self._metadata,
        }

    def __repr__(self) -> str:
        return (
            f"<CredentialSnapshot scope={self._account_scope_id!r} auth_state={self._auth_state!r} "
            f"cookies_count={len(self._cookies)} acquired_at={self._acquired_at!r}>"
        )

    def __str__(self) -> str:
        return f"CredentialSnapshot(scope={self._account_scope_id}, auth={self._auth_state}, count={len(self._cookies)})"


# =============================================================================
# 4. Ephemeral Credential Context
# =============================================================================


class CredentialContext:
    """Scoped, ephemeral in-memory credential context delivered to download execution.

    By default, CredentialContext is NOT a Mapping and NOT generically serializable
    (dict(ctx) and json.dumps(ctx) are disallowed to prevent accidental secret leakage).
    The ONLY approved ways to export sensitive secrets to downstream backend adapters are:
    - `ctx.to_f2_credentials()`
    - `ctx.reveal_for_backend()`

    Lifecycle is strictly governed via context manager (`with credential_provider.acquire(task) as ctx:`).
    Exiting the context executes `close()`, invalidates internal references, and forbids
    subsequent credential access (raising CredentialExpiredError).
    """

    __slots__ = (
        "_snapshot",
        "_account_scope_id",
        "_auth_state",
        "_acquired_at",
        "_is_active",
        "_cookies",
        "_cookie_map",
        "_cookie_header",
        "_cookie_count",
    )

    def __init__(self, snapshot: CredentialSnapshot) -> None:
        self._snapshot: CredentialSnapshot | None = snapshot
        self._account_scope_id = snapshot.account_scope_id
        self._auth_state = snapshot.auth_state
        self._acquired_at = snapshot.acquired_at
        self._is_active = True
        self._cookies: tuple[CredentialCookie, ...] = snapshot.cookies
        self._cookie_map: dict[str, str] = snapshot.cookie_map
        self._cookie_header: str = snapshot.cookie_header
        self._cookie_count = len(self._cookies)

    @property
    def account_scope_id(self) -> str:
        return self._account_scope_id

    @property
    def auth_state(self) -> str:
        return self._auth_state

    @property
    def acquired_at(self) -> str:
        return self._acquired_at

    @property
    def is_active(self) -> bool:
        return self._is_active

    def _check_active(self) -> None:
        if not self._is_active:
            raise CredentialExpiredError(
                f"CredentialContext for scope '{self._account_scope_id}' has expired or been closed."
            )

    @property
    def cookies(self) -> tuple[CredentialCookie, ...]:
        self._check_active()
        return self._cookies

    @property
    def cookie_map(self) -> dict[str, str]:
        self._check_active()
        return dict(self._cookie_map)

    def as_header(self) -> str:
        """Returns HTTP Cookie header formatted string while active."""
        self._check_active()
        return self._cookie_header

    def to_f2_credentials(self) -> dict[str, str]:
        """Explicit sensitive export: returns credential dict for F2 in-process handler kwargs."""
        self._check_active()
        return {"cookie": self._cookie_header}

    def reveal_for_backend(self) -> dict[str, str]:
        """Explicit sensitive export: reveals decrypted in-memory credentials for approved backend."""
        self._check_active()
        return {"cookie": self._cookie_header}

    def as_dict(self) -> dict[str, str]:
        """Deprecated: use reveal_for_backend() or to_f2_credentials() instead."""
        import warnings
        warnings.warn(
            "as_dict() on CredentialContext is deprecated. Use reveal_for_backend() or to_f2_credentials() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.reveal_for_backend()

    def get_credentials(self) -> dict[str, str]:
        """Protocol compatibility method for DownloaderBackend."""
        return self.reveal_for_backend()

    def close(self) -> None:
        """Explicitly invalidates and tears down in-memory credential references."""
        self._is_active = False
        self._cookie_map.clear()
        self._cookies = ()
        self._cookie_header = ""
        self._snapshot = None

    # Context manager lifecycle
    def __enter__(self) -> CredentialContext:
        self._check_active()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def __iter__(self) -> Any:
        raise TypeError(
            "CredentialContext is not a Mapping and cannot be converted via dict(). "
            "Use ctx.reveal_for_backend() or ctx.to_f2_credentials() instead."
        )

    # Deprecated fallback index access for legacy tests only
    def __getitem__(self, key: str) -> str:
        self._check_active()
        if key == "cookie":
            return self._cookie_header
        if key in self._cookie_map:
            return self._cookie_map[key]
        raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        self._check_active()
        if key == "cookie":
            return True
        return key in self._cookie_map

    def get(self, key: str, default: Any = None) -> Any:
        if not self._is_active:
            return default
        if key == "cookie":
            return self._cookie_header
        return self._cookie_map.get(key, default)

    def __repr__(self) -> str:
        return (
            f"<CredentialContext scope={self._account_scope_id!r} active={self._is_active} "
            f"cookies_count={self._cookie_count}>"
        )

    def __str__(self) -> str:
        return f"CredentialContext(scope={self._account_scope_id}, active={self._is_active})"


# =============================================================================
# 5. Snapshot Source Protocols & Implementations
# =============================================================================


@runtime_checkable
class CredentialSnapshotSource(Protocol):
    """Port for capturing authenticated credential snapshots from runtime or synthetic mocks."""

    def capture_snapshot(self, expected_scope_id: str | None = None) -> CredentialSnapshot:
        """Captures authenticated credential snapshot for expected scope."""
        ...


class SyntheticCredentialSource:
    """In-memory synthetic source for automated unit and integration tests.

    Never uses or logs real user credentials; uses explicit synthetic markers.
    """

    def __init__(
        self,
        account_scope_id: str = "douyin:dyacct_synthetic0001",
        auth_state: str = AuthState.AUTH_VALID.value,
        cookies: Iterable[CredentialCookie] | None = None,
        source: str = "synthetic",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.account_scope_id = account_scope_id
        self.auth_state = auth_state
        self.source = source
        self.metadata = metadata or {}
        if cookies is not None:
            self.cookies = list(cookies)
        else:
            self.cookies = [
                CredentialCookie(
                    name="sessionid",
                    value="TEST_SECRET_DO_NOT_USE_sessionid_12345",
                    domain=".douyin.com",
                    path="/",
                    http_only=True,
                    secure=True,
                    expires=time.time() + 86400,
                ),
                CredentialCookie(
                    name="sid_guard",
                    value="TEST_SECRET_DO_NOT_USE_sid_guard_67890",
                    domain=".douyin.com",
                    path="/",
                    http_only=True,
                    secure=True,
                    expires=time.time() + 86400,
                ),
                CredentialCookie(
                    name="passport_assist_user",
                    value="TEST_SECRET_DO_NOT_USE_assist_user",
                    domain=".douyin.com",
                    path="/",
                    http_only=True,
                    secure=True,
                ),
            ]

    def capture_snapshot(self, expected_scope_id: str | None = None) -> CredentialSnapshot:
        return CredentialSnapshot(
            account_scope_id=self.account_scope_id,
            auth_state=self.auth_state,
            cookies=self.cookies,
            source=self.source,
            metadata=self.metadata,
        )


class BrowserRuntimeCredentialSource:
    """Production credential source backed by the dedicated authenticated BrowserRuntime sidecar."""

    def __init__(
        self,
        runtime_provider: Any,
        auth_detector: DouyinAuthStateDetector | None = None,
        platform: str = "douyin",
    ) -> None:
        self.runtime_provider = runtime_provider
        self.auth_detector = auth_detector
        self.platform = platform

    def capture_snapshot(self, expected_scope_id: str | None = None) -> CredentialSnapshot:
        """Captures credentials from live dedicated Chromium instance via allowlisted RPC."""
        if not self.runtime_provider or not self.runtime_provider.is_running():
            raise CredentialSourceUnavailableError(
                "Browser runtime is not running. Live browser context is required to capture credential snapshot."
            )

        try:
            # 1. Execute allowlisted credential snapshot RPC
            raw_res = self.runtime_provider.capture_credential_snapshot(timeout=25.0)
        except Exception as exc:
            raise CredentialSourceUnavailableError(
                f"Failed to capture credential snapshot from browser sidecar: {exc}",
                details={"error": str(exc)},
            ) from exc

        raw_cookies = raw_res.get("cookies", [])
        account_identifier = raw_res.get("account_identifier")

        # 2. Extract AuthState and resolve account identifier from auth_detector
        auth_state_str = AuthState.AUTH_VALID.value
        detected_scope_id: str | None = None
        if self.auth_detector:
            try:
                last = getattr(self.auth_detector, "last_result", None)
                if last and last.auth_state == AuthState.AUTH_VALID and last.account_scope_id:
                    preflight = last
                else:
                    preflight = self.auth_detector.detect_auth_state(ensure_navigation=False)
                auth_state_str = preflight.auth_state.value
                if preflight.account_scope_id:
                    detected_scope_id = preflight.account_scope_id
            except Exception as e:
                logger.warning(f"Auth detector failed during credential capture: {e}")
                auth_state_str = AuthState.AUTH_UNCERTAIN.value

        # 3. Derive deterministic account scope ID using C03 algorithm
        account_scope_id: str | None = detected_scope_id
        if not account_scope_id and account_identifier:
            account_scope_id = generate_account_scope_id(self.platform, account_identifier)
        elif not account_scope_id and self.auth_detector and getattr(self.auth_detector, "last_result", None):
            account_scope_id = self.auth_detector.last_result.account_scope_id
        elif not account_scope_id and expected_scope_id:
            account_scope_id = expected_scope_id
            if not self.auth_detector and any("sessionid" in c.get("name", "") for c in raw_cookies):
                auth_state_str = AuthState.AUTH_VALID.value

        if not account_scope_id:
            auth_state_str = "ACCOUNT_SCOPE_UNRESOLVED"

        # 4. Parse cookies into CredentialCookie instances
        cookies: list[CredentialCookie] = []
        for c_dict in raw_cookies:
            name = c_dict.get("name")
            value = c_dict.get("value")
            domain = c_dict.get("domain")
            if not name or not value or not domain:
                continue
            cookies.append(
                CredentialCookie(
                    name=name,
                    value=value,
                    domain=domain,
                    path=c_dict.get("path", "/"),
                    expires=c_dict.get("expires"),
                    http_only=bool(c_dict.get("http_only")),
                    secure=bool(c_dict.get("secure")),
                    same_site=c_dict.get("same_site"),
                )
            )

        return CredentialSnapshot(
            account_scope_id=account_scope_id or "douyin:dyacct_unresolved",
            auth_state=auth_state_str,
            cookies=cookies,
            source="browser_runtime",
            metadata={
                "raw_cookie_count": len(raw_cookies),
                "has_account_identifier": bool(account_identifier),
            },
        )


# =============================================================================
# 6. Production Douyin Credential Provider
# =============================================================================


class DouyinCredentialProvider(CredentialProvider):
    """Production in-process credential provider implementing Port 1 (contracts.CredentialProvider).

    Enforces account scope verification, auth readiness gating, domain filtering,
    and ephemeral lifecycle scoping for all Downloader tasks.
    """

    def __init__(
        self,
        snapshot_source: CredentialSnapshotSource,
        cache_ttl_sec: float = 0.0,
        require_auth: bool = True,
        platform: str = "douyin",
    ) -> None:
        self.snapshot_source = snapshot_source
        self.cache_ttl_sec = max(0.0, float(cache_ttl_sec))
        self.require_auth = require_auth
        self.platform = platform

        self._cached_snapshot: CredentialSnapshot | None = None
        self._cached_at: float = 0.0

    def invalidate_cache(self) -> None:
        """Forces subsequent requests to capture a fresh credential snapshot."""
        self._cached_snapshot = None
        self._cached_at = 0.0

    def _get_or_capture_snapshot(self, expected_scope_id: str) -> CredentialSnapshot:
        now = time.monotonic()
        if (
            self._cached_snapshot is not None
            and self.cache_ttl_sec > 0
            and (now - self._cached_at) < self.cache_ttl_sec
            and self._cached_snapshot.account_scope_id == expected_scope_id
        ):
            return self._cached_snapshot

        snapshot = self.snapshot_source.capture_snapshot(expected_scope_id=expected_scope_id)
        if self.cache_ttl_sec > 0 and snapshot.is_valid:
            self._cached_snapshot = snapshot
            self._cached_at = now
        return snapshot

    def acquire(
        self,
        task: DownloadTask | str,
        expected_scope_id: str | None = None,
    ) -> CredentialContext:
        """Acquires a scoped, verified CredentialContext for a given task or scope ID.

        Enforces:
        - Scope non-empty
        - Auth state is AUTH_VALID
        - Task scope ID matches credential account scope ID
        - Cookie set is non-empty
        """
        target_scope_id: str
        if isinstance(task, DownloadTask):
            target_scope_id = task.scope_id or ""
        else:
            target_scope_id = expected_scope_id or str(task)

        target_scope_id = target_scope_id.strip()
        if not target_scope_id:
            raise CredentialScopeMismatchError(
                "Task scope_id is missing or unresolved. Credential acquisition rejected.",
                details={"code": "ACCOUNT_SCOPE_UNRESOLVED"},
            )

        snapshot = self._get_or_capture_snapshot(target_scope_id)

        # 1. Auth readiness gating
        if snapshot.auth_state == AuthState.AUTH_REQUIRED.value:
            raise CredentialAuthInvalidError(
                f"Douyin authentication required (login expired or not logged in) for scope '{target_scope_id}'.",
                auth_state=snapshot.auth_state,
                error_code=DownloaderErrorCode.DOWNLOAD_AUTH_REQUIRED,
                subreason="LOGIN_REQUIRED",
                details={"scope_id": target_scope_id},
            )
        elif snapshot.auth_state == AuthState.AUTH_CHALLENGE.value:
            raise CredentialAuthInvalidError(
                f"Interactive security verification/challenge detected on page for scope '{target_scope_id}'.",
                auth_state=snapshot.auth_state,
                error_code=DownloaderErrorCode.DOWNLOAD_AUTH_CHALLENGE,
                subreason="INTERACTIVE_CHALLENGE",
                details={"scope_id": target_scope_id},
            )
        elif snapshot.auth_state == "ACCOUNT_SCOPE_UNRESOLVED":
            raise CredentialScopeMismatchError(
                f"Account identity could not be resolved from browser context for scope '{target_scope_id}'.",
                details={"code": "ACCOUNT_SCOPE_UNRESOLVED"},
            )
        elif snapshot.auth_state != AuthState.AUTH_VALID.value:
            raise CredentialAuthInvalidError(
                f"Authentication state '{snapshot.auth_state}' is not VALID for scope '{target_scope_id}'.",
                auth_state=snapshot.auth_state,
                error_code=DownloaderErrorCode.DOWNLOAD_CREDENTIAL_BRIDGE_FAILED,
                subreason="AUTH_STATE_INVALID",
                details={"scope_id": target_scope_id, "auth_state": snapshot.auth_state},
            )

        # 2. Account scope binding verification
        if snapshot.account_scope_id != target_scope_id:
            raise CredentialScopeMismatchError(
                f"Credential account scope '{snapshot.account_scope_id}' does not match target task scope '{target_scope_id}'.",
                details={
                    "actual_scope_id": snapshot.account_scope_id,
                    "expected_scope_id": target_scope_id,
                },
            )

        # 3. Cookie collection non-empty verification
        if len(snapshot.cookies) == 0:
            raise CredentialEmptyError(
                f"Credential snapshot for scope '{target_scope_id}' contains zero valid allowlisted cookies.",
                details={"scope_id": target_scope_id},
            )

        return CredentialContext(snapshot)

    def get_credentials(self, scope_id: str) -> dict[str, str] | None:
        """Protocol method for contracts.CredentialProvider.

        Returns credential mapping if authenticated and matching, or None if unauthenticated.
        """
        try:
            ctx = self.acquire(scope_id)
            return ctx.get_credentials()
        except CredentialProviderError as exc:
            logger.warning(
                f"Credential acquisition failed for scope '{scope_id}': "
                f"[{exc.error_code.value}:{exc.subreason}] {exc}"
            )
            return None


# =============================================================================
# 7. Fake / In-Memory Credential Provider (for Testing & Mock Workflows)
# =============================================================================


class FakeCredentialProvider(CredentialProvider):
    """In-memory CredentialProvider using synthetic markers for unit testing."""

    def __init__(
        self,
        credentials: dict[str, str] | None = None,
        scope_id: str = "douyin:dyacct_mockdefault1",
        auth_state: str = AuthState.AUTH_VALID.value,
    ) -> None:
        self.scope_id = scope_id
        self.auth_state = auth_state
        self._raw_credentials = credentials or {"cookie": "TEST_SECRET_DO_NOT_USE_cookie_data"}
        self.source = SyntheticCredentialSource(
            account_scope_id=scope_id,
            auth_state=auth_state,
        )
        self.provider = DouyinCredentialProvider(snapshot_source=self.source)

    def acquire(self, task: DownloadTask | str, expected_scope_id: str | None = None) -> CredentialContext:
        return self.provider.acquire(task, expected_scope_id=expected_scope_id)

    def get_credentials(self, scope_id: str) -> dict[str, str] | None:
        if self.auth_state != AuthState.AUTH_VALID.value or scope_id != self.scope_id:
            return None
        return dict(self._raw_credentials)
