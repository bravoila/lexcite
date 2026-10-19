import json
import threading
import time
import httpx
import pytest
from app import db, jobs, llm, websearch
from app.research_agent import runner as engine, retrieval
from app.research_agent.schemas import FactExtraction, Draft, Plan, Repair, Verdict


def setup(client):
    llm.save_settings({"provider":"custom", "base_url":"http://localhost:9999/v1", "model":"fixture", "api_key":""})
    return client.post('/api/cases', json={"title":"Agent 测试", "event_date":"2025-01-01"}).json()['id']


def good(payload):
    row=payload['evidence'][0]
    return Draft(insufficient=False,claims=[{"text":"如果存在不履行义务的情形，应结合合同约定及条文适用条件判断责任。", "kind":"inference", "evidence":[{"article_id":row['id'],"quote":row['body']}]}])


@pytest.mark.parametrize('partial',[False,True])
def test_normal_path_is_two_calls_even_with_recommendations(client,corpus,monkeypatch,partial):
    cid=setup(client);calls=[]
    llm.save_settings({'provider':'custom','base_url':'http://localhost:9999','model':'fixture','agent_rounds':3,'show_reasoning':False})
    def model(system,payload,schema,verify=False):
        calls.append(schema)
        if schema is Draft:
            draft=good(payload)
            if partial:
                draft=Draft.model_validate(draft.model_dump()|{'insufficient':True,'missing_authorities':[{'name':'最高人民法院关于测试争议的规定','reason':'合成补充建议'}]})
            return draft
        if schema is Verdict:
            assert verify and 'previous_attempt' not in payload and 'repair_feedback' not in payload
            return Verdict(checks=[{'claim_index':0,'supported':True,'reason':'合成测试'}])
        raise AssertionError('普通路径不应调用规划或单独的覆盖检查')
    monkeypatch.setattr(llm,'request_json',model)
    result=client.post(f'/api/cases/{cid}/ask',json={'text':'未支付价款怎么办？','online_research':False}).json()
    assert calls==[Draft,Verdict] and result['model_calls']==2 and result['rounds']==1
    assert result['status']==('partial' if partial else 'answered')
    assert bool(result['source_gaps'])==partial and not result['reasoning']
    assert all(run['status']=='completed' and run['usage'] is None and run['elapsed_seconds']>=0 for run in result['model_runs'])
    exported=client.get(f'/api/cases/{cid}/export').text
    assert '模型调用耗时' in exported and '供应商 token 用量：未提供' in exported


def test_new_sources_invalidate_old_partial_claims_before_failed_reanalysis(client,corpus,monkeypatch):
    from app.research_agent import authorities
    cid=setup(client);drafts=[];need={'name':'最高人民法院关于测试新增例外的规定','reason':'合成的新增法源'}
    row=retrieval.corpus('2025-01-01','全国')[0][0]
    def acquire(self,needs,*args):
        new=row|{'id':'synthetic-new-source','law_id':'synthetic-official-law','name':need['name'],'canonical_name':need['name'],'number':'第一条','body':'测试新增例外条文，只用于验证重新复核机制，不是真实法律。'}
        self.rows.append(new)
        return [new]
    monkeypatch.setattr(authorities.ResearchSources,'acquire',acquire)
    def model(system,payload,schema,verify=False):
        if schema is Plan:return Plan(authorities=[need])
        if schema is Draft:
            drafts.append(payload)
            if len(drafts)>1:raise llm.ModelError('合成接口失败',fatal=True,status_code=400)
            draft=good(payload);draft.claims.append(draft.claims[0].model_copy())
            return draft
        if schema is Verdict:return Verdict(checks=[{'claim_index':0,'supported':True,'reason':'合成部分支持'},{'claim_index':1,'supported':False,'reason':'需查询例外','action':'retrieve'}])
        raise AssertionError(schema)
    monkeypatch.setattr(llm,'request_json',model)
    result=engine.answer(cid,'未支付价款怎么办？',online_research=True)
    assert len(drafts)==2 and any(r['id']=='synthetic-new-source' for r in drafts[-1]['evidence'])
    assert result['status']=='model_error' and not result['claims'] and not result['citations']
    assert 'verification' not in result


