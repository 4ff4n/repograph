# Triage — RepoGraph sample repo

A small AI support-ticket triage service used to exercise RepoGraph end to end.
It is intentionally seeded with a handful of security smells so the scanner and
the LLM agent both have something to find.

## What's planted (expected findings)

| file | node | signal / issue |
|---|---|---|
| `api.py` | `import_from_url` | SSRF — unvalidated user URL fetched outbound |
| `providers.py` | `LLMProvider.classify` | raw ticket text (PII) forwarded to LLM |
| `storage.py` | `find_by_status` | SQL injection via string-formatted query |
| `config.py` | `_coerce` | `eval` on env-derived input |
| `cli.py` | `_open_editor` | `subprocess` with `shell=True` |

## Expected scan results (verified)

- **7 modules, 2 llm_call, 4 api, inheritance edge** TicketRepository → BaseRepository
- `process()` is the hub: **blast radius 4** (called by both API routes, the CLI, and `triage_batch`)
- five planted signals flagged exactly as tabled above, no false positives

## Structure

- `config.py` — settings (secrets access, eval)
- `providers.py` — Fireworks/OpenAI LLM calls (2 llm_call nodes)
- `storage.py` — SQLite repo (inheritance: TicketRepository → BaseRepository)
- `service.py` — orchestration hub (`process` = high blast radius)
- `api.py` — FastAPI routes (4 api nodes)
- `cli.py` — command-line entrypoint

## Run for real

```bash
pip install openai fastapi requests pydantic
export FIREWORKS_API_KEY=fw-...
python -m triage.cli "I was double charged for my subscription"
```
