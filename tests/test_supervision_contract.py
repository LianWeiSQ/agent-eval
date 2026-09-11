from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_eval.common import EvalError
from agent_eval.service import Actor, EvaluationService
from agent_eval.supervision import validate_supervision_verdict


class SupervisionContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service = EvaluationService(Path(self.temp.name), project_root=Path(__file__).resolve().parents[1])
        self.actor = Actor()
        benchmark = self.service.create_benchmark(
            self.actor,
            {
                "manifest": {"id": "review-contract", "name": "Review Contract", "version": "1"},
                "tasks": [
                    {
                        "id": "failed-task",
                        "instruction": "return good",
                        "graders": [{"id": "rule", "type": "rule", "config": {"contains": ["good"]}}],
                    }
                ],
            },
            publish=True,
        )
        agent = self.service.create_snapshot(
            self.actor,
            {"name": "Wrong Echo", "version": "1", "adapter_type": "echo", "config": {"fixed_output": "bad"}},
        )
        job = self.service.create_job(self.actor, {"benchmark_id": benchmark["id"], "agent_snapshot_ids": [agent["id"]]})
        self.service.start_job(self.actor, job["id"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.service.get_job(self.actor, job["id"])
            if job["status"] == "completed":
                break
            time.sleep(0.02)
        self.assertEqual(job["status"], "completed")
        self.trial = job["trials"][0]
        self.assertNotEqual(self.trial["outcome"], "pass")

    def tearDown(self) -> None:
        self.service.close()
        self.temp.cleanup()

    def test_model_agreement_with_zero_score_does_not_skip_review(self) -> None:
        snapshot = self.service.create_snapshot(
            self.actor,
            {"name": "Supervisor", "version": "1", "adapter_type": "llm-supervisor", "config": {}},
        )
        original = {
            "verdict": "pass",
            "reason": "The zero score is correct; the task was not completed.",
            "suggestion": "Check the failed command before retrying.",
            "error_types": ["wrong_tool_arguments"],
            "evidence_refs": ["grade:rule"],
            "confidence": 0.9,
            "answer_leakage_risk": "low",
        }
        adapter = Mock()
        adapter.run.return_value = {
            "final_output": original,
            "raw_output": json.dumps(original),
            "usage": {"input_tokens": 10},
        }
        with patch("agent_eval.service.get_adapter", return_value=adapter):
            review = self.service.supervise_trial(
                self.actor,
                self.trial["id"],
                {"supervisor_snapshot_id": snapshot["id"]},
            )
        self.assertEqual(review["verdict"], "uncertain")
        self.assertEqual(review["status"], "pending_review")
        self.assertEqual(review["result"]["validation"]["original_result"]["verdict"], "pass")
        self.assertEqual(json.loads(review["raw_output"]["content"])["verdict"], "pass")
        self.assertEqual(self.service.get_job(self.actor, self.trial["job_id"])["status"], "review_pending")
        self.assertEqual(
            self.service.get_trial_trace(self.actor, self.trial["id"])["actions"]["pending_supervision_id"],
            review["id"],
        )
        self.assertEqual(self.service.get_trial(self.actor, self.trial["id"])["score"], self.trial["score"])
        metrics = self.service._generate_report(self.trial["job_id"])["correction_summary"]
        self.assertEqual(metrics["supervisor_detection_rate"], 0.0)
        self.assertEqual(metrics["supervisor_verdict_conflicts"], 1)

    def test_legacy_revalidation_is_audited_idempotent_and_keeps_raw_reply(self) -> None:
        review = self.service.supervise_trial(self.actor, self.trial["id"])
        original = {**review["result"], "verdict": "pass", "reason": "Zero was correct."}
        raw = {"content": json.dumps(original), "response_metadata": {"finish_reason": "stop"}}
        self.service.database.update(
            "supervision_runs",
            review["id"],
            {"status": "completed", "verdict": "pass", "reason": original["reason"], "result_json": original, "raw_output_json": raw},
        )
        checked = self.service.revalidate_supervision(self.actor, review["id"])
        self.assertEqual(checked["status"], "pending_review")
        self.assertEqual(checked["raw_output"], raw)
        self.assertEqual(self.service.revalidate_supervision(self.actor, review["id"]), checked)
        audit = self.service.database.all("SELECT * FROM audit_events WHERE action='supervision.revalidated'")
        self.assertEqual(len(audit), 1)
        self.service.decide_correction(
            self.actor,
            review["id"],
            {"decision": "reject_feedback", "reason": "Human rejects the suggestion."},
        )
        with self.assertRaises(EvalError):
            self.service.revalidate_supervision(self.actor, review["id"])

    def test_official_and_platform_time_limits_are_distinct(self) -> None:
        task = {
            "id": "TB-task",
            "timeout_seconds": 3600,
            "terminal_bench": {"agent_timeout_seconds": 900, "verifier_timeout_seconds": 900},
        }
        package = self.service._supervision_input(task, self.trial)
        limits = package["task"]["constraints"]
        self.assertEqual(limits["official_agent_timeout_seconds"], 900)
        self.assertEqual(limits["official_verifier_timeout_seconds"], 900)
        self.assertEqual(limits["platform_trial_timeout_seconds"], 3600)
        self.assertNotIn("timeout_seconds", limits)
        self.assertEqual(package["review_contract"]["version"], "task-completion-v2")

    def test_successful_and_uncertain_results_are_preserved(self) -> None:
        result = {"verdict": "pass", "reason": "Task completed."}
        self.assertEqual(validate_supervision_verdict(result, {"outcome": "pass"}), result)
        uncertain = {"verdict": "uncertain", "reason": "Verifier evidence unavailable."}
        self.assertEqual(validate_supervision_verdict(uncertain, {"outcome": "grader_failed"}), uncertain)


if __name__ == "__main__":
    unittest.main()
