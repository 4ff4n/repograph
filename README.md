# RepoGraph

**Semantic code graph + AI security review for Python repositories.**

Your AI wrote the code — RepoGraph shows you what it actually did. It scans a
repo, turns every function, class, API endpoint and LLM call into a node in a
dependency graph, computes each node's *blast radius* (how much of the codebase
transitively depends on it), runs an AI security agent over the riskiest nodes,
and serves an interactive terminal-style UI you can explore.

It also diffs two git refs to show the **blast radius of a pull request** —
what a change reaches, what breaks if you remove something, and whether a PR
introduces risky new code — ready to post as a comment on every PR.

```
scanner (Python AST) ──► graph (NetworkX + SQLite) ──► agents (Fireworks LLM)
                                     │
                                     ▼
                     FastAPI server + interactive TUI  ·  git-diff blast radius
```

---

## Requirements

- Python 3.10 or newer
- git (only needed for the `diff` command)
- A [Fireworks AI](https://fireworks.ai) API key — **optional**, only for the AI
  security agent. Scanning, metrics, the UI and diffs all work without a key.
  Any OpenAI-compatible provider works too.

## Installation

```bash
# 1. Clone
git clone https://github.com/4ff4n/repograph.git
cd repograph

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install (editable, so code changes take effect immediately)
pip install -e .

# 4. Verify
repograph --help                   # should list: scan, diff, serve
```

To leave the virtual environment later, run `deactivate`. To come back to the
project in a new shell, `cd` into it and re-run `source .venv/bin/activate`.

### Configure the AI agent (optional)

```bash
cp .env.example .env
```

Edit `.env` and set your key:

```
FIREWORKS_API_KEY=fw-your-key-here
```

The `.env` file is read automatically from whatever directory you run
`repograph` in.

---

## Quickstart

```bash
# Scan any Python repo (no API key required)
repograph scan /path/to/your/repo

# Explore it in the interactive UI
repograph serve --db /path/to/your/repo
# then open http://127.0.0.1:8000

# Add AI security review of the riskiest nodes (needs a key)
repograph scan /path/to/your/repo --agents

# Blast radius of a change between two git refs
repograph diff /path/to/your/repo --base HEAD~1 --head HEAD
```

`--db` accepts the repo root, its `.repograph` folder, or the exact
`graph.db` file — all three resolve to the same database.

---

## Commands

### `repograph scan` — build the graph

```bash
repograph scan <path> [options]
```

| Flag | Default | Purpose |
| --- | --- | --- |
| `--agents` | off | Run the AI security agent over the riskiest nodes (needs a key) |
| `--model MODEL` | see config | Override the LLM model id for this run |
| `--max-nodes N` | 8 | How many top-risk nodes the agent reviews |
| `--db PATH` | `<repo>/.repograph/graph.db` | Where to write the database |

Scanning is fast and offline. It extracts entities and relationships, computes
metrics, and stores everything in a local SQLite database inside the repo's
`.repograph/` folder (which is git-ignored automatically).

### `repograph serve` — interactive UI

```bash
repograph serve --db <path> [--host HOST] [--port PORT]
```

Opens a terminal-style UI at `http://127.0.0.1:8000`. It is fully keyboard
driven (mouse works too):

| Key | Action |
| --- | --- |
| `j` / `k` or arrows | move within a pane |
| `1`–`4` | jump to Modules / Nodes / Inspector / Issues pane |
| `Enter` | drill into the selection |
| `g` | open / close the graph map overlay |
| `/` | search nodes across the whole repo |
| `r` `b` `n` | sort nodes by risk / blast radius / name |
| `Esc` | back out (closes the map, or returns to summary) |

The UI loads its graph library and fonts from a CDN, so the first load needs
internet. If port 8000 is taken, pass `--port 8080`.

### `repograph diff` — pull-request blast radius

```bash
repograph diff <path> --base <ref> [--head <ref>] [options]
```

Scans the base ref in an isolated git worktree, scans the head (defaulting to
your current working tree), and reports what changed and who is downstream of
it.

| Flag | Default | Purpose |
| --- | --- | --- |
| `--base REF` | *required* | Base git ref, e.g. `origin/main`, `HEAD~1` |
| `--head REF` | working tree | Head git ref to compare against |
| `--markdown PATH` | — | Write a PR-comment-ready markdown report |
| `--json PATH` | — | Write a machine-readable JSON report |
| `--fail-risk N` | off | Exit with code 1 if any changed or impacted node has risk ≥ N (CI gate) |

```bash
# vs your uncommitted working tree
repograph diff . --base origin/main

# between two commits
repograph diff . --base HEAD~1 --head HEAD

# as a CI gate that blocks risky merges
repograph diff . --base origin/main --head HEAD --fail-risk 60 --markdown impact.md
```

---

## What you get

- **Entities**: modules, functions, classes, API route handlers
  (FastAPI/Flask decorators), and detected LLM provider calls
  (OpenAI, Anthropic, Fireworks, LiteLLM, Gemini, and similar).
- **Relationships**: contains, imports, calls, inherits. Call resolution is
  static and conservative — it tracks local variable types
  (`x = SomeClass(...)` → `x.method()` resolves) but never invents an edge it
  cannot prove.
- **Metrics per node**: uses, used-by, blast radius, and a 0–100 heuristic
  risk score from AST-detected signals (network, subprocess, eval/exec, raw
  SQL, unsafe deserialization, secrets access, LLM calls) amplified by blast
  radius.
- **AI security agent**: reviews the top-risk nodes and stores structured
  issues — severity, title, fix guidance, and file:line.
- **Interactive TUI**: browse modules and nodes, inspect blast radius and
  issues, and pop open a graph map — all from the keyboard.
- **PR diffs**: change detection via content hashes, reverse dependency
  walk for impact, high-attention flagging, and a ready-made GitHub Action.

## GitHub Action

Copy `examples/github-workflow.yml` to `.github/workflows/repograph.yml` in
your repository. On every pull request it diffs the code graph against the base
branch and posts (or updates) a comment with the downstream impact. Uncomment
the `--fail-risk` line to turn it into a merge gate.

Remember to set the install source in that file to your repository URL.

---

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `FIREWORKS_API_KEY` | — | API key for the AI agent |
| `REPOGRAPH_MODEL` | `accounts/fireworks/models/kimi-k2p6` | Model id |
| `REPOGRAPH_BASE_URL` | `https://api.fireworks.ai/inference/v1` | Any OpenAI-compatible endpoint |
| `REPOGRAPH_API_KEY_ENV` | `FIREWORKS_API_KEY` | Name of the env var holding the key |
| `REPOGRAPH_DB` | `./.repograph/graph.db` | Default database path for `serve` |

To use a different provider (for example a local Ollama or OpenAI directly),
set `REPOGRAPH_BASE_URL`, `REPOGRAPH_MODEL`, and point `REPOGRAPH_API_KEY_ENV`
at whatever variable holds that provider's key.

Models on hosted providers get retired periodically. If the agent reports that
a model is not deployed, list what your key can use and pick one:

```bash
curl -s https://api.fireworks.ai/inference/v1/models \
  -H "Authorization: Bearer $FIREWORKS_API_KEY" | grep '"id"'
```

Then pass `--model <id>` or set `REPOGRAPH_MODEL` in `.env`.

---

## Try it on the bundled examples

### Sample repo

`examples/sample_repo` is a small AI ticket-triage service seeded with known
issues (SSRF, SQL injection, eval, subprocess, PII-to-LLM). Use it to see every
feature at once:

```bash
repograph scan examples/sample_repo --agents
repograph serve --db examples/sample_repo
```

Its own README lists exactly what the scanner and agent should find, so you can
confirm the tool is working.

### Demo repository with git history

`examples/make_demo_repo.py` generates a throwaway git repo whose history grows
the triage service commit by commit, plus three branches that simulate pull
requests:

```bash
python examples/make_demo_repo.py          # builds it in your temp dir
cd /tmp/repograph-demo

repograph diff . --base main --head pr/refactor-truncation          # modified hub -> 7 impacted
repograph diff . --base main --head pr/remove-resolve               # removal breaks an API route
repograph diff . --base main --head pr/add-webhook --fail-risk 40   # CI gate blocks it
```

`python examples/verify_demo.py` runs every scenario as assertions — use it as
a regression test after changing the scanner, metrics or diff engine.

---

## Project layout

```
repograph/
├── scanner/    # AST parsing -> entities + relations
├── graph/      # NetworkX metrics, SQLite store, diff engine
├── agents/     # LLM client + security review agent
└── server/     # FastAPI API + static terminal-style UI
examples/
├── sample_repo/          # seeded fixture with known issues
├── make_demo_repo.py     # builds a git repo with PR branches
├── verify_demo.py        # asserts the diff engine end to end
└── github-workflow.yml   # drop-in PR blast-radius Action
```

Each layer is independent on purpose — you can work on the scanner without
touching the UI, and vice versa.

## Honest limitations

- **Python only.** Call resolution is static and conservative. Dynamic dispatch,
  `getattr`, and decorators that rewrap functions are not followed; UI
  signal/slot bindings (e.g. Qt) are invisible, so blast radius on
  UI-triggered code is undercounted.
- **Scores are heuristics, not audits.** Risk and security are scored off the
  ten riskiest nodes (so a repo with three dangerous functions does not average
  out to a rosy 100). The AI agent refines security when enabled.
- **The graph errs toward missing edges, never invented ones.** A missing edge
  is safer than a wrong one, so some real relationships (dynamic ones) will not
  appear.

## Roadmap

- AI-authorship tagging (Claude Code / Copilot / Cursor commit signatures) to
  heat-map AI-written code against risk.
- Parallel agent reviews for faster scans on large repos.
- Natural-language graph queries and an MCP server mode so coding agents can
  consult the map before editing.
- Tree-sitter parsers for JavaScript / TypeScript, Go, and Java.

## License

MIT. Contributions welcome.
