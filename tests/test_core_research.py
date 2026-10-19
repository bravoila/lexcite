"""Core regressions. Deterministic model/document fixtures, NOT model accuracy tests."""
import json
import time
import pytest
from app import db, ingest, llm, sources, websearch
from app.research_agent import authorities, context as case_state, runner as engine, retrieval
from app.research_agent.schemas import Draft, Plan, Verdict, FactExtraction, SourceMetadata, ClarificationAudit

QUESTION='离婚后，彩礼可以要求对方返还吗'
SUPPLEMENT='双方是否办理了结婚登记？办了。 离婚前是否共同生活？若共同生活，持续时间大约多久？共同生活一年。 是否存在因给付彩礼导致给付方生活困难的情形？没有 彩礼是否已用于共同生活或生育抚养子女？没有。'
NAME=authorities.CATALOG[0]['name']
# Fabricated bodies solely for exercising source parsing and data flow.
TEXT=NAME+'\n法释〔2024〕1号\n'+ '\n'.join('第'+n+'条 彩礼纠纷测试条文用于自动化验证流程，不用于实际法律判断。' for n in '一二三四五六')+'\n第七条 本规定自2024年2月1日起施行。\n责任编辑：测试'


def create(client,rounds=3):
    llm.save_settings({'provider':'custom','base_url':'http://localhost:9999','model':'fixture','agent_rounds':rounds})
    return client.post('/api/cases',json={'title':'核心流程回归','event_date':'2025-01-01'}).json()['id']


def insert(cid,role,payload):
    return db.add_message(cid,role,payload)


def test_exact_question_answer_supplement_preserves_four_facts(client):
    cid=create(client);insert(cid,'user',{'text':QUESTION})
    pairs=case_state.question_answer_pairs(SUPPLEMENT)
    assert len(pairs)==4
    value=case_state.load(cid,SUPPLEMENT,[])
    assert value['research_focus']==QUESTION and len(value['facts'])==4
    assert [x['answer'] for x in value['facts']]==['办了','共同生活一年','没有','没有']
    assert not case_state.filter_questions([p['question'] for p in pairs],value['facts'])[0]
    assert not any(f['text']=='没有子女' for f in value['facts'])


def test_short_answer_paired_only_when_unambiguous(client):
    cid=create(client);insert(cid,'user',{'text':QUESTION})
    insert(cid,'assistant',{'missing_facts':['双方是否登记结婚？'],'claims':[{'text':'臆造事实'}]})
    v=case_state.load(cid,'办了',[])
    assert v['facts'][0]['answer']=='办了' and v['facts'][0]['question']=='双方是否登记结婚？'
    assert '臆造' not in json.dumps(v,ensure_ascii=False)
    insert(cid,'assistant',{'missing_facts':['是否登记？','是否生育子女？']})
    assert not any(f.get('question') for f in case_state.load(cid,'没有',[])['facts'])


def test_semantic_memory_persisted_and_new_correction_wins(client):
    cid=create(client);insert(cid,'user',{'text':QUESTION})
    v=case_state.load(cid,SUPPLEMENT,[])
    pair=case_state.question_answer_pairs(SUPPLEMENT)[0]
    case_state.apply_extraction(v,FactExtraction(intent='supplement',facts=[{'topic':pair['question'],'text':'双方已办理结婚登记','state':'stated','source_id':'current','quote':pair['quote'],'question':pair['question']}]),SUPPLEMENT)
    mid=insert(cid,'user',{'text':SUPPLEMENT})
    with db.connect() as c:case_state.persist(c,cid,{'case_facts':v['facts'],'research_focus':v['research_focus']},mid)
    db.init()
    assert any(f.get('semantic') for f in case_state.load(cid,'继续',[])['facts'])
    insert(cid,'user',{'text':'双方是否办理了结婚登记？更正：没有办理。'})
    facts=case_state.load(cid,'继续',[])['facts']
    corrected=[f for f in facts if f['topic']==case_state.signature(pair['question'])]
    assert len(corrected)==1 and '更正' in corrected[0]['quote'] and corrected[0]['state']=='stated'


def test_semantic_extraction_rejects_fabricated_quotes_and_no_child_inference(client):
    cid=create(client);v=case_state.load(cid,SUPPLEMENT,[])
    original=json.dumps(v['facts'])
    extracted=FactExtraction(intent='supplement',facts=[
        {'topic':'登记','text':'未登记','state':'stated','source_id':'current','quote':'用户不存在的原话'},
        {'topic':'有无子女','text':'没有子女','state':'stated','source_id':'current','quote':'彩礼是否已用于共同生活或生育抚养子女？没有。'}])
    case_state.apply_extraction(v,extracted,SUPPLEMENT)
    assert json.dumps(v['facts'])==original


