"""Persistent, resumable chunk embeddings with atomic per-article publication."""
import hashlib
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from . import db, llm, retrieval_services as services

_guard=threading.Lock()
_thread=None
_stop=threading.Event()
_requested=threading.Event()
_retry=False


def ensure_schema():
    with db.connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS article_headings(article_id TEXT PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,heading_path TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS embedding_states(
            article_id TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,fingerprint TEXT NOT NULL,
            content_hash TEXT NOT NULL,status TEXT NOT NULL,error TEXT NOT NULL DEFAULT '',updated TEXT NOT NULL,
            PRIMARY KEY(article_id,fingerprint));
        CREATE TABLE IF NOT EXISTS embedding_chunks(
            article_id TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,fingerprint TEXT NOT NULL,
            chunk_no INTEGER NOT NULL,content_hash TEXT NOT NULL,text TEXT NOT NULL,vector TEXT NOT NULL,
            PRIMARY KEY(article_id,fingerprint,chunk_no));
        ''')


def articles():
    with db.connect() as c:
        return [dict(r) for r in c.execute('''SELECT a.*,l.name,l.canonical_name,l.version,l.kind,l.valid_from,l.valid_to,
          COALESCE(h.heading_path,'') AS heading_path FROM articles a JOIN laws l ON a.law_id=l.id
          LEFT JOIN article_headings h ON h.article_id=a.id ORDER BY l.id,a.ordinal''')]


def header(row):
    return ' · '.join(str(x) for x in (row.get('canonical_name') or row['name'],row.get('version'),row.get('kind'),row.get('heading_path'),row['number']) if x)


def content_hash(row):
    return hashlib.sha256(json.dumps([header(row),row.get('valid_from'),row.get('valid_to'),row['body']],ensure_ascii=False).encode()).hexdigest()


def chunks(row, size=1200):
    """Overlapping windows are retrieval units only. All citations retain full body."""
    text=row['body'];out=[];start=0
    while start<len(text):
        end=min(len(text),start+size)
        if end<len(text):
            boundary=max(text.rfind('\n',start+size//2,end),text.rfind('。',start+size//2,end))
            if boundary>start:end=boundary+1
        out.append(header(row)+'\n'+text[start:end])
        if end==len(text):break
        start=max(start+1,end-100)
    return out


def mark_headings(connection, ids, text):
    """Use only explicitly printed chapter/section lines, never generated titles."""
    from .ingest import ARTICLE, _reference_reason, normalize
    text=text.replace('\r\n','\n').replace('\r','\n').replace('\u00a0',' ')
    offsets={}
    for match in ARTICLE.finditer(text):
        if not _reference_reason(text,match):offsets.setdefault(normalize(match[1]),match.start())
    path=[];positions=[]
    for m in re.finditer(r'(?m)^\s*(第[零〇一二三四五六七八九十百千万0-9]+([编章节])[^\n]{0,90})\s*$',text):
        level={'编':0,'章':1,'节':2}[m[2]]
        path=[(n,t) for n,t in path if n<level]+[(level,m[1].strip())]
        positions.append((m.end(),' / '.join(t for _,t in path)))
    for aid,number,body in ids:
        at=offsets.get(number,-1)
        if at<0:continue
        title=next((p for i,p in reversed(positions) if i<=at),'')
        connection.execute('INSERT OR REPLACE INTO article_headings VALUES(?,?)',(aid,title))


def status():
    ensure_schema();s=services.settings();fp=services.fingerprint(s);rows=articles()
    with db.connect() as c:
        states={r['article_id']:dict(r) for r in c.execute('SELECT * FROM embedding_states WHERE fingerprint=?',(fp,))}
        chunk_count=c.execute('SELECT COUNT(*) FROM embedding_chunks WHERE fingerprint=?',(fp,)).fetchone()[0]
    counts={'ready':0,'failed':0,'pending':0};errors=[]
    for row in rows:
        item=states.get(row['id']);current=item and item['content_hash']==content_hash(row)
        key=item['status'] if current and item['status'] in ('ready','failed') else 'pending'
        counts[key]+=1
        if key=='failed' and len(errors)<5:errors.append({'article':header(row),'error':item['error']})
    job=db.get('embedding_job',{})
    configured=s['embedding']['provider']!='none'
    if job.get('fingerprint')!=fp:job={}
    return {'configured':configured,'mode':s['mode'],'embedding_model':s['embedding']['model'],
      'reranker_model':s['reranker']['model'] if s['reranker']['provider']!='none' else '',
      'fingerprint':fp,'total':len(rows),**counts,'chunks':chunk_count,'errors':errors,'job':job,
      'auto_index':s['auto_index'],'legacy_vectors_ignored':True}


def load_vectors(rows,s):
    fp=services.fingerprint(s);wanted={r['id']:content_hash(r) for r in rows}
    with db.connect() as c:
        data=c.execute('''SELECT e.* FROM embedding_chunks e JOIN embedding_states s
          ON s.article_id=e.article_id AND s.fingerprint=e.fingerprint AND s.content_hash=e.content_hash
          WHERE e.fingerprint=? AND s.status='ready' ''',(fp,)).fetchall()
    result=[]
    for row in data:
        if wanted.get(row['article_id'])!=row['content_hash']:continue
        try:v=json.loads(row['vector'])
        except (ValueError,TypeError):raise llm.ModelError('向量索引损坏，请重建索引')
        result.append((row['article_id'],v,row['text']))
    return result


def build(*, retry_failed=True, check=lambda:None):
    ensure_schema();s=services.settings();fp=services.fingerprint(s)
    if s['embedding']['provider']=='none':raise ValueError('请先配置并保存独立向量服务')
    rows=articles()
    with db.connect() as c:
        states={r['article_id']:dict(r) for r in c.execute('SELECT * FROM embedding_states WHERE fingerprint=?',(fp,))}
    pending=[r for r in rows if r['id'] not in states or states[r['id']]['content_hash']!=content_hash(r) or (states[r['id']]['status']!='ready' and (retry_failed or states[r['id']]['status']!='failed'))]
    job={'status':'running','fingerprint':fp,'total':len(pending),'completed':0,'failed':0,'started':db.now()}
    db.put('embedding_job',job);count=0
    records={};batches=[];batch=[]
    for row in pending:
        texts=chunks(row,s['chunk_chars']);digest=content_hash(row)
        records[row['id']]={'row':row,'digest':digest,'texts':texts,'vectors':[None]*len(texts),'received':0}
        for chunk_no,text in enumerate(texts):
            batch.append((row['id'],chunk_no,text))
            if len(batch)==8:batches.append(batch);batch=[]
    if batch:batches.append(batch)

    def fail_articles(article_ids,exc):
        failed_ids=set(article_ids)
        with db.connect() as c:
            for aid in failed_ids:
                rec=records[aid]
                if c.execute('SELECT 1 FROM articles WHERE id=?',(aid,)).fetchone():
                    c.execute('INSERT OR REPLACE INTO embedding_states VALUES(?,?,?,?,?,?)',
                        (aid,fp,rec['digest'],'failed',str(exc),db.now()))
        job.update(status='failed',failed=len(failed_ids),error=str(exc),finished=db.now());db.put('embedding_job',job)
        return {'count':count,'failed':len(failed_ids),'pending':len(pending)-count-len(failed_ids)}

    def publish(aid):
        nonlocal count
        rec=records[aid];row=rec['row'];digest=rec['digest'];texts=rec['texts']
        vectors=services.validate_vectors(rec['vectors'],len(texts))
        check()
        if fp!=services.fingerprint():raise InterruptedError('索引配置已变化')
        with db.connect() as c:
            # Metadata or body edits during a request cannot publish a stale index.
            found=c.execute("""SELECT a.*,l.name,l.canonical_name,l.version,l.kind,l.valid_from,l.valid_to,
              COALESCE(h.heading_path,'') AS heading_path FROM articles a JOIN laws l ON a.law_id=l.id
              LEFT JOIN article_headings h ON h.article_id=a.id WHERE a.id=?""",(row['id'],)).fetchone()
            current=dict(found) if found else None
            if not current or content_hash(current)!=digest:return
            dim=db.get('embedding_dimension:'+fp)
            if dim and dim!=len(vectors[0]):raise llm.ModelError('服务返回的维度与本索引不一致，请检查模型版本并重建')
            c.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',('embedding_dimension:'+fp,json.dumps(len(vectors[0]))))
            c.execute('DELETE FROM embedding_chunks WHERE article_id=? AND fingerprint=?',(row['id'],fp))
            c.executemany('INSERT INTO embedding_chunks VALUES(?,?,?,?,?,?)',[(row['id'],fp,i,digest,t,json.dumps(v)) for i,(t,v) in enumerate(zip(texts,vectors))])
            c.execute('INSERT OR REPLACE INTO embedding_states VALUES(?,?,?,?,?,?)',(row['id'],fp,digest,'ready','',db.now()))
        count+=1;job['completed']=count;db.put('embedding_job',job)

    # Qwen text-embedding-v4 accepts up to 10 inputs; batches of 8 also fit
    # other compatible services. Four requests run at once to reduce latency
    # without opening an unbounded number of paid API calls.
    with ThreadPoolExecutor(max_workers=4,thread_name_prefix='lexcite-embed') as pool:
        for start in range(0,len(batches),4):
            check();wave=batches[start:start+4]
            futures=[pool.submit(services.embed,[item[2] for item in items],cfg=s['embedding'],check=check) for items in wave]
            for items,future in zip(wave,futures):
                try:
                    vectors=future.result()
                    if len(vectors)!=len(items):raise llm.ModelError('向量返回条数与请求不一致')
                    completed=set()
                    for (aid,chunk_no,_),vector in zip(items,vectors):
                        rec=records[aid];rec['vectors'][chunk_no]=vector;rec['received']+=1
                        if rec['received']==len(rec['texts']):completed.add(aid)
                    for aid in completed:
                        try:publish(aid)
                        except llm.ModelError as exc:
                            check()
                            return fail_articles([aid],exc)
                except llm.ModelError as exc:
                    check()
                    return fail_articles([item[0] for item in items],exc)
    job.update(status='completed',finished=db.now());db.put('embedding_job',job)
    db.audit('embedding_index',{'count':count,'fingerprint':fp})
    return {'count':count,'failed':0,'pending':0}


def _worker():
    global _thread,_retry
    from .jobs import MODEL_LOCK
    def check():
        if _stop.is_set():raise InterruptedError('索引已停止')
    try:
        while not _stop.is_set():
            with _guard:
                if not _requested.is_set():break
                _requested.clear();retry=_retry;_retry=False
            while not MODEL_LOCK.acquire(timeout=.2):check()
            try:
                check();build(retry_failed=retry,check=check)
            finally:MODEL_LOCK.release()
    except InterruptedError:
        job=db.get('embedding_job',{});job.update(status='interrupted',finished=db.now());db.put('embedding_job',job)
    except Exception:
        job=db.get('embedding_job',{});job.update(status='failed',error='索引任务未完成，请检查服务日志后重试',finished=db.now());db.put('embedding_job',job)
    finally:
        with _guard:
            _thread=None
            reschedule=_requested.is_set() and not _stop.is_set()
            retry=_retry
        if reschedule:start(manual=retry)


def start(*, manual=False):
    global _thread,_retry
    s=services.settings()
    if s['embedding']['provider']=='none':
        if manual:raise ValueError('请先配置并保存独立向量服务')
        return
    if not manual and (not s['auto_index'] or s['mode']=='lexical'):return
    with _guard:
        _requested.set();_retry=_retry or manual
        if _thread and _thread.is_alive():return
        _stop.clear()
        db.put('embedding_job',{'status':'queued','fingerprint':services.fingerprint(s),'started':db.now()})
        _thread=threading.Thread(target=_worker,name='lexcite-index',daemon=True);_thread.start()


def cancel():
    _stop.set();_requested.clear()


def stop():
    cancel()
    thread=_thread
    if thread:thread.join(timeout=2)


def init():
    ensure_schema()
    job=db.get('embedding_job',{})
    if job.get('status') in ('running','queued'):
        job.update(status='interrupted');db.put('embedding_job',job)
    start()
