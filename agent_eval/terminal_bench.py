"""Pinned task identities and trustworthy official-verifier result ingestion."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from .common import new_id, redact, utc_now
from .dsh_trajectory import load_session_trajectories

INTEGRATION_FILES = ('agent_eval/harbor_dsh.py', 'agent_eval/dsh_process_guard.js',
    'agent_eval/terminal_bench.py', 'agent_eval/dsh_trajectory.py', 'agent_eval/adapters.py',
    'agent_eval/docker_cleanup.py', 'agent_eval/grading_pipeline.py', 'scripts/run_harbor_trial.py')
AGENT_EXCEPTIONS = {'AgentTimeoutError', 'NonZeroAgentExitCodeError'}


def task_digest(task_dir: Path) -> str:
    entries = []
    files = (path for path in task_dir.rglob("*") if path.is_file())
    for path in sorted(files, key=lambda item: item.relative_to(task_dir).as_posix()):
        entries.append([path.relative_to(task_dir).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()])
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


def official_reward(result: dict[str, Any]) -> float:
    """Missing/invalid verifier output is never counted as an Agent failure/pass."""
    if result.get("exception_info") and result['exception_info'].get('exception_type') not in AGENT_EXCEPTIONS:
        raise ValueError("Harbor trial has an exception; inspect result.json")
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    reward = rewards.get("reward")
    if isinstance(reward, bool) or not isinstance(reward, (int, float)) or not math.isfinite(reward) or reward not in (0, 1):
        raise ValueError("Official binary reward is missing or invalid")
    return float(reward)


def validate_harbor_result_identity(result: dict[str, Any], *, task_dir: Path, trial_name: str) -> None:
    """Reject a completed result that does not describe the requested official trial."""
    if result.get("trial_name") != trial_name:
        raise ValueError("Harbor result trial_name does not match the requested trial")
    config = result.get("config") or {}
    result_task = ((config.get("task") or {}).get("path") or (result.get("task_id") or {}).get("path"))
    if not isinstance(result_task, str) or Path(result_task).resolve() != task_dir.resolve():
        raise ValueError("Harbor result task path does not match the pinned task")
    environment = config.get("environment") or {}
    if environment.get("type") != "docker" or environment.get("delete") is not True:
        raise ValueError("Harbor result did not use a disposable Docker environment")


def _phase_failed(result: dict[str, Any], phase: str) -> bool:
    timing = result.get(phase) or {}
    return bool(timing.get("started_at")) and not bool(timing.get("finished_at"))


def _harbor_exception(result: dict[str, Any], kind: str) -> tuple[str, str]:
    """Classify Harbor failures by the phase that actually failed."""
    if kind == "VerifierTimeoutError":
        return "verifier_timeout", "官方评分阶段超时，未取得完整测试结果"
    # Harbor records finished_at for a phase even when that phase raises. Infer the
    # failing phase from the last phase reached as well as explicit exception names.
    if result.get("verifier") or kind.startswith("Verifier"):
        return "verifier_failed", f"官方评分阶段异常：{kind}"
    if result.get("agent_execution"):
        return "agent_execution_failed", f"Agent 执行基础设施异常：{kind}"
    if result.get("agent_setup") or "AgentSetup" in kind:
        return "agent_setup_failed", f"Agent 安装或初始化失败：{kind}"
    if result.get("environment_setup") or kind.startswith("Environment"):
        return "environment_setup_failed", f"任务容器准备失败：{kind}"
    return "harbor_runtime_error", f"Harbor 运行异常：{kind}"


def verifier_evidence(result: dict[str, Any], trial_dir: Path, events: list[dict]) -> dict:
    """Keep raw reward separate from a usable score, preserving all failure dimensions."""
    exception = result.get('exception_info') or {}
    kind = exception.get('exception_type')
    raw_reward = ((result.get('verifier_result') or {}).get('rewards') or {}).get('reward')
    value = {'raw_reward': raw_reward, 'reward': None, 'error': None, 'error_code': None,
             'execution_issue': 'agent_timeout' if kind == 'AgentTimeoutError' else 'agent_exit_error' if kind == 'NonZeroAgentExitCodeError' else None}
    def invalid(code, reason):
        return {**value, 'reward': None, 'error_code': code, 'error': reason}
    if 'AgentTerminationError' in str(exception.get('exception_message') or ''):
        return invalid('agent_termination_failed', '未确认执行进程停止，已禁止评分')
    if kind and kind not in AGENT_EXCEPTIONS:
        code, reason = _harbor_exception(result, str(kind))
        return invalid(code, reason)
    try:
        reward = official_reward(result)
    except ValueError as error:
        return invalid('verifier_result_missing', str(error))
    report_path = trial_dir/'verifier/ctrf.json'
    if not report_path.is_file():
        log = trial_dir/'verifier/test-stdout.txt'
        text = log.read_text(encoding='utf-8', errors='replace') if log.is_file() else ''
        markers = ('Failed to download', 'Failed to fetch', 'No matching distribution',
                   'No module named pytest', 'uvx: command not found', 'curl: command not found')
        if any(marker in text for marker in markers):
            return invalid('verifier_setup_failed', '测试依赖准备失败，测试未正常启动；原始 reward 单独保留')
        return invalid('verifier_evidence_missing', '缺少官方 CTRF 测试报告，无法确认评分过程完整')
    try:
        ctrf = json.loads(report_path.read_text(encoding='utf-8'))['results']
        summary = ctrf['summary']
        tests = ctrf['tests']
        total = int(summary['tests'])
        complete = total > 0 and len(tests) == total and sum(int(summary.get(k) or 0) for k in ('passed','failed','skipped')) == total
        complete = complete and all(t.get('status') in ('passed','failed','skipped') for t in tests)
        counts_match = all(sum(t.get('status') == k for t in tests) == int(summary.get(k) or 0) for k in ('passed','failed','skipped'))
        if not complete or not counts_match:
            raise ValueError('测试报告未完整结束')
        if bool(reward) != (int(summary.get('failed') or 0) == 0):
            raise ValueError('reward 与逐项测试结论不一致')
    except (KeyError, TypeError, ValueError) as error:
        return invalid('verifier_evidence_invalid', f'官方测试报告无效：{error}')
    value['test_summary'] = {k:summary.get(k) for k in ('tests','passed','failed','skipped')}
    value['failed_tests'] = [t.get('name') for t in tests if t.get('status') == 'failed']
    if kind in AGENT_EXCEPTIONS:
        records = list((trial_dir/'agent').glob('guard-*/termination.json'))
        try:
            stops = [json.loads(p.read_text(encoding='utf-8')) for p in records]
            start = datetime.fromisoformat(result['verifier']['started_at'].replace('Z','+00:00'))
            confirmed = any(s.get('verified') is True and datetime.fromisoformat(s['finished_at'].replace('Z','+00:00')) <= start for s in stops)
            if not confirmed:
                raise ValueError('缺少评分前的进程终止确认')
            value['termination_verified'] = True
        except (KeyError, ValueError, TypeError) as error:
            return invalid('agent_boundary_unverified', str(error))
    timing = result.get('verifier') or {}
    if timing.get('started_at'):
        start = datetime.fromisoformat(timing['started_at'].replace('Z','+00:00'))
        for event in events:
            if event.get('event_type') == 'tool_call' and event.get('timestamp'):
                if datetime.fromisoformat(event['timestamp'].replace('Z','+00:00')) >= start:
                    return invalid('agent_boundary_violation', '官方评分开始后仍有执行 Agent 工具调用')
    return {**value, 'reward': reward}


def load_codex_trace(trial_dir: Path) -> dict[str, Any]:
    """Load the native Codex JSONL retained by Harbor without altering it."""
    events: list[dict[str, Any]] = []
    parse_errors: list[dict[str, Any]] = []
    final_output = None
    session_files = sorted((trial_dir / "agent" / "sessions").rglob("*.jsonl"))
    for session in session_files:
        for line_number, line in enumerate(session.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as error:
                parse_errors.append({"session_file": str(session), "line": line_number, "error": str(error)})
                continue
            payload = entry.get("payload") or {}
            timestamp = entry.get("timestamp") or utc_now()
            if entry.get("type") != "response_item":
                continue
            item_type = payload.get("type")
            if item_type == "message" and payload.get("role") == "assistant":
                content = "\n".join(
                    str(part.get("text") or "") for part in payload.get("content") or []
                    if isinstance(part, dict) and part.get("type") in {"output_text", "text"}
                ).strip()
                if content:
                    final_output = {"type": "text", "content": content}
                    events.append({"timestamp": timestamp, "event_type": "agent_message", "status": "completed",
                                   "payload": final_output})
            elif item_type in {"custom_tool_call", "function_call", "local_shell_call"}:
                events.append({"timestamp": timestamp, "event_type": "tool_call", "status": "completed",
                               "payload": {"name": payload.get("name") or item_type,
                                           "call_id": payload.get("call_id"),
                                           "arguments": payload.get("arguments") or payload.get("input")}})
            elif item_type in {"custom_tool_call_output", "function_call_output", "local_shell_call_output"}:
                events.append({"timestamp": timestamp, "event_type": "tool_result", "status": "completed",
                               "payload": {"call_id": payload.get("call_id"), "output": payload.get("output")}})
            elif item_type == "reasoning":
                events.append({"timestamp": timestamp, "event_type": "model_step_finished", "status": "completed",
                               "payload": {"summary": payload.get("summary")}})
    usage = {}
    transcript = trial_dir / "agent" / "codex.txt"
    if transcript.is_file():
        for line_number, line in enumerate(transcript.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # Codex startup notices are plain text and remain in the original file.
            item = entry.get("item") or {}
            if entry.get("type") == "item.completed" and item.get("type") == "agent_message" and item.get("text"):
                final_output = {"type": "text", "content": str(item["text"])}
            if entry.get("type") == "turn.completed" and isinstance(entry.get("usage"), dict):
                usage = entry["usage"]
    return {"events": events, "final_output": final_output, "usage": usage,
            "session_count": len(session_files), "parse_errors": parse_errors}


def normalise_result(
    result: dict[str, Any],
    *,
    trial_dir: Path,
    task_id: str,
    digest: str,
    harness: str = "dsh",
    model: str = "",
) -> dict[str, Any]:
    trajectories = load_session_trajectories(trial_dir / "agent" / "sessions") if harness == "dsh" else []
    events = []
    codex_trace = load_codex_trace(trial_dir) if harness == "codex" else None
    source_events = codex_trace["events"] if codex_trace else (
        event for trajectory in trajectories for event in trajectory.get("events") or []
    )
    for event in source_events:
        events.append({**event, "sequence": len(events) + 1})
    final_output = codex_trace["final_output"] if codex_trace else (trajectories[0].get("final_output") if trajectories else None)
    stdout = trial_dir / "agent" / "dsh.stdout.txt"
    if not final_output and stdout.is_file():
        final_output = {"type": "text", "content": stdout.read_text(encoding="utf-8", errors="replace")}
    assessed = verifier_evidence(result, trial_dir, events)
    verifier_error = assessed['error']
    verification = {"source": "terminal-bench/harbor", "task_id": task_id, "task_digest": digest,
                    **assessed,
                    "harbor_trial_id": result.get("id"), "harbor_exception": result.get("exception_info"),
                    "result_path": str(trial_dir / "result.json")}
    events.append({"sequence": len(events) + 1, "timestamp": utc_now(), "event_type": "official_verifier_result",
                   "status": "failed" if verifier_error else "completed", "payload": verification})
    context = result.get("agent_result") or {}
    trace_usage = codex_trace["usage"] if codex_trace else {}
    return redact({
        "agent_run_id": new_id("arun"), "status": "completed", "final_output": final_output,
        "events": events, "artifacts": [], "official_verification": verification,
        "usage": {"input_tokens": context.get("n_input_tokens") or trace_usage.get("input_tokens"),
                  "cached_input_tokens": context.get("n_cache_tokens") or trace_usage.get("cached_input_tokens"),
                  "output_tokens": context.get("n_output_tokens") or trace_usage.get("output_tokens"),
                  "reasoning_output_tokens": trace_usage.get("reasoning_output_tokens"),
                  "tool_calls": sum(e.get("event_type") == "tool_call" for e in events),
                  "steps": sum(e.get("event_type") == "model_step_started" for e in events),
                  "estimated_cost": context.get("cost_usd"), "currency": "USD"},
        "trajectory": {"runtime": "codex-cli" if harness == "codex" else "deepseek-harness",
                       "model": model or None, "harbor_trial_dir": str(trial_dir),
                       "session_count": codex_trace["session_count"] if codex_trace else len(trajectories),
                       'parse_errors': codex_trace["parse_errors"] if codex_trace else [
                           {'session_file':t['session_file'], **error}
                           for t in trajectories for error in t.get('parse_errors') or []
                       ]}, "error": None,
    })
