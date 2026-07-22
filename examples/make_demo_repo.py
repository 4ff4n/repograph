#!/usr/bin/env python3
"""Build a demo git repository for exercising `repograph diff`.

Creates a repository with a linear history (the triage service growing file by
file) plus three branches that simulate pull requests, each showing a different
diff scenario:

  pr/refactor-truncation  modified hub function      -> wide blast radius
  pr/add-webhook          added high-risk endpoint   -> CI gate should fail
  pr/remove-resolve       removed function           -> breaks an API route

Usage:
    python examples/make_demo_repo.py [target-dir]

The file contents come from examples/sample_repo, so the demo always matches
the fixture. Every edit is asserted, so drift in the sample repo fails loudly
instead of producing a silently wrong demo.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "sample_repo", "triage")

MINIMAL_INIT = '"""AI support-ticket triage service."""\n'


# --------------------------------------------------------------------------- #
# history definition
# --------------------------------------------------------------------------- #

COMMITS = [
    {
        "message": "storage layer and configuration",
        "copy": ["config.py", "storage.py"],
        "write": {"__init__.py": MINIMAL_INIT},
    },
    {
        "message": "add Fireworks LLM provider and triage service",
        "copy": ["providers.py", "service.py", "__init__.py"],
    },
    {
        "message": "expose the triage service over HTTP",
        "copy": ["api.py"],
    },
    {
        "message": "add command-line entrypoint",
        "copy": ["cli.py"],
    },
]


PR_BRANCHES = [
    {
        "name": "pr/refactor-truncation",
        "message": "refactor: extract ticket truncation into a helper",
        "note": "modifies the process() hub -> every caller shows up as impacted",
        "edits": [(
            "service.py",
            '    def process(self, ticket_text: str) -> Ticket:\n'
            '        """Classify, summarise and store a single incoming ticket."""\n'
            '        text = ticket_text[: self.settings.max_ticket_chars]\n',
            '    def _truncate(self, ticket_text: str) -> str:\n'
            '        """Clip incoming text to the configured maximum length."""\n'
            '        return ticket_text[: self.settings.max_ticket_chars]\n'
            '\n'
            '    def process(self, ticket_text: str) -> Ticket:\n'
            '        """Classify, summarise and store a single incoming ticket."""\n'
            '        text = self._truncate(ticket_text)\n',
        )],
    },
    {
        "name": "pr/add-webhook",
        "message": "feat: add backlog export endpoint",
        "note": "adds a shell-injection endpoint -> --fail-risk should block it",
        "edits": [
            (
                "api.py",
                "import requests\nfrom fastapi import FastAPI\n",
                "import subprocess\n\nimport requests\nfrom fastapi import FastAPI\n",
            ),
            (
                "api.py",
                '@app.get("/import")',
                '@app.post("/export")\n'
                'def export_backlog(destination: str):\n'
                '    """Export the backlog by piping it through a shell command."""\n'
                '    rows = _service.backlog()\n'
                '    subprocess.run(f"cat > {destination}", shell=True)\n'
                '    return {"exported": len(rows)}\n'
                '\n'
                '\n'
                '@app.get("/import")',
            ),
        ],
    },
    {
        "name": "pr/remove-resolve",
        "message": "chore: drop the resolve() service method",
        "note": "removes a function the API depends on -> route shows as impacted",
        "edits": [(
            "service.py",
            '    def resolve(self, ticket_id: int) -> None:\n'
            '        self.repo.set_status(ticket_id, "resolved")\n'
            '\n',
            '',
        )],
    },
]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def git(repo: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{proc.stderr}")
    return proc.stdout.strip()


def read_src(name: str) -> str:
    with open(os.path.join(SRC, name), "r", encoding="utf-8") as fh:
        return fh.read()


def write(pkg_dir: str, name: str, text: str) -> None:
    with open(os.path.join(pkg_dir, name), "w", encoding="utf-8") as fh:
        fh.write(text)


def apply_edit(pkg_dir: str, name: str, old: str, new: str) -> None:
    path = os.path.join(pkg_dir, name)
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if old not in text:
        raise SystemExit(
            f"demo generator is out of sync with examples/sample_repo:\n"
            f"  could not find this snippet in {name}:\n  {old[:80]!r}"
        )
    write(pkg_dir, name, text.replace(old, new, 1))


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #

def build(target: str) -> None:
    if os.path.exists(target):
        shutil.rmtree(target)
    pkg = os.path.join(target, "triage")
    os.makedirs(pkg)

    git_init = subprocess.run(["git", "init", "-q", "-b", "main", target],
                              capture_output=True, text=True)
    if git_init.returncode != 0:      # older git without -b support
        subprocess.run(["git", "init", "-q", target], check=True)
        git(target, "checkout", "-q", "-b", "main")
    git(target, "config", "user.email", "demo@repograph.dev")
    git(target, "config", "user.name", "RepoGraph Demo")

    shutil.copy(os.path.join(HERE, "sample_repo", "README.md"),
                os.path.join(target, "README.md"))

    print(f"building demo repository in {target}\n")
    for i, step in enumerate(COMMITS, 1):
        for name in step.get("copy", []):
            write(pkg, name, read_src(name))
        for name, text in step.get("write", {}).items():
            write(pkg, name, text)
        git(target, "add", "-A")
        git(target, "commit", "-q", "-m", step["message"])
        sha = git(target, "rev-parse", "--short", "HEAD")
        print(f"  {sha}  {step['message']}")

    print()
    for branch in PR_BRANCHES:
        git(target, "checkout", "-q", "main")
        git(target, "checkout", "-q", "-b", branch["name"])
        for name, old, new in branch["edits"]:
            apply_edit(pkg, name, old, new)
        git(target, "add", "-A")
        git(target, "commit", "-q", "-m", branch["message"])
        print(f"  branch {branch['name']:<24} {branch['note']}")
    git(target, "checkout", "-q", "main")

    print(f"""
Try the diff engine:

  cd {target}

  # what did the last commit reach?
  repograph diff . --base HEAD~1 --head HEAD

  # simulate each pull request (this is what the GitHub Action runs)
  repograph diff . --base main --head pr/refactor-truncation
  repograph diff . --base main --head pr/remove-resolve
  repograph diff . --base main --head pr/add-webhook --fail-risk 40

  # write the markdown the Action posts as a PR comment
  repograph diff . --base main --head pr/refactor-truncation --markdown impact.md
""")


if __name__ == "__main__":
    dest = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        tempfile.gettempdir(), "repograph-demo")
    build(os.path.abspath(dest))
