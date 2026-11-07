"""Argus Terminal Graphics Protocol Engine.

Implements native inline image rendering for modern terminal emulators using:
1. Sixel Graphics Protocol (DEC VT340 / Windows Terminal 1.22+ / WezTerm / Ghostty)
2. Kitty Graphics Protocol (Kitty / Ghostty / WezTerm)
3. iTerm2 Inline Image Protocol (iTerm2 / WezTerm / Mintty / VS Code Terminal)

Falls back gracefully to high-res unicode text when terminal graphics are unsupported.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
import sys
from typing import TYPE_CHECKING, Any, Optional, Tuple

if TYPE_CHECKING:  # Pillow is optional: only graphical banner rendering needs it.
    from PIL import Image as _PILImage


def require_pillow() -> Any:
    """Import and return ``PIL.Image``, lazily, so plain imports need no Pillow."""
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise RuntimeError(
            "Pillow is required for inline terminal graphics. Install it with 'pip install pillow'."
        ) from exc
    return Image


def detect_graphics_protocol() -> Optional[str]:
    """Detect supported inline image protocol based on environment variables."""
    # 1. Kitty Graphics Protocol
    if os.environ.get("KITTY_WINDOW_ID") or os.environ.get("TERM") == "xterm-kitty":
        return "kitty"

    term_prog = os.environ.get("TERM_PROGRAM", "").lower()
    # Ghostty supports both Kitty and Sixel
    if "ghostty" in term_prog:
        return "kitty"

    # WezTerm supports Kitty, iTerm2, and Sixel
    if "wezterm" in term_prog:
        return "kitty"

    # 2. iTerm2 Protocol
    if "iterm" in term_prog or os.environ.get("LC_TERMINAL") == "iTerm2":
        return "iterm2"
    if "mintty" in term_prog:
        return "sixel"

    # 3. Windows Terminal (WT_SESSION) supports Sixel in v1.22+
    if os.environ.get("WT_SESSION"):
        return "sixel"

    term = os.environ.get("TERM", "").lower()
    if any(k in term for k in ("sixel", "vt340", "xterm-256color")):
        # In generic xterm, Sixel is the most common extension
        return "sixel"

    return None


def encode_kitty(png_bytes: bytes, cols: Optional[int] = None) -> str:
    """Encode PNG into Kitty graphics protocol escape sequences."""
    b64 = base64.b64encode(png_bytes).decode("ascii")
    chunks = [b64[i : i + 4096] for i in range(0, len(b64), 4096)]
    out = []
    for i, chunk in enumerate(chunks):
        m = 1 if i < len(chunks) - 1 else 0
        if i == 0:
            ctrl = f"a=T,f=100,m={m}"
            if cols:
                ctrl += f",c={cols}"
        else:
            ctrl = f"m={m}"
        out.append(f"\033_G{ctrl};{chunk}\033\\")
    return "".join(out)


def encode_iterm2(png_bytes: bytes, width_cells: int = 60) -> str:
    """Encode PNG into iTerm2 inline image protocol escape sequence."""
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return f"\033]1337;File=inline=1;width={width_cells};preserveAspectRatio=1:{b64}\a"


def encode_sixel(im: "_PILImage.Image", max_colors: int = 16) -> str:
    """Encode PIL Image into standard DEC Sixel graphics format."""
    Image = require_pillow()
    im = im.convert("RGB")
    width, height = im.size
    quantized = im.quantize(colors=max_colors, method=Image.Quantize.MEDIANCUT)
    palette = quantized.getpalette() or []
    actual_colors = min(max_colors, len(palette) // 3)
    pixels = quantized.load()

    out = ["\033Pq"]
    # Register color palette (Sixel uses percentage 0-100)
    for i in range(actual_colors):
        r = palette[i * 3] * 100 // 255
        g = palette[i * 3 + 1] * 100 // 255
        b = palette[i * 3 + 2] * 100 // 255
        out.append(f"#{i};2;{r};{g};{b}")

    # Sixel processes 6-pixel horizontal bands
    for band_y in range(0, height, 6):
        band_h = min(6, height - band_y)
        colors_in_band = set()
        for by in range(band_h):
            for x in range(width):
                colors_in_band.add(pixels[x, band_y + by])

        for col_idx in sorted(colors_in_band):
            out.append(f"#{col_idx}")
            cur_char = None
            cur_count = 0
            for x in range(width):
                mask = 0
                for by in range(band_h):
                    if pixels[x, band_y + by] == col_idx:
                        mask |= 1 << by
                ch = chr(63 + mask)
                if ch == cur_char:
                    cur_count += 1
                else:
                    if cur_char is not None:
                        out.append(f"!{cur_count}{cur_char}" if cur_count > 3 else cur_char * cur_count)
                    cur_char = ch
                    cur_count = 1
            if cur_char is not None:
                out.append(f"!{cur_count}{cur_char}" if cur_count > 3 else cur_char * cur_count)
            out.append("$")
        out.append("-")

    out.append("\033\\")
    return "".join(out)


def get_default_banner_path(wordmark_only: bool = False) -> Path:
    """Locate the default wordmark or logo PNG.

    Installed wheels ship the assets inside ``argus/assets``; source checkouts
    fall back to the repository-level ``assets`` directory.
    """
    name = "argus-wordmark.png" if wordmark_only else "argus-logo.png"

    package_asset = Path(__file__).resolve().parents[1] / "assets" / name
    if package_asset.is_file():
        return package_asset

    repo_root = Path(__file__).resolve().parent.parent.parent
    return repo_root / "assets" / name


def render_image_escape(
    image_path: Path | str,
    protocol: Optional[str] = None,
    width_cells: int = 60,
) -> Tuple[str, str]:
    """Generate ANSI escape sequence for the given image and protocol.

    Returns:
        (escape_sequence_string, protocol_name_used)
    """
    path = Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(f"Image not found at {path}")

    proto = protocol or detect_graphics_protocol() or "sixel"

    with open(path, "rb") as f:
        png_bytes = f.read()

    if proto == "kitty":
        return encode_kitty(png_bytes, cols=width_cells), "kitty"
    elif proto == "iterm2":
        return encode_iterm2(png_bytes, width_cells=width_cells), "iterm2"
    elif proto == "sixel":
        Image = require_pillow()
        im = Image.open(path)
        return encode_sixel(im), "sixel"
    else:
        raise ValueError(f"Unknown graphics protocol: {proto}")


def print_banner(
    image_path: Optional[Path | str] = None,
    protocol: Optional[str] = None,
    wordmark_only: bool = False,
) -> bool:
    """Print the high-resolution graphical banner to stdout.

    Returns True if an image protocol escape sequence was written.
    """
    target_path = Path(image_path) if image_path else get_default_banner_path(wordmark_only)
    if not target_path.is_file():
        return False

    try:
        escape_str, used_proto = render_image_escape(target_path, protocol=protocol)
        sys.stdout.write("\n" + escape_str + "\n\n")
        sys.stdout.flush()
        return True
    except Exception as e:
        sys.stderr.write(f"[Argus Graphics Warning] Failed rendering inline image: {e}\n")
        return False


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Test Argus Terminal Graphics protocols.")
    parser.add_argument(
        "--protocol",
        "-p",
        choices=["auto", "sixel", "kitty", "iterm2", "all"],
        default="auto",
        help="Target graphics protocol to test (default: auto)",
    )
    parser.add_argument(
        "--wordmark-only",
        action="store_true",
        help="Render wordmark only (SHARY Thin) without emblem.",
    )
    args = parser.parse_args()

    # Reconfigure stdout for utf-8 on Windows
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    print("=" * 64)
    print(" Argus Terminal Inline Graphics Tester")
    print("=" * 64)
    detected = detect_graphics_protocol()
    print(f"Detected Protocol : {detected or 'None (using Sixel default)'}")
    print(f"Active TERM       : {os.environ.get('TERM', 'unset')}")
    print(f"TERM_PROGRAM      : {os.environ.get('TERM_PROGRAM', 'unset')}")
    print(f"WT_SESSION        : {'Present (Windows Terminal)' if os.environ.get('WT_SESSION') else 'Not detected'}")
    print("=" * 64 + "\n")

    protocols_to_test = (
        ["sixel", "kitty", "iterm2"] if args.protocol == "all" else [detected or "sixel"] if args.protocol == "auto" else [args.protocol]
    )

    for p in protocols_to_test:
        print(f"--> Testing protocol [{p.upper()}]:")
        success = print_banner(protocol=p, wordmark_only=args.wordmark_only)
        if success:
            print(f"    Sent [{p}] escape sequences to terminal.\n")
        else:
            print(f"    Failed to send [{p}].\n")

    print("-" * 64)
    print("Did you see the graphic rendered cleanly above? If so, which protocol worked?")
    print("-" * 64)
