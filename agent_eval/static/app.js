const state = {benchmarks:[], snapshots:[], jobs:[], reviews:[], supervisions:[], trace:null, traceNodes:{}, currentJob:null, jobTab:'results', refreshing:false, initialized:false};
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const labels = {pass:'通过',passed:'达到门槛',completed:'已完成',published:'已发布',draft:'草稿',archived:'已归档',running:'运行中',queued:'排队中',grading:'评分中',canceling:'取消中',canceled:'已取消',cancelled:'已取消',unresolved:'未通过',fail:'未通过',failed:'执行失败',hard_fail:'硬失败',agent_failed:'Agent 失败',grader_failed:'评分异常',infra_failed:'环境异常',review_pending:'待审核',review_required:'待审核',pending_review:'待审核',pending:'待处理',decision_recorded:'已审核',uncertain:'待确认',verified:'证据符合规则',violation:'规则异常',general_below_gate:'未达门槛',domain_below_gate:'未达门槛',platform_check_failed:'平台检查失败',conformance_failed:'合规检查失败',regression_blocked:'回归未通过',approve_retry:'已批准重试',reject_feedback:'已拒绝建议',accept_final:'接受当前结果',terminate:'已终止',submitted:'已提交'};
const status = value => `<span class="status ${esc(value)}">${esc(labels[value] || value || '尚无结果')}</span>`;
const dateText = value => value ? new Date(value).toLocaleString() : '—';
const pct = value => value == null ? '—' : `${(value * 100).toFixed(1)}%`;
const running = job => ['queued','running','grading','canceling'].includes(job.status);
const empty = text => `<div class="empty">${esc(text)}</div>`;
labels.invalid='无效评分';
Object.assign(labels,{agent_timeout:'执行超时',agent_exit_error:'执行异常退出',verifier_setup_failed:'测试准备失败',verifier_timeout:'官方评分超时',agent_termination_failed:'进程终止失败',agent_boundary_unverified:'时间边界未确认',agent_boundary_violation:'评分期间仍在执行',verifier_evidence_missing:'缺少测试报告',verifier_evidence_invalid:'测试报告不完整',harbor_runtime_error:'Harbor 运行异常',integration_changed:'快照代码已更新'});
function configLabel(snapshot) {
  const name=snapshot.name.replace('LLM Supervisor Agent','LLM 监督').replace('DeepSeek Harness Headless Agent','DSH Headless');
  return `${name} · ${snapshot.version}`;
}

async function api(path, options={}) {
  const response = await fetch('/api/v1'+path, {...options, headers:{'Content-Type':'application/json','X-Role':'project_admin',...(options.headers||{})}});
  if (!(response.headers.get('content-type') || '').includes('json')) throw Error('服务返回了无法识别的响应，请检查评测服务。');
  const body = await response.json();
  if (!response.ok || body.error) throw Error(body.error?.message || `HTTP ${response.status}`);
  return body.data;
}
const detailWorkers=new Map();
function cancelDetail(mode) {detailWorkers.get(mode)?.cancel();}
function detailApi(path,mode) {
  cancelDetail(mode);
  return new Promise((resolve,reject)=>{
    const worker=new Worker('/detail-worker.js');
    const finish=(error,value)=>{clearTimeout(timer);worker.terminate();if(detailWorkers.get(mode)?.worker===worker)detailWorkers.delete(mode);error?reject(error):resolve(value);};
    const timer=setTimeout(()=>finish(Error('详情加载超时，请重试。')),120000);
    detailWorkers.set(mode,{worker,cancel:()=>finish(Object.assign(Error('详情加载已取消'),{canceled:true}))});
    worker.onmessage=({data})=>{if(data.error)finish(Error(data.error));else{state.lastDetailProjection=data.projection;finish(null,data.data);}};
    worker.onerror=()=>finish(Error('详情加载失败，请刷新页面重试。'));
    worker.postMessage({path:'/api/v1'+path,mode});
  });
}
function toast(message, error=false) {
  $('toast').textContent=message; $('toast').className='toast'+(error?' error':''); $('toast').style.display='block';
  clearTimeout(toast.timer); toast.timer=setTimeout(()=>$('toast').style.display='none',4500);
}
function openDialog(id) { if (!$(id).open) $(id).showModal(); }
function closeDialog(id) {
  $(id).close();
  if(id==='jobDetailDialog'){jobRequest++;cancelDetail('job');state.currentJob=null;}
  if(id==='traceDialog'){traceRequest++;cancelDetail('trace');state.trace=null;state.traceNodes={};}
  if(id==='detailDialog'){$('detailBody').replaceChildren();cancelDetail('trial');if(detailDownloadUrl){URL.revokeObjectURL(detailDownloadUrl);detailDownloadUrl=null;}}
}
for(const id of ['jobDetailDialog','traceDialog','detailDialog'])$(id).addEventListener('cancel',event=>{event.preventDefault();closeDialog(id);});
function navigate(view) {
  document.querySelectorAll('.nav button').forEach(button=>button.classList.toggle('active',button.dataset.view===view));
  document.querySelectorAll('.view').forEach(section=>section.classList.toggle('active',section.id===view));
}
document.querySelectorAll('.nav button').forEach(button=>button.onclick=()=>navigate(button.dataset.view));

function latest(items, key) {
  const result=new Map();
  for(const item of [...items].sort((a,b)=>String(b.created_at||'').localeCompare(String(a.created_at||'')) || String(b.version||'').localeCompare(String(a.version||''),undefined,{numeric:true}))) {
    if(!result.has(key(item))) result.set(key(item),item);
  }
  return [...result.values()];
}
function benchmarkFamily(benchmark) {
  const text=`${benchmark.name} ${benchmark.package?.manifest?.id || ''}`.toLowerCase();
  if(text.includes('terminal-bench')) return 'terminal';
  if(text.includes('mmlu')) return 'mmlu';
  return 'other';
}
function currentBenchmarks() {
  return latest(state.benchmarks.filter(b=>b.status!=='archived' && benchmarkFamily(b)!=='other'), b=>b.package?.manifest?.id || b.name)
    .sort((a,b)=>(benchmarkFamily(a)==='terminal'?0:1)-(benchmarkFamily(b)==='terminal'?0:1));
}
function currentSnapshots() {
  return latest(state.snapshots.filter(s=>['terminal-bench-harbor','dsh-headless','llm-supervisor'].includes(s.adapter_type)),s=>`${s.adapter_type}:${s.name}`);
}
function benchmarkName(id) {return state.benchmarks.find(b=>b.id===id)?.name || id;}
function metric(label,value,action='') {return `<div class="card metric"><span>${esc(label)}</span>${action?`<button onclick="${action}"><b>${esc(value)}</b></button>`:`<b>${esc(value)}</b>`}</div>`;}

