/** Local-only static host and narrow credential gateway. No token in browser JS. */
import http from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { resolve, dirname, extname, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root=resolve(dirname(fileURLToPath(import.meta.url)),'../apps/web/dist');
const container=process.env.DEMO_CONTAINER==='1';
const tokenPath=process.env.WORKBENCH_UI_TOKEN_FILE;
if(!tokenPath)throw new Error('WORKBENCH_UI_TOKEN_FILE is required');
const file=await stat(tokenPath);
if(!file.isFile() || (file.mode & 0o077)!==0)throw new Error('Token file must be a private local file (0600)');
const token=(await readFile(tokenPath,'utf8')).trim();
if(token.length<32)throw new Error('Token file must contain at least 32 characters');
const target=new URL(process.env.WORKBENCH_UI_API_ORIGIN??'http://127.0.0.1:8911');
if(target.protocol!=='http:'||!(target.hostname==='127.0.0.1'||container&&target.hostname==='api')||target.username||target.password||target.pathname!=='/'||target.search||target.hash)throw new Error('API origin must be a plain 127.0.0.1 HTTP origin');
const port=Number(process.env.WORKBENCH_UI_PORT??8912);
if(!Number.isInteger(port)||port<1024||port>65535)throw new Error('Invalid local UI port');
const origin=`http://127.0.0.1:${port}`;
const csp="default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'";
function send(res,status,body,type='application/json') {
  res.writeHead(status,{'Content-Type':type,'Content-Security-Policy':csp,'X-Content-Type-Options':'nosniff','Cache-Control':'no-store','Referrer-Policy':'no-referrer','Cross-Origin-Resource-Policy':'same-origin'});
  res.end(body);
}
function error(res,status,code,message){send(res,status,JSON.stringify({error:{code,message}}));}
const readPaths=[/^\/ui\/projects$/, /^\/ui\/projects\/[^/]+\/(configuration|compare|evaluations)$/, /^\/ui\/runs\/[^/]+\/(detail|events)$/, /^\/projects\/[^/]+\/runs$/];
const types={'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8','.svg':'image/svg+xml'};
const server=http.createServer(async(req,res)=>{
  try{
    if(req.headers.host!==`127.0.0.1:${port}`)return error(res,403,'invalid_host','Local host required');
    if(req.headers.origin&&req.headers.origin!==origin)return error(res,403,'invalid_origin','Same origin required');
    if(req.headers['sec-fetch-site']==='cross-site')return error(res,403,'invalid_origin','Cross-site requests are refused');
    const url=new URL(req.url,origin);
    if(url.pathname.startsWith('/api/')){
      const path=url.pathname.slice(4);
      const allowed=req.method==='GET'&&readPaths.some(pattern=>pattern.test(path)) || req.method==='POST'&&path==='/runs';
      if(!allowed)return error(res,405,'ui_read_only','Only task creation and UI reads are exposed');
      if(req.method==='POST'&&(req.headers.origin!==origin||!req.headers['content-type']?.startsWith('application/json')))return error(res,403,'invalid_origin','Same-origin JSON is required');
      let chunks=[],length=0;
      for await(const chunk of req){length+=chunk.length;if(length>1_000_000)return error(res,413,'request_too_large','Request exceeds local limit');chunks.push(chunk);}
      const headers={Authorization:`Bearer ${token}`,'Content-Type':'application/json'};
      const key=req.headers['idempotency-key'];if(typeof key==='string')headers['Idempotency-Key']=key;
      const result=await fetch(new URL(path+url.search,target),{method:req.method,headers,redirect:'error',signal:AbortSignal.timeout(30000),...(req.method==='POST'?{body:Buffer.concat(chunks)}:{})});
      return send(res,result.status,Buffer.from(await result.arrayBuffer()));
    }
    if(req.method!=='GET'&&req.method!=='HEAD')return error(res,405,'method_not_allowed','Static files are read-only');
    const filename=resolve(root,'.'+decodeURIComponent(url.pathname==='/'?'/index.html':url.pathname));
    if(!filename.startsWith(root+sep))return error(res,404,'not_found','File not found');
    const content=await readFile(filename);
    send(res,200,req.method==='HEAD'?'':content,types[extname(filename)]??'application/octet-stream');
  }catch(exc){error(res,exc.code==='ENOENT'?404:502,exc.code==='ENOENT'?'not_found':'local_gateway_error','Local service unavailable');}
});
server.requestTimeout=35000;server.headersTimeout=10000;
server.listen(port,container?'0.0.0.0':'127.0.0.1',()=>console.log(`Workbench UI listening at ${origin}`));
for(const signal of ['SIGINT','SIGTERM'])process.on(signal,()=>server.close(()=>process.exit(0)));
