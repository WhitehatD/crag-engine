# Contributing to crag Anchor

Thanks for your interest. The project is in alpha; expect churn.

## Setup

```bash
git clone <repo> crag-anchor && cd crag-anchor
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e '.[all]'
pip install ruff
```

## Before you open a PR

1. **Lint** — `ruff check .` must be clean (config in `pyproject.toml`).
2. **Tests** — the suites are standalone scripts (no pytest). Run the ones
   touching your area, e.g.:
   ```bash
   python apps/daemon/tests/test_engine_paths.py
   python db/tests/test_write_gate.py
   python db/tests/test_capture.py
   python scripts/test-timestamp-convention.py
   ```
   Each exits 0 on pass, non-zero on failure. CI runs the full set.
3. **Conventions** —
   - Conventional commits (`feat:`, `fix:`, `docs:`, `refactor:`, `chore:`).
   - Pure DB functions take an open `sqlite3.Connection`; the caller commits.
   - Timestamps are Python `datetime.now(timezone.utc).isoformat()` — never
     SQLite `datetime('now')` (enforced by `scripts/test-timestamp-convention.py`).
   - Fail-soft: background-path code degrades, it does not raise into the
     save/recall request path.
   - No secrets, private hostnames, or absolute local paths in code, tests,
     or docs. Use documentation IPs (`203.0.113.x`) and `/opt/...`-style
     example paths.
4. **Migrations** — append a new numbered file under `db/migrations/`; never
   edit an existing one. Migrations must be idempotent and apply cleanly to an
   empty DB.

## Licensing of contributions

By contributing you agree your contribution is licensed under Apache-2.0 (the
repository license). Every line of source in this repository is Apache-2.0 —
there is no dual-licensed or commercial code here.
