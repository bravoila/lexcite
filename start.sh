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
docker compose logs --tail=20 lexcite
echo '服务就绪后，未初始化的实例请读取一次性初始化码（不会写入日志）：'
echo '  docker compose exec lexcite cat data/setup-code.txt'
echo '已经初始化或已设置初始密码时没有该文件，直接用工作空间密码登录。'
