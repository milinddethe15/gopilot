from __future__ import annotations
import subprocess
from pathlib import Path

from rich.console import Console

from models import ValidationResult

console = Console()


class Validator:
    """
    Runs go build → go vet → go test in sequence.
    Stops at the first failure so errors are clean and actionable.
    """

    def __init__(self, repo_path: Path, timeout: int = 120):
        self.repo_path = repo_path
        self.timeout = timeout

    def validate(self, test_targets: list[str] | None = None) -> ValidationResult:
        targets = test_targets or ["./..."]

        # ---- 1. Ensure dependencies are available ----
        self._go_mod_download()

        # ---- 2. go build ----
        console.print("[cyan]  go build ./...[/cyan]")
        build = self._run(["go", "build", "./..."])
        if build.returncode != 0:
            console.print(f"[red]  ✗ build failed[/red]")
            return ValidationResult(
                success=False,
                build_output=build.stdout + build.stderr,
            )
        console.print("[green]  ✓ build ok[/green]")

        # ---- 3. go vet ----
        console.print("[cyan]  go vet ./...[/cyan]")
        vet = self._run(["go", "vet", "./..."])
        if vet.returncode != 0:
            console.print("[red]  ✗ vet failed[/red]")
            return ValidationResult(
                success=False,
                build_output=build.stdout,
                vet_output=vet.stdout + vet.stderr,
            )
        console.print("[green]  ✓ vet ok[/green]")

        # ---- 4. go test ----
        test_cmd = ["go", "test", f"-timeout={self.timeout}s", "-v", "-count=1"] + targets
        console.print(f"[cyan]  go test {' '.join(targets)}[/cyan]")
        test = self._run(test_cmd)
        if test.returncode != 0:
            console.print("[red]  ✗ tests failed[/red]")
            return ValidationResult(
                success=False,
                build_output=build.stdout,
                vet_output=vet.stdout,
                test_output=test.stdout + test.stderr,
            )

        console.print("[green]  ✓ all tests passed[/green]")
        return ValidationResult(
            success=True,
            build_output=build.stdout,
            vet_output=vet.stdout,
            test_output=test.stdout,
        )

    # ------------------------------------------------------------------ #

    def _go_mod_download(self) -> None:
        """Download module deps quietly; don't fail the pipeline if this errors."""
        vendor_dir = self.repo_path / "vendor"
        if vendor_dir.exists():
            return  # vendor mode — deps already present
        self._run(["go", "mod", "download"])

    def _run(self, cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            timeout=self.timeout + 30,  # slightly longer than go test -timeout
        )
