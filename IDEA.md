# AI-Contributor

When provided with Go based Github repo and issue link, this agent can fix the issue and generate a commit to fix the issue.

## Layers

### Ingestion

Agent will git clone the repo and read the issue.

### Repo map

```
# Pass 1: Package graph
#   go list -json ./... → gives you packages, files, imports, test files

# Pass 2: Symbol index  
#   For each .go file: regex-extract func/type/const signatures (no full AST needed)
#   Result: {"biz_validator.go": ["func validateEmail(s string) bool", ...]}

# Pass 3: Convention sniff
#   Read CONTRIBUTING.md, look at existing test file patterns,
#   find _test.go naming conventions, find how errors are returned
```

**Output**: A compact repo_map.txt (~2-4KB), injected into every LLM call.

### Planner

Here agent design thinking. Runs tool-calling loops.

```
PLANNING_TOOLS = [
    read_file(path),          # Read full file content
    search_code(pattern),     # ripgrep across repo
    list_directory(path),     # ls with .go filter
    run_go_doc(symbol),       # `go doc pkg.Symbol`
]

# Loop: Claude calls tools until it emits a structured plan
# Typically 3-6 tool calls before it has enough context
```
The planner outputs a structured JSON plan:
```json
{
  "understanding": "The issue is about X validation tag not handling Y edge case",
  "files_to_modify": ["baked_in.go", "baked_in_test.go"],
  "approach": "Add case Y to the existing switch in validateX()",
  "test_strategy": "Add table row with Y input in TestValidateX",
  "go_test_targets": ["./..."]
}
```

### Code generation

Single focused call — not a loop. Inputs: full file content + plan + repo conventions. Output format: unified diff (not full file replacement). This is cleaner, easier to review, and won't accidentally destroy surrounding code.

### Validator

The feedback loop is what separates an agent from a script:
```python
for attempt in range(MAX_RETRIES):  # max 3
    result = run_go_tools(repo_path, plan["go_test_targets"])
    # go build ./... → go vet ./... → go test <targets>
    
    if result.success:
        break
    
    # Feed stderr back to coder for a targeted fix
    fix_diff = coder.fix(original_diff, result.stderr, attempt)
    apply_diff(fix_diff)
```

### PR Writer

One focused Claude call. Inputs: git diff + issue body + accepted PRs from same repo (for style). Output: PR title following conventional commits (fix:, feat:, etc.) + structured body (Summary, Changes, Testing).

## orchestrator.py — the main pipeline

```python
def run(issue_number: int, repo: str):
    # 1. Clone repo to temp dir, create branch: agent/issue-{number}
    # 2. Fetch issue + any existing PR (for convention reference)
    # 3. Build repo map
    # 4. Run planner (tool-calling loop)
    # 5. Run coder (single call, produces diff)
    # 6. Apply diff
    # 7. Validation loop (up to 3 retries with feedback)
    # 8. git commit + generate PR summary
    # 9. Write outputs/ artifacts
```

Observability— every run saves to outputs/{issue}/:

plan.json — what the agent decided to do
tool_calls.jsonl — every tool call in the planning phase
diff.patch — the actual code change
validation.log — go build/test output
pr_summary.md — the final PR body

## Project structure

```
go-contributor/
├── main.py                  # CLI entrypoint: python main.py --issue 1234 --repo go-playground/validator
├── config.yaml              # LLM model, retry limits, timeouts
│
├── agent/
│   ├── orchestrator.py      # Main pipeline: ingest → plan → code → validate → pr
│   ├── planner.py           # Claude tool-calling loop for exploration + planning
│   ├── coder.py             # Claude call to generate diffs / file replacements
│   ├── validator.py         # Subprocess: go build / vet / test with feedback capture
│   └── pr_writer.py         # Claude call to generate PR title + body
│
├── tools/
│   ├── github_client.py     # gh CLI wrapper: fetch issue, comments, linked PRs
│   ├── repo_map.py          # Go-aware: go list -json + function sig extraction
│   ├── file_ops.py          # read_file, write_file, apply_diff
│   └── code_search.py       # ripgrep wrapper: symbol search, pattern search
│
├── prompts/
│   ├── planner_system.md    # Role + tool descriptions + planning output schema
│   ├── coder_system.md      # Diff format instructions + Go conventions
│   └── pr_writer_system.md  # PR body format, references issue, conventional commits
│
└── outputs/                 # Per-run: plan.json, diff.patch, pr_summary.md, logs/
```
