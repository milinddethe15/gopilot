from __future__ import annotations
import subprocess
from pathlib import Path


# ---------------------------------------------------------------------------
#  Basic I/O
# ---------------------------------------------------------------------------

def read_file(repo_path: Path, relative_path: str) -> str:
    """Read a file from the repo. Returns content or an error message string."""
    full = repo_path / relative_path
    if not full.exists():
        return f"ERROR: file not found: {relative_path}"
    try:
        return full.read_text(errors="replace")
    except OSError as e:
        return f"ERROR reading {relative_path}: {e}"


def write_file(repo_path: Path, relative_path: str, content: str) -> None:
    full = repo_path / relative_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)


def list_directory(repo_path: Path, relative_path: str = ".") -> str:
    """List directory contents, excluding common noise."""
    SKIP = {".git", "vendor", "node_modules", "__pycache__", ".idea", ".vscode"}
    target = repo_path / relative_path
    if not target.exists():
        return f"ERROR: directory not found: {relative_path}"
    if not target.is_dir():
        return f"ERROR: not a directory: {relative_path}"

    lines: list[str] = []
    for item in sorted(target.iterdir()):
        if item.name in SKIP:
            continue
        prefix = "📁 " if item.is_dir() else "  "
        lines.append(f"{prefix}{item.name}")
    return "\n".join(lines) if lines else "(empty directory)"


# ---------------------------------------------------------------------------
#  Search-and-replace edit application
# ---------------------------------------------------------------------------

class EditApplicationError(Exception):
    pass


def apply_search_replace(
    repo_path: Path, relative_path: str, search: str, replace: str
) -> None:
    """
    Apply a SEARCH/REPLACE edit to a file.

    Tries exact match first; falls back to whitespace-normalised match.
    Raises EditApplicationError if neither succeeds.
    """
    full = repo_path / relative_path
    if not full.exists():
        raise EditApplicationError(f"File not found: {relative_path}")

    content = full.read_text(errors="replace")

    # --- exact match ---
    if search in content:
        full.write_text(content.replace(search, replace, 1))
        return

    # --- normalised-whitespace match ---
    norm_search = _normalise(search)
    norm_content = _normalise(content)
    if norm_search in norm_content:
        # Locate the matching region in the original and replace it
        idx = norm_content.index(norm_search)
        # Map normalised index back to original: rebuild mapping
        orig_idx = _map_normalised_index(content, idx)
        orig_end = _map_normalised_index(content, idx + len(norm_search))
        full.write_text(content[:orig_idx] + replace + content[orig_end:])
        return

    raise EditApplicationError(
        f"SEARCH block not found in {relative_path}.\n"
        f"First 200 chars of search:\n{search[:200]}\n\n"
        f"Tip: the SEARCH block must match the file content exactly "
        f"(whitespace, indentation, blank lines)."
    )


def _normalise(s: str) -> str:
    """Strip trailing whitespace from each line for fuzzy matching."""
    return "\n".join(line.rstrip() for line in s.splitlines())


def _map_normalised_index(original: str, norm_idx: int) -> int:
    """
    Map a character index in the normalised string back to the original.
    Works by replaying the normalisation and tracking offsets.
    """
    orig_pos = 0
    norm_pos = 0
    lines = original.split("\n")
    for line in lines:
        stripped = line.rstrip()
        if norm_pos + len(stripped) > norm_idx:
            return orig_pos + (norm_idx - norm_pos)
        norm_pos += len(stripped) + 1   # +1 for \n
        orig_pos += len(line) + 1
        if norm_pos > norm_idx:
            break
    return orig_pos


# ---------------------------------------------------------------------------
#  Git helpers
# ---------------------------------------------------------------------------

def get_git_diff(repo_path: Path) -> str:
    result = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    return result.stdout


def commit_changes(repo_path: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", message, "--allow-empty"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
