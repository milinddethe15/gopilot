from __future__ import annotations
from pydantic import BaseModel, Field


class Issue(BaseModel):
    number: int
    title: str
    body: str
    comments: list[str] = []
    labels: list[str] = []


class LinkedPR(BaseModel):
    number: int
    title: str
    body: str
    merged: bool = False


class Plan(BaseModel):
    understanding: str
    root_cause: str
    files_to_modify: list[str]
    new_files: list[str] = []
    approach: str
    test_strategy: str
    go_test_targets: list[str] = Field(default_factory=lambda: ["./..."])
    conventions_noted: list[str] = []


class CodeEdit(BaseModel):
    file_path: str
    search: str          # exact text to find (empty string = new file)
    replace: str         # replacement text (or full content for new files)
    is_new_file: bool = False


class ValidationResult(BaseModel):
    success: bool
    build_output: str = ""
    vet_output: str = ""
    test_output: str = ""

    @property
    def error_summary(self) -> str:
        parts = []
        if self.build_output and ("FAIL" in self.build_output or "Error" in self.build_output):
            parts.append(f"Build errors:\n{self.build_output[-3000:]}")
        if self.vet_output and self.vet_output.strip():
            parts.append(f"Vet errors:\n{self.vet_output[-1500:]}")
        if self.test_output and "FAIL" in self.test_output:
            parts.append(f"Test failures:\n{self.test_output[-3000:]}")
        return "\n\n".join(parts) if parts else "Unknown error — check logs"


class ToolCall(BaseModel):
    name: str
    input: dict
    output: str


class AgentRun(BaseModel):
    issue: Issue
    plan: Plan | None = None
    tool_calls: list[ToolCall] = []
    edits: list[CodeEdit] = []
    diff: str = ""
    validation_attempts: int = 0
    validation_success: bool = False
    pr_title: str = ""
    pr_body: str = ""