def test_repair_invalid_quote_then_succeed(client,corpus,monkeypatch):
    cid=setup(client); calls=[]; events=[]
    def model(system,payload,schema,verify=False):
        calls.append((schema,payload))
        if schema is FactExtraction:return FactExtraction(intent='supplement',facts=[])
        if schema is Plan:return Plan(queries=['不履行合同义务'])
        if schema is Draft:
            draft=good(payload)
            draft.claims[0].evidence[0].quote='本段是假造引文不在原条文中'
            return draft
        if schema is Repair:
            return Repair(patches=[{'claim_index':0,'claim':good(payload).claims[0]}])
        return Verdict(checks=[{"claim_index":0,"supported":True,"reason":"fixture"}])
    monkeypatch.setattr(llm,'request_json',model)
    result=engine.answer(cid,'未支付价款怎么办',progress=events.append)
    assert result['status']=='answered' and result['rounds']==2 and result['model_calls']==3
    assert [schema for schema,payload in calls]==[Draft,Repair,Verdict]
    assert calls[1][1]['failed_claims'][0]['reason']=='引用原文与法条不一致'
    assert len(result['retrieval_runs'])==1
    assert '本段是假造引文' not in json.dumps(result,ensure_ascii=False)
    assert any(e.get('stage')=='repair' for e in events)
    assert events[-1]['stage']=='done' and events[-1]['status']=='completed'


def test_semantic_failure_repairs_without_planning_or_retrieval(client,corpus,monkeypatch):
    cid=setup(client); plans=[]; searches=[]; reviews=[]
    original=retrieval.search
    def search(queries,*args,**kwargs):searches.append(queries);return original(queries,*args,**kwargs)
    def model(system,payload,schema,verify=False):
        if schema is FactExtraction:return FactExtraction(intent='supplement',facts=[])
        if schema is Plan:
            plans.append(payload)
            raise AssertionError('条件化改写不应重新规划')
        if schema is Draft:return good(payload)
        if schema is Repair:
            claim=good(payload).claims[0].model_copy(update={'text':'测试条件化改写，不构成法律意见。'})
            assert payload['failed_claims'][0]['reason']=='缺少适用条件，请条件化'
            return Repair(patches=[{'claim_index':0,'claim':claim}])
        reviews.append(payload)
        return Verdict(checks=[{"claim_index":0,"supported":len(reviews)>1,"reason":"缺少适用条件，请条件化"}])
    monkeypatch.setattr(retrieval,'search',search);monkeypatch.setattr(llm,'request_json',model)
    r=engine.answer(cid,'未支付价款怎么办')
    assert r['status']=='answered' and len(searches)==1 and not plans
    assert r['model_calls']==4 and len(reviews)==2
    assert r['claims'][0]['text']=='测试条件化改写，不构成法律意见。'


def test_repair_queries_survive_many_authorities(client,corpus,monkeypatch):
    from app.research_agent import authorities
    cid=setup(client); searches=[]; plans=[]
    needs=[{'name':f'测试补充法源{i}','reason':'合成多法源需求','articles':[]} for i in range(8)]
    monkeypatch.setattr(authorities,'directory_for',lambda topic:needs)
    original=retrieval.search
    def search(queries,*args,**kwargs):searches.append(queries);return original(queries,*args,**kwargs)
    def model(system,payload,schema,verify=False):
        if schema is FactExtraction:return FactExtraction(intent='supplement',facts=[])
        if schema is Plan:
            plans.append(1)
            return Plan(queries=['修正查询甲','修正查询乙','修正查询丙'])
        if schema is Draft:return good(payload)
        return Verdict(checks=[{"claim_index":0,"supported":False,"reason":"首轮不支持，需修正检索","action":"retrieve"}])
    monkeypatch.setattr(retrieval,'search',search);monkeypatch.setattr(llm,'request_json',model)
    r=engine.answer(cid,'未支付价款怎么办')
    assert r['status']=='missing_sources' and len(searches)==2 and searches[0]!=searches[1]
    assert r['model_calls']==3 and len(plans)==1
    assert [run['stage'] for run in r['model_runs']]==['法律分析','证据复核','研究计划']
    assert sum(1 for q in searches[1] if '修正查询' in q)>=2


