"""Private Qwen3 reranker sidecar. No arbitrary model loading via requests."""
import os
import threading
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

MODEL_ID=os.getenv('RERANK_MODEL','Qwen/Qwen3-Reranker-0.6B')
MODEL_PATH=os.getenv('RERANK_MODEL_PATH') or MODEL_ID
_model=None
_tokenizer=None
_lock=threading.Lock()
MAX_TOKENS=8192
TASK='Given a Chinese legal question, identify directly relevant statutory provisions, including conditions and exceptions.'


@asynccontextmanager
def lifespan(app):
    global _model,_tokenizer
    import torch
    from transformers import AutoTokenizer, AutoModelForCausalLM
    device=os.getenv('RERANK_DEVICE','cpu')
    dtype=torch.float32 if device=='cpu' else torch.float16
    _tokenizer=AutoTokenizer.from_pretrained(MODEL_PATH,padding_side='left',trust_remote_code=False)
    _model=AutoModelForCausalLM.from_pretrained(MODEL_PATH,torch_dtype=dtype,trust_remote_code=False).to(device).eval()
    yield


app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None)


class Input(BaseModel):
    model: str
    query: str = Field(min_length=1,max_length=2400)
    documents: list[str] = Field(min_length=1,max_length=180)
    top_n: int = Field(default=60,ge=1,le=180)
    return_documents: bool = False


@app.get('/healthz')
def health():
    return {'ready':_model is not None,'model':MODEL_ID}


@app.post('/v1/rerank')
def rerank(value: Input):
    if value.model!=MODEL_ID:raise HTTPException(400,'模型名称与已加载模型不一致')
    if any(not x.strip() or len(x)>6000 for x in value.documents):raise HTTPException(400,'候选片段为空或过长')
    if not _lock.acquire(blocking=False):raise HTTPException(429,'重排服务正在处理其他请求')
    try:
        import torch
        rows=[]
        # Match Qwen3's reranker yes/no scoring format. This is not chat generation.
        prefix='<|im_start|>system\nJudge relevance of the Document to the Query under the Instruct. Answer only yes or no.<|im_end|>\n<|im_start|>user\n'
        suffix='<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n'
        sequences=[]
        for doc in value.documents:
            text=prefix+f'<Instruct>: {TASK}\n<Query>: {value.query}\n<Document>: {doc}'+suffix
            ids=_tokenizer.encode(text,add_special_tokens=False)
            if len(ids)>MAX_TOKENS:raise HTTPException(400,'输入超过重排上下文限制，请缩小索引片段')
            sequences.append(ids)
        yes=_tokenizer.convert_tokens_to_ids('yes');no=_tokenizer.convert_tokens_to_ids('no')
        for start in range(0,len(sequences),2):
            inputs=_tokenizer.pad({'input_ids':sequences[start:start+2]},padding=True,return_tensors='pt').to(_model.device)
            with torch.inference_mode():
                # Only yes/no at the last token is needed; avoid a full vocabulary
                # projection for every source token and an unused generation cache.
                logits=_model(**inputs,logits_to_keep=1,use_cache=False).logits[:,-1,:]
                scores=torch.softmax(logits[:,[no,yes]].float(),dim=-1)[:,1].tolist()
            rows.extend({'index':start+i,'relevance_score':score} for i,score in enumerate(scores))
        rows=sorted(rows,key=lambda r:-r['relevance_score'])[:value.top_n]
        if value.return_documents:
            for row in rows:row['document']={'text':value.documents[row['index']]}
        return {'model':MODEL_ID,'results':rows}
    finally:_lock.release()
