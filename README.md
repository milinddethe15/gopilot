# Gopilot

An agentic AI system that reads GitHub issues from open-source Go projects, explores the codebase, generates a fix, validates it with the Go toolchain, and writes a PR summary.

## Demo

Watch the agent autonomously resolve a real issue:

![Agent Demo](outputs/go-playground_validator_1543/demo.mp4)

- **Issue:** [go-playground/validator#1543](https://github.com/go-playground/validator/issues/1543)
- **Generated Patch**: [diff.patch](outputs/go-playground_validator_1543/diff.patch)
- **Output Artifacts:** Check out the [`outputs/go-playground_validator_1543/`](outputs/go-playground_validator_1543/) directory to see the exact planner outputs, tool calls, diffs, and validation logs generated during this run.

## Architecture

Each layer uses the best-fit tool rather than building from scratch.

```
┌──────────────────────────────────────────────────────────────────────┐
│  Layer           Tool                         Why                     │
├──────────────────────────────────────────────────────────────────────┤
│  Issue fetch     GitHub REST API              structured data, no CLI │
│  Repo clone      git                          standard, shallow clone  │
│  Repo map        go list -json + regex        Go-native package graph │
│  Code search     ripgrep (grep fallback)      fast, context-aware     │
│  Planning        LiteLLM (tool-call loop)     multi-step exploration  │
│  Code gen        LiteLLM (single call)        SEARCH/REPLACE blocks   │
│  Validation      go build + vet + test        ground truth            │
│  PR summary      LiteLLM (single call)        convention-aware prose  │
└──────────────────────────────────────────────────────────────────────┘
```

### Pipeline

```
issue number
     │
     ▼
github_client ──► fetch issue + comments + linked PRs
     │
     ▼
git clone ──► shallow clone to temp dir, create branch agent/issue-N
     │
     ▼
RepoMapper ──► go list -json ./... + per-file symbol extraction
               → repo_map.txt (~3–6 KB, fits in context)
     │
     ▼
Planner ──► tool-calling loop (up to 15 iterations)
            tools: read_file, search_code, list_directory, run_go_doc
            → plan.json + tool_calls.jsonl
     │
     ▼
Coder ──► single LLM call with full file contents + plan
          output: SEARCH/REPLACE blocks
          applied via exact-match (+ normalised-whitespace fallback)
     │
     ▼
Validator ──► go build → go vet → go test
              on failure: feed stderr back to Coder (up to 3 retries)
     │
     ▼
PRWriter ──► single LLM call with diff + issue + reference PRs
             → pr_summary.md
     │
     ▼
outputs/{owner}_{repo}_{issue}/
  ├── plan.json          planner's structured output
  ├── tool_calls.jsonl   every tool call during exploration
  ├── repo_map.txt       the compact symbol index
  ├── diff.patch         git diff of all changes
  ├── validation.log     go build/vet/test output
  └── pr_summary.md      PR title + body
```

## Prerequisites

- Python 3.11+
- Go 1.21+ (for `go build`, `go test`, `go list`)
- git
- ripgrep (`rg`) — optional but recommended for faster code search

## Setup

```bash
git clone https://github.com/milinddethe15/gopilot.git
cd gopilot
pip install -r requirements.txt
```

Set environment variables for your chosen provider.
LiteLLM reads the standard per-provider key automatically:

```bash
# Pick ONE of the following (matching your config.yaml model)
export OPENAI_API_KEY=sk-...
export ANTHROPIC_API_KEY=sk-ant-...
export GEMINI_API_KEY=AI...
export GROQ_API_KEY=gsk_...
export OPENROUTER_API_KEY=sk-or-...   # model: openrouter/<provider>/<model>

# Optional: point at a custom proxy or local Ollama
# export LLM_BASE_URL=http://localhost:11434

export GITHUB_TOKEN=ghp_...           # optional but avoids rate limits
```

## Usage

```bash
# Basic run
python main.py --issue 1234 --repo go-playground/validator

# Keep the cloned repo for inspection
python main.py --issue 1234 --repo go-playground/validator --keep-repo

# Skip go validation (faster when iterating on prompts)
python main.py --issue 1234 --repo go-playground/validator --skip-validation

# Full options
python main.py --help
```

Output artefacts are written to `outputs/{owner}_{repo}_{issue}/`.

## Design Decisions

**Why LiteLLM instead of a vendor SDK?**
LiteLLM provides a single OpenAI-compatible interface over 100+ providers
(OpenAI, Anthropic, Gemini, Groq, Ollama, OpenRouter, …). Swapping models
is a one-line change in `config.yaml` with no code changes required.

**Why SEARCH/REPLACE instead of unified diffs?**
Unified diffs require correct line numbers, which LLMs get wrong under pressure.
SEARCH/REPLACE blocks use exact-text matching — more robust when the model sees
the full file content.

**Why a validation retry loop?**
Go is strict. `go build` and `go vet` give precise, machine-readable errors.
Feeding them back to the coder catches ~80 % of first-attempt mistakes without
human intervention.

**Why a separate planner and coder?**
Single-responsibility: the planner uses tools to explore and produces a
structured JSON plan. The coder focuses only on producing correct edits for
known file contents. Separating them keeps each Claude call well-scoped.

## Configuration

Edit `config.yaml`:

```yaml
# LiteLLM model string: <provider>/<model-name>
model: openai/gpt-4o              # OpenAI
# model: anthropic/claude-opus-4-5  # Anthropic
# model: gemini/gemini-2.0-flash    # Google Gemini
# model: groq/llama3-70b-8192       # Groq
# model: ollama/llama3              # local Ollama
max_planning_tool_calls: 15   # max tool calls during exploration
max_validation_retries: 3     # max coder→validate loops
max_tokens: 8192
go_test_timeout: 120s
```

## Debugging

```bash
DEBUG=1 python main.py --issue 1234 --repo go-playground/validator
```

With `DEBUG=1`, full Python tracebacks are printed on error.

The `tool_calls.jsonl` file shows every tool call the planner made —
useful for understanding why certain files were (or weren't) identified.