async function refreshAll() {
  if(state.refreshing) return;
  state.refreshing=true;
  try {
    const [benchmarks,snapshots,jobs,reviews,supervisions]=await Promise.all(['/benchmarks','/agent-snapshots','/jobs','/reviews','/supervisions'].map(path=>api(path)));
    Object.assign(state,{benchmarks,snapshots,jobs,reviews,supervisions});
    $('connectionError').hidden=true;
    renderJobs();renderBenchmarks();renderSnapshots();renderReviews();
    if(!$('jobDialog').open) populateForms();
    if(state.currentJob && $('jobDetailDialog').open && !detailWorkers.has('job')) {
      const updated=state.jobs.find(j=>j.id===state.currentJob.id);
      if(updated?.updated_at!==state.currentJob.updated_at) await loadJob(state.currentJob.id);
    }
  } catch(error) {
    $('connectionError').hidden=false; $('connectionError').textContent='数据未能刷新：'+error.message;
    toast(error.message,true);
  } finally {state.refreshing=false;}
}
function renderJobs() {
  const pending=state.supervisions.filter(s=>s.status==='pending_review').length;
  $('metrics').innerHTML=metric('运行中',state.jobs.filter(running).length)+metric('待纠错审核',pending,"navigate('reviews')")+metric('已结束批次',state.jobs.filter(j=>j.finished_at).length)+metric('全部评测任务',state.jobs.length);
  const query=$('jobSearch').value.trim().toLowerCase();
  const items=state.jobs.filter(j=>`${j.name} ${j.id} ${benchmarkName(j.benchmark_id)}`.toLowerCase().includes(query));
  $('jobCount').textContent=`${items.length} 项`;
  $('jobsTable').innerHTML=items.length?`<table><thead><tr><th>任务 / Benchmark</th><th>执行进度</th><th>批次结论</th><th>创建时间</th><th>操作</th></tr></thead><tbody>${items.map(j=>{
    const progress=j.progress_total?Math.round(j.progress_completed/j.progress_total*100):0;
    return `<tr><td class="task-name"><b>${esc(j.name)}</b><small>${esc(benchmarkName(j.benchmark_id))}</small><small class="muted-id">${esc(j.id)}</small></td><td>${status(j.status)}<div class="progress"><i style="width:${progress}%"></i></div><span class="subtle">${j.progress_completed}/${j.progress_total}</span></td><td>${status(j.conclusion)}</td><td>${esc(dateText(j.created_at))}</td><td><div class="actions"><button class="small" onclick="jobDetail('${j.id}')">查看任务</button>${j.status==='draft'?`<button class="small secondary" onclick="jobAction('${j.id}','start')">启动</button>`:''}${running(j)?`<button class="small danger" onclick="jobAction('${j.id}','cancel')">取消运行</button>`:''}</div></td></tr>`;
  }).join('')}</tbody></table>`:empty(query?'没有匹配的任务':'还没有评测任务，点击“新建评测”开始。');
}
function benchmarkTable(items) {
  if(!items.length) return empty('没有记录');
  return `<table><thead><tr><th>名称</th><th>版本</th><th>题目数</th><th>状态</th><th>操作</th></tr></thead><tbody>${items.map(b=>`<tr><td><b>${esc(b.name)}</b></td><td>${esc(b.version)}</td><td>${b.package.tasks.length}</td><td>${status(b.status)}</td><td><div class="actions"><button class="small secondary" onclick="benchmarkDetail('${b.id}')">查看用例</button>${b.status==='draft'?`<button class="small" onclick="benchmarkAction('${b.id}','publish')">发布</button>`:''}</div></td></tr>`).join('')}</tbody></table>`;
}
function renderBenchmarks() {
  const current=currentBenchmarks(),ids=new Set(current.map(b=>b.id)),history=state.benchmarks.filter(b=>!ids.has(b.id));
  $('benchmarkTable').innerHTML=benchmarkTable(current);$('benchmarkHistory').innerHTML=benchmarkTable(history);$('benchmarkHistoryCount').textContent=`（${history.length}）`;
}
function benchmarkDetail(id) {
  const b=state.benchmarks.find(b=>b.id===id); if(!b) return;
  $('detailTitle').textContent=b.name;
  $('detailBody').innerHTML=`<p class="muted">版本 ${esc(b.version)} · ${b.package.tasks.length} 道题</p>${b.package.tasks.map(t=>`<details class="fold"><summary>${esc(t.id)} · ${esc(t.title || t.name || t.suite || '任务要求')}</summary><div class="panel-body"><div class="code">${esc(t.input?.instruction || t.instruction || JSON.stringify(t.input,null,2))}</div></div></details>`).join('')}<details class="fold"><summary>版本与评分配置</summary><div class="panel-body code">${esc(JSON.stringify(b.package,null,2))}</div></details>`;
  openDialog('detailDialog');
}
function snapshotTable(items) {
  if(!items.length) return empty('没有配置记录');
  return `<table><thead><tr><th>配置名称</th><th>职责</th><th>版本</th><th>操作</th></tr></thead><tbody>${items.map(s=>`<tr><td><b>${esc(s.name)}</b></td><td>${s.adapter_type==='llm-supervisor'?'监督 Agent':'执行 Agent'}</td><td>${esc(s.version)}</td><td><div class="actions"><button class="small secondary" onclick="snapshotDetail('${s.id}')">配置详情</button><button class="small secondary" onclick="health('${s.id}')">连接检查</button></div></td></tr>`).join('')}</tbody></table>`;
}
function renderSnapshots() {
  const current=currentSnapshots(),ids=new Set(current.map(s=>s.id)),history=state.snapshots.filter(s=>!ids.has(s.id));
  $('snapshotTable').innerHTML=snapshotTable(current);$('snapshotHistory').innerHTML=snapshotTable(history);$('snapshotHistoryCount').textContent=`（${history.length}）`;
  $('adapterStatus').innerHTML=snapshotTable(state.snapshots);
}
function snapshotDetail(id) {
  const snapshot=state.snapshots.find(s=>s.id===id);if(snapshot)jsonDetail('Agent 配置详情',encodeURIComponent(JSON.stringify(snapshot)));
}
function reviewCard(s,pending) {
  return `<article class="review-card"><div class="toolbar"><h3>${esc(s.trial_id)}</h3>${status(pending?s.verdict:s.status)}</div><p>${esc(s.reason || '暂无判断理由')}</p><details><summary>监督建议与证据</summary><p>${esc(s.suggestion || '无修改建议')}</p><div class="code">${esc(JSON.stringify(s.evidence_refs||[],null,2))}</div></details><div class="actions"><button class="small secondary" onclick="showTrace('${s.trial_id}')">查看执行轨迹</button>${pending?`<button class="small" onclick="openCorrection('${s.id}')">审核建议</button>`:`<button class="small secondary" onclick="supervisionDetail('${s.id}')">记录详情</button>`}</div></article>`;
}
function renderReviews() {
  const pending=state.supervisions.filter(s=>s.status==='pending_review'),history=state.supervisions.filter(s=>s.status!=='pending_review');
  $('correctionCount').textContent=`${pending.length} 项`;
  $('correctionQueue').innerHTML=pending.map(s=>reviewCard(s,true)).join('')||empty('暂无待审核建议。可在任务详情中对失败样本运行监督。');
  $('supervisionHistory').innerHTML=history.map(s=>reviewCard(s,false)).join('')||empty('暂无监督历史');
  $('supervisionHistoryCount').textContent=`（${history.length}）`;
  $('legacyReviewCount').textContent=`（待处理 ${state.reviews.filter(r=>r.status==='pending').length} / 全部 ${state.reviews.length}）`;
  $('reviewTable').innerHTML=state.reviews.length?`<table><thead><tr><th>执行记录</th><th>状态</th><th>评分</th><th>操作</th></tr></thead><tbody>${state.reviews.map(r=>`<tr><td>${esc(r.trial_id)}</td><td>${status(r.status)}</td><td>${r.score??'—'}</td><td>${r.status==='pending'?`<button class="small secondary" onclick="openReview('${r.id}')">提交人工评分</button>`:`<button class="small secondary" onclick="legacyReviewDetail('${r.id}')">查看记录</button>`}</td></tr>`).join('')}</tbody></table>`:empty('暂无人工打分记录');
}
function supervisionDetail(id) {const s=state.supervisions.find(s=>s.id===id);if(s)jsonDetail('监督记录',encodeURIComponent(JSON.stringify(s)));}
function legacyReviewDetail(id) {const r=state.reviews.find(r=>r.id===id);if(r)jsonDetail('人工打分记录',encodeURIComponent(JSON.stringify(r)));}

