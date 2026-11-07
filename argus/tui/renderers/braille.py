"""High-definition Unicode Braille (2x4 dot matrix) logo renderer."""

from __future__ import annotations

from typing import List, Optional, Tuple

from rich.text import Text

from argus.tui.renderers.base import BaseLogoRenderer, LogoRenderResult
from argus.tui.renderers.rasterizer import LogoRasterizer

# Unicode Braille 2x4 subpixel dot mapping to bit positions
# Bit 0 (0x01): (0, 0) - top-left
# Bit 1 (0x02): (0, 1) - middle-top-left
# Bit 2 (0x04): (0, 2) - middle-bottom-left
# Bit 3 (0x08): (1, 0) - top-right
# Bit 4 (0x10): (1, 1) - middle-top-right
# Bit 5 (0x20): (1, 2) - middle-bottom-right
# Bit 6 (0x40): (0, 3) - bottom-left
# Bit 7 (0x80): (1, 3) - bottom-right
BRAILLE_DOT_MAP: List[Tuple[Tuple[int, int], int]] = [
    ((0, 0), 0x01),
    ((0, 1), 0x02),
    ((0, 2), 0x04),
    ((1, 0), 0x08),
    ((1, 1), 0x10),
    ((1, 2), 0x20),
    ((0, 3), 0x40),
    ((1, 3), 0x80),
]


class BrailleLogoRenderer(BaseLogoRenderer):
    """Renders the canonical SVG logo using a high-resolution 2x4 Unicode Braille dot matrix.

    Provides 8 subpixels per character cell, ensuring smooth continuous diagonals,
    symmetrical curved paths, and crisp hairline strokes without box-drawing gaps.
    """

    name: str = "braille"

    def __init__(
        self,
        alpha_threshold: int = 35,
        luminance_threshold: float = 30.0,
    ) -> None:
        self.alpha_threshold = alpha_threshold
        self.luminance_threshold = luminance_threshold

    def render(
        self,
        target_width: int = 64,
        target_height: Optional[int] = None,
        max_width: int = 76,
        max_height: int = 14,
    ) -> LogoRenderResult:
        """Render logo into rich.text.Text using Unicode Braille glyphs."""
        cols, rows = LogoRasterizer.calculate_cell_dimensions(
            target_width=target_width,
            target_height=target_height,
            max_width=max_width,
            max_height=max_height,
            subpixels_per_cell=(2, 4),
        )

        pixel_w = cols * 2
        pixel_h = rows * 4
        scaled_im = LogoRasterizer.get_scaled_image(pixel_w, pixel_h)
        pixels = scaled_im.load()

        text = Text()
        ansi_lines: List[str] = []

        # Horizontal centering padding if cols < max_width
        left_pad = max(0, (max_width - cols) // 2)
        right_pad = max(0, max_width - cols - left_pad)
        pad_str = " " * left_pad

        for r in range(rows):
            line_ansi: List[str] = [pad_str]
            if left_pad > 0:
                text.append(pad_str)

            for c in range(cols):
                cell_x = c * 2
                cell_y = r * 4
                mask = 0
                active_colors: List[Tuple[int, int, int]] = []

                for (dx, dy), bit in BRAILLE_DOT_MAP:
                    px = cell_x + dx
                    py = cell_y + dy
                    if px < pixel_w and py < pixel_h:
                        r_c, g_c, b_c, a_c = pixels[px, py]
                        if a_c > self.alpha_threshold:
                            lum = 0.299 * r_c + 0.587 * g_c + 0.114 * b_c
                            if lum > self.luminance_threshold:
                                mask |= bit
                                active_colors.append((r_c, g_c, b_c))

                if mask == 0:
                    text.append(" ")
                    line_ansi.append(" ")
                else:
                    # Calculate dominant average color for the cell
                    avg_r = sum(col[0] for col in active_colors) // len(active_colors)
                    avg_g = sum(col[1] for col in active_colors) // len(active_colors)
                    avg_b = sum(col[2] for col in active_colors) // len(active_colors)
                    style_str = f"#{avg_r:02x}{avg_g:02x}{avg_b:02x}"
                    glyph = chr(0x2800 + mask)
                    text.append(glyph, style=style_str)
                    line_ansi.append(f"\033[38;2;{avg_r};{avg_g};{avg_b}m{glyph}\033[0m")

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

