"""Canonical SVG asset loader, validator, and high-performance raster cache."""

from __future__ import annotations

import concurrent.futures
import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional, Tuple

from argus.tui.renderers.base import MissingLogoAssetError, PillowUnavailableError

# Canonical SVG dimensions and aspect ratio
CANONICAL_VIEWBOX_WIDTH = 700
CANONICAL_VIEWBOX_HEIGHT = 220
CANONICAL_ASPECT_RATIO = CANONICAL_VIEWBOX_WIDTH / CANONICAL_VIEWBOX_HEIGHT  # ~3.1818

_PILLOW_HINT = (
    "Pillow is required for the braille, half-block and terminal-graphics logo renderers. "
    "Install it with 'pip install pillow', or use ARGUS_LOGO_RENDERER=auto (default), "
    "'box' or 'text', which need no image dependencies."
)


def require_pillow() -> Any:
    """Import and return ``PIL.Image``.

    Imported lazily so that installing the TUI never forces the image dependency:
    the default 'auto' renderer is pure box-drawing text.

    Raises:
        PillowUnavailableError: If Pillow is not installed.
    """
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise PillowUnavailableError(_PILLOW_HINT) from exc
    return Image


def locate_canonical_svg() -> Path:
    """Locate the canonical Argus logo SVG asset.

    Raises:
        MissingLogoAssetError: If the canonical SVG cannot be located on disk.
    """
    # Installed wheels carry the asset inside the package; editable/development
    # checkouts fall back to the repository-level assets directory.
    package_asset = Path(__file__).resolve().parents[2] / "assets" / "argus-logo.svg"
    if package_asset.is_file():
        return package_asset

    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    candidate = repo_root / "assets" / "argus-logo.svg"
    if candidate.is_file():
        return candidate

    # Fallback to current working directory
    cwd_candidate = Path("assets") / "argus-logo.svg"
    if cwd_candidate.is_file():
        return cwd_candidate.resolve()

    raise MissingLogoAssetError(
        f"Canonical Argus logo SVG not found at '{candidate}'. "
        "Cannot approximate branding from screenshots. Please ensure 'assets/argus-logo.svg' exists."
    )


def compute_file_sha256(path: Path) -> str:
    """Compute the SHA256 hexadecimal hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class LogoRasterizer:
    """Manages rasterization and memory/disk caching of the canonical Argus logo SVG."""

    _master_image: Optional[Any] = None
    _cached_svg_hash: Optional[str] = None

    @classmethod
    def get_master_image(cls) -> Any:
        """Load or rasterize the master canonical RGBA image buffer.

        Guarantees that the SVG is the single source of truth.
        """
        Image = require_pillow()
        svg_path = locate_canonical_svg()
        current_hash = compute_file_sha256(svg_path)

        # In-memory hit
        if cls._master_image is not None and cls._cached_svg_hash == current_hash:
            return cls._master_image

        # Check disk cache
        cache_dir = svg_path.parent / ".cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached_png = cache_dir / f"argus-logo-{current_hash[:16]}.png"
        canonical_png = svg_path.parent / "argus-logo.png"

        loaded_im: Optional[Any] = None

        if cached_png.is_file():
            try:
                loaded_im = Image.open(cached_png).convert("RGBA")
            except (OSError, ValueError):
                loaded_im = None

        if loaded_im is None and canonical_png.is_file():
            try:
                im = Image.open(canonical_png).convert("RGBA")
                if im.size == (CANONICAL_VIEWBOX_WIDTH, CANONICAL_VIEWBOX_HEIGHT):
                    loaded_im = im
            except (OSError, ValueError):
                loaded_im = None

        # If cache missing or stale, rasterize SVG dynamically
        if loaded_im is None:
            loaded_im = cls._rasterize_svg_playwright(svg_path)
            if loaded_im is not None:
                try:
                    loaded_im.save(cached_png, format="PNG")
                    loaded_im.save(canonical_png, format="PNG")
                except OSError:
                    pass

        if loaded_im is None:
            # Fallback: if browser rasterization fails, check canonical PNG
            if canonical_png.is_file():
                loaded_im = Image.open(canonical_png).convert("RGBA")
            else:
                raise RuntimeError(
                    f"Failed to rasterize '{svg_path}' and no raster cache was available."
                )

        cls._master_image = loaded_im
        cls._cached_svg_hash = current_hash
        return cls._master_image

    @classmethod
    def _rasterize_svg_playwright(cls, svg_path: Path) -> Optional[Any]:
        """Rasterize the SVG into a transparent RGBA PNG buffer using headless Chromium/Edge."""
        try:
            import asyncio
            import io

            from playwright.async_api import async_playwright

            with open(svg_path, "r", encoding="utf-8") as f:
                svg_content = f.read()

            async def _render() -> bytes:
                async with async_playwright() as p:
                    browser = await p.chromium.launch(channel="msedge")
                    page = await browser.new_page(
                        viewport={"width": CANONICAL_VIEWBOX_WIDTH, "height": CANONICAL_VIEWBOX_HEIGHT},
                        device_scale_factor=1,
                    )
                    html = f"""<!DOCTYPE html>
