"""Collector error taxonomy and custom exception hierarchy.

Categorizes all collector operational errors into standard error codes.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class CollectorErrorCode(str, Enum):
    CONFIG_ERROR = "CONFIG_ERROR"
    LOCKED = "LOCKED"
    RUNTIME_ERROR = "RUNTIME_ERROR"
    DEPENDENCY_NOT_READY = "DEPENDENCY_NOT_READY"
    AUTH_NOT_READY = "AUTH_NOT_READY"
    SYNC_FAILED = "SYNC_FAILED"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    UNKNOWN = "UNKNOWN"


class CollectorError(Exception):
    """Base exception for all collector subsystem errors."""

    def __init__(
        self,
        code: CollectorErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "details": self.details,
        }


class ConfigError(CollectorError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(CollectorErrorCode.CONFIG_ERROR, message, details)


class LockError(CollectorError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(CollectorErrorCode.LOCKED, message, details)


class DependencyNotReadyError(CollectorError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(CollectorErrorCode.DEPENDENCY_NOT_READY, message, details)


class AuthNotReadyError(CollectorError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(CollectorErrorCode.AUTH_NOT_READY, message, details)


class SyncFailedError(CollectorError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(CollectorErrorCode.SYNC_FAILED, message, details)


class BrowserRuntimeError(CollectorError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(CollectorErrorCode.RUNTIME_ERROR, message, details)

