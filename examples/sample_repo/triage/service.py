"""Triage service — the orchestration hub.

`TriageService.process` is called by both the CLI and the API, so it should
show up in RepoGraph with a high blast radius: many things break if it does.
"""

from __future__ import annotations

from .config import Settings
from .providers import LLMProvider
from .storage import Ticket, TicketRepository


class TriageService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.llm = LLMProvider(settings)
        self.repo = TicketRepository(settings.db_path)
        self.repo.init_schema()

    def process(self, ticket_text: str) -> Ticket:
        """Classify, summarise and store a single incoming ticket."""
        text = ticket_text[: self.settings.max_ticket_chars]
        category = self.llm.classify(text)
        summary = self.llm.summarize(text)
        ticket_id = self.repo.add(text, category, summary)
        return Ticket(id=ticket_id, text=text, category=category,
                      summary=summary, status="open")

    def resolve(self, ticket_id: int) -> None:
        self.repo.set_status(ticket_id, "resolved")

    def backlog(self) -> list[Ticket]:
        return self.repo.find_by_status("open")

    def triage_batch(self, texts: list[str]) -> list[Ticket]:
        return [self.process(t) for t in texts]
