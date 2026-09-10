"""Provision the pinned Terminal-Bench and DeepSeek Harness Harbor integration."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Any

from .benchmark import validate_package
from .common import content_hash


SOURCE = "https://github.com/harbor-framework/terminal-bench-2-1.git"
PINNED_COMMIT = "7131e4375048a0e408a8fb404b5f499d726b695b"
HARBOR_VERSION = "0.22.0"
NODE_VERSION = "22.19.0"
NODE_ARCHIVE_URL = f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-x64.tar.xz"
DEFAULT_TASK = "openssl-selfsigned-cert"
_SETUP_LOCK = threading.Lock()


class HarborSetupError(RuntimeError):
    pass


def _run(arguments: list[str], *, cwd: Path, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            arguments,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise HarborSetupError(f"找不到命令：{arguments[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise HarborSetupError(f"命令执行超时：{arguments[0]}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "unknown error")[-2000:]
        raise HarborSetupError(f"命令失败（{arguments[0]}）：{detail}")
    return result


def _docker_state(project_root: Path) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.OSType}}"],
            cwd=project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except FileNotFoundError:
        return {"available": False, "linux": False, "message": "找不到 Docker 命令"}
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "linux": False, "message": "Docker 引擎连接失败或超时"}
    kind = result.stdout.strip().lower()
    if result.returncode:
        return {"available": False, "linux": False, "message": (result.stderr or "Docker 引擎不可用")[-500:]}
    return {
        "available": True,
        "linux": kind == "linux",
        "message": "Docker Linux 引擎可用" if kind == "linux" else "Terminal-Bench 需要 Linux 容器",
    }


def _credential_source() -> str | None:
    if os.environ.get("DEEPSEEK_API_KEY"):
        return "environment"
    dsh_home = Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh")))
    credential_path = dsh_home / ".credentials.yaml"
    if not credential_path.is_file():
        return None
    try:
        import yaml

        data = yaml.safe_load(credential_path.read_text(encoding="utf-8")) or {}
        refs = data.get("refs") if isinstance(data, dict) else None
        value = (refs.get("DEEPSEEK_API_KEY") if isinstance(refs, dict) else None) or (
            data.get("DEEPSEEK_API_KEY") if isinstance(data, dict) else None
        )
        return "dsh-home" if isinstance(value, str) and value.strip() else None
    except Exception:
        return None


def _commit_available(repo: Path) -> bool:
    if not (repo / ".git").is_dir():
        return False
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{PINNED_COMMIT}^{{commit}}"],
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def terminal_bench_status(
    project_root: Path,
    benchmarks: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
) -> dict[str, Any]:
    root = project_root / ".terminal-bench"
    repo = root / "terminal-bench-2-1"
    runtime = root / "runtime-cache" / "dsh-runtime.tgz"
    try:
        harbor_version = importlib.metadata.version("harbor")
    except importlib.metadata.PackageNotFoundError:
        harbor_version = None
    docker = _docker_state(project_root)
    benchmark = next(
        (
            item
            for item in benchmarks
            if item.get("status") == "published"
            and (item.get("package") or {}).get("manifest", {}).get("id") == "terminal-bench-2.1-full"
        ),
        None,
    )
    snapshot = next(
        (
            item
            for item in snapshots
            if item.get("adapter_type") == "terminal-bench-harbor"
            and (not benchmark or (item.get("config") or {}).get("benchmark_id") == (benchmark.get("package") or {}).get("manifest", {}).get("id"))
        ),
        None,
    )
    credential_source = _credential_source()
    components = {
        "harbor": {"available": harbor_version == HARBOR_VERSION, "version": harbor_version, "required_version": HARBOR_VERSION},
        "docker": docker,
        "terminal_bench_source": {"available": _commit_available(repo), "commit": PINNED_COMMIT, "path": str(repo)},
        "dsh_runtime": {"available": runtime.is_file() and runtime.stat().st_size > 0, "path": str(runtime)},
        "deepseek_credential": {"available": bool(credential_source), "source": credential_source},
        "registration": {
            "available": bool(benchmark and snapshot),
            "benchmark_id": benchmark.get("id") if benchmark else None,
            "agent_snapshot_id": snapshot.get("id") if snapshot else None,
        },
    }
    environment_ready = all(
        (
            components["harbor"]["available"],
            components["docker"]["linux"],
            components["terminal_bench_source"]["available"],
            components["dsh_runtime"]["available"],
            components["deepseek_credential"]["available"],
        )
    )
    return {
        "ready": environment_ready and components["registration"]["available"],
        "environment_ready": environment_ready,
        "default_task": DEFAULT_TASK,
        "components": components,
    }


def _ensure_source(project_root: Path) -> Path:
    root = project_root / ".terminal-bench"
    repo = root / "terminal-bench-2-1"
    root.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").is_dir():
        if repo.exists() and any(repo.iterdir()):
            raise HarborSetupError(f"Terminal-Bench 目录已存在但不是 Git 仓库：{repo}")
        _run(["git", "-c", "core.autocrlf=false", "clone", "--filter=blob:none", "--no-checkout", SOURCE, str(repo)], cwd=project_root)
    if not _commit_available(repo):
        _run(["git", "-C", str(repo), "fetch", "--depth", "1", "origin", PINNED_COMMIT], cwd=project_root)
    if not _commit_available(repo):
        raise HarborSetupError(f"无法获取固定 Terminal-Bench 提交：{PINNED_COMMIT}")
    return repo


def _ensure_runtime(project_root: Path) -> Path:
    runtime_cache = project_root / ".terminal-bench" / "runtime-cache"
    runtime = runtime_cache / "dsh-runtime.tgz"
    if runtime.is_file() and runtime.stat().st_size > 0:
        return runtime
    docker = _docker_state(project_root)
    if not docker["linux"]:
        raise HarborSetupError(str(docker["message"]))
    runtime_cache.mkdir(parents=True, exist_ok=True)
    node_archive = runtime_cache / "node.tar.xz"
    if not node_archive.is_file() or node_archive.stat().st_size == 0:
        partial = runtime_cache / "node.tar.xz.part"
        request = urllib.request.Request(NODE_ARCHIVE_URL, headers={"User-Agent": "Agent-Eval-Harbor-Setup/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=300) as response, partial.open("wb") as target:
                shutil.copyfileobj(response, target)
            partial.replace(node_archive)
        except Exception as exc:
            partial.unlink(missing_ok=True)
            raise HarborSetupError(f"下载 Node {NODE_VERSION} 运行包失败：{exc}") from exc
    command = [
        "docker",
        "run",
        "--rm",
        "--mount",
        f"type=bind,source={project_root},target=/workspace",
        "python:3.12-slim",
        "bash",
        "-lc",
        "cp /workspace/.terminal-bench/runtime-cache/node.tar.xz /tmp/node.tar.xz && "
        "bash /workspace/scripts/prepare_dsh_runtime.sh && "
        "cp /tmp/dsh-runtime.tgz /workspace/.terminal-bench/runtime-cache/dsh-runtime.tgz",
    ]
    result = _run(command, cwd=project_root, timeout=1800)
    (runtime_cache / "setup.log").write_text((result.stdout or "") + (result.stderr or ""), encoding="utf-8")
    if not runtime.is_file() or runtime.stat().st_size == 0:
        raise HarborSetupError("DSH 运行包准备完成后仍未生成")
    return runtime


def prepare_terminal_bench(service: Any, actor: Any) -> dict[str, Any]:
    """Prepare immutable local assets and register a reusable benchmark/snapshot."""
    from scripts import import_terminal_bench_full as importer

    with _SETUP_LOCK:
        project_root = service.project_root
        repo = _ensure_source(project_root)
        runtime = _ensure_runtime(project_root)
        root = project_root / ".terminal-bench"
        destination = root / ("terminal-bench-2-1-full-" + PINNED_COMMIT[:12] + "-lf")
        archive = root / (destination.name + ".tar")
        names = importer.materialize_tasks(repo, destination, archive)
        package = importer.build_package(destination / "tasks", names)
        config = importer.snapshot_config(
            destination / "tasks",
            service.data_dir,
            package,
            python_executable=Path(sys.executable),
        )
        package_hash = validate_package(package)["content_hash"]
        benchmark = next(
            (item for item in service.list_benchmarks(actor) if item["status"] == "published" and item["content_hash"] == package_hash),
            None,
        )
        if benchmark is None:
            benchmark = service.create_benchmark(actor, package, publish=True)
        config_hash = content_hash(config)
        snapshot = next(
            (
                item
                for item in service.list_snapshots(actor)
                if item["adapter_type"] == "terminal-bench-harbor" and item["config_hash"] == config_hash
            ),
            None,
        )
        if snapshot is None:
            snapshot = service.create_snapshot(
                actor,
                {
                    "name": "DeepSeek Harness · Terminal-Bench / Harbor",
                    "version": f"1.2.3-{config_hash[:8]}",
                    "adapter_type": "terminal-bench-harbor",
                    "config": config,
                },
            )
        output = project_root / "benchmarks" / "terminal-bench-2.1-full"
        output.mkdir(parents=True, exist_ok=True)
        (output / "package.json").write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (output / "source-lock.json").write_text(
            json.dumps(
                {
                    "source": SOURCE.removesuffix(".git"),
                    "commit": PINNED_COMMIT,
                    "task_count": len(names),
                    "task_names": names,
                    "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        registration = {
            "benchmark_id": benchmark["id"],
            "agent_snapshot_id": snapshot["id"],
            "source_commit": PINNED_COMMIT,
            "task_count": len(names),
            "default_task": DEFAULT_TASK,
            "dataset_root": config["dataset_root"],
            "runtime_archive": str(runtime),
            "evaluation_started": False,
        }
        (service.data_dir / "terminal-bench-full-registration.json").write_text(
            json.dumps(registration, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return registration
