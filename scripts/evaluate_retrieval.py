"""Opt-in ablation on a frozen copy of legal data, never business cases.

Reports source availability separately from retrieval recall. No LLM grades its
own answers. --run-live calls the selected embedding/reranker and can incur fees.
"""
import argparse
import copy
import datetime as dt
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import db, indexing, retrieval_services as rs
from app.research_agent import retrieval
from app.ingest import article_key

LEGAL_TABLES=('laws','articles','article_headings','embedding_states','embedding_chunks')


def snapshot():
    indexing.ensure_schema();s=rs.settings()
    for kind in ('embedding','reranker'):
        s[kind]=rs.resolve(kind);s[kind].pop('api_key_encrypted',None)
    with db.connect() as c:
        data={t:[dict(r) for r in c.execute('SELECT * FROM '+t)] for t in LEGAL_TABLES}
        dimensions=[dict(r) for r in c.execute("SELECT * FROM kv WHERE key LIKE 'embedding_dimension:%'")]
    return s,data,dimensions,db.get('settings',{})


def restore(s,data,dimensions,chat_settings):
    db.init();indexing.ensure_schema();s=copy.deepcopy(s);s['auto_index']=False;rs.save_settings(s)
    # Only the review period is needed by corpus filtering; never copy chat keys.
    db.put('settings',{'review_days':chat_settings.get('review_days',180)})
    with db.connect() as c:
        for table in LEGAL_TABLES:
            for row in data[table]:
                cols=','.join(row);marks=','.join('?' for _ in row)
                c.execute(f'INSERT INTO {table}({cols}) VALUES({marks})',list(row.values()))
        for row in dimensions:c.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',(row['key'],row['value']))


def matches(row,target):
    return retrieval.matches_law(row,target['law']) and article_key(row['number'])==article_key(target['article'])


def metrics(hits,expected,corpus,k=20):
    available=[x for x in expected if any(matches(r,x) for r in corpus)]
    found=[x for x in expected if any(matches(r,x) for r in hits[:k])]
    first=next((i+1 for i,r in enumerate(hits[:k]) if any(matches(r,x) for x in expected)),None)
    gained=set();gains=[]
    for row in hits[:10]:
        target=next((i for i,x in enumerate(expected) if matches(row,x)),None)
        gains.append(int(target is not None and target not in gained))
        if target is not None:gained.add(target)
    ideal=sum(1/math.log2(i+2) for i in range(min(10,len(available))))
    return {'required_sources':len(expected),'available_sources':len(available),'found_sources':len(found),
      'source_coverage':len(available)/len(expected) if expected else None,
      'recall_at_20_all_required':len(found)/len(expected) if expected else None,
      'recall_at_20_available':len(found)/len(available) if available else None,
      'mrr_at_20':1/first if first else (0 if available else None),
      'ndcg_at_10':sum(g/math.log2(i+2) for i,g in enumerate(gains))/ideal if ideal else None,
      'missing_from_corpus':[x for x in expected if x not in available]}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live',action='store_true')
    parser.add_argument('--build-index',action='store_true',help='Build missing vectors in the isolated evaluation copy')
    parser.add_argument('--dataset',default=str(db.ROOT/'evals/retrieval_scenarios.json'))
    parser.add_argument('--output',default='retrieval-evaluation.json')
    args=parser.parse_args()
    if not args.run_live:parser.error('需显式提供 --run-live；将调用已配置检索服务。')
    dataset=json.loads(Path(args.dataset).read_text())
    db.init();s,data,dimensions,chat=snapshot()
    if s['embedding']['provider']=='none' or s['reranker']['provider']=='none':parser.error('三组对照需要先配置独立向量服务和重排服务。')
    original=os.environ.get('LEXCITE_DATA');report={'kind':'live_retrieval_ablation','created':db.now(),'dataset':dataset.get('name'),
      'models':{k:{'provider':s[k]['provider'],'model':s[k]['model'],'revision':s[k]['revision']} for k in ('embedding','reranker')},
      'source_versions':[{'name':r['canonical_name'] or r['name'],'version':r['version'],'sha256':r['sha256'],'reviewed_at':r['reviewed_at']} for r in data['laws']],
      'limitation':'参考条文为检索验收起点，非穷尽的法律答案；法律适用和最终结论仍需人工评阅。无可用法源时不计算可用法库召回率。','runs':[]}
    try:
        with tempfile.TemporaryDirectory(prefix='lexcite-retrieval-eval-') as temp:
            os.environ['LEXCITE_DATA']=temp;restore(s,data,dimensions,chat)
            if args.build_index:report['index_build']=indexing.build()
            report['index']=indexing.status()
            for mode in ('bm25','hybrid','hybrid_rerank'):
                config=copy.deepcopy(s);config['auto_index']=False;config['mode']='lexical' if mode=='bm25' else 'hybrid'
                if mode!='hybrid_rerank':config['reranker']['provider']='none'
                rs.save_settings(config)
                for case in dataset['cases']:
                    trace=[];started=time.monotonic();date=case.get('event_date') or dt.date.today().isoformat();region=case.get('jurisdiction','全国')
                    corpus,_=retrieval.corpus(date,region)
                    with retrieval.observe(trace.append):hits,notes=retrieval.search([case['query']],date,region,limit=20)
                    run={'mode':mode,'case_id':case['id'],'query':case['query'],'metrics':metrics(hits,case['expected'],corpus),
                      'elapsed_seconds':round(time.monotonic()-started,3),'trace':trace,'warnings':notes,
                      'hits':[{'law':r['name'],'article':r['number'],'channels':r.get('retrieval_channels',[]),'rerank_score':r.get('rerank_score')} for r in hits],
                      'executed_as_requested':bool(trace) and (mode=='bm25' or all(t['embedding_status']=='applied' and (mode!='hybrid_rerank' or t['reranker_status']=='applied') for t in trace))}
                    report['runs'].append(run)
                print('完成 '+mode+'；是否真正启用目标路径见 executed_as_requested',flush=True)
            report['summary']={}
            for mode in ('bm25','hybrid','hybrid_rerank'):
                runs=[r for r in report['runs'] if r['mode']==mode]
                valid=[r for r in runs if r['executed_as_requested'] and r['metrics']['recall_at_20_available'] is not None]
                report['summary'][mode]={'cases':len(runs),'valid_comparisons':len(valid),'mean_recall_at_20_available':sum(r['metrics']['recall_at_20_available'] for r in valid)/len(valid) if valid else None,
                  'mean_ndcg_at_10':sum(r['metrics']['ndcg_at_10'] for r in valid)/len(valid) if valid else None}
    finally:
        if original is None:os.environ.pop('LEXCITE_DATA',None)
        else:os.environ['LEXCITE_DATA']=original
    target=Path(args.output).resolve();target.parent.mkdir(parents=True,exist_ok=True);target.write_text(json.dumps(report,ensure_ascii=False,indent=2));print(target)


if __name__=='__main__':main()
