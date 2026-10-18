// DOM-level integration test against a disposable running server, not visual QA.
// LEXCITE_TEST_URL and LEXCITE_TEST_PASSWORD must refer only to a dedicated test instance.
const {JSDOM,VirtualConsole}=require(process.env.JSDOM_MODULE||'jsdom');
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const http=require('node:http');
const https=require('node:https');
const url=process.env.LEXCITE_TEST_URL;
const password=process.env.LEXCITE_TEST_PASSWORD;
if(!url||!password)throw new Error('Set LEXCITE_TEST_URL and LEXCITE_TEST_PASSWORD for a disposable test instance.');
const root=path.resolve(__dirname,'..');
const errors=[];const vc=new VirtualConsole();vc.on('jsdomError',e=>errors.push(e.message));
let cookie='';
const urlResponses=[];
const caseResponses=[];
const dom=new JSDOM(fs.readFileSync(path.join(root,'static/index.html'),'utf8'),{url,runScripts:'outside-only',pretendToBeVisual:true,virtualConsole:vc});
const w=dom.window;
w.fetch=async(input,options={})=>{
 if(input==='/api/extract-url'&&urlResponses.length){const mock=urlResponses.shift();return {ok:mock.status===200,status:mock.status,json:async()=>mock.data};}
 if(/^\/api\/cases\/[^/]+$/.test(input)&&(!options.method||options.method==='GET')&&caseResponses.length){const mock=caseResponses.shift();return {ok:true,status:200,json:async()=>mock};}
 if(options.body instanceof w.FormData){
  const boundary='----lexcite-dom-test';const parts=[];
  for(const [name,value] of options.body.entries()){
   if(typeof value==='string'){parts.push(Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="${name}"\r\n\r\n${value}\r\n`));continue;}
   const data=await new Promise((resolve,reject)=>{const reader=new w.FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=reject;reader.readAsArrayBuffer(value);});
   parts.push(Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="${name}"; filename="${value.name}"\r\nContent-Type: ${value.type||'application/octet-stream'}\r\n\r\n`),Buffer.from(data),Buffer.from('\r\n'));
  }
  parts.push(Buffer.from(`--${boundary}--\r\n`));options.body=Buffer.concat(parts);options.headers={...options.headers,'Content-Type':`multipart/form-data; boundary=${boundary}`};
 }
 return new Promise((resolve,reject)=>{
 const target=new URL(input,url);options.headers={...options.headers,...(cookie?{Cookie:cookie}:{})};
 const {signal,...requestOptions}=options;
 const request=(target.protocol==='https:'?https:http).request(target,requestOptions,r=>{let body='';r.on('data',d=>body+=d);r.on('end',()=>{if(r.headers['set-cookie'])cookie=r.headers['set-cookie'][0].split(';')[0];resolve({ok:r.statusCode<400,status:r.statusCode,json:async()=>JSON.parse(body)});});});
 if(signal){const abort=()=>request.destroy(new w.DOMException('Aborted','AbortError'));if(signal.aborted)abort();else signal.addEventListener('abort',abort,{once:true});request.on('close',()=>signal.removeEventListener('abort',abort));}
 request.on('error',reject);if(options.body)request.write(options.body);request.end();
 });
};
w.confirm=()=>true;
w.HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','');};
w.HTMLDialogElement.prototype.close=function(){this.removeAttribute('open');this.dispatchEvent(new w.Event('close'));};
w.eval(fs.readFileSync(path.join(root,'static/app.js'),'utf8'));
const $=s=>w.document.querySelector(s);
const click=s=>$(s).dispatchEvent(new w.MouseEvent('click',{bubbles:true}));
const submit=s=>$(s).dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
const set=(s,v)=>{$(s).value=v;};
const input=(s,v)=>{set(s,v);$(s).dispatchEvent(new w.Event('input',{bubbles:true}));};
const example=JSON.parse(fs.readFileSync(path.join(root,'examples/civil_excerpt.json'),'utf8'));
const meta=(name=example.name,version=example.version)=>{input('#law-name',name);input('#law-version',version);input('#law-from','2021-01-01');};
const errorBox=()=>$('#import-dialog .dialog-message.error');
async function wait(predicate,label){for(let i=0;i<120;i++){if(predicate())return;await new Promise(r=>setTimeout(r,50));}throw new Error('Timed out: '+label+'; dialog='+($('.dialog-message:not([hidden])')?.textContent||'')+'; toast='+$('#toast').textContent+'; login='+$('#login-error').textContent);}
(async()=>{
 await wait(()=>$('#login-form').dataset.setup==='false','login ready');
 set('#password',password);submit('#login-form');await wait(()=>!$('#workspace').hidden,'login');
 click('[data-view="laws"]');await wait(()=>!$('#laws-empty').hidden,'empty law library');
 click('#example-law');await wait(()=>$('#import-dialog').open,'example opens import');
 click('#preview-law');await wait(()=>!$('#import-submit').disabled,'article preview');
 assert.match($('#law-preview').textContent,/共识别 3 条/);
 submit('#import-form');await wait(()=>$('#review-dialog').open,'law imported');
 $('#review-confirm').checked=true;submit('#review-form');await wait(()=>!$('#review-dialog').open,'law reviewed');
 click('#new-case');set('#case-name-input','DOM测试：合同价款');set('#case-date-input','2025-01-01');submit('#case-form');
 await wait(()=>$('#case-title').textContent==='DOM测试：合同价款','case created');
 click('#add-memory');set('#memory-text','合同付款期限已经届满。');submit('#memory-form');await wait(()=>$('#memory-count').textContent==='1','memory saved');
 set('#question','对方未支付合同价款，可以请求其支付吗？');set('#answer-mode','extract');submit('#ask-form');
 await wait(()=>$('.source-card'),'answer citations');assert.match($('#messages').textContent,/第五百七十九条/);
 click('.source-card');await wait(()=>$('#citation-dialog').open,'citation dialog');assert.match($('#citation-body').textContent,/支付/);assert.match($('#citation-source').href,/gov.cn/);click('#citation-dialog .close');
 click('[data-view="settings"]');await wait(()=>$('#review-days').value==='180','settings loaded');
 assert.equal($('#model-profile').value,'local');assert.equal($('#provider').value,'none');
 set('#model-profile','api');$('#model-profile').dispatchEvent(new w.Event('change'));
 assert.equal($('#provider').value,'dashscope');assert.equal($('#model-name').value,'qwen-plus');
 set('#model-profile','local');$('#model-profile').dispatchEvent(new w.Event('change'));
 assert.equal($('#provider').value,'none');assert.equal($('#model-name').value,'qwen3:8b');
 set('#provider','ollama');$('#provider').dispatchEvent(new w.Event('change'));
 assert.equal($('#model-name').value,'qwen3:8b');
 set('#reranker-provider','dashscope');$('#reranker-provider').dispatchEvent(new w.Event('change'));
 assert.equal($('#reranker-url').value,'https://dashscope.aliyuncs.com/api/v1');
 assert.equal($('#reranker-name').value,'qwen3-rerank');
 assert.match($('#reranker-url-note').textContent,/\/services\/rerank\/text-rerank\/text-rerank/);
 // Do not save or invoke any model endpoint in this test.
 click('[data-view="research"]');assert.match($('#memories').textContent,/期限已经届满/);

 // Malformed text and duplicate import errors stay inside the top-layer dialog.
 click('[data-view="laws"]');click('#import-law');meta();
 assert.equal($('#law-url').required,false);
 input('#law-text','这个文本没有独立行的法条条号，不能导入。');click('#preview-law');
 await wait(()=>errorBox()&&!errorBox().hidden,'parse error inside dialog');
 assert.equal(errorBox().getAttribute('role'),'alert');assert.equal(w.document.activeElement,errorBox());assert.equal($('#toast').hidden,true);
 assert.match(errorBox().textContent,/未识别/);assert.equal($('#import-dialog').open,true);
 input('#law-text',example.text);click('#preview-law');await wait(()=>!$('#import-submit').disabled,'retry preview clears error');
 assert.equal($('#import-dialog .dialog-message').hidden,true);
 submit('#import-form');await wait(()=>errorBox()&&!errorBox().hidden,'duplicate import inside dialog');
 assert.match(errorBox().textContent,/已存在/);assert.equal($('#law-text').value,example.text);
 click('#import-dialog .close');click('#import-law');assert.equal($('#import-dialog .dialog-message').hidden,true);

 // Upload a real multipart file; no source URL is supplied.
 meta('文件导入测试法规','测试版');
 const file=new w.File([example.text],'法规原文.txt',{type:'text/plain'});
 Object.defineProperty($('#law-file'),'files',{value:[file],configurable:true});
 $('#law-file').dispatchEvent(new w.Event('change',{bubbles:true}));
 await wait(()=>$('#law-text').value===example.text&&!$('#import-fields').disabled,'file text extracted');
 assert.equal($('#law-url').value,'');assert.match($('#file-state').textContent,/法规原文.txt/);
 click('#preview-law');await wait(()=>!$('#import-submit').disabled,'file only preview');
 input('#law-version','测试修订版');assert.equal($('#import-submit').disabled,true);assert.equal($('#law-preview').hidden,true);
 click('#preview-law');await wait(()=>!$('#import-submit').disabled,'changed metadata re-preview');
 submit('#import-form');await wait(()=>$('#review-dialog').open,'file only import');
 assert.equal($('#review-source').hidden,true);assert.equal($('#review-source').hasAttribute('href'),false);
 assert.match($('#review-source-note').textContent,/法规原文.txt/);assert.match($('#review-source-note').textContent,/未提供官网链接/);
 $('#review-confirm').checked=true;set('#review-to','2020-01-01');submit('#review-form');
 await wait(()=>$('#review-dialog .dialog-message.error'),'review error inside dialog');
 assert.match($('#review-dialog .dialog-message').textContent,/失效日/);
 set('#review-to','');submit('#review-form');await wait(()=>!$('#review-dialog').open,'file review enabled');
 click('#new-case');set('#case-name-input','文件来源引用');set('#case-date-input','2025-01-01');submit('#case-form');
 await wait(()=>$('#case-title').textContent==='文件来源引用','file citation case');
 set('#question','文件导入测试法规第577条');set('#answer-mode','extract');submit('#ask-form');
 await wait(()=>$('.source-card'),'file sourced citation');click('.source-card');
 assert.equal($('#citation-source').hidden,true);assert.equal($('#citation-source').hasAttribute('href'),false);
 assert.match($('#citation-source-note').textContent,/法规原文.txt/);click('#citation-dialog .close');

 // URL extraction response is mocked at the browser boundary; backend reader has separate fixture tests.
 click('[data-view="laws"]');click('#import-law');
 $('input[name="import-mode"][value="url"]').checked=true;
 $('input[name="import-mode"][value="url"]').dispatchEvent(new w.Event('change',{bubbles:true}));
 assert.equal($('#law-url').required,true);assert.equal($('#document-source').hidden,true);
 input('#law-url','https://example.com/not-official');click('#fetch-law-url');
 await wait(()=>errorBox()&&!errorBox().hidden,'URL validation inside dialog');assert.match(errorBox().textContent,/官网链接仅支持/);
 assert.equal($('#import-fields').disabled,false);
 input('#law-url','https://www.gov.cn/source.html');
 urlResponses.push({status:200,data:{text:example.text,characters:example.text.length,count:3,source_url:'https://www.npc.gov.cn/final.html',source_kind:'url',source_name:'官网网页正文'}});
 click('#fetch-law-url');await wait(()=>$('#law-text').value===example.text&&!$('#import-fields').disabled,'URL body filled');
 assert.equal($('#law-url').value,'https://www.npc.gov.cn/final.html');assert.equal($('#law-file').value,'');
 meta('链接导入测试法规','测试版');click('#preview-law');await wait(()=>!$('#import-submit').disabled,'URL only preview');
 input('#law-url','https://www.gov.cn/different.html');
 urlResponses.push({status:200,data:{text:example.text,characters:example.text.length,count:3,source_url:'https://www.gov.cn/different.html',source_kind:'url',source_name:'官网网页正文'}});
 click('#preview-law');await wait(()=>!$('#import-submit').disabled,'changed URL automatically read');
 submit('#import-form');await wait(()=>$('#review-dialog').open,'URL only import');
 assert.equal($('#review-source').hidden,false);assert.equal($('#review-source').href,'https://www.gov.cn/different.html');click('#review-dialog .close');

 // Rendering fixtures are isolated from persisted answers and real model APIs.
 const caseId=(await (await w.fetch('/api/cases')).json())[0].id;
 const displayedCase=await (await w.fetch('/api/cases/'+caseId)).json();
 const prior=displayedCase.messages.filter(m=>m.role==='assistant').at(-1).payload;
 const citation={...prior.citations[0],label:1};
 const partial={...prior,status:'partial',summary:'以下仅回答现有资料能够支持的部分。',citations:[citation],
  claims:[{text:'合成的现有资料分析结论。',kind:'rule',section:'answer',citations:[1]}],
  source_gaps:[{name:'最高人民法院关于测试争议的规定',articles:['第一条'],reason:'建议核对专项条件 <img src=x onerror=alert(1)>',detail:'资料库未找到匹配的标准法规名称。'}]};
 displayedCase.messages=[{id:'available-materials-fixture',role:'assistant',payload:partial}];
 caseResponses.push(displayedCase);click(`[data-case="${caseId}"]`);
 await wait(()=>$('.available-conclusions'),'available materials analysis');
 assert.match($('.available-conclusions').textContent,/基于现有资料的结论/);
 assert.match($('.source-gaps h3').textContent,/推荐补充资料/);
 assert.match($('.source-gaps').textContent,/建议核对专项条件/);
 assert.match($('.source-gaps').textContent,/不作为本次结论依据/);
 assert($('.available-conclusions').compareDocumentPosition($('.source-gaps'))&w.Node.DOCUMENT_POSITION_FOLLOWING);
 assert.equal($('.source-gaps img'),null);
 click('.available-conclusions .citation-btn');await wait(()=>$('#citation-dialog').open,'available materials citation');
 assert.equal($('#citation-body').textContent,citation.body);click('#citation-dialog .close');
 displayedCase.messages=[{id:'unsupported-materials-fixture',role:'assistant',payload:{...partial,status:'missing_sources',summary:'现有资料尚不足以形成有依据的结论。',claims:[],citations:[]}}];
 caseResponses.push(displayedCase);click(`[data-case="${caseId}"]`);
 await wait(()=>$('#messages').textContent.includes('现有资料尚不足'),'unsupported materials result');
 assert.equal($('.available-conclusions'),null);assert.equal($('.source-card'),null);
 assert.match($('.source-gaps h3').textContent,/推荐补充资料/);
 assert.deepEqual(errors,[]);
 console.log(JSON.stringify({passed:true,checks:['login','import-preview','law-review','case-create','memory-save','ask-extract','citation-dialog','model-preset','view-state','parse-error-in-dialog','duplicate-error-in-dialog','error-focus-and-reset','file-only-import','metadata-preview-invalidation','review-error-in-dialog','file-source-citation','url-error-in-dialog','url-only-import','changed-url-auto-read','available-conclusions-before-recommendations','recommendation-reasons-escaped','available-conclusion-citations','no-fabricated-conclusion'],visual_browser_test:false,url_success_uses_fixture:true}));
 dom.window.close();
})().catch(e=>{console.error(e);dom.window.close();process.exit(1);});
