"""RepoGraph command line.

  repograph scan <path> [--agents] [--model MODEL] [--max-nodes N]
  repograph serve [--db PATH] [--host HOST] [--port PORT]
"""

from __future__ import annotations

import argparse
import sys

from rich.console import Console
from rich.panel import Panel
from rich.progress import (BarColumn, MofNCompleteColumn, Progress,
                           SpinnerColumn, TextColumn, TimeElapsedColumn)
from rich.table import Table

from repograph.graph import builder, store
from repograph.scanner.python_parser import scan_repository

console = Console()

KIND_STYLE = {
    "module": "bright_black", "function": "green", "class": "blue",
    "api": "yellow", "llm_call": "red",
}


def _stats_panel(summary: dict, db_path: str) -> Panel:
    table = Table.grid(padding=(0, 3))
    table.add_column(style="bold")
    table.add_column(justify="right")
    table.add_row("Files scanned", str(summary["files_scanned"]))
    for kind, count in sorted(summary["kinds"].items()):
        style = KIND_STYLE.get(kind, "white")
        table.add_row(f"[{style}]{kind}[/{style}]", str(count))
    table.add_row("Edges", str(summary["edges"]))
    table.add_row("Doc coverage", f"{summary['doc_coverage']}%")
    if summary["parse_errors"]:
        table.add_row("[red]Parse errors[/red]", str(len(summary["parse_errors"])))
    return Panel(table, title="[bold green]✓ codebase graph[/bold green]",
                 subtitle=f"[dim]{db_path}[/dim]", border_style="green", expand=False)


def cmd_scan(args: argparse.Namespace) -> int:
    with console.status(f"[bold]Scanning[/bold] {args.path} …", spinner="dots"):
        result = scan_repository(args.path)
        graph = builder.build_graph(result)
        summary = builder.summarize(graph, result)

    db_path = args.db or store.default_db_path(result.root)
    conn = store.open_db(db_path)
    store.save_graph(conn, graph, summary, result.root)
    conn.close()
    console.print(_stats_panel(summary, db_path))

    if args.agents:
        from repograph.agents import security

        progress = Progress(
            SpinnerColumn(style="yellow"),
            TextColumn("[bold]security agent[/bold]"),
            BarColumn(bar_width=28, complete_style="yellow", finished_style="green"),
            MofNCompleteColumn(),
            TextColumn("[dim]{task.fields[detail]}[/dim]"),
            TimeElapsedColumn(),
            console=console,
        )
        with progress:
            task = progress.add_task("review", total=args.max_nodes, detail="warming up…")

            def on_progress(idx: int, total: int, node: dict | None, n_issues: int) -> None:
                progress.update(task, total=total, completed=idx)
                if node:
                    name = node["qualname"][-46:]
                    progress.update(task, detail=f"reviewing {name}  ·  {n_issues} issue(s) so far")
                else:
                    progress.update(task, detail=f"{n_issues} issue(s) found")

            outcome = security.run(db_path, model=args.model,
                                   max_nodes=args.max_nodes, progress_cb=on_progress)

        if outcome.get("ok"):
            issues = outcome["issues"]
            style = "red" if issues else "green"
            console.print(f"  [{style}]●[/{style}] reviewed [bold]{outcome['reviewed']}[/bold] nodes → "
                          f"[bold {style}]{issues}[/bold {style}] issue(s)   [dim]{outcome['model']}[/dim]")
            for err in outcome.get("errors", []):
                console.print(f"  [yellow]warning:[/yellow] {err}")
        else:
            console.print(f"  [yellow]agent skipped:[/yellow] {outcome.get('reason')}")

    hint = f" --db {db_path}" if args.db else ""
    console.print(f"\nExplore it with:  [bold cyan]repograph serve{hint}[/bold cyan]")
    return 0



