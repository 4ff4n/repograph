"""FastAPI server for the RepoGraph UI.

Endpoints
---------
GET /               interactive graph UI
GET /api/summary    scan summary + scores
GET /api/graph      nodes + edges for the visualizer
GET /api/node/{id}  one node with in/out edges and its issues
GET /api/issues     all issues (agent findings)
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from repograph.graph import store

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def create_app(db_path: str | None = None) -> FastAPI:
    db_path = db_path or os.getenv("REPOGRAPH_DB", os.path.join(".repograph", "graph.db"))
    # Be forgiving about what --db points at: a repo root or the .repograph
    # directory both resolve to the graph.db inside them.
    if os.path.isdir(db_path):
        for candidate in (os.path.join(db_path, "graph.db"),
                          os.path.join(db_path, ".repograph", "graph.db")):
            if os.path.isfile(candidate):
                db_path = candidate
                break
        else:
            db_path = os.path.join(db_path, "graph.db")  # for the 404 message
    app = FastAPI(title="RepoGraph", version="0.2.0")

    def conn():
        if not os.path.isfile(db_path):
            raise HTTPException(404, f"No graph database at {db_path}. Run `repograph scan` first.")
        return store.open_db(db_path)

    @app.get("/")
    def index():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    @app.get("/api/summary")
    def summary():
        c = conn()
        try:
            return store.load_summary(c)
        finally:
            c.close()

    @app.get("/api/graph")
    def graph():
        c = conn()
        try:
            nodes = store.load_nodes(c)
            for n in nodes:
                n.pop("snippet", None)          # keep the payload light
            return {"nodes": nodes, "edges": store.load_edges(c)}
        finally:
            c.close()

    @app.get("/api/node/{node_id:path}")
    def node(node_id: str):
        c = conn()
        try:
            data = store.load_node(c, node_id)
            if not data:
                raise HTTPException(404, f"Unknown node: {node_id}")
            data["issues"] = store.load_issues(c, node_id)
            return data
        finally:
            c.close()

    @app.get("/api/issues")
    def issues():
        c = conn()
        try:
            return {"issues": store.load_issues(c)}
        finally:
            c.close()

    return app
