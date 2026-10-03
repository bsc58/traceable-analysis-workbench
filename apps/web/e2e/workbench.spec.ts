import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
const evidence=resolve('../../docs/ui/evidence');
const ids=JSON.parse(readFileSync(resolve(evidence,'fixture-ids.json'),'utf8'));
const parameters={current_start:'2025-01-08T00:00:00Z',current_end:'2025-01-15T00:00:00Z',baseline_start:'2025-01-01T00:00:00Z',baseline_end:'2025-01-08T00:00:00Z',as_of:'2025-01-15T00:00:00Z'};
const resultSpec={schema_version:'result_spec@1',requirements:[{column:'orders',entity:{window:'current'}}]};
async function view(page:any,name:string){await page.getByRole('navigation').getByRole('button',{name:new RegExp(name)}).click();}
async function shot(page:any,name:string){await page.screenshot({path:resolve(evidence,name+'.png'),fullPage:true});}

test('six real API views, creation and injection boundary',async({page})=>{
  const external:string[]=[];const errors:string[]=[];
  page.on('request',request=>{if(!request.url().startsWith('http://127.0.0.1:8912/'))external.push(request.url());});
  page.on('pageerror',error=>errors.push(error.message));
  const response=await page.goto('/');
  expect(response?.headers()['content-security-policy']).toContain("frame-ancestors 'none'");
  await expect(page.getByRole('button',{name:'frozen · UNSUPPORTED'})).toBeDisabled();
  await expect(page.getByText('side_effects：',{exact:false}).first()).toBeVisible();
  await shot(page,'01-configuration');
  await view(page,'任务创建');
  await page.getByLabel('问题',{exact:true}).fill('公开合成数据的当前订单数量');
  await page.getByLabel('参数 JSON').fill(JSON.stringify(parameters,null,2));
  await page.getByLabel('result_spec JSON').fill(JSON.stringify(resultSpec,null,2));
  await shot(page,'02-create-task');
  const requestPromise=page.waitForRequest(request=>request.method()==='POST'&&request.url().endsWith('/api/runs'));
  await page.getByRole('button',{name:'创建任务',exact:true}).click();
  const request=await requestPromise;
  expect(request.headers()['idempotency-key']).toBeTruthy();
  expect(request.headers()['authorization']).toBeUndefined();
  await expect(page.getByRole('heading',{name:'执行状态'})).toBeVisible();
  await page.getByLabel('运行 ID').fill(ids.execution);
  await expect(page.getByText('RESOLVED_FAILED',{exact:false}).first()).toBeVisible();
  await expect(page.getByText('合成只读工具未执行，无外部副作用',{exact:false}).first()).toBeVisible();
  await expect(page.getByText('#1 · RUN_ADMITTED')).toBeVisible();
  await shot(page,'03-execution');
  await view(page,'分析报告');await page.getByLabel('运行 ID').fill(ids.report);
  const summary=page.getByTestId('validation-summary');await expect(summary).toContainText('warning');await expect(summary).toContainText('live');
  await expect(page.getByTestId('unverified-text')).toContainText('globalThis.p9aInjected');
  expect(await page.evaluate(()=>Boolean((globalThis as any).p9aInjected))).toBe(false);
  expect(await page.locator('img').count()).toBe(0);
  expect(await summary.evaluate(el=>Boolean(el.compareDocumentPosition(document.querySelector('[data-testid="report-title"]')!)&Node.DOCUMENT_POSITION_FOLLOWING))).toBe(true);
  await shot(page,'04-report-injection');
  await view(page,'历史与比较');await page.getByLabel('左侧运行').selectOption(ids.report);await page.getByLabel('右侧运行').selectOption(ids.compatible);
  await page.getByRole('button',{name:'比较事实'}).click();await expect(page.getByText('PASSED',{exact:true})).toBeVisible();
  await expect(page.getByText('两侧版本摘要与差异')).toBeVisible();await shot(page,'05-history-comparison');
  await page.getByLabel('右侧运行').selectOption(ids.incompatible);await page.getByRole('button',{name:'比较事实'}).click();
  await expect(page.getByRole('alert')).toContainText('incompatible_facts');await shot(page,'05b-incompatible-refusal');
  await view(page,'评测中心');await expect(page.getByRole('heading',{name:'模型评测'})).toBeVisible();await expect(page.getByText('分数：NOT RUN')).toBeVisible();
  await expect(page.getByText('夹具，非模型评测').first()).toBeVisible();await shot(page,'06-evaluations');
  expect(external).toEqual([]);expect(errors).toEqual([]);
});

test('backend refusal code reaches task form unchanged',async({page})=>{
  await page.goto('/');await view(page,'任务创建');
  await page.getByLabel('问题',{exact:true}).fill('超出范围的合成请求');
  await page.getByLabel('参数 JSON').fill(JSON.stringify({...parameters,current_end:'2025-02-01T00:00:00Z'}));
  await page.getByLabel('result_spec JSON').fill(JSON.stringify(resultSpec));
  await page.getByRole('button',{name:'创建任务',exact:true}).click();
  await expect(page.getByRole('alert')).toContainText('out_of_scope');await shot(page,'07-server-rejection');
});

test('blocked candidate, incremental cursor and mobile layout',async({page})=>{
  await page.goto('/');await view(page,'分析报告');await page.getByLabel('运行 ID').fill(ids.blocked);
  await expect(page.getByTestId('validation-summary')).toContainText('blocked');await shot(page,'08-blocked-report');
  await view(page,'执行详情');await page.getByLabel('运行 ID').fill(ids.execution);
  const next=await page.waitForRequest(request=>/\/events\?after=[1-9]/.test(request.url()));
  expect(next.method()).toBe('GET');
  await page.setViewportSize({width:390,height:844});await view(page,'评测中心');await expect(page.getByText('分数：NOT RUN')).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);await shot(page,'09-mobile');
});

test('credential gateway blocks operator writes, cross-origin and missing auth',async({request})=>{
  const forbidden=await request.post(`/api/runs/${ids.execution}/resolutions`,{data:{}});
  expect(forbidden.status()).toBe(405);expect((await forbidden.json()).error.code).toBe('ui_read_only');
  const csrf=await request.post('/api/runs',{headers:{Origin:'https://untrusted.invalid'},data:{}});expect(csrf.status()).toBe(403);
  const host=await request.get('/api/ui/projects',{headers:{Host:'untrusted.invalid'}});expect(host.status()).toBe(403);
  const direct=await request.get('http://127.0.0.1:8911/ui/projects');expect(direct.status()).toBe(401);
  const leak=await request.get('/server.mjs');expect(leak.status()).toBe(404);
});

test('token remains outside browser storage and bundled assets',async({page})=>{
  const filename=process.env.WORKBENCH_UI_TOKEN_FILE;
  if(!filename)throw new Error('Test requires the private local token-file path');
  const token=readFileSync(filename,'utf8').trim();
  const { readdirSync }=await import('node:fs');
  const dir=resolve('dist/assets');
  for(const file of readdirSync(dir)){
    if(readFileSync(resolve(dir,file)).includes(Buffer.from(token)))throw new Error('Credential found in build output');
  }
  await page.goto('/');
  const values=await page.evaluate(()=>({local:{...localStorage},session:{...sessionStorage},cookies:document.cookie}));
  if(JSON.stringify(values).includes(token))throw new Error('Credential found in browser storage');
  expect(values.cookies).toBe('');
});
