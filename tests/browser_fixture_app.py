"""ONLY for scripts/test_browser.py with a disposable database. Not a deployment entrypoint."""
from app.main import app
from app import sources
from app.research_agent import authorities
from source_fixture_data import CRIMINAL, BASIS, CRIMINAL_TEXT

FIXTURE_TEXT = """第一百四十一条 这是导入测试原文，不是真实法律依据。
第一百四十二条 本条依照本法
第一百四十一条的规定办理。本句只测试交叉引用的保留。
第一百四十三条 这是后续测试条文，不是真实法律依据。"""


CORE_NAME=authorities.CATALOG[0]['name']
CORE_TEXT=CORE_NAME+'\n法释〔2024〕1号\n'+'\n'.join('第'+n+'条 彩礼纠纷测试条文，仅供验证界面和引用流程，不是真实法律。' for n in '一二三四五六')+'\n第七条 本规定自2024年2月1日起施行。'
_original_directory=authorities.directory_for

def fixture_directory(topic):
    if llm.settings()['model']=='core-browser-fixture':return [{'name':CORE_NAME,'articles':['第五条'],'reason':'浏览器测试所需文件'}]
    return _original_directory(topic)

authorities.directory_for=fixture_directory

def fixture_download(url):
    if url in [CRIMINAL['url']]+CRIMINAL['fallback_urls']:
        return url, {'content-type':'text/plain; charset=utf-8'},CRIMINAL_TEXT.encode()
    if url in [CRIMINAL['effective_source']]+CRIMINAL['effective_fallback_urls']:
        return url, {'content-type':'text/plain; charset=utf-8'},BASIS.encode()

    if url == authorities.CATALOG[0]['url']:
        return url, {'content-type':'text/plain; charset=utf-8'},CORE_TEXT.encode()

    if url == "https://www.gov.cn/lexcite-browser-fixture.html":
        html = "<html><body><main>" + "".join("<p>"+line+"</p>" for line in FIXTURE_TEXT.splitlines()) + "</main></body></html>"
        return url, {"content-type": "text/html; charset=utf-8"}, html.encode()
    raise ValueError("测试官网返回 HTTP 503，读取暂时失败，请稍后重试。")


# Use the real extract-url API, HTML reader, article splitter and database.
# Replace only the external download; no production endpoint or model is called.
sources.download_official = fixture_download

# Deterministic agent fixtures; activated by the explicit fixture model / query.
import time
from app import llm, websearch
from app.research_agent.schemas import FactExtraction, Plan, Draft, Repair, Verdict, ClarificationAudit
_real_request = llm.request_json


