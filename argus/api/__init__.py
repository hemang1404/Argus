"""Argus Local API Package.

Provides authenticated local REST and Server-Sent Event (SSE) endpoints for the
Argus Desktop Application and external client adapters.
"""

from __future__ import annotations

from argus.api.server import create_app

__all__ = ["create_app"]
