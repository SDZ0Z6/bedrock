@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
rem 不装包也能直接跑；装过 pip install -e . 的话这行也无害
set PYTHONPATH=%~dp0src
echo 正在启动 Bedrock 成本监控平台...
python -m bedrock_cost
pause
