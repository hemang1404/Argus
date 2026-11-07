"""Tool Presets: Pre-configured connectors and setup templates for external coding agents.

Supports:
- Aider (.aider.conf.yml)
- Claude Code (.env.claude / ANTHROPIC_BASE_URL)
- OpenCode (opencode.json)
- Continue (.continue/config.json)
- Cursor (.cursor/rules/argus.mdc / settings)
- Antigravity (.agents/config.json)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class Preset:
    """Pre-configured integration recipe for an external AI coding tool."""
    key: str
    display_name: str
    filename: Optional[str]
    description: str
    content: str
    cli_command: str
    instructions: str


AIDER_CONF = """# Argus Aider Configuration (.aider.conf.yml)
# Connects Aider directly to the local Argus Proxy Gateway with streaming patch healing.

openai-api-base: http://127.0.0.1:8000/v1
openai-api-key: argus-local
model: openai/argus/auto
edit-format: diff
stream: true
cache-prompts: false
"""

OPENCODE_CONF = """{
  "$schema": "https://opencode.ai/config.schema.json",
  "providers": {
    "argus": {
      "name": "Argus Local Gateway",
      "baseURL": "http://127.0.0.1:8000/v1",
      "apiKey": "argus-local",
      "models": [
        {
          "id": "argus/auto",
          "name": "Argus Auto (Cost-Optimized Routing)",
          "contextWindow": 128000,
          "maxTokens": 8192
        },
        {
          "id": "argus/cheap",
          "name": "Argus Cheap (Routine Edits)",
          "contextWindow": 128000,
          "maxTokens": 4096
        },
        {
          "id": "argus/strong",
          "name": "Argus Strong (Complex Logic)",
          "contextWindow": 128000,
          "maxTokens": 8192
        },
        {
          "id": "argus/frontier",
          "name": "Argus Frontier (Deep Architectural Work)",
          "contextWindow": 128000,
          "maxTokens": 8192
        }
      ]
    }
  },
  "defaultProvider": "argus",
  "defaultModel": "argus/auto"
}
"""

CONTINUE_CONF = """{
  "models": [
    {
      "title": "Argus Auto",
      "provider": "openai",
      "model": "argus/auto",
      "apiBase": "http://127.0.0.1:8000/v1",
      "apiKey": "argus-local"
    },
    {
      "title": "Argus Cheap",
      "provider": "openai",
      "model": "argus/cheap",
      "apiBase": "http://127.0.0.1:8000/v1",
      "apiKey": "argus-local"
    },
    {
      "title": "Argus Strong",
      "provider": "openai",
      "model": "argus/strong",
      "apiBase": "http://127.0.0.1:8000/v1",
      "apiKey": "argus-local"
    },
    {
      "title": "Argus Frontier",
      "provider": "openai",
      "model": "argus/frontier",
      "apiBase": "http://127.0.0.1:8000/v1",
      "apiKey": "argus-local"
    }
  ],
  "slashCommands": [
    {
      "name": "recover",
      "description": "Run closed-loop test recovery with Argus"
    }
  ]
}
"""

CLAUDE_ENV = """# Claude Code Environment Configuration (.env.claude)
# Directs Claude Code to use the local Argus Gateway via Anthropic Messages Protocol.

ANTHROPIC_BASE_URL=http://127.0.0.1:8000/v1
ANTHROPIC_API_KEY=argus-local
CLAUDE_CODE_MODEL=argus/auto
"""

CURSOR_RULES = """---
description: Argus AI Routing & Resilient Code Recovery Integration
globs: *
---
# Argus Cursor Integration

Argus acts as your local inference router and patch reconciliation layer.
All model completions are mediated by the local proxy on http://127.0.0.1:8000.

### Settings Configuration:
1. Open Cursor Settings -> Models -> OpenAI API.
2. Toggle "Override OpenAI Base URL" to ON.
3. Set Base URL: `http://127.0.0.1:8000/v1`
4. Set API Key: `argus-local`
5. Model Names: Add `argus/auto`, `argus/cheap`, `argus/strong`, `argus/frontier`

