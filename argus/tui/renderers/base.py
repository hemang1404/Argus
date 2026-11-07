"""Base abstractions and data structures for Argus logo renderers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from rich.text import Text


@dataclass(frozen=True)
class LogoRenderResult:
    """Rendered logo representation ready for terminal display."""

    text: Text
    raw_ansi: str
    width: int
    height: int
    renderer_name: str


class MissingLogoAssetError(FileNotFoundError):
    """Raised when the canonical Argus logo SVG asset cannot be located."""
    pass


class PillowUnavailableError(RuntimeError):
    """Raised when an image-based logo renderer is used without Pillow installed."""
    pass


class BaseLogoRenderer(ABC):
    """Abstract base class for all terminal logo rendering strategies."""

    name: str = "base"

    @abstractmethod
    def render(
        self,
        target_width: int = 64,
        target_height: Optional[int] = None,
        max_width: int = 76,
        max_height: int = 14,
    ) -> LogoRenderResult:
        """Render the canonical logo within the target cell dimensions.

        Args:
            target_width: Target column width in character cells.
            target_height: Target line height in character cells (auto-computed if None).
            max_width: Upper bound on character cell width.
            max_height: Upper bound on character cell height.

        Returns:
            LogoRenderResult containing Rich Text and metadata.
        """
        pass

