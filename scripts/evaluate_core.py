"""Opt-in live model evaluation in an isolated database; no production cases copied.

Run inside the deployed environment after configuring its model:
  python scripts/evaluate_core.py --run-live --online --output core-evaluation.json
This invokes the real configured model (and may cost API credits). It is NOT run
by pytest/CI. Automated checks report observable behavior, not legal correctness.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import db, llm, websearch, indexing, retrieval_services
from app.research_agent import authorities, context as case_state, runner as engine
from app.ingest import normalize

QUESTION='离婚后，彩礼可以要求对方返还吗'
FOLLOWUP='双方是否办理了结婚登记？办了。 离婚前是否共同生活？若共同生活，持续时间大约多久？共同生活一年。 是否存在因给付彩礼导致给付方生活困难的情形？没有 彩礼是否已用于共同生活或生育抚养子女？没有。'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live',action='store_true',help='Explicitly run the real configured model; API charges may apply')
    parser.add_argument('--online',action='store_true',help='Allow official source acquisition for this synthetic case')
    parser.add_argument('--output',default='core-evaluation.json')
    args=parser.parse_args()
    if not args.run_live:parser.error('请显式提供 --run-live；这会调用已配置模型，可能产生费用。')
    db.init();indexing.ensure_schema();settings=llm.resolved();search=websearch.settings()
    retrieval_config=retrieval_services.settings()
    for k in ('embedding','reranker'):
        retrieval_config[k]=retrieval_services.resolve(k);retrieval_config[k].pop('api_key_encrypted',None)
    retrieval_config['auto_index']=False
    encrypted=search.get('api_key_encrypted')
    search['api_key']=llm.cipher().decrypt(encrypted.encode()).decode() if encrypted else ''
    if settings['provider']=='none':parser.error('先在网页配置国产模型 API 或 Ollama 并测试连接。')
    # Copy only reviewed legal sources into the isolated run, never cases or messages.
    with db.connect() as c:
        laws=[dict(r) for r in c.execute('SELECT * FROM laws')]
        articles=[dict(r) for r in c.execute('SELECT * FROM articles')]
        indexes={t:[dict(r) for r in c.execute('SELECT * FROM '+t)] for t in ('article_headings','embedding_states','embedding_chunks')}
    original=os.environ.get('LEXCITE_DATA')
    try:
        with tempfile.TemporaryDirectory(prefix='lexcite-core-eval-') as tmp:
            os.environ['LEXCITE_DATA']=tmp;db.init();indexing.ensure_schema()
            retrieval_services.save_settings(retrieval_config)
            settings.pop('api_key_encrypted',None);settings['show_reasoning']=False
            search.pop('api_key_encrypted',None)
            llm.save_settings(settings);websearch.save_settings(search)
            with db.connect() as c:
                for table,rows in [('laws',laws),('articles',articles)]+list(indexes.items()):
                    for row in rows:
                        columns=','.join(row);placeholders=','.join('?' for _ in row)
                        c.execute(f'INSERT INTO {table}({columns}) VALUES({placeholders})',list(row.values()))
                cid=db.uid();today=dt.date.today().isoformat()
                c.execute('INSERT INTO cases VALUES(?,?,?,?,?)',(cid,'Live synthetic evaluation',today,'全国',db.now()))
            turns=[]
            for question in [QUESTION,FOLLOWUP,'之前不是告诉你了吗？请结合已有信息继续分析。']:
                result=engine.answer(cid,question,online_research=args.online)
                uid=db.add_message(cid,'user',{'text':question});db.add_message(cid,'assistant',result)
                with db.connect() as c:case_state.persist(c,cid,result,uid)
                turns.append(result)
                print(f"完成第 {len(turns)} 轮对话：{result['status']}，结论 {len(result['claims'])} 项",flush=True)
            pairs=case_state.question_answer_pairs(FOLLOWUP)
            source_coverage=[]
            for r in turns[1:]:
                source_coverage.append(sum(any(normalize(p['answer']) in normalize(f.get('quote','')) and (normalize(p['question'][:8]) in normalize(f.get('quote','')) or case_state.signature(f.get('question',''))==case_state.signature(p['question'])) for f in r['case_facts']) for p in pairs))
            report={'kind':'live_provider_run','time':db.now(),'model':settings['model'],'provider':settings['provider'],'online':args.online,
                'checks':{'fact_pairs_covered_after_followup':source_coverage,'expected_pairs':4,
                    'original_focus_preserved':all(r['research_focus']==QUESTION for r in turns[1:]),
                    'claims_per_turn':[len(r['claims']) for r in turns],
                    'modern_special_rule_cited':[any(c['name']==authorities.CATALOG[0]['name'] and c['number']=='第五条' for c in r['citations']) for r in turns],
                    'model_errors':[r['model_errors'] for r in turns]},
                'expert_review_required':['一般规则与例外是否完整','一年是否被错误当成固定界限','未用于养育子女是否被误推为没有子女','已提供信息是否被重复追问','法条版本和过渡适用是否正确','是否无依据预测返还比例'],
                'limitation':'本报告不以同一模型给自己的答案打分；自动指标不能替代法律专家核对。','turns':turns}
            target=Path(args.output).resolve();target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text(json.dumps(report,ensure_ascii=False,indent=2));print(target)
    finally:
        if original is None:os.environ.pop('LEXCITE_DATA',None)
        else:os.environ['LEXCITE_DATA']=original


if __name__=='__main__':main()
