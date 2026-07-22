#!/usr/bin/env python3
"""End-to-end check of the diff engine against the generated demo repository.

Builds the demo repo, runs each pull-request scenario, and asserts the impact
analysis is correct. Run it after changing the scanner, the graph metrics or
the diff engine:

    python examples/verify_demo.py

Exits non-zero on the first failed expectation.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from make_demo_repo import build  # noqa: E402

from repograph import gitscan  # noqa: E402
from repograph.graph import diff as diffmod  # noqa: E402

PASS, FAIL = "  ok  ", "  FAIL"
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{PASS if condition else FAIL}  {label}{'' if condition else '  <- ' + detail}")
    if not condition:
        failures.append(label)


def run_diff(repo: str, base: str, head: str):
    base_graph, _ = gitscan.scan_ref(repo, base)
    head_graph, _ = gitscan.scan_ref(repo, head)
    b_nodes, b_edges = gitscan.graph_to_lists(base_graph)
    h_nodes, h_edges = gitscan.graph_to_lists(head_graph)
    return diffmod.diff_graphs(b_nodes, b_edges, h_nodes, h_edges)


def names(items) -> set[str]:
    return {i["name"] if isinstance(i, dict) else i.name for i in items}


def main() -> int:
    repo = os.path.join(tempfile.gettempdir(), "repograph-demo-verify")
    build(repo)

    print("scenario: pr/refactor-truncation (modified hub)")
    d = run_diff(repo, "main", "pr/refactor-truncation")
    check("process() reported as modified", "process" in names(d.modified))
    check("_truncate() reported as added", "_truncate" in names(d.added))
    impacted = names(d.impacted)
    check("both API routes impacted",
          {"create_ticket", "import_from_url"} <= impacted, str(sorted(impacted)))
    check("CLI entrypoint impacted", "main" in impacted)
    check("high-attention nodes flagged", len(d.hot_impacted) >= 2)

    print("\nscenario: pr/remove-resolve (removed function)")
    d = run_diff(repo, "main", "pr/remove-resolve")
    check("resolve() reported as removed", "resolve" in names(d.removed))
    check("the API route that called it is impacted",
          "resolve_ticket" in names(d.impacted), str(sorted(names(d.impacted))))
    check("removed node itself is not listed as impacted",
          "resolve" not in names(d.impacted))

    print("\nscenario: pr/add-webhook (added risky endpoint)")
    d = run_diff(repo, "main", "pr/add-webhook")
    check("export_backlog() reported as added", "export_backlog" in names(d.added))
    worst, who = d.max_risk()
    check("risk gate trips on the added endpoint", worst >= 40, f"worst={worst}")
    check("gate names the offending node", "export_backlog" in who, who)
    check("new leaf endpoint has no downstream impact", len(d.impacted) == 0)

    print("\nscenario: identical refs")
    d = run_diff(repo, "main", "main")
    check("no changes detected", d.changed_count == 0 and not d.impacted)

    print("\nscenario: CLI exit codes")
    env = {**os.environ}
    blocked = subprocess.run(
        ["repograph", "diff", repo, "--base", "main", "--head", "pr/add-webhook",
         "--fail-risk", "40"], capture_output=True, text=True, env=env)
    check("--fail-risk exits 1 on a risky PR", blocked.returncode == 1,
          f"exit={blocked.returncode}")
    allowed = subprocess.run(
        ["repograph", "diff", repo, "--base", "main", "--head", "pr/add-webhook",
         "--fail-risk", "95"], capture_output=True, text=True, env=env)
    check("--fail-risk exits 0 below threshold", allowed.returncode == 0,
          f"exit={allowed.returncode}")

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("all demo checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
