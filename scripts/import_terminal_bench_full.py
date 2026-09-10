"""Import all pinned Terminal-Bench tasks; never start a paid evaluation."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import tarfile
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_eval.benchmark import validate_package
from agent_eval.common import content_hash
from agent_eval.terminal_bench import INTEGRATION_FILES, task_digest

COMMIT = "7131e4375048a0e408a8fb404b5f499d726b695b"
TASK_COUNT = 89
SOURCE = "https://github.com/harbor-framework/terminal-bench-2-1"
SNAPSHOT_VERSION = "1.2.4"
SNAPSHOT_NAME = "DSH · Terminal-Bench 完整集 / Harbor"


def materialize_tasks(repo: Path, destination: Path, archive_path: Path) -> list[str]:
    """Read committed bytes, leaving the existing working tree untouched."""
    names = subprocess.check_output(
        ["git", "-C", str(repo), "ls-tree", "-d", "--name-only", f"{COMMIT}:tasks"],
        text=True, encoding="utf-8",
    ).splitlines()
    if len(names) != TASK_COUNT:
        raise RuntimeError(f"Expected {TASK_COUNT} pinned tasks, found {len(names)}")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(repo), "-c", "core.autocrlf=false", "-c", "core.eol=lf",
                    "archive", "--format=tar", "-o", str(archive_path), COMMIT, "tasks"], check=True)
    committed = {}
    for entry in subprocess.check_output(
        ["git", "-C", str(repo), "ls-tree", "-rz", COMMIT, "--", "tasks"]
    ).decode("utf-8").split("\0"):
        if entry:
            info, name = entry.split("\t", 1)
            committed[name] = info.split()[2]
    destination.mkdir(parents=True, exist_ok=True)
    destination = destination.resolve()
    expected_files = set()
    with tarfile.open(archive_path) as archive:
        for member in archive.getmembers():
            path = (destination / member.name).resolve()
            if not path.is_relative_to(destination) or not (
                member.name.startswith("tasks/") or (member.name == "tasks" and member.isdir())
            ):
                raise RuntimeError(f"Unexpected archive path: {member.name}")
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise RuntimeError(f"Unsupported archive member: {member.name}")
            expected_files.add(path)
            data = archive.extractfile(member).read()
            blob_hash = hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()
            if committed.get(member.name) != blob_hash:
                raise RuntimeError(f"Archive differs from committed bytes: {member.name}")
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                if path.read_bytes() != data:
                    raise RuntimeError(f"Pinned snapshot changed; refusing to overwrite: {path}")
            else:
                path.write_bytes(data)
    actual_files = {p.resolve() for p in (destination / "tasks").rglob("*") if p.is_file()}
    if actual_files != expected_files or expected_files != {(destination / name).resolve() for name in committed}:
        raise RuntimeError("Pinned task snapshot contains unexpected files")
    return sorted(names)


def build_package(tasks_root: Path, names: list[str]) -> dict:
    tasks = []
    for name in names:
        path = tasks_root / name
        for required in ("task.toml", "instruction.md", "tests/test.sh"):
            if not (path / required).is_file():
                raise RuntimeError(f"Missing official task file: {name}/{required}")
        config = tomllib.loads((path / "task.toml").read_text(encoding="utf-8"))
        metadata = config["metadata"]
        agent_timeout = float(config["agent"]["timeout_sec"])
        verifier_timeout = float(config["verifier"]["timeout_sec"])
        build_timeout = float(config["environment"]["build_timeout_sec"])
        # Harbor enforces each official phase timeout; the outer runner also
        # accommodates the pinned DSH setup (900s) and result collection (300s).
        outer_timeout = math.ceil(build_timeout + 900 + agent_timeout + verifier_timeout + 300)
        tasks.append({
            "id": "TB21-" + name, "title": (config.get("task") or {}).get("description", name),
            "suite": metadata["category"], "version": "2.1",
            "instruction": (path / "instruction.md").read_text(encoding="utf-8"),
            "tags": metadata.get("tags", []), "difficulty": metadata.get("difficulty"),
            "pass_threshold": 100, "timeout_seconds": outer_timeout,
            "environment": {"type": "docker", **config["environment"]},
            "terminal_bench": {"task_name": name, "task_digest": task_digest(path),
                "upstream_commit": COMMIT, "category": metadata["category"],
                "difficulty": metadata.get("difficulty"), "agent_timeout_seconds": agent_timeout,
                "verifier_timeout_seconds": verifier_timeout},
            "graders": [{"id": "terminal-bench-official-v1", "type": "terminal_bench", "version": "2.1", "required": True}],
        })
    package = {"manifest": {
        "id": "terminal-bench-2.1-full", "name": f"Terminal-Bench 2.1 · 完整集（{len(tasks)}题）",
        "version": "2.1.0-full.2", "benchmark_type": "general", "pass_threshold": 100,
        "source": SOURCE, "source_commit": COMMIT,
        "description": "All 89 tasks at the pinned upstream commit, with original instructions, environments and official verifiers. Registration does not mean a full run has completed.",
    }, "tasks": tasks, "fixture": {}}
    validate_package(package)
    return package


def snapshot_config(
    tasks_root: Path,
    data_dir: Path,
    package: dict,
    *,
    python_executable: Path | None = None,
    agent_profile: dict | None = None,
    reasoning_effort: str | None = None,
    base_url: str | None = None,
    auth_mode: str = "api-key",
) -> dict:
    python = python_executable or ROOT / (
        ".terminal-bench-venv/Scripts/python.exe" if sys.platform == "win32" else ".terminal-bench-venv/bin/python"
    )
    if not python.is_file():
        raise RuntimeError("Pinned Harbor Python is missing")
    profile = agent_profile or {
        "id": "dsh-deepseek-v4-flash", "harness": "dsh", "provider": "deepseek-official",
        "model": "deepseek-official/deepseek-v4-flash", "api_key_env": "DEEPSEEK_API_KEY",
    }
    config = {"python": str(python), "dataset_root": str(tasks_root.resolve()),
        "benchmark_id": package["manifest"]["id"],
        "output_root": str(data_dir.resolve() / "terminal-bench"),
        "runner_timeout_seconds": max(task["timeout_seconds"] for task in package["tasks"]),
        "environment_build_timeout_multiplier": 3.0,
        "harbor_version": "0.22.0", "dsh_version": "0.1.1-rc.2",
        "model_profile": profile["id"], "harness": profile["harness"],
        "provider": profile["provider"], "model": profile["model"],
        "api_key_env": profile["api_key_env"],
        "permission_boundary": "disposable-task-container",
        "dataset_commit": COMMIT,
        "integration_files": list(INTEGRATION_FILES),
        "classification_version": "2.1.0", "process_boundary": "confirmed-container-termination-v2",
        "environment_cleanup": "verified-runtime-resources-v1", "image_cache": "retain-prebuilt-images",
        "integration_digest": content_hash({name: (ROOT / name).read_text(encoding="utf-8") for name in INTEGRATION_FILES})}
    if profile["harness"] == "dsh":
        runtime = ROOT / ".terminal-bench/runtime-cache/dsh-runtime.tgz"
        if not runtime.is_file():
            raise RuntimeError("Pinned DSH runtime is missing")
        with runtime.open("rb") as stream:
            config["runtime_archive_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    else:
        config.update({
            "auth_mode": auth_mode,
            "wire_api": "responses",
            "reasoning_effort": reasoning_effort or profile["default_reasoning_effort"],
            "disable_response_storage": True,
            "codex_version": profile.get("codex_version", "0.154.0"),
            "process_boundary": "harbor-container-lifecycle-v1",
        })
        if auth_mode == "api-key":
            config["base_url"] = str(base_url or "").rstrip("/")
    return config


def api(base: str, path: str, payload: dict | None = None) -> dict | list:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(base.rstrip("/") + "/api/v1" + path, data=data,
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    if result.get("error"):
        raise RuntimeError(result["error"])
    return result["data"]


def register(base: str, package: dict, config: dict) -> dict:
    package_hash = validate_package(package)["content_hash"]
    benchmarks = api(base, "/benchmarks")
    benchmark = next((b for b in benchmarks if b["content_hash"] == package_hash), None)
    snapshots = api(base, "/agent-snapshots")
    snapshot = next((s for s in snapshots if s["adapter_type"] == "terminal-bench-harbor" and s["config_hash"] == content_hash(config)), None)
    if snapshot is None:
        if any(s["adapter_type"] == "terminal-bench-harbor" and s["name"] == SNAPSHOT_NAME and s["version"] == SNAPSHOT_VERSION for s in snapshots):
            raise RuntimeError("Snapshot version already exists with different content; choose a new explicit version")
        snapshot = api(base, "/agent-snapshots", {"name": SNAPSHOT_NAME, "version": SNAPSHOT_VERSION,
            "adapter_type": "terminal-bench-harbor", "config": config})
    if benchmark is None:
        benchmark = api(base, "/benchmarks", {"package": package, "publish": True})
    return {"benchmark_id": benchmark["id"], "agent_snapshot_id": snapshot["id"],
        "source_commit": COMMIT, "task_count": len(package["tasks"]),
        "dataset_root": config["dataset_root"], "runner_timeout_seconds": config["runner_timeout_seconds"],
        "evaluation_started": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="http://127.0.0.1:8765")
    parser.add_argument("--data-dir", type=Path, default=ROOT / ".data-supervised-loop")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    root = ROOT / ".terminal-bench"
    destination = root / ("terminal-bench-2-1-full-" + COMMIT[:12] + "-lf")
    archive = root / (destination.name + ".tar")
    names = materialize_tasks(root / "terminal-bench-2-1", destination, archive)
    package = build_package(destination / "tasks", names)
    config = snapshot_config(destination / "tasks", args.data_dir, package)
    output = ROOT / "benchmarks/terminal-bench-2.1-full"
    output.mkdir(parents=True, exist_ok=True)
    (output / "package.json").write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "source-lock.json").write_text(json.dumps({"source": SOURCE, "commit": COMMIT,
        "task_count": len(names), "task_names": names, "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}, indent=2) + "\n", encoding="utf-8")
    if args.prepare_only:
        print(json.dumps({"task_count": len(names), "package": str(output / "package.json"), "config": config}, ensure_ascii=False))
        return
    registration = register(args.api_base, package, config)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    (args.data_dir / "terminal-bench-full-registration.json").write_text(json.dumps(registration, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(registration, ensure_ascii=False))


if __name__ == "__main__":
    main()
