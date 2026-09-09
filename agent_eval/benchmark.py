from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from .common import EvalError, content_hash
from .compatibility import normalise_capabilities


BENCHMARK_TYPES = {"platform", "conformance", "general", "domain", "regression"}


def _yaml_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return None
    lowered = value.lower()
    if lowered in {"null", "~"}:
        return None
    if lowered in {"true", "false"}:
        return lowered == "true"
    if value[0:1] in {"'", '"'}:
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [] if not inner else [_yaml_scalar(part) for part in inner.split(",")]
    if value.startswith("{") and value.endswith("}"):
        inner = value[1:-1].strip()
        result: dict[str, Any] = {}
        if inner:
            for part in inner.split(","):
                key, item = part.split(":", 1)
                result[str(_yaml_scalar(key.strip()))] = _yaml_scalar(item)
        return result
    try:
        return float(value) if "." in value else int(value)
    except ValueError:
        return value


def _simple_yaml(text: str) -> Any:
    """Parse the conservative YAML subset used by benchmark manifests.

    Full YAML remains available automatically when PyYAML is installed. This
    fallback deliberately excludes anchors, tags and multiline executable data.
    """
    prepared: list[tuple[int, str]] = []
    for line_number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#") or raw.strip() == "---":
            continue
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise EvalError("invalid_yaml", f"YAML 第 {line_number} 行不能使用 Tab 缩进")
        prepared.append((len(raw) - len(raw.lstrip(" ")), raw.strip()))

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        if index >= len(prepared):
            return {}, index
        is_list = prepared[index][1].startswith("- ") or prepared[index][1] == "-"
        container: Any = [] if is_list else {}
        while index < len(prepared):
            current_indent, content = prepared[index]
            if current_indent < indent:
                break
            if current_indent > indent:
                raise EvalError("invalid_yaml", f"YAML 缩进不合法：{content}")
            if is_list:
                if not (content.startswith("- ") or content == "-"):
                    break
                value = content[1:].strip()
                if not value:
                    child, index = parse_block(index + 1, prepared[index + 1][0])
                    container.append(child)
                    continue
                if ":" in value:
                    key, raw_value = value.split(":", 1)
                    item: dict[str, Any] = {key.strip(): _yaml_scalar(raw_value)}
                    index += 1
                    while index < len(prepared) and prepared[index][0] > indent:
                        child_indent, child_content = prepared[index]
                        if child_content.startswith("-"):
                            raise EvalError("invalid_yaml", "列表对象的字段必须使用 key: value")
                        child_key, separator, child_value = child_content.partition(":")
                        if not separator:
                            raise EvalError("invalid_yaml", f"YAML 字段缺少冒号：{child_content}")
                        if child_value.strip():
                            item[child_key.strip()] = _yaml_scalar(child_value)
                            index += 1
                        else:
                            if index + 1 >= len(prepared) or prepared[index + 1][0] <= child_indent:
                                item[child_key.strip()] = None
                                index += 1
                            else:
                                child, index = parse_block(index + 1, prepared[index + 1][0])
                                item[child_key.strip()] = child
                    container.append(item)
                    continue
                container.append(_yaml_scalar(value))
                index += 1
            else:
                key, separator, raw_value = content.partition(":")
                if not separator:
                    raise EvalError("invalid_yaml", f"YAML 字段缺少冒号：{content}")
                if raw_value.strip():
                    container[key.strip()] = _yaml_scalar(raw_value)
                    index += 1
                else:
                    if index + 1 >= len(prepared) or prepared[index + 1][0] <= indent:
                        container[key.strip()] = None
                        index += 1
                    else:
                        child, index = parse_block(index + 1, prepared[index + 1][0])
                        container[key.strip()] = child
        return container, index

    return parse_block(0, prepared[0][0])[0] if prepared else {}


def read_structured_file(path: Path) -> Any:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise EvalError("invalid_json", f"{path.name} 第 {exc.lineno} 行：{exc.msg}") from exc
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError:
            return _simple_yaml(text)
        return yaml.safe_load(text)
    raise EvalError("unsupported_format", f"不支持的文件格式：{path.suffix}")


