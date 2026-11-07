"""Recovery Controller: Coordinates non-destructive execution, inference calls, and iterative repair."""

from __future__ import annotations

import ast
import hashlib
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from argus.config import ArgusConfig
from argus.patch.fuzzy_engine import PatchResult, RefusalReason, ResilientPatchEngine
from argus.patch.parser import PatchBlock, PatchParser
from argus.patch.paths import PatchTargetError, validate_patch_blocks
from argus.providers.models import ProviderConfigurationError
from argus.providers.adapter import OpenAICompatibleAdapter, ProviderAdapter, ProviderError
from argus.providers.models import ProviderConfig, PROVIDER_PRESETS
from argus.runtime.budget import (
    ARGUS_SYSTEM_PROMPT,
    GlobalExperimentBudget,
    TaskConditionBudget,
    count_prompt_tokens,
    is_conclusive_provider_refusal,
)
from argus.runtime.candidate import CandidateArtifactStore
from argus.runtime.events import JsonLinesEmitter
from argus.runtime.runner import SubprocessTestRunner, TestExecutionResult
from argus.runtime.workspace import WorkspaceManager
from argus.utils.redaction import scrub_secrets


class LocalizationError(RuntimeError):
    """Raised when reliable code context cannot be localized in the target file."""


@dataclass
class AttemptRecord:
    """Telemetry record for a single recovery attempt."""
    attempt: int
    duration_ms: float
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    tier_used: Optional[int]
    patch_success: bool
    test_passed: bool
    error_summary: str = ""
    model_used: Optional[str] = None
    prompt_tokens_est: int = 0
    reserved_cost_usd: float = 0.0
    transmitted_payload: Dict[str, Any] = field(default_factory=dict)
    prompt_text: str = ""
    raw_response: str = ""
    parsed_patch: str = ""
    context_window: Dict[str, Any] = field(default_factory=dict)
    cached_tokens: int = 0
    # Patch provenance (additive; existing event consumers tolerate new fields).
    matched_file: Optional[str] = None
    patch_strategy: str = ""
    patch_confidence: Optional[float] = None
    refusal_reason: Optional[str] = None


@dataclass
class RecoveryOutcome:
    """Final result of an Argus recovery session."""
    status: str  # "SUCCESS", "FAILED_MAX_ATTEMPTS", "BUDGET_EXCEEDED", "DEADLINE_EXCEEDED", "PROVIDER_RATE_LIMITED", "CONTAINER_SETUP_ERROR", "ALREADY_PASSING", "CONFIG_ERROR", "LOCALIZATION_FAILED"
    total_attempts: int
    total_cost_usd: float
    total_duration_ms: float
    unified_diff: str = ""
    history: List[AttemptRecord] = field(default_factory=list)
    message: str = ""
    patched_content: Optional[str] = None
    target_file: Optional[str] = None
    changed_files: List[str] = field(default_factory=list)
    tests_executed: List[str] = field(default_factory=list)
    diagnostics: str = ""
    run_id: str = ""
    baseline_hash: str = ""
    candidate_hash: str = ""
    verified: bool = False
    approved: bool = False
    tier_used: Optional[int] = None
    effort_used: Optional[str] = None
    routing_reasons: List[str] = field(default_factory=list)
    spend_uncertain: bool = False
    quarantined_usd: float = 0.0
    total_reserved_usd: float = 0.0
    prompt_text: str = ""
    raw_response: str = ""
    unapplied_patch: str = ""
    context_window: Dict[str, Any] = field(default_factory=dict)
    # Observable provider resolution result (notably for explicit 'auto').
    selected_provider: Optional[str] = None


