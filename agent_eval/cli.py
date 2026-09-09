from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Sequence

from .adapters import ADAPTERS
from .benchmark import load_package
from .service import Actor, EvaluationService


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BENCHMARK = PROJECT_ROOT / "benchmarks" / "mmlu-high-school-computer-science-smoke"
DEFAULT_DATA_DIR = Path(os.environ.get("AGENT_EVAL_DATA_DIR", PROJECT_ROOT / ".data"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Agent evaluation platform.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List benchmark cases.")
    list_parser.add_argument("--benchmark", type=Path, default=DEFAULT_BENCHMARK)

    serve_parser = subparsers.add_parser("serve", help="Start the Evaluation REST API and Portal.")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)

    init_parser = subparsers.add_parser("init", help="Initialise storage and MMLU / DSH / Supervisor defaults.")
    init_parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)

    validate_parser = subparsers.add_parser("benchmark-validate", help="Validate a Benchmark package without importing it.")
    validate_parser.add_argument("path", type=Path)
    validate_parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)

    import_parser = subparsers.add_parser("benchmark-import", help="Import a Benchmark package.")
    import_parser.add_argument("path", type=Path)
    import_parser.add_argument("--publish", action="store_true")
    import_parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)

    benchmark_list = subparsers.add_parser("benchmark-list", help="List imported Benchmarks.")
    benchmark_list.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)

    snapshot_create = subparsers.add_parser("snapshot-create", help="Create an immutable AgentSnapshot.")
    snapshot_create.add_argument("--name", required=True)
    snapshot_create.add_argument("--version", required=True)
    snapshot_create.add_argument("--adapter", required=True, choices=tuple(ADAPTERS))
    snapshot_create.add_argument("--config", default="{}", help="JSON configuration; store secret environment references only.")
    snapshot_create.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)

    snapshot_list = subparsers.add_parser("snapshot-list", help="List AgentSnapshots.")
    snapshot_list.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)

    job_run = subparsers.add_parser("job-run", help="Create, start and optionally wait for an EvalJob.")
    job_run.add_argument("--name", default="CLI evaluation")
    job_run.add_argument("--benchmark-id", required=True)
    job_run.add_argument("--snapshot-id", action="append", required=True)
    job_run.add_argument("--supervisor-snapshot-id", help="Real Supervisor snapshot used when a Trial supervision check is requested.")
    job_run.add_argument("--repetitions", type=int, default=1)
    job_run.add_argument("--max-concurrency", type=int, default=4)
    job_run.add_argument("--task-id", action="append")
    job_run.add_argument("--allow-incompatible", action="store_true", help="Run an explicitly incompatible Agent/Benchmark pair for conformance negative testing.")
    job_run.add_argument("--no-wait", action="store_true")
    job_run.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)

    job_status = subparsers.add_parser("job-status", help="Show EvalJob and Trial status.")
    job_status.add_argument("job_id")
    job_status.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)

    job_cancel = subparsers.add_parser("job-cancel", help="Cancel an EvalJob while retaining completed evidence.")
    job_cancel.add_argument("job_id")
    job_cancel.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)

    export = subparsers.add_parser("export", help="Export an EvalJob report.")
    export.add_argument("job_id")
    export.add_argument("--format", choices=("json", "jsonl", "csv", "markdown"), default="markdown")
    export.add_argument("--output", type=Path)
    export.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "list":
        return _platform_command(args)
    if args.command == "list":
        package = load_package(args.benchmark.resolve())
        manifest = package["manifest"]
        print(f"{manifest['id']}@{manifest['version']}")
        for task in package["tasks"]:
            print(f"{task['id']}\t{task['suite']}\t{task['instruction']}")
        return 0



def _platform_command(args: argparse.Namespace) -> int:
    service = EvaluationService(args.data_dir, project_root=PROJECT_ROOT)
    actor = Actor()
    if args.command == "serve":
        from .api import serve

        service.ensure_demo_data(actor)
        service.recover_jobs()
        serve(service, host=args.host, port=args.port)
        return 0
    if args.command == "init":
        print(json.dumps(service.ensure_demo_data(actor), ensure_ascii=False, indent=2))
        return 0
    if args.command == "benchmark-validate":
        print(json.dumps(service.validate_benchmark_path(args.path, actor), ensure_ascii=False, indent=2))
        return 0
    if args.command == "benchmark-import":
        print(json.dumps(service.import_benchmark_path(actor, args.path, publish=args.publish), ensure_ascii=False, indent=2))
        return 0
    if args.command == "benchmark-list":
        for item in service.list_benchmarks(actor):
            print(f"{item['id']}\t{item['name']}@{item['version']}\t{item['status']}\t{len(item['package']['tasks'])} tasks")
        return 0
    if args.command == "snapshot-create":
        try:
            config = json.loads(args.config)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--config 不是有效 JSON：{exc}") from exc
        item = service.create_snapshot(actor, {"name": args.name, "version": args.version, "adapter_type": args.adapter, "config": config})
        print(json.dumps(item, ensure_ascii=False, indent=2))
        return 0
    if args.command == "snapshot-list":
        for item in service.list_snapshots(actor):
            print(f"{item['id']}\t{item['name']}@{item['version']}\t{item['adapter_type']}")
        return 0
    if args.command == "job-run":
        job = service.create_job(
            actor,
            {
                "name": args.name,
                "benchmark_id": args.benchmark_id,
                "agent_snapshot_ids": args.snapshot_id,
                "supervisor_snapshot_id": args.supervisor_snapshot_id,
                "task_filter": {"task_ids": args.task_id} if args.task_id else {},
                "execution": {"repetitions": args.repetitions, "max_concurrency": args.max_concurrency},
                "compatibility_mode": "allow" if args.allow_incompatible else "strict",
            },
        )
        service.start_job(actor, job["id"])
        print(f"Job: {job['id']}")
        if args.no_wait:
            return 0
        while True:
            current = service.get_job(actor, job["id"])
            print(f"\r{current['status']}: {current['progress_completed']}/{current['progress_total']}", end="", flush=True)
            if current["status"] in {"completed", "review_pending", "failed", "canceled"}:
                print()
                if current.get("report"):
                    print(f"Conclusion: {current['report']['conclusion']}")
                return 1 if current["status"] == "failed" else 0
            time.sleep(0.2)
    if args.command == "job-status":
        print(json.dumps(service.get_job(actor, args.job_id), ensure_ascii=False, indent=2))
        return 0
    if args.command == "job-cancel":
        print(json.dumps(service.cancel_job(actor, args.job_id), ensure_ascii=False, indent=2))
        return 0
    if args.command == "export":
        _, content = service.export_report(actor, args.job_id, args.format)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(content)
            print(args.output.resolve())
        else:
            print(content.decode("utf-8-sig"), end="")
        return 0
    raise SystemExit(f"Unsupported command: {args.command}")
