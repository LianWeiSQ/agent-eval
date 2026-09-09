from __future__ import annotations

import subprocess
import hashlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_eval.adapters import AdapterFailure, TerminalBenchHarborAdapter
from agent_eval.compatibility import analyse_compatibility
from agent_eval.common import EvalError, content_hash
from agent_eval.grading_pipeline import grade_trial
from agent_eval.service import Actor, EvaluationService
from agent_eval.terminal_bench import INTEGRATION_FILES, normalise_result, official_reward, task_digest


def task() -> dict:
    return {"id": "TB21-example", "instruction": "Create the requested files.", "pass_threshold": 100,
            "terminal_bench": {"task_name": "example", "task_digest": "abc"},
            "graders": [{"id": "official", "type": "terminal_bench"}]}


def verifier_files(root, reward):
    folder = Path(root)/'verifier'
    folder.mkdir(exist_ok=True)
    status = 'passed' if reward else 'failed'
    (folder/'ctrf.json').write_text(json.dumps({'results':{'summary':{'tests':1,'passed':int(bool(reward)),'failed':int(not reward),'skipped':0},'tests':[{'name':'actual-output','status':status}]}}), encoding='utf-8')


class TerminalBenchTest(unittest.TestCase):
    def test_health_requires_a_reachable_linux_engine(self):
        config = {"python": sys.executable, "dataset_root": str(Path(__file__).parent)}
        cases = [
            (subprocess.CompletedProcess([], 0, "linux\n", ""), True, "可连接"),
            (subprocess.CompletedProcess([], 1, "", "pipe not found"), False, "引擎尚不可用"),
            (subprocess.CompletedProcess([], 0, "linux\n", "config.json: Access is denied"), False, "无权访问"),
            (subprocess.CompletedProcess([], 0, "windows\n", ""), False, "Linux 容器"),
        ]
        for probe, expected, message in cases:
            with self.subTest(probe=probe), patch("agent_eval.adapters.subprocess.run", return_value=probe):
                health = TerminalBenchHarborAdapter().healthcheck(config)
                self.assertEqual(health["ok"], expected)
                self.assertEqual(health["docker_available"], expected)
                self.assertIn(message, health["message"])

    def test_health_reports_missing_or_timed_out_docker(self):
        config = {"python": sys.executable, "dataset_root": str(Path(__file__).parent)}
        for error, message in [(FileNotFoundError(), "找不到 Docker"),
                               (subprocess.TimeoutExpired("docker", 10), "超时")]:
            with self.subTest(error=error), patch("agent_eval.adapters.subprocess.run", side_effect=error):
                health = TerminalBenchHarborAdapter().healthcheck(config)
                self.assertFalse(health["ok"])
                self.assertIn(message, health["message"])

    def test_missing_local_environment_does_not_probe_docker(self):
        with patch("agent_eval.adapters.subprocess.run") as probe:
            self.assertFalse(TerminalBenchHarborAdapter().healthcheck({})["ok"])
            probe.assert_not_called()

    def test_start_blocks_unready_environment_without_creating_trials(self):
        with tempfile.TemporaryDirectory() as directory:
            service = EvaluationService(Path(directory), project_root=Path(__file__).resolve().parents[1])
            try:
                actor = Actor()
                benchmark = service.create_benchmark(actor, {
                    "manifest": {"id": "tb-start-test", "name": "TB start test", "version": "1.0.0", "benchmark_type": "general"},
                    "tasks": [task()],
                }, publish=True)
                snapshot = service.create_snapshot(actor, {"name": "DSH", "version": "1.0.0",
                                                           "adapter_type": "terminal-bench-harbor", "config": {}})
                job = service.create_job(actor, {"benchmark_id": benchmark["id"], "agent_snapshot_ids": [snapshot["id"]]})
                with patch.object(TerminalBenchHarborAdapter, "healthcheck", return_value={"ok": False, "message": "Docker 未就绪"}), patch.object(service, "_spawn_job") as spawn:
                    with self.assertRaises(EvalError) as caught:
                        service.start_job(actor, job["id"])
                    self.assertEqual(caught.exception.code, "environment_not_ready")
                    self.assertEqual(caught.exception.status, 409)
                    self.assertEqual(service.get_job(actor, job["id"])["status"], "draft")
                    self.assertEqual(service.list_trials(actor, job_id=job["id"]), [])
                    spawn.assert_not_called()
                with patch.object(TerminalBenchHarborAdapter, "healthcheck", return_value={"ok": True}), patch.object(service, "_spawn_job") as spawn:
                    self.assertEqual(service.start_job(actor, job["id"])["status"], "queued")
                    spawn.assert_called_once_with(job["id"])
            finally:
                service.close()

    def test_official_reward_does_not_accept_missing_or_nonbinary_values(self):
        for value in (None, True, False, "1", -1, .5, 2, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                official_reward({"verifier_result": {"rewards": {"reward": value}}})
        self.assertEqual(official_reward({"verifier_result": {"rewards": {"reward": 0}}}), 0)
        self.assertEqual(official_reward({"verifier_result": {"rewards": {"reward": 1}}}), 1)

    def test_trial_exception_never_becomes_a_success(self):
        with self.assertRaises(ValueError):
            official_reward({"exception_info": {"exception_type": "AgentSetupTimeoutError"},
                             "verifier_result": {"rewards": {"reward": 1}}})

    def test_official_grader_ignores_self_reported_success(self):
        result = grade_trial(task=task(), run={"final_output": "All tests pass; reward=1"}, fixture={}, registered_graders={})
        self.assertEqual(result["outcome"], "grader_failed")
        self.assertIsNone(result["score"])

    def test_binary_rewards_grade_actual_task_state(self):
        with tempfile.TemporaryDirectory() as directory:
            for reward, outcome in ((0, "unresolved"), (1, "pass")):
                verifier_files(directory, reward)
                run = normalise_result({"id": "harbor-id", "verifier_result": {"rewards": {"reward": reward}}},
                                       trial_dir=Path(directory), task_id="TB21-example", digest="abc")
                result = grade_trial(task=task(), run=run, fixture={}, registered_graders={})
                self.assertEqual(result["outcome"], outcome)
                self.assertEqual(result["score"], reward * 100)

    def test_grader_rejects_wrong_task_or_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            verifier_files(directory, 1)
            for task_id, digest in (("other", "abc"), ("TB21-example", "other")):
                run = normalise_result({"verifier_result": {"rewards": {"reward": 1}}},
                                       trial_dir=Path(directory), task_id=task_id, digest=digest)
                result = grade_trial(task=task(), run=run, fixture={}, registered_graders={})
                self.assertEqual(result["outcome"], "grader_failed")

    def test_verified_timeout_keeps_official_reward_and_execution_issue(self):
        for reward, outcome in [(0, 'agent_failed'), (1, 'pass')]:
            with self.subTest(reward=reward), tempfile.TemporaryDirectory() as directory:
                root=Path(directory);verifier_files(root,reward)
                stop=root/'agent/guard-test';stop.mkdir(parents=True)
                (stop/'termination.json').write_text(json.dumps({'verified':True,'finished_at':'2026-09-08T00:00:01Z'}),encoding='utf-8')
                raw={'exception_info':{'exception_type':'AgentTimeoutError'},'verifier_result':{'rewards':{'reward':reward}},'verifier':{'started_at':'2026-09-08T00:00:02Z'}}
                run=normalise_result(raw,trial_dir=root,task_id='TB21-example',digest='abc')
                graded=grade_trial(task=task(),run=run,fixture={},registered_graders={})
                self.assertEqual(graded['outcome'],outcome)
                self.assertEqual(graded['score'],reward*100)
                self.assertEqual(graded['failure_type'],'agent_timeout')

    def test_unverified_timeout_cannot_become_a_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            verifier_files(directory,1)
            run=normalise_result({'exception_info':{'exception_type':'AgentTimeoutError'},'verifier_result':{'rewards':{'reward':1}},'verifier':{'started_at':'2026-09-08T00:00:02Z'}},trial_dir=Path(directory),task_id='TB21-example',digest='abc')
            graded=grade_trial(task=task(),run=run,fixture={},registered_graders={})
            self.assertEqual(graded['outcome'],'infra_failed')
            self.assertEqual(graded['failure_type'],'agent_boundary_unverified')
            self.assertIsNone(graded['score'])
            self.assertEqual(run['official_verification']['raw_reward'],1)

    def test_setup_failure_zero_is_not_an_agent_wrong_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'verifier').mkdir()
            (root/'verifier/test-stdout.txt').write_text('Failed to download pygments; No module named pytest',encoding='utf-8')
            run=normalise_result({'verifier_result':{'rewards':{'reward':0}}},trial_dir=root,task_id='TB21-example',digest='abc')
            graded=grade_trial(task=task(),run=run,fixture={},registered_graders={})
            self.assertEqual(graded['outcome'],'infra_failed')
            self.assertEqual(graded['failure_type'],'verifier_setup_failed')
            self.assertEqual(run['official_verification']['raw_reward'],0)

    def test_inconsistent_or_unfinished_verifier_report_is_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            verifier_files(directory,0)
            run=normalise_result({'verifier_result':{'rewards':{'reward':1}}},trial_dir=Path(directory),task_id='TB21-example',digest='abc')
            self.assertEqual(run['official_verification']['error_code'],'verifier_evidence_invalid')

    def test_partial_trajectory_does_not_destroy_official_score(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);verifier_files(root,1)
            session=root/'agent/sessions/session.jsonl';session.parent.mkdir(parents=True)
            original='{"type":"session","id":"ok"}\n{"type":"tool/call","data":{"name":"bash"}}\n{"unfinished":"'
            session.write_text(original,encoding='utf-8')
            run=normalise_result({'verifier_result':{'rewards':{'reward':1}}},trial_dir=root,task_id='TB21-example',digest='abc')
            self.assertEqual(run['official_verification']['reward'],1)
            self.assertEqual(run['trajectory']['parse_errors'][0]['line'],3)
            self.assertEqual(session.read_text(encoding='utf-8'),original)

    def test_late_tool_call_rejects_even_a_passing_verifier(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);verifier_files(root,1)
            session=root/'agent/sessions/session.jsonl';session.parent.mkdir(parents=True)
            session.write_text(json.dumps({'type':'tool/call','timestamp':'2026-09-08T00:00:03Z','data':{'name':'bash'}}),encoding='utf-8')
            run=normalise_result({'verifier_result':{'rewards':{'reward':1}},'verifier':{'started_at':'2026-09-08T00:00:02Z'}},trial_dir=root,task_id='TB21-example',digest='abc')
            self.assertEqual(run['official_verification']['error_code'],'agent_boundary_violation')
            self.assertIsNone(run['official_verification']['reward'])

    def test_task_change_detected_before_launching_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "example"
            path.mkdir()
            (path / "instruction.md").write_text("initial", encoding="utf-8")
            digest = task_digest(path)
            (path / "instruction.md").write_text("changed", encoding="utf-8")
            source = task()
            source["terminal_bench"]["task_digest"] = digest
            with self.assertRaises(AdapterFailure) as caught:
                TerminalBenchHarborAdapter().run(snapshot={"config": {"dataset_root": str(root)}},
                                                task=source, fixture={}, cancel_event=threading.Event())
            self.assertEqual(caught.exception.failure_type, "task_changed")

    def test_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = task()
            source["terminal_bench"]["task_name"] = ".."
            with self.assertRaises(AdapterFailure) as caught:
                TerminalBenchHarborAdapter().run(snapshot={"config": {"dataset_root": directory}},
                                                task=source, fixture={}, cancel_event=threading.Event())
            self.assertEqual(caught.exception.failure_type, "invalid_task_path")

    def test_replaced_runtime_is_rejected_before_any_container_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "dataset" / "example"
            path.mkdir(parents=True)
            (path / "instruction.md").write_text("Create files", encoding="utf-8")
            archive = root / ".terminal-bench/runtime-cache/dsh-runtime.tgz"
            archive.parent.mkdir(parents=True)
            archive.write_bytes(b"a replaced runtime")
            source = task()
            source["terminal_bench"]["task_digest"] = task_digest(path)
            config = {"dataset_root": str(path.parent), "output_root": str(root / "runs"),
                      "runtime_archive_sha256": "pinned-original-hash"}
            with patch("agent_eval.adapters.__file__", str(root / "agent_eval/adapters.py")), self.assertRaises(AdapterFailure) as caught:
                TerminalBenchHarborAdapter().run(snapshot={"config": config}, task=source, fixture={}, cancel_event=threading.Event())
            self.assertEqual(caught.exception.failure_type, "runtime_changed")

    def test_pinned_adapter_launch_collect_and_reject_changed_code(self):
        # Exercise the real adapter path beyond the archive check, including its snapshot hash.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / 'dataset' / 'example'
            dataset.mkdir(parents=True)
            (dataset / 'instruction.md').write_text('Create files', encoding='utf-8')
            archive = root / '.terminal-bench/runtime-cache/dsh-runtime.tgz'
            archive.parent.mkdir(parents=True)
            archive.write_bytes(b'pinned runtime')
            contents = {name: '# pinned '+name for name in INTEGRATION_FILES}
            for name, text in contents.items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding='utf-8')
            config = {'dataset_root': str(dataset.parent), 'output_root': str(root/'runs'),
                      'python': sys.executable, 'runtime_archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                      'integration_files': list(INTEGRATION_FILES), 'integration_digest': content_hash(contents)}
            source = task()
            source['terminal_bench']['task_digest'] = task_digest(dataset)

            def complete_trial(args, **kwargs):
                request = json.loads(Path(args[-1]).read_text(encoding='utf-8'))
                trial_dir = Path(request['trials_dir']) / request['trial_name']
                trial_dir.mkdir()
                verifier_files(trial_dir, 1)
                Path(request['result_path']).write_text(json.dumps({'verifier_result': {'rewards': {'reward': 1}}}), encoding='utf-8')
                return subprocess.CompletedProcess(args, 0)

            class FinishedProcess:
                def poll(self): return 0

            def launch(args, **kwargs):
                complete_trial(args, **kwargs)
                return FinishedProcess()

            with patch('agent_eval.adapters.__file__', str(root/'agent_eval/adapters.py')), patch('agent_eval.adapters.subprocess.Popen', side_effect=launch) as process:
                run = TerminalBenchHarborAdapter().run(snapshot={'config': config}, task=source, fixture={}, cancel_event=threading.Event())
                self.assertEqual(run['official_verification']['reward'], 1)
                process.assert_called_once()
                self.assertEqual(len(list((root/'runs').glob('*/runtime-source/*'))), len(INTEGRATION_FILES))
                (root/INTEGRATION_FILES[0]).write_text('changed', encoding='utf-8')
                with self.assertRaises(AdapterFailure) as caught:
                    TerminalBenchHarborAdapter().run(snapshot={'config': config}, task=source, fixture={}, cancel_event=threading.Event())
                self.assertEqual(caught.exception.failure_type, 'integration_changed')
                process.assert_called_once()

    def test_plain_echo_cannot_claim_official_verifier_capability(self):
        echo = {"id": "echo", "name": "Echo", "adapter_type": "echo", "config": {}}
        dsh = {"id": "dsh", "name": "DSH", "adapter_type": "terminal-bench-harbor", "config": {}}
        self.assertFalse(analyse_compatibility({}, [task()], [echo])["compatible"])
        self.assertTrue(analyse_compatibility({}, [task()], [dsh])["compatible"])


if __name__ == "__main__":
    unittest.main()