function fillSelect(select,items,current,label) {
  select.innerHTML=items.map(item=>`<option value="${esc(item.id)}">${esc(label(item))}</option>`).join('');
  if(items.some(item=>item.id===current)) select.value=current;
}
function populateForms(resetAgent=false) {
  const history=$('includeHistory').checked;
  const benchmarks=(history?state.benchmarks:currentBenchmarks()).filter(b=>b.status==='published');
  fillSelect($('jobBenchmark'),benchmarks,$('jobBenchmark').value,b=>`${b.name} · ${b.version}`);
  populateExecutors(resetAgent);
  const supervisors=(history?state.snapshots:currentSnapshots()).filter(s=>s.adapter_type==='llm-supervisor');
  const previous=$('jobSupervisor').value;
  fillSelect($('jobSupervisor'),supervisors,previous,configLabel);
  $('jobSupervisor').insertAdjacentHTML('beforeend','<option value="">离线规则检查（调试用）</option>');
  if(state.initialized && previous==='') $('jobSupervisor').value='';
  state.initialized=true;
}
function populateExecutors(reset=false) {
  const select=$('jobSnapshots'),chosen=reset?[]:[...select.selectedOptions].map(o=>o.value),history=$('includeHistory').checked;
  const benchmark=state.benchmarks.find(b=>b.id===$('jobBenchmark').value),family=benchmark?benchmarkFamily(benchmark):'other';
  const preferred=family==='terminal'?'terminal-bench-harbor':family==='mmlu'?'dsh-headless':null;
  let items=(history?state.snapshots:currentSnapshots()).filter(s=>s.adapter_type!=='llm-supervisor');
  if(!history && preferred) {
    const benchmarkId=benchmark.package?.manifest?.id;
    items=items.filter(s=>s.adapter_type===preferred && (!s.config?.benchmark_id || s.config.benchmark_id===benchmarkId));
    const dedicated=items.filter(s=>s.config?.benchmark_id===benchmarkId);
    if(dedicated.length) items=dedicated;
  }
  items.sort((a,b)=>Number(b.adapter_type===preferred)-Number(a.adapter_type===preferred));
  fillSelect(select,items,'',configLabel);
  const keep=chosen.filter(id=>items.some(s=>s.id===id));
  if(select.multiple) [...select.options].forEach((o,i)=>o.selected=keep.length?keep.includes(o.value):i===0);
  else if(keep.length) select.value=keep[0];
}
function openJobDialog() {
  populateForms();
  if(!$('jobBenchmark').options.length) {toast('请先导入并发布 Benchmark，或在高级设置中选择历史记录。',true);}
  openDialog('jobDialog');updateJobCompatibility();
}
let compatibilityRequest=0;
async function updateJobCompatibility() {
  const request=++compatibilityRequest,form=new FormData($('jobForm')),ids=[...$('jobSnapshots').selectedOptions].map(o=>o.value),message=$('jobCompatibility');
  $('jobSubmit').disabled=true;message.style.color='var(--muted)';
  if(!form.get('benchmark_id') || !ids.length) {message.textContent='请选择 Benchmark 和执行 Agent。';return false;}
  message.textContent='正在检查运行配置…';
  try {
    const estimate=await api('/jobs/estimate',{method:'POST',body:JSON.stringify({benchmark_id:form.get('benchmark_id'),agent_snapshot_ids:ids,execution:{repetitions:+form.get('repetitions'),max_concurrency:+form.get('max_concurrency')}})});
    if(request!==compatibilityRequest) return false;
    const benchmark=state.benchmarks.find(item=>item.id===form.get('benchmark_id'));
    if(benchmark?.package?.manifest?.id==='terminal-bench-2.1-full' && estimate.task_count>1){
      message.textContent=`这会为完整 ${estimate.task_count} 题分别启动隔离容器。请关闭本窗口，使用“Agent / Harbor 评测”指定单题；全量运行需在那里显式确认。`;
      message.style.color='var(--bad)';return false;
    }
    const compatible=estimate.compatibility.compatible;
    if(compatible){
      const terminalIds=ids.filter(id=>state.snapshots.find(s=>s.id===id)?.adapter_type==='terminal-bench-harbor');
      if(terminalIds.length){
        message.textContent='正在检查 Docker 运行环境…';
        const checks=await Promise.all(terminalIds.map(id=>api(`/agent-snapshots/${id}/health`)));
        if(request!==compatibilityRequest)return false;
        const failed=checks.find(check=>!check.ok);
        if(failed){message.textContent=failed.message||'运行环境尚未就绪，请检查 Docker。';message.style.color='var(--bad)';return false;}
      }
    }
    message.textContent=compatible?`配置可用：${estimate.task_count} 道题，共 ${estimate.trial_count} 次执行。`:'当前 Agent 不支持所选任务。请更换执行配置；可在“Agent 配置”中查看能力详情。';
    message.style.color=compatible?'var(--good)':'var(--bad)';$('jobSubmit').disabled=!compatible;return compatible;
  }catch(error){if(request===compatibilityRequest){message.textContent='配置检查失败：'+error.message;message.style.color='var(--bad)';}return false;}
}
$('jobBenchmark').onchange=()=>{populateExecutors(true);updateJobCompatibility();};
$('jobSnapshots').onchange=updateJobCompatibility;
$('includeHistory').onchange=()=>{populateForms();updateJobCompatibility();};
$('multiAgent').onchange=()=>{const select=$('jobSnapshots');select.multiple=$('multiAgent').checked;select.size=select.multiple?4:1;$('multiHint').hidden=!select.multiple;populateExecutors();updateJobCompatibility();};
document.querySelectorAll('#jobForm [name=repetitions],#jobForm [name=max_concurrency]').forEach(input=>input.onchange=updateJobCompatibility);
$('jobForm').onsubmit=async event=>{
  event.preventDefault();
  if($('jobForm').dataset.submitting) return;
  $('jobForm').dataset.submitting='true';
  try {
    if(!await updateJobCompatibility()) return;
    $('jobSubmit').disabled=true;
    const form=new FormData(event.target),ids=[...$('jobSnapshots').selectedOptions].map(o=>o.value);
    const job=await api('/jobs',{method:'POST',body:JSON.stringify({name:form.get('name'),benchmark_id:form.get('benchmark_id'),agent_snapshot_ids:ids,supervisor_snapshot_id:form.get('supervisor_snapshot_id')||null,compatibility_mode:'strict',execution:{repetitions:+form.get('repetitions'),max_concurrency:+form.get('max_concurrency')},gate:{min_pass_rate:+form.get('min_pass_rate')},report_policy:{human_sample_rate:+form.get('human_sample_rate')}})});
    await api(`/jobs/${job.id}/start`,{method:'POST',body:'{}'});closeDialog('jobDialog');toast('评测已创建并启动');await refreshAll();await jobDetail(job.id);
  }catch(error){toast(error.message,true);await refreshAll();}
  finally{delete $('jobForm').dataset.submitting;if($('jobDialog').open)await updateJobCompatibility();}
};