def cmd_diff(args: argparse.Namespace) -> int:
    import json as _json

    from repograph import gitscan
    from repograph.graph import diff as diffmod

    try:
        base_label = gitscan.resolve_ref(args.path, args.base)
        head_label = gitscan.resolve_ref(args.path, args.head) if args.head else "worktree"
    except gitscan.GitError as exc:
        console.print(f"[red]git error:[/red] {exc}")
        return 2

    with console.status(f"[bold]Scanning base[/bold] {args.base} …", spinner="dots"):
        base_graph, _ = gitscan.scan_ref(args.path, args.base)
    with console.status(f"[bold]Scanning head[/bold] {head_label} …", spinner="dots"):
        head_graph, _ = (gitscan.scan_ref(args.path, args.head) if args.head
                         else gitscan.scan_worktree(args.path))

    b_nodes, b_edges = gitscan.graph_to_lists(base_graph)
    h_nodes, h_edges = gitscan.graph_to_lists(head_graph)
    result = diffmod.diff_graphs(b_nodes, b_edges, h_nodes, h_edges)

    # ---- terminal report ---------------------------------------------------
    header = (f"[bold]{len(result.modified)}[/bold] modified · "
              f"[bold]{len(result.added)}[/bold] added · "
              f"[bold]{len(result.removed)}[/bold] removed  →  "
              f"blast radius [bold yellow]{len(result.impacted)}[/bold yellow] nodes")
    console.print(Panel(header, title=f"[bold]diff[/bold] {base_label} → {head_label}",
                        border_style="cyan", expand=False))

    def _changes_table(title: str, rows: list, style: str) -> None:
        if not rows:
            return
        table = Table(title=f"{title} ({len(rows)})", title_justify="left",
                      title_style=f"bold {style}", box=None, padding=(0, 2))
        table.add_column("node", style="bold")
        table.add_column("kind")
        table.add_column("file", style="dim")
        table.add_column("risk", justify="right")
        for r in rows[:15]:
            table.add_row(r["name"], r["kind"], f"{r['file']}:{r['line']}", str(r["risk"]))
        console.print(table)

    _changes_table("modified", result.modified, "yellow")
    _changes_table("added", result.added, "green")
    _changes_table("removed", result.removed, "red")

    if result.impacted:
        table = Table(title=f"impacted downstream ({len(result.impacted)})",
                      title_justify="left", title_style="bold cyan",
                      box=None, padding=(0, 2))
        table.add_column("node", style="bold")
        table.add_column("kind")
        table.add_column("risk", justify="right")
        table.add_column("via", style="dim")
        table.add_column("hops", justify="right")
        for n in result.impacted[:15]:
            risk_style = "red" if n.risk >= 30 else "white"
            table.add_row(n.name, n.kind, f"[{risk_style}]{n.risk}[/{risk_style}]",
                          n.origin_name, str(n.distance))
        console.print(table)
        hot = result.hot_impacted
        if hot:
            console.print(f"  [red]⚠ {len(hot)} high-attention node(s)[/red] "
                          "(exposed API/LLM surface or risk ≥ 30) depend on this change")
    elif result.changed_count:
        console.print("  [green]contained change[/green] — nothing downstream depends on it")
    else:
        console.print("  [green]no semantic changes[/green]")

    # ---- file outputs ------------------------------------------------------
    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as fh:
            fh.write(diffmod.to_markdown(result, base_label, head_label))
        console.print(f"  markdown report → [cyan]{args.markdown}[/cyan]")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            _json.dump(diffmod.to_dict(result), fh, indent=2)
        console.print(f"  json report     → [cyan]{args.json}[/cyan]")

    if args.fail_risk is not None and result.changed_count:
        worst, who = result.max_risk()
        if worst >= args.fail_risk:
            console.print(f"[red]FAIL[/red] risk {worst} ≥ threshold {args.fail_risk} — {who}")
            return 1
        console.print(f"  [green]PASS[/green] highest risk {worst} < threshold {args.fail_risk}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn
    from repograph.server.app import create_app
    app = create_app(db_path=args.db)
    console.print(f"RepoGraph UI → [bold cyan]http://{args.host}:{args.port}[/bold cyan]")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="repograph",
                                     description="Semantic code graph + AI review for Python repos.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="Scan a repository and build the graph.")
    p_scan.add_argument("path", help="Repository root to scan")
    p_scan.add_argument("--db", default=None, help="Database path (default: <repo>/.repograph/graph.db)")
    p_scan.add_argument("--agents", action="store_true", help="Run the security review agent (needs FIREWORKS_API_KEY)")
    p_scan.add_argument("--model", default=None, help="Override the LLM model id")
    p_scan.add_argument("--max-nodes", type=int, default=8, help="Max nodes the agent reviews (default 8)")
    p_scan.set_defaults(func=cmd_scan)

    p_diff = sub.add_parser("diff", help="Blast radius of a change between two git refs.")
    p_diff.add_argument("path", nargs="?", default=".", help="Git repository (default: .)")
    p_diff.add_argument("--base", required=True, help="Base git ref (e.g. origin/main, HEAD~1)")
    p_diff.add_argument("--head", default=None, help="Head git ref (default: working tree)")
    p_diff.add_argument("--markdown", default=None, help="Write a markdown report to this path")
    p_diff.add_argument("--json", default=None, help="Write a JSON report to this path")
    p_diff.add_argument("--fail-risk", type=int, default=None,
                        help="Exit 1 if any changed or impacted node has risk >= N (CI gate)")
    p_diff.set_defaults(func=cmd_diff)

    p_serve = sub.add_parser("serve", help="Serve the interactive graph UI.")
    p_serve.add_argument("--db", default=None, help="Database path (default: ./.repograph/graph.db)")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        console.print("\n[dim]interrupted[/dim]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
