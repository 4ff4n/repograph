"""Graph diff: the blast radius of a change.

Compares two scans (base vs head) of the same repository:

  added     : node ids present only in head
  removed   : node ids present only in base
  modified  : same id, different content hash

Impact is then computed by reverse-BFS over dependency edges
(calls / imports / inherits): every node that transitively depends on a
changed node can feel the change. Each impacted node records its closest
changed origin and distance, so a report can say *why* it is affected.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

DEPENDENCY_EDGES = {"calls", "imports", "inherits"}
HOT_KINDS = {"api", "llm_call"}
HOT_RISK = 30


@dataclass
class ImpactedNode:
    id: str
    name: str
    kind: str
    file: str
    line: int
    risk: int
    origin: str        # id of the closest changed node that reaches it
    origin_name: str
    distance: int      # dependency hops from the origin


@dataclass
class DiffResult:
    added: list[dict] = field(default_factory=list)
    removed: list[dict] = field(default_factory=list)
    modified: list[dict] = field(default_factory=list)
    impacted: list[ImpactedNode] = field(default_factory=list)

    @property
    def changed_count(self) -> int:
        return len(self.added) + len(self.removed) + len(self.modified)

    @property
    def hot_impacted(self) -> list[ImpactedNode]:
        return [n for n in self.impacted if n.kind in HOT_KINDS or n.risk >= HOT_RISK]

    def max_impacted_risk(self) -> int:
        return max((n.risk for n in self.impacted), default=0)

    def max_risk(self) -> tuple[int, str]:
        """Highest risk across changed *and* impacted nodes, with its name.

        A pull request that introduces a dangerous new endpoint should trip a
        CI gate even when nothing downstream depends on it yet.
        """
        best, who = 0, ""
        for node in self.added + self.modified:
            if (node.get("risk") or 0) > best:
                best, who = node["risk"], f"{node['name']} (added/modified)"
        for imp in self.impacted:
            if imp.risk > best:
                best, who = imp.risk, f"{imp.name} (impacted)"
        return best, who


def _brief(node: dict) -> dict:
    return {k: node.get(k) for k in ("id", "name", "kind", "file", "line", "risk", "blast")}


def _reverse_adjacency(edges: list[dict]) -> dict[str, list[str]]:
    rev: dict[str, list[str]] = {}
    for e in edges:
        if e.get("kind") in DEPENDENCY_EDGES:
            rev.setdefault(e["dst"], []).append(e["src"])
    return rev


def diff_graphs(base_nodes: list[dict], base_edges: list[dict],
                head_nodes: list[dict], head_edges: list[dict]) -> DiffResult:
    base = {n["id"]: n for n in base_nodes}
    head = {n["id"]: n for n in head_nodes}
    result = DiffResult()

    for nid, node in head.items():
        if nid not in base:
            result.added.append(_brief(node))
        elif node.get("hash") != base[nid].get("hash"):
            result.modified.append(_brief(node))
    for nid, node in base.items():
        if nid not in head:
            result.removed.append(_brief(node))

    # Seeds that exist in head impact the head graph; removed nodes impact
    # whoever depended on them in the base graph (those callers now break).
    result.impacted = (
        _impact(head, _reverse_adjacency(head_edges),
                [n["id"] for n in result.added + result.modified])
        + _impact(base, _reverse_adjacency(base_edges),
                  [n["id"] for n in result.removed], only_surviving=set(head))
    )
    # dedupe, keep the closest origin per node
    best: dict[str, ImpactedNode] = {}
    for imp in result.impacted:
        if imp.id not in best or imp.distance < best[imp.id].distance:
            best[imp.id] = imp
    result.impacted = sorted(best.values(), key=lambda n: (-n.risk, n.distance, n.id))
    return result


def _impact(nodes: dict[str, dict], rev: dict[str, list[str]],
            seeds: list[str], only_surviving: set | None = None) -> list[ImpactedNode]:
    """Multi-source BFS upstream from the changed nodes."""
    seed_set = set(seeds)
    out: list[ImpactedNode] = []
    seen: dict[str, int] = {}
    queue: deque[tuple[str, str, int]] = deque((s, s, 0) for s in seeds if s in nodes)

    while queue:
        current, origin, dist = queue.popleft()
        for parent in rev.get(current, ()):  # who depends on `current`
            if parent in seed_set or seen.get(parent, 1 << 30) <= dist + 1:
                continue
            seen[parent] = dist + 1
            node = nodes.get(parent)
            if node and (only_surviving is None or parent in only_surviving):
                out.append(ImpactedNode(
                    id=parent, name=node["name"], kind=node["kind"],
                    file=node["file"], line=node.get("line", 0),
                    risk=node.get("risk", 0),
                    origin=origin, origin_name=nodes.get(origin, {}).get("name", origin),
                    distance=dist + 1,
                ))
            queue.append((parent, origin, dist + 1))
    return out


# --------------------------------------------------------------------------- #
# reports
# --------------------------------------------------------------------------- #

def to_dict(diff: DiffResult) -> dict:
    return {
        "changed": diff.changed_count,
        "added": diff.added,
        "removed": diff.removed,
        "modified": diff.modified,
        "blast_radius": len(diff.impacted),
        "impacted": [vars(n) for n in diff.impacted],
        "hot_impacted": [vars(n) for n in diff.hot_impacted],
    }


def to_markdown(diff: DiffResult, base_label: str = "base", head_label: str = "head",
                max_rows: int = 20) -> str:
    lines = ["## RepoGraph impact report", ""]
    lines.append(
        f"**`{base_label}` → `{head_label}`** · "
        f"{len(diff.modified)} modified · {len(diff.added)} added · "
        f"{len(diff.removed)} removed · **blast radius: {len(diff.impacted)} "
        f"downstream node{'s' if len(diff.impacted) != 1 else ''}**"
    )
    lines.append("")

    if not diff.changed_count:
        lines.append("No semantic changes detected — the graphs are identical.")
        return "\n".join(lines)

    def change_table(title: str, rows: list[dict]) -> None:
        if not rows:
            return
        lines.append(f"### {title} ({len(rows)})")
        lines.append("| node | kind | file | risk |")
        lines.append("|---|---|---|---|")
        for r in rows[:max_rows]:
            lines.append(f"| `{r['name']}` | {r['kind']} | `{r['file']}:{r['line']}` | {r['risk']} |")
        if len(rows) > max_rows:
            lines.append(f"| … {len(rows) - max_rows} more | | | |")
        lines.append("")

    change_table("Modified", diff.modified)
    change_table("Added", diff.added)
    change_table("Removed", diff.removed)

    if diff.impacted:
        hot = diff.hot_impacted
        if hot:
            lines.append(f"### ⚠ High-attention downstream nodes ({len(hot)})")
            lines.append("These depend on the change and are exposed surface or already risky:")
            lines.append("")
            lines.append("| node | kind | risk | reached via | hops |")
            lines.append("|---|---|---|---|---|")
            for n in hot[:max_rows]:
                lines.append(f"| `{n.name}` ({n.file}:{n.line}) | {n.kind} | {n.risk} "
                             f"| `{n.origin_name}` | {n.distance} |")
            lines.append("")
        lines.append("<details><summary>All impacted nodes "
                     f"({len(diff.impacted)})</summary>")
        lines.append("")
        lines.append("| node | kind | risk | reached via | hops |")
        lines.append("|---|---|---|---|---|")
        for n in diff.impacted[:60]:
            lines.append(f"| `{n.name}` | {n.kind} | {n.risk} | `{n.origin_name}` | {n.distance} |")
        lines.append("")
        lines.append("</details>")
    else:
        lines.append("Nothing downstream depends on the changed nodes — contained change.")

    lines.append("")
    lines.append("<sub>Generated by [RepoGraph](https://github.com/) · edges: calls, imports, "
                 "inherits · dynamic dispatch and UI signal bindings are not traced.</sub>")
    return "\n".join(lines)
