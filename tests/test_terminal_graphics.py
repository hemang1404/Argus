"""Tests for terminal graphics protocols and inline image rendering."""

from pathlib import Path

import pytest

pytest.importorskip("PIL", reason="Pillow is required for terminal graphics; install 'argus-recovery[tui]'")

from PIL import Image

pytestmark = pytest.mark.graphics

from argus.utils.terminal_graphics import (
    detect_graphics_protocol,
    encode_iterm2,
    encode_kitty,
    encode_sixel,
    get_default_banner_path,
    render_image_escape,
)


def test_encode_kitty():
    fake_png = b"\x89PNG\r\n\x1a\nfakecontent"
    out = encode_kitty(fake_png, cols=50)
    assert out.startswith("\033_G")
    assert out.endswith("\033\\")
    assert "a=T,f=100" in out
    assert "c=50" in out


def test_encode_iterm2():
    fake_png = b"\x89PNG\r\n\x1a\nfakecontent"
    out = encode_iterm2(fake_png, width_cells=45)
    assert out.startswith("\033]1337;File=inline=1;width=45")
    assert out.endswith("\a")


def test_encode_sixel(tmp_path: Path):
    im = Image.new("RGB", (30, 18), color=(10, 20, 30))
    out = encode_sixel(im, max_colors=4)
    assert out.startswith("\033Pq")
    assert out.endswith("\033\\")
    assert "#0;2;" in out


def test_get_default_banner_paths():
    logo_path = get_default_banner_path(wordmark_only=False)
    wordmark_path = get_default_banner_path(wordmark_only=True)
    assert logo_path.is_file()
    assert wordmark_path.is_file()


def test_render_image_escape_sixel():
    logo_path = get_default_banner_path(wordmark_only=False)
    seq, proto = render_image_escape(logo_path, protocol="sixel")
    assert proto == "sixel"
    assert seq.startswith("\033Pq")
    assert seq.endswith("\033\\")


def test_render_image_escape_kitty():
    wordmark_path = get_default_banner_path(wordmark_only=True)
    seq, proto = render_image_escape(wordmark_path, protocol="kitty")
    assert proto == "kitty"
    assert seq.startswith("\033_G")
    assert seq.endswith("\033\\")


def test_render_image_escape_invalid():
    with pytest.raises(FileNotFoundError):
        render_image_escape("non_existent_file.png")

    logo_path = get_default_banner_path()
    with pytest.raises(ValueError):
        render_image_escape(logo_path, protocol="unsupported_protocol")

