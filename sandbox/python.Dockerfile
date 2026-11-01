# 빌드: docker build -t desktop-mcp-sandbox:py312 -f sandbox/python.Dockerfile .
# 빌드 컨텍스트에서 쓰는 파일은 requirements.txt뿐이다(.dockerignore 참고).
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# 대괄호 패턴은 requirements.txt가 없어도 COPY가 실패하지 않게 한다.
COPY requirements.tx[t] /tmp/req/
RUN if [ -f /tmp/req/requirements.txt ]; then pip install --no-cache-dir -r /tmp/req/requirements.txt; fi \
    && pip install --no-cache-dir pytest \
    && rm -rf /tmp/req

WORKDIR /work
