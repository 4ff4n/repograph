"""HTTP API for the triage service.

Exposes FastAPI routes so RepoGraph tags `api` nodes. The `/import` endpoint
fetches a user-supplied URL with no validation — a planted SSRF signal that
also gives the LLM agent an obvious finding.
"""

from __future__ import annotations

import requests
from fastapi import FastAPI
from pydantic import BaseModel

from .config import load_settings
from .service import TriageService

app = FastAPI(title="Triage API")
_service = TriageService(load_settings())


class TicketIn(BaseModel):
    text: str


@app.post("/tickets")
def create_ticket(payload: TicketIn):
    ticket = _service.process(payload.text)
    return {"id": ticket.id, "category": ticket.category, "summary": ticket.summary}


@app.get("/backlog")
def get_backlog():
    return [t.__dict__ for t in _service.backlog()]


@app.post("/tickets/{ticket_id}/resolve")
def resolve_ticket(ticket_id: int):
    _service.resolve(ticket_id)
    return {"ok": True}


@app.get("/import")
def import_from_url(url: str):
    # SSRF: user-controlled URL fetched directly, no scheme/host allowlist.
    body = requests.get(url, timeout=5).text
    results = [_service.process(line) for line in body.splitlines() if line.strip()]
    return {"imported": len(results)}