<html>
<head><style>* {{ margin: 0; padding: 0; box-sizing: border-box; }} body {{ background: transparent; overflow: hidden; }}</style></head>
<body>{svg_content}</body>
</html>"""
                    await page.set_content(html)
                    png_bytes = await page.screenshot(omit_background=True)
                    await browser.close()
                    return png_bytes

            # Textual runs an asyncio event loop on this thread, so asyncio.run()
            # cannot be used inline; run the rasterization in a worker thread instead.
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                png_data = asyncio.run(_render())
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    png_data = pool.submit(lambda: asyncio.run(_render())).result()

            Image = require_pillow()
            return Image.open(io.BytesIO(png_data)).convert("RGBA")
        except Exception:
            # Any failure (missing Playwright, no browser, network sandbox) degrades
            # to the cached PNG / text fallback handled by the caller.
            return None

    @classmethod
    @lru_cache(maxsize=32)
    def get_scaled_image(cls, width_px: int, height_px: int) -> Any:
        """Return a downsampled/resampled RGBA image at the target pixel dimensions.

        Uses Lanczos high-quality resampling with caching.
        """
        Image = require_pillow()
        master = cls.get_master_image()
        if master.size == (width_px, height_px):
            return master
        return master.resize((width_px, height_px), Image.Resampling.LANCZOS)

    @classmethod
    def calculate_cell_dimensions(
        cls,
        target_width: int,
        target_height: Optional[int],
        max_width: int,
        max_height: int,
        subpixels_per_cell: Tuple[int, int],  # (sub_x, sub_y), e.g. (2, 4) for Braille, (1, 2) for Halfblock
    ) -> Tuple[int, int]:
        """Compute character cell width and height preserving canonical aspect ratio without clipping.

        Args:
            target_width: Requested cell width.
            target_height: Requested cell height (optional).
            max_width: Maximum allowed cell width.
            max_height: Maximum allowed cell height.
            subpixels_per_cell: (horizontal subpixels, vertical subpixels).

        Returns:
            (cols, rows) in terminal character cells.
        """
        cols = min(target_width, max_width)
        sub_x, sub_y = subpixels_per_cell

        # Standard terminal character cells have an aspect ratio of ~1:2 (width:height).
        # Subpixel resolution per cell:
        # dot_aspect_ratio = (sub_x / 1.0) / (sub_y / 2.0) = (2 * sub_x) / sub_y
        # For Braille (2, 4): (2 * 2) / 4 = 1.0 (perfectly square dots)
        # For Halfblock (1, 2): (2 * 1) / 2 = 1.0 (perfectly square pixels)
        dot_aspect_ratio = (2.0 * sub_x) / float(sub_y)

        if target_height is not None:
            rows = min(target_height, max_height)
        else:
            # Compute rows from cols to match CANONICAL_ASPECT_RATIO
            # total_pixel_width = cols * sub_x
            # total_pixel_height = total_pixel_width / CANONICAL_ASPECT_RATIO * dot_aspect_ratio
            total_pixel_w = cols * sub_x
            total_pixel_h = total_pixel_w / (CANONICAL_ASPECT_RATIO * dot_aspect_ratio)
            rows = max(1, int(round(total_pixel_h / sub_y)))
            if rows > max_height:
                rows = max_height
                # Scale cols down to avoid vertical clipping
                total_pixel_h = rows * sub_y
                total_pixel_w = total_pixel_h * (CANONICAL_ASPECT_RATIO * dot_aspect_ratio)
                cols = max(1, int(round(total_pixel_w / sub_x)))

        return cols, rows
