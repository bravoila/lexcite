"""Exercise actual HTTP payloads without model keys or pretrained weights."""
import json
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import pytest
from app import llm,retrieval_services as rs


@pytest.fixture
def wire():
    calls=[];reply={}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            value=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path,value,self.headers.get('Authorization')))
            if reply.get('body') is not None:data=reply['body']
            elif self.path.endswith('/embed'):data={'embeddings':[[3.,4.] for _ in value['input']]}
            elif self.path.endswith('/embeddings'):data={'data':[{'index':i,'embedding':[3.,4.]} for i in reversed(range(len(value['input'])))]}
            else:
                docs=value.get('input',value)['documents'];items=[{'index':i,'relevance_score':i/10} for i in range(len(docs))]
                data={'output':{'results':items}} if '/services/' in self.path else {'results':items}
            body=json.dumps(data).encode();self.send_response(reply.get('status',200));self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    # The loopback numeric address is blocked by the SSRF endpoint validator; the
    # mock really lives on this machine, so use the whitelisted localhost name.
    yield f'http://localhost:{server.server_port}',calls,reply
    server.shutdown();server.server_close();thread.join(2)


@pytest.mark.parametrize('provider',['custom','dashscope','siliconflow','ollama'])
def test_embedding_adapter_paths_and_query_recipe(client,wire,provider):
    base,calls,_=wire;cfg=rs.Service(provider=provider,base_url=base+'/v1',model='model-fixture',api_key='separate-key',query_instruction='Find laws').model_dump()
    assert rs.embed(['甲','乙'],query=True,cfg=cfg)==[[.6,.8],[.6,.8]]
    path,body,key=calls[0]
    assert path==('/api/embed' if provider=='ollama' else '/v1/embeddings')
    assert body['input'][0]=='Instruct: Find laws\nQuery: 甲'
    assert key=='Bearer separate-key'
    if provider=='ollama':assert body['truncate'] is False
    rs.embed(['法条原文'],cfg=cfg);assert calls[1][1]['input']==['法条原文']


@pytest.mark.parametrize('provider',['custom','siliconflow','dashscope'])
def test_rerank_adapter_paths_and_results(client,wire,provider):
    base,calls,_=wire;cfg=rs.Service(provider=provider,base_url=base+'/v1',model='rerank-fixture').model_dump()
    assert rs.rerank('问题',['甲','乙'],cfg=cfg)==[(1,.1),(0,0.)]
    path,body,_=calls[0]
    if provider=='dashscope':
        assert path=='/v1/services/rerank/text-rerank/text-rerank'
        assert body['parameters']['top_n']==2 and body['input']['query']=='问题'
    else:assert path=='/v1/rerank' and body['top_n']==2


@pytest.mark.parametrize('body',[{'data':[{'index':0,'embedding':[1,2]},{'index':0,'embedding':[1,2]}]}, {'data':[{'index':None,'embedding':[1,2]}]}, {'data':[{'index':False,'embedding':[1,2]}]}, {'data':[{'index':0,'embedding':[float('nan'),2]}]}])
def test_malformed_embedding_payload_rejected(client,wire,body):
    base,_,reply=wire;reply['body']=body;cfg=rs.Service(provider='custom',base_url=base,model='test').model_dump()
    with pytest.raises(llm.ModelError):rs.embed(['甲'],cfg=cfg)


def test_http_failure_redacts_provider_body_and_key(client,wire):
    base,_,reply=wire;reply.update(status=401,body={'error':{'code':'invalid_api_key','message':'Incorrect API key provided: super-secret'}})
    cfg=rs.Service(provider='custom',base_url=base,model='test',api_key='super-secret').model_dump()
    with pytest.raises(llm.ModelError) as e:rs.embed(['甲'],cfg=cfg)
    assert '401' in str(e.value) and 'invalid_api_key' in str(e.value)
    assert 'Incorrect API key provided' in str(e.value) and 'super-secret' not in str(e.value)


def test_call_respects_research_cancellation(client,wire):
    base,calls,_=wire;cfg=rs.Service(provider='custom',base_url=base,model='test').model_dump()
    def stop():raise InterruptedError()
    with pytest.raises(InterruptedError):rs.embed(['甲'],cfg=cfg,check=stop)
    assert not calls


@pytest.mark.parametrize('model',['qwen3-rerank','qwen3.7-text-rerank','gte-rerank-v2'])
@pytest.mark.parametrize('root',[
    '',
    '/api/v1',
    '/api/v1/',
    '/api/v1/services/rerank/text-rerank/text-rerank',
    '/compatible-mode/v1',
    '/compatible-api/v1',
    '/compatible-api/v1/reranks',
])
def test_aliyun_rerank_uses_native_api_and_preserves_host(client,wire,root,model):
    base,calls,_=wire
    cfg=rs.Service(provider='dashscope',base_url=base+root,model=model).model_dump()
    original=dict(cfg)
    scores=rs.rerank('问题',['甲','乙'],cfg=cfg)
    path,body,_=calls[0]
    assert path=='/api/v1/services/rerank/text-rerank/text-rerank'
    assert body=={'model':model,'input':{'query':'问题','documents':['甲','乙']},'parameters':{'top_n':2}}
    assert scores==[(1,.1),(0,0.)]
    assert cfg==original and len(calls)==1


def test_rerank_test_api_uses_saved_legacy_address_with_native_protocol(client,wire):
    base,calls,_=wire
    value=rs.RetrievalSettings().model_dump();value['auto_index']=False
    value['reranker'].update(provider='dashscope',base_url=base+'/compatible-api/v1',model='qwen3-rerank')
    assert client.post('/api/retrieval/settings',json=value).status_code==200
    response=client.post('/api/retrieval/test/reranker')
    assert response.status_code==200,response.text
    assert response.json()['ok']
    assert calls[0][0]=='/api/v1/services/rerank/text-rerank/text-rerank'


def test_rerank_404_shows_effective_path_without_exposing_secrets(client,wire):
    base,calls,reply=wire;reply.update(status=404,body={})
    cfg=rs.Service(provider='dashscope',base_url=base+'/compatible-mode/v1',model='qwen3-rerank',api_key='private-test-key').model_dump()
    with pytest.raises(llm.ModelError) as caught:
        rs.rerank('问题',['甲','乙'],cfg=cfg)
    assert caught.value.status_code==404
    assert '/api/v1/services/rerank/text-rerank/text-rerank' in str(caught.value)
    assert 'private-test-key' not in str(caught.value)
    assert len(calls)==1
