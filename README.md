# RepoGraph

**A semantic map of your codebase, and the blast radius of every change.**

AI writes more of our code every month. It ships fast, and it never tells you
what it touched. RepoGraph reads a Python repository, turns every function,
class, API route and LLM call into a node in a dependency graph, and answers
the question that usually costs you an afternoon of grepping:

> *If I change this, what breaks?*

It runs as a CLI, a terminal-style web UI, and a pull-request check.

```
scanner (AST)  ──►  graph (NetworkX + SQLite)  ──►  agent (LLM review)
                            │
                ┌───────────┴───────────┐
                ▼                       ▼
        diff / PR impact          terminal-style UI
```

---

## Install

Requires Python 3.10+.

```bash
git clone https://github.com/4ff4n/repograph.git
cd repograph
pip install -e .
```

## Quickstart

```bash
# 1. Map any Python repo — no API key needed
repograph scan /path/to/repo

# 2. Explore it
repograph serve --db /path/to/repo
# → http://127.0.0.1:8000

# 3. See what a change reaches
repograph diff /path/to/repo --base origin/main

# 4. Optional: AI security review of the riskiest nodes
cp .env.example .env          # add your FIREWORKS_API_KEY
repograph scan /path/to/repo --agents
```

---

## What it does

### Maps the code

Every module, function, class, FastAPI/Flask route and LLM provider call
becomes a node. Every import, call and inheritance becomes an edge. Call
resolution is static and deliberately conservative — it tracks imports, local
variable types (`svc = Service()` → `svc.method()` resolves) and class
hierarchies, and declines to guess when it can't be sure. A missing edge is
better than an invented one.

### Scores what matters

Each node gets:

| metric | meaning |
| --- | --- |
| **blast radius** | how many nodes transitively depend on it — the reach of a failure |
| **uses / used by** | direct call edges in and out |
| **risk 0–100** | static signals (network, subprocess, `eval`, raw SQL, secrets, LLM calls), amplified by blast radius |

Risk signals are detected from the AST, not by scanning text, so a method named
`_execute` is not mistaken for `exec`.

The insight is the multiplication: a dangerous function nothing depends on is a
footnote; the same code behind forty callers is a real problem.

### Reviews the dangerous parts

With `--agents`, an LLM reviews the highest-risk nodes and writes structured
findings — severity, what's wrong, how to fix it, file and line. The graph
decides where to look, so the review stays cheap: a few cents per repo instead
of feeding it every file.

Defaults to Fireworks AI; any OpenAI-compatible provider works.

### Reports impact on every pull request

`repograph diff` scans two git refs, detects added, removed and modified nodes
by content hash, and walks the dependency edges backwards to find everyone
affected. Each impacted node reports which change reaches it and in how many
hops. Removed functions report who *used to* depend on them — the callers that
are now broken.

```
╭───────────────── diff 493aadf → efdd18b ──────────────────╮
│ 3 modified · 1 added · 0 removed  →  blast radius 7 nodes │
╰───────────────────────────────────────────────────────────╯
impacted downstream (7)
  node               kind        risk    via              hops
  import_from_url    api           30    process             1
  create_ticket      api           15    process             1
  main               function       0    TriageService       1
  ⚠ 2 high-attention node(s) (exposed API/LLM surface or risk ≥ 30)
```

Outputs a rich terminal report, `--markdown` for PR comments, `--json` for
tooling, and `--fail-risk N` to exit non-zero as a CI gate.

---

## The UI

`repograph serve` opens a terminal-style interface — miller columns, ASCII
meters, fully keyboard-driven.

```
┌ [1] MODULES ─┬─ [2] NODES ──────────────────────┬─ [3] INSPECTOR ─┐
│ triage.api   │ api  import_from_url ███░░ 30  4 │ blast radius    │
│ triage.core  │ fn   download_model  ██░░░ 22  7 │ ████████░░ 12   │
│ …            │ …                                │ risk, edges,    │
├──────────────┴──── [4] ISSUES ───────────────────┤ issues, source  │
│ HIGH  SSRF via unvalidated URL   api.py:45       │                 │
└──────────────────────────────────────────────────┴─────────────────┘
```

