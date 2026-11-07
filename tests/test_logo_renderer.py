"""Comprehensive unit tests for Argus TUI multi-backend logo rendering pipeline."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("PIL", reason="Pillow is required for the logo renderers; install 'argus-recovery[tui]'")
pytest.importorskip("textual", reason="Textual is required for the TUI; install 'argus-recovery[tui]'")

from PIL import Image
from rich.text import Text

pytestmark = pytest.mark.graphics

from argus.config import ArgusConfig
from argus.tui.renderers.base import BaseLogoRenderer, LogoRenderResult, MissingLogoAssetError
from argus.tui.renderers.box_drawing import BoxDrawingLogoRenderer
from argus.tui.renderers.braille import BRAILLE_DOT_MAP, BrailleLogoRenderer
from argus.tui.renderers.factory import create_logo_renderer
from argus.tui.renderers.graphics import TerminalGraphicsLogoRenderer
from argus.tui.renderers.halfblock import HalfBlockLogoRenderer
from argus.tui.renderers.rasterizer import (
    CANONICAL_ASPECT_RATIO,
    LogoRasterizer,
    compute_file_sha256,
    locate_canonical_svg,
)
from argus.tui.renderers.text_fallback import TextFallbackLogoRenderer
from argus.tui.widgets.wordmark import ArgusWordmark


# =============================================================================
# 1. Canonical SVG Asset Verification
# =============================================================================

def test_canonical_svg_exists():
    """Verify the canonical Argus logo SVG asset is located and non-empty."""
    svg_path = locate_canonical_svg()
    assert svg_path.is_file()
    assert svg_path.name == "argus-logo.svg"
    assert svg_path.stat().st_size > 500

    sha = compute_file_sha256(svg_path)
    assert len(sha) == 64


def test_missing_logo_asset_error():
    """Verify MissingLogoAssetError is raised when the canonical SVG is missing."""
    with patch.object(Path, "is_file", return_value=False):
        with pytest.raises(MissingLogoAssetError):
            locate_canonical_svg()


# =============================================================================
# 2. Rasterizer & Aspect Ratio Tests
# =============================================================================

def test_rasterizer_master_image():
    """Verify master image is RGBA, correctly sized, and cached in memory."""
    master = LogoRasterizer.get_master_image()
    assert isinstance(master, Image.Image)
    assert master.mode == "RGBA"
    assert master.size == (700, 220)

    # In-memory identity cache
    master_second = LogoRasterizer.get_master_image()
    assert master is master_second


def test_aspect_ratio_calculations():
    """Verify aspect-ratio calculations preserve ~3.18 ratio without distortion or clipping."""
    # Test Braille (subpixels 2x4)
    cols, rows = LogoRasterizer.calculate_cell_dimensions(
        target_width=64,
        target_height=None,
        max_width=76,
        max_height=14,
        subpixels_per_cell=(2, 4),
    )
    assert cols == 64
    assert 9 <= rows <= 11

    # Test Half-block (subpixels 1x2)
    cols_hb, rows_hb = LogoRasterizer.calculate_cell_dimensions(
        target_width=64,
        target_height=None,
        max_width=76,
        max_height=14,
        subpixels_per_cell=(1, 2),
    )
    assert cols_hb == 64
    assert 9 <= rows_hb <= 11

    # Test constrained width
    cols_small, rows_small = LogoRasterizer.calculate_cell_dimensions(
        target_width=40,
        target_height=None,
        max_width=76,
        max_height=14,
        subpixels_per_cell=(2, 4),
    )
    assert cols_small == 40
    assert rows_small == 6


# =============================================================================
# 3. Braille Subpixel Mapping & Rendering Tests
# =============================================================================

def test_braille_dot_bit_positions():
    """Verify standard Unicode Braille bit positions (U+2800 to U+28FF)."""
    assert len(BRAILLE_DOT_MAP) == 8
    # Top-left dot (0,0) -> 0x01
    assert BRAILLE_DOT_MAP[0] == ((0, 0), 0x01)
    # Bottom-right dot (1,3) -> 0x80
    assert BRAILLE_DOT_MAP[7] == ((1, 3), 0x80)

    # Full 8-dot block: chr(0x2800 + 0xFF) = '⣿'
    assert chr(0x2800 + 0xFF) == "⣿"
    # Empty braille: chr(0x2800) = '⠀'
    assert chr(0x2800) == "⠀"


def test_braille_renderer_output():
    """Verify BrailleLogoRenderer produces continuous non-empty output with correct dimensions."""
    renderer = BrailleLogoRenderer()
    result = renderer.render(target_width=64, max_width=76, max_height=14)

    assert isinstance(result, LogoRenderResult)
    assert result.renderer_name == "braille"
    assert result.width == 76
    assert 9 <= result.height <= 11

    # Check that output text contains Braille characters
    raw_str = result.text.plain
    braille_chars = [ch for ch in raw_str if 0x2800 <= ord(ch) <= 0x28FF]
    assert len(braille_chars) > 50

    # Ensure spans have color styling
    assert len(result.text.spans) > 0


# =============================================================================
# 4. Half-Block Pixel Mapping & Rendering Tests
# =============================================================================

def test_halfblock_renderer_output():
    """Verify HalfBlockLogoRenderer produces 24-bit TrueColor upper/lower half-block output."""
    renderer = HalfBlockLogoRenderer()
    result = renderer.render(target_width=64, max_width=76, max_height=14)

    assert isinstance(result, LogoRenderResult)
    assert result.renderer_name == "halfblock"
    assert result.width == 76
    assert 9 <= result.height <= 11

    raw_str = result.text.plain
    halfblock_chars = [ch for ch in raw_str if ch in ("▀", "▄")]
    assert len(halfblock_chars) > 50
    assert len(result.text.spans) > 0


# =============================================================================
# 5. Terminal Graphics & Fallback Tests
# =============================================================================

def test_terminal_graphics_fallback_when_unsupported():
    """Verify TerminalGraphicsLogoRenderer falls back safely to Braille if unsupported."""
    with patch.dict(os.environ, {"TERM": "xterm-color", "WT_SESSION": "", "TERM_PROGRAM": ""}):
        renderer = TerminalGraphicsLogoRenderer(protocol="kitty")
        result = renderer.render(target_width=64, max_width=76, max_height=14)

        assert "fallback" in result.renderer_name
        assert len(result.text.plain) > 0


# =============================================================================
# 6. Text Fallback Renderer Tests
# =============================================================================

def test_text_fallback_renderer():
    """Verify TextFallbackLogoRenderer outputs clean geometric text without box-drawing art."""
    renderer = TextFallbackLogoRenderer()
    result = renderer.render(target_width=64, max_width=76, max_height=14)

    assert result.renderer_name == "text"
    plain = result.text.plain
    assert "A   R   G   U   S" in plain
    assert "◆" in plain
    assert "▲" in plain


# =============================================================================
# 7. Factory Strategy Selection & Configuration Tests
# =============================================================================

def test_create_logo_renderer_selection():
    """Verify factory returns appropriate renderer based on mode and configuration."""
    r_braille = create_logo_renderer(mode="braille")
    assert isinstance(r_braille, BrailleLogoRenderer)

    r_halfblock = create_logo_renderer(mode="halfblock")
    assert isinstance(r_halfblock, HalfBlockLogoRenderer)

    r_text = create_logo_renderer(mode="text")
    assert isinstance(r_text, TextFallbackLogoRenderer)

    r_box = create_logo_renderer(mode="box")
    assert isinstance(r_box, BoxDrawingLogoRenderer)

    r_auto = create_logo_renderer(mode="auto")
    assert isinstance(r_auto, BoxDrawingLogoRenderer)

    # Config object override
    cfg = ArgusConfig(logo_renderer="halfblock")
    r_cfg = create_logo_renderer(config=cfg)
    assert isinstance(r_cfg, HalfBlockLogoRenderer)

    # Environment variable override
    with patch.dict(os.environ, {"ARGUS_LOGO_RENDERER": "text"}):
        r_env = create_logo_renderer()
        assert isinstance(r_env, TextFallbackLogoRenderer)


# =============================================================================
# 8. Widget Caching & Resize Tests
# =============================================================================

def test_wordmark_widget_caching():
    """Verify ArgusWordmark caches render results and recomputes only on dimension changes."""
    widget = ArgusWordmark(renderer_mode="braille")
    t1 = widget.render()
    assert isinstance(t1, Text)

    # Second call without size change returns cached instance
    t2 = widget.render()
    assert t1 is t2

    # Switching renderer mode invalidates cache
    widget.set_renderer_mode("halfblock")
    t3 = widget.render()
    assert t3 is not t1