def test_exhausted_agent_never_publishes_invalid_claim(client,corpus,monkeypatch):
    cid=setup(client)
    def model(system,payload,schema,verify=False):
        if schema is FactExtraction:return FactExtraction(intent='supplement',facts=[])
        if schema is Plan:return Plan()
        if schema is Draft:return good(payload)
        if schema is Repair:return Repair(patches=[{'claim_index':0,'claim':good(payload).claims[0]}])
        return Verdict(checks=[{"claim_index":0,"supported":False,"reason":"fixture rejection"}])
    monkeypatch.setattr(llm,'request_json',model)
    r=engine.answer(cid,'未支付价款怎么办')
    assert r['rounds']==2 and r['model_calls']==4 and not r['claims'] and not r['citations'] and r['candidate_sources']
    assert r['status']=='no_supported_answer'
    assert any('同一组证据已尝试局部修正' in w for w in r['warnings'])


@pytest.mark.parametrize('repair_supported',[False,True])
def test_local_repair_preserves_verified_claims_and_original_indices(client,corpus,monkeypatch,repair_supported):
    cid=setup(client);calls=[];reviews=[];original=[]
    def model(system,payload,schema,verify=False):
        calls.append(schema)
        if schema is Draft:
            base=good(payload).claims[0]
            original.extend(base.model_copy(deep=True,update={'text':f'合成结论{i}，仅用于验证局部修正。'}) for i in range(3))
            original[0].evidence[0].quote='不存在的合成测试原文'
            return Draft(insufficient=False,claims=original)
        if schema is Repair:
            assert [f['claim_index'] for f in payload['failed_claims']]==[0,2]
            fixed=original[0].model_copy(deep=True)
            fixed.evidence[0].quote=next(r['body'] for r in payload['evidence'] if r['id']==fixed.evidence[0].article_id)
            return Repair(patches=[
                {'claim_index':2,'claim':original[2].model_copy(update={'text':'缩小后的合成结论，仅用于测试。'})},
                {'claim_index':0,'claim':fixed}])
        if schema is Verdict:
            reviews.append(payload)
            assert verify
            if len(reviews)==1:
                assert [c['text'] for c in payload['draft']]==[original[1].text,original[2].text]
                return Verdict(checks=[{'claim_index':0,'supported':True,'reason':'fixture'},{'claim_index':1,'supported':False,'reason':'需要缩小结论'}])
            assert payload['verified_claims']==[{
                'text':original[1].text,'kind':original[1].kind,'section':original[1].section,
                'evidence':[{'article_id':ref.article_id} for ref in original[1].evidence]}]
            assert original[1].text not in [c['text'] for c in payload['draft']]
            return Verdict(checks=[{'claim_index':i,'supported':repair_supported,'reason':'fixture'} for i in range(2)])
        raise AssertionError(schema)
    monkeypatch.setattr(llm,'request_json',model)
    result=engine.answer(cid,'未支付价款怎么办')
    assert calls==[Draft,Verdict,Repair,Verdict] and len(result['retrieval_runs'])==1
    assert result['rounds']==2 and result['verification']['semantic_review']=='model'
    if repair_supported:
        assert result['status']=='answered'
        assert [c['text'] for c in result['claims']]==[original[0].text,original[1].text,'缩小后的合成结论，仅用于测试。']
    else:
        assert result['status']=='partial'
        assert [c['text'] for c in result['claims']]==[original[1].text]
    assert all(c['citations'] for c in result['claims']) and result['citations']