| key | action |
| --- | --- |
| `j` / `k` | move | 
| `1`–`4` | switch pane |
| `Enter` | open selection |
| `g` | graph map overlay |
| `/` | search all nodes |
| `r` `b` `n` | sort by risk / blast / name |
| `Esc` | back |

---

## Pull request checks

Copy `examples/github-workflow.yml` to `.github/workflows/repograph.yml`. Every
PR gets a comment with the impact of the change, updated on each push.

```yaml
- name: Compute blast radius
  run: |
    repograph diff . \
      --base "origin/${{ github.base_ref }}" \
      --head HEAD \
      --markdown impact.md
    # --fail-risk 60      # uncomment to block risky merges
```

---

## Try it without your own repo

`examples/sample_repo` is a small AI ticket-triage service seeded with known
problems — SSRF, SQL injection, `eval`, `subprocess`, and raw user text
forwarded to an LLM. Its README lists exactly what should be found, so it
doubles as a fixture.

```bash
repograph scan examples/sample_repo --agents
repograph serve --db examples/sample_repo
```

`examples/make_demo_repo.py` generates a throwaway git repository with real
history plus three branches that simulate pull requests:

```bash
python examples/make_demo_repo.py
cd /tmp/repograph-demo

repograph diff . --base main --head pr/refactor-truncation        # modified hub → 7 impacted
repograph diff . --base main --head pr/remove-resolve             # removal breaks an API route
repograph diff . --base main --head pr/add-webhook --fail-risk 40 # CI gate blocks it
```

`python examples/verify_demo.py` runs all of it as assertions — the regression
suite for the scanner and diff engine.

---

## Configuration

| variable | default | purpose |
| --- | --- | --- |
| `FIREWORKS_API_KEY` | — | API key for the review agent |
| `REPOGRAPH_MODEL` | `accounts/fireworks/models/kimi-k2p6` | model id |
| `REPOGRAPH_BASE_URL` | `https://api.fireworks.ai/inference/v1` | any OpenAI-compatible endpoint |
| `REPOGRAPH_API_KEY_ENV` | `FIREWORKS_API_KEY` | name of the env var holding the key |

Serverless providers retire model deployments regularly. If yours has gone
away, RepoGraph tells you how to list the ones your account can use.

### Commands

```
repograph scan  <path> [--agents] [--model M] [--max-nodes N] [--db PATH]
repograph diff  <path> --base REF [--head REF] [--markdown F] [--json F] [--fail-risk N]
repograph serve [--db PATH] [--host H] [--port P]
```

`--db` accepts a repository root, a `.repograph` directory, or the database
file itself.

---

## Project layout

```
repograph/
├── scanner/    # AST parsing → entities + relations
├── graph/      # NetworkX metrics, SQLite store, diff engine
├── agents/     # LLM client + security review agent
├── server/     # FastAPI API + terminal-style UI
└── cli.py      # scan / diff / serve
examples/       # sample repo, demo history generator, GitHub Action
```

Each layer is independent — you can contribute to one without touching the
others.

---

## Honest limitations

- **Python only.** Tree-sitter parsers for other languages are on the roadmap.
- **Static resolution.** Dynamic dispatch, `getattr`, decorators that rewrap
  functions, and Qt-style signal/slot bindings produce no edges, so blast
  radius under-counts in UI-heavy code. The edges that exist are trustworthy;
  the graph errs toward missing, never inventing.
- **Scores are heuristics, not audits.** Governance is documentation coverage.
  Risk and security score off the ten riskiest nodes, not the repo-wide mean,
  so a few dangerous functions can't be averaged away by hundreds of harmless
  ones. The agent refines security when enabled.
- **Container noise in diffs.** Editing a method also changes its class's and
  file's hashes, so those appear as modified alongside it.

---

## Roadmap

- AI-authorship tagging — mark which nodes were written by Claude Code, Copilot
  or Cursor from commit metadata, and heat-map AI density against risk
- Parallel agent reviews
- Natural-language graph queries (*"which endpoints reach the database without
  auth?"*)
- MCP server mode, so coding agents can consult the map before editing
- Tree-sitter parsers for JavaScript, TypeScript, Go and Java

---

## Contributing

Issues and pull requests welcome. Before opening a PR, run:

```bash
python examples/verify_demo.py
```

If you change the scanner, add a case to `examples/sample_repo` that proves it —
that fixture has already caught three real bugs.

## License

MIT
