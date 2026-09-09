from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    if isinstance(value, dict):
        return _text(value.get("content") or value.get("text") or value.get("message"))
    return "" if value is None else str(value)


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    try:
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return str(value)


def _arguments(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
    return value if value is not None else {}


def parse_session_jsonl(path: Path) -> dict[str, Any]:
    """Parse an unpacked DSH JSONL session into the evaluation event contract."""
    header: dict[str, Any] = {}
    sources: list[dict[str, Any]] = []
    parse_errors = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig", errors='replace').splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as error:
            parse_errors.append({'line': line_number, 'column': error.colno, 'message': error.msg})
            continue
        if not isinstance(item, dict):
            parse_errors.append({'line': line_number, 'message': 'Expected a JSON object'})
            continue
        if item.get("type") == "session" and not header:
            header = item
        elif isinstance(item, dict):
            sources.append(item)

    events: list[dict[str, Any]] = []
    call_names: dict[str, str] = {}
    final_text = ""
    input_tokens = 0
    output_tokens = 0
    token_usage_seen = False
    steps: set[tuple[Any, Any]] = set()
    current_turn: Any = None
    current_step: Any = None

    type_map = {
        "turn/start": "turn_started",
        "turn/end": "turn_finished",
        "step/start": "model_step_started",
        "step/end": "model_step_finished",
        "user/message": "user_message",
        "request/header": "model_request",
        "request/context": "model_route",
        "assistant/chunk": "assistant_chunk",
        "assistant/message": "model_response",
        "tool/call": "tool_call",
        "tool/result": "tool_result",
        "approval/asked": "approval_requested",
        "approval/decided": "approval_decided",
    }
    for sequence, source in enumerate(sources, start=1):
        source_type = str(source.get("type") or "event")
        data = source.get("data") if isinstance(source.get("data"), dict) else source
        turn = data.get("turn") or data.get("turnId") or source.get("turn") or source.get("turnId")
        step = data.get("step") or data.get("stepId") or source.get("step") or source.get("stepId")
        if source_type == "turn/start":
            current_turn = turn if turn is not None else data.get("id")
        if source_type == "step/start":
            current_step = step if step is not None else data.get("id")
        turn = turn if turn is not None else current_turn
        step = step if step is not None else current_step
        if source_type.startswith("step/") or step is not None:
            steps.add((turn, step))

        payload: dict[str, Any] = {"turn": turn, "step": step, "dsh_event_type": source_type, "dsh_data": data}
        if source_type == "tool/call":
            call_id = data.get("callId") or data.get("call_id") or data.get("id")
            name = str(data.get("name") or data.get("tool") or "unknown")
            if call_id is not None:
                call_names[str(call_id)] = name
            payload.update({"call_id": call_id, "name": name, "arguments": _arguments(data.get("arguments") if "arguments" in data else data.get("rawArguments"))})
        elif source_type == "tool/result":
            call_id = data.get("callId") or data.get("call_id") or data.get("id")
            payload.update(
                {
                    "call_id": call_id,
                    "name": str(data.get("name") or call_names.get(str(call_id), "unknown")),
                    "result": data.get("result") if "result" in data else data.get("content", data.get("message")),
                    "error": data.get("error"),
                }
            )
        elif source_type in {"assistant/message", "assistant/chunk", "user/message"}:
            content = data.get("content") if "content" in data else data.get("message", data.get("text"))
            payload.update({"content": content, "text": _text(content), "model": data.get("model")})
            if source_type == "assistant/message" and payload["text"]:
                final_text = payload["text"]
            usage = data.get("usage") or {}
            if isinstance(usage, dict) and usage:
                token_usage_seen = True
                input_tokens += int(usage.get("inputTokens") or usage.get("input_tokens") or usage.get("promptTokens") or 0)
                output_tokens += int(usage.get("outputTokens") or usage.get("output_tokens") or usage.get("completionTokens") or 0)
                payload["usage"] = usage
        else:
            payload.update({key: value for key, value in data.items() if key not in {"type", "time", "timestamp"}})

        events.append(
            {
                "sequence": sequence,
                "timestamp": _timestamp(source.get("time") or source.get("timestamp")),
                "event_type": type_map.get(source_type, "dsh_" + source_type.replace("/", "_").replace("-", "_")),
                "status": "failed" if payload.get("error") else "completed",
                "payload": payload,
            }
        )
        if source_type == "step/end":
            current_step = None
        if source_type == "turn/end":
            current_turn = None

    return {
        "session_id": header.get("id") or header.get("sessionId") or path.parent.name,
        "session_header": header,
        "session_file": str(path.resolve()),
        "events": events,
        "final_output": {"type": "text", "content": final_text},
        "usage": {
            "input_tokens": input_tokens if token_usage_seen else None,
            "output_tokens": output_tokens if token_usage_seen else None,
            "tool_calls": sum(event["event_type"] == "tool_call" for event in events),
            "steps": len(steps),
        },
        "raw_event_count": len(sources),
        "parse_errors": parse_errors,
    }


def load_session_trajectories(root: Path) -> list[dict[str, Any]]:
    trajectories = [parse_session_jsonl(path) for path in sorted(root.rglob("session.jsonl"))]
    return sorted(
        trajectories,
        key=lambda item: (
            bool(item["session_header"].get("parentSessionId") or item["session_header"].get("originSessionId")),
            item["session_file"],
        ),
    )
