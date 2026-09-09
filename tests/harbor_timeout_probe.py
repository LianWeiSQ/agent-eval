"""Real Docker/Harbor boundary probe; uses a synthetic writer, no model API."""
from pathlib import Path
from agent_eval.harbor_dsh import DshAgent

WRITER = '''import os,signal,subprocess,sys,time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
if len(sys.argv)==1:
    subprocess.Popen([sys.executable,__file__,"detached"],start_new_session=True)
    subprocess.Popen([sys.executable,__file__,"ordinary"])
    if os.environ.get("PROBE_EXIT") == "1":
        time.sleep(.5)
        sys.exit(9)
while True:
    with open("/app/guard-writes.log","a") as f:
        f.write(str(os.getpid())+"\\n")
    time.sleep(.02)
'''

class TimeoutProbeAgent(DshAgent):
    async def install(self, environment):
        await super().install(environment)
        source=self.logs_dir/'probe.py'
        source.write_text(WRITER,encoding='utf-8')
        await environment.upload_file(source,'/installed-agent/probe.py')

    def agent_command(self,instruction):
        return ['python3','/installed-agent/probe.py']