let jobRequest=0;
async function jobDetail(id,tab='results') {
  state.jobTab=tab;openDialog('jobDetailDialog');$('jobDetailTitle').textContent='正在加载任务…';$('jobResults').innerHTML=empty('正在加载…');$('reportContent').innerHTML='';
  await loadJob(id);
}
async function loadJob(id) {
  const request=++jobRequest;
  try {
    const job=await detailApi(`/jobs/${id}?view=summary`,'job');if(request!==jobRequest || !$('jobDetailDialog').open)return;
    state.currentJob=job;$('jobDetailTitle').textContent=job.name;$('jobDetailSubtitle').textContent=`${benchmarkName(job.benchmark_id)} · ${job.id}`;
    renderJobResults(job);await switchJobTab(state.jobTab);
  }catch(error){if(error.canceled || request!==jobRequest)return;$('jobDetailTitle').textContent='任务加载失败';$('jobResults').innerHTML=empty(error.message);toast(error.message,true);}
}
function renderJobResults(job) {
  const trials=job.trials||[],primary=trials.filter(t=>t.attempt===1),finished=primary.filter(t=>t.outcome),passed=primary.filter(t=>t.outcome==='pass').length,ids=new Set(trials.map(t=>t.id));
  const supers=state.supervisions.filter(s=>ids.has(s.trial_id)),retries=trials.filter(t=>t.attempt>1).length;
  $('jobResults').innerHTML=`<div class="grid">${metric('执行进度',`${job.progress_completed}/${job.progress_total}`)}${metric('首次通过',`${passed}/${primary.length}`)}${metric('已有监督记录',`${supers.length}`)}${metric('纠错重试',retries)}</div><p>${status(job.status)} ${status(job.conclusion)}</p><p class="hint">首次结果与重试结果分别保留。${job.conclusion==='passed'?`本批次达到 ${(job.config?.gate?.min_pass_rate??0.8)*100}% 的通过门槛，不表示所有题目都通过。`:''}${finished.length<primary.length?' 尚有任务未产生首次结果。':''}</p><div class="notice">操作顺序：逐题结果 → 执行轨迹 → 监督诊断 → 人工审核 → 重试结果</div>${supervisorPicker()}<div class="table-wrap"><table><thead><tr><th>题目</th><th>尝试</th><th>结果 / 分数</th><th>监督状态</th><th>操作</th></tr></thead><tbody>${trials.map(t=>{
    const supervision=supers.find(s=>s.trial_id===t.id),pending=supervision?.status==='pending_review',canSupervise=['pass','unresolved','agent_failed','hard_fail'].includes(t.outcome);
    return `<tr><td>${esc(t.task_id)}<div class="subtle">重复 ${t.repetition}</div></td><td>${t.attempt===1?'首次':`重试 ${t.attempt-1}`}</td><td>${status(t.outcome||t.execution_status)}<div class="subtle">${t.score??'—'} 分</div>${[...new Set([t.error?.execution_issue,t.error?.verification_issue,t.failure_type].filter(Boolean))].map(issue=>`<div class="subtle">${esc(labels[issue]||issue)}</div>`).join('')}</td><td>${supervision?status(supervision.status==='pending_review'?'pending_review':supervision.verdict):'<span class="muted">尚未监督</span>'}</td><td><div class="actions"><button class="small secondary" onclick="showTrace('${t.id}')">执行轨迹</button>${!supervision&&canSupervise?`<button class="small" onclick="superviseTrial('${t.id}')">运行监督</button>`:''}${pending?`<button class="small" onclick="openCorrection('${supervision.id}')">审核建议</button>`:''}<button class="small secondary" onclick="trialDetail('${t.id}')">原始证据</button></div></td></tr>`;
  }).join('')}</tbody></table></div><details class="fold"><summary>本次运行配置</summary><div class="panel-body code">${esc(JSON.stringify(job.config,null,2))}</div></details>`;
}
async function switchJobTab(tab) {
  state.jobTab=tab;$('resultsTab').setAttribute('aria-selected',tab==='results');$('reportTab').setAttribute('aria-selected',tab==='report');$('jobResults').hidden=tab!=='results';$('reportContent').hidden=tab!=='report';
  if(tab==='report' && state.currentJob)await loadReport(state.currentJob.id);
}
let reportRequest=0;
async function loadReport(id) {
  const request=++reportRequest,el=$('reportContent');el.innerHTML=empty('正在加载报告…');
  try {
    const r=await api(`/jobs/${id}/report`);if(request!==reportRequest || state.currentJob?.id!==id)return;
    const s=r.summary,c=r.correction_summary||{},cost=c.execution_cost||{},st=c.supervisor_tokens||{},hasSupervision=(c.supervised_tasks||0)>0;
    el.innerHTML=`<div class="grid">${metric('首次通过 / 全部题目',`${s.counts?.pass??0}/${s.total_trials}`)}${metric('修复率',hasSupervision?pct(c.fix_rate):'尚未监督')}${metric('执行工具调用',cost.tool_calls??'—')}${metric('监督任务',c.supervised_tasks??0)}</div><p class="hint">全体题目已通过占比 ${pct(s.total_trials?(s.counts?.pass??0)/s.total_trials:0)}；有效评分 ${s.valid_trials}/${s.total_trials}，有效样本通过率 ${pct(s.pass_rate)}。无效或未完成题目尚不能判断能力。</p><div class="notice">${esc(r.interpretation||'首次成绩与辅助纠错成绩分别记录。')}${r.conclusion==='passed'?' 达到批次通过门槛不代表全部题目通过。':''}</div><div class="toolbar"><h3>执行 Agent 结果</h3><div class="actions"><a class="button small" href="/api/v1/jobs/${id}/export/markdown">导出 Markdown</a><a class="button small secondary" href="/api/v1/jobs/${id}/export/json">JSON</a><a class="button small secondary" href="/api/v1/jobs/${id}/export/csv">CSV</a></div></div><div class="table-wrap"><table><thead><tr><th>Agent</th><th>有效样本数</th><th>有效样本通过率</th><th>均分</th><th>分数波动</th></tr></thead><tbody>${r.agent_groups.map(g=>`<tr><td>${esc(g.agent)}</td><td>${g.sample_size}</td><td>${pct(g.pass_rate)}</td><td>${g.mean_score??'—'}</td><td>${g.score_stddev??'—'}</td></tr>`).join('')}</tbody></table></div><details class="fold"><summary>监督与纠错统计</summary><div class="panel-body"><p class="hint">监督可见官方评分，以下指标不代表独立盲评准确率。</p><div class="table-wrap"><table><tbody><tr><th>已有监督 / 纠错任务</th><td>${c.supervised_tasks??0} / ${c.corrected_tasks??0}</td></tr><tr><th>修复成功 / 平均分数变化</th><td>${c.fixed_tasks??0} / ${c.mean_score_delta??'—'}</td></tr><tr><th>监督检出率 / 误报率</th><td>${hasSupervision?pct(c.supervisor_detection_rate):'尚未监督'} / ${hasSupervision?pct(c.supervisor_false_positive_rate):'尚未监督'}</td></tr><tr><th>人工决定</th><td>${esc(JSON.stringify(c.human_decisions||{}))}</td></tr></tbody></table></div></div></details><details class="fold"><summary>用量、耗时与失败分布</summary><div class="panel-body"><p>执行 Token（输入 / 输出）：${cost.input_tokens??'未知'} / ${cost.output_tokens??'未知'}；监督 Token：${hasSupervision?`${st.input??'未知'} / ${st.output??'未知'}`:'尚未监督'}。</p><p class="hint">用量字段不保证覆盖完整计费口径，缺失数据不能当作零成本。</p><p>执行 / 监督 P95 耗时：${s.latency_ms?.p95??'—'} / ${c.supervisor_latency_ms?.p95??'—'} ms</p><div class="code">${esc(JSON.stringify(r.failure_types,null,2))}</div></div></details>`;
  }catch(error){if(request===reportRequest)el.innerHTML=empty('报告暂不可用：'+error.message);}
}

