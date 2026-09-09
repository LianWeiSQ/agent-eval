const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../agent_eval/static/app.js'), 'utf8');
const selectionCode = source.slice(source.indexOf('function reviewSupervisor('), source.indexOf('function openCorrection('));
const old = {id:'old', name:'Supervisor', version:'1.3.1', adapter_type:'llm-supervisor', config:{budget:{max_output_tokens:12000}}};
const latest = {...old, id:'new', version:'1.3.2', config:{budget:{max_output_tokens:32768}}};

function setup() {
  const calls = [], messages = [];
  const state = {snapshots:[latest, old], currentJob:{id:'job', trials:['trial-1','trial-2','trial-3','same-trial'].map(id=>({id})),config:{supervisor_snapshot_id:'old'}}};
  const context = vm.createContext({state, currentSnapshots:()=>[latest], esc:String,
    configLabel:s=>`${s.name} ${s.version}`, document:{querySelectorAll:()=>[]},
    toast:message=>messages.push(message), refreshAll:async()=>{}, showTrace:async()=>{},
    api:async(route, options)=>{calls.push({route, body:JSON.parse(options.body)});},
  });
  vm.runInContext(selectionCode, context);
  return {context, state, calls, messages};
}

(async()=>{
  const first = setup();
  assert.match(first.context.supervisorPicker(), /value="new" selected/);
  await first.context.superviseTrial('trial-1');
  assert.equal(first.calls[0].body.supervisor_snapshot_id, 'new');
  assert.equal(first.state.currentJob.config.supervisor_snapshot_id, 'old');
  first.context.chooseReviewSupervisor('old');
  await first.context.superviseTrial('trial-2');
  assert.equal(first.calls[1].body.supervisor_snapshot_id, 'old');

  const offline=setup();
  offline.state.currentJob.config.supervisor_snapshot_id=null;
  assert.equal(offline.context.supervisorPicker(), '');
  await offline.context.superviseTrial('trial-1');
  assert.equal(offline.calls[0].body.supervisor_snapshot_id, undefined);
  assert.equal(first.context.reviewSupervisor('unrelated-trial'), undefined);

  const concurrent = setup();
  let release;
  concurrent.context.api = async(route, options)=>{
    concurrent.calls.push({route, body:JSON.parse(options.body)});
    await new Promise(resolve=>{release=resolve;});
  };
  const pending=concurrent.context.superviseTrial('same-trial');
  await concurrent.context.superviseTrial('same-trial');
  assert.equal(concurrent.calls.length, 1);
  release();
  await pending;

  const failed = setup();
  failed.context.api=async()=>{throw Error('Supervisor 输出达到 token 上限；请使用关闭思考模式');};
  await failed.context.superviseTrial('trial-3');
  assert.match(failed.messages.at(-1), /思考模式无法关闭/);
  assert.ok(!failed.messages.at(-1).includes('请使用关闭思考模式'));
  console.log('Supervisor selection: default, explicit override, immutable job config, duplicate-click guard and error message passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
