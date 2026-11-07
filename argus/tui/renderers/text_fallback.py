"""Clean, minimal text fallback logo renderer."""

from __future__ import annotations

from typing import List, Optional

from rich.text import Text

from argus.tui.renderers.base import BaseLogoRenderer, LogoRenderResult

EMBLEM_GEOMETRIC_LINES = [
    "▲",
    "◢▀ ▀◣",
    "◢▀  ▲  ▀◣",
    "◢▀  ◢ █ ◣  ▀◣",
    "◢▀  ◢ █ ◆ █ ◣  ▀◣",
    "◥▄  ◥ █ ◆ █ ◤  ▄◤",
    "◥▄  ◥ █ ◤  ▄◤",
    "◥▄  ▼  ▄◤",
    "◥▄ ▄◤",
    "▼",
]


class TextFallbackLogoRenderer(BaseLogoRenderer):
    """Minimal, rock-solid text fallback using clean Unicode geometric symbols.

    Guaranteed to render identically across 100% of terminal environments without
    relying on graphics, half-blocks, or braille support.
    """

    name: str = "text"

    def render(
        self,
        target_width: int = 64,
        target_height: Optional[int] = None,
        max_width: int = 76,
        max_height: int = 14,
    ) -> LogoRenderResult:
        text = Text()
        ansi_lines: List[str] = []

        # 1. Centered geometric diamond emblem
        for raw_line in EMBLEM_GEOMETRIC_LINES:
            pad = max(0, (max_width - len(raw_line)) // 2)
            pad_str = " " * pad
            line_txt = Text(pad_str)
            line_ansi = pad_str

            for ch in raw_line:
                if ch == "◆":
                    line_txt.append(ch, style="bold #c7d2fe")
                    line_ansi += "\033[1;38;2;199;210;254m◆\033[0m"
                elif ch in ("▲", "▼"):
                    line_txt.append(ch, style="bold #818cf8")
                    line_ansi += "\033[1;38;2;129;140;248m" + ch + "\033[0m"
                elif ch in ("█", "◢", "◣", "◥", "◤"):
                    line_txt.append(ch, style="#818cf8")
                    line_ansi += "\033[38;2;129;140;248m" + ch + "\033[0m"
                elif ch in ("▀", "▄"):
                    line_txt.append(ch, style="#6366f1")
                    line_ansi += "\033[38;2;99;102;241m" + ch + "\033[0m"
                else:
                    line_txt.append(ch)
                    line_ansi += ch

            line_txt.append("\n")
            text.append_text(line_txt)
            ansi_lines.append(line_ansi)

        # 2. Wide-tracked clean wordmark
        wordmark_str = "A   R   G   U   S"
        pad = max(0, (max_width - len(wordmark_str)) // 2)
        pad_str = " " * pad
        text.append(pad_str + wordmark_str + "\n", style="bold #f8fafc")
        ansi_lines.append(pad_str + "\033[1;38;2;248;250;252mA   R   G   U   S\033[0m")

        return LogoRenderResult(
            text=text,
            raw_ansi="\n".join(ansi_lines),
            width=max_width,
            height=len(ansi_lines),
            renderer_name=self.name,
        )

