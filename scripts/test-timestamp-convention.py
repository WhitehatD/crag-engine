#!/usr/bin/env python3
# coding: utf-8
"""CI gate — canonical timestamp convention (stdlib-only).

CONVENTION: every TEXT timestamp this
codebase WRITES must be Python `datetime.now(timezone.utc).isoformat()`
('YYYY-MM-DDTHH:MM:SS.ffffff+00:00') — never SQLite `datetime('now')` /
`CURRENT_TIMESTAMP` ('YYYY-MM-DD HH:MM:SS', space separator, naive). Space
(0x20) sorts before 'T' (0x54), so mixing the two formats corrupts every
same-day lexical comparison and ORDER BY: this defect class has produced
duplicate-alert storms and fail-open temporal-cohort false-positive checks.
Migration 025 normalized historical rows; this gate keeps new writers from
re-introducing the split.

Rules:
  R1  No *.py file under apps/, db/ or scripts/ may use datetime('now') /
      CURRENT_TIMESTAMP inside SQL text — as a WRITER or as a COMPARISON
      BOUNDARY (both sides of the format split are equally wrong). Comment/
      docstring mentions are allowed (detected by absence of SQL keywords on
      the line). tests/ directories are exempt: fixtures may deliberately
      replicate the legacy schema to prove parsers tolerate historical rows.
  R2  Migration files with version >= 26 may not use datetime('now') /
      CURRENT_TIMESTAMP at all (no new space-format defaults; existing
      migrations are immutable history and exempt).

Exit 0 = compliant, exit 1 = violations (printed file:line).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN = re.compile(r"datetime\(\s*['\"]now['\"]|CURRENT_TIMESTAMP", re.IGNORECASE)
# A line only counts as a violation when the forbidden token co-occurs with an
# SQL statement keyword — this exempts prose in comments/docstrings that
# *documents* the rule (which necessarily names the forbidden token).
SQLISH = re.compile(r"\b(INSERT|UPDATE|DELETE|VALUES|SELECT|WHERE|DEFAULT|SET)\b")

PY_SCAN_DIRS = ("apps", "db", "scripts")
MIGRATION_DIR = ROOT / "db" / "migrations"
MIGRATION_EXEMPT_MAX_VERSION = 25  # immutable history through 025


def scan_python() -> list[str]:
    violations: list[str] = []
    for base in PY_SCAN_DIRS:
        for path in sorted((ROOT / base).rglob("*.py")):
            if path.resolve() == Path(__file__).resolve():
                continue
            if "tests" in path.parts:  # fixtures may replicate legacy schemas
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError as exc:
                violations.append(f"{path}: unreadable ({exc})")
                continue
            for n, line in enumerate(lines, 1):
                if FORBIDDEN.search(line) and SQLISH.search(line):
                    stripped = line.strip()
                    if stripped.startswith("#") or stripped.startswith("--"):
                        continue
                    violations.append(
                        f"{path.relative_to(ROOT)}:{n}: SQL-side timestamp writer "
                        f"— use _utcnow_iso() / datetime.now(timezone.utc).isoformat(): "
                        f"{stripped[:120]}"
                    )
    return violations


def scan_migrations() -> list[str]:
    violations: list[str] = []
    for path in sorted(MIGRATION_DIR.glob("*.sql")):
        try:
            version = int(path.stem.split("_")[0])
        except ValueError:
            continue
        if version <= MIGRATION_EXEMPT_MAX_VERSION:
            continue
        for n, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            stripped = line.strip()
            if stripped.startswith("--"):
                continue
            if FORBIDDEN.search(line):
                violations.append(
                    f"{path.relative_to(ROOT)}:{n}: new migrations must not "
                    f"introduce SQLite-side timestamps: {stripped[:120]}"
                )
    return violations


def main() -> int:
    violations = scan_python() + scan_migrations()
    if violations:
        print(f"FAIL: {len(violations)} timestamp-convention violation(s):")
        for v in violations:
            print(f"  {v}")
        return 1
    print("OK: timestamp convention clean "
          "(no SQL-side datetime('now')/CURRENT_TIMESTAMP writers)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
