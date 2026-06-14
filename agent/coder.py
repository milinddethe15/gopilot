from __future__ import annotations
import re
from pathlib import Path

import anthropic
from rich.console import Console

from models import CodeEdit, Plan, Issue, ValidationResult
from tools.file_ops import read_file, write_file, apply_search_replace, EditApplicationError

console = Console()

# ---------------------------------------------------------------------------
#  Parsing patterns for SEARCH/REPLACE blocks
# ---------------------------------------------------------------------------

_EDIT_RE = re.compile(
    r"GOFILE:\s*(?P<path>.+?)\s*\n"
    r"<<<<<<< SEARCH\n"
    r"(?P<search>.*?)\n"
    r"=======\n"
    r"(?P<replace>.*?)\n"
    r">>>>>>> REPLACE",
    re.DOTALL,
)

_NEW_FILE_RE = re.compile(
    r"NEWFILE:\s*(?P<path>.+?)\s*\n"
    r"<<<<<<< CREATE\n"
    r"(?P<content>.*?)\n"
    r">>>>>>> END",
    re.DOTALL,
)


class Coder:
    """
    Single-shot code generator.

    Takes the planner's Plan, reads the relevant files in full, and asks
    Claude to produce SEARCH/REPLACE blocks. On validation failure, accepts
    the error context and produces corrective blocks only.
    """

    def __init__(
        self,
        client: anthropic.Anthropic,
        config: dict,
        repo_path: Path,
        system_prompt: str,
    ):
        self.client = client
        self.config = config
        self.repo_path = repo_path
        self.system_prompt = system_prompt

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def generate_edits(
        self,
        issue: Issue,
        plan: Plan,
        validation_error: ValidationResult | None = None,
        previous_edits: list[CodeEdit] | None = None,
    ) -> list[CodeEdit]:
        """
        Generate SEARCH/REPLACE edits. On first call, `validation_error` is None.
        On retries, it contains the failed validation output.
        """
        file_contents = self._read_plan_files(plan)
        user_message = self._build_user_message(
            issue, plan, file_contents, validation_error, previous_edits
        )

        console.print("[bold cyan]Coder generating edits…[/bold cyan]")

        response = self.client.messages.create(
            model=self.config["model"],
            max_tokens=self.config.get("max_tokens", 8192),
            system=self.system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )

        raw_text = "".join(b.text for b in response.content if b.type == "text")
        edits = self._parse_edits(raw_text)

        if not edits:
            console.print("[yellow]⚠ No SEARCH/REPLACE blocks found in coder response[/yellow]")
        else:
            console.print(f"[green]✓ Parsed {len(edits)} edit block(s)[/green]")

        return edits

    def apply_edits(self, edits: list[CodeEdit]) -> list[str]:
        """
        Apply edits to the repository. Returns list of modified/created file paths.
        Raises EditApplicationError on the first edit that cannot be applied.
        """
        modified: list[str] = []
        for edit in edits:
            if edit.is_new_file:
                write_file(self.repo_path, edit.file_path, edit.replace)
                console.print(f"  [green]+ created {edit.file_path}[/green]")
            else:
                apply_search_replace(
                    self.repo_path, edit.file_path, edit.search, edit.replace
                )
                console.print(f"  [green]✎ edited {edit.file_path}[/green]")
            modified.append(edit.file_path)
        return modified

    # ------------------------------------------------------------------ #
    #  Edit parsing                                                        #
    # ------------------------------------------------------------------ #

    def _parse_edits(self, text: str) -> list[CodeEdit]:
        edits: list[CodeEdit] = []

        for m in _EDIT_RE.finditer(text):
            edits.append(CodeEdit(
                file_path=m.group("path").strip(),
                search=m.group("search"),
                replace=m.group("replace"),
                is_new_file=False,
            ))

        for m in _NEW_FILE_RE.finditer(text):
            edits.append(CodeEdit(
                file_path=m.group("path").strip(),
                search="",
                replace=m.group("content"),
                is_new_file=True,
            ))

        return edits

    # ------------------------------------------------------------------ #
    #  Prompt construction                                                 #
    # ------------------------------------------------------------------ #

    def _read_plan_files(self, plan: Plan) -> dict[str, str]:
        contents: dict[str, str] = {}
        for path in plan.files_to_modify:
            contents[path] = read_file(self.repo_path, path)
        # New files don't exist yet — skip
        return contents

    def _build_user_message(
        self,
        issue: Issue,
        plan: Plan,
        file_contents: dict[str, str],
        validation_error: ValidationResult | None,
        previous_edits: list[CodeEdit] | None,
    ) -> str:
        parts: list[str] = []

        if validation_error and previous_edits:
            parts += [
                "# Validation Failed — Corrective Edit Required",
                "",
                "The previous edits were applied but validation failed. "
                "Produce ONLY the corrective SEARCH/REPLACE block(s) needed to fix the errors below.",
                "",
                "## Validation Errors",
                "```",
                validation_error.error_summary,
                "```",
                "",
                "## Previous Edits Applied",
            ]
            for e in previous_edits:
                parts += [
                    f"GOFILE: {e.file_path}",
                    "<<<<<<< SEARCH",
                    e.search,
                    "=======",
                    e.replace,
                    ">>>>>>> REPLACE",
                    "",
                ]
        else:
            parts += [
                f"# Implement Fix for Issue #{issue.number}: {issue.title}",
                "",
                "## Plan",
                f"**Understanding:** {plan.understanding}",
                "",
                f"**Root cause:** {plan.root_cause}",
                "",
                f"**Approach:** {plan.approach}",
                "",
                f"**Test strategy:** {plan.test_strategy}",
                "",
            ]
            if plan.conventions_noted:
                parts += [
                    "**Conventions to follow:**",
                    *[f"  - {c}" for c in plan.conventions_noted],
                    "",
                ]

        parts += ["## File Contents", ""]
        for path, content in file_contents.items():
            parts += [
                f"### {path}",
                "```go",
                content,
                "```",
                "",
            ]

        parts += [
            "---",
            "",
            "Produce the SEARCH/REPLACE edit blocks now.",
            "Remember: SEARCH must match the file content exactly.",
        ]

        return "\n".join(parts)
