from collections import Counter, defaultdict
import datetime as dt
import json
import math
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
import jieba
from .. import db, llm
from ..ingest import normalize, NUM, article_key

jieba.setLogLevel(30)
STOP = set("的 了 是 在 和 与 或 及 有 无 如何 什么 是否 这个 那个 我 他 她 它 可以 应该 需要 根据 请 分析 规定 法律 法条 第 条".split())
ARTICLE_REFERENCE = re.compile(r"(?:第"+NUM+r"|[0-9０-９]+)条(?:之"+NUM+r")?")
REFERENCE_NAMES = {
    "本法", "该法", "此法", "本法典", "该法典", "本条例", "该条例", "本规定", "该规定", "本办法", "该办法",
    "本解释", "该解释", "本规则", "该规则", "本细则", "该细则", "有关规定", "法律规定", "相关法律", "有关法律",
    "相关司法解释", "有关司法解释", "司法解释", "合同规定", "合同约定", "协议规定", "协议约定",
}


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


def law_mentions(query, rows, extra_names=()):
    text = normalize(query)
    mentions, stack = [], []
    for i, char in enumerate(text):
        if char == "《":
            stack.append(i)
        elif char == "》" and stack:
            start = stack.pop()
            if not stack and 2 <= i-start-1 <= 220:
                mentions.append({"start": start, "end": i+1, "name": text[start+1:i]})
    explicit = list(mentions)
    names = {n for row in rows for n in law_names(row)} | set(extra_names)
    for name in sorted(names, key=len, reverse=True):
        key = law_key(name)
        if len(key) < 2:
            continue
        variants = {key, key.replace("解释1", "解释一").replace("解释2", "解释二")}
        for variant in variants:
            pattern = r"[《》〈〉（）()·]*".join(re.escape(c) for c in variant)
            for match in re.finditer(pattern, text):
                if any(m["start"] <= match.start() and match.end() <= m["end"] for m in mentions):
                    continue
                mentions.append({"start": match.start(), "end": match.end(), "name": name})
    suffix = r"(?:法典|法|条例|规定|办法|解释(?:[（(][一二三四五六七八九十0-9]+[）)])?|规则|细则|决定|决议|公约|条约|通则|典|规范|准则|协定)"
    for match in re.finditer(r"([一-鿿]{0,70}"+suffix+r")[：:（）()，,、]*?(?=(?:第"+NUM+r"|[0-9０-９]+)条)", text):
        start, end = match.span(1)
        if any(m["start"] <= start and end <= m["end"] for m in explicit):
            continue
        if any(start <= m["start"] and m["end"] == end for m in mentions):
            continue
        name = match[1]
        name = re.sub(r"^.*(?:法|典|条|条例|规定)(?:和|与|及)", "", name)
        while True:
            cleaned = re.sub(r"^(?:根据|依据|依照|按照|参照|适用|结合|对照|比较|分析|关于|以及|或者|和|与|及|或|在|请)", "", name)
            if cleaned == name:
                break
            name = cleaned
        if len(name) < 2 or name in REFERENCE_NAMES:
            continue
        mentions.append({"start": end-len(name), "end": end, "name": name})
    return sorted(mentions, key=lambda m: (m["start"], -m["end"]))


def article_bindings(query, rows, extra_names=()):
    text = normalize(query)
    mentions = law_mentions(text, rows, extra_names)
    bindings = []
    for match in ARTICLE_REFERENCE.finditer(text):
        if any(m["start"] <= match.start() < m["end"] for m in mentions):
            continue
        preceding = [m for m in mentions if m["end"] <= match.start() and not re.search(r"[。；;！？!?\n]", text[m["end"]:match.start()])]
        subject = max(preceding, key=lambda m: m["end"])["name"] if preceding else None
        label = match[0] if match[0].startswith("第") else "第"+match[0]
        bindings.append((article_key(label), subject))
    return bindings, mentions


def query_laws(query, rows):
    return list(dict.fromkeys(m["name"] for m in law_mentions(query, rows)))


@lru_cache(maxsize=4)
def _catalogue(path, revision):
    with db.connect() as c:
        return tuple(dict(row) for row in c.execute("SELECT id,name,canonical_name FROM laws"))


def authority_gaps(needs, event_date, jurisdiction):
    needs = [need.model_dump() if hasattr(need, "model_dump") else need for need in needs]
    wanted = {law_key(need["name"]) for need in needs}
    if not wanted:
        return []
    eligible, _ = corpus(event_date, jurisdiction)
    available = {r['law_id'] for r in eligible}
    laws = [law for law in _catalogue(*db.corpus_version()) if any(law_key(n) in wanted for n in law_names(law))]
    usable_ids = {law["id"] for law in laws if law["id"] in available}
    article_numbers = defaultdict(set)
    for row in eligible:
        if row["law_id"] in usable_ids:
            article_numbers[row["law_id"]].add(article_key(row["number"]))
    output = []
    for need in needs:
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
        selected.extend(r | {"requested": True} for r in rows if matches_law(r, need['name']) and article_key(r['number']) in numbers)
    return selected


def corpus(event_date, jurisdiction):
    cutoff = (dt.date.today() - dt.timedelta(days=llm.settings()["review_days"])).isoformat()
    rows, warnings = _corpus(*db.corpus_version(), event_date, jurisdiction, cutoff)
    return [dict(row) for row in rows], list(warnings)


