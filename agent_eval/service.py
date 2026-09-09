from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

from .adapters import AdapterFailure, get_adapter
from .benchmark import load_package, validate_package
from .common import ADMIN_ROLES, EvalError, content_hash, new_id, percentile, redact, require_role, utc_now
from .compatibility import analyse_compatibility, incompatibility_message, normalise_capabilities, snapshot_capabilities
from .database import Database
from .grading_pipeline import grade_trial
from .supervision import fixture_supervise, supervision_events


@dataclass(frozen=True)
class Actor:
    tenant_id: str = "dev-tenant"
    project_id: str = "dev-project"
    actor_id: str = "local-user"
    role: str = "project_admin"

    @property
    def platform_admin(self) -> bool:
        return self.role == "platform_admin"


class EvaluationService:
    def __init__(self, data_dir: Path, *, project_root: Path | None = None) -> None:
        self.data_dir = data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_root = self.data_dir / "artifacts"
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.database = Database(self.data_dir / "evaluation.db")
        self.project_root = (project_root or Path(__file__).resolve().parents[1]).resolve()
        self._threads: dict[str, threading.Thread] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._manager_lock = threading.Lock()

    def close(self, timeout: float = 5.0) -> None:
        """Wait for managed background jobs before releasing a temporary data directory."""
        with self._manager_lock:
            threads = list(self._threads.values())
        deadline = time.monotonic() + timeout
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)

    def _scope(self, actor: Actor) -> tuple[Any, ...]:
        return (actor.tenant_id, actor.project_id)

    def _get(self, table: str, item_id: str, actor: Actor) -> dict[str, Any]:
        return self.database.scoped_one(table, item_id, actor.tenant_id, actor.project_id, platform_admin=actor.platform_admin)

    def _audit(self, actor: Actor, action: str, resource_type: str, resource_id: str, details: dict[str, Any] | None = None) -> None:
        self.database.audit(
            tenant_id=actor.tenant_id,
            project_id=actor.project_id,
            actor_id=actor.actor_id,
            role=actor.role,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=redact(details or {}),
        )

    # Benchmark registry
    def validate_benchmark_path(self, path: Path, actor: Actor | None = None) -> dict[str, Any]:
        path = path if path.is_absolute() else self.project_root / path
        resolved = path.resolve()
        if actor is not None and not actor.platform_admin and resolved != self.project_root and self.project_root not in resolved.parents:
            raise EvalError("permission_denied", "项目角色只能校验评测服务目录内的 Benchmark", status=403)
        package = load_package(resolved)
        return {
            "valid": True,
            "benchmark": package["manifest"],
            "task_count": len(package["tasks"]),
            "suites": sorted({task["suite"] for task in package["tasks"]}),
            "content_hash": package["content_hash"],
        }

    def import_benchmark_path(self, actor: Actor, path: Path, *, publish: bool = False) -> dict[str, Any]:
        path = path if path.is_absolute() else self.project_root / path
        resolved = path.resolve()
        if not actor.platform_admin and resolved != self.project_root and self.project_root not in resolved.parents:
            raise EvalError("permission_denied", "项目角色只能导入评测服务目录内的 Benchmark", status=403)
        return self.create_benchmark(actor, load_package(resolved), publish=publish)

    def create_benchmark(self, actor: Actor, package: dict[str, Any], *, publish: bool = False) -> dict[str, Any]:
        require_role(actor.role, ADMIN_ROLES | {"project_operator"})
        normalised = validate_package(package)
        manifest = normalised["manifest"]
        benchmark_id = new_id("bench")
        now = utc_now()
        self.database.insert(
            "benchmarks",
            {
                "id": benchmark_id,
                "tenant_id": actor.tenant_id,
                "project_id": actor.project_id,
                "name": manifest["name"],
                "version": manifest["version"],
                "status": "published" if publish else "draft",
                "visibility": manifest.get("visibility", "private"),
                "content_hash": normalised["content_hash"],
                "package_json": normalised,
                "parent_id": manifest.get("parent_id"),
                "created_by": actor.actor_id,
                "created_at": now,
                "updated_at": now,
            },
        )
        self._audit(actor, "benchmark.import", "benchmark", benchmark_id, {"publish": publish, "task_count": len(normalised["tasks"])})
        return self._get("benchmarks", benchmark_id, actor)

    def list_benchmarks(self, actor: Actor, *, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM benchmarks WHERE tenant_id=? AND project_id=?"
        parameters: list[Any] = [actor.tenant_id, actor.project_id]
        if status:
            sql += " AND status=?"
            parameters.append(status)
        return self.database.all(sql + " ORDER BY created_at DESC", parameters)

    def get_benchmark(self, actor: Actor, benchmark_id: str) -> dict[str, Any]:
        return self._get("benchmarks", benchmark_id, actor)

    def publish_benchmark(self, actor: Actor, benchmark_id: str) -> dict[str, Any]:
        require_role(actor.role, ADMIN_ROLES)
        item = self._get("benchmarks", benchmark_id, actor)
        if item["status"] == "archived":
            raise EvalError("invalid_state", "已归档 Benchmark 不能发布", status=409)
        self.database.update("benchmarks", benchmark_id, {"status": "published", "updated_at": utc_now()})
        self._audit(actor, "benchmark.publish", "benchmark", benchmark_id)
        return self._get("benchmarks", benchmark_id, actor)

    def archive_benchmark(self, actor: Actor, benchmark_id: str) -> dict[str, Any]:
        require_role(actor.role, ADMIN_ROLES)
        self._get("benchmarks", benchmark_id, actor)
        self.database.update("benchmarks", benchmark_id, {"status": "archived", "updated_at": utc_now()})
        self._audit(actor, "benchmark.archive", "benchmark", benchmark_id)
        return self._get("benchmarks", benchmark_id, actor)

    def copy_benchmark(self, actor: Actor, benchmark_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        require_role(actor.role, ADMIN_ROLES | {"project_operator"})
        source = self._get("benchmarks", benchmark_id, actor)
        package = json.loads(json.dumps(source["package"], ensure_ascii=False))
        package["manifest"]["id"] = str(payload.get("id") or package["manifest"]["id"])
        package["manifest"]["name"] = str(payload.get("name") or source["name"])
        package["manifest"]["version"] = str(payload.get("version") or "1.0.1")
        package["manifest"]["parent_id"] = source["id"]
        return self.create_benchmark(actor, package, publish=bool(payload.get("publish", False)))

    def export_benchmark(self, actor: Actor, benchmark_id: str, export_format: str) -> tuple[str, bytes]:
        benchmark = self._get("benchmarks", benchmark_id, actor)
        self._audit(actor, "benchmark.export", "benchmark", benchmark_id, {"format": export_format})
        if export_format == "json":
            return "application/json; charset=utf-8", (json.dumps(benchmark["package"], ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if export_format == "jsonl":
            return "application/x-ndjson; charset=utf-8", "".join(json.dumps(task, ensure_ascii=False) + "\n" for task in benchmark["package"]["tasks"]).encode("utf-8")
        raise EvalError("unsupported_format", f"不支持的 Benchmark 导出格式：{export_format}")

    # Snapshot and grader registries
    def create_snapshot(self, actor: Actor, payload: dict[str, Any]) -> dict[str, Any]:
        require_role(actor.role, ADMIN_ROLES | {"project_operator"})
        name, version, adapter_type = payload.get("name"), payload.get("version"), payload.get("adapter_type")
        if not all(isinstance(item, str) and item.strip() for item in (name, version, adapter_type)):
            raise EvalError("invalid_snapshot", "Snapshot 必须包含 name、version 和 adapter_type")
        get_adapter(str(adapter_type))
        config = redact(payload.get("config") or {})
        if "capabilities" in config:
            config["capabilities"] = normalise_capabilities(config["capabilities"], label="Snapshot config.capabilities")
        if adapter_type == "runtime-http" and (not config.get("base_url") or not config.get("agent_id")):
            raise EvalError("invalid_snapshot", "Runtime HTTP Snapshot 必须配置 base_url 和 agent_id")
        if adapter_type == "dsh-headless" and (not isinstance(config.get("command"), list) or not config.get("command")):
            raise EvalError("invalid_snapshot", "DSH Headless Snapshot 必须配置 command 字符串数组")
        snapshot_id = new_id("asnap")
        self.database.insert(
            "agent_snapshots",
            {
                "id": snapshot_id,
                "tenant_id": actor.tenant_id,
                "project_id": actor.project_id,
                "name": name,
                "version": version,
                "adapter_type": adapter_type,
                "config_hash": content_hash(config),
                "config_json": config,
                "created_by": actor.actor_id,
                "created_at": utc_now(),
            },
        )
        self._audit(actor, "snapshot.create", "agent_snapshot", snapshot_id, {"adapter_type": adapter_type})
        return self._with_snapshot_capabilities(self._get("agent_snapshots", snapshot_id, actor))

    def list_snapshots(self, actor: Actor) -> list[dict[str, Any]]:
        return [
            self._with_snapshot_capabilities(item)
            for item in self.database.all("SELECT * FROM agent_snapshots WHERE tenant_id=? AND project_id=? ORDER BY created_at DESC", self._scope(actor))
        ]

    def create_grader(self, actor: Actor, payload: dict[str, Any]) -> dict[str, Any]:
        require_role(actor.role, ADMIN_ROLES)
        name, version, grader_type = payload.get("name"), payload.get("version"), payload.get("grader_type")
        if grader_type not in {"rule", "schema", "executable", "llm_judge", "human"}:
            raise EvalError("invalid_grader", "grader_type 必须是 rule/schema/executable/llm_judge/human")
        if not isinstance(name, str) or not isinstance(version, str):
            raise EvalError("invalid_grader", "Grader 必须包含 name 和 version")
        config = redact(payload.get("config") or {})
        grader_id = new_id("grader")
        self.database.insert(
            "grader_specs",
            {
                "id": grader_id,
                "tenant_id": actor.tenant_id,
                "project_id": actor.project_id,
                "name": name,
                "version": version,
                "grader_type": grader_type,
                "config_hash": content_hash(config),
                "config_json": config,
                "created_by": actor.actor_id,
                "created_at": utc_now(),
            },
        )
        self._audit(actor, "grader.create", "grader", grader_id, {"grader_type": grader_type})
        return self._get("grader_specs", grader_id, actor)

    def list_graders(self, actor: Actor) -> list[dict[str, Any]]:
        return self.database.all("SELECT * FROM grader_specs WHERE tenant_id=? AND project_id=? ORDER BY created_at DESC", self._scope(actor))

    def adapter_health(self, actor: Actor, snapshot_id: str) -> dict[str, Any]:
        snapshot = self._get("agent_snapshots", snapshot_id, actor)
        return get_adapter(snapshot["adapter_type"]).healthcheck(snapshot.get("config") or {})

    def try_agent(self, actor: Actor, snapshot_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        require_role(actor.role, ADMIN_ROLES | {"project_operator"})
        snapshot = self._get("agent_snapshots", snapshot_id, actor)
        instruction = str(payload.get("instruction") or "").strip()
        if not instruction:
            raise EvalError("invalid_instruction", "请输入想让 Agent 完成的问题")
        if len(instruction) > 4000:
            raise EvalError("invalid_instruction", "试运行问题不能超过 4000 个字符")
        timeout = max(10, min(int(payload.get("timeout_seconds", 180)), 300))
        task = {
            "id": new_id("try"),
            "suite": "interactive-playground",
            "instruction": instruction,
            "timeout_seconds": timeout,
        }
        try:
            run = get_adapter(snapshot["adapter_type"]).run(snapshot=snapshot, task=task, fixture={}, cancel_event=threading.Event())
        except AdapterFailure as exc:
            status = 504 if exc.failure_type == "agent_timeout" else 502
            raise EvalError(exc.failure_type, str(exc), status=status) from exc
        calls = [event for event in run.get("events") or [] if event.get("event_type") == "tool_call"]
        results = [event for event in run.get("events") or [] if event.get("event_type") == "tool_result"]
        evidence = {
            "network_used": bool(calls),
            "tool_calls": len(calls),
            "queries": [{"tool": (event.get("payload") or {}).get("name"), **((event.get("payload") or {}).get("arguments") or {})} for event in calls],
            "tool_results": len(results),
            "sources": list(dict.fromkeys(str((event.get("payload") or {}).get("source_url")) for event in results if (event.get("payload") or {}).get("source_url"))),
            "errors": list(dict.fromkeys(str((event.get("payload") or {}).get("error")) for event in results if (event.get("payload") or {}).get("error"))),
        }
        self._audit(actor, "agent.try", "agent_snapshot", snapshot_id, {"network_used": evidence["network_used"], "tool_calls": evidence["tool_calls"]})
        return {
            "snapshot": {"id": snapshot["id"], "name": snapshot["name"], "version": snapshot["version"], "adapter_type": snapshot["adapter_type"]},
            "final_output": run.get("final_output"),
            "evidence": evidence,
            "usage": run.get("usage") or {},
        }

    # Jobs and trials
    def estimate_job(self, actor: Actor, payload: dict[str, Any]) -> dict[str, Any]:
        benchmark = self._get("benchmarks", str(payload.get("benchmark_id")), actor)
        tasks = self._select_tasks(benchmark["package"]["tasks"], payload.get("task_filter") or {})
        snapshots = self._resolve_snapshots(actor, payload.get("agent_snapshot_ids") or [])
        repetitions = int((payload.get("execution") or {}).get("repetitions", 1))
        count = len(tasks) * len(snapshots) * repetitions
        timeout = int((payload.get("execution") or {}).get("timeout_seconds", 120))
        concurrency = max(1, int((payload.get("execution") or {}).get("max_concurrency", 4)))
        compatibility = analyse_compatibility(benchmark["package"]["manifest"], tasks, snapshots)
        return {"task_count": len(tasks), "agent_count": len(snapshots), "repetitions": repetitions, "trial_count": count, "worst_case_seconds": count * timeout, "parallel_worst_case_seconds": (count * timeout + concurrency - 1) // concurrency, "compatibility": compatibility}

    def create_job(self, actor: Actor, payload: dict[str, Any]) -> dict[str, Any]:
        require_role(actor.role, ADMIN_ROLES | {"project_operator"})
        benchmark = self._get("benchmarks", str(payload.get("benchmark_id")), actor)
        if benchmark["status"] == "archived":
            raise EvalError("invalid_benchmark", "不能用已归档 Benchmark 创建 Job")
        snapshot_ids = list(payload.get("agent_snapshot_ids") or [])
        self._resolve_snapshots(actor, snapshot_ids)
        if not snapshot_ids:
            raise EvalError("invalid_job", "Job 至少需要一个 AgentSnapshot")
        supervisor_snapshot_id = payload.get("supervisor_snapshot_id")
        if supervisor_snapshot_id:
            supervisor = self._get("agent_snapshots", str(supervisor_snapshot_id), actor)
            if supervisor["adapter_type"] != "llm-supervisor":
                raise EvalError("invalid_supervisor", "监督 Agent 快照必须使用 llm-supervisor Adapter")
        estimate = self.estimate_job(actor, payload)
        if estimate["trial_count"] == 0:
            raise EvalError("invalid_job", "筛选后没有可运行 Trial")
        compatibility_mode = str(payload.get("compatibility_mode") or "strict")
        if compatibility_mode not in {"strict", "allow"}:
            raise EvalError("invalid_job", "compatibility_mode 必须是 strict 或 allow")
        compatibility = estimate["compatibility"]
        if not compatibility["compatible"] and compatibility_mode == "strict":
            raise EvalError("incompatible_agent", incompatibility_message(compatibility), status=409)
        execution = {"repetitions": 1, "max_concurrency": 4, "timeout_seconds": 120, "infra_retry_limit": 1, **(payload.get("execution") or {})}
        execution["max_concurrency"] = max(1, min(20, int(execution["max_concurrency"])))
        config = {
            "agent_snapshot_ids": snapshot_ids,
            "task_filter": payload.get("task_filter") or {},
            "execution": execution,
            "baseline_agent_snapshot_id": payload.get("baseline_agent_snapshot_id"),
            "supervisor_snapshot_id": str(supervisor_snapshot_id) if supervisor_snapshot_id else None,
            "report_policy": {"human_sample_rate": 0.0, "invalid_rate_limit": 0.2, **(payload.get("report_policy") or {})},
            "gate": {"min_pass_rate": 0.8, "max_hard_failures": 0, **(payload.get("gate") or {})},
            "compatibility_mode": compatibility_mode,
            "compatibility": compatibility,
            "estimate": estimate,
        }
        idempotency_key = payload.get("idempotency_key")
        if idempotency_key:
            existing = self.database.one("SELECT * FROM jobs WHERE tenant_id=? AND project_id=? AND idempotency_key=?", (actor.tenant_id, actor.project_id, idempotency_key))
            if existing:
                return existing
        job_id = new_id("job")
        now = utc_now()
        self.database.insert(
            "jobs",
            {
                "id": job_id,
                "tenant_id": actor.tenant_id,
                "project_id": actor.project_id,
                "name": str(payload.get("name") or f"{benchmark['name']} evaluation"),
                "benchmark_id": benchmark["id"],
                "status": "draft",
                "conclusion": None,
                "config_json": config,
                "idempotency_key": idempotency_key,
                "progress_completed": 0,
                "progress_total": estimate["trial_count"],
                "cancel_requested": 0,
                "pause_requested": 0,
                "error_json": None,
                "created_by": actor.actor_id,
                "created_at": now,
                "started_at": None,
                "finished_at": None,
                "updated_at": now,
            },
        )
        self._audit(actor, "job.create", "job", job_id, {"trial_count": estimate["trial_count"], "compatible": compatibility["compatible"], "compatibility_mode": compatibility_mode})
        return self._get("jobs", job_id, actor)

    def list_jobs(self, actor: Actor, *, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM jobs WHERE tenant_id=? AND project_id=?"
        parameters: list[Any] = [actor.tenant_id, actor.project_id]
        if status:
            sql += " AND status=?"
            parameters.append(status)
        return self.database.all(sql + " ORDER BY created_at DESC", parameters)

    def get_job(self, actor: Actor, job_id: str, *, summary: bool = False) -> dict[str, Any]:
        item = self._get("jobs", job_id, actor)
        item["trials"] = self.list_trials(actor, job_id=job_id, summary=summary)
        if summary:
            item["detail_view"] = "summary"
            return item
        report = self.database.one("SELECT * FROM reports WHERE job_id=? ORDER BY created_at DESC LIMIT 1", (job_id,))
        item["report"] = report["report"] if report else None
        return item

    def start_job(self, actor: Actor, job_id: str) -> dict[str, Any]:
        require_role(actor.role, ADMIN_ROLES | {"project_operator"})
        job = self._get("jobs", job_id, actor)
        if job["status"] not in {"draft", "queued", "failed"}:
            raise EvalError("invalid_state", f"Job 当前状态 {job['status']} 不能启动", status=409)
        for snapshot_id in job["config"]["agent_snapshot_ids"]:
            snapshot = self._get("agent_snapshots", snapshot_id, actor)
            if snapshot["adapter_type"] == "terminal-bench-harbor":
                health = get_adapter(snapshot["adapter_type"]).healthcheck(snapshot["config"])
                if not health["ok"]:
                    raise EvalError("environment_not_ready", health["message"], status=409)
        self.database.update("jobs", job_id, {"status": "queued", "cancel_requested": 0, "pause_requested": 0, "error_json": None, "updated_at": utc_now()})
        self._audit(actor, "job.start", "job", job_id)
        self._spawn_job(job_id)
        return self._get("jobs", job_id, actor)

    def pause_job(self, actor: Actor, job_id: str) -> dict[str, Any]:
        require_role(actor.role, ADMIN_ROLES | {"project_operator"})
        job = self._get("jobs", job_id, actor)
        if job["status"] not in {"queued", "running", "grading"}:
            raise EvalError("invalid_state", "只有运行中的 Job 可以暂停调度", status=409)
        self.database.update("jobs", job_id, {"pause_requested": 1, "updated_at": utc_now()})
        self._audit(actor, "job.pause", "job", job_id)
        return self._get("jobs", job_id, actor)

    def resume_job(self, actor: Actor, job_id: str) -> dict[str, Any]:
        require_role(actor.role, ADMIN_ROLES | {"project_operator"})
        self._get("jobs", job_id, actor)
        self.database.update("jobs", job_id, {"pause_requested": 0, "updated_at": utc_now()})
        self._audit(actor, "job.resume", "job", job_id)
        self._spawn_job(job_id)
        return self._get("jobs", job_id, actor)

    def cancel_job(self, actor: Actor, job_id: str) -> dict[str, Any]:
        require_role(actor.role, ADMIN_ROLES | {"project_operator"})
        job = self._get("jobs", job_id, actor)
        if job["status"] in {"completed", "canceled"}:
            return job
        self.database.update("jobs", job_id, {"status": "canceling", "cancel_requested": 1, "updated_at": utc_now()})
        with self._manager_lock:
            self._cancel_events.setdefault(job_id, threading.Event()).set()
        self._audit(actor, "job.cancel", "job", job_id)
        return self._get("jobs", job_id, actor)

    def list_trials(self, actor: Actor, *, job_id: str | None = None, outcome: str | None = None, summary: bool = False) -> list[dict[str, Any]]:
        columns = ("id,job_id,task_id,suite,agent_snapshot_id,repetition,attempt,parent_attempt_id,execution_status,"
                   "outcome,score,threshold,failure_stage,failure_type,evidence_complete,usage_json,error_json,"
                   "created_at,started_at,finished_at,updated_at") if summary else "*"
        sql = f"SELECT {columns} FROM trials WHERE tenant_id=? AND project_id=?"
        parameters: list[Any] = [actor.tenant_id, actor.project_id]
        if job_id:
            sql += " AND job_id=?"
            parameters.append(job_id)
        if outcome:
            sql += " AND outcome=?"
            parameters.append(outcome)
        return self.database.all(sql + " ORDER BY created_at, id", parameters)

    def get_trial(self, actor: Actor, trial_id: str) -> dict[str, Any]:
        trial = self._get("trials", trial_id, actor)
        trial["artifacts"] = self.database.all("SELECT * FROM artifacts WHERE trial_id=? ORDER BY created_at", (trial_id,))
        trial["reviews"] = self.database.all("SELECT * FROM reviews WHERE trial_id=? ORDER BY created_at", (trial_id,))
        trial["supervisions"] = self.database.all("SELECT * FROM supervision_runs WHERE trial_id=? ORDER BY created_at", (trial_id,))
        trial["correction_decisions"] = self.database.all("SELECT * FROM correction_decisions WHERE supervision_run_id IN (SELECT id FROM supervision_runs WHERE trial_id=?) ORDER BY created_at", (trial_id,))
        return trial

    def get_artifact_content(self, actor: Actor, artifact_id: str) -> tuple[str, bytes]:
        artifact = self._get("artifacts", artifact_id, actor)
        path = (self.data_dir / artifact["storage_path"]).resolve()
        if self.data_dir not in path.parents or not path.is_file():
            raise EvalError("not_found", "Artifact 文件不存在", status=404)
        self._audit(actor, "artifact.read", "artifact", artifact_id)
        return artifact["media_type"], path.read_bytes()

    def retry_trial(self, actor: Actor, trial_id: str) -> dict[str, Any]:
        require_role(actor.role, ADMIN_ROLES | {"project_operator"})
        trial = self._get("trials", trial_id, actor)
        if trial["outcome"] not in {"infra_failed", "grader_failed"}:
            raise EvalError("not_retryable", "只允许重试基础设施或评分器失败", status=409)
        retry_id = new_id("trial")
        now = utc_now()
        self.database.insert(
            "trials",
            {
                "id": retry_id,
                "tenant_id": trial["tenant_id"],
                "project_id": trial["project_id"],
                "job_id": trial["job_id"],
                "task_id": trial["task_id"],
                "suite": trial["suite"],
                "agent_snapshot_id": trial["agent_snapshot_id"],
                "repetition": trial["repetition"],
                "attempt": trial["attempt"] + 1,
                "parent_attempt_id": trial["id"],
                "execution_status": "pending",
                "outcome": None,
                "score": None,
                "threshold": trial["threshold"],
                "failure_stage": None,
                "failure_type": None,
                "evidence_complete": 0,
                "agent_run_json": None,
                "grades_json": None,
                "usage_json": None,
                "error_json": None,
                "created_at": now,
                "started_at": None,
                "finished_at": None,
                "updated_at": now,
            },
        )
        self.database.update("jobs", trial["job_id"], {"status": "queued", "finished_at": None, "updated_at": now})
        self._audit(actor, "trial.retry", "trial", retry_id, {"parent_attempt_id": trial_id})
        self._spawn_job(trial["job_id"])
        return self._get("trials", retry_id, actor)

    def regrade_trial(self, actor: Actor, trial_id: str) -> dict[str, Any]:
        require_role(actor.role, ADMIN_ROLES | {"project_operator"})
        trial = self._get("trials", trial_id, actor)
        if not trial.get("agent_run"):
            raise EvalError("invalid_state", "Trial 没有可重新评分的 AgentRun", status=409)
        job = self._get("jobs", trial["job_id"], actor)
        benchmark = self._get("benchmarks", job["benchmark_id"], actor)
        task = next((item for item in benchmark["package"]["tasks"] if item["id"] == trial["task_id"]), None)
        if task is None:
            raise EvalError("not_found", "历史 Benchmark 中找不到 Task", status=404)
        result = grade_trial(
            task=task,
            run=trial["agent_run"],
            fixture=benchmark["package"].get("fixture") or {},
            registered_graders=self._registered_graders(actor),
            executable_root=Path(os.environ["AGENT_EVAL_GRADER_ROOT"]) if os.environ.get("AGENT_EVAL_GRADER_ROOT") else None,
            enable_executable=os.environ.get("AGENT_EVAL_ENABLE_EXECUTABLE_GRADERS") == "1",
        )
        self.database.update("trials", trial_id, {"grades_json": (trial.get("grades") or []) + result["grades"], "score": result["score"], "outcome": result["outcome"], "failure_type": result["failure_type"], "updated_at": utc_now()})
        self._generate_report(job["id"])
        self._audit(actor, "trial.regrade", "trial", trial_id)
        return self._get("trials", trial_id, actor)

    # Supervisor and human-approved correction
    def supervise_trial(self, actor: Actor, trial_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        require_role(actor.role, ADMIN_ROLES | {"project_operator", "reviewer"})
        trial = self._get("trials", trial_id, actor)
        if trial.get("outcome") is None:
            raise EvalError("invalid_state", "Trial 尚未完成，不能进行监督检查", status=409)
        existing = self.database.one("SELECT * FROM supervision_runs WHERE trial_id=?", (trial_id,))
        if existing:
            return existing
        job = self._get("jobs", trial["job_id"], actor)
        benchmark = self._get("benchmarks", job["benchmark_id"], actor)
        task = next((item for item in benchmark["package"]["tasks"] if item["id"] == trial["task_id"]), None)
        if task is None:
            raise EvalError("not_found", "Benchmark 中找不到 Trial 对应的 Task", status=404)
        requested_snapshot_id = (payload or {}).get("supervisor_snapshot_id")
        supervisor_snapshot_id = str(requested_snapshot_id or (job.get("config") or {}).get("supervisor_snapshot_id") or "").strip() or None
        supervisor_run: dict[str, Any] | None = None
        if supervisor_snapshot_id:
            supervisor_snapshot = self._get("agent_snapshots", supervisor_snapshot_id, actor)
            if supervisor_snapshot["adapter_type"] != "llm-supervisor":
                raise EvalError("invalid_supervisor", "监督 Agent 快照必须使用 llm-supervisor Adapter")
            review_package = self._supervision_input(task, trial)
            try:
                supervisor_run = get_adapter(supervisor_snapshot["adapter_type"]).run(
                    snapshot=supervisor_snapshot,
                    task={"id": f"supervise-{trial_id}", "instruction": "审查执行结果", "timeout_seconds": int((supervisor_snapshot.get("config") or {}).get("timeout_seconds", 120)), "supervision_input": review_package},
                    fixture={},
                    cancel_event=threading.Event(),
                )
            except AdapterFailure as exc:
                message = str(redact(str(exc)))
                self._audit(actor, "supervision.failed", "trial", trial_id, {
                    "supervisor_snapshot_id": supervisor_snapshot_id,
                    "failure_type": exc.failure_type, "stage": exc.stage, "message": message,
                })
                raise EvalError("supervisor_" + exc.failure_type, message, status=502) from exc
            result = dict(supervisor_run["final_output"])
            result["reference_answer"] = None
        else:
            result = fixture_supervise(task=task, trial=trial)
        supervision_id = new_id("supervision")
        needs_review = result["verdict"] != "pass"
        now = utc_now()
        self.database.insert(
            "supervision_runs",
            {
                "id": supervision_id,
                "tenant_id": trial["tenant_id"],
                "project_id": trial["project_id"],
                "trial_id": trial_id,
                "supervisor_snapshot_id": supervisor_snapshot_id,
                "supervisor_type": supervisor_snapshot["adapter_type"] if supervisor_snapshot_id else "fixture-supervisor",
                "supervisor_version": supervisor_snapshot["version"] if supervisor_snapshot_id else "1.0.0",
                "status": "pending_review" if needs_review else "completed",
                "verdict": result["verdict"],
                "error_types_json": result["error_types"],
                "evidence_refs_json": result["evidence_refs"],
                "reason": result["reason"],
                "suggestion": result["suggestion"],
                "confidence": result["confidence"],
                "answer_leakage_risk": result["answer_leakage_risk"],
                "reference_answer_json": result.get("reference_answer"),
                "result_json": {key: result.get(key) for key in ("verdict", "error_types", "reason", "suggestion", "evidence_refs", "confidence", "answer_leakage_risk")},
                "usage_json": (supervisor_run or {}).get("usage") or {},
                "raw_output_json": {"content": (supervisor_run or {}).get("raw_output"), "response_metadata": (supervisor_run or {}).get("response_metadata")} if supervisor_run else None,
                "created_at": now,
                "finished_at": now,
            },
        )
        if needs_review:
            self.database.update("jobs", job["id"], {"status": "review_pending", "finished_at": None, "updated_at": now})
        self._audit(actor, "supervision.run", "supervision", supervision_id, {"trial_id": trial_id, "verdict": result["verdict"]})
        return self._get("supervision_runs", supervision_id, actor)

    @staticmethod
    def _supervision_input(task: dict[str, Any], trial: dict[str, Any]) -> dict[str, Any]:
        run = trial.get("agent_run") or {}
        events, input_policy = supervision_events(run.get("events") or [])
        return {
            "input_policy": input_policy,
            "task": {
                "id": task.get("id"),
                "title": task.get("title"),
                "instruction": task.get("instruction"),
                "constraints": {
                    "tags": task.get("tags") or [],
                    "timeout_seconds": task.get("timeout_seconds"),
                },
            },
            "attempt": {
                "id": trial.get("id"),
                "attempt": trial.get("attempt"),
                "outcome": trial.get("outcome"),
                "score": trial.get("score"),
                "failure_type": trial.get("failure_type"),
                "final_output": run.get("final_output"),
                "events": events,
                "usage": run.get("usage") or trial.get("usage") or {},
            },
            "official_grades": [
                {
                    "grader_id": grade.get("grader_id"),
                    "status": grade.get("status"),
                    "score": grade.get("score"),
                    "passed": grade.get("passed"),
                    "reason": grade.get("reason"),
                    "evidence_refs": grade.get("evidence_refs") or [],
                    "hard_failures": grade.get("hard_failures") or [],
                }
                for grade in trial.get("grades") or []
            ],
        }

    def list_supervisions(self, actor: Actor, *, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM supervision_runs WHERE tenant_id=? AND project_id=?"
        parameters: list[Any] = [actor.tenant_id, actor.project_id]
        if status:
            sql += " AND status=?"
            parameters.append(status)
        return self.database.all(sql + " ORDER BY created_at DESC", parameters)

    def decide_correction(self, actor: Actor, supervision_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        require_role(actor.role, ADMIN_ROLES | {"project_operator", "reviewer"})
        supervision = self._get("supervision_runs", supervision_id, actor)
        if supervision["status"] != "pending_review":
            raise EvalError("invalid_state", "该监督结果不在待审核状态", status=409)
        if self.database.one("SELECT id FROM correction_decisions WHERE supervision_run_id=?", (supervision_id,)):
            raise EvalError("invalid_state", "该监督结果已经完成审核", status=409)
        decision = str(payload.get("decision") or "")
        if decision not in {"approve_retry", "reject_feedback", "accept_final", "terminate"}:
            raise EvalError("invalid_decision", "decision 必须是 approve_retry/reject_feedback/accept_final/terminate")
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            raise EvalError("invalid_decision", "人工审核必须填写理由")
        trial = self._get("trials", supervision["trial_id"], actor)
        now = utc_now()
        child_trial: dict[str, Any] | None = None
        approved_feedback: str | None = None
        decision_id = new_id("decision")
        items: list[tuple[str, dict[str, Any]]] = []
        if decision == "approve_retry":
            if int(trial["attempt"]) >= 2:
                raise EvalError("attempt_limit", "MVP 每条 Task 最多允许 2 个 Attempt", status=409)
            approved_feedback = str(payload.get("approved_feedback") or supervision.get("suggestion") or "").strip()
            if not approved_feedback:
                raise EvalError("invalid_decision", "批准重试时必须提供审核反馈")
            child_trial = {
                "id": new_id("trial"),
                "tenant_id": trial["tenant_id"],
                "project_id": trial["project_id"],
                "job_id": trial["job_id"],
                "task_id": trial["task_id"],
                "suite": trial["suite"],
                "agent_snapshot_id": trial["agent_snapshot_id"],
                "repetition": trial["repetition"],
                "attempt": int(trial["attempt"]) + 1,
                "parent_attempt_id": trial["id"],
                "execution_status": "pending",
                "outcome": None,
                "score": None,
                "threshold": trial["threshold"],
                "failure_stage": None,
                "failure_type": None,
                "evidence_complete": 0,
                "agent_run_json": None,
                "grades_json": None,
                "usage_json": None,
                "error_json": None,
                "created_at": now,
                "started_at": None,
                "finished_at": None,
                "updated_at": now,
            }
            items.append(("trials", child_trial))
        items.append(
            (
                "correction_decisions",
                {
                    "id": decision_id,
                    "tenant_id": trial["tenant_id"],
                    "project_id": trial["project_id"],
                    "supervision_run_id": supervision_id,
                    "decision": decision,
                    "reviewer_id": actor.actor_id,
                    "reason": reason,
                    "approved_feedback": approved_feedback,
                    "child_trial_id": child_trial["id"] if child_trial else None,
                    "created_at": now,
                },
            )
        )
        self.database.insert_many(items)
        self.database.update("supervision_runs", supervision_id, {"status": "decision_recorded"})
        if child_trial:
            job = self._get("jobs", trial["job_id"], actor)
            self.database.update(
                "jobs",
                trial["job_id"],
                {
                    "status": "queued",
                    "progress_total": int(job.get("progress_total") or 0) + 1,
                    "finished_at": None,
                    "updated_at": now,
                },
            )
            self._spawn_job(trial["job_id"])
        else:
            self._refresh_job_review_status(trial["job_id"])
            refreshed_job = self.database.one("SELECT status FROM jobs WHERE id=?", (trial["job_id"],))
            if refreshed_job and refreshed_job["status"] == "completed":
                self._generate_report(trial["job_id"])
        self._audit(actor, "correction.decide", "correction_decision", decision_id, {"supervision_id": supervision_id, "decision": decision, "child_trial_id": child_trial["id"] if child_trial else None})
        return self._get("correction_decisions", decision_id, actor)

    def get_trial_trace(self, actor: Actor, trial_id: str, *, view: str = "summary") -> dict[str, Any]:
        if view not in {"summary", "detail"}:
            raise EvalError("invalid_view", "view 必须是 summary 或 detail")
        selected = self._get("trials", trial_id, actor)
        root = selected
        while root.get("parent_attempt_id"):
            root = self._get("trials", str(root["parent_attempt_id"]), actor)
        attempts = self.database.all(
            "SELECT * FROM trials WHERE job_id=? AND task_id=? AND agent_snapshot_id=? AND repetition=? ORDER BY attempt, created_at",
            (root["job_id"], root["task_id"], root["agent_snapshot_id"], root["repetition"]),
        )
        job = self._get("jobs", root["job_id"], actor)
        benchmark = self._get("benchmarks", job["benchmark_id"], actor)
        task = next((item for item in benchmark["package"]["tasks"] if item["id"] == root["task_id"]), {"id": root["task_id"], "instruction": ""})
        nodes: list[dict[str, Any]] = [
            {
                "id": f"task:{root['task_id']}",
                "type": "benchmark_task",
                "label": f"Benchmark Task · {root['task_id']}",
                "status": "completed",
                "summary": str(task.get("title") or task.get("instruction") or "")[:160],
                "detail": {"task_id": root["task_id"], "instruction": task.get("instruction"), "suite": root["suite"], "benchmark": f"{benchmark['name']}@{benchmark['version']}"},
            }
        ]
        edges: list[dict[str, str]] = []
        previous_id = nodes[0]["id"]
        pending_supervision_id: str | None = None
        for attempt in attempts:
            attempt_id = f"attempt:{attempt['id']}"
            nodes.append(
                {
                    "id": attempt_id,
                    "type": "attempt",
                    "label": f"执行 Agent · Attempt {attempt['attempt']}",
                    "status": attempt.get("outcome") or attempt["execution_status"],
                    "summary": f"{attempt.get('score') if attempt.get('score') is not None else '-'} 分 · {self._duration_label((attempt.get('usage') or {}).get('duration_ms'))}",
                    "detail": {key: attempt.get(key) for key in ("id", "attempt", "parent_attempt_id", "execution_status", "outcome", "score", "failure_type", "usage")},
                }
            )
            edges.append({"id": f"edge:{previous_id}:{attempt_id}", "source": previous_id, "target": attempt_id, "type": "next"})
            tail_id = attempt_id
            if view == "detail":
                for index, event in enumerate((attempt.get("agent_run") or {}).get("events") or [], start=1):
                    event_id = f"event:{attempt['id']}:{event.get('sequence', index)}"
                    event_type = str(event.get("event_type") or "event")
                    payload = event.get("payload") or {}
                    semantic = self._trace_event_semantics(event_type, payload, task, attempt)
                    nodes.append(
                        {
                            "id": event_id,
                            "type": self._trace_event_type(event_type),
                            "label": self._trace_event_label(event_type, payload),
                            "status": str(event.get("status") or "completed"),
                            "summary": self._trace_event_summary(event_type, payload),
                            "detail": {**event, **semantic},
                        }
                    )
                    edges.append({"id": f"edge:{tail_id}:{event_id}", "source": tail_id, "target": event_id, "type": "next"})
                    tail_id = event_id
            grader_id = f"grader:{attempt['id']}"
            nodes.append(
                {
                    "id": grader_id,
                    "type": "official_grader",
                    "label": "官方评分",
                    "status": "passed" if attempt.get("outcome") == "pass" else "failed" if attempt.get("score") is not None else "incomplete",
                    "summary": f"{attempt.get('score') if attempt.get('score') is not None else '-'} 分 · {attempt.get('outcome') or '-'}",
                    "detail": {"score": attempt.get("score"), "outcome": attempt.get("outcome"), "grades": attempt.get("grades") or [], "failure_type": attempt.get("failure_type")},
                }
            )
            edges.append({"id": f"edge:{tail_id}:{grader_id}", "source": tail_id, "target": grader_id, "type": "grades"})
            tail_id = grader_id
            supervision = self.database.one("SELECT * FROM supervision_runs WHERE trial_id=?", (attempt["id"],))
            if supervision:
                supervision_id = f"supervision:{supervision['id']}"
                nodes.append(
                    {
                        "id": supervision_id,
                        "type": "supervisor",
                        "label": "监督 Agent",
                        "status": supervision.get("verdict") or supervision["status"],
                        "summary": str(supervision.get("reason") or "")[:180],
                        "detail": supervision,
                    }
                )
                edges.append({"id": f"edge:{tail_id}:{supervision_id}", "source": tail_id, "target": supervision_id, "type": "reviews"})
                tail_id = supervision_id
                if supervision["status"] == "pending_review":
                    pending_supervision_id = supervision["id"]
                correction = self.database.one("SELECT * FROM correction_decisions WHERE supervision_run_id=?", (supervision["id"],))
                if correction:
                    decision_id = f"decision:{correction['id']}"
                    nodes.append(
                        {
                            "id": decision_id,
                            "type": "human_review",
                            "label": "人工审核",
                            "status": correction["decision"],
                            "summary": str(correction["reason"])[:180],
                            "detail": correction,
                        }
                    )
                    edges.append({"id": f"edge:{tail_id}:{decision_id}", "source": tail_id, "target": decision_id, "type": correction["decision"]})
                    tail_id = decision_id
            previous_id = tail_id
        latest = attempts[-1]
        latest_supervision = self.database.one("SELECT id FROM supervision_runs WHERE trial_id=?", (latest["id"],))
        return {
            "trial_id": trial_id,
            "root_trial_id": root["id"],
            "view": view,
            "nodes": nodes,
            "edges": edges,
            "actions": {
                "can_supervise": latest.get("outcome") is not None and latest_supervision is None,
                "supervise_trial_id": latest["id"] if latest.get("outcome") is not None and latest_supervision is None else None,
                "pending_supervision_id": pending_supervision_id,
            },
        }

    @staticmethod
    def _duration_label(value: Any) -> str:
        if value is None:
            return "耗时 -"
        return f"{float(value) / 1000:.2f}s"

    @staticmethod
    def _trace_event_type(event_type: str) -> str:
        if event_type in {"tool_call", "tool_call_started"}:
            return "tool_call"
        if event_type in {"tool_result", "tool_call_finished"}:
            return "tool_result"
        if event_type == "assistant_final":
            return "assistant_final"
        if event_type in {"error", "warning"}:
            return "error"
        return "model_generation" if event_type.startswith("model_") else "event"

    @staticmethod
    def _trace_event_label(event_type: str, payload: dict[str, Any]) -> str:
        if event_type in {"tool_call", "tool_call_started"}:
            return f"工具调用 · {payload.get('name') or 'unknown'}"
        if event_type in {"tool_result", "tool_call_finished"}:
            return f"工具结果 · {payload.get('name') or 'unknown'}"
        labels = {"assistant_final": "最终答案", "correction_feedback_received": "收到人工反馈", "session_started": "Session 创建"}
        return labels.get(event_type, event_type.replace("_", " "))

    @staticmethod
    def _trace_event_summary(event_type: str, payload: dict[str, Any]) -> str:
        if event_type in {"tool_call", "tool_call_started"}:
            value = payload.get("arguments") or payload.get("input") or {}
        elif event_type in {"tool_result", "tool_call_finished"}:
            value = payload.get("result") if "result" in payload else payload
        elif event_type == "assistant_final":
            value = payload
        else:
            value = payload.get("message") or payload.get("feedback") or payload
        text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value or "")
        return text[:180]

    @staticmethod
    def _trace_subset(actual: Any, expected: Any) -> bool:
        if isinstance(expected, dict):
            return isinstance(actual, dict) and all(key in actual and EvaluationService._trace_subset(actual[key], value) for key, value in expected.items())
        if isinstance(expected, list):
            return isinstance(actual, list) and len(actual) == len(expected) and all(EvaluationService._trace_subset(item, value) for item, value in zip(actual, expected))
        return actual == expected

    @classmethod
    def _trace_event_semantics(cls, event_type: str, payload: dict[str, Any], task: dict[str, Any], attempt: dict[str, Any]) -> dict[str, str]:
        """Keep runtime completion separate from whether an event obeys Task rules."""
        configs = [spec.get("config") or {} for spec in task.get("graders") or [] if str(spec.get("type") or "rule") == "rule"]
        tool_name = str(payload.get("name") or "")
        if event_type in {"tool_call", "tool_call_started", "tool_result", "tool_call_finished"}:
            for config in configs:
                if tool_name and tool_name in {str(item) for item in config.get("forbidden_tools") or []}:
                    return {"semantic_status": "violation", "semantic_reason": f"本题禁止调用工具：{tool_name}"}
            if event_type in {"tool_call", "tool_call_started"}:
                arguments = payload.get("arguments") or payload.get("input") or {}
                for config in configs:
                    for expected_call in config.get("tool_arguments") or []:
                        if str(expected_call.get("name") or "") == tool_name and not cls._trace_subset(arguments, expected_call.get("arguments") or {}):
                            return {"semantic_status": "violation", "semantic_reason": f"工具参数不符合约定：{tool_name}"}
            required = {str(item) for config in configs for item in config.get("required_tools") or []}
            return {"semantic_status": "verified" if tool_name in required else "observed", "semantic_reason": ""}
        if event_type == "assistant_final":
            value = payload.get("content") or payload.get("message") or ""
            text = str(value)
            for config in configs:
                if any(str(item) not in text for item in config.get("contains") or []):
                    return {"semantic_status": "violation", "semantic_reason": "最终答案不满足输出要求"}
                if any(str(item) in text for item in config.get("excludes") or []):
                    return {"semantic_status": "violation", "semantic_reason": "最终答案包含禁止内容"}
            return {"semantic_status": "verified" if attempt.get("outcome") == "pass" else "observed", "semantic_reason": ""}
        return {"semantic_status": "observed", "semantic_reason": ""}

    def _refresh_job_review_status(self, job_id: str) -> None:
        human = self.database.one("SELECT COUNT(*) AS count FROM reviews WHERE trial_id IN (SELECT id FROM trials WHERE job_id=?) AND status!='submitted'", (job_id,))
        corrections = self.database.one("SELECT COUNT(*) AS count FROM supervision_runs WHERE trial_id IN (SELECT id FROM trials WHERE job_id=?) AND status='pending_review'", (job_id,))
        pending = int((human or {}).get("count") or 0) + int((corrections or {}).get("count") or 0)
        unfinished = self.database.one(
            "SELECT COUNT(*) AS count FROM trials WHERE job_id=? AND execution_status NOT IN ('completed','failed','canceled')",
            (job_id,),
        )
        if pending:
            status = "review_pending"
            finished_at = None
        elif int((unfinished or {}).get("count") or 0) > 0:
            current = self.database.one("SELECT status FROM jobs WHERE id=?", (job_id,)) or {}
            status = current.get("status") if current.get("status") in {"queued", "running", "grading"} else "queued"
            finished_at = None
        else:
            status = "completed"
            finished_at = utc_now()
        self.database.update("jobs", job_id, {"status": status, "finished_at": finished_at, "updated_at": utc_now()})

    # Human review
    def list_reviews(self, actor: Actor, *, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM reviews WHERE tenant_id=? AND project_id=?"
        parameters: list[Any] = [actor.tenant_id, actor.project_id]
        if status:
            sql += " AND status=?"
            parameters.append(status)
        return self.database.all(sql + " ORDER BY created_at DESC", parameters)

    def submit_review(self, actor: Actor, review_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        require_role(actor.role, ADMIN_ROLES | {"project_operator", "reviewer"})
        review = self._get("reviews", review_id, actor)
        if review["status"] == "submitted":
            raise EvalError("invalid_state", "该评审已经提交", status=409)
        score = float(payload.get("score"))
        if not 0 <= score <= 100:
            raise EvalError("invalid_review", "人工评分必须在 0 到 100 之间")
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            raise EvalError("invalid_review", "人工评审必须填写理由")
        self.database.update(
            "reviews",
            review_id,
            {
                "status": "submitted",
                "reviewer_id": actor.actor_id,
                "score": score,
                "verdict": str(payload.get("verdict") or ("pass" if score >= 80 else "unresolved")),
                "issue_types_json": list(payload.get("issue_types") or []),
                "evidence_refs_json": list(payload.get("evidence_refs") or []),
                "reason": reason,
                "submitted_at": utc_now(),
            },
        )
        trial = self._get("trials", review["trial_id"], actor)
        pending = self.database.one("SELECT COUNT(*) AS count FROM reviews WHERE trial_id IN (SELECT id FROM trials WHERE job_id=?) AND status!='submitted'", (trial["job_id"],))
        if pending and int(pending["count"]) == 0:
            self._refresh_job_review_status(trial["job_id"])
            self._generate_report(trial["job_id"])
        self._audit(actor, "review.submit", "review", review_id, {"trial_id": review["trial_id"]})
        return self._get("reviews", review_id, actor)

    # Reports and audit
    def get_report(self, actor: Actor, job_id: str) -> dict[str, Any]:
        self._get("jobs", job_id, actor)
        report = self.database.one("SELECT * FROM reports WHERE job_id=? ORDER BY created_at DESC LIMIT 1", (job_id,))
        if report is None:
            raise EvalError("not_found", "报告尚未生成", status=404)
        return report["report"]

    def export_report(self, actor: Actor, job_id: str, export_format: str) -> tuple[str, bytes]:
        report = self.get_report(actor, job_id)
        self._audit(actor, "report.export", "job", job_id, {"format": export_format})
        if export_format == "json":
            return "application/json; charset=utf-8", (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if export_format == "jsonl":
            return "application/x-ndjson; charset=utf-8", "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in report["trials"]).encode("utf-8")
        if export_format == "csv":
            output = io.StringIO()
            fields = ["trial_id", "task_id", "suite", "agent_snapshot_id", "repetition", "attempt", "outcome", "score", "failure_type", "duration_ms"]
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            for trial in report["trials"]:
                writer.writerow({key: trial.get(key) for key in fields})
            return "text/csv; charset=utf-8", output.getvalue().encode("utf-8-sig")
        if export_format == "markdown":
            return "text/markdown; charset=utf-8", self._report_markdown(report).encode("utf-8")
        raise EvalError("unsupported_format", f"不支持的导出格式：{export_format}")

    def list_audit(self, actor: Actor, *, limit: int = 100) -> list[dict[str, Any]]:
        require_role(actor.role, ADMIN_ROLES)
        return self.database.all("SELECT * FROM audit_events WHERE tenant_id=? AND project_id=? ORDER BY created_at DESC LIMIT ?", (actor.tenant_id, actor.project_id, max(1, min(limit, 500))))

    def dashboard(self, actor: Actor) -> dict[str, Any]:
        jobs = self.list_jobs(actor)
        trials = self.list_trials(actor)
        reviews = self.list_reviews(actor, status="pending")
        correction_reviews = self.list_supervisions(actor, status="pending_review")
        completed = [job for job in jobs if job["status"] in {"completed", "review_pending"}]
        return {
            "counts": {
                "benchmarks": len(self.list_benchmarks(actor)),
                "snapshots": len(self.list_snapshots(actor)),
                "jobs": len(jobs),
                "running_jobs": sum(job["status"] in {"queued", "running", "grading"} for job in jobs),
                "pending_reviews": len(reviews) + len(correction_reviews),
                "trials": len(trials),
            },
            "recent_jobs": jobs[:8],
            "latest_conclusion": completed[0]["conclusion"] if completed else None,
        }

    def ensure_demo_data(self, actor: Actor) -> dict[str, Any]:
        created = False
        benchmarks = self.list_benchmarks(actor)
        by_package_id: dict[str, dict[str, Any]] = {}
        for package_id in ("mmlu-high-school-computer-science-smoke",):
            package_path = self.project_root / "benchmarks" / package_id
            package = load_package(package_path)
            version = package["manifest"]["version"]
            current = next(
                (
                    item
                    for item in benchmarks
                    if item["package"]["manifest"]["id"] == package_id
                    and item["package"]["manifest"]["version"] == version
                ),
                None,
            )
            if current is None:
                current = self.create_benchmark(actor, package, publish=True)
                created = True
            by_package_id[package_id] = current

        snapshots = self.list_snapshots(actor)
        by_snapshot_key = {(item["adapter_type"], item["version"]): item for item in snapshots}
        default_snapshots: dict[str, dict[str, Any]] = {}
        defaults = [
            (
                "llm-supervisor",
                "LLM Supervisor Agent",
                "1.3.2",
                "独立监督 Agent；结构化建议由人工审核后才用于纠错重试",
                {
                    "base_url": "https://api.example.com/v1",
                    "chat_path": "/chat/completions",
                    "model": "GLM-5.3",
                    "api_key_env": "EVAL_SUPERVISOR_API_KEY",
                    "timeout_seconds": 360,
                    "thinking": "enabled",
                    "budget": {"max_output_tokens": 32768},
                },
            ),
            (
                "dsh-headless",
                "DeepSeek Harness Headless Agent",
                "1.2.0",
                "使用本机 DeepSeek Harness 源码的真实 DSH Headless；每个 Trial 保存可导入的独立会话轨迹",
                {
                    "command": [os.environ.get("DSH_COMMAND", "dsh"), "--profile", "headless", "--patch", "{patch_file}", "{instruction}"],
                    "health_command": [os.environ.get("DSH_COMMAND", "dsh"), "--profile", "headless", "--help"],
                    "health_timeout_seconds": 30,
                    "working_directory": os.environ.get("DSH_WORKING_DIRECTORY", str(self.project_root)),
                    "session_root": ".data/dsh-sessions",
                    "patch_file": "examples/dsh-eval-session.patch.yml",
                    "permission_mode": "read-only",
                },
            ),
        ]
        for adapter_type, name, version, description, adapter_config in defaults:
            snapshot_key = (adapter_type, version)
            if snapshot_key not in by_snapshot_key:
                by_snapshot_key[snapshot_key] = self.create_snapshot(actor, {"name": name, "version": version, "adapter_type": adapter_type, "config": {"description": description, **adapter_config}})
                created = True
            default_snapshots[adapter_type] = by_snapshot_key[snapshot_key]
        return {
            "created": created,
            "benchmark_id": by_package_id["mmlu-high-school-computer-science-smoke"]["id"],
            "benchmark_ids": {key: value["id"] for key, value in by_package_id.items()},
            "snapshot_ids": [default_snapshots[key]["id"] for key in ("llm-supervisor", "dsh-headless")],
        }

    def recover_jobs(self) -> list[str]:
        jobs = self.database.all("SELECT * FROM jobs WHERE status IN ('queued','running','grading','canceling')")
        recovered = []
        for job in jobs:
            if job["cancel_requested"]:
                self.database.update("jobs", job["id"], {"status": "canceled", "finished_at": utc_now(), "updated_at": utc_now()})
            else:
                self.database.update("jobs", job["id"], {"status": "queued", "updated_at": utc_now()})
                self._spawn_job(job["id"])
                recovered.append(job["id"])
        return recovered

    # Internal execution helpers
    @staticmethod
    def _with_snapshot_capabilities(snapshot: dict[str, Any]) -> dict[str, Any]:
        return {**snapshot, "capabilities": snapshot_capabilities(snapshot)}

    def _resolve_snapshots(self, actor: Actor, snapshot_ids: list[str]) -> list[dict[str, Any]]:
        return [self._with_snapshot_capabilities(self._get("agent_snapshots", str(snapshot_id), actor)) for snapshot_id in snapshot_ids]

    @staticmethod
    def _select_tasks(tasks: list[dict[str, Any]], task_filter: dict[str, Any]) -> list[dict[str, Any]]:
        selected = list(tasks)
        if task_filter.get("task_ids"):
            wanted = set(task_filter["task_ids"])
            selected = [task for task in selected if task["id"] in wanted]
            missing = wanted - {task["id"] for task in selected}
            if missing:
                raise EvalError("unknown_task", f"未知 Task：{', '.join(sorted(missing))}")
        if task_filter.get("suites"):
            suites = set(task_filter["suites"])
            selected = [task for task in selected if task["suite"] in suites]
        if task_filter.get("tags"):
            tags = set(task_filter["tags"])
            selected = [task for task in selected if tags.issubset(set(task.get("tags") or []))]
        return selected

    def _registered_graders(self, actor: Actor) -> dict[str, dict[str, Any]]:
        return {item["id"]: item for item in self.list_graders(actor)}

    def _spawn_job(self, job_id: str) -> None:
        with self._manager_lock:
            current = self._threads.get(job_id)
            if current and current.is_alive():
                return
            event = self._cancel_events.setdefault(job_id, threading.Event())
            event.clear()
            thread = threading.Thread(target=self._run_job, args=(job_id, event), name=f"eval-{job_id}", daemon=True)
            self._threads[job_id] = thread
            thread.start()

    def _expand_trials(self, job: dict[str, Any], benchmark: dict[str, Any]) -> list[dict[str, Any]]:
        existing = self.database.all("SELECT * FROM trials WHERE job_id=?", (job["id"],))
        if existing:
            return existing
        config = job["config"]
        tasks = self._select_tasks(benchmark["package"]["tasks"], config.get("task_filter") or {})
        repetitions = int((config.get("execution") or {}).get("repetitions", 1))
        now = utc_now()
        for snapshot_id in config["agent_snapshot_ids"]:
            for task in tasks:
                for repetition in range(1, repetitions + 1):
                    self.database.insert(
                        "trials",
                        {
                            "id": new_id("trial"),
                            "tenant_id": job["tenant_id"],
                            "project_id": job["project_id"],
                            "job_id": job["id"],
                            "task_id": task["id"],
                            "suite": task["suite"],
                            "agent_snapshot_id": snapshot_id,
                            "repetition": repetition,
                            "attempt": 1,
                            "parent_attempt_id": None,
                            "execution_status": "pending",
                            "outcome": None,
                            "score": None,
                            "threshold": float(task.get("pass_threshold", benchmark["package"]["manifest"].get("pass_threshold", 80))),
                            "failure_stage": None,
                            "failure_type": None,
                            "evidence_complete": 0,
                            "agent_run_json": None,
                            "grades_json": None,
                            "usage_json": None,
                            "error_json": None,
                            "created_at": now,
                            "started_at": None,
                            "finished_at": None,
                            "updated_at": now,
                        },
                    )
        trials = self.database.all("SELECT * FROM trials WHERE job_id=? ORDER BY created_at, id", (job["id"],))
        self.database.update("jobs", job["id"], {"progress_total": len(trials), "updated_at": utc_now()})
        return trials

    def _run_job(self, job_id: str, cancel_event: threading.Event) -> None:
        try:
            job = self.database.one("SELECT * FROM jobs WHERE id=?", (job_id,))
            if job is None:
                return
            benchmark = self.database.one("SELECT * FROM benchmarks WHERE id=?", (job["benchmark_id"],))
            if benchmark is None:
                raise EvalError("not_found", "Job 绑定的 Benchmark 不存在")
            self.database.update("jobs", job_id, {"status": "running", "started_at": job.get("started_at") or utc_now(), "updated_at": utc_now()})
            trials = self._expand_trials(job, benchmark)
            pending = [trial for trial in trials if trial["execution_status"] in {"pending", "environment_preparing", "agent_running", "collecting", "auto_grading"}]
            max_workers = int(job["config"]["execution"].get("max_concurrency", 4))
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="trial") as executor:
                futures = [executor.submit(self._execute_trial, job_id, trial["id"], benchmark, cancel_event) for trial in pending]
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception:
                        # _execute_trial persists its own failure; one Trial must not stop the Job.
                        pass
                    completed = self.database.one("SELECT COUNT(*) AS count FROM trials WHERE job_id=? AND execution_status IN ('completed','failed','canceled')", (job_id,))
                    self.database.update("jobs", job_id, {"progress_completed": int(completed["count"] if completed else 0), "updated_at": utc_now()})
            current = self.database.one("SELECT * FROM jobs WHERE id=?", (job_id,))
            if current and current["cancel_requested"]:
                self.database.execute("UPDATE trials SET execution_status='canceled', outcome=NULL, finished_at=?, updated_at=? WHERE job_id=? AND execution_status NOT IN ('completed','failed','canceled')", (utc_now(), utc_now(), job_id))
                self.database.update("jobs", job_id, {"status": "canceled", "finished_at": utc_now(), "updated_at": utc_now()})
                self._generate_report(job_id)
                return
            self.database.update("jobs", job_id, {"status": "grading", "updated_at": utc_now()})
            report = self._generate_report(job_id)
            pending_reviews = self.database.one("SELECT COUNT(*) AS count FROM reviews WHERE trial_id IN (SELECT id FROM trials WHERE job_id=?) AND status!='submitted'", (job_id,))
            pending_corrections = self.database.one("SELECT COUNT(*) AS count FROM supervision_runs WHERE trial_id IN (SELECT id FROM trials WHERE job_id=?) AND status='pending_review'", (job_id,))
            has_pending_review = int((pending_reviews or {}).get("count") or 0) + int((pending_corrections or {}).get("count") or 0) > 0
            final_status = "review_pending" if has_pending_review else "completed"
            self.database.update("jobs", job_id, {"status": final_status, "conclusion": report["conclusion"], "finished_at": utc_now() if final_status == "completed" else None, "updated_at": utc_now()})
        except Exception as exc:
            self.database.update("jobs", job_id, {"status": "failed", "error_json": {"type": type(exc).__name__, "message": str(exc)}, "finished_at": utc_now(), "updated_at": utc_now()})

    def _wait_if_paused(self, job_id: str, cancel_event: threading.Event) -> None:
        while not cancel_event.is_set():
            job = self.database.one("SELECT pause_requested,cancel_requested FROM jobs WHERE id=?", (job_id,))
            if not job or not job["pause_requested"]:
                return
            time.sleep(0.1)

    def _execute_trial(self, job_id: str, trial_id: str, benchmark: dict[str, Any], cancel_event: threading.Event) -> None:
        trial = self.database.one("SELECT * FROM trials WHERE id=?", (trial_id,))
        if trial is None:
            return
        self._wait_if_paused(job_id, cancel_event)
        if cancel_event.is_set():
            self.database.update("trials", trial_id, {"execution_status": "canceled", "finished_at": utc_now(), "updated_at": utc_now()})
            return
        task = next(item for item in benchmark["package"]["tasks"] if item["id"] == trial["task_id"])
        correction = self.database.one("SELECT * FROM correction_decisions WHERE child_trial_id=?", (trial_id,))
        if correction and correction.get("approved_feedback"):
            task = {
                **task,
                "instruction": (
                    f"{task['instruction']}\n\n"
                    "人工审核已批准以下纠错反馈。请重新独立完成原始任务，不要假设未观察到的信息：\n"
                    f"{correction['approved_feedback']}"
                ),
                "correction": {
                    "feedback": correction["approved_feedback"],
                    "parent_attempt_id": trial.get("parent_attempt_id"),
                    "decision_id": correction["id"],
                },
            }
        snapshot = self.database.one("SELECT * FROM agent_snapshots WHERE id=?", (trial["agent_snapshot_id"],))
        if snapshot is None:
            self.database.update("trials", trial_id, {"execution_status": "failed", "outcome": "infra_failed", "failure_stage": "environment_preparing", "failure_type": "snapshot_missing", "error_json": {"message": "AgentSnapshot 不存在"}, "finished_at": utc_now(), "updated_at": utc_now()})
            return
        started_at = utc_now()
        self.database.update("trials", trial_id, {"execution_status": "environment_preparing", "started_at": started_at, "updated_at": started_at})
        try:
            adapter = get_adapter(snapshot["adapter_type"])
            self.database.update("trials", trial_id, {"execution_status": "agent_running", "updated_at": utc_now()})
            run = adapter.run(snapshot=snapshot, task=task, fixture=benchmark["package"].get("fixture") or {}, cancel_event=cancel_event)
            environment_spec = task.get("environment") or {"type": "fixture", "snapshot": "local"}
            run["environment_snapshot"] = {
                "id": "esnap-" + content_hash(environment_spec).split(":", 1)[1][:12],
                "content_hash": content_hash(environment_spec),
                "spec": environment_spec,
            }
            self.database.update("trials", trial_id, {"execution_status": "collecting", "updated_at": utc_now()})
            artifact = self._write_artifact(trial, "agent_run", run)
            run["artifacts"] = list(run.get("artifacts") or []) + [artifact["id"]]
            self.database.update("trials", trial_id, {"execution_status": "auto_grading", "agent_run_json": run, "usage_json": run.get("usage") or {}, "evidence_complete": bool(run.get("events") is not None) and not bool((run.get('trajectory') or {}).get('parse_errors')), "updated_at": utc_now()})
            actor = Actor(trial["tenant_id"], trial["project_id"], "scheduler", "project_admin")
            result = grade_trial(
                task=task,
                run=run,
                fixture=benchmark["package"].get("fixture") or {},
                registered_graders=self._registered_graders(actor),
                executable_root=Path(os.environ["AGENT_EVAL_GRADER_ROOT"]) if os.environ.get("AGENT_EVAL_GRADER_ROOT") else None,
                enable_executable=os.environ.get("AGENT_EVAL_ENABLE_EXECUTABLE_GRADERS") == "1",
            )
            self.database.update(
                "trials",
                trial_id,
                {
                    "execution_status": "failed" if result['outcome'] in {'grader_failed','infra_failed','agent_failed'} else "completed",
                    "outcome": result["outcome"],
                    "score": result["score"],
                    "failure_stage": result.get('failure_stage') or ("auto_grading" if result["outcome"] in {'grader_failed','infra_failed'} else None),
                    "failure_type": result["failure_type"],
                    'error_json': ({'execution_issue': run['official_verification'].get('execution_issue'),
                                    'verification_issue': run['official_verification'].get('error_code'),
                                    'raw_reward': run['official_verification'].get('raw_reward'),
                                    'message': run['official_verification'].get('error')}
                                   if run.get('official_verification') and (run['official_verification'].get('execution_issue') or run['official_verification'].get('error_code')) else None),
                    "grades_json": result["grades"],
                    "finished_at": utc_now(),
                    "updated_at": utc_now(),
                },
            )
            sample_rate = float((self.database.one("SELECT * FROM jobs WHERE id=?", (job_id,)) or {}).get("config", {}).get("report_policy", {}).get("human_sample_rate", 0))
            deterministic_sample = int(content_hash(trial_id)[-8:], 16) / 0xFFFFFFFF
            if result["needs_review"] or deterministic_sample < sample_rate:
                self._create_review(trial_id, trial, rubric_version="human-default-v1")
        except AdapterFailure as exc:
            if exc.failure_type == "canceled":
                changes = {"execution_status": "canceled", "outcome": None}
            elif exc.stage == "environment_preparing" or exc.failure_type in {"provider_rate_limited", "provider_unavailable"}:
                changes = {"execution_status": "failed", "outcome": "infra_failed", "score": None}
            else:
                changes = {"execution_status": "failed", "outcome": "agent_failed", "score": 0.0}
            self.database.update("trials", trial_id, {**changes, "failure_stage": exc.stage, "failure_type": exc.failure_type, "error_json": {"message": str(exc)}, "finished_at": utc_now(), "updated_at": utc_now()})
        except Exception as exc:
            self.database.update("trials", trial_id, {"execution_status": "failed", "outcome": "infra_failed", "score": None, "failure_stage": "collecting", "failure_type": "runner_lost", "error_json": {"type": type(exc).__name__, "message": str(exc)}, "finished_at": utc_now(), "updated_at": utc_now()})

    def _write_artifact(self, trial: dict[str, Any], artifact_type: str, value: Any) -> dict[str, Any]:
        artifact_id = new_id("artifact")
        safe_value = redact(value)
        content = (json.dumps(safe_value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        trial_dir = self.artifact_root / trial["tenant_id"] / trial["project_id"] / trial["id"]
        trial_dir.mkdir(parents=True, exist_ok=True)
        path = trial_dir / f"{artifact_id}.json"
        path.write_bytes(content)
        item = {
            "id": artifact_id,
            "tenant_id": trial["tenant_id"],
            "project_id": trial["project_id"],
            "trial_id": trial["id"],
            "artifact_type": artifact_type,
            "media_type": "application/json",
            "size_bytes": len(content),
            "content_hash": content_hash(safe_value),
            "storage_path": str(path.relative_to(self.data_dir)).replace("\\", "/"),
            "metadata_json": {"redacted": True},
            "created_at": utc_now(),
        }
        self.database.insert("artifacts", item)
        return {**item, "metadata": item.pop("metadata_json")}

    def _create_review(self, trial_id: str, trial: dict[str, Any], *, rubric_version: str) -> None:
        if self.database.one("SELECT id FROM reviews WHERE trial_id=? AND status!='submitted'", (trial_id,)):
            return
        self.database.insert(
            "reviews",
            {
                "id": new_id("review"),
                "tenant_id": trial["tenant_id"],
                "project_id": trial["project_id"],
                "trial_id": trial_id,
                "status": "pending",
                "reviewer_id": None,
                "rubric_version": rubric_version,
                "score": None,
                "verdict": None,
                "issue_types_json": [],
                "evidence_refs_json": [],
                "reason": None,
                "blind": 1,
                "created_at": utc_now(),
                "submitted_at": None,
            },
        )

    def _generate_report(self, job_id: str) -> dict[str, Any]:
        job = self.database.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if job is None:
            raise EvalError("not_found", "Job 不存在", status=404)
        benchmark = self.database.one("SELECT * FROM benchmarks WHERE id=?", (job["benchmark_id"],))
        trials = self.database.all("SELECT * FROM trials WHERE job_id=? ORDER BY created_at, id", (job_id,))
        primary_trials = [trial for trial in trials if int(trial.get("attempt") or 1) == 1]
        snapshots = {item["id"]: item for item in self.database.all("SELECT * FROM agent_snapshots WHERE id IN (SELECT DISTINCT agent_snapshot_id FROM trials WHERE job_id=?)", (job_id,))}
        counts = {outcome: sum(trial.get("outcome") == outcome for trial in primary_trials) for outcome in ("pass", "unresolved", "hard_fail", "agent_failed", "infra_failed", "grader_failed")}
        valid_outcomes = {"pass", "unresolved", "hard_fail", "agent_failed"}
        valid = [trial for trial in primary_trials if trial.get("outcome") in valid_outcomes]
        invalid = [trial for trial in primary_trials if trial.get("outcome") in {"infra_failed", "grader_failed"}]
        scores = [float(trial["score"]) for trial in valid if trial.get("score") is not None]
        durations = [float((trial.get("usage") or {}).get("duration_ms")) for trial in primary_trials if (trial.get("usage") or {}).get("duration_ms") is not None]
        pass_rate = counts["pass"] / len(valid) if valid else 0.0
        hard_failure_rate = counts["hard_fail"] / len(valid) if valid else 0.0
        invalid_rate = len(invalid) / len(primary_trials) if primary_trials else 0.0
        groups: list[dict[str, Any]] = []
        for snapshot_id in sorted(snapshots):
            subset = [trial for trial in valid if trial["agent_snapshot_id"] == snapshot_id]
            subset_scores = [float(trial["score"]) for trial in subset if trial.get("score") is not None]
            groups.append({"agent_snapshot_id": snapshot_id, "agent": snapshots[snapshot_id]["name"], "sample_size": len(subset), "passed": sum(trial["outcome"] == "pass" for trial in subset), "pass_rate": round(sum(trial["outcome"] == "pass" for trial in subset) / len(subset), 4) if subset else None, "mean_score": round(mean(subset_scores), 2) if subset_scores else None, "score_stddev": round(pstdev(subset_scores), 3) if len(subset_scores) > 1 else 0.0 if subset_scores else None})
        suite_groups = []
        for suite in sorted({trial["suite"] for trial in primary_trials}):
            subset = [trial for trial in valid if trial["suite"] == suite]
            subset_scores = [float(trial["score"]) for trial in subset if trial.get("score") is not None]
            suite_groups.append({"suite": suite, "sample_size": len(subset), "pass_rate": round(sum(trial["outcome"] == "pass" for trial in subset) / len(subset), 4) if subset else None, "mean_score": round(mean(subset_scores), 2) if subset_scores else None})
        pending_reviews = self.database.one("SELECT COUNT(*) AS count FROM reviews WHERE trial_id IN (SELECT id FROM trials WHERE job_id=?) AND status!='submitted'", (job_id,))
        pending_corrections = self.database.one("SELECT COUNT(*) AS count FROM supervision_runs WHERE trial_id IN (SELECT id FROM trials WHERE job_id=?) AND status='pending_review'", (job_id,))
        pending_review_count = int((pending_reviews or {}).get("count") or 0) + int((pending_corrections or {}).get("count") or 0)
        gate = job["config"].get("gate") or {}
        invalid_limit = float(job["config"].get("report_policy", {}).get("invalid_rate_limit", 0.2))
        if invalid_rate > invalid_limit:
            conclusion = "invalid"
            reasons = [f"无效 Trial 比例 {invalid_rate:.1%} 超过 {invalid_limit:.1%}"]
        elif counts["hard_fail"] > int(gate.get("max_hard_failures", 0)):
            conclusion = "blocked"
            reasons = [f"硬失败 {counts['hard_fail']} 次"]
        elif pass_rate < float(gate.get("min_pass_rate", 0.8)):
            conclusion = "blocked"
            reasons = [f"通过率 {pass_rate:.1%} 低于 {float(gate.get('min_pass_rate', 0.8)):.1%}"]
        elif pending_review_count > 0:
            conclusion = "review_required"
            reasons = [f"仍有 {pending_review_count} 条人工评审待完成"]
        else:
            conclusion = "passed"
            reasons = ["满足当前发布门槛"]
        manifest = (benchmark.get("package") or {}).get("manifest") or {}
        benchmark_type = str(manifest.get("benchmark_type") or "general")
        scoped_failures = {
            "platform": "platform_check_failed",
            "conformance": "conformance_failed",
            "general": "general_below_gate",
            "domain": "domain_below_gate",
            "regression": "regression_blocked",
        }
        gate_conclusion = conclusion
        if conclusion == "blocked":
            conclusion = scoped_failures.get(benchmark_type, "general_below_gate")
        interpretations = {
            "platform": "该结论只反映评测平台数据链路是否正常，不代表 Agent 能力。",
            "conformance": "该结论只反映 Agent 对统一 Runtime、工具与可观测协议的符合程度，不代表领域业务能力。",
            "general": "该结论反映可复用基础能力，不替代具体业务场景评测。",
            "domain": f"该结论只适用于 {manifest.get('domain') or '当前领域'} 场景及本 Benchmark 版本。",
            "regression": "该结论用于已知生产行为回归，不作为跨领域通用排名。",
        }
        interpretation = interpretations.get(benchmark_type, interpretations["general"])
        compatibility = job["config"].get("compatibility")
        if compatibility and not compatibility.get("compatible", True):
            interpretation += " 本 Job 以 allow 模式运行了不兼容组合，失败项包含协议缺失，不能解释为模型语义能力不足。"
        baseline_id = job["config"].get("baseline_agent_snapshot_id")
        baseline = next((item for item in groups if item["agent_snapshot_id"] == baseline_id), None)
        comparisons = []
        if baseline:
            for item in groups:
                if item is baseline:
                    continue
                comparisons.append({"baseline_agent_snapshot_id": baseline_id, "candidate_agent_snapshot_id": item["agent_snapshot_id"], "pass_rate_delta": round((item["pass_rate"] or 0) - (baseline["pass_rate"] or 0), 4), "mean_score_delta": round((item["mean_score"] or 0) - (baseline["mean_score"] or 0), 2), "sample_size": item["sample_size"]})
        chains: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
        for trial in trials:
            chains.setdefault((trial["task_id"], trial["agent_snapshot_id"], int(trial["repetition"])), []).append(trial)
        corrected_chains = [sorted(chain, key=lambda item: int(item["attempt"])) for chain in chains.values() if len(chain) > 1]
        deltas = [float(chain[-1]["score"]) - float(chain[0]["score"]) for chain in corrected_chains if chain[0].get("score") is not None and chain[-1].get("score") is not None]
        supervisions = self.database.all("SELECT * FROM supervision_runs WHERE trial_id IN (SELECT id FROM trials WHERE job_id=?) ORDER BY created_at", (job_id,))
        decisions = self.database.all("SELECT * FROM correction_decisions WHERE supervision_run_id IN (SELECT id FROM supervision_runs WHERE trial_id IN (SELECT id FROM trials WHERE job_id=?)) ORDER BY created_at", (job_id,))
        decisions_by_supervision = {item["supervision_run_id"]: item for item in decisions}
        primary_by_id = {trial["id"]: trial for trial in primary_trials}
        supervised_primary = [item for item in supervisions if item["trial_id"] in primary_by_id]
        actual_failures = [item for item in supervised_primary if primary_by_id[item["trial_id"]].get("outcome") != "pass"]
        false_positives = [item for item in supervised_primary if primary_by_id[item["trial_id"]].get("outcome") == "pass" and item.get("verdict") != "pass"]
        supervisor_usages = [item.get("usage") or {} for item in supervisions]
        correction_summary = {
            "corrected_tasks": len(corrected_chains),
            "fixed_tasks": sum(chain[0].get("outcome") != "pass" and chain[-1].get("outcome") == "pass" for chain in corrected_chains),
            "improved_tasks": sum(delta > 0 for delta in deltas),
            "mean_score_delta": round(mean(deltas), 2) if deltas else None,
            "fix_rate": round(sum(chain[0].get("outcome") != "pass" and chain[-1].get("outcome") == "pass" for chain in corrected_chains) / len(corrected_chains), 4) if corrected_chains else None,
            "supervised_tasks": len(supervised_primary),
            "supervisor_detection_rate": round(sum(item.get("verdict") != "pass" for item in actual_failures) / len(actual_failures), 4) if actual_failures else None,
            "supervisor_false_positive_rate": round(len(false_positives) / sum(primary_by_id[item["trial_id"]].get("outcome") == "pass" for item in supervised_primary), 4) if any(primary_by_id[item["trial_id"]].get("outcome") == "pass" for item in supervised_primary) else None,
            "human_decisions": {decision: sum(item.get("decision") == decision for item in decisions) for decision in ("approve_retry", "reject_feedback", "accept_final", "terminate")},
            "supervisor_latency_ms": {
                "mean": round(mean([float(item["duration_ms"]) for item in supervisor_usages if item.get("duration_ms") is not None]), 3) if any(item.get("duration_ms") is not None for item in supervisor_usages) else None,
                "p95": percentile([float(item["duration_ms"]) for item in supervisor_usages if item.get("duration_ms") is not None], 0.95),
            },
            "supervisor_tokens": {
                "input": sum(int(item.get("input_tokens") or 0) for item in supervisor_usages),
                "output": sum(int(item.get("output_tokens") or 0) for item in supervisor_usages),
            },
            "execution_cost": {
                "attempts": len(trials),
                "duration_ms": round(sum(float((trial.get("usage") or {}).get("duration_ms") or 0) for trial in trials), 3),
                "input_tokens": sum(int((trial.get("usage") or {}).get("input_tokens") or 0) for trial in trials),
                "output_tokens": sum(int((trial.get("usage") or {}).get("output_tokens") or 0) for trial in trials),
                "tool_calls": sum(int((trial.get("usage") or {}).get("tool_calls") or 0) for trial in trials),
            },
        }
        report_trials = [{"trial_id": trial["id"], "task_id": trial["task_id"], "suite": trial["suite"], "agent_snapshot_id": trial["agent_snapshot_id"], "repetition": trial["repetition"], "attempt": trial["attempt"], "execution_status": trial["execution_status"], "outcome": trial.get("outcome"), "score": trial.get("score"), "failure_type": trial.get("failure_type"), "evidence_complete": trial["evidence_complete"], "duration_ms": (trial.get("usage") or {}).get("duration_ms"), "artifact_ids": [item["id"] for item in self.database.all("SELECT id FROM artifacts WHERE trial_id=?", (trial["id"],))]} for trial in trials]
        report = {
            "schema_version": "1.2.0",
            "report_id": new_id("report"),
            "job": {"id": job_id, "name": job["name"], "benchmark_id": job["benchmark_id"], "created_at": job["created_at"]},
            "benchmark": {"name": benchmark["name"] if benchmark else None, "version": benchmark["version"] if benchmark else None, "benchmark_type": benchmark_type, "domain": manifest.get("domain"), "content_hash": benchmark["content_hash"] if benchmark else None},
            "summary": {"total_trials": len(primary_trials), "total_attempts": len(trials), "valid_trials": len(valid), "invalid_trials": len(invalid), "counts": counts, "pass_rate": round(pass_rate, 4), "hard_failure_rate": round(hard_failure_rate, 4), "invalid_rate": round(invalid_rate, 4), "mean_score": round(mean(scores), 2) if scores else None, "latency_ms": {"mean": round(mean(durations), 3) if durations else None, "p50": percentile(durations, 0.5), "p95": percentile(durations, 0.95)}},
            "correction_summary": correction_summary,
            "agent_groups": groups,
            "suite_groups": suite_groups,
            "comparisons": comparisons,
            "failure_types": {failure: sum(trial.get("failure_type") == failure for trial in trials) for failure in sorted({trial.get("failure_type") for trial in trials if trial.get("failure_type")})},
            "conclusion": conclusion,
            "gate_conclusion": gate_conclusion,
            "conclusion_reasons": reasons,
            "interpretation": interpretation,
            "compatibility": compatibility,
            "uncertainty": "样本量小于 20，请勿据此宣称统计显著提升" if len(valid) < 20 else None,
            "trials": report_trials,
            "generated_at": utc_now(),
        }
        self.database.insert("reports", {"id": report["report_id"], "tenant_id": job["tenant_id"], "project_id": job["project_id"], "job_id": job_id, "schema_version": "1.2.0", "report_json": report, "created_at": report["generated_at"]})
        self.database.update("jobs", job_id, {"conclusion": conclusion, "updated_at": utc_now()})
        return report

    @staticmethod
    def _report_markdown(report: dict[str, Any]) -> str:
        summary = report["summary"]
        lines = [
            f"# {report['job']['name']} 评测报告",
            "",
            f"- Job: `{report['job']['id']}`",
            f"- Benchmark: `{report['benchmark']['name']}@{report['benchmark']['version']}`",
            f"- Benchmark 类型: `{report['benchmark']['benchmark_type']}`" + (f" / `{report['benchmark']['domain']}`" if report['benchmark'].get('domain') else ""),
            f"- 结论: **{report['conclusion']}**（门禁 `{report.get('gate_conclusion', report['conclusion'])}`；{'；'.join(report['conclusion_reasons'])}）",
            f"- Trials: {summary['valid_trials']} valid / {summary['total_trials']} total",
            f"- Pass rate (valid scored trials only): {summary['pass_rate']:.1%}",
            f"- Confirmed passes / all tasks: {summary['counts']['pass']}/{summary['total_trials']}",
            f"- Hard failure rate: {summary['hard_failure_rate']:.1%}",
            "",
            f"> {report['interpretation']}",
            "",
            "## Agent 对比",
            "",
            "| Agent | Samples | Passed | Pass Rate | Mean Score | Stddev |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for item in report["agent_groups"]:
            rate = "-" if item["pass_rate"] is None else f"{item['pass_rate']:.1%}"
            lines.append(f"| {item['agent']} | {item['sample_size']} | {item['passed']} | {rate} | {item['mean_score'] if item['mean_score'] is not None else '-'} | {item['score_stddev'] if item['score_stddev'] is not None else '-'} |")
        lines.extend(["", "## Suite", "", "| Suite | Samples | Pass Rate | Mean Score |", "|---|---:|---:|---:|"])
        for item in report["suite_groups"]:
            rate = "-" if item["pass_rate"] is None else f"{item['pass_rate']:.1%}"
            lines.append(f"| {item['suite']} | {item['sample_size']} | {rate} | {item['mean_score'] if item['mean_score'] is not None else '-'} |")
        if report.get("uncertainty"):
            lines.extend(["", f"> {report['uncertainty']}"])
        correction = report.get("correction_summary") or {}
        if correction.get("corrected_tasks"):
            lines.extend(
                [
                    "",
                    "## 人工纠错",
                    "",
                    f"- 进入纠错的 Task: {correction['corrected_tasks']}",
                    f"- 修复成功: {correction['fixed_tasks']}",
                    f"- 分数提升的 Task: {correction['improved_tasks']}",
                    f"- 平均分数变化: {correction['mean_score_delta'] if correction['mean_score_delta'] is not None else '-'}",
                    f"- 修复率: {correction['fix_rate'] if correction.get('fix_rate') is not None else '-'}",
                    f"- Supervisor 检出率: {correction['supervisor_detection_rate'] if correction.get('supervisor_detection_rate') is not None else '-'}",
                    f"- Supervisor 误报率: {correction['supervisor_false_positive_rate'] if correction.get('supervisor_false_positive_rate') is not None else '-'}",
                    f"- Supervisor Token: {correction.get('supervisor_tokens', {}).get('input', 0)} input / {correction.get('supervisor_tokens', {}).get('output', 0)} output",
                    f"- 执行 Agent 工具调用: {correction.get('execution_cost', {}).get('tool_calls', 0)}",
                    "",
                    "> 发布门禁和主通过率仍以 Attempt 1 的首次盲测结果为准。",
                ]
            )
        lines.extend(["", "## Trial 明细", "", "| Trial | Task | Agent Snapshot | Outcome | Score | Failure |", "|---|---|---|---|---:|---|"])
        for trial in report["trials"]:
            lines.append(f"| {trial['trial_id']} | {trial['task_id']} | {trial['agent_snapshot_id']} | {trial['outcome'] or '-'} | {trial['score'] if trial['score'] is not None else '-'} | {trial['failure_type'] or '-'} |")
        return "\n".join(lines) + "\n"