def fixture_model(system, payload, schema, verify=False):
    if llm.settings()['model']=='source-browser-fixture':
        if schema is FactExtraction:return FactExtraction(intent='new_question',facts=[])
        if schema is Plan:
            name='《最高人民法院关于测试纠纷的规定》' if '目录外' in payload['question'] else '《中华人民共和国刑法》'
            return Plan(queries=['过失致人死亡'],authorities=[{'name':name,'articles':['第十五条','第二百三十三条']}])
        if schema is Draft:
            name='最高人民法院关于测试纠纷的规定' if '目录外' in payload['question'] else '中华人民共和国刑法'
            selected=[r for r in payload['evidence'] if r['number'] in ('第15条','第233条') and r['name']=='中华人民共和国刑法']
            if len(selected)<2:return Draft(missing_authorities=[{'name':name,'articles':['第十五条','第二百三十三条']}])
            return Draft(insufficient=False,claims=[{'text':'刑法来源流程的合成测试结论，不是真实法律意见。','kind':'rule','section':'answer','evidence':[{'article_id':r['id'],'quote':r['body']} for r in selected]}])
        if schema is Verdict:return Verdict(checks=[{'claim_index':0,'supported':True,'reason':'合成数据'}])

    if llm.settings()['model']=='core-browser-fixture':
        time.sleep(.2)
        if schema is FactExtraction:return FactExtraction(intent='supplement',facts=[])
        if schema is Plan:return Plan(queries=['彩礼纠纷'],authorities=[{'name':CORE_NAME,'articles':['第五条']}])
        if schema is Draft:
            row=next((r for r in payload['evidence'] if r['number']=='第五条' and r['name']==CORE_NAME),None)
            if row is None:
                return Draft(missing_authorities=[{'name':CORE_NAME,'articles':['第五条']}])
            return Draft(insufficient=False,claims=[{'text':'此处展示带原文引用的测试结论，供验证新版研究界面。','kind':'rule','section':'answer','evidence':[{'article_id':row['id'],'quote':row['body']}]},{'text':'本轮保留用户已提供的信息，以下内容仅用于验证页面。','kind':'inference','section':'application','evidence':[{'article_id':row['id'],'quote':row['body']}]}])
        if schema is Verdict:return Verdict(checks=[{'claim_index':i,'supported':True,'reason':'浏览器协议测试'} for i in range(len(payload['draft']))])

    if schema is FactExtraction:return FactExtraction(intent='supplement',facts=[])
    model = llm.settings()['model']
    if model == 'memory-browser-error':
        raise llm.ModelError('模型接口 HTTP 400（InvalidParameter）：测试参数不兼容，请核对输出格式', fatal=True, status_code=400)
    if model == 'memory-browser-fixture':
        llm.report_reasoning('浏览器测试返回的思考片段（合成数据）：已收到用户关于登记和共同生活的陈述。\n<script>window.reasoningXSS=true</script>')
        time.sleep(1.2)
        llm.report_reasoning('\n生活困难仍为未知，此测试片段不构成法律结论。')
        time.sleep(.4)
        if schema is Plan:
            return Plan(queries=['未支付价款'])
        if schema is Draft:
            return Draft(insufficient=True, missing_facts=['双方是否已办理结婚登记手续？','双方婚后是否实际共同生活？','彩礼给付是否导致给付方生活困难？'])
        if schema is ClarificationAudit:
            source=next(s for s in payload['sources'] if '生活困难 未知' in s['text'])
            return ClarificationAudit(checks=[{'question_index':i,'state':'unknown','source_id':source['id'],'quote':'彩礼是否导致给付方生活困难 未知'} for i in range(len(payload['questions']))])
    if model != 'agent-browser-fixture':
        return _real_request(system, payload, schema, verify=verify)
    time.sleep(.8)
    if schema is Plan:
        return Plan(queries=['未支付价款 违约责任'])
    if schema is Draft:
        row = payload['evidence'][0]
        return Draft(insufficient=False, claims=[{'text':'在不履行合同义务的情形下，应结合约定及法律适用条件判断责任。', 'kind':'inference', 'evidence':[{'article_id':row['id'],'quote':'这是测试用错误引文不在原文中'}]}])
    if schema is Repair:
        patches=[]
        for failed in payload['failed_claims']:
            claim=failed['claim']
            for ref in claim['evidence']:
                ref['quote']=next(r['body'] for r in payload['evidence'] if r['id']==ref['article_id'])
            patches.append({'claim_index':failed['claim_index'],'claim':claim})
        return Repair(patches=patches)
    return Verdict(checks=[{'claim_index':i,'supported':True,'reason':'仅用于浏览器界面测试'} for i in range(len(payload['draft']))])


llm.request_json = fixture_model
_real_search = websearch.search


def fixture_search(query, *args, **kwargs):
    if query == '浏览器公开检索测试':
        return [{'url':'https://www.gov.cn/lexcite-browser-fixture.html','title':'浏览器测试官网线索','snippet':'模拟结果，不是真实法律','state':'search_only','note':'测试线索，尚未复核'}]
    return _real_search(query, *args, **kwargs)


websearch.search = fixture_search

# Embedding and rerank protocol fixtures; no real model/network used.
from app import retrieval_services
_real_retrieval_post=retrieval_services.post

def fixture_retrieval_post(cfg,path,body,check=lambda:None):
    if cfg['model']=='hybrid-browser-fail':raise llm.ModelError('合成测试：向量服务不可用，等待重新配置')
    if cfg['model']=='hybrid-browser-embedding':
        check();time.sleep(.12)
        return {'data':[{'index':i,'embedding':[1.,0.] if ('照管' in t or '看娃' in t) else [0.,1.]} for i,t in enumerate(body['input'])]}
    if cfg['model']=='hybrid-browser-reranker':
        return {'results':[{'index':i,'relevance_score':.9 if '照管' in t else .1} for i,t in enumerate(body['documents'])]}
    return _real_retrieval_post(cfg,path,body,check)

retrieval_services.post=fixture_retrieval_post
_previous_fixture_model=llm.request_json

def hybrid_fixture_model(system,payload,schema,verify=False):
    if llm.settings()['model']!='hybrid-browser-analysis':return _previous_fixture_model(system,payload,schema,verify)
    if schema is Plan:return Plan(queries=['怎么帮人看娃'])
    if schema is FactExtraction:return FactExtraction(intent='new_question',facts=[])
    if schema is Draft:
        row=payload['evidence'][0]
        return Draft(insufficient=False,claims=[{'text':'本条为测试引用，用于核对混合检索的数据传递。','kind':'rule','evidence':[{'article_id':row['id'],'quote':row['body']}]}])
    if schema is Verdict:return Verdict(checks=[{'claim_index':0,'supported':True,'reason':'测试流程'}])
    return _previous_fixture_model(system,payload,schema,verify)
llm.request_json=hybrid_fixture_model
