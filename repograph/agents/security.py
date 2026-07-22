"""Security review agent.

Selects the highest-risk nodes from the graph (static heuristics decide the
shortlist), sends each source snippet to the LLM, and stores structured issues
plus an overall 0-100 security score.
"""

from __future__ import annotations

import json
import sqlite3

from repograph.agents.base import LLMClient, ModelUnavailable
from repograph.graph import store

AGENT_NAME = "security"

SYSTEM_PROMPT = """You are a rigorous application security reviewer.
You receive one code entity (function, class, API handler or LLM call site)
from a larger codebase, plus graph context about how it is used.

Report only concrete, actionable findings visible in the code: SSRF,
injection, secrets handling, unvalidated input reaching outbound requests or
LLM providers, PII leakage, unsafe deserialization, subprocess misuse,
missing auth on exposed endpoints. Do not invent issues; an empty list is a
valid answer.

Respond with ONLY a JSON object, no markdown, in this exact shape:
{
  "risk_score": <0-100, risk of THIS entity>,
  "issues": [
    {
      "severity": "high" | "medium" | "low",
      "title": "<short imperative title>",
      "detail": "<2-4 sentences: what is wrong and how to fix it>"
    }
  ]
}"""


def _shortlist(conn: sqlite3.Connection, limit: int) -> list[dict]:
    nodes = store.load_nodes(conn)
    candidates = [
        n for n in nodes
        if n["kind"] in ("function", "api", "llm_call") and n.get("snippet")
    ]
    candidates.sort(key=lambda n: (n["risk"], n["blast"]), reverse=True)
    return candidates[:limit]


def _review_node(client: LLMClient, node: dict) -> dict:
    meta = node.get("meta") or {}
    context = {
        "kind": node["kind"],
        "file": node["file"],
        "line": node["line"],
        "used_by": node["used_by"],
        "blast_radius": node["blast"],
        "static_signals": meta.get("signals", []),
        "http_route": meta.get("route"),
    }
    user = (
        f"Entity `{node['qualname']}`\n"
        f"Graph context: {json.dumps(context)}\n\n"
        f"Source:\n```python\n{node['snippet'][:3500]}\n```"
    )
    return client.complete_json(SYSTEM_PROMPT, user)


def run(db_path: str, model: str | None = None, max_nodes: int = 8,
        progress_cb=None) -> dict:
    """Run the review. `progress_cb(index, total, node, n_issues)` is called
    before each node review so a CLI can render live progress."""
    client = LLMClient(model=model)
    if not client.available:
        return {"ok": False, "reason": "No API key found (set FIREWORKS_API_KEY in .env)."}

    conn = store.open_db(db_path)
    shortlist = _shortlist(conn, max_nodes)
    if not shortlist:
        return {"ok": False, "reason": "No reviewable nodes in the graph."}

    all_issues: list[dict] = []
    node_scores: list[int] = []
    errors: list[str] = []

    for idx, node in enumerate(shortlist):
        if progress_cb:
            progress_cb(idx, len(shortlist), node, len(all_issues))
        try:
            verdict = _review_node(client, node)
        except ModelUnavailable as exc:
            # Configuration problem, not a per-node failure: stop immediately
            # rather than repeating the same error for every node.
            conn.close()
            return {"ok": False, "reason": str(exc)}
        except Exception as exc:  # noqa: BLE001 - report, don't crash the scan
            errors.append(f"{node['id']}: {exc}")
            continue
        node_scores.append(int(verdict.get("risk_score", node["risk"])))
        for issue in verdict.get("issues", []):
            all_issues.append({
                "node_id": node["id"],
                "severity": str(issue.get("severity", "medium")).lower(),
                "category": "security",
                "title": issue.get("title", "Security finding"),
                "detail": issue.get("detail", ""),
                "file": node["file"],
                "line": node["line"],
            })

    if progress_cb:
        progress_cb(len(shortlist), len(shortlist), None, len(all_issues))

    store.save_issues(conn, all_issues, AGENT_NAME)
    if node_scores:
        # Reviewed nodes are the riskiest slice; score the repo off their mean.
        security_score = max(0, 100 - round(sum(node_scores) / len(node_scores)))
        store.update_score(conn, "security", security_score)
    conn.close()

    return {
        "ok": True,
        "reviewed": len(node_scores),
        "issues": len(all_issues),
        "errors": errors,
        "model": client.model,
    }