@lru_cache(maxsize=4)
def _corpus(path, revision, event_date, jurisdiction, cutoff):
    with db.connect() as c:
        condition = "jurisdiction IN ('全国',?) AND valid_from<=? AND (valid_to IS NULL OR valid_to>?)"
        parameters = (jurisdiction, event_date, event_date, cutoff)
        eligible = [dict(x) for x in c.execute("SELECT * FROM laws WHERE "+condition+" AND reviewed_at>=?", parameters)]
        warnings = [f"《{law['name']}》{law['version']} 尚未复核或复核已过期，本次排除。"
                    for law in c.execute("SELECT name,version FROM laws WHERE "+condition+" AND (reviewed_at IS NULL OR reviewed_at<?)", parameters)]
        groups = defaultdict(list)
        for law in eligible:
            groups[(law_key(law.get("canonical_name") or law["name"]), law["jurisdiction"])].append(law["id"])
        blocked = {lid for ids in groups.values() if len(ids) > 1 for lid in ids}
        if blocked:
            warnings.append("发现同名法规有多个版本在所选日期同时有效，已排除冲突法规；请校对有效期。")
        lids = {l["id"] for l in eligible} - blocked
        rows = []
        ids = sorted(lids)
        for start in range(0, len(ids), 800):
            batch = ids[start:start+800]
            rows.extend(dict(r) for r in c.execute("""SELECT a.*,l.name,l.version,l.jurisdiction,l.kind,l.source_url,l.source_kind,l.source_name,
            l.valid_from,l.valid_to,l.reviewed_at,l.sha256,l.canonical_name,l.document_number,COALESCE(h.heading_path,'') AS heading_path FROM articles a JOIN laws l ON l.id=a.law_id LEFT JOIN article_headings h ON h.article_id=a.id"""
                + " WHERE a.law_id IN ("+",".join("?" for _ in batch)+") ORDER BY l.id,a.ordinal", batch))
    rows = [r | {"display_name":r["name"], "name":r.get("canonical_name") or r["name"], "authority_status":"human_reviewed"} for r in rows]
    return tuple(rows), tuple(warnings)


def cosine(a, b):
    if len(a) != len(b):
        return -1
    dot = sum(x*y for x, y in zip(a, b))
    norm = math.sqrt(sum(x*x for x in a) * sum(y*y for y in b))
    return dot / norm if norm else 0


def search(queries, event_date, jurisdiction, limit=None):
    rows, warnings = corpus(event_date, jurisdiction)
    selected, extra = rank(queries, rows, limit, dense=True, catalogue=_catalogue(*db.corpus_version()))
    return selected, warnings+extra


_observer = ContextVar('retrieval_observer', default=None)


@contextmanager
def observe(callback):
    token=_observer.set(callback)
    try:yield
    finally:_observer.reset(token)


@lru_cache(maxsize=2)
def lexical_index(documents):
    docs = [Counter(tokens(text)) for text in documents]
    lengths = [sum(doc.values()) for doc in docs]
    return docs, lengths, sum(lengths)/max(1, len(lengths)), Counter(token for doc in docs for token in doc)


def rank(queries, rows, limit=None, dense=False, catalogue=()):
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
    prepared = {}
    names = {n for row in (*rows, *catalogue) for n in law_names(row)}
    available_names = {law_key(n) for row in rows for n in law_names(row)}
    catalogued_names = {law_key(n) for row in catalogue for n in law_names(row)}
    for query in queries:
        bindings,mentions=article_bindings(query,(),names)
        targets=list(dict.fromkeys(m["name"] for m in mentions))
        prepared[query]=(bindings,mentions,targets)
        for name in targets:
            if law_key(name) in available_names:
                continue
            exists=law_key(name) in catalogued_names
            warnings.append(('查询点名的法规已入库但被日期、地区、复核或版本冲突筛选排除：' if exists else '查询点名了未入库法规：')+'《'+name+'》；已禁止无主体条号匹配，请补录或核对法规状态。')
    if not rows or not queries:return finish([])
    docs,lengths,avg,df=lexical_index(tuple(indexing.header(r)+' '+r['body'] for r in rows))
    fused=defaultdict(float);exact_ids=set();lex_ids=set();dense_ids=set()
    unique_laws={r['law_id'] for r in rows}
    for query in queries:
        bindings,mentions,targets=prepared[query]
        def allowed(row):
            if targets and not any(matches_law(row,name) for name in targets):
                return False
            same_number=[name for number,name in bindings if number==article_key(row['number'])]
            return not same_number or any(name is None or matches_law(row,name) for name in same_number)
        qt=set(tokens(query));scores=[]
        for i,doc in enumerate(docs):
            if not allowed(rows[i]):continue
            score=0.;matched=0
            for t in qt:
                f=doc[t]
                if not f:continue
                matched+=1
                idf=math.log(1+(len(rows)-df[t]+.5)/(df[t]+.5))
                score+=idf*f*2.5/(f+1.5*(.25+.75*lengths[i]/max(avg,1)))
            exact=any(article_key(rows[i]['number'])==number and (matches_law(rows[i],name) if name else not mentions and len(unique_laws)==1) for number,name in bindings)
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
                        bindings,mentions,targets=prepared[query];best={}
                        for aid,v,text in checked:
                            i=ids[aid]
                            if targets and not any(matches_law(rows[i],n) for n in targets):continue
                            same_number=[name for number,name in bindings if number==article_key(rows[i]['number'])]
                            if same_number and not any(name is None or matches_law(rows[i],name) for name in same_number):continue
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
