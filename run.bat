@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
echo 正在启动 Bedrock 成本监控平台...
python app.py
pause
