"""SQLite persistence for the code graph."""

from __future__ import annotations

import json
import os
import sqlite3
import time

import networkx as nx

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id        TEXT PRIMARY KEY,
    kind      TEXT NOT NULL,
    name      TEXT NOT NULL,
    qualname  TEXT NOT NULL,
    file      TEXT NOT NULL,
    line      INTEGER,
    end_line  INTEGER,
    snippet   TEXT,
    hash      TEXT DEFAULT '',
    meta      TEXT,
    uses      INTEGER DEFAULT 0,
    used_by   INTEGER DEFAULT 0,
    blast     INTEGER DEFAULT 0,
    risk      INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS edges (
    src  TEXT NOT NULL,
    dst  TEXT NOT NULL,
    kind TEXT NOT NULL,
    UNIQUE (src, dst, kind)
);
CREATE TABLE IF NOT EXISTS issues (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id  TEXT,
    severity TEXT,
    category TEXT,
    title    TEXT,
    detail   TEXT,
    file     TEXT,
    line     INTEGER,
    agent    TEXT
);
CREATE TABLE IF NOT EXISTS summary (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def default_db_path(repo_root: str) -> str:
    return os.path.join(repo_root, ".repograph", "graph.db")


def open_db(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(nodes)")}
    if "hash" not in cols:  # migrate v0.1 databases
        conn.execute("ALTER TABLE nodes ADD COLUMN hash TEXT DEFAULT ''")
    return conn


def save_graph(conn: sqlite3.Connection, graph: nx.MultiDiGraph,
               summary: dict, repo_root: str) -> None:
    with conn:
        conn.execute("DELETE FROM nodes")
        conn.execute("DELETE FROM edges")
        conn.execute("DELETE FROM issues")
        conn.executemany(
            "INSERT INTO nodes (id, kind, name, qualname, file, line, end_line,"
            " snippet, hash, meta, uses, used_by, blast, risk)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    n, d["kind"], d["name"], d["qualname"], d["file"],
                    d.get("line", 0), d.get("end_line", 0), d.get("snippet", ""),
                    d.get("hash", ""), json.dumps(d.get("meta") or {}),
                    d.get("uses", 0), d.get("used_by", 0),
                    d.get("blast", 0), d.get("risk", 0),
                )
                for n, d in graph.nodes(data=True)
            ],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO edges VALUES (?,?,?)",
            [(s, t, d.get("kind", "")) for s, t, d in graph.edges(data=True)],
        )
        conn.execute("INSERT OR REPLACE INTO summary VALUES ('summary', ?)",
                     (json.dumps(summary),))
        conn.execute("INSERT OR REPLACE INTO summary VALUES ('repo_root', ?)", (repo_root,))
        conn.execute("INSERT OR REPLACE INTO summary VALUES ('scanned_at', ?)",
                     (str(int(time.time())),))


def save_issues(conn: sqlite3.Connection, issues: list[dict], agent: str) -> None:
    with conn:
        conn.executemany(
            "INSERT INTO issues (node_id, severity, category, title, detail, file, line, agent)"
            " VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    i.get("node_id"), i.get("severity", "medium"),
                    i.get("category", "security"), i.get("title", ""),
                    i.get("detail", ""), i.get("file", ""), i.get("line", 0), agent,
                )
                for i in issues
            ],
        )


def update_score(conn: sqlite3.Connection, key: str, value: int) -> None:
    row = conn.execute("SELECT value FROM summary WHERE key='summary'").fetchone()
    if not row:
        return
    summary = json.loads(row["value"])
    summary.setdefault("scores", {})[key] = value
    with conn:
        conn.execute("INSERT OR REPLACE INTO summary VALUES ('summary', ?)",
                     (json.dumps(summary),))


# ---- read side ------------------------------------------------------------- #

def load_summary(conn: sqlite3.Connection) -> dict:
    out: dict = {}
    for row in conn.execute("SELECT key, value FROM summary"):
        out[row["key"]] = json.loads(row["value"]) if row["key"] == "summary" else row["value"]
    return out


def load_nodes(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM nodes").fetchall()
    return [dict(r) | {"meta": json.loads(r["meta"] or "{}")} for r in rows]


def load_edges(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM edges")]


def load_issues(conn: sqlite3.Connection, node_id: str | None = None) -> list[dict]:
    if node_id:
        rows = conn.execute("SELECT * FROM issues WHERE node_id=? ORDER BY id", (node_id,))
    else:
        rows = conn.execute(
            "SELECT * FROM issues ORDER BY CASE severity"
            " WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, id"
        )
    return [dict(r) for r in rows]


def load_node(conn: sqlite3.Connection, node_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
    if not row:
        return None
    node = dict(row) | {"meta": json.loads(row["meta"] or "{}")}
    node["out_edges"] = [dict(r) for r in conn.execute(
        "SELECT e.dst AS id, e.kind, n.name, n.kind AS node_kind FROM edges e"
        " JOIN nodes n ON n.id = e.dst WHERE e.src=?", (node_id,))]
    node["in_edges"] = [dict(r) for r in conn.execute(
        "SELECT e.src AS id, e.kind, n.name, n.kind AS node_kind FROM edges e"
        " JOIN nodes n ON n.id = e.src WHERE e.dst=?", (node_id,))]
    return node