### Terminal Recovery:
When a test fails, run:
`argus run --file <failing_file> --test "pytest <test_file>"`
"""

ANTIGRAVITY_CONF = """{
  "name": "Argus Local Gateway",
  "endpoint": "http://127.0.0.1:8000/v1",
  "apiKey": "argus-local",
  "models": {
    "default": "argus/auto",
    "cheap": "argus/cheap",
    "strong": "argus/strong",
    "frontier": "argus/frontier"
  },
  "healing": {
    "enabled": true,
    "strategy": "5-tier-resilient"
  }
}
"""


PRESETS_REGISTRY: Dict[str, Preset] = {
    "aider": Preset(
        key="aider",
        display_name="Aider CLI",
        filename=".aider.conf.yml",
        description="Pair programming CLI with diff and whole-file editing support",
        content=AIDER_CONF,
        cli_command="aider --openai-api-base http://127.0.0.1:8000/v1 --openai-api-key argus-local --model openai/argus/auto",
        instructions="Run 'aider' in your project root or pass flags directly. The config file automatically routes to Argus.",
    ),
    "opencode": Preset(
        key="opencode",
        display_name="OpenCode",
        filename="opencode.json",
        description="Terminal AI coding agent with extensible provider schema",
        content=OPENCODE_CONF,
        cli_command="opencode --model argus/auto",
        instructions="Place 'opencode.json' in your project root or ~/.config/opencode/opencode.json.",
    ),
    "continue": Preset(
        key="continue",
        display_name="Continue (VS Code / JetBrains)",
        filename=".continue/config.json",
        description="Open-source AI code assistant inside VS Code and JetBrains IDEs",
        content=CONTINUE_CONF,
        cli_command="# Select 'Argus Auto' from the model dropdown in the Continue panel",
        instructions="Merge or copy into ~/.continue/config.json or .continue/config.json in the workspace root.",
    ),
    "claude": Preset(
        key="claude",
        display_name="Claude Code CLI",
        filename=".env.claude",
        description="Anthropic's terminal agent interacting over the Messages protocol",
        content=CLAUDE_ENV,
        cli_command="export ANTHROPIC_BASE_URL=http://127.0.0.1:8000/v1 && claude",
        instructions="Source .env.claude before launching 'claude', or set ANTHROPIC_BASE_URL in your shell.",
    ),
    "cursor": Preset(
        key="cursor",
        display_name="Cursor IDE",
        filename=".cursorrules",
        description="AI-first code editor with OpenAI endpoint overrides",
        content=CURSOR_RULES,
        cli_command="# In Cursor Settings -> Models: Base URL: http://127.0.0.1:8000/v1",
        instructions="Add .cursorrules to workspace root, then configure Cursor Settings -> Models -> Override OpenAI Base URL.",
    ),
    "antigravity": Preset(
        key="antigravity",
        display_name="Antigravity IDE & CLI",
        filename=".agents/argus-gateway.json",
        description="Google Antigravity autonomous agent pairing environment",
        content=ANTIGRAVITY_CONF,
        cli_command="agy run --endpoint http://127.0.0.1:8000/v1",
        instructions="Saves gateway integration settings in .agents/argus-gateway.json for workspace-level routing.",
    ),
}


def list_presets() -> List[Preset]:
    """Return all available integration presets."""
    return list(PRESETS_REGISTRY.values())


def get_preset(name: str) -> Optional[Preset]:
    """Lookup a preset by key name (case-insensitive)."""
    return PRESETS_REGISTRY.get(name.lower().strip())


def export_preset(
    name: str,
    target_dir: Path | str = ".",
    overwrite: bool = False,
) -> Path:
    """Write the preset configuration file to the target directory."""
    preset = get_preset(name)
    if not preset:
        available = ", ".join(PRESETS_REGISTRY.keys())
        raise ValueError(f"Unknown preset '{name}'. Available presets: {available}")

    if not preset.filename:
        raise ValueError(f"Preset '{name}' does not have a static configuration file.")

    target_path = (Path(target_dir) / preset.filename).resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)

    if target_path.exists() and not overwrite:
        raise FileExistsError(
            f"File already exists: {target_path}. Use overwrite=True or --overwrite to replace."
        )

    target_path.write_text(preset.content, encoding="utf-8")
    return target_path
