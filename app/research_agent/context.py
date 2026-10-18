"""Source-grounded user observations, including question/answer supplements.
Assistant questions provide referents only; assistant claims are never facts.
"""
import json
import re
from .. import db
from ..ingest import normalize

UNKNOWN = re.compile(r"未知|不清楚|不确定|不知道|尚未查明|尚不明确|无法确定|暂不清楚|待核实")
QUESTION = re.compile(r"[?？]|是否|能否|如何|怎么|可否|吗[。！!\s]*$")
META = re.compile(r"之前.*告诉|不是.*说过|已经.*告诉|^(?:请)?(?:继续|接着)(?:分析|研究|说|干)?[。！!\s]*$|为什么.*重复|我说过|上面.*说")
SHORT_REPLY = re.compile(r"^(?:办了|没有|有|是|否|不是|已办理|未办理|不知道|不清楚|未知|不确定|对|不对)[。！!\s]*$")


def signature(text):
    text = normalize(text).lower()
    text = re.sub(r"[，,。？?：:；;！!、\s]", "", text)
    for phrase in ("请问", "请确认", "请补充", "请提供", "是否", "是不是", "有没有", "有无", "已经", "尚未", "并未", "没有", "未曾", "不曾", "实际", "双方", "当事人", "更正", "纠正", "改为", "未知", "不清楚", "不知道", "不确定", "待核实"):
        text = text.replace(phrase, "")
    return re.sub(r"[已未不吗呢了的]", "", text)


def polarity(text):
    if UNKNOWN.search(text):
        return "unknown"
    return "negative" if re.search(r"未|没有|不曾|并非|不是|不[是为]|否认|^否$", text) else "positive"


def sentences(text):
    return [x.strip() for x in re.split(r"[\n\r；;。]", text) if 3 <= len(x.strip()) <= 800]


def question_answer_pairs(text):
    pairs, pending, start = [], "", 0
    for match in re.finditer(r"([^。；;\n？?]+)([？?。；;\n]|$)", text):
        fragment, delimiter = match[1].strip(), match[2]
        if delimiter in ('?', '？'):
            # A short answer can immediately precede the next copied question.
            prefix = re.match(r"^(办了|没有|有|是|否|不是|未知|不知道|不清楚)[，,\s]+(.+)$", fragment)
            if pending and prefix and QUESTION.search(prefix[2]):
                answer = prefix[1]
                stop = match.start() + match[1].find(answer) + len(answer)
                pairs.append({'question':pending, 'answer':answer, 'quote':text[start:stop].strip(), 'start':start, 'end':stop})
                pending = prefix[2] + delimiter
                start = match.start() + match[1].find(prefix[2])
            elif pending and fragment.startswith(('若', '如果', '具体', '持续')):
                pending += fragment + delimiter
            else:
                pending, start = fragment + delimiter, match.start()
        elif pending and fragment:
            pairs.append({'question':pending, 'answer':fragment, 'quote':text[start:match.end()].strip(), 'start':start, 'end':match.end()})
            pending = ''
    return pairs


def observation(source, quote, question='', answer=''):
    text = f'{question} 答：{answer}' if question else quote
    return {'topic':signature(question or text), 'text':text, 'state':'unknown' if UNKNOWN.search(answer or quote) else 'stated',
            'source_id':source['id'], 'source_kind':source['kind'], 'quote':quote, 'question':question, 'answer':answer}


def merge_observations(items):
    output, groups = [], {}
    for item in items:
        key = item['topic']
        old = groups.get(key)
        if old:
            changed = {polarity(old.get('answer') or old['text']), polarity(item.get('answer') or item['text'])} == {'negative','positive'}
            number_pattern=r'[零〇一二三四五六七八九十百千万两0-9.]+(?:年|个月|月|日|天|万元|元|岁)'
            old_numbers=re.findall(number_pattern,old.get('answer') or old['text'])
            new_numbers=re.findall(number_pattern,item.get('answer') or item['text'])
            changed=changed or bool(old_numbers and new_numbers and old_numbers!=new_numbers)
            correction = bool(re.search(r'更正|纠正|改为|之前.*有误|刚才.*错',item['quote']))
            if (changed or old['state']=='conflicting') and not correction:
                item = item | {'state':'conflicting','conflicting_quotes':list(dict.fromkeys(old.get('conflicting_quotes',[old['quote']])+[item['quote']]))}
            output.remove(old)
        groups[key] = item; output.append(item)
    return output


