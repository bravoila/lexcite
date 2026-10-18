from collections import Counter, defaultdict
import datetime as dt
import json
import math
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
import jieba
from .. import db, llm
from ..ingest import normalize, NUM, article_key

jieba.setLogLevel(30)
STOP = set("的 了 是 在 和 与 或 及 有 无 如何 什么 是否 这个 那个 我 他 她 它 可以 应该 需要 根据 请 分析 规定 法律 法条 第 条".split())


def tokens(text):
    words = [x.lower() for x in jieba.cut(text) if len(x.strip()) > 1 and x not in STOP and re.search(r"[\w\u4e00-\u9fff]", x)]
    # Chinese bigrams preserve recall when a legal term is absent from the tokenizer dictionary.
    for seq in re.findall(r"[\u4e00-\u9fff]+", text):
        words.extend(seq[i:i+2] for i in range(len(seq)-1) if seq[i:i+2] not in STOP)
    return words


def law_key(name):
    return re.sub(r"[《》〈〉<>\s（）()·]", "", name).removeprefix("中华人民共和国").replace("解释一", "解释1").replace("解释二", "解释2")


def law_names(row):
    return [n for n in (row.get("canonical_name"), row.get("name"), row.get("display_name")) if n]


def matches_law(row, name):
    return law_key(name) in {law_key(n) for n in law_names(row)}


def query_laws(query, rows):
    explicit = re.findall(r"《([^》]{2,220})》", query)
    if explicit:
        return explicit
    names = sorted({n for row in rows for n in law_names(row) if law_key(n) in law_key(query)}, key=len, reverse=True)
    selected = []
    for name in names:
        if not any(law_key(name) in law_key(other) for other in selected):
            selected.append(name)
    return selected


def authority_gaps(needs, event_date, jurisdiction):
    with db.connect() as c:
        laws = [dict(x) for x in c.execute("SELECT * FROM laws")]
        article_numbers = defaultdict(set)
        for row in c.execute("SELECT law_id,number FROM articles"):
            article_numbers[row[0]].add(article_key(row[1]))
    eligible, _ = corpus(event_date, jurisdiction)
    available = {r['law_id'] for r in eligible}
    output = []
    for need in needs:
        need = need.model_dump() if hasattr(need,'model_dump') else need
        matching = [l for l in laws if matches_law(l, need['name'])]
        usable = [l for l in matching if l['id'] in available]
        absent = [n for n in need.get('articles',[]) if not any(article_key(n) in article_numbers[l['id']] for l in usable)]
        if not matching:
            reason, state = '资料库未找到匹配的标准法规名称；可能尚未导入或名称未校正。', 'not_in_catalog'
        elif not usable:
            reason, state = '法规存在，但未通过日期、地区、复核状态或版本冲突筛选。', 'excluded'
        elif absent:
            reason, state = '匹配法规缺少请求的条文：'+'、'.join(absent), 'missing_articles'
        else:
            continue
        output.append(need | {'state':state,'detail':reason})
    return output


def requested_articles(needs, event_date, jurisdiction):
    rows, _ = corpus(event_date, jurisdiction)
    selected = []
    for need in needs:
        numbers = {article_key(n) for n in need.get('articles', [])}
        selected.extend(r for r in rows if matches_law(r, need['name']) and article_key(r['number']) in numbers)
    return selected


def corpus(event_date, jurisdiction):
    cutoff = (dt.date.today() - dt.timedelta(days=llm.settings()["review_days"])).isoformat()
    with db.connect() as c:
        laws = [dict(x) for x in c.execute("SELECT * FROM laws")]
        eligible, warnings = [], []
        for law in laws:
            if law["jurisdiction"] not in ("全国", jurisdiction):
                continue
            if law["valid_from"] > event_date or (law["valid_to"] and law["valid_to"] <= event_date):
                continue
            if not law["reviewed_at"] or law["reviewed_at"][:10] < cutoff:
                warnings.append(f"《{law['name']}》{law['version']} 尚未复核或复核已过期，本次排除。")
                continue
            eligible.append(law)
        groups = defaultdict(list)
        for law in eligible:
            groups[(law_key(law.get("canonical_name") or law["name"]), law["jurisdiction"])].append(law["id"])
        blocked = {lid for ids in groups.values() if len(ids) > 1 for lid in ids}
        if blocked:
            warnings.append("发现同名法规有多个版本在所选日期同时有效，已排除冲突法规；请校对有效期。")
        lids = {l["id"] for l in eligible} - blocked
        rows = [dict(r) for r in c.execute("""SELECT a.*,l.name,l.version,l.jurisdiction,l.kind,l.source_url,l.source_kind,l.source_name,
            l.valid_from,l.valid_to,l.reviewed_at,l.sha256,l.canonical_name,l.document_number,COALESCE(h.heading_path,'') AS heading_path FROM articles a JOIN laws l ON l.id=a.law_id LEFT JOIN article_headings h ON h.article_id=a.id""") if r["law_id"] in lids]
    rows = [r | {"display_name":r["name"], "name":r.get("canonical_name") or r["name"], "authority_status":"human_reviewed"} for r in rows]
    return rows, warnings


