import time
import subprocess
from pathlib import Path

from app import jobs


ROOT = Path(__file__).resolve().parents[1]


def test_accepted_request_with_lost_response_is_recoverable(client, monkeypatch):
    case_id = client.post("/api/cases", json={"title": "idempotency fixture"}).json()["id"]
    request_id = "lost-response-regression"
    payload = {
        "text": "模拟响应丢失后的同一研究请求",
        "mode": "strict",
        "web_query": "",
        "online_research": False,
        "request_id": request_id,
    }
    calls = []

    def answer(case_id, question, mode, **kwargs):
        calls.append((case_id, question, mode))
        return {
            "status": "insufficient",
            "summary": "mocked result",
            "claims": [],
            "citations": [],
            "case_facts": [],
            "research_focus": question,
        }

    monkeypatch.setattr(jobs.research_agent, "answer", answer)

    # The server accepted the request, but the client loses the response after
    # dispatch. Recovery must use the request ID, not create a new submission.
    accepted = client.post(f"/api/cases/{case_id}/research", json=payload)
    assert accepted.status_code == 202
    original_job_id = accepted.json()["id"]
    try:
        raise ConnectionError("simulated lost HTTP response")
    except ConnectionError:
        pass

    for _ in range(200):
        by_job_id = client.get(f"/api/research/{original_job_id}")
        if by_job_id.json()["status"] not in jobs.ACTIVE:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("research job did not reach a terminal state")

    recovered = client.get(f"/api/research/{request_id}")
    assert recovered.status_code == 200
    assert recovered.json()["id"] == original_job_id
    assert recovered.json()["status"] == "completed"
    assert recovered.json()["result"]["summary"] == "mocked result"

    retried = client.post(f"/api/cases/{case_id}/research", json=payload)
    assert retried.status_code == 202
    assert retried.json()["id"] == original_job_id
    assert len(calls) == 1
    assert len(client.get(f"/api/cases/{case_id}").json()["messages"]) == 2


def test_browser_reuses_request_after_accepted_response_is_lost():
    browser_test = r"""
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {JSDOM}=require('jsdom');
const root=process.cwd();
const html=fs.readFileSync(root+'/static/index.html','utf8');
const script=fs.readFileSync(root+'/static/app.js','utf8');
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function wait(predicate,label){for(let i=0;i<300;i++){if(predicate())return;await pause(5);}throw new Error('Timed out: '+label);}
const response=(data,status=200)=>({ok:status>=200&&status<300,status,json:async()=>structuredClone(data)});
const model={profile:'local',provider:'none',model:'fixture',base_url:'http://localhost:11434/v1',timeout:90,max_tokens:4000,review_days:180,agent_rounds:1,agent_timeout:120,thinking_mode:'auto',json_mode:'auto',profiles:{},verify_model:''};
const service={provider:'none',model:'',base_url:'http://localhost:11434/v1',timeout:90,dimensions:0,revision:'',query_instruction:''};
const retrieval={mode:'lexical',embedding:service,reranker:service,auto_index:false,chunk_chars:1200,recall_k:40,rerank_candidates:60,final_k:14};
const result={status:'insufficient',summary:'mocked result',elapsed_seconds:0,claims:[],citations:[],warnings:[],trace:[],notice:'fixture'};
const caseData={case:{id:'case-a',title:'Fixture',event_date:'2025-01-01',jurisdiction:'全国'},memories:[],observed_facts:[],active_jobs:[],messages:[]};
const dom=new JSDOM(html,{url:'http://localhost',runScripts:'outside-only',pretendToBeVisual:true});
const w=dom.window,$=selector=>w.document.querySelector(selector);
w.HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','');};
w.HTMLDialogElement.prototype.close=function(){this.removeAttribute('open');};
let submissions=[],lookups=0,modelInvocations=0,acceptedJob=null;
const completedJob=()=>({id:'job-a',case_id:'case-a',status:'completed',question:'question survives',message_id:'answer-a',result,events:[]});
w.fetch=async(url,options={})=>{
 if(url==='/api/session')return response({version:w.document.documentElement.dataset.appVersion,initialized:true,authenticated:true});
 if(url==='/api/status')return response({laws:0,articles:0,reviewed:0,model,retrieval});
 if(url==='/api/cases')return response([caseData.case]);
 if(url==='/api/research')return response([]);
 if(url==='/api/settings')return response(model);
 if(url==='/api/retrieval/settings')return response(retrieval);
 if(url==='/api/retrieval/status')return response({configured:false,mode:'lexical',total:0,ready:0,pending:0,failed:0,job:{},errors:[]});
 if(url==='/api/search/settings')return response({provider:'none',base_url:'https://api.bochaai.com/v1',timeout:10});
 if(url==='/api/cases/case-a')return response({...caseData,messages:acceptedJob?[{id:'user-a',role:'user',payload:{text:acceptedJob.question}},{id:'answer-a',role:'assistant',payload:result}]:[]});
 if(url==='/api/cases/case-a/research'){
  const body=JSON.parse(options.body);submissions.push(body);
  if(!acceptedJob){modelInvocations++;acceptedJob=completedJob();throw new TypeError('response lost after server accepted request');}
  assert.equal(body.request_id,acceptedJob.request_id||submissions[0].request_id);
  return response(acceptedJob,202);
 }
 if(url.startsWith('/api/research/')){
  lookups++;
  if(lookups===1)return response({detail:'temporary lookup failure'},500);
  return response(acceptedJob);
 }
 throw new Error('Unexpected fixture request: '+url);
};
w.eval(script);
(async()=>{
 await wait(()=>!$('#workspace').hidden,'workspace boot');
 $('#question').value='question survives';$('#web-query').value='public query';
 $('#ask-form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
 await wait(()=>submissions.length===1&&!$('#resume-research').hidden,'first response-loss recovery attempt');
 assert.equal($('#question').value,'question survives');
 assert.equal($('#web-query').value,'public query');
 assert.equal(modelInvocations,1);
 $('#ask-form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
 await wait(()=>submissions.length===2&&!$('#send').disabled&&$('#messages .message.answer'),'idempotent resubmission completes');
 assert.equal(submissions[0].request_id,submissions[1].request_id);
 assert.deepEqual(submissions[0],submissions[1]);
 assert.equal(modelInvocations,1);
 assert.equal(w.document.querySelectorAll('#messages .message.user').length,1);
 assert.equal(w.document.querySelectorAll('#messages .message.answer').length,1);
 assert.equal(w.sessionStorage.getItem('lexcite.pendingResearch.v1'),null);
 dom.window.close();
})().catch(error=>{console.error(error);dom.window.close();process.exitCode=1;});
"""
    run = subprocess.run(
        ["node", "-e", browser_test],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