let traceRequest=0;
async function showTrace(id,view='summary',layout='journey') {
  const request=++traceRequest;
  try {
    if($('detailDialog').open)closeDialog('detailDialog');
    $('traceTitle').textContent='正在加载执行轨迹…';$('traceBody').innerHTML=empty('正在加载，可随时关闭。');openDialog('traceDialog');
    const trace=await detailApi(`/trials/${id}/trace?view=${view}`,'trace');if(request!==traceRequest || !$('traceDialog').open)return;
    state.trace=trace;state.traceLimit=100;state.traceLayout=layout;state.traceNodes=Object.fromEntries(trace.nodes.map(n=>[n.id,n]));
    $('traceTitle').textContent='执行轨迹';$('traceSubtitle').textContent=`${trace.root_trial_id} · ${view==='summary'?'精简概览':'完整证据'}`;
    const actions=trace.actions||{};
    $('traceBody').innerHTML=`<div class="trace-toolbar"><div class="trace-modes"><button class="small ${view==='detail'?'':'secondary'}" onclick="showTrace('${id}','detail','${layout}')">完整证据</button><button class="small ${view==='summary'?'':'secondary'}" onclick="showTrace('${id}','summary','${layout}')">精简概览</button><button class="small ${layout==='journey'?'':'secondary'}" onclick="showTrace('${id}','${view}','journey')">流程图</button><button class="small ${layout==='raw'?'':'secondary'}" onclick="showTrace('${id}','${view}','raw')">原始列表</button></div><div class="actions">${actions.can_supervise?`<button class="small" onclick="superviseTrial('${actions.supervise_trial_id}')">运行监督</button>`:''}${actions.pending_supervision_id?`<button class="small" onclick="openCorrection('${actions.pending_supervision_id}')">审核建议</button>`:''}</div></div>${actions.can_supervise?supervisorPicker(actions.supervise_trial_id):''}<div id="traceRendered">${renderTracePage(trace,layout)}</div>`;
    openDialog('traceDialog');
  }catch(error){if(!error.canceled && request===traceRequest){$('traceBody').innerHTML=empty(error.message);toast(error.message,true);}}
}
function reviewSupervisor(trialId) {
  const job=state.currentJob;
  if(!job || (trialId && !(job.trials||[]).some(t=>t.id===trialId))) return undefined;
  const available=state.snapshots.filter(s=>s.adapter_type==='llm-supervisor');
  const configured=available.find(s=>s.id===job.config?.supervisor_snapshot_id);
  if(!configured) return undefined;
  return available.find(s=>s.id===state.supervisorChoices?.[job.id]) || currentSnapshots().find(s=>s.adapter_type===configured.adapter_type && s.name===configured.name) || configured;
}
function chooseReviewSupervisor(id) {
  if(!state.currentJob) return;
  state.supervisorChoices={...state.supervisorChoices,[state.currentJob.id]:id};
  document.querySelectorAll('.review-supervisor-select').forEach(select=>select.value=id);
}
function supervisorPicker(trialId) {
  const selected=reviewSupervisor(trialId);
  if(!selected) return '';
  const available=state.snapshots.filter(s=>s.adapter_type==='llm-supervisor');
  return `<label class="review-supervisor-picker">本次监督配置<select class="review-supervisor-select" onchange="chooseReviewSupervisor(this.value)">${available.map(s=>`<option value="${esc(s.id)}" ${s.id===selected.id?'selected':''}>${esc(configLabel(s))} · 输出上限 ${esc(s.config?.budget?.max_output_tokens ?? 4000)}</option>`).join('')}</select><span class="subtle">默认使用最新配置；已完成的监督记录保留原版本。</span></label>`;
}
const supervising=new Set();
async function superviseTrial(id) {
  if(supervising.has(id)) return;supervising.add(id);
  const selected=reviewSupervisor(id);
  const buttons=[...document.querySelectorAll(`button[onclick*="superviseTrial('${id}')"]`)];buttons.forEach(b=>b.disabled=true);toast(`监督${selected?' '+selected.version:''}正在检查执行证据，请稍候…`);
  try {await api(`/trials/${id}/supervise`,{method:'POST',body:JSON.stringify(selected?{supervisor_snapshot_id:selected.id}:{})});toast('监督检查已完成');await refreshAll();await showTrace(id,state.trace?.view||'summary');}
  catch(error){const message=error.message.includes('Supervisor 输出达到 token 上限')?'监督生成达到了输出上限，尚未形成完整结论。请核对本次监督配置的输出预算；GLM-5.3 的思考模式无法关闭。':error.message;toast('监督检查失败：'+message,true);}
  finally{supervising.delete(id);buttons.forEach(b=>b.disabled=false);}
}
function openCorrection(id) {
  const supervision=state.supervisions.find(s=>s.id===id)||Object.values(state.traceNodes).find(n=>n.type==='supervisor'&&n.detail?.id===id)?.detail;
  if(!supervision){toast('未找到监督记录，请刷新后重试。',true);return;}
  const form=$('correctionForm');form.reset();form.elements.supervision_id.value=id;form.elements.approved_feedback.value=supervision.suggestion||'';
  $('correctionContext').textContent=`执行记录：${supervision.trial_id || ''}\n监督判断：${labels[supervision.verdict]||supervision.verdict||''}\n${supervision.reason||''}`;
  openDialog('correctionDialog');
}
$('correctionForm').onsubmit=async event=>{
  event.preventDefault();const form=new FormData(event.target),button=event.target.querySelector('[type=submit]');button.disabled=true;
  try {
    const result=await api(`/supervisions/${form.get('supervision_id')}/decide`,{method:'POST',body:JSON.stringify({decision:form.get('decision'),reason:form.get('reason'),approved_feedback:form.get('approved_feedback')})});
    closeDialog('correctionDialog');toast(result.child_trial_id?'已批准，正在创建新的执行尝试':'审核决定已保存');
    await refreshAll();if($('traceDialog').open && state.trace)await showTrace(state.trace.root_trial_id,state.trace.view||'detail');
  }catch(error){toast(error.message,true);}finally{button.disabled=false;}
};
async function jobAction(id,action) {try{await api(`/jobs/${id}/${action}`,{method:'POST',body:'{}'});toast(action==='cancel'?'已请求取消运行':'任务已启动');await refreshAll();}catch(error){toast(error.message,true);}}
async function benchmarkAction(id,action) {try{await api(`/benchmarks/${id}/${action}`,{method:'POST',body:'{}'});toast('Benchmark 状态已更新');await refreshAll();}catch(error){toast(error.message,true);}}
async function health(id) {try{const result=await api(`/agent-snapshots/${id}/health`);jsonDetail('Agent 连接检查',encodeURIComponent(JSON.stringify(result)));}catch(error){toast(error.message,true);}}
function harborStatusText(value) {
  const components=value?.components||{}, lines=[value?.ready?'环境已就绪，可直接启动评测。':'环境尚未完全就绪，首次启动会自动准备。'];
  for(const [label,key] of [['Harbor','harbor'],['Docker Linux','docker'],['Terminal-Bench','terminal_bench_source'],['OpenAI 凭据','openai_credential'],['DSH 运行包','dsh_runtime'],['DeepSeek 凭据','deepseek_credential'],['默认模型注册','registration']]) {
    const item=components[key]||{},ready=key==='docker'?(item.linux&&item.compose):item.available;
    lines.push(`${ready?'✓':'○'} ${label}${item.version?' '+item.version:''}${item.message?'：'+item.message:''}`);
  }
  return lines.join('\n');
}
function updateHarborReasoning() {
  const model=state.harborStatus?.model_profiles?.find(item=>item.id===$('harborModel').value);
  const select=$('harborReasoning'),previous=select.value;
  select.replaceChildren(...(model?.reasoning_efforts||[]).map(value=>new Option(value,value)));
  select.disabled=!model?.reasoning_efforts?.length;
  if(model?.reasoning_efforts?.includes(previous))select.value=previous;
  else if(model?.default_reasoning_effort)select.value=model.default_reasoning_effort;
}
function populateHarborModels(value) {
  const select=$('harborModel'),previous=select.value;
  select.replaceChildren(...(value?.model_profiles||[]).map(item=>{
    const option=new Option(item.label+(item.available?'':'（凭据未就绪）'),item.id);
    return option;
  }));
  select.value=(value?.model_profiles||[]).some(item=>item.id===previous)?previous:value.default_model_profile;
  updateHarborReasoning();
}
async function loadHarborStatus() {
  $('harborStatus').textContent='正在检查 Harbor 环境…';
  try {const value=await api('/terminal-bench/status');state.harborStatus=value;populateHarborModels(value);$('harborStatus').textContent=harborStatusText(value);}
  catch(error){$('harborStatus').textContent='环境检查失败：'+error.message;}
}
function openHarborDialog(){openDialog('harborDialog');loadHarborStatus();}
async function prepareHarbor(){
  const button=$('harborPrepare'),model=$('harborModel').value,effort=$('harborReasoning').disabled?null:$('harborReasoning').value;
  button.disabled=true;$('harborStatus').textContent='正在准备 Terminal-Bench 和所选 Agent 快照，首次执行可能需要几分钟…';
  try{const result=await api('/terminal-bench/setup',{method:'POST',body:JSON.stringify({model_profile:model,reasoning_effort:effort})});toast(`已准备 ${result.model}：${result.task_count} 题`);await refreshAll();await loadHarborStatus();}
  catch(error){toast('Harbor 准备失败：'+error.message,true);await loadHarborStatus();}
  finally{button.disabled=false;}
}
$('harborForm').onsubmit=async event=>{
  event.preventDefault();const form=new FormData(event.target),button=$('harborSubmit'),all=form.get('all_tasks')==='on';
  const names=String(form.get('task_names')||'').split(',').map(value=>value.trim()).filter(Boolean);
  if(!all&&!names.length){toast('请至少填写一个 Terminal-Bench 任务名。',true);return;}
  const model=form.get('model_profile'),effort=$('harborReasoning').disabled?null:form.get('reasoning_effort');
  button.disabled=true;$('harborPrepare').disabled=true;$('harborStatus').textContent='正在准备环境并启动所选模型评测…';
  try{
    const result=await api('/terminal-bench/evaluations',{method:'POST',body:JSON.stringify({model_profile:model,reasoning_effort:effort,task_names:names,repetitions:+form.get('repetitions'),max_concurrency:+form.get('max_concurrency'),all_tasks:all,confirm_full_run:all})});
    closeDialog('harborDialog');toast(`${result.registration.model} 评测已启动`);await refreshAll();await jobDetail(result.job.id);
  }catch(error){toast('评测启动失败：'+error.message,true);await loadHarborStatus();}
  finally{button.disabled=false;$('harborPrepare').disabled=false;}
};
function openSettings(){openDialog('settingsDialog');}
$('benchmarkForm').onsubmit=async event=>{
  event.preventDefault();const form=new FormData(event.target);
  try{await api('/benchmarks/import',{method:'POST',body:JSON.stringify({path:form.get('path'),publish:form.get('publish')==='on'})});closeDialog('benchmarkDialog');toast('Benchmark 已导入');await refreshAll();}
  catch(error){toast(error.message,true);}
};
$('snapshotForm').onsubmit=async event=>{
  event.preventDefault();const form=new FormData(event.target);
  try{await api('/agent-snapshots',{method:'POST',body:JSON.stringify({name:form.get('name'),version:form.get('version'),adapter_type:form.get('adapter_type'),config:JSON.parse(form.get('config'))})});closeDialog('snapshotDialog');event.target.reset();toast('Agent 配置已创建');await refreshAll();}
  catch(error){toast(error.message,true);}
};
$('reviewForm').onsubmit=async event=>{
  event.preventDefault();const form=new FormData(event.target);
  try{await api(`/reviews/${form.get('review_id')}/submit`,{method:'POST',body:JSON.stringify({score:+form.get('score'),verdict:form.get('verdict'),reason:form.get('reason'),issue_types:String(form.get('issue_types')).split(',').map(x=>x.trim()).filter(Boolean),evidence_refs:String(form.get('evidence_refs')).split(',').map(x=>x.trim()).filter(Boolean)})});closeDialog('reviewDialog');toast('人工评分已保存');await refreshAll();}
  catch(error){toast(error.message,true);}
};

