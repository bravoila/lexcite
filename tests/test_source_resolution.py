"""Regressions for wrapped names, criminal-law lookup and actionable failures.

Full-law bodies below are deliberately synthetic; no legal accuracy score.
"""
import datetime as dt
import time
import pytest
from app import db, ingest, llm, sources, websearch
from app.research_agent import authorities, runner as engine, retrieval
from app.law_names import clean_title, display_title
from app.research_agent.schemas import AuthorityNeed, Plan, CoverageAudit, Draft, Verdict

from source_fixture_data import CRIMINAL, BASIS, CRIMINAL_TEXT


def mock_page(url, require_articles=True):
    return {'source_url':url,'text':CRIMINAL_TEXT if require_articles else BASIS}


def collector():
    return authorities.ResearchSources(None,lambda:None,time.monotonic()+240)


@pytest.mark.parametrize('raw,expected',[
    (' 《中华人民共和国刑法》 ','中华人民共和国刑法'),
    ('《《中华人民共和国刑法》》','中华人民共和国刑法'),
    ('〈刑法〉','刑法'),
    ('《最高人民法院关于适用〈中华人民共和国民法典〉婚姻家庭编的解释（一）》','最高人民法院关于适用〈中华人民共和国民法典〉婚姻家庭编的解释（一）'),
    ('《甲法》与《乙法》','《甲法》与《乙法》'),
])
def test_outer_book_title_only(raw,expected):
    assert clean_title(raw)==expected
    assert AuthorityNeed(name=raw).name==expected
    assert display_title(raw)=='《'+expected+'》'


def test_wrapped_name_matches_existing_library_and_public_query(client,corpus):
    name='《《中华人民共和国民法典》》'
    assert not retrieval.authority_gaps([{'name':name,'articles':['第五百七十九条']}],'2025-01-01','全国')
    assert authorities.public_query('《中华人民共和国刑法》')=='中华人民共和国刑法 官方全文 施行'
    assert authorities.public_query('刑法')=='中华人民共和国刑法 官方全文 施行'
    assert authorities.public_query('《最高人民法院关于测试纠纷的规定》').startswith('最高人民法院')
    assert engine.unique_needs([{'name':name,'articles':[]}])[0]['name']=='中华人民共和国民法典'
    with pytest.raises(ValueError):authorities.public_query('《张某案件解释》')


def test_criminal_no_search_key_required_and_date_separate(client,monkeypatch):
    calls=[]
    def read(url,**kw):calls.append((url,kw));return mock_page(url,**kw)
    monkeypatch.setattr(sources,'read_official',read)
    monkeypatch.setattr(websearch,'search',lambda *a:pytest.fail('catalog lookup must not require search'))
    col=collector();rows=col.acquire([{'name':'《中华人民共和国刑法》','articles':['第十五条','第二百三十三条']}],'2026-09-25','全国',lambda *a:None)
    assert len(rows)==453 and col.searches==0 and col.reads==2
    assert rows[0]['valid_from']=='2024-03-01' and rows[0]['version_basis']['quote']=='2024年3月1日起施行'
    assert calls[-1][1]=={'require_articles':False}
    with db.connect() as c:assert len(c.execute('SELECT * FROM laws').fetchall())==0


def test_criminal_catalog_and_date_mirror_fallback_with_four_read_bound(client,monkeypatch):
    calls=[]
    def read(url,**kw):
        calls.append(url)
        if url in (CRIMINAL['url'],CRIMINAL['effective_source']):raise ValueError('官网 HTTP 503')
        return mock_page(url,**kw)
    monkeypatch.setattr(sources,'read_official',read)
    col=collector();rows=col.acquire([{'name':'刑法'}],'2026-09-25','全国',lambda *a:None)
    assert rows and len(calls)==col.reads==4 and col.searches==0
    assert rows[0]['source_url']==CRIMINAL['fallback_urls'][0]
    assert rows[0]['version_basis']['url']==CRIMINAL['effective_fallback_urls'][0]


