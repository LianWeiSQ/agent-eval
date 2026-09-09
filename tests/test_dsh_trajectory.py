from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_eval.adapters import DeepSeekHarnessHeadlessAdapter, _normalise_structured_output, _render_dsh_instruction
from agent_eval.benchmark import load_package
from agent_eval.dsh_trajectory import parse_session_jsonl


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MMLU_SMOKE_BENCHMARK = PROJECT_ROOT / "benchmarks" / "mmlu-high-school-computer-science-smoke"


class DshTrajectoryTest(unittest.TestCase):
    def test_dsh_windows_batch_shim_receives_one_line_instruction(self) -> None:
        instruction = "Follow the format.\n\nQuestion: What is 2 + 2?\n\nA. 3\nB. 4"
        self.assertEqual(
            _render_dsh_instruction(instruction, r"C:\\Users\\name\\AppData\\Roaming\\npm\\pnpm.cmd"),
            "Follow the format. Question: What is 2 + 2? A. 3 B. 4",
        )
        self.assertEqual(_render_dsh_instruction(instruction, "dsh"), instruction)

    def test_dsh_healthcheck_runs_in_snapshot_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("agent_eval.adapters.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "dsh test"
            result = DeepSeekHarnessHeadlessAdapter().healthcheck(
                {"command": ["dsh"], "health_command": ["dsh", "--version"], "working_directory": directory}
            )
        self.assertTrue(result["ok"])
        self.assertEqual(Path(run.call_args.kwargs["cwd"]), Path(directory))
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")

    def test_mmlu_smoke_benchmark_freezes_twenty_no_tool_questions(self) -> None:
        package = load_package(MMLU_SMOKE_BENCHMARK)
        self.assertEqual(package["manifest"]["id"], "mmlu-high-school-computer-science-smoke")
        self.assertEqual(package["manifest"]["version"], "1.0.1")
        self.assertEqual(len(package["tasks"]), 20)
        self.assertEqual(package["manifest"]["source_metadata"]["config"], "high_school_computer_science")
        for task in package["tasks"]:
            config = task["graders"][0]["config"]
            self.assertEqual(config["max_tool_calls"], 0)
            self.assertIn("FINAL:", task["instruction"])
            self.assertIn(r"FINAL:\s*", config["regex"])
            self.assertTrue(any(f"{letter}\\s*$" in config["regex"] for letter in "ABCD"))

    def test_mmlu_regex_matches_exact_final_output(self) -> None:
        package = load_package(MMLU_SMOKE_BENCHMARK)
        for task in package["tasks"]:
            config = task["graders"][0]["config"]
            answer = task["expected_output"]["answer"]
            self.assertIsNotNone(re.fullmatch(config["regex"].removeprefix("(?m)"), answer))

    def test_only_mmlu_is_bundled_as_a_benchmark_directory(self) -> None:
        bundled = {p.name for p in (PROJECT_ROOT / "benchmarks").iterdir() if p.is_dir() and (p / "benchmark.json").is_file()}
        self.assertEqual(bundled, {"mmlu-high-school-computer-science-smoke"})

    def test_dsh_schema_tasks_parse_plain_or_fenced_json_only(self) -> None:
        task = {"graders": [{"type": "schema"}]}
        expected = {"status": "ok", "answer": 42}
        self.assertEqual(
            _normalise_structured_output(task, {"type": "text", "content": '{"status":"ok","answer":42}'}),
            expected,
        )
        self.assertEqual(
            _normalise_structured_output(task, {"type": "text", "content": '```json\n{"status":"ok","answer":42}\n```'}),
            expected,
        )
        original = {"type": "text", "content": "answer: 42"}
        self.assertIs(_normalise_structured_output(task, original), original)
        self.assertIs(_normalise_structured_output({"graders": [{"type": "rule"}]}, original), original)

    def test_session_jsonl_preserves_agent_steps_tools_and_usage(self) -> None:
        rows = [
            {"type": "session", "id": "sess-1"},
            {"type": "turn/start", "time": 1_700_000_000_000, "data": {"turnId": "turn-1"}},
            {"type": "step/start", "time": 1_700_000_000_100, "data": {"stepId": "step-1"}},
            {"type": "request/header", "data": {"model": "deepseek"}},
            {"type": "tool/call", "data": {"callId": "call-1", "name": "read", "rawArguments": '{"path":"README.md"}'}},
            {"type": "tool/result", "data": {"callId": "call-1", "content": "hello"}},
            {"type": "assistant/message", "data": {"content": [{"type": "text", "text": "done"}], "usage": {"inputTokens": 10, "outputTokens": 2}}},
            {"type": "step/end", "data": {"stepId": "step-1"}},
            {"type": "turn/end", "data": {"turnId": "turn-1"}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            trajectory = parse_session_jsonl(path)

        self.assertEqual(trajectory["session_id"], "sess-1")
        self.assertEqual(trajectory["final_output"]["content"], "done")
        self.assertEqual(trajectory["usage"], {"input_tokens": 10, "output_tokens": 2, "tool_calls": 1, "steps": 1})
        calls = [event for event in trajectory["events"] if event["event_type"] == "tool_call"]
        self.assertEqual(calls[0]["payload"]["name"], "read")
        self.assertEqual(calls[0]["payload"]["arguments"], {"path": "README.md"})
        results = [event for event in trajectory["events"] if event["event_type"] == "tool_result"]
        self.assertEqual(results[0]["payload"]["name"], "read")


if __name__ == "__main__":
    unittest.main()
