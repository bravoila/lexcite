"""Independent embedding/reranking adapters. No implicit chat credentials or remote fallback."""
import hashlib
import json
import math
import os
import time
from urllib.parse import urlsplit
import httpx
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Literal
from . import db, llm
from .models import SettingsInput, validate_configuration

INSTRUCTION = 'Given a Chinese legal question, retrieve statutory provisions that address the issue, including applicable conditions and exceptions.'


class Service(BaseModel):
    provider: Literal['none','ollama','dashscope','siliconflow','custom'] = 'none'
    base_url: str = 'http://host.docker.internal:11434'
    model: str = Field(default='', max_length=160)
    api_key: str | None = Field(default=None, max_length=1000)
    timeout: int = Field(default=90, ge=5, le=300)
    dimensions: int = Field(default=0, ge=0, le=8192)
    revision: str = Field(default='', max_length=100)
    query_instruction: str = Field(default='', max_length=700)

    @model_validator(mode='after')
    def configured(self):
        if self.provider != 'none':
            self.base_url = SettingsInput.endpoint(self.base_url)
        if self.provider != 'none' and not self.model.strip():
            raise ValueError('启用服务时必须填写模型名称')
        self.model=self.model.strip()
        if self.provider=='ollama' and self.dimensions:
            raise ValueError('Ollama 使用模型原生维度，请将维度设为0')
        return self


class RetrievalSettings(BaseModel):
    mode: Literal['hybrid','lexical'] = 'hybrid'
    embedding: Service = Field(default_factory=Service)
    reranker: Service = Field(default_factory=Service)
    auto_index: bool = True
    recall_k: int = Field(default=40, ge=10, le=100)
    rerank_candidates: int = Field(default=60, ge=10, le=100)
    final_k: int = Field(default=14, ge=5, le=30)
    chunk_chars: int = Field(default=1200, ge=300, le=2000)

    @model_validator(mode='after')
    def no_ollama_rerank(self):
        if self.reranker.provider=='ollama':
            raise ValueError('重排请使用独立 rerank 服务；Ollama 仅作为向量服务')
        return self


def settings(*, validate=True):
    stored=db.get('retrieval_settings', {})
    base=RetrievalSettings().model_dump()
    for key in ('embedding','reranker'):
        base[key].update(stored.get(key,{}))
    base.update({k:v for k,v in stored.items() if k not in ('embedding','reranker')})
    for kind in ('embedding','reranker'):
        cfg=base[kind];saved=stored.get(kind,{})
        prefix='EMBEDDING' if kind=='embedding' else 'RERANKER'
        env_values={}
        for field,suffix in (('provider','PROVIDER'),('base_url','BASE_URL'),('model','MODEL'),('timeout','TIMEOUT')):
            name=f'{prefix}_{suffix}';value=os.getenv(name)
            if field not in saved and value:
                env_values[field]=value
        cfg.update(env_values)
    if not validate:
        return base
    result = validate_configuration(RetrievalSettings, base, "检索服务")
    for kind in ('embedding', 'reranker'):
        result[kind].update({key: base[kind][key] for key in ('api_key_encrypted', 'environment_key_disabled') if key in base[kind]})
    return result


def environment_api_key(kind, cfg):
    prefix = 'EMBEDDING' if kind == 'embedding' else 'RERANKER'
    defaults = Service().model_dump()
    provider = os.getenv(prefix + '_PROVIDER') or defaults['provider']
    endpoint = (os.getenv(prefix + '_BASE_URL') or defaults['base_url']).rstrip('/')
    bound = cfg['provider'] != 'none' and cfg['provider'] == provider and cfg['base_url'].rstrip('/') == endpoint
    return (os.getenv(prefix + '_API_KEY') or '') if bound and not cfg.get('environment_key_disabled') else ''


def public_settings():
    s=settings(validate=False)
    try:
        s = validate_configuration(RetrievalSettings, s, "检索服务") | {
            kind: validate_configuration(Service, s[kind], "向量" if kind == 'embedding' else "重排")
            for kind in ('embedding', 'reranker')
        }
    except ValueError as exc:
        s['configuration_error'] = str(exc)
    stored=db.get('retrieval_settings',{})
    for kind in ('embedding','reranker'):
        cfg=s[kind]
        env_key=environment_api_key(kind,cfg)
        encrypted=cfg.pop('api_key_encrypted','')
        uses_environment_key=not encrypted and bool(env_key)
        cfg['environment_defaults']=any(
            field not in stored.get(kind,{}) and bool(os.getenv(f"{'EMBEDDING' if kind=='embedding' else 'RERANKER'}_{suffix}"))
            for field,suffix in (('provider','PROVIDER'),('base_url','BASE_URL'),('model','MODEL'),('timeout','TIMEOUT'))
        )
        cfg['environment_api_key']=uses_environment_key
        cfg['has_api_key']=bool(uses_environment_key or encrypted)
        cfg.pop('api_key',None)
        cfg.pop('environment_key_disabled',None)
    return s


