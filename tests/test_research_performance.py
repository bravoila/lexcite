"""Deterministic request-cost regressions, not model speed or accuracy benchmarks."""
import json

import pytest

from app import db, indexing, llm, retrieval_services as services, sources
from app.research_agent import authorities, retrieval, runner
from app.research_agent.schemas import Draft, Repair, Verdict


@pytest.fixture
def research_case(client):
    llm.save_settings({'provider':'custom','base_url':'http://localhost:9999/v1','model':'fixture','agent_rounds':3})
    return client.post('/api/cases',json={'title':'研究成本回归','event_date':'2025-01-01'}).json()['id']


@pytest.fixture
def official_page(monkeypatch):
    entry=authorities.CATALOG[0]
    text=entry['name']+'\n法释〔2024〕1号\n'
    text+='\n'.join('第'+number+'条 合成价款测试条文仅用于校验研究流程，不是真实法律。' for number in '一二三四五六')
    text+='\n第七条 本规定自2024年2月1日起施行。'
    monkeypatch.setattr(sources,'read_official',lambda url:{'source_url':url,'text':text})
    return {'name':entry['name'],'articles':['第五条'],'reason':'合成补查需求'}


def draft_for(payload, **kwargs):
    row=payload['evidence'][0]
    return Draft(insufficient=False,claims=[{'text':'合成条件分析，仅用于验证引用流程。','kind':'inference',
        'evidence':[{'article_id':row['id'],'quote':row['body']}]}],**kwargs)


def test_official_supplement_reuses_local_embedding_and_reranking(client,corpus,research_case,official_page,monkeypatch):
    cfg=services.RetrievalSettings().model_dump()
    cfg['auto_index']=False
    cfg['embedding'].update(provider='custom',base_url='http://localhost:9998/v1',model='fixture-embedding')
    cfg['reranker'].update(provider='custom',base_url='http://localhost:9998/v1',model='fixture-reranker')
    services.save_settings(cfg)
    embeds=[];reranks=[]
    def embed(texts,**kwargs):
        embeds.append(list(texts))
        return [[1.,0.] for text in texts]
    def rerank(query,documents,**kwargs):
        reranks.append((query,list(documents)))
        return [(i,1.) for i in range(len(documents))]
    monkeypatch.setattr(services,'embed',embed)
    monkeypatch.setattr(services,'rerank',rerank)
    indexing.build();embeds.clear()
    monkeypatch.setattr(authorities,'directory_for',lambda topic:[official_page])
    def model(system,payload,schema,verify=False):
        if schema is Draft:return draft_for(payload)
        assert schema is Verdict and verify
        return Verdict(checks=[{'claim_index':0,'supported':True,'reason':'合成测试'}])
    monkeypatch.setattr(llm,'request_json',model)
    for invocation in range(1,3):
        result=runner.answer(research_case,'未支付价款怎么办？',online_research=True)
        assert result['status']=='answered'
        assert len(embeds)==invocation and len(reranks)==2*invocation
        local=[run for run in result['retrieval_runs'] if run['scope']=='local']
        assert [run['cache_hit'] for run in local]==[False,True]
        assert local[1]['embedding_requests']==local[1]['rerank_requests']==local[1]['reranked_candidates']==0
        assert '复用' in local[1]['summary']
        assert sum(run['embedding_requests'] for run in result['retrieval_runs'])==1
        assert sum(run['rerank_requests'] for run in result['retrieval_runs'])==2
        assert result['citations'][0]['authority_status']=='official_auto_checked'
        assert len(result['citations'][0]['sha256'])==64


