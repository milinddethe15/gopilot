from __future__ import annotations
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import anthropic
from rich.console import Console

from models import Issue, Plan, ToolCall
from tools.code_search import CodeSearcher
from tools.file_ops import read_file, list_directory

console = Console()

# ---------------------------------------------------------------------------
#  Tool definitions (Anthropic tool-use format)
# ---------------------------------------------------------------------------

PLANNING_TOOLS: list[dict] = [
    {
        "name": "read_file",
        "description": (
            "Read the full content of a file in the repository. "
            "Always read a file before making decisions about it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from repo root (e.g. 'baked_in.go' or 'cmd/root.go')",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_code",
        "description": (
            "Search for a pattern across Go files using ripgrep. "
            "Use this to find function definitions, usages, and related code."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Literal string or regex pattern"},
                "file_glob": {
                    "type": "string",
                    "description": "File glob to limit search scope (default: '*.go')",
                    "default": "*.go",
                },
                "context_lines": {
                    "type": "integer",
                    "description": "Lines of context around each match (default: 3)",
                    "default": 3,
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files and subdirectories. Useful for understanding project layout.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to directory (default: '.' = repo root)",
                    "default": ".",
                }
            },
        },
    },
    {
        "name": "run_go_doc",
        "description": "Run `go doc <symbol>` to get documentation for a package or symbol.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Package path or symbol (e.g. 'fmt.Errorf' or './...')",
                }
            },
            "required": ["symbol"],
        },
    },
]

_PLAN_JSON_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


class Planner:
    """
    Runs a tool-calling loop with Claude until it emits a structured JSON plan.
    All tool calls are logged for the output artefacts.
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
        self.searcher = CodeSearcher(repo_path)
        self.tool_calls_log: list[ToolCall] = []

    def plan(self, issue: Issue, repo_map: str) -> Plan:
        user_message = self._build_user_message(issue, repo_map)
        messages: list[dict] = [{"role": "user", "content": user_message}]
        max_iterations = self.config.get("max_planning_tool_calls", 15)

        console.print("[bold cyan]Planner starting exploration loop…[/bold cyan]")

        for iteration in range(max_iterations):
            response = self.client.messages.create(
                model=self.config["model"],
                max_tokens=self.config.get("max_tokens", 8192),
                system=self.system_prompt,
                tools=PLANNING_TOOLS,
                messages=messages,
            )

            # Accumulate tool calls for this turn
            tool_results: list[dict] = []
            has_tool_use = False

            for block in response.content:
                if block.type == "text":
                    # Check if this text contains the final plan
                    plan = self._try_parse_plan(block.text)
                    if plan:
                        console.print(
                            f"[green]✓ Plan produced after {iteration + 1} iteration(s)[/green]"
                        )
                        return plan

                elif block.type == "tool_use":
                    has_tool_use = True
                    tool_output = self._execute_tool(block.name, block.input)
                    self.tool_calls_log.append(
                        ToolCall(name=block.name, input=block.input, output=tool_output)
                    )
                    console.print(
                        f"  [dim]→ {block.name}({self._fmt_input(block.input)})[/dim]"
                    )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": tool_output,
                    })

            # Append assistant turn + tool results for next iteration
            messages.append({"role": "assistant", "content": response.content})
            if tool_results:
                messages.append({"role": "user", "content": tool_results})

            if response.stop_reason == "end_turn" and not has_tool_use:
                # No more tool calls and no plan found — extract best-effort text
                break

        raise RuntimeError(
            f"Planner did not produce a valid JSON plan within {max_iterations} iterations. "
            "Check the logs for the last assistant response."
        )

    # ------------------------------------------------------------------ #
    #  Tool execution                                                      #
    # ------------------------------------------------------------------ #

    def _execute_tool(self, name: str, inputs: dict[str, Any]) -> str:
        if name == "read_file":
            return read_file(self.repo_path, inputs["path"])

        if name == "search_code":
            return self.searcher.search(
                pattern=inputs["pattern"],
                file_glob=inputs.get("file_glob", "*.go"),
                context_lines=int(inputs.get("context_lines", 3)),
            )

        if name == "list_directory":
            return list_directory(self.repo_path, inputs.get("path", "."))

        if name == "run_go_doc":
            result = subprocess.run(
                ["go", "doc", inputs["symbol"]],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=15,
            )
            return result.stdout or result.stderr or "(no output)"

        return f"Unknown tool: {name}"

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_user_message(issue: Issue, repo_map: str) -> str:
        parts = [
            f"## GitHub Issue #{issue.number}: {issue.title}",
            "",
            issue.body or "(no body)",
        ]
        if issue.comments:
            parts += ["", "### Comments"]
            for i, c in enumerate(issue.comments[:10], 1):
                parts.append(f"**Comment {i}:**\n{c}")
        if issue.labels:
            parts += ["", f"Labels: {', '.join(issue.labels)}"]
        parts += ["", "---", "", "## Repository Map", "", repo_map]
        parts += [
            "",
            "---",
            "",
            "Use the available tools to explore the codebase and then output your JSON plan.",
        ]
        return "\n".join(parts)

    @staticmethod
    def _try_parse_plan(text: str) -> Plan | None:
        m = _PLAN_JSON_RE.search(text)
        if not m:
            return None
        try:
            data = json.loads(m.group(1))
            return Plan(**data)
        except Exception:
            return None

    @staticmethod
    def _fmt_input(inp: dict) -> str:
        """Compact single-line representation of tool input for logging."""
        items = ", ".join(f"{k}={repr(v)[:60]}" for k, v in inp.items())
        return items
