"""Build a NetworkX graph from a ScanResult and compute per-node metrics.

Metrics
-------
uses          : outgoing `calls` edges (what this node depends on)
used_by       : incoming `calls` edges (who depends on this node)
blast_radius  : number of nodes that transitively depend on this node,
                following calls / imports / inherits edges in reverse.
                "If this breaks, how much of the codebase can feel it?"
risk          : 0-100 heuristic from static signals (network, subprocess,
                eval/exec, secrets access, LLM calls, raw SQL) amplified by
                blast radius. Agents can refine this later.
"""

from __future__ import annotations

import networkx as nx

from repograph.scanner.python_parser import ScanResult

DEPENDENCY_EDGES = {"calls", "imports", "inherits"}

SIGNAL_WEIGHTS = {
    "eval_exec": 30,
    "subprocess": 25,
    "deserialization": 25,
    "network": 15,
    "sql_raw": 15,
    "secrets_access": 10,
}


def build_graph(result: ScanResult) -> nx.MultiDiGraph:
    graph = nx.MultiDiGraph()
    for ent in result.entities:
        graph.add_node(
            ent.id, kind=ent.kind, name=ent.name, qualname=ent.qualname,
            file=ent.file, line=ent.line, end_line=ent.end_line,
            snippet=ent.snippet, hash=ent.hash, meta=ent.meta,
        )
    for rel in result.relations:
        graph.add_edge(rel.src, rel.dst, kind=rel.kind)
    compute_metrics(graph)
    return graph


def compute_metrics(graph: nx.MultiDiGraph) -> None:
    dep = nx.DiGraph()
    dep.add_nodes_from(graph.nodes)
    for src, dst, data in graph.edges(data=True):
        if data.get("kind") in DEPENDENCY_EDGES:
            dep.add_edge(src, dst)
    reverse = dep.reverse(copy=False)

    for node in graph.nodes:
        calls_out = sum(1 for _, _, d in graph.out_edges(node, data=True) if d.get("kind") == "calls")
        calls_in = sum(1 for _, _, d in graph.in_edges(node, data=True) if d.get("kind") == "calls")
        blast = len(nx.descendants(reverse, node))
        graph.nodes[node]["uses"] = calls_out
        graph.nodes[node]["used_by"] = calls_in
        graph.nodes[node]["blast"] = blast

    max_blast = max((graph.nodes[n]["blast"] for n in graph.nodes), default=0) or 1
    for node, data in graph.nodes(data=True):
        data["risk"] = _risk_score(graph, node, data, max_blast)


def _risk_score(graph: nx.MultiDiGraph, node: str, data: dict, max_blast: int) -> int:
    base = 0
    signals = (data.get("meta") or {}).get("signals", [])
    for sig in signals:
        base += SIGNAL_WEIGHTS.get(sig, 5)

    if data.get("kind") == "api":
        base += 15                       # exposed surface
    if data.get("kind") == "llm_call":
        base += 20                       # data leaves the process
    if any(d.get("kind") == "calls" and graph.nodes[t].get("kind") == "llm_call"
           for _, t, d in graph.out_edges(node, data=True)):
        base += 10                       # feeds an LLM provider

    amplifier = 1.0 + 0.5 * (data.get("blast", 0) / max_blast)
    return min(100, round(base * amplifier))


def summarize(graph: nx.MultiDiGraph, result: ScanResult) -> dict:
    kinds: dict[str, int] = {}
    documented = total_defs = 0
    for _, data in graph.nodes(data=True):
        kinds[data["kind"]] = kinds.get(data["kind"], 0) + 1
        if data["kind"] in ("function", "api", "class"):
            total_defs += 1
            if (data.get("meta") or {}).get("docstring"):
                documented += 1

    risks = [d["risk"] for _, d in graph.nodes(data=True)]
    avg_risk = round(sum(risks) / len(risks)) if risks else 0
    # Score off the riskiest slice, not the repo-wide mean: three dangerous
    # functions in a sea of harmless helpers should NOT average out to ~100.
    top_slice = sorted(risks, reverse=True)[:10]
    hot_risk = round(sum(top_slice) / len(top_slice)) if top_slice else 0
    doc_cov = round(100 * documented / total_defs) if total_defs else 100

    top_risky = sorted(
        (
            {"id": n, "name": d["name"], "kind": d["kind"], "file": d["file"],
             "line": d["line"], "risk": d["risk"], "blast": d["blast"]}
            for n, d in graph.nodes(data=True)
        ),
        key=lambda item: item["risk"], reverse=True,
    )[:10]

    return {
        "files_scanned": result.files_scanned,
        "parse_errors": result.parse_errors,
        "nodes": graph.number_of_nodes(),
        "edges": graph.number_of_edges(),
        "kinds": kinds,
        "scores": {
            # transparent heuristics for the MVP; agents refine security.
            "governance": doc_cov,
            "risk": max(0, 100 - hot_risk),
            "security": max(0, 100 - hot_risk),
            "ai_readiness": 100 if kinds.get("llm_call") else 70,
            "cost_visibility": max(0, 100 - 5 * kinds.get("llm_call", 0)),
        },
        "doc_coverage": doc_cov,
        "avg_risk": avg_risk,
        "hot_risk": hot_risk,
        "top_risky": top_risky,
    }
