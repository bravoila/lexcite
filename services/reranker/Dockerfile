FROM python:3.12-slim
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ENV PYTHONUNBUFFERED=1 HF_HOME=/models/cache
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir torch==2.8.0 --index-url ${TORCH_INDEX_URL} && pip install --no-cache-dir -r requirements.txt && useradd --create-home --uid 10001 reranker && mkdir -p /models && chown -R reranker:reranker /models /srv
COPY server.py .
USER reranker
EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=5s --start-period=600s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8081/healthz',timeout=3)"
CMD ["python","-m","uvicorn","server:app","--host","0.0.0.0","--port","8081","--workers","1"]
