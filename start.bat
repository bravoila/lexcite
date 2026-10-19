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
docker compose logs --tail=20 lexcite
echo 打开 http://localhost:8080 。服务就绪后，未初始化的实例请运行：
echo   docker compose exec lexcite cat data/setup-code.txt
echo 初始化码不会写入日志。已初始化或已设置初始密码时没有该文件，直接用工作空间密码登录。
start http://localhost:8080
pause
