/** Record the local synthetic demo only; never navigate to an external site. */
import { createRequire } from 'node:module';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const require=createRequire(new URL('../apps/web/package.json',import.meta.url));
const { chromium }=require('playwright');
const args=process.argv.slice(2);
function option(name){const index=args.indexOf(name);if(index<0||!args[index+1])throw new Error(`Missing ${name}`);return args[index+1];}
const summary=JSON.parse(await readFile(option('--summary'),'utf8'));
const output=resolve(option('--output'));await mkdir(output,{recursive:true});
const browser=await chromium.launch({headless:true});
const context=await browser.newContext({viewport:{width:1280,height:800},recordVideo:{dir:output,size:{width:1280,height:800}}});
const page=await context.newPage();const errors=[],external=[];
page.on('pageerror',error=>errors.push(error.message));
page.on('request',request=>{if(!request.url().startsWith('http://127.0.0.1:8912/'))external.push(request.url());});
async function view(name){await page.getByRole('navigation').getByRole('button',{name:new RegExp(name)}).click();await page.waitForTimeout(1800);}
async function shot(name){await page.screenshot({path:resolve(output,name+'.png')});}
let result;
try{
  const response=await page.goto('http://127.0.0.1:8912/');
  if(!response.headers()['content-security-policy']?.includes("frame-ancestors 'none'"))throw new Error('CSP absent');
  await page.getByRole('heading',{name:'Skill 版本与摘要'}).waitFor();await page.waitForTimeout(2000);await shot('configuration');
  await view('任务创建');
  await page.getByLabel('问题',{exact:true}).fill('比较两个公开合成订单窗口，引用订单数、退款订单数和退款比例。');
  await page.getByLabel('参数 JSON').fill(JSON.stringify({current_start:'2025-01-08T00:00:00Z',current_end:'2025-01-15T00:00:00Z',baseline_start:'2025-01-01T00:00:00Z',baseline_end:'2025-01-08T00:00:00Z',as_of:'2025-01-15T00:00:00Z'},null,2));
  await page.getByLabel('result_spec JSON').fill(JSON.stringify({requirements:['current','baseline'].flatMap(window=>['orders','refunded_orders','refund_rate'].map(column=>({column,entity:{window}})))},null,2));
  await page.waitForTimeout(2200);await shot('creation');
  const submitted=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/api/runs'));
  await page.getByRole('button',{name:'创建任务',exact:true}).click();
  const created=await (await submitted).json();if(!created.run_id)throw new Error('Creation failed');
  await page.getByText('SUCCEEDED',{exact:true}).waitFor({timeout:30000});await page.waitForTimeout(2000);await shot('execution');
  await view('分析报告');await page.getByTestId('validation-summary').waitFor();
  if(!(await page.getByTestId('validation-summary').textContent()).includes('complete'))throw new Error('Report incomplete');
  if(await page.locator('.fact-value').count()!==6)throw new Error('Expected six checked facts');
  await page.waitForTimeout(2000);await shot('report');
  await page.mouse.wheel(0,620);await page.waitForTimeout(2000);
  await view('历史与比较');await page.evaluate(()=>scrollTo(0,0));
  await page.getByLabel('左侧运行').selectOption(summary.fixture.run_id);
  await page.getByLabel('右侧运行').selectOption(created.run_id);
  await page.getByRole('button',{name:'比较事实'}).click();await page.getByText('PASSED',{exact:true}).waitFor();await page.waitForTimeout(2200);await shot('comparison');
  await view('评测中心');await page.getByRole('heading',{name:'模型评测'}).waitFor();await page.waitForTimeout(2200);await shot('evaluation');
  if(errors.length||external.length)throw new Error('Browser error or external request observed');
  result={status:'PASSED',views:6,created_run:created.run_id,initial_run:summary.fixture.run_id,fixture:true,external_requests:0,page_errors:0,viewport:{width:1280,height:800}};
}finally{
  const video=page.video();await context.close();if(video)await video.saveAs(resolve(output,'demo.webm'));await browser.close();
}
await writeFile(resolve(output,'recording-result.json'),JSON.stringify(result,null,2));
console.log(JSON.stringify(result));
