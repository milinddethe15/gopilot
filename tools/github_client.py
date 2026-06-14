from __future__ import annotations
import os
import subprocess
from pathlib import Path

import requests
from rich.console import Console

from models import Issue, LinkedPR

console = Console()


class GitHubError(Exception):
    pass


class GitHubClient:
    BASE = "https://api.github.com"

    def __init__(self, token: str | None = None):
        self.token = token or os.environ.get("GITHUB_TOKEN", "")
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"

    # ------------------------------------------------------------------ #
    #  Issue fetching                                                       #
    # ------------------------------------------------------------------ #

    def get_issue(self, owner: str, repo: str, number: int) -> Issue:
        url = f"{self.BASE}/repos/{owner}/{repo}/issues/{number}"
        resp = self._get(url)
        data = resp.json()

        comments = self._get_comments(owner, repo, number)
        labels = [lbl["name"] for lbl in data.get("labels", [])]

        return Issue(
            number=number,
            title=data["title"],
            body=data.get("body") or "",
            comments=comments,
            labels=labels,
        )

    def _get_comments(self, owner: str, repo: str, number: int) -> list[str]:
        url = f"{self.BASE}/repos/{owner}/{repo}/issues/{number}/comments"
        resp = self._get(url, params={"per_page": 100})
        return [c["body"] for c in resp.json() if c.get("body")]

    # ------------------------------------------------------------------ #
    #  Linked PR discovery (convention reference)                          #
    # ------------------------------------------------------------------ #

    def find_linked_prs(self, owner: str, repo: str, issue_number: int) -> list[LinkedPR]:
        """
        Find closed/merged PRs that mention this issue in their body or title.
        Used as style reference when writing the PR summary.
        """
        query = f"repo:{owner}/{repo} is:pr is:merged #{issue_number} in:body"
        url = f"{self.BASE}/search/issues"
        try:
            resp = self._get(url, params={"q": query, "per_page": 5})
            results = []
            for item in resp.json().get("items", []):
                results.append(LinkedPR(
                    number=item["number"],
                    title=item["title"],
                    body=item.get("body") or "",
                    merged=True,
                ))
            return results
        except GitHubError:
            return []

    # ------------------------------------------------------------------ #
    #  Repository cloning                                                  #
    # ------------------------------------------------------------------ #

    def clone_repo(self, owner: str, repo: str, target_dir: Path, depth: int = 1) -> Path:
        """Shallow-clone the repo into target_dir/{repo}."""
        clone_url = f"https://github.com/{owner}/{repo}.git"
        dest = target_dir / repo
        if dest.exists():
            console.print(f"[dim]Repo already exists at {dest}, skipping clone[/dim]")
            return dest

        console.print(f"[cyan]Cloning {owner}/{repo}…[/cyan]")
        result = subprocess.run(
            ["git", "clone", "--depth", str(depth), clone_url, str(dest)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise GitHubError(f"git clone failed:\n{result.stderr}")
        return dest

    def create_branch(self, repo_path: Path, branch_name: str) -> None:
        self._git(repo_path, ["checkout", "-b", branch_name])

    # ------------------------------------------------------------------ #
    #  Internals                                                           #
    # ------------------------------------------------------------------ #

    def _get(self, url: str, params: dict | None = None) -> requests.Response:
        resp = self.session.get(url, params=params, timeout=30)
        if resp.status_code == 401:
            raise GitHubError("GitHub auth failed — set GITHUB_TOKEN")
        if resp.status_code == 403:
            raise GitHubError("GitHub rate limit hit — set GITHUB_TOKEN for higher limits")
        if resp.status_code == 404:
            raise GitHubError(f"Not found: {url}")
        resp.raise_for_status()
        return resp

    @staticmethod
    def _git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise GitHubError(f"git {' '.join(args)} failed:\n{result.stderr}")
        return result
