"""Syntax-highlighted unified diff viewer widget."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static


class DiffViewer(Static):
    """Renders a unified diff with line numbers and syntax color highlighting."""

    def __init__(self, diff_text: str = "", *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.diff_text = diff_text

    def update_diff(self, diff_text: str) -> None:
        """Update and re-render the displayed diff."""
        self.diff_text = diff_text
        self.update(self._format_diff())

    def on_mount(self) -> None:
        """Render diff on widget mount."""
        self.update(self._format_diff())

    def _format_diff(self) -> Text:
        """Format diff text with line numbers and colors."""
        if not self.diff_text:
            return Text("No diff available.", style="dim italic")

        out = Text()
        lines = self.diff_text.splitlines()

        old_lineno = 0
        new_lineno = 0

        for line in lines:
            if line.startswith("---") or line.startswith("+++") or line.startswith("@@"):
                out.append(f"       │ {line}\n", style="bold #8188ff")
                if line.startswith("@@"):
                    # Parse @@ -old,count +new,count @@
                    try:
                        parts = line.split("@@")[1].strip().split()
                        old_lineno = int(parts[0].replace("-", "").split(",")[0])
                        new_lineno = int(parts[1].replace("+", "").split(",")[0])
                    except (IndexError, ValueError):
                        pass
            elif line.startswith("diff ") or line.startswith("index ") or line.startswith("\\"):
                # File headers and "No newline at end of file" markers carry no line.
                out.append(f"       │ {line}\n", style="#667085")
            elif line.startswith("+"):
                out.append(f"  {new_lineno:4d} │ {line}\n", style="#7bc79e on #0f1a15")
                new_lineno += 1
            elif line.startswith("-"):
                out.append(f"{old_lineno:4d}   │ {line}\n", style="#e07178 on #1c1114")
                old_lineno += 1
            else:
                out.append(f"{old_lineno:4d} {new_lineno:4d} │ {line}\n", style="#667085")
                old_lineno += 1
                new_lineno += 1

        return out