def _normalise_task(task: dict[str, Any], *, default_suite: str = "default") -> dict[str, Any]:
    task_id = task.get("id") or task.get("task_id") or task.get("case_id")
    instruction = task.get("instruction") or task.get("user_input")
    if not isinstance(task_id, str) or not task_id.strip():
        raise EvalError("invalid_task", "Task 缺少非空 id/case_id")
    if not isinstance(instruction, str) or not instruction.strip():
        raise EvalError("invalid_task", f"Task {task_id} 缺少 instruction/user_input")
    environment = task.get("environment") or {"type": "fixture", "snapshot": "local"}
    if not isinstance(environment, dict) or not environment.get("type"):
        raise EvalError("invalid_task", f"Task {task_id} 的 environment 不合法")
    if environment.get("type") not in {"fixture", "docker", "sandbox"}:
        raise EvalError("invalid_task", f"Task {task_id} 的 environment.type 必须是 fixture/docker/sandbox")
    graders = task.get("graders") or [{"id": "builtin-rule-v1", "type": "rule", "weight": 1.0}]
    if not isinstance(graders, list) or not graders:
        raise EvalError("invalid_task", f"Task {task_id} 必须配置至少一个 grader")
    return {
        **task,
        "id": task_id.strip(),
        "version": str(task.get("version", "1.0.0")),
        "suite": str(task.get("suite") or task.get("category") or default_suite),
        "instruction": instruction,
        "tags": list(task.get("tags") or []),
        "case_type": str(task.get("case_type", "typical")),
        "timeout_seconds": int(task.get("timeout_seconds") or task.get("limits", {}).get("timeout_seconds", 120)),
        "repetitions": int(task.get("repetitions") or task.get("run_policy", {}).get("repetitions", 1)),
        "environment": environment,
        "agent_requirements": normalise_capabilities(task.get("agent_requirements"), label=f"Task {task_id} agent_requirements"),
        "graders": graders,
        "hard_failures": list(task.get("hard_failures") or ["forbidden_tool", "secret_exposure"]),
    }


def validate_package(package: dict[str, Any]) -> dict[str, Any]:
    manifest = package.get("manifest") if isinstance(package.get("manifest"), dict) else package
    tasks = package.get("tasks") or manifest.get("tasks") or []
    benchmark_id = manifest.get("id") or manifest.get("benchmark_id")
    name = manifest.get("name") or benchmark_id
    version = manifest.get("version")
    if not all(isinstance(value, str) and value.strip() for value in (benchmark_id, name, version)):
        raise EvalError("invalid_benchmark", "Benchmark 必须包含 id/benchmark_id、name 和 version")
    benchmark_type = str(manifest.get("benchmark_type") or "general").strip().lower()
    if benchmark_type not in BENCHMARK_TYPES:
        raise EvalError("invalid_benchmark_type", f"benchmark_type 必须是：{', '.join(sorted(BENCHMARK_TYPES))}")
    domain = str(manifest.get("domain") or "").strip() or None
    if benchmark_type == "domain" and domain is None:
        raise EvalError("invalid_benchmark", "domain 类型 Benchmark 必须声明 domain")
    if not isinstance(tasks, list) or not tasks:
        raise EvalError("invalid_benchmark", "Benchmark 至少需要一个 Task")
    benchmark_threshold = float(manifest.get("pass_threshold", 80))
    normalised = [
        {
            **_normalise_task(task, default_suite=str(manifest.get("default_suite", "default"))),
            "pass_threshold": float(task.get("pass_threshold", benchmark_threshold)),
        }
        for task in tasks
    ]
    ids = [task["id"] for task in normalised]
    duplicates = sorted({task_id for task_id in ids if ids.count(task_id) > 1})
    if duplicates:
        raise EvalError("duplicate_task_id", f"Task ID 重复：{', '.join(duplicates)}")
    fixture = package.get("fixture") or manifest.get("fixture") or {}
    result = {
        "schema_version": "1.1.0",
        "manifest": {
            **manifest,
            "id": benchmark_id,
            "name": name,
            "version": version,
            "benchmark_type": benchmark_type,
            "domain": domain,
            "agent_requirements": normalise_capabilities(manifest.get("agent_requirements"), label="Benchmark agent_requirements"),
            "pass_threshold": benchmark_threshold,
            "source": manifest.get("source") or "local",
            "license": manifest.get("license") or "unspecified",
            "visibility": manifest.get("visibility") or "private",
        },
        "tasks": normalised,
        "fixture": fixture,
    }
    result["content_hash"] = content_hash(result)
    return result


def load_package(path: Path) -> dict[str, Any]:
    path = path.resolve()
    if not path.exists():
        raise EvalError("not_found", f"Benchmark 路径不存在：{path}", status=404)
    if path.is_file() and path.suffix.lower() == ".jsonl":
        tasks = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                tasks.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise EvalError("invalid_jsonl", f"JSONL 第 {line_number} 行：{exc.msg}") from exc
        return validate_package(
            {
                "manifest": {"id": path.stem, "name": path.stem, "version": "1.0.0"},
                "tasks": tasks,
            }
        )
    if path.is_file():
        data = read_structured_file(path)
        if not isinstance(data, dict):
            raise EvalError("invalid_benchmark", "Benchmark 文件顶层必须是对象")
        return validate_package(data)

    manifest_path = next(
        (candidate for candidate in (path / "benchmark.yaml", path / "benchmark.yml", path / "benchmark.json") if candidate.is_file()),
        None,
    )
    if manifest_path is None:
        raise EvalError("missing_manifest", "目录中缺少 benchmark.yaml、benchmark.yml 或 benchmark.json")
    manifest = read_structured_file(manifest_path)
    if not isinstance(manifest, dict):
        raise EvalError("invalid_benchmark", "Benchmark manifest 顶层必须是对象")

    tasks: list[dict[str, Any]] = []
    dataset = manifest.get("dataset")
    if dataset:
        dataset_path = path / str(dataset)
        if not dataset_path.is_file():
            raise EvalError("missing_file", f"数据集文件不存在：{dataset}")
        for line_number, line in enumerate(dataset_path.read_text(encoding="utf-8-sig").splitlines(), start=1):
            if line.strip():
                try:
                    tasks.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise EvalError("invalid_jsonl", f"{dataset} 第 {line_number} 行：{exc.msg}") from exc
    tasks_root = path / "tasks"
    if tasks_root.is_dir():
        for task_dir in sorted(item for item in tasks_root.iterdir() if item.is_dir()):
            task_path = next((item for item in (task_dir / "task.yaml", task_dir / "task.yml", task_dir / "task.json") if item.is_file()), None)
            if task_path is None:
                raise EvalError("missing_file", f"{task_dir.name} 缺少 task.yaml/task.json")
            task = read_structured_file(task_path)
            if not isinstance(task, dict):
                raise EvalError("invalid_task", f"{task_path} 顶层必须是对象")
            instruction_file = task.get("instruction_file")
            if instruction_file:
                instruction_path = task_dir / str(instruction_file)
                if not instruction_path.is_file():
                    raise EvalError("missing_file", f"Task {task.get('id')} 缺少 {instruction_file}")
                task["instruction"] = instruction_path.read_text(encoding="utf-8-sig").strip()
            tasks.append(task)

    fixture: dict[str, Any] = {}
    fixture_name = manifest.get("fixture_file")
    if fixture_name:
        fixture_path = path / str(fixture_name)
        if not fixture_path.is_file():
            raise EvalError("missing_file", f"Fixture 文件不存在：{fixture_name}")
        fixture_data = read_structured_file(fixture_path)
        if isinstance(fixture_data, dict):
            fixture = fixture_data
    return validate_package({"manifest": manifest, "tasks": tasks, "fixture": fixture})