@pytest.mark.parametrize('bad_patch',['verified_index','duplicate','missing','extra','changed_text'])
def test_local_repair_rejects_invalid_patch_atomically(client,corpus,monkeypatch,bad_patch):
    cid=setup(client);calls=[];original=[]
    def model(system,payload,schema,verify=False):
        calls.append(schema)
        if schema is Draft:
            original.extend([good(payload).claims[0],good(payload).claims[0]])
            original[0].evidence[0].quote='不存在的合成测试原文'
            original[1].text='保留已通过复核的合成结论。'
            return Draft(insufficient=False,claims=original)
        if schema is Verdict:
            assert len(payload['draft'])==1
            return Verdict(checks=[{'claim_index':0,'supported':True,'reason':'fixture'}])
        if schema is Repair:
            claim=good(payload).claims[0]
            patch={'claim_index':0,'claim':claim}
            patches={
                'verified_index':[patch|{'claim_index':1}],
                'duplicate':[patch,patch],
                'missing':[],
                'extra':[patch,{'claim_index':7,'claim':None}],
                'changed_text':[patch|{'claim':claim.model_copy(update={'text':'不允许在修引文时改写结论。'})}]
            }[bad_patch]
            return Repair(patches=patches)
        raise AssertionError(schema)
    monkeypatch.setattr(llm,'request_json',model)
    result=engine.answer(cid,'未支付价款怎么办')
    assert calls==[Draft,Verdict,Repair] and result['status']=='partial'
    assert [c['text'] for c in result['claims']]==[original[1].text]
    assert any(e['step']=='局部修正未完成' and e['status']=='warning' for e in result['trace'])


def test_reranking_does_not_invalidate_verified_claims_or_restart_analysis(client,corpus,monkeypatch):
    cid=setup(client);searches=[];calls=[]
    rows=retrieval.corpus('2025-01-01','全国')[0]
    def search(*args,**kwargs):
        searches.append(1)
        return ([r|{'rerank_score':len(searches),'retrieval_channels':['bm25']} for r in (rows if len(searches)==1 else reversed(rows))],[])
    def model(system,payload,schema,verify=False):
        calls.append(schema)
        if schema is Draft:
            draft=good(payload);draft.claims.append(draft.claims[0].model_copy(update={'text':'需补证的合成结论。'}))
            return draft
        if schema is Verdict:
            return Verdict(checks=[{'claim_index':0,'supported':True,'reason':'fixture'},{'claim_index':1,'supported':False,'reason':'需要新依据','action':'retrieve'}])
        if schema is Plan:return Plan(queries=['另一种检索表达'])
        raise AssertionError(schema)
    monkeypatch.setattr(retrieval,'search',search);monkeypatch.setattr(llm,'request_json',model)
    result=engine.answer(cid,'未支付价款怎么办')
    assert len(searches)==2 and calls==[Draft,Verdict,Plan]
    assert result['status']=='partial' and len(result['claims'])==1 and result['verification']
    assert any(e['step']=='补充检索没有新增依据' for e in result['trace'])


@pytest.mark.parametrize('field,value',[('body','合成新原文：新增例外必须触发重新分析和复核，不是真实法律。'),('version','发生变化的合成版本'),('valid_to','2026-01-01')])
def test_same_id_with_changed_evidence_invalidates_previous_approval(client,corpus,monkeypatch,field,value):
    cid=setup(client);searches=[];drafts=[]
    rows=retrieval.corpus('2025-01-01','全国')[0]
    def search(*args,**kwargs):
        searches.append(1)
        return ([r|({field:value} if i==0 and len(searches)>1 else {}) for i,r in enumerate(rows)],[])
    def model(system,payload,schema,verify=False):
        if schema is Draft:
            drafts.append(payload)
            if len(drafts)>1:raise llm.ModelError('合成重分析失败',fatal=True)
            draft=good(payload);draft.claims.append(draft.claims[0].model_copy())
            return draft
        if schema is Verdict:
            return Verdict(checks=[{'claim_index':0,'supported':True,'reason':'fixture'},{'claim_index':1,'supported':False,'reason':'需要新依据','action':'retrieve'}])
        if schema is Plan:return Plan(queries=['补充检索'])
        raise AssertionError(schema)
    monkeypatch.setattr(retrieval,'search',search);monkeypatch.setattr(llm,'request_json',model)
    result=engine.answer(cid,'未支付价款怎么办')
    assert len(drafts)==2 and drafts[-1]['evidence'][0][field]==value
    assert result['status']=='model_error' and not result['claims'] and not result['citations']
    assert 'verification' not in result


