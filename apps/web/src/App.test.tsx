import { describe,it,expect,vi,afterEach } from 'vitest';
import { render,screen,fireEvent,waitFor } from '@testing-library/react';
import { Configuration,CreateTask,Evaluations,ReportView,ErrorBox,Execution } from './App';
import { ApiError,submissionAttempt } from './api';
const config={project_id:'synthetic',scope:'只读',sources:[{ref:'s@1',status:'未核实',status_reason:'未探测',catalog:{},catalog_digest:'catalog-hash',capabilities:{live:{status:'PASSED',reason:'支持'},frozen:{status:'UNSUPPORTED',reason:'没有快照'}},tools:[{ref:'read@1',side_effects:'none',retry:'never',input_schema:{}}],skills:[{ref:'skill@1',digest:'skill-hash',instructions:'说明',tools:[]}]}],budgets:{tool_budget:{minimum:1,maximum:30,default:12},report_budget:{minimum:1,maximum:20,default:3}}};
const attack='<img src=x onerror="globalThis.p9aInjected=true"><script>globalThis.p9aInjected=true</script>';
function record(status='valid',mode='live'){return {run:{manifest:{data_version:{mode}}},checked_facts:[{value:42,unit:'orders'}],report:{report:{title:attack,result_status:'complete',facts:[{note:attack}],hypotheses:[attack]},validation:{status,scope:'结构化数值和引用'}}};}
afterEach(()=>{vi.unstubAllGlobals();});
describe('report boundary',()=>{
  it.each(['valid','warning','blocked'])('shows %s and scope before title, separates text',status=>{
    render(<ReportView record={record(status)}/>);
    expect(screen.getByTestId('validation-summary').compareDocumentPosition(screen.getByTestId('report-title'))&Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByTestId('validation-summary')).toHaveTextContent(status);
    expect(screen.getByTestId('validation-summary')).toHaveTextContent('complete');
    expect(screen.getByTestId('unverified-text')).toHaveTextContent('整个区域均未校验');
    expect(document.querySelector('script,img')).toBeNull();
  });
  it('displays frozen from saved manifest',()=>{render(<ReportView record={record('warning','frozen')}/>);expect(screen.getByTestId('validation-summary')).toHaveTextContent('frozen');});
  it('absent report is NOT RUN',()=>{render(<ReportView record={{run:{manifest:{}},report:null}}/>);expect(screen.getByTestId('validation-summary')).toHaveTextContent('NOT RUN');});
});
it('unsupported controls include reason and tool side effects',()=>{render(<Configuration config={config}/>);expect(screen.getByRole('button',{name:'frozen · UNSUPPORTED'})).toBeDisabled();expect(screen.getByText('没有快照')).toBeVisible();expect(screen.getByText(/side_effects/)).toHaveTextContent('none');});
it('empty evaluations stay NOT RUN, fixture is clearly labelled',()=>{render(<Evaluations data={{status:'NOT RUN',scores:null,fixture_runs:[{run_id:'real_saved_fixture'}]}}/>);expect(screen.getByText('夹具，非模型评测')).toBeVisible();expect(screen.getByText('分数：NOT RUN')).toBeVisible();});
it('keeps exact server error code',()=>{render(<ErrorBox error={new ApiError('version_changed','原始原因',409)}/>);expect(screen.getByRole('alert')).toHaveTextContent('version_changed');expect(screen.getByRole('alert')).toHaveTextContent('原始原因');});
it('idempotency key is stable only for an unchanged request',()=>{const a=submissionAttempt(null,'{}');expect(submissionAttempt(a,'{}').key).toBe(a.key);expect(submissionAttempt(a,'{"a":1}').key).not.toBe(a.key);});
it('validates JSON and required facts before sending',async()=>{const fetcher=vi.fn();vi.stubGlobal('fetch',fetcher);render(<CreateTask config={config} onCreated={()=>{}}/>);fireEvent.change(screen.getByLabelText('问题'),{target:{value:'test'}});fireEvent.click(screen.getByRole('button',{name:'创建任务'}));await screen.findByRole('alert');expect(fetcher).not.toHaveBeenCalled();});
it('retries unchanged submissions with same key and preserves server code',async()=>{
  const fetcher=vi.fn().mockResolvedValue({ok:false,status:422,json:async()=>({error:{code:'out_of_scope',message:'原样'}})});vi.stubGlobal('fetch',fetcher);
  render(<CreateTask config={config} onCreated={()=>{}}/>);fireEvent.change(screen.getByLabelText('问题'),{target:{value:'test'}});fireEvent.change(screen.getByLabelText('result_spec JSON'),{target:{value:'{"requirements":[{"column":"orders"}]}'}});
  fireEvent.click(screen.getByRole('button',{name:'创建任务'}));await screen.findByText('out_of_scope');fireEvent.click(screen.getByRole('button',{name:'创建任务'}));await waitFor(()=>expect(fetcher).toHaveBeenCalledTimes(2));
  expect(fetcher.mock.calls[0][1].headers['Idempotency-Key']).toBe(fetcher.mock.calls[1][1].headers['Idempotency-Key']);
});
it('execution requests bounded cursor pages and never exposes operator controls',async()=>{
  vi.stubGlobal('fetch',vi.fn(async(path:string)=>({ok:true,json:async()=>path.includes('/events')?{events:[{seq:1,kind:'RUN_ADMITTED',payload:{}}],next_cursor:1,state:'ADMITTED',has_more:false}:{run:{state:'ADMITTED',manifest:{}},steps:[],attempts:[],resolutions:[]}})));
  render(<Execution runId="r"/>);await screen.findByText('#1 · RUN_ADMITTED');expect(screen.queryByRole('button',{name:/处理|resolve|reconcile/i})).toBeNull();
});
