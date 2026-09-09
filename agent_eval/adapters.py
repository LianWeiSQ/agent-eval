from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .common import EvalError, content_hash, new_id, redact, utc_now
from .dsh_trajectory import load_session_trajectories


class AdapterFailure(Exception):
    def __init__(self, failure_type: str, message: str, *, stage: str = "agent_running") -> None:
        super().__init__(message)
        self.failure_type = failure_type
        self.stage = stage


def _event(sequence: int, event_type: str, payload: Any, *, status: str = "completed") -> dict[str, Any]:
    return {
        "sequence": sequence,
        "timestamp": utc_now(),
        "event_type": event_type,
        "status": status,
        "payload": redact(payload),
    }


def _normalise_structured_output(task: dict[str, Any], final_output: Any) -> Any:
    graders = task.get("graders") or []
    if not any(isinstance(grader, dict) and grader.get("type") == "schema" for grader in graders):
        return final_output
    if not isinstance(final_output, dict) or final_output.get("type") != "text":
        return final_output
    content = final_output.get("content")
    if not isinstance(content, str):
        return final_output
    candidate = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return final_output
    return parsed if isinstance(parsed, (dict, list)) else final_output


def _render_dsh_instruction(instruction: str, executable: str) -> str:
    """Keep a DSH task in one argument when Windows launches it through a batch shim."""
    if executable.lower().endswith((".cmd", ".bat")):
        return " ".join(instruction.split())
    return instruction


class AgentAdapter(ABC):
    adapter_type: str

    def healthcheck(self, config: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "adapter_type": self.adapter_type}

    @abstractmethod
    def run(
        self,
        *,
        snapshot: dict[str, Any],
        task: dict[str, Any],
        fixture: dict[str, Any],
        cancel_event: threading.Event,
    ) -> dict[str, Any]: ...


class EchoAdapter(AgentAdapter):
    adapter_type = "echo"

    def run(self, *, snapshot: dict[str, Any], task: dict[str, Any], fixture: dict[str, Any], cancel_event: threading.Event) -> dict[str, Any]:
        del fixture
        started = time.perf_counter()
        if cancel_event.is_set():
            raise AdapterFailure("canceled", "Trial 已取消")
        config = snapshot.get("config") or {}
        correction = task.get("correction") or {}
        task_overrides = config.get("task_overrides") or {}
        task_config = task_overrides.get(task.get("id"), {}) if isinstance(task_overrides, dict) else {}
        phase_name = "corrected" if correction else "initial"
        phase = task_config.get(phase_name, {}) if isinstance(task_config, dict) else {}
        if not isinstance(phase, dict):
            phase = {}
        output = phase.get(
            "output",
            config.get("corrected_output") if correction and "corrected_output" in config else config.get("fixed_output", task["instruction"]),
        )
        final_output = output if isinstance(output, dict) else {"type": "text", "content": str(output)}
        events = []
        if correction:
            events.append(_event(1, "correction_feedback_received", {"feedback": correction.get("feedback"), "parent_attempt_id": correction.get("parent_attempt_id")}))
        fixture_events = phase.get("fixture_events", config.get("fixture_events") or [])
        for source in fixture_events:
            if isinstance(source, dict):
                events.append(_event(len(events) + 1, str(source.get("event_type") or "runtime_event"), source.get("payload") or {}, status=str(source.get("status") or "completed")))
        events.append(_event(len(events) + 1, "assistant_final", final_output))
        return {
            "agent_run_id": new_id("arun"),
            "status": "completed",
            "final_output": final_output,
            "events": events,
            "artifacts": [],
            "usage": {
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "input_tokens": None,
                "output_tokens": None,
                "tool_calls": sum(event["event_type"] == "tool_call" for event in events),
                "steps": 1,
                "estimated_cost": 0.0,
                "currency": "CNY",
            },
            "error": None,
        }


