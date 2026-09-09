from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent_eval.adapters import EchoAdapter
from agent_eval.benchmark import _simple_yaml, validate_package
from agent_eval.common import EvalError
from agent_eval.grading_pipeline import grade_trial
from agent_eval.service import Actor, EvaluationService


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class EvaluationPlatformTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service = EvaluationService(Path(self.temp.name), project_root=PROJECT_ROOT)
        self.actor = Actor()
        self.defaults = self.service.ensure_demo_data(self.actor)
        # Tiny in-memory tasks exercise the scheduler, not a shipped Benchmark catalog.
        unit = self.service.create_benchmark(self.actor, {
            "manifest": {"id": "unit-job", "name": "Unit Job", "version": "1", "benchmark_type": "general"},
            "tasks": [{"id": name, "instruction": "return expected", "graders": [
                {"id": "unit-rule", "type": "rule", "config": {"contains": ["expected"]}}
            ]} for name in ("unit-1", "unit-2")],
        }, publish=True)
        self.demo = {"benchmark_id": unit["id"]}

    def tearDown(self) -> None:
        self.service.close()
        self.temp.cleanup()

    def _wait(self, job_id: str, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.service.get_job(self.actor, job_id)
            if job["status"] in {"completed", "review_pending", "failed", "canceled"}:
                return job
            time.sleep(0.02)
        self.fail(f"Job {job_id} did not finish")

    def test_job_expands_runs_grades_and_exports(self) -> None:
        fixture_snapshot = self.service.create_snapshot(self.actor, {"name": "Unit Echo", "version": "1", "adapter_type": "echo", "config": {"fixed_output": "expected"}})
        job = self.service.create_job(
            self.actor,
            {
                "name": "deterministic regression",
                "benchmark_id": self.demo["benchmark_id"],
                "agent_snapshot_ids": [fixture_snapshot["id"]],
                "execution": {"repetitions": 2, "max_concurrency": 4},
            },
        )
        self.service.start_job(self.actor, job["id"])
        finished = self._wait(job["id"])
        self.assertEqual(finished["status"], "completed", finished.get("error"))
        self.assertEqual(len(finished["trials"]), 4)
        self.assertTrue(all(trial["outcome"] == "pass" for trial in finished["trials"]))
        report = self.service.get_report(self.actor, job["id"])
        self.assertEqual(report["summary"]["counts"]["pass"], 4)
        self.assertEqual(report["conclusion"], "passed")
        media_type, markdown = self.service.export_report(self.actor, job["id"], "markdown")
        self.assertIn("text/markdown", media_type)
        self.assertIn("deterministic regression", markdown.decode("utf-8"))

    def test_echo_replays_test_local_evidence(self) -> None:
        benchmark = self.service.create_benchmark(self.actor, {
            "manifest": {"id": "unit-replay", "name": "Replay", "version": "1", "benchmark_type": "conformance"},
            "tasks": [{"id": "unit-replay", "instruction": "return expected", "graders": [
                {"id": "replay-rule", "type": "rule", "config": {"contains": ["expected"], "required_tools": ["unit_tool"]}}
            ]}],
        }, publish=True)
        snapshot = self.service.create_snapshot(self.actor, {
            "name": "Unit Replay", "version": "1", "adapter_type": "echo",
            "config": {
                "fixed_output": "expected",
                "capabilities": {"tools": ["unit_tool"], "events": ["tool_call"]},
                "fixture_events": [{"event_type": "tool_call", "payload": {"name": "unit_tool", "arguments": {}}}],
            },
        })
        job = self.service.create_job(self.actor, {"benchmark_id": benchmark["id"], "agent_snapshot_ids": [snapshot["id"]]})
        self.service.start_job(self.actor, job["id"])
        trial = self._wait(job["id"])["trials"][0]
        self.assertEqual(trial["outcome"], "pass")
        self.assertTrue(any(e["event_type"] == "tool_call" for e in trial["agent_run"]["events"]))

    def test_bootstrap_is_idempotent_and_only_registers_mmlu(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = EvaluationService(Path(directory), project_root=PROJECT_ROOT)
            try:
                first = service.ensure_demo_data(self.actor)
                second = service.ensure_demo_data(self.actor)
                self.assertTrue(first["created"])
                self.assertFalse(second["created"])
                self.assertEqual(first["benchmark_ids"], second["benchmark_ids"])
                self.assertEqual(set(first["benchmark_ids"]), {"mmlu-high-school-computer-science-smoke"})
                benchmark = service.get_benchmark(self.actor, first["benchmark_id"])
                self.assertEqual(len(benchmark["package"]["tasks"]), 20)
                self.assertEqual({s["adapter_type"] for s in service.list_snapshots(self.actor)}, {"dsh-headless", "llm-supervisor"})
            finally:
                service.close()

    def test_missing_capabilities_rejected_before_conformance_run(self) -> None:
        benchmark = self.service.create_benchmark(self.actor, {
            "manifest": {"id": "unit-contract", "name": "Contract", "version": "1", "benchmark_type": "conformance"},
            "tasks": [{"id": "unit-contract", "instruction": "use required contracts", "agent_requirements": {
                "tools": ["unit_search"], "events": ["confirmation_requested"], "features": ["artifacts"]}}],
        }, publish=True)
        snapshot = self.service.create_snapshot(self.actor, {"name": "Plain Echo", "version": "1", "adapter_type": "echo", "config": {}})
        payload = {"benchmark_id": benchmark["id"], "agent_snapshot_ids": [snapshot["id"]]}
        compatibility = self.service.estimate_job(self.actor, payload)["compatibility"]
        self.assertFalse(compatibility["compatible"])
        missing = compatibility["agents"][0]["missing"]
        self.assertIn("unit_search", missing["tools"])
        self.assertIn("confirmation_requested", missing["events"])
        self.assertIn("artifacts", missing["features"])
        with self.assertRaises(EvalError) as context:
            self.service.create_job(self.actor, payload)
        self.assertEqual(context.exception.code, "incompatible_agent")
        self.assertEqual(context.exception.status, 409)
        allowed = self.service.create_job(self.actor, {**payload, "compatibility_mode": "allow"})
        self.assertEqual(allowed["status"], "draft")
        self.assertFalse(allowed["config"]["compatibility"]["compatible"])

    def test_dsh_is_compatible_with_mmlu(self) -> None:
        snapshot = next(s for s in self.service.list_snapshots(self.actor) if s["adapter_type"] == "dsh-headless")
        estimate = self.service.estimate_job(self.actor, {
            "benchmark_id": self.defaults["benchmark_id"], "agent_snapshot_ids": [snapshot["id"]]})
        self.assertTrue(estimate["compatibility"]["compatible"], estimate["compatibility"])
        self.assertEqual(estimate["compatibility"]["benchmark_type"], "general")

    def test_failed_conformance_report_has_scoped_conclusion(self) -> None:
        benchmark = self.service.create_benchmark(
            self.actor,
            {
                "manifest": {"id": "conformance-negative", "name": "Conformance Negative", "version": "1.0.0", "benchmark_type": "conformance"},
                "tasks": [
                    {
                        "id": "CN01",
                        "instruction": "return expected",
                        "graders": [{"id": "rule-cn01", "type": "rule", "weight": 1, "config": {"contains": ["expected"]}}],
                    }
                ],
            },
            publish=True,
        )
        snapshot = self.service.create_snapshot(self.actor, {"name": "Wrong Echo", "version": "1.0.0", "adapter_type": "echo", "config": {"fixed_output": "wrong"}})
        job = self.service.create_job(self.actor, {"benchmark_id": benchmark["id"], "agent_snapshot_ids": [snapshot["id"]]})
        self.service.start_job(self.actor, job["id"])
        finished = self._wait(job["id"])
        report = self.service.get_report(self.actor, finished["id"])
        self.assertEqual(report["conclusion"], "conformance_failed")
        self.assertEqual(report["gate_conclusion"], "blocked")
        self.assertIn("不代表领域业务能力", report["interpretation"])

    def test_hard_failure_is_zero_and_requires_review(self) -> None:
        benchmark = self.service.create_benchmark(self.actor, {
            "manifest": {"id": "unit-forbidden", "name": "Forbidden", "version": "1", "benchmark_type": "general"},
            "tasks": [{"id": "unit-forbidden", "instruction": "return expected without tools", "hard_failures": ["forbidden_tool"],
                "graders": [{"id": "unit-rule", "type": "rule", "config": {"contains": ["expected"], "forbidden_tools": ["forbidden_action"]}}]}],
        }, publish=True)
        snapshot = self.service.create_snapshot(self.actor, {"name": "Invalid Tool Echo", "version": "1", "adapter_type": "echo", "config": {
            "fixed_output": "expected", "fixture_events": [{"event_type": "tool_call", "payload": {"name": "forbidden_action", "arguments": {}}}]}})
        job = self.service.create_job(self.actor, {"name": "negative control", "benchmark_id": benchmark["id"], "agent_snapshot_ids": [snapshot["id"]]})
        self.service.start_job(self.actor, job["id"])
        finished = self._wait(job["id"])
        self.assertEqual(finished["status"], "review_pending")
        self.assertEqual(finished["trials"][0]["outcome"], "hard_fail")
        self.assertEqual(finished["trials"][0]["score"], 0.0)
        self.assertEqual(finished["trials"][0]["failure_type"], "forbidden_tool")
        hard_grade = next(grade for grade in finished["trials"][0]["grades"] if grade["hard_failures"])
        self.assertTrue(hard_grade["evidence_refs"])
        self.assertEqual(len(self.service.list_reviews(self.actor, status="pending")), 1)

    def test_schema_grader_and_echo_adapter(self) -> None:
        benchmark = self.service.create_benchmark(
            self.actor,
            {
                "manifest": {"id": "schema-smoke", "name": "Schema Smoke", "version": "1.0.0"},
                "tasks": [
                    {
                        "id": "S01",
                        "instruction": "return structured data",
                        "suite": "core",
                        "environment": {"type": "fixture", "snapshot": "local"},
                        "graders": [
                            {
                                "id": "schema-v1",
                                "type": "schema",
                                "schema": {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "integer"}}},
                            }
                        ],
                    }
                ],
            },
            publish=True,
        )
        snapshot = self.service.create_snapshot(self.actor, {"name": "Echo Structured", "version": "1.0.0", "adapter_type": "echo", "config": {"fixed_output": {"answer": 42}}})
        job = self.service.create_job(self.actor, {"benchmark_id": benchmark["id"], "agent_snapshot_ids": [snapshot["id"]]})
        self.service.start_job(self.actor, job["id"])
        finished = self._wait(job["id"])
        self.assertEqual(finished["trials"][0]["score"], 100.0)
        self.assertEqual(finished["trials"][0]["outcome"], "pass")

    def test_scope_is_enforced(self) -> None:
        other_project = Actor(project_id="other-project")
        with self.assertRaises(EvalError) as context:
            self.service.get_benchmark(other_project, self.demo["benchmark_id"])
        self.assertEqual(context.exception.status, 404)

    def test_duplicate_task_ids_are_rejected(self) -> None:
        package = {
            "manifest": {"id": "duplicate", "name": "Duplicate", "version": "1"},
            "tasks": [
                {"id": "same", "instruction": "one"},
                {"id": "same", "instruction": "two"},
            ],
        }
        with self.assertRaises(EvalError) as context:
            validate_package(package)
        self.assertEqual(context.exception.code, "duplicate_task_id")

    def test_benchmark_type_and_domain_are_validated(self) -> None:
        with self.assertRaises(EvalError) as context:
            validate_package({"manifest": {"id": "bad-type", "name": "Bad", "version": "1", "benchmark_type": "universal"}, "tasks": [{"id": "T1", "instruction": "ok"}]})
        self.assertEqual(context.exception.code, "invalid_benchmark_type")
        with self.assertRaises(EvalError) as context:
            validate_package({"manifest": {"id": "missing-domain", "name": "Missing", "version": "1", "benchmark_type": "domain"}, "tasks": [{"id": "T1", "instruction": "ok"}]})
        self.assertEqual(context.exception.code, "invalid_benchmark")

    def test_yaml_fallback_parses_benchmark_contract(self) -> None:
        parsed = _simple_yaml(
            """
id: yaml-smoke
name: YAML Smoke
version: 1.0.0
tasks:
  - id: Y01
    instruction: return ok
    environment:
      type: fixture
      snapshot: local
    graders:
      - id: rule-v1
        type: rule
        weight: 1.0
"""
        )
        package = validate_package(parsed)
        self.assertEqual(package["manifest"]["id"], "yaml-smoke")
        self.assertEqual(package["tasks"][0]["environment"]["type"], "fixture")
        self.assertEqual(package["tasks"][0]["graders"][0]["type"], "rule")

    def test_executable_grader_example(self) -> None:
        result = grade_trial(
            task={
                "id": "EX01",
                "instruction": "return keyword",
                "expected_keyword": "hello-eval-v1",
                "pass_threshold": 80,
                "hard_failures": [],
                "graders": [
                    {
                        "id": "contains-keyword-executable-v1",
                        "type": "executable",
                        "entrypoint": "contains_keyword.py",
                        "weight": 1,
                    }
                ],
            },
            run={"final_output": {"content": "hello-eval-v1"}, "events": [], "artifacts": []},
            fixture={},
            registered_graders={},
            executable_root=PROJECT_ROOT / "examples" / "graders",
            enable_executable=True,
        )
        self.assertEqual(result["outcome"], "pass", result)
        self.assertEqual(result["score"], 100.0)

    def test_rule_grader_rejects_wrong_tool_arguments_and_order(self) -> None:
        task = {
            "id": "TOOL-CONTRACT-NEGATIVE",
            "instruction": "search then fetch",
            "pass_threshold": 80,
            "hard_failures": [],
            "graders": [
                {
                    "id": "tool-contract-v1",
                    "type": "rule",
                    "weight": 1,
                    "config": {
                        "contains": ["done"],
                        "required_tools": ["mock_search", "mock_fetch"],
                        "tool_sequence": ["mock_search", "mock_fetch"],
                        "tool_arguments": [
                            {"name": "mock_search", "arguments": {"query": "expected", "limit": 3}}
                        ],
                    },
                }
            ],
        }
        run = {
            "final_output": {"content": "done"},
            "events": [
                {"event_type": "tool_call", "payload": {"name": "mock_fetch", "arguments": {"url": "fixture"}}},
                {"event_type": "tool_call", "payload": {"name": "mock_search", "arguments": {"query": "wrong", "limit": 1}}},
            ],
            "artifacts": [],
            "usage": {"tool_calls": 2, "steps": 2},
        }
        result = grade_trial(task=task, run=run, fixture={}, registered_graders={})
        self.assertEqual(result["outcome"], "unresolved")
        self.assertLess(result["score"], 80)
        self.assertIn("工具调用顺序", result["grades"][0]["reason"])
        self.assertIn("参数符合约定", result["grades"][0]["reason"])

    def test_tool_contract_is_scored_independently_of_correct_output(self) -> None:
        task = {
            "id": "unit-tool-contract", "instruction": "计算表达式并返回结果。", "pass_threshold": 80,
            "graders": [
                {"id": "tool-contract", "type": "rule", "weight": 0.85, "config": {"required_tools": ["calculator"], "tool_arguments": [{"name": "calculator", "arguments": {"expression": "2+2"}}], "min_tool_calls": 1}},
                {"id": "output", "type": "rule", "weight": 0.15, "config": {"contains": ["4"]}},
            ],
        }
        run = {"final_output": {"type": "text", "content": "4"}, "events": [
            {"event_type": "tool_call", "payload": {"name": "calculator", "arguments": {"expression": "2+3"}}},
            {"event_type": "tool_result", "payload": {"name": "calculator", "result": 5}},
        ], "artifacts": [], "usage": {"tool_calls": 1}}
        result = grade_trial(task=task, run=run, fixture={}, registered_graders={})
        self.assertEqual(result["outcome"], "unresolved", result)
        self.assertEqual(result["grades"][0]["grader_id"], "tool-contract")
        self.assertIn("参数符合约定", result["grades"][0]["reason"])
        self.assertEqual(result["grades"][1]["score"], 100.0)

    def test_supervisor_human_approval_creates_corrected_attempt_and_trace(self) -> None:
        benchmark = self.service.create_benchmark(
            self.actor,
            {
                "manifest": {"id": "supervised-loop", "name": "Supervised Loop", "version": "1.0.0", "benchmark_type": "general"},
                "tasks": [
                    {
                        "id": "SL01",
                        "suite": "supervision",
                        "instruction": "读取数据并返回正确数值。",
                        "expected_output": {"answer": "125"},
                        "graders": [{"id": "answer-v1", "type": "rule", "config": {"contains": ["125"]}}],
                    }
                ],
            },
            publish=True,
        )
        snapshot = self.service.create_snapshot(
            self.actor,
            {
                "name": "Correction Fixture",
                "version": "1.0.0",
                "adapter_type": "echo",
                "config": {"fixed_output": "152", "corrected_output": "125"},
            },
        )
        job = self.service.create_job(self.actor, {"name": "supervised loop", "benchmark_id": benchmark["id"], "agent_snapshot_ids": [snapshot["id"]]})
        self.service.start_job(self.actor, job["id"])
        first_finish = self._wait(job["id"])
        first = first_finish["trials"][0]
        self.assertEqual(first["attempt"], 1)
        self.assertEqual(first["outcome"], "unresolved")

        supervision = self.service.supervise_trial(self.actor, first["id"])
        self.assertEqual(supervision["verdict"], "fail")
        self.assertEqual(supervision["status"], "pending_review")
        self.assertEqual(supervision["reference_answer"], {"answer": "125"})
        pending_trace = self.service.get_trial_trace(self.actor, first["id"], view="summary")
        self.assertEqual(pending_trace["actions"]["pending_supervision_id"], supervision["id"])
        self.assertIn("supervisor", {node["type"] for node in pending_trace["nodes"]})

        decision = self.service.decide_correction(
            self.actor,
            supervision["id"],
            {
                "decision": "approve_retry",
                "reason": "监督证据与工具结果一致。",
                "approved_feedback": "重新核对工具结果和最终数值。",
            },
        )
        self.assertEqual(decision["decision"], "approve_retry")
        self.assertIsNotNone(decision["child_trial_id"])
        second_finish = self._wait(job["id"])
        attempts = sorted(second_finish["trials"], key=lambda item: item["attempt"])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[1]["parent_attempt_id"], attempts[0]["id"])
        self.assertEqual(attempts[1]["outcome"], "pass")
        correction_event = attempts[1]["agent_run"]["events"][0]
        self.assertEqual(correction_event["event_type"], "correction_feedback_received")
        self.assertNotIn("125", correction_event["payload"]["feedback"])

        detail_trace = self.service.get_trial_trace(self.actor, attempts[1]["id"], view="detail")
        node_types = [node["type"] for node in detail_trace["nodes"]]
        self.assertIn("human_review", node_types)
        self.assertIn("assistant_final", node_types)
        self.assertEqual(sum(node["type"] == "attempt" for node in detail_trace["nodes"]), 2)
        report = self.service.get_report(self.actor, job["id"])
        self.assertEqual(report["summary"]["total_trials"], 1)
        self.assertEqual(report["summary"]["total_attempts"], 2)
        self.assertEqual(report["summary"]["pass_rate"], 0.0)
        self.assertEqual(report["correction_summary"]["fixed_tasks"], 1)
        self.assertEqual(report["correction_summary"]["mean_score_delta"], 100.0)

    def test_echo_adapter_reports_fixture_tool_calls_in_usage(self) -> None:
        snapshot = {
            "config": {
                "fixed_output": "125",
                "fixture_events": [
                    {"event_type": "tool_call", "payload": {"name": "read_data"}},
                    {"event_type": "tool_result", "payload": {"name": "read_data", "result": 125}},
                ],
            }
        }
        run = EchoAdapter().run(
            snapshot=snapshot,
            task={"id": "SL01", "instruction": "读取数据。"},
            fixture={},
            cancel_event=threading.Event(),
        )
        self.assertEqual(run["usage"]["tool_calls"], 1)

    def test_echo_adapter_uses_task_specific_initial_and_corrected_fixtures(self) -> None:
        snapshot = {
            "config": {
                "fixed_output": "fallback",
                "task_overrides": {
                    "SL02": {
                        "initial": {
                            "output": "125",
                            "fixture_events": [{"event_type": "tool_call", "payload": {"name": "read_data", "arguments": {"source": "fixture"}}}],
                        },
                        "corrected": {
                            "output": "125",
                            "fixture_events": [{"event_type": "tool_call", "payload": {"name": "read_data", "arguments": {"source": "report"}}}],
                        },
                    }
                },
            }
        }
        initial = EchoAdapter().run(snapshot=snapshot, task={"id": "SL02", "instruction": "读取 report"}, fixture={}, cancel_event=threading.Event())
        corrected = EchoAdapter().run(
            snapshot=snapshot,
            task={"id": "SL02", "instruction": "读取 report", "correction": {"feedback": "核对工具参数。", "parent_attempt_id": "trial-1"}},
            fixture={},
            cancel_event=threading.Event(),
        )
        self.assertEqual(initial["final_output"]["content"], "125")
        self.assertEqual(initial["events"][0]["payload"]["arguments"]["source"], "fixture")
        self.assertEqual(corrected["events"][0]["event_type"], "correction_feedback_received")
        self.assertEqual(corrected["events"][1]["payload"]["arguments"]["source"], "report")

    def test_benchmark_threshold_is_inherited_and_trace_marks_rule_violations(self) -> None:
        package = validate_package(
            {
                "manifest": {"id": "strict-trace", "name": "Strict Trace", "version": "1.0.0", "pass_threshold": 100},
                "tasks": [
                    {
                        "id": "T01",
                        "instruction": "读取 report 并返回 125。",
                        "graders": [{"id": "rule", "type": "rule", "config": {"contains": ["125"], "required_tools": ["read_data"], "tool_arguments": [{"name": "read_data", "arguments": {"source": "report"}}], "min_tool_calls": 1, "max_tool_calls": 1}}],
                    }
                ],
            }
        )
        task = package["tasks"][0]
        self.assertEqual(task["pass_threshold"], 100.0)
        run = {
            "final_output": {"type": "text", "content": "125"},
            "events": [{"event_type": "tool_call", "payload": {"name": "read_data", "arguments": {"source": "fixture"}}}],
            "usage": {"tool_calls": 1, "steps": 1},
            "artifacts": [],
        }
        result = grade_trial(task=task, run=run, fixture={}, registered_graders={})
        self.assertEqual(result["score"], 80.0)
        self.assertEqual(result["outcome"], "unresolved")
        semantic = self.service._trace_event_semantics("tool_call", run["events"][0]["payload"], task, {"outcome": "unresolved"})
        self.assertEqual(semantic["semantic_status"], "violation")
        self.assertIn("参数", semantic["semantic_reason"])

    def test_rejected_supervision_does_not_create_attempt(self) -> None:
        benchmark = self.service.create_benchmark(
            self.actor,
            {
                "manifest": {"id": "supervised-reject", "name": "Supervised Reject", "version": "1.0.0"},
                "tasks": [{"id": "SR01", "instruction": "return right", "graders": [{"id": "right-v1", "type": "rule", "config": {"contains": ["right"]}}]}],
            },
            publish=True,
        )
        snapshot = self.service.create_snapshot(self.actor, {"name": "Wrong", "version": "1.0.0", "adapter_type": "echo", "config": {"fixed_output": "wrong"}})
        job = self.service.create_job(self.actor, {"benchmark_id": benchmark["id"], "agent_snapshot_ids": [snapshot["id"]]})
        self.service.start_job(self.actor, job["id"])
        trial = self._wait(job["id"])["trials"][0]
        supervision = self.service.supervise_trial(self.actor, trial["id"])
        decision = self.service.decide_correction(self.actor, supervision["id"], {"decision": "reject_feedback", "reason": "监督建议证据不足。"})
        self.assertIsNone(decision["child_trial_id"])
        self.assertEqual(len(self.service.list_trials(self.actor, job_id=job["id"])), 1)
        with self.assertRaises(EvalError) as context:
            self.service.decide_correction(self.actor, supervision["id"], {"decision": "approve_retry", "reason": "duplicate"})
        self.assertEqual(context.exception.code, "invalid_state")

    def test_supervision_input_excludes_reference_answer(self) -> None:
        task = {
            "id": "MMLU-example",
            "title": "选择题",
            "instruction": "选择正确选项。",
            "expected": {"answer": "hidden-reference-answer"},
            "oracle": {"type": "reference_answer"},
        }
        trial = {
            "id": "trial-1",
            "attempt": 1,
            "outcome": "unresolved",
            "score": 60,
            "failure_type": "wrong_answer",
            "agent_run": {"final_output": {"answer": "A"}, "events": [], "usage": {"tool_calls": 1}},
            "grades": [{"grader_id": "official", "reason": "答案不符合要求", "score": 60, "passed": False}],
        }
        review_package = self.service._supervision_input(task, trial)
        serialized = json.dumps(review_package, ensure_ascii=False)
        self.assertNotIn("expected", serialized)
        self.assertNotIn("oracle", serialized)
        self.assertNotIn("hidden-reference-answer", serialized)
        self.assertIn("答案不符合要求", serialized)

    def test_report_includes_supervisor_quality_and_cost_metrics(self) -> None:
        benchmark = self.service.create_benchmark(
            self.actor,
            {
                "manifest": {"id": "supervisor-metrics", "name": "Supervisor Metrics", "version": "1.0.0"},
                "tasks": [
                    {"id": "FAIL", "instruction": "return right", "graders": [{"id": "right", "type": "rule", "config": {"contains": ["right"]}}]},
                    {"id": "PASS", "instruction": "return wrong", "graders": [{"id": "wrong", "type": "rule", "config": {"contains": ["wrong"]}}]},
                ],
            },
            publish=True,
        )
        snapshot = self.service.create_snapshot(
            self.actor,
            {"name": "Metric Fixture", "version": "1.0.0", "adapter_type": "echo", "config": {"fixed_output": "wrong", "corrected_output": "right"}},
        )
        job = self.service.create_job(self.actor, {"benchmark_id": benchmark["id"], "agent_snapshot_ids": [snapshot["id"]]})
        self.service.start_job(self.actor, job["id"])
        finished = self._wait(job["id"])
        trials = {item["task_id"]: item for item in finished["trials"]}
        fail_supervision = self.service.supervise_trial(self.actor, trials["FAIL"]["id"])
        pass_supervision_id = "supervision-false-positive"
        self.service.database.insert(
            "supervision_runs",
            {
                "id": pass_supervision_id,
                "tenant_id": self.actor.tenant_id,
                "project_id": self.actor.project_id,
                "trial_id": trials["PASS"]["id"],
                "supervisor_snapshot_id": None,
                "supervisor_type": "fixture-supervisor",
                "supervisor_version": "1.0.0",
                "status": "pending_review",
                "verdict": "fail",
                "error_types_json": ["false_positive"],
                "evidence_refs_json": ["final_output"],
                "reason": "受控误报",
                "suggestion": "重新检查。",
                "confidence": 0.8,
                "answer_leakage_risk": "low",
                "reference_answer_json": None,
                "result_json": {"verdict": "fail"},
                "usage_json": {"duration_ms": 5, "input_tokens": 10, "output_tokens": 5},
                "raw_output_json": None,
                "created_at": trials["PASS"]["finished_at"],
                "finished_at": trials["PASS"]["finished_at"],
            },
        )
        self.service.decide_correction(self.actor, fail_supervision["id"], {"decision": "approve_retry", "reason": "批准", "approved_feedback": "重新检查答案。"})
        self.service.decide_correction(self.actor, pass_supervision_id, {"decision": "accept_final", "reason": "接受通过结果"})
        self._wait(job["id"])
        report = self.service.get_report(self.actor, job["id"])
        correction = report["correction_summary"]
        self.assertEqual(correction["supervised_tasks"], 2)
        self.assertEqual(correction["supervisor_detection_rate"], 1.0)
        self.assertEqual(correction["supervisor_false_positive_rate"], 1.0)
        self.assertEqual(correction["fix_rate"], 1.0)
        self.assertEqual(correction["human_decisions"]["approve_retry"], 1)
        self.assertGreaterEqual(correction["execution_cost"]["attempts"], 3)


if __name__ == "__main__":
    unittest.main()
