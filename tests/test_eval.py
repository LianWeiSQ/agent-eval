"""Regression checks using only the retained MMLU package and in-memory outputs."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import unittest
from agent_eval.benchmark import load_package
from agent_eval.cli import main
from agent_eval.grading_pipeline import grade_trial

ROOT = Path(__file__).resolve().parents[1]

class BenchmarkEvalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tasks = load_package(ROOT / "benchmarks/mmlu-high-school-computer-science-smoke")["tasks"]

    def grade(self, task, text, events=None):
        return grade_trial(task=task, run={"final_output": {"type": "text", "content": text}, "events": events or [], "usage": {"tool_calls": len(events or [])}}, fixture={}, registered_graders={})

    def test_exact_mmlu_answers_pass(self):
        for task in self.tasks:
            with self.subTest(task=task["id"]):
                result = self.grade(task, task["expected_output"]["answer"])
                self.assertEqual(result["outcome"], "pass")
                self.assertEqual(result["score"], 100)

    def test_wrong_choices_do_not_pass(self):
        for task in self.tasks:
            correct = task["expected_output"]["answer"]
            wrong = next("FINAL: " + letter for letter in "ABCD" if "FINAL: " + letter != correct)
            with self.subTest(task=task["id"]):
                self.assertEqual(self.grade(task, wrong)["outcome"], "unresolved")

    def test_correct_choice_with_tool_use_violates_contract(self):
        task = self.tasks[0]
        result = self.grade(task, task["expected_output"]["answer"], [{"event_type": "tool_call", "payload": {"name": "unit_tool", "arguments": {}}}])
        self.assertNotEqual(result["outcome"], "pass")

    def test_default_cli_list_works_without_removed_packages(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["list"]), 0)
        self.assertIn("mmlu-high-school-computer-science-smoke", output.getvalue())
        self.assertEqual(output.getvalue().count("MMLU-HSCS-"), 20)

if __name__ == "__main__":
    unittest.main()
