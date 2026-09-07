"""Pure deterministic QW-04 incremental watermark sync policy engine.

Authoritative invariants per QW-04 / QW-15:
1. Incremental sync ALWAYS begins at cursor="0".
2. Strategy A (first-known early stop) is strictly PROHIBITED. Encountering a known item
   MUST NOT trigger an early stop.
3. Safe early stop condition is STRICTLY evaluated at page boundaries:
   `int(response_cursor) <= int(committed_watermark) OR has_more == 0`.
4. Candidate watermark is established from Page 1 (newest window) response cursor.
5. In backfill mode, watermark boundary stop is bypassed in favor of pagination limits or end of collection.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class StopAction(str, enum.Enum):
    """Action to take after evaluating page facts."""

    CONTINUE = "continue"
    STOP_SAFE_BOUNDARY = "stop_safe_boundary"
    STOP_TERMINAL = "stop_terminal"
    STOP_LIMIT = "stop_limit"
    STOP_ERROR = "stop_error"


class StopReasonCode(str, enum.Enum):
    """Machine-readable reason for the stop decision."""

    HAS_MORE = "has_more"
    WATERMARK_REACHED = "watermark_reached"
    END_OF_COLLECTION = "end_of_collection"
    EMPTY_COLLECTION = "empty_collection"
    MAX_PAGES_REACHED = "max_pages_reached"
    BACKFILL_LIMIT_REACHED = "backfill_limit_reached"
    LOOP_DETECTED = "loop_detected"


@dataclass(frozen=True)
class PageFacts:
    """Facts observed from a single fetched page."""

    page_number: int
    request_cursor: str
    response_cursor: str
    has_more: int
    items_count: int
    new_items_count: int = 0
    known_items_count: int = 0
    reappeared_items_count: int = 0
    duplicate_in_run_count: int = 0


@dataclass(frozen=True)
class StopDecision:
    """Decision output by IncrementalSyncPolicy after evaluating page facts."""

    action: StopAction
    reason_code: StopReasonCode
    candidate_watermark_cursor: str | None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def should_stop(self) -> bool:
        return self.action != StopAction.CONTINUE


def is_watermark_reached(response_cursor: str, committed_watermark: str) -> bool:
    """Evaluates whether response_cursor is older than or equal to committed_watermark.

    Douyin cursors are monotonically decreasing timestamps.
    Therefore, response_cursor <= committed_watermark means we have paged into or past
    the historical boundary of the previous sync.
    """
    try:
        return int(response_cursor) <= int(committed_watermark)
    except (ValueError, TypeError):
        return str(response_cursor) == str(committed_watermark)


class IncrementalSyncPolicy:
    """Pure deterministic sync policy engine implementing QW-04 rules."""

    def __init__(
        self,
        mode: str = "incremental",
        committed_watermark: str | None = None,
        max_pages: int | None = None,
        backfill_limit: int | None = None,
        history_complete: bool | None = None,
    ) -> None:
        self.mode = mode.lower()
        self.committed_watermark = str(committed_watermark) if committed_watermark is not None else None
        self.max_pages = max_pages
        self.backfill_limit = backfill_limit
        if history_complete is None:
            self.history_complete = self.committed_watermark is not None
        else:
            self.history_complete = history_complete
        self._candidate_watermark: str | None = None
        self._accumulated_items: int = 0
        self._accumulated_pages: int = 0

    @property
    def candidate_watermark(self) -> str | None:
        return self._candidate_watermark

    def evaluate_page(self, page: PageFacts) -> StopDecision:
        """Evaluates a single page's facts against sync invariants and returns a StopDecision."""
        self._accumulated_pages += 1
        self._accumulated_items += page.items_count

        # 1. Establish candidate watermark from Page 1 (only when starting from head "0")
        if page.page_number == 1 and page.items_count > 0 and str(page.request_cursor).strip() == "0":
            resp_cur = str(page.response_cursor).strip()
            if resp_cur and resp_cur != "0":
                self._candidate_watermark = resp_cur

        # 2. Guard against infinite loops (same non-zero cursor returned with has_more=1)
        if (
            page.page_number > 1
            and page.response_cursor == page.request_cursor
            and page.response_cursor != "0"
            and page.has_more == 1
        ):
            return StopDecision(
                action=StopAction.STOP_ERROR,
                reason_code=StopReasonCode.LOOP_DETECTED,
                candidate_watermark_cursor=None,
                details={
                    "page_number": page.page_number,
                    "repeated_cursor": page.response_cursor,
                    "message": "Infinite pagination loop detected: response_cursor == request_cursor",
                },
            )

        # 3. Empty collection on Page 1
        if page.page_number == 1 and page.items_count == 0:
            return StopDecision(
                action=StopAction.STOP_TERMINAL,
                reason_code=StopReasonCode.EMPTY_COLLECTION,
                candidate_watermark_cursor=None,
                details={"page_number": 1, "items_count": 0},
            )

        # 4. Terminal end of collection (no more items or explicit has_more=0)
        if page.has_more == 0 or page.response_cursor == "0" or page.items_count == 0:
            return StopDecision(
                action=StopAction.STOP_TERMINAL,
                reason_code=StopReasonCode.END_OF_COLLECTION,
                candidate_watermark_cursor=self._candidate_watermark,
                details={
                    "page_number": page.page_number,
                    "has_more": page.has_more,
                    "response_cursor": page.response_cursor,
                    "items_count": page.items_count,
                },
            )

        # 5. Incremental Watermark Boundary Check (QW-04)
        # Bypassed in backfill mode OR if historical coverage is not complete!
        # Safe boundary stop requires that history is already complete.
        if self.mode == "incremental" and self.history_complete and self.committed_watermark is not None:
            if is_watermark_reached(page.response_cursor, self.committed_watermark):
                return StopDecision(
                    action=StopAction.STOP_SAFE_BOUNDARY,
                    reason_code=StopReasonCode.WATERMARK_REACHED,
                    candidate_watermark_cursor=self._candidate_watermark,
                    details={
                        "page_number": page.page_number,
                        "response_cursor": page.response_cursor,
                        "committed_watermark": self.committed_watermark,
                    },
                )

        # 6. Max pages limit check
        if self.max_pages is not None and page.page_number >= self.max_pages:
            return StopDecision(
                action=StopAction.STOP_LIMIT,
                reason_code=StopReasonCode.MAX_PAGES_REACHED,
                candidate_watermark_cursor=None,  # STOP_LIMIT must never advance watermark!
                details={
                    "page_number": page.page_number,
                    "max_pages": self.max_pages,
                    "uncommitted_candidate_watermark": self._candidate_watermark,
                },
            )

        # 7. Backfill limit check
        if (
            self.mode == "backfill"
            and self.backfill_limit is not None
            and self._accumulated_items >= self.backfill_limit
        ):
            return StopDecision(
                action=StopAction.STOP_LIMIT,
                reason_code=StopReasonCode.BACKFILL_LIMIT_REACHED,
                candidate_watermark_cursor=None,  # STOP_LIMIT must never advance watermark!
                details={
                    "accumulated_items": self._accumulated_items,
                    "backfill_limit": self.backfill_limit,
                    "uncommitted_candidate_watermark": self._candidate_watermark,
                },
            )

        # 8. Continue to next page
        return StopDecision(
            action=StopAction.CONTINUE,
            reason_code=StopReasonCode.HAS_MORE,
            candidate_watermark_cursor=self._candidate_watermark,
            details={
                "page_number": page.page_number,
                "response_cursor": page.response_cursor,
            },
        )
