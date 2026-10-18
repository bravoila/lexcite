@echo off
chcp 65001 >nul
cd /d "%~dp0"
where docker >nul 2>nul
if errorlevel 1 (
  echo 请先安装并启动 Docker Desktop。
  pause
  exit /b 1
)
if not exist .env copy .env.example .env >nul
docker compose up -d --build
if errorlevel 1 (
  echo 启动失败，请查看上方错误。
  pause
  exit /b 1
)
echo 打开 http://localhost:8080 ，首次使用请复制下方初始化码。
docker compose logs --tail=20 lexcite
start http://localhost:8080
pause
