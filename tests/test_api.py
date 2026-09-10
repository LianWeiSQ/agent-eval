from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from agent_eval.api import ApiHandler, EvaluationHTTPServer
from agent_eval.service import Actor, EvaluationService


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class EvaluationApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service = EvaluationService(Path(self.temp.name), project_root=PROJECT_ROOT)
        self.service.ensure_demo_data(Actor())
        handler = type("TestApiHandler", (ApiHandler,), {"service": self.service, "log_message": lambda *args: None})
        self.server = EvaluationHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def test_second_server_cannot_silently_share_the_same_port(self) -> None:
        with self.assertRaises(OSError):
            duplicate = EvaluationHTTPServer(self.server.server_address, ApiHandler)
            duplicate.server_close()

    def test_job_summary_omits_raw_trajectories_but_full_evidence_is_preserved(self) -> None:
        actor = Actor()
        benchmark = self.service.create_benchmark(actor, {
            "manifest": {"id": "compact-job", "name": "Compact Job", "version": "1.0.0"},
            "tasks": [{"id": "one", "instruction": "Say done"}],
        }, publish=True)
        snapshot = self.service.create_snapshot(actor, {"name": "Compact Echo", "version": "1", "adapter_type": "echo", "config": {}})
        job = self.service.create_job(actor, {"benchmark_id": benchmark["id"], "agent_snapshot_ids": [snapshot["id"]]})
        self.service.start_job(actor, job["id"])
        self.service.close()
        trial = self.service.list_trials(actor, job_id=job["id"])[0]
        run = {"events": [{"payload": {"text": "x" * 2_000_000}}]}
        self.service.database.update("trials", trial["id"], {"agent_run_json": run})
        with urllib.request.urlopen(self.base_url + f"/api/v1/jobs/{job['id']}?view=summary") as response:
            raw = response.read()
        summary = json.loads(raw)["data"]
        self.assertLess(len(raw), 10000)
        self.assertEqual(summary["detail_view"], "summary")
        self.assertEqual(summary["trials"][0]["id"], trial["id"])
        self.assertEqual(summary["trials"][0]["outcome"], trial["outcome"])
        self.assertNotIn("agent_run", summary["trials"][0])
        self.assertNotIn("grades", summary["trials"][0])
        self.assertNotIn("report", summary)
        self.assertEqual(self.service.get_job(actor, job["id"])["trials"][0]["agent_run"], run)

    def test_health_dashboard_and_portal(self) -> None:
        with urllib.request.urlopen(self.base_url + "/health") as response:
            health = json.loads(response.read().decode("utf-8"))["data"]
        with urllib.request.urlopen(self.base_url + "/api/v1/dashboard") as response:
            dashboard = json.loads(response.read().decode("utf-8"))["data"]
        with urllib.request.urlopen(self.base_url + "/") as response:
            portal = response.read().decode("utf-8")
        self.assertEqual(health["status"], "ok")
        self.assertEqual(dashboard["counts"]["benchmarks"], 1)
        self.assertEqual(dashboard["counts"]["snapshots"], 2)
        self.assertIn("Agent Eval 评测", portal)
        self.assertIn("执行轨迹", portal)
        self.assertIn("Agent / Harbor 评测", portal)
        snapshots = self.service.list_snapshots(Actor())
        supervisor = next(item for item in snapshots if item["adapter_type"] == "llm-supervisor")
        self.assertEqual(supervisor["version"], "1.3.2")
        self.assertEqual(supervisor["config"]["api_key_env"], "EVAL_SUPERVISOR_API_KEY")
        dsh = next(item for item in snapshots if item["adapter_type"] == "dsh-headless")
        self.assertEqual(dsh["version"], "1.2.0")
        self.assertEqual(dsh["config"]["command"][1:], ["--profile", "headless", "--patch", "{patch_file}", "{instruction}"])
        self.assertEqual(dsh["config"]["patch_file"], "examples/dsh-eval-session.patch.yml")

    def test_terminal_bench_backend_routes_expose_status_setup_and_direct_start(self) -> None:
        with patch.object(self.service, "terminal_bench_status", return_value={"ready": True}) as status:
            with urllib.request.urlopen(self.base_url + "/api/v1/terminal-bench/status") as response:
                self.assertTrue(json.loads(response.read())["data"]["ready"])
            status.assert_called_once()
        registration = {"benchmark_id": "bench-1", "agent_snapshot_id": "asnap-1"}
        with patch.object(self.service, "prepare_terminal_bench", return_value=registration) as prepare:
            request = urllib.request.Request(self.base_url + "/api/v1/terminal-bench/setup", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request) as response:
                self.assertEqual(json.loads(response.read())["data"], registration)
            prepare.assert_called_once()
        started = {"job": {"id": "job-1", "status": "queued"}, "selected_task_ids": ["TB21-example"]}
        with patch.object(self.service, "start_terminal_bench_evaluation", return_value=started) as start:
            payload = json.dumps({"task_names": ["example"]}).encode()
            request = urllib.request.Request(self.base_url + "/api/v1/terminal-bench/evaluations", data=payload, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request) as response:
                self.assertEqual(json.loads(response.read())["data"], started)
            start.assert_called_once_with(Actor(), {"task_names": ["example"]})

    def test_agent_try_api_returns_observable_result(self) -> None:
        snapshot = self.service.create_snapshot(Actor(), {"name": "Echo Playground", "version": "1.0.0", "adapter_type": "echo", "config": {}})
        request = urllib.request.Request(
            self.base_url + f"/api/v1/agent-snapshots/{snapshot['id']}/try",
            data=json.dumps({"instruction": "你好"}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Role": "project_admin"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            result = json.loads(response.read().decode("utf-8"))["data"]
        self.assertEqual(result["final_output"]["content"], "你好")
        self.assertFalse(result["evidence"]["network_used"])

    def test_supervision_and_trace_api(self) -> None:
        actor = Actor()
        benchmark = self.service.create_benchmark(actor, {
            "manifest": {"id": "unit-supervision-api", "name": "Supervision API", "version": "1", "benchmark_type": "general"},
            "tasks": [{"id": "unit-supervision", "instruction": "return expected", "expected_output": {"answer": "expected"},
                "graders": [{"id": "unit-rule", "type": "rule", "config": {"contains": ["expected"]}}]}],
        }, publish=True)
        snapshot = self.service.create_snapshot(actor, {"name": "Unit Correction Echo", "version": "1", "adapter_type": "echo", "config": {
            "fixed_output": "wrong", "corrected_output": "expected", "fixture_events": [
                {"event_type": "tool_call", "payload": {"name": "unit_read", "arguments": {}}}]}})
        job = self.service.create_job(actor, {"benchmark_id": benchmark["id"], "agent_snapshot_ids": [snapshot["id"]]})
        self.service.start_job(actor, job["id"])
        deadline = time.monotonic() + 5
        trial = None
        while time.monotonic() < deadline:
            trials = self.service.list_trials(actor, job_id=job["id"])
            if trials and trials[0]["outcome"] is not None:
                trial = trials[0]
                break
            time.sleep(0.02)
        self.assertIsNotNone(trial)
        request = urllib.request.Request(
            self.base_url + f"/api/v1/trials/{trial['id']}/supervise",
            data=b"{}",
            headers={"Content-Type": "application/json", "X-Role": "project_admin"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            supervision = json.loads(response.read().decode("utf-8"))["data"]
        self.assertEqual(supervision["verdict"], "fail")
        with urllib.request.urlopen(self.base_url + f"/api/v1/trials/{trial['id']}/trace?view=detail") as response:
            trace = json.loads(response.read().decode("utf-8"))["data"]
        self.assertEqual(trace["actions"]["pending_supervision_id"], supervision["id"])
        self.assertIn("tool_call", {node["type"] for node in trace["nodes"]})


if __name__ == "__main__":
    unittest.main()
