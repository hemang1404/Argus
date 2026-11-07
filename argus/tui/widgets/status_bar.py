"""Bottom status bar widget displaying active workspace and version."""

from __future__ import annotations

import importlib.metadata
from pathlib import Path

from rich.cells import cell_len
from rich.text import Text
from textual.widget import Widget

try:
    _DEFAULT_VERSION = f"v{importlib.metadata.version('argus-recovery')}"
except Exception:
    _DEFAULT_VERSION = "v0.2.0"


class StatusBar(Widget):
    """Pinned bottom status bar showing runtime identity and version."""

    def __init__(self, workspace: str = ".", version: str = _DEFAULT_VERSION, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.workspace = str(Path(workspace).resolve())
        self.version = version

    def render(self) -> Text:
        """Render two-column minimalist status text."""
        total_width = self.size.width if self.size.width > 0 else 80

        left_text = "argus-runtime"
        right_text = f"✦  {self.version}"

        # ✦ is an ambiguous-width glyph: measure display cells, not code points.
        pad = max(1, total_width - cell_len(left_text) - cell_len(right_text) - 4)

        t = Text()
        t.append(left_text, style="#64748b")
        t.append(" " * pad)
        t.append("✦ ", style="#818cf8")
        t.append(self.version, style="#64748b")
        return t
