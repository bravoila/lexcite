// Isolated DOM regressions. All API responses are fixtures; no network/model calls.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {JSDOM,VirtualConsole}=require(process.env.JSDOM_MODULE||'jsdom');
const root=path.resolve(__dirname,'..');
const html=fs.readFileSync(path.join(root,'static/index.html'),'utf8');
const script=fs.readFileSync(path.join(root,'static/app.js'),'utf8');
const checks=[];
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function wait(predicate,label){for(let i=0;i<200;i++){if(predicate())return;await pause(5);}throw new Error('Timed out: '+label);}
const response=(data,status=200)=>({ok:status>=200&&status<300,status,json:async()=>structuredClone(data)});
const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return {promise,resolve};};
const caseData=(id='a',title='Case '+id)=>({case:{id,title,event_date:'2025-01-01',jurisdiction:'全国'},messages:[],memories:[],observed_facts:[],active_jobs:[]});
const service=()=>({provider:'none',model:'',base_url:'http://localhost:11434/v1',timeout:90,dimensions:0,revision:'',query_instruction:''});
const model=()=>({profile:'local',provider:'none',model:'fixture',base_url:'http://localhost:11434/v1',timeout:90,max_tokens:4000,review_days:180,agent_rounds:1,agent_timeout:120,thinking_mode:'auto',json_mode:'auto',profiles:{},verify_model:''});
const retrieval=()=>({mode:'lexical',embedding:service(),reranker:service(),auto_index:false,chunk_chars:1200,recall_k:40,rerank_candidates:60,final_k:14});
function harness(override=()=>undefined){
 const errors=[],calls=[],vc=new VirtualConsole();vc.on('jsdomError',error=>errors.push(error.message));
 const dom=new JSDOM(html,{url:'http://localhost',runScripts:'outside-only',pretendToBeVisual:true,virtualConsole:vc}),w=dom.window;
 const nativeTimer=w.setTimeout.bind(w);
 w.setTimeout=(callback,ms,...args)=>nativeTimer(callback,[700,1400,2800,5000,10000].includes(ms)?2:ms,...args);
 w.confirm=()=>true;
 w.HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','');};
 w.HTMLDialogElement.prototype.close=function(){this.removeAttribute('open');this.dispatchEvent(new w.Event('close'));};
 w.fetch=async(url,options={})=>{
  calls.push({url,options});
  const custom=override(url,options,w);
  if(custom!==undefined)return custom;
  if(url==='/api/session')return response({version:w.document.documentElement.dataset.appVersion,initialized:true,authenticated:true});
  if(url==='/api/status')return response({laws:0,articles:0,reviewed:0,model:model(),retrieval:retrieval()});
  if(url==='/api/cases')return response([caseData('a').case,caseData('b').case]);
  if(url==='/api/research')return response([]);
  if(url==='/api/settings')return response(model());
  if(url==='/api/retrieval/settings')return response(retrieval());
  if(url==='/api/retrieval/status')return response({configured:false,mode:'lexical',total:0,ready:0,pending:0,failed:0,job:{},errors:[]});
  if(url==='/api/search/settings')return response({provider:'none',base_url:'https://api.bochaai.com/v1',timeout:10});
  if(/^\/api\/cases\/[ab]$/.test(url))return response(caseData(url.at(-1)));
  if(/\/memories$/.test(url))return response({id:'new-memory'});
  throw new Error('Unexpected fixture request: '+url);
 };
 const $=selector=>w.document.querySelector(selector);
 const click=selector=>$(selector).dispatchEvent(new w.MouseEvent('click',{bubbles:true}));
 const submit=selector=>$(selector).dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
 w.eval(script);
 return {dom,w,$,click,submit,calls,errors,ready:()=>wait(()=>!$('#workspace').hidden,'workspace ready')};
}
async function run(){
 for(const route of ['/api/status','/api/cases','/api/research','/api/cases/a']){
  const h=harness(url=>url===route?response({detail:'Synthetic boot failure'},500):undefined);
  try{
   await wait(()=>h.$('#login-error').textContent.includes('Synthetic boot failure'),'visible boot failure');
   assert(!h.$('#login-screen').hidden);
   assert(h.$('#workspace').hidden);
   assert.deepEqual(h.errors,[]);
  }finally{h.dom.window.close();}
 }
 checks.push('boot-failures-remain-visible');

 let sessions=0;
 const mismatch=harness((url,options,w)=>{
  if(url==='/api/session')return response({version:++sessions===1?w.document.documentElement.dataset.appVersion:'mixed-version',initialized:true,authenticated:false});
  if(url==='/api/login')return response({});
 });
 try{
  await wait(()=>mismatch.$('#login-form').dataset.setup==='false','login form initialized');
  mismatch.$('#password').value='test-only-password';mismatch.submit('#login-form');
  await wait(()=>mismatch.$('#login-error').textContent.includes('版本不一致'),'post-login version mismatch');
  assert(mismatch.$('#login-button').disabled);
  assert(!mismatch.$('#login-screen').hidden&&mismatch.$('#workspace').hidden);
 }finally{mismatch.dom.window.close();}
 checks.push('post-login-version-mismatch-stays-blocked');

 let pending=null;
 const h=harness(url=>pending?.[url]?.promise);
 try{
  await h.ready();
  pending={'/api/cases/b':deferred(),'/api/cases/a':deferred()};
  h.click('[data-case="b"]');h.click('[data-case="a"]');
  assert(h.$('#send').disabled&&h.$('#add-memory').disabled);
  pending['/api/cases/a'].resolve(response(caseData('a','Latest selected case')));
  await wait(()=>h.$('#case-title').textContent==='Latest selected case','latest case applies');
  pending['/api/cases/b'].resolve(response(caseData('b','Stale response')));
  await pause(20);
  assert.equal(h.$('#case-title').textContent,'Latest selected case');
  assert.equal(h.$('#research-view').getAttribute('aria-busy'),'false');
  assert(!h.$('#send').disabled);
  pending=null;
  h.click('#add-memory');h.$('#memory-text').value='Only belongs to case a';h.submit('#memory-form');
  await wait(()=>h.calls.some(call=>call.url==='/api/cases/a/memories')&&!h.$('#memory-dialog').open&&h.$('#case-title').textContent==='Case a'&&!h.$('#send').disabled,'mutation targets selected case');
  assert(!h.calls.some(call=>call.url==='/api/cases/b/memories'));
  assert.deepEqual(h.errors,[]);
 }finally{h.dom.window.close();}
 checks.push('out-of-order-case-response-ignored','mutations-use-current-case');

 for(const failure of ['404','500','timeout','interrupted']){
  let polls=0,recovered=false;
  const h=harness((url,options,w)=>{
   if(url==='/api/cases/a/research')return response({id:'job-a',case_id:'a',status:'running',question:'Keep this question',events:[]},202);
   if(url==='/api/research/job-a'){
    polls++;
    if(recovered)return response({id:'job-a',case_id:'a',status:'completed',question:'Keep this question',events:[]});
    if(failure==='timeout')return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new w.DOMException('Aborted','AbortError')),{once:true}));
    if(failure==='interrupted')return response({id:'job-a',case_id:'a',status:'interrupted',error:'Service restarted',events:[]});
    return response({detail:'Synthetic poll failure'},Number(failure));
   }
  });
  try{
   await h.ready();
   h.$('#question').value='Keep this question';h.submit('#ask-form');
   await wait(()=>polls>0&&!h.$('#send').disabled,'poll failure releases controls');
   assert.equal(h.$('#question').value,'Keep this question');
   assert(!h.$('#question').readOnly&&!h.$('#web-query').disabled&&!h.$('#online-research').disabled&&!h.$('#answer-mode').disabled);
   assert.equal(polls,['500','timeout'].includes(failure)?4:1);
   assert.equal(h.$('#messages .pending-user'),null);
   assert(h.$('#cancel-research').hidden);
   if(['500','timeout'].includes(failure)){
    assert(!h.$('#resume-research').hidden);
    assert.match(h.$('#agent-detail').textContent,/服务端任务可能仍在执行/);
    recovered=true;h.click('#resume-research');
    await wait(()=>polls===5&&!h.$('#send').disabled&&h.$('#resume-research').hidden&&!h.$('#resume-research').disabled,'recover completed job');
    assert(h.$('#resume-research').hidden);
    assert.equal(h.calls.filter(call=>call.url==='/api/cases/a/research').length,1);
   }else{
    assert(h.$('#resume-research').hidden);
   }
   assert.deepEqual(h.errors,[]);
  }finally{h.dom.window.close();}
 }
 checks.push('404-stops-polling','500-bounded-retries','hanging-poll-times-out','terminal-state-unlocks-input','recovery-never-resubmits-question');

 for(const missing of [false,true]){
  let recovery=false,polls=0;
  const pending=deferred();
  const h=harness(url=>{
   if(url==='/api/cases/a/research')return response({id:'job-a',case_id:'a',status:'running',question:'Original question',events:[]},202);
   if(url==='/api/research/job-a'){polls++;return recovery?pending.promise:response({detail:'Synthetic poll failure'},500);}
  });
  try{
   await h.ready();h.$('#question').value='Original question';h.submit('#ask-form');
   await wait(()=>polls===4&&!h.$('#send').disabled,'paused original job');
   h.$('#question').value='A different edited question';recovery=true;h.click('#resume-research');
   assert(h.$('#send').disabled&&h.$('#question').readOnly);
   h.submit('#ask-form');h.click('#resume-research');
   assert.equal(h.calls.filter(call=>call.url==='/api/cases/a/research').length,1);
   assert.equal(polls,5);
   pending.resolve(missing?response({detail:'Job deleted'},404):response({id:'job-a',case_id:'a',status:'completed',question:'Original question',events:[]}));
   await wait(()=>!h.$('#send').disabled&&h.$('#resume-research').hidden&&h.$('#research-view').getAttribute('aria-busy')==='false','recovery terminates');
   if(missing)assert.match(h.$('#agent-detail').textContent,/任务或案件已不存在/);
   assert.equal(h.$('#question').value,'A different edited question');
   assert.deepEqual(h.errors,[]);
  }finally{h.dom.window.close();}
 }
 checks.push('recovery-serializes-with-submission','recovery-404-clears-stale-job','recovery-preserves-edited-question');

 const injection='<img src=x onerror="window.injected=true">';
 const payload={status:injection,summary:'fixture',elapsed_seconds:1,claims:[{text:'fixture',kind:'rule',citations:[injection]}],
  citations:[{label:injection,name:'fixture',number:'第一条',version:'test',jurisdiction:'全国'}],warnings:[],trace:[{step:'fixture',round:injection,detail:'fixture'}],
  retrieval_runs:[{methods:['BM25'],round:1,summary:'fixture',embedding_status:'disabled',reranker_status:'disabled',lexical_candidates:injection,dense_candidates:injection,exact_candidates:injection,reranked_candidates:injection,elapsed_seconds:injection}],
  web_sources:[{url:'javascript:alert(1)',title:'fixture',note:'fixture',article_count:injection}]};
 const config=harness(url=>{
  if(url==='/api/cases/a')return response({...caseData(),messages:[{id:'x',role:'assistant',payload}]});
  if(url==='/api/settings')return response({...model(),configuration_error:'Model endpoint rejected'});
  if(url==='/api/search/settings')return response({provider:'none',base_url:'http://localhost:8080',timeout:10,configuration_error:'Search endpoint rejected'});
  if(url==='/api/retrieval/settings')return response({...retrieval(),configuration_error:'Retrieval endpoint rejected'});
  if(url==='/api/retrieval/status')return response({configured:false,mode:'lexical',total:injection,ready:injection,pending:injection,failed:injection,job:{total:injection,completed:injection},errors:[]});
  if(url==='/api/laws')return response([{id:'" onclick="window.injected=true',name:'fixture',article_count:injection,version:'test',kind:'法律',jurisdiction:'全国',valid_from:'2021-01-01'}]);
 });
 try{
  await config.ready();
  assert.equal(config.$('#messages img'),null);
  assert.match(config.$('.status-chip').textContent,/<img/);
  assert.equal(config.$('.web-lead a'),null);
  config.click('[data-view="laws"]');
  await wait(()=>config.$('#laws-table').textContent.includes('fixture'),'law table rendered');
  assert.equal(config.$('#laws-table img'),null);
  assert.equal(config.$('#laws-table [onclick]'),null);
  config.click('[data-view="settings"]');
  await wait(()=>config.$('#settings-result').textContent==='Model endpoint rejected','invalid model config repairable');
  assert.equal(config.$('#search-result').textContent,'Search endpoint rejected');
  assert.equal(config.$('#retrieval-result').textContent,'Retrieval endpoint rejected');
  assert.equal(config.$('#index-status img'),null);
  assert(!config.$('#settings-form button.primary').disabled);
  assert.deepEqual(config.errors,[]);
 }finally{config.dom.window.close();}
 checks.push('server-labels-counts-and-ids-escaped','unsafe-links-not-rendered','invalid-configurations-remain-editable');
 console.log(JSON.stringify({passed:checks.length,checks,fixture_only:true}));
}
run().catch(error=>{console.error(error);process.exitCode=1;});
