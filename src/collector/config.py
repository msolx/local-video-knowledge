"""Configuration model and parser for the collector subsystem."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError

FORBIDDEN_SECRET_KEYS = {"sessionid", "cookie", "cookies", "token", "password", "secret"}


@dataclass
class CollectorConfig:
    """Platform-agnostic configuration for collection operations."""
    platform: str = "douyin"
    runtime_root: Path = field(default_factory=lambda: Path("./runtime"))
    raw_archive_root: Path = field(default_factory=lambda: Path("./data/raw"))
    canonical_root: Path = field(default_factory=lambda: Path("./data/canonical"))
    database_path: Path = field(default_factory=lambda: Path("./data/metadata.db"))
    log_level: str = "INFO"
    lock_timeout_sec: int = 3600
    page_size: int = 10
    headless: bool = True
    profile_path: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def lock_path(self) -> Path:
        return self.runtime_root / f"{self.platform}_collector.lock"

    @property
    def log_dir(self) -> Path:
        return self.runtime_root / "logs"

    def ensure_directories(self) -> None:
        """Create non-sensitive runtime and data storage directories."""
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.raw_archive_root.mkdir(parents=True, exist_ok=True)
        self.canonical_root.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        if self.database_path.parent:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], base_path: Path | None = None) -> CollectorConfig:
        """Construct config from dictionary, validating secrets and normalizing paths."""
        base = base_path or Path.cwd()

        # Check for forbidden secret leakage in configuration
        cls._validate_no_secrets(raw)

        def resolve_path(val: Any) -> Path:
            p = Path(val)
            return p if p.is_absolute() else (base / p).resolve()

        platform = raw.get("platform", "douyin")
        runtime_root = resolve_path(raw.get("runtime_root", "./runtime"))
        raw_archive_root = resolve_path(raw.get("raw_archive_root", "./data/raw"))
        canonical_root = resolve_path(raw.get("canonical_root", "./data/canonical"))
        database_path = resolve_path(raw.get("database_path", "./data/metadata.db"))
        log_level = str(raw.get("log_level", "INFO")).upper()
        lock_timeout_sec = int(raw.get("lock_timeout_sec", 3600))
        page_size = int(raw.get("page_size", 10))
        headless = bool(raw.get("headless", True))

        profile_path = None
        if raw.get("profile_path"):
            profile_path = resolve_path(raw["profile_path"])

        extra = raw.get("extra", {})

        return cls(
            platform=platform,
            runtime_root=runtime_root,
            raw_archive_root=raw_archive_root,
            canonical_root=canonical_root,
            database_path=database_path,
            log_level=log_level,
            lock_timeout_sec=lock_timeout_sec,
            page_size=page_size,
            headless=headless,
            profile_path=profile_path,
            extra=extra,
        )

    @classmethod
    def load(cls, path: str | Path) -> CollectorConfig:
        """Load configuration from a JSON file."""
        config_file = Path(path).resolve()
        if not config_file.exists():
            raise ConfigError(f"Configuration file not found: {config_file}")
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            raise ConfigError(f"Failed to parse configuration file {config_file}: {e}")

        if not isinstance(data, dict):
            raise ConfigError(f"Configuration file {config_file} must contain a JSON object")

        return cls.from_dict(data, base_path=config_file.parent)

    @staticmethod
    def _validate_no_secrets(data: Any, path: str = "") -> None:
        """Recursively scan data for forbidden credential keys."""
        if isinstance(data, dict):
            for k, v in data.items():
                k_lower = str(k).lower()
                if any(secret in k_lower for secret in FORBIDDEN_SECRET_KEYS):
                    raise ConfigError(
                        f"Sensitive credential key '{k}' detected at '{path}'. "
                        "Credentials must NOT be stored in collector configuration files."
                    )
                CollectorConfig._validate_no_secrets(v, f"{path}.{k}" if path else str(k))
        elif isinstance(data, list):
            for i, item in enumerate(data):
                CollectorConfig._validate_no_secrets(item, f"{path}[{i}]")
