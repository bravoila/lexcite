"""Deterministic contracts/regressions. Synthetic vectors are not quality evidence."""
import json
import math
import pytest
from app import db, llm, indexing, retrieval_services as rs
from app.research_agent import retrieval


def configure(**kwargs):
    s=rs.RetrievalSettings().model_dump();s['auto_index']=False
    s['embedding'].update(provider='custom',base_url='http://localhost:9998/v1',model='synthetic-embedding',api_key='embedding-secret')
    for k,v in kwargs.items():s[k]=v
    rs.save_settings(s)
    return rs.settings()


def add_law(client,name='测试照护规则',body='第一条 监护人应当履行照管及保护职责。',date='2021-01-01'):
    value={'name':name,'version':'仅测试','valid_from':date,'text':body}
    r=client.post('/api/laws',json=value);assert r.status_code==200,r.text
    lid=r.json()['id'];assert client.post('/api/laws/'+lid+'/review',json={'confirmed':True,'valid_from':date}).status_code==200
    return client.get('/api/laws/'+lid).json()


def vector_stub(texts,**kw):
    return [[1.,0.] if ('照管' in t or '看娃' in t) else [0.,1.] for t in texts]


def test_independent_encrypted_credentials_and_endpoint_change(client):
    llm.save_settings({'provider':'deepseek','base_url':'https://api.deepseek.com/v1','model':'deepseek-chat','api_key':'chat-secret'})
    configure();assert rs.resolve('embedding')['api_key']=='embedding-secret'
    assert llm.resolved()['api_key']=='chat-secret'
    public=rs.public_settings();assert 'secret' not in json.dumps(public)
    public['embedding']['base_url']='http://localhost:9997/v1';rs.save_settings(public)
    assert rs.resolve('embedding')['api_key']==''


def test_legacy_configuration_migrates_once_without_auto_calls(client):
    with db.connect() as c:c.execute("DELETE FROM kv WHERE key='retrieval_settings'")
    llm.save_settings({'provider':'custom','base_url':'http://legacy:8080/v1','embedding_model':'legacy-embed','api_key':'old-secret'})
    rs.init();s=rs.settings();assert not s['auto_index'];assert rs.resolve('embedding')['api_key']=='old-secret'
    llm.save_settings({'provider':'custom','base_url':'http://changed:8080/v1','embedding_model':'different','api_key':'new-secret'})
    rs.init();assert rs.settings()['embedding']['model']=='legacy-embed'


@pytest.mark.parametrize('value',[[],[[0,0]],[[float('nan'),1]],[[float('inf'),1]],[[True,1]],[[1],[1,0]]])
def test_invalid_vectors_rejected(value):
    with pytest.raises(llm.ModelError):rs.validate_vectors(value,1,2)


def test_index_is_incremental_and_content_sensitive(client,monkeypatch):
    law=add_law(client);configure();calls=[]
    monkeypatch.setattr(rs,'embed',lambda texts,**kw:(calls.extend(texts) or vector_stub(texts)))
    assert indexing.build()['count']==1
    assert indexing.build()['count']==0
    with db.connect() as c:c.execute('UPDATE laws SET canonical_name=? WHERE id=?',('测试监护规范',law['law']['id']))
    assert indexing.status()['pending']==1
    rows,_=retrieval.corpus('2025-01-01','全国');assert not indexing.load_vectors(rows,rs.settings())
    assert indexing.build()['count']==1
    assert indexing.status()['ready']==1 and len(calls)==2


def test_index_batches_articles_and_runs_embedding_requests_in_parallel(client,monkeypatch):
    import threading,time
    for i in range(10):add_law(client,f'并行索引规则{i}','第一条 义务人应当依法履行法律规定的相关义务并承担相应责任。')
    configure();guard=threading.Lock();active=peak=0;batch_sizes=[]
    def embed_batch(texts,**kw):
        nonlocal active,peak
        with guard:
            active+=1;peak=max(peak,active);batch_sizes.append(len(texts))
        time.sleep(.03)
        with guard:active-=1
        return [[1.,0.] for _ in texts]
    monkeypatch.setattr(rs,'embed',embed_batch)
    assert indexing.build()['count']==10
    assert sorted(batch_sizes)==[2,8] and peak==2
    assert indexing.status()['ready']==10