// Existing trace rendering and historical review helpers are retained below.

function renderTracePage(trace,layout) {
  const structural=new Set(['benchmark_task','attempt','official_grader','supervisor','human_review']);
  let seen=0,total=0;
  for(const node of trace.nodes)if(!structural.has(node.type))total++;
  const nodes=trace.nodes.filter(node=>structural.has(node.type) || ++seen<=state.traceLimit);
  const shown=Math.min(total,state.traceLimit),page={...trace,nodes};
  return `<p class="hint">显示 ${shown}/${total} 条执行事件，任务、评分和监督结论始终保留。</p>${layout==='journey'?traceJourney(page):traceRaw(page)}${shown<total?'<button class="secondary" onclick="moreTraceEvents()">再显示 100 条事件</button>':''}`;
}
function moreTraceEvents(){if(!state.trace)return;state.traceLimit+=100;$('traceRendered').innerHTML=renderTracePage(state.trace,state.traceLayout);}

function traceValue(node){const payload=node?.detail?.payload||{};let value=node?.summary||'';if(node?.type==='assistant_final')value=payload.content??payload.output??value;if(node?.type==='tool_result')value=payload.result??payload.output??value;return typeof value==='string'?value:JSON.stringify(value)}
function traceIcon(type){return ({tool_call:'↗',tool_result:'✓',assistant_final:'●',official_grader:'★',supervisor:'◎',human_review:'✓',benchmark_task:'◇'})[type]||'•'}
function traceSemantic(node){const semantic=node?.detail?.semantic_status||'observed',reason=node?.detail?.semantic_reason||'';return {semantic,reason}}
function traceEvent(node,previousToolResult){const value=traceValue(node),assessment=traceSemantic(node),mismatch=node.type==='assistant_final'&&previousToolResult&&value.trim()!==previousToolResult.trim(),violation=assessment.semantic==='violation'||mismatch;const label=mismatch?'最终答案与工具结果不一致':node.label;const reason=assessment.reason||(mismatch?'最终答案与最近工具结果不同':'');return `<button class="trace-event ${esc(node.type)} ${violation?'violation':esc(assessment.semantic)}" onclick="traceNodeDetail('${esc(node.id)}')"><i class="trace-event-icon">${traceIcon(node.type)}</i><span><strong>${esc(label)}</strong><span>${esc(String(value).slice(0,1200))}${String(value).length>1200?'…（点击查看完整内容）':''}</span>${reason?`<span class="trace-violation">规则判断：${esc(reason)}</span>`:''}</span></button>`}
function traceJourney(trace){const task=trace.nodes.find(n=>n.type==='benchmark_task'),groups=[],byId=Object.fromEntries(trace.nodes.map(n=>[n.id,n]));let current=null;for(const node of trace.nodes){if(node.type==='attempt'){current={attempt:node,events:[],grader:null,supervision:null,decision:null};groups.push(current)}else if(current){if(node.type==='official_grader')current.grader=node;else if(node.type==='supervisor')current.supervision=node;else if(node.type==='human_review')current.decision=node;else if(!['benchmark_task'].includes(node.type))current.events.push(node)}}const taskHtml=task?`<button class="trace-task" onclick="traceNodeDetail('${esc(task.id)}')"><h3>${esc(task.label)}</h3><p>${esc(task.summary||'')}</p></button>`:'';const steps=groups.map((group,index)=>{const attempt=group.attempt,isPass=attempt.status==='pass',tone=isPass?'pass':'fail';let lastToolResult='';const events=group.events.map(node=>{const rendered=traceEvent(node,lastToolResult);if(node.type==='tool_result')lastToolResult=traceValue(node);return rendered}).join('');const grader=group.grader?`<button class="trace-score ${group.grader.status==='passed'?'pass':'fail'}" onclick="traceNodeDetail('${esc(group.grader.id)}')"><span><b>官方评分</b><small>${esc(group.grader.summary||'')}</small></span>${status(group.grader.status)}</button>`:'';const review=[group.supervision,group.decision].filter(Boolean).map(node=>{const approved=node.type==='human_review'&&node.status==='approve_retry';return `<button class="trace-review ${approved?'approved':''}" onclick="traceNodeDetail('${esc(node.id)}')"><h4>${esc(node.label)} ${status(node.status)}</h4><p>${esc(node.summary||'')}</p></button>`}).join('');return `<article class="trace-step"><div class="trace-index ${review?'review':tone}">${index+1}</div><div class="trace-lane"><section class="trace-attempt ${tone}"><header class="trace-attempt-head"><div><div class="trace-attempt-title">执行 Agent · Attempt ${esc(group.attempt.detail?.attempt||index+1)}</div><div class="trace-attempt-meta">${esc(group.attempt.summary||'')}</div></div>${status(group.attempt.status)}</header><div class="trace-events">${events||'<div class="muted">此视图没有可显示的执行事件；切换到“详情”查看。</div>'}</div>${grader}</section>${review}</div></article>`}).join('');return `<div class="trace-journey">${taskHtml}${steps||'<div class="empty">没有可展示的 Attempt</div>'}</div>`}
function traceRaw(trace){return `<div class="trace-raw">${trace.nodes.map(n=>{const assessment=traceSemantic(n),semantic=assessment.semantic;return `<button class="trace-node ${esc(n.status)} ${esc(semantic)}" onclick="traceNodeDetail('${esc(n.id)}')"><strong>${esc(n.label)} ${status(n.status)}${semantic==='violation'?status('violation'):semantic==='verified'?status('verified'):''}</strong><span>${esc(n.summary||'')}</span>${assessment.reason?`<span class="trace-violation">规则判断：${esc(assessment.reason)}</span>`:''}</button>`}).join('')}</div>`}
function traceNodeDetail(id){const node=state.traceNodes[id];if(!node)return;showJsonDetail(node.label,node.detail||node)}
async function trialDetail(id){$('detailTitle').textContent='正在加载原始证据…';$('detailBody').innerHTML=empty('正在加载，可随时关闭。');openDialog('detailDialog');try{const t=await detailApi(`/trials/${id}`,'trial');if($('detailDialog').open)showJsonDetail(`Trial ${id}`,t)}catch(e){if(!e.canceled)toast(e.message,true)}}
let detailDownloadUrl=null;
function jsonDetail(title,encoded){showJsonDetail(title,JSON.parse(decodeURIComponent(encoded)));}
function showJsonDetail(title,value){
  const text=JSON.stringify(value,null,2);
  if(detailDownloadUrl)URL.revokeObjectURL(detailDownloadUrl);
  detailDownloadUrl=URL.createObjectURL(new Blob([text],{type:'application/json;charset=utf-8'}));
  $('detailTitle').textContent=title;
  $('detailBody').innerHTML=`${text.length>40000?'<p class="hint">内容较长，页面展示前 40,000 字符；可下载完整证据。</p>':''}<a class="button secondary small" download="evaluation-evidence.json" href="${detailDownloadUrl}">下载完整 JSON</a><div class="code">${esc(text.slice(0,40000))}</div>`;
  openDialog('detailDialog');
}
async function loadAudit(){try{const logs=await api('/audit?limit=30');document.getElementById('auditTable').innerHTML=logs.length?`<table><thead><tr><th>时间</th><th>操作</th><th>资源</th></tr></thead><tbody>${logs.map(a=>`<tr><td>${esc(new Date(a.created_at).toLocaleString())}</td><td>${esc(a.action)}<br><span class="muted">${esc(a.actor_id)}</span></td><td>${esc(a.resource_type)}<br><span class="muted">${esc(a.resource_id)}</span></td></tr>`).join('')}</tbody></table>`:'<div class="empty">暂无审计日志</div>'}catch(e){toast(e.message,true)}}
function openReview(id){document.querySelector('#reviewForm [name=review_id]').value=id;openDialog('reviewDialog')}

refreshAll();
setInterval(()=>{if(!document.hidden)refreshAll();},10000);
