"""Ticket persistence.

A tiny SQLite repository. The `find_by_status` method builds a query with
string formatting instead of parameters — a planted SQL-injection signal.
`BaseRepository` gives the scanner an inheritance edge to resolve.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass
class Ticket:
    id: int
    text: str
    category: str
    summary: str
    status: str


class BaseRepository:
    """Common connection handling for all repositories."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row

    def _execute(self, sql: str, params: tuple = ()):
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur


class TicketRepository(BaseRepository):
    """Reads and writes support tickets."""

    def init_schema(self) -> None:
        self._execute(
            "CREATE TABLE IF NOT EXISTS tickets ("
            "id INTEGER PRIMARY KEY, text TEXT, category TEXT, "
            "summary TEXT, status TEXT DEFAULT 'open')"
        )

    def add(self, text: str, category: str, summary: str) -> int:
        cur = self._execute(
            "INSERT INTO tickets (text, category, summary) VALUES (?, ?, ?)",
            (text, category, summary),
        )
        return cur.lastrowid

    def find_by_status(self, status: str) -> list[Ticket]:
        # String-formatted query: planted SQL-injection signal.
        rows = self._execute(
            "SELECT * FROM tickets WHERE status = '%s'" % status
        ).fetchall()
        return [Ticket(**dict(r)) for r in rows]

    def set_status(self, ticket_id: int, status: str) -> None:
        self._execute(
            "UPDATE tickets SET status = ? WHERE id = ?", (status, ticket_id)
        )
