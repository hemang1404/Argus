"""Truecolor Unicode half-block (▀ / ▄) logo renderer."""

from __future__ import annotations

from typing import List, Optional

from rich.text import Text

from argus.tui.renderers.base import BaseLogoRenderer, LogoRenderResult
from argus.tui.renderers.rasterizer import LogoRasterizer


class HalfBlockLogoRenderer(BaseLogoRenderer):
    """Renders the canonical SVG logo using Unicode half-block characters with 24-bit TrueColor.

    Each terminal cell represents two vertically stacked pixels, providing vivid color fidelity
    and solid geometric fill for the violet emblem and light-slate wordmark.
    """

    name: str = "halfblock"

    def __init__(self, alpha_threshold: int = 35) -> None:
        self.alpha_threshold = alpha_threshold

    def render(
        self,
        target_width: int = 64,
        target_height: Optional[int] = None,
        max_width: int = 76,
        max_height: int = 14,
    ) -> LogoRenderResult:
        """Render logo into rich.text.Text using upper and lower half-block characters."""
        cols, rows = LogoRasterizer.calculate_cell_dimensions(
            target_width=target_width,
            target_height=target_height,
            max_width=max_width,
            max_height=max_height,
            subpixels_per_cell=(1, 2),
        )

        pixel_w = cols
        pixel_h = rows * 2
        scaled_im = LogoRasterizer.get_scaled_image(pixel_w, pixel_h)
        pixels = scaled_im.load()

        text = Text()
        ansi_lines: List[str] = []

        # Horizontal centering padding
        left_pad = max(0, (max_width - cols) // 2)
        right_pad = max(0, max_width - cols - left_pad)
        pad_str = " " * left_pad

        for r in range(rows):
            line_ansi: List[str] = [pad_str]
            if left_pad > 0:
                text.append(pad_str)

            for c in range(cols):
                top_y = r * 2
                bot_y = r * 2 + 1

                r1, g1, b1, a1 = pixels[c, top_y]
                r2, g2, b2, a2 = pixels[c, bot_y]

                top_on = a1 > self.alpha_threshold
                bot_on = a2 > self.alpha_threshold

                if top_on and bot_on:
                    top_hex = f"#{r1:02x}{g1:02x}{b1:02x}"
                    bot_hex = f"#{r2:02x}{g2:02x}{b2:02x}"
                    text.append("▀", style=f"{top_hex} on {bot_hex}")
                    line_ansi.append(
                        f"\033[38;2;{r1};{g1};{b1}m\033[48;2;{r2};{g2};{b2}m▀\033[0m"
                    )
                elif top_on and not bot_on:
                    top_hex = f"#{r1:02x}{g1:02x}{b1:02x}"
                    text.append("▀", style=top_hex)
                    line_ansi.append(f"\033[38;2;{r1};{g1};{b1}m▀\033[0m")
                elif not top_on and bot_on:
                    bot_hex = f"#{r2:02x}{g2:02x}{b2:02x}"
                    text.append("▄", style=bot_hex)
                    line_ansi.append(f"\033[38;2;{r2};{g2};{b2}m▄\033[0m")
                else:
                    text.append(" ")
                    line_ansi.append(" ")

            if right_pad > 0:
                text.append(" " * right_pad)
                line_ansi.append(" " * right_pad)

            text.append("\n")
            ansi_lines.append("".join(line_ansi))

        raw_ansi = "\n".join(ansi_lines)
        return LogoRenderResult(
            text=text,
            raw_ansi=raw_ansi,
            width=max_width,
            height=rows,
            renderer_name=self.name,
        )

