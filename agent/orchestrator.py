from __future__ import annotations
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import anthropic
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule

from agent.planner import Planner
from agent.coder import Coder
from agent.validator import Validator
from agent.pr_writer import PRWriter
from models import AgentRun, Issue, Plan, CodeEdit
from tools.github_client import GitHubClient
from tools.file_ops import get_git_diff, commit_changes, EditApplicationError
from tools.repo_map import RepoMapper

console = Console()


class Orchestrator:
    """
    Main pipeline:
      1. Fetch issue + clone repo
      2. Build repo map
      3. Plan (tool-calling loop)
      4. Generate + apply edits
      5. Validate (with retry loop)
      6. Write PR summary
      7. Save all artefacts
    """

    def __init__(
        self,
        anthropic_api_key: str,
        anthropic_auth_key: str,
        anthropic_base_url: str,
        github_token: str | None,
        config: dict,
    ):
        self.claude = anthropic.Anthropic(api_key=anthropic_api_key, auth_token=anthropic_auth_key, base_url=anthropic_base_url)
        self.github = GitHubClient(token=github_token)
        self.config = config
        self.prompts = self._load_prompts()

    def run(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        output_dir: Path,
        keep_repo: bool = False,
    ) -> AgentRun:
        output_dir.mkdir(parents=True, exist_ok=True)
        run_dir = output_dir / f"{owner}_{repo}_{issue_number}"
        run_dir.mkdir(parents=True, exist_ok=True)

        console.print(Rule(f"[bold]Issue #{issue_number} · {owner}/{repo}[/bold]"))

        # ---------------------------------------------------------------- #
        # 1. Fetch issue                                                    #
        # ---------------------------------------------------------------- #
        console.print("\n[bold]Step 1/6 — Fetching issue[/bold]")
        issue = self.github.get_issue(owner, repo, issue_number)
        linked_prs = self.github.find_linked_prs(owner, repo, issue_number)
        console.print(
            Panel(
                f"[bold]{issue.title}[/bold]\n\n{(issue.body or '')[:400]}…",
                title=f"Issue #{issue.number}",
                border_style="blue",
            )
        )

        # ---------------------------------------------------------------- #
        # 2. Clone repo + create branch                                     #
        # ---------------------------------------------------------------- #
        console.print("\n[bold]Step 2/6 — Cloning repository[/bold]")
        work_dir = Path(tempfile.mkdtemp(prefix="go-contributor-"))
        try:
            repo_path = self.github.clone_repo(owner, repo, work_dir)
            branch = f"agent/issue-{issue_number}"
            subprocess.run(
                ["git", "checkout", "-b", branch],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )

            # ------------------------------------------------------------ #
            # 3. Build repo map                                             #
            # ------------------------------------------------------------ #
            console.print("\n[bold]Step 3/6 — Building repo map[/bold]")
            repo_map = RepoMapper(repo_path).build()
            (run_dir / "repo_map.txt").write_text(repo_map)
            console.print(f"  Repo map: {len(repo_map)} chars")

            # ------------------------------------------------------------ #
            # 4. Plan                                                       #
            # ------------------------------------------------------------ #
            console.print("\n[bold]Step 4/6 — Planning[/bold]")
            planner = Planner(
                self.claude, self.config, repo_path, self.prompts["planner"]
            )
            plan = planner.plan(issue, repo_map)
            (run_dir / "plan.json").write_text(plan.model_dump_json(indent=2))
            (run_dir / "tool_calls.jsonl").write_text(
                "\n".join(tc.model_dump_json() for tc in planner.tool_calls_log)
            )
            console.print(
                Panel(
                    f"[bold]Files to modify:[/bold] {', '.join(plan.files_to_modify)}\n\n"
                    f"[bold]Approach:[/bold] {plan.approach[:300]}",
                    title="Plan",
                    border_style="cyan",
                )
            )

            # ------------------------------------------------------------ #
            # 5. Code → Validate loop                                       #
            # ------------------------------------------------------------ #
            console.print("\n[bold]Step 5/6 — Coding + Validation[/bold]")
            coder = Coder(
                self.claude, self.config, repo_path, self.prompts["coder"]
            )
            validator = Validator(
                repo_path,
                timeout=int(str(self.config.get("go_test_timeout", "120s")).rstrip("s")),
            )

            all_edits: list[CodeEdit] = []
            validation_result = None
            max_retries = self.config.get("max_validation_retries", 3)

            for attempt in range(max_retries + 1):
                if attempt > 0:
                    console.print(f"\n[yellow]↺ Retry {attempt}/{max_retries}[/yellow]")

                edits = coder.generate_edits(
                    issue=issue,
                    plan=plan,
                    validation_error=validation_result if attempt > 0 else None,
                    previous_edits=all_edits if attempt > 0 else None,
                )

                if not edits:
                    console.print("[red]✗ Coder produced no edits — aborting[/red]")
                    break

                # On retry, reset to HEAD before re-applying (clean slate)
                if attempt > 0:
                    subprocess.run(
                        ["git", "checkout", "HEAD", "--", "."],
                        cwd=repo_path,
                        capture_output=True,
                    )

                try:
                    coder.apply_edits(edits)
                    all_edits = edits
                except EditApplicationError as e:
                    console.print(f"[red]✗ Edit application failed: {e}[/red]")
                    # Feed the error back as if it were a validation failure
                    from models import ValidationResult
                    validation_result = ValidationResult(
                        success=False,
                        build_output=str(e),
                    )
                    continue

                validation_result = validator.validate(plan.go_test_targets)

                if validation_result.success:
                    console.print(f"[green bold]✓ Validation passed on attempt {attempt + 1}[/green bold]")
                    break

                if attempt == max_retries:
                    console.print(
                        f"[red]✗ Validation still failing after {max_retries + 1} attempts[/red]"
                    )

            # Save validation log
            if validation_result:
                (run_dir / "validation.log").write_text(
                    f"success: {validation_result.success}\n\n"
                    f"--- build ---\n{validation_result.build_output}\n\n"
                    f"--- vet ---\n{validation_result.vet_output}\n\n"
                    f"--- test ---\n{validation_result.test_output}"
                )

            # ------------------------------------------------------------ #
            # 6. Commit + PR summary                                        #
            # ------------------------------------------------------------ #
            console.print("\n[bold]Step 6/6 — Generating PR summary[/bold]")
            diff = get_git_diff(repo_path)
            (run_dir / "diff.patch").write_text(diff)

            if diff.strip():
                commit_changes(repo_path, f"agent: fix issue #{issue_number}")
            else:
                console.print("[yellow]⚠ No changes to commit[/yellow]")

            pr_writer = PRWriter(self.claude, self.config, self.prompts["pr_writer"])
            pr_title, pr_body = pr_writer.write(issue, plan, diff, linked_prs)

            pr_summary = f"# {pr_title}\n\n{pr_body}"
            (run_dir / "pr_summary.md").write_text(pr_summary)

            # ------------------------------------------------------------ #
            # Build and return the run record                               #
            # ------------------------------------------------------------ #
            agent_run = AgentRun(
                issue=issue,
                plan=plan,
                tool_calls=planner.tool_calls_log,
                edits=all_edits,
                diff=diff,
                validation_attempts=max_retries + 1,
                validation_success=validation_result.success if validation_result else False,
                pr_title=pr_title,
                pr_body=pr_body,
            )
            (run_dir / "run.json").write_text(agent_run.model_dump_json(indent=2))

            console.print(Rule("[bold green]Run complete[/bold green]"))
            console.print(f"\nArtefacts saved to: [bold]{run_dir}[/bold]")
            console.print(f"  plan.json · tool_calls.jsonl · diff.patch · validation.log · pr_summary.md")

            return agent_run

        finally:
            if not keep_repo:
                shutil.rmtree(work_dir, ignore_errors=True)

    # ------------------------------------------------------------------ #

    def _load_prompts(self) -> dict[str, str]:
        prompts_dir = Path(__file__).parent.parent / "prompts"
        return {
            "planner": (prompts_dir / "planner_system.md").read_text(),
            "coder": (prompts_dir / "coder_system.md").read_text(),
            "pr_writer": (prompts_dir / "pr_writer_system.md").read_text(),
        }
