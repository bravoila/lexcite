"""Bounded legal research with source-grounded analysis and independent review."""
import json
import re
import time
from .. import db, llm, websearch
from ..ingest import article_key
from . import retrieval, authorities, context as case_state
from .prompts import PLAN, DRAFT, VERIFY, CLARIFY, FACTS
from .schemas import Draft, Plan, Verdict, ClarificationAudit, FactExtraction, CoverageAudit, SourceMetadata
from .verification import validate_claim


class BudgetExceeded(InterruptedError):
    pass


def unique_needs(items):
    result, grouped=[],{}
    for item in items:
        row=item.model_dump() if hasattr(item,'model_dump') else dict(item)
        row['name']=authorities.clean_title(row['name'])
        key=retrieval.law_key(row['name'])
        if key not in grouped:
            grouped[key]=dict(row);result.append(grouped[key])
        else:
            old=grouped[key]
            old['articles']=list(dict.fromkeys(old.get('articles',[])+row.get('articles',[])))[:8]
            if row.get('reason'):old['reason']=row['reason']
    return result[:8]


def split_questions(items):
    facts, legal=[],[]
    for item in items:
        if re.search(r'evidence|法源|法律依据|司法解释.*(?:缺|未提供)|(?:缺少|未提供|未检索).*(?:法条|法规|依据)',item,re.I):legal.append(item)
        else:facts.append(item)
    return facts,legal


