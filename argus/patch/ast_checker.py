"""Universal AST Syntax Checker for validating code patches before writing to disk."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Optional


@dataclass
class ASTValidationResult:
    """Outcome of full-file AST validation."""
    is_valid: bool
    error_message: Optional[str] = None
    line_number: Optional[int] = None
    column_offset: Optional[int] = None
    line_snippet: Optional[str] = None
    # What "valid" means here. This gate is SYNTACTIC: ast.parse plus the
    # compile-time checks of compile() (e.g. module-level ``return``). It says
    # nothing about semantic correctness; only tests can speak to that.
    level: str = "syntactic"

    def formatted_error(self) -> str:
        """User-friendly error description for feedback loop."""
        if self.is_valid:
            return "Syntax OK"
        return (
            f"SyntaxError at line {self.line_number}, col {self.column_offset}: "
            f"{self.error_message} -> '{self.line_snippet}'"
        )


PYTHON_SUFFIXES = (".py", ".pyi", ".pyw")


def is_python_filename(filename: str) -> bool:
    """Case-insensitive check for Python source file names."""
    return filename.lower().endswith(PYTHON_SUFFIXES)


class ASTChecker:
    """Enforces full-file AST syntax validity on synthesized code."""

    @staticmethod
    def validate_python_code(code: str, filename: str = "<synthesized>") -> ASTValidationResult:
        """Syntactic validation of a full Python module (not semantic).

        Runs ``ast.parse`` and then ``compile()`` on the tree so that errors only
        detected at compile time (``return``/``yield`` outside a function,
        ``break`` outside a loop, misuse of ``nonlocal``) are also rejected.
        Passing this gate does NOT mean the code is correct or that tests pass.
        
        Args:
            code: The complete synthesized contents of the file after patching.
            filename: The relative or absolute path of the target file.
            
        Returns:
            ASTValidationResult indicating success or detailed syntax error location.
        """
        try:
            tree = ast.parse(code, filename=filename, mode="exec")
            compile(tree, filename, "exec", dont_inherit=True)
            return ASTValidationResult(is_valid=True)
        except SyntaxError as err:
            return ASTValidationResult(
                is_valid=False,
                error_message=err.msg or "Invalid syntax",
                line_number=err.lineno,
                column_offset=err.offset,
                line_snippet=(err.text or "").strip(),
            )
        except (ValueError, RecursionError, MemoryError) as ex:
            return ASTValidationResult(
                is_valid=False,
                error_message=f"Parser failure: {str(ex)}",
            )
