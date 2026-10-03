import { useEffect, useRef, useState } from 'react';
import { api, ApiError, submissionAttempt } from './api';

type Data = Record<string, any>;
const views = ['项目配置','任务创建','执行详情','分析报告','历史与比较','评测中心'];
const enc = encodeURIComponent;
export function Json({value}:{value:unknown}) {return <pre>{JSON.stringify(value,null,2)}</pre>;}
export function ErrorBox({error}:{error:unknown}) {
  if (!error) return null;
  return <div role="alert" className="error"><strong>{error instanceof ApiError ? error.code : 'client_error'}</strong><p>{error instanceof Error ? error.message : String(error)}</p></div>;
}
function Panel({title, children}:{title:string; children:React.ReactNode}) {return <section className="panel"><h2>{title}</h2>{children}</section>;}
function useLoad(path:string, revision=0) {
  const [data,setData] = useState<any>(null), [error,setError] = useState<unknown>(null);
  useEffect(() => {let alive=true; setData(null);setError(null);
    if(path) api(path).then(d=>{if(alive)setData(d);}).catch(e=>{if(alive)setError(e);});
    return ()=>{alive=false;};},[path,revision]);
  return {data,error};
}
export function Configuration({config}:{config:Data}) {
  return <><p className="muted">{config.scope}</p>{config.sources.map((source:Data)=><div key={source.ref}>
    <Panel title={`数据源 · ${source.ref}`}><p>数据源状态：{source.status} · {source.status_reason}</p>
      <div className="capabilities">{Object.entries(source.capabilities).map(([name,value]:any)=><div key={name}>
        <button type="button" disabled aria-describedby={`cap-${name}`}>{name} · {value.status}</button>
        <small id={`cap-${name}`}>{value.reason}{value.status==='PASSED'?'；配置页仅展示':''}</small></div>)}</div>
      <details><summary>数据目录 · {source.catalog_digest}</summary><Json value={source.catalog}/></details>
    </Panel>
    <Panel title="Skill 版本与摘要">{source.skills.map((skill:Data)=><article key={skill.ref}><h3>{skill.ref}</h3><p className="mono">sha256 · {skill.digest}</p><p>{skill.instructions}</p><Json value={{tools:skill.tools,attachments:skill.attachments}}/></article>)}</Panel>
    <Panel title="工具清单">{source.tools.map((tool:Data)=><article key={tool.ref}><h3>{tool.ref}</h3><p>{tool.description}</p><p>side_effects：<b>{tool.side_effects}</b> · retry：{tool.retry}</p><details><summary>参数契约</summary><Json value={tool.input_schema}/></details></article>)}</Panel>
  </div>)}<Panel title="预算范围"><Json value={config.budgets}/></Panel></>;
}
export function CreateTask({config,onCreated}:{config:Data;onCreated:(id:string)=>void}) {
  const [sourceRef,setSourceRef]=useState(config.sources[0]?.ref??'');
  const source=config.sources.find((s:Data)=>s.ref===sourceRef);
  const [skill,setSkill]=useState(source?.skills[0]?.ref??'');
  const [question,setQuestion]=useState(''),[parameters,setParameters]=useState('{}');
  const [spec,setSpec]=useState('{"schema_version":"result_spec@1","requirements":[]}');
  const [mode,setMode]=useState('live');
  const [toolBudget,setToolBudget]=useState(config.budgets.tool_budget.default);
  const [reportBudget,setReportBudget]=useState(config.budgets.report_budget.default);
  const [busy,setBusy]=useState(false),[error,setError]=useState<unknown>(null);
  const attempt=useRef<{body:string;key:string}|null>(null);
  async function submit(event:React.FormEvent) {
    event.preventDefault(); if(busy)return; setError(null);setBusy(true);
    try {
      const parsedParameters=JSON.parse(parameters), parsedSpec=JSON.parse(spec);
      if(!parsedParameters || Array.isArray(parsedParameters) || typeof parsedParameters!=='object')throw new Error('参数必须是 JSON 对象');
      if(!Array.isArray(parsedSpec?.requirements) || !parsedSpec.requirements.length)throw new Error('result_spec 至少需要一条必要事实');
      const body=JSON.stringify({project_id:config.project_id,source_ref:sourceRef,skill_ref:skill,question,
        parameters:parsedParameters,result_spec:parsedSpec,consistency:mode,tool_budget:Number(toolBudget),report_budget:Number(reportBudget)});
      attempt.current=submissionAttempt(attempt.current,body);
      const result=await api('/runs',{method:'POST',body,headers:{'Idempotency-Key':attempt.current.key}});
      onCreated(result.run_id);
    }catch(e){setError(e);}finally{setBusy(false);}
  }
  return <form onSubmit={submit} className="panel"><h2>创建分析任务</h2><p>提交只创建任务。执行器的接入和启动由主线负责。</p>
    <label>数据源<select value={sourceRef} onChange={e=>{setSourceRef(e.target.value);setSkill(config.sources.find((s:Data)=>s.ref===e.target.value)?.skills[0]?.ref??'');setMode('live');}}>{config.sources.map((s:Data)=><option key={s.ref}>{s.ref}</option>)}</select></label>
    <label>Skill<select value={skill} onChange={e=>setSkill(e.target.value)}>{source?.skills.map((s:Data)=><option key={s.ref}>{s.ref}</option>)}</select></label>
    <label>问题<textarea required maxLength={8000} value={question} onChange={e=>setQuestion(e.target.value)}/></label>
    <label>参数 JSON<textarea className="editor" value={parameters} onChange={e=>setParameters(e.target.value)}/></label>
    <label>result_spec JSON<textarea className="editor" value={spec} onChange={e=>setSpec(e.target.value)}/></label>
    <label>数据模式<select value={mode} onChange={e=>setMode(e.target.value)}>{['live','frozen'].map(m=><option key={m} disabled={source?.capabilities[m]?.status!=='PASSED'}>{m}</option>)}</select></label>
    {source?.capabilities.frozen?.status!=='PASSED'&&<p>frozen · UNSUPPORTED：{source?.capabilities.frozen?.reason??'数据源未声明支持'}</p>}
    <div className="columns">{[['工具调用预算',toolBudget,setToolBudget,'tool_budget'],['报告提交预算',reportBudget,setReportBudget,'report_budget']].map(([label,value,setter,key]:any)=><label key={key}>{label}<input type="number" required min={config.budgets[key].minimum} max={config.budgets[key].maximum} value={value} onChange={e=>setter(e.target.value)}/></label>)}</div>
    <p className="muted">同一请求重试沿用幂等键；修改内容后生成新键。</p><ErrorBox error={error}/><button className="primary" disabled={busy||!source||!skill}>{busy?'提交中…':'创建任务'}</button>
  </form>;
}
export function Execution({runId}:{runId:string}) {
  const [revision,setRevision]=useState(0);
  const {data:record,error}=useLoad(runId?`/ui/runs/${enc(runId)}/detail`:'',revision);
  const [eventRows,setEvents]=useState<Data[]>([]),[streamError,setStreamError]=useState<unknown>(null),[state,setState]=useState('');
  useEffect(()=>{let active=true,timer:ReturnType<typeof setTimeout>;let cursor=0;setEvents([]);setStreamError(null);setState('');
    async function poll(){try {const page=await api(`/ui/runs/${enc(runId)}/events?after=${cursor}&limit=100`);if(!active)return;
      if(page.events.length)setRevision(value=>value+1);
      cursor=page.next_cursor;setState(page.state);setEvents(previous=>[...previous,...page.events.filter((item:Data)=>!previous.some(old=>old.seq===item.seq))]);setStreamError(null);
      timer=setTimeout(poll,page.has_more?0:2000);
    }catch(e){if(active){setStreamError(e);timer=setTimeout(poll,5000);}}}
    if(runId)void poll(); return()=>{active=false;clearTimeout(timer);};},[runId]);
  return <><ErrorBox error={error||streamError}/>{record&&<><Panel title="执行状态"><p className="badge">{state||record.run.state}</p><p className="mono">{runId}</p><p>只读记录 · 操作员处理功能尚未接入</p><Json value={record.run.manifest.execution}/></Panel>
    <Panel title="步骤与 attempt"><Json value={{steps:record.steps,attempts:record.attempts}}/></Panel>
    <Panel title="错误、对账与人工处理记录"><Json value={{errors:record.attempts.filter((a:Data)=>a.error_code||a.error),reconciliation:eventRows.filter(e=>/RECONCIL|UNKNOWN|RESOL/.test(e.kind)||e.payload?.reconciliation),resolutions:record.resolutions}}/></Panel></>}
    <Panel title="事件流 · 游标增量拉取"><ol className="events">{eventRows.map(e=><li key={e.seq}><b>#{e.seq} · {e.kind}</b><small>{e.at}</small><Json value={e.payload}/></li>)}</ol>{!eventRows.length&&<p>尚无事件</p>}</Panel></>;
}
export function ReportView({record}:{record:Data}) {
  const artifact=record.report,body=artifact?.report??{},validation=artifact?.validation??{};
  const mode=record.run.manifest.data_version?.mode??record.run.manifest.request?.consistency??'未核实';
  const facts=record.checked_facts??[];
  return <><section className="validation" data-testid="validation-summary" aria-label="校验摘要">
    <p>校验结果：<b>{validation.status??'NOT RUN'}</b></p><p>校验范围：{validation.scope??'尚未提交结构化事实校验'}</p>
    <p>result_status：<b>{body.result_status??'NOT RUN'}</b></p><p>数据模式：<b>{mode}</b></p></section>
    <h1 data-testid="report-title">标题（未校验）：{body.title??'尚无报告'}</h1>
    <Panel title="结构化事实核对记录"><p>仅展示本次校验记录接纳的事实。文字解释不在核对范围内。</p>{facts.length ? <div className="fact-list">{facts.map((fact:Data,index:number)=><article key={index}><div className="fact-value"><strong>{String(fact.value)}</strong><span>{fact.unit}</span></div><p>{fact.operation} · {(fact.inputs??[]).map((input:Data)=>input.column).join(" / ")}</p><p className="muted">{(fact.inputs??[]).map((input:Data)=>JSON.stringify(input.entity)).join(" → ")}</p><details><summary>实体、时间与引用结构</summary><Json value={fact}/></details></article>)}</div> : <p>尚无通过单项核对的结构化事实</p>}<details><summary>校验问题与覆盖范围</summary><Json value={{errors:validation.errors,warnings:validation.warnings,coverage:validation.requirement_coverage}}/></details></Panel>
    <section className="unverified" data-testid="unverified-text"><h2>文字区 · 未校验</h2><p>以下整个区域均未校验</p><Json value={{title:body.title,notes:(body.facts??[]).map((f:Data)=>f.note??f.label).filter(Boolean),hypotheses:body.hypotheses??[],limitations:body.limitations??[],next_checks:body.next_checks??[]}}/></section></>;
}
function ReportPage({runId}:{runId:string}) {const {data,error}=useLoad(runId?`/ui/runs/${enc(runId)}/detail`:'');return <><ErrorBox error={error}/>{data&&<ReportView record={data}/>}</>;}
function History({project,onSelect}:{project:string;onSelect:(id:string)=>void}) {
  const {data:rows,error}=useLoad(`/projects/${enc(project)}/runs`);
  const [left,setLeft]=useState(''),[right,setRight]=useState(''),[comparison,setComparison]=useState<Data|null>(null),[failure,setFailure]=useState<unknown>(null);
  async function compare(){setFailure(null);setComparison(null);try{setComparison(await api(`/ui/projects/${enc(project)}/compare?left=${enc(left)}&right=${enc(right)}`));}catch(e){setFailure(e);}}
  return <><ErrorBox error={error||failure}/><Panel title="历史运行 · 最近最多 200 条"><ul className="history">{rows?.map((r:Data)=><li key={r.id}><button onClick={()=>onSelect(r.id)}>{r.id}</button><span>{r.state} · {r.created_at}</span></li>)}</ul></Panel>
    <Panel title="事实比较"><p>按实体键、列、单位、时间和运算配对。版本差异始终单独列出。</p><div className="columns">{[['左侧运行',left,setLeft],['右侧运行',right,setRight]].map(([label,value,setter]:any)=><label key={label}>{label}<select value={value} onChange={e=>{setter(e.target.value);setComparison(null);}}><option value="">请选择</option>{rows?.map((r:Data)=><option key={r.id} value={r.id}>{r.id}</option>)}</select></label>)}</div>
    <button disabled={!left||!right} onClick={compare}>比较事实</button>{comparison&&<><p>{comparison.status}</p>{comparison.error&&<ErrorBox error={new ApiError(comparison.error.code,comparison.error.message,422)}/>}<h3>两侧版本摘要与差异</h3><Json value={comparison.versions}/>{!comparison.error&&<Json value={comparison.facts}/>}</>}</Panel></>;
}
export function Evaluations({data}:{data:Data}) {return <><Panel title="模型评测"><p className="badge">{data.status}</p><p>{data.reason}</p><p>分数：{data.scores===null?'NOT RUN':JSON.stringify(data.scores)}</p>{data.records?.length>0&&<Json value={data.records}/>}</Panel><Panel title="已保存的夹具活动">{data.fixture_runs?.length?data.fixture_runs.map((r:Data)=><article key={r.run_id}><b>夹具，非模型评测</b><p>{r.run_id} · {r.state}</p><small>{r.created_at}</small></article>):<p>NOT RUN</p>}</Panel></>;}
function EvaluationPage({project}:{project:string}) {const {data,error}=useLoad(`/ui/projects/${enc(project)}/evaluations`);return <><ErrorBox error={error}/>{data&&<Evaluations data={data}/>}</>;}
export function App() {
  const [view,setView]=useState(0),[project,setProject]=useState(''),[runId,setRunId]=useState('');
  const {data:projects,error:projectError}=useLoad('/ui/projects');
  const {data:config,error:configError}=useLoad(project?`/ui/projects/${enc(project)}/configuration`:'');
  useEffect(()=>{if(projects?.projects.length&&!project)setProject(projects.projects[0]);},[projects,project]);
  function select(id:string){setRunId(id);setView(2);}
  return <div className="shell"><aside><div className="brand">AW<span>可追溯分析工作台</span></div><p className="eyebrow">LOCAL WORKSPACE</p><nav aria-label="工作台视图">{views.map((label,index)=><button key={label} aria-current={view===index?'page':undefined} onClick={()=>setView(index)}><span>0{index+1}</span>{label}</button>)}</nav><footer>证据 · 版本 · 边界<br/>本机固定身份</footer></aside><main><header><div><p className="eyebrow">ANALYSIS WORKBENCH</p>{view===3?<p>报告查看</p>:<h1>{views[view]}</h1>}</div><label>项目<select value={project} onChange={e=>{setProject(e.target.value);setRunId('');}}>{projects?.projects.map((p:string)=><option key={p}>{p}</option>)}</select></label></header>
    <ErrorBox error={projectError||configError}/>{!config&&!configError&&<p role="status">加载项目配置…</p>}
    {config&&view===0&&<Configuration config={config}/>} {config&&view===1&&<CreateTask key={project} config={config} onCreated={select}/>}
    {(view===2||view===3)&&<label className="run-input">运行 ID<input value={runId} onChange={e=>setRunId(e.target.value)} placeholder="从历史选择，或输入运行 ID"/></label>}
    {view===2&&runId&&<Execution key={runId} runId={runId}/>} {view===3&&runId&&<ReportPage key={runId} runId={runId}/>}
    {(view===2||view===3)&&!runId&&<p>请先选择或创建任务。</p>}
    {project&&view===4&&<History key={project} project={project} onSelect={select}/>} {project&&view===5&&<EvaluationPage key={project} project={project}/>}
  </main></div>;
}
