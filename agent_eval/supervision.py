from __future__ import annotations

from typing import Any


SUPERVISION_CONTRACT_VERSION = "task-completion-v2"
SUPERVISION_CONTRACT = {
    "version": SUPERVISION_CONTRACT_VERSION,
    "target": "verdict 判断执行 Agent 是否完成任务，不是判断官方评分是否正确。",
    "pass": "可观察证据支持任务全部必要要求已完成。",
    "fail": "可观察证据支持任务要求未完成；失败任务被正确判为零分，仍应输出 fail。",
    "uncertain": "证据不足、评分可信度存疑或无法消解的矛盾，需要人工复核。",
    "grading_agreement": "是否同意官方评分写在 reason 中，不能用 pass 表示同意零分。",
}


def validate_supervision_verdict(result: dict[str, Any], trial: dict[str, Any]) -> dict[str, Any]:
    """Route contradictory passes to review, preserving the model's original result."""
    if result.get("verdict") != "pass" or trial.get("outcome") in {None, "pass"}:
        return result
    return {
        **result,
        "verdict": "uncertain",
        "reason": "监督原始 pass 与 Trial 未通过的结果冲突，需人工复核；这不是任务已通过。原始理由："
        + str(result.get("reason") or ""),
        "error_types": list(dict.fromkeys([*(result.get("error_types") or []), "verdict_conflict"])),
        "validation": {
            "version": SUPERVISION_CONTRACT_VERSION,
            "code": "verdict_conflict",
            "trial_outcome": trial.get("outcome"),
            "original_result": dict(result),
            "note": "平台只把矛盾结果送人工审核，不把官方评分一致性解释为任务成功。",
        },
    }


def supervision_events(source: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Project DSH transport logs onto completed messages and tool evidence."""
    streaming = {"assistant_chunk", "dsh_reasoning_chunks", "dsh_tool_call_chunks",
                 "dsh_text_chunks", "model_request", "dsh_session_title_llm_request"}
    omitted: dict[str, int] = {}
    raw_fields_removed = 0
    events = []
    for event in source:
        kind = event.get("event_type")
        payload = event.get("payload")
        is_dsh = isinstance(payload, dict) and "dsh_event_type" in payload
        if is_dsh and kind in streaming:
            omitted[kind] = omitted.get(kind, 0) + 1
            continue
        if is_dsh:
            payload = dict(payload)
            if kind == "model_response" and isinstance(payload.get("text"), str):
                # Completed text is normalized separately; raw content also embeds reasoning.
                payload = {key: payload[key] for key in ("turn", "step", "text", "model", "usage") if key in payload}
                raw_fields_removed += 1
            elif "dsh_data" in payload and any(key in payload for key in ("arguments", "result", "content", "text", "usage", "error")):
                payload.pop("dsh_data")
                raw_fields_removed += 1
        events.append({"sequence": event.get("sequence"), "event_type": kind,
                       "status": event.get("status"), "payload": payload})
    return events, {
        "version": "normalized-events-v1", "original_event_count": len(source),
        "included_event_count": len(events), "omitted_event_types": omitted,
        "normalized_raw_fields_removed": raw_fields_removed,
        "tool_events_truncated": False,
        "note": "流式片段、模型请求和重复原始消息不纳入监督；保留完整规范化工具调用、结果、完成的回复及原事件序号。原始轨迹仍保存在 Trial。",
    }


def fixture_supervise(*, task: dict[str, Any], trial: dict[str, Any]) -> dict[str, Any]:
    """Build a deterministic supervisor result from official grader evidence."""
    outcome = str(trial.get("outcome") or "")
    score = trial.get("score")
    grades = list(trial.get("grades") or [])
    failed_grades = [grade for grade in grades if grade.get("passed") is False or grade.get("status") == "failed"]
    evidence_refs = list(
        dict.fromkeys(
            str(ref)
            for grade in failed_grades or grades
            for ref in (grade.get("evidence_refs") or [])
            if ref
        )
    )
    reasons = [str(grade.get("reason")) for grade in failed_grades if grade.get("reason")]
    reference_answer = task.get("expected_output")
    if reference_answer is None:
        reference_answer = task.get("expected")
    if reference_answer is None:
        reference_answer = task.get("oracle")

    if outcome == "pass":
        return {
            "verdict": "pass",
            "error_types": [],
            "reason": "官方评分器判定当前结果通过。",
            "suggestion": "",
            "evidence_refs": evidence_refs,
            "confidence": 1.0,
            "answer_leakage_risk": "low",
            "reference_answer": reference_answer,
        }
    if score is None or outcome in {"infra_failed", "grader_failed"}:
        return {
            "verdict": "uncertain",
            "error_types": [str(trial.get("failure_type") or "insufficient_evidence")],
            "reason": "当前运行或评分证据不完整，无法可靠判断答案质量。",
            "suggestion": "先排查运行环境或评分器错误，再重新执行评测。",
            "evidence_refs": evidence_refs,
            "confidence": 0.5,
            "answer_leakage_risk": "low",
            "reference_answer": reference_answer,
        }
    return {
        "verdict": "fail",
        "error_types": [str(trial.get("failure_type") or "wrong_answer")],
        "reason": "；".join(reasons) or "最终结果未达到官方评分阈值。",
        "suggestion": "重新检查失败证据、工具返回结果和最终答案之间的一致性。",
        "evidence_refs": evidence_refs or ["final_output"],
        "confidence": 1.0,
        "answer_leakage_risk": "low",
        "reference_answer": reference_answer,
    }
