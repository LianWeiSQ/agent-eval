from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import import_terminal_bench_full as importer


class TerminalBenchImportTest(unittest.TestCase):
    def test_fresh_snapshot_pins_all_runtime_files_without_local_machine_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / '.terminal-bench/runtime-cache/dsh-runtime.tgz'
            archive.parent.mkdir(parents=True)
            archive.write_bytes(b'pinned-runtime')
            python = root / ('.terminal-bench-venv/Scripts/python.exe' if importer.sys.platform == 'win32' else '.terminal-bench-venv/bin/python')
            python.parent.mkdir(parents=True)
            python.write_bytes(b'placeholder')
            for name in importer.INTEGRATION_FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('# frozen '+name, encoding='utf-8')
            with patch.object(importer, 'ROOT', root):
                config = importer.snapshot_config(root/'tasks', root/'.data', {'manifest':{'id':'full'}, 'tasks':[{'timeout_seconds':9000}]})
            self.assertEqual(config['python'], str(python))
            self.assertIn('agent_eval/dsh_process_guard.js', config['integration_files'])
            self.assertIn('agent_eval/dsh_trajectory.py', config['integration_files'])
            self.assertIn('agent_eval/docker_cleanup.py', config['integration_files'])
            self.assertEqual(config['runner_timeout_seconds'], 9000)
            self.assertEqual(config['environment_build_timeout_multiplier'], 3.0)
            self.assertEqual(config['environment_cleanup'], 'verified-runtime-resources-v1')
            self.assertEqual(config['image_cache'], 'retain-prebuilt-images')

    def test_snapshot_config_accepts_backend_python_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / '.terminal-bench/runtime-cache/dsh-runtime.tgz'
            archive.parent.mkdir(parents=True)
            archive.write_bytes(b'pinned-runtime')
            python = root / 'backend-python.exe'
            python.write_bytes(b'placeholder')
            for name in importer.INTEGRATION_FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('# frozen '+name, encoding='utf-8')
            with patch.object(importer, 'ROOT', root):
                config = importer.snapshot_config(
                    root/'tasks', root/'.data', {'manifest':{'id':'full'}, 'tasks':[{'timeout_seconds':9000}]},
                    python_executable=python,
                )
            self.assertEqual(config['python'], str(python))

    def test_codex_auth_snapshot_records_mode_without_endpoint_or_secret_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / 'backend-python.exe'
            python.write_bytes(b'placeholder')
            for name in importer.INTEGRATION_FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('# frozen '+name, encoding='utf-8')
            profile = {
                'id': 'codex-gpt-5.6-sol', 'harness': 'codex', 'provider': 'openai',
                'model': 'gpt-5.6-sol', 'api_key_env': 'OPENAI_API_KEY',
                'default_reasoning_effort': 'xhigh', 'codex_version': '0.154.0',
            }
            with patch.object(importer, 'ROOT', root):
                config = importer.snapshot_config(
                    root/'tasks', root/'.data',
                    {'manifest': {'id': 'full'}, 'tasks': [{'timeout_seconds': 9000}]},
                    python_executable=python, agent_profile=profile,
                    reasoning_effort='xhigh', auth_mode='codex-auth-json',
                )
            self.assertEqual(config['auth_mode'], 'codex-auth-json')
            self.assertNotIn('base_url', config)
            self.assertNotIn('auth.json', str(config))

    def test_snapshot_uses_commit_and_preserves_existing_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            task = repo / "tasks/example"
            task.mkdir(parents=True)
            instruction = task / "instruction.md"
            instruction.write_bytes(b"Original\n")

            def git(*args):
                return subprocess.check_output(["git", "-C", str(repo), *args], text=True, encoding="utf-8").strip()

            git("init", "-q")
            git("-c", "core.autocrlf=false", "add", "tasks")
            git("-c", "user.name=Import Test", "-c", "user.email=import@example.invalid", "commit", "-qm", "Fixture")
            commit = git("rev-parse", "HEAD")
            instruction.write_bytes(b"Uncommitted change\n")
            destination, archive = root / "snapshot", root / "snapshot.tar"
            with patch.object(importer, "COMMIT", commit), patch.object(importer, "TASK_COUNT", 1):
                self.assertEqual(importer.materialize_tasks(repo, destination, archive), ["example"])
                pinned = destination / "tasks/example/instruction.md"
                self.assertEqual(pinned.read_bytes(), b"Original\n")
                self.assertEqual(instruction.read_bytes(), b"Uncommitted change\n")
                self.assertEqual(importer.materialize_tasks(repo, destination, archive), ["example"])
                pinned.write_bytes(b"Locally changed snapshot\n")
                with self.assertRaisesRegex(RuntimeError, "refusing to overwrite"):
                    importer.materialize_tasks(repo, destination, archive)
                self.assertEqual(pinned.read_bytes(), b"Locally changed snapshot\n")

    def test_long_task_keeps_official_phase_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "long-task"
            (task / "tests").mkdir(parents=True)
            (task / "instruction.md").write_text("Complete the task.", encoding="utf-8")
            (task / "tests/test.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            (task / "task.toml").write_text(
                '[metadata]\ncategory="software-engineering"\n'
                '[agent]\ntimeout_sec=12000\n[verifier]\ntimeout_sec=12000\n'
                '[environment]\nbuild_timeout_sec=600\ncpus=4\nmemory_mb=8192\n', encoding="utf-8")
            package = importer.build_package(root, ["long-task"])
            imported = package["tasks"][0]
            self.assertEqual(imported["timeout_seconds"], 25800)
            self.assertEqual(imported["terminal_bench"]["agent_timeout_seconds"], 12000)
            self.assertEqual(imported["terminal_bench"]["verifier_timeout_seconds"], 12000)
            self.assertEqual(imported["environment"]["memory_mb"], 8192)
            self.assertEqual(imported["graders"][0]["type"], "terminal_bench")

    def test_repeat_registration_reuses_records_without_starting_job(self):
        package = {"tasks": [{"id": "example"}]}
        config = {"dataset_root": "pinned/tasks", "runner_timeout_seconds": 25800}
        responses = {
            "/benchmarks": [{"id": "existing-benchmark", "content_hash": "package-hash"}],
            "/agent-snapshots": [{"id": "existing-snapshot", "adapter_type": "terminal-bench-harbor",
                                  "config_hash": importer.content_hash(config)}],
        }

        def read_only_api(base, path, payload=None):
            self.assertIsNone(payload, "Re-import must not mutate matching records")
            return responses[path]  # Any unexpected endpoint, including /jobs, fails.

        with patch.object(importer, "api", side_effect=read_only_api), patch.object(
            importer, "validate_package", return_value={"content_hash": "package-hash"}
        ):
            result = importer.register("http://localhost", package, config)
        self.assertEqual(result["benchmark_id"], "existing-benchmark")
        self.assertEqual(result["agent_snapshot_id"], "existing-snapshot")
        self.assertFalse(result["evaluation_started"])


if __name__ == "__main__":
    unittest.main()