class RuntimeHttpAdapter(AgentAdapter):
    adapter_type = "runtime-http"

    def healthcheck(self, config: dict[str, Any]) -> dict[str, Any]:
        base_url = str(config.get("base_url", "")).rstrip("/")
        if not base_url:
            return {"ok": False, "error": "base_url 未配置"}
        request = urllib.request.Request(base_url + str(config.get("health_path", "/health")), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return {"ok": 200 <= response.status < 300, "status": response.status}
        except Exception as exc:  # health endpoint errors are returned, not raised
            return {"ok": False, "error": str(exc)}

    def run(self, *, snapshot: dict[str, Any], task: dict[str, Any], fixture: dict[str, Any], cancel_event: threading.Event) -> dict[str, Any]:
        del fixture
        config = snapshot.get("config") or {}
        base_url = str(config.get("base_url", "")).rstrip("/")
        if not base_url:
            raise AdapterFailure("invalid_output", "Runtime HTTP Snapshot 缺少 base_url")
        timeout = int(task.get("timeout_seconds", config.get("timeout_seconds", 120)))
        payload = {
            "agent_id": config.get("agent_id"),
            "input": task["instruction"],
            "model": config.get("model"),
            "config": {
                **(config.get("run_config") or {}),
                "max_steps": (config.get("budget") or {}).get("max_steps", 50),
            },
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
            "X-Tenant-ID": snapshot["tenant_id"],
            "X-Project-ID": snapshot["project_id"],
        }
        auth_env = config.get("auth_token_env")
        if auth_env and os.environ.get(str(auth_env)):
            headers["Authorization"] = "Bearer " + os.environ[str(auth_env)]
        request = urllib.request.Request(
            base_url + str(config.get("run_path", "/agent/run")),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        started = time.perf_counter()
        events: list[dict[str, Any]] = []
        text_parts: list[str] = []
        usage: dict[str, Any] = {"tool_calls": 0, "steps": 0, "estimated_cost": 0.0, "currency": "CNY"}
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content_type = response.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    body = json.loads(response.read().decode("utf-8"))
                    data = body.get("data", body)
                    final = data.get("final_output") or data.get("output") or data
                    events.append(_event(1, "assistant_final", final))
                else:
                    for raw_line in response:
                        if cancel_event.is_set():
                            raise AdapterFailure("canceled", "Trial 已取消")
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line.startswith("data:"):
                            continue
                        try:
                            source_event = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        source_type = str(source_event.get("type", "runtime_event"))
                        mapped_type = {
                            "tool-call": "tool_call",
                            "tool-result": "tool_result",
                            "patch": "artifact_created",
                            "error": "error",
                            "finish": "session_finished",
                            "step-start": "model_step_started",
                            "step-finish": "model_step_finished",
                        }.get(source_type, source_type.replace("-", "_"))
                        events.append(_event(len(events) + 1, mapped_type, source_event))
                        if source_type == "text-delta":
                            text_parts.append(str(source_event.get("text", "")))
                        elif source_type == "tool-call":
                            usage["tool_calls"] += 1
                        elif source_type == "step-finish":
                            usage["steps"] += 1
                            tokens = source_event.get("tokens") or {}
                            usage["input_tokens"] = (usage.get("input_tokens") or 0) + int(tokens.get("input", 0))
                            usage["output_tokens"] = (usage.get("output_tokens") or 0) + int(tokens.get("output", 0))
                            usage["estimated_cost"] = (usage.get("estimated_cost") or 0) + float(source_event.get("cost", 0) or 0)
                    final = {"type": "text", "content": "".join(text_parts)}
                    events.append(_event(len(events) + 1, "assistant_final", final))
        except AdapterFailure:
            raise
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise AdapterFailure("agent_crash", f"Runtime HTTP 返回 HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            failure = "agent_timeout" if "timed out" in str(exc).lower() else "agent_crash"
            raise AdapterFailure(failure, f"Runtime HTTP 调用失败：{exc}") from exc
        usage["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return {
            "agent_run_id": new_id("arun"),
            "status": "completed",
            "final_output": redact(final),
            "events": events,
            "artifacts": [],
            "usage": usage,
            "error": None,
        }


class LlmSupervisorAdapter(AgentAdapter):
    """Independent LLM supervisor that reviews an execution package without tools."""

    adapter_type = "llm-supervisor"

    def healthcheck(self, config: dict[str, Any]) -> dict[str, Any]:
        key_env = str(config.get("api_key_env", "EVAL_SUPERVISOR_API_KEY"))
        return {
            "ok": bool(os.environ.get(key_env)),
            "adapter_type": self.adapter_type,
            "model": config.get("model", "GLM-5.3"),
            "base_url": config.get("base_url", ""),
            "key_environment": key_env,
            "message": "Key 环境变量已设置" if os.environ.get(key_env) else f"请设置环境变量 {key_env}",
        }

    def run(self, *, snapshot: dict[str, Any], task: dict[str, Any], fixture: dict[str, Any], cancel_event: threading.Event) -> dict[str, Any]:
        del fixture
        if cancel_event.is_set():
            raise AdapterFailure("canceled", "监督检查已取消")
        config = snapshot.get("config") or {}
        key_env = str(config.get("api_key_env", "EVAL_SUPERVISOR_API_KEY"))
        api_key = os.environ.get(key_env)
        if not api_key:
            raise AdapterFailure("agent_crash", f"Supervisor API Key 环境变量未设置：{key_env}")
        review_package = task.get("supervision_input")
        if not isinstance(review_package, dict):
            raise AdapterFailure("invalid_arguments", "Supervisor 缺少 supervision_input")
        started = time.perf_counter()
        events = [_event(1, "session_started", {"adapter": self.adapter_type, "model": config.get("model", "GLM-5.3")})]
        schema = {
            "verdict": "pass | fail | uncertain",
            "error_types": ["evidence_mismatch | wrong_tool_arguments | invalid_output | external_api_failure | insufficient_evidence | other"],
            "reason": "基于可观察证据的判断理由",
            "suggestion": "不直接泄漏标准答案的修改建议",
            "evidence_refs": ["event:序号 或 grade:评分器ID"],
            "confidence": "0 到 1",
            "answer_leakage_risk": "low | medium | high",
        }
        review_content = json.dumps({"output_schema": schema, "review_package": review_package}, ensure_ascii=False)
        if len(review_content) > 256000:
            raise AdapterFailure("input_too_large", "规范化后的监督证据仍超过 256000 字符，已停止发送以避免上下文超限；需进一步分段审查，原始轨迹完整保留。", stage="supervisor_input")
        messages = [
            {
                "role": "system",
                "content": (
                    "你是独立的 Agent 评测监督员。只能依据提供的任务要求、执行轨迹、工具返回结果和官方评分理由审查。"
                    "禁止猜测或输出未提供的标准答案；建议只能指出检查方向、约束和证据一致性。"
                    "必须只输出一个 JSON 对象，不要 Markdown，不要额外文字。JSON 字段必须完整。"
                    "输入按 input_policy 排除了流式片段和重复日志，请引用保留的原始 event 序号；不能依据未提供的内容作出判断。"
                ),
            },
            {"role": "user", "content": review_content},
        ]
        base_url = str(config.get("base_url", "")).rstrip("/")
        if not base_url:
            raise AdapterFailure("invalid_arguments", "Supervisor 未配置 base_url")
        endpoint = base_url + str(config.get("chat_path", "/chat/completions"))
        payload = {
            "model": config.get("model", "GLM-5.3"),
            "messages": messages,
            "temperature": 0,
            "max_tokens": int((config.get("budget") or {}).get("max_output_tokens", 4000)),
            "response_format": {"type": "json_object"},
        }
        if config.get("thinking") is not None:
            payload["thinking"] = {"type": str(config["thinking"])}
        request = urllib.request.Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json", "Accept": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=int(task.get("timeout_seconds", 120))) as response:
                response_body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            raise AdapterFailure("provider_unavailable", f"Supervisor HTTP {exc.code}: {redact(detail)}", stage="provider_call") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise AdapterFailure("provider_unavailable", f"Supervisor 调用失败：{exc}", stage="provider_call") from exc
        if cancel_event.is_set():
            raise AdapterFailure("canceled", "监督检查已取消")
        validated, raw_content, metadata = self._parse_response(response_body)
        metadata["input_chars"] = len(review_content)
        metadata["input_policy"] = review_package.get("input_policy")
        usage = response_body.get("usage") or {}
        events.append(_event(2, "model_step_finished", {**metadata, "usage": usage}))
        events.append(_event(3, "assistant_final", validated))
        return {
            "agent_run_id": new_id("srun"),
            "status": "completed",
            "final_output": validated,
            "events": events,
            "artifacts": [],
            "usage": {
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
                "output_tokens": int(usage.get("completion_tokens", 0) or 0),
                "tool_calls": 0,
                "steps": 1,
                "estimated_cost": None,
                "currency": "CNY",
            },
            "raw_output": raw_content,
            "response_metadata": metadata,
            "error": None,
        }

    @classmethod
    def _parse_response(cls, body: Any) -> tuple[dict[str, Any], str, dict[str, Any]]:
        choices = body.get("choices") if isinstance(body, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise AdapterFailure("invalid_output", "Supervisor 响应缺少有效 choices", stage="supervisor_output")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise AdapterFailure("invalid_output", "Supervisor 响应缺少 message", stage="supervisor_output")
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        details = usage.get("completion_tokens_details") or {}
        metadata = {
            "finish_reason": choice.get("finish_reason"),
            "completion_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": details.get("reasoning_tokens") if isinstance(details, dict) else None,
            "has_reasoning_content": bool(message.get("reasoning_content")),
        }
        diagnostic = json.dumps(metadata, ensure_ascii=False)
        if choice.get("finish_reason") == "length":
            raise AdapterFailure("output_truncated", f"Supervisor 输出达到 token 上限；请使用关闭思考模式或更高输出预算的新快照。{diagnostic}", stage="supervisor_output")
        if message.get("refusal") or choice.get("finish_reason") == "content_filter":
            raise AdapterFailure("invalid_output", f"Supervisor 拒绝输出监督结论。{diagnostic}", stage="supervisor_output")
        raw_content = message.get("content")
        if not isinstance(raw_content, str) or not raw_content.strip():
            raise AdapterFailure("invalid_output", f"Supervisor 最终正文为空，不能视为 uncertain 结论。{diagnostic}", stage="supervisor_output")
        candidate = raw_content.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, flags=re.DOTALL | re.IGNORECASE)
        if fenced:
            candidate = fenced.group(1).strip()
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise AdapterFailure("invalid_output", f"Supervisor 最终正文不是完整 JSON。{diagnostic}", stage="supervisor_output") from exc
        return cls._validate_result(parsed), raw_content, metadata

    @staticmethod
    def _validate_result(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise AdapterFailure("invalid_output", "Supervisor 输出必须是 JSON 对象")
        required = {"verdict", "reason", "suggestion", "error_types", "evidence_refs", "confidence", "answer_leakage_risk"}
        if required - value.keys():
            raise AdapterFailure("invalid_output", "Supervisor JSON 缺少必需字段：" + ", ".join(sorted(required - value.keys())), stage="supervisor_output")
        for key in ("verdict", "reason"):
            if not isinstance(value[key], str) or not value[key].strip():
                raise AdapterFailure("invalid_output", f"Supervisor {key} 必须是非空文本", stage="supervisor_output")
        if not isinstance(value["suggestion"], str):
            raise AdapterFailure("invalid_output", "Supervisor suggestion 必须是文本", stage="supervisor_output")
        verdict_raw = str(value.get("verdict") or "").strip().lower()
        verdict = {
            "passed": "pass",
            "通过": "pass",
            "成功": "pass",
            "failed": "fail",
            "failure": "fail",
            "unresolved": "fail",
            "hard_fail": "fail",
            "hard-fail": "fail",
            "失败": "fail",
            "未通过": "fail",
            "不通过": "fail",
            "unknown": "uncertain",
            "unclear": "uncertain",
            "不确定": "uncertain",
            "待定": "uncertain",
        }.get(verdict_raw, verdict_raw)
        if verdict not in {"pass", "fail", "uncertain"}:
            if any(marker in verdict_raw for marker in ("uncertain", "unknown", "unclear", "不确定", "待定")):
                verdict = "uncertain"
            elif any(marker in verdict_raw for marker in ("fail", "unresolved", "hard_fail", "hard-fail", "失败", "未通过", "不通过", "错误")):
                verdict = "fail"
            elif any(marker in verdict_raw for marker in ("pass", "通过", "成功")):
                verdict = "pass"
            else:
                verdict = "uncertain"
        error_types = value.get("error_types") or []
        evidence_refs = value.get("evidence_refs") or []
        if isinstance(error_types, str):
            error_types = [item.strip() for item in re.split(r"[,，;；、]", error_types) if item.strip()]
        if isinstance(evidence_refs, str):
            evidence_refs = [item.strip() for item in re.split(r"[,，;；、]", evidence_refs) if item.strip()]
        if not isinstance(error_types, list):
            error_types = []
        if not isinstance(evidence_refs, list):
            evidence_refs = []
        error_types = [str(item) for item in error_types]
        evidence_refs = [str(item) for item in evidence_refs]
        confidence_raw = str(value.get("confidence", "0.5")).strip().rstrip("%")
        try:
            confidence = float(confidence_raw)
            if str(value.get("confidence", "")).strip().endswith("%"):
                confidence /= 100
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))
        risk_raw = str(value.get("answer_leakage_risk") or "low").strip().lower()
        risk = {"低": "low", "中": "medium", "高": "high"}.get(risk_raw, risk_raw)
        if risk not in {"low", "medium", "high"}:
            risk = "low"
        return {
            "verdict": verdict,
            "error_types": error_types,
            "reason": str(value.get("reason") or "").strip(),
            "suggestion": str(value.get("suggestion") or "").strip(),
            "evidence_refs": evidence_refs,
            "confidence": confidence,
            "answer_leakage_risk": risk,
        }


class DeepSeekHarnessHeadlessAdapter(AgentAdapter):
    adapter_type = "dsh-headless"

    def healthcheck(self, config: dict[str, Any]) -> dict[str, Any]:
        configured = config.get("command") or ["dsh"]
        command = config.get("health_command") or [configured[0], "--version"]
        cwd_value = config.get("working_directory")
        cwd = Path(str(cwd_value)).expanduser() if cwd_value else None
        if cwd is not None and not cwd.is_absolute():
            cwd = (Path.cwd() / cwd).resolve()
        if cwd is not None and not cwd.is_dir():
            return {"ok": False, "error": f"DSH 工作目录不存在：{cwd}"}
        try:
            timeout = max(5, min(int(config.get("health_timeout_seconds", 30)), 60))
            completed = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                check=False,
            )
            return {"ok": completed.returncode == 0, "exit_code": completed.returncode, "version": completed.stdout.strip()[:300]}
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "error": str(exc)}

    def run(self, *, snapshot: dict[str, Any], task: dict[str, Any], fixture: dict[str, Any], cancel_event: threading.Event) -> dict[str, Any]:
        del fixture
        config = snapshot.get("config") or {}
        raw_command = config.get("command")
        if not isinstance(raw_command, list) or not raw_command or not all(isinstance(item, str) for item in raw_command):
            raise AdapterFailure("invalid_output", "DSH Snapshot 的 command 必须是字符串数组")
        project_root = Path.cwd().resolve()
        cwd_value = config.get("working_directory")
        cwd = (project_root / str(cwd_value)).resolve() if cwd_value else project_root
        patch_value = config.get("patch_file")
        patch_file = (project_root / str(patch_value)).resolve() if patch_value else None
        session_root_value = config.get("session_root") or ".data/dsh-sessions"
        session_root = (project_root / str(session_root_value)).resolve()
        run_session_root = session_root / new_id("dsh-session")
        run_session_root.mkdir(parents=True, exist_ok=False)
        instruction = _render_dsh_instruction(str(task["instruction"]), raw_command[0])
        replacements = {
            "{instruction}": instruction,
            "{working_directory}": str(cwd),
            "{session_root}": str(run_session_root),
            "{patch_file}": str(patch_file) if patch_file else "",
        }
        command = []
        for part in raw_command:
            rendered = part
            for token, value in replacements.items():
                rendered = rendered.replace(token, value)
            command.append(rendered)
        if not any("{instruction}" in part for part in raw_command):
            command.append(instruction)
        if not cwd.is_dir():
            raise AdapterFailure("environment_create_failed", f"DSH 工作目录不存在：{cwd}", stage="environment_preparing")
        if patch_file and not patch_file.is_file():
            raise AdapterFailure("environment_create_failed", f"DSH patch 不存在：{patch_file}", stage="environment_preparing")
        inherited_names = (
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "PROGRAMDATA",
            "HOMEDRIVE",
            "HOMEPATH",
            "PNPM_HOME",
            "DSH_HOME",
            "DEEPSEEK_API_KEY",
            "DEEPSEEK_BASE_URL",
            "DEEPSEEK_SEARCH_BASE_URL",
            "DEEPSEEK_MODEL",
        )
        child_env = {name: os.environ[name] for name in inherited_names if os.environ.get(name) is not None}
        child_env.update(
            {
                "DSH_EVAL_SESSION_ROOT": str(run_session_root),
                "DSH_SESSION_ROOT": str(run_session_root),
                "DSH_TELEMETRY_MODE": "DISABLED",
                "DSH_TELEMETRY_DISABLED": "1",
                "DSH_PERMISSION_MODE": str(config.get("permission_mode") or "read-only"),
            }
        )
        for target, value in (config.get("environment") or {}).items():
            child_env[str(target)] = str(value)
        for target, source in (config.get("environment_from") or {}).items():
            if os.environ.get(str(source)) is not None:
                child_env[str(target)] = os.environ[str(source)]
        started = time.perf_counter()
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=child_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
            )
            timeout_at = time.monotonic() + int(task.get("timeout_seconds", 120))
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    if cancel_event.is_set():
                        process.terminate()
                        process.communicate()
                        raise AdapterFailure("canceled", "Trial 已取消")
                    if time.monotonic() >= timeout_at:
                        process.kill()
                        process.communicate()
                        raise AdapterFailure("agent_timeout", "DSH 执行超时")
        except AdapterFailure:
            raise
        except OSError as exc:
            raise AdapterFailure("agent_crash", f"无法启动 DSH：{exc}") from exc
        stdout = stdout if isinstance(stdout, str) else ""
        stderr = stderr if isinstance(stderr, str) else ""
        if process.returncode != 0:
            raise AdapterFailure("agent_crash", f"DSH 退出码 {process.returncode}: {redact(stderr[-1000:])}")
        trajectories = load_session_trajectories(run_session_root)
        trajectory = trajectories[0] if trajectories else None
        events = list(trajectory["events"]) if trajectory else []
        events_path = config.get("events_path")
        if not events and events_path:
            event_file = (cwd / str(events_path)).resolve() if cwd else Path(str(events_path)).resolve()
            if event_file.is_file():
                for line in event_file.read_text(encoding="utf-8-sig").splitlines():
                    if line.strip():
                        source = json.loads(line)
                        events.append(_event(len(events) + 1, str(source.get("event_type", "dsh_event")), source))
        final_output = trajectory["final_output"] if trajectory and trajectory["final_output"].get("content") else {"type": "text", "content": stdout.strip()}
        final_output = _normalise_structured_output(task, final_output)
        events.append(_event(len(events) + 1, "assistant_final", final_output))
        trajectory_usage = trajectory["usage"] if trajectory else {}
        return {
            "agent_run_id": new_id("arun"),
            "status": "completed",
            "final_output": redact(final_output),
            "events": events,
            "artifacts": [],
            "usage": {
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "input_tokens": trajectory_usage.get("input_tokens"),
                "output_tokens": trajectory_usage.get("output_tokens"),
                "tool_calls": trajectory_usage.get("tool_calls", sum(event["event_type"] == "tool_call" for event in events)),
                "steps": trajectory_usage.get("steps", len(events)),
                "estimated_cost": None,
                "currency": "CNY",
                "exit_code": process.returncode,
            },
            "process": {"command": command[:1] + ["[ARGS REDACTED]"], "stdout": redact(stdout), "stderr": redact(stderr), "exit_code": process.returncode},
            "trajectory": {
                "runtime": "deepseek-harness",
                "session_id": trajectory.get("session_id") if trajectory else None,
                "session_file": trajectory.get("session_file") if trajectory else None,
                "child_sessions": [item.get("session_id") for item in trajectories[1:]],
                "raw_event_count": trajectory.get("raw_event_count", 0) if trajectory else 0,
            },
            "error": None,
        }