@pytest.mark.parametrize('malformed',[False,True])
@pytest.mark.parametrize('recovers',[False,True])
def test_review_failure_retries_only_review(client,corpus,monkeypatch,malformed,recovers):
    cid=setup(client);calls=[];reviews=[]
    def model(system,payload,schema,verify=False):
        calls.append(schema)
        if schema is Draft:return good(payload)
        if schema is Verdict:
            reviews.append(payload)
            if len(reviews)>1 and recovers:return Verdict(checks=[{'claim_index':0,'supported':True,'reason':'fixture'}])
            if malformed:return Verdict(checks=[])
            raise llm.ModelError('合成复核服务暂时不可用')
        raise AssertionError(schema)
    monkeypatch.setattr(llm,'request_json',model)
    result=engine.answer(cid,'未支付价款怎么办')
    assert calls==[Draft,Verdict,Verdict] and result['rounds']==2
    assert len(result['retrieval_runs'])==1 and reviews[0]['draft']==reviews[1]['draft']
    assert bool(result['claims'])==recovers
    assert result['status']==('answered' if recovers else 'no_supported_answer' if malformed else 'model_error')


def test_draft_failure_retries_once_without_retrieving(client,corpus,monkeypatch):
    cid=setup(client);calls=[]
    def model(system,payload,schema,verify=False):
        calls.append(schema)
        raise llm.ModelError('合成分析服务暂时不可用')
    monkeypatch.setattr(llm,'request_json',model)
    result=engine.answer(cid,'未支付价款怎么办')
    assert calls==[Draft,Draft] and result['rounds']==2
    assert result['status']=='model_error' and not result['claims']
    assert len(result['retrieval_runs'])==1


def test_unchanged_retrieval_still_allows_local_citation_repair(client,corpus,monkeypatch):
    cid=setup(client);calls=[];reviews=[];original=[]
    def model(system,payload,schema,verify=False):
        calls.append(schema)
        if schema is Draft:
            original.extend(good(payload).claims[0].model_copy(deep=True,update={'text':f'合成结论{i}。'}) for i in range(3))
            original[1].evidence[0].quote='不存在的合成测试原文'
            return Draft(insufficient=False,claims=original)
        if schema is Verdict:
            reviews.append(payload)
            if len(reviews)==1:
                return Verdict(checks=[{'claim_index':0,'supported':True,'reason':'fixture'},{'claim_index':1,'supported':False,'reason':'需要新依据','action':'retrieve'}])
            assert len(payload['draft'])==1 and payload['draft'][0]['text']==original[1].text
            return Verdict(checks=[{'claim_index':0,'supported':True,'reason':'fixture'}])
        if schema is Plan:return Plan(queries=['未支付价款怎么办'])
        if schema is Repair:
            assert [f['claim_index'] for f in payload['failed_claims']]==[1]
            claim=original[1].model_copy(deep=True)
            claim.evidence[0].quote=next(r['body'] for r in payload['evidence'] if r['id']==claim.evidence[0].article_id)
            return Repair(patches=[{'claim_index':1,'claim':claim}])
        raise AssertionError(schema)
    monkeypatch.setattr(llm,'request_json',model)
    result=engine.answer(cid,'未支付价款怎么办')
    assert calls==[Draft,Verdict,Plan,Repair,Verdict] and result['rounds']==2
    assert result['status']=='partial' and [c['text'] for c in result['claims']]==[original[0].text,original[1].text]


def test_dropping_rejected_claim_does_not_mark_partial_answer_complete(client,corpus,monkeypatch):
    cid=setup(client);calls=[]
    def model(system,payload,schema,verify=False):
        calls.append(schema)
        if schema is Draft:
            draft=good(payload);draft.claims.append(draft.claims[0].model_copy(update={'text':'无法支持的合成结论。'}))
            return draft
        if schema is Verdict:
            return Verdict(checks=[{'claim_index':0,'supported':True,'reason':'fixture'},{'claim_index':1,'supported':False,'reason':'应删除'}])
        if schema is Repair:return Repair(patches=[{'claim_index':1,'claim':None}])
        raise AssertionError(schema)
    monkeypatch.setattr(llm,'request_json',model)
    result=engine.answer(cid,'未支付价款怎么办')
    assert calls==[Draft,Verdict,Repair] and len(result['claims'])==1
    assert result['status']=='partial' and result['coverage']['coverage']=='partial'


