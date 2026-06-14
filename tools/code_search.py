from __future__ import annotations
import shutil
import subprocess
from pathlib import Path


class CodeSearcher:
    """
    Wraps ripgrep (or grep as fallback) for fast in-repo code search.
    Used by the planner agent to find symbol definitions and usages.
    """

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path
        self._rg = shutil.which("rg")  # None if ripgrep not installed

    def search(
        self,
        pattern: str,
        file_glob: str = "*.go",
        context_lines: int = 3,
        max_results: int = 30,
    ) -> str:
        """
        Search for `pattern` across Go files.
        Returns formatted matches with filename:line context.
        """
        if self._rg:
            return self._ripgrep(pattern, file_glob, context_lines, max_results)
        return self._grep(pattern, file_glob, context_lines, max_results)

    def find_symbol(self, symbol_name: str) -> str:
        """Find all definitions (func/type/var) of a symbol name."""
        pattern = rf"\b{re.escape(symbol_name)}\b"
        return self.search(pattern, context_lines=2, max_results=20)

    # ------------------------------------------------------------------ #
    #  Backends                                                            #
    # ------------------------------------------------------------------ #

    def _ripgrep(
        self, pattern: str, file_glob: str, context: int, max_results: int
    ) -> str:
        cmd = [
            self._rg,
            "--color=never",
            "--no-heading",
            "--line-number",
            f"--glob={file_glob}",
            f"--context={context}",
            f"--max-count={max_results}",
            "--glob=!vendor/**",
            pattern,
            str(self.repo_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        out = result.stdout.strip()
        if not out:
            return f"(no matches for pattern: {pattern!r})"
        # Trim absolute paths to relative
        out = out.replace(str(self.repo_path) + "/", "")
        lines = out.splitlines()
        if len(lines) > max_results * (context * 2 + 2):
            lines = lines[: max_results * (context * 2 + 2)]
            lines.append("… (results truncated)")
        return "\n".join(lines)

    def _grep(
        self, pattern: str, file_glob: str, context: int, max_results: int
    ) -> str:
        cmd = [
            "grep",
            "-r",
            "--include=" + file_glob,
            f"-A{context}",
            f"-B{context}",
            "-n",
            "--color=never",
            pattern,
            str(self.repo_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        out = result.stdout.strip()
        if not out:
            return f"(no matches for pattern: {pattern!r})"
        out = out.replace(str(self.repo_path) + "/", "")
        lines = out.splitlines()[:max_results * (context * 2 + 2)]
        return "\n".join(lines)


import re  # noqa: E402 — placed after class to avoid circular at top
