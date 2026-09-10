"""Register the pinned development suite and run real DSH/Harbor trials in Eval."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_eval.common import content_hash
from agent_eval.service import Actor, EvaluationService
from agent_eval.terminal_bench import INTEGRATION_FILES, task_digest

COMMIT = "7131e4375048a0e408a8fb404b5f499d726b695b"
TASKS = ["openssl-selfsigned-cert", "log-summary-date-ranges", "fix-git", "db-wal-recovery", "cancel-async-tasks"]


def build_package() -> dict:
    repo = ROOT / ".terminal-bench/terminal-bench-2-1"
    actual = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    if actual != COMMIT:
        raise RuntimeError(f"Expected Terminal-Bench commit {COMMIT}; found {actual}")
    tasks = []
    for name in TASKS:
        path = repo / "tasks" / name
        changed = subprocess.check_output(["git", "-C", str(repo), "-c", "core.autocrlf=false", "status", "--porcelain", "--", "tasks/" + name], text=True)
        if changed.strip():
            raise RuntimeError(f"Official task {name} differs from the pinned commit")
        config = tomllib.loads((path / "task.toml").read_text(encoding="utf-8"))
        tasks.append({
            "id": "TB21-" + name, "suite": "terminal-bench-development", "version": "2.1",
            "instruction": (path / "instruction.md").read_text(encoding="utf-8"),
            "tags": config["metadata"].get("tags", []), "pass_threshold": 100,
            "timeout_seconds": 3300,
            "environment": {"type": "docker", **config["environment"]},
            "terminal_bench": {"task_name": name, "task_digest": task_digest(path), "upstream_commit": COMMIT},
            "graders": [{"id": "terminal-bench-official-v1", "type": "terminal_bench", "version": "2.1", "required": True}],
        })
    return {"manifest": {
        "id": "terminal-bench-2.1-dev", "name": "Terminal-Bench 2.1 · DSH 开发集（5题）",
        "version": "2.1.0-dev.1", "benchmark_type": "general", "pass_threshold": 100,
        "source": "https://github.com/harbor-framework/terminal-bench-2-1", "source_commit": COMMIT,
        "description": "5-task integration/development subset; not the full 89-task benchmark or leaderboard result.",
    }, "tasks": tasks, "fixture": {}}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", action="append", choices=TASKS)
    parser.add_argument("--all-dev", action="store_true")
    parser.add_argument("--register-only", action="store_true")
    parser.add_argument("--data-dir", type=Path, default=ROOT / ".data-supervised-loop")
    args = parser.parse_args()
    package = build_package()
    archive = ROOT / ".terminal-bench/runtime-cache/dsh-runtime.tgz"
    if not archive.is_file():
        raise RuntimeError("Prepare dsh-runtime.tgz with scripts/prepare_dsh_runtime.sh first")
    with archive.open("rb") as stream:
        runtime_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    package_dir = ROOT / "benchmarks/terminal-bench-2.1-dev"
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "package.json").write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    service = EvaluationService(args.data_dir)
    actor = Actor()
    from agent_eval.benchmark import validate_package
    package_hash = validate_package(package)["content_hash"]
    benchmark = next((b for b in service.list_benchmarks(actor) if b["content_hash"] == package_hash), None)
    if benchmark is None:
        benchmark = service.create_benchmark(actor, package, publish=True)
    config = {"python": str(ROOT / (".terminal-bench-venv/Scripts/python.exe" if sys.platform == "win32" else ".terminal-bench-venv/bin/python")),
              "dataset_root": str(ROOT / ".terminal-bench/terminal-bench-2-1/tasks"),
              "output_root": str(args.data_dir.resolve() / "terminal-bench"),
              "runner_timeout_seconds": 3300, "harbor_version": "0.22.0", "dsh_version": "0.1.1-rc.2",
              "model": "deepseek-official/deepseek-v4-flash", "permission_boundary": "disposable-task-container",
              "dataset_commit": COMMIT,
              "classification_version": "2.1.0", "process_boundary": "confirmed-container-termination-v2",
              "environment_cleanup": "verified-runtime-resources-v1", "image_cache": "retain-prebuilt-images",
              "runtime_archive_sha256": runtime_hash,
              "integration_files": list(INTEGRATION_FILES),
              "integration_digest": content_hash({name: (ROOT / name).read_text(encoding="utf-8") for name in INTEGRATION_FILES})}
    snapshot = next((s for s in service.list_snapshots(actor) if s["adapter_type"] == "terminal-bench-harbor" and s["config_hash"] == content_hash(config)), None)
    if snapshot is None:
        snapshot = service.create_snapshot(actor, {"name": "DSH · Terminal-Bench / Harbor", "version": "1.2.4-dev.1",
                                                  "adapter_type": "terminal-bench-harbor", "config": config})
    registry = {"benchmark_id": benchmark["id"], "agent_snapshot_id": snapshot["id"],
                "source_commit": COMMIT, "task_names": TASKS}
    registry_path = args.data_dir / "terminal-bench-registration.json"
    registry_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(registry, ensure_ascii=False), flush=True)
    if args.register_only:
        return
    selected = TASKS if args.all_dev else args.task or [TASKS[0]]
    supervisors = [s for s in service.list_snapshots(actor) if s["adapter_type"] == "llm-supervisor" and s["version"] == "1.3.2"]
    job = service.create_job(actor, {
        "name": "Terminal-Bench 2.1 · DSH · " + ", ".join(selected), "benchmark_id": benchmark["id"],
        "agent_snapshot_ids": [snapshot["id"]],
        "supervisor_snapshot_id": supervisors[0]["id"] if supervisors else None,
        "task_filter": {"task_ids": ["TB21-" + name for name in selected]},
        "execution": {"repetitions": 1, "max_concurrency": 1, "timeout_seconds": 3300, "infra_retry_limit": 0},
    })
    print(json.dumps({"job_id": job["id"], "selected": selected}), flush=True)
    service.start_job(actor, job["id"])
    previous = None
    while True:
        current = service.get_job(actor, job["id"])
        status = (current["status"], current["progress_completed"], tuple(t["execution_status"] for t in current["trials"]))
        if status != previous:
            print(json.dumps({"job_id": job["id"], "status": status}, ensure_ascii=False), flush=True)
            previous = status
        if current.get("finished_at"):
            target = args.data_dir / "terminal-bench" / (job["id"] + ".json")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"result": str(target), "outcomes": [{k: t.get(k) for k in ("id", "task_id", "outcome", "score", "error")} for t in current["trials"]]}, ensure_ascii=False), flush=True)
            break
        time.sleep(2)
    service.close()


if __name__ == "__main__":
    main()
