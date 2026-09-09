"""DSH installed Agent for Harbor 0.22.0 (Linux task containers only)."""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import uuid
from pathlib import Path

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from .dsh_trajectory import load_session_trajectories

DSH_VERSION = "0.1.1-rc.2"
NODE_VERSION = "22.19.0"


class DshAgent(BaseInstalledAgent):
    """Uses DSH's actual tools inside the task, never a proxy answer generator."""

    @staticmethod
    def name() -> str:
        return "dsh-eval"

    def version(self) -> str:
        return DSH_VERSION

    def __init__(self, *args, verifier_preflight=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.verifier_preflight = verifier_preflight
        self.guard_directory = '/logs/agent/guard-' + uuid.uuid4().hex

    def agent_command(self, instruction: str) -> list[str]:
        return ['/installed-agent/dsh/node_modules/.bin/dsh', '--profile', 'headless',
                '--patch', '/installed-agent/dsh.patch.yml', instruction]

    async def install(self, environment: BaseEnvironment) -> None:
        cache = Path(__file__).resolve().parents[1] / ".terminal-bench/runtime-cache"
        archive = cache / "dsh-runtime.tgz"
        if not archive.is_file():
            raise RuntimeError("Prepare the pinned DSH runtime archive before launching trials")
        await environment.upload_file(archive, "/installed-agent/runtime.tgz")
        await environment.upload_file(Path(__file__).with_name('dsh_process_guard.js'), '/installed-agent/process-guard.js')
        await self.exec_as_root(environment, command=(
            "tar -xzf /installed-agent/runtime.tgz -C /installed-agent && "
            "cp /installed-agent/dsh/package-lock.json /logs/agent/dsh-package-lock.json"
        ), timeout_sec=300)
        patch = self.logs_dir / "dsh.patch.yml"
        patch.write_text(
            "- id: session-persistence-jsonl\n  config:\n"
            "    root: /logs/agent/sessions\n    compression: none\n"
            "- id: agent-default-model\n  config:\n"
            "    provider: deepseek-official\n    model: deepseek-v4-flash\n",
            encoding="utf-8",
        )
        await environment.upload_file(patch, "/installed-agent/dsh.patch.yml")
        await self.exec_as_root(environment, command=(
            "mkdir -p /logs/agent/sessions /tmp/dsh-home && "
            "chmod -R a+rwX /logs/agent /tmp/dsh-home"
        ))
        if self.verifier_preflight:
            # Prepare public test dependencies before the agent clock starts. Official tests remain unchanged.
            setup = (
                'set -eu; export DEBIAN_FRONTEND=noninteractive UV_HTTP_TIMEOUT=90; '
                'apt-get -o Acquire::Retries=3 update; '
                'apt-get -o Acquire::Retries=3 install -y curl expect; '
                'curl --fail --retry 3 --connect-timeout 20 --max-time 120 -LsS '
                'https://astral.sh/uv/0.9.5/install.sh -o /tmp/eval-uv-install.sh; '
                'sh /tmp/eval-uv-install.sh; '
                '/root/.local/bin/uvx -p 3.13 -w pytest==8.4.1 -w pytest-json-ctrf==0.3.5 '
                '-w requests==2.32.4 pytest --version'
            ) if self.verifier_preflight == 'uv' else (
                'python -m pip install --retries 3 --timeout 90 --break-system-packages '
                'pytest==8.4.1 pytest-json-ctrf==0.3.5'
            )
            await self.exec_as_root(environment, command='('+setup+') > /logs/agent/verifier-preflight.log 2>&1', timeout_sec=600)

    @with_prompt_template
    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError("DEEPSEEK_API_KEY is missing")
        agent_env = {
            "DEEPSEEK_API_KEY": key,
            "DSH_HOME": "/tmp/dsh-home",
            # Full access applies only to this disposable task container.
            "DSH_PERMISSION_MODE": "danger-full-access",
            "DSH_TELEMETRY_DISABLED": "1",
            "DO_NOT_TRACK": "1",
        }
        for name in ("DEEPSEEK_BASE_URL", "DEEPSEEK_SEARCH_BASE_URL"):
            if os.environ.get(name):
                agent_env[name] = os.environ[name]
        command = (
            "export PATH=/installed-agent/node/bin:$PATH; "
            "exec /installed-agent/node/bin/node /installed-agent/process-guard.js launch " +
            shlex.quote(self.guard_directory) + ' ' + shlex.join(self.agent_command(instruction)) +
            " > /logs/agent/dsh.stdout.txt 2> /logs/agent/dsh.stderr.txt"
        )
        execution = asyncio.create_task(self.exec_as_agent(environment, command=command, env=agent_env))
        try:
            await asyncio.shield(execution)
        except BaseException:
            # Harbor's cancellation of docker exec does not terminate the container process.
            # Wait for verified remote termination before returning control to the verifier.
            try:
                stopped = await self.exec_as_root(environment, command=(
                    '/installed-agent/node/bin/node /installed-agent/process-guard.js stop '+shlex.quote(self.guard_directory)
                ), timeout_sec=20)
                if not json.loads(stopped.stdout or '{}').get('verified'):
                    raise RuntimeError('Remote agent termination was not confirmed')
                try:
                    await asyncio.wait_for(asyncio.shield(execution), timeout=15)
                except asyncio.TimeoutError:
                    execution.cancel()
                except Exception:
                    pass
            except Exception as error:
                execution.cancel()
                raise RuntimeError('AgentTerminationError: refusing to start verification without confirmed termination') from error
            raise

    def populate_context_post_run(self, context: AgentContext) -> None:
        trajectories = load_session_trajectories(self.logs_dir / "sessions")
        if trajectories:
            usages = [item.get("usage") or {} for item in trajectories]
            for source, target in (("input_tokens", "n_input_tokens"), ("output_tokens", "n_output_tokens")):
                values = [u[source] for u in usages if u.get(source) is not None]
                setattr(context, target, sum(values) if values else None)
            context.metadata = {"runtime": "deepseek-harness", "sessions": len(trajectories),
                                "tool_calls": sum(u.get("tool_calls", 0) for u in usages),
                                "trajectory_errors": sum(len(t.get('parse_errors') or []) for t in trajectories)}
