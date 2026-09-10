from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_eval.docker_cleanup import cleanup_trial_environment, compose_project_name, reap_output_root


def completed(args, stdout="", returncode=0):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")


class DockerCleanupTest(unittest.TestCase):
    def test_only_runtime_resources_are_removed_and_images_are_retained(self):
        responses = [
            completed([], "container-id\n"), completed([], "network-id\n"), completed([], "volume-id\n"),
            completed([]), completed([]), completed([]),
            completed([]), completed([]), completed([]),
        ]
        with patch("agent_eval.docker_cleanup._docker", side_effect=responses) as docker:
            report = cleanup_trial_environment("example__harbor-123456789abc")
        commands = [call.args[0] for call in docker.call_args_list]
        self.assertTrue(report["verified"])
        self.assertEqual(report["status"], "cleaned")
        self.assertTrue(report["images_retained"])
        self.assertIn(["rm", "-f", "container-id"], commands)
        self.assertIn(["network", "rm", "network-id"], commands)
        self.assertIn(["volume", "rm", "-f", "volume-id"], commands)
        self.assertFalse(any(command[:1] == ["image"] for command in commands))

    def test_unscoped_project_name_is_rejected(self):
        for name in ("other-project", "example__harbor-not-hex", "example__harbor-123456789abc__env"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                compose_project_name(name)

    def test_startup_reaper_uses_only_requests_below_configured_output_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = root / "harbor-123456789abc/request.json"
            request.parent.mkdir()
            request.write_text(json.dumps({"trial_name": "example__harbor-123456789abc"}), encoding="utf-8")
            outside = root.parent / "unrelated-request.json"
            outside.write_text(json.dumps({"trial_name": "other__harbor-aaaaaaaaaaaa"}), encoding="utf-8")
            try:
                report = {"status": "cleaned", "verified": True}
                with patch("agent_eval.docker_cleanup.cleanup_trial_environment", return_value=report) as cleanup:
                    self.assertEqual(reap_output_root(root), [report])
                cleanup.assert_called_once_with("example__harbor-123456789abc")
            finally:
                outside.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
