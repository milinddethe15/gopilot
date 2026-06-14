from __future__ import annotations
import json
import re
import subprocess
from pathlib import Path


# ---------------------------------------------------------------------------
# Regex patterns for Go symbol extraction (single-pass, line-level)
# ---------------------------------------------------------------------------
_FUNC = re.compile(
    r'^func\s+(?:\((?P<recv>[^)]+)\)\s+)?(?P<name>[A-Za-z_]\w*)\s*\('
)
_TYPE = re.compile(r'^type\s+(?P<name>[A-Za-z_]\w+)\s+(?:struct|interface|\w)')
_CONST_BLOCK = re.compile(r'^\s+(?P<name>[A-Za-z_]\w+)\s*(?:=\s*".+"|[A-Z_]+\s*=).*$')


class RepoMapper:
    """
    Builds a compact, Go-aware repository map that fits comfortably in the
    Claude context window (~3–6 KB for a medium-sized package).

    Covers:
      - Package → file mapping (from `go list -json`)
      - Per-file function/type signatures (regex, not full AST)
      - Convention hints: error style, test structure, module name
    """

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path

    def build(self, max_symbols_per_file: int = 40) -> str:
        packages = self._list_packages()
        module_name = self._get_module_name()
        conventions = self._sniff_conventions()

        sections: list[str] = []
        sections.append(self._fmt_header(module_name, packages))

        for pkg in packages:
            for go_file in pkg.get("GoFiles", []) + pkg.get("TestGoFiles", []):
                rel_path = Path(pkg.get("Dir", "")).relative_to(self.repo_path) / go_file
                symbols = self._extract_symbols(
                    self.repo_path / rel_path, max_symbols_per_file
                )
                if symbols:
                    sections.append(f"\n### {rel_path}\n" + "\n".join(f"  {s}" for s in symbols))

        if conventions:
            sections.append("\n### Conventions Detected\n" + "\n".join(f"  • {c}" for c in conventions))

        return "\n".join(sections)

    # ------------------------------------------------------------------ #
    #  Package structure via go list                                       #
    # ------------------------------------------------------------------ #

    def _list_packages(self) -> list[dict]:
        result = subprocess.run(
            ["go", "list", "-e", "-json", "./..."],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if not result.stdout.strip():
            return []
        return self._parse_go_list_json(result.stdout)

    @staticmethod
    def _parse_go_list_json(output: str) -> list[dict]:
        """go list -json emits concatenated JSON objects, not a JSON array."""
        packages: list[dict] = []
        decoder = json.JSONDecoder()
        idx = 0
        output = output.strip()
        while idx < len(output):
            while idx < len(output) and output[idx].isspace():
                idx += 1
            if idx >= len(output):
                break
            try:
                obj, end = decoder.raw_decode(output, idx)
                packages.append(obj)
                idx = end
            except json.JSONDecodeError:
                break
        return packages

    def _get_module_name(self) -> str:
        go_mod = self.repo_path / "go.mod"
        if not go_mod.exists():
            return "unknown"
        for line in go_mod.read_text().splitlines():
            if line.startswith("module "):
                return line.split()[1]
        return "unknown"

    # ------------------------------------------------------------------ #
    #  Symbol extraction (per-file, regex-based)                          #
    # ------------------------------------------------------------------ #

    def _extract_symbols(self, file_path: Path, limit: int) -> list[str]:
        if not file_path.exists():
            return []
        try:
            lines = file_path.read_text(errors="replace").splitlines()
        except OSError:
            return []

        symbols: list[str] = []
        for line in lines:
            m = _FUNC.match(line)
            if m:
                recv = m.group("recv")
                name = m.group("name")
                sig = f"func ({recv}) {name}(…)" if recv else f"func {name}(…)"
                symbols.append(sig)
                continue
            m = _TYPE.match(line)
            if m:
                symbols.append(f"type {m.group('name')} …")
                continue

        return symbols[:limit]

    # ------------------------------------------------------------------ #
    #  Convention sniffing                                                 #
    # ------------------------------------------------------------------ #

    def _sniff_conventions(self) -> list[str]:
        hints: list[str] = []

        # Error style
        go_files = list(self.repo_path.rglob("*.go"))
        sample = "\n".join(
            p.read_text(errors="replace")
            for p in go_files[:20]
            if "vendor" not in str(p)
        )
        if "fmt.Errorf(" in sample:
            hints.append("Error wrapping: fmt.Errorf(\"...: %w\", err)")
        elif "errors.New(" in sample:
            hints.append("Errors created with errors.New()")
        if "t.Run(" in sample:
            hints.append("Tests use t.Run() sub-tests")
        if "tt.name" in sample or "tc.name" in sample or "name:" in sample:
            hints.append("Table-driven tests with named cases")
        if "testify" in sample or "assert." in sample:
            hints.append("Test assertions via testify")
        if "require." in sample:
            hints.append("Fatal assertions via require.*")

        # CONTRIBUTING / README hints
        for fname in ("CONTRIBUTING.md", "contributing.md"):
            p = self.repo_path / fname
            if p.exists():
                hints.append("CONTRIBUTING.md present — read for PR requirements")
                break

        return hints

    # ------------------------------------------------------------------ #
    #  Formatting                                                          #
    # ------------------------------------------------------------------ #

    def _fmt_header(self, module_name: str, packages: list[dict]) -> str:
        lines = [f"# Repo Map — {module_name}", ""]
        lines.append("## Packages")
        for pkg in packages:
            import_path = pkg.get("ImportPath", "?")
            go_files = pkg.get("GoFiles", [])
            test_files = pkg.get("TestGoFiles", [])
            all_files = go_files + ([f"[test] {f}" for f in test_files])
            lines.append(f"  {import_path}")
            if all_files:
                lines.append("    " + ", ".join(all_files))
        lines.append("")
        lines.append("## Symbols")
        return "\n".join(lines)