def cosine(a, b):
    if len(a) != len(b):
        return -1
    dot = sum(x*y for x, y in zip(a, b))
    norm = math.sqrt(sum(x*x for x in a) * sum(y*y for y in b))
    return dot / norm if norm else 0


def search(queries, event_date, jurisdiction, limit=None):
    rows, warnings = corpus(event_date, jurisdiction)
    selected, extra = rank(queries, rows, limit, dense=True)
    return selected, warnings+extra


_observer = ContextVar('retrieval_observer', default=None)


@contextmanager
def observe(callback):
    token=_observer.set(callback)
    try:yield
    finally:_observer.reset(token)


def rank(queries, rows, limit=None, dense=False):
    """Three recall lanes, optional reranking, then bounded same-law expansion."""
    from .. import indexing, retrieval_services as services
    started=time.monotonic();s=services.settings();limit=limit or s['final_k'];warnings=[]
    queries=list(dict.fromkeys(q for q in queries if q.strip()))[:9]
    detail={'scope':'local' if dense else 'temporary','configured_mode':s['mode'],
      'embedding_model':s['embedding']['model'],'reranker_model':s['reranker']['model'],
      'embedding_status':'disabled','reranker_status':'disabled','corpus_articles':len(rows),
      'indexed_articles':0,'lexical_candidates':0,'dense_candidates':0,'exact_candidates':0,
      'candidate_count':0,'reranked_candidates':0,'embedding_requests':0,'rerank_requests':0}
    def finish(selected):
        methods=['BM25']
        if detail['exact_candidates']:methods.append('法规／条号匹配')
        if detail['embedding_status'] in ('applied','partial'):methods.append('向量 '+s['embedding']['model'])
        if detail['reranker_status']=='applied':methods.append('重排 '+s['reranker']['model'])
        detail.update(methods=methods,returned=len(selected),elapsed_seconds=round(time.monotonic()-started,3),warnings=warnings,
          ranked_ids=[r['id'] for r in selected],summary=('本地法库' if dense else '官网临时条文')+'：'+' + '.join(methods)+f"；向量覆盖 {detail['indexed_articles']}/{len(rows)} 条")
        callback=_observer.get()
        if callback:callback(detail)
        return selected,warnings
    if not rows or not queries:return finish([])
    docs=[Counter(tokens(indexing.header(r)+' '+r['body'])) for r in rows]
    lengths=[sum(d.values()) for d in docs];avg=sum(lengths)/len(lengths)
    df=Counter(t for d in docs for t in d);fused=defaultdict(float);exact_ids=set();lex_ids=set();dense_ids=set()
    unique_laws={r['law_id'] for r in rows}
    for query in queries:
        qnumbers={article_key(x) for x in re.findall(r'第'+NUM+r'条(?:之'+NUM+r')?',normalize(query))}
        targets=query_laws(query,rows)
        # A query that names another law (even one absent from the library) must
        # never feed subject-less article numbers into the exact lane.
        foreign=[n for n in re.findall(r'《([^》]{2,220})》',query) if not any(matches_law(r,n) for r in rows)]
        for name in re.findall(r'([一-鿿]{1,24}(?:法|条例|规定|办法|解释|规则|细则|决定|决议|公约|条约|通则|法典|典|规范|准则|协定))第'+NUM+r'条',normalize(query)):
            if name in ('本法','该法','此法','有关规定','法律规定','相关法律','有关法律','本规定','该规定','本办法','该办法','本条例','该条例','本解释','该解释','本规则','该规则','本细则','该细则'):continue
            if name not in foreign and not any(matches_law(r,name) for r in rows):foreign.append(name)
        if foreign:
            warnings.append('查询点名了未入库法规：'+'、'.join('《'+n+'》' for n in foreign)+'；已禁止无主体条号匹配，请补录或核对法规名称。')
        subject_bound=bool(targets) or (len(unique_laws)==1 and not foreign)
        qt=set(tokens(query));scores=[]
        for i,doc in enumerate(docs):
            if targets and not any(matches_law(rows[i],name) for name in targets):continue
            score=0.;matched=0
            for t in qt:
                f=doc[t]
                if not f:continue
                matched+=1
                idf=math.log(1+(len(rows)-df[t]+.5)/(df[t]+.5))
                score+=idf*f*2.5/(f+1.5*(.25+.75*lengths[i]/max(avg,1)))
            exact=article_key(rows[i]['number']) in qnumbers and subject_bound
            if exact:exact_ids.add(i)
            if (matched>0 and score>0) or exact:scores.append((i,score+(100 if exact else 0)))
        for position,(i,_) in enumerate(sorted(scores,key=lambda x:-x[1])[:s['recall_k']]):
            fused[i]+=1/(60+position);lex_ids.add(i)
    if dense and s['mode']=='hybrid':
        if s['embedding']['provider']=='none':
            detail['embedding_status']='not_configured';warnings.append('本次未启用语义召回：尚未配置独立向量服务，实际仅使用关键词／条号召回。')
        else:
            try:
                indexing.ensure_schema();stored=indexing.load_vectors(rows,s)
                detail['indexed_articles']=len({r[0] for r in stored})
                if not stored:
                    detail['embedding_status']='index_missing';warnings.append('本次未使用向量召回：当前模型尚无可用索引，请到“语义检索与重排”建立索引。')
                else:
                    qs=([q for q in queries if not q.startswith('《')] or queries)[:3]
                    detail['embedding_requests']=1
                    qvectors=services.embed(qs,query=True,cfg=s['embedding'])
                    ids={r['id']:i for i,r in enumerate(rows)}
                    checked=[(aid,services.validate_vectors([v],1,len(qvectors[0]))[0],text) for aid,v,text in stored]
                    for query,qv in zip(qs,qvectors):
                        targets=query_laws(query,rows);best={}
                        for aid,v,text in checked:
                            i=ids[aid]
                            if targets and not any(matches_law(rows[i],n) for n in targets):continue
                            score=sum(a*b for a,b in zip(qv,v))
                            best[i]=max(best.get(i,-2),score)
                        for position,(i,score) in enumerate(sorted(best.items(),key=lambda x:-x[1])[:s['recall_k']]):
                            fused[i]+=1/(60+position);dense_ids.add(i)
                    detail['embedding_status']='applied' if detail['indexed_articles']==len(rows) else 'partial'
                    if detail['embedding_status']=='partial':warnings.append(f"向量索引仅覆盖本次可用法条 {detail['indexed_articles']}/{len(rows)} 条；未覆盖部分仍参加关键词检索。")
            except llm.ModelError as exc:
                detail['embedding_status']='failed';warnings.append(str(exc)+'；本轮语义召回失败，已明确降级为关键词／条号召回。')
    elif not dense and s['mode']=='hybrid':detail['embedding_status']='temporary_unindexed'
    detail.update(lexical_candidates=len(lex_ids),dense_candidates=len(dense_ids),exact_candidates=len(exact_ids))
    indices=sorted(fused,key=lambda i:(-fused[i],rows[i]['ordinal']))[:s['rerank_candidates']]
    # All explicit matches are audited even if semantic ranking puts them last.
    indices=list(dict.fromkeys(sorted(exact_ids)+indices))
    detail['candidate_count']=len(indices)
    scores={}
    if s['mode']=='hybrid' and indices:
        if s['reranker']['provider']=='none':
            detail['reranker_status']='not_configured'
            if s['embedding']['provider']!='none':warnings.append('本次未启用重排模型，候选使用 RRF 融合顺序。')
        else:
            try:
                qt=set(tokens(queries[0]));documents=[]
                for i in indices:
                    windows=indexing.chunks(rows[i],s['chunk_chars'])
                    if len(windows)>1:
                        windows=sorted(windows,key=lambda w:-len(qt.intersection(tokens(w))))[:2]
                    documents.append('\n'.join(windows))
                detail['rerank_requests']=1
                rerank_query=next((q for q in queries if q.startswith(queries[0]+'\n')),queries[0])
                ranked=services.rerank(rerank_query[:2000],documents,cfg=s['reranker'])
                scores={indices[j]:score for j,score in ranked};indices=[indices[j] for j,_ in ranked]
                detail.update(reranker_status='applied',reranked_candidates=len(indices))
            except llm.ModelError as exc:
                detail['reranker_status']='failed';warnings.append(str(exc)+'；重排失败，保留融合顺序。')
    indices=list(dict.fromkeys(sorted(exact_ids)+indices[:limit]))
    selected=[rows[i]|{'retrieval_score':round(fused[i],6),'retrieval_channels':[name for name,items in [('exact',exact_ids),('bm25',lex_ids),('dense',dense_ids)] if i in items],
        **({'rerank_score':scores[i]} if i in scores else {})} for i in indices]
    ids={r['id'] for r in selected};max_expanded=len(selected)+3
    for row in list(selected[:4]):
        refs={article_key(x) for x in re.findall(r'(?:本法|本解释|本规定|本条例|本办法)\s*(第'+NUM+r'条(?:之'+NUM+r')?)',row['body'])}
        for candidate in rows:
            if candidate['law_id']==row['law_id'] and article_key(candidate['number']) in refs and candidate['id'] not in ids:
                selected.append(candidate|{'retrieval_score':0,'expanded':True});ids.add(candidate['id'])
                if len(selected)>=max_expanded:return finish(selected)
    return finish(selected)


def reindex():
    from .. import indexing
    return indexing.build()
