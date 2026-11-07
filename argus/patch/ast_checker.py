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

    def formatted_error(self) -> str:
        """User-friendly error description for feedback loop."""
        if self.is_valid:
            return "Syntax OK"
        return (
            f"SyntaxError at line {self.line_number}, col {self.column_offset}: "
            f"{self.error_message} -> '{self.line_snippet}'"
        )


class ASTChecker:
    """Enforces full-file AST syntax validity on synthesized code."""

    @staticmethod
    def validate_python_code(code: str, filename: str = "<synthesized>") -> ASTValidationResult:
        """Parse the full Python file module into an Abstract Syntax Tree.
        
        Args:
            code: The complete synthesized contents of the file after patching.
            filename: The relative or absolute path of the target file.
            
        Returns:
            ASTValidationResult indicating success or detailed syntax error location.
        """
        try:
            ast.parse(code, filename=filename, mode="exec")
            return ASTValidationResult(is_valid=True)
        except SyntaxError as err:
            return ASTValidationResult(
                is_valid=False,
                error_message=err.msg or "Invalid syntax",
                line_number=err.lineno,
                column_offset=err.offset,
                line_snippet=(err.text or "").strip(),
            )
        except Exception as ex:
            return ASTValidationResult(
                is_valid=False,
                error_message=f"Parser failure: {str(ex)}",
            )