def test_single_character_cannot_suppress_unrelated_question(client):
    audit=ClarificationAudit(checks=[{'question_index':0,'state':'answered','source_id':'s','quote':'没'}])
    assert case_state.grounded_audit(['是否育有子女？'],audit,[{'id':'s','text':'没有生活困难'}])[0]


def test_article_number_never_crosses_explicit_law_identity(client,corpus):
    rows,_=retrieval.search(['《不存在的司法解释》第五百七十九条'],'2025-01-01','全国')
    assert rows==[]
    rows,_=retrieval.search(['《中华人民共和国民法典》第五百七十九条'],'2025-01-01','全国')
    assert rows[0]['number']=='第五百七十九条'


def test_standard_name_can_fix_filename_without_reimport(client,corpus):
    lid=corpus['law']['id']
    with db.connect() as c:c.execute('UPDATE laws SET name=? WHERE id=?',('mingfa',lid))
    assert retrieval.authority_gaps([{'name':'中华人民共和国民法典','articles':['第五百七十九条']}],'2025-01-01','全国')
    response=client.post('/api/laws/'+lid+'/review',json={'confirmed':True,'valid_from':'2021-01-01','canonical_name':'中华人民共和国民法典','document_number':'测试文号'})
    assert response.status_code==200
    rows,_=retrieval.search(['《中华人民共和国民法典》第五百七十九条'],'2025-01-01','全国')
    assert rows[0]['name']=='中华人民共和国民法典' and rows[0]['display_name']=='mingfa'
    assert not retrieval.authority_gaps([{'name':'中华人民共和国民法典','articles':['第五百七十九条']}],'2025-01-01','全国')


def test_duplicate_versions_use_standard_identity(client,corpus):
    value=json.loads((db.ROOT/'examples/civil_excerpt.json').read_text())
    value.update(name='another-file',canonical_name='中华人民共和国民法典',version='重复版本')
    lid=client.post('/api/laws',json=value).json()['id']
    client.post('/api/laws/'+lid+'/review',json={'confirmed':True,'valid_from':'2021-01-01'})
    rows,notes=retrieval.corpus('2025-01-01','全国')
    assert not rows and any('冲突' in n for n in notes)


def page(text=TEXT):return {'text':text,'source_url':authorities.CATALOG[0]['url']}


def test_official_fulltext_metadata_and_snapshot(client):
    rows=authorities.validate_page(page(),NAME,entry=authorities.CATALOG[0])
    assert len(rows)==7 and rows[0]['authority_status']=='official_auto_checked'
    assert rows[0]['reviewed_at'] is None and len(rows[0]['sha256'])==64
    assert rows[0]['temporal_provisions'] and '责任编辑' not in rows[-1]['body']


@pytest.mark.parametrize('text',[TEXT.replace('第三条','第八条'),TEXT.replace('2024年2月1日','2025年2月1日'),TEXT.replace('〔2024〕1号','〔2024〕2号'),TEXT.replace(NAME,'另一个文件'),TEXT.replace('第七条 本规定自2024年2月1日起施行。','')])
def test_wrong_metadata_or_truncated_law_not_accepted(client,text):
    with pytest.raises(ValueError):authorities.validate_page(page(text),NAME,entry=authorities.CATALOG[0])


def test_offline_gap_not_fact_question_and_unrelated_candidates_not_citations(client,corpus,monkeypatch):
    cid=create(client);calls=[]
    original_search=retrieval.search
    monkeypatch.setattr(retrieval,'search',lambda queries,*args,**kwargs:original_search(['未支付价款'],*args,**kwargs))
    def model(system,payload,schema,verify=False):
        calls.append(schema)
        if schema is Plan:return Plan(queries=['未支付价款'],authorities=[{'name':NAME,'articles':['第五条'],'reason':'需要专项依据'}])
        if schema is Draft:return Draft(coverage='missing',missing_authorities=[{'name':NAME,'articles':['第五条'],'reason':'候选仅为合同条文，无法支持彩礼争点'}])
        raise AssertionError('无相关结论时不应进入复核')
    monkeypatch.setattr(llm,'request_json',model)
    monkeypatch.setattr(sources,'read_official',lambda *a:pytest.fail('offline must not fetch'))
    r=engine.answer(cid,QUESTION,online_research=False)
    assert r['status']=='missing_sources' and r['source_gaps'] and not r['missing_facts'] and not r['citations']
    assert r['candidate_sources'] and r['rounds']==1 and calls==[Draft]
    assert not any(e['step']=='引用校验完成' for e in r['trace'])


