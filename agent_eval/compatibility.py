from __future__ import annotations

from typing import Any, Iterable

from .common import EvalError


CAPABILITY_KEYS = ("features", "tools", "events", "domains")


ADAPTER_CAPABILITIES: dict[str, dict[str, list[str]]] = {
    "echo": {
        "features": ["final_output", "structured_output", "events", "usage"],
        "events": ["assistant_final"],
    },
    "runtime-http": {
        "features": ["final_output", "events", "usage"],
        "events": ["*"],
    },
    "dsh-headless": {
        "features": ["final_output", "events", "trace", "usage"],
        "events": ["*"],
        "domains": ["general"],
    },
    "terminal-bench-harbor": {
        "features": ["final_output", "events", "trace", "usage", "tool_calling", "official_verifier"],
        "events": ["*"],
        "domains": ["general"],
    },
    "llm-supervisor": {
        "features": ["final_output", "structured_output", "events", "usage", "supervision"],
        "events": ["session_started", "model_step_finished", "assistant_final"],
        "domains": ["supervision"],
    },
}


def _names(value: Any, *, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise EvalError("invalid_capabilities", f"{label} 必须是字符串数组，且元素不能为空")
    return sorted(set(item.strip() for item in value))


def normalise_capabilities(value: Any, *, label: str = "capabilities") -> dict[str, list[str]]:
    if value is None:
        return {key: [] for key in CAPABILITY_KEYS}
    if not isinstance(value, dict):
        raise EvalError("invalid_capabilities", f"{label} 必须是对象")
    unknown = sorted(set(value) - set(CAPABILITY_KEYS))
    if unknown:
        raise EvalError("invalid_capabilities", f"{label} 包含未知字段：{', '.join(unknown)}")
    return {key: _names(value.get(key), label=f"{label}.{key}") for key in CAPABILITY_KEYS}


def merge_capabilities(*values: dict[str, Iterable[str]]) -> dict[str, list[str]]:
    return {
        key: sorted({str(item) for value in values for item in value.get(key, [])})
        for key in CAPABILITY_KEYS
    }


def snapshot_capabilities(snapshot: dict[str, Any]) -> dict[str, list[str]]:
    adapter_type = str(snapshot.get("adapter_type", ""))
    defaults = normalise_capabilities(ADAPTER_CAPABILITIES.get(adapter_type, {}), label=f"Adapter {adapter_type}")
    declared = normalise_capabilities((snapshot.get("config") or {}).get("capabilities"), label="Snapshot config.capabilities")
    return merge_capabilities(defaults, declared)


def task_requirements(task: dict[str, Any]) -> dict[str, list[str]]:
    requirements = normalise_capabilities(task.get("agent_requirements"), label=f"Task {task.get('id')} agent_requirements")
    features = set(requirements["features"])
    tools = set(requirements["tools"])
    events = set(requirements["events"])

    features.add("final_output")
    for grader in task.get("graders") or []:
        if grader.get("type") == "terminal_bench":
            features.add("official_verifier")
        if grader.get("type") == "schema":
            features.add("structured_output")
        config = {**(task.get("expected") or {}), **(grader.get("config") or {})}
        required_tools = set(str(item) for item in config.get("required_tools", []))
        required_tools.update(str(item.get("name")) for item in config.get("tool_arguments", []) if item.get("name"))
        required_tools.update(str(item) for item in config.get("tool_sequence", []))
        tools.update(required_tools)
        if required_tools:
            features.add("events")
            events.add("tool_call")
        required_events = set(str(item) for item in config.get("required_events", []))
        required_events.update(str(item.get("event_type")) for item in config.get("required_event_payloads", []) if item.get("event_type"))
        if required_events:
            features.add("events")
            events.update(required_events)
        usage_limits = {"max_steps", "max_duration_ms", "max_input_tokens", "max_output_tokens", "max_estimated_cost"}
        if usage_limits.intersection(config):
            features.add("usage")

    if "artifact_created" in events:
        features.add("artifacts")
    if "trace_linked" in events:
        features.add("trace")
    return {
        "features": sorted(features),
        "tools": sorted(tools),
        "events": sorted(events),
        "domains": requirements["domains"],
    }


def benchmark_requirements(manifest: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, list[str]]:
    declared = normalise_capabilities(manifest.get("agent_requirements"), label="Benchmark agent_requirements")
    derived = merge_capabilities(*(task_requirements(task) for task in tasks)) if tasks else normalise_capabilities(None)
    domain = str(manifest.get("domain") or "").strip()
    if domain:
        declared["domains"] = sorted(set(declared["domains"]) | {domain})
    return merge_capabilities(declared, derived)


def _missing(required: list[str], available: list[str]) -> list[str]:
    if "*" in available:
        return []
    return sorted(set(required) - set(available))


def analyse_compatibility(manifest: dict[str, Any], tasks: list[dict[str, Any]], snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    requirements = benchmark_requirements(manifest, tasks)
    agents = []
    for snapshot in snapshots:
        available = snapshot_capabilities(snapshot)
        missing = {key: _missing(requirements[key], available[key]) for key in CAPABILITY_KEYS}
        agents.append(
            {
                "agent_snapshot_id": snapshot["id"],
                "agent": snapshot["name"],
                "compatible": not any(missing.values()),
                "available": available,
                "missing": missing,
            }
        )
    return {
        "compatible": all(item["compatible"] for item in agents),
        "benchmark_type": manifest.get("benchmark_type", "general"),
        "domain": manifest.get("domain"),
        "requirements": requirements,
        "agents": agents,
    }


def incompatibility_message(analysis: dict[str, Any]) -> str:
    details = []
    for agent in analysis["agents"]:
        if agent["compatible"]:
            continue
        missing = [f"{key}={','.join(values)}" for key, values in agent["missing"].items() if values]
        details.append(f"{agent['agent']} 缺少 " + "; ".join(missing))
    return "Benchmark 与 AgentSnapshot 不兼容：" + "；".join(details) + "。如需执行合规负测，请显式设置 compatibility_mode=allow。"