def test_still_invalid_repaired_quote_never_reaches_semantic_review(client,corpus,monkeypatch):
    cid=setup(client);calls=[]
    def model(system,payload,schema,verify=False):
        calls.append(schema)
        if schema is Draft:
            draft=good(payload);draft.claims[0].evidence[0].quote='不存在的合成测试原文'
            return draft
        if schema is Repair:
            return Repair(patches=[{'claim_index':0,'claim':payload['failed_claims'][0]['claim']}])
        raise AssertionError('无有效引用时不能复核或重复检索')
    monkeypatch.setattr(llm,'request_json',model)
    result=engine.answer(cid,'未支付价款怎么办')
    assert calls==[Draft,Repair] and result['rounds']==2
    assert not result['claims'] and not result['citations'] and 'verification' not in result
    assert any('同一组证据已尝试局部修正' in w for w in result['warnings'])


def test_cancellation_during_local_repair_stops_before_review(client,corpus,monkeypatch):
    cid=setup(client);calls=[];cancelled=[False]
    def model(system,payload,schema,verify=False):
        calls.append(schema)
        if schema is Draft:
            draft=good(payload);draft.claims[0].evidence[0].quote='不存在的合成测试原文'
            return draft
        if schema is Repair:
            cancelled[0]=True
            return Repair(patches=[{'claim_index':0,'claim':good(payload).claims[0]}])
        raise AssertionError(schema)
    monkeypatch.setattr(llm,'request_json',model)
    with pytest.raises(InterruptedError):
        engine.answer(cid,'未支付价款怎么办',cancelled=lambda:cancelled[0])
    assert calls==[Draft,Repair]
    assert not client.get(f'/api/cases/{cid}').json()['messages']


def test_web_leads_not_legal_evidence(client,corpus,monkeypatch):
    cid=setup(client); sent=[]
    lead={"url":"https://www.gov.cn/fixture.html","title":"测试法规","snippet":"搜索摘要并非法源","note":"待复核","state":"search_only"}
    def search(query,*args):sent.append(query);return [lead]
    monkeypatch.setattr(websearch,'search',search)
    monkeypatch.setattr(websearch,'read_lead',lambda row:row|{'state':'read_unreviewed'})
    def model(system,payload,schema,verify=False):
        if schema is FactExtraction:return FactExtraction(intent='supplement',facts=[])
        if schema is Plan:return Plan()
        assert all('fixture.html' not in e.get('source_url','') for e in payload['evidence'])
        return Draft(insufficient=False,claims=[{"text":"搜索摘要构成法律依据。","kind":"rule","evidence":[{"article_id":lead['url'],"quote":'搜索摘要并非法源测试文本'}]}])
    monkeypatch.setattr(llm,'request_json',model)
    r=engine.answer(cid,'当事人张某未支付价款怎么办',web_query='民法典 价款')
    assert sent==['民法典 价款'] and r['web_sources'] and not r['claims']


def test_no_network_without_public_keywords(client,corpus,monkeypatch):
    cid=setup(client)
    def forbidden(*args,**kwargs):raise AssertionError('unexpected network')
    monkeypatch.setattr(websearch,'search',forbidden)
    r=engine.answer(cid,'未支付价款怎么办',mode='extract')
    assert any(e['stage']=='web' and e['status']=='skipped' for e in r['trace'])


def wait_job(client,jid):
    for _ in range(200):
        value=client.get('/api/research/'+jid).json()
        if value['status'] not in jobs.ACTIVE:return value
        time.sleep(.01)
    raise AssertionError('research did not finish')


def test_job_progress_idempotency_and_reload(client,corpus,monkeypatch):
    cid=setup(client); entered=threading.Event(); release=threading.Event()
    def model(system,payload,schema,verify=False):
        if schema is FactExtraction:return FactExtraction(intent='supplement',facts=[])
        if schema is Plan:return Plan()
        if schema is Draft:
            entered.set(); assert release.wait(4); return good(payload)
        return Verdict(checks=[{"claim_index":0,"supported":True,"reason":"fixture"}])
    monkeypatch.setattr(llm,'request_json',model)
    body={'text':'未支付价款怎么办','request_id':'repeat-safe'}
    try:
        response=client.post(f'/api/cases/{cid}/research',json=body)
        assert response.status_code==202
        jid=response.json()['id'];assert entered.wait(2)
        progress=client.get('/api/research/'+jid).json()
        assert progress['status']=='running' and progress['events'][-1]['stage']=='draft'
        assert client.get(f'/api/cases/{cid}').json()['active_jobs'][0]['id']==jid
        assert client.post(f'/api/cases/{cid}/research',json=body).json()['id']==jid
        assert client.delete(f'/api/cases/{cid}').status_code==409
        assert client.post(f'/api/cases/{cid}/research',json=body|{'text':'changed'}).status_code==409
    finally: release.set()
    done=wait_job(client,jid)
    assert done['status']=='completed' and done['result']['claims']
    assert len(client.get(f'/api/cases/{cid}').json()['messages'])==2
    assert client.post(f'/api/cases/{cid}/research',json=body).json()['id']==jid
    assert len(client.get(f'/api/cases/{cid}').json()['messages'])==2