@pytest.mark.parametrize('has_gap',[False,True])
@pytest.mark.parametrize('supported',[False,True])
def test_combined_analysis_publishes_only_verified_partial_conclusions(client,corpus,monkeypatch,has_gap,supported):
    cid=create(client,rounds=1);calls=[];draft_evidence=[]
    needs=[{'name':'最高人民法院关于测试争议的规定','articles':['第一条'],'reason':'测试专项条文可能影响个案判断，尚待核对'}] if has_gap else []
    monkeypatch.setattr(sources,'read_official',lambda *a:pytest.fail('offline must not fetch'))
    def model(system,payload,schema,verify=False):
        calls.append(schema)
        if schema is Plan:return Plan(queries=['未支付价款'],authorities=needs)
        if schema is Draft:
            assert payload['analysis_scope']=='available_sources'
            row=payload['evidence'][0]
            draft_evidence.append(row)
            return Draft(coverage='partial',insufficient=False,missing_authorities=needs,claims=[{'text':'这项合成结论仅用于验证已有资料的部分分析流程，不是法律意见。','kind':'rule','section':'answer','evidence':[{'article_id':row['id'],'quote':row['body']}]}])
        if schema is Verdict:return Verdict(checks=[{'claim_index':0,'supported':supported,'reason':'合成支持性校验'}])
        raise AssertionError(schema)
    monkeypatch.setattr(llm,'request_json',model)
    response=client.post(f'/api/cases/{cid}/ask',json={'text':'未支付价款怎么办？','online_research':False})
    assert response.status_code==200,response.text
    result=response.json()
    assert calls==[Draft,Verdict]
    assert result['analysis_scope']=='available_sources' and result['coverage']['coverage']=='partial'
    assert bool(result['source_gaps'])==has_gap and result['missing_facts']==[]
    assert result['status']==('partial' if supported else 'missing_sources' if has_gap else 'no_supported_answer')
    assert bool(result['claims'])==bool(result['citations'])==supported
    exported=client.get(f'/api/cases/{cid}/export').text
    if supported:
        assert result['citations'][0]['id']==draft_evidence[0]['id']
        assert '现有资料' in result['summary'] and '完整解决' in result['summary']
        assert '基于现有资料的结论' in exported
        if has_gap:
            assert exported.index('基于现有资料的结论')<exported.index('推荐补充资料')
            assert '建议补充原因：'+needs[0]['reason'] in exported
    else:
        assert '基于现有资料的结论' not in exported
    if has_gap:
        assert needs[0]['name'] not in {r['name'] for r in result['citations']}
        assert '推荐补充资料' in exported and '不作为本次结论依据' in exported
    saved=client.get(f'/api/cases/{cid}').json()['messages'][-1]['payload']
    assert saved['claims']==result['claims'] and saved['source_gaps']==result['source_gaps']


def test_combined_analysis_cannot_cite_nonexistent_article(client,corpus,monkeypatch):
    cid=create(client,rounds=1)
    def model(system,payload,schema,verify=False):
        if schema is Draft:return Draft(claims=[{'text':'不得发布的合成结论','kind':'rule','evidence':[{'article_id':'fabricated-id','quote':'不存在的合成测试原文'}]}])
        raise AssertionError('非法证据不得进入复核')
    monkeypatch.setattr(llm,'request_json',model)
    result=engine.answer(cid,'未支付价款怎么办？',online_research=False)
    assert not result['claims'] and not result['citations']
    assert result['coverage']['selected_ids']==[] and result['coverage']['coverage']=='missing'
    assert any(e['step']=='引用校验完成' and '通过 0 项' in e['detail'] for e in result['trace'])


def test_empty_draft_does_not_report_successful_citation_validation(client,corpus,monkeypatch):
    cid=create(client)
    def model(system,payload,schema,verify=False):
        if schema is Plan:return Plan(queries=['未支付价款'])
        if schema is Draft:return Draft(insufficient=True,missing_facts=['彩礼返还的法律依据未在evidence中提供'])
        raise AssertionError(schema)
    monkeypatch.setattr(llm,'request_json',model)
    r=engine.answer(cid,QUESTION)
    assert r['legal_issues'] and not r['missing_facts']
    assert not any(e['step']=='引用校验完成' for e in r['trace'])
    assert any(e['step']=='尚未形成带依据的结论' for e in r['trace'])