def test_saved_retrieval_keys_take_precedence_over_environment(client,monkeypatch):
    configure();monkeypatch.setenv('EMBEDDING_API_KEY','env-embedding-secret')
    monkeypatch.setenv('RERANKER_API_KEY','env-reranker-secret')
    settings=rs.public_settings()
    assert not settings['embedding']['environment_api_key'] and settings['embedding']['has_api_key']
    assert rs.resolve('embedding')['api_key']=='embedding-secret'
    assert 'env-embedding-secret' not in json.dumps(settings)


def test_environment_settings_are_defaults_and_saved_user_values_win(client,monkeypatch):
    from app import websearch
    monkeypatch.setenv('MODEL_PROFILE','api')
    monkeypatch.setenv('MODEL_LOCAL_PROVIDER','ollama')
    monkeypatch.setenv('MODEL_CLOUD_PROVIDER','dashscope')
    monkeypatch.setenv('MODEL_CLOUD_BASE_URL','https://env.example/v1')
    monkeypatch.setenv('MODEL_CLOUD_NAME','env-chat')
    monkeypatch.setenv('MODEL_CLOUD_API_KEY','env-chat-key')
    monkeypatch.setenv('EMBEDDING_PROVIDER','dashscope')
    monkeypatch.setenv('EMBEDDING_BASE_URL','https://env.example/compatible-mode/v1')
    monkeypatch.setenv('EMBEDDING_MODEL','env-embedding')
    monkeypatch.setenv('EMBEDDING_API_KEY','env-embedding-key')
    monkeypatch.setenv('SEARCH_PROVIDER','bocha')
    assert llm.settings()['profile']=='api' and llm.settings()['model']=='env-chat'
    assert llm.resolved()['api_key']=='env-chat-key'
    assert llm.public_settings()['profiles']['local']['provider']=='ollama'
    assert llm.public_settings()['profiles']['api']['provider']=='dashscope'
    assert 'env-chat-key' not in json.dumps(llm.public_settings())
    assert rs.settings()['embedding']['model']=='env-embedding'
    assert rs.resolve('embedding')['api_key']=='env-embedding-key'
    assert websearch.settings()['provider']=='bocha'
    assert websearch.public_settings()['environment_defaults']

    llm.save_settings({'profile':'local','provider':'custom','base_url':'https://user.example/v1','model':'user-chat','api_key':'user-chat-key'})
    assert llm.settings()['model']=='user-chat' and llm.resolved()['api_key']=='user-chat-key'
    configured=rs.RetrievalSettings().model_dump()
    configured['embedding'].update(provider='custom',base_url='https://user.example/v1',model='user-embedding',api_key='user-embedding-key')
    rs.save_settings(configured)
    assert rs.settings()['embedding']['model']=='user-embedding'
    assert rs.resolve('embedding')['api_key']=='user-embedding-key'
    websearch.save_settings({'provider':'searxng','base_url':'http://search.example','api_key':'user-search-key','timeout':12})
    assert websearch.settings()['provider']=='searxng'
    public_search=websearch.public_settings()
    assert not public_search['environment_api_key'] and public_search['has_api_key']
    assert 'user-search-key' not in json.dumps(public_search)


def test_changed_profile_never_reuses_old_vectors(client,monkeypatch):
    add_law(client);configure();monkeypatch.setattr(rs,'embed',vector_stub);indexing.build()
    s=rs.public_settings();s['embedding']['dimensions']=3;rs.save_settings(s)
    assert indexing.status()['ready']==0
    rows,_=retrieval.corpus('2025-01-01','全国');assert not indexing.load_vectors(rows,rs.settings())


def test_failed_article_is_not_partially_published_and_retry_works(client,monkeypatch):
    add_law(client,body='第一条 '+('照管职责。'*600));configure(chunk_chars=300)
    calls=[]
    def fail(texts,**kw):
        calls.append(1)
        if len(calls)>1:raise llm.ModelError('测试失败')
        return vector_stub(texts)
    monkeypatch.setattr(rs,'embed',fail)
    result=indexing.build();assert result['failed']==1 and indexing.status()['ready']==0
    with db.connect() as c:assert c.execute('SELECT COUNT(*) FROM embedding_chunks').fetchone()[0]==0
    monkeypatch.setattr(rs,'embed',vector_stub);assert indexing.build()['count']==1
    assert indexing.status()['failed']==0


