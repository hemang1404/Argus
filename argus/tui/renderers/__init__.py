"""Argus TUI logo rendering engine."""

from argus.tui.renderers.base import (
    BaseLogoRenderer,
    LogoRenderResult,
    MissingLogoAssetError,
)
from argus.tui.renderers.box_drawing import BoxDrawingLogoRenderer
from argus.tui.renderers.braille import BrailleLogoRenderer
from argus.tui.renderers.factory import create_logo_renderer
from argus.tui.renderers.graphics import TerminalGraphicsLogoRenderer
from argus.tui.renderers.halfblock import HalfBlockLogoRenderer
from argus.tui.renderers.rasterizer import (
    CANONICAL_ASPECT_RATIO,
    LogoRasterizer,
    locate_canonical_svg,
)
from argus.tui.renderers.text_fallback import TextFallbackLogoRenderer

__all__ = [
    "BaseLogoRenderer",
    "LogoRenderResult",
    "MissingLogoAssetError",
    "BoxDrawingLogoRenderer",
    "BrailleLogoRenderer",
    "HalfBlockLogoRenderer",
    "TerminalGraphicsLogoRenderer",
    "TextFallbackLogoRenderer",
    "create_logo_renderer",
    "LogoRasterizer",
    "CANONICAL_ASPECT_RATIO",
    "locate_canonical_svg",
]