@pytest.mark.parametrize('change', ['old_revision','new_revision','missing_article','wrong_basis','wrong_date'])
def test_criminal_rejects_incomplete_wrong_version_or_date(client,change):
    page=mock_page(CRIMINAL['url']);basis=mock_page(CRIMINAL['effective_source'],False)
    if change=='old_revision':page['text']=page['text'].replace('修正案（十二）','修正案（十一）')
    if change=='new_revision':page['text']=page['text'].replace('》修正','》、2026年9月1日通过的《中华人民共和国刑法修正案（十三）》修正')
    if change=='missing_article':page['text']=page['text'].replace('第15条 合成条文15仅供自动化测试，不是法律依据。\n','')
    if change=='wrong_basis':basis['text']=basis['text'].replace('修正案（十二）','修正案（十一）')
    if change=='wrong_date':basis['text']=basis['text'].replace('2024年3月1日','2023年12月29日')
    with pytest.raises(ValueError):authorities.validate_page(page,'刑法',entry=CRIMINAL,effective_page=basis)


def test_criminal_old_date_does_not_read_newer_text(client,monkeypatch):
    monkeypatch.setattr(sources,'read_official',lambda *a,**k:pytest.fail('no newer law fetch'))
    col=collector();assert not col.acquire([{'name':'刑法'}],'2020-01-01','全国',lambda *a:None)
    assert col.leads[-1]['state']=='version_not_covered'


def test_stale_local_version_does_not_block_online_supplement(client,monkeypatch):
    monkeypatch.setattr(sources,'read_official',lambda url,**kw:mock_page(url,**kw))
    monkeypatch.setattr(websearch,'search',lambda *a:pytest.fail('catalog lookup must not require search'))
    law={'name':'中华人民共和国刑法','version':'本地测试版','jurisdiction':'全国','kind':'法律',
         'source_kind':'paste','valid_from':'2021-01-01','text':'第一条 本地测试条文正文。'}
    lid=client.post('/api/laws',json=law).json()['id']
    events=[]
    col=collector();rows=col.acquire([{'name':'刑法'}],'2026-09-25','全国',lambda *a:events.append(a))
    assert len(rows)==453  # 未复核的本地同名版本不阻断,线上版本照常纳入临时证据
    assert any(e[0]=='source_read' and '未阻断' in e[1] for e in events)
    with db.connect() as c:
        c.execute("UPDATE laws SET reviewed_at=? WHERE id=?",(dt.date.today().isoformat()+'T00:00:00Z',lid))
    col2=collector();assert not col2.acquire([{'name':'刑法'}],'2026-09-25','全国',lambda *a:None)
    assert any('人工复核版本' in lead['note'] for lead in col2.leads)


@pytest.mark.parametrize('kind,expected',[('none','search_not_configured'),('search','search_limit'),('read','read_limit'),('time','time_limit')])
def test_distinct_limits_and_missing_search_configuration(client,monkeypatch,kind,expected):
    col=collector();need={'name':'最高人民法院关于测试争议的规定'}
    if kind=='search':col.searches=2
    if kind=='read':col.reads=4
    if kind=='time':col.deadline=time.monotonic()+10;need={'name':'刑法'}
    monkeypatch.setattr(websearch,'search',lambda *a:pytest.fail('no network attempt allowed'))
    assert not col.acquire([need],'2026-09-25','全国',lambda *a:None)
    assert col.leads[-1]['state']==expected
    assert '或达到本次搜索上限' not in col.leads[-1]['note']


def test_missing_search_and_failed_search_not_confused(client,monkeypatch):
    monkeypatch.setattr(websearch,'settings',lambda:{'provider':'bocha'})
    def fail(*a,**kw):raise websearch.SearchError('搜索服务返回 HTTP 401')
    monkeypatch.setattr(websearch,'search',fail)
    col=collector();assert not col.acquire([{'name':'最高人民法院关于测试纠纷的规定'}],'2026-09-25','全国',lambda *a:None)
    assert col.leads[-1]['state']=='search_failed' and '401' in col.leads[-1]['note']