def load(case_id, question, memories):
    with db.connect() as c:
        rows = c.execute('SELECT id,role,payload FROM messages WHERE case_id=? ORDER BY rowid',(case_id,)).fetchall()
        snapshot = c.execute('SELECT payload FROM case_state_snapshots WHERE case_id=?',(case_id,)).fetchone()
    sources, pending = [], []
    for row in rows:
        body = json.loads(row['payload'])
        if row['role']=='user':
            sources.append({'id':row['id'],'text':body['text'],'kind':'user','pending':pending})
            pending=[]
        else:
            # Only the explicit questions list supplies references for short replies.
            pending=[{'id':row['id']+':'+str(i),'text':q} for i,q in enumerate(body.get('missing_facts',[]))]
    sources.append({'id':'current','text':question,'kind':'user','pending':pending})
    sources.extend({'id':'memory:'+m['id'],'text':m['text'],'kind':'confirmed','pending':[]} for m in memories)
    observations, focus, qa_count = [], '', 0
    for source in sources:
        text = source['text']; pairs = question_answer_pairs(text)
        if not pairs and SHORT_REPLY.fullmatch(text.strip()) and len(source['pending'])==1:
            pairs=[{'question':source['pending'][0]['text'],'answer':text.strip(),'quote':text.strip(),'start':0,'end':len(text)}]
        for pair in pairs:
            observations.append(observation(source,pair['quote'],pair['question'],pair['answer']));qa_count+=1
        remainder = list(text)
        for pair in pairs:
            remainder[pair['start']:pair['end']] = ' '*(pair['end']-pair['start'])
        rest=''.join(remainder)
        for quote in sentences(rest):
            if (QUESTION.search(quote) and not UNKNOWN.search(quote)) or META.search(quote):continue
            item=observation(source,quote)
            if len(item['topic'])>=3:observations.append(item)
        if source['kind']=='user':
            for line in sentences(rest):
                if META.search(line) or UNKNOWN.search(line):continue
                if QUESTION.search(line) and not SHORT_REPLY.fullmatch(line):focus=line
    if not focus:
        focus=next((s['text'][:800] for s in sources if s['kind']=='user' and s['text'].strip() and not META.search(s['text'])),question)
    refs={s['id']:s for s in sources}
    # Reuse only saved observations still grounded in the original USER source.
    if snapshot:
        previous=json.loads(snapshot[0])
        semantic=previous.get('semantic_facts',[])
        for item in semantic:
            source=refs.get(item['source_id'])
            if source and normalize(item['quote']) in normalize(source['text']):
                observations=[o for o in observations if not (o['source_id']==item['source_id'] and normalize(o['quote'])==normalize(item['quote']))]
                observations.append(item)
        if previous.get('research_focus') and (not question or question_answer_pairs(question) or SHORT_REPLY.fullmatch(question.strip()) or META.search(question)):
            focus=previous['research_focus']
    order={s['id']:i for i,s in enumerate(sources)}
    observations.sort(key=lambda item:order.get(item['source_id'],-1))
    observations=merge_observations(observations)
    selected, size=[],0
    for item in reversed(observations):
        if size+len(item['text'])+len(item.get('quote',''))<=18000 and len(selected)<70:
            selected.append(item);size+=len(item['text'])+len(item.get('quote',''))
    selected.reverse()
    history, budget=[],0
    for source in reversed([s for s in sources if s['kind']=='user' and s['id']!='current']):
        if len(history)<12 and budget+len(source['text'])<=8000:history.append(source['text']);budget+=len(source['text'])
    history.reverse()
    grounding=[];budget=0
    for source in reversed(sources):
        if source['text'] and budget+len(source['text'])<=16000:
            grounding.append({'id':source['id'],'text':source['text'],'kind':source['kind']});budget+=len(source['text'])
    grounded_ids={s['id'] for s in grounding}
    for item in selected:
        if item['source_id'] not in grounded_ids:
            grounding.append({'id':item['source_id'],'text':item['quote'],'kind':item['source_kind']});grounded_ids.add(item['source_id'])
    return {'facts':selected,'history':history,'research_focus':focus[:800],'sources':grounding,'pending_questions':pending,'source_order':list(order),
            'omitted':len(observations)-len(selected),'semantic_facts':[i for i in selected if i.get('semantic')],
            'needs_extraction':bool(question_answer_pairs(question) or pending or SHORT_REPLY.fullmatch(question.strip()) or re.search(r'更正|纠正|分别|依次',question))}


