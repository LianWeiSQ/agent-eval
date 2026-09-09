from __future__ import annotations

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from agent_eval.adapters import LlmSupervisorAdapter, get_adapter


class MockSupervisorHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        if self.path != "/chat/completions":
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen_request = body
        self.server.seen_authorization = self.headers.get("Authorization")
        self.server.seen_supervision_input = json.loads(body["messages"][-1]["content"])["review_package"]
        result = {
            "verdict": "fail", "error_types": ["evidence_mismatch"],
            "reason": "最终输出与工具证据不一致。", "suggestion": "核对输出与本次工具结果。",
            "evidence_refs": ["event:3", "grade:official"], "confidence": 0.96, "answer_leakage_risk": "low",
        }
        content = json.dumps({
            "choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 140, "completion_tokens": 60, "total_tokens": 200},
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


class LlmSupervisorHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MockSupervisorHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_real_supervisor_receives_no_reference_answer_and_records_usage(self) -> None:
        snapshot = {
            "name": "Test Supervisor",
            "version": "1.0.0",
            "adapter_type": "llm-supervisor",
            "config": {
                "base_url": self.base_url,
                "chat_path": "/chat/completions",
                "api_key_env": "TEST_SUPERVISOR_API_KEY",
            },
        }
        task = {
            "id": "supervise-trial",
            "instruction": "审查执行结果",
            "timeout_seconds": 10,
            "supervision_input": {
                "task": {"id": "TB21-example", "instruction": "生成任务要求的文件。"},
                "attempt": {"final_output": {"content": "done"}, "events": []},
                "official_grades": [{"reason": "最终输出与工具证据不一致"}],
            },
        }
        with patch.dict(os.environ, {"TEST_SUPERVISOR_API_KEY": "mock-key-never-persisted"}):
            run = get_adapter("llm-supervisor").run(snapshot=snapshot, task=task, fixture={}, cancel_event=threading.Event())
        self.assertEqual(run["final_output"]["verdict"], "fail")
        self.assertEqual(run["usage"]["input_tokens"], 140)
        self.assertEqual(run["usage"]["output_tokens"], 60)
        self.assertNotIn("tools", self.server.seen_request)
        self.assertEqual(self.server.seen_authorization, "Bearer mock-key-never-persisted")
        serialized = json.dumps(self.server.seen_supervision_input, ensure_ascii=False)  # type: ignore[attr-defined]
        self.assertNotIn("expected", serialized)
        self.assertNotIn("oracle", serialized)

    def test_supervisor_normalizes_common_equivalent_verdicts(self) -> None:
        result = LlmSupervisorAdapter._validate_result(
            {
                "verdict": "unresolved",
                "error_types": ["wrong_tool_arguments"],
                "reason": "工具参数不符合约定。",
                "suggestion": "核对工具参数。",
                "evidence_refs": ["event:3"],
                "confidence": 0.9,
                "answer_leakage_risk": "low",
            }
        )
        self.assertEqual(result["verdict"], "fail")
        explained = LlmSupervisorAdapter._validate_result(
            {
                "verdict": "不通过：工具参数错误",
                "error_types": ["wrong_tool_arguments"],
                "reason": "工具参数不符合约定。",
                "suggestion": "核对工具参数。",
                "evidence_refs": ["event:3"],
                "confidence": 0.9,
                "answer_leakage_risk": "low",
            }
        )
        self.assertEqual(explained["verdict"], "fail")
        fallback = LlmSupervisorAdapter._validate_result(
            {
                "verdict": "需要进一步核查",
                "error_types": "wrong_tool_arguments，invalid_output",
                "reason": "证据不足。",
                "suggestion": "人工复核。",
                "evidence_refs": "event:3，grade:tool-contract",
                "confidence": "85%",
                "answer_leakage_risk": "低",
            }
        )
        self.assertEqual(fallback["verdict"], "uncertain")
        self.assertEqual(fallback["error_types"], ["wrong_tool_arguments", "invalid_output"])
        self.assertEqual(fallback["evidence_refs"], ["event:3", "grade:tool-contract"])
        self.assertEqual(fallback["confidence"], 0.85)
        self.assertEqual(fallback["answer_leakage_risk"], "low")


if __name__ == "__main__":
    unittest.main()