def test_cancel_prevents_more_calls_and_does_not_store_draft(client,corpus,monkeypatch):
    cid=setup(client); entered=threading.Event(); release=threading.Event(); calls=[]
    def model(*args,**kwargs):calls.append(1);entered.set();assert release.wait(4);return Plan()
    monkeypatch.setattr(llm,'request_json',model)
    jid=client.post(f'/api/cases/{cid}/research',json={'text':'未支付价款怎么办'}).json()['id']
    try:
        assert entered.wait(2)
        assert client.post('/api/research/'+jid+'/cancel').json()['status']=='cancel_requested'
    finally:release.set()
    assert wait_job(client,jid)['status']=='cancelled'
    assert len(calls)==1 and not client.get(f'/api/cases/{cid}').json()['messages']
    assert not jobs.MODEL_LOCK.locked()


def test_restart_marks_abandoned_task_interrupted(client):
    cid=client.post('/api/cases',json={'title':'restart'}).json()['id']
    with db.connect() as c:c.execute("INSERT INTO research_jobs(id,case_id,request_id,input,status,created,updated) VALUES(?,?,?,?,'running',?,?)",('abandoned',cid,'x','{}',db.now(),db.now()))
    jobs.init()
    assert client.get('/api/research/abandoned').json()['status']=='interrupted'


def test_search_keys_and_provider_switch(client):
    body={'provider':'bocha','base_url':'https://api.bochaai.com/v1','api_key':'search-secret'}
    r=client.post('/api/search/settings',json=body).json()
    assert r['has_api_key'] and 'search-secret' not in json.dumps(r)
    assert 'search-secret' not in json.dumps(db.get('search_settings'))
    assert client.post('/api/search/settings',json=body|{'api_key':None}).json()['has_api_key']
    assert not client.post('/api/search/settings',json={'provider':'searxng','base_url':'http://localhost:8888','api_key':None}).json()['has_api_key']