def apply_extraction(state, extraction, current_question):
    refs={s['id']:s for s in state['sources']};pending={q['id']:q['text'] for q in state['pending_questions']}
    accepted=[]
    for fact in extraction.facts:
        source=refs.get(fact.source_id)
        if not source or not normalize(fact.quote) or normalize(fact.quote) not in normalize(source['text']):continue
        # A one-word reply needs the actual prior question or an in-message Q/A.
        if len(normalize(fact.quote))<3 and fact.question_id not in pending:continue
        if fact.question_id and fact.question_id not in pending:continue
        # A denial of use of funds is not a denial that children exist.
        if re.search(r'子女|孩子|孕育|生育',fact.topic) and '用于' in fact.quote and not re.search(r'(?:没有|未|已|曾|有)\s*(?:怀孕|生育|孩子|子女)',fact.quote):continue
        item=fact.model_dump(exclude={'question_id','supersedes'})
        item.update({'topic':signature(fact.topic),'source_kind':source.get('kind','user'),'semantic':True})
        literal=next((o for o in state['facts'] if o['source_id']==fact.source_id and normalize(o['quote'])==normalize(fact.quote) and o.get('answer')),None)
        if literal:
            # A deterministic Q/A association is stronger than a paraphrase.
            # Retain the actual reply even if the model reverses a negation.
            item.update({k:literal[k] for k in ('topic','text','state','question','answer')})
        if fact.question_id:item['question']=pending[fact.question_id]
        if UNKNOWN.search(fact.quote):item['state']='unknown'
        accepted.append(item)
    if accepted:
        facts=list(state['facts'])
        for item in accepted:
            facts=[o for o in facts if not (o['source_id']==item['source_id'] and normalize(o['quote'])==normalize(item['quote']))]
            facts.append(item)
        order={source_id:i for i,source_id in enumerate(state['source_order'])}
        facts.sort(key=lambda item:order.get(item['source_id'],-1))
        state['facts']=merge_observations(facts)[-70:]
        state['semantic_facts']=[x for x in state['facts'] if x.get('semantic')]
    # New questions must actually occur in the user's current utterance.
    if extraction.intent=='new_question' and extraction.research_question and normalize(extraction.research_question) in normalize(current_question) and not question_answer_pairs(current_question):
        state['research_focus']=extraction.research_question
    return state


def persist(connection, case_id, result, user_message_id):
    items=[]
    for item in result.get('case_facts',[]):
        value=item.copy()
        if value['source_id']=='current':value['source_id']=user_message_id
        items.append(value)
    snapshot={'semantic_facts':[x for x in items if x.get('semantic')],'research_focus':result.get('research_focus','')}
    connection.execute('INSERT OR REPLACE INTO case_state_snapshots VALUES(?,?)',(case_id,json.dumps(snapshot,ensure_ascii=False)))


def classify_question(question, facts):
    key=signature(question)
    for fact in reversed(facts):
        keys={signature(fact.get('question') or fact['text']),signature(fact['topic'])}
        if len(key)>=4 and key in keys:return fact['state'],fact
    return 'missing',None


def filter_questions(questions, facts):
    missing, omitted, seen=[],[],set()
    for question in questions:
        question=question.strip()[:350];key=signature(question)
        if not question or key in seen:continue
        seen.add(key);state,fact=classify_question(question,facts)
        if state in ('stated','unknown'):omitted.append({'question':question,'state':state,'source_id':fact['source_id'],'quote':fact['quote']})
        else:missing.append(question)
    return missing,omitted


def grounded_audit(questions, verdict, sources, facts=()):
    refs={s['id']:s['text'] for s in sources};checks=verdict.checks
    if sorted(c.question_index for c in checks)!=list(range(len(questions))):raise ValueError('追问核对结果不完整')
    kept,excluded=[],[]
    for item in checks:
        q=questions[item.question_index];text=refs.get(item.source_id,'')
        grounded=len(normalize(item.quote))>=4 and normalize(item.quote) in normalize(text)
        if len(normalize(item.quote))<4:
            grounded=any(f['source_id']==item.source_id and normalize(f['quote'])==normalize(item.quote) and signature(f.get('question',''))==signature(q) for f in facts)
        if item.state in ('answered','unknown') and grounded and classify_question(q,facts)[0]!='conflicting':
            state='unknown' if UNKNOWN.search(item.quote) else item.state
            excluded.append({'question':q,'state':state,'source_id':item.source_id,'quote':item.quote})
        else:kept.append(q)
    return kept,excluded