def save_settings(value):
    value=RetrievalSettings.model_validate(value).model_dump()
    old=settings(validate=False)
    old_stored=db.get('retrieval_settings',{})
    for kind in ('embedding','reranker'):
        cfg=value[kind];key=cfg.pop('api_key',None)
        same=all(cfg[k]==old[kind][k] for k in ('provider','base_url'))
        if key is not None:
            cfg['api_key_encrypted']=llm.cipher().encrypt(key.encode()).decode() if key else ''
        elif same and 'api_key_encrypted' in old_stored.get(kind,{}):
            cfg['api_key_encrypted']=old_stored[kind]['api_key_encrypted']
        elif not same:
            cfg['api_key_encrypted']=''
        cfg['environment_key_disabled'] = key == '' or (key is None and old[kind].get('environment_key_disabled', False))
    db.put('retrieval_settings', value)
    return public_settings()


def init():
    if db.get('retrieval_settings') is not None:return
    legacy=llm.settings()
    if legacy.get('embedding_model'):
        value=RetrievalSettings().model_dump()
        resolved=llm.resolved() if legacy.get('api_key_encrypted') else {'api_key':''}
        value['embedding'].update(provider=legacy['provider'] if legacy['provider'] in ('ollama','dashscope','siliconflow') else 'custom',
            base_url=legacy['base_url'],model=legacy['embedding_model'],api_key=resolved['api_key'],timeout=min(300,legacy['timeout']))
        # Migration does not spend API credits or reuse old vectors silently.
        value['auto_index']=False
        save_settings(value)


def resolve(kind, cfg=None):
    cfg=dict(cfg or settings()[kind])
    cfg=validate_configuration(Service,cfg,'向量' if kind=='embedding' else '重排')
    key=cfg.get('api_key') or ''
    if not key and cfg.get('api_key_encrypted'):
        try:key=llm.cipher().decrypt(cfg['api_key_encrypted'].encode()).decode()
        except Exception as exc:raise llm.ModelError('检索服务密钥无法解密，请重新保存该服务密钥') from exc
    if not key:
        key=environment_api_key(kind,cfg)
    cfg['api_key']=key
    return cfg


def fingerprint(s=None):
    s=s or settings();e=s['embedding']
    identity={k:e.get(k) for k in ('provider','base_url','model','dimensions','revision','query_instruction')}
    identity.update(recipe='law-chunks-v1',chunk_chars=s['chunk_chars'])
    return hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:32]


def post(cfg, path, body, check=lambda:None):
    check();control=llm._control.get();timeout=cfg['timeout']
    if control:
        control[0]();timeout=min(timeout,max(.1,control[1]-time.monotonic()))
    headers={'Content-Type':'application/json'}
    if cfg.get('api_key'):headers['Authorization']='Bearer '+cfg['api_key']
    end=time.monotonic()+timeout
    try:
        with httpx.Client(timeout=timeout,follow_redirects=False,trust_env=False) as client:
            with client.stream('POST',cfg['base_url'].rstrip('/')+path,json=body,headers=headers) as response:
                if response.status_code>=300:
                    llm.check_status(response,cfg.get('api_key',''))
                data=bytearray()
                for chunk in response.iter_bytes():
                    check()
                    if control:control[0]()
                    if time.monotonic()>end:raise llm.ModelError('检索服务响应超过时限')
                    data.extend(chunk)
                    if len(data)>16_000_000:raise llm.ModelError('检索服务响应超过16MB，已停止')
        check()
        if control:control[0]()
        return json.loads(data)
    except (llm.ModelError,InterruptedError):raise
    except Exception as exc:raise llm.ModelError('检索服务连接失败或返回格式错误；未改变已有原文资料') from exc