@pytest.mark.parametrize('changed',['queries','date','jurisdiction','limit','configuration','corpus','review_days'])
def test_local_search_cache_respects_search_identity(client,corpus,monkeypatch,changed):
    calls=[];events=[];original=retrieval.rank
    def rank(*args,**kwargs):
        calls.append(1)
        return original(*args,**kwargs)
    monkeypatch.setattr(retrieval,'rank',rank)
    args={'queries':['未支付价款','履行义务'],'event_date':'2025-01-01','jurisdiction':'全国'}
    with retrieval.reuse_searches(),retrieval.observe(events.append):
        first=retrieval.search(**args)
        assert retrieval.search(**args)==first and len(calls)==1
        if changed=='queries':args['queries']=list(reversed(args['queries']))
        elif changed=='date':args['event_date']='2025-02-01'
        elif changed=='jurisdiction':args['jurisdiction']='北京市'
        elif changed=='limit':args['limit']=5
        elif changed=='configuration':
            cfg=services.settings();cfg['final_k']=5;services.save_settings(cfg)
        elif changed=='corpus':
            with db.connect() as c:c.execute('UPDATE laws SET document_number=? WHERE id=?',('changed',corpus['law']['id']))
        else:llm.save_settings({'review_days':181})
        retrieval.search(**args)
    assert len(calls)==2 and [event['cache_hit'] for event in events]==[False,True,False]


def test_cached_rows_are_isolated_and_do_not_survive_request_scope(client,corpus,monkeypatch):
    calls=[];original=retrieval.rank
    def rank(*args,**kwargs):
        calls.append(1)
        return original(*args,**kwargs)
    monkeypatch.setattr(retrieval,'rank',rank)
    args=(['未支付价款'],'2025-01-01','全国')
    with retrieval.reuse_searches():
        rows,notes=retrieval.search(*args)
        original_body=rows[0]['body']
        rows[0]['body']='mutated'
        rows[0]['retrieval_channels'].append('mutated')
        notes.append('mutated')
        cached,cached_notes=retrieval.search(*args)
        assert cached[0]['body']==original_body and 'mutated' not in cached[0]['retrieval_channels']
        assert 'mutated' not in cached_notes and len(calls)==1
    retrieval.search(*args)
    with retrieval.reuse_searches():retrieval.search(*args)
    assert len(calls)==3


def test_failed_retrieval_service_is_retried_instead_of_cached(client,corpus,monkeypatch):
    cfg=services.RetrievalSettings().model_dump()
    cfg['auto_index']=False
    cfg['reranker'].update(provider='custom',base_url='http://localhost:9998/v1',model='fixture-reranker')
    services.save_settings(cfg);calls=[];events=[]
    def rerank(query,documents,**kwargs):
        calls.append(1)
        if len(calls)==1:raise llm.ModelError('合成临时重排错误')
        return [(i,1.) for i in range(len(documents))]
    monkeypatch.setattr(services,'rerank',rerank)
    with retrieval.reuse_searches(),retrieval.observe(events.append):
        first=retrieval.search(['未支付价款'],'2025-01-01','全国')
        second=retrieval.search(['未支付价款'],'2025-01-01','全国')
        third=retrieval.search(['未支付价款'],'2025-01-01','全国')
    assert len(calls)==2 and [event['cache_hit'] for event in events]==[False,False,True]
    assert '重排失败' in ''.join(first[1]) and '重排失败' not in ''.join(second[1])
    assert second==third


