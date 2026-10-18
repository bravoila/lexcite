"""Regression fixtures for user context and actual provider-returned reasoning.
The model replies below are synthetic protocol fixtures, not legal evaluations.
"""
import json
import threading
import time
import httpx
import pytest
from app import db, llm
from app.research_agent import context as case_state, runner as engine, retrieval
from app.research_agent.schemas import CoverageAudit, FactExtraction, Plan, Draft, Verdict, ClarificationAudit

FACTS = '彩礼给付以结婚为目的\n双方已办理结婚登记手续\n婚后共同生活\n彩礼是否导致给付方生活困难 未知'
QUESTIONS = ['双方是否已办理结婚登记手续？', '双方婚后是否实际共同生活？', '彩礼给付是否导致给付方生活困难？']


def create(client):
    llm.save_settings({'provider':'custom','base_url':'http://localhost:9999/v1','model':'fixture','agent_rounds':1})
    return client.post('/api/cases',json={'title':'截图回归','event_date':'2025-01-01'}).json()['id']


def message(cid, text, role='user'):
    mid=db.uid()
    with db.connect() as c:
        c.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(mid,cid,role,json.dumps({'text':text},ensure_ascii=False),db.now()))
    return mid


def test_screenshot_fact_retention_and_unknown(client):
    cid=create(client);message(cid,'离婚时彩礼能否返还？');mid=message(cid,FACTS)
    for _ in range(8):message(cid,'之前不是告诉你了吗')
    value=case_state.load(cid,'继续',[])
    assert value['research_focus']=='离婚时彩礼能否返还？'
    assert len(value['facts'])==4
    assert case_state.filter_questions(QUESTIONS[:2],value['facts'])[0]==[]
    assert value['facts'][-1]['state']=='unknown' and value['facts'][-1]['source_id']==mid


def test_long_statement_and_case_isolation(client):
    cid=create(client);message(cid,FACTS+'\n'+'长文本'*550)
    message(cid,'助手臆造：生活困难属实',role='assistant')
    assert any('已办理结婚登记' in f['text'] for f in case_state.load(cid,'之前不是告诉你了吗',[])['facts'])
    other=create(client)
    assert not case_state.load(other,'继续',[])['facts']
    assert all('助手臆造' not in f['text'] for f in case_state.load(cid,'继续',[])['facts'])


def test_correction_conflict_and_unknown_to_known(client):
    cid=create(client);message(cid,'双方已办理结婚登记手续');message(cid,'双方未办理结婚登记手续')
    v=case_state.load(cid,'继续',[]);assert v['facts'][0]['state']=='conflicting'
    assert case_state.filter_questions(QUESTIONS[:1],v['facts'])[0]
    message(cid,'更正：双方未办理结婚登记手续')
    v=case_state.load(cid,'继续',[]);assert len(v['facts'])==1 and v['facts'][0]['state']=='stated'
    message(cid,'是否支付价款 未知');message(cid,'已经支付价款')
    assert case_state.load(cid,'继续',[])['facts'][-1]['state']=='stated'


def test_grounded_paraphrase_audit_requires_real_quote(client):
    audit=ClarificationAudit(checks=[{'question_index':0,'state':'answered','source_id':'a','quote':'彩礼是否导致给付方生活困难 未知'}])
    kept,removed=case_state.grounded_audit(QUESTIONS[2:],audit,[{'id':'a','text':FACTS}])
    assert not kept and removed[0]['state']=='unknown'
    audit.checks[0].quote='彩礼导致给付方生活困难'
    assert case_state.grounded_audit(QUESTIONS[2:],audit,[{'id':'a','text':FACTS}])[0]==QUESTIONS[2:]
    audit.checks=[]
    with pytest.raises(ValueError):case_state.grounded_audit(QUESTIONS[2:],audit,[])


