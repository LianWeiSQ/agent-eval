"""Private subprocess boundary: Harbor dependencies stay out of the Eval server."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load_credential() -> None:
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


async def main(request: dict) -> None:
    from importlib.metadata import version
    if version("harbor") != "0.22.0":
        raise RuntimeError("This integration is pinned to harbor==0.22.0")
    from harbor.models.trial.config import TrialConfig
    from harbor.trial.trial import Trial
    load_credential()
    config = TrialConfig.model_validate({
        "task": {"path": request["task_dir"]},
        "trial_name": request["trial_name"],
        "trials_dir": request["trials_dir"],
        "environment_build_timeout_multiplier": request.get("environment_build_timeout_multiplier", 3.0),
        "agent": {
            "import_path": "agent_eval.harbor_dsh:DshAgent",
            "model_name": "deepseek-official/deepseek-v4-flash",
            "override_setup_timeout_sec": 900,
            'kwargs': {'verifier_preflight': request.get('verifier_preflight')},
        },
        "environment": {"type": "docker", "delete": True},
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
