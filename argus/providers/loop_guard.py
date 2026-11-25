"""Streaming repetition-loop detector for local and open-weight inference."""

from __future__ import annotations

from collections import Counter, deque
from typing import Deque, Optional


class RepetitionLoopDetector:
    """Detect degenerate repetition loops mid-stream so generation can abort early.

    Tracks two complementary signals:
    1. Repeated non-trivial lines within a recent sliding window (catches line/edit loops).
    2. Periodic tail repetition and dense n-gram repeats across the generated stream
       (catches arbitrary-length token/reasoning phrase loops without newlines).
    """

    def __init__(
        self,
        *,
        min_line_chars: int = 20,
        line_window: int = 24,
        line_repeat_threshold: int = 5,
        ngram_chars: int = 80,
        ngram_repeat_threshold: int = 4,
        min_tail_period: int = 24,
        max_tail_period: int = 320,
    ) -> None:
        self.min_line_chars = min_line_chars
        self.line_window = line_window
        self.line_repeat_threshold = line_repeat_threshold
        self.ngram_chars = ngram_chars
        self.ngram_repeat_threshold = ngram_repeat_threshold
        self.min_tail_period = min_tail_period
        self.max_tail_period = max_tail_period

        self._buffer = ""
        self._pending_line = ""
        self._recent_lines: Deque[str] = deque(maxlen=line_window)
        self._ngram_counts: Counter[str] = Counter()
        self._next_scan_pos = 0
        self.triggered_reason: Optional[str] = None

    def _has_periodic_tail(self) -> bool:
        k = self.ngram_repeat_threshold
        buf = self._buffer
        n = len(buf)
        max_p = min(self.max_tail_period, n // k)
        for p in range(self.min_tail_period, max_p + 1):
            unit = buf[n - p :]
            if len(unit.strip()) < self.min_line_chars:
                continue
            if buf.endswith(unit * k):
                return True
        return False

    def feed(self, chunk: str) -> Optional[str]:
        """Consume a streamed text delta and return a trigger reason if a loop is detected."""
        if self.triggered_reason is not None or not chunk:
            return self.triggered_reason

        self._buffer += chunk
        self._pending_line += chunk

        # 1. Line-level repeat check on complete lines
        while "\n" in self._pending_line:
            line, self._pending_line = self._pending_line.split("\n", 1)
            stripped = line.strip()
            if len(stripped) >= self.min_line_chars:
                self._recent_lines.append(stripped)
                if sum(1 for ln in self._recent_lines if ln == stripped) >= self.line_repeat_threshold:
                    self.triggered_reason = "line_repeat"
                    return self.triggered_reason

        # 2. Exact periodic suffix check (handles any period regardless of stride alignment)
        if self._has_periodic_tail():
            self.triggered_reason = "ngram_repeat"
            return self.triggered_reason

        # 3. N-gram repeat check across the full stream
        while self._next_scan_pos + self.ngram_chars <= len(self._buffer):
            gram = self._buffer[self._next_scan_pos : self._next_scan_pos + self.ngram_chars]
            self._next_scan_pos += 1
            if len(gram.strip()) >= self.min_line_chars:
                self._ngram_counts[gram] += 1
                if self._ngram_counts[gram] >= self.ngram_repeat_threshold:
                    self.triggered_reason = "ngram_repeat"
                    return self.triggered_reason

        return None
