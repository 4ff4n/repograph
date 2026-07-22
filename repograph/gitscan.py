"""Scan arbitrary git refs by checking them out into a temporary worktree."""

from __future__ import annotations

import subprocess
import tempfile

from repograph.graph import builder
from repograph.scanner.python_parser import scan_repository


class GitError(RuntimeError):
    pass


def _git(repo: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", repo, *args],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout.strip()


def resolve_ref(repo: str, ref: str) -> str:
    return _git(repo, "rev-parse", "--short", ref)


def scan_ref(repo: str, ref: str):
    """Scan `ref` of `repo` in an isolated worktree. Returns (graph, summary)."""
    tmp = tempfile.mkdtemp(prefix="repograph-base-")
    _git(repo, "worktree", "add", "--detach", "--force", tmp, ref)
    try:
        result = scan_repository(tmp)
        graph = builder.build_graph(result)
        return graph, builder.summarize(graph, result)
    finally:
        subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", tmp],
                       capture_output=True)


def scan_worktree(repo: str):
    """Scan the working tree as-is (uncommitted changes included)."""
    result = scan_repository(repo)
    graph = builder.build_graph(result)
    return graph, builder.summarize(graph, result)


def graph_to_lists(graph) -> tuple[list[dict], list[dict]]:
    nodes = [dict(d) | {"id": n} for n, d in graph.nodes(data=True)]
    edges = [{"src": s, "dst": t, "kind": d.get("kind", "")}
             for s, t, d in graph.edges(data=True)]
    return nodes, edges
