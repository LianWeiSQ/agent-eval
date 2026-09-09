"""Verify remote process termination with the real pinned Harbor runtime, without LLM calls."""
import asyncio
import json
import os
from pathlib import Path
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from harbor.models.trial.config import TrialConfig
from harbor.trial.trial import Trial
from agent_eval.terminal_bench import normalise_result,task_digest

VERIFIER='''#!/bin/sh
python3 - <<'PY'
import json,pathlib,time
reports=list(pathlib.Path('/logs/agent').glob('guard-*/termination.json'))
assert len(reports)==1
boundary=json.loads(reports[0].read_text())
assert boundary['verified'] and not boundary['remaining_processes']
p=pathlib.Path('/app/guard-writes.log');before=p.read_bytes()
assert before
time.sleep(1)
assert p.read_bytes()==before, 'A surviving process wrote during verification'
v=pathlib.Path('/logs/verifier');v.mkdir(exist_ok=True)
(v/'ctrf.json').write_text(json.dumps({'results':{'summary':{'tests':1,'passed':1,'failed':0,'skipped':0},'tests':[{'name':'no-writes-after-cutoff','status':'passed'}]}}))
(v/'reward.txt').write_text('1')
PY
'''

async def main():
    root=ROOT/'.data-supervised-loop/termination-verification'/uuid.uuid4().hex[:10]
    task=root/'task';(task/'tests').mkdir(parents=True)
    (task/'instruction.md').write_text('Synthetic process isolation check; no model inference.',encoding='utf-8')
    (task/'task.toml').write_text('''version="1.0"
[agent]
timeout_sec=3
[verifier]
timeout_sec=20
[environment]
docker_image="alexgshaw/regex-chess:20251031"
cpus=1
memory_mb=1024
''',encoding='utf-8')
    (task/'tests/test.sh').write_text(VERIFIER,encoding='utf-8',newline='\n')
    os.environ['DEEPSEEK_API_KEY']='synthetic-probe-no-network-call'
    outcomes=[]
    for mode,expected in [('timeout','AgentTimeoutError'),('exit','NonZeroAgentExitCodeError')]:
        config=TrialConfig.model_validate({'task':{'path':str(task)},'trial_name':'guard-'+mode+'-'+root.name,'trials_dir':str(root),
            'agent':{'import_path':'tests.harbor_timeout_probe:TimeoutProbeAgent','override_setup_timeout_sec':180,
                     'env':{'PROBE_EXIT':'1' if mode=='exit' else '0'}},
            'environment':{'type':'docker','delete':True}})
        trial=await Trial.create(config)
        # Extra env is a documented AgentConfig field; assignment also covers older pinned versions.
        trial.agent.extra_env['PROBE_EXIT']='1' if mode=='exit' else '0'
        result=await trial.run();raw=result.model_dump(mode='json')
        p=root/config.trial_name
        assert raw['exception_info']['exception_type']==expected, raw.get('exception_info')
        assert raw['verifier_result']['rewards']['reward']==1, raw.get('verifier_result')
        run=normalise_result(raw,trial_dir=p,task_id='boundary-probe',digest=task_digest(task))
        assert run['official_verification']['reward']==1,run['official_verification']
        assert run['official_verification']['termination_verified']
        outcomes.append({'mode':mode,'exception':expected,'verified':True,'official_reward':1,'path':str(p)})
    (root/'verification.json').write_text(json.dumps(outcomes,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'checks':outcomes,'verification_path':str(root/'verification.json')}),flush=True)

if __name__=='__main__':asyncio.run(main())