def extract_context_frame(
    code: str,
    file_path: str,
    error_feedback: str,
    window_radius: int = 25,
    problem_statement: Optional[str] = None,
) -> tuple[str, int, int, Optional[int]]:
    """Extract a localized context window around the failure site from error traceback and problem statement.

    Args:
        code: The complete current code of the target file.
        file_path: The target file path (relative or absolute).
        error_feedback: The traceback or failure feedback string.
        window_radius: The number of lines before and after the failure site to include.
        problem_statement: Optional task description or problem statement for symbol localization.

    Returns:
        tuple of (annotated_code_snippet, start_line_1_indexed, end_line_1_indexed, target_line_found)
        When localization is impossible on a large file (>100 lines), returns ("", 0, 0, None).
    """
    lines = code.splitlines()
    total_lines = len(lines)
    if total_lines == 0:
        return ("", 0, 0, None)

    # 1. For small files (<= 60 lines), context minimization is unnecessary and full file is safest
    if total_lines <= 60:
        annotated = "\n".join(f"{i+1:4d} | {line}" for i, line in enumerate(lines))
        return (annotated, 1, total_lines, None)

    # 2. Extract potential line numbers matching target file in error_feedback or problem_statement
    target_name = Path(file_path).name
    search_text = f"{error_feedback or ''}\n{problem_statement or ''}"
    # Match File "path/target.py", line 123, target.py:123, GitHub URL #L123, and prose references
    patterns = [
        rf'(?:^|[/\\"\s]){re.escape(target_name)}["\']?,\s*line\s+(\d+)',
        rf'(?:^|[/\\"\s]){re.escape(target_name)}:(\d+)',
        rf'(?:^|[/\\"\s#]){re.escape(target_name)}[^\s#]*#L(\d+)',
        rf'(?:^|[/\\"\s]){re.escape(target_name)}["\']?\s*(?:at\s+)?line\s+(\d+)',
        rf'line\s+(\d+)\s+(?:of|in)\s+["\']?(?:[^\s\'"]*[/\\])?{re.escape(target_name)}',
    ]

    candidate_lines: list[int] = []
    for pattern in patterns:
        for match in re.finditer(pattern, search_text, re.IGNORECASE):
            try:
                lineno = int(match.group(1))
                if 1 <= lineno <= total_lines:
                    candidate_lines.append(lineno)
            except (ValueError, IndexError):
                continue

    # Pick the deepest/innermost frame (last occurrence in Python traceback)
    target_line = candidate_lines[-1] if candidate_lines else None

    if target_line is not None:
        # Check AST for enclosing function or class node to guarantee complete semantic definition
        try:
            tree = ast.parse(code)
            best_node = None
            min_span = float("inf")
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    s = getattr(node, "lineno", 0)
                    e = getattr(node, "end_lineno", s)
                    if s <= target_line <= e:
                        span = e - s
                        if span < min_span:
                            min_span = span
                            best_node = node
            if best_node is not None:
                node_start = getattr(best_node, "lineno", target_line)
                node_end = getattr(best_node, "end_lineno", target_line)
                # Expand window to enclose entire function/class with 2 lines padding
                start_line = max(1, min(node_start - 2, target_line - window_radius))
                end_line = min(total_lines, max(node_end + 2, target_line + window_radius))
            else:
                start_line = max(1, target_line - window_radius)
                end_line = min(total_lines, target_line + window_radius)
        except Exception:
            start_line = max(1, target_line - window_radius)
            end_line = min(total_lines, target_line + window_radius)
    else:
        # Fallback if no matching frame in traceback:
        # Check for class or function definitions matching symbols from error_feedback and problem_statement
        combined_text = "\n".join(filter(None, [error_feedback, problem_statement]))

        class_names = re.findall(r'\b[A-Z][a-zA-Z0-9_]+\b', combined_text)
        method_names = re.findall(r'`([a-zA-Z_][a-zA-Z0-9_]*)`', combined_text)
        method_names += re.findall(r'\b(?:def\s+|method\s+)([a-zA-Z_][a-zA-Z0-9_]*)', combined_text)
        method_names += re.findall(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(', combined_text)

        excluded_symbols = {
            "Failed", "ValueError", "Exception", "AssertionError", "TypeError",
            "AttributeError", "KeyError", "IndexError", "DID", "NOT", "RAISE",
            "True", "False", "None", "File", "Line",
        }

        matched_class_idx = None
        matched_class_name = None

        # A. Search for matching class definition
        for w in class_names:
            if w in excluded_symbols:
                continue
            variants = [w]
            if w.endswith("s") and len(w) > 3:
                variants.append(w[:-1])
            for v in variants:
                for i, line in enumerate(lines):
                    if re.search(rf'^\s*class\s+{re.escape(v)}\b', line):
                        matched_class_idx = i
                        matched_class_name = v
                        break
                if matched_class_idx is not None:
                    break
            if matched_class_idx is not None:
                break

        # B. If class matched, identify relevant method within class body
        if matched_class_idx is not None:
            class_indent = len(lines[matched_class_idx]) - len(lines[matched_class_idx].lstrip())
            class_end_idx = total_lines
            for i in range(matched_class_idx + 1, total_lines):
                line = lines[i]
                if line.strip() and not line.strip().startswith("#"):
                    line_indent = len(line) - len(line.lstrip())
                    if line_indent <= class_indent and (line.startswith("class ") or line.startswith("def ")):
                        class_end_idx = i
                        break

            clean_methods = [m for m in method_names if m not in excluded_symbols and m != matched_class_name]
            # If constructor or class instantiation is implied, or if no specific method was identified for the class,
            # check __init__ first as the canonical entry point for class initialization and attribute validation.
            if (
                "__init__" in clean_methods
                or not clean_methods
                or any(x in combined_text.lower() for x in [f"{matched_class_name.lower()}(", "__init__", "constructor", "init", "instantiat", "creat"])
            ):
                if "__init__" not in clean_methods:
                    clean_methods.insert(0, "__init__")

            target_method_idx = None
            for m in clean_methods:
                for i in range(matched_class_idx + 1, class_end_idx):
                    if re.search(rf'^\s*def\s+{re.escape(m)}\b', lines[i]):
                        target_method_idx = i
                        break
                if target_method_idx is not None:
                    break

            if target_method_idx is None:
                # If no explicitly named method was found in the class body, check if __init__ exists
                # as the canonical entry point for class initialization and validation.
                for i in range(matched_class_idx + 1, class_end_idx):
                    if re.search(r'^\s*def\s+__init__\b', lines[i]):
                        target_method_idx = i
                        break

            if target_method_idx is not None:
                target_line = target_method_idx + 1
                method_indent = len(lines[target_method_idx]) - len(lines[target_method_idx].lstrip())
                method_end_idx = class_end_idx
                for i in range(target_method_idx + 1, class_end_idx):
                    line = lines[i]
                    if line.strip() and not line.strip().startswith("#"):
                        line_indent = len(line) - len(line.lstrip())
                        if line_indent <= method_indent and re.match(r'^\s*def\s+', line):
                            method_end_idx = i
                            break

                # Tight, verbatim window: 2 lines before method header through 2 lines after method end
                start_line = max(1, target_method_idx - 1)
                end_line = min(total_lines, method_end_idx + 2)
            else:
                target_line = matched_class_idx + 1
                start_line = max(1, matched_class_idx + 1 - 2)
                end_line = min(total_lines, matched_class_idx + 90)

        else:
            # C. Check for standalone function definitions matching symbols
            matched_func_idx = None
            clean_funcs = [m for m in method_names if m not in excluded_symbols]
            for m in clean_funcs:
                for i, line in enumerate(lines):
                    if re.search(rf'^\s*def\s+{re.escape(m)}\b', line):
                        matched_func_idx = i
                        break
                if matched_func_idx is not None:
                    break

            if matched_func_idx is not None:
                target_line = matched_func_idx + 1
                start_line = max(1, target_line - window_radius)
                end_line = min(total_lines, target_line + window_radius)
            elif total_lines <= 100:
                start_line = 1
                end_line = total_lines
            else:
                # For large files (>100 lines), blindly selecting the beginning of the file produces
                # misleading context and causes ambiguous patch rejections.
                # Explicit localization failure.
                return ("", 0, 0, None)

    slice_lines = lines[start_line - 1 : end_line]
    annotated = "\n".join(f"{start_line + i:4d} | {line}" for i, line in enumerate(slice_lines))
    return (annotated, start_line, end_line, target_line)


def validate_target_file(
    target_path: Optional[Path | str],
    test_file: Optional[Path | str] = None,
    test_command: Optional[str] = None,
    workspace_root: Optional[Path | str] = None,
) -> Optional[str]:
    """Validate that target_path is a legitimate source module and not a test file.

    Enforces that Argus repairs source code, not the verifying test suite.
    Rejects:
    1. Identical paths (target_file is the test_file, whether relative or absolute).
    2. Path aliases or symlinks resolving to the same physical file.
    3. Files specified as test targets in test_command (e.g. 'pytest tests/test_foo.py').
    4. Conventional Python test filenames ('test_*.py', '*_test.py', 'conftest.py').
    5. Files located within recognized test directories ('tests', 'test', 'testing', '__tests__').

    Returns an error message string if invalid, or None if valid.
    """
    if not target_path:
        return "Target path cannot be empty."

    resolved_target = Path(target_path).resolve()

    # 1. Direct comparison with test_file
    if test_file:
        test_path = Path(test_file)
        if not test_path.is_absolute() and workspace_root:
            resolved_test = (Path(workspace_root) / test_path).resolve()
        else:
            resolved_test = test_path.resolve()

        if resolved_target == resolved_test:
            return f"Target file cannot be the test file being executed: '{target_path}'"
        try:
            if resolved_target.exists() and resolved_test.exists() and os.path.samefile(resolved_target, resolved_test):
                return f"Target file resolves to the same file as the test file: '{target_path}'"
        except Exception:
            pass

    # 2. Comparison with test file mentioned in test_command
    if test_command:
        for tok in test_command.split():
            clean_tok = tok.strip("\"'").split("::")[0]
            if clean_tok.endswith((".py", ".pyc")):
                tok_path = Path(clean_tok)
                if not tok_path.is_absolute() and workspace_root:
                    tok_path = Path(workspace_root) / tok_path
                try:
                    resolved_tok = tok_path.resolve()
                    if resolved_tok == resolved_target:
                        return f"Target file cannot match the test path specified in test command: '{clean_tok}'"
                    if tok_path.exists() and resolved_target.exists() and os.path.samefile(resolved_tok, resolved_target):
                        return f"Target file resolves to the test path specified in test command: '{clean_tok}'"
                except Exception:
                    pass

    # 3. Conventional Python test filename patterns
    target_name_lower = resolved_target.name.lower()
    if target_name_lower.startswith("test_"):
        return f"Target file appears to be a test file by naming convention ('test_*'): '{resolved_target.name}'"
    if target_name_lower.endswith("_test.py"):
        return f"Target file appears to be a test file by naming convention ('*_test.py'): '{resolved_target.name}'"
    if target_name_lower == "conftest.py":
        return "Target file cannot be a test configuration module ('conftest.py')"

    # 4. Recognized test directories
    test_dir_names = {"tests", "test", "testing", "__tests__"}
    ws_path = Path(workspace_root).resolve() if workspace_root else None
    if ws_path:
        try:
            rel_parts = [p.lower() for p in resolved_target.relative_to(ws_path).parts[:-1]]
        except ValueError:
            rel_parts = [p.lower() for p in resolved_target.parts[:-1]]
    else:
        rel_parts = [p.lower() for p in resolved_target.parts[:-1]]

    for part in rel_parts:
        if part in test_dir_names:
            return f"Target file cannot reside inside a test directory ('{part}'): '{resolved_target}'"

    return None


class RecoveryController:
    """Orchestrates the bounded execution-feedback loop."""

    def __init__(
        self,
        config: Optional[ArgusConfig] = None,
        adapter: Optional[ProviderAdapter] = None,
        container_evaluator: Optional[Any] = None,
    ) -> None:
        self.config = config or ArgusConfig()
        self.patch_engine = ResilientPatchEngine(
            similarity_threshold=self.config.similarity_threshold,
            ambiguity_margin=self.config.ambiguity_margin,
            enforce_ast=True,
            enable_tier4=getattr(self.config, "enable_tier4", False),
        )
        self.test_runner = SubprocessTestRunner(timeout_seconds=self.config.test_timeout_seconds)
        self.model_cost = self.config.get_model_cost()
        self.adapter: Optional[ProviderAdapter] = adapter
        self.selected_provider: Optional[str] = None
        self.budget_tracker: Optional[TaskConditionBudget] = None
        self.container_evaluator = container_evaluator
        if self.container_evaluator is None and getattr(self.config, "use_container_runner", False):
            from argus.runtime.container import ContainerConfig, ContainerEvaluator
            c_cfg = ContainerConfig(
                image=self.config.container_image or "ubuntu:22.04",
                timeout_seconds=self.config.repo_test_timeout_seconds,
                base_commit=self.config.base_commit,
                expected_candidate_hash=self.config.expected_candidate_hash,
            )
            self.container_evaluator = ContainerEvaluator(c_cfg)

    def close(self) -> None:
        """Deterministically close any underlying network adapters and HTTP clients."""
        if self.adapter is not None:
            if hasattr(self.adapter, "close"):
                try:
                    self.adapter.close()
                except Exception:
                    pass

    def __enter__(self) -> RecoveryController:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def _get_adapter(self) -> ProviderAdapter:
        """Lazily initialize or return the provider adapter."""
        if self.adapter is not None:
            return self.adapter

        p_name = (self.config.provider or "").lower().strip()
        if p_name == "groq":
            raise RuntimeError(
                "Groq is strictly prohibited as an inference provider in Argus. "
                "Please configure an authorized provider via 'argus provider set' (e.g. ollama, openai, deepseek, antigravity)."
            )
        provider_timeout = float(getattr(self.config, "provider_timeout_seconds", 45.0))

        p_cfg = getattr(self.config, "provider_config", None)
        if p_cfg is not None and (not p_name or p_cfg.name.lower() == p_name):
            self.selected_provider = p_cfg.name
            p_cfg.timeout_seconds = provider_timeout
            if p_cfg.name.lower() == "antigravity":
                from argus.providers.antigravity import AntigravityHostAdapter
                self.adapter = AntigravityHostAdapter(
                    config=p_cfg,
                    timeout_seconds=provider_timeout,
                )
            else:
                self.adapter = OpenAICompatibleAdapter(p_cfg)
            return self.adapter

        # Check user provider storage
        try:
            from argus.providers.storage import ProviderStorage
            storage = ProviderStorage()
            stored = storage.get_provider(self.config.provider)
            if stored and (stored.api_key or not PROVIDER_PRESETS.get(stored.name, None) or not PROVIDER_PRESETS[stored.name].requires_key):
                self.selected_provider = stored.name
                stored.model = self.config.model or stored.model
                stored.timeout_seconds = provider_timeout
                if stored.name.lower() == "antigravity":
                    from argus.providers.antigravity import AntigravityHostAdapter
                    self.adapter = AntigravityHostAdapter(
                        config=stored,
                        timeout_seconds=provider_timeout,
                    )
                else:
                    self.adapter = OpenAICompatibleAdapter(stored)
                return self.adapter
        except Exception:
            pass

        # Explicit 'auto' is the only path allowed to discover a provider.
        if p_name == "auto":
            try:
                from argus.providers.storage import ProviderStorage
                active = ProviderStorage().get_active_provider()
            except Exception:
                active = None

            if active is not None:
                self.selected_provider = active.name
                active.model = self.config.model or active.model
                active.timeout_seconds = provider_timeout
                if active.name.lower() == "antigravity":
                    from argus.providers.antigravity import AntigravityHostAdapter
                    self.adapter = AntigravityHostAdapter(
                        config=active,
                        timeout_seconds=provider_timeout,
                    )
                else:
                    self.adapter = OpenAICompatibleAdapter(active)
                return self.adapter

            from argus.providers.antigravity import (
                AntigravityHostAdapter,
                find_antigravity_cli,
            )
            if find_antigravity_cli() is not None:
                host_cfg = ProviderConfig(
                    name="antigravity",
                    base_url="host://antigravity-cli",
                    model=self.config.model or "gemini-3.8-flash-low",
                    timeout_seconds=provider_timeout,
                )
                self.selected_provider = "antigravity"
                self.adapter = AntigravityHostAdapter(
                    config=host_cfg,
                    timeout_seconds=provider_timeout,
                )
                return self.adapter

            raise ProviderConfigurationError(
                "Explicit 'auto' provider discovery found no stored active provider "
                "and no Antigravity host runtime. No request was made."
            )

        # Check for preset definitions (e.g. groq, openai, deepseek, gemini)
        preset = PROVIDER_PRESETS.get(p_name)
        if preset is not None:
            api_key = None
            if p_name == "deepseek":
                api_key = self.config.deepseek_api_key or os.getenv("DEEPSEEK_API_KEY")
            elif p_name == "openai":
                api_key = os.getenv("OPENAI_API_KEY")
            elif p_name == "gemini":
                api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
            elif p_name == "groq":
                api_key = os.getenv("GROQ_API_KEY")

            if not api_key:
                try:
                    from argus.providers.storage import ProviderStorage
                    storage = ProviderStorage()
                    api_key = storage.get_api_key(p_name)
                except Exception:
                    pass

            if not api_key and preset.requires_key:
                raise RuntimeError(
                    f"No API key configured for provider '{p_name}'. "
                    f"Please configure your API key via 'argus provider set' or set {preset.key.upper()}_API_KEY."
                )

            cfg = ProviderConfig(
                name=p_name,
                base_url=preset.default_base_url,
                model=self.config.model or preset.default_model,
                api_key=api_key or "",
                timeout_seconds=provider_timeout,
            )
            self.selected_provider = p_name
            self.adapter = OpenAICompatibleAdapter(cfg)
            return self.adapter

        # Check for antigravity provider name
        if p_name == "antigravity":
            from argus.providers.antigravity import AntigravityHostAdapter
            host_cfg = ProviderConfig(
                name="antigravity",
                base_url="host://antigravity-cli",
                model=self.config.model or "gemini-3.8-flash-low",
                timeout_seconds=provider_timeout,
            )
            self.selected_provider = "antigravity"
            self.adapter = AntigravityHostAdapter(
                config=host_cfg,
                timeout_seconds=provider_timeout,
            )
            return self.adapter

        if not p_name:
            raise ProviderConfigurationError(
                "No inference provider selected. Use '--provider <name>' or request "
                "host discovery explicitly with '--provider auto'. No request was made."
            )

        # No implicit provider substitution. Previously this branch routed any
        # unknown provider name to DeepSeek whenever DEEPSEEK_API_KEY happened to
        # be present, which silently changed the requested provider.
        raise ProviderConfigurationError(
            f"Unknown or unconfigured provider '{self.config.provider}'. "
            "Configure it explicitly with 'argus provider set' or select an "
            "authorized provider with --provider. No request was made."
        )

    def _get_client(self) -> Any:
        """Backwards compatibility for legacy tests."""
        raise RuntimeError("Direct Groq client has been removed. Groq is strictly prohibited in Argus.")

    def auto_localize_target_file(
        self,
        repo_dir: Path,
        failure_output: str,
        test_command: str,
    ) -> Optional[Path]:
        """Auto-localize the most probable buggy source file using traceback parsing and hybrid AST indexing."""
        from argus.localization.localizer import DeterministicLocalizer, EXCLUDED_DIRS
        import ast
        import re

        # 1. Parse Traceback Frames directly
        tb_matches = re.findall(r'File "([^"]+\.py)"', failure_output)
        tb_matches += re.findall(r"File '([^']+\.py)'", failure_output)
        # Support python unittest and standard compiler formats: path/file.py:123
        colon_matches = re.findall(r'([a-zA-Z0-9_/\\-]+\.py):\d+', failure_output)
        # Support pytest brief reports: FAILED path/to/test.py::test_fn -> source frames in summary
        summary_matches = re.findall(r'(?:in|from)\s+([a-zA-Z0-9_/\\-]+\.py)', failure_output)
        cmd_tokens = [tok.strip("\"'") for tok in test_command.split() if tok.strip("\"'").endswith(".py")]

        all_candidates = tb_matches + colon_matches + summary_matches + cmd_tokens
        identified_test_files: List[Path] = []

        for cand in reversed(all_candidates):  # Start from innermost frame
            clean_cand = cand.replace("\\", "/").strip()
            p = Path(clean_cand)
            if not p.is_absolute():
                p = (repo_dir / clean_cand).resolve()

            if p.is_file():
                try:
                    rel = p.relative_to(repo_dir)
                    parts_lower = [part.lower() for part in rel.parts]
                    is_excluded = bool(set(parts_lower).intersection(EXCLUDED_DIRS))
                    is_test = any(
                        part.startswith("test") or part.endswith("_test.py") or part.startswith("test_")
                        for part in parts_lower
                    )
                    if is_test:
                        if p not in identified_test_files:
                            identified_test_files.append(p)
                    elif not is_excluded:
                        return p
                except ValueError:
                    continue

        # 2. Inspect failing test files for imported project modules & stem naming
        scored_candidates: List[tuple[float, Path]] = []
        for tf in identified_test_files:
            stem = tf.stem
            if stem.startswith("test_"):
                core_stem = stem[5:]
            elif stem.endswith("_test"):
                core_stem = stem[:-5]
            else:
                core_stem = stem

            tree = None
            try:
                tree = ast.parse(tf.read_text("utf-8", errors="replace"))
            except Exception:
                pass

            imported_modules: List[str] = []
            if tree:
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            imported_modules.append(alias.name)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imported_modules.append(node.module)

            potential_sources: set[Path] = set()
            for mod in imported_modules:
                rel_mod = mod.replace(".", "/")
                for prefix in ("", "src/"):
                    c1 = (repo_dir / prefix / f"{rel_mod}.py").resolve()
                    c2 = (repo_dir / prefix / rel_mod / "__init__.py").resolve()
                    if c1.is_file():
                        potential_sources.add(c1)
                    if c2.is_file():
                        potential_sources.add(c2)
                if mod.startswith("src."):
                    stripped = mod[4:].replace(".", "/")
                    c3 = (repo_dir / "src" / f"{stripped}.py").resolve()
                    if c3.is_file():
                        potential_sources.add(c3)

            # Check stem matching (e.g. test_calculator -> calculator.py or src/calculator.py)
            for prefix in ("", "src/"):
                stem_cand = (repo_dir / prefix / f"{core_stem}.py").resolve()
                if stem_cand.is_file():
                    potential_sources.add(stem_cand)

            for src_path in potential_sources:
                try:
                    rel = src_path.relative_to(repo_dir)
                    parts_lower = [part.lower() for part in rel.parts]
                    if set(parts_lower).intersection(EXCLUDED_DIRS) or any(part.startswith("test") for part in parts_lower):
                        continue
                    score = 20.0
                    if core_stem and core_stem in src_path.stem:
                        score += 30.0
                    content = src_path.read_text("utf-8", errors="replace")
                    for token in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", failure_output):
                        if len(token) > 2 and token in content:
                            score += 2.0
                    scored_candidates.append((score, src_path))
                except Exception:
                    continue

        if scored_candidates:
            scored_candidates.sort(key=lambda x: x[0], reverse=True)
            return scored_candidates[0][1]

        # 3. If no source file in traceback or test imports, use Hybrid Localizer with AST Symbol Table & BM25
        try:
            from argus.localization.hybrid_localizer import HybridLocalizer
            hybrid = HybridLocalizer(repo_dir)
            scored = hybrid.rank_candidates(f"{test_command}\n{failure_output}", top_k=3)
            if scored:
                return scored[0].file_path
        except Exception:
            pass

        # 4. Fallback to DeterministicLocalizer symbol extraction
        try:
            det = DeterministicLocalizer(repo_dir)
            files = det.extract_explicit_file_paths(failure_output)
            if files:
                return files[0]
        except Exception:
            pass

        return None

    def run(
        self,
        target_file: Optional[str | Path] = None,
        test_command: Optional[str] = None,
        test_file: Optional[str | Path] = None,
        workspace_root: Optional[str | Path] = None,
        on_attempt_start: Optional[callable] = None,
        on_attempt_end: Optional[callable] = None,
        event_emitter: Optional[JsonLinesEmitter] = None,
        cancel_event: Optional[Any] = None,
        **kwargs: Any,
    ) -> RecoveryOutcome:
        """Run the bounded recovery loop in an isolated workspace."""
        start_time = time.perf_counter()
        history: List[AttemptRecord] = []
        total_cost = 0.0

        # Handle flexible argument signatures: run(test_command="...") or run("pytest ...")
        if test_command is None and target_file and any(tok in str(target_file) for tok in ["pytest", "python -m", "unittest"]):
            test_command = str(target_file)
            target_file = None

        if not test_command:
            test_command = kwargs.get("cmd") or "pytest"

        ws_root = Path(workspace_root).resolve() if workspace_root else None
        if not ws_root:
            if target_file and Path(target_file).is_absolute():
                ws_root = Path(target_file).parent
            else:
                ws_root = Path.cwd().resolve()

        # Step 0: Auto-localization if target_file was omitted
        if target_file is None:
            prelim_res = self.test_runner.run_test(test_command, cwd=ws_root)
            if prelim_res.passed:
                return RecoveryOutcome(
                    status="ALREADY_PASSING",
                    total_attempts=0,
                    total_cost_usd=0.0,
                    total_duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                    message="Target test suite already passes without any modifications.",
                    run_id=f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}",
                    tests_executed=[test_command],
                )

            # Check if test framework failed before executing tests (e.g. file not found / syntax error in CLI)
            is_runner_setup_error = (
                prelim_res.exit_code in (4, 5)
                or "file or directory not found" in (prelim_res.stderr or "").lower()
                or "no tests ran" in (prelim_res.stdout or "").lower()
            )
            if is_runner_setup_error:
                err_detail = (prelim_res.stderr or prelim_res.stdout).strip()
                return RecoveryOutcome(
                    status="TEST_SETUP_ERROR",
                    total_attempts=0,
                    total_cost_usd=0.0,
                    total_duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                    message=f"Test command failed to execute or find tests: {err_detail}",
                    run_id=f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}",
                    tests_executed=[test_command],
                )

            localized = self.auto_localize_target_file(
                repo_dir=ws_root,
                failure_output=prelim_res.traceback_summary or (prelim_res.stdout + "\n" + prelim_res.stderr),
                test_command=test_command,
            )
            if localized is None:
                return RecoveryOutcome(
                    status="LOCALIZATION_FAILED",
                    total_attempts=0,
                    total_cost_usd=0.0,
                    total_duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                    message="Could not auto-localize the target source file from test failure. Please specify target_file explicitly.",
                    run_id=f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}",
                    tests_executed=[test_command],
                )
            target_path = localized
            target_file = str(target_path.relative_to(ws_root))
        else:
            target_path = Path(target_file)
            if not target_path.is_absolute():
                target_path = (ws_root / target_path).resolve()

        run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        baseline_hash = ""
        if target_path.is_file():
            baseline_hash = hashlib.sha256(target_path.read_bytes()).hexdigest()

        if event_emitter:
            event_emitter.session_init(
                run_id=run_id,
                target_file=str(target_file),
                test_command=test_command,
                baseline_hash=baseline_hash,
                model=self.config.model,
                max_attempts=self.config.max_attempts,
                budget_limit_usd=self.config.budget_limit_usd,
            )

        task_id = kwargs.get("task_id") or run_id
        condition_name = kwargs.get("condition") or "default"
        global_budget = kwargs.get("global_budget")
        if global_budget is None:
            global_budget = GlobalExperimentBudget.get_instance(self.config.global_experiment_budget_usd)

        self.budget_tracker = TaskConditionBudget(
            task_id=task_id,
            condition=condition_name,
            budget_limit_usd=self.config.budget_limit_usd,
            global_budget=global_budget,
        )

        difficulty_decision = None

        def _finalize(outcome: RecoveryOutcome) -> RecoveryOutcome:
            outcome.run_id = run_id
            outcome.selected_provider = outcome.selected_provider or self.selected_provider
            outcome.baseline_hash = baseline_hash
            outcome.target_file = str(target_file)
            outcome.changed_files = [str(target_file)] if outcome.unified_diff else []
            outcome.tests_executed = [test_command]
            if self.budget_tracker is not None:
                outcome.total_cost_usd = round(self.budget_tracker.total_committed, 6)
                outcome.spend_uncertain = self.budget_tracker.spend_uncertain
                outcome.quarantined_usd = round(self.budget_tracker.quarantined_spend, 6)
            if outcome.history:
                outcome.total_reserved_usd = round(sum(h.reserved_cost_usd for h in outcome.history), 7)
            if difficulty_decision is not None:
                outcome.effort_used = difficulty_decision.effort
                outcome.routing_reasons = list(difficulty_decision.reasons)
            if outcome.history:
                last_h = outcome.history[-1]
                outcome.diagnostics = last_h.error_summary
                outcome.prompt_text = getattr(last_h, "prompt_text", "")
                outcome.raw_response = getattr(last_h, "raw_response", "")
                outcome.unapplied_patch = getattr(last_h, "parsed_patch", "")
                outcome.context_window = getattr(last_h, "context_window", {})
            if outcome.status == "SUCCESS" and outcome.patched_content is not None:
                outcome.verified = True
                outcome.candidate_hash = hashlib.sha256(outcome.patched_content.encode("utf-8")).hexdigest()
            else:
                outcome.verified = False

            # Persist isolated per-run artifact
            try:
                CandidateArtifactStore.save_run_artifact(
                    run_id=run_id,
                    target_file=str(target_file),
                    test_command=test_command,
                    baseline_hash=baseline_hash,
                    status=outcome.status,
                    verified=outcome.verified,
                    tier_used=outcome.tier_used,
                    attempts=outcome.total_attempts,
                    cost_usd=outcome.total_cost_usd,
                    duration_ms=outcome.total_duration_ms,
                    patched_content=outcome.patched_content,
                    unified_diff=outcome.unified_diff,
                    workspace_root=ws_root,
                    prompt_text=outcome.prompt_text,
                    raw_response=outcome.raw_response,
                    parsed_patch=outcome.unapplied_patch,
                    context_window=outcome.context_window,
                )
            except Exception:
                pass

            if event_emitter:
                event_emitter.outcome(
                    status=outcome.status,
                    run_id=run_id,
                    verified=outcome.verified,
                    total_attempts=outcome.total_attempts,
                    total_cost_usd=outcome.total_cost_usd,
                    total_duration_ms=outcome.total_duration_ms,
                    target_file=str(target_file),
                    unified_diff=outcome.unified_diff,
                    message=outcome.message,
                )

            return outcome

        if test_file is None:
            # Auto-detect test file from test_command tokens if present on disk
            for token in test_command.split():
                clean_tok = token.strip("\"'")
                cand = Path(clean_tok)
                if not cand.is_absolute() and ws_root:
                    cand = ws_root / cand
                if cand.is_file() and cand.suffix in (".py", ".js", ".ts", ".go", ".rs"):
                    test_file = cand
                    break

        # Security validation: strictly reject attempts to patch test files
        target_validation_err = validate_target_file(
            target_path=target_path,
            test_file=test_file,
            test_command=test_command,
            workspace_root=ws_root,
        )
        if target_validation_err:
            return _finalize(
                RecoveryOutcome(
                    status="INVALID_TARGET",
                    total_attempts=0,
                    total_cost_usd=0.0,
                    total_duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                    message=f"SECURITY REFUSAL: {target_validation_err}",
                )
            )

        # Validate configuration
        issues = self.config.validate()
        if self.adapter is not None:
            # If an adapter was explicitly injected, ignore missing external provider settings
            issues = [i for i in issues if "provider" not in i.lower()]
        if issues:
            return _finalize(
                RecoveryOutcome(
                    status="CONFIG_ERROR",
                    total_attempts=0,
                    total_cost_usd=0.0,
                    total_duration_ms=0.0,
                    message="; ".join(issues),
                )
            )

        # Step 1: Initialize Workspace
        use_worktree = kwargs.get("use_worktree", getattr(self.config, "use_worktree", False))
        with WorkspaceManager(repo_root=ws_root, target_file=target_path, test_file=test_file, use_worktree=use_worktree) as ws:
            exec_test_cmd = test_command
            if test_file and ws.test_file_rel:
                orig_str = str(test_file)
                rel_str = str(ws.test_file_rel).replace("\\", "/")
                if orig_str in exec_test_cmd:
                    exec_test_cmd = exec_test_cmd.replace(orig_str, rel_str)

            # Step 2: Baseline evaluation
            active_container = kwargs.get("container_evaluator", self.container_evaluator)
            if active_container is not None:
                baseline_cres = active_container.evaluate(
                    candidate_content=ws.read_target_file(),
                    target_rel_path=str(ws.target_file_rel),
                    test_command=exec_test_cmd,
                    expected_base_commit=kwargs.get("base_commit", getattr(self.config, "base_commit", None)),
                )
                is_infra_error = (
                    baseline_cres.exit_code in (125, 126, 127, 128, 129, 130)
                    or "OCI runtime" in (baseline_cres.traceback_summary or "")
                    or (baseline_cres.error is not None and any(k in baseline_cres.error for k in ("MISMATCH", "CONTAINER", "DOCKER", "Docker")))
                )
                if is_infra_error:
                    return _finalize(
                        RecoveryOutcome(
                            status="CONTAINER_SETUP_ERROR",
                            total_attempts=0,
                            total_cost_usd=0.0,
                            total_duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                            message=baseline_cres.error or baseline_cres.traceback_summary,
                        )
                    )
                if baseline_cres.passed:
                    return _finalize(
                        RecoveryOutcome(
                            status="ALREADY_PASSING",
                            total_attempts=0,
                            total_cost_usd=0.0,
                            total_duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                            message="Target test suite already passes in container without any modifications.",
                        )
                    )
                baseline_failure = baseline_cres.traceback_summary or baseline_cres.stderr
            else:
                if getattr(self.config, "require_container_isolation", False) or getattr(self.config, "use_container_runner", False):
                    return _finalize(
                        RecoveryOutcome(
                            status="CONTAINER_SETUP_ERROR",
                            total_attempts=0,
                            total_cost_usd=0.0,
                            total_duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                            message="SECURITY REFUSAL: Container runner or isolation is required by configuration, but no active container evaluator is available. Host execution of untrusted code is prohibited.",
                        )
                    )
                baseline_res = self.test_runner.run_test(exec_test_cmd, cwd=ws.temp_dir)
                if baseline_res.passed:
                    return _finalize(
                        RecoveryOutcome(
                            status="ALREADY_PASSING",
                            total_attempts=0,
                            total_cost_usd=0.0,
                            total_duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                            message="Target test suite already passes without any modifications.",
                        )
                    )
                if (
                    baseline_res.exit_code in (4, 5)
                    or "file or directory not found" in (baseline_res.stderr or "").lower()
                    or "no tests ran" in (baseline_res.stdout or "").lower()
                ):
                    err_detail = (baseline_res.stderr or baseline_res.stdout).strip()
                    return _finalize(
                        RecoveryOutcome(
                            status="TEST_SETUP_ERROR",
                            total_attempts=0,
                            total_cost_usd=0.0,
                            total_duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                            message=f"Test command failed to execute or find tests: {err_detail}",
                        )
                    )
                baseline_failure = baseline_res.traceback_summary

            last_error = baseline_failure

            # Classify difficulty for adaptive inference routing
            from argus.routing.difficulty import DifficultyClassifier
            classifier = DifficultyClassifier()
            difficulty_decision = classifier.classify(
                prompt=f"Fix failing test: {exec_test_cmd}",
                code=ws.read_target_file(),
                error_feedback=last_error,
                files=[str(target_path)],
                explicit_override=getattr(self.config, "effort", None),
                disable_adaptive=not getattr(self.config, "adaptive_routing", True),
            )

            # Step 3: Sequential Recovery Loop with Atomic Budget Reservation & 429 Separation
            cumulative_backoff_sec = 0.0
            rate_limit_retries = 0
            attempt_idx = 1

            while attempt_idx <= self.config.max_attempts:
                # Cooperative cancellation: stop between attempts, before any further
                # provider dispatch or spend. Nothing is quarantined here because no
                # new reservation has been made for this attempt yet.
                if cancel_event is not None and cancel_event.is_set():
                    return _finalize(
                        RecoveryOutcome(
                            status="CANCELLED",
                            total_attempts=len(history),
                            total_cost_usd=round(self.budget_tracker.total_committed, 6),
                            total_duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                            history=history,
                            message="Recovery cancelled by user before the next attempt.",
                        )
                    )

                attempt_start = time.perf_counter()

                # Check task deadline before starting attempt
                elapsed_task_sec = time.perf_counter() - start_time
                if elapsed_task_sec > self.config.task_deadline_seconds:
                    return _finalize(
                        RecoveryOutcome(
                            status="DEADLINE_EXCEEDED",
                            total_attempts=len(history),
                            total_cost_usd=round(self.budget_tracker.total_committed, 6),
                            total_duration_ms=round(elapsed_task_sec * 1000, 2),
                            history=history,
                            message=f"Task deadline of {self.config.task_deadline_seconds}s exceeded.",
                        )
                    )

                # 3-Tier Escalation Ladder
                if not getattr(self.config, "escalation_enabled", True):
                    tier_model = self.config.model
                    tier_effort = difficulty_decision.effort
                    context_radius = 25
                    tier_idx = 1
                else:
                    if attempt_idx == 1:
                        tier_model = self.config.small_model or self.config.model
                        tier_effort = getattr(self.config, "reasoning_effort", None) or ("low" if difficulty_decision.effort == "low" else "medium")
                        context_radius = 25
                        tier_idx = 1
                    elif attempt_idx == 2:
                        tier_model = self.config.small_model or self.config.model
                        tier_effort = getattr(self.config, "reasoning_effort", None) or "high"
                        context_radius = 25
                        tier_idx = 2
                    else:  # attempt 3+
                        tier_model = self.config.large_model or self.config.model
                        tier_effort = getattr(self.config, "reasoning_effort", None) or "high"
                        context_radius = 45  # Deep reasoning + max context
                        tier_idx = 3

                if on_attempt_start:
                    on_attempt_start(attempt_idx, self.config.max_attempts)
                if event_emitter:
                    event_emitter.attempt_start(
                        attempt=attempt_idx,
                        max_attempts=self.config.max_attempts,
                        model=tier_model,
                    )

                # Read current file text
                current_code = ws.read_target_file()

                # Build prompt and query model
                try:
                    feed_err = last_error if getattr(self.config, "include_feedback", True) else ""
                    feed_orig = baseline_failure if getattr(self.config, "include_feedback", True) else ""
                    prompt = self._build_prompt(
                        file_path=str(ws.target_file_rel),
                        code=current_code,
                        test_command=exec_test_cmd,
                        error_feedback=feed_err,
                        attempt=attempt_idx,
                        original_failure=feed_orig,
                        context_radius=context_radius,
                        problem_statement=kwargs.get("problem_statement"),
                    )
                except LocalizationError as loc_err:
                    return _finalize(
                        RecoveryOutcome(
                            status="LOCALIZATION_FAILED",
                            total_attempts=len(history),
                            total_cost_usd=round(self.budget_tracker.total_committed, 6),
                            total_duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                            history=history,
                            message=str(loc_err),
                        )
                    )

                curr_prompt = prompt
                curr_context_window = dict(getattr(self, "_last_context_window", {}))
                curr_raw_response = ""
                curr_parsed_patch = ""

                # Conservative prompt token estimation
                p_tokens_est = count_prompt_tokens(prompt, tier_model)
                eff_max_completion = getattr(self.config, "max_completion_tokens", None) or getattr(self.config, "max_tokens", 2500) or 2500
                tier_cost_calc = self.config.get_model_cost(tier_model)

                # Pre-dispatch check and reserve
                ok, reserved_cost, reason = self.budget_tracker.check_and_reserve(
                    prompt_tokens=p_tokens_est,
                    max_completion_tokens=eff_max_completion,
                    model_cost=tier_cost_calc,
                )
                if not ok:
                    # Escalation budget mitigation:
                    # If on tier 3 (large model escalation) and task budget exceeded, check if small model fits!
                    if tier_idx == 3 and "TASK_BUDGET_EXCEEDED" in reason:
                        small_model_name = self.config.small_model or "qwen2.5-coder:7b"
                        small_cost_calc = self.config.get_model_cost(small_model_name)
                        small_ok, small_reserved, _ = self.budget_tracker.check_and_reserve(
                            prompt_tokens=p_tokens_est,
                            max_completion_tokens=eff_max_completion,
                            model_cost=small_cost_calc,
                        )
                        if small_ok:
                            tier_model = small_model_name
                            tier_cost_calc = small_cost_calc
                            reserved_cost = small_reserved
                            ok = True

                    if not ok:
                        # Refuse before network dispatch
                        return _finalize(
                            RecoveryOutcome(
                                status="BUDGET_EXCEEDED",
                                total_attempts=len(history),
                                total_cost_usd=round(self.budget_tracker.total_committed, 6),
                                total_duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                                history=history,
                                message=f"Pre-dispatch budget refusal: {reason}",
                                tier_used=tier_idx,
                            )
                        )

                # Model dispatch loop with 429 rate limit backoff
                dispatch_success = False
                dispatched = False
                response_text = ""
                p_tokens = 0
                c_tokens = 0
                cached_tokens = 0
                tx_payload = {}

                while True:
                    # Check task deadline during retries
                    elapsed_task_sec = time.perf_counter() - start_time
                    if elapsed_task_sec > self.config.task_deadline_seconds:
                        self.budget_tracker.handle_uncertain_abort(reserved_cost, dispatched=dispatched)
                        return _finalize(
                            RecoveryOutcome(
                                status="DEADLINE_EXCEEDED",
                                total_attempts=len(history),
                                total_cost_usd=round(self.budget_tracker.total_committed, 6),
                                total_duration_ms=round(elapsed_task_sec * 1000, 2),
                                history=history,
                                message=f"Task deadline of {self.config.task_deadline_seconds}s exceeded during dispatch.",
                                tier_used=tier_idx,
                                spend_uncertain=self.budget_tracker.spend_uncertain,
                                quarantined_usd=round(self.budget_tracker.quarantined_spend, 6),
                            )
                        )

                    try:
                        dispatched = True
                        call_res = self._call_model(
                            prompt,
                            effort=tier_effort,
                            model=tier_model,
                            cwd=str(ws.workspace_root),
                        )
                        if len(call_res) >= 5:
                            response_text, p_tokens, c_tokens, cached_tokens, tx_payload = call_res[:5]
                        elif len(call_res) >= 4:
                            response_text, p_tokens, c_tokens, cached_tokens = call_res[:4]
                            tx_payload = {}
                        else:
                            response_text, p_tokens, c_tokens = call_res[:3]
                            cached_tokens = 0
                            tx_payload = {}

                        # Dispatch succeeded: settle reservation
                        call_cost = self.budget_tracker.settle(
                            reserved_amount=reserved_cost,
                            prompt_tokens=p_tokens,
                            completion_tokens=c_tokens,
                            model_cost=tier_cost_calc,
                            cached_prompt_tokens=cached_tokens,
                        )
                        dispatch_success = True
                        break

                    except ProviderError as p_err:
                        if p_err.is_retryable or p_err.status_code == 429:
                            # 429 Rate Limit: conclusive refusal before inference. Release reservation!
                            self.budget_tracker.release(reserved_cost)
                            dispatched = False

                            if not getattr(self.config, "allow_rate_limit_retries", True):
                                return _finalize(
                                    RecoveryOutcome(
                                        status="PROVIDER_RATE_LIMITED",
                                        total_attempts=len(history),
                                        total_cost_usd=round(self.budget_tracker.total_committed, 6),
                                        total_duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                                        history=history,
                                        message=f"Provider rate limited (429) and retries are disabled: {p_err}",
                                        tier_used=tier_idx,
                                    )
                                )

                            backoff_sleep = p_err.retry_after if p_err.retry_after and p_err.retry_after > 0 else (2.0 * (2 ** rate_limit_retries))
                            backoff_sleep = min(backoff_sleep, 60.0)

                            rate_limit_retries += 1
                            cumulative_backoff_sec += backoff_sleep

                            if cumulative_backoff_sec > self.config.rate_limit_backoff_max_seconds:
                                return _finalize(
                                    RecoveryOutcome(
                                        status="PROVIDER_RATE_LIMITED",
                                        total_attempts=len(history),
                                        total_cost_usd=round(self.budget_tracker.total_committed, 6),
                                        total_duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                                        history=history,
                                        message=f"Rate limit backoff ceiling ({self.config.rate_limit_backoff_max_seconds}s) exceeded.",
                                        tier_used=tier_idx,
                                    )
                                )

                            # Sleep for backoff
                            time.sleep(min(backoff_sleep, 0.05 if kwargs.get("mock_test") else backoff_sleep))

                            # Re-reserve budget for retry
                            re_ok, re_cost, re_reason = self.budget_tracker.check_and_reserve(
                                prompt_tokens=p_tokens_est,
                                max_completion_tokens=eff_max_completion,
                                model_cost=tier_cost_calc,
                            )
                            if not re_ok:
                                return _finalize(
                                    RecoveryOutcome(
                                        status="BUDGET_EXCEEDED",
                                        total_attempts=len(history),
                                        total_cost_usd=round(self.budget_tracker.total_committed, 6),
                                        total_duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                                        history=history,
                                        message=f"Pre-dispatch budget refusal upon 429 retry: {re_reason}",
                                        tier_used=tier_idx,
                                    )
                                )
                            reserved_cost = re_cost
                            continue  # Retry dispatch without incrementing attempt_idx!

                        elif is_conclusive_provider_refusal(p_err):
                            # Pre-dispatch rejection or auth error: release reservation!
                            self.budget_tracker.release(reserved_cost)
                            attempt_dur = round((time.perf_counter() - attempt_start) * 1000, 2)
                            record = AttemptRecord(
                                attempt=attempt_idx,
                                duration_ms=attempt_dur,
                                prompt_tokens=0,
                                completion_tokens=0,
                                cost_usd=0.0,
                                tier_used=tier_idx,
                                patch_success=False,
                                test_passed=False,
                                error_summary=f"Provider error ({p_err.status_code}): {str(p_err)[:120]}",
                                model_used=tier_model,
                                prompt_text=curr_prompt,
                                raw_response="",
                                parsed_patch="",
                                context_window=curr_context_window,
                            )
                            history.append(record)
                            if on_attempt_end:
                                on_attempt_end(record)
                            if event_emitter:
                                event_emitter.inference_done(
                                    attempt=attempt_idx,
                                    duration_ms=attempt_dur,
                                    prompt_tokens=0,
                                    completion_tokens=0,
                                    cost_usd=0.0,
                                    model=tier_model,
                                )
                            last_error = f"Inference failure: {p_err}"
                            attempt_idx += 1
                            break

                        else:
                            # Post-dispatch client/gateway timeout, 504, 500, 502, 503, connection dropped:
                            # Provider received request or compute is UNCERTAIN! Quarantine!
                            self.budget_tracker.quarantine(reserved_cost)
                            attempt_dur = round((time.perf_counter() - attempt_start) * 1000, 2)
                            record = AttemptRecord(
                                attempt=attempt_idx,
                                duration_ms=attempt_dur,
                                prompt_tokens=0,
                                completion_tokens=0,
                                cost_usd=reserved_cost,
                                tier_used=tier_idx,
                                patch_success=False,
                                test_passed=False,
                                error_summary=f"Inference timeout / uncertain spend (spend quarantined: ${reserved_cost:.6f}): {p_err}",
                                model_used=tier_model,
                                prompt_text=curr_prompt,
                                raw_response="",
                                parsed_patch="",
                                context_window=curr_context_window,
                            )
                            history.append(record)
                            if on_attempt_end:
                                on_attempt_end(record)
                            if event_emitter:
                                event_emitter.inference_done(
                                    attempt=attempt_idx,
                                    duration_ms=attempt_dur,
                                    prompt_tokens=0,
                                    completion_tokens=0,
                                    cost_usd=reserved_cost,
                                    model=tier_model,
                                )
                            last_error = f"Inference timeout: {p_err}"
                            attempt_idx += 1
                            break

                    except Exception as ex:
                        if not dispatched and is_conclusive_provider_refusal(ex):
                            self.budget_tracker.release(reserved_cost)
                            quarantined_usd = 0.0
                        else:
                            # Dispatched, timeout, cancellation, or connection abort: QUARANTINE!
                            self.budget_tracker.quarantine(reserved_cost)
                            quarantined_usd = reserved_cost

                        attempt_dur = round((time.perf_counter() - attempt_start) * 1000, 2)
                        record = AttemptRecord(
                            attempt=attempt_idx,
                            duration_ms=attempt_dur,
                            prompt_tokens=0,
                            completion_tokens=0,
                            cost_usd=quarantined_usd,
                            tier_used=tier_idx,
                            patch_success=False,
                            test_passed=False,
                            error_summary=f"Inference error: {str(ex)[:120]}",
                            model_used=tier_model,
                            prompt_tokens_est=p_tokens_est,
                            reserved_cost_usd=reserved_cost,
                            transmitted_payload=tx_payload,
                        )
                        history.append(record)
                        if on_attempt_end:
                            on_attempt_end(record)
                        if event_emitter:
                            event_emitter.inference_done(
                                attempt=attempt_idx,
                                duration_ms=attempt_dur,
                                prompt_tokens=0,
                                completion_tokens=0,
                                cost_usd=quarantined_usd,
                                model=tier_model,
                            )
                        last_error = f"Inference failure: {ex}"
                        attempt_idx += 1
                        break

                if not dispatch_success:
                    continue

                if event_emitter:
                    event_emitter.inference_done(
                        attempt=attempt_idx,
                        duration_ms=round((time.perf_counter() - attempt_start) * 1000, 2),
                        prompt_tokens=p_tokens,
                        completion_tokens=c_tokens,
                        cost_usd=call_cost,
                        model=tier_model,
                    )

                # Parse patch blocks
                curr_raw_response = response_text
                patch_blocks = PatchParser.parse(response_text)
                if patch_blocks:
                    curr_parsed_patch = "\n".join(
                        getattr(b, "raw_block", f"<<<<<<< SEARCH\n{b.search_text}\n=======\n{b.replace_text}\n>>>>>>> REPLACE")
                        for b in patch_blocks
                    )
                if not patch_blocks:
                    attempt_dur = round((time.perf_counter() - attempt_start) * 1000, 2)
                    record = AttemptRecord(
                        attempt=attempt_idx,
                        duration_ms=attempt_dur,
                        prompt_tokens=p_tokens,
                        completion_tokens=c_tokens,
                        cost_usd=call_cost,
                        tier_used=tier_idx,
                        patch_success=False,
                        test_passed=False,
                        error_summary="Model did not produce recognizable SEARCH/REPLACE blocks.",
                        model_used=tier_model,
                        prompt_tokens_est=p_tokens_est,
                        reserved_cost_usd=reserved_cost,
                        transmitted_payload=tx_payload,
                        prompt_text=curr_prompt,
                        raw_response=curr_raw_response,
                        parsed_patch=curr_parsed_patch,
                        context_window=curr_context_window,
                        cached_tokens=cached_tokens,
                    )
                    history.append(record)
                    if on_attempt_end:
                        on_attempt_end(record)
                    if event_emitter:
                        event_emitter.patch_reconciled(
                            attempt=attempt_idx,
                            patch_success=False,
                            tier_used=None,
                            ast_valid=False,
                            error_summary=record.error_summary,
                        )
                    last_error = "Formatting error: Please output exact SEARCH/REPLACE blocks."
                    attempt_idx += 1
                    continue

                # Refuse cross-file, rename, creation/deletion and traversal
                # patches before mutating any workspace text. This is checked for
                # every block up front, so a later block cannot be applied after
                # an earlier one was already accepted.
                try:
                    validate_patch_blocks(patch_blocks, str(ws.target_file_rel))
                except PatchTargetError as exc:
                    attempt_dur = round((time.perf_counter() - attempt_start) * 1000, 2)
                    refusal_msg = f"{exc.code}: {exc.message}"
                    record = AttemptRecord(
                        attempt=attempt_idx,
                        duration_ms=attempt_dur,
                        prompt_tokens=p_tokens,
                        completion_tokens=c_tokens,
                        cost_usd=call_cost,
                        tier_used=None,
                        patch_success=False,
                        test_passed=False,
                        error_summary=refusal_msg,
                        model_used=tier_model,
                        prompt_tokens_est=p_tokens_est,
                        reserved_cost_usd=reserved_cost,
                        transmitted_payload=tx_payload,
                        prompt_text=curr_prompt,
                        raw_response=curr_raw_response,
                        parsed_patch=curr_parsed_patch,
                        context_window=curr_context_window,
                        cached_tokens=cached_tokens,
                        matched_file=str(ws.target_file_rel),
                        patch_strategy="none",
                        refusal_reason=exc.code,
                    )
                    history.append(record)
                    if on_attempt_end:
                        on_attempt_end(record)
                    if event_emitter:
                        event_emitter.patch_reconciled(
                            attempt=attempt_idx,
                            patch_success=False,
                            tier_used=None,
                            ast_valid=False,
                            error_summary=refusal_msg,
                        )
                    last_error = f"Unsafe patch target: {refusal_msg}"
                    attempt_idx += 1
                    continue

                # Apply patches sequentially to current workspace code
                working_code = current_code
                patch_success = True
                patch_res: Optional[PatchResult] = None

                for block in patch_blocks:
                    patch_res = self.patch_engine.apply_patch(
                        working_code,
                        block,
                        filename=str(ws.target_file_rel),
                    )
                    if patch_res.success:
                        patch_res.matched_file = str(ws.target_file_rel)
                    if not patch_res.success:
                        patch_success = False
                        break
                    working_code = patch_res.patched_code or working_code

                if not patch_success:
                    attempt_dur = round((time.perf_counter() - attempt_start) * 1000, 2)
                    refusal_msg = patch_res.details if patch_res else "Failed to reconcile patch."
                    record = AttemptRecord(
                        attempt=attempt_idx,
                        duration_ms=attempt_dur,
                        prompt_tokens=p_tokens,
                        completion_tokens=c_tokens,
                        cost_usd=call_cost,
                        tier_used=patch_res.tier_used if patch_res else None,
                        patch_success=False,
                        test_passed=False,
                        error_summary=refusal_msg,
                        model_used=tier_model,
                        prompt_tokens_est=p_tokens_est,
                        reserved_cost_usd=reserved_cost,
                        transmitted_payload=tx_payload,
                        prompt_text=curr_prompt,
                        raw_response=curr_raw_response,
                        parsed_patch=curr_parsed_patch,
                        context_window=curr_context_window,
                        cached_tokens=cached_tokens,
                        matched_file=str(ws.target_file_rel) if patch_res else None,
                        patch_strategy=patch_res.strategy if patch_res else "",
                        patch_confidence=patch_res.confidence if patch_res else None,
                        refusal_reason=(
                            patch_res.refusal_reason.value
                            if patch_res and patch_res.refusal_reason != RefusalReason.NONE
                            else None
                        ),
                    )
                    history.append(record)
                    if on_attempt_end:
                        on_attempt_end(record)
                    if event_emitter:
                        event_emitter.patch_reconciled(
                            attempt=attempt_idx,
                            patch_success=False,
                            tier_used=patch_res.tier_used if patch_res else None,
                            ast_valid=False,
                            error_summary=refusal_msg,
                        )
                    last_error = f"Patch application failed: {refusal_msg}"
                    attempt_idx += 1
                    continue

                # Write candidate to workspace
                ws.write_target_file(working_code)

                # Optional AST safety check
                ast_valid = True
                if ws.target_file_rel.suffix == ".py":
                    from argus.patch.ast_checker import ASTChecker
                    ast_check = ASTChecker.validate_python_code(working_code, filename=str(ws.target_file_rel))
                    if not ast_check.is_valid:
                        ast_valid = False
                        attempt_dur = round((time.perf_counter() - attempt_start) * 1000, 2)
                        record = AttemptRecord(
                            attempt=attempt_idx,
                            duration_ms=attempt_dur,
                            prompt_tokens=p_tokens,
                            completion_tokens=c_tokens,
                            cost_usd=call_cost,
                            tier_used=patch_res.tier_used if patch_res else None,
                            patch_success=False,
                            test_passed=False,
                            error_summary=f"AST Syntax Error: {ast_check.formatted_error()}",
                            model_used=tier_model,
                            prompt_tokens_est=p_tokens_est,
                            reserved_cost_usd=reserved_cost,
                            transmitted_payload=tx_payload,
                            prompt_text=curr_prompt,
                            raw_response=curr_raw_response,
                            parsed_patch=curr_parsed_patch,
                            context_window=curr_context_window,
                            cached_tokens=cached_tokens,
                        )
                        history.append(record)
                        if on_attempt_end:
                            on_attempt_end(record)
                        if event_emitter:
                            event_emitter.patch_reconciled(
                                attempt=attempt_idx,
                                patch_success=True,
                                tier_used=patch_res.tier_used if patch_res else None,
                                ast_valid=False,
                                error_summary=f"AST Syntax Error: {ast_check.formatted_error()}",
                            )
                        last_error = f"Syntax error in synthesized code: {ast_check.formatted_error()}"
                        ws.reset_target_file()
                        attempt_idx += 1
                        continue

                if event_emitter:
                    event_emitter.patch_reconciled(
                        attempt=attempt_idx,
                        patch_success=True,
                        tier_used=patch_res.tier_used if patch_res else None,
                        ast_valid=True,
                        error_summary="",
                    )

                # Run test on candidate (Container execution or local runner)
                if active_container is not None:
                    c_res = active_container.evaluate(
                        candidate_content=working_code,
                        target_rel_path=str(ws.target_file_rel),
                        test_command=exec_test_cmd,
                        expected_base_commit=kwargs.get("base_commit", getattr(self.config, "base_commit", None)),
                        expected_candidate_hash=hashlib.sha256(working_code.encode("utf-8")).hexdigest(),
                    )
                    test_passed = c_res.passed
                    if not test_passed and c_res.error and ("CONTAINER" in c_res.error or "MISMATCH" in c_res.error or c_res.exit_code in (125, 128, 129, 130)):
                        return _finalize(
                            RecoveryOutcome(
                                status="CONTAINER_SETUP_ERROR",
                                total_attempts=len(history),
                                total_cost_usd=round(self.budget_tracker.total_committed, 6),
                                total_duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                                history=history,
                                message=c_res.error or c_res.traceback_summary,
                                tier_used=tier_idx,
                            )
                        )
                    test_summary = "" if test_passed else (c_res.traceback_summary or c_res.error or c_res.stderr)
                else:
                    if getattr(self.config, "require_container_isolation", False) or getattr(self.config, "use_container_runner", False):
                        return _finalize(
                            RecoveryOutcome(
                                status="CONTAINER_SETUP_ERROR",
                                total_attempts=len(history),
                                total_cost_usd=round(self.budget_tracker.total_committed, 6),
                                total_duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                                history=history,
                                message="SECURITY REFUSAL: Container runner or isolation is required by configuration, but no active container evaluator is available. Host execution of untrusted candidate code is prohibited.",
                                tier_used=tier_idx,
                            )
                        )
                    test_res = self.test_runner.run_test(exec_test_cmd, cwd=ws.temp_dir)
                    test_passed = test_res.passed
                    test_summary = "" if test_passed else test_res.traceback_summary

                attempt_dur = round((time.perf_counter() - attempt_start) * 1000, 2)
                record = AttemptRecord(
                    attempt=attempt_idx,
                    duration_ms=attempt_dur,
                    prompt_tokens=p_tokens,
                    completion_tokens=c_tokens,
                    cost_usd=call_cost,
                    tier_used=patch_res.tier_used if patch_res else None,
                    patch_success=True,
                    test_passed=test_passed,
                    error_summary=test_summary,
                    model_used=tier_model,
                    prompt_tokens_est=p_tokens_est,
                    reserved_cost_usd=reserved_cost,
                    transmitted_payload=tx_payload,
                    prompt_text=curr_prompt,
                    raw_response=curr_raw_response,
                    parsed_patch=curr_parsed_patch,
                    context_window=curr_context_window,
                    cached_tokens=cached_tokens,
                )
                history.append(record)
                if on_attempt_end:
                    on_attempt_end(record)
                if event_emitter:
                    event_emitter.test_result(
                        attempt=attempt_idx,
                        passed=test_passed,
                        duration_ms=attempt_dur,
                        error_summary=test_summary,
                    )

                if test_passed:
                    # Success!
                    diff = ws.generate_unified_diff()
                    patched_code = ws.read_target_file()
                    return _finalize(
                        RecoveryOutcome(
                            status="SUCCESS",
                            total_attempts=attempt_idx,
                            total_cost_usd=round(self.budget_tracker.total_committed, 6),
                            total_duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                            unified_diff=diff,
                            history=history,
                            message=f"Bug resolved and verified passing in attempt {attempt_idx}!",
                            patched_content=patched_code,
                            target_file=str(target_file),
                            tier_used=patch_res.tier_used if patch_res else None,
                        )
                    )
                else:
                    last_error = test_summary
                    ws.reset_target_file()
                    attempt_idx += 1

            # Max attempts exhausted
            return _finalize(
                RecoveryOutcome(
                    status="FAILED_MAX_ATTEMPTS",
                    total_attempts=len(history),
                    total_cost_usd=round(self.budget_tracker.total_committed, 6),
                    total_duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                    history=history,
                    message=f"Failed to resolve bug after {self.config.max_attempts} attempts.",
                )
            )

    def _call_model(
        self,
        prompt: str,
        effort: Optional[str] = None,
        model: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> tuple[str, int, int, int]:
        """Invoke configured inference provider via adapter."""
        adapter = self._get_adapter()
        messages = [
            {"role": "system", "content": ARGUS_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        eff_tokens = getattr(self.config, "max_tokens", 2500) or 2500
        eff_temp = float(getattr(self.config, "temperature", 0.0))
        kwargs: dict[str, Any] = {
            "messages": messages,
            "temperature": eff_temp,
            "max_tokens": eff_tokens,
        }
        if getattr(self.config, "max_completion_tokens", None) is not None:
            kwargs["max_completion_tokens"] = self.config.max_completion_tokens
        eff_effort = getattr(self.config, "reasoning_effort", None) or effort
        if eff_effort:
            kwargs["effort"] = eff_effort
        if model:
            kwargs["model"] = model
        if cwd:
            kwargs["cwd"] = cwd
        try:
            resp = adapter.chat_completion(**kwargs)
        except TypeError:
            kwargs.pop("effort", None)
            kwargs.pop("cwd", None)
            try:
                resp = adapter.chat_completion(**kwargs)
            except TypeError:
                kwargs.pop("model", None)
                resp = adapter.chat_completion(**kwargs)
        cached_tokens = getattr(resp, "cache_read_tokens", 0) or 0
        tx_payload = getattr(resp, "transmitted_payload", {}) or {}
        return resp.content, resp.prompt_tokens, resp.completion_tokens, cached_tokens, tx_payload

    def _build_prompt(
        self,
        file_path: str,
        code: str,
        test_command: str,
        error_feedback: str,
        attempt: int,
        original_failure: Optional[str] = None,
        context_radius: int = 25,
        problem_statement: Optional[str] = None,
    ) -> str:
        """Construct the prompt with code context and execution feedback."""
        # Centralized secret scrubbing: ensure credentials in error traces, problem statements,
        # or code context never reach model prompts.
        safe_error_feedback = scrub_secrets(error_feedback or "")
        safe_original_failure = scrub_secrets(original_failure or "")
        safe_problem_statement = scrub_secrets(problem_statement) if problem_statement else None

        total_lines = len(code.splitlines())
        annotated_code, start_line, end_line, target_line = extract_context_frame(
            code=code,
            file_path=file_path,
            error_feedback=safe_error_feedback or safe_original_failure or "",
            window_radius=context_radius,
            problem_statement=safe_problem_statement,
        )

        if start_line == 0:
            raise LocalizationError(
                f"Localization failure: Unable to reliably localize failure site in `{file_path}` ({total_lines} lines). "
                "Neither traceback nor problem statement identified a matching line, class, or method."
            )

        self._last_context_window = {
            "start_line": start_line,
            "end_line": end_line,
            "target_line": target_line,
        }

        if target_line is not None and (start_line > 1 or end_line < total_lines):
            code_header = (
                f"#### Localized Source Code Context:\n"
                f"Showing lines {start_line}-{end_line} of {total_lines} in `{file_path}` "
                f"(focused around failure frame line {target_line}):"
            )
        elif start_line > 1 or end_line < total_lines:
            code_header = (
                f"#### Source Code Context (Truncated):\n"
                f"Showing lines {start_line}-{end_line} of {total_lines} in `{file_path}`:"
            )
        else:
            code_header = f"#### Current Source Code (`{file_path}`):"

        # Build feedback section: if multi-attempt and safe_error_feedback differs from safe_original_failure,
        # present both the root bug context and the specific feedback from the prior attempt
        if attempt > 1 and safe_original_failure and safe_error_feedback.strip() != safe_original_failure.strip():
            feedback_section = (
                f"#### Primary Test Failure (Root Bug):\n"
                f"```\n"
                f"{safe_original_failure.strip()}\n"
                f"```\n\n"
                f"#### Feedback / Diagnostic from Attempt {attempt - 1}:\n"
                f"```\n"
                f"{safe_error_feedback.strip()}\n"
                f"```"
            )
        else:
            feedback_section = (
                f"#### Test Failure / Feedback:\n"
                f"```\n"
                f"{safe_error_feedback.strip() if safe_error_feedback else 'Test failed.'}\n"
                f"```"
            )

        problem_section = f"#### Problem Statement:\n{safe_problem_statement.strip()}\n\n" if safe_problem_statement else ""

        full_prompt = f"""### REPAIR TASK (Attempt {attempt})

Target File: `{file_path}`
Test Command: `{test_command}`

{problem_section}{code_header}
```python
{annotated_code}
```

{feedback_section}

#### Instructions:
1. Analyze the failure and identify the exact root cause in `{file_path}`.
2. Output your solution using one or more SEARCH/REPLACE blocks:
<<<<<<< SEARCH
[exact lines from the source code above to replace]
=======
[replacement lines with the bug fixed]
>>>>>>> REPLACE
3. Keep SEARCH blocks minimal (typically 2-6 lines directly surrounding the change), copied exactly from the provided source code, and uniquely anchored around the intended change to guarantee a single unambiguous match character-for-character.
4. Do NOT include line numbers or pipe symbols (' | ') in the SEARCH or REPLACE blocks. Output pure code.
5. Ensure your replacement contains 100% valid Python syntax.
"""
        return scrub_secrets(full_prompt)

    execute_recovery = run