def test_stage_inputs_keep_full_evidence_and_facts_without_display_metadata(client,corpus,research_case,monkeypatch):
    question='未支付价款怎么办？'
    history='双方尚未确认付款时间，是否支付价款未知。'
    db.add_message(research_case,'user',{'text':question})
    db.add_message(research_case,'user',{'text':history})
    original_search=retrieval.search;original_rows=[];inputs=[];reviews=[]
    temporal='合成过渡规定：施行前的事项须核对原规则，不是真实法律。'
    def search(*args,**kwargs):
        rows,notes=original_search(*args,**kwargs)
        rows=[row|{'temporal_provisions':[temporal],'version_note':'合成版本警告',
            'version_basis':{'name':'合成修正案','effective_from':'2021-01-01','quote':'合成施行日期原文',
                'url':'https://www.gov.cn/fixture','sha256':'0'*64,'fetched_at':'2025-01-01'},
            'source_checks':['合成展示元数据'*100],'source_name':'合成来源展示名称'*100} for row in rows]
        original_rows.extend(rows)
        return rows,notes
    monkeypatch.setattr(retrieval,'search',search)
    def model(system,payload,schema,verify=False):
        inputs.append((schema,payload))
        assert history in payload['previous_user_messages_unconfirmed']
        assert question not in payload['previous_user_messages_unconfirmed']
        assert any(fact['state']=='unknown' for fact in payload['user_stated_facts'])
        assert {row['id']:row['body'] for row in payload['evidence']}=={row['id']:row['body'] for row in original_rows}
        for row in payload['evidence']:
            assert not {'sha256','source_url','source_name','source_checks','retrieval_score','temporal_provisions'} & row.keys()
            assert row['authority_status']=='human_reviewed' and row['valid_from']=='2021-01-01'
        assert payload['evidence_context']==[{'law_id':original_rows[0]['law_id'],'temporal_provisions':[temporal],
            'version_note':'合成版本警告','version_basis':{'name':'合成修正案','effective_from':'2021-01-01','quote':'合成施行日期原文'}}]
        old=payload|{'evidence':original_rows}
        assert len(json.dumps(payload,ensure_ascii=False))<len(json.dumps(old,ensure_ascii=False))
        if schema is Draft:
            draft=draft_for(payload)
            draft.claims.append(draft.claims[0].model_copy(update={'text':'另一项合成条件分析。'}))
            return draft
        if schema is Repair:
            failed=payload['failed_claims'][0]
            assert failed['claim_index']==1 and 'text' not in failed
            return Repair(patches=[{'claim_index':1,'claim':failed['claim']|{'text':'缩小后的合成条件分析。'}}])
        assert schema is Verdict and verify
        reviews.append(payload)
        assert all(ref['quote'] for claim in payload['draft'] for ref in claim['evidence'])
        if len(reviews)==1:
            return Verdict(checks=[{'claim_index':0,'supported':True,'reason':'合成支持'},
                {'claim_index':1,'supported':False,'reason':'请缩小结论','action':'rewrite'}])
        assert payload['verified_claims'][0]['evidence']==[{'article_id':payload['draft'][0]['evidence'][0]['article_id']}]
        return Verdict(checks=[{'claim_index':0,'supported':True,'reason':'合成支持'}])
    monkeypatch.setattr(llm,'request_json',model)
    result=runner.answer(research_case,question,online_research=False)
    assert result['status']=='answered' and [schema for schema,payload in inputs]==[Draft,Verdict,Repair,Verdict]
    assert all(payload['user_stated_facts']==result['case_facts'] for schema,payload in inputs)
    citation=result['citations'][0]
    assert citation['sha256'] and citation['source_name']==original_rows[0]['source_name']
    assert citation['temporal_provisions']==[temporal] and citation['version_basis']['url']
    assert [run['payload_characters'] for run in result['model_runs']]==[
        len(json.dumps(payload,ensure_ascii=False)) for schema,payload in inputs]


def test_late_source_reanalysis_does_not_resend_previous_quotes(client,corpus,research_case,official_page,monkeypatch):
    drafts=[]
    monkeypatch.setattr(authorities,'directory_for',lambda topic:[])
    def model(system,payload,schema,verify=False):
        if schema is Draft:
            drafts.append(payload)
            if len(drafts)==1:return draft_for(payload,missing_authorities=[official_page])
            previous=payload['previous_attempt']['claims'][0]
            first=drafts[0]['evidence'][0]
            assert previous['evidence']==[{'article_id':first['id']}]
            assert first['body'] not in json.dumps(payload['previous_attempt'],ensure_ascii=False)
            assert payload['evidence_context'][0]['temporal_provisions']
            return draft_for(payload)
        assert schema is Verdict and verify
        return Verdict(checks=[{'claim_index':0,'supported':True,'reason':'合成支持'}])
    monkeypatch.setattr(llm,'request_json',model)
    result=runner.answer(research_case,'未支付价款怎么办？',online_research=True)
    assert result['status']=='answered' and len(drafts)==2
    assert result['model_calls']==3 and result['citations'][0]['source_url']