def test_unknown_wrapped_title_search_receives_only_clean_name(client,monkeypatch):
    monkeypatch.setattr(websearch,'settings',lambda:{'provider':'bocha'});seen=[]
    monkeypatch.setattr(websearch,'search',lambda q,*a:seen.append(q) or [])
    col=collector();col.acquire([{'name':'《最高人民法院关于测试纠纷的规定》','reason':'当事人张某身份证123456789012345678'}],'2026-09-25','全国',lambda *a:None)
    assert seen==['最高人民法院关于测试纠纷的规定 官方全文 施行']
    assert col.leads[-1]['state']=='not_found'


def test_failed_directory_uses_configured_search_once(client,monkeypatch):
    monkeypatch.setattr(sources,'read_official',lambda *a,**kw:(_ for _ in ()).throw(ValueError('官网拒绝访问')))
    monkeypatch.setattr(websearch,'settings',lambda:{'provider':'bocha'});seen=[]
    monkeypatch.setattr(websearch,'search',lambda q,*a:seen.append(q) or [])
    col=collector();need={'name':authorities.CATALOG[0]['name']}
    col.acquire([need],'2026-09-25','全国',lambda *a:None);col.acquire([need],'2026-09-25','全国',lambda *a:None)
    assert col.reads==1 and col.searches==1 and len(seen)==1


def test_engine_criminal_two_requested_articles_with_citations(client,monkeypatch):
    llm.save_settings({'provider':'custom','base_url':'http://localhost:9999','model':'source-fixture','agent_rounds':1})
    cid=client.post('/api/cases',json={'title':'刑法来源测试','event_date':'2026-09-25'}).json()['id']
    monkeypatch.setattr(sources,'read_official',mock_page)
    def model(system,payload,schema,verify=False):
        if schema is Plan:return Plan(queries=['过失致人死亡'],authorities=[{'name':'《中华人民共和国刑法》','articles':['第十五条','第二百三十三条']}])
        if schema is CoverageAudit:return CoverageAudit(selected_ids=[r['id'] for r in payload['candidate_evidence'] if ingest.article_key(r['number']) in [(15,0),(233,0)]],coverage='sufficient',reason='合成管线测试')
        if schema is Draft:
            selected=[r for r in payload['evidence'] if ingest.article_key(r['number']) in [(15,0),(233,0)]]
            if len(selected)<2:return Draft(missing_authorities=[{'name':'中华人民共和国刑法','articles':['第十五条','第二百三十三条']}])
            return Draft(insufficient=False,claims=[{'text':'合成测试结论，不是真实法律意见。','kind':'rule','section':'answer','evidence':[{'article_id':r['id'],'quote':r['body']} for r in selected]}])
        if schema is Verdict:return Verdict(checks=[{'claim_index':0,'supported':True,'reason':'合成管线测试'}])
        raise AssertionError(schema)
    monkeypatch.setattr(llm,'request_json',model)
    response=client.post(f'/api/cases/{cid}/ask',json={'text':'过失致人死亡的依据是什么？','online_research':True})
    assert response.status_code==200,response.text
    result=response.json();assert result['status']=='answered',result
    assert {ingest.article_key(x['number']) for x in result['citations']}=={(15,0),(233,0)}
    assert all(x['name']=='中华人民共和国刑法' and x['valid_from']=='2024-03-01' for x in result['citations'])
    assert not result['source_gaps']
    exported=client.get(f'/api/cases/{cid}/export').text
    assert '修正案日期依据' in exported and '《《' not in exported


def test_engine_missing_service_diagnostic_is_in_gap_and_export(client,monkeypatch):
    llm.save_settings({'provider':'custom','base_url':'http://localhost:9999','model':'source-fixture','agent_rounds':1})
    cid=client.post('/api/cases',json={'title':'缺口测试','event_date':'2026-09-25'}).json()['id']
    monkeypatch.setattr(llm,'request_json',lambda *a,**kw:Draft(missing_authorities=[{'name':'《最高人民法院关于测试纠纷的规定》'}]))
    result=client.post(f'/api/cases/{cid}/ask',json={'text':'处理测试纠纷','online_research':True}).json()
    gap=result['source_gaps'][0]
    assert gap['online_state']=='search_not_configured' and '模型 API 密钥不等于' in gap['online_detail']
    assert '联网补查：' in client.get(f'/api/cases/{cid}/export').text
