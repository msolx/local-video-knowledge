"""Structured logging with secret redaction for the collector subsystem."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

# Patterns matching sensitive Douyin session credentials and signing tokens
REDACTION_PATTERNS = [
    (re.compile(r"(sessionid(?:_ss)?=)[a-zA-Z0-9%_-]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(sid_guard=)[a-zA-Z0-9%_-]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(sid_tt=)[a-zA-Z0-9%_-]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(msToken=)[a-zA-Z0-9%_\./+=-]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(a_bogus=)[a-zA-Z0-9%_\./+=-]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(passport_csrf_token(?:_default)?=)[a-zA-Z0-9%_-]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(Bearer\s+)[a-zA-Z0-9_\-\.]+", re.IGNORECASE), r"\1[REDACTED]"),
]


def redact_secrets(text: str) -> str:
    """Sanitize all known credentials from text."""
    if not isinstance(text, str):
        return text
    sanitized = text
    for pattern, repl in REDACTION_PATTERNS:
        sanitized = pattern.sub(repl, sanitized)
    return sanitized


class RedactingFormatter(logging.Formatter):
    """Logging formatter that sanitizes secrets before formatting."""

    def format(self, record: logging.LogRecord) -> str:
        original_msg = record.getMessage()
        record.msg = redact_secrets(original_msg)
        record.args = ()
        return super().format(record)


def configure_collector_logging(
    run_id: str,
    log_dir: Path | None = None,
    log_level: str = "INFO",
) -> logging.Logger:
    """Configure and return a structured redacting logger for a specific collector run."""
    logger = logging.getLogger(f"collector.{run_id}")
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    format_str = f"%(asctime)s [%(levelname)s] [run:{run_id}] [%(name)s] %(message)s"
    formatter = RedactingFormatter(format_str)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler if log_dir provided
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / "collector.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger
