from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_eval.adapters import AdapterFailure, LlmSupervisorAdapter
from agent_eval.cli import _parser
from agent_eval.common import EvalError
from agent_eval.service import Actor, EvaluationService


RESULT = {
    "verdict": "fail",
    "reason": "The final output does not match the tool evidence.",
    "suggestion": "Check the output against the tool result.",
    "error_types": ["evidence_mismatch"],
    "evidence_refs": ["event:2"],
    "confidence": 0.9,
    "answer_leakage_risk": "low",
}


def response(content: object, finish: str = "stop") -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 200, "completion_tokens": 1000},
    }


class SupervisorOutputTest(unittest.TestCase):
    def test_dsh_streaming_logs_are_removed_without_changing_tool_evidence(self) -> None:
        task = {"id": "TB21-example", "instruction": "Create the requested files.", "expected": "hidden-answer"}
        events = [
            {"sequence": n, "event_type": "dsh_reasoning_chunks", "payload": {"dsh_event_type": "reasoning/chunks", "dsh_data": "x" * 2000}}
            for n in range(1000)
        ]
        events.extend([
            {"sequence": 1001, "event_type": "tool_call", "payload": {"dsh_event_type": "tool/call", "dsh_data": {"duplicate": True}, "name": "bash", "call_id": "call-1", "arguments": {"command": "cat result.txt"}}},
            {"sequence": 1002, "event_type": "tool_result", "payload": {"dsh_event_type": "tool/result", "dsh_data": {"duplicate": True}, "call_id": "call-1", "result": "actual-result", "error": None}},
            {"sequence": 1003, "event_type": "model_response", "payload": {"dsh_event_type": "assistant/message", "dsh_data": "duplicate reasoning", "content": "reasoning", "text": "Done."}},
            {"sequence": 1004, "event_type": "assistant_chunk", "payload": {"text": "non-DSH evidence must remain"}},
        ])
        trial = {"id": "trial", "agent_run": {"events": events, "final_output": "Done."},
                 "grades": [{"grader_id": "official", "score": 100, "passed": True, "reason": "6 checks passed"}]}
        original = json.dumps(trial)
        review = EvaluationService._supervision_input(task, trial)
        self.assertEqual(json.dumps(trial), original)
        self.assertEqual([event["sequence"] for event in review["attempt"]["events"]], [1001, 1002, 1003, 1004])
        self.assertEqual(review["attempt"]["events"][0]["payload"]["arguments"], events[1000]["payload"]["arguments"])
        self.assertEqual(review["attempt"]["events"][1]["payload"]["result"], "actual-result")
        self.assertEqual(review["attempt"]["events"][2]["payload"]["text"], "Done.")
        self.assertEqual(review["official_grades"][0]["reason"], "6 checks passed")
        self.assertEqual(review["input_policy"]["omitted_event_types"], {"dsh_reasoning_chunks": 1000})
        self.assertNotIn("hidden-answer", json.dumps(review))
        self.assertLess(len(json.dumps(review)), 5000)

    def test_oversized_normalized_input_is_rejected_before_provider_call(self) -> None:
        with patch.dict(os.environ, {"TEST_SUPERVISOR_KEY": "mock-key"}), patch("agent_eval.adapters.urllib.request.urlopen") as provider:
            with self.assertRaises(AdapterFailure) as caught:
                LlmSupervisorAdapter().run(snapshot={"config": {"api_key_env": "TEST_SUPERVISOR_KEY"}},
                    task={"supervision_input": {"attempt": {"final_output": "x" * 256001}}}, fixture={}, cancel_event=threading.Event())
            self.assertEqual(caught.exception.failure_type, "input_too_large")
            provider.assert_not_called()

    def test_empty_and_malformed_outputs_are_not_uncertain_verdicts(self) -> None:
        for content in ("", "  ", None, "not JSON", "{}", "[]", '{"verdict":"pass"}', json.dumps({**RESULT, "reason": ""})):
            with self.subTest(content=content), self.assertRaises(AdapterFailure) as caught:
                LlmSupervisorAdapter._parse_response(response(content))
            self.assertEqual(caught.exception.failure_type, "invalid_output")

    def test_truncation_is_explicit_even_when_content_looks_valid(self) -> None:
        for content in ("", json.dumps(RESULT), '{"verdict":'):
            with self.subTest(content=content), self.assertRaises(AdapterFailure) as caught:
                LlmSupervisorAdapter._parse_response(response(content, "length"))
            self.assertEqual(caught.exception.failure_type, "output_truncated")
            self.assertIn('"completion_tokens": 1000', str(caught.exception))

    def test_reasoning_text_is_not_used_as_final_verdict(self) -> None:
        body = response(None)
        body["choices"][0]["message"]["reasoning_content"] = json.dumps(RESULT)
        with self.assertRaises(AdapterFailure):
            LlmSupervisorAdapter._parse_response(body)

    def test_bad_envelopes_and_refusals_are_rejected(self) -> None:
        for body in (None, [], {}, {"choices": []}, {"choices": [None]}, {"choices": [{}]}, response(json.dumps(RESULT), "content_filter")):
            with self.subTest(body=body), self.assertRaises(AdapterFailure):
                LlmSupervisorAdapter._parse_response(body)

    def test_valid_json_and_explained_uncertainty_are_preserved(self) -> None:
        for verdict in ("pass", "fail", "uncertain"):
            for fenced in (False, True):
                expected = {**RESULT, "verdict": verdict}
                content = json.dumps(expected)
                if fenced:
                    content = "```json\n" + content + "\n```"
                result, raw, metadata = LlmSupervisorAdapter._parse_response(response(content))
                self.assertEqual(result, expected)
                self.assertEqual(raw, content)
                self.assertEqual(metadata["finish_reason"], "stop")

    def test_request_honors_snapshot_budget_and_thinking(self) -> None:
        body = response(json.dumps(RESULT))
        config = {"base_url": "https://api.example.com/v1", "api_key_env": "TEST_SUPERVISOR_KEY", "thinking": "disabled", "budget": {"max_output_tokens": 4000}}
        with patch.dict(os.environ, {"TEST_SUPERVISOR_KEY": "mock-key"}), patch(
            "agent_eval.adapters.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(body).encode())
        ) as request:
            run = LlmSupervisorAdapter().run(snapshot={"config": config}, task={"supervision_input": {}}, fixture={}, cancel_event=threading.Event())
        payload = json.loads(request.call_args.args[0].data)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["max_tokens"], 4000)
        self.assertEqual(run["response_metadata"]["completion_tokens"], 1000)

    def test_cli_accepts_real_supervisor(self) -> None:
        args = _parser().parse_args(["job-run", "--benchmark-id", "b", "--snapshot-id", "a", "--supervisor-snapshot-id", "s"])
        self.assertEqual(args.supervisor_snapshot_id, "s")
        args = _parser().parse_args(["snapshot-create", "--name", "s", "--version", "1", "--adapter", "llm-supervisor"])
        self.assertEqual(args.adapter, "llm-supervisor")

    def test_cli_accepts_direct_terminal_bench_run(self) -> None:
        args = _parser().parse_args(["terminal-bench-run", "--task", "openssl-selfsigned-cert"])
        self.assertEqual(args.task, ["openssl-selfsigned-cert"])
        self.assertFalse(args.all_tasks)

    def test_failed_supervision_is_audited_retryable_and_does_not_create_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = EvaluationService(Path(directory))
            actor = Actor()
            try:
                benchmark = service.create_benchmark(actor, {
                    "manifest": {"id": "supervisor-retry", "name": "Supervisor retry", "version": "1.0.0"},
                    "tasks": [{"id": "T1", "instruction": "Return the correct value.", "graders": [{"id": "answer", "type": "rule", "config": {"contains": ["125"]}}]}],
                }, publish=True)
                executor = service.create_snapshot(actor, {"name": "executor", "version": "1", "adapter_type": "echo", "config": {"fixed_output": "152"}})
                supervisor = service.create_snapshot(actor, {"name": "supervisor", "version": "1", "adapter_type": "llm-supervisor", "config": {"api_key_env": "TEST_SUPERVISOR_KEY", "base_url": "https://api.example.com/v1"}})
                job = service.create_job(actor, {"name": "retryable check", "benchmark_id": benchmark["id"], "agent_snapshot_ids": [executor["id"]], "supervisor_snapshot_id": supervisor["id"]})
                service.start_job(actor, job["id"])
                service.close()
                before = service.get_job(actor, job["id"])
                trial = before["trials"][0]
                self.assertIsNotNone(trial["outcome"])
                with patch.dict(os.environ, {"TEST_SUPERVISOR_KEY": "mock-key"}), patch(
                    "agent_eval.adapters.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response("", "length")).encode())
                ), self.assertRaises(EvalError) as caught:
                    service.supervise_trial(actor, trial["id"])
                self.assertEqual(caught.exception.code, "supervisor_output_truncated")
                self.assertEqual(caught.exception.status, 502)
                self.assertEqual(service.list_supervisions(actor), [])
                self.assertEqual(service.get_job(actor, job["id"])["status"], before["status"])
                audit = service.database.one("SELECT * FROM audit_events WHERE action='supervision.failed'")
                self.assertEqual(audit["details"]["failure_type"], "output_truncated")
                with patch.dict(os.environ, {"TEST_SUPERVISOR_KEY": "mock-key"}), patch(
                    "agent_eval.adapters.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response(json.dumps(RESULT))).encode())
                ):
                    check = service.supervise_trial(actor, trial["id"])
                self.assertEqual(check["status"], "pending_review")
                self.assertEqual(check["raw_output"]["response_metadata"]["finish_reason"], "stop")
                after = service.get_job(actor, job["id"])
                self.assertEqual(len(after["trials"]), 1)
                self.assertEqual(after["trials"][0]["score"], trial["score"])
                self.assertEqual(after["trials"][0]["agent_run"], trial["agent_run"])
            finally:
                service.close()
