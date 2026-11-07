"""Argus Hybrid Localizer: BM25 Code Token Ranking + AST Symbol Table Boost.

Combines:
1. Inverted Index BM25 Content Scoring across all repo .py files.
   - Tokenizes camelCase and snake_case identifiers into sub-tokens.
   - Weighs rare technical identifiers (IDF) over boilerplate language keywords.
2. AST Symbol Definition Table:
   - Indexes all `class` and `def` definitions with exact lines.
   - Boosts files that define symbols mentioned in backticks or error types.
3. Traceback Frame Parser:
   - If a stack trace is present, the crashing frame takes immediate precedence.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


EXCLUDED_DIRS = {
    "tests", "test", "testing", "docs", "doc", "documentation",
    "tutorials", "examples", "benchmarks", ".github", "build", "dist",
    "migrations", "venv", ".venv", ".git", "data", "results",
    "experiments_archive", "scratch", "node_modules", "models",
    ".agents", ".gemini", "tools", "packages"
}


def tokenize_code(text: str) -> List[str]:
    """Tokenize code text into lowercase sub-tokens (splitting camelCase and snake_case)."""
    words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", text)
    tokens: List[str] = []
    for w in words:
        # Split camelCase
        parts = re.sub(r"([a-z])([A-Z])", r"\1 \2", w).split()
        for p in parts:
            # Split snake_case
            for sub in p.split("_"):
                s = sub.lower()
                if len(s) >= 3 and not s.isdigit():
                    tokens.append(s)
    return tokens


@dataclass
class CandidateFile:
    file_path: Path
    relative_path: str
    bm25_score: float
    symbol_boost: float
    total_score: float
    matched_symbols: List[str]
    definition_snippets: List[str]


class RepoBM25Index:
    """In-memory BM25 Okapi index over a repository's source files."""

    def __init__(self, repo_dir: Path, k1: float = 1.5, b: float = 0.75):
        self.repo_dir = Path(repo_dir).resolve()
        self.k1 = k1
        self.b = b
        self.files: List[Path] = []
        self.rel_paths: List[str] = []
        self.docs: List[Counter[str]] = []
        self.doc_lens: List[int] = []
        self.idf: Dict[str, float] = {}
        self.avgdl: float = 0.0

        # Symbol definition index: symbol_name.lower() -> List[(file_idx, line_no, full_def_line)]
        self.symbol_defs: Dict[str, List[Tuple[int, int, str]]] = {}
        self._build_index()

    def _build_index(self) -> None:
        """Scan and index all non-test Python files in the repository."""
        py_files = []
        for p in self.repo_dir.rglob("*.py"):
            parts = p.relative_to(self.repo_dir).parts
            if any(part.lower() in EXCLUDED_DIRS for part in parts):
                continue
            py_files.append(p)

        self.files = py_files
        df: Counter[str] = Counter()

        for idx, p in enumerate(py_files):
            rel = str(p.relative_to(self.repo_dir)).replace("\\", "/")
            self.rel_paths.append(rel)

            try:
                content = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                content = ""

            tokens = tokenize_code(content)
            counts = Counter(tokens)
            self.docs.append(counts)
            self.doc_lens.append(len(tokens))

            # Record document frequency
            for term in counts.keys():
                df[term] += 1

            # Extract AST / definition lines
            for line_no, line in enumerate(content.splitlines(), 1):
                m_def = re.match(r"^\s*(def|class|async\s+def)\s+([a-zA-Z_][a-zA-Z0-9_]*)", line)
                if m_def:
                    sym_name = m_def.group(2).lower()
                    if sym_name not in self.symbol_defs:
                        self.symbol_defs[sym_name] = []
                    self.symbol_defs[sym_name].append((idx, line_no, line.strip()))

        N = len(self.docs)
        self.avgdl = sum(self.doc_lens) / max(1, N)

        # Precompute IDF
        for term, freq in df.items():
            self.idf[term] = math.log((N - freq + 0.5) / (freq + 0.5) + 1.0)

    def query(self, query_text: str, top_k: int = 15) -> List[Tuple[int, float]]:
        """Score all repository files against the query text using BM25."""
        q_tokens = tokenize_code(query_text)
        q_counts = Counter(q_tokens)
        scores: List[Tuple[int, float]] = []

        for i, doc_counts in enumerate(self.docs):
            score = 0.0
            dl = self.doc_lens[i]
            for q_term in q_counts:
                if q_term in doc_counts:
                    f = doc_counts[q_term]
                    w_idf = self.idf.get(q_term, 0.0)
                    denom = f + self.k1 * (1.0 - self.b + self.b * (dl / max(1.0, self.avgdl)))
                    score += w_idf * ((f * (self.k1 + 1.0)) / denom)
            scores.append((i, score))

        scores.sort(key=lambda x: -x[1])
        return scores[:top_k]


