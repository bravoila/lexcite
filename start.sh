#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if ! command -v docker >/dev/null 2>&1; then
  echo '请先安装并启动 Docker Desktop / Docker Engine。详见 docs/DEPLOYMENT.md。'
  exit 1
fi
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
fi
docker compose up -d --build
echo '法据已启动：http://localhost:8080'
echo '首次打开页面需要初始化码，请在以下日志中查看：'
docker compose logs --tail=20 lexcite