def validate_vectors(values, count, dimensions=0):
    if not isinstance(values,list) or len(values)!=count:raise llm.ModelError('向量返回条数与请求不一致')
    dim=dimensions or (len(values[0]) if values and isinstance(values[0],list) else 0)
    if dim<1 or dim>8192:raise llm.ModelError('向量维度无效')
    result=[]
    for v in values:
        if not isinstance(v,list) or len(v)!=dim or any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) for x in v):
            raise llm.ModelError('向量含非有限数字或维度不一致')
        norm=math.sqrt(sum(x*x for x in v))
        if not math.isfinite(norm) or norm<=0:raise llm.ModelError('向量范数无效')
        result.append([x/norm for x in v])
    return result


def embed(texts, *, query=False, cfg=None, check=lambda:None):
    cfg=resolve('embedding',cfg)
    if cfg['provider']=='none':raise llm.ModelError('尚未配置独立向量服务')
    if not texts:return []
    if any(not isinstance(t,str) or not t.strip() for t in texts):raise llm.ModelError('向量输入不能为空')
    instruction=cfg.get('query_instruction','')
    inputs=[f'Instruct: {instruction}\nQuery: {t}' if query and instruction else t for t in texts]
    body={'model':cfg['model'],'input':inputs}
    if cfg['provider']=='ollama':
        cfg['base_url']=cfg['base_url'].removesuffix('/v1').removesuffix('/api')
        body['truncate']=False
        data=post(cfg,'/api/embed',body,check)
        values=data.get('embeddings')
    else:
        if cfg.get('dimensions'):body['dimensions']=cfg['dimensions']
        body['encoding_format']='float'
        data=post(cfg,'/embeddings',body,check)
        rows=data.get('data',[])
        if not isinstance(rows,list) or any(not isinstance(r,dict) or type(r.get('index')) is not int for r in rows) or sorted(r['index'] for r in rows)!=list(range(len(inputs))):
            raise llm.ModelError('向量接口返回的索引不完整或重复')
        values=[r.get('embedding') for r in sorted(rows,key=lambda r:r['index'])]
    return validate_vectors(values,len(inputs),cfg.get('dimensions',0))


def rerank(query, documents, *, cfg=None, check=lambda:None):
    cfg=resolve('reranker',cfg)
    if cfg['provider']=='none':raise llm.ModelError('尚未配置重排服务')
    if cfg['provider']=='dashscope':
        body={'model':cfg['model'],'input':{'query':query,'documents':documents},'parameters':{'top_n':len(documents)}}
        path='/services/rerank/text-rerank/text-rerank'
        base=cfg['base_url'].rstrip('/')
        if base.endswith(path):
            base=base[:-len(path)]
        # Preserve the configured host/region while migrating old chat or
        # compatible rerank roots to the documented DashScope native API.
        for suffix in ('/compatible-api/v1/reranks','/compatible-mode/v1/reranks',
                       '/compatible-api/v1','/compatible-mode/v1'):
            if base.endswith(suffix):
                base=base[:-len(suffix)]+'/api/v1'
                break
        if not urlsplit(base).path:
            base+='/api/v1'
        cfg['base_url']=base
    else:
        body={'model':cfg['model'],'query':query,'documents':documents,'top_n':len(documents),'return_documents':False}
        path='/rerank'
    try:
        data=post(cfg,path,body,check)
    except llm.ModelError as exc:
        if exc.status_code!=404:
            raise
        request_path=urlsplit(cfg['base_url']).path.rstrip('/')+path
        hint='；阿里百炼重排使用 /api/v1 根地址，请核对域名、地域和模型是否可用' if cfg['provider']=='dashscope' else '；请核对该服务是否提供此重排接口'
        raise llm.ModelError(f'{exc}；重排请求路径：{request_path}{hint}',
            fatal=exc.fatal,status_code=exc.status_code,code=exc.code) from exc
    rows=data.get('output',{}).get('results') if cfg['provider']=='dashscope' else data.get('results')
    if not isinstance(rows,list) or len(rows)!=len(documents):raise llm.ModelError('重排结果未覆盖全部候选')
    scores={}
    for row in rows:
        if not isinstance(row,dict):raise llm.ModelError('重排结果结构错误')
        i=row.get('index');score=row.get('relevance_score')
        if type(i) is not int or not 0<=i<len(documents) or i in scores or isinstance(score,bool) or not isinstance(score,(int,float)) or not math.isfinite(score):
            raise llm.ModelError('重排返回无效候选编号或分数')
        scores[i]=float(score)
    return sorted(scores.items(),key=lambda x:(-x[1],x[0]))
