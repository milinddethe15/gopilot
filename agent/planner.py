from __future__ import annotations
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import litellm
from rich.console import Console

from models import Issue, Plan, ToolCall
from tools.code_search import CodeSearcher
from tools.file_ops import read_file, list_directory

console = Console()

# ---------------------------------------------------------------------------
#  Tool definitions (OpenAI function-calling format, compatible with LiteLLM)
# ---------------------------------------------------------------------------

PLANNING_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the full content of a file in the repository. "
                "Always read a file before making decisions about it."
            ),
            "parameters": {
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
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": (
                "Search for a pattern across Go files using ripgrep. "
                "Use this to find function definitions, usages, and related code."
            ),
            "parameters": {
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
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and subdirectories. Useful for understanding project layout.",
            "parameters": {
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
    },
    {
        "type": "function",
        "function": {
            "name": "run_go_doc",
            "description": "Run `go doc <symbol>` to get documentation for a package or symbol.",
            "parameters": {
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
    },
]

_PLAN_JSON_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


class Planner:
    """
    Runs a tool-calling loop with the configured LLM until it
    emits a structured JSON plan.  All tool calls are logged for the output
    artefacts.
    """

    def __init__(
        self,
        api_key: str | None,
        base_url: str | None,
        config: dict,
        repo_path: Path,
        system_prompt: str,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.config = config
        self.repo_path = repo_path
        self.system_prompt = system_prompt
        self.searcher = CodeSearcher(repo_path)
        self.tool_calls_log: list[ToolCall] = []

    def plan(self, issue: Issue, repo_map: str) -> Plan:
        user_message = self._build_user_message(issue, repo_map)
        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_message},
        ]
        max_iterations = self.config.get("max_planning_tool_calls", 15)

        console.print("[bold cyan]Planner starting exploration loop…[/bold cyan]")

        for iteration in range(max_iterations):
            call_kwargs: dict[str, Any] = dict(
                model=self.config["model"],
                max_tokens=self.config.get("max_tokens", 8192),
                tools=PLANNING_TOOLS,
                messages=messages,
            )
            if self.api_key:
                call_kwargs["api_key"] = self.api_key
            if self.base_url:
                call_kwargs["base_url"] = self.base_url

            response = litellm.completion(**call_kwargs)
            msg = response.choices[0].message
            finish_reason = response.choices[0].finish_reason

            # ---- Check text content for a final JSON plan ----
            text_content: str = msg.content or ""
            if text_content:
                plan = self._try_parse_plan(text_content)
                if plan:
                    console.print(
                        f"[green]✓ Plan produced after {iteration + 1} iteration(s)[/green]"
                    )
                    return plan

            # ---- Handle tool calls ----
            tool_calls = msg.tool_calls or []
            if tool_calls:
                # Append the assistant turn (preserving tool_calls metadata)
                messages.append({
                    "role": "assistant",
                    "content": text_content or None,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in tool_calls
                    ],
                })

                # Execute each tool and add results
                for tc in tool_calls:
                    name = tc.function.name
                    try:
                        inputs = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        inputs = {}

                    tool_output = self._execute_tool(name, inputs)
                    self.tool_calls_log.append(
                        ToolCall(name=name, input=inputs, output=tool_output)
                    )
                    console.print(
                        f"  [dim]→ {name}({self._fmt_input(inputs)})[/dim]"
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tool_output,
                    })
                continue  # Next iteration with tool results in context

            # ---- No tool calls and no plan found — model finished ----
            if finish_reason == "stop":
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
