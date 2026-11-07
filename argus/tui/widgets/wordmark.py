"""Argus canonical terminal logo widget with multi-backend rendering."""

from __future__ import annotations

from typing import Optional

from rich.text import Text
from textual.events import Resize
from textual.widget import Widget

from argus.config import ArgusConfig
from argus.tui.renderers.base import BaseLogoRenderer, LogoRenderResult
from argus.tui.renderers.factory import create_logo_renderer
from argus.tui.renderers.text_fallback import TextFallbackLogoRenderer

CANVAS_WIDTH = 76
MAX_LOGO_HEIGHT = 18


class ArgusWordmark(Widget):
    """Renders the canonical Argus emblem and wordmark via a modular rendering pipeline.

    Supports Unicode Braille, Half-block, Sixel, Kitty, and Text fallback rendering
    with aspect-ratio preservation and dynamic terminal resize adaptation.
    """

    def __init__(
        self,
        version: str = "v0.2.0",
        renderer_mode: Optional[str] = None,
        config: Optional[ArgusConfig] = None,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.version = version
        self.renderer_mode = renderer_mode
        self.config = config or ArgusConfig()
        self._renderer: Optional[BaseLogoRenderer] = None
        self._cached_result: Optional[LogoRenderResult] = None
        self._cached_width: int = 0
        self._cached_height: int = 0

    @property
    def renderer(self) -> BaseLogoRenderer:
        """Active logo rendering strategy."""
        if self._renderer is None:
            self._renderer = create_logo_renderer(
                mode=self.renderer_mode,
                config=self.config,
            )
        return self._renderer

    def set_renderer_mode(self, mode: str) -> None:
        """Switch active rendering strategy and invalidate cache."""
        self.renderer_mode = mode
        self._renderer = create_logo_renderer(mode=mode, config=self.config)
        self._cached_result = None
        self.refresh()

    def on_resize(self, event: Resize) -> None:
        """Invalidate rendered cache when the widget or terminal dimensions change."""
        new_width = event.size.width
        new_height = event.size.height
        if new_width != self._cached_width or new_height != self._cached_height:
            self._cached_result = None
            self.refresh()

    def render(self) -> Text:
        """Render the logo through the active rendering strategy with caching."""
        available_width = self.size.width if self.size.width > 0 else CANVAS_WIDTH
        available_height = self.size.height if self.size.height > 0 else MAX_LOGO_HEIGHT

        # Target width clamped to canvas width and available width
        target_width = min(available_width, CANVAS_WIDTH)

        # Cache check
        if (
            self._cached_result is not None
            and self._cached_width == target_width
            and self._cached_height == available_height
        ):
            return self._cached_result.text

        try:
            result = self.renderer.render(
                target_width=target_width,
                target_height=None,
                max_width=target_width,
                max_height=min(available_height, MAX_LOGO_HEIGHT),
            )
        except Exception:
            # Absolute safety: never crash TUI startup on render failure
            fallback = TextFallbackLogoRenderer()
            result = fallback.render(
                target_width=target_width,
                max_width=target_width,
                max_height=min(available_height, MAX_LOGO_HEIGHT),
            )

        self._cached_result = result
        self._cached_width = target_width
        self._cached_height = available_height

        return result.text
