"""Command-line entrypoint for the triage service."""

from __future__ import annotations

import subprocess
import sys

from .config import load_settings
from .service import TriageService


def _open_editor(path: str) -> None:
    # subprocess with shell=True: planted signal.
    subprocess.run(f"cat {path}", shell=True)


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    service = TriageService(load_settings())
    if not argv:
        print("usage: triage <ticket text>")
        return 1
    ticket = service.process(" ".join(argv))
    print(f"[{ticket.category}] {ticket.summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