def test_semantic_recall_rerank_and_full_article_survive(client,monkeypatch):
    law=add_law(client);add_law(client,'测试道路规则','第一条 车辆应当靠右侧通行。')
    configure();monkeypatch.setattr(rs,'embed',vector_stub);indexing.build()
    rows,_=retrieval.corpus('2025-01-01','全国')
    lexical=dict(rs.public_settings());lexical['mode']='lexical';rs.save_settings(lexical)
    before,_=retrieval.search(['怎么帮人看娃'],'2025-01-01','全国');assert not before
    configure(reranker={'provider':'custom','base_url':'http://localhost:9999/v1','model':'mock-reranker'})
    monkeypatch.setattr(rs,'rerank',lambda q,docs,**kw:sorted(enumerate([1. if '照管' in d else 0. for d in docs]),key=lambda x:-x[1]))
    traces=[]
    with retrieval.observe(traces.append):result,warnings=retrieval.search(['怎么帮人看娃'],'2025-01-01','全国')
    assert result[0]['law_id']==law['law']['id'];assert result[0]['body']==law['articles'][0]['body']
    assert traces[0]['embedding_status']=='applied' and traces[0]['reranker_status']=='applied'
    assert 'dense' in result[0]['retrieval_channels'] and not warnings


def test_dates_and_review_filter_precede_dense_search(client,monkeypatch):
    add_law(client,date='2030-01-01');configure();monkeypatch.setattr(rs,'embed',vector_stub);indexing.build()
    calls=[];monkeypatch.setattr(rs,'embed',lambda *a,**kw:calls.append(1))
    rows,_=retrieval.search(['看娃'],'2025-01-01','全国');assert not rows and not calls


def test_exact_article_survives_low_rerank_score_and_other_law(client,monkeypatch):
    target=add_law(client);other=add_law(client,'测试道路规则','第一条 车辆应当靠右侧通行。')
    configure(reranker={'provider':'custom','base_url':'http://localhost:9999/v1','model':'mock-reranker'})
    monkeypatch.setattr(rs,'embed',vector_stub);indexing.build()
    monkeypatch.setattr(rs,'rerank',lambda q,docs,**kw:list(reversed([(i,float(i)) for i in range(len(docs))])))
    rows,_=retrieval.search(['《测试照护规则》第一条'],'2025-01-01','全国')
    assert rows[0]['law_id']==target['law']['id'] and all(r['law_id']!=other['law']['id'] for r in rows)


def test_provider_failure_has_explicit_degradation(client,monkeypatch):
    add_law(client);configure();monkeypatch.setattr(rs,'embed',vector_stub);indexing.build()
    def fail(*a,**kw):raise llm.ModelError('测试不可用')
    monkeypatch.setattr(rs,'embed',fail);trace=[]
    with retrieval.observe(trace.append):rows,notes=retrieval.search(['监护人 照管 保护 职责'],'2025-01-01','全国')
    assert rows and '降级' in ''.join(notes)
    assert trace[0]['embedding_status']=='failed' and trace[0]['embedding_requests']==1


def test_partial_coverage_reported(client,monkeypatch):
    add_law(client);configure();monkeypatch.setattr(rs,'embed',vector_stub);indexing.build();add_law(client,'测试道路规则','第一条 车辆应当靠右侧通行。')
    traces=[]
    with retrieval.observe(traces.append):retrieval.search(['照管职责'],'2025-01-01','全国')
    assert traces[0]['embedding_status']=='partial' and traces[0]['indexed_articles']==1


def test_invalid_rerank_does_not_drop_candidates(client,monkeypatch):
    add_law(client);configure(reranker={'provider':'custom','base_url':'http://localhost:9999/v1','model':'mock'})
    def invalid(*args,**kwargs):return {'results':[{'index':500,'relevance_score':.9}]}
    monkeypatch.setattr(rs,'post',invalid)
    result,notes=retrieval.search(['监护人 照管职责'],'2025-01-01','全国')
    assert result and '重排失败' in ''.join(notes)


