from __future__ import annotations
import anthropic
from rich.console import Console

from models import Issue, Plan, LinkedPR

console = Console()

_TITLE_RE = __import__("re").compile(r"##\s*TITLE\s*\n(.+?)(?:\n|$)", __import__("re").IGNORECASE)
_BODY_RE = __import__("re").compile(r"##\s*BODY\s*\n(.*)", __import__("re").DOTALL | __import__("re").IGNORECASE)


class PRWriter:
    def __init__(
        self,
        client: anthropic.Anthropic,
        config: dict,
        system_prompt: str,
    ):
        self.client = client
        self.config = config
        self.system_prompt = system_prompt

    def write(
        self,
        issue: Issue,
        plan: Plan,
        diff: str,
        linked_prs: list[LinkedPR] | None = None,
    ) -> tuple[str, str]:
        """Returns (title, body)."""
        user_message = self._build_message(issue, plan, diff, linked_prs or [])

        console.print("[bold cyan]PR writer generating summary…[/bold cyan]")

        response = self.client.messages.create(
            model=self.config["model"],
            max_tokens=2048,
            system=self.system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )

        raw = "".join(b.text for b in response.content if b.type == "text")
        title, body = self._parse(raw, issue.number)
        console.print(f"[green]✓ PR title: {title!r}[/green]")
        return title, body

    # ------------------------------------------------------------------ #

    def _build_message(
        self, issue: Issue, plan: Plan, diff: str, linked_prs: list[LinkedPR]
    ) -> str:
        parts = [
            f"## Issue #{issue.number}: {issue.title}",
            "",
            issue.body or "(no body)",
            "",
            "## Implementation Plan",
            f"**Understanding:** {plan.understanding}",
            f"**Approach:** {plan.approach}",
            f"**Test strategy:** {plan.test_strategy}",
            "",
            "## Git Diff",
            "```diff",
            diff[:6000] if len(diff) > 6000 else diff,
            "```",
        ]

        if linked_prs:
            parts += ["", "## Reference PRs (style guide)"]
            for pr in linked_prs[:3]:
                parts += [f"### PR #{pr.number}: {pr.title}", pr.body[:500], ""]

        parts += ["", "---", "Output the TITLE and BODY sections now."]
        return "\n".join(parts)

    @staticmethod
    def _parse(raw: str, issue_number: int) -> tuple[str, str]:
        title_m = _TITLE_RE.search(raw)
        body_m = _BODY_RE.search(raw)

        title = title_m.group(1).strip() if title_m else f"fix: resolve issue #{issue_number}"
        body = body_m.group(1).strip() if body_m else raw.strip()

        # Ensure issue reference exists
        if f"#{issue_number}" not in body:
            body += f"\n\nCloses #{issue_number}"

        return title, body