def answer(case_id, question, mode='strict', progress=None, cancelled=None, web_query='', online_research=None):
    started=time.monotonic();s=llm.settings();deadline=started+s['agent_timeout']
    case, memories, _=db.case_context(case_id)
    state=case_state.load(case_id,question,memories)
    if len(question)+sum(len(m['text']) for m in memories)>10000:raise ValueError('当前问题与已确认事实超过模型上下文预算，请整理事实或缩短问题后重试。未截断事实。')
    use_model=mode=='strict' and s['provider']!='none'
    online=s['online_research'] if online_research is None else online_research
    trace,warnings,evidence,candidates=[],[],[],[]
    if state['omitted']:warnings.append(f"本轮信息摘要未纳入 {state['omitted']} 段较早陈述；原始对话仍保留，请将关键内容补入已确认事实。")
    result={'status':'insufficient','claims':[],'citations':[],'missing_facts':[],'source_gaps':[],'legal_issues':[],
            'warnings':warnings,'trace':trace,'web_sources':[],'rounds':0,'model_calls':0,'coverage':None,
            'generated_at':db.now(),'event_date':case['event_date'],'jurisdiction':case['jurisdiction'],'analysis_scope':'available_sources',
            'model':s['model'] if s['provider']!='none' else None,'thinking_mode':s['thinking_mode'],
            'reasoning':[],'reasoning_enabled':s['show_reasoning'],'case_facts':state['facts'],'research_focus':state['research_focus'],
            'retrieval_runs':[],'model_runs':[],'model_errors':[],'clarifications_removed':[],'online_research':online,'candidate_sources':[],
            'notice':'已陈述事实不等于客观事实已核实。引用匹配不保证法律适用正确；应另核对过渡规定。联网临时原文未人工复核，不自动纳入正式法库。'}
    context={'question':question,'confirmed_facts':[m['text'] for m in memories],
             'previous_user_messages_unconfirmed':state['history'],'event_date':case['event_date'],'jurisdiction':case['jurisdiction'],
             'research_focus':state['research_focus'],'user_stated_facts':state['facts']}

    def check():
        if cancelled and cancelled():raise InterruptedError('研究已取消')
        if time.monotonic()>=deadline:raise BudgetExceeded('已达到本次研究总时限')

    def emit(stage,step,detail,status='running',round_number=0):
        event={'seq':len(trace)+1,'stage':stage,'step':step,'status':status,'detail':detail,'round':round_number or result['rounds'],'elapsed_seconds':round(time.monotonic()-started,1)}
        trace.append(event)
        if progress:progress(event)

    def call(system,payload,schema,verify=False):
        check()
        if result['model_calls']>=18:raise BudgetExceeded('达到18次模型调用上限')
        result['model_calls']+=1
        item={'call':result['model_calls'],'stage':{Plan:'研究计划',Draft:'法律分析',Verdict:'证据复核',ClarificationAudit:'追问核对',FactExtraction:'理解案件信息',CoverageAudit:'检查法源覆盖',SourceMetadata:'核对官方文件'}.get(schema,'模型调用'),
              'round':result['rounds'],'model':s['verify_model'] if verify and s['verify_model'] else s['model'],'text':'','status':'running','truncated':False}
        run={'call':item['call'],'stage':item['stage'],'round':item['round'],'model':item['model'],'status':'running',
             'payload_characters':len(json.dumps(payload,ensure_ascii=False)),'elapsed_seconds':0,'usage':None}
        result['model_runs'].append(run)
        call_started=time.monotonic()
        auxiliary=schema in (Plan,FactExtraction,ClarificationAudit,SourceMetadata)
        options={'thinking_mode':'disabled'} if auxiliary and s['provider']!='custom' else {}
        if schema is Plan:options['max_tokens']=min(s['max_tokens'],1200)
        run['thinking_mode']=options.get('thinking_mode',s['thinking_mode'])
        if s['show_reasoning']:result['reasoning'].append(item)
        last=[0.0]
        def publish():
            if progress and s['show_reasoning']:progress({'kind':'reasoning','reasoning':result['reasoning']})
        def receive(fragment):
            if not s['show_reasoning']:return
            remaining=min(24000-len(item['text']),100000-sum(len(x['text']) for x in result['reasoning']))
            if remaining>0:item['text']+=fragment[:remaining]
            if len(fragment)>max(0,remaining):item['truncated']=True
            if time.monotonic()-last[0]>=.25:publish();last[0]=time.monotonic()
        publish()
        try:
            with llm.capture_reasoning(receive if s['show_reasoning'] else None),llm.capture_usage(lambda usage:run.update(usage=usage)),llm.request_options(**options):
                value=llm.request_json(system,payload,schema,verify=verify)
            if not isinstance(value,schema):
                try:value=schema.model_validate(value.model_dump() if hasattr(value,'model_dump') else value)
                except Exception as exc:raise llm.ModelError('模型返回结构与当前研究阶段不匹配') from exc
            check();item['status']='available' if item['text'] else 'unavailable'
            run['status']='completed'
            return value
        except llm.ModelError as exc:
            run['status']='failed'
            item['status']='failed';result['model_errors'].append({'stage':item['stage'],'message':str(exc),'fatal':exc.fatal,'http_status':exc.status_code});raise
        except InterruptedError:
            item['status']='interrupted';run['status']='interrupted';raise
        finally:
            run['elapsed_seconds']=round(time.monotonic()-call_started,3)
            publish()
            emit('model_call',run['stage']+'调用结束',f"耗时 {run['elapsed_seconds']} 秒；请求数据 {run['payload_characters']} 字符；"+(f"供应商报告用量 {run['usage']}" if run['usage'] is not None else '供应商未报告 token 用量'),run['status'])

    collector=authorities.ResearchSources(call,check,deadline)

    def sync_context():
        context.update({'research_focus':state['research_focus'],'user_stated_facts':state['facts']})
        result.update({'research_focus':state['research_focus'],'case_facts':state['facts']})

    def questions(items):
        fact_questions,legal=split_questions(items)
        result['legal_issues']=list(dict.fromkeys(result['legal_issues']+legal))
        kept,removed=case_state.filter_questions(fact_questions,state['facts']);result['clarifications_removed'].extend(removed)
        return kept[:3]

    def gaps(needs):
        remaining=[]
        for need in needs:
            if any(retrieval.matches_law(row,need['name']) and (not need.get('articles') or all(any(r['law_id']==row['law_id'] and article_key(n)==article_key(r['number']) for r in collector.rows) for n in need['articles'])) for row in collector.rows):continue
            remaining.append(need)
        return retrieval.authority_gaps(remaining,case['event_date'],case['jurisdiction'])

    def update_gaps(needs, explicit=()):
        result['source_gaps']=gaps(needs)
        for raw in explicit:
            need=raw.model_dump() if hasattr(raw,'model_dump') else raw
            if not any(retrieval.law_key(x['name'])==retrieval.law_key(need['name']) for x in result['source_gaps']):
                result['source_gaps'].append(need|{'state':'not_retrieved','detail':need.get('reason') or '本轮尚未取得支持核心争点的条文。'})

    def bound(rows):
        selected,seen,size=[],set(),0
        for row in rows:
            if row['id'] not in seen and size+len(row['body'])<=18000:
                selected.append(row);seen.add(row['id']);size+=len(row['body'])
        return selected

    def finish(summary=None):
        result['missing_facts']=questions(result['missing_facts'])
        if result['missing_facts'] and state['facts'] and not result['model_errors'] and time.monotonic()<deadline-2 and result['model_calls']<18:
            emit('clarifications','核对是否重复追问','对照用户原话与已记录的问答，只保留尚未回答的必要问题。')
            try:
                audit=call(CLARIFY,{'questions':result['missing_facts'],'sources':state['sources'],'user_stated_facts':state['facts']},ClarificationAudit)
                result['missing_facts'],removed=case_state.grounded_audit(result['missing_facts'],audit,state['sources'],state['facts'])
                result['clarifications_removed'].extend(removed)
            except (llm.ModelError,ValueError,AttributeError,BudgetExceeded):warnings.append('追问语义核对未完成，已保留可确定的原话去重结果。')
        sync_context();result['unknown_facts']=[x for x in state['facts'] if x['state'] in ('unknown','conflicting')]
        result['candidate_sources']=[{'name':r['name'],'number':r['number'],'id':r['id']} for r in candidates[:8]]
        if not use_model:
            result['status']='evidence_only' if evidence else 'insufficient'
            result['citations']=[r|{'label':i+1,'quotes':[]} for i,r in enumerate(evidence[:8])]
        elif not result['claims']:
            result['citations']=[]
            result['status']='model_error' if result['model_errors'] else 'missing_sources' if result['source_gaps'] or result['legal_issues'] else 'needs_facts' if result['missing_facts'] else 'no_supported_answer'
        elif result['source_gaps'] or any(e['fatal'] for e in result['model_errors']):result['status']='partial'
        if result['status']=='model_error':summary='模型调用未完成；本案原话与检索状态已保留，请按接口错误处理后重试。'
        elif not summary:
            summary={'answered':'以下结论基于本次取得并核对的资料，请重点核对个案适用条件。','partial':'以下仅回答现有资料能够支持的部分，不能据此认定问题已得到完整解决。','missing_sources':'现有资料尚不足以形成有依据的结论；推荐补充资料及本次检查结果见下方。','needs_facts':'已识别现有依据，尚需以下必要事实才能形成进一步分析。','no_supported_answer':'本次尚未形成有足够证据支持的结论；没有将检索候选冒充回答依据。','insufficient':'没有检索到符合本案日期、地区与复核要求的条文。','evidence_only':'以下为原文检索结果，未生成法律适用结论。'}[result['status']]
        result['summary']=summary
        result['web_sources']=result['web_sources']+collector.leads
        for gap in result['source_gaps']:
            attempts=[x for x in collector.leads if retrieval.law_key(x.get('requested_name',''))==retrieval.law_key(gap['name'])]
            if attempts:
                gap['online_state']=attempts[-1]['state']
                gap['online_detail']=attempts[-1]['note']
        warnings[:]=list(dict.fromkeys(warnings))
        emit('done','研究结束',f"完成 {result['rounds']} 轮、{result['model_calls']} 次模型调用；形成 {len(result['claims'])} 项带引用结论。",'completed' if result['claims'] or not use_model else 'warning')
        result['elapsed_seconds']=round(time.monotonic()-started,2)
        return result

    def save_claims(valid,approved,rows):
        refs,claims={},[]
        for i,claim in enumerate(valid):
            if i not in approved:continue
            labels=[]
            for ref in claim.evidence:
                if ref.article_id not in refs:refs[ref.article_id]=next(r for r in rows if r['id']==ref.article_id)|{'label':len(refs)+1,'quotes':[]}
                if ref.quote not in refs[ref.article_id]['quotes']:refs[ref.article_id]['quotes'].append(ref.quote)
                labels.append(refs[ref.article_id]['label'])
            claims.append({'text':claim.text,'kind':claim.kind,'section':claim.section,'citations':list(dict.fromkeys(labels))})
        result['claims'],result['citations']=claims,list(refs.values())
        if result['claims']:result['verification']={'quote_integrity':True,'semantic_review':'model','legal_correctness_guaranteed':False,'reviewer_model':s['verify_model'] or s['model']}

    def on_retrieval(item):
        item['round']=result['rounds'];result['retrieval_runs'].append(item)
        emit('retrieval_method','本轮实际检索方式',item['summary'],'warning' if item['warnings'] else 'completed')

    def retrieve(queries,needs):
        nonlocal evidence,candidates
        emit('retrieve','检索本地已复核法源','按法规身份、版本、日期、地区筛选；相同条号不跨法规替代。')
        found,notes=retrieval.search(queries,case['event_date'],case['jurisdiction']);warnings.extend(notes)
        exact=retrieval.requested_articles(needs,case['event_date'],case['jurisdiction'])
        candidates=bound(exact+found+evidence)
        if collector.rows:
            online_hits,notes=retrieval.rank(queries,collector.rows);warnings.extend(notes)
            candidates=bound(online_hits+candidates)
        evidence=candidates
        emit('retrieve','候选检索完成',f'取得 {len(candidates)} 条完整候选；分析和独立复核将检查与争点的直接关系。','completed')

    def acquire():
        if not online or not result['source_gaps']:return False
        before=len(collector.rows);source_started=time.monotonic()
        try:
            new=collector.acquire(result['source_gaps'],case['event_date'],case['jurisdiction'],emit)
        except (llm.ModelError,websearch.SearchError,ValueError) as exc:
            warnings.append(str(exc));emit('sources','官方补查未完成',str(exc),'warning')
            return False
        finally:
            if len(collector.rows)>before:
                # New provisions may contain exceptions to an earlier conclusion.
                result['claims']=[];result['citations']=[]
                result.pop('verification',None)
            emit('source_timing','官方补查耗时',f"耗时 {round(time.monotonic()-source_started,3)} 秒；新增 {len(collector.rows)-before} 条临时原文。",'completed' if len(collector.rows)>before else 'warning')
        return bool(new)

    def run():
        if use_model and state['needs_extraction']:
            emit('context','理解补充信息','关联问题与回答，保留否定、未知、时间和用户原话来源。')
            try:
                extracted=call(FACTS,{'current_question':question,'research_focus':state['research_focus'],'sources':state['sources'],'pending_questions':state['pending_questions'],'existing_facts':state['facts']},FactExtraction)
                case_state.apply_extraction(state,extracted,question);sync_context()
                emit('context','案件信息已整理',f"本轮使用 {len(state['facts'])} 项用户陈述；保持用户陈述与手工确认分开。",'completed')
            except llm.ModelError as exc:
                warnings.append(str(exc));emit('context','保留原话问答信息','语义整理未完成，仍使用从原话关联的问答；未丢弃用户输入。','warning')
                if exc.fatal:return finish()
        needs=authorities.directory_for(state['research_focus']) if use_model else []
        needs=[n for n in needs if not authorities.entry_for(n['name']) or authorities.entry_for(n['name'])['effective_from']<=case['event_date']]
        feedback,previous,last_ids,manual_web=[],None,None,False
        with db.connect() as c:
            library_names=[n for r in c.execute("SELECT name,canonical_name FROM laws") for n in r if n]
        for number in range(1,(s['agent_rounds'] if use_model else 1)+1):
            check();result['rounds']=number;focus=state['research_focus'];plan=None
            fact_query=focus+'\n'+'；'.join(x['text'] for x in state['facts'])[:900] if state['facts'] else focus
            queries=list(dict.fromkeys([focus,fact_query,question]+['《'+n['name']+'》'+' '.join(n.get('articles',[])) for n in needs]))[:9]
            if use_model and number>1:
                emit('repair','制定修正方案','上一轮引用或支持性检查未通过，按具体反馈修正；不因推荐文件尚缺而重复完整研究。')
                try:
                    plan=call(PLAN,context|{'repair_feedback':feedback,'source_gaps':result['source_gaps'],'authority_directory':needs,'previous_observation':{'candidate_ids':last_ids,'coverage':result['coverage']}},Plan)
                    needs=unique_needs(needs+plan.authorities)
                    queries=list(dict.fromkeys([focus,fact_query]+[q[:400] for q in plan.queries if q.strip()]+['《'+n['name']+'》'+' '.join(n.get('articles',[])) for n in needs]))[:9]
                    result['missing_facts']=questions(plan.questions)
                    emit('plan','检索计划就绪','；'.join(queries)[:900],'completed')
                except llm.ModelError as exc:
                    warnings.append(str(exc));emit('plan','保留原研究问题检索',str(exc),'warning')
            retrieve(queries,needs)
            if any(e['fatal'] for e in result['model_errors']):return finish()
            if not manual_web and web_query.strip():
                manual_web=True
                try:
                    emit('web','联网搜索用户提供的公开词',web_query)
                    leads=websearch.search(web_query,check,max(.1,deadline-time.monotonic()))
                    result['web_sources']=leads
                    for i,lead in enumerate(leads[:2]):
                        check()
                        if deadline-time.monotonic()<36:break
                        result['web_sources'][i]=websearch.read_lead(lead)
                except websearch.SearchError as exc:warnings.append(str(exc))
            if not use_model:
                if not web_query.strip():emit('web','本次未联网','原文模式仅检索本地资料；公开检索词留空。','skipped')
                return finish()
            update_gaps(needs)
            if acquire():
                retrieve(queries,needs);update_gaps(needs)
            if any(e['fatal'] for e in result['model_errors']):return finish()
            ids=tuple(sorted(r['id'] for r in evidence))
            if result['claims'] and ids!=last_ids:
                result['claims']=[];result['citations']=[]
                result.pop('verification',None)
            if number>1 and ids==last_ids and (not evidence or plan and plan.action=='clarify'):
                emit('clarifications','停止无新增依据的重复分析','检索证据未新增，保留已核对的部分和必要事实问题。','warning')
                break
            draft=None
            for source_pass in range(2):
                draft=None
                payload=context|{'evidence':evidence,'source_gaps':result['source_gaps'],'analysis_scope':result['analysis_scope'],'repair_feedback':feedback,'previous_attempt':previous}
                emit('draft','分析规则与个案条件','一次检查相关性与覆盖，先回答现有原文支持的部分，同时列出推荐资料。')
                try:draft=call(DRAFT,payload,Draft)
                except llm.ModelError as exc:
                    warnings.append(str(exc));feedback=[{'type':'model_error','reason':str(exc)}]
                    if exc.fatal:return finish()
                    break
                previous=draft.model_dump();result['missing_facts']=questions(draft.missing_facts)
                needs=unique_needs(needs+draft.missing_authorities);update_gaps(needs,draft.missing_authorities)
                if source_pass==0 and acquire():
                    queries=list(dict.fromkeys(queries+['《'+n['name']+'》'+' '.join(n.get('articles',[])) for n in needs]))[:9]
                    retrieve(queries,needs);update_gaps(needs)
                    continue
                break
            if any(e['fatal'] for e in result['model_errors']):return finish()
            if draft is None:continue
            ids=tuple(sorted(r['id'] for r in evidence))
            if not draft.claims:
                emit('draft','尚未形成带依据的结论','模型返回空草稿；本轮没有结论进入引用校验。接下来检查法源缺口，不计为校验通过。','warning')
                result['coverage']=CoverageAudit(selected_ids=[],coverage='missing',missing_authorities=draft.missing_authorities,reason=draft.limitation or '现有资料未形成相关的受支持结论。').model_dump()
                feedback=[{'type':'empty_draft','missing_authorities':[x.model_dump() for x in draft.missing_authorities],'legal_issues':result['legal_issues'],'reason':draft.limitation or '需补齐核心法源；已有一般规则时可作条件分析。'}]
                if result['source_gaps'] or result['missing_facts'] or result['legal_issues'] or ids==last_ids:break
                last_ids=ids;continue
            valid,feedback=[],[]
            emit('citations','校验引用原文','逐项核对法规ID、完整条文中的连续引文、法规名称和条号。')
            for i,claim in enumerate(draft.claims):
                ok,reason=validate_claim(claim,evidence,library_names)
                if ok:valid.append(claim)
                else:feedback.append({'claim_index':i,'type':'invalid_citation','reason':reason})
            emit('citations','引用校验完成',f'通过 {len(valid)} 项；需修正 {len(feedback)} 项。','completed' if not feedback else 'warning')
            selected_ids=list(dict.fromkeys(ref.article_id for claim in valid for ref in claim.evidence))
            coverage=draft.coverage or ('partial' if draft.insufficient or result['source_gaps'] else 'sufficient')
            if not valid:coverage='missing'
            elif draft.insufficient or result['source_gaps'] or coverage=='missing':coverage='partial'
            result['coverage']=CoverageAudit(selected_ids=selected_ids,coverage=coverage,missing_authorities=draft.missing_authorities,reason=draft.limitation or '与法律分析合并检查；结论仍须独立复核。').model_dump()
            emit('coverage','法源覆盖与分析一并完成',f"候选 {len(evidence)} 条；覆盖状态 {coverage}；推荐补充 {len(result['source_gaps'])} 项。",'completed' if coverage=='sufficient' else 'warning')
            if not valid:last_ids=ids;continue
            emit('review','复核证据支持性','检查引用是否支持结论，核对例外、过渡适用、否定、未知和是否遗漏关键条件。')
            try:
                verdict=call(VERIFY,context|{'evidence':evidence,'source_gaps':result['source_gaps'],'coverage':result['coverage'],'draft':[c.model_dump() for c in valid]},Verdict,verify=True)
                if sorted(v.claim_index for v in verdict.checks)!=list(range(len(valid))):raise llm.ModelError('复核结果不完整')
            except llm.ModelError as exc:
                warnings.append(str(exc));feedback.append({'type':'review_error','reason':str(exc)})
                if exc.fatal:return finish()
                continue
            approved={v.claim_index for v in verdict.checks if v.supported}
            feedback.extend({'type':'unsupported_claim','text':valid[v.claim_index].text,'reason':v.reason} for v in verdict.checks if not v.supported)
            emit('review','证据复核完成',f'支持 {len(approved)} 项；需补证或改写 {len(valid)-len(approved)} 项。','completed' if len(approved)==len(valid) else 'warning')
            if approved:save_claims(valid,approved,evidence);result['status']='partial'
            if approved and not feedback and not draft.insufficient and not result['source_gaps'] and not result['missing_facts'] and coverage=='sufficient':
                result['claims']=[];save_claims(valid,approved,evidence);result['status']='answered';return finish()
            if approved and not feedback:return finish()
            last_ids=ids
        return finish()
    try:
        with llm.controlled(check,deadline), retrieval.observe(on_retrieval):return run()
    except BudgetExceeded as exc:
        warnings.append(str(exc)+'，保留已经通过校验的部分。');return finish()
