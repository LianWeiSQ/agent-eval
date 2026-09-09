from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable

from .common import EvalError, new_id, utc_now


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS benchmarks (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'private',
    content_hash TEXT NOT NULL,
    package_json TEXT NOT NULL,
    parent_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tenant_id, project_id, name, version)
);
CREATE INDEX IF NOT EXISTS idx_benchmarks_scope ON benchmarks(tenant_id, project_id, created_at);

CREATE TABLE IF NOT EXISTS agent_snapshots (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    adapter_type TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(tenant_id, project_id, name, version)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_scope ON agent_snapshots(tenant_id, project_id, created_at);

CREATE TABLE IF NOT EXISTS grader_specs (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    grader_type TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(tenant_id, project_id, name, version)
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    benchmark_id TEXT NOT NULL,
    status TEXT NOT NULL,
    conclusion TEXT,
    config_json TEXT NOT NULL,
    idempotency_key TEXT,
    progress_completed INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    pause_requested INTEGER NOT NULL DEFAULT 0,
    error_json TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(benchmark_id) REFERENCES benchmarks(id),
    UNIQUE(tenant_id, project_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_jobs_scope ON jobs(tenant_id, project_id, created_at);

CREATE TABLE IF NOT EXISTS trials (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    suite TEXT NOT NULL,
    agent_snapshot_id TEXT NOT NULL,
    repetition INTEGER NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    parent_attempt_id TEXT,
    execution_status TEXT NOT NULL,
    outcome TEXT,
    score REAL,
    threshold REAL NOT NULL,
    failure_stage TEXT,
    failure_type TEXT,
    evidence_complete INTEGER NOT NULL DEFAULT 0,
    agent_run_json TEXT,
    grades_json TEXT,
    usage_json TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES jobs(id),
    FOREIGN KEY(agent_snapshot_id) REFERENCES agent_snapshots(id),
    UNIQUE(job_id, task_id, agent_snapshot_id, repetition, attempt)
);
CREATE INDEX IF NOT EXISTS idx_trials_job ON trials(job_id, created_at);
CREATE INDEX IF NOT EXISTS idx_trials_scope ON trials(tenant_id, project_id, created_at);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    trial_id TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(trial_id) REFERENCES trials(id)
);
CREATE INDEX IF NOT EXISTS idx_artifacts_trial ON artifacts(trial_id);

CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    trial_id TEXT NOT NULL,
    status TEXT NOT NULL,
    reviewer_id TEXT,
    rubric_version TEXT NOT NULL,
    score REAL,
    verdict TEXT,
    issue_types_json TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    reason TEXT,
    blind INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    submitted_at TEXT,
    FOREIGN KEY(trial_id) REFERENCES trials(id)
);
CREATE INDEX IF NOT EXISTS idx_reviews_scope ON reviews(tenant_id, project_id, status, created_at);

CREATE TABLE IF NOT EXISTS supervision_runs (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    trial_id TEXT NOT NULL,
    supervisor_snapshot_id TEXT,
    supervisor_type TEXT NOT NULL,
    supervisor_version TEXT NOT NULL,
    status TEXT NOT NULL,
    verdict TEXT,
    error_types_json TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    reason TEXT,
    suggestion TEXT,
    confidence REAL,
    answer_leakage_risk TEXT,
    reference_answer_json TEXT,
    result_json TEXT,
    usage_json TEXT,
    raw_output_json TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY(trial_id) REFERENCES trials(id),
    FOREIGN KEY(supervisor_snapshot_id) REFERENCES agent_snapshots(id),
    UNIQUE(trial_id)
);
CREATE INDEX IF NOT EXISTS idx_supervision_scope ON supervision_runs(tenant_id, project_id, status, created_at);

CREATE TABLE IF NOT EXISTS correction_decisions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    supervision_run_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    approved_feedback TEXT,
    child_trial_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(supervision_run_id) REFERENCES supervision_runs(id),
    FOREIGN KEY(child_trial_id) REFERENCES trials(id),
    UNIQUE(supervision_run_id)
);
CREATE INDEX IF NOT EXISTS idx_correction_scope ON correction_decisions(tenant_id, project_id, created_at);

CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);
CREATE INDEX IF NOT EXISTS idx_reports_job ON reports(job_id, created_at);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_scope ON audit_events(tenant_id, project_id, created_at);
"""


JSON_COLUMNS = {
    "package_json": "package",
    "config_json": "config",
    "error_json": "error",
    "agent_run_json": "agent_run",
    "grades_json": "grades",
    "usage_json": "usage",
    "metadata_json": "metadata",
    "issue_types_json": "issue_types",
    "evidence_refs_json": "evidence_refs",
    "error_types_json": "error_types",
    "reference_answer_json": "reference_answer",
    "result_json": "result",
    "raw_output_json": "raw_output",
    "report_json": "report",
    "details_json": "details",
}


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with closing(self.connect()) as connection:
            connection.executescript(SCHEMA)
            self._ensure_column(connection, "supervision_runs", "supervisor_snapshot_id", "TEXT")
            self._ensure_column(connection, "supervision_runs", "result_json", "TEXT")
            self._ensure_column(connection, "supervision_runs", "usage_json", "TEXT")
            self._ensure_column(connection, "supervision_runs", "raw_output_json", "TEXT")

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
        existing = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            connection.commit()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _decoded(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        for column, target in JSON_COLUMNS.items():
            if column in item:
                raw = item.pop(column)
                item[target] = json.loads(raw) if raw else None
        for key in ("cancel_requested", "pause_requested", "evidence_complete", "blind"):
            if key in item:
                item[key] = bool(item[key])
        return item

    def execute(self, sql: str, parameters: Iterable[Any] = ()) -> None:
        with closing(self.connect()) as connection:
            connection.execute(sql, tuple(parameters))
            connection.commit()

    def insert(self, table: str, item: dict[str, Any]) -> None:
        columns = list(item)
        placeholders = ",".join("?" for _ in columns)
        values = [json.dumps(item[key], ensure_ascii=False) if key.endswith("_json") and item[key] is not None else item[key] for key in columns]
        try:
            with closing(self.connect()) as connection:
                connection.execute(
                    f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
                    values,
                )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise EvalError("conflict", f"数据冲突：{exc}", status=409) from exc

    def insert_many(self, items: Iterable[tuple[str, dict[str, Any]]]) -> None:
        try:
            with closing(self.connect()) as connection:
                for table, item in items:
                    columns = list(item)
                    placeholders = ",".join("?" for _ in columns)
                    values = [json.dumps(item[key], ensure_ascii=False) if key.endswith("_json") and item[key] is not None else item[key] for key in columns]
                    connection.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})", values)
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise EvalError("conflict", f"数据冲突：{exc}", status=409) from exc

    def update(self, table: str, item_id: str, changes: dict[str, Any]) -> None:
        if not changes:
            return
        values = [json.dumps(value, ensure_ascii=False) if key.endswith("_json") and value is not None else value for key, value in changes.items()]
        values.append(item_id)
        assignments = ",".join(f"{key}=?" for key in changes)
        with closing(self.connect()) as connection:
            cursor = connection.execute(f"UPDATE {table} SET {assignments} WHERE id=?", values)
            connection.commit()
        if cursor.rowcount == 0:
            raise EvalError("not_found", f"资源不存在：{item_id}", status=404)

    def one(self, sql: str, parameters: Iterable[Any] = ()) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(sql, tuple(parameters)).fetchone()
        return self._decoded(row)

    def all(self, sql: str, parameters: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(sql, tuple(parameters)).fetchall()
        return [self._decoded(row) for row in rows if row is not None]

    def scoped_one(
        self,
        table: str,
        item_id: str,
        tenant_id: str,
        project_id: str,
        *,
        platform_admin: bool = False,
    ) -> dict[str, Any]:
        if platform_admin:
            item = self.one(f"SELECT * FROM {table} WHERE id=?", (item_id,))
        else:
            item = self.one(
                f"SELECT * FROM {table} WHERE id=? AND tenant_id=? AND project_id=?",
                (item_id, tenant_id, project_id),
            )
        if item is None:
            raise EvalError("not_found", f"资源不存在或无权访问：{item_id}", status=404)
        return item

    def audit(
        self,
        *,
        tenant_id: str,
        project_id: str,
        actor_id: str,
        role: str,
        action: str,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.insert(
            "audit_events",
            {
                "id": new_id("audit"),
                "tenant_id": tenant_id,
                "project_id": project_id,
                "actor_id": actor_id,
                "role": role,
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "details_json": details or {},
                "created_at": utc_now(),
            },
        )