def test_online_acquires_official_text_and_persists_followup(client,monkeypatch):
    cid=create(client);fetches=[];draft_context=[]
    # Limit catalog to one full-text fixture. No canned answer in production.
    monkeypatch.setattr(authorities,'directory_for',lambda topic:[{'name':NAME,'articles':['第五条'],'reason':'专项依据'}])
    def read(url):fetches.append(url);return page()
    monkeypatch.setattr(sources,'read_official',read)
    monkeypatch.setattr(websearch,'search',lambda *a:pytest.fail('known official URL needs no search API'))
    def model(system,payload,schema,verify=False):
        if schema is FactExtraction:return FactExtraction(intent='supplement',facts=[])
        if schema is Plan:return Plan(queries=['彩礼纠纷'],authorities=[{'name':NAME,'articles':['第五条']}])
        if schema is Draft:
            draft_context.append(payload);row=payload['evidence'][0]
            return Draft(insufficient=False,claims=[{'text':'这是用于验证引用流程的测试结论。','kind':'rule','section':'answer','evidence':[{'article_id':row['id'],'quote':row['body']}]}])
        if schema is Verdict:return Verdict(checks=[{'claim_index':0,'supported':True,'reason':'Synthetic fixture only'}])
        raise AssertionError(schema)
    monkeypatch.setattr(llm,'request_json',model)
    first=client.post(f'/api/cases/{cid}/ask',json={'text':QUESTION,'online_research':True}).json()
    second=client.post(f'/api/cases/{cid}/ask',json={'text':SUPPLEMENT,'online_research':True}).json()
    assert first['status']==second['status']=='answered'
    assert len(second['case_facts'])==4 and second['research_focus']==QUESTION
    assert len(draft_context[-1]['user_stated_facts'])==4
    assert second['claims'][0]['section']=='answer'
    assert second['citations'][0]['name']==NAME and second['citations'][0]['authority_status']=='official_auto_checked'
    assert len(client.get('/api/laws').json())==0 # temporary evidence never silently imported
    db.init()
    detail=client.get(f'/api/cases/{cid}').json()
    assert len(detail['observed_facts'])==4 and len(detail['messages'])==4
    assert len(fetches)==2


def test_online_failure_preserves_gap_and_does_not_loop_identically(client,monkeypatch):
    cid=create(client);attempts=[]
    monkeypatch.setattr(authorities,'directory_for',lambda _: [{'name':NAME,'articles':[]}])
    monkeypatch.setattr(llm,'request_json',lambda system,payload,schema,verify=False:schema())
    def fail(url):attempts.append(url);raise ValueError('官网拒绝访问')
    monkeypatch.setattr(sources,'read_official',fail)
    r=engine.answer(cid,QUESTION,online_research=True)
    assert r['status']=='missing_sources' and len(attempts)==1 and not r['citations']
    assert r['web_sources'][0]['state']=='read_failed' and '拒绝访问' in r['web_sources'][0]['note']


def test_official_fetch_date_and_reviewed_version_guard(client,monkeypatch):
    monkeypatch.setattr(sources,'read_official',lambda _:page())
    col=authorities.ResearchSources(None,lambda:None,time.monotonic()+120)
    assert not col.acquire([{'name':NAME}],'2023-01-01','全国',lambda *a:None)
    assert '尚未施行' in col.leads[0]['note']
    law={'name':NAME,'version':'已核对版本','jurisdiction':'全国','kind':'司法解释','valid_from':'2024-02-01','source_url':authorities.CATALOG[0]['url'],'text':TEXT}
    lid=client.post('/api/laws',json=law).json()['id']
    client.post('/api/laws/'+lid+'/review',json={'confirmed':True,'valid_from':'2024-02-01'})
    col=authorities.ResearchSources(None,lambda:None,time.monotonic()+120)
    assert not col.acquire([{'name':NAME}],'2025-01-01','全国',lambda *a:None)
    assert '日期重叠' in col.leads[0]['note']


def test_auto_search_public_title_only(client,monkeypatch):
    title='最高人民法院关于测试合同争议的规定';queries=[]
    monkeypatch.setattr(websearch,'settings',lambda:{'provider':'bocha'})
    monkeypatch.setattr(websearch,'search',lambda q,*a:queries.append(q) or [])
    col=authorities.ResearchSources(None,lambda:None,time.monotonic()+120)
    col.acquire([{'name':title,'reason':'当事人张某身份证123456789012345678'}],'2025-01-01','全国',lambda *a:None)
    assert queries==[title+' 官方全文 施行']
    with pytest.raises(ValueError):authorities.public_query('张某的案件解释')


