#!/usr/bin/env python3
"""
go-contributor: Agentic AI contributor for open-source Go projects.

Usage:
    python main.py --issue 1234 --repo go-playground/validator
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

import click
import yaml
from rich.console import Console

console = Console()


def load_config(config_path: Path) -> dict:
    if not config_path.exists():
        console.print(f"[red]Config not found: {config_path}[/red]")
        sys.exit(1)
    with open(config_path) as f:
        return yaml.safe_load(f)


@click.command()
@click.option("--issue", "-i", required=True, type=int, help="GitHub issue number")
@click.option(
    "--repo", "-r", required=True,
    help="GitHub repo in owner/name format (e.g. go-playground/validator)",
)
@click.option(
    "--output-dir", default="outputs", show_default=True,
    help="Directory for run artefacts",
)
@click.option(
    "--config", "config_path", default="config.yaml", show_default=True,
    help="Path to config.yaml",
)
@click.option(
    "--keep-repo", is_flag=True, default=False,
    help="Don't delete the cloned repo after the run (useful for debugging)",
)
@click.option(
    "--skip-validation", is_flag=True, default=False,
    help="Skip go build/vet/test (faster iteration when debugging prompts)",
)
def main(
    issue: int,
    repo: str,
    output_dir: str,
    config_path: str,
    keep_repo: bool,
    skip_validation: bool,
) -> None:
    # ---- Validate repo format ----
    if "/" not in repo:
        console.print("[red]--repo must be in owner/name format (e.g. go-playground/validator)[/red]")
        sys.exit(1)
    owner, repo_name = repo.split("/", 1)

    # ---- LLM configuration ----
    llm_api_key = os.environ.get("LLM_API_KEY", None)
    llm_base_url = os.environ.get("LLM_BASE_URL", None)

    github_token = os.environ.get("GITHUB_TOKEN", "")
    if not github_token:
        console.print(
            "[yellow]GITHUB_TOKEN not set — GitHub API rate limits will apply (60 req/hr)[/yellow]"
        )

    # ---- Load config ----
    config = load_config(Path(config_path))
    if skip_validation:
        config["max_validation_retries"] = 0
        console.print("[yellow]Skipping validation (--skip-validation)[/yellow]")

    # ---- Run ----
    # Import here so startup errors surface cleanly before heavy imports
    from agent.orchestrator import Orchestrator

    orchestrator = Orchestrator(
        llm_api_key=llm_api_key,
        llm_base_url=llm_base_url,
        github_token=github_token or None,
        config=config,
    )

    try:
        run = orchestrator.run(
            owner=owner,
            repo=repo_name,
            issue_number=issue,
            output_dir=Path(output_dir),
            keep_repo=keep_repo,
        )
        # Print PR summary to stdout for quick review
        console.print(f"\n[bold]PR Title:[/bold] {run.pr_title}")
        console.print(f"\n[bold]PR Body:[/bold]\n{run.pr_body}")

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted[/yellow]")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n[red bold]Error:[/red bold] {e}")
        if os.environ.get("DEBUG"):
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
