"""Terminal Graphics Protocol logo renderer (SIXEL and Kitty)."""

from __future__ import annotations

import io
import os
from typing import Optional

from rich.text import Text

from argus.tui.renderers.base import BaseLogoRenderer, LogoRenderResult
from argus.tui.renderers.rasterizer import LogoRasterizer
from argus.utils.terminal_graphics import (
    detect_graphics_protocol,
    encode_kitty,
    encode_sixel,
)


class TerminalGraphicsLogoRenderer(BaseLogoRenderer):
    """Renders the logo using SIXEL or Kitty graphics protocols when supported.

    Falls back safely to Braille if the active terminal cannot reliably display graphics
    without screen tearing or unsupported escape sequences.
    """

    def __init__(self, protocol: str = "sixel") -> None:
        self.protocol = protocol.lower()
        self.name = f"graphics_{self.protocol}"

    def is_protocol_supported(self) -> bool:
        """Verify whether the requested graphics protocol is available in the current terminal."""
        detected = detect_graphics_protocol()
        if self.protocol == "sixel":
            return detected == "sixel" or "sixel" in os.environ.get("TERM", "").lower()
        elif self.protocol == "kitty":
            return detected == "kitty" or bool(os.environ.get("KITTY_WINDOW_ID"))
        return False

    def render(
        self,
        target_width: int = 64,
        target_height: Optional[int] = None,
        max_width: int = 76,
        max_height: int = 14,
    ) -> LogoRenderResult:
        """Render using graphics escape sequence, falling back safely to Braille if unsupported."""
        if not self.is_protocol_supported():
            # Graceful capability fallback
            from argus.tui.renderers.braille import BrailleLogoRenderer
            fallback = BrailleLogoRenderer()
            res = fallback.render(target_width, target_height, max_width, max_height)
            return LogoRenderResult(
                text=res.text,
                raw_ansi=res.raw_ansi,
                width=res.width,
                height=res.height,
                renderer_name=f"{self.name}_fallback_braille",
            )

        cols, rows = LogoRasterizer.calculate_cell_dimensions(
            target_width=target_width,
            target_height=target_height,
            max_width=max_width,
            max_height=max_height,
            subpixels_per_cell=(1, 2),
        )

        master = LogoRasterizer.get_master_image()

        if self.protocol == "kitty":
            buf = io.BytesIO()
            master.save(buf, format="PNG")
            raw_ansi = encode_kitty(buf.getvalue(), cols=cols)
        else:  # sixel
            raw_ansi = encode_sixel(master, max_colors=16)

        # In Textual TUI, raw escape sequences in widgets need to be wrapped or text-represented
        text = Text(raw_ansi)
        return LogoRenderResult(
            text=text,
            raw_ansi=raw_ansi,
            width=cols,
            height=rows,
            renderer_name=self.name,
        )