def test_eval_metrics_separate_missing_law_from_retrieval_failure(client):
    import importlib.util
    spec=importlib.util.spec_from_file_location('retrieval_eval',db.ROOT/'scripts/evaluate_retrieval.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    expected=[{'law':'缺少的规范','article':'第五条'}]
    result=module.metrics([],expected,[])
    assert result['source_coverage']==0 and result['recall_at_20_available'] is None
    assert result['recall_at_20_all_required']==0 and result['missing_from_corpus']==expected


def test_no_low_corpus_bm25_threshold_blocks_exact_keyword(client):
    add_law(client)
    s=rs.RetrievalSettings().model_dump();s['mode']='lexical';rs.save_settings(s)
    rows,_=retrieval.search(['照管'],'2025-01-01','全国')
    assert rows


def test_index_worker_queues_during_research_and_cancel_preserves_data(client,monkeypatch):
    import time
    from app import jobs
    add_law(client);configure();calls=[]
    monkeypatch.setattr(rs,'embed',lambda texts,**kw:(calls.append(1) or vector_stub(texts)))
    jobs.MODEL_LOCK.acquire()
    try:
        assert client.post('/api/reindex').status_code==202
        assert indexing.status()['job']['status']=='queued'
        client.post('/api/reindex/cancel')
    finally:jobs.MODEL_LOCK.release()
    end=time.monotonic()+3
    while indexing._thread and time.monotonic()<end:time.sleep(.01)
    assert not calls and indexing.status()['ready']==0
    assert indexing.status()['job']['status']=='interrupted'
    client.post('/api/reindex')
    end=time.monotonic()+3
    while indexing._thread and time.monotonic()<end:time.sleep(.01)
    assert indexing.status()['ready']==1


def test_auto_index_after_new_import_and_review(client,monkeypatch):
    import time
    monkeypatch.setattr(rs,'embed',vector_stub)
    s=rs.RetrievalSettings().model_dump();s['embedding'].update(provider='custom',model='synthetic',base_url='http://localhost:9998/v1')
    assert client.post('/api/retrieval/settings',json=s).status_code==200
    add_law(client)
    end=time.monotonic()+3
    while indexing._thread and time.monotonic()<end:time.sleep(.01)
    # No missed scheduling at a worker's final iteration.
    assert indexing.status()['ready']==1


def test_service_configuration_does_not_change_during_index_or_research(client):
    from app import jobs
    jobs.MODEL_LOCK.acquire()
    try:
        assert client.post('/api/retrieval/settings',json=rs.RetrievalSettings().model_dump()).status_code==409
    finally:jobs.MODEL_LOCK.release()


def test_chapter_metadata_keeps_citation_body_unchanged(client):
    value=add_law(client,body='第一章 监护职责\n第一条 监护人应当履行照管及保护职责。')
    row=indexing.articles()[0]
    assert row['heading_path']=='第一章 监护职责'
    assert row['body']==value['articles'][0]['body']


def test_evaluation_harness_uses_isolated_laws_and_reports_three_actual_paths(client,monkeypatch,tmp_path):
    import importlib.util,sys
    add_law(client);configure(reranker={'provider':'custom','base_url':'http://localhost:9999/v1','model':'mock'})
    monkeypatch.setattr(rs,'embed',vector_stub)
    monkeypatch.setattr(rs,'rerank',lambda query,docs,**kw:[(i,1.) for i in range(len(docs))])
    client.post('/api/cases',json={'title':'不可复制的业务案件'})
    spec=importlib.util.spec_from_file_location('ablation_test',db.ROOT/'scripts/evaluate_retrieval.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    dataset=tmp_path/'scenarios.json';output=tmp_path/'report.json'
    dataset.write_text(json.dumps({'name':'synthetic only','cases':[{'id':'one','query':'监护人 照管职责','event_date':'2025-01-01','expected':[{'law':'测试照护规则','article':'第一条'}]}]}))
    monkeypatch.setattr(sys,'argv',['evaluate_retrieval.py','--run-live','--build-index','--dataset',str(dataset),'--output',str(output)])
    module.main();report=json.loads(output.read_text())
    assert len(report['runs'])==3 and all(r['executed_as_requested'] for r in report['runs'])
    assert all(r['metrics']['recall_at_20_available']==1 for r in report['runs'])
    assert 'secret' not in output.read_text() and '不可复制的业务案件' not in output.read_text()
    assert indexing.status()['ready']==0
    assert len(client.get('/api/cases').json())==1
