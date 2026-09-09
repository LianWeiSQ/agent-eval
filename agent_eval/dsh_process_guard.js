// Only used inside one disposable Harbor task container, never on the host.
const fs = require('node:fs');
const {spawn} = require('node:child_process');
const path = require('node:path');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const [mode, directory, ...argv] = process.argv.slice(2);
if (process.platform !== 'linux' || !fs.existsSync('/.dockerenv') || !directory?.startsWith('/logs/agent/guard-')) {
  throw Error('Process guard requires a disposable Linux Docker task container');
}
fs.mkdirSync(directory, {recursive:true});
const file = name => path.join(directory, name);
const write = (name, value) => {fs.writeFileSync(file(name+'.tmp'), JSON.stringify(value)); fs.renameSync(file(name+'.tmp'), file(name));};
function processes() {
  const result = [];
  for (const name of fs.readdirSync('/proc')) {
    if (!/^\d+$/.test(name)) continue;
    try {
      const text = fs.readFileSync(`/proc/${name}/stat`, 'utf8');
      const fields = text.slice(text.lastIndexOf(')')+2).split(' ');
      result.push({pid:Number(name), ppid:Number(fields[1]), state:fields[0], key:`${name}:${fields[19]}`});
    } catch (e) {if (!['ENOENT','ESRCH'].includes(e.code)) throw e;}
  }
  return result;
}
async function stop() {
  // Fence a delayed launch before trying to read its baseline.
  fs.writeFileSync(file('stop-requested'), 'stop');
  let baseline;
  for (let i=0; i<100; i++) {
    if (fs.existsSync(file('baseline.json'))) {baseline=JSON.parse(fs.readFileSync(file('baseline.json'),'utf8'));break;}
    await sleep(50);
  }
  if (!baseline) throw Error('Agent launch baseline is unavailable; verification must not start');
  const old = new Set(baseline.processes.map(p=>p.key));
  const killed = new Set();
  function targets() {
    const all=processes(), byPid=new Map(all.map(p=>[p.pid,p]));
    const protectedPids=new Set([1]);
    let current=process.pid;
    while(current && !protectedPids.has(current)) {protectedPids.add(current);current=byPid.get(current)?.ppid;}
    return all.filter(p=>!protectedPids.has(p.pid) && p.state!=='Z' &&
      (!old.has(p.key) || p.key===baseline.controller));
  }
  let quiet=0;
  for(let i=0; i<100; i++) {
    const active=targets();
    if (!active.length) {if(++quiet>=3) break;await sleep(50);continue;}
    quiet=0;
    // Freeze before killing: TERM handlers must not keep editing scored artifacts.
    for(const p of active) {try {process.kill(p.pid,'SIGSTOP');} catch(e) {if(e.code!=='ESRCH')throw e;}}
    for(const p of active) {
      try {if(processes().some(x=>x.key===p.key))process.kill(p.pid,'SIGKILL');killed.add(p.key);}
      catch(e) {if(e.code!=='ESRCH')throw e;}
    }
    await sleep(50);
  }
  const remaining=targets();
  const result={schema_version:'1',status:'terminated',verified:remaining.length===0 && quiet>=3,
    finished_at:new Date().toISOString(),terminated_processes:[...killed],remaining_processes:remaining};
  write('termination.json',result);
  process.stdout.write(JSON.stringify(result)+'\n');
  if(!result.verified)process.exitCode=1;
}
async function launch() {
  if(!argv.length)throw Error('No agent command');
  const all=processes();
  write('baseline.json',{controller:all.find(p=>p.pid===process.pid).key,processes:all});
  if(fs.existsSync(file('stop-requested'))) {process.exitCode=125;return;}
  const child=spawn(argv[0],argv.slice(1),{env:process.env,stdio:'inherit'});
  child.on('error',error=>{write('exit.json',{status:'launch_failed',message:error.message,verified:false});process.exitCode=127;});
  child.on('exit',(code,signal)=>{
    write('exit.json',{schema_version:'1',status:'exited',verified:code===0,exit_code:code,signal,finished_at:new Date().toISOString()});
    process.exitCode=code??1;
  });
}
(mode==='stop'?stop():mode==='launch'?launch():Promise.reject(Error('Unknown guard operation')))
  .catch(error=>{process.stderr.write(error.message+'\n');process.exitCode=1;});
