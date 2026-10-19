"use strict";
(()=>{
const CLIENT_VERSION = "1.4.0";
let versionMismatch=false;
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
if(document.documentElement.dataset.appVersion!==CLIENT_VERSION){
 const error=$('#login-error');if(error)error.textContent='页面与脚本版本不一致，请完整更新 app 和 static 后重启，再刷新网页。';
 const button=$('#login-button');if(button)button.disabled=true;
 const loading=$('#client-load-status');if(loading)loading.hidden=true;
 return;
}
const state = {jobId:null,recoveryJobId:null,caseRequest:0,caseLoading:false, searchSettings:null,caseId:null, case:null, cases:[], view:"research", citations:new Map(), editing:false, lawId:null, previewText:null, busy:false, settings:null, laws:[], importBusy:false, importSaving:false, importAbort:null, importSource:null, sourceEdited:false, loadedURL:null};
const pendingResearchKey="lexcite.pendingResearch.v1";
let pendingResearchFallback=null;
function pendingResearch(){
 try{const value=JSON.parse(sessionStorage.getItem(pendingResearchKey));if(value?.case_id&&value?.request_id&&value?.payload){pendingResearchFallback=value;return value;}return pendingResearchFallback;}
 catch{return pendingResearchFallback;}
}
function savePendingResearch(value){
 pendingResearchFallback=value;
 try{sessionStorage.setItem(pendingResearchKey,JSON.stringify(value));}catch{}
}
function clearPendingResearch(jobId){
 const pending=pendingResearch();
 if(pending?.job_id===jobId){pendingResearchFallback=null;try{sessionStorage.removeItem(pendingResearchKey);}catch{}}
}
function restoreResearchPayload(payload){
 $("#question").value=payload.text||"";$("#web-query").value=payload.web_query||"";
 $("#answer-mode").value=payload.mode||"strict";
 $("#online-research").value=payload.online_research===null||payload.online_research===undefined?"default":payload.online_research?"on":"off";
 fitQuestion();
}
function sameResearchPayload(a,b){
 return ["text","mode","web_query","online_research"].every(key=>(a[key]??null)===(b[key]??null));
}
const esc = x => String(x ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
// Only http(s) URLs are linkable; anything else (javascript:, data:, protocol-relative
// into a different origin…) must degrade to plain text so injected sources cannot
// plant an executable href in the rendered answer.
function safeHref(u){try{const p=new URL(u,location.origin).protocol;return (p==="http:"||p==="https:")?u:null;}catch(e){return null;}}
function cleanLawTitle(value){
 let s=String(value??'').trim();const pairs={'《':'》','〈':'〉'};
 while(pairs[s[0]]&&s.at(-1)===pairs[s[0]]){
  let depth=0,whole=true;const open=s[0],close=s.at(-1);
  for(let i=0;i<s.length;i++){if(s[i]===open)depth++;else if(s[i]===close){depth--;if(!depth&&i!==s.length-1){whole=false;break;}}}
  if(!whole||depth)break;s=s.slice(1,-1).trim();
 }
 return s;
}
const lawTitleText=name=>'《'+cleanLawTitle(name)+'》';
const lawTitle=name=>esc(lawTitleText(name));
let defaultOnlineResearch=false;
const today = () => new Date(Date.now()-new Date().getTimezoneOffset()*60000).toISOString().slice(0,10);
let toastTimer;
function dialogMessage(dialog){let node=dialog.querySelector('.dialog-message');if(!node){node=document.createElement('div');node.className='dialog-message';node.tabIndex=-1;node.hidden=true;dialog.querySelector('.dialog-heading').after(node);}return node;}
function showDialogMessage(dialog,text,error=false){const node=dialogMessage(dialog);node.textContent=text;node.className='dialog-message'+(error?' error':'');node.setAttribute('role',error?'alert':'status');node.hidden=false;$('#toast').hidden=true;if(error){node.focus({preventScroll:true});if(dialog.id!=='import-dialog')node.scrollIntoView?.({block:'nearest'});}}
function clearDialogMessage(dialog){const node=dialog.querySelector('.dialog-message');if(node){node.textContent='';node.hidden=true;}}
function toast(text,error=false){
 clearTimeout(toastTimer);
 const dialog=$$('dialog[open]').at(-1);
 if(dialog){showDialogMessage(dialog,text,error);return;}
 $("#toast").textContent=text;$("#toast").className="toast"+(error?" error":"");$("#toast").hidden=false;toastTimer=setTimeout(()=>$("#toast").hidden=true,error?9000:4500);
}
async function api(path, options={}){
 const headers={"X-Lexcite":"1",...(options.headers||{})};
 if(options.body && !(options.body instanceof FormData)){headers["Content-Type"]="application/json";options.body=JSON.stringify(options.body);}
 const response=await fetch("/api"+path,{...options,headers,credentials:"same-origin"});
 let data;try{data=await response.json();}catch(cause){if(cause.name==='AbortError')throw cause;const error=new Error('服务器未返回可读取的结果，请检查服务状态后重试。');error.status=response.status;throw error;}
 if(!response.ok){if(response.status===401 && !path.includes("login"))location.reload();let detail=data.detail;if(response.status===404&&path==='/extract-url')detail='当前后端缺少链接读取接口，请完整更新 app 和 static 文件夹并重启服务。';if(Array.isArray(detail))detail=detail.map(x=>`${x.loc.slice(1).join(".")}: ${x.msg}`).join("；");const error=new Error(typeof detail==="string"?detail:"操作失败，请稍后重试");error.status=response.status;throw error;}
 return data;
}
const guarded = fn => async event=>{try{await fn(event);}catch(e){if(e.name!=='AbortError')toast(e.message,true);}};
function showDialog(id){clearDialogMessage($(id));$('#toast').hidden=true;$(id).showModal();}
function view(name){state.view=name;["research","laws","settings"].forEach(v=>$("#"+v+"-view").hidden=v!==name);$$('[data-view]').forEach(b=>b.classList.toggle("active",b.dataset.view===name));$("#breadcrumb").innerHTML="工作空间 <span>/</span> "+({research:"案件研究",laws:"法条资料库",settings:"模型与设置"}[name]);$("#sidebar").classList.remove("open");if(name==="laws")loadLaws().catch(e=>toast(e.message,true));if(name==="settings")loadSettings().catch(e=>toast(e.message,true));}
async function status(){const d=await api("/status");defaultOnlineResearch=!!d.model.online_research;updateSourceNote();$("#law-count").textContent=d.laws;$("#stat-laws").textContent=d.laws;$("#stat-articles").textContent=d.articles;$("#stat-reviewed").textContent=d.reviewed;$("#model-state").textContent=d.model.configuration_error?"模型配置需修正":d.model.provider==="none"?"法条检索模式":d.model.model;$("#privacy-state").textContent=d.retrieval&&[d.retrieval.embedding,d.retrieval.reranker].some(s=>s.provider!=="none")?"分析与检索服务分别配置":d.model.provider==="none"?"无需模型 · 原文检索":d.model.provider==="ollama"?"本地模型":"已连接模型服务";const errors=[d.model.configuration_error,d.retrieval?.configuration_error].filter(Boolean);if(errors.length)toast(errors.join('；')+'。请到“模型与设置”修正。',true);}
function renderRetrieval(p){if(!p.retrieval_runs?.length)return '';const names={applied:'已使用',partial:'部分覆盖',failed:'失败并降级',not_configured:'未配置',index_missing:'没有可用索引',disabled:'未启用',temporary_unindexed:'临时条文未建向量索引'};return `<details class="retrieval-proof"><summary>本次实际检索 · ${esc([...new Set(p.retrieval_runs.flatMap(r=>r.methods))].join(' + '))}</summary>${p.retrieval_runs.map(r=>`<p><b>${esc(r.summary)}</b><br>第 ${esc(r.round)} 轮 · 向量：${esc(names[r.embedding_status])} · 重排：${esc(names[r.reranker_status])}<br>关键词候选 ${esc(r.lexical_candidates)} · 语义候选 ${esc(r.dense_candidates)} · 精确匹配 ${esc(r.exact_candidates)} · 重排 ${esc(r.reranked_candidates)} · ${esc(r.elapsed_seconds)} 秒</p>`).join('')}</details>`;}
function renderModelRuns(p){if(!p.model_runs?.length)return '';return `<details class="retrieval-proof model-performance"><summary>模型调用耗时 · ${p.model_runs.length} 次</summary>${p.model_runs.map(r=>`<p><b>第 ${esc(r.round)} 轮 · ${esc(r.stage)}</b><br>${esc(r.model)} · ${esc(r.elapsed_seconds)} 秒 · ${{completed:'完成',failed:'失败',interrupted:'中断'}[r.status]||esc(r.status)}<br>请求数据 ${esc(r.payload_characters)} 字符 · 总 token：${esc(r.usage?.total_tokens??'供应商未提供')}</p>`).join('')}<p class="quiet">字符数仅指请求数据，不是 token 数；用量仅展示供应商返回值，不作估算。</p></details>`;}
async function boot(){const d=await api("/session");if(d.version!==CLIENT_VERSION||document.documentElement.dataset.appVersion!==CLIENT_VERSION){versionMismatch=true;$('#login-button').disabled=true;throw new Error(`页面与后端版本不一致（页面 ${CLIENT_VERSION}，后端 ${d.version||'旧版'}）。请完整更新 app 和 static 后重启，再刷新网页。`);}$('#client-load-status').hidden=true;$("#setup-label").hidden=d.initialized;$("#login-title").textContent=d.initialized?"登录工作空间":"初始化工作空间";$("#login-button").textContent=d.initialized?"登录":"设置密码并进入";$("#login-form").dataset.setup=String(!d.initialized);if(d.authenticated){await Promise.all([status(),loadCases()]);const pending=pendingResearch();if(pending&&state.cases.some(item=>item.id===pending.case_id)){let recovered=null,lookupError=null;try{recovered=await pollResearch(pending.request_id);}catch(error){if(error.status!==404)lookupError=error;}await openCase(pending.case_id);restoreResearchPayload(pending.payload);if(recovered){savePendingResearch({...pending,job_id:recovered.id});if(state.jobId!==recovered.id)await monitorResearch(recovered);}else if(state.jobId){savePendingResearch({...pending,job_id:state.jobId});}else if(lookupError){state.recoveryJobId=pending.request_id;$('#resume-research').hidden=false;$('#agent-current').textContent='研究请求状态暂不可用';$('#agent-detail').textContent='原问题与请求标识已保留；可恢复查看或重新提交完全相同的问题。';}else{$('#working').hidden=false;$('#agent-current').textContent='等待恢复研究请求';$('#agent-detail').textContent='原问题与请求标识已保留；重新提交完全相同的问题会复用此请求。';}}else{const running=await api("/research");if(running.length)await openCase(running[0].case_id);else if(state.cases.length)await openCase(state.cases[0].id);}}$("#login-screen").hidden=d.authenticated;$("#workspace").hidden=!d.authenticated;fitQuestion();}
async function loadCases(){state.cases=await api("/cases");renderCases();}
function renderCases(){$("#case-list").innerHTML=state.cases.length?state.cases.map(c=>`<button class="case-row ${c.id===state.caseId?"active":""}" data-case="${esc(c.id)}" title="${esc(c.title)}">${esc(c.title)}</button>`).join(""):"<p class='quiet'>尚无研究案件</p>";}
function syncCaseControls(){const disabled=state.busy||state.caseLoading;$('#send').disabled=disabled;$('#add-memory').disabled=disabled;$$('#case-actions button').forEach(button=>button.disabled=disabled);}
async function openCase(id){
 if(state.busy){toast("请等待当前研究完成后再切换案件");return;}
 const request=++state.caseRequest;state.caseLoading=true;syncCaseControls();$('#research-view').setAttribute('aria-busy','true');
 try{
  const d=await api("/cases/"+id);if(request!==state.caseRequest)return;
  $("#working").hidden=true;state.recoveryJobId=null;$('#resume-research').hidden=true;state.caseId=id;state.case=d.case;
  $("#case-title").textContent=d.case.title;$("#case-title").title=d.case.title;$("#case-meta").textContent=`基准日期 ${d.case.event_date} · ${d.case.jurisdiction}`;$("#case-actions").hidden=false;state.citations.clear();
  $("#messages").innerHTML=d.messages.length?d.messages.map(m=>renderMessage(m)).join(""):'<div class="welcome"><div class="empty-symbol">§</div><h2>开始本案的法律研究</h2><p>输入事实与问题。已提供的信息会随本案保留；<br>关键事实可在右侧手工确认。</p></div>';
  renderMemories(d.memories);$("#observed-facts").innerHTML=renderFacts(d.observed_facts||[]);renderCases();view("research");fitQuestion();scrollMessages();
  if(d.active_jobs?.length){$("#question").value=d.active_jobs[0].question||"";fitQuestion();monitorResearch(d.active_jobs[0]);}
  else{const pending=pendingResearch();if(pending?.case_id===id)restoreResearchPayload(pending.payload);if(d.last_problem){renderProgress(d.last_problem);$("#cancel-research").hidden=true;$("#agent-current").textContent=d.last_problem.status==="cancelled"?"上次研究已停止":"上次研究未完成";$("#agent-detail").textContent=d.last_problem.error;}}
 }catch(error){if(request===state.caseRequest)throw error;}
 finally{if(request===state.caseRequest){state.caseLoading=false;$('#research-view').setAttribute('aria-busy','false');syncCaseControls();}}
}
function renderMessage(message){
 const p=message.payload;if(message.role==="user")return `<div class="message user">${esc(p.text)}</div>`;
 const key=message.id||String(Date.now());p.citations.forEach(c=>state.citations.set(key+"_"+c.label,c));
 const cite=n=>`<button class="citation-btn" data-citation="${esc(key+"_"+n)}" aria-label="查看引用${esc(n)}">[${esc(n)}]</button>`;
 const label={answered:"已核对引用",partial:"部分结论",evidence_only:"仅供原文核对",insufficient:"依据不足",model_error:"模型调用失败",missing_sources:"缺少必要法源",needs_facts:"尚需案件事实",no_supported_answer:"尚无受支持结论"}[p.status]||p.status;
 return `<article class="message answer"><div class="answer-top"><b>法据</b><span class="status-chip ${p.status==="answered"?"":"warn"}">${esc(label)}</span><span>${esc(p.elapsed_seconds)} 秒</span></div><div class="answer-content"><p class="answer-summary">${esc(p.summary)}</p>${renderClaims(p.claims,cite)}${renderSourceGaps(p)}${p.missing_facts?.length?`<div class="missing"><h3>还需确认</h3><ul>${p.missing_facts.map(x=>`<li>${esc(x)}</li>`).join("")}</ul></div>`:""}${p.unknown_facts?.length?`<div class="unknown-facts"><h3>已标记未知／有待核对</h3>${renderFacts(p.unknown_facts)}<p class="quiet">未知事项保留为待调查信息；涉及这些事项的分析须说明条件。</p></div>`:""}${renderWebSources(p.web_sources||[])}${p.warnings.length?`<div class="warning-list">${p.warnings.map(esc).join("<br>")}</div>`:""}${p.citations.length?`<div class="sources">${p.citations.map(c=>`<button class="source-card" data-citation="${esc(key+"_"+c.label)}"><span>[${esc(c.label)}]</span><span>${lawTitle(c.name)}${esc(c.number)}<small>${esc(c.version)} · ${esc(c.jurisdiction)} · ${c.authority_status==='official_auto_checked'?'官网临时原文 · 未人工复核':'人工复核资料'}</small></span><span class="source-arrow">↗</span></button>`).join("")}</div>`:""}</div>${renderRetrieval(p)}${renderModelRuns(p)}${renderReasoning(p)}${p.case_facts?.length?`<details class="fact-snapshot"><summary>本轮采用的用户信息 · ${p.case_facts.length} 项</summary><p class="quiet">沿用问题：${esc(p.research_focus)}</p>${renderFacts(p.case_facts)}</details>`:""}<details class="trace"><summary>查看检索与校验过程 · ${esc(p.model||"原文模式")}</summary><ol class="agent-history">${p.trace.map(t=>`<li><b>${esc(t.step)}</b>${t.round?` · 第 ${esc(t.round)} 轮`:""}<p>${esc(typeof t.detail==="string"?t.detail:JSON.stringify(t.detail))}</p></li>`).join("")}</ol>${p.candidate_sources?.length?`<details class="candidate-details"><summary>查看检索候选（不代表结论依据）</summary><ul>${p.candidate_sources.map(r=>`<li>${lawTitle(r.name)}${esc(r.number)}</li>`).join('')}</ul></details>`:''}<p>${esc(p.notice)}</p><p>引用原文匹配与模型复核，不等于法律适用已经正确。</p></details></article>`;
}
function renderClaims(claims,cite){if(!claims.length)return '';const titles={answer:'对本问题的回答',rule:'规则与例外',application:'结合本案事实',limitation:'适用限制'};return `<section class="available-conclusions"><h3>基于现有资料的结论</h3>${Object.entries(titles).map(([section,title])=>{const rows=claims.filter(c=>(c.section||'rule')===section);return rows.length?`<section class="answer-section"><h4>${title}</h4>${rows.map(c=>`<div class="claim"><span class="claim-type">${c.kind==='inference'?'基于事实的条件分析':'法律规则'}</span>${esc(c.text)} ${c.citations.map(cite).join('')}</div>`).join('')}</section>`:'';}).join('')}</section>`;}
function renderSourceGaps(p){
 const gaps=p.source_gaps||[],issues=p.legal_issues||[];if(!gaps.length&&!issues.length)return '';
 return `<div class="source-gaps"><h3>推荐补充资料</h3><p class="quiet">以下为研究目录或模型提出、经本次资料检查后的补充建议，不作为本次结论依据。名称、条号及适用性仍需核对；未匹配也可能是名称或版本问题，不代表文件一定不存在。</p>${gaps.map(g=>`<div><b>${lawTitle(g.name)}${esc((g.articles||[]).join('、'))}</b>${g.reason&&g.detail&&g.reason!==g.detail?`<p><strong>建议补充原因：</strong>${esc(g.reason)}</p>`:''}<p>${esc(g.detail||g.reason)}</p>${g.online_detail?`<p class="online-source-error"><strong>联网补查：</strong>${esc(g.online_detail)}</p>`:''}</div>`).join('')}${issues.length?`<ul>${issues.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:''}${gaps.some(g=>g.online_state==='search_not_configured')?'<button class="secondary" data-action="search-settings">配置联网搜索</button>':''}<p class="quiet">${p.online_research?'本次已开启官网补查，具体读取结果见“官网来源与线索”。补充原文核对后，可能完善或改变现有判断。':' 如需进一步完善分析，可开启“联网补齐官方法源”后重试，或导入推荐文件并复核。'}</p></div>`;
}
const factLabels={stated:'已提供',unknown:'明确未知',conflicting:'存在矛盾'};
function renderFacts(facts){return facts.length?facts.map(f=>`<div class="observed-fact"><span class="fact-state ${esc(f.state)}">${factLabels[f.state]||'用户陈述'}</span><span>${esc(f.text)}</span><small>${f.source_kind==='confirmed'?'手工确认事实':'用户原话'}${f.conflicting_quotes?': '+f.conflicting_quotes.map(esc).join(' / '):''}</small></div>`).join(''):'<p class="quiet">本案暂无可整理的事实陈述。</p>';}
function reasoningStatus(item){return {running:'正在接收',available:'接口已返回',unavailable:'本次接口未返回思考字段',failed:'调用失败，内容可能不完整',interrupted:'调用已中断，内容可能不完整'}[item.status]||item.status;}
function reasoningEmpty(item){return item?.status==='running'?'等待模型返回思考内容…':item?'本次模型接口未返回可展示的思考内容。':'尚未收到思考内容；仅检索模式或关闭展示时不会收集。';}
function renderReasoning(p){if(!('reasoning' in p))return '';return `<details class="reasoning-history"><summary>查看模型思考 · ${p.reasoning?.length||0} 次调用</summary><p class="reasoning-notice">以下为所选模型接口实际返回的内容，未经引用校验，可能有错误或不完整；不属于已核对的结论。</p>${!p.reasoning_enabled?'<p>本轮已关闭思考内容的展示与保存。</p>':!p.reasoning?.length?'<p>本轮未收到模型思考内容。</p>':p.reasoning.map(item=>`<details class="reasoning-entry"><summary>第 ${esc(item.round)} 轮 · ${esc(item.stage)} · ${esc(item.model)} · ${esc(reasoningStatus(item))}</summary><pre class="reasoning-text">${esc(item.text||reasoningEmpty(item))}</pre>${item.truncated?'<p class="quiet">已达到保存上限，此处内容有截断。</p>':''}</details>`).join('')}</details>`;}
let liveReasoning=[],reasoningJob=null,selectedReasoning=null;
function renderLiveReasoning(job){
 if(reasoningJob!==job.id){reasoningJob=job.id;selectedReasoning=null;$('#reasoning-call').innerHTML='';}
 liveReasoning=job.reasoning||[];const select=$('#reasoning-call');
 for(const item of liveReasoning){let option=[...select.options].find(o=>o.value===String(item.call));if(!option){option=document.createElement('option');option.value=String(item.call);select.append(option);}option.textContent=`第 ${item.round} 轮 · ${item.stage} · ${item.model}`;}
 select.hidden=!liveReasoning.length;select.value=String(selectedReasoning||liveReasoning.at(-1)?.call||'');updateReasoningText();
}
function updateReasoningText(){const item=liveReasoning.find(x=>String(x.call)===$('#reasoning-call').value);const pre=$('#reasoning-text'),bottom=pre.scrollHeight-pre.scrollTop-pre.clientHeight<35;const text=item?.text||reasoningEmpty(item);if(pre.textContent!==text){pre.textContent=text;if(bottom)pre.scrollTop=pre.scrollHeight;}$('#reasoning-status').textContent=item?reasoningStatus(item)+(item.truncated?' · 达到保存上限，内容已截断':''):'';}
$('#reasoning-call').onchange=()=>{selectedReasoning=Number($('#reasoning-call').value);updateReasoningText();};
function selectLivePane(name){$$('[data-live-pane]').forEach(b=>{const active=b.dataset.livePane===name;b.setAttribute('aria-selected',String(active));b.tabIndex=active?0:-1;});$('#live-reasoning').hidden=name!=='reasoning';$('#live-steps').hidden=name!=='steps';}
$$('[data-live-pane]').forEach(b=>{b.onclick=()=>selectLivePane(b.dataset.livePane);b.onkeydown=e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();const name=e.key==='Home'?'reasoning':e.key==='End'?'steps':b.dataset.livePane==='reasoning'?'steps':'reasoning';selectLivePane(name);$('#live-tab-'+name).focus();}};});
function scrollMessages(){const m=$("#messages");m.scrollTop=m.scrollHeight;}
function renderMemories(items){$("#memory-count").textContent=items.length;$("#memories").innerHTML=items.map(m=>`<div class="memory-item">${esc(m.text)}<button class="text-button" data-delete-memory="${esc(m.id)}">移除此事实</button></div>`).join("");}
function newCase(edit=false){state.editing=edit;$("#case-dialog-title").textContent=edit?"修改案件信息":"新建研究案件";$("#case-name-input").value=edit?state.case.title:"";$("#case-date-input").value=edit?state.case.event_date:today();$("#case-region-input").value=edit?state.case.jurisdiction:"全国";showDialog("#case-dialog");}
function renderSource(prefix,source){const link=$('#'+prefix+'-source');const href=safeHref(source.source_url);link.hidden=!href;if(href)link.href=href;else link.removeAttribute('href');$('#'+prefix+'-source-note').textContent=`正文来源：${source.source_name||'导入正文'}。${source.source_url?'官网链接供核对；正文和版本仍需人工确认。':'未提供官网链接，请由导入人对照权威原文核实来源、完整性和有效期。'}`;}
function showCitation(key){const c=state.citations.get(key);if(!c)return;$("#citation-title").textContent=`${lawTitleText(c.name)}${c.number}`;$("#citation-meta").textContent=`${c.version} · ${c.jurisdiction} · 生效 ${c.valid_from} · 失效 ${c.valid_to||"未录入"} · 复核 ${(c.reviewed_at||"").slice(0,10)}`;$("#citation-quotes").innerHTML=c.quotes?.length?"<h3>回答引用片段</h3>"+c.quotes.map(q=>`<div class="quote">${esc(q)}</div>`).join(""):"";$("#citation-body").textContent=c.body;renderSource('citation',c);if(c.authority_status==='official_auto_checked'){$('#citation-meta').textContent+=` · 官网临时原文 · 读取于 ${c.fetched_at}`;$('#citation-source-note').textContent=(c.source_checks||[]).join('；')+'。'+(c.version_note||'未人工复核')+(c.temporal_provisions?.length?' 过渡与施行条款：'+c.temporal_provisions.join('；'):'');}$("#citation-hash").textContent="源文本 SHA256: "+c.sha256;if(c.law_id&&c.authority_status!=='official_auto_checked'){const b=document.createElement('button');b.className='secondary';b.textContent='重算校验';b.onclick=guarded(async()=>{b.disabled=true;try{const r=await api('/laws/'+c.law_id+'/hash-check');$("#citation-hash").textContent=`源文本 SHA256: ${r.sha256} · 存档重算${r.ok?'一致':'不一致，请重新导入原文'}`+(r.snapshot_missing?'（无原始快照）':'');}catch(e){$("#citation-hash").textContent='重算校验失败：'+e.message;}finally{b.disabled=false;}});$("#citation-hash").append(' ',b);}if(c.version_basis){const basis=c.version_basis;const href=safeHref(basis.url);if(href){const a=document.createElement('a');a.href=href;a.target='_blank';a.rel='noopener noreferrer';a.textContent='核对修正案施行日期 ↗';$('#citation-source-note').append(document.createElement('br'),a,document.createTextNode(' · '+basis.effective_from+' · SHA256 '+basis.sha256));}else{$('#citation-source-note').append(document.createElement('br'),document.createTextNode('修正案施行依据 · 生效 '+basis.effective_from+' · SHA256 '+basis.sha256));}}showDialog("#citation-dialog");}
async function loadLaws(){state.laws=await api("/laws");const settings=await api("/settings");$("#laws-empty").hidden=state.laws.length>0;$("#laws-table").innerHTML=state.laws.map(l=>{const fresh=l.reviewed_at&&(Date.now()-new Date(l.reviewed_at).getTime())/86400000<=settings.review_days;return `<tr><td><b>${esc(l.canonical_name||l.name)}</b>${l.canonical_name&&l.canonical_name!==l.name?`<small>原名称：${esc(l.name)}</small>`:""}<small>${esc(l.version)} · ${esc(l.kind)}</small></td><td>${esc(l.jurisdiction)}</td><td>${esc(l.valid_from)}<small>至 ${esc(l.valid_to||"未录入失效日")}</small></td><td><span class="status-chip ${fresh?"":"warn"}">${fresh?"已复核":l.reviewed_at?"需重新复核":"待复核"}</span><small>${esc((l.reviewed_at||"").slice(0,10))}</small></td><td>${esc(l.article_count)}</td><td><button class="text-button" data-review="${esc(l.id)}">查看 / 复核</button><button class="text-button danger" data-delete-law="${esc(l.id)}">删除</button></td></tr>`;}).join("");await status();}
const FILE_HINT='TXT、Markdown、文字型 PDF、DOCX · 最大20MB';
function importMode(){return $('input[name="import-mode"]:checked').value;}
function invalidatePreview(){state.previewText=null;$('#import-submit').disabled=true;$('#law-preview').hidden=true;}
function syncImportMode(){const url=importMode()==='url';$('#document-source').hidden=url;$('#law-file').disabled=url;$('#url-source').hidden=!url;$('#fetch-law-url').hidden=!url;$('#law-url').required=url;$('#law-text').required=!url;$('#law-url-label').textContent=url?'官网原文链接（必填）':'官网原文链接（选填，便于追溯）';$('#preview-law').textContent=url?'读取链接并预览条文':'预览条文拆分';invalidatePreview();}
function setImportBusy(busy,saving=false){state.importBusy=busy;state.importSaving=busy&&saving;$('#import-fields').disabled=busy;$('#import-submit').disabled=busy||!state.previewText;$$('#import-dialog .close').forEach(b=>b.disabled=state.importSaving);$('#import-progress').textContent=busy?(saving?'正在保存法规…':'正在处理，可取消后重试…'):'导入后仍需复核启用';}
function cancelImport(){if(state.importSaving)return;state.importAbort?.abort();state.importAbort=null;setImportBusy(false);}
async function importJob(fn,saving=false){if(state.importBusy)return;clearDialogMessage($('#import-dialog'));const controller=new AbortController();state.importAbort=controller;setImportBusy(true,saving);try{await fn(controller.signal);}catch(e){if(!controller.signal.aborted)showDialogMessage($('#import-dialog'),e.message,true);}finally{if(state.importAbort===controller){state.importAbort=null;setImportBusy(false);}}}
function openImport(value={}){cancelImport();$("#import-form").reset();$("#law-name").value=value.name||"";$("#law-canonical").value=value.canonical_name||"";$("#law-document-number").value=value.document_number||"";$("#law-version").value=value.version||"";$("#law-kind").value=value.kind||'法律';$("#law-region").value=value.jurisdiction||"全国";$("#law-from").value=value.valid_from||"";$("#law-to").value=value.valid_to||"";$("#law-url").value=value.source_url||"";$("#law-text").value=value.text||"";$("#file-state").textContent=FILE_HINT;$('#law-source-status').textContent='';state.importSource={source_kind:value.source_kind||'paste',source_name:value.source_name||'粘贴正文'};state.sourceEdited=false;state.loadedURL=null;syncImportMode();showDialog("#import-dialog");}
function lawPayload(){const source=state.importSource||{source_kind:'paste',source_name:'粘贴正文'};return {canonical_name:$("#law-canonical").value.trim(),document_number:$("#law-document-number").value.trim(),name:$("#law-name").value.trim(),version:$("#law-version").value.trim(),jurisdiction:$("#law-region").value.trim(),kind:$("#law-kind").value,source_url:$("#law-url").value.trim(),source_kind:source.source_kind,source_name:source.source_name.slice(0,235)+(state.sourceEdited?'（已编辑正文）':''),valid_from:$("#law-from").value,valid_to:$("#law-to").value||null,text:$("#law-text").value};}
async function reviewLaw(id){const d=await api("/laws/"+id);state.lawId=id;$("#review-canonical").value=d.law.canonical_name||d.law.name;$("#review-document-number").value=d.law.document_number||"";$("#review-title").textContent=d.law.name;$("#review-version").textContent=d.law.version+" · "+d.articles.length+" 条";renderSource('review',d.law);$("#review-from").value=d.law.valid_from;$("#review-to").value=d.law.valid_to||"";$("#review-confirm").checked=false;$("#review-articles").innerHTML=d.articles.map(a=>`<div class="article-preview"><b>${esc(a.number)}</b>${esc(a.body)}</div>`).join("");showDialog("#review-dialog");}
const presets={none:["http://host.docker.internal:11434/v1","qwen3:8b"],ollama:["http://host.docker.internal:11434/v1","qwen3:8b"],dashscope:["https://dashscope.aliyuncs.com/compatible-mode/v1","qwen-plus"],deepseek:["https://api.deepseek.com","deepseek-flash"],siliconflow:["https://api.siliconflow.cn/v1","Qwen/Qwen3-8B"],custom:["http://host.docker.internal:8000/v1","Qwen/Qwen3-8B"]};
function applySearchSettings(search){state.searchSettings=search;$("#search-provider").value=search.provider;$("#search-base").value=search.base_url;$("#search-key").value="";$("#search-timeout").value=search.timeout;$("#search-key-state").textContent=search.environment_api_key?"已从服务器 .env 读取与当前服务和地址绑定的搜索密钥；页面不会获取明文。":search.has_api_key?"已保存搜索密钥；相同服务和地址下留空保留。":search.environment_defaults?"其余默认参数来自服务器 .env；页面已保存参数优先。":"博查须填写；自建 SearXNG 可留空。";$('#search-result').textContent=search.configuration_error||'';}
async function loadSettings(includeSearch=true){if(includeSearch)await loadRetrievalSettings();const s=await api("/settings");if(includeSearch)applySearchSettings(await api("/search/settings"));state.settings=s;$("#default-online-research").checked=!!s.online_research;defaultOnlineResearch=!!s.online_research;updateSourceNote();$("#thinking-mode").value=s.thinking_mode;$("#show-reasoning").checked=s.show_reasoning!==false;$("#json-mode").value=s.json_mode||"auto";$("#agent-rounds").value=s.agent_rounds;$("#agent-timeout").value=s.agent_timeout;$("#model-profile").value=s.profile;$("#provider").value=s.provider;$("#base-url").value=s.base_url;$("#model-name").value=s.model;$("#verify-model").value=s.verify_model;$("#api-key").value="";$("#model-timeout").value=s.timeout;$("#max-tokens").value=s.max_tokens;$("#review-days").value=s.review_days;$("#key-state").textContent=s.environment_api_key?"已从服务器 .env 读取与当前服务和地址绑定的模型密钥；页面不会获取明文。":s.has_api_key?"已保存密钥；同一服务和地址留空保留。密钥不会返回到浏览器。":"当前运行方式未配置密钥；本地 Ollama 可留空。";$("#settings-result").textContent=s.configuration_error||(s.environment_defaults?"当前使用服务器 .env 中尚未被页面设置覆盖的默认值；保存页面配置后将优先使用页面值。":"");}

$("#login-form").addEventListener("submit",async e=>{e.preventDefault();const button=$("#login-button");button.disabled=true;$("#login-error").textContent="";try{const body={password:$("#password").value,setup_code:$("#setup-code").value};if(e.target.dataset.setup==="true")await api("/setup",{method:"POST",body});await api("/login",{method:"POST",body});$("#password").value="";await boot();}catch(err){$("#login-error").textContent=err.message;}finally{button.disabled=versionMismatch;}});
$("#logout").onclick=guarded(async()=>{await api("/logout",{method:"POST"});location.reload();});
$$('[data-view]').forEach(b=>b.onclick=()=>view(b.dataset.view));
document.addEventListener("click",guarded(async e=>{const b=e.target.closest("button");if(!b)return;if(b.classList.contains("close"))b.closest("dialog").close();if(b.dataset.case)await openCase(b.dataset.case);if(b.dataset.citation)showCitation(b.dataset.citation);if(b.dataset.importUrl){openImport({source_url:b.dataset.importUrl});$('input[name="import-mode"][value="url"]').checked=true;syncImportMode();await previewImport(true);}if(b.dataset.review)await reviewLaw(b.dataset.review);if(b.dataset.action){if(b.dataset.action==="case")newCase();else if(b.dataset.action==="search-settings"){view("settings");$("#search-settings-form").scrollIntoView({block:"start"});$("#search-provider").focus();}else view(b.dataset.action);}if(b.dataset.deleteMemory&&confirm("移除此条已确认事实？此操作不会删除历史对话。")){await api(`/cases/${state.caseId}/memories/${b.dataset.deleteMemory}`,{method:"DELETE"});await openCase(state.caseId);}if(b.dataset.deleteLaw&&confirm("删除此法规版本？历史回答仍保留引用快照，新回答不再使用该资料。")){await api("/laws/"+b.dataset.deleteLaw,{method:"DELETE"});await loadLaws();}}));
$("#menu-toggle").onclick=()=>$("#sidebar").classList.toggle("open");
$("#new-case").onclick=()=>newCase();$("#edit-case").onclick=()=>newCase(true);
$("#case-form").onsubmit=guarded(async e=>{e.preventDefault();const body={title:$("#case-name-input").value.trim(),event_date:$("#case-date-input").value,jurisdiction:$("#case-region-input").value.trim()};const result=await api(state.editing?"/cases/"+state.caseId:"/cases",{method:state.editing?"PATCH":"POST",body});$("#case-dialog").close();await loadCases();await openCase(state.editing?state.caseId:result.id);});
$("#delete-case").onclick=guarded(async()=>{if(!confirm("永久删除本案对话和已确认事实？"))return;await api("/cases/"+state.caseId,{method:"DELETE"});location.reload();});
$("#export-case").onclick=()=>{if(state.caseId)location.href="/api/cases/"+state.caseId+"/export";};
$("#add-memory").onclick=()=>{if(!state.caseId){newCase();return;}$("#memory-text").value="";showDialog("#memory-dialog");};
$("#memory-form").onsubmit=guarded(async e=>{e.preventDefault();await api(`/cases/${state.caseId}/memories`,{method:"POST",body:{text:$("#memory-text").value.trim()}});$("#memory-dialog").close();await openCase(state.caseId);toast("事实已保存，仅用于当前案件");});
function renderWebSources(rows){return rows.length?`<details class="web-leads"><summary>官网来源与线索 · ${rows.length} 项</summary><p class="quiet">只有标为“临时原文”的来源通过了本次自动检查；搜索摘要和读取失败的页面均不能作为法条依据。</p>${rows.map(r=>{const href=safeHref(r.url);return `<div class="web-lead"><span class="fact-state">${r.state==='temporary_evidence'?'临时原文 · 未人工复核':'线索 · 不作为法条依据'}</span>${href?`<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(r.title)} ↗</a>`:`<b>${esc(r.title)}</b>`}<p>${esc(r.note)}</p>${r.article_count?`<small>识别 ${esc(r.article_count)} 条 · ${esc(r.fetched_at)}</small>`:""}<details><summary>${r.previews?.length?'查看已读取的条文示例':'查看搜索摘要（非法律依据）'}</summary>${r.previews?.length?r.previews.map(a=>`<p>${esc(a.number)} ${esc(a.body)}</p>`).join(''):`<p>${esc(r.snippet||'无摘要')}</p>`}</details>${href?`<button class="secondary" data-import-url="${esc(href)}">读取并导入复核</button>`:''}</div>`}).join('')}</details>`:'';}
function renderProgress(job){
 $('#working').hidden=false;renderLiveReasoning(job);const events=job.events||[];const last=events.at(-1);
 $('#agent-current').textContent=job.status==='cancel_requested'?'正在停止研究':last?`${last.round?'第 '+last.round+' 轮 · ':''}${last.step}`:'研究任务已受理';
 $('#agent-detail').textContent=job.status==='cancel_requested'?'将在当前请求返回或超时后停止；不会继续发起新的模型调用。':last?.detail||'正在读取本案事实与研究设置。';
 $('#agent-steps').innerHTML=events.map(t=>`<li class="${esc(t.status)}"><span>${t.status==='completed'?'✓':t.status==='warning'?'!':t.status==='skipped'?'—':'·'}</span><div><b>${esc(t.step)}${t.round?` · 第 ${esc(t.round)} 轮`:''}</b><small>${esc(t.detail)}</small></div><time>${esc(t.elapsed_seconds)}s</time></li>`).join('');
 const list=$('#agent-steps');list.scrollTop=list.scrollHeight;$('#cancel-research').disabled=job.status==='cancel_requested';
}
function researchBusy(busy){state.busy=busy;$('#question').readOnly=busy;for(const id of ['web-query','online-research','answer-mode','resume-research'])$('#'+id).disabled=busy;syncCaseControls();}
async function pollResearch(id){
 const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),10000);
 try{return await api('/research/'+id,{signal:controller.signal});}
 catch(error){if(error.name==='AbortError')throw new Error('读取研究进度超时');throw error;}
 finally{clearTimeout(timer);}
}
async function monitorResearch(initial){
 if(state.busy&&state.jobId===initial.id)return;
 if(state.busy&&state.jobId)throw new Error('已有其他研究正在查看，请先停止或等待完成。');
 researchBusy(true);state.jobId=initial.id;state.recoveryJobId=null;$('#resume-research').hidden=true;$('#cancel-research').hidden=false;
 const messages=$('#messages');messages.querySelector('.welcome')?.remove();if(!messages.querySelector('.pending-user')){const pending=document.createElement('div');pending.className='message user pending-user';pending.textContent=initial.question||$('#question').value;messages.append(pending);scrollMessages();}
 let job=initial,failures=0;
 try{
  renderProgress(job);
  while(['running','cancel_requested'].includes(job.status)){
   await new Promise(resolve=>setTimeout(resolve,Math.min(700*2**failures,5000)));
   try{job=await pollResearch(job.id);failures=0;renderProgress(job);}
   catch(error){
    failures++;if((error.status>=400&&error.status<500&&![408,429].includes(error.status))||failures>=4)throw error;
    $('#agent-detail').textContent=`进度连接暂时中断，正在重试（${failures}/4）：${error.message}`;
   }
  }
  researchBusy(false);$('#cancel-research').hidden=true;
  if(job.status==='completed'){clearPendingResearch(job.id);if($('#question').value.trim()===(job.question||initial.question||'').trim())$('#question').value='';fitQuestion();await openCase(job.case_id);$('#working details').open=false;}
  else{clearPendingResearch(job.id);$('#messages .pending-user')?.remove();$('#agent-current').textContent=job.status==='cancelled'?'研究已停止':job.status==='interrupted'?'研究被服务重启中断':'研究未完成';$('#agent-detail').textContent=job.error||'请重新提问';}
 }catch(error){
  $('#messages .pending-user')?.remove();$('#working').hidden=false;
  $('#agent-current').textContent=job.status==='completed'?'研究已完成，记录暂未加载':'进度读取已暂停';
  const missing=error.status===404;state.recoveryJobId=missing?null:initial.id;$('#resume-research').hidden=missing;
  $('#agent-detail').textContent=(missing?'任务或案件已不存在，已解除等待。请检查历史记录后重新提问。':'服务端任务可能仍在执行。原问题已保留，可恢复查看；不要重复提交。')+' '+error.message;
  toast(error.message,true);
 }finally{if(state.jobId===initial.id){state.jobId=null;researchBusy(false);$('#cancel-research').hidden=true;}}
}
$('#resume-research').onclick=guarded(async()=>{
 const id=state.recoveryJobId,cid=state.caseId;if(!id||state.busy||state.caseLoading)return;
 researchBusy(true);
 try{const job=await pollResearch(id),pending=pendingResearch();if(pending?.request_id===id)savePendingResearch({...pending,job_id:job.id});if(state.caseId===cid&&!state.caseLoading)await monitorResearch(job);}
 catch(error){if(error.status===404){state.recoveryJobId=null;$('#resume-research').hidden=true;$('#agent-current').textContent='任务已不存在';$('#agent-detail').textContent='任务或案件已不存在，已解除等待。原问题已保留，请检查历史记录后重新提问。';}throw error;}
 finally{if(!state.jobId)researchBusy(false);}
});
$('#cancel-research').onclick=guarded(async()=>{if(state.jobId)renderProgress(await api('/research/'+state.jobId+'/cancel',{method:'POST'}));});
$('#ask-form').onsubmit=async e=>{
 e.preventDefault();if(state.busy||state.caseLoading)return;if(!state.caseId){newCase();return;}
 const text=$('#question').value.trim();if(!text)return;
 const payload={text,mode:$('#answer-mode').value,web_query:$('#web-query').value.trim(),online_research:$('#online-research').value==='default'?null:$('#online-research').value==='on'};
 const prior=pendingResearch();
 if(prior&&(prior.case_id!==state.caseId||!sameResearchPayload(prior.payload,payload))){toast('上次请求仍未确认完成。请先恢复或重新提交完全相同的问题，避免重复研究。',true);return;}
 const request_id=prior?.request_id||globalThis.crypto?.randomUUID?.()||String(Date.now())+Math.random().toString(16).slice(2);
 savePendingResearch({case_id:state.caseId,request_id,payload,job_id:prior?.job_id||null});
 researchBusy(true);$('#working details').open=true;
 const cid=state.caseId;
 try{const job=await api(`/cases/${cid}/research`,{method:'POST',body:{...payload,request_id}});savePendingResearch({case_id:cid,request_id,payload,job_id:job.id});await monitorResearch(job);}
 catch(err){researchBusy(false);toast(err.message,true);try{const job=await pollResearch(request_id);savePendingResearch({case_id:cid,request_id,payload,job_id:job.id});if(state.caseId===cid&&!state.caseLoading)await monitorResearch(job);}catch(recoveryError){if(recoveryError.status!==404){state.recoveryJobId=request_id;$('#resume-research').hidden=false;$('#agent-current').textContent='研究状态暂不可用';$('#agent-detail').textContent='服务端可能已接受请求；原问题和请求标识已保留，可恢复查看或重新提交完全相同的问题。';}else{state.recoveryJobId=null;$('#resume-research').hidden=true;$('#agent-current').textContent='研究请求未能确认';$('#agent-detail').textContent='原问题和请求标识已保留；重新提交完全相同的问题会安全地复用此请求。';}}}
};
function updateSourceNote(){
 const selected=$('#online-research').value;
 $('#online-default-note').textContent=selected==='on'?'本次补查官方全文；临时来源会标明未人工复核。':selected==='off'?'本次仅使用本地已复核法库。':defaultOnlineResearch?'默认联网补齐官方法源，可在上方切换。':'默认仅使用本地已复核法库，可在上方切换。';
}
function fitQuestion(){
 const input=$('#question');
 if(!input.getClientRects().length)return;
 const cap=Number.parseFloat(getComputedStyle(input).maxHeight)||112;
 input.style.height='auto';
 input.style.height=Math.min(Math.max(input.scrollHeight,36),cap)+'px';
 input.style.overflowY=input.scrollHeight>cap?'auto':'hidden';
}
$('#question').addEventListener('input',fitQuestion);
$('#online-research').addEventListener('change',updateSourceNote);
window.addEventListener('resize',fitQuestion);
if(typeof ResizeObserver!=='undefined'){
 let lastWidth=0;
 const observer=new ResizeObserver(entries=>{const width=entries[0].contentRect.width;if(width!==lastWidth){lastWidth=width;fitQuestion();}});
 observer.observe($('#ask-form'));
}
$("#question").addEventListener("keydown",e=>{if((e.ctrlKey||e.metaKey)&&e.key==="Enter")$("#ask-form").requestSubmit();});
$("#import-law").onclick=()=>openImport();
$("#example-law").onclick=guarded(async()=>{openImport(await api("/example"));toast("演示资料仅含3条，不能覆盖完整问题；导入后请复核。");});
$("#official-law").onclick=guarded(async()=>{const b=$("#official-law");b.disabled=true;b.textContent="正在读取…";try{openImport(await api("/official/civil-code",{method:"POST"}));}finally{b.disabled=false;b.textContent="读取民法典";}});
$$('input[name="import-mode"]').forEach(input=>input.onchange=()=>{syncImportMode();clearDialogMessage($('#import-dialog'));});
$('#import-dialog').addEventListener('cancel',e=>{if(state.importSaving)e.preventDefault();else cancelImport();});
$('#import-dialog').addEventListener('close',cancelImport);
$('#import-fields').addEventListener('input',e=>{invalidatePreview();if(e.target.id==='law-text'&&state.importSource?.source_kind!=='paste')state.sourceEdited=true;});
$('#import-fields').addEventListener('change',invalidatePreview);
$("#law-file").onchange=guarded(async()=>{const file=$("#law-file").files[0];if(!file)return;invalidatePreview();await importJob(async signal=>{if(file.size>20_000_000)throw new Error('文件最大20MB，请按单部法规拆分。');$("#file-state").textContent="正在提取文字…";const form=new FormData();form.append("file",file);try{const d=await api("/extract",{method:"POST",body:form,signal});if(signal.aborted)return;$("#law-text").value=d.text;state.importSource={source_kind:'file',source_name:d.source_name};state.sourceEdited=false;state.loadedURL=null;$("#file-state").textContent=`${d.source_name} · ${d.characters.toLocaleString()} 字符`;$('#law-source-status').textContent='文件正文已读取。请核对法规信息并预览；官网链接可以留空。';}catch(e){if(!signal.aborted)$("#file-state").textContent="提取失败，请尝试转换为TXT";throw e;}finally{if(!signal.aborted)$('#law-file').value='';}});});
async function readURLText(signal){
 const url=$('#law-url').value.trim();state.loadedURL=null;$('#law-source-status').textContent='正在读取官网正文…';
 try{const d=await api('/extract-url',{method:'POST',body:{url},signal});if(signal.aborted)return;$('#law-text').value=d.text;$('#law-url').value=d.source_url;state.loadedURL=d.source_url;state.importSource={source_kind:'url',source_name:d.source_name};state.sourceEdited=false;$('#law-source-status').textContent=`已读取 ${d.characters.toLocaleString()} 字符，识别 ${d.count} 条。无需上传文件，请核对名称、版本、日期和正文完整性。`;}
 catch(e){if(!signal.aborted)$('#law-source-status').textContent='官网读取未完成，原因见上方提示。可以重试当前链接。';throw e;}
}
async function previewImport(forceRead=false){
 if(state.importBusy)return;clearDialogMessage($('#import-dialog'));
 const urlMode=importMode()==='url';
 if(urlMode&&!$('#law-url').reportValidity())return;
 if(!urlMode&&!$('#law-text').reportValidity())return;
 invalidatePreview();
 await importJob(async signal=>{
  if(urlMode&&(forceRead||state.loadedURL!==$('#law-url').value.trim()||!$('#law-text').value.trim()))await readURLText(signal);
  if(signal.aborted)return;
  const p=lawPayload();const d=await api('/laws/preview',{method:'POST',body:{text:p.text},signal});if(signal.aborted)return;
  state.previewText=JSON.stringify(p);$('#law-preview').hidden=false;
  $('#law-preview').innerHTML=`<b>共识别 ${esc(d.count)} 条，请核对完整性</b>`+(d.warnings?.length?`<div class="warning-list">${d.warnings.map(esc).join('<br>')}</div>`:'')+d.articles.map(a=>`<div class="article-preview"><b>${esc(a.number)}</b>${esc(a.body)}</div>`).join('');
 });
}
$('#fetch-law-url').onclick=guarded(()=>previewImport(true));
$('#preview-law').onclick=guarded(()=>previewImport());
$("#import-form").onsubmit=guarded(async e=>{e.preventDefault();if(state.importBusy)return;const p=lawPayload();if(!$('#import-form').reportValidity())return;if(state.previewText!==JSON.stringify(p))throw new Error("内容已改变，请先重新预览当前资料");await importJob(async signal=>{const d=await api("/laws",{method:"POST",body:p,signal});setImportBusy(false);state.importAbort=null;$("#import-dialog").close();await loadLaws();await reviewLaw(d.id);if(d.duplicate){toast(d.message);return;}toast(`已导入${d.count}条，接下来请核对来源、完整性和有效期后启用。`);},true);});
$("#review-form").onsubmit=guarded(async e=>{e.preventDefault();await api(`/laws/${state.lawId}/review`,{method:"POST",body:{canonical_name:$("#review-canonical").value.trim(),document_number:$("#review-document-number").value.trim(),confirmed:$("#review-confirm").checked,valid_from:$("#review-from").value,valid_to:$("#review-to").value||null}});$("#review-dialog").close();await loadLaws();toast("已记录人工复核并启用此版本");});
$("#model-profile").onchange=()=>{const profile=$("#model-profile").value,defaults=state.settings?.profiles?.[profile];if(!defaults)return;$("#provider").value=defaults.provider;$("#base-url").value=defaults.base_url;$("#model-name").value=defaults.model;$("#api-key").value="";$("#verify-model").value="";$("#key-state").textContent=defaults.environment_api_key?"此运行方式的密钥来自服务器 .env；留空使用，不会传到浏览器。":profile==="local"?"本地 Ollama 通常不需要密钥。":"请填写该 API 运行方式的密钥，或在服务器 .env 中配置。";};
$("#provider").onchange=()=>{const p=presets[$("#provider").value];$("#base-url").value=p[0];$("#model-name").value=p[1];$("#api-key").value="";$("#verify-model").value="";$("#key-state").textContent="更换服务或地址不会继承旧密钥。只有服务和地址均与服务器 .env 配置匹配时，才可使用对应环境密钥；否则请重新填写。";};
$("#settings-form").onsubmit=guarded(async e=>{e.preventDefault();const profile=$("#model-profile").value,provider=$("#provider").value;const key=$("#api-key").value;const body={profile,online_research:$("#default-online-research").checked,show_reasoning:$("#show-reasoning").checked,json_mode:$("#json-mode").value,thinking_mode:$("#thinking-mode").value,agent_rounds:Number($("#agent-rounds").value),agent_timeout:Number($("#agent-timeout").value),provider,base_url:$("#base-url").value.trim(),model:$("#model-name").value.trim(),api_key:key||null,verify_model:$("#verify-model").value.trim(),timeout:Number($("#model-timeout").value),max_tokens:Number($("#max-tokens").value),review_days:Number($("#review-days").value)};await api("/settings",{method:"POST",body});await loadSettings(false);await status();$("#settings-result").textContent="设置已保存。请测试连接后开始使用。";toast("模型设置已保存");});
$("#test-model").onclick=guarded(async()=>{const b=$("#test-model");b.disabled=true;$("#settings-result").textContent="正在测试已保存配置…";try{const d=await api("/settings/test",{method:"POST"});$("#settings-result").textContent=d.message;}catch(e){$("#settings-result").textContent=e.message;throw e;}finally{b.disabled=false;}});

$('#search-provider').onchange=()=>{$('#search-base').value=$('#search-provider').value==='searxng'?'http://searxng:8080':'https://api.bochaai.com/v1';$('#search-key').value='';$('#search-key-state').textContent='切换服务后请重新填写对应密钥。';};
$('#search-settings-form').onsubmit=guarded(async e=>{e.preventDefault();const provider=$('#search-provider').value,base_url=$('#search-base').value.trim();const body={provider,base_url,api_key:$('#search-key').value||null,timeout:Number($('#search-timeout').value)};applySearchSettings(await api('/search/settings',{method:'POST',body}));$('#search-result').textContent='搜索设置已保存。提问时开启官方补查，或填写公开检索词即可使用。';});
$('#test-search').onclick=guarded(async()=>{const b=$('#test-search');b.disabled=true;$('#search-result').textContent='正在测试已保存的搜索配置…';try{const d=await api('/search/test',{method:'POST'});$('#search-result').textContent=d.message;}catch(e){$('#search-result').textContent=e.message;}finally{b.disabled=false;}});
const retrievalPresets={
 embedding:{none:['http://host.docker.internal:11434',''],ollama:['http://host.docker.internal:11434','qwen3-embedding:0.6b'],dashscope:['https://dashscope.aliyuncs.com/compatible-mode/v1','text-embedding-v4'],siliconflow:['https://api.siliconflow.cn/v1','Qwen/Qwen3-Embedding-4B'],custom:['http://reranker:8081/v1','']},
 reranker:{none:['http://reranker:8081/v1',''],dashscope:['https://dashscope.aliyuncs.com/api/v1','qwen3-rerank'],siliconflow:['https://api.siliconflow.cn/v1','Qwen/Qwen3-Reranker-4B'],custom:['http://reranker:8081/v1','Qwen/Qwen3-Reranker-0.6B']}
};
let retrievalSettings=null,indexTimer=null;
function serviceNote(kind){const p=$('#'+kind+'-provider').value;$('#'+kind+'-url').placeholder=p==='dashscope'?'https://业务空间ID.cn-beijing.maas.aliyuncs.com/'+(kind==='embedding'?'compatible-mode/v1':'api/v1'):'';$('#'+kind+'-url-note').textContent=p==='dashscope'?(kind==='embedding'?'默认地址适用于北京地域；若使用其他地域或业务空间专属域名，请换成控制台提供且与 API 密钥地域一致的 compatible-mode/v1 根地址。模型需支持 OpenAI 兼容 Embeddings 接口。':'qwen3-rerank 使用 DashScope 原生 /api/v1 根地址，程序追加 /services/rerank/text-rerank/text-rerank。默认域名适用于北京；其他地域或业务空间请使用控制台对应域名。不要使用聊天的 compatible-mode/v1 或旧版 compatible-api/v1。'):p==='ollama'?'Ollama 根地址；同一 Compose 内用 http://ollama:11434。':kind==='reranker'?'使用提供 /rerank 接口的服务；自建 Compose 用 http://reranker:8081/v1。':'使用提供 /embeddings 接口的兼容服务地址。';}
async function loadRetrievalSettings(){
 retrievalSettings=await api('/retrieval/settings');const s=retrievalSettings;
 $('#retrieval-mode').value=s.mode;$('#auto-index').checked=s.auto_index;
 for(const k of ['embedding','reranker']){const c=s[k];$('#'+k+'-provider').value=c.provider;$('#'+k+'-name').value=c.model;$('#'+k+'-url').value=c.base_url;$('#'+k+'-key').value='';$('#'+k+'-timeout').value=c.timeout;$('#'+k+'-key-state').textContent=c.environment_api_key?'已从服务器 .env 读取密钥；页面不会获取明文。':c.has_api_key?'已保存此服务的密钥；同地址留空保留。':c.environment_defaults?'连接参数来自服务器 .env 默认值；页面保存配置优先。':'未保存密钥；内网服务可留空。';serviceNote(k);}
 $('#embedding-dimensions').value=s.embedding.dimensions;$('#embedding-revision').value=s.embedding.revision;$('#embedding-instruction').value=s.embedding.query_instruction;
 for(const [id,key] of [['chunk-chars','chunk_chars'],['recall-k','recall_k'],['rerank-candidates','rerank_candidates'],['final-k','final_k']])$('#'+id).value=s[key];
 $('#retrieval-result').textContent=s.configuration_error||'';
 await refreshIndexStatus();
}
for(const kind of ['embedding','reranker']){
 $('#'+kind+'-provider').onchange=()=>{const p=$('#'+kind+'-provider').value,preset=retrievalPresets[kind][p];$('#'+kind+'-url').value=preset[0];$('#'+kind+'-name').value=preset[1];$('#'+kind+'-key').value='';$('#'+kind+'-key-state').textContent='更换服务或地址后不会继承旧密钥，请填写该服务密钥。';if(kind==='embedding'){$('#embedding-dimensions').value=0;$('#embedding-revision').value='';$('#embedding-instruction').value=['ollama','siliconflow'].includes(p)?'Given a Chinese legal question, retrieve statutory provisions that address the issue, including applicable conditions and exceptions.':'';}serviceNote(kind);};
 $('#test-'+kind).onclick=guarded(async()=>{const b=$('#test-'+kind);b.disabled=true;$('#retrieval-result').textContent='正在测试已保存的配置…';try{const d=await api('/retrieval/test/'+kind,{method:'POST'});$('#retrieval-result').textContent=d.message;}catch(e){$('#retrieval-result').textContent=e.message;throw e;}finally{b.disabled=false;}});
}
$('#retrieval-settings-form').onsubmit=guarded(async e=>{
 e.preventDefault();const body={mode:$('#retrieval-mode').value,auto_index:$('#auto-index').checked};
 for(const kind of ['embedding','reranker']){const provider=$('#'+kind+'-provider').value,base_url=$('#'+kind+'-url').value.trim();body[kind]={provider,base_url,model:$('#'+kind+'-name').value.trim(),timeout:Number($('#'+kind+'-timeout').value),api_key:$('#'+kind+'-key').value||null};}
 Object.assign(body.embedding,{dimensions:Number($('#embedding-dimensions').value),revision:$('#embedding-revision').value.trim(),query_instruction:$('#embedding-instruction').value.trim()});
 for(const [id,key] of [['chunk-chars','chunk_chars'],['recall-k','recall_k'],['rerank-candidates','rerank_candidates'],['final-k','final_k']])body[key]=Number($('#'+id).value);
 try{await api('/retrieval/settings',{method:'POST',body});await loadRetrievalSettings();$('#retrieval-result').textContent='检索配置已保存。自动索引开启时将在后台处理；可在下方查看真实状态。';}catch(err){$('#retrieval-result').textContent=err.message;throw err;}
});
async function refreshIndexStatus(){
 clearTimeout(indexTimer);const d=await api('/retrieval/status');const active=['queued','running'].includes(d.job.status);const label={queued:'等待当前研究结束后建立索引',running:'正在建立索引',completed:'本次索引任务完成',failed:'索引任务失败',interrupted:'索引已中断，可补齐继续'}[d.job.status]||'尚未建立索引';
 const box=$('#index-status');box.classList.toggle('warn',!d.configured||d.pending>0||d.failed>0||d.mode==='lexical');
 box.innerHTML=`<b>${d.mode==='lexical'?'当前选择：仅关键词与条号':!d.configured?'向量服务未配置，当前只能关键词／条号召回':esc(label)}</b><div class="index-counts"><span>法条 ${esc(d.total)}</span><span>已索引 ${esc(d.ready)}</span><span>待处理 ${esc(d.pending)}</span><span>失败 ${esc(d.failed)}</span></div><small>${d.job.total?`本轮进度 ${esc(d.job.completed||0)}/${esc(d.job.total)} · `:''}向量：${esc(d.embedding_model||'未配置')} · 重排：${esc(d.reranker_model||'未配置')} · 有效检索范围仍受法规日期、地区和复核状态限制。</small>${d.job.error?`<p>${esc(d.job.error)}</p>`:''}${d.errors.map(x=>`<p>${esc(x.article)}：${esc(x.error)}</p>`).join('')}`;
 $('#cancel-index').hidden=!active;$('#reindex').disabled=active||!d.configured;for(const k of ['embedding','reranker'])$('#test-'+k).disabled=active;
 if(active&&state.view==='settings')indexTimer=setTimeout(()=>refreshIndexStatus().catch(e=>{$('#retrieval-result').textContent=e.message;}),1200);
}
$('#reindex').onclick=guarded(async()=>{try{await api('/reindex',{method:'POST'});$('#retrieval-result').textContent='索引任务已提交，可刷新页面后继续查看；只补齐当前模型缺少或失败的法条。';await refreshIndexStatus();}catch(e){$('#retrieval-result').textContent=e.message;throw e;}});
$('#cancel-index').onclick=guarded(async()=>{await api('/reindex/cancel',{method:'POST'});$('#retrieval-result').textContent='已请求停止，当前请求结束后保留已完成条文。';await refreshIndexStatus();});

boot().catch(e=>{$("#client-load-status").hidden=true;$("#login-error").textContent="无法连接服务："+e.message;});

})();