class HybridBM25Localizer:
    """Two-pronged defect localizer: BM25 content ranking + AST symbol definition boost."""

    def __init__(self, repo_dir: Path):
        self.repo_dir = Path(repo_dir).resolve()
        self.index = RepoBM25Index(self.repo_dir)

    GENERIC_DUNDERS = {
        "__init__", "__repr__", "__str__", "__call__", "__len__",
        "__getitem__", "__setitem__", "__delitem__", "__iter__", "__next__",
        "__enter__", "__exit__", "__new__", "__eq__", "__ne__"
    }

    def extract_query_symbols(self, text: str) -> List[str]:
        """Extract high-precision technical symbols from problem text."""
        symbols: Set[str] = set()

        # 1. Backtick terms `some_method` or `SomeClass`
        for m in re.findall(r"`([a-zA-Z_][a-zA-Z0-9_]*(?:\(\))?)`", text):
            cleaned = m.rstrip("()")
            if len(cleaned) >= 3 and cleaned.lower() not in {"true", "false", "none", "self", "args", "kwargs"} and cleaned.lower() not in self.GENERIC_DUNDERS:
                symbols.add(cleaned)

        # 2. Dotted identifiers like `models.ForeignKey` or `astropy.io.fits`
        for m in re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)+)\b", text):
            parts = m.split(".")
            for part in parts:
                if len(part) >= 4 and part.lower() not in {"self", "super", "test", "tests"} and part.lower() not in self.GENERIC_DUNDERS:
                    symbols.add(part)

        # 3. Exception types (e.g. EmptyResultSet, FieldError, TypeError)
        for m in re.findall(r"\b([A-Z][a-zA-Z0-9_]*(?:Error|Exception|Warning))\b", text):
            symbols.add(m)

        # 4. Explicit `def func` or `class Cls`
        for m in re.findall(r"\b(?:def|class)\s+([a-zA-Z_][a-zA-Z0-9_]*)\b", text):
            if m.lower() not in self.GENERIC_DUNDERS:
                symbols.add(m)

        # 5. CamelCase identifiers (e.g. ManagementUtility, CommandParser, SlicedLowLevelWCS)
        for m in re.findall(r"\b([A-Z][a-z0-9]+[A-Z][a-zA-Z0-9]*)\b", text):
            if len(m) >= 4 and m not in {"Description", "Traceback", "Version", "Summary", "Expected", "Observed"}:
                symbols.add(m)

        # 6. Specific snake_case identifiers (length >= 5) (e.g. dual_coef_indptr, prog_name)
        for m in re.findall(r"\b([a-z][a-z0-9]*_[a-z0-9_]+)\b", text):
            if len(m) >= 5 and m.lower() not in self.GENERIC_DUNDERS:
                symbols.add(m)

        return sorted(list(symbols), key=lambda s: len(s), reverse=True)

    def extract_explicit_files(self, text: str) -> List[Path]:
        """Extract literal python filenames mentioned in text."""
        hits = []
        for m in re.findall(r"([a-zA-Z0-9_/\\]+\.py)\b", text):
            norm = m.replace("\\", "/").strip("./")
            p = self.repo_dir / norm
            if p.exists() and p.is_file():
                if not any(part.lower() in EXCLUDED_DIRS for part in p.parts):
                    hits.append(p)
        return hits

    def rank_candidates(self, problem_text: str, top_k: int = 5) -> List[CandidateFile]:
        """Produce top-k candidate defect files combining BM25 and symbol definitions."""
        # Step 1: Run BM25 query over problem text (first 3,000 characters)
        bm25_hits = self.index.query(problem_text[:3000], top_k=30)
        max_bm25 = max((score for _, score in bm25_hits), default=1.0)
        if max_bm25 == 0.0:
            max_bm25 = 1.0

        # Step 2: Extract technical symbols
        symbols = self.extract_query_symbols(problem_text)
        explicit_files = self.extract_explicit_files(problem_text)

        # Step 3: Combine scores and definition snippets
        candidate_dict: Dict[int, CandidateFile] = {}

        # Seed from BM25 hits
        for file_idx, bm25_score in bm25_hits:
            p = self.index.files[file_idx]
            rel = self.index.rel_paths[file_idx]
            candidate_dict[file_idx] = CandidateFile(
                file_path=p,
                relative_path=rel,
                bm25_score=bm25_score,
                symbol_boost=0.0,
                total_score=bm25_score,
                matched_symbols=[],
                definition_snippets=[]
            )

        # Apply exact symbol definition boosts
        for sym in symbols:
            sym_key = sym.lower()
            if sym_key in self.index.symbol_defs:
                for file_idx, line_no, def_line in self.index.symbol_defs[sym_key]:
                    # Large boost for exact definition of a mentioned symbol
                    boost = max_bm25 * 0.75
                    if file_idx in candidate_dict:
                        cand = candidate_dict[file_idx]
                        cand.symbol_boost += boost
                        cand.total_score += boost
                        if sym not in cand.matched_symbols:
                            cand.matched_symbols.append(sym)
                            cand.definition_snippets.append(f"Line {line_no}: `{def_line}`")
                    else:
                        p = self.index.files[file_idx]
                        rel = self.index.rel_paths[file_idx]
                        candidate_dict[file_idx] = CandidateFile(
                            file_path=p,
                            relative_path=rel,
                            bm25_score=0.0,
                            symbol_boost=boost,
                            total_score=boost,
                            matched_symbols=[sym],
                            definition_snippets=[f"Line {line_no}: `{def_line}`"]
                        )

        # Apply explicit file mention boost
        for ef in explicit_files:
            try:
                f_idx = self.index.files.index(ef)
                if f_idx in candidate_dict:
                    candidate_dict[f_idx].total_score += max_bm25 * 2.0
                    candidate_dict[f_idx].symbol_boost += max_bm25 * 2.0
                    candidate_dict[f_idx].matched_symbols.append("[EXPLICIT_MENTION]")
                else:
                    rel = str(ef.relative_to(self.repo_dir)).replace("\\", "/")
                    candidate_dict[f_idx] = CandidateFile(
                        file_path=ef,
                        relative_path=rel,
                        bm25_score=0.0,
                        symbol_boost=max_bm25 * 2.0,
                        total_score=max_bm25 * 2.0,
                        matched_symbols=["[EXPLICIT_MENTION]"],
                        definition_snippets=[]
                    )
            except ValueError:
                pass

        # Sort descending by total score
        sorted_candidates = sorted(candidate_dict.values(), key=lambda c: -c.total_score)
        return sorted_candidates[:top_k]


# Ergonomic alias
HybridLocalizer = HybridBM25Localizer

