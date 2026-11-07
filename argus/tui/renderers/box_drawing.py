"""7-column wide architectural box-drawing logo renderer with centered typography.

Supports:
- layout='wordmark': Pure minimalist centered 3-row wordmark without bulky emblem.
- layout='inline': Small architectural 'A in an eye' emblem placed to the left of ARGUS.
- layout='stacked': Legacy stacked emblem above wordmark.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from rich.text import Text

from argus.tui.renderers.base import BaseLogoRenderer, LogoRenderResult

# Compact architectural 'A in an eye' emblem (matches 7-col width and 3-row height)
EYE_A_GLYPH: List[str] = [
    "╭──▲──╮",
    "│ /█\\ │",
    "╰──▼──╯",
]

# Legacy faceted diamond emblem
EMBLEM_LINES = [
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

# Wide 7-column architectural glyphs (matching SHARY Thin 1.10 aspect ratio)
GLYPHS_7WIDE: Dict[str, List[str]] = {
    "A": [
        "╭─────╮",
        "├─────┤",
        "┴     ┴",
    ],
    "R": [
        "╭─────╮",
        "├─────╯",
        "┴    ╲ ",
    ],
    "G": [
        "╭──────",
        "│   ──┐",
        "╰─────╯",
    ],
    "U": [
        "╷     ╷",
        "│     │",
        "╰─────╯",
    ],
    "S": [
        "╭──────",
        "╰─────╮",
        "──────╯",
    ],
}


class BoxDrawingLogoRenderer(BaseLogoRenderer):
    """Renders the canonical 7-column wide architectural box-drawing typography.

    Provides balanced, wide letter proportions (matching SHARY Thin aspect ratio),
    open letter counters, clean serifs, and continuous diagonal slopes without
    blockiness or dot-matrix artifacts.
    """

    name: str = "box"

    def __init__(self, gap: int = 3, layout: str = "wordmark") -> None:
        self.gap = gap
        self.layout = layout.lower().strip()

    def render(
        self,
        target_width: int = 76,
        target_height: Optional[int] = None,
        max_width: int = 76,
        max_height: int = 16,
    ) -> LogoRenderResult:
        text = Text()
        ansi_lines: List[str] = []

        # 1. Optional legacy stacked emblem
        if self.layout == "stacked":
            for raw_line in EMBLEM_LINES:
                pad_l = max(0, (max_width - len(raw_line)) // 2)
                pad_r = max(0, max_width - len(raw_line) - pad_l)
                pad_l_str = " " * pad_l
                pad_r_str = " " * pad_r

                line_txt = Text(pad_l_str)
                line_ansi = pad_l_str

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

                line_txt.append(pad_r_str + "\n")
                line_ansi += pad_r_str
                text.append_text(line_txt)
                ansi_lines.append(line_ansi)

            text.append("\n")
            ansi_lines.append("")

        # 2. Wordmark rows
        letters = ["A", "R", "G", "U", "S"]
        word_rows = ["" for _ in range(3)]
        gap_str = " " * self.gap

        # If layout is 'inline', prepend the 'A in an eye' emblem
        if self.layout == "inline":
            eye_sep = " " * (self.gap + 1)
            for r in range(3):
                word_rows[r] = EYE_A_GLYPH[r] + eye_sep

        for i, ch in enumerate(letters):
            glyph_lines = GLYPHS_7WIDE.get(ch, ["       "] * 3)
            for r in range(3):
                word_rows[r] += glyph_lines[r] + (gap_str if i < len(letters) - 1 else "")

        # 3. Exact symmetrical horizontal centering
        for row_str in word_rows:
            line_len = len(row_str)
            pad_left = max(0, (max_width - line_len) // 2)
            pad_right = max(0, max_width - line_len - pad_left)
            pad_l_str = " " * pad_left
            pad_r_str = " " * pad_right

            line_txt = Text(pad_l_str)
            line_ansi = pad_l_str

            if self.layout == "inline":
                eye_w = len(EYE_A_GLYPH[0])
                eye_part = row_str[:eye_w]
                rest_part = row_str[eye_w:]

                line_txt.append(eye_part, style="bold #818cf8")
                line_ansi += f"\033[1;38;2;129;140;248m{eye_part}\033[0m"

                line_txt.append(rest_part, style="bold #e2e8f0")
                line_ansi += f"\033[1;38;2;226;232;240m{rest_part}\033[0m"
            else:
                line_txt.append(row_str, style="bold #e2e8f0")
                line_ansi += f"\033[1;38;2;226;232;240m{row_str}\033[0m"

            line_txt.append(pad_r_str + "\n")
            line_ansi += pad_r_str
            text.append_text(line_txt)
            ansi_lines.append(line_ansi)

        return LogoRenderResult(
            text=text,
            raw_ansi="\n".join(ansi_lines),
            width=max_width,
            height=len(ansi_lines),
            renderer_name=self.name,
        )