@pytest.mark.parametrize('provider',['bocha','searxng'])
def test_search_wire_and_official_filter(client,monkeypatch,provider):
    body={'provider':provider,'base_url':'https://api.bochaai.com/v1' if provider=='bocha' else 'http://localhost:8888','api_key':'search-secret'}
    client.post('/api/search/settings',json=body)
    captured=[]
    def handler(request):
        captured.append(request)
        rows=[{'url':'https://www.gov.cn/fixture','name':'官网','title':'官网'}, {'url':'https://gov.cn.evil.example/','name':'伪装'}, {'url':'http://127.0.0.1/admin'}, {'url':'https://www.gov.cn/fixture'}]
        return httpx.Response(200,json={'code':200,'data':{'webPages':{'value':rows}}} if provider=='bocha' else {'results':rows})
    original=httpx.Client
    monkeypatch.setattr(websearch.httpx,'Client',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    rows=websearch.search('公开词')
    assert len(rows)==1 and rows[0]['url']=='https://www.gov.cn/fixture'
    req=captured[0]
    if provider=='bocha':
        assert req.headers['Authorization']=='Bearer search-secret'
        assert json.loads(req.content)['query'].startswith('公开词')
    else:assert req.url.params['format']=='json' and 'Authorization' not in req.headers


@pytest.mark.parametrize('provider,mode,expected',[('deepseek','enabled',('thinking',{'type':'enabled'})),('ollama','enabled',('reasoning_effort','high')),('siliconflow','disabled',('enable_thinking',False)),('custom','auto',None)])
def test_thinking_adapter_final_content_only(client,monkeypatch,provider,mode,expected):
    llm.save_settings({'provider':provider,'base_url':'http://localhost:9999/v1','model':'fixture','thinking_mode':mode,'api_key':''})
    bodies=[]
    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{"queries":["公开计划"]}','reasoning_content':'PRIVATE_INTERNAL_REASONING'}}]})
    original=httpx.Client
    monkeypatch.setattr(llm.httpx,'Client',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    value=llm.request_json('test',{},Plan)
    if expected:assert bodies[0][expected[0]]==expected[1]
    else:assert 'enable_thinking' not in bodies[0] and 'reasoning_effort' not in bodies[0]
    assert 'PRIVATE_INTERNAL' not in value.model_dump_json() and '/no_think' not in json.dumps(bodies)


def test_qwen_sse_ignores_reasoning_and_requires_finish(client,monkeypatch):
    llm.save_settings({'provider':'dashscope','base_url':'http://localhost:9999/v1','model':'fixture','thinking_mode':'enabled','api_key':''})
    def handler(req):
        body=json.loads(req.content);assert body['stream'] and body['enable_thinking']
        chunks=[{'choices':[{'delta':{'reasoning_content':'PRIVATE_INTERNAL_REASONING'}}]}, {'choices':[{'delta':{'content':'{"queries":["公开计划"]}'},'finish_reason':'stop'}]}]
        return httpx.Response(200,text='\n\n'.join('data: '+json.dumps(c) for c in chunks)+'\n\ndata: [DONE]\n\n')
    original=httpx.Client
    monkeypatch.setattr(llm.httpx,'Client',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    assert llm.request_json('test',{},Plan).queries==['公开计划']


def test_budget_exhaustion_stops_new_calls(client,corpus,monkeypatch):
    cid=setup(client); calls=[]; elapsed=[0]
    monkeypatch.setattr(engine.time,'monotonic',lambda:elapsed[0])
    def model(*args,**kwargs):
        calls.append(1);elapsed[0]=400;return Plan()
    monkeypatch.setattr(llm,'request_json',model)
    result=engine.answer(cid,'未支付价款怎么办')
    assert len(calls)==1 and not result['claims']
    assert any('研究总时限' in w for w in result['warnings'])


def test_search_failure_preserves_local_analysis(client,corpus,monkeypatch):
    cid=setup(client)
    def fail(*args):raise websearch.SearchError('测试搜索不可用')
    monkeypatch.setattr(websearch,'search',fail)
    def model(system,payload,schema,verify=False):
        if schema is FactExtraction:return FactExtraction(intent='supplement',facts=[])
        if schema is Plan:return Plan()
        if schema is Draft:return good(payload)
        return Verdict(checks=[{'claim_index':0,'supported':True,'reason':'fixture'}])
    monkeypatch.setattr(llm,'request_json',model)
    result=engine.answer(cid,'未支付价款怎么办',web_query='公开词')
    assert result['status']=='answered' and '测试搜索不可用' in result['warnings']


def test_agent_auth_required(client):
    client.post('/api/logout')
    for route in ['/api/research','/api/research/any','/api/search/settings']:
        assert client.get(route).status_code==401
    assert client.post('/api/research/any/cancel').status_code==401


def test_clarification_stops_unproductive_retries(client,corpus,monkeypatch):
    cid=setup(client); plans=[]
    def model(system,payload,schema,verify=False):
        if schema is FactExtraction:return FactExtraction(intent='supplement',facts=[])
        if schema is Plan:
            plans.append(1)
            raise AssertionError('缺事实不应启动检索规划')
        if schema is Draft:return good(payload)
        if schema is Repair:return Repair(patches=[{'claim_index':0,'claim':None}],missing_facts=['请补充约定付款日期？'])
        return Verdict(checks=[{'claim_index':0,'supported':False,'reason':'缺少事实'}])
    monkeypatch.setattr(llm,'request_json',model)
    result=engine.answer(cid,'未支付价款怎么办')
    assert result['rounds']==2 and result['model_calls']==3
    assert result['missing_facts']==['请补充约定付款日期？'] and not result['claims']
    assert not plans and result['status']=='needs_facts'