def test_screenshot_repeat_questions_removed_end_to_end(client,corpus,monkeypatch):
    cid=create(client);message(cid,'离婚时彩礼能否返还？');inputs=[]
    def model(system,payload,schema,verify=False):
        inputs.append(payload)
        if schema is CoverageAudit:return CoverageAudit(selected_ids=[x['id'] for x in payload['candidate_evidence']],coverage='sufficient',reason='Synthetic protocol fixture; not a legal assessment')
        if schema is FactExtraction:return FactExtraction(intent='supplement',facts=[])
        if schema is Plan:return Plan(queries=['未支付价款'],questions=QUESTIONS)
        if schema is Draft:return Draft(insufficient=True,missing_facts=QUESTIONS+['彩礼给付日期是什么时候？'])
        assert schema is ClarificationAudit
        source=next(s for s in payload['sources'] if '生活困难 未知' in s['text'])
        return ClarificationAudit(checks=[{'question_index':i,'state':'unknown' if '生活困难' in q else 'missing','source_id':source['id'],'quote':'彩礼是否导致给付方生活困难 未知' if '生活困难' in q else ''} for i,q in enumerate(payload['questions'])])
    monkeypatch.setattr(llm,'request_json',model)
    result=client.post(f'/api/cases/{cid}/ask',json={'text':FACTS}).json()
    assert result['missing_facts']==['彩礼给付日期是什么时候？']
    assert result['unknown_facts'][0]['state']=='unknown'
    assert len(result['case_facts'])==4 and result['research_focus']=='离婚时彩礼能否返还？'
    assert inputs[1]['user_stated_facts']==result['case_facts']
    assert 'unknown' in engine.DRAFT
    result=client.post(f'/api/cases/{cid}/ask',json={'text':'之前不是告诉你了吗'}).json()
    assert result['missing_facts']==['彩礼给付日期是什么时候？'] and len(result['case_facts'])==4
    assert len(client.get(f'/api/cases/{cid}').json()['observed_facts'])==4


def test_http400_stops_after_one_call_preserves_original_focus(client,corpus,monkeypatch):
    cid=create(client);message(cid,'未支付价款怎么办？');message(cid,FACTS);calls=[];searches=[]
    def broken(*args,**kwargs):
        calls.append(1);raise llm.ModelError('模型接口 HTTP 400：参数不兼容',fatal=True,status_code=400)
    original=retrieval.search
    def search(queries,*args):searches.append(queries);return original(queries,*args)
    monkeypatch.setattr(llm,'request_json',broken);monkeypatch.setattr(retrieval,'search',search)
    value=engine.answer(cid,'之前不是告诉你了吗')
    assert value['status']=='model_error' and not value['claims'] and not value['citations'] and value['candidate_sources']
    assert calls==[1] and searches[0][0]=='未支付价款怎么办？'
    assert value['model_errors'][0]['http_status']==400 and len(value['case_facts'])==4


def wire(monkeypatch,handler):
    original=httpx.Client
    monkeypatch.setattr(llm.httpx,'Client',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))


@pytest.mark.parametrize('provider,thinking,json_mode,forced',[('dashscope','auto','auto',False),('dashscope','enabled','auto',False),('dashscope','disabled','auto',True),('dashscope','enabled','strict',True),('deepseek','auto','prompt',False),('ollama','enabled','auto',True)])
def test_json_compatibility_and_reasoning_sse(client,monkeypatch,provider,thinking,json_mode,forced):
    llm.save_settings({'provider':provider,'base_url':'http://localhost:9999/v1','model':'fixture','thinking_mode':thinking,'json_mode':json_mode})
    fragments=[]
    def handler(req):
        body=json.loads(req.content);assert ('response_format' in body)==forced and body['stream']
        chunks=[{'choices':[{'delta':{'reasoning_content':'接口测试片段一'}}]}, {'choices':[{'delta':{'reasoning':'；片段二'}}]}, {'choices':[{'delta':{'content':'{"queries":["完整结果"]}'},'finish_reason':'stop'}]}]
        return httpx.Response(200,headers={'Content-Type':'text/event-stream'},text='\n\n'.join('data: '+json.dumps(c) for c in chunks)+'\n\ndata: [DONE]\n')
    wire(monkeypatch,handler)
    with llm.capture_reasoning(fragments.append):value=llm.request_json('测试',{},Plan)
    assert ''.join(fragments)=='接口测试片段一；片段二' and value.queries==['完整结果']
    assert '接口测试' not in value.model_dump_json()


def test_request_options_are_scoped_and_usage_is_provider_reported(client,monkeypatch):
    llm.save_settings({'provider':'dashscope','base_url':'http://localhost:9999/v1','model':'fixture','thinking_mode':'enabled','max_tokens':4000})
    bodies=[];usage=[]
    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{"queries":["测试"]}'}}],'usage':{'prompt_tokens':120,'completion_tokens':30,'total_tokens':150}})
    wire(monkeypatch,handler)
    with llm.capture_usage(usage.append):
        with llm.request_options(thinking_mode='disabled',max_tokens=1200):
            llm.request_json('test',{},Plan)
        llm.request_json('test',{},Plan)
    assert bodies[0]['enable_thinking'] is False and bodies[0]['max_tokens']==1200
    assert bodies[1]['enable_thinking'] is True and bodies[1]['max_tokens']==4000
    assert bodies[1]['stream_options']=={'include_usage':True}
    assert usage==[{'prompt_tokens':120,'completion_tokens':30,'total_tokens':150}]*2
    assert llm.settings()['thinking_mode']=='enabled' and llm.settings()['max_tokens']==4000