class TerminalBenchHarborAdapter(AgentAdapter):
    adapter_type = "terminal-bench-harbor"

    def healthcheck(self, config: dict[str, Any]) -> dict[str, Any]:
        python = Path(str(config.get("python") or ""))
        dataset = Path(str(config.get("dataset_root") or ""))
        result = {"ok": False, "adapter_type": self.adapter_type,
                  "python_available": python.is_file(), "dataset_available": dataset.is_dir(),
                  "docker_available": False}
        if not result["python_available"] or not result["dataset_available"]:
            return {**result, "message": "Terminal-Bench 的 Python 环境或任务目录不可用，请检查 Agent 配置。"}
        try:
            probe = subprocess.run(["docker", "info", "--format", "{{.OSType}}"],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
        except FileNotFoundError:
            return {**result, "message": "找不到 Docker 命令，请先安装并启动 Docker Desktop。"}
        except (OSError, subprocess.TimeoutExpired):
            return {**result, "message": "Docker 连接失败或超时，请确认 Docker Desktop 已启动，并在普通用户终端启动 Eval。"}
        error = probe.stderr.lower()
        if any(value in error for value in ("access is denied", "permission denied", "拒绝访问")):
            return {**result, "message": "Eval 无权访问 Docker 配置或引擎。请在普通用户 PowerShell 中重新启动 Eval。"}
        if probe.returncode != 0:
            return {**result, "message": "Docker 引擎尚不可用，请启动 Docker Desktop，等待引擎就绪后重新检查。"}
        if probe.stdout.strip() != "linux":
            return {**result, "message": "Terminal-Bench 需要 Linux 容器，请将 Docker Desktop 切换到 Linux 容器模式。"}
        return {**result, "ok": True, "docker_available": True,
                "message": "Docker Linux 引擎可连接，任务目录与 Python 环境可用。",
                "note": "此检查不调用模型；模型凭据和具体任务镜像仍由运行阶段验证。"}

    def run(self, *, snapshot: dict[str, Any], task: dict[str, Any], fixture: dict[str, Any], cancel_event: threading.Event) -> dict[str, Any]:
        from .terminal_bench import INTEGRATION_FILES, normalise_result, task_digest
        config = snapshot.get("config") or {}
        source = task.get("terminal_bench") or {}
        dataset = Path(config["dataset_root"]).resolve()
        task_dir = (dataset / source["task_name"]).resolve()
        if not task_dir.is_relative_to(dataset) or not task_dir.is_dir():
            raise AdapterFailure("invalid_task_path", "Terminal-Bench task must be inside the pinned dataset", stage="environment_preparing")
        digest = task_digest(task_dir)
        if digest != source.get("task_digest"):
            raise AdapterFailure("task_changed", "Terminal-Bench task hash changed; re-import an explicitly versioned dataset", stage="environment_preparing")
        output = Path(config["output_root"]).resolve() / new_id("harbor")
        output.mkdir(parents=True)
        trial_name = source["task_name"] + "__" + output.name
        result_path = output / "completed-result.json"
        cancel_path = output / "cancel"
        request = {"task_dir": str(task_dir), "trial_name": trial_name, "trials_dir": str(output),
                   "result_path": str(result_path), "cancel_path": str(cancel_path),
                   'verifier_preflight': (config.get('verifier_preflight_tasks') or {}).get(source['task_name'])}
        if task.get("correction"):
            request["extra_instructions"] = ["Human-approved feedback from the previous attempt:\n" + str(task["correction"]["feedback"])]
        request_path = output / "request.json"
        request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
        root = Path(__file__).resolve().parents[1]
        import hashlib
        runtime_archive = root / ".terminal-bench/runtime-cache/dsh-runtime.tgz"
        if not runtime_archive.is_file():
            raise AdapterFailure("runtime_missing", "Prepare the pinned DSH runtime archive first", stage="environment_preparing")
        with runtime_archive.open("rb") as stream:
            runtime_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        if runtime_hash != config.get("runtime_archive_sha256"):
            raise AdapterFailure("runtime_changed", "DSH runtime archive does not match the immutable snapshot", stage="environment_preparing")
        source_dir = output / "runtime-source"
        source_dir.mkdir()
        files = config.get('integration_files') or ('agent_eval/harbor_dsh.py','agent_eval/terminal_bench.py',
                  'agent_eval/adapters.py','agent_eval/grading_pipeline.py','scripts/run_harbor_trial.py')
        if not set(files).issubset(INTEGRATION_FILES):
            raise AdapterFailure('integration_changed', '快照中的适配代码文件清单无效')
        if config.get('integration_digest') and content_hash({name:(root/name).read_text(encoding='utf-8') for name in files}) != config['integration_digest']:
            raise AdapterFailure('integration_changed', '适配代码已更新，请选择与当前代码匹配的新 Agent 快照')
        for name in files:
            (source_dir / Path(name).name).write_bytes((root / name).read_bytes())
        started = time.perf_counter()
        child_env = dict(os.environ)
        child_env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "DO_NOT_TRACK": "1"})
        timeout = min(float(config.get("runner_timeout_seconds", 3300)), float(task.get('timeout_seconds') or 3300))
        with (output / "runner.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen([config["python"], str(root / "scripts/run_harbor_trial.py"), str(request_path)],
                                       cwd=root, env=child_env, stdout=log, stderr=subprocess.STDOUT)
            canceled_at = None
            while process.poll() is None:
                if cancel_event.wait(0.5) or time.perf_counter() - started > timeout:
                    if canceled_at is None:
                        cancel_path.write_text("cancel", encoding="utf-8")
                        canceled_at = time.perf_counter()
                    elif time.perf_counter() - canceled_at > 120:
                        process.terminate()
                        process.wait(timeout=15)
                        break
                    time.sleep(0.5)
        if cancel_event.is_set():
            raise AdapterFailure("canceled", f"Harbor trial canceled; logs: {output}")
        if not result_path.is_file():
            raise AdapterFailure("harbor_runner_failed", f"Harbor did not produce a completed result; inspect {output / 'runner.log'}", stage="environment_preparing")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        run = normalise_result(result, trial_dir=output / trial_name, task_id=task["id"], digest=digest)
        run["usage"]["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return run


ADAPTERS: dict[str, AgentAdapter] = {
    adapter.adapter_type: adapter
    for adapter in (EchoAdapter(), RuntimeHttpAdapter(), LlmSupervisorAdapter(), DeepSeekHarnessHeadlessAdapter(), TerminalBenchHarborAdapter())
}


def get_adapter(adapter_type: str) -> AgentAdapter:
    adapter = ADAPTERS.get(adapter_type)
    if adapter is None:
        raise EvalError("unknown_adapter", f"未知 Agent Adapter：{adapter_type}")
    return adapter