def test_changed_duration_requires_correction(client):
    cid=create(client);insert(cid,'user',{'text':'共同生活多久？一年。'})
    state=case_state.load(cid,'共同生活多久？三年。',[])
    assert state['facts'][0]['state']=='conflicting'
    state=case_state.load(cid,'共同生活多久？更正，三年。',[])
    assert state['facts'][0]['state']=='stated'


def test_unknown_official_metadata_requires_all_original_quotes(client):
    name='最高人民法院关于测试纠纷的规定'
    text=name+'\n最高人民法院\n第一条 测试专用条文，不是真实法律。\n第二条 本规定自2024年2月1日起施行。'
    meta=SourceMetadata(is_primary_law=True,name=name,title_quote=name,effective_from='2024-02-01',effective_quote='本规定自2024年2月1日起施行。',jurisdiction='全国',jurisdiction_quote='最高人民法院',kind='司法解释',contains_complete_text=True)
    assert len(authorities.validate_page(page(text),name,meta))==2
    meta.effective_quote='自2025年1月1日起施行'
    with pytest.raises(ValueError):authorities.validate_page(page(text),name,meta)


def test_live_harness_isolated_storage_and_report(client,monkeypatch,tmp_path):
    """Tests the runner with a fixture engine; does not invoke a real model."""
    import importlib.util,sys
    spec=importlib.util.spec_from_file_location('eval_core',db.ROOT/'scripts/evaluate_core.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    original_case=create(client)
    original_dir=str(db.data_dir())
    def fake(cid,text,**kwargs):
        assert str(db.data_dir())!=original_dir
        with db.connect() as c:assert not c.execute('SELECT id FROM cases WHERE id=?',(original_case,)).fetchone()
        state=case_state.load(cid,text,[])
        return {'status':'missing_sources','claims':[],'citations':[],'case_facts':state['facts'],'research_focus':state['research_focus'],'model_errors':[]}
    monkeypatch.setattr(engine,'answer',fake)
    target=tmp_path/'live-runner-fixture.json'
    monkeypatch.setattr(sys,'argv',['evaluate_core.py','--run-live','--output',str(target)])
    module.main()
    report=json.loads(target.read_text())
    assert len(report['turns'])==3 and report['checks']['fact_pairs_covered_after_followup']==[4,4]
    assert str(db.data_dir())==original_dir
    assert len(client.get('/api/cases').json())==1


def test_auto_search_failure_keeps_supported_local_part(client,corpus,monkeypatch):
    cid=create(client,rounds=1)
    missing={'name':'最高人民法院关于测试争议的规定','reason':'测试缺口'}
    monkeypatch.setattr(websearch,'settings',lambda:{'provider':'bocha'})
    def fail(*a,**kw):raise websearch.SearchError('测试搜索故障')
    monkeypatch.setattr(websearch,'search',fail)
    def model(system,payload,schema,verify=False):
        if schema is Plan:return Plan(queries=['未支付价款'],authorities=[missing])
        if schema is Draft:
            row=payload['evidence'][0]
            return Draft(insufficient=True,claims=[{'text':'部分测试规则保留。','kind':'rule','evidence':[{'article_id':row['id'],'quote':row['body']}]}],missing_authorities=[missing])
        if schema is Verdict:return Verdict(checks=[{'claim_index':0,'supported':True,'reason':'测试支持'}])
        raise AssertionError(schema)
    monkeypatch.setattr(llm,'request_json',model)
    r=engine.answer(cid,'未支付价款怎么办？',online_research=True)
    assert r['status']=='partial' and r['claims'] and r['source_gaps']
    assert r['web_sources'][0]['state']=='search_failed'


def test_model_paraphrase_cannot_reverse_literal_qa_answer(client):
    cid=create(client);state=case_state.load(cid,SUPPLEMENT,[])
    pair=case_state.question_answer_pairs(SUPPLEMENT)[0]
    value=FactExtraction(intent='supplement',facts=[{'topic':'登记结婚','text':'双方没有登记结婚','source_id':'current','quote':pair['quote'],'state':'stated'}])
    case_state.apply_extraction(state,value,SUPPLEMENT)
    assert any(f.get('answer')=='办了' for f in state['facts'])
    assert not any(f['text']=='双方没有登记结婚' for f in state['facts'])
