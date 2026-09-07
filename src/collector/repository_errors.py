"""Machine-readable exception hierarchy for the Collector Metadata Repository (DY-C08)."""

from __future__ import annotations

from enum import Enum
from typing import Any


class RepositoryErrorCode(str, Enum):
    """Machine-readable error codes for metadata repository operations."""

    REPOSITORY_INVALID_INPUT = "REPOSITORY_INVALID_INPUT"
    REPOSITORY_NOT_INITIALIZED = "REPOSITORY_NOT_INITIALIZED"
    REPOSITORY_SCHEMA_ERROR = "REPOSITORY_SCHEMA_ERROR"
    REPOSITORY_CONFLICT = "REPOSITORY_CONFLICT"
    REPOSITORY_STATE_CONFLICT = "REPOSITORY_STATE_CONFLICT"
    REPOSITORY_IO_ERROR = "REPOSITORY_IO_ERROR"
    REPOSITORY_CORRUPT = "REPOSITORY_CORRUPT"
    REPOSITORY_TRANSACTION_FAILED = "REPOSITORY_TRANSACTION_FAILED"
    REPOSITORY_NOT_FOUND = "REPOSITORY_NOT_FOUND"
    REPOSITORY_UNKNOWN = "REPOSITORY_UNKNOWN"


class RepositoryError(Exception):
    """Base exception for all metadata repository errors."""

    def __init__(
        self,
        message: str,
        code: RepositoryErrorCode = RepositoryErrorCode.REPOSITORY_UNKNOWN,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        """Structured error dictionary without leaking sensitive payloads."""
        return {
            "code": self.code.value,
            "message": self.message,
            "details": self.details,
        }


class RepositoryInvalidInputError(RepositoryError):
    """Raised when repository input parameters fail contract or format validation."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, RepositoryErrorCode.REPOSITORY_INVALID_INPUT, details)


class RepositoryNotInitializedError(RepositoryError):
    """Raised when repository methods are invoked prior to migration / initialization."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, RepositoryErrorCode.REPOSITORY_NOT_INITIALIZED, details)


class RepositorySchemaError(RepositoryError):
    """Raised when DB schema definition or canonical JSON validation fails."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, RepositoryErrorCode.REPOSITORY_SCHEMA_ERROR, details)


class RepositoryConflictError(RepositoryError):
    """Raised when an entity or observation ID exists with conflicting payload."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, RepositoryErrorCode.REPOSITORY_CONFLICT, details)


class RepositoryStateConflictError(RepositoryError):
    """Raised when optimistic concurrency or lifecycle state validation fails."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, RepositoryErrorCode.REPOSITORY_STATE_CONFLICT, details)


class RepositoryIOError(RepositoryError):
    """Raised on physical SQLite disk I/O, lock timeout, or filesystem failure."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, RepositoryErrorCode.REPOSITORY_IO_ERROR, details)


class RepositoryCorruptError(RepositoryError):
    """Raised when SQLite database file is corrupt or violates integrity check."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, RepositoryErrorCode.REPOSITORY_CORRUPT, details)


class RepositoryTransactionError(RepositoryError):
    """Raised when an atomic transaction rollback occurs or fails to commit."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, RepositoryErrorCode.REPOSITORY_TRANSACTION_FAILED, details)


class RepositoryNotFoundError(RepositoryError):
    """Raised when an expected record or run is not found in the repository."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, RepositoryErrorCode.REPOSITORY_NOT_FOUND, details)
