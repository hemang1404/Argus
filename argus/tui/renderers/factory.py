"""Logo renderer factory and capability-aware strategy selection."""

from __future__ import annotations

import os
from typing import Optional

from argus.config import ArgusConfig
from argus.tui.renderers.base import BaseLogoRenderer
from argus.tui.renderers.box_drawing import BoxDrawingLogoRenderer
from argus.tui.renderers.braille import BrailleLogoRenderer
from argus.tui.renderers.graphics import TerminalGraphicsLogoRenderer
from argus.tui.renderers.halfblock import HalfBlockLogoRenderer
from argus.tui.renderers.text_fallback import TextFallbackLogoRenderer


def create_logo_renderer(
    mode: Optional[str] = None,
    config: Optional[ArgusConfig] = None,
) -> BaseLogoRenderer:
    """Create and return a LogoRenderer strategy based on configuration and capability detection.

    Selection Behavior:
    - 'auto': Selects the 5-row architectural box-drawing renderer (smooth continuous curves, no dots).
    - 'box': Explicit 5-row architectural box-drawing renderer.
    - 'braille': Explicit Braille 2x4 subpixel dot matrix.
    - 'halfblock': Explicit 24-bit TrueColor upper/lower half-block rendering.
    - 'sixel': Sixel terminal graphics if supported, otherwise graceful fallback to box.
    - 'kitty': Kitty terminal graphics if supported, otherwise graceful fallback to box.
    - 'text': Pure text and geometric character fallback.
    """
    # 1. Determine requested mode: argument > environment variable > config > "auto"
    selected_mode = (
        mode
        or os.environ.get("ARGUS_LOGO_RENDERER")
        or (getattr(config, "logo_renderer", None) if config else None)
        or "auto"
    ).lower().strip()

    layout = (
        os.environ.get("ARGUS_LOGO_LAYOUT")
        or getattr(config, "logo_layout", None)
        or "wordmark"
    ).lower().strip()

    if selected_mode in ("inline", "eye", "box-inline"):
        return BoxDrawingLogoRenderer(layout="inline")
    elif selected_mode in ("wordmark", "clean"):
        return BoxDrawingLogoRenderer(layout="wordmark")
    elif selected_mode in ("box", "claude", "5row", "box_drawing"):
        return BoxDrawingLogoRenderer(layout=layout)
    elif selected_mode == "braille":
        return BrailleLogoRenderer()
    elif selected_mode == "halfblock":
        return HalfBlockLogoRenderer()
    elif selected_mode == "sixel":
        graphics = TerminalGraphicsLogoRenderer(protocol="sixel")
        if graphics.is_protocol_supported():
            return graphics
        return BoxDrawingLogoRenderer(layout=layout)
    elif selected_mode == "kitty":
        graphics = TerminalGraphicsLogoRenderer(protocol="kitty")
        if graphics.is_protocol_supported():
            return graphics
        return BoxDrawingLogoRenderer(layout=layout)
    elif selected_mode == "text":
        return TextFallbackLogoRenderer()
    elif selected_mode == "auto":
        # Default to clean architectural box-drawing
        return BoxDrawingLogoRenderer(layout=layout)
    else:
        # Unknown mode: safe fallback
        return BoxDrawingLogoRenderer(layout=layout)

