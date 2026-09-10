"""Private subprocess boundary: Harbor dependencies stay out of the Eval server."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load_deepseek_credential() -> None:
    """Reuse only the configured DeepSeek credential; never copy the user's home."""
    if os.environ.get("DEEPSEEK_API_KEY"):
        return
    import yaml
    home = Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh")))
    credential_path = home / ".credentials.yaml"
    if credential_path.is_file():
        data = yaml.safe_load(credential_path.read_text(encoding="utf-8")) or {}
        refs = data.get("refs") if isinstance(data, dict) else None
        value = (refs.get("DEEPSEEK_API_KEY") if isinstance(refs, dict) else None) or (
            data.get("DEEPSEEK_API_KEY") if isinstance(data, dict) else None
        )
        if isinstance(value, str) and value:
            os.environ["DEEPSEEK_API_KEY"] = value
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise RuntimeError("No DEEPSEEK_API_KEY in environment or DSH credential store")


def docker_proxy_environment() -> dict[str, str]:
    """Forward Docker Desktop's non-secret engine proxy to task containers."""
    try:
        probe = subprocess.run(
            ["docker", "info", "--format", "{{json .}}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
        info = json.loads(probe.stdout) if probe.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}

    def safe_proxy(value: object) -> str | None:
        candidate = str(value or "").strip()
        if not candidate:
            return None
        if "://" not in candidate:
            candidate = "http://" + candidate
        parsed = urlsplit(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        return candidate

    result: dict[str, str] = {}
    for source, upper, lower in (
        ("HttpProxy", "HTTP_PROXY", "http_proxy"),
        ("HttpsProxy", "HTTPS_PROXY", "https_proxy"),
    ):
        value = safe_proxy(info.get(source))
        if value:
            result[upper] = result[lower] = value
    no_proxy = str(info.get("NoProxy") or "").strip()
    if no_proxy:
        result["NO_PROXY"] = result["no_proxy"] = no_proxy
    return result


def agent_config(request: dict) -> dict:
    harness = str(request.get("harness") or "dsh")
    if harness == "dsh":
        load_deepseek_credential()
        return {
            "import_path": "agent_eval.harbor_dsh:DshAgent",
            "model_name": str(request.get("model") or "deepseek-official/deepseek-v4-flash"),
            "override_setup_timeout_sec": 900,
        }
    if harness != "codex":
        raise RuntimeError(f"Unsupported Terminal-Bench harness: {harness}")
    model = str(request.get("model") or "gpt-5.6-sol").split("/")[-1]
    reasoning_effort = str(request.get("reasoning_effort") or "xhigh")
    common = {
        "name": "codex",
        "model_name": "openai/" + model,
        "override_setup_timeout_sec": 900,
        "kwargs": {
            "version": str(request.get("codex_version") or "0.154.0"),
            "reasoning_effort": reasoning_effort,
            "config": {
                "model": model,
                "model_reasoning_effort": reasoning_effort,
                "disable_response_storage": bool(request.get("disable_response_storage", True)),
                "network_access": "enabled",
            },
        },
    }
    auth_mode = str(request.get("auth_mode") or "api-key")
    if auth_mode == "codex-auth-json":
        auth_path = Path(os.environ.get("CODEX_AUTH_JSON_PATH") or "")
        if not auth_path.is_file():
            raise RuntimeError("CODEX_AUTH_JSON_PATH does not point to a readable Codex auth file")
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("OPENAI_BASE_URL", None)
        return common
    if auth_mode != "api-key":
        raise RuntimeError(f"Unsupported Codex auth mode: {auth_mode}")
    key_env = str(request.get("api_key_env") or "OPENAI_API_KEY")
    key = os.environ.get(key_env)
    if not key:
        raise RuntimeError(f"No API key in environment variable {key_env}")
    os.environ["OPENAI_API_KEY"] = key
    os.environ.pop("CODEX_AUTH_JSON_PATH", None)
    os.environ.pop("CODEX_FORCE_AUTH_JSON", None)
    base_url = str(request.get("base_url") or "").rstrip("/")
    if not base_url:
        raise RuntimeError("Codex model base_url is missing")
    os.environ["OPENAI_BASE_URL"] = base_url
    common["kwargs"]["config"].update({
        "model_provider": "eval_openai",
        "model_providers": {
            "eval_openai": {
                "name": "OpenAI-compatible evaluation endpoint",
                "base_url": base_url,
                "wire_api": "responses",
                "requires_openai_auth": True,
            }
        },
    })
    return common


async def main(request: dict) -> None:
    from importlib.metadata import version
    if version("harbor") != "0.22.0":
        raise RuntimeError("This integration is pinned to harbor==0.22.0")
    from harbor.models.trial.config import TrialConfig
    from harbor.trial.trial import Trial
    environment = {"type": "docker", "delete": True}
    proxy_environment = docker_proxy_environment()
    if proxy_environment:
        environment["env"] = proxy_environment
    config = TrialConfig.model_validate({
        "task": {"path": request["task_dir"]},
        "trial_name": request["trial_name"],
        "trials_dir": request["trials_dir"],
        "environment_build_timeout_multiplier": request.get("environment_build_timeout_multiplier", 3.0),
        "agent": agent_config(request),
        "environment": environment,
        "extra_instructions": request.get("extra_instructions", []),
    })
    trial = await Trial.create(config)
    execution = asyncio.create_task(trial.run())
    try:
        while not execution.done():
            if Path(request["cancel_path"]).exists():
                execution.cancel()
                break
            await asyncio.wait({execution}, timeout=1)
        result = await execution
        Path(request["result_path"]).write_text(result.model_dump_json(indent=2), encoding="utf-8")
    finally:
        if not execution.done():
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)


if __name__ == "__main__":
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    asyncio.run(main(request))
