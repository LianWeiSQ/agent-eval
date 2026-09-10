"""Async guard failures must abort the agent phase instead of allowing verification."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from agent_eval.harbor_dsh import DshAgent

class FakeAgent(DshAgent):
    stop_verified = True
    stop_requested = False
    execution_done = False
    async def exec_as_agent(self,*args,**kwargs):
        await self.stopped.wait()
        self.execution_done=True
        raise RuntimeError('terminated')
    async def exec_as_root(self,*args,**kwargs):
        self.stop_requested=True
        self.stopped.set()
        return SimpleNamespace(stdout='{"verified":true}' if self.stop_verified else '{"verified":false}')

class TerminationTest(unittest.IsolatedAsyncioTestCase):
    def test_instruction_is_explicitly_positional(self):
        with tempfile.TemporaryDirectory() as d:
            command = DshAgent(logs_dir=Path(d)).agent_command('- task begins with a dash')
        self.assertEqual(command[-2:], ['--', '- task begins with a dash'])

    async def test_cancellation_waits_for_remote_stop(self):
        with tempfile.TemporaryDirectory() as d, patch.dict('os.environ',{'DEEPSEEK_API_KEY':'unused-test-key'}):
            agent=FakeAgent(logs_dir=Path(d));agent.stopped=asyncio.Event()
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(agent.run('probe',None,None),.02)
            self.assertTrue(agent.stop_requested)
            self.assertTrue(agent.execution_done)

    async def test_failed_stop_is_not_a_normal_agent_timeout(self):
        with tempfile.TemporaryDirectory() as d, patch.dict('os.environ',{'DEEPSEEK_API_KEY':'unused-test-key'}):
            agent=FakeAgent(logs_dir=Path(d));agent.stopped=asyncio.Event();agent.stop_verified=False
            with self.assertRaisesRegex(RuntimeError,'AgentTerminationError'):
                await asyncio.wait_for(agent.run('probe',None,None),.02)

if __name__=='__main__':unittest.main()
