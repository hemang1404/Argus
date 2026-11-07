"""Deterministic Symbol Extraction and Codebase Localization for Argus.

Extracts high-signal identifiers, dotted modules, file references, and error traces
from issue descriptions or execution diagnostics, and executes fast local git-grep / AST searches
to pinpoint target files and line numbers in milliseconds without external LLM tokens.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Set


EXCLUDED_DIRS = {
    "tests", "test", "testing", "docs", "doc", "documentation",
    "tutorials", "examples", "benchmarks", ".github", "build", "dist",
    "migrations", "venv", ".venv", ".git", "data", "results",
    "experiments_archive", "scratch", "node_modules", "models",
    ".agents", ".gemini", "tools", "packages"
}


@dataclass
class LocalizationCandidate:
    """Represents a localized candidate edit site within a repository."""
    file_path: Path
    relative_path: str
    line_number: int
    score: float
    reason: str
    symbol: str = ""

    def __repr__(self) -> str:
        return f"<LocalizationCandidate {self.relative_path}:{self.line_number} (score={self.score:.1f}, reason='{self.reason}')>"


class DeterministicLocalizer:
    """Fast, deterministic code localizer using regex heuristics and git-grep."""

    def __init__(self, repo_dir: Path):
        self.repo_dir = Path(repo_dir).resolve()

    def extract_symbols(self, text: str) -> List[str]:
        """Extract high-signal identifiers, dotted symbols, and function names."""
        symbols: Set[str] = set()

        # 1. Dotted python paths / calls (e.g. 'astropy.io.ascii.html' or 'self.data.cols')
        for match in re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)+)\b", text):
            # Ignore standard library or trivial prefixes
            parts = match.split(".")
            if parts[0] not in {"self", "super", "os", "sys", "re", "math", "test", "tests"}:
                symbols.add(match)
                # Also add the leaf method / class name
                if len(parts[-1]) >= 3:
                    symbols.add(parts[-1])
                # Check if parts form an on-disk module path (e.g. astropy/io/ascii)
                rel_candidate = Path(*parts)
                if (self.repo_dir / rel_candidate).is_dir() or (self.repo_dir / f"{rel_candidate}.py").is_file():
                    symbols.add(str(rel_candidate).replace("\\", "/"))

        # 2. Backtick quoted identifiers `symbol()` or `MyClass`
        for match in re.findall(r"`([a-zA-Z_][a-zA-Z0-9_]*(?:\(\))?)`", text):
            cleaned = match.rstrip("()")
            if len(cleaned) >= 3 and cleaned not in {"True", "False", "None"}:
                symbols.add(cleaned)

        # 3. Direct function definitions or calls mentioned (e.g. def my_func or my_func())
        for match in re.findall(r"\bdef\s+([a-zA-Z_][a-zA-Z0-9_]*)\b", text):
            symbols.add(match)

        # 4. Exception types
        for match in re.findall(r"\b([A-Z][a-zA-Z0-9_]*(?:Error|Exception))\b", text):
            symbols.add(match)

        # 5. Format parameters (e.g. format="html", format='ascii.rst')
        for match in re.findall(r'format\s*=\s*["\']([a-zA-Z0-9_\.]+)["\']', text):
            base_fmt = match.split(".")[-1]
            symbols.add(base_fmt)
            symbols.add(base_fmt.upper())

        # 6. Distinct uppercase words / classes from title and body (e.g. HTML, Table, RST)
        for match in re.findall(r"\b([A-Z][A-Z0-9_]{2,})\b", text):
            if match not in {"ASCII", "UTF", "URL", "HTTP", "HTTPS", "JSON", "XML", "SQL", "LINUX"}:
                symbols.add(match)
            elif match in {"HTML", "RST", "CSV"}:
                symbols.add(match)

        return sorted(list(symbols), key=lambda s: (len(s.split(".")), len(s)), reverse=True)

    def extract_explicit_file_paths(self, text: str) -> List[Path]:
        """Extract existing relative python files mentioned directly in the problem text."""
        candidates = []
        # Pattern for paths ending with .py
        matches = re.findall(r"([a-zA-Z0-9_/\\]+\.py)\b", text)
        for m in matches:
            cleaned = m.replace("\\", "/").strip("./")
            full_path = self.repo_dir / cleaned
            if full_path.exists() and full_path.is_file():
                if not any(part in EXCLUDED_DIRS for part in full_path.parts):
                    candidates.append(full_path)
        return candidates

    def locate_traceback_frame(self, traceback_text: str) -> Optional[LocalizationCandidate]:
        """Extract candidate location from standard Python exception traceback."""
        if not traceback_text:
            return None

        # Standard traceback line: File "path/to/file.py", line 123, in func_name
        tb_matches = re.findall(r'File\s+["\']([^"\']+\.py)["\'],\s+line\s+(\d+)(?:,\s+in\s+([a-zA-Z0-9_]+))?', traceback_text)
        if not tb_matches:
            return None

        # Inspect frames in reverse order (closest to error)
        for file_str, line_str, func_name in reversed(tb_matches):
            p = Path(file_str)
            # Check if relative to repo or absolute
            target_file = None
            if p.is_absolute() and p.exists():
                target_file = p
            else:
                norm = file_str.replace("\\", "/").strip("./")
                candidate_p = self.repo_dir / norm
                if candidate_p.exists():
                    target_file = candidate_p
                else:
                    # Search for matching filename inside repo
                    matches = list(self.repo_dir.glob(f"**/{p.name}"))
                    if matches:
                        target_file = matches[0]

            if target_file and target_file.exists():
                rel_path = str(target_file.relative_to(self.repo_dir)).replace("\\", "/")
                # Ignore test files in traceback if possible
                if any(part in EXCLUDED_DIRS for part in target_file.parts):
                    continue

                line_num = int(line_str)
                return LocalizationCandidate(
                    file_path=target_file,
                    relative_path=rel_path,
                    line_number=line_num,
                    score=1000.0,
                    reason=f"Traceback frame in {func_name or 'module'} at line {line_num}",
                    symbol=func_name or "",
                )

        return None

    def search_git_grep(self, symbol: str, max_results: int = 10) -> List[tuple[Path, int, str]]:
        """Run fast git-grep to locate definitions or occurrences of a symbol."""
        if not (self.repo_dir / ".git").exists():
            return []

        # Search for def / class first
        results = []
        queries = [
            f"def {symbol}",
            f"class {symbol}",
            symbol,
        ]

        for q in queries:
            cmd = ["git", "grep", "-n", "-I", q]
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(self.repo_dir),
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if proc.returncode == 0:
                    for line in proc.stdout.splitlines():
                        parts = line.split(":", 2)
                        if len(parts) >= 3:
                            rel_p, line_str, content = parts[0], parts[1], parts[2]
                            p = self.repo_dir / rel_p
                            if p.exists() and p.suffix == ".py":
                                if not any(ex in rel_p.replace("\\", "/").split("/") for ex in EXCLUDED_DIRS):
                                    try:
                                        results.append((p, int(line_str), content.strip()))
                                    except ValueError:
                                        continue
                if results:
                    break
            except Exception:
                pass

        return results[:max_results]

    def locate(self, problem_text: str, traceback_text: str = "") -> List[LocalizationCandidate]:
        """End-to-end deterministic localization.
        
        Returns a ranked list of candidate edit locations (highest score first).
        """
        candidates: List[LocalizationCandidate] = []

        # 1. Highest priority: Traceback frame if available
        tb_candidate = self.locate_traceback_frame(traceback_text)
        if tb_candidate:
            candidates.append(tb_candidate)

        # 2. Explicit file paths mentioned in the problem statement
        explicit_files = self.extract_explicit_file_paths(problem_text)
        for ef in explicit_files:
            rel = str(ef.relative_to(self.repo_dir)).replace("\\", "/")
            candidates.append(LocalizationCandidate(
                file_path=ef,
                relative_path=rel,
                line_number=1,  # Default to beginning or first function
                score=500.0,
                reason="Explicit file mentioned in issue description",
            ))

        # 3. Extract symbols and git-grep
        symbols = self.extract_symbols(problem_text)
        for sym in symbols[:8]:  # Top 8 symbols
            leaf = sym.split(".")[-1]
            if len(leaf) < 4:
                continue
            grep_hits = self.search_git_grep(leaf, max_results=3)
            for path, line_no, content in grep_hits:
                rel = str(path.relative_to(self.repo_dir)).replace("\\", "/")
                # Give bonus if definition
                is_def = "def " in content or "class " in content
                score = 300.0 if is_def else 150.0

                # Check if already present
                existing = next((c for c in candidates if c.file_path == path), None)
                if existing:
                    if is_def:
                        existing.line_number = line_no
                        existing.score += 150.0
                        existing.reason += f" + definition of '{leaf}' at line {line_no}"
                else:
                    candidates.append(LocalizationCandidate(
                        file_path=path,
                        relative_path=rel,
                        line_number=line_no,
                        score=score,
                        reason=f"Found symbol '{leaf}' at line {line_no}",
                        symbol=leaf,
                    ))

        # Sort descending by score
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates
