from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .common import EvalError, new_id, redact, utc_now


def _final_text(final_output: Any) -> str:
    if isinstance(final_output, str):
        return final_output
    if isinstance(final_output, dict):
        return str(final_output.get("content") or final_output.get("message") or json.dumps(final_output, ensure_ascii=False))
    return str(final_output)


def _contains_subset(actual: Any, expected: Any) -> bool:
    """Return whether expected is recursively contained in observable data."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _contains_subset(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            _contains_subset(actual_item, expected_item)
            for actual_item, expected_item in zip(actual, expected)
        )
    return actual == expected


def _result(spec: dict[str, Any], score: float | None, *, passed: bool | None, reason: str, evidence: list[str] | None = None, hard_failures: list[str] | None = None, status: str = "completed", confidence: float | None = None) -> dict[str, Any]:
    return {
        "id": new_id("grade"),
        "grader_id": str(spec.get("id") or spec.get("name") or spec.get("type")),
        "grader_version": str(spec.get("version", "1.0.0")),
        "grader_type": str(spec.get("type", "rule")),
        "status": status,
        "score": score,
        "passed": passed,
        "reason": reason,
        "evidence_refs": evidence or [],
        "hard_failures": sorted(set(hard_failures or [])),
        "confidence": confidence,
        "created_at": utc_now(),
    }


def _generic_rule(spec: dict[str, Any], task: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    config = {**(task.get("expected") or {}), **(spec.get("config") or {})}
    text = _final_text(run.get("final_output"))
    events = run.get("events") or []
    event_types = [str(event.get("event_type")) for event in events]
    tool_events = [
        event
        for event in events
        if event.get("event_type") in {"tool_call", "tool_call_started"}
    ]
    tool_names = [str((event.get("payload") or {}).get("name")) for event in tool_events]
    called_tools = {
        name for name in tool_names
    }
    checks: list[tuple[bool, str]] = []
    for item in config.get("contains", []):
        checks.append((str(item) in text, f"输出包含：{item}"))
    for item in config.get("excludes", []):
        checks.append((str(item) not in text, f"输出不包含：{item}"))
    if config.get("regex"):
        checks.append((re.search(str(config["regex"]), text) is not None, f"输出匹配正则：{config['regex']}"))
    for event_type in config.get("required_events", []):
        checks.append((event_type in event_types, f"存在事件：{event_type}"))
    for event_type in config.get("forbidden_events", []):
        checks.append((event_type not in event_types, f"不存在事件：{event_type}"))
    for tool in config.get("required_tools", []):
        checks.append((str(tool) in called_tools, f"调用必需工具：{tool}"))
    for tool in config.get("forbidden_tools", []):
        checks.append((str(tool) not in called_tools, f"不调用禁止工具：{tool}"))
    expected_sequence = [str(item) for item in config.get("tool_sequence", [])]
    if expected_sequence:
        sequence_index = 0
        for name in tool_names:
            if sequence_index < len(expected_sequence) and name == expected_sequence[sequence_index]:
                sequence_index += 1
        checks.append((sequence_index == len(expected_sequence), f"工具调用顺序为：{' -> '.join(expected_sequence)}"))
    for expected_call in config.get("tool_arguments", []):
        expected_name = str(expected_call.get("name", ""))
        expected_arguments = expected_call.get("arguments") or {}
        matched = any(
            str((event.get("payload") or {}).get("name")) == expected_name
            and _contains_subset((event.get("payload") or {}).get("arguments") or {}, expected_arguments)
            for event in tool_events
        )
        checks.append((matched, f"工具 {expected_name} 参数符合约定"))
    for expected_event in config.get("required_event_payloads", []):
        expected_type = str(expected_event.get("event_type", ""))
        expected_payload = expected_event.get("payload") or {}
        matched = any(
            str(event.get("event_type")) == expected_type
            and _contains_subset(event.get("payload") or {}, expected_payload)
            for event in events
        )
        checks.append((matched, f"事件 {expected_type} 载荷符合约定"))
    if "should_use_skill" in config:
        used_skill = "skill_loaded" in event_types
        checks.append((used_skill == bool(config["should_use_skill"]), "Skill 路由符合任务要求"))
    expected_answer = config.get("answer_contains")
    if expected_answer is not None:
        checks.append((str(expected_answer) in text, f"输出包含答案：{expected_answer}"))
    final_output = run.get("final_output")
    output_constraints = config.get("output_constraints") or {}
    if isinstance(final_output, dict) and output_constraints.get("required_fields"):
        checks.append((all(field in final_output for field in output_constraints["required_fields"]), "最终输出包含全部必需字段"))
    usage = run.get("usage") or {}
    observed_tool_calls = int(usage.get("tool_calls")) if usage.get("tool_calls") is not None else len(tool_events)
    if config.get("max_steps") is not None:
        checks.append((int(usage.get("steps") or 0) <= int(config["max_steps"]), f"步骤数不超过 {config['max_steps']}"))
    if config.get("max_tool_calls") is not None:
        checks.append((observed_tool_calls <= int(config["max_tool_calls"]), f"工具调用不超过 {config['max_tool_calls']}"))
    if config.get("min_tool_calls") is not None:
        checks.append((observed_tool_calls >= int(config["min_tool_calls"]), f"工具调用不少于 {config['min_tool_calls']}"))
    if config.get("max_duration_ms") is not None:
        checks.append((float(usage.get("duration_ms") or 0) <= float(config["max_duration_ms"]), f"耗时不超过 {config['max_duration_ms']}ms"))
    if config.get("max_input_tokens") is not None:
        checks.append((int(usage.get("input_tokens") or 0) <= int(config["max_input_tokens"]), f"输入 Token 不超过 {config['max_input_tokens']}"))
    if config.get("max_output_tokens") is not None:
        checks.append((int(usage.get("output_tokens") or 0) <= int(config["max_output_tokens"]), f"输出 Token 不超过 {config['max_output_tokens']}"))
    if config.get("max_estimated_cost") is not None:
        checks.append((float(usage.get("estimated_cost") or 0) <= float(config["max_estimated_cost"]), f"预估成本不超过 {config['max_estimated_cost']}"))
    if not checks:
        checks.append((bool(text.strip()), "Agent 返回非空最终输出"))
    passed_count = sum(passed for passed, _ in checks)
    score = round(100 * passed_count / len(checks), 2)
    failed = [description for passed, description in checks if not passed]
    return _result(
        spec,
        score,
        passed=score >= float(spec.get("pass_threshold", task.get("pass_threshold", 80))),
        reason="；".join(failed) if failed else "全部规则通过",
        evidence=[f"event:{index + 1}" for index, event in enumerate(events) if event.get("event_type") in set(config.get("required_events", []))],
    )


def _validate_schema(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    errors: list[str] = []
    schema_type = schema.get("type")
    type_map = {"object": dict, "array": list, "string": str, "number": (int, float), "integer": int, "boolean": bool, "null": type(None)}
    expected_type = type_map.get(schema_type)
    if expected_type and (not isinstance(value, expected_type) or (schema_type in {"number", "integer"} and isinstance(value, bool))):
        return [f"{path} 应为 {schema_type}"]
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path} 缺少字段 {key}")
        for key, child_schema in (schema.get("properties") or {}).items():
            if key in value and isinstance(child_schema, dict):
                errors.extend(_validate_schema(value[key], child_schema, f"{path}.{key}"))
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            errors.extend(_validate_schema(item, schema["items"], f"{path}[{index}]"))
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} 不在允许值中")
    if isinstance(value, (str, list)):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            errors.append(f"{path} 长度不足")
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            errors.append(f"{path} 数量不足")
    return errors


def _schema_grade(spec: dict[str, Any], task: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    schema = spec.get("schema") or (spec.get("config") or {}).get("schema") or task.get("output_schema")
    if not isinstance(schema, dict):
        return _result(spec, None, passed=None, reason="Schema Grader 缺少 schema", status="failed")
    errors = _validate_schema(run.get("final_output"), schema)
    return _result(spec, 0.0 if errors else 100.0, passed=not errors, reason="；".join(errors) if errors else "输出满足 Schema")


def _executable_grade(spec: dict[str, Any], task: dict[str, Any], run: dict[str, Any], *, executable_root: Path | None, enabled: bool) -> dict[str, Any]:
    if not enabled:
        return _result(spec, None, passed=None, reason="可执行 Grader 默认关闭；需显式启用并配置受限执行根目录", status="failed")
    entrypoint = spec.get("entrypoint") or (spec.get("config") or {}).get("entrypoint")
    if not entrypoint or executable_root is None:
        return _result(spec, None, passed=None, reason="Executable Grader 缺少 entrypoint/root", status="failed")
    root = executable_root.resolve()
    script = (root / str(entrypoint)).resolve()
    if root not in script.parents or not script.is_file():
        return _result(spec, None, passed=None, reason="Executable Grader 路径越界或不存在", status="failed")
    command = [str(script)] if script.suffix.lower() not in {".py"} else [sys.executable, str(script)]
    payload = json.dumps({"task": task, "agent_run": run}, ensure_ascii=False)
    try:
        completed = subprocess.run(command, input=payload, capture_output=True, text=True, timeout=int(spec.get("timeout_seconds", 30)), cwd=root, shell=False, check=False)
        if completed.returncode != 0:
            return _result(spec, None, passed=None, reason=f"Grader 退出码 {completed.returncode}: {redact(completed.stderr[-500:])}", status="failed")
        output = json.loads(completed.stdout)
        return _result(spec, float(output["score"]), passed=bool(output.get("passed", float(output["score"]) >= 80)), reason=str(output.get("reason", "Executable Grader 完成")), evidence=list(output.get("evidence_refs") or []), hard_failures=list(output.get("hard_failures") or []))
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, KeyError, ValueError) as exc:
        return _result(spec, None, passed=None, reason=f"Executable Grader 失败：{exc}", status="failed")


def _llm_grade(spec: dict[str, Any], task: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    config = spec.get("config") or {}
    endpoint = config.get("endpoint")
    secret_env = config.get("api_key_env")
    if not endpoint or not secret_env or not os.environ.get(str(secret_env)):
        return _result(spec, None, passed=None, reason="LLM Judge 未配置 endpoint 或 API Key 环境变量", status="failed")
    rubric = config.get("rubric") or "按任务完成度、准确性、完整性、证据和约束遵循从 0 到 100 评分。"
    request_body = {
        "model": config.get("model"),
        "temperature": 0,
        "messages": [
            {"role": "system", "content": "你是评测器。只根据可观察证据评分，不推测隐藏思维。仅输出 JSON：score, passed, reason, evidence_refs, confidence, needs_human_review。"},
            {"role": "user", "content": json.dumps({"task": task["instruction"], "rubric": rubric, "final_output": run.get("final_output"), "events": run.get("events", [])}, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(str(endpoint), data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ[str(secret_env)]}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=int(spec.get("timeout_seconds", 60))) as response:
            body = json.loads(response.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
        judge = json.loads(content) if isinstance(content, str) else content
        return _result(spec, float(judge["score"]), passed=bool(judge["passed"]), reason=str(judge["reason"]), evidence=list(judge.get("evidence_refs") or []), confidence=float(judge.get("confidence", 0.0)))
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, json.JSONDecodeError) as exc:
        return _result(spec, None, passed=None, reason=f"LLM Judge 失败：{exc}", status="failed")


def _detect_hard_failures(task: dict[str, Any], run: dict[str, Any], grades: list[dict[str, Any]]) -> list[str]:
    configured = set(task.get("hard_failures") or [])
    found = {failure for grade in grades for failure in grade.get("hard_failures", [])}
    forbidden_tools = set((task.get("expected") or {}).get("forbidden_tools") or [])
    for grader in task.get("graders") or []:
        forbidden_tools.update((grader.get("config") or {}).get("forbidden_tools") or [])
    for event in run.get("events") or []:
        if event.get("event_type") in {"tool_call", "tool_call_started"}:
            name = (event.get("payload") or {}).get("name")
            if name in forbidden_tools:
                found.add("forbidden_tool")
    text = _final_text(run.get("final_output"))
    if re.search(r"(?i)(api[_-]?key|authorization|bearer)\s*[:=]\s*[a-z0-9._-]{12,}", text):
        found.add("secret_exposure")
    return sorted(found & configured)


def grade_trial(
    *,
    task: dict[str, Any],
    run: dict[str, Any],
    fixture: dict[str, Any],
    registered_graders: dict[str, dict[str, Any]],
    executable_root: Path | None = None,
    enable_executable: bool = False,
) -> dict[str, Any]:
    specs: list[dict[str, Any]] = []
    for source in task.get("graders") or []:
        spec = dict(source)
        registered = registered_graders.get(str(spec.get("id")))
        if registered:
            spec = {**registered.get("config", {}), **spec}
            spec.setdefault("type", registered["grader_type"])
            spec.setdefault("version", registered["version"])
        specs.append(spec)
    grades: list[dict[str, Any]] = []
    for spec in specs:
        grader_type = str(spec.get("type", "rule"))
        if grader_type == "terminal_bench":
            verification = run.get("official_verification") or {}
            reward = verification.get("reward")
            expected_digest = (task.get("terminal_bench") or {}).get("task_digest")
            valid = (verification.get("source") == "terminal-bench/harbor"
                     and verification.get("task_id") == task["id"]
                     and bool(expected_digest) and verification.get("task_digest") == expected_digest
                     and not verification.get("error")
                     and not isinstance(reward, bool) and isinstance(reward, (int, float)) and reward in (0, 1))
            grades.append(_result(spec, float(reward) * 100 if valid else None,
                                  passed=bool(reward) if valid else None,
                                  reason=(f"Terminal-Bench official verifier reward={reward}" + (f"；执行状态：{verification['execution_issue']}（评分前已停止）" if verification.get('execution_issue') else '')) if valid else (verification.get('error') or "官方验证结果缺失、异常或任务标识不匹配；不计为 Agent 答错"),
                                  evidence=[verification["result_path"]] if verification.get("result_path") else [],
                                  status="completed" if valid else "failed"))
        elif grader_type == "rule":
            grades.append(_generic_rule(spec, task, run))
        elif grader_type == "schema":
            grades.append(_schema_grade(spec, task, run))
        elif grader_type == "executable":
            grades.append(_executable_grade(spec, task, run, executable_root=executable_root, enabled=enable_executable))
        elif grader_type == "llm_judge":
            grades.append(_llm_grade(spec, task, run))
        elif grader_type == "human":
            grades.append(_result(spec, None, passed=None, reason="等待人工评审", status="pending"))
        else:
            grades.append(_result(spec, None, passed=None, reason=f"不支持的 Grader 类型：{grader_type}", status="failed"))

    hard_failures = _detect_hard_failures(task, run, grades)
    if hard_failures:
        if not any(grade.get("hard_failures") for grade in grades):
            grades.append(
                _result(
                    {"id": "hard-failure-v1", "type": "rule", "version": "1.0.0"},
                    0.0,
                    passed=False,
                    reason=f"命中硬失败：{', '.join(hard_failures)}",
                    evidence=[f"event:{index + 1}" for index, _ in enumerate(run.get("events") or [])] + list(run.get("artifacts") or []),
                    hard_failures=hard_failures,
                )
            )
        return {"grades": grades, "score": 0.0, "outcome": "hard_fail", "failure_type": hard_failures[0], "hard_failures": hard_failures, "needs_review": True}
    required_failures = [grade for grade, spec in zip(grades, specs) if grade["status"] == "failed" and bool(spec.get("required", True))]
    if required_failures:
        issue = (run.get('official_verification') or {}).get('error_code') or 'grader_failed'
        infrastructure = issue in {'verifier_setup_failed','agent_termination_failed','agent_boundary_violation','agent_boundary_unverified','harbor_runtime_error'}
        return {"grades": grades, "score": None, "outcome": "infra_failed" if infrastructure else "grader_failed", "failure_type": issue, 'failure_stage': 'auto_grading', "hard_failures": [], "needs_review": False}
    weighted = [(float(grade["score"]), float(spec.get("weight", 1.0))) for grade, spec in zip(grades, specs) if grade["score"] is not None and grade["status"] == "completed"]
    if not weighted:
        return {"grades": grades, "score": None, "outcome": "grader_failed", "failure_type": "grader_failed", "hard_failures": [], "needs_review": False}
    score = round(sum(value * weight for value, weight in weighted) / sum(weight for _, weight in weighted), 2)
    threshold = float(task.get("pass_threshold", 80))
    needs_review = any(grade["status"] == "pending" or (grade.get("confidence") is not None and grade["confidence"] < 0.7) for grade in grades)
    execution_issue = (run.get('official_verification') or {}).get('execution_issue')
    return {"grades": grades, "score": score, "outcome": "pass" if score >= threshold else "agent_failed" if execution_issue else "unresolved", "failure_type": execution_issue or (None if score >= threshold else "wrong_answer"), 'failure_stage': 'agent_running' if execution_issue else None, "hard_failures": [], "needs_review": needs_review}