def test_json_fallback_local_think_and_empty_reporting(client,monkeypatch):
    create(client);fragments=[]
    wire(monkeypatch,lambda req:httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'<think>本地测试片段</think>{"queries":["<think>只是数据</think>"]}'}}]}))
    with llm.capture_reasoning(fragments.append):value=llm.request_json('测试',{},Plan)
    assert fragments==['本地测试片段'] and value.queries==['<think>只是数据</think>']


def test_error_body_redacted_and_classified(client,monkeypatch):
    create(client);llm.save_settings({'provider':'custom','base_url':'http://localhost:9999','model':'fixture','api_key':'secret-key-for-test'})
    wire(monkeypatch,lambda req:httpx.Response(400,json={'error':{'code':'InvalidParameter','message':'enable_thinking conflicts with response_format secret-key-for-test Bearer another-secret sk-abcdefg'}}))
    with llm.capture_reasoning(lambda s:None),pytest.raises(llm.ModelError) as error:llm.request_json('test',{},Plan)
    assert error.value.fatal and error.value.status_code==400
    assert 'InvalidParameter' in str(error.value) and 'response_format' in str(error.value)
    assert not any(secret in str(error.value) for secret in ['secret-key-for-test','another-secret','sk-abcdefg'])


@pytest.mark.parametrize('enabled',[True,False])
def test_reasoning_job_stream_persistence_and_disabled(client,monkeypatch,enabled):
    cid=create(client);llm.save_settings({'provider':'custom','base_url':'http://localhost:9999','model':'fixture','agent_rounds':1,'show_reasoning':enabled})
    message(cid,FACTS);entered=threading.Event();release=threading.Event()
    def model(*args,**kw):
        llm.report_reasoning('接口测试片段，未经校验 <script>alert(1)</script>');entered.set();assert release.wait(4);return Plan()
    monkeypatch.setattr(llm,'request_json',model)
    jid=client.post(f'/api/cases/{cid}/research',json={'text':'之前不是告诉你了吗'}).json()['id']
    try:
        assert entered.wait(2)
        live=client.get('/api/research/'+jid).json()
        assert bool(live['reasoning'])==enabled
        if enabled:assert '接口测试片段' in live['reasoning'][0]['text'] and live['status']=='running'
    finally:release.set()
    for _ in range(100):
        job=client.get('/api/research/'+jid).json()
        if job['status']=='completed':break
        time.sleep(.01)
    assert job['status']=='completed'
    saved=client.get(f'/api/cases/{cid}').json()['messages'][-1]['payload']
    assert bool(saved['reasoning'])==enabled and len(saved['case_facts'])==4
    assert '接口测试片段' not in json.dumps(case_state.load(cid,'继续',[]),ensure_ascii=False)


def test_reasoning_budget_and_unavailable_are_honest(client,monkeypatch):
    cid=create(client)
    def model(*args,**kwargs):llm.report_reasoning('测'*30000);return Plan()
    monkeypatch.setattr(llm,'request_json',model)
    value=engine.answer(cid,'未支付价款怎么办？')
    assert len(value['reasoning'][0]['text'])==24000 and value['reasoning'][0]['truncated']
    monkeypatch.setattr(llm,'request_json',lambda *args,**kwargs:Plan())
    value=engine.answer(cid,'未支付价款怎么办？')
    assert value['reasoning'][0]['status']=='unavailable' and value['reasoning'][0]['text']==''


def test_conflict_cannot_be_silently_suppressed_by_audit(client):
    cid=create(client);message(cid,'双方已办理结婚登记手续');message(cid,'双方未办理结婚登记手续');mid=message(cid,'双方未办理结婚登记手续')
    state=case_state.load(cid,'继续',[])
    assert state['facts'][0]['state']=='conflicting'
    audit=ClarificationAudit(checks=[{'question_index':0,'state':'answered','source_id':mid,'quote':'双方未办理结婚登记手续'}])
    assert case_state.grounded_audit(QUESTIONS[:1],audit,state['sources'],state['facts'])[0]==QUESTIONS[:1]
