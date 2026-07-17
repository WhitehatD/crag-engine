-- Phase 8: task-graph injection (seed insight clusters per task type)
CREATE TABLE IF NOT EXISTS task_clusters (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type       TEXT NOT NULL,        -- audit, deploy, frontend, backend, infra, memory, watchdog, notification, security
    project         TEXT,                 -- NULL = global; else project-specific cluster
    insight_id      INTEGER REFERENCES insights(id),
    principle_id    INTEGER REFERENCES principles(id),
    rank            INTEGER NOT NULL DEFAULT 0,   -- lower = higher priority in cluster
    created_at      TEXT DEFAULT (datetime('now')),
    CHECK ((insight_id IS NOT NULL) OR (principle_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_task_clusters_type_project ON task_clusters(task_type, project, rank);

-- NULL-safe unique constraint. The original inline UNIQUE(task_type, project, insight_id, principle_id)
-- was toothless because SQLite treats each NULL as distinct in UNIQUE constraints. Since every row has
-- exactly one of (insight_id, principle_id) populated and the other NULL (per the CHECK above), the
-- inline constraint never rejected duplicates and `/admin/seed_task_clusters` was non-idempotent
-- (Phase 8 verifier finding 2026-05-16). Expression index with COALESCE forces NULL equivalence to
-- a sentinel, so INSERT OR IGNORE behaves correctly. Requires SQLite 3.9.0+ for expression indexes.
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_clusters_unique_nullsafe
ON task_clusters(
    task_type,
    COALESCE(project, ''),
    COALESCE(insight_id, -1),
    COALESCE(principle_id, -1)
);

INSERT OR IGNORE INTO schema_version (version) VALUES (8);
