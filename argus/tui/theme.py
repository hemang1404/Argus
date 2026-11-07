"""Argus Terminal UI — Canonical Design System & Color Tokens.

Minimalist Linear/Raycast design language:
- Obsidian base (#08090d), elevated slate surface (#11131a), dark input (#161922)
- Primary text (#f8fafc), secondary text (#94a3b8), muted metadata (#64748b)
- Canonical accent: Electric Indigo (#818cf8), hover accent (#6366f1)
- Semantic states: verified/ready (#3fb950), tip/warn (#d97706), error (#ef4444)
"""

from __future__ import annotations

# Canonical Primary Accent
COLOR_ACCENT = "#818cf8"
COLOR_ACCENT_HOVER = "#6366f1"
COLOR_ACCENT_MUTED = "#4f46e5"

# Neutral Backgrounds
COLOR_BG_BASE = "#08090d"
COLOR_BG_SURFACE = "#11131a"
COLOR_BG_INPUT = "#161922"
COLOR_BG_OPTION = "#0d0f15"
COLOR_BG_HOVER = "#1e2230"
COLOR_BG_ACTIVE = "#1a1e2d"

# Neutral Borders
COLOR_BORDER_NEUTRAL = "#272a38"
COLOR_BORDER_SUBTLE = "#212638"

# Neutral Text & Content
COLOR_TEXT_PRIMARY = "#f8fafc"
COLOR_TEXT_SECONDARY = "#94a3b8"
COLOR_TEXT_MUTED = "#64748b"
COLOR_TEXT_DIM = "#475569"

# Semantic Indicators
COLOR_SUCCESS = "#3fb950"  # Connected / Verified
COLOR_WARNING = "#d97706"  # Tip / Warning / Ready to connect (Amber)
COLOR_ERROR = "#ef4444"    # Error / Failed / Discard
COLOR_ERROR_BG = "#2a1518"
COLOR_ERROR_BORDER = "#7f1d1d"

